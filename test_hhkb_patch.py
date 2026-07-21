#!/usr/bin/env python3
"""Tests for hhkb_patch.py.

Two layers:
  * Unit tests exercise the pure byte-manipulation helpers against synthetic
    BOM/CSI fixtures (no external tools, run everywhere).
  * Integration tests drive the full CLI (`main`) against the real Assets.car,
    using the system `assetutil`. They self-skip when the catalog or assetutil
    is unavailable.

Run with:  pytest test_hhkb_patch.py
"""

import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import struct
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hhkb_patch as pc

HAVE_ASSETUTIL = shutil.which("assetutil") is not None
HAVE_CAR = os.path.isfile(pc.DEFAULT_CAR)
INTEGRATION = HAVE_ASSETUTIL and HAVE_CAR
INTEGRATION_REASON = (
    f"needs assetutil ({HAVE_ASSETUTIL}) and {pc.DEFAULT_CAR} ({HAVE_CAR})"
)


def rb(path):
    with open(path, "rb") as fh:
        return fh.read()


def wb(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #
def make_csi_blob(payload, *, prefix=b"\x00" * 16, suffix=b"\x00" * 8, dup_region=False):
    """Build a minimal CSI block: [header ... region_len ...][DWAR][data].

    The header embeds the "region length" field (bytes from DWAR to end), which
    build_new_block must find and update. `dup_region` embeds it twice to force
    the ambiguity guard.
    """
    dwar = b"DWAR" + b"\x00\x00\x00\x00" + struct.pack("<I", len(payload)) + payload
    region = len(dwar)
    region_field = struct.pack("<I", region)
    header = prefix + region_field + (region_field if dup_region else b"") + suffix
    return header + dwar


def make_bom(blocks):
    """Build a minimal BOMStore buffer holding `blocks` (index 0 is the null slot)."""
    data = bytearray(b"\x00" * 32)
    pointers = [(0, 0)]
    for block in blocks:
        pointers.append((len(data), len(block)))
        data += block
    index_off = len(data)
    idx = bytearray(struct.pack(">I", len(pointers)))
    for addr, length in pointers:
        idx += struct.pack(">II", addr, length)
    data += idx
    struct.pack_into(
        ">8sIIIIII", data, 0,
        b"BOMStore", 1, len(blocks), index_off, len(idx), 0, 0,
    )
    return bytes(data), index_off, pointers


# --------------------------------------------------------------------------- #
# Unit tests: pure helpers
# --------------------------------------------------------------------------- #
def test_find_asset_entry_single_match():
    info = [
        {"AssetStorageVersion": "x"},
        {"Name": "Foo", "AssetType": "Image"},
        {"Name": "KeyboardDatalist", "AssetType": "Data", "SHA1Digest": "ab"},
    ]
    entry = pc.find_asset_entry(info, "KeyboardDatalist")
    assert entry["SHA1Digest"] == "ab"


def test_find_asset_entry_missing_raises():
    info = [{"Name": "Other", "AssetType": "Data"}]
    with pytest.raises(SystemExit):
        pc.find_asset_entry(info, "KeyboardDatalist")


def test_find_asset_entry_wrong_type_ignored():
    info = [{"Name": "KeyboardDatalist", "AssetType": "Image"}]
    with pytest.raises(SystemExit):
        pc.find_asset_entry(info, "KeyboardDatalist")


def test_find_asset_entry_multiple_raises():
    info = [
        {"Name": "Dup", "AssetType": "Data"},
        {"Name": "Dup", "AssetType": "Data"},
    ]
    with pytest.raises(SystemExit):
        pc.find_asset_entry(info, "Dup")


def test_find_unique_u32_offsets():
    buf = b"\x00\x00\x13\x00\x00\x00\x99\x13\x00\x00\x00"
    # value 19 (0x13) as u32le appears at offsets 2 and 7
    offs = pc.find_unique_u32le(buf, 0, len(buf), 19)
    assert offs == [2, 7]


def test_find_unique_u32_respects_bounds():
    buf = struct.pack("<I", 5) + struct.pack("<I", 5)
    assert pc.find_unique_u32le(buf, 4, 8, 5) == [4]
    assert pc.find_unique_u32le(buf, 0, 4, 5) == [0]


def test_parse_bom_roundtrip():
    data, index_off, pointers = make_bom([b"AAAA", b"BBBBBB"])
    got_off, got_ptrs = pc.parse_bom_block_table(data)
    assert got_off == index_off
    assert got_ptrs == pointers
    assert got_ptrs[0] == (0, 0)


def test_parse_bom_rejects_non_bom():
    with pytest.raises(SystemExit):
        pc.parse_bom_block_table(b"NOTABOM!" + b"\x00" * 64)


def test_parse_bom_rejects_truncated_header():
    with pytest.raises(SystemExit):
        pc.parse_bom_block_table(b"BOMStore")


def test_parse_bom_rejects_out_of_bounds_table():
    data, _, _ = make_bom([b"block"])
    corrupted = bytearray(data)
    struct.pack_into(">I", corrupted, 16, len(corrupted) + 10)
    with pytest.raises(SystemExit):
        pc.parse_bom_block_table(corrupted)


def test_parse_bom_rejects_truncated_pointer_list():
    data, index_off, _ = make_bom([b"block"])
    corrupted = bytearray(data)
    struct.pack_into(">I", corrupted, index_off, 100)
    with pytest.raises(SystemExit):
        pc.parse_bom_block_table(corrupted)


@pytest.fixture
def find_block_fixture():
    blocks = [b"first-block", b"the-target-block!!", b"third"]
    data, _, pointers = make_bom(blocks)
    target = hashlib.sha256(blocks[1]).hexdigest()
    return blocks, data, pointers, target


def test_find_block_by_digest_finds_correct_index(find_block_fixture):
    blocks, data, pointers, target = find_block_fixture
    idx, addr, length = pc.find_block_by_digest(data, pointers, target)
    assert idx == 2  # index 0 null, block 0 -> ptr 1, block 1 -> ptr 2
    assert data[addr : addr + length] == blocks[1]


def test_find_block_by_digest_expected_len_tiebreaker(find_block_fixture):
    blocks, data, pointers, target = find_block_fixture
    idx, _, length = pc.find_block_by_digest(
        data, pointers, target, expected_len=len(blocks[1])
    )
    assert idx == 2
    # A unique digest remains authoritative when metadata length differs.
    idx, _, _ = pc.find_block_by_digest(
        data, pointers, target, expected_len=3
    )
    assert idx == 2


def test_find_block_by_digest_not_found(find_block_fixture):
    _, data, pointers, _ = find_block_fixture
    with pytest.raises(SystemExit):
        pc.find_block_by_digest(data, pointers, hashlib.sha256(b"nope").hexdigest())


def test_find_block_by_digest_ambiguous():
    data, _, pointers = make_bom([b"same", b"same"])
    digest = hashlib.sha256(b"same").hexdigest()
    with pytest.raises(SystemExit):
        pc.find_block_by_digest(data, pointers, digest)


def test_extract_payload_extracts():
    blob = make_csi_blob(b'{"hello":1}')
    assert pc.extract_payload(blob) == b'{"hello":1}'


def test_extract_payload_no_dwar():
    with pytest.raises(SystemExit):
        pc.extract_payload(b"no marker here")


def test_extract_payload_truncated_header():
    with pytest.raises(SystemExit):
        pc.extract_payload(b"prefixDWAR\x00")


def test_extract_payload_rejects_declared_length_past_end():
    blob = b"\x00" * 8 + b"DWAR" + b"\x00" * 4 + struct.pack("<I", 100) + b"abc"
    with pytest.raises(SystemExit):
        pc.extract_payload(blob)


@pytest.fixture
def valid_datalist():
    return {
        "PD-TEST": {
            "typeNumber": "PD-TEST",
            "layoutType": 1,
            "colorType": 0,
            "series": 1,
            "layoutTypeName": 1,
            "postfix": "",
            "isKeymapChangeable": True,
            "firmTypeNumber": "TEST01",
            "firmDataSize": 1234,
        }
    }


def test_datalist_validation_accepts_valid_datalist(valid_datalist):
    assert pc.validate_datalist(valid_datalist) is valid_datalist


def test_datalist_validation_rejects_non_object():
    with pytest.raises(SystemExit):
        pc.validate_datalist([])


def test_datalist_validation_rejects_key_mismatch(valid_datalist):
    valid_datalist["PD-TEST"]["typeNumber"] = "PD-OTHER"
    with pytest.raises(SystemExit):
        pc.validate_datalist(valid_datalist)


def test_datalist_validation_rejects_missing_field(valid_datalist):
    del valid_datalist["PD-TEST"]["firmDataSize"]
    with pytest.raises(SystemExit):
        pc.validate_datalist(valid_datalist)


def test_datalist_validation_rejects_wrong_field_type(valid_datalist):
    valid_datalist["PD-TEST"]["layoutType"] = True
    with pytest.raises(SystemExit):
        pc.validate_datalist(valid_datalist)


def test_datalist_validation_bundled_datalist_is_valid():
    value = pc.parse_json_payload(
        rb(pc.DEFAULT_NEW_JSON), pc.DEFAULT_NEW_JSON, datalist=True
    )
    assert len(value) == 68


@pytest.fixture
def windows_flow_setup(tmp_path, monkeypatch):
    target = tmp_path / "keyboardDataList.json"
    payload = rb(pc.DEFAULT_NEW_JSON)
    value = json.loads(payload)
    monkeypatch.chdir(tmp_path)
    return target, payload, value


def test_windows_flow_missing_target_is_not_created(windows_flow_setup):
    target, payload, value = windows_flow_setup
    with pytest.raises(SystemExit):
        pc.run_windows(payload, value, datalist=str(target))
    assert not target.exists()


def test_windows_flow_patch_is_verified_and_backed_up(windows_flow_setup):
    target, payload, value = windows_flow_setup
    original = b'{"old": true}'
    target.write_bytes(original)
    pc.run_windows(
        payload,
        value,
        new_json=pc.DEFAULT_NEW_JSON,
        datalist=str(target),
    )
    assert target.read_bytes() == payload
    assert target.with_name("keyboardDataList.json.bak").read_bytes() == original


def test_build_new_block_grow_shrink_same():
    blob = make_csi_blob(b'{"x":1}')  # 7-byte payload
    for new in (b'{"xy":2222}', b"{}", b'{"x":1}'):
        new_blob, old_len, new_len = pc.build_new_block(blob, new)
        assert old_len == 7
        assert new_len == len(new)
        assert pc.extract_payload(new_blob) == new
        dwar = new_blob.find(b"DWAR")
        # DWAR length field updated
        assert struct.unpack("<I", new_blob[dwar + 8 : dwar + 12])[0] == len(new)
        # region-length field updated to (len(blob) - dwar)
        assert struct.pack("<I", len(new_blob) - dwar) in new_blob[:dwar]


def test_build_new_block_no_dwar():
    with pytest.raises(SystemExit):
        pc.build_new_block(b"x" * 32, b"data")


def test_build_new_block_payload_not_to_end():
    blob = make_csi_blob(b'{"x":1}') + b"\xff"  # trailing junk breaks length invariant
    with pytest.raises(SystemExit):
        pc.build_new_block(blob, b"new")


def test_build_new_block_ambiguous_region_field():
    blob = make_csi_blob(b'{"x":1}', dup_region=True)
    with pytest.raises(SystemExit):
        pc.build_new_block(blob, b'{"y":2}')


# --------------------------------------------------------------------------- #
# Integration helpers
# --------------------------------------------------------------------------- #
def run_main(argv, cwd=None):
    """Invoke hhkb_patch.main() in-process; return captured stdout."""
    old_argv = sys.argv
    old_cwd = os.getcwd()
    sys.argv = ["hhkb_patch.py"] + argv
    buf = io.StringIO()
    try:
        if cwd is not None:
            os.chdir(cwd)
        with contextlib.redirect_stdout(buf):
            pc.main()
    finally:
        os.chdir(old_cwd)
        sys.argv = old_argv
    return buf.getvalue()


def assetutil_info(path):
    return pc.run_assetutil_info(path)


def kbd_entry(path):
    return pc.find_asset_entry(assetutil_info(path), "KeyboardDatalist")


def extract_from_car(path):
    data = bytearray(rb(path))
    entry = kbd_entry(path)
    _, pointers = pc.parse_bom_block_table(data)
    _, addr, length = pc.find_block_by_digest(
        data, pointers, entry["SHA1Digest"], entry.get("SizeOnDisk")
    )
    return pc.extract_payload(bytes(data[addr : addr + length]))


def strip_variable(info):
    """Drop the KeyboardDatalist data entry and file-mtime Timestamp for comparison."""
    out = []
    for e in info:
        if isinstance(e, dict) and e.get("Name") == "KeyboardDatalist" and e.get("AssetType") == "Data":
            continue
        if isinstance(e, dict):
            e = {k: v for k, v in e.items() if k != "Timestamp"}
        out.append(e)
    return out


@pytest.fixture(scope="module")
def integration_data():
    if not INTEGRATION:
        pytest.skip(INTEGRATION_REASON)
    orig_payload = extract_from_car(pc.DEFAULT_CAR)
    orig_entry = kbd_entry(pc.DEFAULT_CAR)
    return orig_payload, orig_entry


@pytest.fixture
def integration_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _modified_json(orig_payload, extra_entries=1):
    d = json.loads(orig_payload)
    base = next(iter(d.values()))
    for i in range(extra_entries):
        e = copy.deepcopy(base)
        e["typeNumber"] = f"PD-TEST{i:03d}"
        d[e["typeNumber"]] = e
    return json.dumps(d, indent=2).encode()


def _run(argv, tmp_path):
    return run_main(argv, cwd=str(tmp_path))


# ---- export ---------------------------------------------------------- #
@pytest.mark.skipif(not INTEGRATION, reason=INTEGRATION_REASON)
def test_export_matches_direct_extract(integration_data, integration_tmp_dir):
    orig_payload, _ = integration_data
    out = integration_tmp_dir / "exported.json"
    _run(["--export", str(out)], integration_tmp_dir)
    assert out.read_bytes() == orig_payload
    assert len(json.loads(orig_payload)) > 0


@pytest.mark.skipif(not INTEGRATION, reason=INTEGRATION_REASON)
def test_export_without_patch_writes_no_car(integration_tmp_dir):
    _run(["--export", str(integration_tmp_dir / "e.json")], integration_tmp_dir)
    assert not (integration_tmp_dir / "Assets.car").exists()


# ---- identity patch -------------------------------------------------- #
@pytest.mark.skipif(not INTEGRATION, reason=INTEGRATION_REASON)
def test_patch_with_original_is_byte_identical_block(integration_data, integration_tmp_dir):
    orig_payload, orig_entry = integration_data
    src = integration_tmp_dir / "orig.json"
    src.write_bytes(orig_payload)
    _run([str(src)], integration_tmp_dir)
    out = integration_tmp_dir / "Assets.car"
    assert kbd_entry(str(out))["SHA1Digest"].lower() == orig_entry["SHA1Digest"].lower()


# ---- modified patch -------------------------------------------------- #
@pytest.mark.skipif(not INTEGRATION, reason=INTEGRATION_REASON)
def test_patch_modified_roundtrips(integration_data, integration_tmp_dir):
    orig_payload, _ = integration_data
    new_bytes = _modified_json(orig_payload, extra_entries=3)
    src = integration_tmp_dir / "new.json"
    src.write_bytes(new_bytes)
    _run([str(src)], integration_tmp_dir)
    out = integration_tmp_dir / "Assets.car"
    assert kbd_entry(str(out))["Data Length"] == len(new_bytes)
    assert extract_from_car(str(out)) == new_bytes
    assert len(json.loads(extract_from_car(str(out)))) == len(json.loads(orig_payload)) + 3


@pytest.mark.skipif(not INTEGRATION, reason=INTEGRATION_REASON)
def test_other_assets_untouched(integration_data, integration_tmp_dir):
    orig_payload, _ = integration_data
    src = integration_tmp_dir / "new.json"
    src.write_bytes(_modified_json(orig_payload, extra_entries=5))
    _run([str(src)], integration_tmp_dir)
    out = integration_tmp_dir / "Assets.car"
    assert strip_variable(assetutil_info(pc.DEFAULT_CAR)) == strip_variable(assetutil_info(str(out)))


# ---- backups --------------------------------------------------------- #
@pytest.mark.skipif(not INTEGRATION, reason=INTEGRATION_REASON)
def test_default_backup_created(integration_data, integration_tmp_dir):
    orig_payload, _ = integration_data
    src = integration_tmp_dir / "new.json"
    src.write_bytes(orig_payload)
    _run([str(src)], integration_tmp_dir)
    backup = integration_tmp_dir / "Assets.car.bak"
    assert backup.is_file()
    assert backup.read_bytes() == rb(pc.DEFAULT_CAR)


@pytest.mark.skipif(not INTEGRATION, reason=INTEGRATION_REASON)
def test_existing_backup_preserved(integration_data, integration_tmp_dir):
    orig_payload, _ = integration_data
    src = integration_tmp_dir / "new.json"
    src.write_bytes(orig_payload)
    backup = integration_tmp_dir / "Assets.car.bak"
    backup.write_bytes(b"SENTINEL")
    _run([str(src)], integration_tmp_dir)
    assert backup.read_bytes() == b"SENTINEL"
    stamped = [
        name.name
        for name in integration_tmp_dir.iterdir()
        if name.name.startswith("Assets.car.") and name.name.endswith(".bak") and name.name != "Assets.car.bak"
    ]
    assert len(stamped) == 1
    assert (integration_tmp_dir / stamped[0]).read_bytes() == rb(pc.DEFAULT_CAR)


# ---- validation & errors -------------------------------------------- #
@pytest.mark.skipif(not INTEGRATION, reason=INTEGRATION_REASON)
def test_invalid_json_rejected(integration_tmp_dir):
    src = integration_tmp_dir / "bad.json"
    src.write_bytes(b"this is not json")
    with pytest.raises(SystemExit):
        _run([str(src)], integration_tmp_dir)


@pytest.mark.skipif(not INTEGRATION, reason=INTEGRATION_REASON)
def test_default_patches_with_bundled_json_in_temp_cwd(integration_tmp_dir):
    _run([], integration_tmp_dir)
    out = integration_tmp_dir / "Assets.car"
    assert out.is_file()
    assert extract_from_car(str(out)) == rb(pc.DEFAULT_NEW_JSON)
    assert (integration_tmp_dir / "Assets.car.bak").is_file()


@pytest.mark.skipif(not INTEGRATION, reason=INTEGRATION_REASON)
def test_verification_failure_does_not_replace_output(integration_data, integration_tmp_dir):
    orig_payload, _ = integration_data
    src = integration_tmp_dir / "new.json"
    src.write_bytes(orig_payload)
    out = integration_tmp_dir / "Assets.car"
    sentinel = b"existing destination"
    out.write_bytes(sentinel)
    real_info = pc.run_assetutil_info

    def fail_for_temporary_output(path):
        if path == pc.DEFAULT_CAR:
            return real_info(path)
        pc.fail("simulated verification failure")

    with mock.patch.object(pc, "run_assetutil_info", side_effect=fail_for_temporary_output):
        with pytest.raises(SystemExit):
            _run([str(src)], integration_tmp_dir)
    assert out.read_bytes() == sentinel


@pytest.mark.skipif(not INTEGRATION, reason=INTEGRATION_REASON)
def test_export_then_patch_exports_pre_patch(integration_data, integration_tmp_dir):
    orig_payload, _ = integration_data
    src = integration_tmp_dir / "new.json"
    src.write_bytes(_modified_json(orig_payload, extra_entries=1))
    exported = integration_tmp_dir / "before.json"
    _run([str(src), "--export", str(exported)], integration_tmp_dir)
    assert exported.read_bytes() == orig_payload

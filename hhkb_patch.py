#!/usr/bin/env python3
"""Replace a single JSON `Data` asset inside a compiled Apple asset catalog (.car).

There is no off-the-shelf tool that edits a compiled `.car` in place (Apple's only
writer, `actool`, needs the full original `.xcassets`). This script instead relies on
the system `assetutil` to *locate and verify* the target asset, then performs a
surgical binary patch of just that one rendition, leaving every other asset untouched.

How the patch works
-------------------
A `.car` is a BOM container: a header, a set of data "blocks", and a block table of
(absolute-offset, length) pointers. A `Data` asset is one block that holds a `CTSI`
("ISTC") CSI header followed by a `RAWD` ("DWAR") payload wrapper:

    ... CSI header ...
    [DWAR][flags u32][rawLength u32][ raw bytes ]

Replacing the raw bytes only requires:
  1. writing the new bytes into a rebuilt block,
  2. updating the `DWAR` rawLength field,
  3. updating the CSI "rawdata region length" field (== 12 + rawLength),
  4. appending the rebuilt block at EOF and repointing this block's block-table
     entry (address + length) to it. All other offsets stay valid; the old bytes
     become harmless dead space.

Nothing in the format stores/validates a checksum of the data, so no digest fixups
are needed. `assetutil` re-reads the output at the end as a self-check.
"""

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time

DEFAULT_CAR = "/Applications/hhkb-keymap-tool.app/Contents/Resources/Assets.car"
DEFAULT_ASSET = "KeyboardDatalist"
DEFAULT_OUTPUT = "Assets.car"
# The replacement JSON bundled with this project, used to patch by default.
DEFAULT_NEW_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "KeyboardDatalist.json"
)
DWAR_MAGIC = b"DWAR"  # 'RAWD' as stored little-endian; marks the csi_rawdata payload
BOM_MAGIC = b"BOMStore"

IS_WINDOWS = os.name == "nt"
# On Windows the Keymap Tool keeps the keymap as a plain JSON file next to the app,
# so there is no .car to patch -- we just back up and copy the file into place.
WINDOWS_DATALIST = (
    r"C:\Program Files\PFU\Happy Hacking Keyboard Keymap Tool\keyboardDataList.json"
)

KEYBOARD_FIELDS = {
    "typeNumber": str,
    "layoutType": int,
    "colorType": int,
    "series": int,
    "layoutTypeName": int,
    "postfix": str,
    "isKeymapChangeable": bool,
    "firmTypeNumber": str,
    "firmDataSize": int,
}


def fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def validate_datalist(value):
    """Validate the structure expected by the HHKB KeyboardDatalist asset."""
    if not isinstance(value, dict) or not value:
        fail("KeyboardDatalist must be a non-empty JSON object")
    for key, entry in value.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            fail("each KeyboardDatalist entry must be an object keyed by type number")
        missing = [field for field in KEYBOARD_FIELDS if field not in entry]
        if missing:
            fail(f"KeyboardDatalist entry {key!r} is missing fields: {', '.join(missing)}")
        if entry["typeNumber"] != key:
            fail(
                f"KeyboardDatalist key {key!r} does not match "
                f"typeNumber {entry['typeNumber']!r}"
            )
        for field, expected_type in KEYBOARD_FIELDS.items():
            field_value = entry[field]
            if expected_type is int:
                valid = isinstance(field_value, int) and not isinstance(field_value, bool)
            else:
                valid = isinstance(field_value, expected_type)
            if not valid:
                fail(
                    f"KeyboardDatalist entry {key!r} field {field!r} must be "
                    f"{expected_type.__name__}"
                )
    return value


def parse_json_payload(payload, source, *, datalist=False):
    """Parse JSON bytes and optionally enforce the KeyboardDatalist schema."""
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"{source} is not valid JSON: {exc}")
    if datalist:
        validate_datalist(value)
    return value


def print_datalist_diff(old_payload, new_value):
    """Print a compact replacement summary without blocking non-interactive use."""
    try:
        old_value = json.loads(old_payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("warning: existing KeyboardDatalist is not valid JSON; diff unavailable", file=sys.stderr)
        return
    if not isinstance(old_value, dict) or not isinstance(new_value, dict):
        return
    old_keys = set(old_value)
    new_keys = set(new_value)
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed = sorted(key for key in old_keys & new_keys if old_value[key] != new_value[key])
    print(f"datalist changes: +{len(added)} added, -{len(removed)} removed, ~{len(changed)} changed")
    if removed:
        preview = ", ".join(removed[:8])
        suffix = f", … (+{len(removed) - 8} more)" if len(removed) > 8 else ""
        print(
            f"warning: replacement removes existing keyboard types: {preview}{suffix}",
            file=sys.stderr,
        )


def files_equal(path_a, path_b):
    """Compare two regular files without loading both into memory."""
    try:
        if os.path.getsize(path_a) != os.path.getsize(path_b):
            return False
        with open(path_a, "rb") as left, open(path_b, "rb") as right:
            while True:
                left_chunk = left.read(1024 * 1024)
                right_chunk = right.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


def local_backup_path(source):
    """Backup path in the current directory: ./<basename>.bak."""
    return os.path.join(os.getcwd(), os.path.basename(source) + ".bak")


def copy_backup(source, backup):
    """Create a backup; if a stale one exists, keep it and also save the current source."""
    if os.path.abspath(backup) == os.path.abspath(source):
        fail("backup path is the same as the source")
    if os.path.exists(backup):
        if files_equal(source, backup):
            print(f"backup already exists, keeping it: {backup}")
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if backup.endswith(".bak"):
            alt = f"{backup[:-4]}.{stamp}.bak"
        else:
            alt = f"{backup}.{stamp}"
        print(
            f"warning: existing backup differs from the current source, keeping it: {backup}",
            file=sys.stderr,
        )
        try:
            shutil.copy2(source, alt)
        except OSError as exc:
            fail(f"could not write backup {alt}: {exc}")
        print(f"backup of current source written: {alt}")
        return
    try:
        shutil.copy2(source, backup)
    except OSError as exc:
        fail(f"could not write backup {backup}: {exc}")
    print(f"backup of source written: {backup}")


def write_temp_file(destination, data, *, mode_source=None):
    """Write and fsync a temporary sibling ready for an atomic replacement."""
    directory = os.path.dirname(os.path.abspath(destination)) or "."
    suffix = os.path.splitext(destination)[1] or ".tmp"
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{os.path.basename(destination)}.",
            suffix=suffix,
            dir=directory,
            delete=False,
        ) as fh:
            temp_path = fh.name
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode_source is not None:
            shutil.copystat(mode_source, temp_path)
        return temp_path
    except OSError as exc:
        if "temp_path" in locals():
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        fail(f"could not write temporary output beside {destination}: {exc}")


def replace_with_temp(temp_path, destination, *, permission_hint=""):
    try:
        os.replace(temp_path, destination)
    except OSError as exc:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        hint = f" ({permission_hint})" if permission_hint else ""
        fail(f"could not replace {destination}: {exc}{hint}")

def run_assetutil_info(car_path):
    """Return the parsed `assetutil --info` listing for a .car (uses the system tool)."""
    if shutil.which("assetutil") is None:
        fail("`assetutil` not found (it ships with macOS at /usr/bin/assetutil)")
    proc = subprocess.run(
        ["assetutil", "--info", car_path],
        capture_output=True,
    )
    if proc.returncode != 0:
        fail(f"assetutil failed: {proc.stderr.decode(errors='replace').strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        fail(f"could not parse assetutil output: {exc}")


def find_asset_entry(info, name):
    matches = [
        e
        for e in info
        if isinstance(e, dict)
        and e.get("Name") == name
        and e.get("AssetType") == "Data"
    ]
    if not matches:
        names = sorted(
            e.get("Name")
            for e in info
            if isinstance(e, dict) and e.get("AssetType") == "Data"
        )
        fail(
            f"no Data asset named {name!r} in catalog. Data assets present: {names}"
        )
    if len(matches) > 1:
        fail(f"multiple Data assets named {name!r}; cannot disambiguate")
    return matches[0]


def parse_bom_block_table(data):
    """Parse the BOM header + block-table pointer list (all big-endian)."""
    if len(data) < 32:
        fail("input is too short to contain a BOM header")
    if data[:8] != BOM_MAGIC:
        fail("input is not a BOM/.car file (missing 'BOMStore' magic)")
    _magic, _ver, _nblk, index_off, index_len, _vars_off, _vars_len = struct.unpack(
        ">8sIIIIII", data[:32]
    )
    if index_off < 32 or index_off > len(data) - 4:
        fail(f"invalid BOM block-table offset: {index_off}")
    if index_len < 4 or index_off + index_len > len(data):
        fail(f"invalid BOM block-table length: {index_len}")
    count = struct.unpack(">I", data[index_off : index_off + 4])[0]
    table_end = index_off + 4 + count * 8
    if table_end > index_off + index_len or table_end > len(data):
        fail(f"BOM block table is truncated (count={count})")
    pointers = []
    pos = index_off + 4
    for _ in range(count):
        addr, length = struct.unpack(">II", data[pos : pos + 8])
        pointers.append((addr, length))
        pos += 8
    return index_off, pointers


def find_block_by_digest(data, pointers, digest_hex, expected_len=None):
    """Find a block by SHA-256, using expected length only to disambiguate."""
    target = digest_hex.lower()
    hits = []
    for idx, (addr, length) in enumerate(pointers):
        if addr == 0 or length == 0 or addr + length > len(data):
            continue
        if hashlib.sha256(data[addr : addr + length]).hexdigest() == target:
            hits.append((idx, addr, length))
    if not hits:
        fail("could not locate the asset's block by digest (catalog may be modified)")
    if len(hits) > 1 and expected_len is not None:
        length_hits = [hit for hit in hits if hit[2] == expected_len]
        if len(length_hits) == 1:
            return length_hits[0]
    if len(hits) > 1:
        fail("ambiguous: multiple blocks share the asset digest")
    return hits[0]


def find_unique_u32le(buf, start, end, value):
    needle = struct.pack("<I", value)
    offsets = []
    i = start
    while True:
        j = buf.find(needle, i, end)
        if j < 0:
            break
        offsets.append(j)
        i = j + 1
    return offsets


def extract_payload(blob):
    """Return the raw asset bytes stored inside a CSI block."""
    dwar = blob.find(DWAR_MAGIC)
    if dwar < 0:
        fail("no 'DWAR' (RAWD) payload marker in block; asset is not raw/uncompressed")
    if dwar + 12 > len(blob):
        fail("truncated DWAR payload header")
    old_len = struct.unpack("<I", blob[dwar + 8 : dwar + 12])[0]
    payload_start = dwar + 12
    if payload_start + old_len != len(blob):
        fail(
            "unexpected block layout: raw payload does not extend to end of block "
            f"(dwar={dwar}, old_len={old_len}, block_len={len(blob)})"
        )
    return bytes(blob[payload_start:])


def build_new_block(blob, new_payload):
    """Return a rebuilt CSI block with `new_payload` swapped in for the old raw data."""
    dwar = blob.find(DWAR_MAGIC)
    if dwar < 0:
        fail("no 'DWAR' (RAWD) payload marker in block; asset is not raw/uncompressed")
    if dwar + 12 > len(blob):
        fail("truncated DWAR payload header")

    old_len = struct.unpack("<I", blob[dwar + 8 : dwar + 12])[0]
    payload_start = dwar + 12
    if payload_start + old_len != len(blob):
        fail(
            "unexpected block layout: raw payload does not extend to end of block "
            f"(dwar={dwar}, old_len={old_len}, block_len={len(blob)})"
        )

    # CSI "rawdata region length" field == bytes from DWAR marker to end of block.
    old_region = len(blob) - dwar
    region_offsets = find_unique_u32le(blob, 0, dwar, old_region)
    if len(region_offsets) != 1:
        fail(
            "could not uniquely locate the CSI region-length field "
            f"(found {len(region_offsets)} candidates for value {old_region})"
        )
    region_off = region_offsets[0]

    new_len = len(new_payload)
    new_blob = bytearray(blob[:payload_start]) + new_payload
    struct.pack_into("<I", new_blob, dwar + 8, new_len)              # DWAR rawLength
    struct.pack_into("<I", new_blob, region_off, len(new_blob) - dwar)  # region length
    return bytes(new_blob), old_len, new_len


def run_windows(new_payload, new_value=None, *, export=None, new_json=None, datalist=None):
    """Windows flow: back up keyboardDataList.json, then copy the new JSON over it."""
    target = datalist if datalist is not None else WINDOWS_DATALIST

    if export:
        if not os.path.isfile(target):
            fail(f"keyboardDataList.json not found: {target}")
        try:
            with open(target, "rb") as fh:
                current = fh.read()
            with open(export, "wb") as fh:
                fh.write(current)
        except OSError as exc:
            fail(f"could not export keymap to {export}: {exc}")
        print(f"exported keymap: {len(current)} bytes -> {export}")

    if new_payload is None:
        return

    if not os.path.isfile(target):
        fail(f"keyboardDataList.json not found: {target}")

    try:
        with open(target, "rb") as fh:
            current = fh.read()
    except OSError as exc:
        fail(f"could not read {target}: {exc}")
    if new_value is not None:
        print_datalist_diff(current, new_value)

    copy_backup(target, local_backup_path(target))

    temp_path = write_temp_file(target, new_payload, mode_source=target)
    try:
        with open(temp_path, "rb") as fh:
            if fh.read() != new_payload:
                fail("post-write verification failed: keyboardDataList.json bytes differ")
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    replace_with_temp(temp_path, target, permission_hint="try running as Administrator")
    label = new_json or DEFAULT_NEW_JSON
    print(f"copied '{label}': {len(new_payload)} bytes -> {target}")


def main():
    ap = argparse.ArgumentParser(
        description="Patch the HHKB Keymap Tool keyboard list "
        "(Assets.car on macOS, keyboardDataList.json on Windows).",
    )
    ap.add_argument(
        "new_json",
        nargs="?",
        help="replacement JSON file "
        f"(default: bundled {os.path.basename(DEFAULT_NEW_JSON)}). "
        "Pass --export alone to only export without patching",
    )
    ap.add_argument(
        "--export",
        metavar="PATH",
        help="export the current keyboard list to PATH (can be combined with a patch)",
    )
    ap.add_argument(
        "--car",
        default=DEFAULT_CAR,
        help="source Assets.car to read/patch (macOS)",
    )
    ap.add_argument(
        "--datalist",
        default=WINDOWS_DATALIST,
        help="path to keyboardDataList.json to back up and overwrite (Windows)",
    )
    args = ap.parse_args()

    # Default action is to patch using the JSON bundled with this project. When only
    # --export is given (and no explicit replacement), just export without patching.
    new_json = args.new_json
    if not new_json and not args.export:
        new_json = DEFAULT_NEW_JSON

    new_payload = None
    new_value = None
    if new_json:
        if not os.path.isfile(new_json):
            fail(f"replacement JSON not found: {new_json}")
        try:
            with open(new_json, "rb") as fh:
                new_payload = fh.read()
        except OSError as exc:
            fail(f"could not read replacement JSON {new_json}: {exc}")
        new_value = parse_json_payload(new_payload, new_json, datalist=True)

    # On Windows the keymap is a plain JSON file, not a compiled .car: just copy it.
    if IS_WINDOWS:
        return run_windows(
            new_payload,
            new_value,
            export=args.export,
            new_json=new_json,
            datalist=args.datalist,
        )

    car = args.car
    if not os.path.isfile(car):
        fail(f"source catalog not found: {car}")

    # 1) Locate & verify the target asset with the system tool.
    info = run_assetutil_info(car)
    entry = find_asset_entry(info, DEFAULT_ASSET)
    compression = entry.get("Compression")
    if compression is not None and compression != "uncompressed":
        fail(f"asset {DEFAULT_ASSET!r} uses unsupported compression: {compression}")
    digest = entry.get("SHA1Digest")  # actually a SHA-256 hex digest of the CSI block
    if not isinstance(digest, str) or not digest:
        fail("assetutil metadata is missing the target asset digest")
    size_on_disk = entry.get("SizeOnDisk")
    old_data_len = entry.get("Data Length")

    try:
        with open(car, "rb") as fh:
            data = bytearray(fh.read())
    except OSError as exc:
        fail(f"could not read source catalog {car}: {exc}")

    index_off, pointers = parse_bom_block_table(data)
    block_idx, addr, blen = find_block_by_digest(data, pointers, digest, size_on_disk)
    blob = bytes(data[addr : addr + blen])

    # Optional: export the current asset contents.
    if args.export:
        payload = extract_payload(blob)
        try:
            with open(args.export, "wb") as fh:
                fh.write(payload)
        except OSError as exc:
            fail(f"could not export {DEFAULT_ASSET!r} to {args.export}: {exc}")
        print(f"exported '{DEFAULT_ASSET}': {len(payload)} bytes -> {args.export}")

    if new_payload is None:
        return

    if new_value is not None:
        print_datalist_diff(extract_payload(blob), new_value)

    # 2) Rebuild the block with the new payload.
    new_blob, old_len, new_len = build_new_block(blob, new_payload)
    if old_data_len is not None and old_len != old_data_len:
        fail(
            f"sanity check failed: DWAR length {old_len} != assetutil Data Length "
            f"{old_data_len}"
        )

    # 3) Append rebuilt block at EOF (4-byte aligned) and repoint its block-table entry.
    while len(data) % 4 != 0:
        data.append(0)
    new_addr = len(data)
    data += new_blob
    entry_off = index_off + 4 + block_idx * 8
    struct.pack_into(">II", data, entry_off, new_addr, len(new_blob))

    out_path = DEFAULT_OUTPUT
    copy_backup(car, local_backup_path(car))

    # 4) Write and verify a temporary sibling, then atomically replace the destination.
    temp_path = write_temp_file(out_path, data, mode_source=car)
    try:
        out_info = run_assetutil_info(temp_path)
        out_entry = find_asset_entry(out_info, DEFAULT_ASSET)
        out_digest = out_entry.get("SHA1Digest")
        if not isinstance(out_digest, str):
            fail("post-patch verification failed: asset digest missing")
        if hashlib.sha256(new_blob).hexdigest() != out_digest.lower():
            fail("post-patch verification failed: block digest mismatch")
        if out_entry.get("Data Length") != new_len:
            fail(
                f"post-patch verification failed: Data Length {out_entry.get('Data Length')}"
                f" != {new_len}"
            )
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    replace_with_temp(temp_path, out_path)

    print(
        f"patched '{DEFAULT_ASSET}': {old_len} -> {new_len} bytes "
        f"(block #{block_idx}); wrote {out_path}"
    )
    print("verified with assetutil OK")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""TS4 dynamic main-menu background updater.

Uses the newest save to identify the active household and zone. When possible it
matches that zone to the original lot in My Library/Tray and uses the large
Gallery opening image (group 0x00000003 .bpi). If the lot is not in Tray, it
falls back to the save's 0x0F lot thumbnail.
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageEnhance

DBPF_MAGIC = b"DBPF"
TYPE_ZONE_DATA = 0x00000006
TYPE_SAVEGAME_DATA = 0x0000000D
TYPE_SAVEGAME_LOT_THUMBNAIL1 = 0x0000000F
TYPE_DST_IMAGE = 0x00B2D882
EXPECTED_BG_WIDTH = 1920
EXPECTED_BG_HEIGHT = 1080
EXPECTED_DDS_SIZE = 2073728
EXPECTED_FOURCC = b"DXT5"
OUTPUT_PACKAGE_NAME = "ZZZ_Dynamic_Lot_Main_Menu.package"
TRAY_IMAGE_MAGIC = 0x3C0E789B4824FC8E
TRAY_IMAGE_KEY = bytes((0x41, 0x25, 0xE6, 0xCD, 0x47, 0xBA, 0xB2, 0x1A))


@dataclass
class DBPFEntry:
    type_id: int
    group_id: int
    instance_a: int
    instance_b: int
    offset: int
    file_size_raw: int
    mem_size: int
    compression_flags: int

    @property
    def file_size(self):
        return self.file_size_raw & 0x7FFFFFFF


class DBPF:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if self.data[:4] != DBPF_MAGIC:
            raise ValueError(f"Not a DBPF file: {self.path}")
        count = struct.unpack_from("<I", self.data, 0x24)[0]
        index_size = struct.unpack_from("<I", self.data, 0x2C)[0]
        index_offset = struct.unpack_from("<I", self.data, 0x40)[0]
        if index_offset + index_size > len(self.data):
            raise ValueError("DBPF index lies outside file")
        flags = struct.unpack_from("<I", self.data, index_offset)[0]
        if flags != 0:
            raise ValueError(f"Unsupported compact DBPF index flags 0x{flags:08X}")
        if index_size < 4 + count * 32:
            raise ValueError("Unexpected DBPF index size")
        self.entries = []
        pos = index_offset + 4
        for _ in range(count):
            self.entries.append(DBPFEntry(*struct.unpack_from("<8I", self.data, pos)))
            pos += 32

    def resources(self, type_id):
        return [e for e in self.entries if e.type_id == type_id]

    def raw_resource(self, entry):
        end = entry.offset + entry.file_size
        if end > len(self.data):
            raise ValueError("Resource lies outside DBPF file")
        return self.data[entry.offset:end]


def refpack_decompress(data: bytes, expected_size=None) -> bytes:
    if len(data) < 5 or data[1] != 0xFB:
        raise ValueError(f"Unknown RefPack header: {data[:8].hex()}")
    out_size = int.from_bytes(data[2:5], "big")
    i = 5
    out = bytearray()
    while i < len(data) and len(out) < out_size:
        cc = data[i]
        i += 1
        if cc < 0x80:
            b1 = data[i]; i += 1
            plain = cc & 3
            copy_len = ((cc & 0x1C) >> 2) + 3
            offset = ((cc & 0x60) << 3) + b1 + 1
            out.extend(data[i:i + plain]); i += plain
        elif cc < 0xC0:
            b1, b2 = data[i], data[i + 1]; i += 2
            plain = (b1 >> 6) & 3
            copy_len = (cc & 0x3F) + 4
            offset = ((b1 & 0x3F) << 8) + b2 + 1
            out.extend(data[i:i + plain]); i += plain
        elif cc < 0xE0:
            b1, b2, b3 = data[i], data[i + 1], data[i + 2]; i += 3
            plain = cc & 3
            copy_len = ((cc & 0x0C) << 6) + b3 + 5
            offset = ((cc & 0x10) << 12) + (b1 << 8) + b2 + 1
            out.extend(data[i:i + plain]); i += plain
        elif cc < 0xFC:
            plain = ((cc & 0x1F) << 2) + 4
            out.extend(data[i:i + plain]); i += plain
            continue
        else:
            plain = cc & 3
            out.extend(data[i:i + plain]); i += plain
            break
        if offset > len(out):
            raise ValueError("Invalid RefPack back-reference")
        for _ in range(copy_len):
            out.append(out[-offset])
    if len(out) != out_size:
        raise ValueError(f"RefPack output size mismatch: got {len(out)}, expected {out_size}")
    return bytes(out)


def read_varint(buf: bytes, pos: int):
    value = 0
    shift = 0
    for _ in range(10):
        if pos >= len(buf):
            raise ValueError("Truncated protobuf varint")
        b = buf[pos]; pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7
    raise ValueError("Oversized protobuf varint")


def parse_proto_fields(buf: bytes):
    fields = []
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if field <= 0:
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            value, pos = read_varint(buf, pos)
        elif wire == 1:
            if pos + 8 > len(buf): raise ValueError("Truncated fixed64")
            value = int.from_bytes(buf[pos:pos + 8], "little"); pos += 8
        elif wire == 2:
            n, pos = read_varint(buf, pos)
            if pos + n > len(buf): raise ValueError("Truncated length-delimited field")
            value = buf[pos:pos + n]; pos += n
        elif wire == 5:
            if pos + 4 > len(buf): raise ValueError("Truncated fixed32")
            value = int.from_bytes(buf[pos:pos + 4], "little"); pos += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire}")
        fields.append((field, wire, value))
    return fields


def first_field(fields, number, wire=None):
    for f, w, v in fields:
        if f == number and (wire is None or wire == w):
            return v
    return None


def find_zone_message(buf: bytes, household_id: int, depth=0):
    if depth > 5 or not buf:
        return None
    try:
        fields = parse_proto_fields(buf)
    except Exception:
        return None
    f5 = [v for f, w, v in fields if f == 5 and w in (0, 1)]
    f6 = [v for f, w, v in fields if f == 6 and w in (0, 1)]
    if household_id in f6 and f5:
        return int(f5[0])
    for _, w, v in fields:
        if w == 2 and isinstance(v, (bytes, bytearray)) and 2 <= len(v) <= 2_000_000:
            found = find_zone_message(bytes(v), household_id, depth + 1)
            if found is not None:
                return found
    return None


def active_household_and_zone(savegame_data: bytes):
    top = parse_proto_fields(savegame_data)
    slot = first_field(top, 2, 2)
    if not isinstance(slot, (bytes, bytearray)):
        raise ValueError("Could not locate SaveSlotData")
    fields = parse_proto_fields(bytes(slot))
    household = first_field(fields, 11)
    if household is None:
        raise ValueError("Could not locate active household ID")
    household = int(household)
    state = first_field(fields, 8, 2)
    zone = find_zone_message(bytes(state), household) if isinstance(state, (bytes, bytearray)) else None
    if zone is None:
        zone = find_zone_message(bytes(slot), household)
    if zone is None:
        raise ValueError(f"Could not find current/home zone for household 0x{household:016X}")
    return household, int(zone)


def decode_resource(pkg: DBPF, entry: DBPFEntry):
    raw = pkg.raw_resource(entry)
    if entry.compression_flags == 0x0001FFFF or (len(raw) >= 2 and raw[1] == 0xFB):
        return refpack_decompress(raw, entry.mem_size)
    return raw


def get_active_context(save_path: Path):
    pkg = DBPF(save_path)
    entries = pkg.resources(TYPE_SAVEGAME_DATA)
    if not entries:
        raise ValueError("Save contains no SaveGameData (type 0x0D)")
    household, zone = active_household_and_zone(decode_resource(pkg, entries[0]))
    return pkg, household, zone


def zone_blob(pkg: DBPF, zone_id: int):
    hi = (zone_id >> 32) & 0xFFFFFFFF
    lo = zone_id & 0xFFFFFFFF
    entry = next((e for e in pkg.resources(TYPE_ZONE_DATA)
                  if e.instance_a == hi and e.instance_b == lo), None)
    return decode_resource(pkg, entry) if entry else b""


def decode_tray_image(path: Path):
    data = path.read_bytes()
    if len(data) < 24:
        raise ValueError(f"Tray image is too small: {path.name}")
    size = int.from_bytes(data[0:8], "little")
    magic = int.from_bytes(data[8:16], "little")
    version = int.from_bytes(data[16:24], "little")
    if magic != TRAY_IMAGE_MAGIC or version != 2:
        raise ValueError(f"Unrecognized Tray image wrapper: {path.name}")
    payload = data[24:24 + size]
    if len(payload) != size:
        raise ValueError(f"Truncated Tray image: {path.name}")
    decoded = bytes(b ^ TRAY_IMAGE_KEY[i & 7] for i, b in enumerate(payload))
    if not (decoded.startswith(b"\xFF\xD8") or decoded.startswith(b"\x89PNG")):
        raise ValueError(f"Decoded Tray image is not JPEG/PNG: {path.name}")
    return decoded


def tray_lots(tray_dir: Path):
    if not tray_dir.is_dir():
        return []
    lots = []
    for item in tray_dir.glob("*.trayitem"):
        try:
            instance = int(item.name.split("!0x", 1)[1].split(".", 1)[0], 16)
        except Exception:
            continue
        blueprint = tray_dir / f"0x00000000!0x{instance:016x}.blueprint"
        opening = tray_dir / f"0x00000003!0x{instance:016x}.bpi"
        if blueprint.exists() and opening.exists():
            lots.append((instance, opening))
    return lots


def find_gallery_image(pkg: DBPF, zone_id: int, tray_dir: Path):
    blob = zone_blob(pkg, zone_id)
    if not blob:
        return None
    matches = []
    for instance, opening in tray_lots(tray_dir):
        pos = blob.find(instance.to_bytes(8, "little"))
        if pos >= 0:
            matches.append((pos, instance, opening))
    if not matches:
        return None
    matches.sort(key=lambda x: x[0])
    _, instance, opening = matches[0]
    return decode_tray_image(opening), instance, opening.name


def fallback_save_thumbnail(pkg: DBPF, zone_id: int):
    hi = (zone_id >> 32) & 0xFFFFFFFF
    lo = zone_id & 0xFFFFFFFF
    entry = next((e for e in pkg.resources(TYPE_SAVEGAME_LOT_THUMBNAIL1)
                  if e.instance_a == hi and e.instance_b == lo), None)
    if not entry:
        raise ValueError(f"Could not find lot image for zone 0x{zone_id:016X}")
    raw = decode_resource(pkg, entry)
    if not raw.startswith(b"\xFF\xD8"):
        raise ValueError("Save lot thumbnail is not a JPEG")
    return raw


def extract_active_lot_image(save_path: Path, tray_dir: Path):
    pkg, household, zone = get_active_context(save_path)
    gallery = find_gallery_image(pkg, zone, tray_dir)
    if gallery:
        image, tray_id, filename = gallery
        return image, household, zone, f"Gallery/Tray {filename}", tray_id
    return fallback_save_thumbnail(pkg, zone), household, zone, "SaveGameLotThumbnail1 fallback", None


def make_background(image_bytes: bytes):
    import io
    with Image.open(io.BytesIO(image_bytes)) as im:
        lot = im.convert("RGB")
    src_ratio = lot.width / lot.height
    dst_ratio = EXPECTED_BG_WIDTH / EXPECTED_BG_HEIGHT
    if src_ratio >= dst_ratio:
        new_h = EXPECTED_BG_HEIGHT
        new_w = round(new_h * src_ratio)
    else:
        new_w = EXPECTED_BG_WIDTH
        new_h = round(new_w / src_ratio)
    frame = lot.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = max(0, (new_w - EXPECTED_BG_WIDTH) // 2)
    top = max(0, (new_h - EXPECTED_BG_HEIGHT) // 2)
    frame = frame.crop((left, top, left + EXPECTED_BG_WIDTH, top + EXPECTED_BG_HEIGHT))
    return ImageEnhance.Sharpness(frame).enhance(1.08)


def compatible_background_entries(pkg: DBPF):
    targets = []
    for e in pkg.resources(TYPE_DST_IMAGE):
        if e.file_size != EXPECTED_DDS_SIZE:
            continue
        raw = pkg.raw_resource(e)
        if (len(raw) >= 128 and raw[:4] == b"DDS " and raw[84:88] == EXPECTED_FOURCC
                and struct.unpack_from("<I", raw, 16)[0] == EXPECTED_BG_WIDTH
                and struct.unpack_from("<I", raw, 12)[0] == EXPECTED_BG_HEIGHT):
            targets.append(e)
    return targets


def image_to_template_dds(image, template_header: bytes, temp_dir: Path):
    temp = temp_dir / "_dynamic_menu_temp.dxt5.dds"
    image.convert("RGBA").save(temp, format="DDS", pixel_format="DXT5")
    dds = temp.read_bytes()
    try: temp.unlink()
    except OSError: pass
    if len(dds) != EXPECTED_DDS_SIZE or dds[:4] != b"DDS " or dds[84:88] != EXPECTED_FOURCC:
        raise ValueError("Unexpected Pillow DDS output; expected 1920x1080 DXT5")
    header = bytes(template_header[:128])
    if len(header) != 128 or header[:4] != b"DDS " or header[84:88] != EXPECTED_FOURCC:
        raise ValueError("Template DDS header is not compatible DXT5")
    return header + dds[128:]


def patch_template(template_path: Path, output_path: Path, dds: bytes):
    pkg = DBPF(template_path)
    targets = compatible_background_entries(pkg)
    if not targets:
        raise ValueError("Template contains no compatible 1920x1080 DXT5 background resource")
    out = bytearray(pkg.data)
    for e in targets:
        out[e.offset:e.offset + e.file_size] = dds
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_bytes(out)
    os.replace(tmp, output_path)
    return len(targets)


def candidate_ts4_roots():
    home = Path.home()
    roots = [home / "Documents" / "Electronic Arts" / "The Sims 4",
             home / "OneDrive" / "Documents" / "Electronic Arts" / "The Sims 4"]
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                personal, _ = winreg.QueryValueEx(key, "Personal")
                roots.insert(0, Path(os.path.expandvars(personal)) / "Electronic Arts" / "The Sims 4")
        except Exception:
            pass
    result, seen = [], set()
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key); result.append(root)
    return result


def find_ts4_root(explicit=None):
    if explicit:
        root = Path(explicit).expanduser()
        if root.exists(): return root
        raise FileNotFoundError(f"TS4 user folder not found: {root}")
    for root in candidate_ts4_roots():
        if (root / "saves").is_dir() and (root / "Mods").is_dir():
            return root
    raise FileNotFoundError("Could not locate The Sims 4 user folder")


def newest_save(saves_dir: Path, explicit=None):
    if explicit:
        path = Path(explicit).expanduser()
        if path.exists(): return path
        raise FileNotFoundError(path)
    saves = list(saves_dir.glob("Slot_*.save"))
    if not saves:
        raise FileNotFoundError(f"No Slot_*.save files found in {saves_dir}")
    return max(saves, key=lambda p: p.stat().st_mtime)


def try_launch_game():
    if os.name != "nt": return False
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "EA Games/The Sims 4/Game/Bin/TS4_x64.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Origin Games/The Sims 4/Game/Bin/TS4_x64.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam/steamapps/common/The Sims 4/Game/Bin/TS4_x64.exe",
    ]
    for exe in candidates:
        if exe.exists():
            try:
                subprocess.Popen([str(exe)], cwd=str(exe.parent)); return True
            except Exception:
                pass
    return False


def main():
    parser = argparse.ArgumentParser(description="Update Sims 4 main-menu background to the active lot")
    parser.add_argument("--ts4-root")
    parser.add_argument("--save")
    parser.add_argument("--template")
    parser.add_argument("--output")
    parser.add_argument("--preview")
    parser.add_argument("--launch", action="store_true")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        default_template = Path(sys._MEIPASS) / "Main_Menu_Dynamic_Template.package"
        temp_dir = Path(os.environ.get("TEMP", str(Path.home())))
    else:
        default_template = script_dir.parent / "assets" / "Main_Menu_Dynamic_Template.package"
        temp_dir = script_dir

    template = Path(args.template).expanduser() if args.template else default_template
    if not template.exists():
        raise FileNotFoundError(f"Template package not found: {template}")
    root = find_ts4_root(args.ts4_root)
    save = newest_save(root / "saves", args.save)
    output = Path(args.output).expanduser() if args.output else root / "Mods" / OUTPUT_PACKAGE_NAME

    print("TS4 Dynamic Lot Main Menu Updater")
    print(f"User folder : {root}")
    print(f"Save        : {save.name}")
    print(f"Template    : {template.name}")

    image_bytes, household, zone, source, tray_id = extract_active_lot_image(save, root / "Tray")
    print(f"Household   : 0x{household:016X}")
    print(f"Zone        : 0x{zone:016X}")
    if tray_id is not None:
        print(f"Tray item   : 0x{tray_id:016X}")
    print(f"Image source: {source}")

    image = make_background(image_bytes)
    if args.preview:
        preview = Path(args.preview).expanduser()
        preview.parent.mkdir(parents=True, exist_ok=True)
        image.save(preview, "PNG")
        print(f"Preview     : {preview}")

    template_pkg = DBPF(template)
    targets = compatible_background_entries(template_pkg)
    if not targets:
        raise ValueError("Template contains no compatible 1920x1080 DXT5 background resource")
    header = template_pkg.raw_resource(targets[0])[:128]
    dds = image_to_template_dds(image, header, temp_dir)
    count = patch_template(template, output, dds)
    print(f"Updated     : {output}")
    print(f"Textures    : {count} background resources patched")
    print("SUCCESS: main-menu package is ready.")

    if args.launch:
        print("Launch      : started The Sims 4" if try_launch_game()
              else "Launch      : game executable not found automatically; launch Sims 4 normally.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nERROR: {exc}")
        print("The updater did not overwrite your save file.")
        raise SystemExit(1)

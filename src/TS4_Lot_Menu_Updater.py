#!/usr/bin/env python3
"""
TS4 Dynamic Lot Main Menu Updater

Reads the newest Sims 4 save, determines the active/last-played household's
current home zone, prefers the rectangular lot preview cached by The Sims 4, falls back to the save lot thumbnail when needed, builds a 1920x1080 menu image, encodes it as DXT5, and patches the
background DDS resource in a copy of the supplied menu template.

Designed for The Sims 4 1.127-era DBPF/save structure used by this project.
"""
from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

try:
    from PIL import Image, ImageFilter, ImageEnhance
except Exception:
    print("ERROR: Pillow is required. Run Setup_And_Update.bat once to install it.")
    raise

DBPF_MAGIC = b"DBPF"
TYPE_SAVEGAME_DATA = 0x0000000D
TYPE_SAVEGAME_LOT_THUMBNAIL1 = 0x0000000F
TYPE_SAVEGAME_LOT_THUMBNAIL2 = 0x00000010
TYPE_CACHED_LOT_PREVIEW = 0xD33C281E
TYPE_DST_IMAGE = 0x00B2D882
EXPECTED_BG_WIDTH = 1920
EXPECTED_BG_HEIGHT = 1080
EXPECTED_DDS_SIZE = 2073728  # 128-byte header + 1920x1080 DXT5 data
EXPECTED_FOURCC = b"DXT5"
OUTPUT_PACKAGE_NAME = "ZZZ_Dynamic_Lot_Main_Menu.package"


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
    def file_size(self) -> int:
        return self.file_size_raw & 0x7FFFFFFF

    @property
    def resource_instance(self) -> int:
        return (self.instance_b << 32) | self.instance_a


class DBPF:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if self.data[:4] != DBPF_MAGIC:
            raise ValueError(f"Not a DBPF file: {self.path}")
        self.entry_count = struct.unpack_from("<I", self.data, 0x24)[0]
        self.index_size = struct.unpack_from("<I", self.data, 0x2C)[0]
        self.index_offset = struct.unpack_from("<I", self.data, 0x40)[0]
        if self.index_offset + self.index_size > len(self.data):
            raise ValueError("DBPF index lies outside file")
        self.index_flags = struct.unpack_from("<I", self.data, self.index_offset)[0]
        if self.index_flags != 0:
            raise ValueError(
                f"Unsupported compact DBPF index flags 0x{self.index_flags:08X}. "
                "This helper currently expects the standard Sims 4 index used by your saves/mod."
            )
        expected = 4 + self.entry_count * 32
        if self.index_size < expected:
            raise ValueError("Unexpected DBPF index size")
        self.entries: list[DBPFEntry] = []
        pos = self.index_offset + 4
        for _ in range(self.entry_count):
            vals = struct.unpack_from("<8I", self.data, pos)
            self.entries.append(DBPFEntry(*vals))
            pos += 32

    def resources(self, type_id: int) -> list[DBPFEntry]:
        return [e for e in self.entries if e.type_id == type_id]

    def raw_resource(self, entry: DBPFEntry) -> bytes:
        end = entry.offset + entry.file_size
        if end > len(self.data):
            raise ValueError("Resource lies outside DBPF file")
        return self.data[entry.offset:end]


def refpack_decompress(data: bytes, expected_size: Optional[int] = None) -> bytes:
    if len(data) < 5 or data[1] != 0xFB:
        raise ValueError(f"Unknown RefPack header: {data[:8].hex()}")
    out_size = int.from_bytes(data[2:5], "big")
    i = 5
    out = bytearray()
    while i < len(data) and len(out) < out_size:
        cc = data[i]
        i += 1
        if cc < 0x80:
            if i >= len(data):
                raise ValueError("Truncated RefPack stream")
            b1 = data[i]
            i += 1
            plain = cc & 0x03
            copy_len = ((cc & 0x1C) >> 2) + 3
            offset = ((cc & 0x60) << 3) + b1 + 1
            out.extend(data[i:i + plain])
            i += plain
            if offset > len(out):
                raise ValueError("Invalid RefPack back-reference")
            for _ in range(copy_len):
                out.append(out[-offset])
        elif cc < 0xC0:
            if i + 1 >= len(data):
                raise ValueError("Truncated RefPack stream")
            b1, b2 = data[i], data[i + 1]
            i += 2
            plain = (b1 >> 6) & 0x03
            copy_len = (cc & 0x3F) + 4
            offset = ((b1 & 0x3F) << 8) + b2 + 1
            out.extend(data[i:i + plain])
            i += plain
            if offset > len(out):
                raise ValueError("Invalid RefPack back-reference")
            for _ in range(copy_len):
                out.append(out[-offset])
        elif cc < 0xE0:
            if i + 2 >= len(data):
                raise ValueError("Truncated RefPack stream")
            b1, b2, b3 = data[i], data[i + 1], data[i + 2]
            i += 3
            plain = cc & 0x03
            copy_len = ((cc & 0x0C) << 6) + b3 + 5
            offset = ((cc & 0x10) << 12) + (b1 << 8) + b2 + 1
            out.extend(data[i:i + plain])
            i += plain
            if offset > len(out):
                raise ValueError("Invalid RefPack back-reference")
            for _ in range(copy_len):
                out.append(out[-offset])
        elif cc < 0xFC:
            plain = ((cc & 0x1F) << 2) + 4
            out.extend(data[i:i + plain])
            i += plain
        else:
            plain = cc & 0x03
            out.extend(data[i:i + plain])
            i += plain
            break
    if len(out) != out_size:
        raise ValueError(f"RefPack output size mismatch: got {len(out)}, expected {out_size}")
    return bytes(out)


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(10):
        if pos >= len(buf):
            raise ValueError("Truncated protobuf varint")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
    raise ValueError("Oversized protobuf varint")


def parse_proto_fields(buf: bytes):
    fields = []
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        f, w = key >> 3, key & 7
        if f <= 0:
            raise ValueError("Invalid protobuf field number")
        if w == 0:
            v, pos = read_varint(buf, pos)
        elif w == 1:
            if pos + 8 > len(buf):
                raise ValueError("Truncated fixed64")
            v = int.from_bytes(buf[pos:pos + 8], "little")
            pos += 8
        elif w == 2:
            n, pos = read_varint(buf, pos)
            if pos + n > len(buf):
                raise ValueError("Truncated length-delimited field")
            v = buf[pos:pos + n]
            pos += n
        elif w == 5:
            if pos + 4 > len(buf):
                raise ValueError("Truncated fixed32")
            v = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {w}")
        fields.append((f, w, v))
    return fields


def first_field(fields, number, wire=None):
    for f, w, v in fields:
        if f == number and (wire is None or w == wire):
            return v
    return None


def find_zone_message(buf: bytes, household_id: int, depth: int = 0):
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


def get_active_household_and_zone(savegame_data: bytes) -> tuple[int, int]:
    top = parse_proto_fields(savegame_data)
    save_slot_data = first_field(top, 2, 2)
    if not isinstance(save_slot_data, (bytes, bytearray)):
        raise ValueError("Could not locate SaveSlotData (top-level protobuf field 2)")
    slot_fields = parse_proto_fields(bytes(save_slot_data))
    household_id = first_field(slot_fields, 11)
    if household_id is None:
        raise ValueError("Could not locate active household ID (SaveSlotData field 11)")
    household_id = int(household_id)
    gameplay_state = first_field(slot_fields, 8, 2)
    zone_id = None
    if isinstance(gameplay_state, (bytes, bytearray)):
        zone_id = find_zone_message(bytes(gameplay_state), household_id)
    if zone_id is None:
        zone_id = find_zone_message(bytes(save_slot_data), household_id)
    if zone_id is None:
        raise ValueError(f"Could not find a current/home zone for household 0x{household_id:016X}")
    return household_id, int(zone_id)


def get_active_household_zone(save_path: Path) -> tuple[int, int, DBPF]:
    pkg = DBPF(save_path)
    data_entries = pkg.resources(TYPE_SAVEGAME_DATA)
    if not data_entries:
        raise ValueError("Save contains no SaveGameData (type 0x0D)")
    data_entry = data_entries[0]
    raw = pkg.raw_resource(data_entry)
    if data_entry.compression_flags == 0x0001FFFF or (len(raw) >= 2 and raw[1] == 0xFB):
        decoded = refpack_decompress(raw, data_entry.mem_size)
    else:
        decoded = raw
    household_id, zone_id = get_active_household_and_zone(decoded)
    return household_id, zone_id, pkg


def extract_save_lot_thumbnail(pkg: DBPF, zone_id: int) -> tuple[bytes, str]:
    zone_hi = (zone_id >> 32) & 0xFFFFFFFF
    zone_lo = zone_id & 0xFFFFFFFF
    for type_id, label in (
        (TYPE_SAVEGAME_LOT_THUMBNAIL2, "save Thumbnail2 (400x400)"),
        (TYPE_SAVEGAME_LOT_THUMBNAIL1, "save Thumbnail1 (256x256)"),
    ):
        for e in pkg.resources(type_id):
            if e.instance_a == zone_hi and e.instance_b == zone_lo:
                raw = pkg.raw_resource(e)
                if e.compression_flags == 0x0001FFFF or (len(raw) >= 2 and raw[1] == 0xFB):
                    raw = refpack_decompress(raw, e.mem_size)
                if raw.startswith(b"\xFF\xD8"):
                    return raw, label
    expected_instance = (zone_lo << 32) | zone_hi
    raise ValueError(
        f"Could not find a saved lot thumbnail for zone 0x{zone_id:016X} "
        f"(expected instance 0x{expected_instance:016X})"
    )


def extract_cached_lot_preview(cache_path: Path) -> Optional[tuple[bytes, str]]:
    if not cache_path.exists():
        return None
    try:
        cache = DBPF(cache_path)
    except Exception:
        return None
    preferred = None
    rectangular = []
    import io
    for e in cache.resources(TYPE_CACHED_LOT_PREVIEW):
        raw = cache.raw_resource(e)
        if not raw.startswith(b"\xFF\xD8"):
            continue
        try:
            with Image.open(io.BytesIO(raw)) as im:
                size = im.size
        except Exception:
            continue
        w, h = size
        if w <= h:
            continue
        if e.group_id == 0x00000003 and e.resource_instance == 0 and size == (591, 394):
            preferred = (raw, "localthumbcache rectangular lot preview (591x394)")
        rectangular.append((w * h, e, raw, size))
    if preferred is not None:
        return preferred
    if not rectangular:
        return None
    rectangular.sort(key=lambda x: x[0], reverse=True)
    _, _, raw, size = rectangular[0]
    return raw, f"localthumbcache rectangular lot preview ({size[0]}x{size[1]})"


def extract_active_lot_thumbnail(save_path: Path, cache_path: Optional[Path] = None) -> tuple[bytes, int, int, str]:
    household_id, zone_id, pkg = get_active_household_zone(save_path)
    if cache_path is not None:
        cached = extract_cached_lot_preview(cache_path)
        if cached is not None:
            image_bytes, source = cached
            return image_bytes, household_id, zone_id, source
    image_bytes, source = extract_save_lot_thumbnail(pkg, zone_id)
    return image_bytes, household_id, zone_id, source


def make_background(image_bytes: bytes) -> Image.Image:
    import io
    with Image.open(io.BytesIO(image_bytes)) as im:
        lot = im.convert("RGB")
    src_ratio = lot.width / lot.height
    dst_ratio = EXPECTED_BG_WIDTH / EXPECTED_BG_HEIGHT
    if src_ratio >= dst_ratio:
        h = EXPECTED_BG_HEIGHT
        w = round(h * src_ratio)
    else:
        w = EXPECTED_BG_WIDTH
        h = round(w / src_ratio)
    frame = lot.resize((w, h), Image.Resampling.LANCZOS)
    left = max(0, (w - EXPECTED_BG_WIDTH) // 2)
    top = max(0, (h - EXPECTED_BG_HEIGHT) // 2)
    frame = frame.crop((left, top, left + EXPECTED_BG_WIDTH, top + EXPECTED_BG_HEIGHT))
    frame = ImageEnhance.Sharpness(frame).enhance(1.15)
    return frame


def image_to_template_dds(image: Image.Image, template_dds_header: bytes, temp_dir: Path) -> bytes:
    temp_dds = temp_dir / "_dynamic_menu_temp.dxt5.dds"
    image.convert("RGBA").save(temp_dds, format="DDS", pixel_format="DXT5")
    dds = temp_dds.read_bytes()
    try:
        temp_dds.unlink()
    except OSError:
        pass
    if len(dds) != EXPECTED_DDS_SIZE or dds[:4] != b"DDS " or dds[84:88] != EXPECTED_FOURCC:
        raise ValueError("Unexpected Pillow DDS output; expected 1920x1080 DXT5")
    header = bytes(template_dds_header[:128])
    if len(header) != 128 or header[:4] != b"DDS " or header[84:88] != EXPECTED_FOURCC:
        raise ValueError("Template DDS header is not a compatible DXT5 header")
    if struct.unpack_from("<I", header, 12)[0] != EXPECTED_BG_HEIGHT or struct.unpack_from("<I", header, 16)[0] != EXPECTED_BG_WIDTH:
        raise ValueError("Template DDS dimensions are not 1920x1080")
    result = header + dds[128:]
    if len(result) != EXPECTED_DDS_SIZE:
        raise ValueError("DXT5 output size mismatch")
    return result


def compatible_background_entries(pkg: DBPF) -> list[DBPFEntry]:
    targets = []
    for e in pkg.resources(TYPE_DST_IMAGE):
        if e.file_size != EXPECTED_DDS_SIZE:
            continue
        raw = pkg.raw_resource(e)
        if len(raw) < 128 or raw[:4] != b"DDS ":
            continue
        width = struct.unpack_from("<I", raw, 16)[0]
        height = struct.unpack_from("<I", raw, 12)[0]
        if width == EXPECTED_BG_WIDTH and height == EXPECTED_BG_HEIGHT and raw[84:88] == EXPECTED_FOURCC:
            targets.append(e)
    return targets


def patch_template_package(template_path: Path, output_path: Path, dds: bytes) -> int:
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


def candidate_ts4_roots() -> list[Path]:
    roots = []
    home = Path.home()
    roots.append(home / "Documents" / "Electronic Arts" / "The Sims 4")
    roots.append(home / "OneDrive" / "Documents" / "Electronic Arts" / "The Sims 4")
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                personal, _ = winreg.QueryValueEx(k, "Personal")
                roots.insert(0, Path(os.path.expandvars(personal)) / "Electronic Arts" / "The Sims 4")
        except Exception:
            pass
    seen = set()
    result = []
    for p in roots:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def find_ts4_root(explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return p
        raise FileNotFoundError(f"TS4 user folder not found: {p}")
    for p in candidate_ts4_roots():
        if (p / "saves").is_dir() and (p / "Mods").is_dir():
            return p
    raise FileNotFoundError("Could not locate The Sims 4 user folder")


def newest_save(saves_dir: Path, explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    saves = [p for p in saves_dir.glob("Slot_*.save") if not p.name.lower().endswith(".ver0.save")]
    if not saves:
        raise FileNotFoundError(f"No Slot_*.save files found in {saves_dir}")
    return max(saves, key=lambda p: p.stat().st_mtime)


def try_launch_game() -> bool:
    if os.name != "nt":
        return False
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "EA Games" / "The Sims 4" / "Game" / "Bin" / "TS4_x64.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Origin Games" / "The Sims 4" / "Game" / "Bin" / "TS4_x64.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam" / "steamapps" / "common" / "The Sims 4" / "Game" / "Bin" / "TS4_x64.exe",
    ]
    for exe in candidates:
        if exe.exists():
            try:
                subprocess.Popen([str(exe)], cwd=str(exe.parent))
                return True
            except Exception:
                pass
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Update Sims 4 main-menu background to the active household lot thumbnail")
    parser.add_argument("--ts4-root", help="Override The Sims 4 user folder")
    parser.add_argument("--save", help="Override save file instead of newest Slot_*.save")
    parser.add_argument("--template", help="Template main-menu package; defaults beside this script")
    parser.add_argument("--output", help="Output .package path; defaults to Mods/ZZZ_Dynamic_Lot_Main_Menu.package")
    parser.add_argument("--preview", help="Optional path to save the generated 1920x1080 PNG preview")
    parser.add_argument("--launch", action="store_true", help="Try to launch TS4 after updating")
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

    ts4_root = find_ts4_root(args.ts4_root)
    save_path = newest_save(ts4_root / "saves", args.save)
    output_path = Path(args.output).expanduser() if args.output else ts4_root / "Mods" / OUTPUT_PACKAGE_NAME

    print("TS4 Dynamic Lot Main Menu Updater")
    print(f"User folder : {ts4_root}")
    print(f"Save        : {save_path.name}")
    print(f"Template    : {template.name}")

    cache_path = ts4_root / "localthumbcache.package"
    image_bytes, household_id, zone_id, source = extract_active_lot_thumbnail(save_path, cache_path)
    expected_instance = ((zone_id & 0xFFFFFFFF) << 32) | ((zone_id >> 32) & 0xFFFFFFFF)
    print(f"Household   : 0x{household_id:016X}")
    print(f"Zone        : 0x{zone_id:016X}")
    print(f"Lot thumb   : 0x{expected_instance:016X}")
    print(f"Image source: {source}")

    image = make_background(image_bytes)
    if args.preview:
        preview = Path(args.preview).expanduser()
        preview.parent.mkdir(parents=True, exist_ok=True)
        image.save(preview, "PNG")
        print(f"Preview     : {preview}")

    template_pkg = DBPF(template)
    bg_entries = compatible_background_entries(template_pkg)
    if not bg_entries:
        raise ValueError("Template contains no compatible 1920x1080 DXT5 background resource")
    template_header = template_pkg.raw_resource(bg_entries[0])[:128]
    dds = image_to_template_dds(image, template_header, temp_dir)
    count = patch_template_package(template, output_path, dds)
    print(f"Updated     : {output_path}")
    print(f"Textures    : {count} background resources patched")
    print("SUCCESS: main-menu package is ready.")
    if args.launch:
        if try_launch_game():
            print("Launch      : started The Sims 4")
        else:
            print("Launch      : game executable not found automatically; launch Sims 4 normally.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nERROR: {exc}")
        print("The updater did not overwrite your save file.")
        raise SystemExit(1)

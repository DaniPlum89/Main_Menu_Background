#!/usr/bin/env python3
"""
TS4 Dynamic Lot Main Menu Updater

Reads the newest Sims 4 save, determines the active/last-played household's
current home zone, extracts that save's SaveGameLotThumbnail1 JPEG, builds a
1280x720 menu image, converts BC1/DXT1 blocks to Sims 4's planar DST1 layout,
and patches both DDS background resources in a copy of the supplied menu mod.

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
TYPE_DST_IMAGE = 0x00B2D882
EXPECTED_BG_WIDTH = 1280
EXPECTED_BG_HEIGHT = 720
EXPECTED_DDS_SIZE = 0x70880  # 128-byte header + 1280x720 BC1 data
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
        # Sims 4 DBPF index stores the low 32-bit word first, then high 32-bit word
        # for these resources. Displayed 64-bit instance = B:A.
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


# -------------------- EA RefPack/QFS decompression --------------------
def refpack_decompress(data: bytes, expected_size: Optional[int] = None) -> bytes:
    # TS4 save resources used here begin: flags, 0xFB, 3-byte BE output length.
    if len(data) < 5 or data[1] != 0xFB:
        raise ValueError(f"Unknown RefPack header: {data[:8].hex()}")
    out_size = int.from_bytes(data[2:5], "big")
    if expected_size and out_size != expected_size:
        # Continue: mem-size is advisory, header is authoritative.
        pass
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


# -------------------- Minimal protobuf wire parser --------------------
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


def parse_proto_fields(buf: bytes) -> list[tuple[int, int, object]]:
    fields: list[tuple[int, int, object]] = []
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        field_no, wire = key >> 3, key & 7
        if field_no <= 0:
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            value, pos = read_varint(buf, pos)
        elif wire == 1:
            if pos + 8 > len(buf):
                raise ValueError("Truncated fixed64")
            value = int.from_bytes(buf[pos:pos + 8], "little")
            pos += 8
        elif wire == 2:
            length, pos = read_varint(buf, pos)
            if pos + length > len(buf):
                raise ValueError("Truncated length-delimited field")
            value = buf[pos:pos + length]
            pos += length
        elif wire == 5:
            if pos + 4 > len(buf):
                raise ValueError("Truncated fixed32")
            value = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        else:
            # Groups are not expected in this TS4 data path.
            raise ValueError(f"Unsupported protobuf wire type {wire}")
        fields.append((field_no, wire, value))
    return fields


def first_field(fields, number: int, wire: Optional[int] = None):
    for f, w, v in fields:
        if f == number and (wire is None or w == wire):
            return v
    return None


def find_zone_message(buf: bytes, household_id: int, depth: int = 0) -> Optional[int]:
    if depth > 5 or not buf:
        return None
    try:
        fields = parse_proto_fields(buf)
    except Exception:
        return None

    f5_values = [v for f, w, v in fields if f == 5 and w in (0, 1)]
    f6_values = [v for f, w, v in fields if f == 6 and w in (0, 1)]
    if household_id in f6_values and f5_values:
        return int(f5_values[0])

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

    # In this save format, field 8 contains the gameplay state; a nested message
    # contains field 5 = zone ID and field 6 = household ID.
    gameplay_state = first_field(slot_fields, 8, 2)
    zone_id = None
    if isinstance(gameplay_state, (bytes, bytearray)):
        zone_id = find_zone_message(bytes(gameplay_state), household_id)
    if zone_id is None:
        # Fallback: search all nested SaveSlotData messages for the same field pair.
        zone_id = find_zone_message(bytes(save_slot_data), household_id)
    if zone_id is None:
        raise ValueError(f"Could not find a current/home zone for household 0x{household_id:016X}")
    return household_id, int(zone_id)


# -------------------- Save thumbnail extraction --------------------
def extract_active_lot_thumbnail(save_path: Path) -> tuple[bytes, int, int]:
    pkg = DBPF(save_path)
    data_entries = pkg.resources(TYPE_SAVEGAME_DATA)
    if not data_entries:
        raise ValueError("Save contains no SaveGameData (type 0x0D)")
    # Current saves normally have one 0x0D resource.
    data_entry = data_entries[0]
    raw = pkg.raw_resource(data_entry)
    if data_entry.compression_flags == 0x0001FFFF or (len(raw) >= 2 and raw[1] == 0xFB):
        decoded = refpack_decompress(raw, data_entry.mem_size)
    else:
        decoded = raw

    household_id, zone_id = get_active_household_and_zone(decoded)
    zone_hi = (zone_id >> 32) & 0xFFFFFFFF
    zone_lo = zone_id & 0xFFFFFFFF

    candidates = pkg.resources(TYPE_SAVEGAME_LOT_THUMBNAIL1)
    match = None
    for e in candidates:
        # For TS4 save-lot thumbnails, the displayed instance swaps the two
        # 32-bit halves of the zone ID. In the DBPF index this means A=zone_hi,
        # B=zone_lo, exactly as observed in the user's save.
        if e.instance_a == zone_hi and e.instance_b == zone_lo:
            match = e
            break
    if match is None:
        expected_instance = (zone_lo << 32) | zone_hi
        available = ", ".join(f"0x{e.resource_instance:016X}" for e in candidates[:12])
        raise ValueError(
            f"Could not find SaveGameLotThumbnail1 for zone 0x{zone_id:016X}. "
            f"Expected resource instance 0x{expected_instance:016X}. "
            f"Available examples: {available or 'none'}"
        )
    jpg = pkg.raw_resource(match)
    if not jpg.startswith(b"\xFF\xD8"):
        raise ValueError("Matched lot thumbnail is not a JPEG")
    return jpg, household_id, zone_id


# -------------------- Background composition + DST1 --------------------
def make_background(jpg_bytes: bytes) -> Image.Image:
    import io
    with Image.open(io.BytesIO(jpg_bytes)) as im:
        lot = im.convert("RGB")

    # Blurred full-frame backdrop: fill the widescreen frame without stretching.
    src_ratio = lot.width / lot.height
    dst_ratio = EXPECTED_BG_WIDTH / EXPECTED_BG_HEIGHT
    if src_ratio > dst_ratio:
        h = EXPECTED_BG_HEIGHT
        w = round(h * src_ratio)
    else:
        w = EXPECTED_BG_WIDTH
        h = round(w / src_ratio)
    backdrop = lot.resize((w, h), Image.Resampling.LANCZOS)
    left = max(0, (w - EXPECTED_BG_WIDTH) // 2)
    top = max(0, (h - EXPECTED_BG_HEIGHT) // 2)
    backdrop = backdrop.crop((left, top, left + EXPECTED_BG_WIDTH, top + EXPECTED_BG_HEIGHT))
    backdrop = backdrop.filter(ImageFilter.GaussianBlur(radius=32))
    backdrop = ImageEnhance.Brightness(backdrop).enhance(0.78)

    # Preserve the actual square lot thumbnail in the center instead of stretching it.
    foreground = lot.resize((EXPECTED_BG_HEIGHT, EXPECTED_BG_HEIGHT), Image.Resampling.LANCZOS)
    x = (EXPECTED_BG_WIDTH - EXPECTED_BG_HEIGHT) // 2
    backdrop.paste(foreground, (x, 0))
    return backdrop


def image_to_dst1(image: Image.Image, template_dds_header: bytes, temp_dir: Path) -> bytes:
    temp_dds = temp_dir / "_dynamic_menu_temp.dxt1.dds"
    image.save(temp_dds, format="DDS", pixel_format="DXT1")
    dxt = temp_dds.read_bytes()
    try:
        temp_dds.unlink()
    except OSError:
        pass
    if len(dxt) != EXPECTED_DDS_SIZE or dxt[:4] != b"DDS " or dxt[84:88] != b"DXT1":
        raise ValueError(
            f"Unexpected Pillow DDS output ({len(dxt)} bytes, FOURCC={dxt[84:88]!r}); "
            "expected 1280x720 DXT1"
        )
    blocks = dxt[128:]
    if len(blocks) % 8:
        raise ValueError("DXT1 block data is not 8-byte aligned")

    # Sims 4 DST1 stores each BC1 block's 4-byte color endpoints in one plane,
    # followed by all 4-byte selector/index words in a second plane.
    endpoints = bytearray()
    selectors = bytearray()
    for pos in range(0, len(blocks), 8):
        endpoints.extend(blocks[pos:pos + 4])
        selectors.extend(blocks[pos + 4:pos + 8])

    header = bytearray(template_dds_header[:128])
    if len(header) != 128 or header[:4] != b"DDS ":
        raise ValueError("Template DDS header is invalid")
    header[84:88] = b"DST1"
    result = bytes(header) + bytes(endpoints) + bytes(selectors)
    if len(result) != EXPECTED_DDS_SIZE:
        raise ValueError("DST1 output size mismatch")
    return result


def patch_template_package(template_path: Path, output_path: Path, dst1: bytes) -> int:
    pkg = DBPF(template_path)
    targets = [e for e in pkg.resources(TYPE_DST_IMAGE) if e.file_size == EXPECTED_DDS_SIZE]
    if len(targets) < 2:
        raise ValueError(
            f"Template has only {len(targets)} compatible 1280x720 DST1 background resource(s); expected at least 2"
        )
    out = bytearray(pkg.data)
    for e in targets:
        out[e.offset:e.offset + e.file_size] = dst1
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_bytes(out)
    os.replace(tmp, output_path)
    return len(targets)


# -------------------- Windows location helpers --------------------
def candidate_ts4_roots() -> list[Path]:
    roots: list[Path] = []
    home = Path.home()
    roots.append(home / "Documents" / "Electronic Arts" / "The Sims 4")
    roots.append(home / "OneDrive" / "Documents" / "Electronic Arts" / "The Sims 4")
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                personal, _ = winreg.QueryValueEx(k, "Personal")
                personal = os.path.expandvars(personal)
                roots.insert(0, Path(personal) / "Electronic Arts" / "The Sims 4")
        except Exception:
            pass
    # Deduplicate while preserving order.
    seen = set(); result = []
    for p in roots:
        key = str(p).lower()
        if key not in seen:
            seen.add(key); result.append(p)
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
    tried = "\n  ".join(str(p) for p in candidate_ts4_roots())
    raise FileNotFoundError(f"Could not locate The Sims 4 user folder. Tried:\n  {tried}")


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
    parser.add_argument("--preview", help="Optional path to save the generated 1280x720 PNG preview")
    parser.add_argument("--launch", action="store_true", help="Try to launch TS4 after updating")
    args = parser.parse_args()

    # When bundled by PyInstaller, bundled data lives in sys._MEIPASS.
    # During normal source runs, use the repository assets folder.
    script_dir = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundle_dir = Path(sys._MEIPASS)
        default_template = bundle_dir / "Main_Menu_Dynamic_Template.package"
        temp_dir = Path(os.environ.get("TEMP", str(Path.home())))
    else:
        bundle_dir = script_dir
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

    jpg, household_id, zone_id = extract_active_lot_thumbnail(save_path)
    expected_instance = ((zone_id & 0xFFFFFFFF) << 32) | ((zone_id >> 32) & 0xFFFFFFFF)
    print(f"Household   : 0x{household_id:016X}")
    print(f"Zone        : 0x{zone_id:016X}")
    print(f"Lot thumb   : 0x{expected_instance:016X}")

    image = make_background(jpg)
    if args.preview:
        preview = Path(args.preview).expanduser()
        preview.parent.mkdir(parents=True, exist_ok=True)
        image.save(preview, "PNG")
        print(f"Preview     : {preview}")

    # Use the first compatible template DST resource's exact DDS header.
    template_pkg = DBPF(template)
    dst_entries = [e for e in template_pkg.resources(TYPE_DST_IMAGE) if e.file_size == EXPECTED_DDS_SIZE]
    if not dst_entries:
        raise ValueError("Template contains no compatible DST1 background resource")
    template_header = template_pkg.raw_resource(dst_entries[0])[:128]
    dst1 = image_to_dst1(image, template_header, temp_dir)
    count = patch_template_package(template, output_path, dst1)
    print(f"Updated     : {output_path}")
    print(f"Textures    : {count} background resources patched")

    cache = ts4_root / "localthumbcache.package"
    if cache.exists():
        try:
            cache.unlink()
            print("Cache       : localthumbcache.package cleared")
        except Exception as exc:
            print(f"Cache       : could not delete localthumbcache.package ({exc})")

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

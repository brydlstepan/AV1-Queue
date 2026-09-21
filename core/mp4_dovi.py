"""
Dolby Vision container signalling for AV1-in-MP4.

SvtAv1EncApp bakes the DoVi RPU into the AV1 bitstream (T.35 OBUs), but a player
only treats an MP4 as Dolby Vision when the ``av01`` sample entry carries a
``dvvC`` box (and the file lists the ``dby1`` brand). ffmpeg's MP4 muxer only
writes ``dvvC`` when the input stream already has DOVI configuration side data,
and a raw IVF never does — the ffmpeg CLI has no option to supply it. HandBrake
gets there by attaching AV_PKT_DATA_DOVI_CONF through the libavformat API; this
module does the equivalent as a post-mux box insert.

The insert grows the header, so every chunk offset moves and mdat is rewritten
by a streaming copy. Any file shape we do not fully understand (fragmented,
64-bit box sizes, offsets that would overflow stco) is left untouched and the
caller gets ``None`` — same degrade-gracefully posture as the rest of the DoVi
path.
"""

import os
import struct
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Tuple

DVVC_SIZE = 32
DBY1 = b"dby1"

# HandBrake libhb/dovi_common.c hb_dovi_levels[]:
# (level, max pixels/sec, max width, max Mbps main tier, max Mbps high tier)
_DV_LEVELS: List[Tuple[int, int, int, int, int]] = [
    (1, 22118400, 1280, 20, 50),
    (2, 27648000, 1280, 20, 50),
    (3, 49766400, 1920, 20, 70),
    (4, 62208000, 2560, 20, 70),
    (5, 124416000, 3840, 20, 70),
    (6, 199065600, 3840, 25, 130),
    (7, 248832000, 3840, 25, 130),
    (8, 398131200, 3840, 40, 130),
    (9, 497664000, 3840, 40, 130),
    (10, 995328000, 3840, 60, 240),
    (11, 995328000, 7680, 60, 240),
    (12, 1990656000, 7680, 120, 480),
    (13, 3981312000, 7680, 240, 800),
]

# AV1 spec Annex A max bitrate (Mbps) by seq_level_idx: (main tier, high tier).
# Levels the spec leaves undefined (2.2, 2.3, 3.2, 3.3, 4.2, 4.3) repeat the
# previous defined level's values.
_AV1_MAX_MBPS: Dict[int, Tuple[float, float]] = {
    0: (1.5, 1.5), 1: (3.0, 3.0), 2: (3.0, 3.0), 3: (3.0, 3.0),
    4: (6.0, 6.0), 5: (10.0, 10.0), 6: (10.0, 10.0), 7: (10.0, 10.0),
    8: (12.0, 30.0), 9: (20.0, 50.0), 10: (20.0, 50.0), 11: (20.0, 50.0),
    12: (30.0, 100.0), 13: (40.0, 160.0), 14: (60.0, 240.0), 15: (60.0, 240.0),
    16: (60.0, 240.0), 17: (100.0, 480.0), 18: (160.0, 800.0), 19: (160.0, 800.0),
}


def dovi_level(width: int, height: int, fps: float, av1_level_idx: int, high_tier: bool = False) -> int:
    """
    DV level for an AV1 stream, mirroring HandBrake's hb_dovi_level(): the
    lowest level whose pixel rate, width and bitrate ceilings all cover the
    stream. The bitrate side uses the AV1 level's cap (HandBrake's job max
    rate), not the actual bitrate — that is why a 3840x1604@24 encode signalled
    as AV1 level 5.0 (30 Mbps) lands on DV level 8, not 6.
    """
    idx = av1_level_idx if av1_level_idx in _AV1_MAX_MBPS else max(_AV1_MAX_MBPS)
    max_mbps = _AV1_MAX_MBPS[idx][1 if high_tier else 0]
    pps = width * height * fps
    col = 4 if high_tier else 3
    for row in _DV_LEVELS:
        if pps <= row[1] and width <= row[2] and max_mbps <= row[col]:
            return row[0]
    return _DV_LEVELS[-1][0]


def build_dvvc(profile: int, level: int, compat_id: int) -> bytes:
    """32-byte ``dvvC`` box: DV 1.0, RPU + base layer present, no enhancement layer."""
    bits = (
        (profile & 0x7F) << 41
        | (level & 0x3F) << 35
        | 1 << 34            # rpu_present_flag
        | 0 << 33            # el_present_flag
        | 1 << 32            # bl_present_flag
        | (compat_id & 0xF) << 28
        | 0 << 26            # dv_md_compression: none
    )
    payload = bytes([1, 0]) + bits.to_bytes(6, "big") + bytes(16)
    return struct.pack(">I4s", DVVC_SIZE, b"dvvC") + payload


_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


def _read_boxes(buf: bytes, start: int, end: int):
    """Yield (offset, size, type) for the boxes packed in buf[start:end]."""
    pos = start
    while pos + 8 <= end:
        size, btype = struct.unpack_from(">I4s", buf, pos)
        if size < 8 or pos + size > end:
            # size 0 (to EOF) and 1 (64-bit) never appear inside moov from ffmpeg
            raise ValueError(f"unsupported box size {size} for {btype!r}")
        yield pos, size, btype
        pos += size


def _top_level(f: BinaryIO, file_size: int) -> List[Tuple[int, int, bytes]]:
    boxes = []
    pos = 0
    while pos + 8 <= file_size:
        f.seek(pos)
        size, btype = struct.unpack(">I4s", f.read(8))
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
        elif size == 0:
            size = file_size - pos
        if size < 8 or pos + size > file_size:
            raise ValueError(f"bad top-level box {btype!r} size {size}")
        boxes.append((pos, size, btype))
        pos += size
    return boxes


def _find_av01(moov: bytes) -> Optional[Tuple[List[Tuple[int, int]], int, int]]:
    """
    Locate the video sample entry. Returns (ancestor chain of (offset, size) from
    moov down to av01, av01 offset, av01 size) with offsets relative to ``moov``.
    """
    def walk(start: int, end: int, chain: List[Tuple[int, int]]):
        for off, size, btype in _read_boxes(moov, start, end):
            here = chain + [(off, size)]
            if btype in _CONTAINERS:
                found = walk(off + 8, off + size, here)
                if found:
                    return found
            elif btype == b"stsd":
                # full box header (4) + entry_count (4) before the entries
                found = walk(off + 16, off + size, here)
                if found:
                    return found
            elif btype == b"av01":
                return here
        return None

    top = list(_read_boxes(moov, 0, len(moov)))
    if not top or top[0][2] != b"moov":
        return None
    chain = walk(8, len(moov), [(0, len(moov))])
    if not chain:
        return None
    return chain, chain[-1][0], chain[-1][1]


def _av01_info(moov: bytes, off: int, size: int) -> Optional[Dict[str, object]]:
    """Width, seq_level_idx_0 and tier from the av01 entry and its av1C child."""
    # box header 8 + 6 reserved + 2 dref idx + 16 pre-defined/reserved
    width, height = struct.unpack_from(">HH", moov, off + 8 + 24)
    # 78-byte visual sample entry body precedes the child boxes
    has_dovi = False
    av1c = None
    for coff, csize, ctype in _read_boxes(moov, off + 8 + 78, off + size):
        if ctype in (b"dvvC", b"dvcC"):
            has_dovi = True
        elif ctype == b"av1C":
            av1c = coff
    if av1c is None:
        return None
    return {
        "has_dovi": has_dovi, "width": width, "height": height,
        "level_idx": moov[av1c + 8 + 1] & 0x1F,
        "high_tier": bool(moov[av1c + 8 + 2] & 0x80),
    }


def _shift_chunk_offsets(moov: bytearray, deltas: List[Tuple[int, int]]) -> bool:
    """
    Add, to every stco/co64 entry, the total of ``deltas`` (original-file
    position, inserted length) whose position is at or before the entry.
    Returns False if a 32-bit stco entry would overflow.
    """
    def shift(o: int) -> int:
        return o + sum(n for pos, n in deltas if o >= pos)

    def walk(start: int, end: int) -> bool:
        for off, size, btype in _read_boxes(bytes(moov[start:end]), 0, end - start):
            off += start
            if btype in _CONTAINERS:
                if not walk(off + 8, off + size):
                    return False
            elif btype == b"stco":
                n = struct.unpack_from(">I", moov, off + 12)[0]
                for i in range(n):
                    p = off + 16 + i * 4
                    v = shift(struct.unpack_from(">I", moov, p)[0])
                    if v > 0xFFFFFFFF:
                        return False
                    struct.pack_into(">I", moov, p, v)
            elif btype == b"co64":
                n = struct.unpack_from(">I", moov, off + 12)[0]
                for i in range(n):
                    p = off + 16 + i * 8
                    struct.pack_into(">Q", moov, p, shift(struct.unpack_from(">Q", moov, p)[0]))
        return True

    return walk(8, len(moov))


def _video_fps(moov: bytes) -> Optional[float]:
    """Average fps of the av01 track: sample count / media duration."""
    found = _find_av01(moov)
    if not found:
        return None
    trak_off, trak_size = found[0][1]  # chain is [moov, trak, mdia, ...]
    timescale = duration = None
    samples = 0
    stack = [(trak_off + 8, trak_off + trak_size)]
    while stack:
        start, end = stack.pop()
        for off, size, btype in _read_boxes(moov, start, end):
            if btype in _CONTAINERS:
                stack.append((off + 8, off + size))
            elif btype == b"mdhd":
                ver = moov[off + 8]
                if ver == 1:
                    timescale, duration = struct.unpack_from(">IQ", moov, off + 8 + 20)
                else:
                    timescale, duration = struct.unpack_from(">II", moov, off + 8 + 12)
            elif btype == b"stts":
                n = struct.unpack_from(">I", moov, off + 12)[0]
                for i in range(n):
                    samples += struct.unpack_from(">I", moov, off + 16 + i * 8)[0]
    if not (timescale and duration and samples):
        return None
    return samples * timescale / duration


def _copy(src: BinaryIO, dst: BinaryIO, start: int, end: int) -> None:
    src.seek(start)
    left = end - start
    while left > 0:
        chunk = src.read(min(left, 8 << 20))
        if not chunk:
            raise IOError("unexpected end of file while copying")
        dst.write(chunk)
        left -= len(chunk)


def inject_dolby_vision(
    src_path: Path,
    dst_path: Path,
    *,
    profile: int = 10,
    compat_id: int = 1,
    fps: Optional[float] = None,
) -> Optional[Dict[str, int]]:
    """
    Write ``dst_path`` = ``src_path`` plus a ``dvvC`` box on the AV1 sample entry
    and the ``dby1`` compatible brand. Returns the DV config that was written
    ({"profile", "level", "compat_id"}), or None when the file was left alone
    (``dst_path`` is not created then). ``fps`` overrides the value derived from
    the track's own timing.
    """
    src_path, dst_path = Path(src_path), Path(dst_path)
    try:
        file_size = src_path.stat().st_size
        with open(src_path, "rb") as f:
            top = _top_level(f, file_size)
            types = [t for _, _, t in top]
            if b"moof" in types or b"moov" not in types or b"ftyp" not in types:
                return None
            ftyp_off, ftyp_size, _ = top[types.index(b"ftyp")]
            moov_off, moov_size, _ = top[types.index(b"moov")]
            f.seek(moov_off)
            moov = f.read(moov_size)

            found = _find_av01(moov)
            if not found:
                return None
            chain, av01_off, av01_size = found
            info = _av01_info(moov, av01_off, av01_size)
            if not info or info["has_dovi"]:
                return None

            rate = fps if fps else _video_fps(moov)
            if not rate:
                return None
            level = dovi_level(
                int(info["width"]), int(info["height"]), float(rate),
                int(info["level_idx"]), bool(info["high_tier"]),
            )
            dvvc = build_dvvc(profile, level, compat_id)

            # ftyp: append the dby1 brand unless already listed
            f.seek(ftyp_off)
            ftyp = bytearray(f.read(ftyp_size))
            brands_pos = 16
            has_dby1 = any(ftyp[p:p + 4] == DBY1 for p in range(brands_pos, len(ftyp) - 3, 4))
            ftyp_add = b"" if has_dby1 else DBY1
            if ftyp_add:
                ftyp += ftyp_add
                struct.pack_into(">I", ftyp, 0, len(ftyp))

            # moov: insert dvvC at the end of the av01 entry, grow every ancestor
            new_moov = bytearray(moov)
            insert_at = av01_off + av01_size
            new_moov[insert_at:insert_at] = dvvc
            for off, size in chain:
                struct.pack_into(">I", new_moov, off, size + len(dvvc))

            deltas: List[Tuple[int, int]] = []
            if ftyp_add:
                deltas.append((ftyp_off + ftyp_size, len(ftyp_add)))
            # chunk offsets that point past the insertion point in the *original* file
            deltas.append((moov_off + insert_at, len(dvvc)))
            if not _shift_chunk_offsets(new_moov, deltas):
                return None

            with open(dst_path, "wb") as out:
                out.write(ftyp)
                cursor = ftyp_off + ftyp_size
                # everything between ftyp and moov (free/mdat when moov is last)
                _copy(f, out, cursor, moov_off)
                out.write(new_moov)
                _copy(f, out, moov_off + moov_size, file_size)
        return {"profile": profile, "level": level, "compat_id": compat_id}
    except (ValueError, IOError, OSError, struct.error):
        try:
            dst_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

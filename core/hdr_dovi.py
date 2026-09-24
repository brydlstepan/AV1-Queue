"""
HDR10 & Dolby Vision Metadata Processor
Detects HDR10 / HLG / HDR10+ / Dolby Vision from ffprobe (stream side data plus
sampled frame SEI) for the queue's skip / quarantine policy, output naming and
the post-encode check of HandBrake's output.

Policy (see README.md "HDR & Dolby Vision"): a DV source whose base layer
stands on its own (profile 7 / 8.1, BL signal-compatibility id != 0) is always
encoded as HDR10; a profile-5 source (compat id 0) is skipped upstream. The
encode itself — HDR10 static metadata, HDR10+ and (settings.preserve_dovi_rpu)
the DoVi RPU, including its P7 → 8.1 conversion — is HandBrake's.
"""

import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"


_FILENAME_DOVI = re.compile(
    r"(?:^|[.\-_\[\(])(?:dv|dovi|dolby[\.\-_]?vision)(?:$|[.\-_\]\)])",
    re.I,
)
# "+" cannot occur inside a word, so no trailing boundary is required — this
# also catches compound tags like "HDR10+DV". "HDR10Plus" needs one.
_FILENAME_HDR10PLUS = re.compile(
    r"(?:^|[.\-_\[\(])hdr10(?:\+|plus(?:$|[.\-_\]\)]))",
    re.I,
)
# Intentionally hdr10 only — bare "hdr" false-positives (e.g. HDREMUX)
_FILENAME_HDR10 = re.compile(
    r"(?:^|[.\-_\[\(])hdr10(?:$|[.\-_\]\)])",
    re.I,
)
_FILENAME_HLG = re.compile(
    r"(?:^|[.\-_\[\(])hlg(?:$|[.\-_\]\)])",
    re.I,
)
# Stream-tag HDR signal. Same reasoning as _FILENAME_HDR10: match real tokens,
# never a bare "hdr" substring that also occurs in "HDRip"/"HDREMUX".
_TAG_HDR_TOKEN = re.compile(
    r"\b(?:hdr10(?:\+|plus)?|dolby[\s.\-_]?vision|dovi|hlg|pq|smpte2084|bt2020)\b",
    re.I,
)


def _frac_to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(Fraction(str(val).strip()))
    except Exception:
        try:
            return float(val)
        except Exception:
            return None


def _normalize_mastering_nits(lmax: float, lmin: float) -> Tuple[float, float]:
    """
    Ensure luminance is in cd/m² (nits) for the mastering-display string.

    FFprobe usually already returns nits (e.g. 1000.0 or \"10000000/10000\").
    Some paths still emit HEVC SEI raw units (0.0001 cd/m²), e.g. max=10000000.
    Values above the practical HDR ceiling are treated as raw SEI and scaled.
    """
    if lmax > 10000.0:
        print(
            f"[!] Mastering max-luminance {lmax:g} exceeds the practical HDR "
            f"ceiling (10000 nits) — assuming raw HEVC SEI units "
            f"(0.0001 cd/m²) and scaling by 1/10000. If this is an exotic "
            f"master with genuine >10000-nit luminance, the reported mastering "
            f"display will be wrong."
        )
        return lmax / 10000.0, lmin / 10000.0
    return lmax, lmin


def _fmt_nit(val: float, *, is_min: bool) -> str:
    """Format nits without truncating exotic min floors."""
    if is_min:
        s = f"{val:.12f}".rstrip("0").rstrip(".")
    else:
        s = f"{val:.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _format_mastering(side: Dict[str, Any]) -> Optional[str]:
    gx = _frac_to_float(side.get("green_x"))
    gy = _frac_to_float(side.get("green_y"))
    bx = _frac_to_float(side.get("blue_x"))
    by = _frac_to_float(side.get("blue_y"))
    rx = _frac_to_float(side.get("red_x"))
    ry = _frac_to_float(side.get("red_y"))
    wx = _frac_to_float(side.get("white_point_x"))
    wy = _frac_to_float(side.get("white_point_y"))
    lmax = _frac_to_float(side.get("max_luminance"))
    lmin = _frac_to_float(side.get("min_luminance"))
    if None in (gx, gy, bx, by, rx, ry, wx, wy, lmax, lmin):
        return None
    lmax, lmin = _normalize_mastering_nits(lmax, lmin)
    return (
        f"G({gx:.4f},{gy:.4f})B({bx:.4f},{by:.4f})"
        f"R({rx:.4f},{ry:.4f})WP({wx:.4f},{wy:.4f})"
        f"L({_fmt_nit(lmax, is_min=False)},{_fmt_nit(lmin, is_min=True)})"
    )


_SVT_CHROMA_POSITION = {"left": "vertical", "topleft": "colocated"}


def svt_chroma_flags(chroma_location: Any) -> List[str]:
    """ffprobe chroma_location → SVT-AV1 chroma-sample-position (left → vertical)."""
    pos = _SVT_CHROMA_POSITION.get(str(chroma_location or "").strip().lower())
    return ["--chroma-sample-position", pos] if pos else []


def _parse_content_light(side: Dict[str, Any]) -> Optional[str]:
    max_cll = side.get("max_content")
    max_fall = side.get("max_average")
    if max_cll is None or max_fall is None:
        return None
    try:
        return f"{int(float(max_cll))},{int(float(max_fall))}"
    except Exception:
        return None


def _side_data_mastering_cll(side_list: Any) -> Tuple[Optional[str], Optional[str]]:
    """Pull mastering-display / content-light strings from a side_data_list."""
    mastering = None
    content_light = None
    for side in side_list or []:
        if not isinstance(side, dict):
            continue
        side_type = str(side.get("side_data_type") or "").upper()
        if "MASTERING" in side_type:
            mastering = _format_mastering(side) or mastering
        if "CONTENT LIGHT" in side_type or side_type.endswith("CLL"):
            content_light = _parse_content_light(side) or content_light
    return mastering, content_light


class HDRDoviProcessor:
    def __init__(self, bin_dir: Path = BIN_DIR):
        self.bin_dir = bin_dir
        self.ffprobe = bin_dir / "ffprobe.exe"
        self.ffmpeg = bin_dir / "ffmpeg.exe"

    def probe_video_streams(self, file_path: Path) -> Dict[str, Any]:
        """Probes video, audio, and subtitle streams using ffprobe JSON output."""
        # Explicit stream_side_data so DoVi/HDR10+/mastering detection does not
        # depend on whether -show_streams alone includes nested side data.
        cmd = [
            str(self.ffprobe),
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-show_entries", "stream_side_data",
            str(file_path),
        ]
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            if res.returncode != 0:
                raise RuntimeError((res.stderr or "").strip() or f"ffprobe exit {res.returncode}")
            return json.loads(res.stdout)
        except Exception as e:
            print(f"[!] Error probing file {file_path}: {e}")
            # probe_failed distinguishes "probed and found nothing" from "could not
            # probe" — the caller must fail closed on DoVi rather than assume SDR.
            return {"streams": [], "format": {}, "probe_failed": True}

    def probe_frame_mastering_cll(
        self,
        file_path: Path,
        *,
        seek_sec: Optional[float] = None,
        max_frames: int = 8,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Sample frame SEI for mastering display / CLL.

        Many HDR/DoVi remuxes only expose these on frames, not stream side_data.
        HandBrake's UI treats files without mastering metadata as non-HDR10.
        """
        if not self.ffprobe.is_file():
            return None, None
        interval = f"%+#{max(1, int(max_frames))}"
        if seek_sec is not None and seek_sec > 0:
            interval = f"{seek_sec:.3f}{interval}"
        cmd = [
            str(self.ffprobe),
            "-v", "quiet",
            "-select_streams", "v:0",
            "-read_intervals", interval,
            "-show_frames",
            "-show_entries", "frame=side_data_list",
            "-of", "json",
            str(file_path),
        ]
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", timeout=60
            )
            if res.returncode != 0:
                return None, None
            data = json.loads(res.stdout or "{}")
        except Exception as e:
            print(f"[!] Frame HDR metadata probe failed for {file_path}: {e}")
            return None, None

        mastering = content_light = None
        for frame in data.get("frames") or []:
            m, c = _side_data_mastering_cll(frame.get("side_data_list"))
            mastering = mastering or m
            content_light = content_light or c
            if mastering and content_light:
                break
        return mastering, content_light

    def probe_frame_hdr10plus(
        self,
        file_path: Path,
        *,
        seek_sec: Optional[float] = None,
        max_frames: int = 8,
    ) -> bool:
        """
        Sample frame SEI for an HDR10+ (SMPTE ST 2094-40) side_data_type.

        HDR10+ dynamic metadata lives in frame SEI, not stream side_data, so
        ``ffprobe -show_streams`` rarely surfaces it and a genuine HDR10+ source
        is otherwise missed and silently degraded to HDR10. Mirrors
        probe_frame_mastering_cll's frame-sampling pattern.
        """
        if not self.ffprobe.is_file():
            return False
        interval = f"%+#{max(1, int(max_frames))}"
        if seek_sec is not None and seek_sec > 0:
            interval = f"{seek_sec:.3f}{interval}"
        cmd = [
            str(self.ffprobe),
            "-v", "quiet",
            "-select_streams", "v:0",
            "-read_intervals", interval,
            "-show_frames",
            "-show_entries", "frame=side_data_list",
            "-of", "json",
            str(file_path),
        ]
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", timeout=60
            )
            if res.returncode != 0:
                return False
            data = json.loads(res.stdout or "{}")
        except Exception as e:
            print(f"[!] Frame HDR10+ probe failed for {file_path}: {e}")
            return False

        for frame in data.get("frames") or []:
            for side in frame.get("side_data_list") or []:
                st = str(side.get("side_data_type") or "").upper()
                st_ns = st.replace(" ", "")
                if (
                    "2094-40" in st
                    or "209440" in st_ns
                    or "HDR10+" in st
                    or "HDR10PLUS" in st_ns
                    or ("DYNAMIC" in st and "HDR" in st)
                ):
                    return True
        return False

    def analyze_hdr_and_dovi(
        self,
        file_path: Path,
        temp_dir: Path = None,
        probe: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Analyzes video stream for HDR10 / HDR10+ / Dolby Vision.
        Pass probe= to reuse an existing ffprobe JSON dict (avoids a second probe).
        """
        _ = temp_dir
        file_path = Path(file_path)
        probe = probe if isinstance(probe, dict) else self.probe_video_streams(file_path)
        video_stream = None
        for s in probe.get("streams", []):
            if s.get("codec_type") == "video":
                video_stream = s
                break

        name = file_path.name
        fname_dovi = bool(_FILENAME_DOVI.search(name))
        fname_hdr10plus = bool(_FILENAME_HDR10PLUS.search(name))
        fname_hdr10 = bool(_FILENAME_HDR10.search(name)) and not fname_dovi and not fname_hdr10plus
        fname_hlg = bool(_FILENAME_HLG.search(name))

        empty = {
            "is_hdr": False,
            "is_dovi": False,
            "dovi_filename_hint": False,
            "is_hdr10plus": False,
            "hdr10plus_filename_hint": False,
            "is_hlg": False,
            "dovi_profile": None,
            "color_primaries": "",
            "color_transfer": "",
            "color_space": "",
            "chroma_location": "",
            "source": "none",
        }

        probe_failed = bool(probe.get("probe_failed"))

        if not video_stream:
            if fname_dovi or fname_hdr10 or fname_hdr10plus or fname_hlg:
                return {
                    "is_hdr": True,
                    "is_dovi": False,
                    # Could not read the stream, but the name says Dolby Vision.
                    # Profile 5 is unencodable, so the caller must quarantine
                    # rather than assume this is a safe P7/P8.1 base layer.
                    "dovi_unverified": bool(fname_dovi) and probe_failed,
                    "probe_failed": probe_failed,
                    "dovi_filename_hint": bool(fname_dovi),
                    # Probe-only, like is_dovi — a filename token is a hint, not proof
                    "is_hdr10plus": False,
                    "hdr10plus_filename_hint": bool(fname_hdr10plus),
                    "is_hlg": bool(fname_hlg),
                    "dovi_profile": None,
                    "color_primaries": "",
                    "color_transfer": "arib-std-b67" if fname_hlg else "smpte2084",
                    "color_space": "",
                    "chroma_location": "",
                    "source": "filename",
                }
            empty["probe_failed"] = probe_failed
            return empty

        color_primaries = str(video_stream.get("color_primaries") or "")
        color_transfer = str(video_stream.get("color_transfer") or "")
        color_space = str(video_stream.get("color_space") or "")
        tags = video_stream.get("tags") or {}
        tag_blob = " ".join(str(v) for v in tags.values()).lower()

        prim_l = color_primaries.lower()
        trc_l = color_transfer.lower()
        space_l = color_space.lower()

        is_hdr10 = (
            "bt2020" in prim_l
            or "smpte2084" in trc_l
            or "pq" in trc_l
            or "arib-std-b67" in trc_l
            or "hlg" in trc_l
            or "bt2020" in space_l
            # Whole-token match only: a bare "hdr" substring also matches
            # release-name noise like "HDRip"/"HDREMUX" in a title tag and
            # would tag a plain SDR source as PQ/BT.2020.
            or bool(_TAG_HDR_TOKEN.search(tag_blob))
        )

        is_dovi = False
        is_hdr10plus = False
        dovi_profile = None
        dovi_level = None
        dovi_compat_id = None
        mastering = None
        content_light = None

        for side in video_stream.get("side_data_list") or []:
            side_type = str(side.get("side_data_type") or "").upper()
            if (
                "DOVI" in side_type
                or "DOLBY VISION" in side_type
                or side.get("dv_profile") is not None
                or side.get("dv_version_major") is not None
            ):
                is_dovi = True
                if side.get("dv_profile") is not None:
                    dovi_profile = side.get("dv_profile")
                if side.get("dv_level") is not None:
                    dovi_level = side.get("dv_level")
                if side.get("dv_bl_signal_compatibility_id") is not None:
                    dovi_compat_id = side.get("dv_bl_signal_compatibility_id")
            if "HDR10+" in side_type or "DYNAMIC HDR10+" in side_type or "HDR10PLUS" in side_type.replace(" ", ""):
                is_hdr10plus = True

        m0, c0 = _side_data_mastering_cll(video_stream.get("side_data_list"))
        mastering, content_light = m0, c0

        # Filenames only ever *hint*. is_dovi / is_hdr10plus require probe side_data,
        # so the output name can never claim a layer we did not actually find.
        dovi_filename_hint = bool(fname_dovi)
        hdr10plus_filename_hint = bool(fname_hdr10plus)
        if fname_hdr10 or fname_hlg:
            is_hdr10 = True
        if is_dovi or is_hdr10plus or dovi_filename_hint or hdr10plus_filename_hint:
            is_hdr10 = True

        is_hlg = fname_hlg or "arib" in trc_l or "hlg" in trc_l

        # Mastering/CLL are usually frame SEI — sample start, mid, and late frames
        if (is_hdr10 or is_dovi or is_hdr10plus or dovi_filename_hint or hdr10plus_filename_hint) and (
            not mastering or not content_light
        ):
            seeks: List[Optional[float]] = [None]
            try:
                dur = float((probe.get("format") or {}).get("duration") or 0)
            except (TypeError, ValueError):
                dur = 0.0
            if dur > 30:
                seeks.extend([dur * 0.35, max(0.0, dur - 12.0)])
            elif dur > 5:
                seeks.append(dur * 0.5)
            for sk in seeks:
                fm, fc = self.probe_frame_mastering_cll(file_path, seek_sec=sk, max_frames=6)
                mastering = mastering or fm
                content_light = content_light or fc
                if mastering and content_light:
                    break

        # HDR10+ (SMPTE ST 2094-40) lives in frame SEI, not stream side_data, so a
        # genuine HDR10+ source is missed by -show_streams and silently degraded
        # to HDR10. Sample frames when the stream side_data did not flag it. (§4.3)
        if not is_hdr10plus and (
            is_hdr10 or is_dovi or hdr10plus_filename_hint or dovi_filename_hint
        ):
            hp_seeks: List[Optional[float]] = [None]
            try:
                _dur = float((probe.get("format") or {}).get("duration") or 0)
            except (TypeError, ValueError):
                _dur = 0.0
            if _dur > 30:
                hp_seeks.append(_dur * 0.35)
            for sk in hp_seeks:
                if self.probe_frame_hdr10plus(file_path, seek_sec=sk, max_frames=6):
                    is_hdr10plus = True
                    break

        _, ff_range = self._color_range_flags(video_stream)

        source = "probe"
        if (fname_dovi or fname_hdr10 or fname_hdr10plus or fname_hlg) and not (
            "bt2020" in prim_l or "smpte2084" in trc_l or "hlg" in trc_l
        ):
            source = "filename+probe" if (is_hdr10 or is_dovi or is_hdr10plus or dovi_filename_hint or hdr10plus_filename_hint) else "filename"

        return {
            "is_hdr": bool(is_hdr10 or is_dovi or is_hdr10plus or dovi_filename_hint or hdr10plus_filename_hint),
            "is_dovi": is_dovi,
            "dovi_filename_hint": dovi_filename_hint,
            "is_hdr10plus": is_hdr10plus,
            "hdr10plus_filename_hint": hdr10plus_filename_hint,
            # HLG is encoded correctly (transfer 18) but shares the HDR10 filename
            # tag; exposed so the UI can still name the source accurately.
            "is_hlg": bool(is_hlg and not is_dovi and not dovi_filename_hint),
            "dovi_profile": dovi_profile,
            "dovi_level": dovi_level,
            "dovi_compat_id": dovi_compat_id,
            "mastering_display": mastering,
            "content_light": content_light,
            # Frame-level SEI sampling ran to completion. Cached analyses from
            # before this flag existed lack it, so the encode step knows to
            # re-analyze those once instead of on every single run.
            "frame_probed": True,
            "color_primaries": color_primaries,
            "color_transfer": color_transfer or ("smpte2084" if (is_hdr10 or is_dovi or is_hdr10plus or dovi_filename_hint or hdr10plus_filename_hint) else ""),
            "color_space": color_space,
            "color_range": ff_range,
            # ffprobe's name for the source's 4:2:0 chroma siting; passed to
            # HandBrake as chroma-sample-position (see svt_chroma_flags)
            "chroma_location": str(video_stream.get("chroma_location") or ""),
            "source": source,
        }

    @staticmethod
    def _color_range_flags(video_stream: Optional[Dict[str, Any]]) -> Tuple[str, str]:
        """Return (AV1 color_range value, ffmpeg color_range name): 0 / tv = limited, 1 / pc = full."""
        cr = ""
        if isinstance(video_stream, dict):
            cr = str(video_stream.get("color_range") or "").lower()
        if cr in ("pc", "full", "jpeg", "1"):
            return "1", "pc"
        return "0", "tv"

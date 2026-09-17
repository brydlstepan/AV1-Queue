"""
HDR10 & Dolby Vision Metadata Processor
Detects HDR10 / HDR10+ / Dolby Vision from ffprobe and builds
SVT-AV1-Tritium color flags.

Policy (see README.md "HDR & Dolby Vision"): a DV source whose base layer
stands on its own (profile 7 / 8.1, BL signal-compatibility id != 0) is always
encoded as HDR10; a profile-5 source (compat id 0) is skipped upstream. RPU
passthrough for those P7/P8.1 sources is opt-in (settings.preserve_dovi_rpu,
off by default) and best-effort — same degrade-gracefully posture as HDR10+:
  - HDR10+ JSON via hdr10plus_tool (if present) → --hdr10plus-json
  - DoVi RPU via dovi_tool extract-rpu (if present and the setting is on) →
    --dolby-vision-rpu. The raw extracted RPU is passed through unconverted:
    dovi_tool's documented `convert` modes only retarget HEVC profiles (8.1
    Blu-ray compat, MEL, 8.4), none of which apply to AV1/profile 10, and
    SVT-AV1-Tritium's --dolby-vision-rpu is built on libdovi (the same
    library dovi_tool uses) to do the AV1 packing itself.
Otherwise falls back to static HDR10 color signaling (primaries / transfer / matrix).
"""

import json
import re
import subprocess
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"


def _parse_timecode(val: Any) -> Optional[float]:
    """Parse seconds or HH:MM:SS(.ms) into float seconds."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    parts = s.split(":")
    try:
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
    except ValueError:
        return None
    return None

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
    Ensure luminance is in cd/m² (nits) for SVT --mastering-display.

    FFprobe usually already returns nits (e.g. 1000.0 or \"10000000/10000\").
    Some paths still emit HEVC SEI raw units (0.0001 cd/m²), e.g. max=10000000.
    Values above the practical HDR ceiling are treated as raw SEI and scaled.
    """
    if lmax > 10000.0:
        print(
            f"[!] Mastering max-luminance {lmax:g} exceeds the practical HDR "
            f"ceiling (10000 nits) — assuming raw HEVC SEI units "
            f"(0.0001 cd/m²) and scaling by 1/10000. If this is an exotic "
            f"master with genuine >10000-nit luminance, the --mastering-display "
            f"value will be wrong."
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
        self.hdr10plus_tool = bin_dir / "hdr10plus_tool.exe"
        self.dovi_tool = bin_dir / "dovi_tool.exe"

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
        Returns SVT-AV1-Tritium compatible color metadata flags when HDR/DoVi is present.
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
            "svt_flags": [],
            "ffmpeg_color": [],
            "color_primaries": "",
            "color_transfer": "",
            "color_space": "",
            "source": "none",
        }

        probe_failed = bool(probe.get("probe_failed"))

        if not video_stream:
            if fname_dovi or fname_hdr10 or fname_hdr10plus or fname_hlg:
                transfer = "18" if fname_hlg else "16"
                flags = [
                    "--color-primaries", "9",
                    "--transfer-characteristics", transfer,
                    "--matrix-coefficients", "9",
                    "--color-range", "0",
                ]
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
                    "svt_flags": flags,
                    "ffmpeg_color": self._ffmpeg_color_args(transfer),
                    "color_primaries": "",
                    "color_transfer": "arib-std-b67" if fname_hlg else "smpte2084",
                    "color_space": "",
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

        svt_range, ff_range = self._color_range_flags(video_stream)

        svt_flags: List[str] = []
        ffmpeg_color: List[str] = []
        if is_hdr10 or is_dovi or is_hdr10plus or dovi_filename_hint:
            transfer = "18" if is_hlg and not is_dovi and not dovi_filename_hint else "16"
            svt_flags.extend([
                "--color-primaries", "9",
                "--transfer-characteristics", transfer,
                "--matrix-coefficients", "9",
                "--color-range", svt_range,
            ])
            if mastering:
                svt_flags.extend(["--mastering-display", mastering])
            if content_light:
                svt_flags.extend(["--content-light", content_light])
            ffmpeg_color = self._ffmpeg_color_args(transfer, color_range=ff_range)

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
            "svt_flags": svt_flags,
            "ffmpeg_color": ffmpeg_color,
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
            "source": source,
        }

    def _ffmpeg_extract_hevc_annexb(
        self,
        source_file: Path,
        hevc_path: Path,
        *,
        log: Callable[[str], None],
        percent_cb: Optional[Callable[[float], None]] = None,
        duration_sec: Optional[float] = None,
        trim_start: Optional[str] = None,
        trim_end: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        label: str = "HEVC",
        map_spec: str = "0:v:0",
    ) -> bool:
        """Demux video to annex-B HEVC with live ffmpeg -progress (updates percent_cb 0–100)."""
        hevc_path = Path(hevc_path)
        if hevc_path.exists():
            try:
                hevc_path.unlink()
            except Exception:
                pass

        cmd: List[str] = [
            str(self.ffmpeg), "-y", "-hide_banner", "-nostdin",
            "-loglevel", "error", "-stats_period", "0.5",
            "-progress", "pipe:1",
        ]
        t0 = _parse_timecode(trim_start)
        t1 = _parse_timecode(trim_end)
        # Input-side seek keeps copy demux fast on large remuxes (test mode)
        if t0 is not None and t0 > 0:
            cmd.extend(["-ss", str(trim_start)])
        cmd.extend(["-i", str(source_file)])
        if t0 is not None and t1 is not None and t1 > t0:
            cmd.extend(["-t", f"{t1 - t0:.3f}"])
        elif t1 is not None:
            cmd.extend(["-to", str(trim_end)])
        cmd.extend([
            "-map", str(map_spec or "0:v:0"), "-c", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "hevc", str(hevc_path),
        ])

        span = duration_sec
        if t0 is not None and t1 is not None and t1 > t0:
            span = t1 - t0
        elif t0 is not None and span is not None and span > t0:
            span = span - t0

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as e:
            log(f"{label} extract spawn failed: {e}")
            return False

        last_pct = -1.0
        last_emit = 0.0
        out_time_sec = 0.0

        def emit_pct(raw: float, force: bool = False):
            nonlocal last_pct, last_emit
            if not percent_cb:
                return
            pct = max(0.0, min(100.0, float(raw)))
            now = time.monotonic()
            if not force and pct < last_pct + 0.5 and (now - last_emit) < 0.4:
                return
            last_pct = pct
            last_emit = now
            try:
                percent_cb(pct)
            except Exception:
                pass

        emit_pct(0.0, force=True)
        assert proc.stdout is not None
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        pass
                    log(f"{label} extract cancelled")
                    return False
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                line = line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        out_time_sec = int(line.split("=", 1)[1]) / 1_000_000.0
                    except ValueError:
                        pass
                    if span and span > 0:
                        emit_pct(100.0 * out_time_sec / span)
                elif line.startswith("out_time="):
                    # fallback HH:MM:SS.micro
                    tc = _parse_timecode(line.split("=", 1)[1])
                    if tc is not None:
                        out_time_sec = tc
                        if span and span > 0:
                            emit_pct(100.0 * out_time_sec / span)
                elif line == "progress=end":
                    emit_pct(100.0, force=True)
            proc.wait(timeout=30)
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            log(f"{label} extract failed: {e}")
            return False

        err = ""
        try:
            if proc.stderr:
                err = proc.stderr.read() or ""
        except Exception:
            pass

        if proc.returncode != 0 or not hevc_path.is_file() or hevc_path.stat().st_size < 64:
            log(f"{label} extract failed: {err[-400:] if err else f'exit {proc.returncode}'}")
            return False
        emit_pct(100.0, force=True)
        return True

    @staticmethod
    def verify_hdr10plus_json(json_path: Path) -> Optional[str]:
        """Validate extracted HDR10+ JSON. Returns None when usable, else a reason."""
        json_path = Path(json_path)
        if not json_path.is_file():
            return "HDR10+ JSON was not produced"
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            return f"HDR10+ JSON is not valid JSON ({e})"
        scenes = data.get("SceneInfo") if isinstance(data, dict) else None
        if not isinstance(scenes, list) or not scenes:
            return "HDR10+ JSON contains no SceneInfo entries"
        return None

    def extract_hdr10plus_json(
        self,
        source_file: Path,
        out_json: Path,
        progress_cb: Optional[Callable[[str], None]] = None,
        percent_cb: Optional[Callable[[float], None]] = None,
        duration_sec: Optional[float] = None,
        trim_start: Optional[str] = None,
        trim_end: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[Path]:
        """
        Extract HDR10+ JSON via hdr10plus_tool (required).

        Prefer the tool's native MKV/HEVC readers; only demux with ffmpeg when
        the container isn't something hdr10plus_tool accepts directly.
        """
        if not self.hdr10plus_tool.is_file():
            if progress_cb:
                progress_cb(
                    "hdr10plus_tool.exe missing from bin/ — run setup to install "
                    "(quietvoid/hdr10plus_tool). HDR10+ JSON inject skipped."
                )
            return None
        if not self.ffmpeg.is_file():
            return None

        source_file = Path(source_file)
        out_json = Path(out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        hevc_path: Optional[Path] = None
        extract_input = source_file

        def log(msg: str):
            if progress_cb:
                progress_cb(msg)

        suffix = source_file.suffix.lower()
        # hdr10plus_tool reads HEVC annex-B and Matroska natively
        native_ok = suffix in (".mkv", ".hevc", ".h265", ".bin")
        # Trimmed ranges / non-MKV containers still need a demuxed annex-B span
        need_demux = (not native_ok) or (trim_start is not None or trim_end is not None)

        if need_demux:
            hevc_path = out_json.with_suffix(".hevc")

            def map_hevc_pct(p: float):
                if percent_cb:
                    percent_cb(max(0.0, min(90.0, p * 0.90)))

            log("Extracting HEVC bitstream for HDR10+…")
            ok = self._ffmpeg_extract_hevc_annexb(
                source_file,
                hevc_path,
                log=log,
                percent_cb=map_hevc_pct,
                duration_sec=duration_sec,
                trim_start=trim_start,
                trim_end=trim_end,
                cancel_event=cancel_event,
                label="HEVC",
            )
            if not ok:
                return None
            extract_input = hevc_path
        else:
            if percent_cb:
                try:
                    percent_cb(50.0)
                except Exception:
                    pass

        if cancel_event is not None and cancel_event.is_set():
            if hevc_path:
                try:
                    hevc_path.unlink(missing_ok=True)
                except Exception:
                    pass
            return None

        log("Extracting HDR10+ JSON (hdr10plus_tool)…")
        if percent_cb:
            try:
                percent_cb(92.0)
            except Exception:
                pass
        ht = subprocess.run(
            [str(self.hdr10plus_tool), "extract", str(extract_input), "-o", str(out_json)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if hevc_path:
            try:
                hevc_path.unlink(missing_ok=True)
            except Exception:
                pass
        if ht.returncode != 0 or not out_json.is_file() or out_json.stat().st_size < 8:
            log(f"hdr10plus_tool extract failed: {(ht.stderr or ht.stdout or '')[-400:]}")
            return None
        if percent_cb:
            try:
                percent_cb(100.0)
            except Exception:
                pass
        log(f"HDR10+ JSON ready ({out_json.name}, {out_json.stat().st_size} bytes)")
        return out_json

    @staticmethod
    def verify_dovi_rpu(rpu_path: Path) -> Optional[str]:
        """Validate an extracted DoVi RPU. Returns None when usable, else a reason."""
        rpu_path = Path(rpu_path)
        if not rpu_path.is_file():
            return "DoVi RPU was not produced"
        if rpu_path.stat().st_size < 64:
            return "DoVi RPU file is implausibly small"
        return None

    def extract_dovi_rpu(
        self,
        source_file: Path,
        out_rpu: Path,
        progress_cb: Optional[Callable[[str], None]] = None,
        percent_cb: Optional[Callable[[float], None]] = None,
        duration_sec: Optional[float] = None,
        trim_start: Optional[str] = None,
        trim_end: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[Path]:
        """
        Extract a raw Dolby Vision RPU via dovi_tool (opt-in, settings.preserve_dovi_rpu).

        Only meaningful for P7/P8.1 sources whose base layer is already valid HDR10 —
        callers must gate this on dovi_compat_id != 0 and not profile 5, same as the
        existing "base layer stands on its own" policy. Mirrors
        extract_hdr10plus_json's demux-then-extract shape.
        """
        if not self.dovi_tool.is_file():
            if progress_cb:
                progress_cb(
                    "dovi_tool.exe missing from bin/ — run setup to install "
                    "(quietvoid/dovi_tool). DoVi RPU passthrough skipped."
                )
            return None
        if not self.ffmpeg.is_file():
            return None

        source_file = Path(source_file)
        out_rpu = Path(out_rpu)
        out_rpu.parent.mkdir(parents=True, exist_ok=True)
        hevc_path: Optional[Path] = None
        extract_input = source_file

        def log(msg: str):
            if progress_cb:
                progress_cb(msg)

        suffix = source_file.suffix.lower()
        # dovi_tool reads HEVC annex-B and Matroska natively
        native_ok = suffix in (".mkv", ".hevc", ".h265", ".bin")
        need_demux = (not native_ok) or (trim_start is not None or trim_end is not None)

        if need_demux:
            hevc_path = out_rpu.with_suffix(".hevc")

            def map_hevc_pct(p: float):
                if percent_cb:
                    percent_cb(max(0.0, min(90.0, p * 0.90)))

            log("Extracting HEVC bitstream for Dolby Vision RPU…")
            ok = self._ffmpeg_extract_hevc_annexb(
                source_file,
                hevc_path,
                log=log,
                percent_cb=map_hevc_pct,
                duration_sec=duration_sec,
                trim_start=trim_start,
                trim_end=trim_end,
                cancel_event=cancel_event,
                label="HEVC",
            )
            if not ok:
                return None
            extract_input = hevc_path
        else:
            if percent_cb:
                try:
                    percent_cb(50.0)
                except Exception:
                    pass

        if cancel_event is not None and cancel_event.is_set():
            if hevc_path:
                try:
                    hevc_path.unlink(missing_ok=True)
                except Exception:
                    pass
            return None

        log("Extracting Dolby Vision RPU (dovi_tool)…")
        if percent_cb:
            try:
                percent_cb(92.0)
            except Exception:
                pass
        dt = subprocess.run(
            [str(self.dovi_tool), "extract-rpu", str(extract_input), "-o", str(out_rpu)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if hevc_path:
            try:
                hevc_path.unlink(missing_ok=True)
            except Exception:
                pass
        if dt.returncode != 0 or not out_rpu.is_file() or out_rpu.stat().st_size < 64:
            log(f"dovi_tool extract-rpu failed: {(dt.stderr or dt.stdout or '')[-400:]}")
            return None
        if percent_cb:
            try:
                percent_cb(100.0)
            except Exception:
                pass
        log(f"Dolby Vision RPU ready ({out_rpu.name}, {out_rpu.stat().st_size} bytes)")
        return out_rpu

    def apply_dovi_crop_edit(
        self,
        rpu_path: Path,
        out_rpu: Path,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> Optional[Path]:
        """
        Zero a DoVi RPU's active-area offsets via dovi_tool editor.

        The RPU is extracted from the *uncropped* source, but SVT then encodes
        an autocropped (letterbox-removed) frame. Active area (L5/L8) offsets
        in the untouched RPU would describe bars that no longer exist in the
        encoded picture. dovi_tool's documented editor JSON config
        {"active_area": {"crop": true}} is exactly the fix quietvoid ships for
        this: "should be set to true when final video has no letterbox bars"
        (docs/editor.md, assets/editor_examples/crop.json). No "mode" key is
        set — that field retargets HEVC-specific profiles (8.1 Blu-ray compat,
        MEL, 8.4) and doesn't apply to our AV1/profile-10 RPU, same reasoning
        as extract_dovi_rpu passing the RPU through unconverted.
        """
        if not self.dovi_tool.is_file():
            return None

        def log(msg: str):
            if progress_cb:
                progress_cb(msg)

        rpu_path = Path(rpu_path)
        out_rpu = Path(out_rpu)
        out_rpu.parent.mkdir(parents=True, exist_ok=True)
        config_path = out_rpu.with_suffix(".editcfg.json")
        try:
            config_path.write_text(
                json.dumps({"active_area": {"crop": True}}), encoding="utf-8"
            )
        except Exception as e:
            log(f"Could not write dovi_tool editor config: {e}")
            return None

        dt = subprocess.run(
            [str(self.dovi_tool), "editor", "-i", str(rpu_path), "-j", str(config_path), "-o", str(out_rpu)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            config_path.unlink(missing_ok=True)
        except Exception:
            pass
        if dt.returncode != 0 or not out_rpu.is_file() or out_rpu.stat().st_size < 64:
            log(f"dovi_tool editor (active-area crop) failed: {(dt.stderr or dt.stdout or '')[-400:]}")
            return None
        log(f"Dolby Vision RPU active area corrected for autocrop ({out_rpu.name})")
        return out_rpu

    @staticmethod
    def _ffmpeg_color_args(transfer_code: str, color_range: str = "tv") -> List[str]:
        """
        HDR tags for final mux.

        av1_metadata BSF takes integer AV1/H.273 codes (not names like bt2020):
          primaries 9 = BT.2020, transfer 16 = PQ / 18 = HLG, matrix 9 = BT.2020 NCL
          color_range 0 = limited/TV, 1 = full/PC (prefer ints over tv/pc).
        Container -color_range still uses ffmpeg names tv/pc.
        """
        trc_name = "arib-std-b67" if str(transfer_code) == "18" else "smpte2084"
        trc_n = "18" if str(transfer_code) == "18" else "16"
        rng = "pc" if str(color_range).lower() in ("pc", "full", "jpeg", "1") else "tv"
        bsf_rng = "1" if rng == "pc" else "0"
        bsf = (
            "av1_metadata="
            "color_primaries=9:"
            f"transfer_characteristics={trc_n}:"
            "matrix_coefficients=9:"
            f"color_range={bsf_rng}"
        )
        return [
            "-color_primaries", "bt2020",
            "-color_trc", trc_name,
            "-colorspace", "bt2020nc",
            "-color_range", rng,
            "-bsf:v", bsf,
        ]

    @staticmethod
    def _color_range_flags(video_stream: Optional[Dict[str, Any]]) -> Tuple[str, str]:
        """
        Return (svt --color-range value, ffmpeg color_range name).
        SVT: 0 = limited/TV, 1 = full/PC.
        """
        cr = ""
        if isinstance(video_stream, dict):
            cr = str(video_stream.get("color_range") or "").lower()
        if cr in ("pc", "full", "jpeg", "1"):
            return "1", "pc"
        return "0", "tv"

"""
HandBrakeCLI encoder — the app's only encode path.

Video encode (single-pass SVT-AV1-Tritium), HDR10+ / Dolby Vision passthrough,
audio and the final mux all run in HandBrakeCLI built against SVT-AV1-Tritium
(Uranite/HandBrake-SVT-AV1-Tritium, installed to bin/handbrake/ by setup). The
queue keeps everything around it: probe, the DoVi skip / quarantine policy,
audio track selection, test-mode segments, SSIMU2 and subtitle sidecars.

Mapping from the job config:
  - preset svt_params → -x key=value:… (Tritium's own option names, parsed by
    the SVT library — the same names SvtAv1EncApp takes as --long-options)
  - autocrop → HandBrake's --crop-mode conservative over AUTOCROP_PREVIEWS
    sampled frames (least crop wins, so a mixed-aspect / IMAX title keeps its
    full frame); the crop it applied is read back from its log
    (parse_applied_crop) for SSIMU2 and the job stats. Off → --crop-mode none
  - resolution_target → --maxHeight (downscale only) with --loose-anamorphic,
    so the width scales with it and square pixels stay square — HandBrake's
    default (auto) anamorphic would keep the width and stretch the pixels
    instead (3840x1600 at 1080p → 3840x1080 with a 27:40 PAR, not 2592x1080)
  - DoVi P7 → 8.1 conversion, RPU crop correction and HDR10+ passthrough are
    HandBrake's (--hdr-dynamic-metadata); the static HDR10 flags come from its
    own scan of the source
  - chapters and subtitles are stripped (--no-markers, -s none); audio tracks
    are left unnamed
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
HANDBRAKE_DIR = BASE_DIR / "bin" / "handbrake"
HANDBRAKE_CLI = HANDBRAKE_DIR / "HandBrakeCLI.exe"

# "Encoding: task 1 of 1, 12.34 % (45.67 fps, avg 44.00 fps, ETA 00h12m34s)"
# — the fps part only appears once HandBrake has a rate estimate.
PROGRESS_RE = re.compile(
    r"Encoding: task (\d+) of (\d+), ([\d.]+) %"
    r"(?: \(([\d.]+) fps, avg ([\d.]+) fps, ETA (\d+)h(\d+)m(\d+)s\))?"
)

_MIXDOWN = {6: "5point1", 2: "stereo", 1: "mono"}

# Frames HandBrake samples across the title for crop detection (its default
# is 10). Tested with a synthetic title whose full-frame section was 3% of the
# runtime: 10 samples cropped it away, 30 kept the full frame.
AUTOCROP_PREVIEWS = 30

# "+ source: 1920 * 1080, crop (140/140/0/0): 1920 * 800, scale: …" (top/bottom/left/right)
_APPLIED_CROP_RE = re.compile(r"\+ source: \d+ \* \d+, crop \((\d+)/(\d+)/(\d+)/(\d+)\)")
# Logged when the crop/scale filter would be a no-op (nothing cropped or scaled)
_NO_CROP_SCALE = "work: skipping crop/scale filter"


def handbrake_available() -> bool:
    return HANDBRAKE_CLI.is_file()


def _kbps(bitrate: Any, fallback: int) -> int:
    """'320k' / '320' / 320 → 320."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*k?\s*$", str(bitrate or ""), re.I)
    return int(float(m.group(1))) if m else fallback


def _encopts(svt_params: Dict[str, Any]) -> str:
    parts = []
    for key, val in (svt_params or {}).items():
        if val is None or val == "":
            continue
        parts.append(f"{str(key).lstrip('-')}={val}")
    return ":".join(parts)


def hb_audio_track_numbers(
    all_audio: List[Dict[str, Any]], selected: List[Dict[str, Any]]
) -> List[int]:
    """
    HandBrake numbers audio tracks 1..N in container order — the same order
    ffprobe lists them, so a track's number is its position among the
    source's audio streams (not its absolute ffprobe stream_index).
    """
    order = [t["stream_index"] for t in sorted(all_audio, key=lambda t: t["stream_index"])]
    out = []
    for t in selected:
        try:
            out.append(order.index(t["stream_index"]) + 1)
        except ValueError:
            raise RuntimeError(f"Audio stream {t['stream_index']} not found in source") from None
    return out


def parse_applied_crop(log_text: str) -> Optional[Dict[str, int]]:
    """
    The crop HandBrake actually applied, from its activity log. A cropped job
    logs it on the "+ source:" line of the Crop and Scale filter; an uncropped
    one either has no crop on that line (scale only) or skips the filter
    entirely. The scan's own "autocrop = …" line is NOT used: it's HandBrake's
    majority guess, which conservative mode can override. None when none of
    these lines is found (log format changed), so callers never guess.
    """
    m = _APPLIED_CROP_RE.search(log_text or "")
    if m:
        top, bottom, left, right = (int(v) for v in m.groups())
        return {"left": left, "top": top, "right": right, "bottom": bottom}
    if _NO_CROP_SCALE in (log_text or "") or "+ source:" in (log_text or ""):
        return {"left": 0, "top": 0, "right": 0, "bottom": 0}
    return None


def build_handbrake_command(
    input_file: Path,
    output_file: Path,
    *,
    container: str,
    crf: float,
    preset: int,
    svt_params: Dict[str, Any],
    audio_tracks: List[Dict[str, Any]],
    audio_track_numbers: List[int],
    autocrop: bool,
    target_height: int,
    dynamic_metadata: str,
) -> List[str]:
    """
    audio_tracks come from pipeline.select_and_prioritize_audio (target_codec,
    target_channels, target_bitrate, layout_desc); audio_track_numbers are the
    matching HandBrake track numbers. dynamic_metadata is a
    --hdr-dynamic-metadata value ("all", "hdr10plus", "dolbyvision") or "none".
    """
    webm = str(container).lower() == "webm"
    cmd: List[str] = [
        str(HANDBRAKE_CLI),
        "-i", str(input_file),
        "-o", str(output_file),
        "-f", "av_webm" if webm else "av_mp4",
        "--no-markers",
        "-s", "none",
        "--disable-hw-decoding",
        "-e", "svt_av1_10bit",
        "--encoder-preset", str(int(preset)),
        "-q", f"{float(crf):g}",
    ]
    if not webm:
        cmd.append("-O")  # faststart
    opts = _encopts(svt_params)
    if opts:
        cmd.extend(["-x", opts])

    if autocrop:
        cmd.extend(["--crop-mode", "conservative", "--previews", f"{AUTOCROP_PREVIEWS}:0"])
    else:
        cmd.extend(["--crop-mode", "none"])
    cmd.append("--loose-anamorphic")
    if target_height and target_height > 0:
        cmd.extend(["--maxHeight", str(int(target_height))])

    if dynamic_metadata and dynamic_metadata != "none":
        cmd.extend(["--hdr-dynamic-metadata", dynamic_metadata])
    else:
        cmd.append("--no-hdr-dynamic-metadata")

    if audio_tracks:
        encoders, bitrates, mixdowns = [], [], []
        for t in audio_tracks:
            eac3 = str(t.get("target_codec")) == "eac3"
            ch = int(t.get("target_channels") or 2)
            encoders.append("eac3" if eac3 else "opus")
            bitrates.append(str(_kbps(t.get("target_bitrate"), 640 if eac3 else 160)))
            mixdowns.append(_MIXDOWN.get(ch, "stereo"))
        cmd.extend([
            "-a", ",".join(str(n) for n in audio_track_numbers),
            "-E", ",".join(encoders),
            "-B", ",".join(bitrates),
            "-6", ",".join(mixdowns),
            # Unnamed tracks, like the SVT path's mux: don't carry the source
            # title (often stale, e.g. "DTS-HD MA 7.1") or invent one
            "--no-keep-aname",
            "--automatic-naming-behaviour", "off",
        ])
    else:
        cmd.extend(["-a", "none"])
    return cmd

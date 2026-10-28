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
  - resolution_target → --maxWidth / --maxHeight: the picture is downscaled
    (never up) to fit inside the target's box, keeping its shape, the way
    Blu-ray, streaming and HandBrake's own presets define "1080p" — a 2.40:1
    3840x1600 source at 1080p is 1920x800, at 1440p 2560x1066. With
    --non-anamorphic so pixels are always square (SAR 1:1): when a crop doesn't
    scale to exact even sizes, --loose-anamorphic would keep the shape with a
    ~0.1 % pixel-aspect correction instead, and a non-1:1 SAR reads as
    "anamorphic" to some players / Jellyfin client profiles. HandBrake's
    default (auto) anamorphic would keep the width and stretch the pixels
  - DoVi P7 → 8.1 conversion, RPU crop correction and HDR10+ passthrough are
    HandBrake's (--hdr-dynamic-metadata); the static HDR10 flags come from its
    own scan of the source
  - chapters and subtitles are stripped (--no-markers, -s none), as is the
    source's container metadata such as its title (--no-metadata); audio
    tracks are left unnamed
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

# SVT options Tritium 4.2.0 no longer has. HandBrake doesn't fail on them — it
# logs "error parsing option …" and encodes without — so a preset that still
# carries one silently loses the setting. Dropped here so the command (and the
# HandBrake log) stays clean; the queue notes it in the job log.
REMOVED_SVT_PARAMS = frozenset({"enable-daala-rd", "enable-daala-filtering"})


def split_removed_svt_params(svt_params: Dict[str, Any]) -> tuple:
    """(params the encoder understands, sorted names of removed ones that were present)."""
    kept = {k: v for k, v in (svt_params or {}).items() if str(k).lstrip("-") not in REMOVED_SVT_PARAMS}
    dropped = sorted(str(k).lstrip("-") for k in (svt_params or {}) if str(k).lstrip("-") in REMOVED_SVT_PARAMS)
    return kept, dropped

# Frames HandBrake samples across the title for crop detection (its default
# is 10). Tested with a synthetic title whose full-frame section was 3% of the
# runtime: 10 samples cropped it away, 30 kept the full frame.
AUTOCROP_PREVIEWS = 30

# "+ source: 1920 * 1080, crop (140/140/0/0): 1920 * 800, scale: …" (top/bottom/left/right)
_APPLIED_CROP_RE = re.compile(r"\+ source: \d+ \* \d+, crop \((\d+)/(\d+)/(\d+)/(\d+)\)")
# Logged when the crop/scale filter would be a no-op (nothing cropped or scaled)
_NO_CROP_SCALE = "work: skipping crop/scale filter"


# resolution_target → the box the picture must fit inside; (0, 0) = keep source
RESOLUTION_BOXES: Dict[str, Tuple[int, int]] = {
    "source": (0, 0),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
}


def target_box_for(resolution_target: Any) -> Tuple[int, int]:
    """(max width, max height) for a resolution_target label; (0, 0) = no limit."""
    return RESOLUTION_BOXES.get(str(resolution_target or "source").strip().lower(), (0, 0))


def fit_in_box(width: int, height: int, box: Tuple[int, int]) -> Tuple[int, int]:
    """
    Size a width x height picture comes out at inside box: downscale only,
    aspect kept, even dimensions (as HandBrake rounds them). An approximation
    for labels / naming — HandBrake's own result is the reference.
    """
    w, h = int(width or 0), int(height or 0)
    bw, bh = box
    if not (w and h and bw and bh):
        return w, h
    scale = min(bw / w, bh / h, 1.0)
    if scale >= 1.0:
        return w, h
    return int(round(w * scale / 2)) * 2, int(round(h * scale / 2)) * 2


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


# Lines worth surfacing from a *successful* encode's activity log. HandBrake
# finishes even when the SVT library rejects an option ("error parsing option
# …"), so a typo'd or outdated preset option is otherwise lost silently.
_HB_NOTABLE_RE = re.compile(
    r"error parsing option|Svt\[(?:warn|error|fatal)\]|\bwarning\b|\berror:",
    re.I,
)
_HB_DECODER_RE = re.compile(r"(\S+)-decoder done: (\d+) frames, (\d+) decoder errors")
_HB_FRAMES_RE = re.compile(r"vfr: (\d+) frames output, (\d+) dropped")
_HB_TIMESTAMP_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s*")


def summarize_handbrake_log(log_text: str, max_notes: int = 15) -> Dict[str, Any]:
    """
    {notes, frames, dropped} from a HandBrake activity log. notes are the
    warning / error lines (timestamps stripped, de-duplicated, capped), plus
    one per decoder that reported errors; frames / dropped come from the
    "vfr:" summary (None when absent).
    """
    notes: List[str] = []
    seen = set()
    frames = dropped = None
    for raw in (log_text or "").splitlines():
        line = _HB_TIMESTAMP_RE.sub("", raw).strip()
        if not line:
            continue
        m = _HB_DECODER_RE.search(line)
        if m:
            if int(m.group(3)):
                note = f"{m.group(1)} decoder: {m.group(3)} errors in {m.group(2)} frames"
                if note not in seen:
                    seen.add(note)
                    notes.append(note)
            continue
        m = _HB_FRAMES_RE.search(line)
        if m:
            frames, dropped = int(m.group(1)), int(m.group(2))
            continue
        if _HB_NOTABLE_RE.search(line) and line not in seen:
            seen.add(line)
            notes.append(line)
    if len(notes) > max_notes:
        extra = len(notes) - max_notes
        notes = notes[:max_notes] + [f"… and {extra} more (see the HandBrake log)"]
    return {"notes": notes, "frames": frames, "dropped": dropped}


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
    target_box: Tuple[int, int],
    dynamic_metadata: str,
) -> List[str]:
    """
    audio_tracks come from pipeline.select_and_prioritize_audio (audio_format,
    target_channels, target_bitrate); audio_track_numbers are the
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
        # Drop the source's container metadata — its title is usually the scene
        # release name, which players can show instead of the library name.
        # HDR / DoVi signalling and track languages are unaffected.
        "--no-metadata",
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
    cmd.append("--non-anamorphic")  # square pixels, see the module docstring
    box_w, box_h = target_box or (0, 0)
    if box_w > 0 and box_h > 0:
        # Fit inside the box (downscale only); HandBrake applies both limits
        cmd.extend(["--maxWidth", str(int(box_w)), "--maxHeight", str(int(box_h))])

    if dynamic_metadata and dynamic_metadata != "none":
        cmd.extend(["--hdr-dynamic-metadata", dynamic_metadata])
    else:
        cmd.append("--no-hdr-dynamic-metadata")

    if audio_tracks:
        encoders, bitrates, mixdowns = [], [], []
        for t in audio_tracks:
            eac3 = str(t.get("audio_format")) == "eac3"
            ch = int(t.get("target_channels") or 2)
            encoders.append("eac3" if eac3 else "opus")
            bitrates.append(str(_kbps(t.get("target_bitrate"), 640 if eac3 else 160)))
            mixdowns.append(_MIXDOWN.get(ch, "stereo"))
        cmd.extend([
            "-a", ",".join(str(n) for n in audio_track_numbers),
            "-E", ",".join(encoders),
            "-B", ",".join(bitrates),
            "-6", ",".join(mixdowns),
            # Unnamed tracks: don't carry the source title (often stale after
            # the re-encode, e.g. "DTS-HD MA 7.1") or invent one — players
            # label them from language, codec and layout
            "--no-keep-aname",
            "--automatic-naming-behaviour", "off",
        ])
    else:
        cmd.extend(["-a", "none"])
    return cmd

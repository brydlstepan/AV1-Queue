"""
Line downloaded subtitles up with the audio they'll play against.

A subtitle made for another release can start late (extra logos on one of them)
or run at another speed (made for another frame rate). Speech activity in the
audio is compared with the cue timing to measure both, then the timestamps are
rewritten. A correction is only trusted when it holds across the whole film and
its speed has a frame-rate explanation; anything else (another cut of the film,
a false match) is reported as rejected and the file is left alone.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import numpy as np
except ImportError:  # sync is optional: subtitles still download without it
    np = None

_HZ = 10  # activity samples per second, also the measuring resolution
_AUDIO_RATE = 8000
_DECODE_TIMEOUT = 3600

_NTSC_FILM = 24000 / 1001
# Speed changes between the common film / PAL frame rates, as cue time multipliers
_FPS_RATIOS: Tuple[float, ...] = (
    1.0,
    25 / 24, 24 / 25,
    25 / _NTSC_FILM, _NTSC_FILM / 25,
    24 / _NTSC_FILM, _NTSC_FILM / 24,
)
# A hand-tweaked conversion lands near a frame-rate ratio, not on it
_FITTED_RATIO_TOLERANCE = 0.001
_MAX_RESIDUAL_SECONDS = 0.5
_MIN_WINDOWS_IN_SYNC = 0.8  # share of measured windows; music scenes can miss
_MIN_WINDOWS = 6
_MIN_WINDOW_CUES = 5
_MIN_CUES = 30
_MIN_DURATION_SECONDS = 300
_ALREADY_SYNCED_SECONDS = 0.2

_CUE_TIME_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)(\s*-->\s*)(\d+):(\d+):(\d+)[,.](\d+)"
)


def _seconds(h: str, m: str, s: str, frac: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(3, "0")[:3]) / 1000


def _timestamp(value: float) -> str:
    ms = int(round(max(0.0, value) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _parse_cues(text: str) -> List[Tuple[float, float]]:
    cues = []
    for match in _CUE_TIME_RE.finditer(text):
        g = match.groups()
        cues.append((_seconds(*g[:4]), _seconds(*g[5:])))
    return cues


def _pick_audio_stream(media: Path, ffprobe: Path) -> Optional[Tuple[int, int]]:
    """(stream index, channels) of the widest audio track: 5.1 has a dialogue channel."""
    res = subprocess.run(
        [str(ffprobe), "-v", "quiet", "-print_format", "json", "-show_streams",
         "-select_streams", "a", str(media)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    streams = (json.loads(res.stdout or "{}").get("streams") or []) if res.returncode == 0 else []
    if not streams:
        return None
    best = max(streams, key=lambda s: int(s.get("channels") or 0))
    return int(best["index"]), int(best.get("channels") or 0)


def speech_activity(media: Path, ffmpeg: Path, ffprobe: Path) -> Optional[Any]:
    """Speech-band energy above its local baseline, _HZ values per second. None if unreadable."""
    if np is None:
        return None
    picked = _pick_audio_stream(media, ffprobe)
    if not picked:
        return None
    index, channels = picked
    if channels >= 3:
        mix = "c0=FC"
    elif channels == 2:
        mix = "c0=0.5*c0+0.5*c1"
    else:
        mix = "c0=c0"
    cmd = [
        str(ffmpeg), "-v", "error", "-nostdin", "-i", str(media), "-map", f"0:{index}",
        # first_pts: a track that starts late stays at its real position
        "-af", f"aresample=async=1:first_pts=0,pan=mono|{mix},highpass=f=300,lowpass=f=3000",
        "-ar", str(_AUDIO_RATE), "-f", "s16le", "-",
    ]
    samples = _AUDIO_RATE // _HZ
    block_bytes = samples * 2
    energy: List[Any] = []
    pending = b""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            chunk = proc.stdout.read(block_bytes * 1024)
            if not chunk:
                break
            pending += chunk
            usable = len(pending) // block_bytes
            if usable:
                pcm = np.frombuffer(pending[:usable * block_bytes], np.int16).astype(np.float32)
                energy.append(np.log1p((pcm.reshape(usable, samples) ** 2).mean(axis=1)))
                pending = pending[usable * block_bytes:]
        proc.wait(timeout=_DECODE_TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
    if not energy:
        return None
    level = np.concatenate(energy)
    kernel = 30 * _HZ
    if len(level) <= kernel:
        return None
    activity = np.clip(level - np.convolve(level, np.ones(kernel) / kernel, "same"), 0, None)
    return activity - activity.mean()


def _cue_signal(cues: List[Tuple[float, float]], length: int, ratio: float = 1.0, shift: float = 0.0) -> Any:
    signal = np.zeros(length, np.float32)
    for begin, end in cues:
        i, j = int((begin * ratio + shift) * _HZ), int((end * ratio + shift) * _HZ)
        if j > 0 and i < length:
            signal[max(i, 0):min(j, length)] = 1
    return signal - signal.mean()


def _best_lag(signal: Any, reference: Any, lag_range: Optional[Tuple[int, int]] = None) -> Tuple[int, float]:
    """(lag, normalized score): the signal matches the reference when moved later by lag."""
    size = 2 * max(len(signal), len(reference))
    corr = np.fft.irfft(np.fft.rfft(reference, size) * np.conj(np.fft.rfft(signal, size)), size)
    lags = np.rint(np.fft.fftfreq(size, 1 / size)).astype(int)
    if lag_range is not None:
        corr = np.where((lags >= lag_range[0]) & (lags <= lag_range[1]), corr, -np.inf)
    best = int(np.argmax(corr))
    norm = float(np.linalg.norm(signal) * np.linalg.norm(reference))
    return int(lags[best]), float(corr[best]) / norm if norm else 0.0


def _window_offsets(
    cues: List[Tuple[float, float]], activity: Any, ratio: float, shift: float
) -> List[Tuple[float, float]]:
    """(window centre, further shift the cues want there) across the film."""
    duration = len(activity) / _HZ
    window = max(120.0, min(600.0, duration / 8))
    pad = window / 5
    placed = [(b * ratio + shift, e * ratio + shift) for b, e in cues]
    span = int(window * _HZ)
    margin = int(pad * _HZ)
    out: List[Tuple[float, float]] = []
    start = 0.0
    while start + window <= duration:
        first = int(start * _HZ)
        # The signal starts pad early, so cues just outside the window can still
        # be matched into it: a lag of -margin means no further shift, ±pad around it
        here = [(b - start + pad, e - start + pad) for b, e in placed if start - pad <= b < start + window + pad]
        if sum(1 for b, _ in here if pad <= b < pad + window) >= _MIN_WINDOW_CUES:
            signal = _cue_signal(here, span + 2 * margin)
            lag, _ = _best_lag(signal, activity[first:first + span], (-2 * margin, 0))
            out.append((start + window / 2, lag / _HZ + pad))
        start += window
    return out


def _in_sync_share(offsets: List[Tuple[float, float]]) -> Tuple[float, float]:
    """(share of windows within the limit, worst residual among those windows)."""
    residuals = sorted(abs(o) for _, o in offsets)
    inside = [r for r in residuals if r <= _MAX_RESIDUAL_SECONDS]
    return len(inside) / len(residuals), inside[-1] if inside else residuals[0]


def _nearest_fps_ratio(ratio: float) -> Tuple[float, float]:
    nearest = min(_FPS_RATIOS, key=lambda r: abs(ratio / r - 1))
    return nearest, abs(ratio / nearest - 1)


def measure_sync(cues: List[Tuple[float, float]], activity: Any) -> Dict[str, Any]:
    """
    {status: "ok", ratio, shift, residual, fitted} | {status: "rejected" | "unavailable", reason}.
    Exact frame-rate ratios win when they hold; a fitted ratio has to stay close
    to one of them and hold just the same.
    """
    duration = len(activity) / _HZ
    if duration < _MIN_DURATION_SECONDS or len(cues) < _MIN_CUES:
        return {"status": "unavailable", "reason": "too short to measure"}

    candidates = []
    for ratio in _FPS_RATIOS:
        lag, score = _best_lag(_cue_signal(cues, len(activity), ratio), activity)
        candidates.append((score, ratio, lag / _HZ))
    candidates.sort(reverse=True)

    measured_any = False
    for _, ratio, shift in candidates[:3]:
        offsets = _window_offsets(cues, activity, ratio, shift)
        if len(offsets) < _MIN_WINDOWS:
            continue
        measured_any = True
        shift += float(np.median([o for _, o in offsets]))
        offsets = _window_offsets(cues, activity, ratio, shift)
        share, worst = _in_sync_share(offsets)
        if share >= _MIN_WINDOWS_IN_SYNC:
            return {"status": "ok", "ratio": ratio, "shift": shift, "residual": worst, "fitted": False}

        # Steady drift left over: fit it, ignoring windows that are nowhere near
        near = [(t, o) for t, o in offsets if abs(o) <= 5.0]
        if len(near) < max(_MIN_WINDOWS, _MIN_WINDOWS_IN_SYNC * len(offsets)):
            continue
        slope, intercept = np.polyfit([t for t, _ in near], [o for _, o in near], 1)
        fitted_ratio = ratio * (1 + float(slope))
        if _nearest_fps_ratio(fitted_ratio)[1] > _FITTED_RATIO_TOLERANCE:
            continue
        fitted_shift = shift + float(intercept)
        offsets = _window_offsets(cues, activity, fitted_ratio, fitted_shift)
        share, worst = _in_sync_share(offsets)
        if share >= _MIN_WINDOWS_IN_SYNC:
            return {"status": "ok", "ratio": fitted_ratio, "shift": fitted_shift, "residual": worst, "fitted": True}

    if not measured_any:
        return {"status": "unavailable", "reason": "too little dialogue to measure"}
    return {"status": "rejected", "reason": "no offset and frame-rate speed fits the whole film"}


def sync_subtitle_file(path: Path, activity: Any) -> Dict[str, Any]:
    """Measure one SRT against the audio; rewrite its timestamps when that's trusted."""
    if np is None or activity is None:
        return {"status": "unavailable", "reason": "audio could not be analysed"}
    path = Path(path)
    # newline="" so CRLF survives the rewrite
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
        text = f.read()
    result = measure_sync(_parse_cues(text), activity)
    if result["status"] != "ok":
        return result
    ratio, shift = result["ratio"], result["shift"]
    if ratio == 1.0 and abs(shift) <= _ALREADY_SYNCED_SECONDS:
        result["changed"] = False
        return result

    def retime(match: "re.Match[str]") -> str:
        g = match.groups()
        begin = _seconds(*g[:4]) * ratio + shift
        end = _seconds(*g[5:]) * ratio + shift
        return f"{_timestamp(begin)}{g[4]}{_timestamp(end)}"

    path.write_text(_CUE_TIME_RE.sub(retime, text), encoding="utf-8-sig", newline="")
    result["changed"] = True
    return result


def describe_sync(result: Dict[str, Any]) -> str:
    """Job-log wording for a sync_subtitle_file result."""
    if result.get("status") != "ok":
        return str(result.get("reason") or "not measured")
    if not result.get("changed"):
        return f"already in sync (residual {result['residual']:.1f} s)"
    speed = "" if result["ratio"] == 1.0 else f", ×{result['ratio']:.5f}"
    return f"synced {result['shift']:+.1f} s{speed}, residual {result['residual']:.1f} s"

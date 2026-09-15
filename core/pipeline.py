"""
Transcode & Packaging Pipeline
Handles:
- Audio stream discovery, prioritization (preferred languages), and Opus 5.1/Stereo transcoding
- Dolby Vision / HDR10 metadata detection (SVT color flags)
- Direct SVT-AV1-Tritium encode (core/svt_encode.py) with real-time progress parsing
- Final Web-Optimized MP4 / WebM muxing (+faststart, stripped chapters/subtitles, ISO language tags)
- HDR10 static color tags + HDR10+ passthrough; Dolby Vision is never emitted (see README.md)
- Temporary cache cleanup
"""

import os
import sys
import json
import time
import shutil
import re
import subprocess
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable, Tuple

from core.hdr_dovi import HDRDoviProcessor
from core.subtitle_extract import extract_text_subtitles
from core.win_process import boost_process

# The 20 ISO 639-2 codes whose terminological (/T) and bibliographic (/B) forms
# differ. Only used if langcodes is unavailable — see iso639_2 / _to_bibliographic.
_ISO639_2T_TO_B = {
    "bod": "tib", "ces": "cze", "cym": "wel", "deu": "ger", "ell": "gre",
    "eus": "baq", "fas": "per", "fra": "fre", "hye": "arm", "isl": "ice",
    "kat": "geo", "mkd": "mac", "mri": "mao", "msa": "may", "mya": "bur",
    "nld": "dut", "ron": "rum", "slk": "slo", "sqi": "alb", "zho": "chi",
}

BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"
CORE_DIR = BASE_DIR / "core"
TEMP_DIR = BASE_DIR / "_temp"
VS_DIR = BASE_DIR / "vs"
VENV_PYTHON = VS_DIR / "python-env" / "Scripts" / "python.exe"
if not VENV_PYTHON.exists():
    VENV_PYTHON = Path(sys.executable)


def frame_rate_to_float(value: Any) -> Optional[float]:
    """Parse an ffprobe r_frame_rate/avg_frame_rate string (e.g. "24000/1001") to float."""
    if value is None:
        return None
    try:
        from fractions import Fraction
        f = float(Fraction(str(value).strip()))
        return f if f > 0 else None
    except Exception:
        try:
            f = float(value)
            return f if f > 0 else None
        except Exception:
            return None


def parse_hms(value: Any) -> float:
    """Parse H:M:S / M:S / seconds into float seconds."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"Invalid timestamp '{value}'") from e
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    raise ValueError(f"Invalid timestamp '{value}'")


def format_hms(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# Resolution-target label → downscale height (0 = keep source).
RES_HEIGHT_MAP = {"source": 0, "1080p": 1080, "1440p": 1440, "2160p": 2160}


def target_height_for(resolution_target: Any) -> int:
    """Map a resolution_target label to a downscale height (0 = keep source)."""
    return RES_HEIGHT_MAP.get(str(resolution_target).lower(), 0)


def normalize_audio_format(value: Any) -> str:
    """Canonical audio format: 'opus' or 'eac3', defaulting to 'opus'."""
    fmt = (str(value) if value else "opus").strip().lower()
    return fmt if fmt in ("opus", "eac3") else "opus"


def crop_to_csv(crop: Optional[Dict[str, int]]) -> str:
    """Format a crop dict as the "left,top,right,bottom" string the VS scripts parse."""
    crop = crop or {}
    return (
        f"{int(crop.get('left', 0))},"
        f"{int(crop.get('top', 0))},"
        f"{int(crop.get('right', 0))},"
        f"{int(crop.get('bottom', 0))}"
    )


class TranscodePipeline:
    def __init__(self, bin_dir: Path = BIN_DIR, temp_dir: Path = TEMP_DIR):
        self.bin_dir = bin_dir
        self.temp_dir = temp_dir
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.hdr_processor = HDRDoviProcessor(bin_dir)
        self.current_process: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self._tracked_procs: List[subprocess.Popen] = []

    def _track_process(self, proc: subprocess.Popen) -> None:
        with self._proc_lock:
            self._tracked_procs.append(proc)

    def _untrack_process(self, proc: subprocess.Popen) -> None:
        with self._proc_lock:
            self._tracked_procs = [p for p in self._tracked_procs if p is not proc]

    @staticmethod
    def _kill_pid_tree(pid: int) -> None:
        if not pid:
            return
        # Prefer Windows taskkill so nested ffmpeg/SVT children die reliably
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                return
            except Exception:
                pass
        try:
            import psutil
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except Exception:
                    pass
            try:
                parent.kill()
            except Exception:
                pass
        except Exception:
            pass

    @classmethod
    def _kill_popen_tree(cls, proc: subprocess.Popen) -> None:
        if proc is None:
            return
        pid = getattr(proc, "pid", None)
        if proc.poll() is None and pid:
            cls._kill_pid_tree(pid)
        try:
            proc.wait(timeout=3)
        except Exception:
            pass

    def kill_all_processes(self) -> None:
        """Force-kill the active encode tree and any tracked helpers (ffmpeg, SVT, etc.)."""
        with self._proc_lock:
            procs = list(self._tracked_procs)
            if self.current_process is not None:
                procs.append(self.current_process)
            self._tracked_procs = []
            self.current_process = None

        # Kill deepest children first by reversing tracked order, then unique by pid
        seen = set()
        for proc in reversed(procs):
            pid = getattr(proc, "pid", None)
            if pid in seen:
                continue
            seen.add(pid)
            self._kill_popen_tree(proc)

    def _signal_process_trees(self, action: str) -> bool:
        """Suspend or resume the tracked encode helpers + active process tree.

        Suspend freezes children before the parent (so the parent can't spawn a
        new child that escapes the freeze); resume unfreezes the parent first.
        """
        label = action.capitalize()
        try:
            import psutil
        except Exception as e:
            print(f"[!] {label} error: {e}")
            return False

        with self._proc_lock:
            procs = list(self._tracked_procs)
            if self.current_process is not None:
                procs.append(self.current_process)

        done = False
        seen = set()
        for proc in procs:
            if proc is None or proc.poll() is not None:
                continue
            pid = getattr(proc, "pid", None)
            if not pid or pid in seen:
                continue
            seen.add(pid)
            try:
                p = psutil.Process(pid)
                children = p.children(recursive=True)
                if action == "suspend":
                    for child in children:
                        try:
                            child.suspend()
                        except Exception:
                            pass
                    p.suspend()
                else:
                    p.resume()
                    for child in children:
                        try:
                            child.resume()
                        except Exception:
                            pass
                done = True
            except Exception as e:
                print(f"[!] {label} error pid={pid}: {e}")
        return done

    def suspend_current_process(self) -> bool:
        """Suspends (freezes) tracked encode helpers and the active encode process tree."""
        return self._signal_process_trees("suspend")

    def resume_current_process(self) -> bool:
        """Resumes (unfreezes) tracked encode helpers and the active encode process tree."""
        return self._signal_process_trees("resume")

    def _run_capture(
        self,
        cmd: List[str],
        *,
        cancel_event: Optional[threading.Event] = None,
        on_abort: Optional[Callable[[], None]] = None,
        cancel_msg: str = "Operation cancelled by user.",
        fail_prefix: str = "Subprocess failed",
        **popen_kwargs: Any,
    ) -> Tuple[str, str, int]:
        """
        Run cmd to completion, draining stdout+stderr on a worker thread so a full
        pipe buffer can't deadlock a poll loop, and kill it if cancel_event fires.
        Returns (stdout, stderr, returncode). Raises RuntimeError(cancel_msg) on
        cancel and RuntimeError(fail_prefix: …) if communicate() itself failed;
        on_abort (e.g. delete a partial file) runs before either raise.
        """
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_kwargs,
        )
        self._track_process(proc)
        captured: Dict[str, Any] = {}

        def _drain():
            try:
                captured["out"], captured["err"] = proc.communicate()
            except Exception as e:
                captured["exc"] = e

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()
        try:
            while reader.is_alive():
                if cancel_event and cancel_event.is_set():
                    self._kill_popen_tree(proc)
                    reader.join(timeout=5)
                    if on_abort:
                        try:
                            on_abort()
                        except Exception:
                            pass
                    raise RuntimeError(cancel_msg)
                reader.join(timeout=0.2)
        finally:
            self._untrack_process(proc)

        if captured.get("exc"):
            if on_abort:
                try:
                    on_abort()
                except Exception:
                    pass
            raise RuntimeError(f"{fail_prefix}: {captured['exc']}")
        return captured.get("out"), captured.get("err"), proc.returncode

    def _run_to_log(
        self,
        cmd: List[str],
        err_log: Path,
        cancel_event: Optional[threading.Event] = None,
        poll: float = 0.2,
    ) -> Optional[int]:
        """
        Run cmd with stdout discarded and stderr → err_log, polling to completion.
        Returns the exit code, or None if cancel_event fired (process killed).
        Raises only if the process cannot be spawned. Callers decide how to treat
        a non-zero code or a None (cancel) result.
        """
        err_f = None
        try:
            err_f = open(err_log, "w", encoding="utf-8", errors="replace")
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err_f)
        except Exception:
            if err_f is not None:
                try:
                    err_f.close()
                except Exception:
                    pass
            raise
        self._track_process(proc)
        try:
            while True:
                if cancel_event and cancel_event.is_set():
                    self._kill_popen_tree(proc)
                    return None
                if proc.poll() is not None:
                    return proc.returncode
                time.sleep(poll)
        finally:
            self._untrack_process(proc)
            try:
                err_f.close()
            except Exception:
                pass

    def clean_audio_track_title(self, title: str, source_file: Optional[Path] = None) -> str:
        """
        Keep only a short track label. Source releases often stuff the full
        filename (or release name) into the audio stream title tag.
        """
        raw = (title or "").strip()
        if not raw:
            return ""

        cleaned = raw

        # Strip absolute/relative paths if present
        cleaned = cleaned.replace("\\", "/")
        if "/" in cleaned:
            cleaned = cleaned.split("/")[-1]

        if source_file is not None:
            stem = source_file.stem
            name = source_file.name
            # Remove filename / stem when title is basically the file name
            for token in (name, stem):
                if not token:
                    continue
                if cleaned.lower() == token.lower():
                    return ""
                # "Filename - Surround 5.1" / "Filename: English"
                patterns = [
                    rf"^{re.escape(token)}\s*[-–_:|]\s*",
                    rf"^{re.escape(token)}\s+",
                    rf"\s*[-–_:|]\s*{re.escape(token)}$",
                ]
                for pat in patterns:
                    cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)

        # Drop common container extensions left behind
        cleaned = re.sub(
            r"\.(mkv|mp4|m2ts|ts|m4v|mov|avi|webm)$",
            "",
            cleaned,
            flags=re.IGNORECASE
        ).strip(" -–_:|.\t")

        # If what's left still looks like a long release name (lots of dots), treat as useless
        if cleaned.count(".") >= 3 and len(cleaned) > 40:
            return ""

        return cleaned

    def extract_test_segment(
        self,
        input_file: Path,
        start_hms: str,
        end_hms: str,
        output_file: Path,
        audio_stream_indices: Optional[List[int]] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        fps: Optional[float] = None,
    ) -> Path:
        """
        Extract a time range from the source for Test Mode encodes.
        Uses stream copy for speed (keyframe-aligned).
        If audio_stream_indices is set, only video + those audio streams are kept
        (preserves inspector track selection).

        Video is bounded by an exact -frames:v count (derived from the source's
        real frame rate), not -t/-to. With B-frame-heavy HEVC (the norm for movie
        remuxes/Dolby Vision), a duration-based -t cutoff on a stream copy can
        overshoot by a second or more and — worse — leaves the container with a
        bogus, non-standard average frame rate (e.g. 500000/21149 instead of the
        source's real 24000/1001), which is what actually causes choppy playback
        downstream, not literal dropped/duplicated frames. -frames:v counts
        packets instead of walltime, so it isn't thrown off by B-frame reorder
        delay and the output keeps the source's real, standard frame rate.
        """
        start_sec = parse_hms(start_hms)
        end_sec = parse_hms(end_hms)
        if end_sec <= start_sec:
            raise ValueError(f"Test segment end ({end_hms}) must be after start ({start_hms}).")

        duration = end_sec - start_sec
        output_file.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = str(self.bin_dir / "ffmpeg.exe")

        if not fps:
            try:
                media = self.probe_media(input_file)
                fps = frame_rate_to_float((media.get("video") or {}).get("r_frame_rate"))
            except Exception:
                fps = None
        fps = fps or 24.0
        frame_count = max(1, round(duration * fps))

        if progress_cb:
            progress_cb(
                f"Extracting test segment {format_hms(start_sec)} → {format_hms(end_sec)} "
                f"({duration:.1f}s)..."
            )

        # -ss before -i for fast seek. Video is cut to an exact frame count
        # (frame-accurate, standard-frame-rate-preserving); -t is kept only as a
        # generous safety bound so audio (unaffected by -frames:v) doesn't run
        # past the segment if something upstream is misconfigured.
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-nostdin",
            "-ss", f"{start_sec:.3f}",
            "-i", str(input_file),
            "-frames:v", str(frame_count),
            "-t", f"{duration + 2.0:.3f}",
            "-map", "0:v:0",
        ]
        if audio_stream_indices is not None:
            # Empty list = video-only segment; never silently map every audio stream
            for s_idx in audio_stream_indices:
                cmd.extend(["-map", f"0:{int(s_idx)}"])
        else:
            raise ValueError("extract_test_segment requires audio_stream_indices")

        cmd.extend([
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(output_file),
        ])

        err_log = output_file.with_suffix(output_file.suffix + ".ffmpeg.log")
        returncode = self._run_to_log(cmd, err_log, cancel_event)
        if returncode is None:
            raise RuntimeError("Test segment extract cancelled by user.")

        if returncode != 0 or not output_file.exists() or output_file.stat().st_size < 1024:
            tail = ""
            try:
                tail = err_log.read_text(encoding="utf-8", errors="replace")[-800:]
            except Exception:
                pass
            raise RuntimeError(
                f"Failed to extract test segment {start_hms}→{end_hms}."
                + (f"\n{tail}" if tail else "")
            )

        if progress_cb:
            progress_cb(f"Test segment ready ({output_file.name}).")
        return output_file

    def detect_black_bar_crop(
        self,
        input_file: Path,
        duration_sec: Optional[float] = None,
        src_w: Optional[int] = None,
        src_h: Optional[int] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, int]:
        """
        HandBrake-style autocrop using ffmpeg cropdetect.
        Returns {left, top, right, bottom} in pixels (even values). All zeros = no crop.
        Pass duration_sec / src_w / src_h to skip an extra ffprobe when already known.
        """
        ffmpeg = str(self.bin_dir / "ffmpeg.exe")
        # A caller-supplied duration of 0/None is treated the same: some MKV
        # remuxes report format.duration as missing, which previously slipped
        # through as "duration_sec=0.0" (not None) and skipped this re-probe,
        # leaving autocrop to sample only the first few seconds of the file.
        if not src_w or not src_h or not duration_sec:
            media = self.probe_media(input_file)
            vid = media.get("video") or {}
            if not src_w:
                src_w = int(vid.get("width") or 0)
            if not src_h:
                src_h = int(vid.get("height") or 0)
            if not duration_sec:
                duration_sec = float(media.get("duration") or 0)
        src_w = int(src_w or 0)
        src_h = int(src_h or 0)
        dur = float(duration_sec or 0)

        if src_w < 16 or src_h < 16:
            return {"left": 0, "top": 0, "right": 0, "bottom": 0}

        # Sample several points (skip very start/end where credits/fades fool detection)
        sample_starts: List[float] = []
        if dur <= 45:
            sample_starts = [0.0]
        elif dur <= 180:
            sample_starts = [dur * 0.2, dur * 0.5, dur * 0.75]
        else:
            sample_starts = [dur * p for p in (0.12, 0.28, 0.45, 0.62, 0.78)]

        if progress_cb:
            progress_cb(f"Detecting black bars ({len(sample_starts)} sample(s))...")

        # ffmpeg cropdetect emits the result as "crop=W:H:X:Y" on the summary line.
        crop_eq = re.compile(r"crop=(?P<w>\d+):(?P<h>\d+):(?P<x>\d+):(?P<y>\d+)")

        # Least-crop-wins: collect every valid detection, then
        # take the smallest bar per edge (selection + rationale below).
        edge_lefts: List[int] = []
        edge_tops: List[int] = []
        edge_rights: List[int] = []
        edge_bottoms: List[int] = []
        for ss in sample_starts:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Autocrop cancelled by user.")

            cmd = [
                ffmpeg, "-hide_banner", "-nostdin",
                "-ss", f"{max(0.0, ss):.3f}",
                "-i", str(input_file),
                "-t", "6",
                "-an",
                # cropdetect limit is a 0-1 fraction, not raw 0-255: this ffmpeg
                # scales it to the source bit depth, so a literal 24 is far too
                # strict on 10-bit HDR (black ≈ 64/1023) and misses letterboxing.
                "-vf", "fps=2,cropdetect=limit=24/255:round=2:reset=0",
                "-f", "null", "-",
            ]
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
                blob = (res.stderr or "") + "\n" + (res.stdout or "")
            except Exception as e:
                print(f"[!] Autocrop sample failed at {ss:.1f}s: {e}")
                continue

            for line in blob.splitlines():
                m = crop_eq.search(line)
                if not m:
                    continue
                w, h, x, y = (int(m.group("w")), int(m.group("h")), int(m.group("x")), int(m.group("y")))
                if w < 16 or h < 16 or x < 0 or y < 0:
                    continue
                if x + w > src_w + 2 or y + h > src_h + 2:
                    continue
                edge_lefts.append(x)
                edge_tops.append(y)
                edge_rights.append(max(0, src_w - (x + w)))
                edge_bottoms.append(max(0, src_h - (y + h)))

        if not (edge_lefts and edge_tops and edge_rights and edge_bottoms):
            if progress_cb:
                progress_cb("Autocrop: no black bars detected.")
            return {"left": 0, "top": 0, "right": 0, "bottom": 0}

        # Smallest bar per edge wins (least crop): one bright frame that exposes a
        # true picture edge vetoes every dark frame that saw a larger bar. A small
        # variance tolerance snaps sub-VAR_TOL cropdetect jitter down first, so a
        # 1px wobble across samples can't invent a distinct (smaller) edge.
        VAR_TOL = 2

        def least_edge(values: List[int]) -> int:
            snapped = [max(0, v - (v % VAR_TOL)) for v in values]
            return min(snapped)

        left = least_edge(edge_lefts)
        top = least_edge(edge_tops)
        right = least_edge(edge_rights)
        bottom = least_edge(edge_bottoms)

        # Round odd values DOWN — crop less, never into picture — and keep even
        # dimensions for YUV420 / AV1.
        def even_down(v: int) -> int:
            v = max(0, int(v))
            return v - (v % 2)

        left, top, right, bottom = map(even_down, (left, top, right, bottom))

        # Ignore tiny noise crops (< 4px total per axis)
        if left + right < 4:
            left = right = 0
        if top + bottom < 4:
            top = bottom = 0

        # Safety: don't crop away most of the frame
        if (src_w - left - right) < src_w * 0.5 or (src_h - top - bottom) < src_h * 0.5:
            if progress_cb:
                progress_cb("Autocrop: rejected unsafe crop values.")
            return {"left": 0, "top": 0, "right": 0, "bottom": 0}

        crop = {"left": left, "top": top, "right": right, "bottom": bottom}
        if progress_cb:
            if any(crop.values()):
                progress_cb(
                    f"Autocrop: L{left} T{top} R{right} B{bottom} "
                    f"→ {src_w - left - right}x{src_h - top - bottom}"
                )
            else:
                progress_cb("Autocrop: no significant black bars.")
        return crop

    def probe_media(self, file_path: Path, probe: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Returns structured metadata about video, audio, and subtitle streams.
        Pass probe= to reuse an existing ffprobe JSON dict.
        """
        data = probe if isinstance(probe, dict) else self.hdr_processor.probe_video_streams(file_path)
        streams = data.get("streams", [])
        
        video_info = None
        audio_tracks = []
        subtitle_tracks = []

        for idx, s in enumerate(streams):
            codec_type = s.get("codec_type")
            tags = s.get("tags", {})
            lang = tags.get("language", "und").lower()
            title = self.clean_audio_track_title(tags.get("title", ""), file_path)
            
            if codec_type == "video" and not video_info:
                video_info = {
                    "index": s.get("index", idx),
                    "codec": s.get("codec_name", ""),
                    "width": s.get("width", 0),
                    "height": s.get("height", 0),
                    "pix_fmt": s.get("pix_fmt", ""),
                    "r_frame_rate": s.get("r_frame_rate", "24/1"),
                    "color_primaries": s.get("color_primaries", ""),
                    "color_transfer": s.get("color_transfer", ""),
                    "is_hdr": "bt2020" in s.get("color_primaries", "").lower() or "smpte2084" in s.get("color_transfer", "").lower()
                }
            elif codec_type == "audio":
                channels = s.get("channels", 2)
                channel_layout = s.get("channel_layout", "stereo")
                audio_tracks.append({
                    "stream_index": s.get("index", idx),
                    "codec": s.get("codec_name", ""),
                    "channels": channels,
                    "channel_layout": channel_layout,
                    "bitrate": int(s.get("bit_rate", 0)) if s.get("bit_rate") else 0,
                    "language": lang,
                    "title": title
                })
            elif codec_type == "subtitle":
                subtitle_tracks.append({
                    "stream_index": s.get("index", idx),
                    "codec": s.get("codec_name", ""),
                    "language": lang,
                    "title": title
                })

        return {
            "format": data.get("format", {}),
            "video": video_info,
            "audio_tracks": audio_tracks,
            "subtitle_tracks": subtitle_tracks,
            "duration": self._probe_duration(data.get("format", {}), streams)
        }

    @staticmethod
    def _probe_duration(fmt: Dict[str, Any], streams: List[Dict[str, Any]]) -> float:
        """
        Container duration, with fallback for files where ffprobe's format.duration
        is missing or zero (some MKV remuxes lack a Segment Info duration even
        though every stream is a normal, full-length track). Without this, autocrop
        and other duration-dependent logic silently treat the file as near-instant
        and only sample the first few seconds.
        """
        dur = float(fmt.get("duration") or 0)
        if dur > 0:
            return dur
        for s in streams:
            if s.get("codec_type") != "video":
                continue
            try:
                d = float(s.get("duration") or 0)
                if d > 0:
                    return d
            except (TypeError, ValueError):
                pass
            tags = s.get("tags") or {}
            tag_dur = tags.get("DURATION") or tags.get("duration")
            if tag_dur:
                try:
                    d = parse_hms(tag_dur)
                    if d > 0:
                        return d
                except ValueError:
                    pass
        return 0.0

    def lang_family(self, lang: Optional[str]) -> str:
        """Normalize language tags to a stable ISO 639-2/T family key (via langcodes)."""
        raw = (lang or "und").strip()
        if not raw or raw.lower() in ("und", "unknown"):
            return "und"
        try:
            from langcodes import Language

            return Language.get(raw).to_alpha3() or "und"
        except Exception:
            l = raw.lower().replace("_", "-")
            primary = l.split("-", 1)[0]
            if len(primary) == 3:
                return primary
            return "und"

    def iso639_2(self, lang: Optional[str], container: str = "mp4") -> str:
        """
        Container language tag — never leave a 2-letter code as "und".

        MP4/ISOBMFF wants ISO 639-2/T (ces, deu, fra); the Matroska spec's
        Language element wants the bibliographic form (cze, ger, fre), so WebM
        output gets /B. Everything else keeps /T.
        """
        fam = self.lang_family(lang)
        if fam and fam != "und":
            return self._to_bibliographic(fam) if str(container).lower() in ("webm", "mkv", "matroska") else fam
        raw = (lang or "und").strip().lower().split("-", 1)[0]
        if len(raw) == 3:
            return raw
        return "und"

    @staticmethod
    def _to_bibliographic(alpha3_t: str) -> str:
        """ISO 639-2/T → 639-2/B (ces→cze, deu→ger, …). Identity when they agree."""
        try:
            from langcodes import Language

            return Language.get(alpha3_t).to_alpha3(variant="B") or alpha3_t
        except Exception:
            return _ISO639_2T_TO_B.get(alpha3_t, alpha3_t)

    def select_and_prioritize_audio(
        self,
        audio_tracks: List[Dict[str, Any]],
        user_selected_indices: Optional[List[int]] = None,
        bitrate_51: str = "320k",
        bitrate_stereo: str = "160k",
        languages: Optional[List[str]] = None,
        audio_format: str = "opus",
    ) -> List[Dict[str, Any]]:
        """
        Orders audio tracks by preferred languages, then others.
        Assigns encode targets: ≥5ch → 5.1 (incl. 7.1 downmix), 2ch → stereo, else mono.
        audio_format: "opus" (libopus) or "eac3".
        """
        priority = [self.lang_family(x) for x in (languages or ["eng", "ces"])]
        fmt = normalize_audio_format(audio_format)
        ffmpeg_codec = "eac3" if fmt == "eac3" else "libopus"

        if user_selected_indices is not None:
            # Preserve the caller's selection order (inspector checkboxes / remap list)
            by_idx = {t["stream_index"]: t for t in audio_tracks}
            ordered = [by_idx[i] for i in user_selected_indices if i in by_idx]
        else:
            buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k in priority}
            other_tracks: List[Dict[str, Any]] = []
            for t in audio_tracks:
                fam = self.lang_family(t.get("language"))
                if fam in buckets:
                    buckets[fam].append(t)
                else:
                    other_tracks.append(t)
            ordered = []
            for fam in priority:
                ordered.extend(buckets.get(fam, []))
            ordered.extend(other_tracks)

        # Assign output transcode params (7.1/atmos-ish → 5.1 via ffmpeg -ac 6)
        for t in ordered:
            ch = int(t.get("channels") or 0)
            t["audio_format"] = fmt
            t["target_codec"] = ffmpeg_codec
            if ch >= 5:
                t["target_channels"] = 6
                t["target_bitrate"] = bitrate_51 or ("640k" if fmt == "eac3" else "320k")
                t["layout_desc"] = "5.1 Surround"
            elif ch == 2:
                t["target_channels"] = 2
                t["target_bitrate"] = bitrate_stereo or ("192k" if fmt == "eac3" else "160k")
                t["layout_desc"] = "Stereo"
            else:
                t["target_channels"] = 1
                t["target_bitrate"] = "96k" if fmt == "opus" else (bitrate_stereo or "192k")
                t["layout_desc"] = "Mono"

        return ordered

    def pick_best_track_for_language(self, tracks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Prefer the best source for a 5.1 encode: surround first, then channel count, then bitrate."""
        if not tracks:
            return None

        def score(t: Dict[str, Any]):
            ch = int(t.get("channels") or 0)
            br = int(t.get("bitrate") or 0)
            surround = 1 if ch >= 5 else 0
            return (surround, ch, br)

        return max(tracks, key=score)

    def select_default_audio_indices(
        self,
        audio_tracks: List[Dict[str, Any]],
        languages: Optional[List[str]] = None,
        best_only: bool = True,
    ) -> List[int]:
        """
        Auto-pick tracks for preferred languages (default: English, then Czech).
        If best_only, keep one best track per language when multiples exist.
        Otherwise include every matching track for those languages.
        """
        priority = [self.lang_family(x) for x in (languages or ["eng", "ces"])]
        # Preserve first occurrence order while dropping duplicate families
        seen = set()
        langs: List[str] = []
        for fam in priority:
            if fam not in seen:
                seen.add(fam)
                langs.append(fam)

        selected: List[int] = []
        for fam in langs:
            group = [t for t in audio_tracks if self.lang_family(t.get("language")) == fam]
            if not group:
                continue
            if best_only:
                best = self.pick_best_track_for_language(group)
                if best:
                    selected.append(best["stream_index"])
            else:
                for t in group:
                    selected.append(t["stream_index"])

        # If no preferred-language tracks exist, fall back to a single best overall track
        if not selected and audio_tracks:
            best = self.pick_best_track_for_language(audio_tracks)
            if best:
                selected.append(best["stream_index"])

        return selected

    def transcode_audio_tracks(
        self,
        input_file: Path,
        ordered_tracks: List[Dict[str, Any]],
        job_temp_dir: Path,
        progress_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None
    ) -> List[Dict[str, Any]]:
        """Extracts and transcodes selected audio tracks (Opus or E-AC-3, sequential)."""
        if not ordered_tracks:
            return []

        ffmpeg = str(self.bin_dir / "ffmpeg.exe")
        total = len(ordered_tracks)
        results: List[Optional[Dict[str, Any]]] = [None] * total

        def encode_one(idx: int, track: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if cancel_event and cancel_event.is_set():
                return None

            s_idx = track["stream_index"]
            lang = track["language"]
            fmt = normalize_audio_format(track.get("audio_format"))
            codec = track.get("target_codec") or ("eac3" if fmt == "eac3" else "libopus")
            ext = "eac3" if fmt == "eac3" else "opus"
            codec_label = "E-AC-3" if fmt == "eac3" else "Opus"
            out_file = job_temp_dir / f"audio_track_{idx}_{lang}.{ext}"

            if progress_cb:
                progress_cb(
                    f"Transcoding audio track {idx+1}/{total} "
                    f"({lang.upper()} {track['layout_desc']}) to {codec_label}..."
                )

            cmd = [
                ffmpeg, "-y", "-hide_banner", "-nostdin",
                "-i", str(input_file),
                "-map", f"0:{s_idx}",
                "-c:a", codec,
                "-b:a", track["target_bitrate"],
                "-ac", str(track["target_channels"]),
            ]
            if fmt == "opus":
                cmd.extend(["-vbr", "on"])
            cmd.append(str(out_file))

            err_log = job_temp_dir / f"audio_track_{idx}_{lang}.ffmpeg.log"
            returncode = self._run_to_log(cmd, err_log, cancel_event)
            if returncode is None:
                return None  # cancelled — outer loop treats this as a stop, not a failure
            if returncode != 0:
                tail = ""
                try:
                    tail = err_log.read_text(encoding="utf-8", errors="replace")[-800:]
                except Exception:
                    pass
                print(f"[!] Warning: Failed to transcode audio stream {s_idx} (exit {returncode})")
                if tail:
                    print(tail)
                return None

            if not out_file.is_file() or out_file.stat().st_size < 64:
                print(f"[!] Warning: Audio transcode produced empty/tiny file for stream {s_idx}")
                return None

            return {
                "file": out_file,
                "language": self.iso639_2(lang),
                "title": self.clean_audio_track_title(track.get("title", ""), input_file) or track.get("layout_desc", ""),
                "channels": track["target_channels"],
                "bitrate": track["target_bitrate"],
                "audio_format": fmt,
            }

        # Sequential — concurrent ffmpeg reads of the same MKV are unsafe on Windows
        failures: List[str] = []
        for idx, track in enumerate(ordered_tracks):
            if cancel_event and cancel_event.is_set():
                break
            result = encode_one(idx, track)
            if result is None and not (cancel_event and cancel_event.is_set()):
                failures.append(
                    f"stream {track.get('stream_index')} ({track.get('language', '?')})"
                )
            results[idx] = result

        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Audio transcode cancelled by user.")
        if failures:
            raise RuntimeError(
                "Audio transcode failed for: " + ", ".join(failures)
            )

        return [r for r in results if r is not None]

    def run_svt_encode(
        self,
        input_file: Path,
        job_temp_dir: Path,
        crf: float = 30.0,
        preset: int = 4,
        resolution_target: str = "source",
        extra_svt_params: str = "",
        hdr10plus_json: Optional[str] = None,
        crop: Optional[Dict[str, int]] = None,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        pid_cb: Optional[Callable[[int], None]] = None,
    ) -> Path:
        """
        Executes core/svt_encode.py (direct single-pass SVT-AV1-Tritium encode)
        and monitors progress in real time. Returns path to the final IVF stream.

        pid_cb, if given, is called with the spawned process's PID as soon as it
        starts, so the caller can persist it onto the job record — that's what
        lets a crash-recovery pass on the next startup find and kill this exact
        process tree if the server dies mid-encode and orphans it.
        """
        script_path = CORE_DIR / "svt_encode.py"
        python_bin = str(VENV_PYTHON)

        target_height = target_height_for(resolution_target)

        cmd = [
            python_bin, str(script_path),
            "-i", str(input_file),
            "-t", str(job_temp_dir),
            "--preset", str(preset),
            "--crf", str(crf),
            "--target-height", str(target_height)
        ]

        if crop and any(int(crop.get(k, 0) or 0) for k in ("left", "top", "right", "bottom")):
            cmd.extend(["--crop", crop_to_csv(crop)])

        if extra_svt_params:
            cmd.extend(["--svt-params", extra_svt_params])
        # Dedicated argv (not inside --svt-params) so spaces in paths survive
        if hdr10plus_json:
            cmd.extend(["--hdr10plus-json", str(hdr10plus_json)])

        # Environment with bin/ and vs/ in path and UTF-8 encoding
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir};{VS_DIR};{VS_DIR / 'plugins64'};{env.get('PATH', '')}"
        env["PYTHONPATH"] = f"{VS_DIR};{CORE_DIR};{env.get('PYTHONPATH', '')}"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # Disable Rich live redraws (they use \\r and deadlock piped stdout)
        env["SVTENCODE_NONINTERACTIVE"] = "1"
        env["TERM"] = "dumb"

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Binary mode + chunked drain: readline() deadlocks on Rich \\r updates
            env=env,
            bufsize=0,
        )
        self.current_process = process
        self._track_process(process)
        # Opt encode worker out of Windows EcoQoS so SVT keeps all cores when
        # the Studio / IDE window is unfocused (otherwise often ~4 threads).
        boost_process(process.pid)
        if pid_cb:
            try:
                pid_cb(process.pid)
            except Exception:
                pass

        current_stage = "ENCODING"
        stage_num = 1
        output_error = []
        line_buf = ""

        def handle_text(text: str) -> None:
            nonlocal line_buf
            # Normalize carriage-return progress redraws into line feeds
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            line_buf += text
            while "\n" in line_buf:
                line_str, line_buf = line_buf.split("\n", 1)
                line_str = line_str.strip()
                if not line_str:
                    continue

                # Machine progress: __AV1Q_PROGRESS__ <label...> <pct> <fps> <done> <total>
                if line_str.startswith("__AV1Q_PROGRESS__"):
                    parts = line_str.split()
                    # parts[0]=prefix, parts[1:-3]=label words, then pct fps done total
                    if len(parts) >= 6 and progress_cb:
                        try:
                            pct = float(parts[-4])
                            fps_v = float(parts[-3])
                        except ValueError:
                            continue
                        progress_cb({
                            "stage": current_stage,
                            "stage_num": stage_num,
                            "percent": pct,
                            "fps": fps_v,
                            "raw_log": "",  # UI-only — do not spam job log
                        })
                    continue

                pct_match = re.search(r"(\d{1,3}(?:\.\d+)?)%", line_str)
                fps_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:fps|FPS)", line_str, re.I)
                percent = float(pct_match.group(1)) if pct_match else None
                fps = float(fps_match.group(1)) if fps_match else None

                if progress_cb:
                    progress_cb({
                        "stage": current_stage,
                        "stage_num": stage_num,
                        "percent": percent,
                        "fps": fps,
                        "raw_log": line_str
                    })

        def drain_stdout() -> None:
            try:
                assert process.stdout is not None
                while True:
                    chunk = process.stdout.read(4096)
                    if not chunk:
                        break
                    handle_text(chunk.decode("utf-8", errors="replace"))
            except Exception as e:
                output_error.append(e)

        reader = threading.Thread(target=drain_stdout, daemon=True)
        reader.start()

        try:
            while True:
                if cancel_event and cancel_event.is_set():
                    self.kill_all_processes()
                    raise RuntimeError("Encode cancelled by user.")

                if process.poll() is not None and not reader.is_alive():
                    break
                time.sleep(0.2)

            reader.join(timeout=5)
            if line_buf.strip():
                handle_text("\n")

            if output_error:
                raise RuntimeError(f"Failed reading encode output: {output_error[0]}")

            ret_code = process.poll()
            if ret_code != 0:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("Encode cancelled by user.")
                raise RuntimeError(f"Encode failed with exit code {ret_code}.")
        finally:
            self._untrack_process(process)
            self.current_process = None

        final_ivf = job_temp_dir / f"{input_file.stem}.ivf"
        stray_ivf = input_file.parent / f"{input_file.stem}.ivf"
        if not final_ivf.exists() and stray_ivf.exists():
            # Legacy stray IVF left beside the source — adopt then prefer temp
            try:
                shutil.move(str(stray_ivf), str(final_ivf))
            except Exception:
                final_ivf = stray_ivf
        elif final_ivf.exists() and stray_ivf.exists() and stray_ivf.resolve() != final_ivf.resolve():
            try:
                stray_ivf.unlink(missing_ok=True)
            except Exception:
                pass

        if not final_ivf.exists():
            raise FileNotFoundError(f"Encoded IVF file not found for {input_file.name}")

        return final_ivf

    def measure_final_ssimu2(
        self,
        source_file: Path,
        encoded_file: Path,
        ssimu2_mode: str = "auto",
        crop: Optional[Dict[str, int]] = None,
        resolution_target: str = "source",
        skip: int = 1,
        progress_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """
        Score source vs final encode with SSIMULACRA2 (avg / p15 / min).
        Runs in the VS venv as a subprocess.
        """
        target_height = target_height_for(resolution_target)
        crop_str = crop_to_csv(crop)

        if progress_cb:
            progress_cb("Measuring average SSIMU2 on test encode...")

        script = CORE_DIR / "measure_ssimu2.py"
        cmd = [
            str(VENV_PYTHON), str(script),
            "--source", str(source_file),
            "--encoded", str(encoded_file),
            "--mode", str(ssimu2_mode or "auto"),
            "--crop", crop_str,
            "--target-height", str(target_height),
            "--skip", str(max(1, int(skip))),
        ]

        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir};{VS_DIR};{VS_DIR / 'plugins64'};{env.get('PATH', '')}"
        env["PYTHONPATH"] = f"{VS_DIR};{CORE_DIR};{env.get('PYTHONPATH', '')}"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        # communicate() runs on a worker (inside _run_capture) so the pipes drain
        # continuously — polling while nothing reads stdout/stderr deadlocks as
        # soon as VapourSynth fills the ~64 KB pipe buffer with per-frame warnings.
        stdout, stderr, returncode = self._run_capture(
            cmd,
            cancel_event=cancel_event,
            cancel_msg="SSIMU2 measurement cancelled by user.",
            fail_prefix="SSIMU2 measurement failed",
            env=env,
        )

        raw = (stdout or "").strip().splitlines()
        payload = None
        for line in reversed(raw):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    payload = json.loads(line)
                    break
                except Exception:
                    continue

        if not payload or not payload.get("ok"):
            err = (payload or {}).get("error") if payload else None
            if not err:
                err = (stderr or stdout or f"exit {returncode}")[-500:]
            raise RuntimeError(f"SSIMU2 measurement failed: {err}")

        if progress_cb:
            progress_cb(
                f"SSIMU2 avg {payload['avg']:.2f} "
                f"(p15 {payload['p15']:.2f}, min {payload['min']:.2f}, "
                f"{payload['frames']} frames, {payload['mode']})"
            )
        return payload

    def mux_output(
        self,
        ivf_video: Path,
        audio_files: List[Dict[str, Any]],
        output_file: Path,
        container: str = "mp4",
        progress_cb: Optional[Callable[[str], None]] = None,
        color_args: Optional[List[str]] = None,
        cancel_event=None,
    ) -> Path:
        """
        Mux AV1 video + Opus audio into MP4 (+faststart) or WebM.

        Writes to ``*.partial.mp4`` / ``*.partial.webm`` first, then atomically
        replaces the final path so a crash mid-mux never truncates an existing
        good encode. (Must keep a real container extension — ffmpeg rejects
        ``*.mp4.partial``.)

        MP4 / WebM: ffmpeg stream-copy (+ optional HDR10 color BSF). Dolby Vision
        is never emitted (see README.md) — HDR10 static tags + HDR10+ passthrough only.
        """
        fmt = "webm" if str(container).lower() == "webm" else "mp4"
        ffmpeg = str(self.bin_dir / "ffmpeg.exe")
        ffprobe = str(self.bin_dir / "ffprobe.exe")
        output_file = Path(output_file)
        if output_file.suffix.lower() != f".{fmt}":
            output_file = output_file.with_suffix(f".{fmt}")
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Never mux straight onto the deliverable path
        # e.g. movie.mp4 -> movie.partial.mp4 (NOT movie.mp4.partial)
        partial = output_file.with_name(f"{output_file.stem}.partial{output_file.suffix}")
        try:
            if partial.exists():
                partial.unlink()
        except Exception:
            pass

        if progress_cb:
            if fmt == "webm":
                progress_cb("Muxing final WebM container...")
            else:
                progress_cb("Muxing final Web-Optimized MP4 container...")

        cmd = [
            ffmpeg, "-y",
            "-i", str(ivf_video)
        ]

        for a in audio_files:
            cmd.extend(["-i", str(a["file"])])

        cmd.extend([
            "-map", "0:v:0",
            "-c:v", "copy"
        ])
        if color_args:
            cmd.extend(list(color_args))

        for idx, a in enumerate(audio_files):
            lang = self.iso639_2(a.get("language"), container=fmt)
            cmd.extend([
                "-map", f"{idx+1}:a:0",
                "-c:a", "copy",
                f"-metadata:s:a:{idx}", f"language={lang}",
            ])
            if idx == 0:
                cmd.extend([f"-disposition:a:{idx}", "default"])
            else:
                cmd.extend([f"-disposition:a:{idx}", "0"])
            if a.get("title"):
                cmd.extend([f"-metadata:s:a:{idx}", f"title={a['title']}"])

        cmd.extend([
            "-sn",
            "-map_chapters", "-1",
        ])
        if fmt == "mp4":
            cmd.extend(["-movflags", "+faststart", "-f", "mp4"])
        else:
            cmd.extend(["-f", "webm"])
        cmd.append(str(partial))

        # Tracked Popen (not subprocess.run) so Stop can kill an in-flight mux;
        # a blocking run() here leaves the worker stuck for minutes on a large file.
        # A cancel or a spawn/pipe error deletes the partial before raising.
        def _drop_partial():
            try:
                partial.unlink(missing_ok=True)
            except Exception:
                pass

        _out, mux_err, returncode = self._run_capture(
            cmd,
            cancel_event=cancel_event,
            on_abort=_drop_partial,
            cancel_msg="Muxing cancelled by user.",
            fail_prefix="FFmpeg muxing failed",
        )

        if returncode != 0:
            _drop_partial()
            label = "WebM" if fmt == "webm" else "MP4"
            raise RuntimeError(f"FFmpeg {label} muxing failed: {mux_err}")

        if not partial.is_file() or partial.stat().st_size < 64:
            try:
                partial.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError("FFmpeg mux produced an empty or missing partial file")

        # Confirm HDR10 color signaling landed (best-effort — never blocks publish)
        probe_target = partial
        if color_args:
            try:
                probe = subprocess.run(
                    [
                        ffprobe, "-v", "error",
                        "-select_streams", "v:0",
                        "-show_entries", "stream=color_primaries,color_transfer,color_space,color_range:stream_side_data",
                        "-of", "json",
                        str(probe_target),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if probe.returncode == 0 and probe.stdout:
                    streams = (json.loads(probe.stdout) or {}).get("streams") or []
                    if streams:
                        s0 = streams[0]
                        dovi_side = None
                        for sd in s0.get("side_data_list") or []:
                            if sd.get("dv_profile") is not None or "DOVI" in str(sd.get("side_data_type") or "").upper():
                                dovi_side = sd
                                break
                        prim = str(s0.get("color_primaries") or "")
                        trc = str(s0.get("color_transfer") or "")
                        if progress_cb:
                            msg = (
                                "Mux color probe: "
                                f"primaries={prim or '?'} · "
                                f"transfer={trc or '?'} · "
                                f"space={s0.get('color_space') or '?'} · "
                                f"range={s0.get('color_range') or '?'}"
                            )
                            if dovi_side is not None:
                                msg += (
                                    f" · DoVi P{dovi_side.get('dv_profile', '?')}."
                                    f"{dovi_side.get('dv_level', '?')}"
                                )
                            progress_cb(msg)
                        if color_args and not (prim and trc):
                            if progress_cb:
                                progress_cb(
                                    "WARNING: Mux color tags missing after remux — check player HDR"
                                )
            except Exception as e:
                if progress_cb:
                    progress_cb(f"Mux color probe skipped: {e}")

        # Atomic publish: existing final is replaced only after a complete partial
        try:
            os.replace(str(partial), str(output_file))
        except Exception as e:
            try:
                partial.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(f"Could not publish muxed file to {output_file}: {e}") from e

        if progress_cb:
            progress_cb(f"Published final: {output_file.name}")
        return output_file

    def extract_subtitles_from_source(
        self,
        source_file: Path,
        progress_cb: Optional[Callable[[str], None]] = None,
        languages: Optional[List[str]] = None,
        kinds: Optional[List[str]] = None,
        output_dir: Optional[Path] = None,
        basename: Optional[str] = None,
        strip_credits: bool = False,
    ) -> Dict[str, Any]:
        """Extract text subs from the original source (background-safe).

        When basename/output_dir are set, sidecars use the target output name
        next to the encode (not the source stem).
        """
        return extract_text_subtitles(
            source_file,
            ffprobe=self.bin_dir / "ffprobe.exe",
            ffmpeg=self.bin_dir / "ffmpeg.exe",
            output_dir=output_dir,
            basename=basename,
            languages=languages,
            kinds=kinds,
            strip_credits=strip_credits,
            progress_cb=progress_cb,
        )

    def cleanup_temp_files(self, job_temp_dir: Path):
        """Removes intermediate temp folder and heavy video/audio fragments."""
        try:
            if job_temp_dir.exists():
                shutil.rmtree(job_temp_dir, ignore_errors=True)
        except Exception as e:
            print(f"[!] Cleanup error: {e}")

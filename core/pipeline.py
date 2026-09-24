"""
Transcode & Packaging Pipeline
Handles:
- Media probe, audio stream discovery and prioritization (preferred languages,
  Opus / E-AC-3 targets that HandBrake then encodes)
- Test-mode segment extraction
- HandBrakeCLI encode runner (command built by core/handbrake_encode.py) with
  real-time progress parsing, process tracking and pause / cancel
- Post-encode SSIMU2 scoring, subtitle sidecar extraction, temp cleanup
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
        # Prefer Windows taskkill so nested ffmpeg / HandBrake children die reliably
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
        """Force-kill the active encode tree and any tracked helpers (ffmpeg, HandBrakeCLI, etc.)."""
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
        priority = [self.lang_family(x) for x in (languages or ["eng"])]
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
        Auto-pick tracks for preferred languages (default: English).
        If best_only, keep one best track per language when multiples exist.
        Otherwise include every matching track for those languages.
        """
        priority = [self.lang_family(x) for x in (languages or ["eng"])]
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

    def run_handbrake_encode(
        self,
        cmd: List[str],
        partial: Path,
        log_file: Path,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        pid_cb: Optional[Callable[[int], None]] = None,
    ) -> Path:
        """
        Run a HandBrakeCLI command (core/handbrake_encode.build_handbrake_command)
        that writes ``partial``. Progress comes from stdout's carriage-return
        "Encoding: task …, N %" lines; HandBrake's activity log (stderr) goes to
        log_file, and its tail is raised on failure. The caller verifies and
        publishes the partial — on cancel or failure it is deleted here.
        """
        from core.handbrake_encode import PROGRESS_RE

        partial = Path(partial)
        partial.parent.mkdir(parents=True, exist_ok=True)
        try:
            partial.unlink(missing_ok=True)
        except Exception:
            pass

        def _drop_partial():
            # Windows can't delete a file HandBrake still has open — wait for
            # the killed process to actually exit first
            try:
                process.wait(timeout=15)
            except Exception:
                pass
            try:
                partial.unlink(missing_ok=True)
            except Exception:
                pass

        log_fh = open(log_file, "w", encoding="utf-8", errors="replace")
        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as e:
            log_fh.close()
            raise RuntimeError(f"Could not start HandBrakeCLI: {e}") from e
        self.current_process = process
        self._track_process(process)
        boost_process(process.pid)
        if pid_cb:
            try:
                pid_cb(process.pid)
            except Exception:
                pass

        err_tail: List[str] = []
        muxing = [False]

        def drain_stderr() -> None:
            assert process.stderr is not None
            for raw in iter(process.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                log_fh.write(line + "\n")
                if line:
                    err_tail.append(line)
                    if len(err_tail) > 40:
                        err_tail.pop(0)

        def drain_stdout() -> None:
            assert process.stdout is not None
            buf = ""
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line or not progress_cb:
                        continue
                    m = PROGRESS_RE.search(line)
                    if m:
                        progress_cb({
                            "percent": float(m.group(3)),
                            "fps": float(m.group(4)) if m.group(4) else None,
                            "raw_log": "",  # UI-only — do not spam job log
                        })
                    elif line.startswith("Muxing") and not muxing[0]:
                        muxing[0] = True
                        progress_cb({"percent": 100.0, "fps": None, "raw_log": "HandBrake: muxing output…"})

        readers = [
            threading.Thread(target=drain_stderr, daemon=True),
            threading.Thread(target=drain_stdout, daemon=True),
        ]
        for t in readers:
            t.start()

        try:
            while process.poll() is None:
                if cancel_event and cancel_event.is_set():
                    self.kill_all_processes()
                    _drop_partial()
                    raise RuntimeError("Encode cancelled by user.")
                time.sleep(0.2)
            for t in readers:
                t.join(timeout=5)
            if cancel_event and cancel_event.is_set():
                _drop_partial()
                raise RuntimeError("Encode cancelled by user.")
            if process.returncode != 0 or not partial.is_file() or partial.stat().st_size < 64:
                _drop_partial()
                tail = "\n".join(err_tail[-12:]).strip()
                detail = f"\nLast HandBrake output:\n{tail}" if tail else ""
                raise RuntimeError(f"HandBrakeCLI failed with exit code {process.returncode}.{detail}")
        finally:
            self._untrack_process(process)
            self.current_process = None
            log_fh.close()
        return partial

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

"""
Queue Manager for AV1 Queue Transcode Jobs
Handles background worker execution, state persistence, live progress events, and WebSocket broadcasting.
"""

import copy
import json
import os
import time
import traceback
import uuid
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Set, Tuple

from core.handbrake_encode import (
    build_handbrake_command,
    handbrake_available,
    hb_audio_track_numbers,
    parse_applied_crop,
    split_removed_svt_params,
    summarize_handbrake_log,
)
from core.hdr_dovi import svt_chroma_position
from core.pipeline import TranscodePipeline, frame_rate_to_float, normalize_audio_format, target_height_for
from core.app_settings import (
    load_settings, save_settings, get_pipeline_settings, JOB_CONFIG_DEFAULTS,
)
from core.media_tagging import build_media_tag, library_output_path, output_hdr_label, refresh_media_tag_quality
from core.subtitle_extract import select_default_subtitle_indices
from core.subtitle_search import search_and_download_missing

BASE_DIR = Path(__file__).resolve().parent.parent
SERVER_DIR = BASE_DIR / "server"
QUEUE_FILE = SERVER_DIR / "queue.json"
_LEGACY_QUEUE = BASE_DIR / "queue.json"
TEMP_DIR = BASE_DIR / "_temp"
HISTORY_DIR = BASE_DIR / "history"


def _ensure_queue_file() -> Path:
    """Prefer server/queue.json; migrate from repo-root if needed."""
    if QUEUE_FILE.is_file():
        return QUEUE_FILE
    if _LEGACY_QUEUE.is_file():
        try:
            SERVER_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(_LEGACY_QUEUE), str(QUEUE_FILE))
        except Exception:
            return _LEGACY_QUEUE
    return QUEUE_FILE


def allocate_unique_output_path(
    desired: str | Path,
    *,
    reserved: Optional[Set[str]] = None,
) -> Path:
    """
    Avoid silently overwriting an existing non-empty deliverable or another
    queued job's output_path. Appends _2, _3, … before the extension.
    """
    path = Path(desired)
    reserved_norm: Set[str] = set()
    for r in reserved or ():
        try:
            reserved_norm.add(str(Path(r).resolve()).lower())
        except Exception:
            reserved_norm.add(str(r).lower())

    def taken(p: Path) -> bool:
        try:
            key = str(p.resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key in reserved_norm:
            return True
        try:
            return p.is_file() and p.stat().st_size > 0
        except Exception:
            return False

    if not taken(path):
        try:
            return path.resolve()
        except Exception:
            return path

    stem, suffix, parent = path.stem, path.suffix, path.parent
    for n in range(2, 10000):
        cand = parent / f"{stem}_{n}{suffix}"
        if not taken(cand):
            try:
                return cand.resolve()
            except Exception:
                return cand
    raise RuntimeError(f"Could not allocate unique output path near {path}")


HISTORY_DIR.mkdir(parents=True, exist_ok=True)


class JobStatus:
    # Persisted in queue.json / history — names kept from the old pipeline:
    # EXTRACTING = prepare (probe, test segment), REMUXING = verify & publish.
    QUEUED = "QUEUED"
    EXTRACTING = "EXTRACTING"
    FINAL_ENCODE = "FINAL_ENCODE"
    REMUXING = "REMUXING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


# Statuses that mean the worker was mid-job when the process died
_IN_FLIGHT_STATUSES = frozenset({
    JobStatus.EXTRACTING,
    JobStatus.FINAL_ENCODE,
    JobStatus.REMUXING,
})


# Overall progress bar ranges (stage-local 0–100% maps into these).
_STAGE_PROGRESS_RANGES = {
    JobStatus.EXTRACTING: (0.0, 5.0),
    JobStatus.FINAL_ENCODE: (5.0, 92.0),
    JobStatus.REMUXING: (92.0, 99.0),
    JobStatus.COMPLETED: (100.0, 100.0),
}


def map_stage_progress(status: str, stage_percent: Optional[float] = None) -> float:
    """Map a stage-local percent (or stage start) into overall 0–100 job progress."""
    start, end = _STAGE_PROGRESS_RANGES.get(status, (0.0, 100.0))
    if stage_percent is None:
        return start
    pct = max(0.0, min(100.0, float(stage_percent)))
    return start + (end - start) * (pct / 100.0)


def _stamp_log(msg: str) -> str:
    """Prefix a job-log line with its local time: '[HH:MM:SS] text'.

    24-hour and zero-padded so the prefix is always 10 characters and the
    messages line up in the log. Stamped once here, where the line is stored, so
    the live stream, the saved job log and the history view all show the same.
    """
    return f"[{time.strftime('%H:%M:%S')}] {msg}"


def _resolved_str(path: Optional[str]) -> str:
    """Canonical form used as the reserved-output-path key."""
    if not path:
        return ""
    try:
        return str(Path(path).resolve())
    except Exception:
        return str(path)


def _keep_output_dir(old_path: str | Path, desired: str | Path) -> str:
    """Keep ``old_path``'s directory; only take the basename from ``desired``.

    Custom output paths must not be yanked back next to the source when
    autoname / HDR re-label / edit refreshes the filename.
    """
    old = Path(old_path) if old_path else Path()
    des = Path(desired)
    if not old.parts:
        return str(des)
    return str(old.parent / des.name)


def _configured_output_dir(cfg: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """Folder for finished encodes from Settings → Output folder path.

    Independent of whether Watch folder auto-queue is on:

    - Path set → that folder for every job (manual add and watch alike)
    - Path blank + watch-folder job → ``<watch_dir>/encoded``
    - Path blank + manual job → None (caller keeps next-to-source / old dir)

    Refuses a configured path inside the watched folder (falls back to
    ``encoded/``), same rule as WatchFolderService.
    """
    data = settings if settings is not None else load_settings()
    configured = str(data.get("watch_folder_output_path") or "").strip()
    watch_dir = (
        cfg.get("watch_source_dir")
        or str(data.get("watch_folder_path") or "").strip()
        or None
    )

    if configured:
        out = Path(configured)
        if watch_dir:
            try:
                watch_resolved = Path(watch_dir).resolve()
                out_resolved = out.resolve()
                if watch_resolved == out_resolved or watch_resolved in out_resolved.parents:
                    return Path(watch_dir) / "encoded"
                return out_resolved
            except OSError:
                return out
        try:
            return out.resolve()
        except OSError:
            return out

    if cfg.get("watch_source_dir"):
        return Path(cfg["watch_source_dir"]) / "encoded"
    return None


def _place_in_output_dir(
    desired: str | Path,
    *,
    cfg: Dict[str, Any],
    old_path: str = "",
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Put ``desired``'s basename in the configured output dir, else keep ``old_path``'s dir."""
    out_dir = _configured_output_dir(cfg, settings)
    if out_dir is not None:
        return str(out_dir / Path(desired).name)
    if old_path:
        return _keep_output_dir(old_path, desired)
    return str(desired)


def _fmt_duration(seconds: Any) -> str:
    """'2h 35m 46s' / '1m 18s' / '9s' / '0.4s' for job-log lines."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if s < 10:
        return f"{s:.1f}s"
    total = int(round(s))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def _fmt_bytes(n: Any) -> str:
    """Binary units, labelled like Windows Explorer (GB = GiB)."""
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


_LAYOUT_BY_CHANNELS = {6: "5.1", 2: "stereo", 1: "mono"}

# Encode-progress lines (every 5 %) only for inputs at least this long
_PROGRESS_LOG_MIN_SECONDS = 300


def _audio_track_line(t: Dict[str, Any], fmt_label: str) -> str:
    """'ENG TrueHD 7.1 (#2) → Opus 5.1 @ 320k' — source track and its encode target."""
    codec = str(t.get("codec") or "?")
    codec = {"truehd": "TrueHD", "eac3": "E-AC-3", "ac3": "AC-3", "dts": "DTS",
             "aac": "AAC", "flac": "FLAC", "opus": "Opus", "pcm_s24le": "PCM",
             "pcm_s16le": "PCM"}.get(codec, codec.upper())
    src_layout = str(t.get("channel_layout") or "").split("(")[0] or f"{t.get('channels', '?')}ch"
    title = str(t.get("title") or "").strip()
    target = _LAYOUT_BY_CHANNELS.get(int(t.get("target_channels") or 0), "?")
    return (
        f"{str(t.get('language') or 'und').upper()} {codec} {src_layout} (#{t.get('stream_index')})"
        + (f" \"{title}\"" if title else "")
        + f" → {fmt_label} {target} @ {t.get('target_bitrate') or '?'}"
    )


_TAG_SOURCE_LABELS = {
    "filename": "from the filename",
    "nfo": "from an .nfo next to the source",
    "tmdb": "via TMDB",
    "manual": "edited manually",
    "partial": "no IMDb id",
    "guess": "guessed from the filename",
}


def _naming_line(tag: Dict[str, Any]) -> str:
    """How the output name's title / IMDb id were resolved at add time."""
    if not tag:
        return "Naming: no media tag (custom output path)"
    if tag.get("media_kind") == "episode":
        se = ""
        if tag.get("season") is not None and tag.get("episode") is not None:
            se = f" S{int(tag['season']):02d}E{int(tag['episode']):02d}"
        ep = f" \"{tag['epname']}\"" if tag.get("epname") else ""
        head = f"Naming: episode \"{tag.get('show') or tag.get('title') or '?'}\"{se}{ep}"
    else:
        year = f" ({tag['year']})" if tag.get("year") else ""
        head = f"Naming: \"{tag.get('title') or '?'}\"{year}"
    bits = [head]
    if tag.get("imdb_id"):
        bits.append(str(tag["imdb_id"]))
    src = "manual" if tag.get("verified") and tag.get("source") == "manual" else tag.get("source")
    if src in _TAG_SOURCE_LABELS:
        bits.append(_TAG_SOURCE_LABELS[src])
    if tag.get("lookup_note"):
        bits.append(str(tag["lookup_note"]))
    return " · ".join(bits)


def _container_title(fmt: Optional[Dict[str, Any]]) -> str:
    """The source container's title tag (ffprobe format tags; MKV may say TITLE)."""
    tags = (fmt or {}).get("tags") or {}
    for k, v in tags.items():
        if str(k).lower() == "title":
            return str(v or "").strip()
    return ""


def default_output_path(input_path: str, config: Optional[Dict[str, Any]] = None,
                        media_tag: Optional[Dict[str, Any]] = None) -> str:
    """Build the usual output filename next to the source (test vs full encode)."""
    inp = Path(input_path)
    cfg = config or {}
    settings = load_settings()
    use_library = cfg.get("autoname_output")
    if use_library is None:
        use_library = settings.get("autoname_output", True)

    if use_library and media_tag:
        return library_output_path(inp, media_tag, config=cfg)

    ext = "webm" if str(cfg.get("container", JOB_CONFIG_DEFAULTS["container"])).lower() == "webm" else "mp4"
    if cfg.get("test_mode"):
        start_tag = str(cfg.get("trim_start", "start")).replace(":", "")
        end_tag = str(cfg.get("trim_end", "end")).replace(":", "")
        return str(inp.parent / f"{inp.stem}_test_{start_tag}-{end_tag}_av1_boost.{ext}")
    return str(inp.parent / f"{inp.stem}_av1_boost.{ext}")


class QueueManager:
    def __init__(self, pipeline: Optional[TranscodePipeline] = None):
        self.pipeline = pipeline or TranscodePipeline()
        self.jobs: List[Dict[str, Any]] = []
        self.current_job_id: Optional[str] = None
        self.is_running = False
        self.is_paused = False
        self._worker_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._listeners = []
        self._lock = threading.Lock()
        self._history_lock = threading.Lock()
        # The running job's append_log (pause / resume write to it); None between jobs
        self._job_log = None
        # Pause bookkeeping for the active clock (stage timings exclude pauses)
        self._pause_clock_lock = threading.Lock()
        self._paused_total = 0.0
        self._paused_since: Optional[float] = None
        self.load_queue()
        self._recover_interrupted_jobs()

    def add_listener(self, callback):
        self._listeners.append(callback)

    def emit_event(self, event_type: str, data: Dict[str, Any]):
        for cb in list(self._listeners):
            try:
                cb(event_type, data)
            except Exception:
                pass

    @staticmethod
    def _progress_payload(job: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
        """Snapshot a job's live progress fields into a 'job_progress' WS payload.
        Callers hold progress_lock and pass extras like log=..., stage_percent=..."""
        payload = {
            "id": job["id"],
            "status": job["status"],
            "stage": job["stage"],
            "stage_num": job.get("stage_num", 0),
            "progress": job.get("progress", 0),
            "stage_percent": job.get("stage_percent", 0),
            "fps": job.get("fps", 0),
            "elapsed": job.get("elapsed_seconds", 0),
        }
        payload.update(extra)
        return payload

    def _reserved_output_paths(self, exclude_job_id: Optional[str] = None) -> Set[str]:
        reserved: Set[str] = set()
        for j in self.jobs:
            if exclude_job_id and j.get("id") == exclude_job_id:
                continue
            op = j.get("output_path")
            if not op:
                continue
            # Only reserve paths for jobs that still matter (not already completed/cancelled away)
            st = j.get("status")
            if st in (JobStatus.COMPLETED, JobStatus.CANCELLED):
                continue
            reserved.add(_resolved_str(op))
        return reserved

    def _set_job_pid(self, job: Dict[str, Any], pid: Optional[int]) -> None:
        """
        Persist the active encoder subprocess PID on the job record.
        If the server process dies mid-encode (crash, force-kill), this is the
        only way a later _recover_interrupted_jobs() pass can find and kill the
        orphaned HandBrakeCLI process tree it left running
        — otherwise it keeps consuming CPU cores indefinitely, invisible to the
        app, and silently competes with whatever job runs next.
        """
        job["pid"] = pid
        try:
            self.save_queue()
        except Exception:
            pass

    def _recover_interrupted_jobs(self) -> int:
        """Mark mid-flight jobs FAILED so Start can requeue them after a crash.
        Also kills any leftover encoder process tree those jobs left running —
        the previous server process died before it could clean that up itself.
        """
        recovered = 0
        with self._lock:
            for j in self.jobs:
                if j.get("status") not in _IN_FLIGHT_STATUSES:
                    continue
                pid = j.get("pid")
                if pid:
                    try:
                        TranscodePipeline._kill_pid_tree(int(pid))
                    except Exception as e:
                        print(f"[queue] Could not kill orphaned process tree (pid {pid}): {e}")
                prev = j.get("status")
                j["status"] = JobStatus.FAILED
                j["stage"] = "Interrupted — ready to retry"
                j["stage_num"] = 0
                j["progress"] = 0
                j["fps"] = 0
                j["pid"] = None
                j["error"] = (
                    f"Interrupted while {prev} (server/process restarted). "
                    "Press Start to retry, or Reset to edit."
                )
                # The normal cleanup lives in _execute_single_job's finally, which
                # never ran. A retry gets a fresh job id, so without this the
                # partial encode (tens of GB) would be stranded forever.
                shutil.rmtree(TEMP_DIR / f"job_{j['id']}", ignore_errors=True)
                recovered += 1
            if recovered:
                self._save_queue_unlocked()
        if recovered:
            print(f"[queue] Recovered {recovered} interrupted job(s) → FAILED (retry on Start)")
        self._prune_orphan_temp_dirs()
        return recovered

    def _prune_orphan_temp_dirs(self) -> None:
        """Drop _temp/job_* directories with no matching queue entry (earlier crashes)."""
        try:
            if not TEMP_DIR.is_dir():
                return
            with self._lock:
                live = {f"job_{j['id']}" for j in self.jobs}
            for d in TEMP_DIR.glob("job_*"):
                if d.is_dir() and d.name not in live:
                    shutil.rmtree(d, ignore_errors=True)
                    print(f"[queue] Removed orphaned temp dir {d.name}")
        except Exception as e:
            print(f"[queue] Temp dir prune failed: {e}")

    def load_queue(self):
        with self._lock:
            path = _ensure_queue_file()
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, list):
                        raise ValueError("queue.json root must be a list")
                    self.jobs = data
                except Exception as e:
                    # Preserve corrupt file — never silently wipe a large batch
                    try:
                        corrupt = path.with_suffix(path.suffix + ".corrupt")
                        shutil.copy2(path, corrupt)
                        print(f"[!] queue.json corrupt ({e}); copied to {corrupt.name}; starting empty")
                    except Exception as e2:
                        print(f"[!] queue.json corrupt ({e}); backup failed ({e2}); starting empty")
                    self.jobs = []
            else:
                self.jobs = []

    def _save_queue_unlocked(self):
        """Caller must hold self._lock."""
        try:
            path = _ensure_queue_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.jobs, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(str(tmp), str(path))
        except Exception as e:
            print(f"[!] Error saving queue: {e}")
            try:
                tmp = _ensure_queue_file().with_suffix(".json.tmp")
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def save_queue(self):
        with self._lock:
            self._save_queue_unlocked()

    def add_job(self, input_path: str, output_path: Optional[str] = None, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        inp = Path(input_path).resolve()
        if not inp.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        # Single ffprobe shared by media + HDR analysis
        raw_probe = self.pipeline.hdr_processor.probe_video_streams(inp)
        media = self.pipeline.probe_media(inp, probe=raw_probe)
        hdr_info = self.pipeline.hdr_processor.analyze_hdr_and_dovi(inp, probe=raw_probe)

        settings = load_settings()
        # Base defaults from the single source of truth, then overlay the ones
        # that must track a user-facing app setting. load_settings() always
        # returns every _DEFAULTS key, so a plain lookup is safe.
        default_config = copy.deepcopy(JOB_CONFIG_DEFAULTS)
        default_config.update({
            "autoname_output": settings["autoname_output"],
            "name_template_movie": settings["name_template_movie"],
            "name_template_episode": settings["name_template_episode"],
            "subtitle_search": settings["subtitle_search"],
        })

        has_audio_order = bool(config) and "audio_tracks_order" in config
        has_sub_tracks = bool(config) and "subtitle_tracks" in config
        if config:
            cleaned = dict(config)
            # Explicit key (including []) = user choice; missing key keeps auto defaults.
            audio_order = cleaned.pop("audio_tracks_order", None) if has_audio_order else None
            sub_tracks = cleaned.pop("subtitle_tracks", None) if has_sub_tracks else None
            default_config.update(cleaned)
            if has_audio_order:
                default_config["audio_tracks_order"] = (
                    list(audio_order) if audio_order is not None else []
                )
            if has_sub_tracks:
                default_config["subtitle_tracks"] = (
                    list(sub_tracks) if sub_tracks is not None else []
                )

        # Saved pipeline / Test Mode settings win over whatever the caller sent
        # (a stale browser, the watch folder): a job only keeps its preset.
        default_config.update(get_pipeline_settings(settings))

        # Auto-pick audio only when the caller didn't specify a selection — the
        # probe-based default is otherwise computed and immediately discarded.
        if not has_audio_order:
            default_config["audio_tracks_order"] = self.pipeline.select_default_audio_indices(
                media["audio_tracks"],
                languages=default_config.get("audio_languages"),
                best_only=default_config.get("audio_best_only", JOB_CONFIG_DEFAULTS["audio_best_only"]),
            )
        # Same for subtitles (e.g. watch-folder adds): the tracks extraction
        # would pick by the job's languages / kinds, stored so the edit dialog
        # shows exactly what will be extracted.
        if not has_sub_tracks:
            default_config["subtitle_tracks"] = select_default_subtitle_indices(
                media["subtitle_tracks"],
                languages=default_config.get("subtitle_languages"),
                kinds=default_config.get("subtitle_kinds"),
            )

        media_tag = build_media_tag(
            inp,
            video=media.get("video"),
            hdr_info=hdr_info,
            settings=self._naming_settings(default_config),
            container=default_config.get("container") or JOB_CONFIG_DEFAULTS["container"],
            resolution_target=default_config.get("resolution_target") or JOB_CONFIG_DEFAULTS["resolution_target"],
        )

        # Default output path: library naming when enabled, else _av1_boost.
        # When Output folder path is set (Watch settings), every job — manual or
        # watch — lands there. Blank + watch job → watch\encoded; blank + manual
        # → next to the source. Watch auto-queue On/Off does not change this.
        if not output_path:
            output_path = default_output_path(str(inp), default_config, media_tag)
        use_library_in_out = (
            bool(_configured_output_dir(default_config, settings) or default_config.get("watch_source_dir"))
            and default_config.get("autoname_output")
            and media_tag
        )
        output_path = _place_in_output_dir(
            library_output_path(inp, media_tag, config=default_config) if use_library_in_out else output_path,
            cfg=default_config,
            old_path=str(output_path),
            settings=settings,
        )

        job = {
            "id": str(uuid.uuid4()),
            "filename": inp.name,
            "input_path": str(inp),
            "output_path": str(Path(output_path)),
            "status": JobStatus.QUEUED,
            "stage": "Waiting in Queue",
            "stage_num": 0,
            "progress": 0.0,
            "stage_percent": 0.0,
            "fps": 0.0,
            "elapsed_seconds": 0,
            "created_at": time.time(),
            "media_info": {
                "duration": media["duration"],
                "video": media["video"],
                "audio_tracks": media["audio_tracks"],
                "subtitle_tracks": media["subtitle_tracks"],
                "hdr": hdr_info,
                # Container title — usually the scene release name even when the
                # file was renamed; ranks OpenSubtitles results by release
                "source_title": _container_title(media.get("format")),
            },
            "media_tag": media_tag,
            "config": default_config,
            "stats": {},
            "logs": [],
            "pid": None,
            "error": None
        }

        with self._lock:
            job["output_path"] = str(
                allocate_unique_output_path(
                    job["output_path"],
                    reserved=self._reserved_output_paths(),
                )
            )
            self.jobs.append(job)
        self.save_queue()
        self.emit_event("job_added", job)
        self._maybe_autostart_after_add()
        return job

    def _maybe_autostart_after_add(self) -> None:
        """If Settings → Start queue when a file is added, start an idle queue.

        Applies to every add (manual and watch). A paused queue stays paused
        (is_running is still true while paused). Independent of watch-folder
        auto-queue being on.
        """
        if not load_settings().get("watch_folder_autostart"):
            return
        if self.is_running:
            return
        try:
            self.start_queue()
        except RuntimeError:
            # e.g. a Stop still unwinding; the job stays queued
            pass

    def update_job(self, job_id: str, config_updates: Optional[Dict[str, Any]] = None,
                   media_tag_updates: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Update config / media tags on a queued job (preset, audio tracks, naming, etc.)."""
        with self._lock:
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            if not job:
                raise KeyError(f"Job not found: {job_id}")
            if job.get("status") != JobStatus.QUEUED:
                raise ValueError("Only queued jobs can be edited")

            cfg = dict(job.get("config") or {})
            updates = dict(config_updates or {})
            # Any explicit config edit means the job is intentionally current
            if updates:
                updates["preset_stale"] = False

            if "audio_tracks_order" in updates:
                # Explicit list (including []) — empty means video-only, not "use defaults"
                order = updates.pop("audio_tracks_order")
                cfg["audio_tracks_order"] = list(order) if order is not None else []
            if "subtitle_tracks" in updates:
                # Explicit list (including []) — empty means extract none
                subs = updates.pop("subtitle_tracks")
                cfg["subtitle_tracks"] = list(subs) if subs is not None else []

            cfg.update(updates)
            job["config"] = cfg

            tag = dict(job.get("media_tag") or {})
            if media_tag_updates:
                for k, v in media_tag_updates.items():
                    if v is None and k in ("year", "imdb_id", "tmdb_id", "note"):
                        tag[k] = None if k != "note" else ""
                    elif v is not None:
                        tag[k] = v
                tag["verified"] = True
                tag["source"] = "manual"

            # Refresh quality label when preset resolution / container / title fields change
            refresh_keys = ("test_mode", "trim_start", "trim_end", "container", "autoname_output", "resolution_target", "preset_id")
            if media_tag_updates or any(k in (config_updates or {}) for k in refresh_keys):
                video = (job.get("media_info") or {}).get("video")
                hdr_info = (job.get("media_info") or {}).get("hdr")
                # Render with the templates this job snapshotted at add time, so
                # a later change to the global template doesn't retroactively
                # rename jobs already sitting in the queue.
                job_settings = self._naming_settings(cfg)
                tag = refresh_media_tag_quality(
                    tag,
                    video=video,
                    hdr_info=hdr_info,
                    resolution_target=cfg.get("resolution_target") or JOB_CONFIG_DEFAULTS["resolution_target"],
                    container=cfg.get("container") or JOB_CONFIG_DEFAULTS["container"],
                    settings=job_settings,
                )
                job["media_tag"] = tag
                desired = default_output_path(job["input_path"], cfg, tag)
                desired = _place_in_output_dir(
                    desired, cfg=cfg, old_path=job.get("output_path") or ""
                )
                reserved = self._reserved_output_paths(exclude_job_id=job_id)
                job["output_path"] = str(
                    allocate_unique_output_path(desired, reserved=reserved)
                )

        self.save_queue()
        self.emit_event("job_update", job)
        return job

    def mark_preset_stale(self, preset_id: str) -> int:
        """Flag QUEUED jobs that still snapshot an older version of this preset."""
        if not preset_id:
            return 0
        touched: List[Dict[str, Any]] = []
        changed = False
        with self._lock:
            for job in self.jobs:
                if job.get("status") != JobStatus.QUEUED:
                    continue
                cfg = dict(job.get("config") or {})
                if cfg.get("preset_id") != preset_id:
                    continue
                if not cfg.get("preset_stale"):
                    cfg["preset_stale"] = True
                    job["config"] = cfg
                    changed = True
                touched.append(job)
        if changed:
            self.save_queue()
        for job in touched:
            self.emit_event("job_update", job)
        return len(touched)

    # Pipeline keys that change the output filename (extension / test suffix)
    _NAMING_PIPELINE_KEYS = ("container", "test_mode", "trim_start", "trim_end")

    def _stamp_pipeline_settings(
        self, job: Dict[str, Any], overrides: Dict[str, Any], reserved: Set[str]
    ) -> Dict[str, Any]:
        """
        Overlay the current pipeline settings onto a job's config. Caller holds
        self._lock and owns ``reserved`` (a set it keeps updated across jobs).
        Returns {key: (old, new)} for what actually changed — {} if nothing did.

        A job only permanently keeps its preset and per-job picks; these keys
        follow the current Pipeline / Test Mode settings. When a change alters
        the output name (container, test range) the filename is rebuilt in the
        same folder the job already writes to.
        """
        cfg = dict(job.get("config") or {})

        def current(key: str) -> Any:
            # A key the job never stored behaves as its default — not a change
            val = cfg.get(key)
            if val is None:
                val = "auto" if key == "ssimu2" else JOB_CONFIG_DEFAULTS.get(key)
            return val

        changes = {k: (current(k), v) for k, v in overrides.items() if current(k) != v}
        if not changes:
            return {}
        cfg.update(overrides)
        job["config"] = cfg

        if any(k in changes for k in self._NAMING_PIPELINE_KEYS):
            tag = dict(job.get("media_tag") or {})
            job_settings = dict(load_settings())
            for k in ("name_template_movie", "name_template_episode"):
                if cfg.get(k):
                    job_settings[k] = cfg[k]
            tag = refresh_media_tag_quality(
                tag,
                video=(job.get("media_info") or {}).get("video"),
                hdr_info=(job.get("media_info") or {}).get("hdr"),
                resolution_target=cfg.get("resolution_target") or JOB_CONFIG_DEFAULTS["resolution_target"],
                container=cfg.get("container") or JOB_CONFIG_DEFAULTS["container"],
                settings=job_settings,
            )
            job["media_tag"] = tag
            old_path = job.get("output_path") or ""
            reserved.discard(_resolved_str(old_path))
            desired = default_output_path(job["input_path"], cfg, tag)
            desired = _place_in_output_dir(desired, cfg=cfg, old_path=old_path)
            new_path = str(allocate_unique_output_path(desired, reserved=reserved))
            job["output_path"] = new_path
            reserved.add(_resolved_str(new_path))
        return changes

    def apply_pipeline_settings_to_queued(self) -> List[Dict[str, Any]]:
        """Stamp the saved pipeline settings onto every QUEUED job (on Save / toggle)."""
        overrides = get_pipeline_settings()
        if not overrides:
            return []
        updated: List[Dict[str, Any]] = []
        with self._lock:
            # Built once and mutated as paths are reassigned. Rebuilding it per
            # job made this O(n^2) with a Path.resolve() syscall per pair — a
            # multi-second freeze on a large queue over UNC paths — and each
            # rebuild still saw the stale paths of jobs later in the loop.
            reserved = self._reserved_output_paths()
            for job in self.jobs:
                if job.get("status") != JobStatus.QUEUED:
                    continue
                if self._stamp_pipeline_settings(job, overrides, reserved):
                    updated.append(job)

        if updated:
            self.save_queue()
            for job in updated:
                self.emit_event("job_update", job)
        return updated

    def apply_test_mode_to_queued(self, test_mode_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Save Test Mode as a pipeline setting, then stamp it onto every QUEUED job."""
        fields: Dict[str, Any] = {"test_mode": bool(test_mode_config.get("test_mode"))}
        if fields["test_mode"]:
            fields["trim_start"] = str(test_mode_config.get("trim_start") or JOB_CONFIG_DEFAULTS["trim_start"])
            fields["trim_end"] = str(test_mode_config.get("trim_end") or JOB_CONFIG_DEFAULTS["trim_end"])
        save_settings({"pipeline": fields})
        return self.apply_pipeline_settings_to_queued()

    def _refresh_pipeline_settings_for_start(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Re-read the pipeline settings as a job starts. Returns what changed."""
        overrides = get_pipeline_settings()
        if not overrides:
            return {}
        with self._lock:
            reserved = self._reserved_output_paths(exclude_job_id=job["id"])
            changes = self._stamp_pipeline_settings(job, overrides, reserved)
        if changes:
            self.save_queue()
            self.emit_event("job_update", job)
        return changes

    def requeue_job(self, job_id: str) -> Dict[str, Any]:
        """Reset a cancelled/failed job back to QUEUED so it can be edited and run again."""
        with self._lock:
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            if not job:
                raise KeyError(f"Job not found: {job_id}")
            if job.get("status") not in (JobStatus.CANCELLED, JobStatus.FAILED):
                raise ValueError("Only cancelled or failed jobs can be reset")
            if self.current_job_id == job_id and self.is_running:
                raise ValueError("Cannot reset the currently running job")

            job["status"] = JobStatus.QUEUED
            job["stage"] = "Waiting in Queue"
            job["stage_num"] = 0
            job["progress"] = 0.0
            job["fps"] = 0.0
            job["elapsed_seconds"] = 0
            job["error"] = None
            job["logs"] = []
            job["stats"] = {}
            job["pid"] = None

        self.save_queue()
        self.emit_event("job_update", job)
        return job

    def remove_job(self, job_id: str) -> bool:
        with self._lock:
            if self.current_job_id == job_id and self.is_running:
                self._cancel_event.set()
                self.pipeline.kill_all_processes()
            self.jobs = [j for j in self.jobs if j["id"] != job_id]
        self.save_queue()
        self.emit_event("job_removed", {"id": job_id})
        return True

    def reorder_jobs(self, ordered_ids: List[str]) -> List[Dict[str, Any]]:
        """Reorder the queue to match ordered_ids (first QUEUED is next to encode).

        Active encodes (EXTRACTING / FINAL_ENCODE / REMUXING) stay pinned at the
        front in their previous relative order and are ignored in ordered_ids —
        they are not drag-reorderable. Any other job IDs not listed are appended
        in their previous relative order.
        """
        active_statuses = {
            JobStatus.EXTRACTING,
            JobStatus.FINAL_ENCODE,
            JobStatus.REMUXING,
        }
        with self._lock:
            active = [j for j in self.jobs if j.get("status") in active_statuses]
            active_ids = {j["id"] for j in active}
            by_id = {j["id"]: j for j in self.jobs if j["id"] not in active_ids}
            seen: Set[str] = set()
            new_rest: List[Dict[str, Any]] = []
            for jid in ordered_ids:
                if jid in active_ids or jid in seen:
                    continue
                job = by_id.get(jid)
                if not job:
                    continue
                new_rest.append(job)
                seen.add(jid)
            for j in self.jobs:
                if j["id"] not in active_ids and j["id"] not in seen:
                    new_rest.append(j)
            self.jobs = active + new_rest
            result = list(self.jobs)
        self.save_queue()
        self.emit_event("queue_reordered", {"job_ids": [j["id"] for j in result]})
        return result

    def start_queue(self):
        # A Stop only signals the worker; it may still be unwinding (killing
        # processes, moving a watch-folder source). Starting over it would clear
        # the cancel it is acting on and leave two worker loops running.
        # Already running is not "stopping": answer at once instead of waiting.
        with self._lock:
            if self.is_running:
                return
        prev = self._worker_thread
        if prev is not None and prev.is_alive() and prev is not threading.current_thread():
            prev.join(timeout=5)
            if prev.is_alive():
                raise RuntimeError("The queue is still stopping — try Start again in a moment.")

        # Atomic check-and-set so concurrent Start cannot spawn two workers.
        with self._lock:
            if self.is_running:
                return
            self.is_running = True
            self.is_paused = False
            self._end_pause_clock()
            self._cancel_event.clear()

        # Retry failed jobs on Start; cancelled stay cancelled until Reset
        requeued = []
        with self._lock:
            for j in self.jobs:
                if j["status"] == JobStatus.FAILED:
                    j["status"] = JobStatus.QUEUED
                    j["stage"] = "Waiting in Queue"
                    j["stage_num"] = 0
                    j["progress"] = 0.0
                    j["fps"] = 0.0
                    j["elapsed_seconds"] = 0
                    j["error"] = None
                    j["logs"] = []
                    j["stats"] = {}
                    j["pid"] = None
                    requeued.append(j)
        self.save_queue()
        for j in requeued:
            self.emit_event("job_update", j)

        # Clear any leftover ffmpeg/SVT from a previous stop/crash
        self.pipeline.kill_all_processes()
        self._worker_thread = threading.Thread(target=self._process_queue_loop, daemon=True)
        self._worker_thread.start()
        self.emit_event("queue_started", {})

    def _paused_seconds(self) -> float:
        """Total time spent paused so far (including a pause in progress)."""
        with self._pause_clock_lock:
            total = self._paused_total
            if self._paused_since is not None:
                total += time.monotonic() - self._paused_since
            return total

    def _active_clock(self) -> float:
        """A monotonic clock that stands still while the queue is paused — stage
        timings ("encode took …", fps) count only time spent working."""
        return time.monotonic() - self._paused_seconds()

    def _end_pause_clock(self) -> float:
        """Close an open pause (resume / stop / start). Returns its length."""
        with self._pause_clock_lock:
            if self._paused_since is None:
                return 0.0
            paused_for = time.monotonic() - self._paused_since
            self._paused_total += paused_for
            self._paused_since = None
            return paused_for

    def _log_to_current_job(self, msg: str) -> None:
        log = self._job_log
        if log is not None:
            try:
                log(msg, update_stage=False)
            except Exception:
                pass

    def pause_queue(self):
        if not self.is_paused:
            self._log_to_current_job("Paused")
            with self._pause_clock_lock:
                if self._paused_since is None:
                    self._paused_since = time.monotonic()
        self.is_paused = True
        self.pipeline.suspend_current_process()
        self.emit_event("queue_paused", {})

    def resume_queue(self):
        if self.is_paused:
            paused_for = self._end_pause_clock()
            self._log_to_current_job(f"Resumed (paused {_fmt_duration(paused_for)})")
        self.is_paused = False
        self.pipeline.resume_current_process()
        self.emit_event("queue_resumed", {})

    def stop_queue(self):
        self._end_pause_clock()  # Stop while paused must not leave the clock frozen
        self.is_running = False
        self.current_job_id = None
        self._cancel_event.set()
        # Immediately kill ffmpeg / SVT / encode process trees
        self.pipeline.kill_all_processes()
        self.emit_event("queue_stopped", {})

    def _process_queue_loop(self):
        while self.is_running:
            if self.is_paused:
                time.sleep(1)
                continue

            next_job = None
            with self._lock:
                for j in self.jobs:
                    if j["status"] == JobStatus.QUEUED:
                        next_job = j
                        break

            if not next_job:
                # No more jobs in queue
                self.is_running = False
                self.current_job_id = None
                self.emit_event("queue_completed", {})
                break

            self._execute_single_job(next_job)

            # A cancel that did not come from stop_queue (which also clears
            # is_running) targeted just the job that finished — e.g. the user
            # deleted the running job. Clear it so the rest of the queue can
            # proceed instead of every later job short-circuiting forever.
            if self.is_running and self._cancel_event.is_set():
                self._cancel_event.clear()

    def _wait_if_paused(self) -> None:
        """Block while paused (does not clear cancel). Used between in-job stages."""
        while self.is_paused and self.is_running and not self._cancel_event.is_set():
            time.sleep(0.25)

    @staticmethod
    def _dovi_skip_reason(hdr_analysis: Optional[Dict[str, Any]]) -> Optional[str]:
        """
        Reason a Dolby Vision source must be skipped rather than encoded, or None.

        Profile 5 (BL signal-compatibility id 0) has an IPT-PQc2 base layer that is
        not a valid HDR10 picture; profile 4 is legacy/rare and quarantined for
        manual review. Filename-only DoVi hints are never authoritative — if probe
        did not confirm DoVi side_data, quarantine rather than guess P5 vs P7/P8.1.
        Confirmed DoVi with neither a usable compat id nor a known dual-layer
        profile is also quarantined. (see README.md "HDR & Dolby Vision")
        """
        if not hdr_analysis:
            return None

        # Filename says Dolby Vision but probe did not confirm side_data (failed
        # probe OR successful probe that missed DoVi). P5 cannot be ruled out.
        if hdr_analysis.get("dovi_unverified") or (
            hdr_analysis.get("dovi_filename_hint") and not hdr_analysis.get("is_dovi")
        ):
            return (
                "Dolby Vision indicated by filename but not confirmed by probe — "
                "profile 5 cannot be ruled out, so this file is quarantined for "
                "manual review. Original file kept untouched."
            )

        if not hdr_analysis.get("is_dovi"):
            return None

        def _as_int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        compat = _as_int(hdr_analysis.get("dovi_compat_id"))
        profile = _as_int(hdr_analysis.get("dovi_profile"))

        # compat_id 0 is the authoritative "base layer cannot stand alone" signal;
        # profile 5 corroborates it.
        if compat == 0 or profile == 5:
            return (
                "Dolby Vision Profile 5 (base-layer signal compatibility 0) — the "
                "base layer is IPT-PQc2 and cannot stand alone as HDR10; encoding it "
                "would bake in a colour cast. Original file kept untouched."
            )
        if profile == 4:
            return (
                "Dolby Vision Profile 4 (legacy) — quarantined for manual review; "
                "not encoded. Original file kept untouched."
            )
        # Non-zero compat → base layer stands alone.
        if compat is not None:
            return None
        # Known dual-layer profiles are safe even when compat is missing from the probe.
        if profile in (7, 8):
            return None
        return (
            "Dolby Vision detected but profile / base-layer compatibility could not "
            "be determined — quarantined for manual review. Original file kept untouched."
        )

    def _archive_job_to_history(self, job: Dict[str, Any], job_id: str) -> None:
        """Persist job JSON under history/ and drop it from the active queue."""
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        history_file = HISTORY_DIR / f"{job_id}.json"
        try:
            with open(history_file, "w", encoding="utf-8") as hf:
                json.dump(job, hf, indent=2)
        except Exception as he:
            print(f"[!] Error saving history file: {he}")
        with self._lock:
            self.jobs = [j for j in self.jobs if j["id"] != job_id]
        self.save_queue()
        self.emit_event("history_updated", job)
        self.emit_event("job_removed", {"id": job_id})

    @staticmethod
    def _move_watch_source(job: Dict[str, Any], subdir: str) -> None:
        """
        Move a watch-folder job's source into <watch_dir>/<subdir>/ (failed/,
        skipped/). The job is about to leave the queue for history, and a source
        left in the watched folder would be re-queued by the next startup sweep.
        Only touches sources still sitting directly in the watched folder.
        """
        watch_dir = (job.get("config") or {}).get("watch_source_dir")
        if not watch_dir:
            return
        src = Path(job.get("input_path") or "")
        try:
            if not src.is_file() or src.parent.resolve() != Path(watch_dir).resolve():
                return
            dest_dir = Path(watch_dir) / subdir
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            if dest.exists():
                dest = dest_dir / f"{src.stem}_{job['id'][:8]}{src.suffix}"
            shutil.move(str(src), str(dest))
            job["input_path"] = str(dest)
            job.setdefault("logs", []).append(_stamp_log(f"Source moved to {dest}"))
        except Exception as e:
            job.setdefault("logs", []).append(_stamp_log(f"Could not move source to {subdir}/: {e}"))

    def _append_history_logs(self, job_id: str, lines: List[str]) -> None:
        """Add log lines to a job already archived in history/ (background
        subtitle step) and tell the UI to refresh Finished."""
        if not lines:
            return
        path = HISTORY_DIR / f"{job_id}.json"
        with self._history_lock:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, ValueError):
                return  # deleted from Finished meanwhile
            record.setdefault("logs", []).extend(lines)
            tmp = path.with_suffix(".json.tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(record, f, indent=2)
                os.replace(str(tmp), str(path))
            except OSError as e:
                print(f"[!] Could not add subtitle log to history: {e}")
                return
        self.emit_event("history_updated", record)

    def _finalize_skipped(self, job, job_id, start_time, reason):
        """Record a skipped source in history without producing any output."""
        job["status"] = JobStatus.SKIPPED
        job["stage"] = "Skipped — original file kept"
        job["progress"] = 100.0
        job["stage_percent"] = 100.0
        job["elapsed_seconds"] = int(time.time() - start_time)
        job["completed_at"] = time.time()
        job["error"] = None
        job["skip_reason"] = reason
        job["stats"] = {
            "skipped": True,
            "reason": reason,
            "duration_seconds": job["elapsed_seconds"],
        }
        self._move_watch_source(job, "skipped")
        self._archive_job_to_history(job, job_id)
        print(f"[queue] Job {job_id} skipped: {reason}")

    def _finalize_failed(self, job, job_id, start_time, error: str, cancelled: bool = False) -> None:
        """Finish a failed or cancelled encode.

        Cancelled jobs stay in the pending queue (Reset to re-run). Failed jobs
        are archived to Finished with logs for debugging.
        """
        status = JobStatus.CANCELLED if cancelled else JobStatus.FAILED
        job["status"] = status
        job["stage"] = f"{'Cancelled' if cancelled else 'Error'}: {error}"
        job["error"] = error
        job["elapsed_seconds"] = int(time.time() - start_time)
        job["completed_at"] = time.time()
        job["pid"] = None
        logs = job.setdefault("logs", [])
        marker = "CANCELLED" if cancelled else "FAILED"
        logs.append(f"{marker}: {error}")
        try:
            tb = traceback.format_exc().strip()
            if tb and not tb.startswith("NoneType: None"):
                for line in tb.splitlines()[-40:]:
                    logs.append(line)
        except Exception:
            pass
        while len(logs) > 200:
            logs.pop(0)
        job["stats"] = {
            "failed": not cancelled,
            "cancelled": cancelled,
            "duration_seconds": job["elapsed_seconds"],
            "error": error,
        }
        if cancelled:
            # Keep in queue for Reset — do not move to Finished.
            self.save_queue()
            # Unless the cancel was the job being deleted: an update for it now
            # would make the UI add the removed job back as a ghost card.
            with self._lock:
                still_queued = any(j["id"] == job_id for j in self.jobs)
            if still_queued:
                self.emit_event("job_update", job)
            print(f"[!] Job {job_id} cancelled: {error}")
            return
        self._move_watch_source(job, "failed")
        self._archive_job_to_history(job, job_id)
        print(f"[!] Job {job_id} failed: {error}")

    @staticmethod
    def _naming_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
        """
        App settings with the job's naming templates overlaid, so a later change to the global naming settings doesn't
        retroactively rename jobs already in the queue.
        """
        settings = dict(load_settings())
        for k in ("name_template_movie", "name_template_episode"):
            if cfg.get(k):
                settings[k] = cfg[k]
        return settings

    def _encode_with_handbrake(
        self,
        job: Dict[str, Any],
        job_id: str,
        cfg: Dict[str, Any],
        encode_input: Path,
        out_path: Path,
        seg_media: Optional[Dict[str, Any]],
        ordered_audio: List[Dict[str, Any]],
        autocrop: bool,
        hdr_analysis: Dict[str, Any],
        svt_params: Dict[str, Any],
        settings_live: Dict[str, Any],
        encode_temp_dir: Path,
        encode_cb,
        append_log,
    ) -> Tuple[Path, Optional[Dict[str, int]]]:
        """
        HandBrakeCLI encode + audio + mux (core/handbrake_encode.py)
        straight to ``*.partial.<ext>`` beside the deliverable, then check the
        output kept the source's HDR signalling before publishing it.

        Returns (published path, crop HandBrake applied). The crop is None when
        autocrop was on but the applied crop couldn't be read from its log.
        """
        container = str(cfg.get("container", JOB_CONFIG_DEFAULTS["container"])).lower()
        container = "webm" if container == "webm" else "mp4"
        out_path = Path(out_path)
        if out_path.suffix.lower() != f".{container}":
            out_path = out_path.with_suffix(f".{container}")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Last-chance collision guard (file may have appeared since job was queued)
        with self._lock:
            reserved = self._reserved_output_paths(exclude_job_id=job_id)
        safe_out = allocate_unique_output_path(out_path, reserved=reserved)
        if safe_out.resolve() != out_path.resolve():
            append_log(f"Output path already occupied — writing to {safe_out.name} instead", update_stage=False)
            out_path = safe_out
        if str(out_path) != job.get("output_path"):
            job["output_path"] = str(out_path)
            self.save_queue()
            self.emit_event("job_update", job)
        partial = out_path.with_name(f"{out_path.stem}.partial{out_path.suffix}")

        hb_params, removed_svt = split_removed_svt_params(svt_params)
        if removed_svt:
            append_log(
                "Ignoring SVT option(s) removed in Tritium 4.2: " + ", ".join(removed_svt)
                + " — edit the preset to clear them",
                update_stage=False,
            )
        # HandBrake leaves the AV1 chroma siting unset; take it from the source
        chroma = svt_chroma_position(hdr_analysis.get("chroma_location"))
        if chroma:
            hb_params.setdefault("chroma-sample-position", chroma)
        preserve_dovi = bool(settings_live.get("preserve_dovi_rpu"))
        all_audio = (seg_media or job.get("media_info") or {}).get("audio_tracks") or []
        cmd = build_handbrake_command(
            encode_input,
            partial,
            container=container,
            crf=cfg.get("crf", JOB_CONFIG_DEFAULTS["crf"]),
            preset=cfg.get("preset", JOB_CONFIG_DEFAULTS["preset"]),
            svt_params=hb_params,
            audio_tracks=ordered_audio,
            audio_track_numbers=hb_audio_track_numbers(all_audio, ordered_audio),
            autocrop=autocrop,
            target_height=target_height_for(
                cfg.get("resolution_target", JOB_CONFIG_DEFAULTS["resolution_target"])
            ),
            # P7 → 8.1 conversion and RPU crop correction are HandBrake's own
            dynamic_metadata="all" if preserve_dovi else "hdr10plus",
        )
        append_log("HandBrakeCLI " + subprocess.list2cmdline(cmd[1:]), update_stage=False)

        t_enc = self._active_clock()
        try:
            self.pipeline.run_handbrake_encode(
                cmd,
                partial,
                encode_temp_dir / "handbrake.log",
                progress_cb=encode_cb,
                cancel_event=self._cancel_event,
                pid_cb=lambda pid: self._set_job_pid(job, pid),
            )
        except Exception:
            self.pipeline.kill_all_processes()
            raise
        finally:
            self._set_job_pid(job, None)
        if self._cancel_event.is_set():
            partial.unlink(missing_ok=True)
            raise RuntimeError("Job cancelled.")

        enc_seconds = self._active_clock() - t_enc
        try:
            hb_log = (encode_temp_dir / "handbrake.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            hb_log = ""
        hb_summary = summarize_handbrake_log(hb_log)
        enc_bits = [f"HandBrake encode took {_fmt_duration(enc_seconds)}"]
        if hb_summary["frames"]:
            enc_bits.append(f"{hb_summary['frames']} frames")
            if enc_seconds > 0:
                enc_bits.append(f"{hb_summary['frames'] / enc_seconds:.2f} fps avg")
            enc_bits.append(f"{hb_summary['dropped'] or 0} dropped")
        append_log(" · ".join(enc_bits), update_stage=False)
        for note in hb_summary["notes"]:
            append_log(f"HandBrake: {note}", update_stage=False)

        applied_crop: Optional[Dict[str, int]] = {"left": 0, "top": 0, "right": 0, "bottom": 0}
        if autocrop:
            applied_crop = parse_applied_crop(hb_log)
            if applied_crop is None:
                append_log("Autocrop: couldn't read the crop HandBrake applied from its log", update_stage=False)
            elif any(applied_crop.values()):
                c = applied_crop
                append_log(
                    f"Autocrop (HandBrake): top {c['top']} · bottom {c['bottom']} · "
                    f"left {c['left']} · right {c['right']}",
                    update_stage=False,
                )
            else:
                append_log("Autocrop (HandBrake): no black bars found", update_stage=False)

        t_verify = self._active_clock()
        job["status"] = JobStatus.REMUXING
        job["stage"] = "Verifying HandBrake output"
        job["stage_num"] = 3
        job["stage_percent"] = 0.0
        job["progress"] = map_stage_progress(JobStatus.REMUXING)
        self.save_queue()
        self.emit_event("job_update", job)

        try:
            # Probe only: the partial's name comes from the source's (DV / HDR10
            # tokens), which would make a lost-HDR output look HDR
            out_info = self.pipeline.hdr_processor.analyze_hdr_and_dovi(
                partial, use_filename=False
            )
        except Exception as e:
            out_info = None
            append_log(f"Output HDR check skipped: {e}", update_stage=False)
        if out_info is not None:
            bits = ["HDR10" if out_info.get("is_hdr") else "SDR"]
            if out_info.get("is_dovi"):
                bits.append(f"DoVi profile {out_info.get('dovi_profile', '?')}")
            if out_info.get("is_hdr10plus"):
                bits.append("HDR10+")
            append_log(f"Output check: {' · '.join(bits)}", update_stage=False)

            src_hdr = bool(hdr_analysis.get("is_hdr") or hdr_analysis.get("is_dovi"))
            if src_hdr and not out_info.get("is_hdr"):
                partial.unlink(missing_ok=True)
                raise RuntimeError("HandBrake output lost the source's HDR signalling — output discarded.")
            if hdr_analysis.get("is_dovi") and preserve_dovi and not out_info.get("is_dovi"):
                append_log(
                    "WARNING: Dolby Vision not signalled in the output — HDR10 only "
                    "(RPU passthrough is best-effort)",
                    update_stage=False,
                )
            if hdr_analysis.get("is_hdr10plus") and not out_info.get("is_hdr10plus"):
                # ffprobe here can't read AV1 frame metadata, so absence isn't proof
                append_log(
                    "HDR10+ passed to HandBrake (--hdr-dynamic-metadata); not "
                    "independently confirmed in the AV1 output",
                    update_stage=False,
                )
            out_path = self._rename_for_output_hdr(
                job, job_id, cfg, out_path, output_hdr_label(hdr_analysis, out_info), append_log
            )

        try:
            os.replace(str(partial), str(out_path))
        except Exception as e:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"Could not publish HandBrake output to {out_path}: {e}") from e
        append_log(
            f"Published final: {out_path.name} (verify + publish {_fmt_duration(self._active_clock() - t_verify)})",
            update_stage=False,
        )
        return out_path, applied_crop

    def _rename_for_output_hdr(
        self,
        job: Dict[str, Any],
        job_id: str,
        cfg: Dict[str, Any],
        out_path: Path,
        actual_hdr: str,
        append_log,
    ) -> Path:
        """
        The queue-time name predicted the HDR format from the source; once the
        output check knows what the file really carries, re-render the name
        with that label (e.g. DoVi expected but the RPU didn't survive → HDR10).
        Only for library-named outputs — a custom output path is left alone.
        """
        tag = job.get("media_tag") or {}
        # Jobs queued before hdr_label existed only have it inside tag["quality"]
        expected = tag.get("hdr_label")
        if expected is None:
            expected = " ".join(str(tag.get("quality") or "").split()[1:])
        if not cfg.get("autoname_output", True) or expected == actual_hdr:
            return out_path
        mi = job.get("media_info") or {}
        predicted = Path(default_output_path(job["input_path"], cfg, tag))
        # A collision suffix (_2, _3, …) may have been added at queue time.
        # Compare stems only — the job may write to a custom / watch output dir.
        if not out_path.stem.startswith(predicted.stem):
            return out_path
        new_tag = refresh_media_tag_quality(
            tag,
            video=mi.get("video"),
            hdr_info=mi.get("hdr"),
            resolution_target=cfg.get("resolution_target") or JOB_CONFIG_DEFAULTS["resolution_target"],
            container=cfg.get("container") or JOB_CONFIG_DEFAULTS["container"],
            settings=self._naming_settings(cfg),
            hdr_override=actual_hdr,
        )
        job["media_tag"] = new_tag
        desired = Path(
            _place_in_output_dir(
                default_output_path(job["input_path"], cfg, new_tag),
                cfg=cfg,
                old_path=str(out_path),
            )
        )
        if desired.stem == predicted.stem:
            return out_path  # the template has no [hdr] token
        with self._lock:
            reserved = self._reserved_output_paths(exclude_job_id=job_id)
        new_path = Path(allocate_unique_output_path(desired, reserved=reserved))
        append_log(
            f"Output HDR is {actual_hdr or 'SDR'} (named {expected or 'SDR'} at queue time) "
            f"— naming it {new_path.name}",
            update_stage=False,
        )
        job["output_path"] = str(new_path)
        self.save_queue()
        self.emit_event("job_update", job)
        return new_path

    def _execute_single_job(self, job: Dict[str, Any]):
        job_id = job["id"]
        self.current_job_id = job_id
        self._wait_if_paused()
        if self._cancel_event.is_set() or not self.is_running:
            # Early exit before the try/finally below — still clear the sticky id.
            if self.current_job_id == job_id:
                self.current_job_id = None
            return
        # Pipeline / Test Mode settings are read now, as the job starts — the job
        # only permanently keeps its preset. (Also refreshes the output name if
        # the container or test range changed since it was queued.)
        pipeline_changes = self._refresh_pipeline_settings_for_start(job)
        inp_path = Path(job["input_path"])
        out_path = Path(job["output_path"])
        cfg = job["config"]
        job_temp_dir = TEMP_DIR / f"job_{job_id}"
        encode_temp_dir = job_temp_dir / "encode"
        encode_temp_dir.mkdir(parents=True, exist_ok=True)

        start_time = time.time()
        job_paused_at_start = self._paused_seconds()  # pauses before this job don't count
        job["status"] = JobStatus.EXTRACTING
        job["stage"] = "Probing Audio & HDR/DoVi Metadata"
        job["stage_num"] = 1
        job["progress"] = map_stage_progress(JobStatus.EXTRACTING)
        job["elapsed_seconds"] = 0
        self.save_queue()
        self.emit_event("job_update", job)

        progress_lock = threading.Lock()
        stop_elapsed = threading.Event()
        last_progress_emit = [0.0]  # monotonic; throttle elapsed ticker when progress is active

        def emit_elapsed(extra: Optional[Dict[str, Any]] = None):
            with progress_lock:
                # Skip if a real progress event landed in the last ~0.9s (same elapsed clock)
                if time.monotonic() - last_progress_emit[0] < 0.9:
                    return
                job["elapsed_seconds"] = int(time.time() - start_time)
                self.emit_event("job_progress", self._progress_payload(job, **(extra or {})))

        def elapsed_ticker():
            # Keep the UI elapsed clock moving even when a stage has no progress lines
            while not stop_elapsed.wait(1.0):
                emit_elapsed()

        ticker_thread = threading.Thread(target=elapsed_ticker, daemon=True)
        ticker_thread.start()
        emit_elapsed()  # start counting immediately

        try:
            # 1. HDR & Dolby Vision — drives the skip / quarantine policy below;
            # HandBrake reads the same metadata from the source for the encode.
            # Reuse HDR analysis from queue-time probe when present (same source file)
            hdr_analysis = (job.get("media_info") or {}).get("hdr")
            need_hdr = (
                not isinstance(hdr_analysis, dict)
                or "is_hdr" not in hdr_analysis
                # Pre-frame_probed cached analyses missed frame-level mastering/CLL
                # → HandBrake shows SDR. Testing for a missing mastering_display
                # instead would re-probe forever on HDR sources that legitimately
                # carry no MDCV (HLG, many WEB-DLs) — six deep seeks every run.
                or not hdr_analysis.get("frame_probed")
                # Analyses cached before chroma_location was recorded
                or "chroma_location" not in hdr_analysis
            )
            if need_hdr:
                hdr_analysis = self.pipeline.hdr_processor.analyze_hdr_and_dovi(inp_path)
                mi = job.get("media_info")
                if isinstance(mi, dict):
                    mi["hdr"] = hdr_analysis

            settings_live = load_settings()
            if not handbrake_available():
                raise RuntimeError(
                    "HandBrakeCLI.exe missing from bin/handbrake/ — run setup.bat to install it."
                )

            # Preset SVT-AV1-Tritium params plus the app-wide lp / low-memory
            # settings; HandBrake hands them to the Tritium library as -x options.
            svt_params = dict(cfg.get("svt_params") or {})
            try:
                lp_n = int(settings_live.get("svt_lp", 0) or 0)
            except Exception:
                lp_n = 0
            if lp_n > 0:
                svt_params["lp"] = lp_n
            else:
                # Do not invent lp beyond settings / preset: SVT lp 0 is auto.
                svt_params.pop("lp", None)
            if settings_live.get("svt_low_memory"):
                svt_params["low-memory"] = 1
            else:
                svt_params.pop("low-memory", None)

            encode_input = inp_path
            seg_media = None

            # 2. Audio selection — None/missing → defaults; [] → video-only
            audio_tracks = job["media_info"]["audio_tracks"]
            selected_indices = cfg.get("audio_tracks_order", None)
            if selected_indices is None:
                selected_indices = self.pipeline.select_default_audio_indices(
                    audio_tracks,
                    languages=cfg.get("audio_languages"),
                    best_only=cfg.get("audio_best_only", JOB_CONFIG_DEFAULTS["audio_best_only"]),
                )
            ordered_audio = self.pipeline.select_and_prioritize_audio(
                audio_tracks,
                selected_indices,
                bitrate_51=cfg.get("audio_bitrate_51", JOB_CONFIG_DEFAULTS["audio_bitrate_51"]),
                bitrate_stereo=cfg.get("audio_bitrate_stereo", JOB_CONFIG_DEFAULTS["audio_bitrate_stereo"]),
                languages=cfg.get("audio_languages"),
                audio_format=cfg.get("audio_format", JOB_CONFIG_DEFAULTS["audio_format"]),
            )
            audio_fmt = normalize_audio_format(cfg.get("audio_format"))
            audio_label = "E-AC-3" if audio_fmt == "eac3" else "Opus"
            container_early = str(cfg.get("container", JOB_CONFIG_DEFAULTS["container"])).lower()
            if audio_fmt == "eac3" and container_early == "webm":
                raise RuntimeError("E-AC-3 audio requires MP4 container (WebM only supports Opus).")

            def append_log(msg: str, update_stage: bool = False):
                line = _stamp_log(msg)
                with progress_lock:
                    job["logs"].append(line)
                    if len(job["logs"]) > 200:
                        job["logs"].pop(0)
                    if update_stage:
                        job["stage"] = msg  # stage text stays unstamped
                    job["elapsed_seconds"] = int(time.time() - start_time)
                    last_progress_emit[0] = time.monotonic()
                    self.emit_event("job_progress", self._progress_payload(job, log=line))

            self._job_log = append_log  # pause / resume land in this job's log

            if pipeline_changes:
                append_log(
                    "Pipeline settings applied at start: "
                    + ", ".join(f"{k} {old} → {new}" for k, (old, new) in pipeline_changes.items()),
                    update_stage=False,
                )

            # Source summary
            mi = job.get("media_info") or {}
            v = mi.get("video") or {}
            src_bits = [f"{str(v.get('codec') or '?').upper()} {v.get('width', '?')}×{v.get('height', '?')}"]
            if "10" in str(v.get("pix_fmt") or ""):
                src_bits.append("10-bit")
            src_fps = frame_rate_to_float(v.get("r_frame_rate"))
            if src_fps:
                src_bits.append(f"{src_fps:.3f} fps")
            if mi.get("duration"):
                src_bits.append(_fmt_duration(mi["duration"]))
            try:
                src_bits.append(_fmt_bytes(inp_path.stat().st_size))
            except OSError:
                pass
            append_log(f"Source: {inp_path.name} · " + " · ".join(src_bits), update_stage=False)
            if mi.get("source_title"):
                append_log(f"Source title (release name): {mi['source_title']}", update_stage=False)

            append_log(_naming_line(job.get("media_tag") or {}), update_stage=False)

            if cfg.get("watch_source_dir"):
                via = {"tag": "from the filename's preset- tag", "default": "the watch-folder default"}.get(
                    cfg.get("watch_preset_via"), "none matched — built-in defaults"
                )
                append_log(
                    f"Watch folder: queued from {cfg['watch_source_dir']} · preset "
                    + (f"\"{cfg['preset_name']}\" " if cfg.get("preset_name") else "")
                    + f"({via})",
                    update_stage=False,
                )

            # Encode settings in plain words (the HandBrake command has the rest)
            preset_label = f"\"{cfg['preset_name']}\"" if cfg.get("preset_name") else "none (built-in defaults)"
            if cfg.get("preset_stale"):
                preset_label += " (outdated — the saved preset changed since this job was queued)"
            res_target = str(cfg.get("resolution_target") or "source")
            set_bits = [
                f"Settings: preset {preset_label}",
                f"CRF {float(cfg.get('crf', JOB_CONFIG_DEFAULTS['crf'])):g}",
                f"SVT preset {cfg.get('preset', JOB_CONFIG_DEFAULTS['preset'])}",
                "source resolution" if res_target == "source" else f"max {res_target}",
                container_early.upper(),
                f"audio {audio_label}",
                "autocrop on" if cfg.get("autocrop", True) else "autocrop off",
            ]
            if hdr_analysis.get("is_dovi"):
                set_bits.append("DoVi RPU kept" if settings_live.get("preserve_dovi_rpu") else "DoVi RPU discarded")
            if lp_n > 0:
                set_bits.append(f"lp {lp_n}")
            if settings_live.get("svt_low_memory"):
                set_bits.append("low-memory")
            n_svt = len([k for k in (cfg.get("svt_params") or {}) if k not in ("lp", "low-memory")])
            if n_svt:
                set_bits.append(f"{n_svt} SVT override(s)")
            set_bits.append("SSIMU2 on" if cfg.get("ssimu2_post") else "SSIMU2 off")
            append_log(" · ".join(set_bits), update_stage=False)

            if (hdr_analysis.get("is_dovi") or hdr_analysis.get("is_hdr")
                    or hdr_analysis.get("is_hdr10plus")
                    or hdr_analysis.get("dovi_filename_hint")
                    or hdr_analysis.get("hdr10plus_filename_hint")):
                bits = []
                if hdr_analysis.get("is_dovi"):
                    prof = hdr_analysis.get("dovi_profile")
                    bits.append(f"DoVi{f' profile {prof}' if prof is not None else ''}")
                elif hdr_analysis.get("dovi_filename_hint"):
                    bits.append("DoVi (filename hint)")
                if hdr_analysis.get("is_hdr10plus"):
                    bits.append("HDR10+")
                elif hdr_analysis.get("is_hdr"):
                    bits.append("HDR")
                if hdr_analysis.get("mastering_display"):
                    bits.append("mastering")
                if hdr_analysis.get("content_light"):
                    bits.append("CLL")
                trc = hdr_analysis.get("color_transfer") or "?"
                append_log(
                    f"Color: {', '.join(bits)} · transfer={trc} · flags via {hdr_analysis.get('source', 'probe')}",
                    update_stage=False,
                )
            else:
                append_log("Color: SDR (no HDR/DoVi signaling)", update_stage=False)

            # Policy (see README.md "HDR & Dolby Vision"): a Dolby Vision source whose base layer cannot
            # stand on its own — profile 5, BL signal-compatibility id 0 — must never
            # be re-encoded. Leave the original file untouched.
            skip_reason = self._dovi_skip_reason(hdr_analysis)
            if skip_reason:
                append_log(f"Skipping encode — {skip_reason}", update_stage=True)
                self._finalize_skipped(job, job_id, start_time, skip_reason)
                return

            # Preserve inspector / default selection order
            selected_indices = [t["stream_index"] for t in ordered_audio]
            if ordered_audio:
                for t in ordered_audio:
                    append_log("Audio: " + _audio_track_line(t, audio_label), update_stage=False)
            else:
                append_log("Audio: none (video-only)", update_stage=False)

            if cfg.get("test_mode"):
                trim_start = cfg.get("trim_start", JOB_CONFIG_DEFAULTS["trim_start"])
                trim_end = cfg.get("trim_end", JOB_CONFIG_DEFAULTS["trim_end"])
                append_log(
                    f"Test Mode enabled — encoding segment {trim_start} → {trim_end}",
                    update_stage=True
                )
                append_log(
                    "Keeping selected audio stream(s): "
                    + ", ".join(str(i) for i in selected_indices),
                    update_stage=False
                )
                segment_path = job_temp_dir / f"test_segment{inp_path.suffix}"
                t_seg = self._active_clock()
                encode_input = self.pipeline.extract_test_segment(
                    inp_path,
                    trim_start,
                    trim_end,
                    segment_path,
                    audio_stream_indices=selected_indices,
                    progress_cb=lambda msg: append_log(msg, update_stage=True),
                    cancel_event=self._cancel_event,
                    fps=frame_rate_to_float(
                        (job["media_info"].get("video") or {}).get("r_frame_rate")
                    ),
                )
                if self._cancel_event.is_set():
                    self.pipeline.kill_all_processes()
                    raise RuntimeError("Job cancelled.")
                try:
                    seg_size = _fmt_bytes(Path(encode_input).stat().st_size)
                except OSError:
                    seg_size = "?"
                append_log(
                    f"Test segment cut in {_fmt_duration(self._active_clock() - t_seg)} ({seg_size})",
                    update_stage=False,
                )

                # Segment only contains the mapped audio tracks — rebind stream indexes in order
                seg_media = self.pipeline.probe_media(encode_input)
                seg_audio = seg_media.get("audio_tracks", [])
                if len(seg_audio) < len(ordered_audio):
                    raise RuntimeError(
                        f"Test segment has {len(seg_audio)} audio track(s), "
                        f"expected {len(ordered_audio)} from selection."
                    )
                remapped = []
                for i, orig in enumerate(ordered_audio):
                    track = dict(orig)
                    track["stream_index"] = seg_audio[i]["stream_index"]
                    track["channels"] = seg_audio[i].get("channels", orig.get("channels"))
                    track["codec"] = seg_audio[i].get("codec", orig.get("codec"))
                    track["bitrate"] = seg_audio[i].get("bitrate", orig.get("bitrate"))
                    remapped.append(track)
                ordered_audio = remapped
                append_log(
                    f"Using {len(ordered_audio)} selected audio track(s) on test segment.",
                    update_stage=False
                )


            # Progress lines in the job log every 5 %. Avg fps / ETA come from the
            # active clock (pauses excluded) and the encode input's frame count —
            # HandBrake's own avg keeps counting while the process is suspended.
            enc_dur = float((seg_media or job.get("media_info") or {}).get("duration") or 0)
            enc_fps = frame_rate_to_float(((job.get("media_info") or {}).get("video") or {}).get("r_frame_rate"))
            enc_total_frames = enc_dur * enc_fps if enc_dur > 0 and enc_fps else 0.0
            enc_progress = {"start": None, "next": 5}

            def log_encode_progress(pct: float) -> None:
                # Short inputs (Test Mode clips): HandBrake's % counts frames handed
                # to SVT, whose look-ahead buffer is a big share of a short clip —
                # the % races ahead and fps / ETA are meaningless. Full films: fine.
                if enc_dur < _PROGRESS_LOG_MIN_SECONDS:
                    return
                now = self._active_clock()
                if enc_progress["start"] is None:
                    enc_progress["start"] = now  # first report: HandBrake has begun encoding
                    return
                if pct < enc_progress["next"]:
                    return
                step = int(pct // 5) * 5
                enc_progress["next"] = step + 5
                spent = now - enc_progress["start"]
                bits = [f"Encode {step}%"]
                if enc_total_frames and spent > 0:
                    bits.append(f"{enc_total_frames * pct / 100 / spent:.2f} fps avg")
                bits.append(f"elapsed {_fmt_duration(spent)}")
                if 0 < pct < 100 and spent > 0:
                    bits.append(f"ETA ~{_fmt_duration(spent * (100 - pct) / pct)}")
                append_log(" · ".join(bits), update_stage=False)

            def encode_cb(data):
                if self._cancel_event.is_set():
                    self.pipeline.kill_all_processes()
                    return  # don't raise into stdout reader (mis-labels cancel)

                raw = data.get("raw_log", "")
                if raw:
                    raw = _stamp_log(raw)
                with progress_lock:
                    if raw:
                        job["logs"].append(raw)
                        if len(job["logs"]) > 200:
                            job["logs"].pop(0)

                    if data.get("percent") is not None:
                        job["stage_percent"] = float(data["percent"])
                        job["progress"] = map_stage_progress(job["status"], data["percent"])
                    if data.get("fps") is not None:
                        job["fps"] = data["fps"]

                    job["elapsed_seconds"] = int(time.time() - start_time)
                    last_progress_emit[0] = time.monotonic()
                    self.emit_event("job_progress", self._progress_payload(job, log=raw))

                # Outside progress_lock: append_log takes it too
                if data.get("percent") is not None and not raw:
                    log_encode_progress(float(data["percent"]))

            self._wait_if_paused()
            if self._cancel_event.is_set():
                raise RuntimeError("Job cancelled.")

            # 3. HandBrakeCLI: autocrop + encode + audio + mux, then verify and publish.
            job["status"] = JobStatus.FINAL_ENCODE
            job["stage"] = "Encoding (HandBrake · SVT-AV1-Tritium)"
            job["stage_num"] = 2
            job["stage_percent"] = 0.0
            job["progress"] = map_stage_progress(JobStatus.FINAL_ENCODE)
            self.save_queue()
            self.emit_event("job_update", job)

            # Autocrop is HandBrake's (conservative: least crop wins); it also
            # corrects the DoVi RPU's active area for the crop it applies.
            final_out, crop = self._encode_with_handbrake(
                job, job_id, cfg, encode_input, out_path, seg_media, ordered_audio,
                bool(cfg.get("autocrop", True)), hdr_analysis, svt_params, settings_live,
                encode_temp_dir, encode_cb, append_log,
            )
            out_path = final_out

            # 5. Optional post-encode SSIMU2 score (Pipeline Status → SSIMU2 post-encode score)
            # Final file is already published — cancel after this point still counts as COMPLETED.
            ssimu2_stats: Dict[str, Any] = {}
            if cfg.get("ssimu2_post", False) and crop is None:
                append_log("SSIMU2 skipped: the applied crop is unknown, so the score would compare mismatched frames", update_stage=False)
            elif cfg.get("ssimu2_post", False) and not self._cancel_event.is_set():
                job["stage"] = "Measuring SSIMU2"
                job["stage_num"] = 3
                self.save_queue()
                self.emit_event("job_update", job)
                t_ssim = self._active_clock()
                try:
                    # Full encodes: sample every 3rd frame; short test clips: every frame
                    metric_skip = 1 if cfg.get("test_mode") else 3
                    ssimu2_stats = self.pipeline.measure_final_ssimu2(
                        source_file=encode_input,
                        encoded_file=final_out,
                        ssimu2_mode=cfg.get("ssimu2", "auto"),
                        crop=crop,
                        resolution_target=cfg.get("resolution_target", JOB_CONFIG_DEFAULTS["resolution_target"]),
                        skip=metric_skip,
                        progress_cb=lambda msg: append_log(msg, update_stage=True),
                        cancel_event=self._cancel_event,
                    )
                except Exception as metric_err:
                    append_log(f"SSIMU2 measurement skipped: {metric_err}", update_stage=False)
                    ssimu2_stats = {}
                append_log(f"SSIMU2 step took {_fmt_duration(self._active_clock() - t_ssim)}", update_stage=False)
            elif self._cancel_event.is_set():
                append_log(
                    "Cancelled after publish — keeping the output (skipping SSIMU2)",
                    update_stage=False,
                )

            # 6. Completed Stats & Cleanup
            # Test mode: compare against the segment we actually encoded, not the full movie
            compare_src = encode_input if cfg.get("test_mode") else inp_path
            try:
                orig_size = compare_src.stat().st_size
            except OSError:
                orig_size = 0
            try:
                final_size = final_out.stat().st_size
            except OSError:
                final_size = 0
            ratio = ((orig_size - final_size) / orig_size * 100) if orig_size > 0 else 0

            # Test mode: extrapolate the full-length output size from the segment's
            # size/duration ratio. Audio scales ~linearly with duration (constant
            # target bitrate), but video is CRF-mode — this assumes the tested
            # segment's complexity is representative of the whole title, which
            # won't always hold (an action scene vs. a dialogue scene, etc.).
            estimated_full_bytes = None
            if cfg.get("test_mode") and final_size > 0:
                full_duration = float((job.get("media_info") or {}).get("duration") or 0)
                seg_duration = float((seg_media or {}).get("duration") or 0)
                if full_duration > 0 and seg_duration > 0:
                    estimated_full_bytes = int(round(final_size * (full_duration / seg_duration)))

            # Watch-folder verification + return-to-source-folder. Only applies to
            # jobs the watcher itself queued (marked via watch_source_dir) — a
            # manually-added job never has this set, even if its source happens to
            # sit inside the watched folder.
            watch_source_dir = cfg.get("watch_source_dir")
            if watch_source_dir and cfg.get("test_mode"):
                # A test segment says nothing about the full encode — the source
                # stays in the watch folder (still dedup'd while it's queued).
                append_log("Test Mode — source left in the watch folder", update_stage=False)
            elif watch_source_dir:
                try:
                    verify_probe = self.pipeline.probe_media(final_out)
                    verify_ok = bool(verify_probe.get("video")) and verify_probe.get("duration", 0) > 0
                    if verify_ok and not cfg.get("test_mode"):
                        src_duration = float((job.get("media_info") or {}).get("duration") or 0)
                        out_duration = float(verify_probe.get("duration") or 0)
                        if src_duration > 0 and abs(out_duration - src_duration) > max(5.0, src_duration * 0.05):
                            verify_ok = False
                    if verify_ok:
                        dest_dir = Path(watch_source_dir) / "encoded_sources"
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        src_file = Path(job["input_path"])
                        if src_file.is_file():
                            dest_path = dest_dir / src_file.name
                            if dest_path.exists():
                                dest_path = dest_dir / f"{src_file.stem}_{job_id[:8]}{src_file.suffix}"
                            shutil.move(str(src_file), str(dest_path))
                            job["input_path"] = str(dest_path)
                            append_log(f"Source verified — moved to {dest_path}", update_stage=False)
                    else:
                        append_log(
                            "Output verification failed — source left in watch folder for review",
                            update_stage=False,
                        )
                except Exception as watch_err:
                    append_log(f"Watch-folder post-processing skipped: {watch_err}", update_stage=False)

            job["status"] = JobStatus.COMPLETED
            job["stage"] = "Encode Completed"
            job["progress"] = 100.0
            job["elapsed_seconds"] = int(time.time() - start_time)
            job["completed_at"] = time.time()
            job["error"] = None
            job["stats"] = {
                "original_bytes": orig_size,
                "final_bytes": final_size,
                "reduction_percent": round(ratio, 1),
                "duration_seconds": job["elapsed_seconds"],
                "test_mode": bool(cfg.get("test_mode")),
                "encoder": "handbrake",
            }
            if ssimu2_stats:
                job["stats"]["ssimu2_avg"] = ssimu2_stats.get("avg")
                job["stats"]["ssimu2_p15"] = ssimu2_stats.get("p15")
                job["stats"]["ssimu2_min"] = ssimu2_stats.get("min")
                job["stats"]["ssimu2_frames"] = ssimu2_stats.get("frames")
                job["stats"]["ssimu2_mode"] = ssimu2_stats.get("mode")
            if cfg.get("test_mode"):
                job["stats"]["trim_start"] = cfg.get("trim_start")
                job["stats"]["trim_end"] = cfg.get("trim_end")
                if estimated_full_bytes is not None:
                    job["stats"]["estimated_full_bytes"] = estimated_full_bytes
            if crop and any(crop.values()):
                job["stats"]["crop"] = crop

            # Result summary
            out_duration = float(
                ((seg_media or {}).get("duration") if cfg.get("test_mode") else (job.get("media_info") or {}).get("duration"))
                or 0
            )
            res_bits = [f"Result: {_fmt_bytes(final_size)}"]
            if orig_size > 0:
                # ratio is the reduction: positive = smaller than the source
                res_bits.append(
                    f"{'−' if ratio >= 0 else '+'}{abs(ratio):.1f}% vs {_fmt_bytes(orig_size)} "
                    + ("test segment" if cfg.get("test_mode") else "source")
                )
            if out_duration > 0 and final_size > 0:
                res_bits.append(f"{final_size * 8 / out_duration / 1e6:.2f} Mb/s overall")
            paused_s = self._paused_seconds() - job_paused_at_start
            res_bits.append(
                f"total {_fmt_duration(max(0.0, time.time() - start_time - paused_s))}"
                + (f" (+{_fmt_duration(paused_s)} paused)" if paused_s >= 1 else "")
            )
            append_log(" · ".join(res_bits), update_stage=False)
            if estimated_full_bytes:
                full_dur = (job.get("media_info") or {}).get("duration")
                append_log(
                    f"Estimated full-length size ≈ {_fmt_bytes(estimated_full_bytes)} for {_fmt_duration(full_dur)}"
                    " (assumes the test range is representative)",
                    update_stage=False,
                )

            # Save to separate history file
            self._archive_job_to_history(job, job_id)

            # Subtitle extract / OpenSubtitles fill — background so the next
            # encode can start immediately. Test Mode skips it unless
            # "Extract subtitles in Test Mode" is on: then it runs unchanged
            # (full-length, from the original, online search included) as a
            # check that subtitles are found — timing isn't cut to the segment.
            if not cfg.get("test_mode") or cfg.get("test_mode_subtitles"):
                src_for_subs = Path(job["input_path"])
                out_for_subs = Path(final_out)
                sub_basename = out_for_subs.stem
                sub_dir = out_for_subs.parent
                jid = job_id
                fname = job.get("filename") or src_for_subs.name
                settings_preview = load_settings()
                search_on = cfg.get("subtitle_search")
                if search_on is None:
                    search_on = settings_preview.get("subtitle_search", False)
                do_extract = bool(cfg.get("extract_subtitles", True))
                do_search = bool(search_on)

                if do_extract or do_search:
                    def _subtitle_worker():
                        logs: List[str] = []
                        # Same lines, stamped, for the job's log in Finished — the
                        # job was archived before this thread started
                        job_lines: List[str] = []

                        def _cb(msg: str):
                            logs.append(msg)
                            job_lines.append(_stamp_log(f"Subtitles: {msg}"))
                            print(f"[subs {fname}] {msg}")

                        try:
                            if do_extract:
                                result = self.pipeline.extract_subtitles_from_source(
                                    src_for_subs,
                                    progress_cb=_cb,
                                    languages=cfg.get("subtitle_languages"),
                                    kinds=cfg.get("subtitle_kinds"),
                                    output_dir=sub_dir,
                                    basename=sub_basename,
                                    strip_credits=bool(cfg.get("subtitle_strip_credits", True)),
                                    # Per-job choice from the inspector / edit
                                    # dialog; None (older jobs) = by rule
                                    stream_indices=cfg.get("subtitle_tracks"),
                                )
                                self.emit_event("subtitle_extract", {
                                    "id": jid,
                                    "filename": fname,
                                    "status": result.get("status"),
                                    "extracted": result.get("extracted", 0),
                                    "files": result.get("files") or [],
                                    "message": result.get("message"),
                                    "logs": logs,
                                })

                            if do_search:
                                settings = load_settings()
                                tag = job.get("media_tag") or {}
                                video = (job.get("media_info") or {}).get("video") or {}
                                src_fps = frame_rate_to_float(video.get("r_frame_rate"))
                                season = tag.get("season")
                                episode = tag.get("episode")
                                try:
                                    season_i = int(season) if season is not None else None
                                except (TypeError, ValueError):
                                    season_i = None
                                try:
                                    episode_i = int(episode) if episode is not None else None
                                except (TypeError, ValueError):
                                    episode_i = None
                                search_result = search_and_download_missing(
                                    src_for_subs,
                                    languages=list(cfg.get("subtitle_languages") or []),
                                    imdb_id=tag.get("imdb_id"),
                                    query=tag.get("title") or src_for_subs.stem,
                                    api_key=str(settings.get("opensubtitles_api_key") or ""),
                                    username=str(settings.get("opensubtitles_username") or ""),
                                    password=str(settings.get("opensubtitles_password") or ""),
                                    progress_cb=_cb,
                                    basename=sub_basename,
                                    output_dir=sub_dir,
                                    strip_credits=bool(cfg.get("subtitle_strip_credits", True)),
                                    kinds=list(cfg.get("subtitle_kinds") or ["standard"]),
                                    fps=src_fps,
                                    media_kind=tag.get("media_kind"),
                                    season=season_i,
                                    episode=episode_i,
                                    # Filename + container title: a renamed
                                    # library file keeps its release name
                                    # (BluRay / REMUX / group) only in the title
                                    release_hint=" ".join(filter(None, (
                                        src_for_subs.name,
                                        (job.get("media_info") or {}).get("source_title"),
                                    ))),
                                )
                                self.emit_event("subtitle_search", {
                                    "id": jid,
                                    "filename": fname,
                                    "status": search_result.get("status"),
                                    "downloaded": search_result.get("downloaded", 0),
                                    "files": search_result.get("files") or [],
                                    "missing": search_result.get("missing") or [],
                                    "message": search_result.get("message"),
                                    "logs": logs,
                                })
                        except Exception as sub_err:
                            print(f"[!] Subtitle extract/search failed for {fname}: {sub_err}")
                            job_lines.append(_stamp_log(f"Subtitles: FAILED — {sub_err}"))
                            self.emit_event("subtitle_extract", {
                                "id": jid,
                                "filename": fname,
                                "status": "error",
                                "extracted": 0,
                                "files": [],
                                "message": str(sub_err),
                                "logs": logs,
                            })
                        self._append_history_logs(jid, job_lines)

                    job_lines_note = (
                        "Subtitles: running in the background "
                        + ("(extract" + (" + online search" if do_search else "") + ")" if do_extract else "(online search)")
                        + " — results are added below when done"
                    )
                    self._append_history_logs(jid, [_stamp_log(job_lines_note)])
                    threading.Thread(
                        target=_subtitle_worker,
                        name=f"subs-{job_id[:8]}",
                        daemon=True,
                    ).start()

        except Exception as e:
            self._finalize_failed(
                job,
                job_id,
                start_time,
                str(e),
                cancelled=self._cancel_event.is_set(),
            )

        finally:
            self._job_log = None
            stop_elapsed.set()
            job["elapsed_seconds"] = int(time.time() - start_time)
            self.current_job_id = None
            # Always clean job temp (success path also calls cleanup — idempotent)
            try:
                self.pipeline.cleanup_temp_files(job_temp_dir)
            except Exception as ce:
                print(f"[!] Temp cleanup: {ce}")
            self.save_queue()
            # Push a live update when the job is still pending (cancelled stays in queue).
            # Completed / failed were already moved to history + job_removed.
            with self._lock:
                still_pending = any(j["id"] == job_id for j in self.jobs)
            if still_pending:
                self.emit_event("job_update", job)

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
import tempfile
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Set

from core.pipeline import TranscodePipeline, frame_rate_to_float, normalize_audio_format
from core.app_settings import load_settings, JOB_CONFIG_DEFAULTS
from core.media_tagging import build_media_tag, library_output_path, refresh_media_tag_quality
from core.subtitle_search import search_and_download_missing
from core.svt_binary import get_svt_status

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


def _windows_short_path(path: Path) -> Path:
    """8.3 short path when available — avoids spaces that break some native tools."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        got = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))
        if got and buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return path


def stage_svt_meta_file(src: Path, job_id: str, filename: str) -> Path:
    """
    Copy RPU/HDR10+ JSON to a space-free temp location.
    Project paths may contain spaces (e.g. '.git Projects'); SVT rejects those.
    Returns the staged path (caller should clean via staged_meta_dirs / cleanup_staged_meta).
    """
    src = Path(src)
    # Prefer OS temp (usually no spaces); fall back to drive root _av1q_meta;
    # the system-drive root is a last-resort candidate that never depends on
    # a (possibly space-containing) username/profile path.
    system_drive = os.environ.get("SystemDrive", "C:")
    candidates = [
        Path(tempfile.gettempdir()) / "av1queue_meta" / job_id,
        Path(src.drive + "\\") / "_av1q_meta" / job_id if getattr(src, "drive", None) else None,
        Path(system_drive + "\\") / "_av1q_meta" / job_id,
    ]
    dest_dir = None
    for c in candidates:
        if c is None:
            continue
        if " " in str(c):
            continue
        dest_dir = c
        break
    if dest_dir is None:
        dest_dir = Path(tempfile.gettempdir()) / "av1queue_meta" / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    shutil.copy2(src, dest)
    if " " in str(dest):
        short = _windows_short_path(dest)
        if " " not in str(short):
            dest = short
        else:
            # 8.3 short names are disabled on this volume — GetShortPathNameW
            # just echoes the long path back with no error. Relocate to the
            # system-drive root, which never depends on a (possibly
            # space-containing) username/profile path.
            safe_dir = Path(system_drive + "\\") / "_av1q_meta" / job_id
            safe_dir.mkdir(parents=True, exist_ok=True)
            safe_dest = safe_dir / filename
            os.replace(dest, safe_dest)
            dest = safe_dest
    if " " in str(dest):
        raise RuntimeError(f"Could not stage a space-free path for {filename} (got {dest})")
    return dest


def cleanup_staged_meta(job_id: str, staged_paths: Optional[List[str]] = None) -> None:
    """Remove staged RPU/JSON dirs for this job (temp root + parents of staged files)."""
    roots = [Path(tempfile.gettempdir()) / "av1queue_meta" / job_id]
    if staged_paths:
        for sp in staged_paths:
            try:
                p = Path(sp)
                if p.is_file() or p.is_dir():
                    roots.append(p.parent if p.is_file() else p)
            except Exception:
                pass
    seen = set()
    for root in roots:
        try:
            key = str(root.resolve()).lower()
        except Exception:
            key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if root.is_dir():
                shutil.rmtree(root, ignore_errors=True)
        except Exception:
            pass

HISTORY_DIR.mkdir(parents=True, exist_ok=True)


class JobStatus:
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


def _resolved_str(path: Optional[str]) -> str:
    """Canonical form used as the reserved-output-path key."""
    if not path:
        return ""
    try:
        return str(Path(path).resolve())
    except Exception:
        return str(path)


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


def hdr_metadata_blockers(
    hdr_analysis: Dict[str, Any],
    svt_caps: Dict[str, Any],
    bin_dir: Path,
) -> List[str]:
    """
    Reasons the source's dynamic HDR metadata could not survive this encode.

    Only *probe-confirmed* layers are considered. A filename hint is not proof
    that the source carries anything, so it must never fail a job — it only ever
    triggers a best-effort extraction attempt.
    """
    reasons: List[str] = []
    bin_dir = Path(bin_dir)
    if hdr_analysis.get("is_hdr10plus"):
        if not svt_caps.get("hdr10plus_json"):
            reasons.append(
                "source is HDR10+, but this SVT-AV1-Tritium binary has no "
                "--hdr10plus-json (needs a libhdr10plus-enabled build)"
            )
        if not (bin_dir / "hdr10plus_tool.exe").is_file():
            reasons.append("source is HDR10+, but bin/hdr10plus_tool.exe is missing")
    return reasons


HDR_STRICT_HINT = (
    "Install the missing dependency, or turn off "
    "\"Fail when HDR metadata cannot be preserved\" in Settings "
    "to encode this as plain HDR10."
)


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
        orphaned SvtAv1EncApp/svt_encode.py process tree it left running
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
        if config:
            cleaned = dict(config)
            # Explicit key (including []) = user choice; missing key keeps auto defaults.
            audio_order = cleaned.pop("audio_tracks_order", None) if has_audio_order else None
            default_config.update(cleaned)
            if has_audio_order:
                default_config["audio_tracks_order"] = (
                    list(audio_order) if audio_order is not None else []
                )

        # Auto-pick audio only when the caller didn't specify a selection — the
        # probe-based default is otherwise computed and immediately discarded.
        if not has_audio_order:
            default_config["audio_tracks_order"] = self.pipeline.select_default_audio_indices(
                media["audio_tracks"],
                languages=default_config.get("audio_languages"),
                best_only=default_config.get("audio_best_only", JOB_CONFIG_DEFAULTS["audio_best_only"]),
            )

        media_tag = build_media_tag(
            inp,
            video=media.get("video"),
            hdr_info=hdr_info,
            settings=settings,
            container=default_config.get("container") or JOB_CONFIG_DEFAULTS["container"],
            resolution_target=default_config.get("resolution_target") or JOB_CONFIG_DEFAULTS["resolution_target"],
        )

        # Default output path: library naming when enabled, else _av1_boost
        if not output_path:
            output_path = default_output_path(str(inp), default_config, media_tag)

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
                "hdr": hdr_info
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
        return job

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

            cfg.update(updates)
            job["config"] = cfg

            tag = dict(job.get("media_tag") or {})
            if media_tag_updates:
                for k, v in media_tag_updates.items():
                    if v is None and k in ("year", "imdb_id", "tmdb_id"):
                        tag[k] = None
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
                job_settings = dict(load_settings())
                for k in ("name_template_movie", "name_template_episode"):
                    if cfg.get(k):
                        job_settings[k] = cfg[k]
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

    def apply_test_mode_to_queued(self, test_mode_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Stamp current Test Mode settings onto every QUEUED job (used on Start / toggle)."""
        fields = {
            "test_mode": bool(test_mode_config.get("test_mode")),
        }
        if fields["test_mode"]:
            fields["trim_start"] = str(test_mode_config.get("trim_start") or JOB_CONFIG_DEFAULTS["trim_start"])
            fields["trim_end"] = str(test_mode_config.get("trim_end") or JOB_CONFIG_DEFAULTS["trim_end"])

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
                cfg = dict(job.get("config") or {})
                cfg.update(fields)
                job["config"] = cfg
                reserved.discard(_resolved_str(job.get("output_path")))
                desired = default_output_path(
                    job["input_path"], cfg, job.get("media_tag")
                )
                new_path = str(allocate_unique_output_path(desired, reserved=reserved))
                job["output_path"] = new_path
                reserved.add(_resolved_str(new_path))
                updated.append(job)

        if updated:
            self.save_queue()
            for job in updated:
                self.emit_event("job_update", job)
        return updated

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

        Any job IDs not listed are appended in their previous relative order.
        """
        with self._lock:
            by_id = {j["id"]: j for j in self.jobs}
            seen: Set[str] = set()
            new_jobs: List[Dict[str, Any]] = []
            for jid in ordered_ids:
                job = by_id.get(jid)
                if not job or jid in seen:
                    continue
                new_jobs.append(job)
                seen.add(jid)
            for j in self.jobs:
                if j["id"] not in seen:
                    new_jobs.append(j)
            self.jobs = new_jobs
            result = list(self.jobs)
        self.save_queue()
        self.emit_event("queue_reordered", {"job_ids": [j["id"] for j in result]})
        return result

    def start_queue(self, test_mode_config: Optional[Dict[str, Any]] = None):
        # Atomic check-and-set so concurrent Start cannot spawn two workers.
        with self._lock:
            if self.is_running:
                return
            self.is_running = True
            self.is_paused = False
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

        # Per-job Test Mode only — do not stamp the UI toggle onto every queued job here.
        # (UI confirms before Start when Test Mode is on with multiple jobs; toggle syncs via apply-test-mode.)
        _ = test_mode_config

        # Clear any leftover ffmpeg/SVT from a previous stop/crash
        self.pipeline.kill_all_processes()
        self._worker_thread = threading.Thread(target=self._process_queue_loop, daemon=True)
        self._worker_thread.start()
        self.emit_event("queue_started", {})

    def pause_queue(self):
        self.is_paused = True
        self.pipeline.suspend_current_process()
        self.emit_event("queue_paused", {})

    def resume_queue(self):
        self.is_paused = False
        self.pipeline.resume_current_process()
        self.emit_event("queue_resumed", {})

    def stop_queue(self):
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
        self._archive_job_to_history(job, job_id)
        print(f"[queue] Job {job_id} skipped: {reason}")

    def _finalize_failed(self, job, job_id, start_time, error: str, cancelled: bool = False) -> None:
        """Archive a failed/cancelled encode to history with logs for debugging."""
        status = JobStatus.CANCELLED if cancelled else JobStatus.FAILED
        job["status"] = status
        job["stage"] = f"{'Cancelled' if cancelled else 'Error'}: {error}"
        job["error"] = error
        job["elapsed_seconds"] = int(time.time() - start_time)
        job["completed_at"] = time.time()
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
        self._archive_job_to_history(job, job_id)
        print(f"[!] Job {job_id} {status.lower()}: {error}")

    def _execute_single_job(self, job: Dict[str, Any]):
        job_id = job["id"]
        self.current_job_id = job_id
        self._wait_if_paused()
        if self._cancel_event.is_set() or not self.is_running:
            # Early exit before the try/finally below — still clear the sticky id.
            if self.current_job_id == job_id:
                self.current_job_id = None
            return
        inp_path = Path(job["input_path"])
        out_path = Path(job["output_path"])
        cfg = job["config"]
        job_temp_dir = TEMP_DIR / f"job_{job_id}"
        audio_temp_dir = job_temp_dir / "audio"
        encode_temp_dir = job_temp_dir / "encode"
        audio_temp_dir.mkdir(parents=True, exist_ok=True)
        encode_temp_dir.mkdir(parents=True, exist_ok=True)
        staged_meta_paths: List[str] = []

        start_time = time.time()
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
            # 1. HDR & Dolby Vision (needed for SVT flags before video encode)
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
            )
            if need_hdr:
                hdr_analysis = self.pipeline.hdr_processor.analyze_hdr_and_dovi(
                    inp_path, encode_temp_dir
                )
                mi = job.get("media_info")
                if isinstance(mi, dict):
                    mi["hdr"] = hdr_analysis
            def _svt_params_cli(params: Any) -> str:
                if not isinstance(params, dict) or not params:
                    return ""
                parts = []
                for key, val in params.items():
                    if val is None or val == "":
                        continue
                    flag = key if str(key).startswith("--") else f"--{key}"
                    parts.append(f"{flag} {val}")
                return " ".join(parts)

            hdr_flags = " ".join(hdr_analysis.get("svt_flags", []))
            # Merge pipeline-owned SVT flags (lp / low-memory) over preset params.
            svt_merged = dict(cfg.get("svt_params") or {})
            settings_live = load_settings()
            try:
                lp_n = int(settings_live.get("svt_lp", 0) or 0)
            except Exception:
                lp_n = 0
            if lp_n > 0:
                svt_merged["lp"] = lp_n
            else:
                svt_merged.pop("lp", None)
            if settings_live.get("svt_low_memory"):
                svt_merged["low-memory"] = 1
            else:
                svt_merged.pop("low-memory", None)
            preset_svt = _svt_params_cli(svt_merged)
            # Do not invent --lp beyond settings / preset: SVT --lp 0 is auto.
            extra_svt_params = " ".join(p for p in (hdr_flags, preset_svt) if p)

            encode_input = inp_path
            seg_media = None

            # 2. Audio prep — None/missing → defaults; [] → video-only
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
            container_early = str(cfg.get("container", JOB_CONFIG_DEFAULTS["container"])).lower()
            if audio_fmt == "eac3" and container_early == "webm":
                raise RuntimeError("E-AC-3 audio requires MP4 container (WebM only supports Opus).")

            def append_log(msg: str, update_stage: bool = False):
                with progress_lock:
                    job["logs"].append(msg)
                    if len(job["logs"]) > 200:
                        job["logs"].pop(0)
                    if update_stage:
                        job["stage"] = msg
                    job["elapsed_seconds"] = int(time.time() - start_time)
                    last_progress_emit[0] = time.monotonic()
                    self.emit_event("job_progress", self._progress_payload(job, log=msg))

            def set_stage_progress(pct: float, stage: Optional[str] = None, log_msg: Optional[str] = None):
                """Update step-1 ring + overall bar; optional one-shot log line."""
                with progress_lock:
                    pct = max(0.0, min(100.0, float(pct)))
                    job["stage_percent"] = pct
                    job["progress"] = map_stage_progress(JobStatus.EXTRACTING, pct)
                    if stage:
                        job["stage"] = stage
                    job["elapsed_seconds"] = int(time.time() - start_time)
                    last_progress_emit[0] = time.monotonic()
                    if log_msg:
                        job["logs"].append(log_msg)
                        if len(job["logs"]) > 200:
                            job["logs"].pop(0)
                    extra = {"log": log_msg} if log_msg else {}
                    self.emit_event("job_progress", self._progress_payload(job, **extra))

            # HDR / DoVi (+ optional HDR10+ JSON when Tritium supports it)
            svt_caps = (get_svt_status() or {}).get("caps") or {}
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
                if hdr_flags:
                    append_log(f"SVT HDR flags: {hdr_flags}", update_stage=False)
                else:
                    append_log("WARNING: HDR/DoVi detected but no SVT color flags were built", update_stage=False)
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

            # Preflight — fail in seconds rather than after hours of encoding.
            hdr_strict = bool(settings_live.get("hdr_strict", True))
            hdr_blockers = hdr_metadata_blockers(
                hdr_analysis, svt_caps, self.pipeline.bin_dir
            )
            if hdr_blockers:
                if hdr_strict:
                    raise RuntimeError(
                        "Cannot preserve dynamic HDR metadata — "
                        + "; ".join(hdr_blockers)
                        + ". "
                        + HDR_STRICT_HINT
                    )
                for reason in hdr_blockers:
                    append_log(
                        f"WARNING: {reason} — encoding with static HDR10 flags only",
                        update_stage=False,
                    )

            duration_sec = None
            try:
                duration_sec = float((job.get("media_info") or {}).get("duration") or 0) or None
            except (TypeError, ValueError):
                duration_sec = None

            # HDR10+ extracted later from encode_input (after test segment) so frames match
            want_hdr10plus = bool(
                (hdr_analysis.get("is_hdr10plus") or hdr_analysis.get("hdr10plus_filename_hint"))
                and svt_caps.get("hdr10plus_json")
            )
            if want_hdr10plus and not (self.pipeline.bin_dir / "hdr10plus_tool.exe").is_file():
                append_log(
                    "hdr10plus_tool.exe missing from bin/ — run setup_env / setup.ps1 to install it",
                    update_stage=False,
                )
                want_hdr10plus = False

            hdr10plus_json_path: Optional[str] = None

            # DoVi RPU passthrough — opt-in (settings.preserve_dovi_rpu, off by
            # default). Only reachable here for P7/P8.1 sources: _dovi_skip_reason
            # already sent P5/P4/unconfirmed DoVi through the skip/quarantine path
            # above, so is_dovi at this point always means a safe base layer.
            # Best-effort like HDR10+: a failure here degrades to plain HDR10
            # rather than blocking the job — see README.md "HDR & Dolby Vision".
            want_dovi_rpu = bool(
                settings_live.get("preserve_dovi_rpu")
                and hdr_analysis.get("is_dovi")
                and svt_caps.get("dolby_vision_rpu")
            )
            if want_dovi_rpu and not (self.pipeline.bin_dir / "dovi_tool.exe").is_file():
                append_log(
                    "dovi_tool.exe missing from bin/ — run setup_env / setup.ps1 to install it "
                    "— encoding without DoVi RPU passthrough",
                    update_stage=False,
                )
                want_dovi_rpu = False

            dovi_rpu_path: Optional[str] = None

            # Preserve inspector / default selection order
            selected_indices = [t["stream_index"] for t in ordered_audio]
            if ordered_audio:
                append_log(
                    "Audio selection: "
                    + ", ".join(
                        f"{t.get('language','und').upper()}#{t['stream_index']}"
                        for t in ordered_audio
                    ),
                    update_stage=False
                )
            else:
                append_log("Audio selection: none (video-only)", update_stage=False)

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

            # HDR10+ metadata from the same file we will encode (segment or full)
            meta_inject: List[str] = []
            if want_hdr10plus:
                json_path = encode_temp_dir / "hdr10plus.json"
                set_stage_progress(56.0, stage="Extracting HDR10+ metadata")

                def _hdr10p_pct(p: float):
                    mapped = 56.0 + (max(0.0, min(100.0, p)) * 0.20)
                    set_stage_progress(mapped, stage=f"Extracting HDR10+ ({p:.0f}%)")

                extracted = self.pipeline.hdr_processor.extract_hdr10plus_json(
                    encode_input,
                    json_path,
                    progress_cb=lambda m: append_log(m, update_stage=False),
                    percent_cb=_hdr10p_pct,
                    duration_sec=duration_sec if not cfg.get("test_mode") else None,
                    cancel_event=self._cancel_event,
                )
                if self._cancel_event.is_set():
                    self.pipeline.kill_all_processes()
                    raise RuntimeError("Job cancelled.")
                json_problem = (
                    "HDR10+ extract produced nothing"
                    if not extracted
                    else self.pipeline.hdr_processor.verify_hdr10plus_json(extracted)
                )
                if json_problem is None:
                    staged = stage_svt_meta_file(extracted, job_id, "hdr10plus.json")
                    hdr10plus_json_path = str(staged)
                    staged_meta_paths.append(hdr10plus_json_path)
                    meta_inject.append("--hdr10plus-json")
                    append_log(f"HDR10+ JSON staged for SVT: {staged}", update_stage=False)
                elif hdr_analysis.get("is_hdr10plus") and hdr_strict:
                    raise RuntimeError(
                        f"HDR10+ metadata unusable ({json_problem}) — refusing to encode "
                        f"an HDR10+ source as plain HDR10. " + HDR_STRICT_HINT
                    )
                else:
                    append_log(
                        f"HDR10+ metadata unavailable ({json_problem}) — "
                        "encoding with static HDR10 color flags only",
                        update_stage=False,
                    )

            # DoVi RPU from the same file we will encode (segment or full). Best-effort:
            # any failure just logs and falls back to plain HDR10, never raises.
            if want_dovi_rpu:
                rpu_path_tmp = encode_temp_dir / "dovi_rpu.bin"
                set_stage_progress(76.0, stage="Extracting Dolby Vision RPU")

                def _dovi_pct(p: float):
                    mapped = 76.0 + (max(0.0, min(100.0, p)) * 0.10)
                    set_stage_progress(mapped, stage=f"Extracting DoVi RPU ({p:.0f}%)")

                extracted_rpu = self.pipeline.hdr_processor.extract_dovi_rpu(
                    encode_input,
                    rpu_path_tmp,
                    progress_cb=lambda m: append_log(m, update_stage=False),
                    percent_cb=_dovi_pct,
                    duration_sec=duration_sec if not cfg.get("test_mode") else None,
                    cancel_event=self._cancel_event,
                )
                if self._cancel_event.is_set():
                    self.pipeline.kill_all_processes()
                    raise RuntimeError("Job cancelled.")
                rpu_problem = (
                    "DoVi RPU extract produced nothing"
                    if not extracted_rpu
                    else self.pipeline.hdr_processor.verify_dovi_rpu(extracted_rpu)
                )
                if rpu_problem is None:
                    staged_rpu = stage_svt_meta_file(extracted_rpu, job_id, "dovi_rpu.bin")
                    dovi_rpu_path = str(staged_rpu)
                    staged_meta_paths.append(dovi_rpu_path)
                    meta_inject.append("--dolby-vision-rpu")
                    append_log(f"DoVi RPU staged for SVT: {staged_rpu}", update_stage=False)
                else:
                    append_log(
                        f"DoVi RPU unavailable ({rpu_problem}) — "
                        "encoding with HDR10 only (RPU passthrough skipped)",
                        update_stage=False,
                    )

            if self._cancel_event.is_set():
                self.pipeline.kill_all_processes()
                raise RuntimeError("Job cancelled.")

            if meta_inject:
                append_log(f"SVT metadata inject: {' '.join(meta_inject)}", update_stage=False)

            def audio_cb(msg):
                append_log(msg, update_stage=True)

            audio_fmt = normalize_audio_format(cfg.get("audio_format"))
            audio_label = "E-AC-3" if audio_fmt == "eac3" else "Opus"

            # Audio after HDR10+ extract — avoid concurrent ffmpeg + VS on the same MKV
            append_log(
                f"Transcoding {len(ordered_audio)} audio track(s) to {audio_label}...",
                update_stage=True
            )
            job["status"] = JobStatus.EXTRACTING
            job["stage"] = f"Transcoding Audio to {audio_label}"
            job["stage_num"] = 1
            job["progress"] = map_stage_progress(JobStatus.EXTRACTING, 20)
            self.save_queue()
            self.emit_event("job_update", job)

            processed_audio = self.pipeline.transcode_audio_tracks(
                encode_input,
                ordered_audio,
                audio_temp_dir,
                audio_cb,
                self._cancel_event
            )
            if self._cancel_event.is_set():
                self.pipeline.kill_all_processes()
                raise RuntimeError("Job cancelled.")
            if ordered_audio and len(processed_audio) != len(ordered_audio):
                raise RuntimeError(
                    f"Audio track count mismatch: got {len(processed_audio)}, "
                    f"expected {len(ordered_audio)}"
                )
            append_log(f"Audio transcode finished ({len(processed_audio)} track(s)).", update_stage=False)

            # 3. SVT-AV1-Tritium video encoding (exclusive access to source after audio)
            def encode_cb(data):
                if self._cancel_event.is_set():
                    self.pipeline.kill_all_processes()
                    return  # don't raise into stdout reader (mis-labels cancel)

                raw = data.get("raw_log", "")
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

            # Autocrop (least-crop-wins — see detect_black_bar_crop)
            crop = {"left": 0, "top": 0, "right": 0, "bottom": 0}
            if cfg.get("autocrop", True):
                try:
                    crop_duration = None
                    src_w = src_h = None
                    if cfg.get("test_mode") and isinstance(seg_media, dict):
                        crop_duration = float(seg_media.get("duration") or 0) or None
                        v = (seg_media.get("video") or {})
                        src_w = int(v.get("width") or 0) or None
                        src_h = int(v.get("height") or 0) or None
                    else:
                        crop_duration = (job.get("media_info") or {}).get("duration")
                        v = ((job.get("media_info") or {}).get("video") or {})
                        src_w = int(v.get("width") or 0) or None
                        src_h = int(v.get("height") or 0) or None
                    crop = self.pipeline.detect_black_bar_crop(
                        encode_input,
                        duration_sec=crop_duration,
                        src_w=src_w,
                        src_h=src_h,
                        progress_cb=lambda msg: append_log(msg, update_stage=True),
                        cancel_event=self._cancel_event,
                    )
                except Exception as crop_err:
                    append_log(f"Autocrop skipped: {crop_err}", update_stage=False)
                    crop = {"left": 0, "top": 0, "right": 0, "bottom": 0}
                if self._cancel_event.is_set():
                    self.pipeline.kill_all_processes()
                    raise RuntimeError("Job cancelled.")

            # A DoVi RPU carries frame-geometry-dependent metadata (active area /
            # L5-L8 trims) extracted from the *uncropped* source above — autocrop
            # runs after that and can shrink the actual encoded frame, so an
            # RPU injected alongside a real crop would describe geometry that no
            # longer matches the picture. Correct it via dovi_tool's documented
            # editor fix for exactly this ({"active_area": {"crop": true}} —
            # "should be set to true when final video has no letterbox bars")
            # rather than dropping RPU passthrough outright. Still best-effort:
            # if the edit itself fails, fall back to HDR10-only same as any
            # other RPU failure. See README.md "HDR & Dolby Vision".
            if dovi_rpu_path and any(int(crop.get(k, 0) or 0) for k in ("left", "top", "right", "bottom")):
                cropped_rpu_tmp = encode_temp_dir / "dovi_rpu_cropped.bin"
                corrected = self.pipeline.hdr_processor.apply_dovi_crop_edit(
                    Path(dovi_rpu_path),
                    cropped_rpu_tmp,
                    progress_cb=lambda m: append_log(m, update_stage=False),
                )
                if corrected:
                    staged_cropped = stage_svt_meta_file(corrected, job_id, "dovi_rpu_cropped.bin")
                    dovi_rpu_path = str(staged_cropped)
                    staged_meta_paths.append(dovi_rpu_path)
                    append_log(
                        "Autocrop is active — corrected DoVi RPU active area to match "
                        "the cropped frame (dovi_tool editor).",
                        update_stage=False,
                    )
                else:
                    append_log(
                        "Autocrop is active and DoVi RPU active-area correction failed — "
                        "dropping RPU passthrough for this job (RPU geometry would no "
                        "longer match the cropped frame); encoding with HDR10 only.",
                        update_stage=False,
                    )
                    dovi_rpu_path = None
                    if "--dolby-vision-rpu" in meta_inject:
                        meta_inject.remove("--dolby-vision-rpu")

            self._wait_if_paused()
            if self._cancel_event.is_set():
                raise RuntimeError("Job cancelled.")

            # Only now — autocrop is done, so nothing left to overwrite this with
            job["status"] = JobStatus.FINAL_ENCODE
            job["stage"] = "Encoding (SVT-AV1-Tritium)"
            job["stage_num"] = 2
            job["stage_percent"] = 0.0
            job["progress"] = map_stage_progress(JobStatus.FINAL_ENCODE)
            self.save_queue()
            self.emit_event("job_update", job)

            try:
                encode_resolution = cfg.get("resolution_target", JOB_CONFIG_DEFAULTS["resolution_target"])

                encoded_ivf = self.pipeline.run_svt_encode(
                    input_file=encode_input,
                    job_temp_dir=encode_temp_dir,
                    crf=cfg.get("crf", JOB_CONFIG_DEFAULTS["crf"]),
                    preset=cfg.get("preset", JOB_CONFIG_DEFAULTS["preset"]),
                    resolution_target=encode_resolution,
                    extra_svt_params=extra_svt_params,
                    hdr10plus_json=hdr10plus_json_path,
                    dolby_vision_rpu=dovi_rpu_path,
                    crop=crop,
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
                self.pipeline.kill_all_processes()
                raise RuntimeError("Job cancelled.")

            self._wait_if_paused()
            if self._cancel_event.is_set():
                raise RuntimeError("Job cancelled.")

            # 4. Final container mux (MP4 or WebM)
            container = str(cfg.get("container", JOB_CONFIG_DEFAULTS["container"])).lower()
            if container != "webm":
                container = "mp4"
            job["status"] = JobStatus.REMUXING
            if container == "webm":
                job["stage"] = "Muxing WebM"
            else:
                job["stage"] = "Muxing Web-Optimized MP4 (+faststart)"
            job["stage_num"] = 3
            job["stage_percent"] = 0.0
            job["progress"] = map_stage_progress(JobStatus.REMUXING)
            self.save_queue()
            self.emit_event("job_update", job)

            def mux_cb(msg):
                append_log(msg, update_stage=False)

            # Rebuild mux color args fresh (stored ffmpeg_color may be from an older BSF syntax)
            color_args = None
            if (
                hdr_analysis.get("is_hdr")
                or hdr_analysis.get("is_dovi")
                or hdr_analysis.get("is_hdr10plus")
                or hdr_analysis.get("dovi_filename_hint")
            ):
                trc = str(hdr_analysis.get("color_transfer") or "smpte2084").lower()
                transfer_code = "18" if ("arib" in trc or "hlg" in trc) else "16"
                ff_range = str(hdr_analysis.get("color_range") or "tv")
                color_args = self.pipeline.hdr_processor._ffmpeg_color_args(
                    transfer_code, color_range=ff_range
                )

            # Output naming is final at queue-add (hdr_label already mirrors encode policy).

            # Last-chance collision guard (file may have appeared since job was queued)
            with self._lock:
                reserved = self._reserved_output_paths(exclude_job_id=job_id)
            safe_out = allocate_unique_output_path(out_path, reserved=reserved)
            if safe_out.resolve() != Path(out_path).resolve():
                append_log(
                    f"Output path already occupied — writing to {safe_out.name} instead",
                    update_stage=False,
                )
                out_path = safe_out
                job["output_path"] = str(safe_out)
                self.save_queue()
                self.emit_event("job_update", job)

            final_out = self.pipeline.mux_output(
                encoded_ivf,
                processed_audio,
                out_path,
                container=container,
                progress_cb=mux_cb,
                color_args=color_args,
                cancel_event=self._cancel_event,
            )

            # 5. Optional post-encode SSIMU2 score (Pipeline Status → SSIMU2 post-encode score)
            # Final file is already published — cancel after this point still counts as COMPLETED.
            ssimu2_stats: Dict[str, Any] = {}
            if cfg.get("ssimu2_post", False) and not self._cancel_event.is_set():
                job["stage"] = "Measuring SSIMU2"
                job["stage_num"] = 3
                self.save_queue()
                self.emit_event("job_update", job)
                try:
                    # Full encodes: sample every 3rd frame; short test clips: every frame
                    metric_skip = 1 if cfg.get("test_mode") else 3
                    ssimu2_stats = self.pipeline.measure_final_ssimu2(
                        source_file=encode_input,
                        encoded_file=encoded_ivf,
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
            elif self._cancel_event.is_set():
                append_log(
                    "Cancelled after mux — keeping published output (skipping SSIMU2)",
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
            if watch_source_dir:
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

            # Save to separate history file
            self._archive_job_to_history(job, job_id)

            # Subtitle extract / OpenSubtitles fill — background so the next
            # encode can start immediately. Skip in Test Mode (segment encodes
            # shouldn't leave full-movie sidecars).
            if not cfg.get("test_mode"):
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

                        def _cb(msg: str):
                            logs.append(msg)
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
                                    release_hint=src_for_subs.name,
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
                            self.emit_event("subtitle_extract", {
                                "id": jid,
                                "filename": fname,
                                "status": "error",
                                "extracted": 0,
                                "files": [],
                                "message": str(sub_err),
                                "logs": logs,
                            })

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
            stop_elapsed.set()
            job["elapsed_seconds"] = int(time.time() - start_time)
            self.current_job_id = None
            # Always clean job temp + staged HDR10+ JSON (success path also calls cleanup — idempotent)
            try:
                self.pipeline.cleanup_temp_files(job_temp_dir)
            except Exception as ce:
                print(f"[!] Temp cleanup: {ce}")
            try:
                cleanup_staged_meta(job_id, staged_meta_paths)
            except Exception:
                pass
            # Remove legacy IVF left beside the source from older builds
            try:
                stray = Path(job.get("input_path") or "")
                if stray.is_file():
                    leftover = stray.parent / f"{stray.stem}.ivf"
                    if leftover.is_file():
                        leftover.unlink(missing_ok=True)
            except Exception:
                pass
            self.save_queue()
            # Only push live updates for jobs still in the pending queue (failed/cancelled).
            # Completed jobs were already moved to history + job_removed.
            with self._lock:
                still_pending = any(j["id"] == job_id for j in self.jobs)
            if still_pending:
                self.emit_event("job_update", job)

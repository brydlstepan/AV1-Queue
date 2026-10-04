"""
Watch-folder auto-queue.

Watches a directory for new video files and queues them automatically via
QueueManager.add_job(), in place (no copy on add — matches manual add/drag-drop
behavior). A filename containing "preset-<id>" resolves that preset from
server/presets/ (builtin/ + local/); otherwise a configured default preset is used, and if
that isn't found either, add_job's own built-in defaults apply.

Dedup is by resolved input_path against QueueManager.jobs — the same list a
manually-added job lands in — so a file already queued manually is never
double-added, and vice versa. This is the only interaction point with manual
add; the manual add/browse/drag-drop path itself is unchanged.

The finished AV1 output is written to <watch_dir>/encoded/ (never back into the
watched folder itself, or the watcher would re-detect its own output as a new
source and re-encode it forever). Once that output is verified,
QueueManager._execute_single_job moves the original source into
<watch_dir>/encoded_sources/; a source whose job fails goes to failed/, one
skipped by the Dolby Vision policy to skipped/ (so the startup sweep doesn't
re-queue it). Test Mode encodes leave the source where it is.
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".webm", ".ts", ".m2ts"}
ENCODED_DIRNAME = "encoded"

_PRESET_TAG_RE = re.compile(r"preset-([a-z0-9_-]+)", re.IGNORECASE)


def resolve_preset(
    filename: str, presets: List[Dict[str, Any]], default_preset_id: str
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Match "preset-<id>" in the filename against presets by id (case-insensitive),
    falling back to default_preset_id, then to no preset at all (add_job's own
    built-in defaults apply). Returns (preset, how): how is "tag", "default"
    or "none" — logged on the job."""
    by_id = {str(p.get("id", "")).lower(): p for p in presets}

    m = _PRESET_TAG_RE.search(Path(filename).stem)
    if m:
        match = by_id.get(m.group(1).lower())
        if match:
            return match, "tag"

    if default_preset_id:
        match = by_id.get(str(default_preset_id).lower())
        if match:
            return match, "default"

    return None, "none"


def build_config(preset: Optional[Dict[str, Any]], watch_dir: Path, preset_via: str = "none") -> Dict[str, Any]:
    """Config overlay for a watch-folder job — only preset-derived fields plus the
    watch_source_dir marker (and how the preset was chosen, for the job log);
    everything else falls back to add_job's own defaults."""
    p = preset or {}
    config: Dict[str, Any] = {"watch_source_dir": str(watch_dir), "watch_preset_via": preset_via}
    if preset:
        config["preset_id"] = p.get("id")
        config["preset_name"] = p.get("name")
        config["crf"] = p.get("crf", 30.0)
        config["preset"] = p.get("preset", 4)
        config["resolution_target"] = p.get("resolution_target", "source")
        config["audio_bitrate_51"] = p.get("audio_bitrate_51", "320k")
        config["audio_bitrate_stereo"] = p.get("audio_bitrate_stereo", "160k")
        if p.get("audio_languages"):
            config["audio_languages"] = p.get("audio_languages")
        config["audio_best_only"] = p.get("audio_best_only", True)
        config["svt_params"] = dict(p.get("svt_params") or {})
    return config


class _StableFileWaiter:
    """A file landing via a same-volume move is instantly stable; a cross-volume
    copy is not. Poll size until unchanged across consecutive checks before
    treating the file as finished."""

    # Log once when a file has been unopenable this long, so a file that can
    # never be opened exclusively (permissions, a stuck writer) isn't silent.
    STUCK_LOG_SECONDS = 120.0

    def __init__(self, poll_seconds: float = 1.0, stable_checks: int = 3, log=print):
        self.poll_seconds = poll_seconds
        self.stable_checks = stable_checks
        self.log = log

    @staticmethod
    def _exclusive_open_error(path: Path) -> int:
        """0 if the file can be opened exclusively right now, else the Win32 error.

        Size alone is not enough: a copy that stalls for a couple of seconds
        (SMB reconnect, AV scan, slow source disk) looks identical to a finished
        one, and the truncated file then gets encoded out from under the writer.

        Python's own open() requests a sharing mode that succeeds even while a
        copy is in progress, so it cannot answer this. CreateFileW with
        dwShareMode=0 asks for exclusive access and fails (sharing violation)
        exactly while someone else holds a handle. Any failure counts as "not
        ready" — guessing "free" on an unexpected error would let a file still
        being written slip through. Non-Windows falls back to the size check alone.
        """
        if os.name != "nt":
            return 0
        try:
            kernel32 = _kernel32()
            handle = kernel32.CreateFileW(
                str(path),
                0x80000000,  # GENERIC_READ
                0,           # dwShareMode = 0 -> exclusive
                None,
                3,           # OPEN_EXISTING
                0,
                None,
            )
            if handle is None or handle == _INVALID_HANDLE:
                # use_last_error=True: ctypes saved the code right after the call
                return ctypes.get_last_error() or -1
            kernel32.CloseHandle(handle)
            return 0
        except Exception:
            return -1

    def wait_until_stable(self, path: Path, cancel_event: threading.Event) -> bool:
        last_size = -1
        stable_count = 0
        blocked_since: Optional[float] = None
        logged_stuck = False
        while not cancel_event.is_set():
            try:
                size = path.stat().st_size
            except OSError:
                return False
            open_err = self._exclusive_open_error(path) if size == last_size else 0
            if size == last_size and not open_err:
                blocked_since = None
                stable_count += 1
                if stable_count >= self.stable_checks:
                    return True
            else:
                stable_count = 0
                last_size = size
                if open_err:
                    blocked_since = blocked_since or time.monotonic()
                    if not logged_stuck and time.monotonic() - blocked_since >= self.STUCK_LOG_SECONDS:
                        logged_stuck = True
                        self.log(
                            f"[watch] Still waiting for {path.name}: size unchanged but the "
                            f"file can't be opened exclusively (Windows error {open_err}) — "
                            "another program may still be writing it"
                        )
                else:
                    blocked_since = None  # size still changing: being written, not stuck
            time.sleep(self.poll_seconds)
        return False


_INVALID_HANDLE = ctypes.c_void_p(-1).value
_kernel32_dll = None


def _kernel32():
    """kernel32 loaded with use_last_error=True, so ctypes.get_last_error() is
    the code from our own call — not one clobbered by ctypes / the interpreter
    in between (which the global windll.kernel32 + GetLastError() can't promise)."""
    global _kernel32_dll
    if _kernel32_dll is None:
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k.CreateFileW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        _kernel32_dll = k
    return _kernel32_dll


class WatchFolderService:
    """Watches `watch_dir` for new video files and queues them via `queue_mgr`."""

    def __init__(
        self,
        watch_dir: Path,
        queue_mgr,
        load_presets_fn,
        default_preset_id: str = "",
        output_dir: Optional[str] = None,
        log=print,
    ):
        self.watch_dir = Path(watch_dir)
        self.queue_mgr = queue_mgr
        self.load_presets_fn = load_presets_fn
        self.default_preset_id = default_preset_id
        self.output_dir = Path(output_dir).resolve() if output_dir else None
        self.log = log
        self._observer: Optional[Observer] = None
        self._pending: Dict[str, threading.Thread] = {}
        self._pending_lock = threading.Lock()
        self._cancel_event = threading.Event()

    def start(self) -> None:
        if not self.watch_dir.is_dir():
            self.log(f"[watch] Folder does not exist, not starting: {self.watch_dir}")
            return

        handler = _Handler(self)
        observer = Observer()
        observer.schedule(handler, str(self.watch_dir), recursive=False)
        observer.start()
        self._observer = observer
        self.log(f"[watch] Watching {self.watch_dir} for new files")

        # Files that arrived while the app was closed (or during a settings
        # restart) fire no on_created event, so without this sweep they would
        # sit in the folder forever and never be queued.
        for p in sorted(self.watch_dir.glob("*")):
            if p.is_file():
                self.handle_candidate(p)

    def stop(self) -> None:
        self._cancel_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def handle_candidate(self, path: Path) -> None:
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            return
        key = str(path.resolve())
        # Sources the queue moved into encoded_sources/ / failed/ / skipped/ can
        # surface as move events — only files directly in the folder count
        try:
            if Path(key).parent != self.watch_dir.resolve():
                return
        except OSError:
            return
        with self._pending_lock:
            if key in self._pending:
                return
            t = threading.Thread(target=self._process, args=(path,), name=f"watch-{path.name}", daemon=True)
            self._pending[key] = t
            t.start()

    def _process(self, path: Path) -> None:
        try:
            waiter = _StableFileWaiter(log=self.log)
            if not waiter.wait_until_stable(path, self._cancel_event):
                return
            if not path.is_file():
                return

            resolved = str(path.resolve()).lower()
            with self.queue_mgr._lock:
                already_queued = any(
                    str(Path(j["input_path"]).resolve()).lower() == resolved
                    for j in self.queue_mgr.jobs
                )
            if already_queued:
                return

            presets = self.load_presets_fn()
            preset, preset_via = resolve_preset(path.name, presets, self.default_preset_id)
            config = build_config(preset, self.watch_dir, preset_via)

            # The output must never land back inside the watched folder itself —
            # otherwise the watcher immediately re-detects its own output as a new
            # source and re-encodes it forever. Route it into encoded/ (non-recursive
            # watch, so it's never re-scanned) — separate from encoded_sources/,
            # where the verified original source is moved to on completion.
            ext = "webm" if str(config.get("container", "mp4")).lower() == "webm" else "mp4"
            out_dir = self.output_dir if self.output_dir else (self.watch_dir / ENCODED_DIRNAME)
            if self.output_dir:
                try:
                    watch_resolved = self.watch_dir.resolve()
                    out_resolved = out_dir.resolve()
                    if watch_resolved == out_resolved or watch_resolved in out_resolved.parents:
                        self.log(
                            f"[watch] Output folder must not be inside the watched folder — "
                            f"using {ENCODED_DIRNAME}/ instead ({out_dir})"
                        )
                        out_dir = self.watch_dir / ENCODED_DIRNAME
                except OSError:
                    pass
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / f"{path.stem}_av1_boost.{ext}"

            job = self.queue_mgr.add_job(str(path), output_path=str(output_path), config=config)
            self.log(f"[watch] Auto-queued {path.name} (job {job['id'][:8]}, preset={config.get('preset_id') or 'default'})")
        except Exception as e:
            self.log(f"[watch] Failed to auto-queue {path.name}: {e}")
        finally:
            key = str(path.resolve())
            with self._pending_lock:
                self._pending.pop(key, None)


class _Handler(FileSystemEventHandler):
    def __init__(self, service: WatchFolderService):
        self.service = service

    def on_created(self, event):
        if not event.is_directory:
            self.service.handle_candidate(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self.service.handle_candidate(Path(event.dest_path))

"""
AV1 Queue Studio - FastAPI & WebSocket Server
Provides local REST API and real-time WebSocket progress stream for the Queue GUI.
"""

import sys
import os
import re
import json
import time
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional
import psutil

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "server" / "static"

# Import QueueManager
sys.path.insert(0, str(BASE_DIR))
from core.queue_manager import QueueManager
from core.app_settings import load_settings, save_settings
from core.presets_store import (
    delete_preset_file,
    load_presets,
    save_preset_file,
)
from core.svt_binary import ensure_svt_binary, get_svt_status
from core.watch_folder import WatchFolderService

app = FastAPI(title="AV1 Queue")
queue_mgr = QueueManager()

# Connected WebSocket clients
active_websockets: List[WebSocket] = []
loop = None
_svt_startup: Dict[str, Any] = {}
_watch_service: Optional[WatchFolderService] = None


def broadcast_event(event_type: str, data: Dict[str, Any]):
    """Thread-safe WebSocket broadcaster."""
    global loop
    if not loop or not active_websockets:
        return
    message = json.dumps({"event": event_type, "data": data})
    for ws in list(active_websockets):
        try:
            asyncio.run_coroutine_threadsafe(ws.send_text(message), loop)
        except Exception:
            pass


queue_mgr.add_listener(broadcast_event)


@app.on_event("startup")
async def startup_event():
    global loop, _svt_startup
    loop = asyncio.get_running_loop()
    try:
        from core.win_process import boost_current_process
        if boost_current_process():
            print("[qos] Server process opted out of Windows EcoQoS / power throttling")
    except Exception as e:
        print(f"[qos] Could not adjust process QoS: {e}")
    try:
        _svt_startup = ensure_svt_binary(BASE_DIR / "bin")
    except Exception as e:
        print(f"[svt] Startup binary select failed: {e}")
        _svt_startup = {"ok": False, "message": str(e)}
    _restart_watch_service()


@app.on_event("shutdown")
async def shutdown_event():
    global _watch_service
    if _watch_service is not None:
        _watch_service.stop()
        _watch_service = None


def _restart_watch_service():
    """(Re)start the watch-folder service from current settings. Safe to call
    repeatedly — stops any previous observer first."""
    global _watch_service
    if _watch_service is not None:
        _watch_service.stop()
        _watch_service = None
    settings = load_settings()
    if not settings.get("watch_folder_enabled"):
        return
    watch_path = settings.get("watch_folder_path")
    if not watch_path:
        return
    try:
        service = WatchFolderService(
            Path(watch_path),
            queue_mgr,
            load_presets,
            default_preset_id=settings.get("watch_folder_default_preset", ""),
        )
        service.start()
        _watch_service = service
    except Exception as e:
        print(f"[watch] Failed to start watch folder service: {e}")


# Request Models
class AddJobRequest(BaseModel):
    input_path: str
    output_path: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class BatchAddRequest(BaseModel):
    paths: List[str]
    config: Optional[Dict[str, Any]] = None


class ActionRequest(BaseModel):
    action: str  # start, pause, resume, stop
    job_id: Optional[str] = None
    # Optional Test Mode fields sent with "start" (and apply-test-mode)
    test_mode: Optional[bool] = None
    trim_start: Optional[str] = None
    trim_end: Optional[str] = None


class PresetModel(BaseModel):
    id: str
    name: str
    description: Optional[str] = ""
    crf: float
    preset: int
    resolution_target: Optional[str] = "source"
    audio_bitrate_51: str
    audio_bitrate_stereo: str
    # Preferred audio languages in priority order (ISO-ish codes: eng, ces, …)
    audio_languages: Optional[List[str]] = None
    # If true, auto-select only the best track when a language has multiples
    audio_best_only: Optional[bool] = True
    # Non-default SVT-AV1-Tritium overrides → --svt-params
    svt_params: Optional[Dict[str, Any]] = None


class UpdateJobRequest(BaseModel):
    job_id: str
    config: Optional[Dict[str, Any]] = None
    media_tag: Optional[Dict[str, Any]] = None


class SettingsUpdateRequest(BaseModel):
    autoname_output: Optional[bool] = None
    name_template_movie: Optional[str] = None
    name_template_episode: Optional[str] = None
    svt_lp: Optional[int] = None
    svt_low_memory: Optional[bool] = None
    ssimu2_target: Optional[float] = None
    hdr_strict: Optional[bool] = None
    tmdb_lookup: Optional[bool] = None
    tmdb_api_key: Optional[str] = None
    subtitle_search: Optional[bool] = None
    opensubtitles_api_key: Optional[str] = None
    opensubtitles_username: Optional[str] = None
    opensubtitles_password: Optional[str] = None
    watch_folder_enabled: Optional[bool] = None
    watch_folder_path: Optional[str] = None
    watch_folder_default_preset: Optional[str] = None
    hour_format: Optional[str] = None
    allow_builtin_preset_edits: Optional[bool] = None


@app.get("/api/presets")
async def get_presets():
    return load_presets()


@app.post("/api/presets/save")
async def save_preset(preset: PresetModel):
    presets = load_presets()
    incoming = preset.dict() if hasattr(preset, "dict") else preset.model_dump()
    # Fields that affect queued job snapshots (must match frontend PRESET_SYNC_KEYS)
    sync_keys = (
        "name", "crf", "preset", "resolution_target",
        "audio_bitrate_51", "audio_bitrate_stereo", "svt_params",
    )
    # Only overwrite fields the client actually submitted — the editor's
    # payload doesn't always include every field, and blindly overwriting
    # with model defaults would silently wipe values already stored on the
    # preset for any field it omits.
    fields_set = preset.__fields_set__ if hasattr(preset, "__fields_set__") else getattr(preset, "model_fields_set", set())
    allow_builtin = bool(load_settings().get("allow_builtin_preset_edits"))

    existing = next((p for p in presets if p.get("id") == preset.id), None)
    is_new = existing is None
    was_builtin = bool(existing and existing.get("builtin"))
    preset_changed = False

    if existing:
        old = dict(existing)
        merged = dict(existing)
        for key in fields_set:
            merged[key] = incoming.get(key)
        if merged.get("svt_params") is None:
            merged["svt_params"] = existing.get("svt_params") or {}
        for key in sync_keys:
            if key == "svt_params":
                if (old.get("svt_params") or {}) != (merged.get("svt_params") or {}):
                    preset_changed = True
                    break
            elif old.get(key) != merged.get(key):
                preset_changed = True
                break
        to_save = merged
    else:
        if incoming.get("svt_params") is None:
            incoming["svt_params"] = {}
        to_save = incoming

    try:
        save_preset_file(
            to_save,
            is_new=is_new,
            was_builtin=was_builtin,
            allow_builtin_edits=allow_builtin,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if preset_changed:
        queue_mgr.mark_preset_stale(preset.id)
    return {"status": "ok", "presets": load_presets()}


@app.post("/api/presets/delete")
async def delete_preset(req: ActionRequest):
    if not req.job_id:
        raise HTTPException(status_code=400, detail="Missing preset id")
    allow_builtin = bool(load_settings().get("allow_builtin_preset_edits"))
    try:
        delete_preset_file(req.job_id, allow_builtin_edits=allow_builtin)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"status": "ok", "presets": load_presets()}


HISTORY_DIR = BASE_DIR / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _history_path(job_id: Optional[str]) -> Path:
    """Resolve a history record path, rejecting anything that escapes HISTORY_DIR."""
    if not job_id or not _JOB_ID_RE.match(str(job_id)):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    target = (HISTORY_DIR / f"{job_id}.json").resolve()
    if target.parent != HISTORY_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid job_id")
    return target


class OpenPathRequest(BaseModel):
    path: str


@app.get("/api/history")
async def get_history():
    items = []
    for f in HISTORY_DIR.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as hf:
                items.append(json.load(hf))
        except Exception:
            pass
    items.sort(key=lambda x: x.get("completed_at", 0), reverse=True)
    return items


@app.post("/api/history/delete")
async def delete_history_item(req: ActionRequest):
    target = _history_path(req.job_id)
    if target.exists():
        target.unlink()
    return {"status": "ok"}


@app.post("/api/history/requeue")
async def requeue_history_item(req: ActionRequest):
    target = _history_path(req.job_id)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="History record not found")
    try:
        with open(target, "r", encoding="utf-8") as f:
            item = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read history record: {e}")
    input_path = item.get("input_path")
    if not input_path or not Path(input_path).is_file():
        raise HTTPException(status_code=400, detail="Original source file no longer exists")
    try:
        job = queue_mgr.add_job(input_path, config=item.get("config"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        target.unlink()
    except Exception:
        pass
    return {"status": "ok", "job": job}


@app.post("/api/history/clear")
async def clear_history():
    for f in HISTORY_DIR.glob("*.json"):
        try:
            f.unlink()
        except Exception:
            pass
    return {"status": "ok"}


def _resolved_user_path(raw: Optional[str]) -> Path:
    """Resolve a user-supplied path; reject empty / null-byte paths."""
    if raw is None or not str(raw).strip() or "\x00" in str(raw):
        raise HTTPException(status_code=400, detail="Invalid path")
    return Path(str(raw).strip()).expanduser().resolve()


@app.post("/api/open_folder")
async def open_in_explorer(req: OpenPathRequest):
    p = _resolved_user_path(req.path)
    if not p.exists():
        p = p.parent
    if p.exists():
        # Explorer requires "/select,<path>" as a single token (no space
        # after the comma) — splitting it into two argv items stops Explorer
        # from highlighting the target file.
        if p.is_file():
            subprocess.Popen(["explorer.exe", f"/select,{p}"])
        else:
            subprocess.Popen(["explorer.exe", str(p)])
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Path does not exist")


@app.get("/")
async def get_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/queue")
async def get_queue():
    return {
        "is_running": queue_mgr.is_running,
        "is_paused": queue_mgr.is_paused,
        "current_job_id": queue_mgr.current_job_id,
        "jobs": queue_mgr.jobs
    }


@app.post("/api/queue/add")
async def add_job(req: AddJobRequest):
    try:
        job = queue_mgr.add_job(req.input_path, req.output_path, req.config)
        return {"status": "ok", "job": job}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/queue/batch_add")
async def batch_add_jobs(req: BatchAddRequest):
    added = []
    errors = []
    for p in req.paths:
        try:
            job = queue_mgr.add_job(p, config=req.config)
            added.append(job)
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    return {"status": "ok", "added": len(added), "errors": errors, "jobs": added}


@app.post("/api/queue/remove")
async def remove_job(req: ActionRequest):
    if not req.job_id:
        raise HTTPException(status_code=400, detail="Missing job_id")
    success = queue_mgr.remove_job(req.job_id)
    return {"status": "ok", "success": success}


@app.post("/api/queue/update")
async def update_job(req: UpdateJobRequest):
    if not req.job_id:
        raise HTTPException(status_code=400, detail="Missing job_id")
    try:
        job = queue_mgr.update_job(
            req.job_id,
            config_updates=req.config or {},
            media_tag_updates=req.media_tag,
        )
        return {"status": "ok", "job": job}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/queue/requeue")
async def requeue_job(req: ActionRequest):
    if not req.job_id:
        raise HTTPException(status_code=400, detail="Missing job_id")
    try:
        job = queue_mgr.requeue_job(req.job_id)
        return {"status": "ok", "job": job}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/queue/action")
async def queue_action(req: ActionRequest):
    act = req.action.lower()
    if act == "start":
        tm = None
        if req.test_mode is not None:
            tm = {"test_mode": bool(req.test_mode)}
            if tm["test_mode"]:
                tm["trim_start"] = req.trim_start or "01:01:10"
                tm["trim_end"] = req.trim_end or "01:01:30"
        queue_mgr.start_queue(test_mode_config=tm)
    elif act == "pause":
        queue_mgr.pause_queue()
    elif act == "resume":
        queue_mgr.resume_queue()
    elif act == "stop":
        queue_mgr.stop_queue()
    elif act == "apply-test-mode":
        if req.test_mode is None:
            raise HTTPException(status_code=400, detail="test_mode required")
        tm = {"test_mode": bool(req.test_mode)}
        if tm["test_mode"]:
            tm["trim_start"] = req.trim_start or "01:01:10"
            tm["trim_end"] = req.trim_end or "01:01:30"
        updated = queue_mgr.apply_test_mode_to_queued(tm)
        return {
            "status": "ok",
            "updated": len(updated),
            "is_running": queue_mgr.is_running,
            "is_paused": queue_mgr.is_paused,
        }
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action {act}")
    return {"status": "ok", "is_running": queue_mgr.is_running, "is_paused": queue_mgr.is_paused}


@app.get("/api/probe")
async def probe_file(
    path: str = Query(...),
    audio_format: str = Query("opus"),
):
    p = _resolved_user_path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="File not found")
    raw = queue_mgr.pipeline.hdr_processor.probe_video_streams(p)
    media = queue_mgr.pipeline.probe_media(p, probe=raw)
    hdr = queue_mgr.pipeline.hdr_processor.analyze_hdr_and_dovi(p, probe=raw)
    fmt = (audio_format or "opus").strip().lower()
    if fmt not in ("opus", "eac3"):
        fmt = "opus"
    prioritized_audio = queue_mgr.pipeline.select_and_prioritize_audio(
        media["audio_tracks"],
        audio_format=fmt,
    )
    return {
        "media": media,
        "hdr": hdr,
        "prioritized_audio": prioritized_audio
    }


_GPU_POLL_SECONDS = 6.0
_SVT_POLL_SECONDS = 30.0
_gpu_cache = {"at": 0.0, "value": None}
_svt_cache = {"at": 0.0, "value": None}


def _query_gpu():
    """nvidia-smi is a process spawn; the UI polls system stats every 2s, so this
    is throttled rather than run on every request."""
    now = time.monotonic()
    if _gpu_cache["value"] is not None and now - _gpu_cache["at"] < _GPU_POLL_SECONDS:
        return _gpu_cache["value"]
    gpu_info = None
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2
        )
        if res.returncode == 0:
            parts = [p.strip() for p in res.stdout.strip().split(",")]
            if len(parts) >= 5:
                gpu_info = {
                    "name": parts[0],
                    "utilization": float(parts[1]),
                    "temp": float(parts[2]),
                    "mem_used_mb": float(parts[3]),
                    "mem_total_mb": float(parts[4])
                }
    except Exception:
        pass
    _gpu_cache["at"] = now
    _gpu_cache["value"] = gpu_info
    return gpu_info


def _cached_svt_status():
    """get_svt_status() re-reads the marker, globs bin/svt and can shell out to
    wmic (5s timeout). It only changes when the binary is swapped."""
    now = time.monotonic()
    if _svt_cache["value"] is not None and now - _svt_cache["at"] < _SVT_POLL_SECONDS:
        return _svt_cache["value"]
    value = get_svt_status()
    _svt_cache["at"] = now
    _svt_cache["value"] = value
    return value


@app.get("/api/system")
async def get_system_stats():
    cpu_pct = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    gpu_info = _query_gpu()
    svt = _cached_svt_status()
    return {
        "cpu_percent": cpu_pct,
        "cpu_logical": psutil.cpu_count(logical=True) or 0,
        "cpu_physical": psutil.cpu_count(logical=False) or 0,
        "ram_percent": ram.percent,
        "ram_used_gb": round(ram.used / (1024**3), 1),
        "ram_total_gb": round(ram.total / (1024**3), 1),
        "gpu": gpu_info,
        "svt": svt,
        "svt_caps": svt.get("caps") or {},
    }


@app.get("/api/settings")
async def get_settings():
    data = load_settings()
    # Never echo password in clear if empty; still return field for form binding
    return {"status": "ok", "settings": data}


@app.post("/api/settings")
async def update_settings(req: SettingsUpdateRequest):
    raw = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    updates = {k: v for k, v in raw.items() if v is not None}
    if "hour_format" in updates:
        fmt = str(updates["hour_format"]).strip()
        if fmt not in ("12", "24"):
            raise HTTPException(status_code=400, detail="hour_format must be '12' or '24'")
        updates["hour_format"] = fmt
    try:
        data = save_settings(updates)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if any(k.startswith("watch_folder_") for k in updates):
        _restart_watch_service()
    return {"status": "ok", "settings": data}


def get_available_drives():
    import string
    from ctypes import windll
    drives = []
    try:
        bitmask = windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if bitmask & 1:
                drives.append(f"{letter}:\\")
            bitmask >>= 1
    except Exception:
        drives = ["C:\\", "D:\\", "E:\\"]
    return drives


@app.post("/api/dialog/pick_files")
async def pick_files_dialog():
    """Opens the native Windows File Explorer Open Dialog with multi-select using tkinter."""
    import tkinter as tk
    from tkinter import filedialog

    def _run_picker():
        selected = []
        # Create a hidden root window
        root = tk.Tk()
        root.attributes("-topmost", True)
        root.withdraw()
        root.lift()
        root.focus_force()

        filetypes = [
            ("Video Files", "*.mkv;*.mp4;*.ts;*.m2ts;*.mov;*.avi;*.webm"),
            ("All Files", "*.*"),
        ]
        paths = filedialog.askopenfilenames(
            parent=root,
            title="Select Video Files to Transcode",
            filetypes=filetypes,
        )
        if paths:
            selected.extend(list(paths))
        root.destroy()
        # Resolve inside the worker — Path.resolve on large/network files can be
        # slow and must not block the asyncio loop after the dialog closes.
        return [str(Path(p).resolve()) for p in selected]

    loop = asyncio.get_event_loop()
    paths = await loop.run_in_executor(None, _run_picker)

    if paths:
        return {"status": "ok", "paths": paths}

    return {"status": "cancelled", "paths": []}


@app.get("/api/browse")
async def browse_directory(path: Optional[str] = None):
    """File/Directory navigation helper with drive selection."""
    if not path or path == "" or path == "drives":
        drives = get_available_drives()
        return {
            "current_path": "This PC",
            "parent_path": None,
            "items": [{"name": d, "path": d, "is_dir": True, "size": 0} for d in drives]
        }

    target = _resolved_user_path(path)
    if not target.exists() or not target.is_dir():
        target = target.parent

    items = []
    try:
        for entry in target.iterdir():
            try:
                is_dir = entry.is_dir()
                is_video = entry.suffix.lower() in [".mkv", ".mp4", ".mov", ".avi", ".webm", ".ts", ".m2ts"]
                if is_dir or is_video:
                    items.append({
                        "name": entry.name,
                        "path": str(entry.resolve()),
                        "is_dir": is_dir,
                        "size": entry.stat().st_size if not is_dir else 0
                    })
            except Exception:
                pass
    except Exception as e:
        return {"current_path": str(target), "items": [], "error": str(e)}

    # Sort directories first, then alphabetical
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

    # If at root of a drive, parent goes back to drives list
    parent_p = str(target.parent) if target.parent != target else "drives"

    return {
        "current_path": str(target),
        "parent_path": parent_p,
        "items": items
    }


@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        # Send initial queue snapshot
        await websocket.send_text(json.dumps({
            "event": "initial_state",
            "data": {
                "is_running": queue_mgr.is_running,
                "is_paused": queue_mgr.is_paused,
                "current_job_id": queue_mgr.current_job_id,
                "jobs": queue_mgr.jobs
            }
        }))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)
    except Exception:
        if websocket in active_websockets:
            active_websockets.remove(websocket)


# Mount static assets
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

"""Persistent app settings (API keys, feature toggles)."""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent
SERVER_DIR = BASE_DIR / "server"
SETTINGS_FILE = SERVER_DIR / "settings.json"
_LEGACY_SETTINGS = BASE_DIR / "settings.json"


def _ensure_settings_path() -> Path:
    """Prefer server/settings.json; migrate from repo-root if needed."""
    if SETTINGS_FILE.is_file():
        return SETTINGS_FILE
    if _LEGACY_SETTINGS.is_file():
        try:
            SERVER_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(_LEGACY_SETTINGS), str(SETTINGS_FILE))
        except Exception:
            return _LEGACY_SETTINGS
    return SETTINGS_FILE

_DEFAULTS: Dict[str, Any] = {
    "autoname_output": True,
    "name_template_movie": "[name] ([year]) [imdbid-[imdbid]] - [[quality]]",
    "name_template_episode": "[show] - S[season]E[episode] - [epname] - [[quality]]",
    "svt_lp": 0,
    "svt_low_memory": False,
    "ssimu2_target": 80.0,
    "hdr_strict": True,
    "tmdb_lookup": True,
    "tmdb_api_key": "",
    "subtitle_search": False,
    "opensubtitles_api_key": "",
    "opensubtitles_username": "",
    "opensubtitles_password": "",
    "watch_folder_enabled": False,
    "watch_folder_path": "",
    "watch_folder_default_preset": "",
    "hour_format": "24",
    "allow_builtin_preset_edits": False,
}


_cache_lock = threading.Lock()
_cache: Dict[str, Any] | None = None
_cache_stamp: tuple | None = None


def load_settings() -> Dict[str, Any]:
    """Settings from disk, memoized on the file's (mtime, size).

    Callers hit this several times per queued job; re-reading and re-parsing the
    file each time showed up as hundreds of redundant reads on a batch add.
    """
    path = _ensure_settings_path()
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None

    with _cache_lock:
        if _cache is not None and stamp == _cache_stamp:
            return dict(_cache)

    data = dict(_DEFAULTS)
    if stamp is not None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data.update({k: raw[k] for k in _DEFAULTS if k in raw})
        except Exception:
            pass

    with _cache_lock:
        globals()["_cache"] = dict(data)
        globals()["_cache_stamp"] = stamp
    return data


def save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    data = load_settings()
    for key in _DEFAULTS:
        if key in updates:
            data[key] = updates[key]
    path = _ensure_settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(str(tmp), str(path))
    except Exception as e:
        raise RuntimeError(f"Could not save settings: {e}") from e
    return data

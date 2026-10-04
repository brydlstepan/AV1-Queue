"""Persistent app settings (API keys, feature toggles)."""

from __future__ import annotations

import json
import os
import re
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
    "name_template_movie": "[name] ([year]) [imdbid-[imdbid]] - [[resolution]]",
    "name_template_episode": "[show] - S[season]E[episode] - [epname] - [[resolution]]",
    "svt_lp": 0,
    "svt_low_memory": False,
    "ssimu2_target": 80.0,
    "preserve_dovi_rpu": True,
    "tmdb_lookup": True,
    "tmdb_api_key": "",
    "subtitle_search": False,
    "opensubtitles_api_key": "",
    "opensubtitles_username": "",
    "opensubtitles_password": "",
    "watch_folder_enabled": False,
    "watch_folder_path": "",
    "watch_folder_output_path": "",
    "watch_folder_default_preset": "",
    "hour_format": "24",
    "allow_builtin_preset_edits": False,
    # Pipeline + Test Mode settings (see PIPELINE_KEYS). Empty until the UI has
    # saved them once; queue jobs then follow these instead of a snapshot.
    "pipeline": {},
}


# Per-job encode config applied to every newly added job, before any preset or
# caller overrides. Single source of truth for these defaults (previously inline
# literals in queue_manager.add_job). Values that should track a user-facing app
# setting — autoname_output, the name templates, subtitle_search — are NOT
# duplicated here; add_job overlays those from load_settings() so the setting
# stays authoritative.
JOB_CONFIG_DEFAULTS: Dict[str, Any] = {
    "crf": 30.0,
    "preset": 4,
    "resolution_target": "source",
    "audio_bitrate_51": "320k",
    "audio_bitrate_stereo": "160k",
    "audio_format": "opus",
    "audio_languages": ["eng"],
    "audio_best_only": True,
    "container": "mp4",
    "test_mode": False,
    "trim_start": "01:01:10",
    "trim_end": "01:01:30",
    "autocrop": True,
    "ssimu2_post": False,
    "extract_subtitles": True,
    # Also run the normal subtitle step (extract + online search, full length)
    # for Test Mode encodes — a check that subtitles are found; off by default
    "test_mode_subtitles": False,
    "subtitle_languages": ["eng"],
    "subtitle_kinds": ["standard"],
    "subtitle_strip_credits": True,
}


# Job config keys owned by the global Pipeline settings / Test Mode rather than
# by the preset. A job keeps only its preset (crf, preset, resolution, audio
# bitrates, svt_params) and per-job choices (audio/subtitle track picks, naming
# edits); these follow the current settings: stamped on queued jobs when the
# settings are saved, and re-applied when a job starts.
PIPELINE_KEYS = (
    "ssimu2", "container", "audio_format", "audio_languages", "audio_best_only",
    "autocrop", "ssimu2_post", "extract_subtitles", "subtitle_strip_credits",
    "test_mode_subtitles", "subtitle_languages", "subtitle_kinds",
    "test_mode", "trim_start", "trim_end",
)

_HMS_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")
_PIPELINE_BOOLS = {
    "audio_best_only", "autocrop", "ssimu2_post", "extract_subtitles",
    "subtitle_strip_credits", "test_mode_subtitles", "test_mode",
}
_PIPELINE_CHOICES = {
    "ssimu2": {"auto", "gpu", "cpu"},
    "container": {"mp4", "webm"},
    "audio_format": {"opus", "eac3"},
}
_PIPELINE_LISTS = {"audio_languages": False, "subtitle_languages": True, "subtitle_kinds": True}


def normalize_pipeline(raw: Any) -> Dict[str, Any]:
    """Keep only known pipeline keys with valid values; silently drop the rest."""
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for key in PIPELINE_KEYS:
        if key not in raw:
            continue
        val = raw[key]
        if key in _PIPELINE_BOOLS:
            if isinstance(val, bool):
                out[key] = val
        elif key in _PIPELINE_CHOICES:
            if isinstance(val, str) and val.lower() in _PIPELINE_CHOICES[key]:
                out[key] = val.lower()
        elif key in _PIPELINE_LISTS:
            allow_empty = _PIPELINE_LISTS[key]
            if isinstance(val, list):
                items = [str(v).strip() for v in val if str(v).strip()]
                if items or allow_empty:
                    out[key] = items
        elif key in ("trim_start", "trim_end"):
            if isinstance(val, str) and _HMS_RE.match(val.strip()):
                out[key] = val.strip()
    # WebM cannot carry E-AC-3 (same rule the UI enforces)
    if out.get("container") == "webm" and out.get("audio_format") == "eac3":
        out["audio_format"] = "opus"
    return out


def get_pipeline_settings(settings: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """The saved pipeline overrides (only keys the UI has saved; may be empty)."""
    data = settings if settings is not None else load_settings()
    return normalize_pipeline(data.get("pipeline"))


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
    # [quality] was renamed [resolution] (same value); show saved templates with
    # the new token. [quality] still renders, for jobs queued before the rename.
    for k in ("name_template_movie", "name_template_episode"):
        if isinstance(data.get(k), str):
            data[k] = data[k].replace("[quality]", "[resolution]")

    with _cache_lock:
        globals()["_cache"] = dict(data)
        globals()["_cache_stamp"] = stamp
    return data


def save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    data = load_settings()
    for key in _DEFAULTS:
        if key in updates:
            if key == "pipeline":
                # Partial updates merge (Test Mode saves only its own keys)
                merged = dict(get_pipeline_settings(data))
                merged.update(normalize_pipeline(updates[key]))
                data[key] = merged
            else:
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

    # Prime the cache from the value just written. Relying on the (mtime_ns, size)
    # stamp alone can serve stale data when a later save lands in the same clock
    # tick with an identical size.
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    with _cache_lock:
        globals()["_cache"] = dict(data)
        globals()["_cache_stamp"] = stamp
    return data

"""Individual preset files in builtin/ (git-tracked) and local/ (gitignored)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
PRESETS_ROOT = BASE_DIR / "server" / "presets"
BUILTIN_DIR = PRESETS_ROOT / "builtin"
LOCAL_DIR = PRESETS_ROOT / "local"

_LEGACY_SERVER = BASE_DIR / "server" / "presets.json"
_LEGACY_ROOT = BASE_DIR / "presets.json"

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def _ensure_dirs() -> None:
    BUILTIN_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)


def _safe_filename(preset_id: str) -> str:
    raw = str(preset_id or "").strip() or "preset"
    cleaned = _SAFE_ID.sub("_", raw).strip("._") or "preset"
    return f"{cleaned}.json"


def _strip_meta(preset: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(preset)
    out.pop("builtin", None)
    return out


def _read_preset_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data.get("id"):
            return None
        return data
    except Exception:
        return None


def _write_preset_file(path: Path, preset: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _strip_meta(preset)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(str(tmp), str(path))


def _load_dir(folder: Path, *, builtin: bool) -> List[Dict[str, Any]]:
    if not folder.is_dir():
        return []
    items: List[Dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        data = _read_preset_file(path)
        if not data:
            continue
        data = dict(data)
        data["builtin"] = builtin
        items.append(data)
    return items


def _find_legacy_presets() -> Tuple[Optional[Path], List[Dict[str, Any]]]:
    for path in (_LEGACY_SERVER, _LEGACY_ROOT):
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                return path, [p for p in raw if isinstance(p, dict) and p.get("id")]
        except Exception:
            continue
    return None, []


def _maybe_migrate_legacy() -> None:
    """One-shot: move server/presets.json (or root legacy) into builtin/*.json."""
    _ensure_dirs()
    if any(BUILTIN_DIR.glob("*.json")) or any(LOCAL_DIR.glob("*.json")):
        return
    path, items = _find_legacy_presets()
    if not items:
        return
    for preset in items:
        dest = BUILTIN_DIR / _safe_filename(str(preset["id"]))
        _write_preset_file(dest, preset)
    if path and path.is_file():
        try:
            path.unlink()
        except Exception:
            pass


def find_preset_path(preset_id: str) -> Tuple[Optional[Path], bool]:
    """Return (path, is_builtin) for an existing preset id."""
    _ensure_dirs()
    name = _safe_filename(preset_id)
    builtin_path = BUILTIN_DIR / name
    if builtin_path.is_file():
        return builtin_path, True
    local_path = LOCAL_DIR / name
    if local_path.is_file():
        return local_path, False
    # Id may not match filename if it was renamed oddly — scan
    for path in BUILTIN_DIR.glob("*.json"):
        data = _read_preset_file(path)
        if data and data.get("id") == preset_id:
            return path, True
    for path in LOCAL_DIR.glob("*.json"):
        data = _read_preset_file(path)
        if data and data.get("id") == preset_id:
            return path, False
    return None, False


def load_presets() -> List[Dict[str, Any]]:
    """Builtin presets first, then local. Each entry includes builtin: bool."""
    _maybe_migrate_legacy()
    builtins = _load_dir(BUILTIN_DIR, builtin=True)
    locals_ = _load_dir(LOCAL_DIR, builtin=False)
    # Prefer builtin if the same id somehow exists in both
    seen = {p["id"] for p in builtins}
    locals_ = [p for p in locals_ if p.get("id") not in seen]
    return builtins + locals_


def save_preset_file(
    preset: Dict[str, Any],
    *,
    is_new: bool,
    was_builtin: bool,
    allow_builtin_edits: bool,
) -> Dict[str, Any]:
    """
    Persist one preset.
    - New presets always go to local/
    - Existing builtin updates require allow_builtin_edits and stay in builtin/
    - Existing local updates stay in local/
    """
    _ensure_dirs()
    preset_id = str(preset.get("id") or "").strip()
    if not preset_id:
        raise ValueError("Preset id is required")

    if is_new:
        if find_preset_path(preset_id)[0] is not None:
            raise ValueError(f"Preset id already exists: {preset_id}")
        path = LOCAL_DIR / _safe_filename(preset_id)
        builtin = False
    elif was_builtin:
        if not allow_builtin_edits:
            raise PermissionError("Built-in presets are read-only")
        path = BUILTIN_DIR / _safe_filename(preset_id)
        # Remove old file if id-based name changed
        old_path, _ = find_preset_path(preset_id)
        if old_path and old_path != path and old_path.is_file():
            try:
                old_path.unlink()
            except Exception:
                pass
        builtin = True
    else:
        path = LOCAL_DIR / _safe_filename(preset_id)
        old_path, was_b = find_preset_path(preset_id)
        if was_b:
            raise PermissionError("Cannot overwrite a built-in preset as local")
        if old_path and old_path != path and old_path.is_file():
            try:
                old_path.unlink()
            except Exception:
                pass
        builtin = False

    _write_preset_file(path, preset)
    out = dict(preset)
    out["builtin"] = builtin
    return out


def delete_preset_file(preset_id: str, *, allow_builtin_edits: bool) -> None:
    path, is_builtin = find_preset_path(preset_id)
    if path is None:
        return
    if is_builtin and not allow_builtin_edits:
        raise PermissionError("Built-in presets are read-only")
    try:
        path.unlink()
    except FileNotFoundError:
        pass

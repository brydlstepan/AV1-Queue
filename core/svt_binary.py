"""
Select the best SvtAv1EncApp (SVT-AV1-Tritium) build for this machine.

Variants live under bin/svt/:
  SvtAv1EncApp-x86-64-v3-znver2.exe    # x86-64-v3 baseline, Zen2 PGO profile (default)
  SvtAv1EncApp-znver2.exe              # native znver2 build, fallback

Tritium's official Windows releases only ship PGO-profiled builds trained on
AMD Zen2 hardware — there is no separate AVX-512 tier like the old Essential
vendoring had. Both builds default to `--asm max`, which auto-detects and
uses whatever instruction set (including AVX-512) the runtime CPU actually
supports, so the PGO training target does not gate available ISA — it only
affects how well-tuned the branch/scheduling profile is for a given CPU.

On ensure_svt_binary(), the matching file is copied to bin/SvtAv1EncApp.exe
only when the active selection changes (tracked by bin/.svt-active).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"
SVT_VARIANTS_DIR = BIN_DIR / "svt"
ACTIVE_EXE = BIN_DIR / "SvtAv1EncApp.exe"
ACTIVE_MARKER = BIN_DIR / ".svt-active"

# Preference order (first existing file wins). No AVX-512-specific tier exists
# in Tritium's official builds — --asm max at runtime picks up AVX-512 anyway
# when the CPU supports it, regardless of which of these was PGO-trained on.
VARIANT_CANDIDATES = (
    "SvtAv1EncApp-x86-64-v3-znver2.exe",
    "SvtAv1EncApp-znver2.exe",
)


def get_cpu_name() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        winreg.CloseKey(key)
        return str(name or "").strip()
    except Exception:
        return ""


def detect_svt_caps(bin_dir: Optional[Path] = None) -> Optional[Dict[str, bool]]:
    """Probe SvtAv1EncApp --help for the capabilities the app gates on:
    HDR10+ passthrough (--hdr10plus-json) and Dolby Vision RPU passthrough
    (--dolby-vision-rpu, used only when the user opts into preserve_dovi_rpu —
    see README.md "HDR & Dolby Vision"). Tritium has no --full-help; --help
    alone already lists everything."""
    root = Path(bin_dir) if bin_dir else BIN_DIR
    exe = root / "SvtAv1EncApp.exe"
    caps = {"hdr10plus_json": False, "dolby_vision_rpu": False}
    if not exe.is_file():
        return caps
    try:
        import subprocess

        r = subprocess.run(
            [str(exe), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        low = ((r.stdout or "") + "\n" + (r.stderr or "")).lower()
        caps["hdr10plus_json"] = "--hdr10plus-json" in low
        caps["dolby_vision_rpu"] = "--dolby-vision-rpu" in low
    except Exception as e:
        # Returning the default here would persist a false "no HDR10+ support"
        # into the marker and fail every HDR10+ job at preflight.
        print(f"[svt] Capability probe failed: {e}")
        return None
    return caps


def _pick_variant_name() -> Optional[str]:
    for name in VARIANT_CANDIDATES:
        path = SVT_VARIANTS_DIR / name
        if path.is_file() and path.stat().st_size > 1024 * 1024:
            return name
    return None


def _file_fingerprint(path: Path) -> Dict[str, Any]:
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))}


def _read_marker() -> Dict[str, Any]:
    try:
        if ACTIVE_MARKER.is_file():
            return json.loads(ACTIVE_MARKER.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_marker(payload: Dict[str, Any]) -> None:
    try:
        ACTIVE_MARKER.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[!] Could not write {ACTIVE_MARKER.name}: {e}")


def ensure_svt_binary(bin_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Select and activate the best SvtAv1EncApp for this machine.
    Returns a status dict (also printed for server logs).
    """
    global BIN_DIR, SVT_VARIANTS_DIR, ACTIVE_EXE, ACTIVE_MARKER
    if bin_dir is not None:
        BIN_DIR = Path(bin_dir)
        SVT_VARIANTS_DIR = BIN_DIR / "svt"
        ACTIVE_EXE = BIN_DIR / "SvtAv1EncApp.exe"
        ACTIVE_MARKER = BIN_DIR / ".svt-active"

    cpu_name = get_cpu_name()
    chosen = _pick_variant_name()
    info: Dict[str, Any] = {
        "cpu_name": cpu_name,
        "variants_dir": str(SVT_VARIANTS_DIR),
        "chosen": chosen,
        "active": str(ACTIVE_EXE),
        "action": "unchanged",
        "ok": True,
        "message": "",
        "caps": {},
    }

    if not chosen:
        if ACTIVE_EXE.is_file():
            info["message"] = "No bin/svt variants found; keeping existing SvtAv1EncApp.exe"
        else:
            info["ok"] = False
            info["message"] = "SvtAv1EncApp.exe missing and no bin/svt variants available"
            info["action"] = "missing"
        print(f"[svt] {info['message']}")
        info["caps"] = detect_svt_caps(BIN_DIR) or _read_marker().get("caps") or {"hdr10plus_json": False}
        return info

    src = SVT_VARIANTS_DIR / chosen
    src_fp = _file_fingerprint(src)
    marker = _read_marker()
    needs_copy = (
        not ACTIVE_EXE.is_file()
        or marker.get("variant") != chosen
        or marker.get("source") != src_fp
    )

    if needs_copy:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ACTIVE_EXE.with_suffix(".exe.tmp")
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, ACTIVE_EXE)
        except Exception as e:
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass
            info["ok"] = False
            info["action"] = "error"
            info["message"] = f"Failed to activate {chosen}: {e}"
            print(f"[svt] {info['message']}")
            return info
        info["action"] = "activated"
        info["message"] = f"Activated {chosen}"
    else:
        info["message"] = f"Already using {chosen}"

    # Keep the last known-good caps if the probe failed, rather than persisting
    # a false negative that would block HDR10+ jobs at preflight.
    caps = detect_svt_caps(BIN_DIR) or marker.get("caps") or {"hdr10plus_json": False}
    info["caps"] = caps
    _write_marker({
        "variant": chosen,
        "source": src_fp,
        "cpu_name": cpu_name,
        "caps": caps,
    })
    print(f"[svt] {info['message']}")
    if cpu_name:
        print(f"[svt] CPU: {cpu_name}")
    return info


def get_svt_status() -> Dict[str, Any]:
    """Status for /api/system without re-copying."""
    marker = _read_marker()
    return {
        "cpu_name": marker.get("cpu_name") or get_cpu_name(),
        "active_variant": marker.get("variant"),
        "active_exe": str(ACTIVE_EXE) if ACTIVE_EXE.is_file() else None,
        "caps": marker.get("caps") or {},
        "variants": sorted(p.name for p in SVT_VARIANTS_DIR.glob("SvtAv1EncApp-*.exe"))
        if SVT_VARIANTS_DIR.is_dir()
        else [],
    }

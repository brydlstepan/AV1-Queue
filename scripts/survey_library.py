#!/usr/bin/env python3
"""
Library survey — what HDR layers do our sources actually carry?

Answers the question that gates the HDR strategy (see README.md "HDR & Dolby Vision"):
  How many sources carry HDR10+? How many carry Dolby Vision, and at which profile?

Runs two detectors per file and reports both:

  1. "tool"  — AV1Queue's own HDRDoviProcessor.analyze_hdr_and_dovi(). This is
               exactly what the encode pipeline will decide, so the survey doubles
               as a validation of the detector.
  2. "frame" — an independent frame-level SEI probe for HDR10+ (ST 2094-40).

The delta between them is meaningful: HDR10+ metadata lives in frame SEI, not
stream side_data, so the tool detector (which only reads stream side_data) is
expected to under-report. Files where frame finds HDR10+ and tool does not are
printed explicitly.

Usage:
    python scripts/survey_library.py "D:\\Media" [options]

Options:
    --csv PATH        write per-file results (default: survey_results.csv)
    --ext LIST        comma-separated extensions (default: mkv,mp4,m2ts,ts,webm,mov)
    --workers N       parallel probes (default: 4)
    --no-frame-probe  skip the frame-level HDR10+ check (much faster, less accurate)
    --limit N         stop after N files (for a quick sample)

Requires only ffprobe. Looks in bin/, bin/ffmpeg/bin/, then PATH.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

DEFAULT_EXTS = "mkv,mp4,m2ts,ts,webm,mov"
IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""


def find_ffprobe() -> Optional[Path]:
    for cand in (
        BASE_DIR / "bin" / f"ffprobe{EXE}",
        BASE_DIR / "bin" / "ffmpeg" / "bin" / f"ffprobe{EXE}",
    ):
        if cand.is_file():
            return cand
    which = shutil.which("ffprobe")
    return Path(which) if which else None


def load_tool_analyzer():
    """Import AV1Queue's own detector. Returns None if unavailable."""
    try:
        from core.hdr_dovi import HDRDoviProcessor  # noqa: PLC0415
        return HDRDoviProcessor(bin_dir=BASE_DIR / "bin")
    except Exception as e:
        print(f"[!] Could not load core.hdr_dovi ({e}) — running frame probe only.")
        return None


def run_json(cmd: List[str], timeout: int = 120) -> Dict[str, Any]:
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        return json.loads(res.stdout or "{}")
    except Exception:
        return {}


def probe_streams(ffprobe: Path, path: Path) -> Dict[str, Any]:
    return run_json([
        str(ffprobe), "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", "-show_entries", "stream_side_data",
        str(path),
    ])


def frame_probe_hdr10plus(ffprobe: Path, path: Path, duration: float) -> bool:
    """
    Independent HDR10+ check. ST 2094-40 lives in frame SEI, so stream-level
    side_data misses it on most real files. Samples three points in the file.
    """
    points = [0.0]
    if duration and duration > 60:
        points = [duration * 0.10, duration * 0.45, duration * 0.75]
    for ss in points:
        interval = f"{ss:.2f}%+#12" if ss > 0 else "%+#12"
        data = run_json([
            str(ffprobe), "-v", "quiet", "-print_format", "json",
            "-select_streams", "v:0",
            "-read_intervals", interval,
            "-show_frames", "-show_entries", "frame=side_data_list",
            str(path),
        ], timeout=90)
        for frame in data.get("frames") or []:
            for side in frame.get("side_data_list") or []:
                st = str(side.get("side_data_type") or "").upper().replace(" ", "")
                if "2094-40" in st or "209440" in st or "HDR10+" in st or "HDR10PLUS" in st:
                    return True
    return False


def duration_of(probe: Dict[str, Any]) -> float:
    try:
        return float((probe.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def classify(tool: Dict[str, Any], frame_h10p: bool) -> str:
    """Bucket into the HDR/DoVi tiers, best layer first."""
    hdr10plus = bool(tool.get("is_hdr10plus")) or frame_h10p
    if tool.get("is_dovi"):
        prof = tool.get("dovi_profile")
        label = f"DV P{prof}" if prof is not None else "DV P?"
        return f"{label} + HDR10+" if hdr10plus else label
    if hdr10plus:
        return "HDR10+"
    if tool.get("is_hlg"):
        return "HLG"
    if tool.get("is_hdr"):
        return "HDR10"
    return "SDR"


def analyse(
    path: Path, ffprobe: Path, analyzer, do_frame: bool
) -> Optional[Dict[str, Any]]:
    probe = probe_streams(ffprobe, path)
    if not probe.get("streams"):
        return None
    dur = duration_of(probe)

    tool: Dict[str, Any] = {}
    if analyzer is not None:
        try:
            tool = analyzer.analyze_hdr_and_dovi(path, probe=probe) or {}
        except Exception:
            tool = {}

    frame_h10p = False
    if do_frame:
        looks_hdr = bool(
            tool.get("is_hdr") or tool.get("is_dovi")
            or "2084" in str(tool.get("color_transfer") or "")
        )
        if looks_hdr or not tool:
            frame_h10p = frame_probe_hdr10plus(ffprobe, path, dur)

    vid = next((s for s in probe["streams"] if s.get("codec_type") == "video"), {})
    return {
        "file": str(path),
        "name": path.name,
        "tier": classify(tool, frame_h10p),
        "codec": vid.get("codec_name") or "",
        "width": vid.get("width") or "",
        "height": vid.get("height") or "",
        "transfer": vid.get("color_transfer") or "",
        "dv_profile": tool.get("dovi_profile") if tool.get("dovi_profile") is not None else "",
        "dv_compat_id": tool.get("dovi_compat_id") if tool.get("dovi_compat_id") is not None else "",
        "tool_hdr10plus": bool(tool.get("is_hdr10plus")),
        "frame_hdr10plus": frame_h10p,
        "detector_disagree": bool(frame_h10p and not tool.get("is_hdr10plus")),
        "mastering": tool.get("mastering_display") or "",
        "content_light": tool.get("content_light") or "",
        "duration_s": round(dur, 1),
        "size_gb": round(path.stat().st_size / (1024 ** 3), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Survey HDR layers across a media library.")
    ap.add_argument("root", help="folder to walk (recursive)")
    ap.add_argument("--csv", default="survey_results.csv")
    ap.add_argument("--ext", default=DEFAULT_EXTS)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-frame-probe", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    ffprobe = find_ffprobe()
    if not ffprobe:
        print("[!] ffprobe not found. Run scripts/setup.ps1 or put ffprobe on PATH.")
        return 2

    root = Path(args.root)
    if not root.is_dir():
        print(f"[!] Not a directory: {root}")
        return 2

    exts = {f".{e.strip().lstrip('.').lower()}" for e in args.ext.split(",") if e.strip()}
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"[!] No files matching {sorted(exts)} under {root}")
        return 1

    print(f"ffprobe: {ffprobe}")
    print(f"Scanning {len(files)} file(s) under {root}")
    print(f"Frame-level HDR10+ probe: {'OFF' if args.no_frame_probe else 'ON'}\n")

    analyzer = load_tool_analyzer()
    rows: List[Dict[str, Any]] = []
    failed: List[Path] = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {
            pool.submit(analyse, p, ffprobe, analyzer, not args.no_frame_probe): p
            for p in files
        }
        for i, fut in enumerate(as_completed(futs), 1):
            p = futs[fut]
            try:
                row = fut.result()
            except Exception as e:
                print(f"  [!] {p.name}: {e}")
                row = None
            if row:
                rows.append(row)
            else:
                failed.append(p)
            print(f"\r  {i}/{len(files)}", end="", flush=True)
    print("\n")

    if not rows:
        print("[!] Nothing could be probed.")
        return 1

    tiers = Counter(r["tier"] for r in rows)
    total = len(rows)
    hours = sum(r["duration_s"] for r in rows) / 3600.0

    print("=" * 62)
    print("  LIBRARY HDR SURVEY")
    print("=" * 62)
    print(f"  {'TIER':<24} {'FILES':>7} {'SHARE':>8}   {'HOURS':>7}")
    print("  " + "-" * 58)
    for tier, n in sorted(tiers.items(), key=lambda kv: -kv[1]):
        th = sum(r["duration_s"] for r in rows if r["tier"] == tier) / 3600.0
        print(f"  {tier:<24} {n:>7} {n / total * 100:>7.1f}%   {th:>7.1f}")
    print("  " + "-" * 58)
    print(f"  {'TOTAL':<24} {total:>7} {'100.0%':>8}   {hours:>7.1f}")
    print()

    h10p = sum(1 for r in rows if r["frame_hdr10plus"] or r["tool_hdr10plus"])
    dv = sum(1 for r in rows if r["dv_profile"] != "")
    print(f"  HDR10+ present : {h10p:>5} / {total}  ({h10p / total * 100:.1f}%)")
    print(f"  Dolby Vision   : {dv:>5} / {total}  ({dv / total * 100:.1f}%)")

    dv_profiles = Counter(str(r["dv_profile"]) for r in rows if r["dv_profile"] != "")
    if dv_profiles:
        print("\n  DV profile breakdown:")
        for prof, n in sorted(dv_profiles.items()):
            note = {
                "5": "non-BC base layer — skipped, original kept",
                "7": "dual layer — RPU discarded, base layer encoded as HDR10",
                "8": "base layer already HDR10-compatible — RPU discarded",
            }.get(prof, "")
            print(f"    P{prof:<3} {n:>5}   {note}")

    disagree = [r for r in rows if r["detector_disagree"]]
    if disagree:
        print(f"\n  [!] DETECTOR GAP")
        print(f"      {len(disagree)} file(s) carry HDR10+ in frame SEI that the")
        print(f"      pipeline's stream-level detector does NOT see. These would")
        print(f"      silently lose HDR10+ on encode today:")
        for r in disagree[:10]:
            print(f"        - {r['name']}")
        if len(disagree) > 10:
            print(f"        ... and {len(disagree) - 10} more (see CSV)")

    if failed:
        print(f"\n  [!] {len(failed)} file(s) could not be probed:")
        for p in failed[:10]:
            print(f"        - {p.name}")

    out = Path(args.csv)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n  Per-file results: {out.resolve()}")

    print("\n  Use this to decide whether the HDR10+ passthrough path is worth")
    print("  building out, or is a near-empty set.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

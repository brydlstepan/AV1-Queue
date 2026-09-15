#!/usr/bin/env python3
"""Compare source vs encoded IVF/MP4 and print average SSIMULACRA2 as JSON."""

from __future__ import annotations

import argparse
import json
import gc
import sys
from pathlib import Path
from statistics import quantiles


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--encoded", required=True)
    parser.add_argument("--mode", default="auto", choices=["auto", "cpu", "gpu"])
    parser.add_argument("--crop", default="0,0,0,0", help="left,top,right,bottom")
    parser.add_argument("--target-height", type=int, default=0)
    parser.add_argument("--skip", type=int, default=1, help="Score every Nth frame")
    parser.add_argument("--cache", default="", help="Optional ffms2 cache path for source")
    args = parser.parse_args()

    from vstools import vs, core, clip_async_render  # noqa: WPS433 — requires VS env
    from vs_geometry import apply_crop, resize_to_height

    crop_parts = [int(float(x.strip() or 0)) for x in str(args.crop).split(",")]
    while len(crop_parts) < 4:
        crop_parts.append(0)
    left, top, right, bottom = crop_parts[:4]

    source_path = Path(args.source)
    encoded_path = Path(args.encoded)
    cache = args.cache or None

    try:
        if cache:
            source = core.ffms2.Source(source=str(source_path), cachefile=str(cache))
        else:
            source = core.ffms2.Source(source=str(source_path), cache=False)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Failed to open source: {e}"}))
        return 1

    try:
        encoded = core.ffms2.Source(source=str(encoded_path), cache=False)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Failed to open encoded: {e}"}))
        return 1

    source = apply_crop(source, left, top, right, bottom)

    target_height = int(args.target_height or 0)
    if target_height > 0 and source.height > target_height:
        source = resize_to_height(source, target_height)
    elif source.width != encoded.width or source.height != encoded.height:
        source = source.resize.Spline36(width=encoded.width, height=encoded.height)

    n_src, n_enc = len(source), len(encoded)
    if n_src != n_enc:
        # Allow tiny mismatch from keyframe-aligned test cuts
        n = min(n_src, n_enc)
        if abs(n_src - n_enc) > 3 or n < 8:
            print(json.dumps({
                "ok": False,
                "error": f"Frame count mismatch source={n_src} encoded={n_enc}"
            }))
            return 1
        source = source[:n]
        encoded = encoded[:n]

    skip = max(1, int(args.skip or 1))
    if skip > 1:
        source = source[::skip]
        encoded = encoded[::skip]

    mode = args.mode
    used_mode = mode
    result = None
    err = None

    def try_gpu():
        return core.vship.SSIMULACRA2(source, encoded, numStream=2)

    def try_cpu():
        return core.vszip.SSIMULACRA2(source, encoded)

    if mode == "gpu":
        try:
            result = try_gpu()
            used_mode = "gpu"
        except Exception as e:
            err = str(e)
    elif mode == "cpu":
        try:
            result = try_cpu()
            used_mode = "cpu"
        except Exception as e:
            err = str(e)
    else:
        try:
            result = try_gpu()
            used_mode = "gpu"
        except Exception:
            try:
                result = try_cpu()
                used_mode = "cpu"
            except Exception as e:
                err = str(e)

    if result is None:
        print(json.dumps({"ok": False, "error": f"SSIMU2 unavailable: {err}"}))
        return 1

    scores: list[float] = [0.0] * result.num_frames

    def collect(n: int, f: vs.VideoFrame) -> None:
        if used_mode == "gpu":
            scores[n] = float(f.props.get("_SSIMULACRA2"))
        else:
            scores[n] = float(f.props.get("SSIMULACRA2"))

    clip_async_render(result, outfile=None, progress=None, callback=collect)
    del result, source, encoded
    gc.collect()

    # Drop frames that failed to score (NaN). Flooring them to 0.0 instead would
    # sink `min`/`p15` and misreport a good encode as catastrophic.
    dropped = sum(1 for s in scores if s != s)
    valid = [max(0.0, float(s)) for s in scores if s == s]
    if not valid:
        print(json.dumps({"ok": False, "error": "No SSIMU2 scores produced"}))
        return 1

    avg = sum(valid) / len(valid)
    ordered = sorted(valid)
    p15 = quantiles(ordered, n=100)[14] if len(ordered) >= 2 else ordered[0]
    minimum = ordered[0]

    print(json.dumps({
        "ok": True,
        "mode": used_mode,
        "frames": len(valid),
        "dropped": dropped,
        "skip": skip,
        "avg": round(avg, 4),
        "p15": round(p15, 4),
        "min": round(minimum, 4),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())

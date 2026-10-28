"""
Shared VapourSynth crop helper.

Used by core/measure_ssimu2.py (the post-encode quality check) to reproduce the
crop HandBrake applied to the encode, so the two compare the same geometry — a
mismatch would skew the score without error. (The downscale is matched there by
resizing to the encode's own dimensions.)
"""

from __future__ import annotations


def apply_crop(clip, left: int, top: int, right: int, bottom: int):
    """Crop clip if any side is non-zero; otherwise return it unchanged."""
    if any(v > 0 for v in (left, top, right, bottom)):
        return clip.std.Crop(left=left, right=right, top=top, bottom=bottom)
    return clip


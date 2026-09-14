"""
Shared VapourSynth crop/resize helpers.

Used by both core/svt_encode.py (the actual encode) and core/measure_ssimu2.py
(the post-encode quality check) so the two can never silently drift apart on
geometry — a mismatch here would invalidate the SSIMU2 score without error.
"""

from __future__ import annotations


def apply_crop(clip, left: int, top: int, right: int, bottom: int):
    """Crop clip if any side is non-zero; otherwise return it unchanged."""
    if any(v > 0 for v in (left, top, right, bottom)):
        return clip.std.Crop(left=left, right=right, top=top, bottom=bottom)
    return clip


def resize_to_height(clip, target_height: int):
    """Downscale to target_height (Spline36, even width) if clip is taller; else unchanged."""
    if target_height and target_height > 0 and clip.height > target_height:
        new_h = target_height
        new_w = int(round(clip.width * new_h / clip.height))
        new_w -= new_w % 2
        return clip.resize.Spline36(width=new_w, height=new_h)
    return clip

"""Pure window selection for cleaned segments driven by content roles and quality curves."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from src.core.ontology import ContentRole
from src.core.types import CleanedSegment
from src.modules.shot_windows import QualityCurve, select_golden_windows

MULTI_TAKE_SPAN_SEC: float = 30.0


def window_cleaned_segments(
    segments: list[CleanedSegment],
    roles: Mapping[Path, ContentRole],
    min_len: float,
    max_len: float,
    curve_fn: Callable[[Path, float, float], QualityCurve | None],
) -> list[CleanedSegment]:
    """Window cleaned segments by content role and quality curve.

    A-roll is kept whole. Other roles are windowed to their optimal quality
    sub-window(s) when longer than ``max_len``. Clips without an assigned role
    are returned unchanged.
    """
    out: list[CleanedSegment] = []
    for seg in segments:
        role = roles.get(seg.clip_path)
        if role is None:
            out.append(seg)
            continue

        role_val = role.value

        if role == ContentRole.A_ROLL or role_val == "a_roll":
            meta = dict(seg.metadata)
            meta["content_role"] = role_val
            out.append(replace(seg, metadata=meta))
            continue

        span = seg.end_time - seg.start_time
        if span <= max_len:
            meta = dict(seg.metadata)
            meta["content_role"] = role_val
            out.append(replace(seg, metadata=meta))
            continue

        max_takes = 2 if seg.duration >= MULTI_TAKE_SPAN_SEC else 1
        curve = curve_fn(seg.clip_path, seg.start_time, seg.end_time)
        windows = select_golden_windows(
            curve,
            seg.start_time,
            seg.end_time,
            min_len,
            max_len,
            max_takes=max_takes,
        )
        if not windows:
            windows = [(seg.start_time, seg.end_time)]

        for w0, w1 in windows:
            delta_s = w0 - seg.start_time
            delta_e = w1 - seg.end_time
            meta = dict(seg.metadata)
            meta["content_role"] = role_val
            meta["golden_window"] = {
                "span": [seg.start_time, seg.end_time],
                "window": [w0, w1],
            }
            new_seg = replace(
                seg,
                start_time=w0,
                end_time=w1,
                duration=w1 - w0,
                source_start_time=seg.source_start_time + delta_s,
                source_end_time=seg.source_end_time + delta_e,
                metadata=meta,
            )
            out.append(new_seg)

    return out

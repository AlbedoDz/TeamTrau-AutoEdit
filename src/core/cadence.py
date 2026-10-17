"""Rhythmic cadence engine: musical bar grid extraction and bar-locked cut generation.

Everything here is a pure function of its inputs (no audio decoding, no I/O), so it is cheap to
test with synthetic beat lists and safe to call again with scene knowledge later.

* :func:`extract_musical_bar_grid` turns tracked beats + song sections into a
  :class:`~src.core.types.MusicalBarGrid` (bar starts and 2/4/8-bar phrases that never cross a
  section boundary).
* :func:`cadence_cuts_with_stats` / :func:`generate_cadence_cuts` pick cut times that sit only on
  bar boundaries or half-bar points, follow an emotional curve per SongSection, vary the slot
  length deterministically from a seed, then snap every cut to the timeline frame grid.

Callers fall back to the legacy beat filter whenever these return ``None`` (low confidence).
"""

import math
import random
from collections.abc import Sequence
from fractions import Fraction
from typing import Any

import numpy as np

from src.core.timebase import frame_rate_fraction
from src.core.types import (
    BeatMarker,
    CadenceProfile,
    MusicalBarGrid,
    SongSection,
    SongSectionType,
)

# Default slot lengths, in bars, per profile. ``PipelineConfig.cadence_multiples`` overrides them.
PROFILE_MULTIPLES: dict[CadenceProfile, tuple[float, ...]] = {
    CadenceProfile.FAST_SYNCOPATED: (0.5, 1.0, 2.0),
    CadenceProfile.BALANCED_NARRATIVE: (1.0, 2.0, 4.0),
    CadenceProfile.LINGERING_CINEMATIC: (2.0, 4.0, 8.0),
    CadenceProfile.VIRAL_BURST: (0.5, 1.0),
}

# Below this a bar grid is not trusted and callers use the legacy beat filter.
MIN_GRID_CONFIDENCE = 0.4
# Coefficient of variation of bar lengths above which downbeats are considered irregular.
MAX_BAR_LENGTH_CV = 0.08
_MIN_REGULAR_BARS = 3


def coerce_profile(profile: CadenceProfile | str | None) -> CadenceProfile:
    """Accept enum or string; unknown values fall back to ``balanced_narrative``."""
    if isinstance(profile, CadenceProfile):
        return profile
    try:
        return CadenceProfile(str(profile))
    except ValueError:
        return CadenceProfile.BALANCED_NARRATIVE


def profile_multiples(
    profile: CadenceProfile | str | None, config: Any | None = None
) -> tuple[float, ...]:
    """Allowed slot lengths (in bars) for ``profile``; ``config.cadence_multiples`` wins if set."""
    override = getattr(config, "cadence_multiples", None) if config is not None else None
    if override:
        cleaned = sorted({float(m) for m in override if float(m) > 0})
        if cleaned:
            return tuple(cleaned)
    return PROFILE_MULTIPLES[coerce_profile(profile)]


# --------------------------------------------------------------------------- bar grid


def estimate_beats_per_bar(strengths: Sequence[float]) -> int:
    """Pick 3 or 4 beats per bar from the accent pattern of the beat strengths (default 4).

    3 is chosen only when a 3-beat grouping explains the accents clearly better than 4-beat
    grouping, so 4/4 (and anything ambiguous) stays 4.
    """
    s = np.asarray(strengths, dtype=float)
    if s.size < 12:
        return 4
    spread = float(np.std(s)) + 1e-9

    def contrast(m: int) -> float:
        best = max(float(np.mean(s[p::m])) for p in range(m))
        return (best - float(np.mean(s))) / spread

    return 3 if contrast(3) > 1.25 * contrast(4) + 0.15 else 4


def assign_downbeats(beats: Sequence[BeatMarker], beats_per_bar: int) -> list[BeatMarker]:
    """Mark every ``beats_per_bar``-th beat as downbeat, choosing the phase with most accent."""
    ordered = sorted(beats, key=lambda b: b.timestamp)
    if not ordered:
        return []
    bpb = max(2, int(beats_per_bar))
    phases = range(min(bpb, len(ordered)))
    best = max(phases, key=lambda p: sum(b.strength for b in ordered[p::bpb]))
    return [
        BeatMarker(b.timestamp, b.frame_idx, b.strength, (i % bpb) == best)
        for i, b in enumerate(ordered)
    ]


def _regularise(
    beats: list[BeatMarker], tempo_bpm: float, bpb: int, duration: float
) -> tuple[list[float], float, float]:
    """Evenly spaced bars from a least-squares beat lattice. Returns (bars, bar_len, confidence)."""
    ts = np.array([b.timestamp for b in beats], dtype=float)
    period0 = float(np.median(np.diff(ts))) if ts.size >= 4 else 0.0
    if period0 <= 0:
        period0 = 60.0 / tempo_bpm if tempo_bpm > 0 else 0.5
    idx = np.round((ts - ts[0]) / period0)
    if np.unique(idx).size >= 2:
        slope, intercept = np.polyfit(idx, ts, 1)
        period = float(slope) if slope > 0 else period0
        t0 = float(intercept)
    else:
        period, t0 = period0, float(ts[0])
    resid = ts - (t0 + idx * period)
    resid_ratio = float(np.std(resid)) / period if period > 0 else 1.0
    confidence = 0.6 * float(np.clip(1.0 - resid_ratio / 0.15, 0.0, 1.0))
    if len(beats) < bpb * _MIN_REGULAR_BARS:
        confidence = min(confidence, 0.3)

    strengths = np.array([b.strength for b in beats], dtype=float)
    phase_score = [float(strengths[(idx.astype(int) % bpb) == p].sum()) for p in range(bpb)]
    phase = int(np.argmax(phase_score))
    bar_len = bpb * period
    anchor = t0 + phase * period
    k_min = math.ceil((0.0 - anchor) / bar_len - 1e-9)
    bars: list[float] = []
    k = k_min
    while anchor + k * bar_len < duration - 1e-6:
        bars.append(anchor + k * bar_len)
        k += 1
    return bars, bar_len, confidence


def _smooth_lattice(downs: list[float], hop_sec: float) -> tuple[float, float] | None:
    """Least-squares ``first + k * bar_len`` through the downbeats, if it tracks them closely.

    Beat trackers quantise to analysis frames (+-0.5 hop), which makes raw downbeat times drift
    against the true tempo. The fitted lattice removes that, but is only used when no downbeat
    is further than 2.5 hops from it (so a drifting live tempo keeps its detected downbeats).
    """
    k = np.arange(len(downs), dtype=float)
    slope, intercept = np.polyfit(k, np.asarray(downs, dtype=float), 1)
    if slope <= 0:
        return None
    resid = np.abs(np.asarray(downs) - (intercept + slope * k))
    if float(resid.max()) > 2.5 * max(hop_sec, 1e-3):
        return None
    return float(slope), float(intercept)


def _refine_to_beats(
    bars: list[float], beats: list[BeatMarker], hop_sec: float
) -> tuple[list[float], list[float]]:
    """Move each bar to the strongest beat within half a hop; per-bar confidence = its strength."""
    if not beats:
        return list(bars), [0.0] * len(bars)
    ts = np.array([b.timestamp for b in beats], dtype=float)
    st = np.array([b.strength for b in beats], dtype=float)
    tol = max(hop_sec, 0.0) / 2.0 + 1e-9
    out: list[float] = []
    conf: list[float] = []
    for bt in bars:
        near = np.nonzero(np.abs(ts - bt) <= tol)[0]
        if near.size:
            j = int(near[np.argmax(st[near])])
            out.append(float(ts[j]))
            conf.append(float(np.clip(st[j], 0.0, 1.0)))
        else:
            out.append(float(bt))
            conf.append(0.0)
    return out, conf


def _phrases(
    bars: list[float], sections: Sequence[SongSection], duration: float, tol: float
) -> tuple[list[float], list[float], list[float]]:
    """Group bars into 2/4/8-bar phrases, restarting at every section start."""
    starts = sorted({s.start_time for s in sections if 0.0 < s.start_time < duration})
    # Bars before the first boundary form group 0; each boundary opens a new group.
    groups: list[list[float]] = [[] for _ in range(len(starts) + 1)]
    for b in bars:
        g = sum(1 for s in starts if b >= s - tol)
        groups[g].append(b)
    out: list[list[float]] = [[], [], []]
    for grp in groups:
        for slot, n in enumerate((2, 4, 8)):
            out[slot].extend(grp[::n])
    return out[0], out[1], out[2]


def extract_musical_bar_grid(
    beats: Sequence[BeatMarker],
    sections: Sequence[SongSection],
    tempo_bpm: float,
    duration: float,
    hop_sec: float,
    beats_per_bar: int = 4,
) -> MusicalBarGrid:
    """Bar boundaries (and phrases) from tracked beats.

    Bars come from the downbeats when they are regular (>= 3 bars, bar-length CV <= 8 %).
    Otherwise bars are regularised from the beat lattice at the best accent phase and the
    confidence is capped at 0.6. Each bar is then nudged to the strongest beat within half a
    hop. Phrases never cross a section boundary.
    """
    bpb = max(2, int(beats_per_bar))
    ordered = sorted(beats, key=lambda b: b.timestamp)
    if len(ordered) < 2 or duration <= 0:
        return MusicalBarGrid(tempo_bpm=float(tempo_bpm), beats_per_bar=bpb, confidence=0.0)

    downs = [b.timestamp for b in ordered if b.is_downbeat]
    bars: list[float] = []
    confidence = 0.0
    bar_len = 0.0
    lattice = False
    if len(downs) >= _MIN_REGULAR_BARS + 1:
        lens = np.diff(downs)
        mean_len = float(np.mean(lens))
        cv = float(np.std(lens)) / mean_len if mean_len > 0 else 1.0
        if mean_len > 0 and cv <= MAX_BAR_LENGTH_CV:
            bar_len = float(np.median(lens))
            bars = list(downs)
            smooth = _smooth_lattice(downs, hop_sec)
            if smooth is not None:  # frame-quantised downbeats -> least-squares lattice
                bar_len, first = smooth
                bars = [first]
            while bars[-1] + bar_len < duration - 1e-6:  # downbeats may stop before the end
                bars.append(bars[-1] + bar_len)
            confidence = float(np.clip(1.0 - cv / (2 * MAX_BAR_LENGTH_CV), 0.5, 1.0))
            lattice = smooth is not None
    if not bars:
        bars, bar_len, confidence = _regularise(ordered, tempo_bpm, bpb, duration)

    if lattice:  # already statistically better than any single frame-quantised beat
        bar_conf = [1.0] * len(bars)
    else:
        bars, bar_conf = _refine_to_beats(bars, ordered, hop_sec)
    p2, p4, p8 = _phrases(bars, sections, duration, tol=max(hop_sec, 0.0) / 2.0)
    derived_tempo = 60.0 * bpb / bar_len if bar_len > 0 else float(tempo_bpm)
    return MusicalBarGrid(
        tempo_bpm=derived_tempo,
        beats_per_bar=bpb,
        bar_times=bars,
        phrase_2=p2,
        phrase_4=p4,
        phrase_8=p8,
        confidence=confidence if bars else 0.0,
        bar_confidence=bar_conf,
    )


# --------------------------------------------------------------------------- frame snapping


def snap_to_frame(t: float, fps: float | Fraction) -> float:
    """Nearest frame boundary to ``t`` seconds, using the exact (NTSC-aware) frame rate."""
    rate = frame_rate_fraction(fps)
    frames = math.floor(Fraction(t).limit_denominator(10**9) * rate + Fraction(1, 2))
    return float(Fraction(frames) / rate)


# --------------------------------------------------------------------------- cut generation


def grid_points(bar_grid: MusicalBarGrid, duration: float) -> list[float]:
    """Allowed cut points: every bar start and half-bar midpoint strictly inside the track."""
    bars = [b for b in bar_grid.bar_times if b >= 0.0]
    if not bars:
        return []
    lens = np.diff(bars)
    bar_len = float(np.median(lens)) if lens.size else 0.0
    if bar_len <= 0:
        bar_len = 60.0 * bar_grid.beats_per_bar / bar_grid.tempo_bpm if bar_grid.tempo_bpm > 0 else 0.0
    if bar_len <= 0:
        return []
    eps = 1e-6
    pts: list[float] = []
    for i, b in enumerate(bars):
        nxt = bars[i + 1] if i + 1 < len(bars) else b + bar_len
        if b < duration - eps:
            pts.append(b)
        mid = (b + nxt) / 2.0
        if mid < duration - eps:
            pts.append(mid)
    return pts


def _section_target(
    section: SongSection | None, progress: float, k: int
) -> float:
    """Desired slot length (bars) for a position inside a section: the emotional curve."""
    if section is None:
        return 2.0
    if section.is_climax or section.section_type == SongSectionType.DROP:
        return 1.0 if k % 2 == 0 else 0.5  # 1 <-> 1/2
    if section.section_type == SongSectionType.INTRO:
        return 4.0 - 2.0 * progress  # 4 -> 2
    if section.section_type == SongSectionType.BUILDUP:
        return 2.0 - progress  # 2 -> 1
    return 4.0 + 4.0 * progress  # OUTRO: 4 -> 8


def _position(target: float, allowed: Sequence[float]) -> float:
    """Fractional index of ``target`` inside the sorted ``allowed`` list (log-spaced)."""
    if target <= allowed[0]:
        return 0.0
    if target >= allowed[-1]:
        return float(len(allowed) - 1)
    for i in range(len(allowed) - 1):
        lo, hi = allowed[i], allowed[i + 1]
        if lo <= target <= hi:
            return i + (math.log(target) - math.log(lo)) / (math.log(hi) - math.log(lo))
    return 0.0


def _find_section(sections: Sequence[SongSection], t: float) -> SongSection | None:
    for s in sections:
        if s.start_time <= t < s.end_time:
            return s
    return None


def _segments(
    pts: list[float],
    sections: Sequence[SongSection],
    duration: float,
    unit: int,
) -> list[tuple[int, int, SongSection | None]]:
    """Index ranges ``[a, b)`` into ``pts`` that never straddle a section boundary.

    Boundaries are placed on the nearest point whose index is a multiple of ``unit`` (2 = bar
    boundary, 1 = half-bar). Index ``len(pts)`` stands for the end of the track.
    """
    n = len(pts)
    cand = [i for i in range(0, n, unit)]

    def nearest(t: float) -> int:
        if not cand:
            return 0
        best = min(cand, key=lambda i: abs(pts[i] - t))
        return n if t >= duration - 1e-6 or abs(duration - t) < abs(pts[best] - t) else best

    bounds = {0, n}
    for s in sections:
        for t in (s.start_time, s.end_time):
            if 0.0 < t < duration:
                bounds.add(nearest(t))
    ordered = sorted(bounds)

    def time_of(i: int) -> float:
        return duration if i >= n else pts[i]

    out: list[tuple[int, int, SongSection | None]] = []
    for a, b in zip(ordered, ordered[1:]):
        mid = (time_of(a) + time_of(b)) / 2.0
        out.append((a, b, _find_section(sections, mid)))
    return out


def cadence_cuts_with_stats(
    bar_grid: MusicalBarGrid | None,
    sections: Sequence[SongSection],
    profile: CadenceProfile | str | None,
    multiples: Sequence[float] | None,
    duration: float,
    fps: float,
    seed: int = 7,
) -> tuple[list[float], float, list[float]] | None:
    """Cut times on bar/half-bar points following the section curve, snapped to frames.

    Returns ``(cuts, max_snap_error_sec, slot_lengths_sec)`` or ``None`` when the grid is missing
    or not trustworthy (callers then use the legacy beat filter). ``cuts`` starts at 0.0 and
    ends at ``duration``; no slot crosses a section boundary; never three consecutive slots of
    the same length; the result is identical for identical inputs and ``seed``.
    """
    if (
        bar_grid is None
        or len(bar_grid.bar_times) < 2
        or bar_grid.confidence < MIN_GRID_CONFIDENCE
        or duration <= 0
        or fps <= 0
    ):
        return None
    mult = tuple(multiples) if multiples else profile_multiples(profile)
    half_counts = sorted({max(1, int(round(m * 2))) for m in mult if m > 0})
    if not half_counts:
        return None
    allowed = [h / 2.0 for h in half_counts]
    pts = grid_points(bar_grid, duration)
    n = len(pts)
    if n < 2:
        return None
    ordered_sections = sorted(sections, key=lambda s: s.start_time)
    unit = 2 if half_counts[0] >= 2 else 1
    rng = random.Random(seed)

    ends: list[int] = []  # slot end indices into pts (n = end of track)
    last_lens: list[int] = []
    for a, b, section in _segments(pts, ordered_sections, duration, unit):
        perm = rng.sample([-1, 0, 1], 3)
        cursor = a
        k = 0
        seg_ends: list[int] = []
        while cursor < b:
            rem = b - cursor
            progress = (cursor - a) / max(b - a, 1)
            pos = _position(_section_target(section, progress, k), allowed)
            want = min(max(int(round(pos)) + perm[k % 3], 0), len(half_counts) - 1)
            order = sorted(range(len(half_counts)), key=lambda i: (abs(i - want), i))

            def fits(i: int, rem: int = rem) -> bool:
                return half_counts[i] <= rem

            def leaves_ok(i: int, rem: int = rem) -> bool:
                left = rem - half_counts[i]
                return left == 0 or left >= half_counts[0]

            def repeats(i: int) -> bool:
                return len(last_lens) >= 2 and last_lens[-1] == last_lens[-2] == half_counts[i]

            choice = next((i for i in order if fits(i) and leaves_ok(i) and not repeats(i)), None)
            if choice is None:
                choice = next((i for i in order if fits(i) and leaves_ok(i)), None)
            if choice is None:
                choice = next((i for i in order if fits(i) and not repeats(i)), None)
            if choice is None:
                choice = next((i for i in order if fits(i)), None)
            if choice is None:  # remainder shorter than the shortest allowed slot
                if seg_ends:  # absorb it into the previous slot of this segment
                    seg_ends[-1] = b
                    ends[-1] = b
                    last_lens[-1] += rem
                else:
                    seg_ends.append(b)
                    ends.append(b)
                    last_lens.append(rem)
                break
            cursor += half_counts[choice]
            if repeats(choice) and seg_ends and len(half_counts) > 1:
                # Only a forced tail piece can still repeat: fold it into the previous slot.
                seg_ends[-1] = cursor
                ends[-1] = cursor
                last_lens[-1] += half_counts[choice]
            else:
                seg_ends.append(cursor)
                ends.append(cursor)
                last_lens.append(half_counts[choice])
            k += 1

    raw = [0.0] + [pts[e] for e in ends if e < n] + [duration]
    # A stub shorter than half a half-bar at the very end is absorbed by the last real slot.
    half_len = float(np.median(np.diff(pts))) if n > 1 else 0.0
    if len(raw) > 2 and duration - raw[-2] < 0.5 * half_len:
        del raw[-2]

    cuts: list[float] = []
    max_err = 0.0
    for t in raw:
        snapped = snap_to_frame(t, fps)
        max_err = max(max_err, abs(snapped - t))
        if cuts and snapped <= cuts[-1] + 1e-9:
            continue  # two cuts collapsed onto one frame
        cuts.append(snapped)
    if len(cuts) < 2:
        return None
    slots = [b - a for a, b in zip(cuts, cuts[1:])]
    return cuts, max_err, slots


def generate_cadence_cuts(
    bar_grid: MusicalBarGrid | None,
    sections: Sequence[SongSection],
    profile: CadenceProfile | str | None,
    multiples: Sequence[float] | None,
    duration: float,
    fps: float,
    seed: int = 7,
) -> list[float] | None:
    """Like :func:`cadence_cuts_with_stats` but only the cut list (``None`` = use legacy path)."""
    res = cadence_cuts_with_stats(bar_grid, sections, profile, multiples, duration, fps, seed)
    return None if res is None else res[0]

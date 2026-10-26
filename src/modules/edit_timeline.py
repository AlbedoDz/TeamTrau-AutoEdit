"""Edit timeline synthesis: analysed scenes + beat cuts -> frame-exact EditTimeline.

This is the ONLY place where scenes are chosen for beat slots. The resulting
EditTimeline is what the GUI shows and edits, what the project file stores, and
what Deliver renders / exports, so the master video always matches the timeline.
"""

import dataclasses
import logging
import math
import random
from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path

from src.core.analysis_cache import AnalysisCache
from src.core.cadence import (
    MIN_GRID_CONFIDENCE,
    cadence_cuts_with_stats,
    coerce_profile,
    grid_points,
    profile_multiples,
    snap_to_frame,
)
from src.core.config import PipelineConfig
from src.core.ontology import natural_sort_key
from src.core.timebase import frame_rate_fraction
from src.core.types import (
    CadenceProfile,
    CleanedSegment,
    DefectType,
    DeliverSettings,
    EditClip,
    EditTimeline,
    MusicalBarGrid,
    ScoredScene,
    SongSection,
    SongSectionType,
)
from src.modules.shot_windows import SubWindowPlanner
from src.modules.visual_flow import assign_ambient_motion, evaluate_cut_continuity

logger = logging.getLogger(__name__)


def select_scenes_for_cuts(
    scored_scenes: list[ScoredScene],
    cut_timestamps: list[float],
    sections: list[SongSection] | None = None,
    action_drop_threshold: float = 0.70,
) -> list[ScoredScene]:
    """Align scored scenes to cut intervals based on music structural sections and motion energy.

    - DROP / CLIMAX sections: prioritizes high-action footage (motion_energy >= action_drop_threshold).
    - INTRO / OUTRO sections: prioritizes scenic/calm footage (motion_energy < 0.35).
    - BUILDUP sections: prioritizes dynamic footage (0.35 <= motion_energy < action_drop_threshold).
    - Gracefully falls back to ranked scenes if any pool is empty or sections is None.
    """
    num_slots = len(cut_timestamps) - 1
    if num_slots <= 0:
        return []
    if not scored_scenes:
        raise ValueError("Cannot select scenes from empty scored_scenes.")

    high_action = [s for s in scored_scenes if s.motion_energy >= action_drop_threshold]
    scenic = [s for s in scored_scenes if s.motion_energy < 0.35]
    dynamic = [s for s in scored_scenes if 0.35 <= s.motion_energy < action_drop_threshold]

    pool_drop = high_action if high_action else scored_scenes
    pool_scenic = scenic if scenic else scored_scenes
    pool_dynamic = dynamic if dynamic else scored_scenes

    cursors = {"drop": 0, "scenic": 0, "dynamic": 0, "default": 0}

    def pick(pool: list[ScoredScene], key: str, slot_dur: float) -> ScoredScene:
        """Round-robin through the pool, preferring a scene long enough to fill the slot.

        A scene shorter than its beat slot would otherwise leave a hole that pushes every
        later cut off the beat.
        """
        start = cursors[key]
        n = len(pool)
        for k in range(n):
            cand = pool[(start + k) % n]
            needed = slot_dur * max(0.1, float(cand.speed or 1.0))
            if cand.scene.duration + 1e-6 >= needed:
                cursors[key] = start + k + 1
                return cand
        cursors[key] = start + 1
        return max(pool, key=lambda c: c.scene.duration / max(0.1, float(c.speed or 1.0)))

    selected: list[ScoredScene] = []
    for i in range(num_slots):
        slot_start = cut_timestamps[i]
        slot_end = cut_timestamps[i + 1]
        slot_dur = slot_end - slot_start
        mid_t = (slot_start + slot_end) / 2.0

        if sections:
            sec: SongSection | None = None
            for s in sections:
                if s.start_time <= mid_t < s.end_time:
                    sec = s
                    break
            if sec is None:
                sec = min(
                    sections,
                    key=lambda s: min(abs(s.start_time - mid_t), abs(s.end_time - mid_t)),
                )

            if sec.section_type == SongSectionType.DROP or sec.is_climax:
                candidate = pick(pool_drop, "drop", slot_dur)
            elif sec.section_type in (SongSectionType.INTRO, SongSectionType.OUTRO):
                candidate = pick(pool_scenic, "scenic", slot_dur)
            else:
                candidate = pick(pool_dynamic, "dynamic", slot_dur)
        else:
            candidate = pick(scored_scenes, "default", slot_dur)

        selected.append(candidate)

    return selected


def clip_from_scene(
    sc: ScoredScene,
    frames: int,
    *,
    is_teaser: bool = False,
    label: str = "",
    source_in: float | None = None,
) -> EditClip:
    """Place a scored scene on the timeline for ``frames`` output frames.

    ``source_in`` places the clip at an arbitrary in-point inside the scene (a sub-window of a long
    shot); ``available`` is then the footage left between that in-point and the scene end.
    ``scene_start``/``scene_end`` always identify the originating scene.

    When the scene was cut from a render proxy (VFR->CFR cache file) the clip remembers the
    user's original media (``SceneCut.original_path``) so NLE exports can relink to it.
    """
    if source_in is None:
        s_in, available = sc.scene.start_time, sc.scene.duration
    else:
        s_in = float(source_in)
        available = sc.scene.start_time + sc.scene.duration - s_in
    return EditClip(
        original_path=sc.scene.original_path,
        source_path=sc.scene.clip_path,
        source_in=s_in,
        frames=max(1, int(frames)),
        available=max(0.04, available),
        speed=max(0.1, float(sc.speed or 1.0)),
        crop_window=sc.crop_window,
        ken_burns=(sc.metadata or {}).get("ken_burns"),
        is_teaser=is_teaser,
        label=label or sc.scene.clip_path.stem,
        scene_start=sc.scene.start_time,
        scene_end=sc.scene.end_time,
    )


# --------------------------------------------------------------------------- cadence allocation

_EPS = 1e-6


def _section_at(sections: Sequence[SongSection], t: float) -> SongSection | None:
    for s in sections:
        if s.start_time <= t < s.end_time:
            return s
    return None


def _preferred_bars(length_sec: float, threshold: float) -> tuple[float, float]:
    """Bars a scene of ``length_sec`` should fill: long shots linger, short ones flash."""
    if length_sec >= max(threshold + 3.0, 15.0):
        return 4.0, 8.0
    if length_sec >= threshold:
        return 2.0, 4.0
    if length_sec >= 4.0:
        return 1.0, 2.0
    return 0.5, 1.0


def _bar_length(bar_grid: MusicalBarGrid) -> float:
    bars = [b for b in bar_grid.bar_times if b >= 0.0]
    if len(bars) >= 2:
        diffs = sorted(y - x for x, y in zip(bars, bars[1:], strict=False))
        return diffs[len(diffs) // 2]
    return 60.0 * bar_grid.beats_per_bar / max(bar_grid.tempo_bpm, 1e-3)


class _ScenePicker:
    """Section-aware round-robin that prefers scenes with enough unused footage for the slot."""

    def __init__(
        self, scored: list[ScoredScene], action_drop_threshold: float, planner: SubWindowPlanner
    ) -> None:
        high = [s for s in scored if s.motion_energy >= action_drop_threshold]
        scenic = [s for s in scored if s.motion_energy < 0.35]
        dynamic = [s for s in scored if 0.35 <= s.motion_energy < action_drop_threshold]
        self.pools = {
            "drop": high or scored,
            "scenic": scenic or scored,
            "dynamic": dynamic or scored,
            "default": scored,
        }
        self.cursors = {k: 0 for k in self.pools}
        self.planner = planner
        self.last: ScoredScene | None = None

    @staticmethod
    def pool_key(section: SongSection | None, has_sections: bool) -> str:
        if not has_sections or section is None:
            return "default"
        if section.section_type == SongSectionType.DROP or section.is_climax:
            return "drop"
        if section.section_type in (SongSectionType.INTRO, SongSectionType.OUTRO):
            return "scenic"
        return "dynamic"

    def _free(self, cand: ScoredScene) -> float:
        return self.planner.room(cand) / max(0.1, float(cand.speed or 1.0))

    def _take_fit(self, key: str, slot_sec: float) -> tuple[ScoredScene, float] | None:
        """Next scene of pool ``key`` (round-robin) with unused footage for the whole slot."""
        pool = self.pools[key]
        n = len(pool)
        start = self.cursors[key]
        fits: list[tuple[int, ScoredScene, float]] = []
        for k in range(n):
            cand = pool[(start + k) % n]
            free = self._free(cand)
            if free + _EPS >= slot_sec:
                fits.append((k, cand, free))
                if len(fits) == 2:
                    break
        if not fits:
            return None
        k, cand, free = fits[0]
        if cand is self.last and len(fits) > 1:
            k, cand, free = fits[1]
        self.cursors[key] = start + k + 1
        self.last = cand
        return cand, free

    def pick(self, key: str, slot_sec: float, min_slot_sec: float) -> tuple[ScoredScene, float]:
        """Return (scene, free timeline seconds).

        The section's pool goes first; once its footage is used up, any other scene with unused
        footage fills the slot (a long section with few matching scenes otherwise alternates two
        shots). Used footage is reset only when no scene can fill even the smallest slot (a repeat
        is then unavoidable); the scene with the most free footage is used when nothing fits.
        """
        for attempt in range(2):
            for k in (key, "default") if key != "default" else (key,):
                hit = self._take_fit(k, slot_sec)
                if hit is not None:
                    return hit
            pool = self.pools[key]
            if self._free(max(pool, key=self._free)) + _EPS < min_slot_sec:
                pool = self.pools["default"]
            best = max(pool, key=self._free)
            best_free = self._free(best)
            if attempt == 0 and best_free + _EPS < min_slot_sec:
                self.planner.reset_epoch()
                continue
            self.cursors[key] += 1
            self.last = best
            return best, best_free
        raise AssertionError("unreachable")  # pragma: no cover


@dataclasses.dataclass
class CadenceSlot:
    """One allocated timeline slot: where it sits, which scene fills it and from which in-point."""

    start: float
    end: float
    scene: ScoredScene
    window: tuple[float, float] | None = None  # source (in, out); None = scene start


def allocate_cadence_slots(
    config: PipelineConfig,
    scored_scenes: list[ScoredScene],
    cuts: list[float],
    bar_grid: MusicalBarGrid,
    sections: Sequence[SongSection],
    allowed_bars: Sequence[float],
    planner: SubWindowPlanner,
    *,
    seed: int = 7,
) -> list[CadenceSlot]:
    """Scene-aware refinement of cadence ``cuts`` (bar/half-bar points, frame snapped).

    For each slot a scene is chosen; the slot is then adapted to it without leaving the bar grid:
    shrunk (split at a half-bar point, the remainder becomes a new slot) when the scene cannot fill
    it, grown over following same-section slots for long shots, and sized 1-2 bars for mid-length
    scenes and 0.5-1 bar for short ones, within ``allowed_bars`` where possible. Each slot also
    gets its source in-point (an unused, smooth sub-window for long scenes).
    """
    fps = config.fps
    cuts = list(cuts)
    ordered = sorted(sections, key=lambda s: s.start_time)
    bar_len = _bar_length(bar_grid)
    allowed = sorted({float(a) for a in allowed_bars if a > 0}) or [1.0]
    min_slot = min(allowed) * bar_len
    gp = sorted({snap_to_frame(p, fps) for p in grid_points(bar_grid, cuts[-1])})
    rng = random.Random(seed ^ 0xA3)
    picker = _ScenePicker(scored_scenes, config.action_drop_threshold, planner)
    threshold = config.extended_shot_threshold_sec
    out: list[CadenceSlot] = []

    def split_point(a: float, b: float, limit: float, want: Sequence[float]) -> float | None:
        """Largest grid point inside (a, b), at most ``limit`` after ``a``, preferring ``want`` bars."""
        cand = [
            g
            for g in gp
            if a + 0.5 * bar_len - 0.05 <= g <= a + limit + 0.02 and a + _EPS < g < b - _EPS
        ]
        if not cand:
            return None
        pref = [g for g in cand if any(abs((g - a) / bar_len - m) <= 0.06 for m in want)]
        return max(pref or cand)

    half = bar_len / 2.0
    lens: list[int] = []  # finished slot lengths in half-bars

    def retime(i: int, a: float, b: float, sec: SongSection | None, free: float) -> float:
        """Change slot ``i`` to another allowed length so three equal slots never follow each other."""
        cur = (b - a) / bar_len
        extra = [h / 2.0 for h in range(1, 17) if h / 2.0 not in allowed]  # last resort: any half-bar
        order = sorted(allowed, key=lambda m: (abs(m - cur), -m)) + sorted(extra, key=lambda m: (abs(m - cur), -m))
        for length in order:
            if abs(length - cur) <= 0.06:
                continue
            target = a + length * bar_len
            near = [g for g in gp if abs(g - target) <= 0.05]
            if not near or length * bar_len > free + 0.02:
                continue
            g = near[0]
            if g > cuts[-1] - _EPS or abs(g - b) < _EPS:
                continue
            if g > b and (
                length * bar_len > config.max_scene_len_sec + 0.02
                or _section_at(ordered, g - 0.01) is not sec
                or _section_at(ordered, a + 0.01) is not sec
            ):
                continue
            j = i + 1
            while j < len(cuts) and cuts[j] < g - _EPS:
                j += 1
            if j < len(cuts) and abs(cuts[j] - g) <= _EPS:
                del cuts[i + 1 : j]
            else:
                del cuts[i + 1 : j]
                cuts.insert(i + 1, g)
            return g
        return b

    i = 0
    while i < len(cuts) - 1:
        a, b = cuts[i], cuts[i + 1]
        sec = _section_at(ordered, (a + b) / 2.0)
        cand, free = picker.pick(picker.pool_key(sec, bool(ordered)), b - a, min_slot)
        spd = max(0.1, float(cand.speed or 1.0))
        total = cand.scene.duration / spd
        lo, hi = _preferred_bars(total, threshold)
        d = [m for m in allowed if lo - _EPS <= m <= hi + _EPS]
        if not d:
            d = [min(allowed, key=lambda m: min(abs(m - lo), abs(m - hi)))]

        # Grow: a long shot in a too-short slot absorbs following slots of the same section.
        if (b - a) / bar_len < min(d) - 0.06 and total >= 4.0:
            cap = min(free, config.max_scene_len_sec, max(allowed) * bar_len)
            options: list[int] = []
            for j in range(i + 2, len(cuts)):
                c = cuts[j]
                if _section_at(ordered, (a + c) / 2.0) is not sec or c - a > cap + 0.02:
                    break
                if any(abs((c - a) / bar_len - m) <= 0.06 for m in d):
                    options.append(j)
            if options:
                j = options[rng.randrange(len(options))]
                b = cuts[j]
                del cuts[i + 1 : j]

        # Shrink: the scene cannot fill the slot, or a mid/short scene should not linger.
        slot = b - a
        limit: float | None = None
        want: Sequence[float] = allowed
        if slot > free + 0.02:
            limit = free
        elif total < threshold and slot / bar_len > max(d) + 0.06:
            limit, want = max(d) * bar_len, d
        if limit is not None:
            g = split_point(a, b, limit, want)
            if g is not None:
                cuts.insert(i + 1, g)
                b = g

        if len(lens) >= 2 and lens[-1] == lens[-2] == round((b - a) / half):
            b = retime(i, a, b, sec, free)
        lens.append(round((b - a) / half))

        window = planner.choose(cand, (b - a) * spd)
        out.append(CadenceSlot(a, b, cand, window))
        i += 1
    return out


def _teaser_cuts(
    config: PipelineConfig,
    bar_grid: MusicalBarGrid,
    t0: float,
    end: float,
    num_scenes: int,
) -> list[float] | None:
    """Cut times ``[t0, ..., teaser_end]`` for the hook: 3-5 uneven clips of whole beats.

    ``teaser_end`` is the half-bar point nearest ``teaser_duration`` (at most one bar beyond it and
    within 25 % of the timeline); every inner cut sits on a beat. All times are frame snapped.
    """
    fps = config.fps
    bar_len = _bar_length(bar_grid)
    bpb = max(2, int(bar_grid.beats_per_bar))
    beat = bar_len / bpb
    bars = [b for b in bar_grid.bar_times if b >= 0.0]
    if not bars or beat <= 0:
        return None
    beats = [b + k * beat for b in bars for k in range(bpb) if b + k * beat < end - _EPS]
    half_bars = [b + k * bar_len / 2.0 for b in bars for k in range(2) if b + k * bar_len / 2.0 < end - _EPS]
    target = t0 + config.teaser_duration
    limit = min(target + bar_len, t0 + 0.25 * (end - t0))
    ends = [h for h in half_bars if t0 + 3 * beat - 0.05 <= h <= limit + _EPS]
    if not ends:
        return None
    teaser_end = min(ends, key=lambda h: (abs(h - target), h))
    inner = [b for b in beats if t0 + _EPS < b <= teaser_end + 0.02]
    n_beats = len(inner)
    if n_beats < 3:
        return None
    n = min(5, max(3, num_scenes), n_beats)
    lens = [n_beats // n + (1 if i < n_beats % n else 0) for i in range(n)]
    for i in range(0, n - 1, 2):  # vary by a beat: e.g. 1,2,1,2,1
        if lens[i] > 1 and lens[i] == lens[i + 1]:
            lens[i] -= 1
            lens[i + 1] += 1
    cuts = [snap_to_frame(t0, fps)]
    acc = 0
    for length in lens:
        acc += length
        cuts.append(snap_to_frame(inner[acc - 1], fps))
    cuts[-1] = snap_to_frame(teaser_end, fps)
    if any(b - a <= _EPS for a, b in zip(cuts, cuts[1:], strict=False)):
        return None
    return cuts


def _cadence_plan(
    config: PipelineConfig,
    bar_grid: MusicalBarGrid | None,
    sections: Sequence[SongSection],
    profile: CadenceProfile | str | None,
    seed: int | None,
    t0: float,
    end: float,
) -> tuple[list[float], float, tuple[float, ...]] | None:
    """Base cadence cuts for ``[t0, end]`` or ``None`` when the legacy beat path must be used."""
    if not config.use_cadence_engine or bar_grid is None:
        return None
    if bar_grid.confidence < MIN_GRID_CONFIDENCE or end - t0 <= 0:
        return None
    prof = coerce_profile(profile if profile is not None else config.cadence_profile)
    multiples = profile_multiples(prof, config)
    res = cadence_cuts_with_stats(
        bar_grid,
        sections,
        prof,
        multiples,
        end,
        config.fps,
        config.cadence_seed if seed is None else seed,
    )
    if res is None:
        return None
    cuts, err, _slots = res
    if t0 > _EPS:
        cuts = [snap_to_frame(t0, config.fps)] + [c for c in cuts if c > t0 + _EPS]
    if len(cuts) < 2:
        return None
    return cuts, err, multiples


def build_edit_timeline(
    config: PipelineConfig,
    scored_scenes: list[ScoredScene],
    cut_timestamps: list[float],
    *,
    soundtrack_path: Path | None = None,
    sections: list[SongSection] | None = None,
    include_teaser: bool | None = None,
    name: str = "AutoEdit_Sequence",
    variant_key: str = "master",
    soundtrack_in: float = 0.0,
    bar_grid: MusicalBarGrid | None = None,
    profile: CadenceProfile | str | None = None,
    cadence_seed: int | None = None,
    analysis_cache: AnalysisCache | None = None,
) -> EditTimeline:
    """Choose a scene for every slot (plus optional teaser hook) at frame precision.

    With ``config.use_cadence_engine`` and a trustworthy ``bar_grid`` the slots come from the
    cadence engine (bar/half-bar cuts, scene-aware slot lengths, sub-windows of long shots).
    ``cut_timestamps`` then only supplies the start and end of the window; otherwise (no grid, low
    confidence, engine off) the beat-slot path runs exactly as before.
    """
    if not scored_scenes:
        raise ValueError("Cannot build timeline with empty scored_scenes list.")
    if len(cut_timestamps) < 2:
        raise ValueError("cut_timestamps must have at least 2 points (start and end).")

    fps = config.fps
    t0 = cut_timestamps[0]
    total_duration = cut_timestamps[-1] - t0
    should_teaser = include_teaser if include_teaser is not None else config.enable_teaser
    has_teaser = should_teaser and (total_duration >= config.teaser_duration + 4.0)

    plan = _cadence_plan(config, bar_grid, sections or [], profile, cadence_seed, t0, cut_timestamps[-1])
    planner: SubWindowPlanner | None = None
    if plan is not None:
        base_cuts, snap_err, multiples = plan
        if analysis_cache is None and config.enable_cache:
            analysis_cache = AnalysisCache(config)
        planner = SubWindowPlanner(config.extended_shot_threshold_sec, cache=analysis_cache)
        rate = frame_rate_fraction(fps)
        base_t0 = base_cuts[0]

        def frame_at(t: float) -> int:
            return _round_half_up(Fraction(t - base_t0).limit_denominator(10**9) * rate)

        t0 = base_t0
    else:

        def frame_at(t: float) -> int:
            return int(round((t - t0) * fps))

    clips: list[EditClip] = []
    teaser_end = t0 + config.teaser_duration
    if plan is not None and has_teaser:
        assert bar_grid is not None
        t_cuts = _teaser_cuts(config, bar_grid, t0, cut_timestamps[-1], len(scored_scenes))
        if t_cuts is None:
            has_teaser = False
        else:
            teaser_end = t_cuts[-1]
    if has_teaser:
        teaser_frames = frame_at(teaser_end)
        if plan is not None:
            bounds = [frame_at(t) for t in t_cuts]  # whole-beat cuts on the grid
            num_teaser = len(bounds) - 1
        else:
            num_teaser = min(5, max(3, len(scored_scenes)))
            bounds = [round(teaser_frames * j / num_teaser) for j in range(num_teaser + 1)]
        for j in range(num_teaser):
            cand = scored_scenes[j % len(scored_scenes)]
            t_frames = bounds[j + 1] - bounds[j]
            t_in: float | None = None
            if planner is not None:
                spd = max(0.1, float(cand.speed or 1.0))
                need = t_frames / fps * spd
                if cand.scene.duration > need + 0.04:
                    got = planner.choose(cand, need)
                    t_in = got[0] if got else None
            clips.append(
                clip_from_scene(
                    cand,
                    t_frames,
                    is_teaser=True,
                    label=f"TEASER_{cand.scene.clip_path.stem}_{j:02d}",
                    source_in=t_in,
                )
            )
        main_cuts = [teaser_end] + [t for t in (base_cuts if plan is not None else cut_timestamps) if t > teaser_end]
    else:
        main_cuts = list(base_cuts) if plan is not None else list(cut_timestamps)

    if plan is not None and planner is not None and bar_grid is not None and len(main_cuts) >= 2:
        slots = allocate_cadence_slots(
            config,
            scored_scenes,
            main_cuts,
            bar_grid,
            sections or [],
            multiples,
            planner,
            seed=config.cadence_seed if cadence_seed is None else cadence_seed,
        )
    else:
        main_scenes = select_scenes_for_cuts(
            scored_scenes=scored_scenes,
            cut_timestamps=main_cuts,
            sections=sections,
            action_drop_threshold=config.action_drop_threshold,
        )
        slots = [
            CadenceSlot(main_cuts[i], main_cuts[i + 1], main_scenes[i])
            for i in range(len(main_cuts) - 1)
        ]

    prev: ScoredScene | None = None
    for i, slot in enumerate(slots):
        frames = frame_at(slot.end) - frame_at(slot.start)
        if frames <= 0:
            continue
        cand = slot.scene
        clip = clip_from_scene(
            cand,
            frames,
            label=f"{cand.scene.clip_path.stem}_beat_{i:03d}",
            source_in=slot.window[0] if slot.window else None,
        )

        if config.enable_ken_burns:
            motion = assign_ambient_motion(
                cand, clip_index=i, motion_threshold=config.ken_burns_motion_threshold
            )
            clip.ken_burns = motion
        if prev is not None and config.enable_exposure_guard:
            _dist, is_gap = evaluate_cut_continuity(
                prev, cand, threshold=config.exposure_guard_threshold
            )
            if is_gap:
                clip.transition_in_frames = int(config.cross_dissolve_frames)
        clips.append(clip)
        prev = cand

    return EditTimeline(
        name=name,
        fps=fps,
        canvas_width=config.canvas_width,
        canvas_height=config.canvas_height,
        clips=clips,
        soundtrack_path=Path(soundtrack_path) if soundtrack_path else None,
        soundtrack_in=soundtrack_in,
        sections=list(sections or []),
        variant_key=variant_key,
        kind="music",
        bar_grid=bar_grid if plan is not None else None,
        cut_snap_error_max_sec=snap_err if plan is not None else None,
    )


def _round_half_up(x: Fraction) -> int:
    return math.floor(x + Fraction(1, 2))


def retarget_timeline(edit_tl: EditTimeline, settings: DeliverSettings) -> EditTimeline:
    """Re-express ``edit_tl`` on the Deliver canvas / frame rate, keeping cut points in seconds.

    - Clip lengths are converted by duration. Boundaries are rounded cumulatively (not clip by
      clip) so the total stays within one frame of the original and the cut grid does not
      drift; every clip keeps at least one frame.
    - ``available``, ``source_in``, ``speed``, soundtrack and sections are untouched.
    - Crop windows are kept as-is (no new data is invented) but flagged with
      ``crop_needs_recompute`` when the canvas aspect ratio changed.
    - Returns ``edit_tl`` itself (identity) when canvas and frame rate already match.
    """
    old_rate = frame_rate_fraction(edit_tl.fps)
    new_rate = settings.fps_fraction
    same_canvas = (edit_tl.canvas_width, edit_tl.canvas_height) == settings.canvas
    if same_canvas and old_rate == new_rate:
        return edit_tl

    aspect_changed = (
        edit_tl.canvas_width * settings.canvas_height != edit_tl.canvas_height * settings.canvas_width
    )
    ratio = new_rate / old_rate
    old_boundary = 0  # cumulative frames at the old rate
    new_boundary = 0  # cumulative frames already assigned at the new rate
    clips: list[EditClip] = []
    for c in edit_tl.clips:
        old_boundary += c.frames
        target = _round_half_up(Fraction(old_boundary) * ratio)
        frames = max(1, target - new_boundary)
        new_boundary += frames
        transition = c.transition_in_frames
        if transition > 0:
            transition = max(1, _round_half_up(Fraction(transition) * ratio))
        clips.append(
            dataclasses.replace(
                c,
                frames=frames,
                transition_in_frames=transition,
                crop_needs_recompute=c.crop_needs_recompute
                or (aspect_changed and c.crop_window is not None),
            )
        )
    return dataclasses.replace(
        edit_tl,
        fps=settings.fps,
        canvas_width=settings.canvas_width,
        canvas_height=settings.canvas_height,
        clips=clips,
    )


_DEFECT_COLORS = {
    DefectType.CAMERA_SHAKE: "RED",
    DefectType.WHIP_PAN: "RED",
    DefectType.HEAD_TAIL_WOBBLE: "ORANGE",
}


def build_rough_cut_edit_timeline(
    config: PipelineConfig,
    cleaned_segments: list[CleanedSegment],
    name: str = "RoughCut_Assembly",
    variant_key: str = "master",
) -> EditTimeline:
    """Chronological string-out of golden takes, keeping camera audio (no soundtrack).

    The staged workflow stores it under ``variant_key="rough_cut"``; the standalone
    ``workflow_mode="rough_cut"`` keeps the historical ``master`` key.
    """
    if not cleaned_segments:
        raise ValueError("Cannot build rough cut timeline with empty cleaned_segments list.")
    fps = config.fps
    ordered = sorted(
        cleaned_segments, key=lambda s: (natural_sort_key(s.clip_path), s.source_start_time)
    )
    clips: list[EditClip] = []
    for i, seg in enumerate(ordered):
        markers = [
            {
                "name": f"Pruned: {d.defect_type.value}",
                "color": _DEFECT_COLORS.get(d.defect_type, "YELLOW"),
                "comment": d.description,
            }
            for d in seg.defects_removed
        ]
        clips.append(
            EditClip(
                source_path=seg.clip_path,
                source_in=seg.source_start_time,
                frames=max(1, int(round(seg.duration * fps))),
                available=max(0.04, seg.duration),
                label=f"{seg.clip_path.stem}_take_{i + 1:03d}",
                markers=markers,
                original_path=seg.scene.original_path,
                scene_start=seg.source_start_time,
                scene_end=seg.source_end_time,
            )
        )
    return EditTimeline(
        name=name,
        fps=fps,
        canvas_width=config.canvas_width,
        canvas_height=config.canvas_height,
        clips=clips,
        soundtrack_path=None,
        variant_key=variant_key,
        kind="rough_cut",
    )


def scene_key(path: Path | str, start: float) -> tuple[str, float]:
    """Stable identity of a scene: resolved source path + start time (10 ms precision)."""
    return (str(Path(path).resolve()), round(float(start), 2))


def clip_scene_key(clip: EditClip) -> tuple[str, float]:
    """Identity of the scene a clip came from (sub-window clips match by ``scene_start``)."""
    start = clip.scene_start if clip.scene_start is not None else clip.source_in
    return scene_key(clip.source_path, start)


def apply_scene_curation(
    edit_tl: EditTimeline,
    included: list[ScoredScene],
    excluded: list[ScoredScene],
) -> tuple[EditTimeline, int]:
    """Replace clips that come from excluded scenes with included ones.

    Cut points (the beat grid) and every manual edit on other clips are preserved: each
    affected clip keeps its length and is re-filled with the next included scene that is
    long enough (round-robin). Returns (new timeline, number of clips replaced).

    A ``rough_cut`` timeline is a chronological string-out of golden takes: clips of excluded
    scenes are simply dropped (nothing is refilled, the rest closes up). ``replaced`` then counts
    the dropped clips.
    """
    excluded_keys = {scene_key(s.scene.clip_path, s.scene.start_time) for s in excluded}
    hit = [c for c in edit_tl.clips if clip_scene_key(c) in excluded_keys]
    if not hit:
        return edit_tl, 0
    if edit_tl.kind == "rough_cut":
        kept = [c for c in edit_tl.clips if clip_scene_key(c) not in excluded_keys]
        if not kept:
            raise ValueError("Every scene is excluded - include at least one scene to keep the rough cut.")
        return dataclasses.replace(edit_tl, clips=kept), len(hit)
    pool = [
        s for s in included if scene_key(s.scene.clip_path, s.scene.start_time) not in excluded_keys
    ]
    if not pool:
        raise ValueError("Every scene is excluded - include at least one scene to rebuild the edit.")

    fps = edit_tl.fps
    cursor = 0
    clips: list[EditClip] = []
    replaced = 0
    for c in edit_tl.clips:
        if clip_scene_key(c) not in excluded_keys:
            clips.append(c)
            continue
        need = c.frames / fps
        pick = None
        for k in range(len(pool)):
            cand = pool[(cursor + k) % len(pool)]
            if cand.scene.duration / max(0.1, float(cand.speed or 1.0)) + 1e-6 >= need:
                pick, cursor = cand, cursor + k + 1
                break
        if pick is None:  # nothing long enough: take the longest, renderer holds the last frame
            pick = max(pool, key=lambda s: s.scene.duration / max(0.1, float(s.speed or 1.0)))
            cursor += 1
        new = clip_from_scene(pick, c.frames, is_teaser=c.is_teaser, label=c.label)
        new.transition_in_frames = c.transition_in_frames
        clips.append(new)
        replaced += 1
    return dataclasses.replace(edit_tl, clips=clips), replaced

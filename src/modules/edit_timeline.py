"""Edit timeline synthesis: analysed scenes + beat cuts -> frame-exact EditTimeline.

This is the ONLY place where scenes are chosen for beat slots. The resulting
EditTimeline is what the GUI shows and edits, what the project file stores, and
what Deliver renders / exports, so the master video always matches the timeline.
"""

import dataclasses
import logging
import math
from fractions import Fraction
from pathlib import Path

from src.core.config import PipelineConfig
from src.core.ontology import natural_sort_key
from src.core.timebase import frame_rate_fraction
from src.core.types import (
    CleanedSegment,
    DefectType,
    DeliverSettings,
    EditClip,
    EditTimeline,
    ScoredScene,
    SongSection,
    SongSectionType,
)
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


def clip_from_scene(sc: ScoredScene, frames: int, *, is_teaser: bool = False, label: str = "") -> EditClip:
    """Place a scored scene on the timeline for ``frames`` output frames.

    When the scene was cut from a render proxy (VFR->CFR cache file) the clip remembers the
    user's original media (``SceneCut.original_path``) so NLE exports can relink to it.
    """
    return EditClip(
        original_path=sc.scene.original_path,
        source_path=sc.scene.clip_path,
        source_in=sc.scene.start_time,
        frames=max(1, int(frames)),
        available=max(0.04, sc.scene.duration),
        speed=max(0.1, float(sc.speed or 1.0)),
        crop_window=sc.crop_window,
        ken_burns=(sc.metadata or {}).get("ken_burns"),
        is_teaser=is_teaser,
        label=label or sc.scene.clip_path.stem,
    )


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
) -> EditTimeline:
    """Choose a scene for every beat slot (plus optional teaser hook) at frame precision."""
    if not scored_scenes:
        raise ValueError("Cannot build timeline with empty scored_scenes list.")
    if len(cut_timestamps) < 2:
        raise ValueError("cut_timestamps must have at least 2 points (start and end).")

    fps = config.fps
    t0 = cut_timestamps[0]
    total_duration = cut_timestamps[-1] - t0
    should_teaser = include_teaser if include_teaser is not None else config.enable_teaser
    has_teaser = should_teaser and (total_duration >= config.teaser_duration + 4.0)

    def frame_at(t: float) -> int:
        return int(round((t - t0) * fps))

    clips: list[EditClip] = []
    if has_teaser:
        teaser_end = t0 + config.teaser_duration
        teaser_frames = frame_at(teaser_end)
        num_teaser = min(5, max(3, len(scored_scenes)))
        bounds = [round(teaser_frames * j / num_teaser) for j in range(num_teaser + 1)]
        for j in range(num_teaser):
            cand = scored_scenes[j % len(scored_scenes)]
            clips.append(
                clip_from_scene(
                    cand,
                    bounds[j + 1] - bounds[j],
                    is_teaser=True,
                    label=f"TEASER_{cand.scene.clip_path.stem}_{j:02d}",
                )
            )
        main_cuts = [teaser_end] + [t for t in cut_timestamps if t > teaser_end]
    else:
        main_cuts = list(cut_timestamps)

    main_scenes = select_scenes_for_cuts(
        scored_scenes=scored_scenes,
        cut_timestamps=main_cuts,
        sections=sections,
        action_drop_threshold=config.action_drop_threshold,
    )
    prev: ScoredScene | None = None
    for i in range(len(main_cuts) - 1):
        frames = frame_at(main_cuts[i + 1]) - frame_at(main_cuts[i])
        if frames <= 0:
            continue
        cand = main_scenes[i]
        clip = clip_from_scene(cand, frames, label=f"{cand.scene.clip_path.stem}_beat_{i:03d}")

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
) -> EditTimeline:
    """Chronological string-out of golden takes, keeping camera audio (no soundtrack)."""
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
            )
        )
    return EditTimeline(
        name=name,
        fps=fps,
        canvas_width=config.canvas_width,
        canvas_height=config.canvas_height,
        clips=clips,
        soundtrack_path=None,
        variant_key="master",
        kind="rough_cut",
    )


def scene_key(path: Path | str, start: float) -> tuple[str, float]:
    """Stable identity of a scene: resolved source path + start time (10 ms precision)."""
    return (str(Path(path).resolve()), round(float(start), 2))


def apply_scene_curation(
    edit_tl: EditTimeline,
    included: list[ScoredScene],
    excluded: list[ScoredScene],
) -> tuple[EditTimeline, int]:
    """Replace clips that come from excluded scenes with included ones.

    Cut points (the beat grid) and every manual edit on other clips are preserved: each
    affected clip keeps its length and is re-filled with the next included scene that is
    long enough (round-robin). Returns (new timeline, number of clips replaced).
    """
    excluded_keys = {scene_key(s.scene.clip_path, s.scene.start_time) for s in excluded}
    hit = [c for c in edit_tl.clips if scene_key(c.source_path, c.source_in) in excluded_keys]
    if not hit:
        return edit_tl, 0
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
        if scene_key(c.source_path, c.source_in) not in excluded_keys:
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

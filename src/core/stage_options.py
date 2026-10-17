"""Shared option handling for the staged workflow (CLI ``run`` and the MCP ``execute_pipeline_job``)."""

from typing import Final, Literal

from src.core.config import PipelineConfig
from src.core.genre_presets import GENRE_PRESETS, ROUGH_CUT_PRESET
from src.core.types import CadenceProfile

STAGES: Final[tuple[str, ...]] = ("rough_cut", "sequence", "all")
CADENCE_NAMES: Final[tuple[str, ...]] = tuple(p.value for p in CadenceProfile)
GENRE_KEYS: Final[tuple[str, ...]] = (*GENRE_PRESETS.keys(), ROUGH_CUT_PRESET.key)

StageName = Literal["rough_cut", "sequence", "all"]


def apply_staged_options(
    cfg: PipelineConfig,
    *,
    genre: str | None = None,
    cadence: str | None = None,
    rough_cut: bool | None = None,
) -> None:
    """Apply ``genre`` (preset key), ``cadence`` (profile name) and ``rough_cut`` to ``cfg``.

    The genre drives pacing only: the canvas / frame rate chosen via the preset are kept. An explicit
    ``cadence`` beats the genre's profile (and its bar multiples). ``rough_cut`` maps to
    ``run_rough_cut_in_all`` (used by ``stage="all"``). Unknown names raise ``ValueError``.
    """
    if genre is not None:
        key = genre.strip()
        if key not in GENRE_KEYS:
            raise ValueError(f"Unknown genre '{genre}'. Available: {', '.join(GENRE_KEYS)}")
        canvas = (cfg.preset_name, cfg.canvas_width, cfg.canvas_height, cfg.fps)
        preset = ROUGH_CUT_PRESET if key == ROUGH_CUT_PRESET.key else GENRE_PRESETS[key]
        preset.apply(cfg)
        cfg.preset_name, cfg.canvas_width, cfg.canvas_height, cfg.fps = canvas
    if cadence is not None:
        name = cadence.strip().lower()
        if name not in CADENCE_NAMES:
            raise ValueError(f"Unknown cadence '{cadence}'. Available: {', '.join(CADENCE_NAMES)}")
        cfg.cadence_profile = name
        cfg.cadence_multiples = None
    if rough_cut is not None:
        cfg.run_rough_cut_in_all = bool(rough_cut)


# Pacing knobs of a genre that no GUI panel exposes: the GUI re-applies them from the chosen genre
# whenever it builds a config (the panel-backed ones - speed ramp, teaser, reframing, ... - are reloaded
# into their panels when the genre is picked).
GENRE_PACING_FIELDS: Final[tuple[str, ...]] = (
    "min_scene_len_sec",
    "min_beat_interval",
    "standard_min_beat_interval",
    "climax_min_beat_interval",
    "min_speed_multiplier",
    "action_drop_threshold",
)


def apply_genre_pacing(cfg: PipelineConfig, genre: str) -> None:
    """Copy only the panel-less pacing fields of preset ``genre`` into ``cfg`` (unknown key: ``ValueError``)."""
    key = genre.strip()
    if key not in GENRE_KEYS:
        raise ValueError(f"Unknown genre '{genre}'. Available: {', '.join(GENRE_KEYS)}")
    preset = ROUGH_CUT_PRESET if key == ROUGH_CUT_PRESET.key else GENRE_PRESETS[key]
    for name in GENRE_PACING_FIELDS:
        setattr(cfg, name, getattr(preset, name))

"""Advanced pipeline configuration options and overrides for curate and assemble stages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from src.core.config import PipelineConfig

ADVANCED_OPTIONS: dict[str, dict[str, Any]] = {
    # curate stage
    "min_take_duration_sec": {
        "stages": {"curate"},
        "type": "float",
        "min": 0.5,
        "max": 10.0,
    },
    "max_scene_len_sec": {
        "stages": {"curate"},
        "type": "float",
        "min": 2.0,
        "max": 120.0,
    },
    "enable_shake_pruning": {
        "stages": {"curate"},
        "type": "bool",
    },
    "enable_focus_guard": {
        "stages": {"curate"},
        "type": "bool",
    },
    "enable_occlusion_guard": {
        "stages": {"curate"},
        "type": "bool",
    },
    "enable_exposure_clipping_guard": {
        "stages": {"curate"},
        "type": "bool",
    },
    "enable_head_tail_trim": {
        "stages": {"curate"},
        "type": "bool",
    },
    "enable_dead_air_detection": {
        "stages": {"curate"},
        "type": "bool",
    },
    # assemble stage
    "enable_smart_reframing": {
        "stages": {"assemble"},
        "type": "bool",
    },
    "enable_ken_burns": {
        "stages": {"assemble"},
        "type": "bool",
    },
    "enable_speed_ramping": {
        "stages": {"assemble"},
        "type": "bool",
    },
    "enable_teaser": {
        "stages": {"assemble"},
        "type": "bool",
    },
    "watermark_enabled": {
        "stages": {"assemble"},
        "type": "bool",
    },
    "teaser_duration": {
        "stages": {"assemble"},
        "type": "float",
        "min": 1.0,
        "max": 30.0,
    },
    "duration_mode": {
        "stages": {"assemble"},
        "type": "enum",
        "values": ["fixed", "footage_driven", "full_track"],
    },
}


def apply_config_overrides(
    cfg: PipelineConfig,
    overrides: Mapping[str, Any] | None,
    stage: Literal["curate", "assemble"],
) -> PipelineConfig:
    """Validate and apply option overrides for a specific pipeline stage.

    Returns a new PipelineConfig copy with overrides applied.
    Raises ValueError on invalid keys, disallowed stages, type errors, or out-of-range values.
    """
    if overrides is None or len(overrides) == 0:
        return cfg

    if not isinstance(overrides, Mapping):
        raise ValueError(f"overrides must be a mapping, got {type(overrides).__name__}")

    updates: dict[str, Any] = {}
    for key, val in overrides.items():
        if key not in ADVANCED_OPTIONS:
            raise ValueError(f"Unknown option: {key!r}")
        spec = ADVANCED_OPTIONS[key]
        if stage not in spec["stages"]:
            raise ValueError(f"Option {key!r} is not allowed for stage {stage!r}")

        opt_type = spec["type"]
        if opt_type == "bool":
            if not isinstance(val, bool):
                raise ValueError(f"Option {key!r} must be a bool, got {type(val).__name__}: {val!r}")
            updates[key] = val
        elif opt_type == "float":
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(
                    f"Option {key!r} must be a float, got {type(val).__name__}: {val!r}"
                )
            f_val = float(val)
            min_val = spec.get("min")
            max_val = spec.get("max")
            if min_val is not None and f_val < min_val:
                raise ValueError(f"Option {key!r} value {f_val} is less than min {min_val}")
            if max_val is not None and f_val > max_val:
                raise ValueError(f"Option {key!r} value {f_val} is greater than max {max_val}")
            updates[key] = f_val
        elif opt_type == "enum":
            allowed_values = spec.get("values", [])
            if val not in allowed_values:
                raise ValueError(
                    f"Option {key!r} value {val!r} is not in allowed values: {allowed_values}"
                )
            updates[key] = val
        else:
            raise ValueError(f"Unsupported option type {opt_type!r} for key {key!r}")

    return cfg.model_copy(update=updates)


def describe_options() -> dict[str, list[dict[str, Any]]]:
    """Return dictionary describing available advanced options per stage."""
    default_cfg = PipelineConfig()
    result: dict[str, list[dict[str, Any]]] = {"curate": [], "assemble": []}

    for key, spec in ADVANCED_OPTIONS.items():
        item: dict[str, Any] = {
            "key": key,
            "type": spec["type"],
            "default": getattr(default_cfg, key),
        }
        if "min" in spec:
            item["min"] = spec["min"]
        if "max" in spec:
            item["max"] = spec["max"]
        if "values" in spec:
            item["values"] = list(spec["values"])

        for stage in spec["stages"]:
            if stage in result:
                result[stage].append(dict(item))

    return result

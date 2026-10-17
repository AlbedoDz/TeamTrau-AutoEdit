"""Phase 22 step 1: DeliverSettings, frame-rate table, presets, retarget_timeline, persistence."""

import json
import os
from fractions import Fraction
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.core.config import DEFAULT_PRESET_NAME, PRESETS, PipelineConfig, frame_rate_fraction
from src.core.project_io import (
    deliver_settings_from_dict,
    deliver_settings_to_dict,
    edit_timeline_from_dict,
    edit_timeline_to_dict,
)
from src.core.timebase import is_ntsc_rate
from src.core.types import CropWindow, DeliverSettings, EditClip, EditTimeline
from src.modules import fcpxml_exporter
from src.modules.edit_timeline import retarget_timeline
from src.ui.controllers.project_manager import (
    PROJECT_SCHEMA_VERSION,
    ProjectData,
    ProjectManager,
)

# ---------------------------------------------------------------- frame_rate_fraction


@pytest.mark.parametrize(
    ("fps", "expected"),
    [
        (23.976, Fraction(24000, 1001)),
        (24000 / 1001, Fraction(24000, 1001)),
        (29.97, Fraction(30000, 1001)),
        (47.952, Fraction(48000, 1001)),
        (59.94, Fraction(60000, 1001)),
        (119.88, Fraction(120000, 1001)),
        (24.0, Fraction(24)),
        (25.0, Fraction(25)),
        (30.0, Fraction(30)),
        (48.0, Fraction(48)),
        (50, Fraction(50)),
        (60.0, Fraction(60)),
        (120.0, Fraction(120)),
        (Fraction(30000, 1001), Fraction(30000, 1001)),
    ],
)
def test_frame_rate_fraction_table(fps, expected):
    assert frame_rate_fraction(fps) == expected


def test_frame_rate_fraction_rejects_non_positive():
    with pytest.raises(ValueError, match="positive"):
        frame_rate_fraction(0)


def test_is_ntsc_rate():
    assert is_ntsc_rate(29.97) and is_ntsc_rate(23.976) and is_ntsc_rate(59.94)
    assert not is_ntsc_rate(30.0) and not is_ntsc_rate(25.0)


def test_fcpxml_exporter_uses_shared_frame_rate():
    assert fcpxml_exporter.frame_rate is frame_rate_fraction
    assert fcpxml_exporter.frame_rate(119.88) == Fraction(120000, 1001)


# ---------------------------------------------------------------- DeliverSettings


def _settings(**kw) -> DeliverSettings:
    base = {"canvas_width": 1920, "canvas_height": 1080, "fps": 30.0}
    base.update(kw)
    return DeliverSettings(**base)


def test_deliver_settings_defaults():
    s = _settings()
    assert (s.fit_mode, s.color, s.audio_rate, s.audio_channels) == ("auto", "rec709", 48000, 2)
    assert (s.master, s.subtitles, s.watermark_in_nle, s.relink_original) == ("hevc", "none", False, True)
    assert s.variants == ["active"]
    assert s.fcpxml_version == "1.8"
    assert s.canvas == (1920, 1080)
    assert s.fps_fraction == Fraction(30)
    assert _settings(fps=29.97).fps_fraction == Fraction(30000, 1001)


def test_deliver_settings_variants_are_not_shared():
    a, b = _settings(), _settings()
    a.variants.append("all")
    assert b.variants == ["active"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"canvas_width": 1921}, "canvas_width must be even"),
        ({"canvas_height": 1081}, "canvas_height must be even"),
        ({"canvas_width": 0}, "canvas_width must be a positive integer"),
        ({"canvas_height": -2}, "canvas_height must be a positive integer"),
        ({"fps": 0}, "fps must be a positive number"),
        ({"fps": -24.0}, "fps must be a positive number"),
        ({"audio_rate": 22050}, "audio_rate must be one of"),
        ({"audio_channels": 6}, "audio_channels must be 1 or 2"),
        ({"fit_mode": "zoom"}, "fit_mode must be one of"),
        ({"color": "rec601"}, "color must be one of"),
        ({"master": "prores_4444"}, "master must be one of"),
        ({"subtitles": "vtt"}, "subtitles must be one of"),
        ({"variants": []}, "variants must be a non-empty list"),
        ({"fcpxml_version": ""}, "fcpxml_version"),
    ],
)
def test_deliver_settings_validation(kwargs, message):
    with pytest.raises(ValueError, match=message):
        _settings(**kwargs)


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_deliver_settings_accepts_audio_rates(rate):
    assert _settings(audio_rate=rate).audio_rate == rate


def test_deliver_settings_validate_after_mutation():
    s = _settings()
    s.canvas_width = 1001
    with pytest.raises(ValueError, match="even"):
        s.validate()


def test_deliver_settings_from_timeline():
    tl = EditTimeline("T", 59.94, 1080, 1920, [])
    s = DeliverSettings.from_timeline(tl, PipelineConfig())
    assert s.canvas == (1080, 1920)
    assert s.fps == 59.94
    assert s.fps_fraction == Fraction(60000, 1001)
    assert s.master == "hevc" and s.subtitles == "none"
    burn = DeliverSettings.from_timeline(tl, PipelineConfig(burn_hardsubs=True))
    assert burn.subtitles == "burn"
    assert DeliverSettings.from_timeline(tl, None, master="prores_hq").master == "prores_hq"


# ---------------------------------------------------------------- presets


NEW_PRESETS = {
    "1080p23.976": (1920, 1080, 23.976),
    "1080p29.97": (1920, 1080, 29.97),
    "1080p59.94": (1920, 1080, 59.94),
    "4k23.976": (3840, 2160, 23.976),
    "4k29.97": (3840, 2160, 29.97),
    "4k59.94": (3840, 2160, 59.94),
    "portrait_2k": (1440, 2560, 30.0),
    "portrait_4k": (2160, 3840, 30.0),
    "square_1080": (1080, 1080, 30.0),
    "4x5_1080x1350": (1080, 1350, 30.0),
    "ultrawide_2560x1080": (2560, 1080, 30.0),
    "dci_2k_2048x1080": (2048, 1080, 24.0),
}
OLD_PRESETS = (
    "1080p30", "1080p60", "1080p24", "4k30", "4k60", "4k24",
    "social_vertical", "social_vertical_60", "cinema_4k",
)


def test_preset_table_contains_old_and_new():
    for name in OLD_PRESETS:
        assert name in PRESETS
    for name, (w, h, fps) in NEW_PRESETS.items():
        assert (PRESETS[name].width, PRESETS[name].height, PRESETS[name].fps) == (w, h, fps)
        assert PRESETS[name].name == name
    assert len(PRESETS) == len(OLD_PRESETS) + len(NEW_PRESETS)
    assert DEFAULT_PRESET_NAME == "1080p30"


def test_every_preset_is_a_valid_deliver_canvas():
    for name, p in PRESETS.items():
        s = DeliverSettings(canvas_width=p.width, canvas_height=p.height, fps=p.fps)
        assert s.fps_fraction > 0, name
        cfg = PipelineConfig.from_preset(name)
        assert (cfg.canvas_width, cfg.canvas_height, cfg.fps) == (p.width, p.height, p.fps)


def test_ntsc_presets_map_to_1001():
    for name in ("1080p23.976", "1080p29.97", "1080p59.94", "4k23.976", "4k29.97", "4k59.94"):
        assert is_ntsc_rate(PRESETS[name].fps), name


# ---------------------------------------------------------------- retarget_timeline


def _clip(frames: int, **kw) -> EditClip:
    return EditClip(Path("a.mp4"), kw.pop("source_in", 1.5), frames, kw.pop("available", 20.0), **kw)


def _timeline(frames: list[int], fps: float = 30.0, canvas=(1920, 1080), **kw) -> EditTimeline:
    return EditTimeline("T", fps, canvas[0], canvas[1], [_clip(f, **kw) for f in frames])


def test_retarget_identity_when_canvas_and_fps_match():
    tl = _timeline([30, 45])
    assert retarget_timeline(tl, _settings()) is tl
    # float label of the same exact rate is still identity
    ntsc = _timeline([30], fps=29.97)
    assert retarget_timeline(ntsc, _settings(fps=30000 / 1001)) is ntsc


def test_retarget_changes_canvas_and_fps_only():
    tl = _timeline([30, 45], speed=2.0, available=7.0, source_in=3.25)
    tl.soundtrack_path = Path("music.wav")
    tl.soundtrack_in = 4.0
    out = retarget_timeline(tl, _settings(canvas_width=3840, canvas_height=2160, fps=60.0))
    assert (out.canvas_width, out.canvas_height, out.fps) == (3840, 2160, 60.0)
    assert [c.frames for c in out.clips] == [60, 90]
    for c in out.clips:
        assert (c.available, c.source_in, c.speed) == (7.0, 3.25, 2.0)
    assert out.soundtrack_path == Path("music.wav") and out.soundtrack_in == 4.0
    # the input is untouched
    assert (tl.fps, [c.frames for c in tl.clips]) == (30.0, [30, 45])


@pytest.mark.parametrize(
    ("src_fps", "dst_fps"),
    [(30.0, 29.97), (30.0, 23.976), (24.0, 29.97), (25.0, 30.0), (29.97, 24.0), (60.0, 23.976), (23.976, 59.94)],
)
def test_retarget_total_within_one_frame_and_cut_grid_does_not_drift(src_fps, dst_fps):
    lengths = [7, 13, 22, 31, 9, 40, 17, 5, 28, 11] * 8
    tl = _timeline(lengths, fps=src_fps)
    out = retarget_timeline(tl, _settings(fps=dst_fps))
    new_rate = frame_rate_fraction(dst_fps)
    old_rate = frame_rate_fraction(src_fps)
    assert abs(out.duration - tl.duration) <= 1.0 / dst_fps + 1e-9
    assert all(c.frames >= 1 for c in out.clips)
    old_acc = new_acc = 0
    for old_c, new_c in zip(tl.clips, out.clips, strict=True):
        old_acc += old_c.frames
        new_acc += new_c.frames
        exact = Fraction(old_acc) / old_rate * new_rate
        # every cut stays within half a frame of the exact (cumulative) time: no accumulated drift
        assert abs(new_acc - exact) <= Fraction(1, 2)


def test_retarget_min_one_frame_per_clip():
    tl = _timeline([1, 1, 1, 1, 60], fps=60.0)
    out = retarget_timeline(tl, _settings(fps=23.976))
    assert all(c.frames >= 1 for c in out.clips)
    assert abs(out.duration - tl.duration) <= 2 / 23.976


def test_retarget_flags_crop_when_aspect_changes_but_keeps_window():
    crop = CropWindow(100, 0, 608, 1080)
    tl = _timeline([30, 30])
    tl.clips[0].crop_window = crop
    portrait = retarget_timeline(tl, _settings(canvas_width=1080, canvas_height=1920))
    assert portrait.clips[0].crop_window == crop
    assert portrait.clips[0].crop_needs_recompute is True
    assert portrait.clips[1].crop_needs_recompute is False  # nothing to recompute without a crop

    same_aspect = retarget_timeline(tl, _settings(canvas_width=3840, canvas_height=2160, fps=60.0))
    assert same_aspect.clips[0].crop_needs_recompute is False
    assert same_aspect.clips[0].crop_window == crop


def test_retarget_scales_transition_frames():
    tl = _timeline([30, 30])
    tl.clips[1].transition_in_frames = 10
    out = retarget_timeline(tl, _settings(fps=60.0))
    assert out.clips[1].transition_in_frames == 20
    assert out.clips[0].transition_in_frames == 0


# ---------------------------------------------------------------- persistence


def test_deliver_settings_dict_roundtrip():
    s = _settings(
        canvas_width=1080, canvas_height=1350, fps=23.976, fit_mode="fill", color="rec2020_hlg",
        audio_rate=44100, audio_channels=1, master="prores_hq", subtitles="burn",
        watermark_in_nle=True, relink_original=False, variants=["all"], fcpxml_version="1.9",
    )
    raw = deliver_settings_to_dict(s)
    json.dumps(raw)  # JSON-serializable
    assert deliver_settings_from_dict(raw) == s


def test_deliver_settings_from_dict_tolerates_missing_and_unknown_keys():
    s = deliver_settings_from_dict({"canvas_width": 1280, "canvas_height": 720, "future_key": 1})
    assert (s.canvas, s.fps, s.master) == ((1280, 720), 30.0, "hevc")
    with pytest.raises(ValueError):
        deliver_settings_from_dict({"canvas_width": 1281})


def test_project_schema_is_2_3_and_stores_deliver_settings(tmp_path: Path):
    assert PROJECT_SCHEMA_VERSION == "2.3.0"
    data = ProjectData(project_name="P")
    assert data.version == "2.3.0" and data.deliver_settings == {}
    assert data.get_deliver_settings() is None
    s = _settings(fps=29.97, master="dnxhr_hqx")
    data.deliver_settings = deliver_settings_to_dict(s)
    mgr = ProjectManager(recent_path=tmp_path / "recent.json")
    path = tmp_path / "p.tteproj"
    assert mgr.save_project(path, data)
    loaded = mgr.load_project(path)
    assert loaded is not None and loaded.version == "2.3.0"
    assert loaded.get_deliver_settings() == s


def test_v21_project_file_still_loads(tmp_path: Path):
    clip = {"source_path": str(tmp_path / "a.mp4"), "source_in": 0.0, "frames": 30, "available": 3.0}
    v21 = {
        "project_name": "Old",
        "version": "2.1.0",
        "preset_name": "1080p30",
        "edit_timelines": {
            "master": {"name": "T", "fps": 30.0, "canvas_width": 1920, "canvas_height": 1080, "clips": [clip]}
        },
        "active_timeline_key": "master",
    }
    path = tmp_path / "old.tteproj"
    path.write_text(json.dumps(v21), encoding="utf-8")
    loaded = ProjectManager(recent_path=tmp_path / "recent.json").load_project(path)
    assert loaded is not None
    assert loaded.version == "2.1.0"
    assert loaded.deliver_settings == {} and loaded.get_deliver_settings() is None
    tl = loaded.get_edit_timelines()["master"]
    assert tl.clips[0].original_path is None and tl.clips[0].crop_needs_recompute is False


def test_invalid_stored_deliver_settings_are_ignored():
    data = ProjectData.from_dict({"deliver_settings": {"canvas_width": 1921, "canvas_height": 1080}})
    assert data.get_deliver_settings() is None


def test_edit_clip_original_and_crop_flag_roundtrip():
    tl = _timeline([30])
    tl.clips[0].original_path = Path("orig.mov")
    tl.clips[0].crop_window = CropWindow(1, 2, 3, 4)
    tl.clips[0].crop_needs_recompute = True
    back = edit_timeline_from_dict(json.loads(json.dumps(edit_timeline_to_dict(tl))))
    assert back.clips[0].original_path == Path("orig.mov")
    assert back.clips[0].crop_needs_recompute is True
    assert back.clips[0].media_path() == Path("orig.mov")
    assert back.clips[0].media_path(relink_original=False) == Path("a.mp4")

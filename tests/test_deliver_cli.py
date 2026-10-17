"""Phase 22 step 7: `deliver` CLI options, option parsing and MCP argument plumbing (no ffmpeg needed)."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

import main as cli_module
from src.core.deliver_options import build_deliver_settings, parse_canvas, parse_fps, parse_variants
from src.core.project_io import deliver_settings_to_dict, edit_timeline_to_dict
from src.core.types import (
    UNSUPPORTED_RAW_EXTENSIONS,
    VIDEO_EXTENSIONS,
    DeliverResult,
    DeliverSettings,
    EditClip,
    EditTimeline,
)
from src.ui.controllers.project_manager import ProjectData


def _timeline(tmp_path: Path, key: str = "master", fps: float = 29.97) -> EditTimeline:
    clips = [EditClip(tmp_path / f"{n}.mp4", 0.0, 30, 3.0, label=n) for n in "ab"]
    return EditTimeline(key.title(), fps, 1920, 1080, clips=clips, variant_key=key)


def _project(tmp_path: Path, settings: DeliverSettings | None = None) -> Path:
    data = ProjectData(
        project_name="p",
        output_dir=str(tmp_path / "out"),
        edit_timelines={
            "master": edit_timeline_to_dict(_timeline(tmp_path)),
            "cinematic": edit_timeline_to_dict(_timeline(tmp_path, "cinematic")),
        },
        active_timeline_key="master",
        deliver_settings=deliver_settings_to_dict(settings) if settings else {},
    )
    path = tmp_path / "proj.tteproj"
    path.write_text(json.dumps(data.to_dict()), encoding="utf-8")
    return path


@pytest.fixture
def fake_adapter(monkeypatch):
    """Capture submit_deliver_job; resolve_variant_keys stays the real static method."""
    captured: dict = {}

    def submit(self, *args, **kwargs):
        captured["args"], captured["kwargs"] = args, kwargs
        out = Path(args[1])
        res = DeliverResult(
            success=True, output_dir=out, render_path=out / "proj_master.mov", encoder_used="prores_ks",
            duration=2.0, master_codec=kwargs["settings"].master, manifest_path=out / "deliver_manifest.json",
            subtitle_paths=[out / "proj_master.srt"],
        )
        handle = MagicMock()
        handle.result = res
        return handle

    monkeypatch.setattr(cli_module.StudioBackendAdapter, "submit_deliver_job", submit)
    return captured


def _invoke(*args: str):
    return CliRunner().invoke(cli_module.cli, ["deliver", *args])


# ----------------------------------------------------------------------------- plumbing
def test_deliver_defaults_come_from_the_timeline(tmp_path, fake_adapter):
    res = _invoke("--project", str(_project(tmp_path)))
    assert res.exit_code == 0, res.output
    s: DeliverSettings = fake_adapter["kwargs"]["settings"]
    assert (s.canvas, s.fps, s.master, s.variants, s.relink_original) == ((1920, 1080), 29.97, "hevc", ["active"], True)
    assert fake_adapter["kwargs"]["base_name"] == "proj_master"
    assert set(fake_adapter["kwargs"]["variant_timelines"]) == {"master", "cinematic"}


def test_deliver_defaults_come_from_saved_project_settings(tmp_path, fake_adapter):
    saved = DeliverSettings(canvas_width=1080, canvas_height=1920, fps=25.0, master="prores_hq", color="rec2020_hlg")
    res = _invoke("--project", str(_project(tmp_path, saved)))
    assert res.exit_code == 0, res.output
    assert fake_adapter["kwargs"]["settings"] == saved


def test_every_option_overrides_the_saved_settings(tmp_path, fake_adapter):
    saved = DeliverSettings(canvas_width=1080, canvas_height=1920, fps=25.0)
    res = _invoke(
        "--project", str(_project(tmp_path, saved)), "--canvas", "2560x1080", "--fps", "59.94",
        "--fit", "fill", "--color", "rec2020_pq", "--master", "dnxhr_hqx", "--subtitles", "ass",
        "--audio-rate", "44100", "--audio-channels", "1", "--variants", "master,cinematic",
        "--no-relink", "--watermark-in-nle",
    )
    assert res.exit_code == 0, res.output
    s = fake_adapter["kwargs"]["settings"]
    assert s == DeliverSettings(
        canvas_width=2560, canvas_height=1080, fps=59.94, fit_mode="fill", color="rec2020_pq",
        audio_rate=44100, audio_channels=1, master="dnxhr_hqx", subtitles="ass",
        watermark_in_nle=True, relink_original=False, variants=["master", "cinematic"],
    )
    # several variants -> files live in <key>/ subfolders, named after the project
    assert fake_adapter["kwargs"]["base_name"] == "proj"


def test_preset_option_sets_canvas_and_fps(tmp_path, fake_adapter):
    res = _invoke("--project", str(_project(tmp_path)), "--preset", "square_1080")
    assert res.exit_code == 0, res.output
    assert fake_adapter["kwargs"]["settings"].canvas == (1080, 1080)
    res = _invoke("--project", str(_project(tmp_path)), "--preset", "4k29.97", "--fps", "25")
    assert (fake_adapter["kwargs"]["settings"].canvas, fake_adapter["kwargs"]["settings"].fps) == ((3840, 2160), 25.0)


def test_variants_all_is_accepted_and_no_watermark_flag(tmp_path, fake_adapter):
    res = _invoke("--project", str(_project(tmp_path)), "--variants", "all", "--no-watermark-in-nle")
    assert res.exit_code == 0, res.output
    assert fake_adapter["kwargs"]["settings"].variants == ["all"]
    assert fake_adapter["kwargs"]["settings"].watermark_in_nle is False


def test_summary_names_master_codec_and_files(tmp_path, fake_adapter):
    res = _invoke("--project", str(_project(tmp_path)), "--master", "prores_422")
    assert res.exit_code == 0, res.output
    assert "Master (prores_422)" in res.output and "proj_master.mov" in res.output
    assert "deliver_manifest.json" in res.output and "proj_master.srt" in res.output
    assert "1920x1080 @ 29.97fps" in res.output


# ----------------------------------------------------------------------------- validation
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--canvas", "1920"], "canvas must look like"),
        (["--canvas", "1921x1080"], "must be even"),
        (["--canvas", "0x0"], "positive"),
        (["--preset", "nope"], "unknown preset"),
        (["--preset", "1080p30", "--canvas", "1280x720"], "either preset or canvas"),
        (["--fps", "abc"], "fps must be"),
        (["--fps", "-5"], "positive"),
        (["--fps", "30/0"], "fps must be"),
        (["--variants", "nonexistent"], "Unknown variant"),
        (["--format", "mp4,wav"], "Unknown format"),
    ],
)
def test_validation_errors_use_bad_parameter(tmp_path, fake_adapter, args, message):
    res = _invoke("--project", str(_project(tmp_path)), *args)
    assert res.exit_code == 2, res.output
    assert message in res.output
    assert "args" not in fake_adapter  # never submitted


@pytest.mark.parametrize(
    "args",
    [["--fit", "squash"], ["--color", "srgb"], ["--master", "mp3"], ["--subtitles", "vtt"],
     ["--audio-rate", "22050"], ["--audio-channels", "6"]],
)
def test_enum_options_reject_bad_values(tmp_path, fake_adapter, args):
    res = _invoke("--project", str(_project(tmp_path)), *args)
    assert res.exit_code == 2
    assert "Invalid value" in res.output


def test_run_command_stays_analysis_only():
    help_text = CliRunner().invoke(cli_module.cli, ["run", "--help"]).output
    for flag in ("--master", "--canvas", "--variants", "--watermark-in-nle"):
        assert flag not in help_text


# ----------------------------------------------------------------------------- option parsing
def test_parsers():
    assert parse_canvas(" 1080 X 1920 ") == (1080, 1920)
    assert parse_fps("30000/1001") == pytest.approx(29.97, abs=1e-3)
    assert parse_fps(25) == 25.0
    assert parse_variants("all, master") == ["all"]
    assert parse_variants("Active,cinematic") == ["active", "cinematic"]
    assert parse_variants("") is None and parse_variants(None) is None
    base = DeliverSettings(canvas_width=1920, canvas_height=1080, fps=30.0)
    assert build_deliver_settings(base) == base
    with pytest.raises(ValueError, match="audio-rate"):
        build_deliver_settings(base, audio_rate=1)


# ----------------------------------------------------------------------------- shared constants
def test_video_extensions_constant_is_shared():
    from src.ui.widgets.media_browser import SUPPORTED_VIDEO_EXTS

    expected = {
        ".mp4", ".mov", ".mkv", ".m4v", ".mxf", ".avi", ".mts", ".m2ts",
        ".webm", ".mpg", ".mpeg", ".wmv", ".3gp", ".flv",
    }
    assert set(VIDEO_EXTENSIONS) == expected
    assert all(e == e.lower() and e.startswith(".") for e in VIDEO_EXTENSIONS)
    assert SUPPORTED_VIDEO_EXTS is VIDEO_EXTENSIONS
    assert ".braw" in UNSUPPORTED_RAW_EXTENSIONS and not (UNSUPPORTED_RAW_EXTENSIONS & VIDEO_EXTENSIONS)


def test_ingest_scan_accepts_new_containers_and_reports_raw(tmp_path):
    from src.core.service import collect_video_files

    for name in ("a.mxf", "b.AVI", "c.mts", "d.webm", "e.braw", "notes.txt", "f.mp4"):
        (tmp_path / name).write_bytes(b"\x00")
    (tmp_path / "extra").mkdir()
    single = tmp_path / "extra" / "g.m2ts"
    single.write_bytes(b"\x00")
    found, skipped = collect_video_files([tmp_path, single])
    assert sorted(f.name for f in found) == ["a.mxf", "b.AVI", "c.mts", "d.webm", "f.mp4", "g.m2ts"]
    assert [f.name for f in skipped] == ["e.braw"]
    assert collect_video_files(tmp_path / "missing") == ([], [])

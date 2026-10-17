"""Unit tests for DaVinci Resolve Studio API dispatcher and fallback handling."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.config import PipelineConfig
from src.dispatchers import resolve_worker as rw
from src.dispatchers.resolve_dispatcher import ResolveDispatcher


def test_resolve_dispatcher_dry_run():
    """Verify dry-run mode validates parameters without requiring live DaVinci Resolve GUI."""
    cfg = PipelineConfig(canvas_width=1920, canvas_height=1080, fps=30.0)
    dispatcher = ResolveDispatcher(cfg)

    fcpxml = Path("e:/fake/timeline.fcpxml")
    media = [Path("e:/fake/clip1.mp4")]

    res = dispatcher.dispatch_project(
        fcpxml_path=fcpxml,
        source_media_paths=media,
        project_name="TestProject",
        dry_run=True,
    )

    assert res["dry_run"] is True
    assert res["connected"] is True
    assert res["imported_timeline"] is True
    assert "1920x1080@30.0fps" in res["canvas"]


def test_resolve_dispatcher_offline_fallback():
    """Verify clean error reporting when DaVinci Resolve is not running."""
    cfg = PipelineConfig()
    dispatcher = ResolveDispatcher(cfg)

    # Force connection failure (in-process and worker fallback)
    dispatcher.connect = MagicMock(return_value=False)
    dispatcher._dispatch_in_subprocess = MagicMock(return_value=None)

    res = dispatcher.dispatch_project(
        fcpxml_path=Path("timeline.fcpxml"),
        source_media_paths=[],
        dry_run=False,
    )

    assert res["connected"] is False
    assert "error" in res
    assert "not running" in res["error"].lower()


def test_resolve_dispatcher_mock_api_execution(tmp_path):
    """Verify project settings and timeline import calls on mock Resolve object."""
    cfg = PipelineConfig(canvas_width=3840, canvas_height=2160, fps=60.0)
    dispatcher = ResolveDispatcher(cfg)

    mock_app = MagicMock()
    mock_pm = MagicMock()
    mock_proj = MagicMock()
    mock_media_pool = MagicMock()

    mock_app.GetProjectManager.return_value = mock_pm
    mock_pm.GetCurrentProject.return_value = MagicMock(name="user_open_project")
    mock_pm.GetProjectListInCurrentFolder.return_value = ["Other"]
    mock_pm.CreateProject.return_value = mock_proj
    mock_proj.GetMediaPool.return_value = mock_media_pool
    mock_proj.AddRenderJob.return_value = "Job_999"

    dispatcher._resolve_app = mock_app
    dispatcher.connect = MagicMock(return_value=True)

    fcpxml = tmp_path / "mock.fcpxml"
    fcpxml.write_text("<fcpxml></fcpxml>", encoding="utf-8")

    res = dispatcher.dispatch_project(
        fcpxml_path=fcpxml,
        source_media_paths=[],
        project_name="MockAutoEdit",
        render_output_dir=tmp_path / "render",
        dry_run=False,
    )

    assert res["connected"] is True
    assert res["queued_render"] is True
    assert res["render_job_id"] == "Job_999"

    # Verify canvas settings were applied to Resolve project
    mock_proj.SetSetting.assert_any_call("timelineResolutionWidth", "3840")
    mock_proj.SetSetting.assert_any_call("timelineResolutionHeight", "2160")
    mock_proj.SetSetting.assert_any_call("timelineFrameRate", "60")

    # The user's open project is saved, never reused: a NEW project is created instead.
    mock_pm.SaveProject.assert_called_once()
    mock_pm.CreateProject.assert_called_once_with("MockAutoEdit")
    assert not mock_pm.GetCurrentProject.return_value.SetSetting.called


def test_frame_rate_setting_keeps_ntsc_rates():
    from src.dispatchers.resolve_worker import frame_rate_setting

    assert frame_rate_setting(29.97) == "29.97"
    assert frame_rate_setting(23.976) == "23.976"
    assert frame_rate_setting(30.0) == "30"


def test_unique_project_name_avoids_collision():
    from src.dispatchers.resolve_worker import unique_project_name

    assert unique_project_name(["A"], "B") == "B"
    assert unique_project_name(["B"], "B").startswith("B_")


def test_worker_fallback_skips_wrong_python_versions():
    cfg = PipelineConfig()
    d = ResolveDispatcher(cfg)
    d.connect = MagicMock(return_value=False)
    d._worker_interpreters = MagicMock(return_value=[["py", "-3.11"], ["py", "-3.13"]])  # type: ignore[method-assign]
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[:2])
        out = (
            'RESULT:{"fusion_import_failed": true, "error": "x"}'
            if cmd[1] == "-3.11"
            else 'RESULT:{"imported_timeline": true, "project_name": "P"}'
        )
        return MagicMock(stdout=out, returncode=0)

    import src.dispatchers.resolve_dispatcher as mod

    orig = mod.subprocess.run
    mod.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        res = d.dispatch_project(Path("t.fcpxml"), [], "P", dry_run=False)
    finally:
        mod.subprocess.run = orig  # type: ignore[assignment]
    assert calls == [["py", "-3.11"], ["py", "-3.13"]]
    assert res["imported_timeline"] is True and res["connected"] is True


# --------------------------------------------------------------------------------------
# Phase 22 step 4: timeline settings, sourceClipsPath, image scaling, render format/codec
# --------------------------------------------------------------------------------------


def _mock_resolve(settings=None, codecs=None):
    app, pm, proj, pool = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    app.GetProjectManager.return_value = pm
    pm.GetProjectListInCurrentFolder.return_value = []
    pm.CreateProject.return_value = proj
    proj.GetMediaPool.return_value = pool
    proj.AddRenderJob.return_value = "J1"
    if settings is not None:
        proj.GetSetting.return_value = settings
    if codecs is not None:
        proj.GetRenderCodecs.return_value = codecs
    return app, proj, pool


def _payload(tmp_path, **kw):
    clip = tmp_path / "footage" / "a.mp4"
    clip.parent.mkdir(exist_ok=True)
    clip.write_bytes(b"x")
    base = {
        "fcpxml": str(tmp_path / "t.fcpxml"),
        "media": [str(clip)],
        "project_name": "P",
        "width": 1080,
        "height": 1920,
        "fps": 29.97,
        "render_dir": None,
    }
    base.update(kw)
    return base


def test_run_dispatch_sets_timeline_and_import_options(tmp_path):
    app, proj, pool = _mock_resolve()
    info = rw.run_dispatch(app, _payload(tmp_path))
    proj.SetSetting.assert_any_call("timelineResolutionWidth", "1080")
    proj.SetSetting.assert_any_call("timelineResolutionHeight", "1920")
    proj.SetSetting.assert_any_call("timelineFrameRate", "29.97")
    proj.SetSetting.assert_any_call("timelineDropFrame", "0")
    opts = pool.ImportTimelineFromFile.call_args.args[1]
    assert opts["importSourceClips"] is True
    assert opts["sourceClipsPath"] == str(tmp_path / "footage")
    assert info["imported_timeline"] is True and info["fit_mode"] == "auto"


def test_import_retries_without_source_clips_path(tmp_path):
    app, _, pool = _mock_resolve()
    pool.ImportTimelineFromFile.side_effect = [None, MagicMock(name="timeline")]
    info = rw.run_dispatch(app, _payload(tmp_path))
    assert pool.ImportTimelineFromFile.call_count == 2
    assert "sourceClipsPath" not in pool.ImportTimelineFromFile.call_args.args[1]
    assert info["imported_timeline"] is True


@pytest.mark.parametrize(
    "fit,value",
    [
        ("fit", "Scale entire image to fit"),
        ("fill", "Scale full frame with crop"),
        ("stretch", "Stretch frame to all corners"),
        ("auto", "Scale entire image to fit"),
    ],
)
def test_fit_mode_maps_to_mismatched_resolution_setting(tmp_path, fit, value):
    app, proj, _ = _mock_resolve(settings={"timelineFrameRate": "24", "someMismatchedResolutionKey": "x"})
    proj.SetSetting.return_value = True
    info = rw.run_dispatch(app, _payload(tmp_path, fit_mode=fit))
    proj.SetSetting.assert_any_call("someMismatchedResolutionKey", value)  # discovered key wins
    assert info["image_scaling"] == {"someMismatchedResolutionKey": value}


def test_unexposed_settings_are_reported_not_fatal(tmp_path):
    app, proj, _ = _mock_resolve(settings={"timelineFrameRate": "24"})
    proj.SetSetting.return_value = False
    info = rw.run_dispatch(app, _payload(tmp_path))
    assert info["imported_timeline"] is True
    assert any("drop-frame" in w for w in info["warnings"])
    assert any("Mismatched" in w for w in info["warnings"])


def test_render_settings_follow_master(tmp_path):
    codecs = {"Apple ProRes 422 HQ": "ProRes422HQ", "Apple ProRes 422": "ProRes422"}
    app, proj, _ = _mock_resolve(codecs=codecs)
    info = rw.run_dispatch(app, _payload(tmp_path, render_dir=str(tmp_path / "out"), master="mov+prores"))
    proj.SetCurrentRenderFormatAndCodec.assert_called_once_with("mov", "ProRes422HQ")
    settings = proj.SetRenderSettings.call_args.args[0]
    assert settings["FrameRate"] == 29.97 and settings["TargetDir"] == str(tmp_path / "out")
    assert info["render_format"] == "mov" and info["queued_render"] is True


def test_default_master_is_hevc_mp4_and_missing_codec_warns(tmp_path):
    app, proj, _ = _mock_resolve(codecs={"H.265": "H265"})
    rw.run_dispatch(app, _payload(tmp_path, render_dir=str(tmp_path / "o")))
    proj.SetCurrentRenderFormatAndCodec.assert_called_once_with("mp4", "H265")
    app, proj, _ = _mock_resolve(codecs={"H.264": "H264"})
    info = rw.run_dispatch(app, _payload(tmp_path, render_dir=str(tmp_path / "o")))
    assert not proj.SetCurrentRenderFormatAndCodec.called
    assert any("not offered" in w for w in info["warnings"]) and info["queued_render"] is True


def test_source_clips_path_prefers_common_parent_never_drive_root(tmp_path):
    a, b = tmp_path / "x" / "a.mp4", tmp_path / "y" / "b.mp4"
    assert rw.source_clips_path([str(a), str(a)]) == str(tmp_path / "x")
    assert rw.source_clips_path([str(a), str(b)]) == str(tmp_path)
    assert rw.source_clips_path([]) is None


def test_dispatcher_passes_fit_color_master_to_worker(monkeypatch):
    import src.dispatchers.resolve_dispatcher as mod

    seen = {}
    monkeypatch.setattr(mod, "run_dispatch", lambda app, payload: seen.update(payload) or {"connected": True})
    d = ResolveDispatcher(PipelineConfig())
    d._resolve_app = MagicMock()
    d.dispatch_project(Path("t.fcpxml"), [], "P", fit_mode="fill", color="rec2020_hlg", master="h264")
    assert (seen["fit_mode"], seen["color"], seen["master"]) == ("fill", "rec2020_hlg", "h264")
    dry = d.dispatch_project(Path("t.fcpxml"), [], "P", dry_run=True, fit_mode="stretch")
    assert dry["fit_mode"] == "stretch" and dry["master"] == "hevc" and dry["dry_run"] is True

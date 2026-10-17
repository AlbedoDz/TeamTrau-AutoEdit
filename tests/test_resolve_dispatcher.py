"""Unit tests for DaVinci Resolve Studio API dispatcher and fallback handling."""

from pathlib import Path
from unittest.mock import MagicMock

from src.core.config import PipelineConfig
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

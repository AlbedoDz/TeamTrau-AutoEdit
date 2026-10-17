"""GUI tests for the Edit vs Deliver split: analysis never exports, Export delivers the live edit."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QMessageBox

from src.core.types import (
    DeliverResult,
    EditClip,
    EditTimeline,
    PipelineExecutionResult,
)
from src.ui.app import create_app
from src.ui.main_window import StudioMainWindow
from src.ui.widgets.export_panel import ExportPanelWidget

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


@pytest.fixture
def studio(qapp, tmp_path, monkeypatch):
    """StudioMainWindow with a mocked backend adapter and isolated recents."""
    monkeypatch.setattr(
        "src.ui.controllers.project_manager.DEFAULT_RECENT_PROJECTS_PATH", tmp_path / "recents.json"
    )
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *a, **k: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *a, **k: None)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question", lambda *a, **k: QMessageBox.StandardButton.Discard
    )
    window = StudioMainWindow()
    adapter = MagicMock()
    handle = MagicMock()
    handle.is_running.return_value = False
    handle.result = None  # keep the watcher thread from emitting signals
    adapter.submit_job.return_value = handle
    adapter.submit_deliver_job.return_value = handle
    window.adapter = adapter
    yield window, adapter, tmp_path
    window.close()


def _timeline(tmp_path: Path, key: str = "master", labels: tuple[str, ...] = ("a", "b", "c")) -> EditTimeline:
    clips = [
        EditClip(tmp_path / f"{lbl}.mp4", 0.0, 30 * (i + 1), 10.0, label=lbl)
        for i, lbl in enumerate(labels)
    ]
    return EditTimeline(
        name=key.title(), fps=30.0, canvas_width=1920, canvas_height=1080, clips=clips, variant_key=key
    )


def _analysis_result(timelines: dict[str, EditTimeline]) -> PipelineExecutionResult:
    return PipelineExecutionResult(
        success=True,
        total_duration=6.0,
        clip_count=3,
        scenes_detected=3,
        audio_bpm=120.0,
        sections_detected=0,
        fcpxml_path=None,
        premiere_xml_path=None,
        otio_path=None,
        capcut_draft_path=None,
        render_path=None,
        encoder_used=None,
        resolve_status=None,
        edit_timelines=timelines,
    )


def _setup_media(window: StudioMainWindow, tmp_path: Path) -> None:
    footage = tmp_path / "footage"
    footage.mkdir()
    (footage / "clip1.mp4").write_bytes(b"\x00" * 1024)
    soundtrack = tmp_path / "bgm.mp3"
    soundtrack.write_bytes(b"\x00" * 2048)
    window.media_browser.edit_input_dir.setText(str(footage))
    window.media_browser.edit_soundtrack.setText(str(soundtrack))
    window.media_browser._video_files = [footage / "clip1.mp4"]


def test_analysis_never_renders_or_exports(studio):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path)
    # Even with every export option ticked, analysis must not render or export NLE files.
    window.export_panel.chk_enable_render.setChecked(True)
    window.export_panel.chk_fcpxml.setChecked(True)

    window._on_start_pipeline()

    adapter.submit_job.assert_called_once()
    kwargs = adapter.submit_job.call_args.kwargs
    assert kwargs["no_render"] is True
    assert kwargs["export_nle"] is False
    assert kwargs["stage"] == "all"  # the classic entry point is Auto Run All
    adapter.submit_deliver_job.assert_not_called()


def test_export_disabled_until_timeline_exists(studio):
    window, _adapter, tmp_path = studio
    assert window.action_export.isEnabled() is False
    assert window.btn_run.isEnabled() is False
    assert window.action_render_preview.isEnabled() is False
    assert window.export_dialog.btn_run_export.isEnabled() is False

    window._on_job_finished(_analysis_result({"master": _timeline(tmp_path)}))

    assert window.action_export.isEnabled() is True
    assert window.btn_run.isEnabled() is True
    assert window.action_render_preview.isEnabled() is True
    assert window.export_dialog.btn_run_export.isEnabled() is True
    assert window.project_manager.is_dirty is True
    assert "● 3. Sequence" in window.lbl_workflow_steps.text()  # job finished (stage all) -> step 3
    assert "Timeline ready" in window.lbl_message.text()

    window._on_new_project()
    assert window.action_export.isEnabled() is False


def test_failed_analysis_keeps_export_disabled(studio):
    window, _adapter, _ = studio
    res = _analysis_result({})
    res.success = False
    res.error_message = "boom"
    window._on_job_finished(res)
    assert window.action_export.isEnabled() is False


def test_export_delivers_current_edit_without_reanalysis(studio):
    window, adapter, tmp_path = studio
    window._on_job_finished(_analysis_result({"master": _timeline(tmp_path)}))
    _setup_media(window, tmp_path)

    # User deletes the middle clip (ripple) before exporting
    canvas = window.timeline_view.canvas
    canvas.selected_clip = canvas.clips[1]
    window._on_delete_clip()

    window.export_panel.chk_enable_render.setChecked(True)
    window.export_panel.chk_fcpxml.setChecked(True)
    window.export_panel.chk_premiere.setChecked(False)
    window.export_panel.chk_otio.setChecked(False)
    window.export_panel.chk_capcut.setChecked(False)

    window._on_start_deliver()

    adapter.submit_job.assert_not_called()
    adapter.submit_deliver_job.assert_called_once()
    args, kwargs = adapter.submit_deliver_job.call_args
    sent: EditTimeline = args[0]
    assert [c.label for c in sent.clips] == ["a", "c"]
    assert sent.total_frames == 30 + 90
    assert kwargs["formats"] == {"mp4", "fcpxml"}
    assert kwargs["preview"] is False
    assert kwargs["run_async"] is True


def test_render_preview_submits_preview_job(studio):
    window, adapter, tmp_path = studio
    window._on_job_finished(_analysis_result({"master": _timeline(tmp_path)}))
    window._on_render_preview()
    adapter.submit_job.assert_not_called()
    kwargs = adapter.submit_deliver_job.call_args.kwargs
    assert kwargs["preview"] is True
    assert kwargs["formats"] == {"mp4"}


def test_export_without_timeline_does_nothing(studio):
    window, adapter, _ = studio
    window._on_start_deliver()
    adapter.submit_deliver_job.assert_not_called()


def test_deliver_finished_preview_loads_monitor(studio, monkeypatch):
    window, _adapter, tmp_path = studio
    window._on_job_finished(_analysis_result({"master": _timeline(tmp_path)}))
    mp4 = tmp_path / "preview.mp4"
    mp4.write_bytes(b"\x00")
    loaded: list[Path] = []
    monkeypatch.setattr(
        window.player_widget, "load_source_video", lambda p, *a, **k: loaded.append(Path(p)) or True
    )

    window._on_deliver_finished(DeliverResult(success=True, render_path=mp4, is_preview=True))

    assert loaded == [mp4]
    assert window.action_export.isEnabled() is True


def test_deliver_finished_export_reports_deliverables(studio):
    window, _adapter, tmp_path = studio
    window._on_job_finished(_analysis_result({"master": _timeline(tmp_path)}))
    fcpxml = tmp_path / "out.fcpxml"
    fcpxml.write_text("<fcpxml/>")
    window._on_deliver_finished(DeliverResult(success=True, fcpxml_path=fcpxml, duration=4.0))
    assert "out.fcpxml" in window.export_panel.lbl_deliv_fcpxml.text()
    assert "● 4. Deliver" in window.lbl_workflow_steps.text()  # export finished -> step 4
    # Saving after a Deliver result must not crash (DeliverResult has no clip_count)
    assert window._do_save(tmp_path / "p.tteproj") is True


def test_get_selected_formats_mapping(qapp):
    panel = ExportPanelWidget()
    boxes = {
        "mp4": panel.chk_enable_render,
        "fcpxml": panel.chk_fcpxml,
        "premiere": panel.chk_premiere,
        "otio": panel.chk_otio,
        "capcut": panel.chk_capcut,
    }
    for name, box in boxes.items():
        for other in boxes.values():
            other.setChecked(False)
        assert panel.get_selected_formats() == set()
        box.setChecked(True)
        assert panel.get_selected_formats() == {name}
    for box in boxes.values():
        box.setChecked(True)
    assert panel.get_selected_formats() == set(boxes)


def test_timeline_view_edit_roundtrip_and_variant_switch(qapp, tmp_path):
    from src.ui.widgets.timeline_view import TimelineViewWidget

    view = TimelineViewWidget()
    edited: list[bool] = []
    view.timeline_edited.connect(lambda: edited.append(True))
    master = _timeline(tmp_path, "master")
    other = _timeline(tmp_path, "cinematic", labels=("x", "y"))
    view.set_edit_timelines({"master": master, "cinematic": other}, active_key="master")
    assert edited == []  # loading is not an edit
    assert len(view.canvas.clips) == 3
    assert view.canvas.clips[1].start_time == pytest.approx(1.0)

    view.canvas.selected_clip = view.canvas.clips[0]
    assert view.delete_selected_clip() is not None
    assert edited == [True]
    # ripple: the next clip now starts at 0
    assert view.canvas.clips[0].start_time == 0.0
    assert [c.label for c in view.get_edit_timeline().clips] == ["b", "c"]

    # Switching keeps edits per timeline
    view.switch_variant("cinematic")
    assert len(view.canvas.clips) == 2
    view.switch_variant("master")
    assert [c.label for c in view.get_edit_timeline().clips] == ["b", "c"]
    assert [c.label for c in view.get_edit_timelines()["cinematic"].clips] == ["x", "y"]

    view.clear_timeline()
    assert view.get_edit_timelines() == {}

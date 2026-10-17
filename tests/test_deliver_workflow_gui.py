"""Phase 22 step 7: Deliver Settings in the Export panel / main window (headless Qt, mocked backend)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QMessageBox

from src.core.config import PRESETS
from src.core.deliver_check import SourceFormat, count_source_mismatch
from src.core.project_io import deliver_settings_to_dict, edit_timeline_to_dict
from src.core.types import (
    DeliverSettings,
    EditClip,
    EditTimeline,
    PipelineExecutionResult,
)
from src.ui.app import create_app
from src.ui.controllers.project_manager import ProjectData
from src.ui.main_window import StudioMainWindow
from src.ui.widgets.export_panel import ExportPanelWidget, aspect_ratio_label, preset_label

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


@pytest.fixture
def studio(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.ui.controllers.project_manager.DEFAULT_RECENT_PROJECTS_PATH", tmp_path / "recents.json"
    )
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *a, **k: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *a, **k: None)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question", lambda *a, **k: QMessageBox.StandardButton.Discard
    )
    # Never shell out to ffprobe from the panel in these tests.
    monkeypatch.setattr("src.ui.widgets.export_panel.probe_source_formats", lambda tl: {})
    window = StudioMainWindow()
    adapter = MagicMock()
    handle = MagicMock()
    handle.is_running.return_value = False
    handle.result = None
    adapter.submit_job.return_value = handle
    adapter.submit_deliver_job.return_value = handle
    window.adapter = adapter
    yield window, adapter, tmp_path
    window.close()


def _timeline(tmp_path: Path, key: str = "master", fps: float = 30.0, size=(1920, 1080)) -> EditTimeline:
    clips = [EditClip(tmp_path / f"{n}.mp4", 0.0, 30, 10.0, label=n) for n in ("a", "b", "c")]
    return EditTimeline(key.title(), fps, size[0], size[1], clips=clips, variant_key=key)


def _result(timelines: dict[str, EditTimeline]) -> PipelineExecutionResult:
    return PipelineExecutionResult(
        success=True, total_duration=3.0, clip_count=3, scenes_detected=3, audio_bpm=120.0,
        sections_detected=0, fcpxml_path=None, premiere_xml_path=None, otio_path=None,
        capcut_draft_path=None, render_path=None, encoder_used=None, resolve_status=None,
        edit_timelines=timelines,
    )


# ----------------------------------------------------------------------------- labelling
@pytest.mark.parametrize(
    ("w", "h", "label"),
    [
        (1920, 1080, "16:9"), (3840, 2160, "16:9"), (1080, 1920, "9:16"), (1440, 2560, "9:16"),
        (1080, 1080, "1:1"), (1080, 1350, "4:5"), (2560, 1080, "21:9"), (2048, 1080, "256:135 DCI"),
        (4096, 2160, "256:135 DCI"), (1000, 400, "2.50:1"),
    ],
)
def test_aspect_ratio_label(w, h, label):
    assert aspect_ratio_label(w, h) == label


def test_every_preset_is_labelled_by_its_real_aspect(studio):
    window, _adapter, _ = studio
    seen = {}
    for i in range(window.preset_combo.count()):
        seen[window.preset_combo.itemData(i)] = window.preset_combo.itemText(i)
    assert set(seen) == set(PRESETS)
    for name, preset in PRESETS.items():
        assert seen[name] == preset_label(preset)
        assert aspect_ratio_label(preset.width, preset.height) in seen[name]
    assert "(1:1 1080x1080)" in seen["square_1080"]
    assert "(4:5 1080x1350)" in seen["4x5_1080x1350"]
    assert "(21:9 2560x1080)" in seen["ultrawide_2560x1080"]
    assert "DCI" in seen["dci_2k_2048x1080"] and "DCI" in seen["cinema_4k"]
    assert "9:16" in seen["portrait_2k"] and "9:16" not in seen["square_1080"]


# ----------------------------------------------------------------------------- panel <-> settings
def test_panel_default_settings_match_dataclass_defaults(qapp):
    panel = ExportPanelWidget()
    base = DeliverSettings(canvas_width=1920, canvas_height=1080, fps=30.0)
    assert panel.get_deliver_settings(base) == base
    assert panel.combo_deliver_preset.currentData() == "1080p30"


def test_panel_settings_roundtrip(qapp):
    panel = ExportPanelWidget()
    wanted = DeliverSettings(
        canvas_width=1080, canvas_height=1350, fps=59.94, fit_mode="fill", color="rec2020_pq",
        audio_rate=96000, audio_channels=1, master="prores_hq", subtitles="burn",
        watermark_in_nle=True, relink_original=False, variants=["all"],
    )
    panel.load_deliver_settings(wanted)
    assert panel.get_deliver_settings(DeliverSettings(canvas_width=640, canvas_height=360, fps=24.0)) == wanted
    assert panel.combo_deliver_preset.currentData() == "custom"  # 4:5 @ 59.94 is not a preset
    assert panel.lbl_deliver_aspect.text() == "4:5"


def test_panel_roundtrip_with_picked_variants_and_odd_fps(qapp):
    panel = ExportPanelWidget()
    panel.set_available_variants(["master", "cinematic", "social"], "master")
    wanted = DeliverSettings(
        canvas_width=1280, canvas_height=720, fps=12.5, variants=["social", "master"], master="dnxhr_hqx"
    )
    panel.load_deliver_settings(wanted)
    got = panel.get_deliver_settings(wanted)
    assert got.fps == 12.5 and sorted(got.variants) == ["master", "social"]
    assert panel.combo_variant_target.currentIndex() == 2


def test_fps_choices_include_ntsc_and_high_rates(qapp):
    panel = ExportPanelWidget()
    rates = {round(float(panel.combo_deliver_fps.itemData(i)), 3) for i in range(panel.combo_deliver_fps.count())}
    assert {23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0, 119.88} <= rates


def test_preset_change_updates_size_and_fps_and_manual_edit_goes_custom(qapp):
    panel = ExportPanelWidget()
    changed: list[bool] = []
    panel.deliver_settings_changed.connect(lambda: changed.append(True))

    panel.combo_deliver_preset.setCurrentIndex(panel.combo_deliver_preset.findData("4k29.97"))
    assert (panel.spin_deliver_width.value(), panel.spin_deliver_height.value()) == (3840, 2160)
    assert panel._current_fps() == 29.97
    assert changed

    panel.combo_deliver_preset.setCurrentIndex(panel.combo_deliver_preset.findData("square_1080"))
    assert panel.lbl_deliver_aspect.text() == "1:1"

    panel.spin_deliver_width.setValue(1000)
    assert panel.combo_deliver_preset.currentData() == "custom"
    panel.spin_deliver_height.setValue(1001)  # odd sizes are bumped to the next even value
    assert panel.spin_deliver_height.value() == 1002
    s = panel.get_deliver_settings(DeliverSettings(canvas_width=640, canvas_height=360, fps=30.0))
    assert s.canvas == (1000, 1002)

    panel.spin_deliver_width.setValue(1920)
    panel.spin_deliver_height.setValue(1080)
    panel._select_fps(30.0)
    assert panel.combo_deliver_preset.currentData() == "1080p30"


def test_canvas_spin_range(qapp):
    panel = ExportPanelWidget()
    for spin in (panel.spin_deliver_width, panel.spin_deliver_height):
        assert (spin.minimum(), spin.maximum()) == (16, 16384)


def test_loading_settings_does_not_emit_change(qapp):
    panel = ExportPanelWidget()
    changed: list[bool] = []
    panel.deliver_settings_changed.connect(lambda: changed.append(True))
    panel.load_deliver_settings(DeliverSettings(canvas_width=1080, canvas_height=1920, fps=25.0, master="h264"))
    assert changed == []


# ----------------------------------------------------------------------------- mismatch warning
def test_mismatch_counts_scaled_and_conformed_clips(tmp_path):
    tl = _timeline(tmp_path)
    media = {c.media_path(True): None for c in tl.clips}
    paths = list(media)
    formats = {
        paths[0]: SourceFormat(1920, 1080, 30.0),  # exact match
        paths[1]: SourceFormat(3840, 2160, 30.0),  # scaled
        paths[2]: SourceFormat(1920, 1080, 29.97),  # conformed
    }
    st = DeliverSettings(canvas_width=1920, canvas_height=1080, fps=30.0)
    rep = count_source_mismatch(tl, formats, st)
    assert (rep.total, rep.scaled, rep.conformed, rep.affected) == (3, 1, 1, 2)
    assert "2 of 3 clips will be scaled/conformed" in rep.message
    # A vertical canvas scales everything; unknown files are skipped.
    vertical = DeliverSettings(canvas_width=1080, canvas_height=1920, fps=30.0)
    rep = count_source_mismatch(tl, {paths[0]: formats[paths[0]]}, vertical)
    assert (rep.total, rep.affected) == (1, 1)
    assert count_source_mismatch(tl, formats, DeliverSettings(canvas_width=3840, canvas_height=2160, fps=29.97)).affected == 3


def test_panel_shows_mismatch_warning_and_updates_with_canvas(qapp, tmp_path, monkeypatch):
    tl = _timeline(tmp_path)
    p = [c.media_path(True) for c in tl.clips]
    fake = {p[0]: SourceFormat(1920, 1080, 30.0), p[1]: SourceFormat(3840, 2160, 30.0), p[2]: SourceFormat(1920, 1080, 30.0)}
    monkeypatch.setattr("src.ui.widgets.export_panel.probe_source_formats", lambda t: fake)
    panel = ExportPanelWidget()
    panel.set_source_timeline(tl, background=False)
    assert panel.source_mismatch.affected == 1
    assert "1 of 3 clips" in panel.lbl_source_mismatch.text()
    assert not panel.lbl_source_mismatch.isHidden()

    panel.combo_deliver_preset.setCurrentIndex(panel.combo_deliver_preset.findData("social_vertical"))
    assert panel.source_mismatch.affected == 3  # a vertical canvas scales every 16:9 clip

    panel.combo_deliver_preset.setCurrentIndex(panel.combo_deliver_preset.findData("4k30"))
    assert panel.source_mismatch.affected == 2

    panel.set_source_timeline(None)
    assert panel.source_mismatch.affected == 0 and panel.lbl_source_mismatch.isHidden()


def test_panel_background_probe_delivers_result(qapp, tmp_path, monkeypatch):
    tl = _timeline(tmp_path)
    p = [c.media_path(True) for c in tl.clips]
    fake = {x: SourceFormat(640, 360, 24.0) for x in p}
    monkeypatch.setattr("src.ui.widgets.export_panel.probe_source_formats", lambda t: fake)
    panel = ExportPanelWidget()
    panel.set_source_timeline(tl, background=True)
    import time

    deadline = time.time() + 5
    while time.time() < deadline and not panel.source_mismatch.affected:
        qapp.processEvents()
        time.sleep(0.02)
    assert panel.source_mismatch.affected == 3


# ----------------------------------------------------------------------------- main window
def test_analysis_adopts_timeline_canvas_as_deliver_default(studio):
    window, _adapter, tmp_path = studio
    window._on_job_finished(_result({"master": _timeline(tmp_path, fps=25.0, size=(1080, 1920))}))
    panel = window.export_panel
    assert (panel.spin_deliver_width.value(), panel.spin_deliver_height.value()) == (1080, 1920)
    assert panel._current_fps() == 25.0
    assert panel.lbl_deliver_aspect.text() == "9:16"


def test_export_passes_deliver_settings_and_never_analyses(studio):
    window, adapter, tmp_path = studio
    window._on_job_finished(_result({"master": _timeline(tmp_path), "cinematic": _timeline(tmp_path, "cinematic")}))
    panel = window.export_panel
    panel.combo_deliver_preset.setCurrentIndex(panel.combo_deliver_preset.findData("1080p29.97"))
    panel.combo_deliver_master.setCurrentIndex(panel.combo_deliver_master.findData("prores_422"))
    panel.combo_deliver_fit.setCurrentIndex(panel.combo_deliver_fit.findData("fill"))
    panel.chk_deliver_relink.setChecked(False)
    panel.combo_variant_target.setCurrentIndex(1)  # all variants
    panel.chk_enable_render.setChecked(True)

    window._on_start_deliver()

    adapter.submit_job.assert_not_called()
    adapter.submit_deliver_job.assert_called_once()
    args, kwargs = adapter.submit_deliver_job.call_args
    settings: DeliverSettings = kwargs["settings"]
    assert settings.canvas == (1920, 1080) and settings.fps == 29.97
    assert (settings.master, settings.fit_mode, settings.relink_original) == ("prores_422", "fill", False)
    assert settings.variants == ["all"]
    assert set(kwargs["variant_timelines"]) == {"master", "cinematic"}
    assert kwargs["preview"] is False
    assert args[0].variant_key == "master"


def test_preview_passes_settings_but_only_mp4(studio):
    window, adapter, tmp_path = studio
    window._on_job_finished(_result({"master": _timeline(tmp_path)}))
    window.export_panel.combo_deliver_master.setCurrentIndex(window.export_panel.combo_deliver_master.findData("prores_hq"))
    window._on_render_preview()
    adapter.submit_job.assert_not_called()
    kwargs = adapter.submit_deliver_job.call_args.kwargs
    assert kwargs["preview"] is True and kwargs["formats"] == {"mp4"}
    assert kwargs["settings"].canvas == (1920, 1080)  # canvas aspect is kept; the service ignores the codec


def test_export_without_timeline_still_does_nothing(studio):
    window, adapter, _ = studio
    window._on_start_deliver()
    adapter.submit_deliver_job.assert_not_called()
    assert window.export_dialog.btn_run_export.isEnabled() is False


def test_all_variants_export_no_longer_prints_the_active_only_stub(studio):
    window, _adapter, tmp_path = studio
    window._on_job_finished(_result({"master": _timeline(tmp_path), "cinematic": _timeline(tmp_path, "cinematic")}))
    window.export_panel.combo_variant_target.setCurrentIndex(1)
    window._on_start_deliver()
    assert "active timeline only" not in window.console.toPlainText()


def test_project_save_and_open_restore_deliver_settings(studio):
    window, _adapter, tmp_path = studio
    window._on_job_finished(_result({"master": _timeline(tmp_path), "cinematic": _timeline(tmp_path, "cinematic")}))
    panel = window.export_panel
    panel.combo_deliver_preset.setCurrentIndex(panel.combo_deliver_preset.findData("square_1080"))
    panel.combo_deliver_master.setCurrentIndex(panel.combo_deliver_master.findData("dnxhr_hqx"))
    panel.combo_deliver_color.setCurrentIndex(panel.combo_deliver_color.findData("rec2020_hlg"))
    panel.combo_deliver_subtitles.setCurrentIndex(panel.combo_deliver_subtitles.findData("srt"))
    panel.chk_deliver_relink.setChecked(False)
    path = tmp_path / "deliver.tteproj"
    assert window._do_save(path) is True

    saved = ProjectData.from_dict(json.loads(path.read_text(encoding="utf-8")))
    stored = saved.get_deliver_settings()
    assert stored is not None
    assert (stored.canvas, stored.master, stored.color, stored.subtitles, stored.relink_original) == (
        (1080, 1080), "dnxhr_hqx", "rec2020_hlg", "srt", False,
    )

    window._on_new_project()
    assert panel.get_deliver_settings(stored).master == "hevc"  # new project resets the panel
    assert window._do_open(path) is True
    restored = panel.get_deliver_settings(DeliverSettings(canvas_width=640, canvas_height=360, fps=24.0))
    assert restored == stored


def test_project_without_stored_settings_uses_timeline_canvas(studio):
    window, _adapter, tmp_path = studio
    tl = _timeline(tmp_path, fps=24.0, size=(3840, 2160))
    data = ProjectData(
        project_name="legacy",
        edit_timelines={"master": edit_timeline_to_dict(tl)},
        active_timeline_key="master",
    )
    assert data.deliver_settings == {}
    path = tmp_path / "legacy.tteproj"
    path.write_text(json.dumps(data.to_dict()), encoding="utf-8")
    assert window._do_open(path) is True
    panel = window.export_panel
    assert (panel.spin_deliver_width.value(), panel.spin_deliver_height.value()) == (3840, 2160)
    assert panel._current_fps() == 24.0
    assert panel.combo_deliver_preset.currentData() == "4k24"


def test_changing_deliver_settings_marks_project_dirty(studio):
    window, _adapter, tmp_path = studio
    window._on_job_finished(_result({"master": _timeline(tmp_path)}))
    window._set_dirty(False)
    window.export_panel.combo_deliver_color.setCurrentIndex(window.export_panel.combo_deliver_color.findData("passthrough"))
    assert window.project_manager.is_dirty is True


def test_stored_settings_dict_is_what_project_io_writes(studio):
    window, _adapter, tmp_path = studio
    window._on_job_finished(_result({"master": _timeline(tmp_path)}))
    path = tmp_path / "x.tteproj"
    window._do_save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))["deliver_settings"]
    expected = deliver_settings_to_dict(
        window.export_panel.get_deliver_settings(DeliverSettings(canvas_width=1920, canvas_height=1080, fps=30.0))
    )
    assert raw == expected

"""GUI tests for the staged workflow (Phase 23 B3): 3 stage buttons, 4-step stepper, rough-cut review,
re-edit without re-ingest, badges, bar/phrase ruler, project round-trip, cancel per stage.

Most tests run against a mocked backend adapter; ``test_real_backend_*`` drive the REAL adapter on tiny
synthetic footage and a click-track soundtrack.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QPixmap, QRegion
from PySide6.QtWidgets import QMessageBox

from src.core.service import StudioBackendAdapter
from src.core.types import (
    BeatMarker,
    EditClip,
    EditTimeline,
    MusicalBarGrid,
    PipelineExecutionResult,
    SceneCut,
    ScoredScene,
)
from src.modules.audio_analyzer import AudioAnalysisResult
from src.ui.app import create_app
from src.ui.controllers.project_manager import ProjectData
from src.ui.main_window import ROUGH_CUT_READY_MESSAGE, StudioMainWindow
from src.ui.widgets.inspector_panel import estimate_rough_cut_minutes, rough_cut_estimate_text
from src.ui.widgets.scene_grid import SceneGridWidget, scene_badges
from src.ui.widgets.timeline_view import (
    TimelineViewWidget,
    clip_sub_window_text,
    compute_bar_lines,
    lock_badge_text,
    sort_variant_keys,
    variant_pill_label,
)

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


@pytest.fixture
def studio(qapp, tmp_path, monkeypatch):
    """StudioMainWindow with a mocked adapter and isolated recents."""
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


# ------------------------------------------------------------------ builders


def _scene(name: str, start: float, end: float, **meta) -> ScoredScene:
    cut = SceneCut(Path(name), start, end, int(start * 30), int(end * 30), end - start)
    return ScoredScene(cut, 100.0, 0.5, 0.6, metadata=dict(meta))


def _golden(trimmed: float = 0.0, defects: list[dict] | None = None, **extra) -> dict:
    return {
        "golden": {
            "source_in": 0.0,
            "source_out": 5.0,
            "defects": defects or [],
            "requires_deflicker": False,
            "original_scene_duration": 5.0 + trimmed,
            "trimmed_sec": trimmed,
        },
        **extra,
    }


def _timeline(key: str = "master", kind: str = "music", n: int = 3, **kw) -> EditTimeline:
    clips = [EditClip(Path(f"/x/{i}.mp4"), 0.0, 30 * (i + 1), 10.0, label=f"c{i}") for i in range(n)]
    return EditTimeline(
        name=key.title(), fps=30.0, canvas_width=1920, canvas_height=1080, clips=clips, variant_key=key,
        kind=kind, **kw,
    )  # type: ignore[arg-type]


def _result(timelines, stage="all", scenes=None, audio=None, **kw) -> PipelineExecutionResult:
    return PipelineExecutionResult(
        success=True, total_duration=6.0, clip_count=3, scenes_detected=3, audio_bpm=120.0,
        sections_detected=0, fcpxml_path=None, premiere_xml_path=None, otio_path=None,
        capcut_draft_path=None, render_path=None, encoder_used=None, resolve_status=None,
        edit_timelines=timelines, stage=stage, scored_scenes=scenes or [], audio_analysis=audio, **kw,
    )


def _grid(bpm: float = 120.0, bars: int = 40) -> MusicalBarGrid:
    bar = 4 * 60.0 / bpm
    times = [round(i * bar, 6) for i in range(bars)]
    return MusicalBarGrid(
        tempo_bpm=bpm, beats_per_bar=4, bar_times=times,
        phrase_2=times[::2], phrase_4=times[::4], phrase_8=times[::8], confidence=0.9,
    )


def _audio(bpm: float = 120.0) -> AudioAnalysisResult:
    grid = _grid(bpm)
    beats = [BeatMarker(i * 60.0 / bpm, i, 1.0, i % 4 == 0) for i in range(120)]
    return AudioAnalysisResult(
        tempo_bpm=bpm, duration=60.0, beats=beats, cut_timestamps=[0.0, 2.0, 4.0],
        sample_rate=22050, bar_grid=grid, cadence_cuts=[0.0, 4.0, 8.0],
    )


def _setup_media(window: StudioMainWindow, tmp_path: Path, music: bool = True, footage: bool = True) -> None:
    if footage:
        d = tmp_path / "footage"
        d.mkdir(exist_ok=True)
        (d / "clip1.mp4").write_bytes(b"\x00" * 1024)
        window.media_browser.edit_input_dir.setText(str(d))
        window.media_browser._video_files = [d / "clip1.mp4"]
    if music:
        bgm = tmp_path / "bgm.mp3"
        bgm.write_bytes(b"\x00" * 2048)
        window.media_browser.edit_soundtrack.setText(str(bgm))
    window._refresh_stage_controls()


# ------------------------------------------------------------------ buttons & validation


def test_three_stage_buttons_exist(studio):
    window, _, _ = studio
    insp = window.inspector_panel
    assert insp.btn_rough_cut.text() == "🔨 1. Run Rough Cut"
    assert insp.btn_sequence.text() == "✨ 2. Auto-Edit Sequence"
    assert insp.btn_analyze.text() == "⚡ Auto Run All"  # historic attribute name kept
    assert insp.btn_analyze.objectName() == "primaryButton"
    assert insp.chk_rough_cut_pruning.isChecked() is True  # GUI default ON
    for btn in (insp.btn_rough_cut, insp.btn_sequence, insp.btn_analyze):
        assert btn.toolTip()
    # Tabs were only ever appended to: order is unchanged
    assert window.tab_widget.tabText(0) == "Media Assets"
    assert window.tab_widget.tabText(1) == "Scene Quality & Review"


def test_button_states_follow_inputs(studio):
    window, _, tmp_path = studio
    insp = window.inspector_panel
    window._refresh_stage_controls()
    assert not insp.btn_rough_cut.isEnabled()
    assert not insp.btn_sequence.isEnabled()
    assert not insp.btn_analyze.isEnabled()
    assert "footage" in insp.btn_rough_cut.toolTip().lower() or "source" in insp.btn_rough_cut.toolTip().lower()

    _setup_media(window, tmp_path, music=False)  # footage only
    assert insp.btn_rough_cut.isEnabled()
    assert not insp.btn_sequence.isEnabled() and "soundtrack" in insp.btn_sequence.toolTip()
    assert not insp.btn_analyze.isEnabled() and "soundtrack" in insp.btn_analyze.toolTip()

    _setup_media(window, tmp_path, footage=False)  # + soundtrack
    assert insp.btn_rough_cut.isEnabled() and insp.btn_sequence.isEnabled() and insp.btn_analyze.isEnabled()


def test_run_all_without_music_only_in_rough_cut_workflow(studio):
    window, _, tmp_path = studio
    _setup_media(window, tmp_path, music=False)
    insp = window.inspector_panel
    assert not insp.btn_analyze.isEnabled()
    insp.combo_workflow.setCurrentIndex(insp.combo_workflow.findData("rough_cut"))
    assert insp.btn_analyze.isEnabled()


def test_sequence_needs_music_and_scenes_or_footage(studio):
    window, _, tmp_path = studio
    insp = window.inspector_panel
    _setup_media(window, tmp_path, footage=False)  # soundtrack only, nothing to edit
    assert not insp.btn_sequence.isEnabled()

    window.scene_grid.set_scenes([_scene("a.mp4", 0, 5, **_golden()), _scene("b.mp4", 0, 5, **_golden())])
    assert insp.btn_sequence.isEnabled()  # golden scenes from a rough cut are enough

    window.scene_grid.exclude_all()
    assert not insp.btn_sequence.isEnabled()
    assert "excluded" in insp.btn_sequence.toolTip().lower()
    window.scene_grid.include_all()
    assert insp.btn_sequence.isEnabled()


def test_buttons_disabled_while_job_runs_and_back_after(studio):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path)
    insp = window.inspector_panel
    running = MagicMock()
    running.is_running.return_value = True
    adapter.submit_job.return_value = running
    window._on_run_rough_cut()
    assert window._current_job is running
    assert not insp.btn_rough_cut.isEnabled()
    assert not insp.btn_sequence.isEnabled()
    assert not insp.btn_analyze.isEnabled()
    assert window.btn_stop.isEnabled()
    assert not window.btn_run.isEnabled()  # Export stays off while running

    running.is_running.return_value = False
    window._on_job_finished(_result({"rough_cut": _timeline("rough_cut", "rough_cut")}, stage="rough_cut"))
    assert insp.btn_rough_cut.isEnabled() and insp.btn_analyze.isEnabled()
    assert window.btn_run.isEnabled()  # a rough cut can be exported too


def test_rough_cut_time_estimate_label():
    assert estimate_rough_cut_minutes(600.0) == pytest.approx(10 * 25 / 60)
    assert "add footage" in rough_cut_estimate_text(0, 0, 0)
    assert "est. ~4.2 min" in rough_cut_estimate_text(600.0, 3, 3)
    assert "est. < 1 min" in rough_cut_estimate_text(30.0, 1, 1)
    assert "probing footage 1/3" in rough_cut_estimate_text(60.0, 1, 3)
    assert "est. ~21 min" in rough_cut_estimate_text(3000.0, 2, 2)


def test_estimate_label_follows_probed_durations(studio):
    from src.core.types import VideoMetadata

    window, _, tmp_path = studio
    _setup_media(window, tmp_path)
    clip = window.media_browser._video_files[0]
    window.media_browser._probed_metadata[clip] = VideoMetadata(clip, 1920, 1080, 30.0, 1200.0, 36000)
    window.media_browser.footage_probed.emit()
    assert "est. ~8.3 min" in window.inspector_panel.lbl_rough_estimate.text()


def test_media_changed_message_lists_runnable_stages(studio):
    window, _, tmp_path = studio
    _setup_media(window, tmp_path, music=False)
    window._on_media_changed()
    assert "Rough Cut" in window.lbl_message.text()
    assert "Auto Run All" not in window.lbl_message.text()
    _setup_media(window, tmp_path, footage=False)
    window._on_media_changed()
    assert all(n in window.lbl_message.text() for n in ("Rough Cut", "Auto-Edit Sequence", "Auto Run All"))


# ------------------------------------------------------------------ jobs submitted per stage


def test_rough_cut_job_kwargs_need_no_soundtrack(studio):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path, music=False)
    window._on_run_rough_cut()
    adapter.submit_job.assert_called_once()
    kw = adapter.submit_job.call_args.kwargs
    assert kw["stage"] == "rough_cut"
    assert kw["soundtrack"] is None
    assert kw["scenes"] is None
    assert kw["no_render"] is True and kw["export_nle"] is False
    assert "[Rough Cut]" in window.lbl_stage.text()


def test_sequence_reuses_scenes_without_ingest(studio):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path)
    rough = _timeline("rough_cut", "rough_cut")
    window._on_job_finished(
        _result({"rough_cut": rough}, stage="rough_cut",
                scenes=[_scene("a.mp4", 0, 5, **_golden()), _scene("b.mp4", 5, 9, **_golden(1.2)), _scene("c.mp4", 0, 4)])
    )
    window.scene_grid.set_scene_included(1, False)  # user rejects a take
    adapter.submit_job.reset_mock()

    window._on_run_sequence()

    adapter.submit_job.assert_called_once()
    kw = adapter.submit_job.call_args.kwargs
    assert kw["stage"] == "sequence"
    assert [sc.scene.clip_path.name for sc in kw["scenes"]] == ["a.mp4", "b.mp4", "c.mp4"]
    assert [sc.metadata["included"] for sc in kw["scenes"]] == [True, False, True]
    assert "rough_cut" in kw["existing_timelines"]
    assert kw["no_render"] is True and kw["export_nle"] is False
    # Re-use means the review grid and the rough cut stay on screen while the job runs
    assert len(window.scene_grid.get_all_scenes()) == 3
    assert "rough_cut" in window.timeline_view.get_edit_timelines()
    assert "[Sequence]" in window.lbl_stage.text()


def test_sequence_without_scenes_ingests_footage(studio):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path)
    window._on_run_sequence()
    kw = adapter.submit_job.call_args.kwargs
    assert kw["stage"] == "sequence" and kw["scenes"] is None and kw["existing_timelines"] is None


def test_sequence_blocked_without_soundtrack(studio):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path, music=False)
    window._on_run_sequence()
    adapter.submit_job.assert_not_called()


@pytest.mark.parametrize("pruning", [True, False])
def test_auto_run_all_passes_stage_all_and_pruning_flag(studio, pruning):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path)
    window.inspector_panel.chk_rough_cut_pruning.setChecked(pruning)
    window._on_run_full_auto()
    kw = adapter.submit_job.call_args.kwargs
    assert kw["stage"] == "all"
    assert kw["config"].run_rough_cut_in_all is pruning
    assert "[Auto Run]" in window.lbl_stage.text()


@pytest.mark.parametrize("slot", ["_on_run_rough_cut", "_on_run_sequence", "_on_run_full_auto", "_on_start_pipeline"])
def test_analysis_never_exports(studio, slot):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path)
    window.export_panel.chk_enable_render.setChecked(True)
    window.export_panel.chk_fcpxml.setChecked(True)
    getattr(window, slot)()
    kw = adapter.submit_job.call_args.kwargs
    assert kw["no_render"] is True and kw["export_nle"] is False
    adapter.submit_deliver_job.assert_not_called()


# ------------------------------------------------------------------ stepper, rough-cut review, merge


def _step_text(window) -> str:
    return window.lbl_workflow_steps.text()


def test_stepper_strings_per_stage(studio):
    window, _, tmp_path = studio
    assert "● 1. Ingest" in _step_text(window)
    for name in ("2. Rough Cut", "3. Sequence", "4. Deliver"):
        assert f"✔ {name}" not in _step_text(window) and f"● {name}" not in _step_text(window)

    window._on_job_finished(_result({"rough_cut": _timeline("rough_cut", "rough_cut")}, stage="rough_cut"))
    assert "✔ 1. Ingest" in _step_text(window) and "● 2. Rough Cut" in _step_text(window)

    window._on_job_finished(_result({"rough_cut": _timeline("rough_cut", "rough_cut"), "master": _timeline()}, stage="sequence"))
    assert "✔ 2. Rough Cut" in _step_text(window) and "● 3. Sequence" in _step_text(window)

    window._on_job_finished(_result({"master": _timeline()}, stage="all"))
    assert "● 3. Sequence" in _step_text(window)

    window._on_deliver_finished(__import__("src.core.types", fromlist=["DeliverResult"]).DeliverResult(success=True, duration=3.0))
    assert "✔ 3. Sequence" in _step_text(window) and "● 4. Deliver" in _step_text(window)
    window._on_new_project()
    assert "● 1. Ingest" in _step_text(window)


def test_rough_cut_finish_switches_to_review_and_loads_rough_timeline(studio):
    window, _, tmp_path = studio
    assert window.tab_widget.currentWidget() is not window.scene_grid
    scenes = [_scene("a.mp4", 0, 5, **_golden()), _scene("b.mp4", 5, 9, **_golden(0.8, [{"type": "HEAD_TAIL_WOBBLE", "start": 5.0, "end": 5.8, "description": "start wobble"}]))]
    window._on_job_finished(_result({"rough_cut": _timeline("rough_cut", "rough_cut")}, stage="rough_cut", scenes=scenes))

    assert window.tab_widget.currentWidget() is window.scene_grid
    assert window._active_timeline_key == "rough_cut"
    assert window.timeline_view.get_active_timeline_key() == "rough_cut"
    assert len(window.scene_grid.get_all_scenes()) == 2
    assert window.lbl_message.text() == ROUGH_CUT_READY_MESSAGE
    assert "Rough cut ready" in window.lbl_stage.text()
    assert window._pipeline_stage == "rough_cut"
    assert window.btn_run.isEnabled()  # export allowed when ANY edit timeline exists
    assert window.timeline_view._variant_buttons["rough_cut"].text().startswith("🔨 Rough Cut")
    assert "🟢 Golden Take" in window.scene_grid.table.item(0, 7).text()
    assert "Trimmed: Wobble 0.8s" in window.scene_grid.table.item(1, 7).text()


def test_sequence_finish_activates_master_and_keeps_rough_cut(studio):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path)
    rough = _timeline("rough_cut", "rough_cut")
    window._on_job_finished(_result({"rough_cut": rough}, stage="rough_cut", scenes=[_scene("a.mp4", 0, 5, **_golden())]))
    window._on_run_sequence()
    # The backend result may not repeat the rough cut: the window merges what it sent as existing
    window._on_job_finished(_result({"master": _timeline("master"), "beat_driven": _timeline("beat_driven")}, stage="sequence", audio=_audio()))

    assert list(window._edit_timelines) == ["rough_cut", "master", "beat_driven"]
    assert window._active_timeline_key == "master"
    assert window.lbl_stage.text() == "Status: Timeline ready"
    assert window._pipeline_stage == "sequence"
    assert window.btn_run.isEnabled()
    labels = [b.text() for b in window.timeline_view._variant_buttons.values()]
    assert [t.split(" (")[0] for t in labels] == ["🔨 Rough Cut", "✨ Master", "⚡ Beat-Driven"]
    # Real beats / bar grid reach the ruler
    assert window.timeline_view.canvas.bar_grid is not None
    assert len(window.timeline_view.canvas.audio_analysis.beats) == 120


def test_warnings_from_result_reach_console(studio):
    window, _, _ = studio
    res = _result({"master": _timeline()}, stage="sequence", warnings=["variant cinematic failed: boom"])
    window._on_job_finished(res)
    assert "variant cinematic failed: boom" in window.console.toPlainText()


# ------------------------------------------------------------------ cancel per stage


@pytest.mark.parametrize("stage,label", [("rough_cut", "Rough Cut"), ("sequence", "Sequence"), ("all", "Auto Run")])
def test_cancel_per_stage(studio, stage, label):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path)
    running = MagicMock()
    running.is_running.return_value = True
    running.job_id = "job-1"
    adapter.submit_job.return_value = running
    window._start_pipeline_job(stage)
    assert window.btn_stop.isEnabled()

    window._on_stop_pipeline()
    adapter.cancel_job.assert_called_once_with("job-1")
    assert label in window.console.toPlainText()
    assert f"Cancelling {label}" in window.lbl_message.text()

    running.is_running.return_value = False
    res = _result({}, stage=stage)  # type: ignore[arg-type]
    res.success, res.cancelled = False, True
    window._on_job_finished(res)
    assert f"{label} cancelled" in window.lbl_stage.text()
    assert window.inspector_panel.btn_rough_cut.isEnabled()
    assert window._running_stage is None


def test_cancelled_sequence_keeps_previous_edit(studio):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path)
    window._on_job_finished(_result({"rough_cut": _timeline("rough_cut", "rough_cut")}, stage="rough_cut", scenes=[_scene("a.mp4", 0, 5, **_golden())]))
    window._on_run_sequence()
    res = _result({}, stage="sequence")
    res.success, res.cancelled = False, True
    window._on_job_finished(res)
    assert "rough_cut" in window._edit_timelines
    assert len(window.scene_grid.get_all_scenes()) == 1
    assert window._pipeline_stage == "rough_cut"


# ------------------------------------------------------------------ genre / cadence / rhythm group


def test_genre_list_has_23_subgenres_grouped(studio):
    window, _, _ = studio
    combo = window.inspector_panel.combo_genre
    keys = [combo.itemData(i) for i in range(combo.count()) if combo.itemData(i)]
    assert len(keys) == 23 and len(set(keys)) == 23
    assert combo.itemData(0) == ""
    headers = [i for i in range(combo.count()) if combo.itemData(i) is None]
    assert len(headers) == 4
    assert all(not combo.model().item(i).isEnabled() for i in headers)


def test_genre_change_updates_config_via_build_config(studio):
    window, _, _ = studio
    window.preset_combo.setCurrentIndex(window.preset_combo.findData("4k30"))
    from src.core.genre_presets import GENRE_PRESETS

    cfg = window._build_config()
    assert cfg.cadence_profile == "balanced_narrative" and cfg.cadence_multiples is None

    window.inspector_panel.combo_genre.setCurrentIndex(window.inspector_panel.combo_genre.findData("sports_extreme"))
    preset = GENRE_PRESETS["sports_extreme"]
    cfg = window._build_config()
    assert cfg.cadence_profile == "fast_syncopated"
    assert cfg.cadence_multiples == list(preset.target_bar_multiples)
    assert cfg.min_scene_len_sec == preset.min_scene_len_sec
    assert cfg.min_beat_interval == preset.min_beat_interval
    assert cfg.enable_speed_ramping == preset.enable_speed_ramping  # panel-backed fields flow via the panels
    # The canvas the user chose is NOT clobbered by the genre's preferred render preset
    assert (cfg.canvas_width, cfg.canvas_height, cfg.fps) == (3840, 2160, 30.0)
    assert window.preset_combo.currentData() == "4k30"
    assert window.project_manager.is_dirty

    # Explicit cadence beats the genre's; Auto follows it again
    insp = window.inspector_panel
    insp.combo_cadence.setCurrentIndex(insp.combo_cadence.findData("lingering_cinematic"))
    assert window._build_config().cadence_profile == "lingering_cinematic"
    assert window._build_config().cadence_multiples is None
    insp.combo_cadence.setCurrentIndex(0)
    assert window._build_config().cadence_profile == "fast_syncopated"

    insp.combo_genre.setCurrentIndex(0)  # back to Auto
    assert window._build_config().min_scene_len_sec != 0.0  # no crash, default pacing path


def test_rhythm_spinboxes_roundtrip_through_config(studio):
    window, _, _ = studio
    insp = window.inspector_panel
    insp.spin_long_shot.setValue(18.0)
    insp.spin_max_scene.setValue(22.0)
    insp.chk_rough_cut_pruning.setChecked(False)
    cfg = window._build_config()
    assert (cfg.extended_shot_threshold_sec, cfg.max_scene_len_sec, cfg.run_rough_cut_in_all) == (18.0, 22.0, False)
    insp.spin_long_shot.setValue(5.0)
    insp.load_from_config(cfg)
    assert insp.spin_long_shot.value() == 18.0 and insp.spin_max_scene.value() == 22.0
    assert insp.chk_rough_cut_pruning.isChecked() is False


def test_recommended_preset_selects_genre_and_pacing(studio):
    from src.core.genre_presets import GENRE_PRESETS

    window, _, _ = studio
    preset = GENRE_PRESETS["travel_cinematic"]
    window._on_preset_recommended(preset)
    assert window.inspector_panel.selected_genre_key() == "travel_cinematic"
    cfg = window._build_config()
    assert cfg.cadence_profile == "lingering_cinematic"
    assert window.inspector_panel.combo_cadence.currentData() == "auto"


def test_genre_then_sequence_is_quick_reedit_with_current_scenes(studio):
    window, adapter, tmp_path = studio
    _setup_media(window, tmp_path)
    window._on_job_finished(_result({"rough_cut": _timeline("rough_cut", "rough_cut")}, stage="rough_cut", scenes=[_scene("a.mp4", 0, 5, **_golden())]))
    window.inspector_panel.combo_genre.setCurrentIndex(window.inspector_panel.combo_genre.findData("social_tiktok_fast"))
    window._on_run_sequence()
    kw = adapter.submit_job.call_args.kwargs
    assert kw["stage"] == "sequence" and len(kw["scenes"]) == 1
    assert kw["config"].cadence_profile == "viral_burst"


# ------------------------------------------------------------------ scene badges


def test_scene_badges_pure():
    plain = _scene("a.mp4", 0, 5)
    assert scene_badges(plain) == []
    golden = _scene("a.mp4", 0, 5, **_golden())
    assert [b.kind for b in scene_badges(golden)] == ["golden"]
    trimmed = _scene(
        "a.mp4", 0, 5,
        **_golden(0.84, [{"type": "HEAD_TAIL_WOBBLE", "start": 0.0, "end": 0.84, "description": "start wobble"},
                         {"type": "CAMERA_SHAKE", "start": 3.0, "end": 3.5, "description": "shake"}]),
    )
    bs = {b.kind: b for b in scene_badges(trimmed)}
    assert bs["trimmed"].text == "🟡 Trimmed: Wobble +1 0.8s"
    assert "Wobble 0.00-0.84s: start wobble" in bs["trimmed"].tooltip
    assert "Shake 3.00-3.50s: shake" in bs["trimmed"].tooltip
    long_shot = _scene("a.mp4", 0, 25, is_long_shot=True)
    assert scene_badges(long_shot)[0].text == "🟣 Long Shot >15s"
    assert scene_badges(_scene("a.mp4", 0, 12, is_long_shot=True))[0].text == "🟣 Long Shot 12s"
    dead = _scene("a.mp4", 0, 5, has_dead_air=True)
    assert [b.text for b in scene_badges(dead)] == ["Dead-Air"]
    assert scene_badges(plain, included=False)[0].text == "🔴 Excluded"


def test_scene_grid_badge_cells_tooltips_and_filter(qapp):
    grid = SceneGridWidget()
    scenes = [
        _scene("a.mp4", 0, 5, **_golden()),
        _scene("b.mp4", 0, 6, **_golden(1.5, [{"type": "DEFOCUS_BLUR", "start": 4.0, "end": 5.5, "description": "focus hunt"}])),
        _scene("c.mp4", 0, 25, is_long_shot=True, **_golden()),
        _scene("d.mp4", 0, 4, has_dead_air=True),
        _scene("e.mp4", 0, 4),
    ]
    grid.set_scenes(scenes)
    t = grid.table
    assert t.item(0, 7).text() == "🟢 Golden Take"
    assert "🟡 Trimmed: Defocus 1.5s" in t.item(1, 7).text()
    assert "Defocus 4.00-5.50s: focus hunt" in t.item(1, 7).toolTip()
    assert "🟣 Long Shot >15s" in t.item(2, 7).text()
    assert t.item(3, 7).text() == "Dead-Air"  # existing behaviour kept
    assert t.item(4, 7).text() == "Active"

    # Unchecking the Inc checkbox adds the Excluded badge (and keeps the signal semantics)
    toggled: list[tuple[int, bool]] = []
    grid.scene_toggled.connect(lambda i, v: toggled.append((i, v)))
    t.item(4, 0).setCheckState(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.CheckState.Unchecked)
    assert toggled == [(4, False)]
    assert "🔴 Excluded" in t.item(4, 7).text()
    grid.set_scene_included(4, True)
    assert t.item(4, 7).text() == "Active"

    # Filter by badge
    def visible() -> list[int]:
        return [r for r in range(t.rowCount()) if not t.isRowHidden(r)]

    for kind, rows in (("golden", [0, 1, 2]), ("trimmed", [1]), ("long", [2]), ("dead_air", [3])):
        grid.combo_badge_filter.setCurrentIndex(grid.combo_badge_filter.findData(kind))
        assert visible() == rows, kind
    grid.set_scene_included(0, False)
    grid.combo_badge_filter.setCurrentIndex(grid.combo_badge_filter.findData("excluded"))
    assert visible() == [0]
    grid.reset_filters()
    assert visible() == [0, 1, 2, 3, 4]


def test_scene_grid_honours_included_metadata(qapp):
    grid = SceneGridWidget()
    grid.set_scenes([_scene("a.mp4", 0, 5, included=False), _scene("b.mp4", 0, 5, included=True)])
    assert [grid.is_scene_included(i) for i in (0, 1)] == [False, True]
    assert "🔴 Excluded" in grid.table.item(0, 7).text()


# ------------------------------------------------------------------ variant pills


def test_variant_pill_labels_and_order(qapp):
    widget = TimelineViewWidget()
    tls = {k: _timeline(k) for k in ("social_teaser", "master", "custom_x", "cinematic", "rough_cut", "beat_driven")}
    tls["rough_cut"] = _timeline("rough_cut", "rough_cut")
    widget.set_edit_timelines(tls, active_key="master")
    assert list(widget._variant_buttons) == ["rough_cut", "master", "beat_driven", "cinematic", "social_teaser", "custom_x"]
    texts = [b.text() for b in widget._variant_buttons.values()]
    assert [t.split(" (")[0] for t in texts[:5]] == ["🔨 Rough Cut", "✨ Master", "⚡ Beat-Driven", "🎬 Cinematic", "📱 Social Teaser"]
    assert texts[5] == "Custom_X (6.0s)"  # unknown keys keep "<name> (<dur>s)"
    assert variant_pill_label("master", "Whatever", 12.34) == "✨ Master (12.3s)"
    assert sort_variant_keys(["x", "cinematic", "rough_cut"]) == ["rough_cut", "cinematic", "x"]
    # keys / switching semantics unchanged
    widget.switch_variant("rough_cut", emit_signal=False)
    assert widget.get_active_timeline_key() == "rough_cut"


# ------------------------------------------------------------------ ruler geometry (pure)


def test_bar_lines_levels_and_labels():
    grid = _grid(120.0, 40)  # 2 s bars
    lines = compute_bar_lines(grid, 30.0, 0.0, 40.0)
    by_bar = {ln.bar_number: ln for ln in lines}
    assert by_bar[1].level == 8 and by_bar[9].level == 8 and by_bar[17].level == 8
    assert by_bar[5].level == 4 and by_bar[13].level == 4
    assert by_bar[3].level == 2 and by_bar[2].level == 1
    assert [ln.bar_number for ln in lines if ln.labelled] == [1, 5, 9, 13, 17, 21]  # phrase starts only
    assert by_bar[2].time == 2.0 and not by_bar[2].labelled


def test_bar_lines_density_adapts_to_zoom():
    grid = _grid(120.0, 40)
    # bars 10 px apart: lines yes, labels skipped (< ~12 px)
    near = compute_bar_lines(grid, 5.0, 0.0, 80.0)
    assert len(near) == 40 and not any(ln.labelled for ln in near)
    # bars 2 px apart: plain bars dropped, phrase lines remain
    far = compute_bar_lines(grid, 1.0, 0.0, 80.0)
    assert far and all(ln.level >= 2 for ln in far) and not any(ln.labelled for ln in far)
    # very zoomed in: every bar, only phrase starts carry numbers
    close = compute_bar_lines(grid, 120.0, 0.0, 12.0)
    assert [ln.bar_number for ln in close if ln.labelled][:2] == [1, 5]


def test_bar_lines_visible_range_only_and_empty_cases():
    grid = _grid(120.0, 400)
    lines = compute_bar_lines(grid, 30.0, 100.0, 110.0)
    assert lines and min(ln.time for ln in lines) >= 100.0 - 2 * 2.0 and max(ln.time for ln in lines) <= 110.0 + 2 * 2.0
    assert len(lines) <= 10
    assert compute_bar_lines(None, 30.0, 0.0, 10.0) == []
    assert compute_bar_lines(MusicalBarGrid(tempo_bpm=120.0), 30.0, 0.0, 10.0) == []


def test_bar_lines_without_phrases_label_two_bar_starts():
    times = [i * 2.0 for i in range(20)]
    grid = MusicalBarGrid(tempo_bpm=120.0, bar_times=times, phrase_2=times[::2])
    lines = compute_bar_lines(grid, 30.0, 0.0, 40.0)
    assert [ln.bar_number for ln in lines if ln.labelled][:3] == [1, 3, 5]


def test_lock_badge_and_sub_window_text():
    text, ok = lock_badge_text(0.012, 30.0)  # type: ignore[misc]
    assert text == "downbeat lock ✔ ±12 ms" and ok
    text, ok = lock_badge_text(0.030, 30.0)  # type: ignore[misc]
    assert text == "downbeat lock ⚠ ±30 ms" and not ok  # half frame @30fps = 16.7 ms
    assert lock_badge_text(None, 30.0) is None
    clip = EditClip(Path("/x/long.mp4"), 19.2, 240, 21.8, scene_start=0.0, scene_end=41.0)
    assert clip_sub_window_text(clip, 30.0) == "sub-window 19.2–27.2 s of 41 s"
    assert clip_sub_window_text(EditClip(Path("/x/a.mp4"), 0.0, 150, 5.0, scene_start=0.0, scene_end=5.0), 30.0) is None
    assert clip_sub_window_text(EditClip(Path("/x/a.mp4"), 0.0, 150, 5.0), 30.0) is None


def test_timeline_view_ruler_bar_grid_badge_and_subwindow(qapp):
    widget = TimelineViewWidget()
    grid = _grid(120.0, 30)
    sub = EditClip(Path("/x/long.mp4"), 19.2, 240, 21.8, label="long", scene_start=0.0, scene_end=41.0)
    tl = EditTimeline(
        name="Master", fps=30.0, canvas_width=1920, canvas_height=1080, clips=[sub, _timeline().clips[0]],
        variant_key="master", bar_grid=grid, cut_snap_error_max_sec=0.012,
    )
    widget.set_edit_timelines({"master": tl, "rough_cut": _timeline("rough_cut", "rough_cut")}, active_key="master")
    assert widget.canvas.bar_grid is grid
    assert not widget.lbl_lock_badge.isHidden() and widget.lbl_lock_badge.text() == "downbeat lock ✔ ±12 ms"
    item = widget.canvas.clips[0]
    assert "sub-window 19.2–27.2 s of 41 s" in widget.canvas.clip_tooltip(item)
    assert "sub-window" not in widget.canvas.clip_tooltip(widget.canvas.clips[1])
    widget.switch_variant("rough_cut", emit_signal=False)  # rough cut: no bars, no lock badge
    assert widget.canvas.bar_grid is None and widget.lbl_lock_badge.isHidden()
    widget.clear_timeline()
    assert widget.canvas.bar_grid is None


def test_timeline_canvas_paints_fast_with_200_clips(qapp):
    widget = TimelineViewWidget()
    clips = [EditClip(Path(f"/x/{i}.mp4"), 0.0, 60, 10.0, label=f"c{i}", scene_start=0.0, scene_end=20.0) for i in range(200)]
    tl = EditTimeline(name="Master", fps=30.0, canvas_width=1920, canvas_height=1080, clips=clips,
                      variant_key="master", bar_grid=_grid(120.0, 220), cut_snap_error_max_sec=0.004)
    widget.set_edit_timelines({"master": tl}, active_key="master")
    canvas = widget.canvas
    canvas.set_zoom(40.0)
    pix = QPixmap(1200, canvas.height())
    region = QRegion(0, 0, 1200, canvas.height())
    canvas.render(pix, QPoint(0, 0), region)  # warm up
    t0 = time.perf_counter()
    n = 20
    for _ in range(n):
        canvas.render(pix, QPoint(0, 0), region)
    per_frame = (time.perf_counter() - t0) / n
    assert per_frame < 1 / 30, f"{per_frame * 1000:.1f} ms per visible-range paint"


# ------------------------------------------------------------------ project round trip


def test_derive_pipeline_stage_for_old_projects():
    derive = StudioMainWindow._derive_pipeline_stage
    rough = {"rough_cut": _timeline("rough_cut", "rough_cut")}
    assert derive(ProjectData(), {}) == "ingest"
    assert derive(ProjectData(), rough) == "rough_cut"
    assert derive(ProjectData(), {"master": _timeline()}) == "sequence"
    assert derive(ProjectData(pipeline_stage="rough_cut"), {"master": _timeline()}) == "rough_cut"  # stored wins
    assert derive(ProjectData(deliverables={"render_path": "x"}), rough) == "deliver"
    assert derive(ProjectData(timeline_clips=[{"clip_id": "a"}]), {}) == "sequence"


def test_project_roundtrip_stage_audio_state_and_golden_scenes(studio, tmp_path):
    window, adapter, _ = studio
    scenes = [
        _scene("a.mp4", 0, 5, **_golden()),
        _scene("b.mp4", 5, 9, **_golden(0.8, [{"type": "CAMERA_SHAKE", "start": 5.0, "end": 5.8, "description": "shake"}])),
        _scene("c.mp4", 0, 25, is_long_shot=True, **_golden()),
    ]
    window._on_job_finished(_result({"rough_cut": _timeline("rough_cut", "rough_cut")}, stage="rough_cut", scenes=scenes))
    window.scene_grid.set_scene_included(1, False)
    master = _timeline("master", bar_grid=_grid(), cut_snap_error_max_sec=0.01)
    window._on_job_finished(_result({"rough_cut": _timeline("rough_cut", "rough_cut"), "master": master}, stage="sequence", scenes=scenes, audio=_audio()))
    window.scene_grid.set_scene_included(1, False)  # a sequence result re-loads the grid: re-apply the curation
    assert window._pipeline_stage == "sequence"

    path = tmp_path / "staged.tteproj"
    assert window._do_save(path) is True
    raw = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert raw["pipeline_stage"] == "sequence"
    assert len(raw["audio_state"]["beats"]) == 120 and raw["audio_state"]["bar_grid"]["bar_times"]
    assert raw["scored_scenes"][0]["metadata"]["golden"]["trimmed_sec"] == 0.0
    assert [s["included"] for s in raw["scored_scenes"]] == [True, False, True]

    w2 = StudioMainWindow()
    try:
        w2.adapter = adapter
        assert w2._do_open(path) is True
        assert w2._pipeline_stage == "sequence"
        assert "✔ 2. Rough Cut" in w2.lbl_workflow_steps.text() and "● 3. Sequence" in w2.lbl_workflow_steps.text()
        assert w2._audio_state is not None and len(w2._audio_state.beats) == 120
        assert w2._audio_state.bar_grid.bar_times == window._audio_state.bar_grid.bar_times
        assert w2.timeline_view.canvas.bar_grid is not None
        assert len(w2.timeline_view.canvas.audio_analysis.beats) == 120  # real beats, not cut-point synthesis
        assert [w2.scene_grid.is_scene_included(i) for i in range(3)] == [True, False, True]
        assert "🟢 Golden Take" in w2.scene_grid.table.item(0, 7).text()
        assert "Shake" in w2.scene_grid.table.item(1, 7).text()
        assert "🟣 Long Shot" in w2.scene_grid.table.item(2, 7).text()
        assert "🔴 Excluded" in w2.scene_grid.table.item(1, 7).text()
        assert w2.inspector_panel.btn_sequence.isEnabled() is False  # no soundtrack in this project: reason shown
        assert "soundtrack" in w2.inspector_panel.btn_sequence.toolTip()
    finally:
        w2.close()


def test_new_project_resets_stage_audio_and_buttons(studio):
    window, _, tmp_path = studio
    window._on_job_finished(_result({"master": _timeline()}, stage="sequence", audio=_audio()))
    assert window._audio_state is not None
    window._on_new_project()
    assert window._pipeline_stage == "ingest" and window._audio_state is None
    assert window.timeline_view.canvas.bar_grid is None
    assert not window.inspector_panel.btn_sequence.isEnabled()


def test_ui_state_persistence_is_opt_in(studio, tmp_path, monkeypatch):
    window, _, _ = studio
    monkeypatch.setattr("src.ui.main_window.DEFAULT_RECENT_PROJECTS_PATH", tmp_path / "recents.json")
    window.inspector_panel.chk_rough_cut_pruning.setChecked(False)
    window.inspector_panel.spin_long_shot.setValue(20.0)
    window.project_manager.set_dirty(False)
    window.close()
    assert not (tmp_path / "ui_state.json").exists()  # not enabled: nothing written

    w2 = StudioMainWindow()
    try:
        w2.inspector_panel.chk_rough_cut_pruning.setChecked(False)
        w2.inspector_panel.spin_long_shot.setValue(20.0)
        w2.inspector_panel.set_genre("sports_extreme")
        w2._pipeline_stage = "rough_cut"
        w2.enable_ui_state_persistence()
        w2.project_manager.set_dirty(False)
        w2.close()
    finally:
        pass
    assert (tmp_path / "ui_state.json").is_file()

    w3 = StudioMainWindow()
    try:
        w3.enable_ui_state_persistence()
        assert w3.inspector_panel.chk_rough_cut_pruning.isChecked() is False
        assert w3.inspector_panel.spin_long_shot.value() == 20.0
        assert w3.inspector_panel.selected_genre_key() == "sports_extreme"
        assert w3.tab_widget.currentWidget() is w3.scene_grid
    finally:
        w3.project_manager.set_dirty(False)
        w3.close()


# ------------------------------------------------------------------ REAL backend (tiny synthetic media)


@pytest.fixture(scope="module")
def real_media(tmp_path_factory) -> dict[str, Path]:
    from test_staged_pipeline import make_footage, write_click_track

    root = tmp_path_factory.mktemp("gui_staged_media")
    footage = root / "footage"
    footage.mkdir()
    make_footage(footage / "cam_a.mp4", seconds_per_scene=4.0, scenes=3)
    music = root / "click.wav"
    write_click_track(music, 120.0, 4, 24.0)
    return {"footage": footage, "music": music}


def _pump(qapp, cond, timeout: float = 300.0) -> bool:
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        qapp.processEvents()
        time.sleep(0.03)
    return bool(cond())


def test_real_backend_rough_cut_then_reedit_then_export(qapp, real_media, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.ui.controllers.project_manager.DEFAULT_RECENT_PROJECTS_PATH", tmp_path / "recents.json"
    )
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(f"PySide6.QtWidgets.QMessageBox.{name}", lambda *a, **k: None)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question", lambda *a, **k: QMessageBox.StandardButton.Discard
    )
    window = StudioMainWindow(StudioBackendAdapter())
    orig_build = window._build_config

    def fast_config():
        cfg = orig_build()
        cfg.cache_dir = tmp_path / "cache"
        cfg.enable_audio_fix = False
        cfg.enable_vlm_tagging = False
        cfg.enable_transcription = False
        cfg.enable_teaser = False
        cfg.auto_normalize_vfr = False
        return cfg

    window._build_config = fast_config  # type: ignore[method-assign]
    calls: list[dict] = []
    real_submit = window.adapter.submit_job

    def spy(*a, **kw):
        calls.append(kw)
        return real_submit(*a, **kw)

    window.adapter.submit_job = spy  # type: ignore[method-assign]
    try:
        out = tmp_path / "out"
        window.media_browser.set_input_dir(real_media["footage"])
        window.media_browser.set_soundtrack_path(real_media["music"])
        window.media_browser.set_output_dir(out)
        window.media_browser._video_files = sorted(real_media["footage"].glob("*.mp4"))
        window._refresh_stage_controls()
        insp = window.inspector_panel
        assert insp.btn_rough_cut.isEnabled() and insp.btn_sequence.isEnabled() and insp.btn_analyze.isEnabled()

        # 1. Rough cut -> review
        insp.btn_rough_cut.click()
        assert _pump(qapp, lambda: window._current_job is None and window._running_stage is None)
        assert "Rough cut ready" in window.lbl_stage.text(), window.console.toPlainText()[-600:]
        assert window.tab_widget.currentWidget() is window.scene_grid
        assert list(window._edit_timelines) == ["rough_cut"] and window._active_timeline_key == "rough_cut"
        assert window.scene_grid.get_all_scenes(), "golden takes expected in the review grid"
        assert all("🟢 Golden Take" in window.scene_grid.table.item(r, 7).text() for r in range(window.scene_grid.table.rowCount()))
        assert "● 2. Rough Cut" in window.lbl_workflow_steps.text()
        assert window.btn_run.isEnabled()

        # 2. Change genre, re-edit from the reviewed takes (no re-ingest)
        insp.combo_genre.setCurrentIndex(insp.combo_genre.findData("sports_extreme"))
        n_scenes = len(window.scene_grid.get_all_scenes())
        insp.btn_sequence.click()
        assert _pump(qapp, lambda: window._current_job is None and window._running_stage is None)
        assert calls[-1]["stage"] == "sequence" and len(calls[-1]["scenes"]) == n_scenes
        assert calls[-1]["config"].cadence_profile == "fast_syncopated"
        assert window.lbl_stage.text() == "Status: Timeline ready", window.console.toPlainText()[-800:]
        assert list(window._edit_timelines)[0] == "rough_cut" and "master" in window._edit_timelines
        assert window._active_timeline_key == "master"
        assert "● 3. Sequence" in window.lbl_workflow_steps.text()
        assert window.timeline_view.canvas.bar_grid is not None
        assert not window.timeline_view.lbl_lock_badge.isHidden()
        assert window._pipeline_stage == "sequence"

        # 3. Export only on request (FCPXML, no render)
        ep = window.export_panel
        ep.chk_enable_render.setChecked(False)
        for nm in ("chk_fcpxml", "chk_premiere", "chk_otio", "chk_capcut"):
            getattr(ep, nm).setChecked(nm == "chk_fcpxml")
        window._on_start_deliver()
        assert _pump(qapp, lambda: window._current_job is None)
        assert "● 4. Deliver" in window.lbl_workflow_steps.text(), window.console.toPlainText()[-600:]
        assert list(out.rglob("*.fcpxml"))
    finally:
        window.project_manager.set_dirty(False)
        window.close()

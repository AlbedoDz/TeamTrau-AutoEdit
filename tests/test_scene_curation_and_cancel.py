"""Scene curation -> timeline, and cancellation reporting."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.service import StudioBackendAdapter
from src.core.types import (
    DeliverResult,
    EditClip,
    EditTimeline,
    PipelineExecutionResult,
    SceneCut,
    ScoredScene,
)
from src.modules.edit_timeline import apply_scene_curation


def _scene(name: str, start: float, dur: float, speed: float = 1.0) -> ScoredScene:
    return ScoredScene(
        scene=SceneCut(Path(name), start, start + dur, 0, 0, dur),
        sharpness_score=1.0, aesthetic_score=1.0, composite_score=1.0, speed=speed,
    )


def _timeline(scenes: list[ScoredScene], frames: int = 30) -> EditTimeline:
    clips = [
        EditClip(s.scene.clip_path, s.scene.start_time, frames, s.scene.duration, label=f"c{i}")
        for i, s in enumerate(scenes)
    ]
    return EditTimeline("t", 30.0, 1920, 1080, clips)


def test_curation_replaces_only_excluded_and_keeps_grid():
    a, b, c = _scene("a.mp4", 0, 5), _scene("b.mp4", 0, 5), _scene("c.mp4", 0, 5)
    tl = _timeline([a, b, c, b])
    new, n = apply_scene_curation(tl, included=[a, c], excluded=[b])
    assert n == 2
    assert [cl.frames for cl in new.clips] == [cl.frames for cl in tl.clips]
    assert new.total_frames == tl.total_frames
    assert all(Path(cl.source_path).name != "b.mp4" for cl in new.clips)
    assert new.clips[0] is tl.clips[0]  # untouched clips are preserved as-is
    assert new.clips[1].label == "c1"


def test_curation_prefers_long_enough_scene():
    short, long_, bad = _scene("s.mp4", 0, 0.5), _scene("l.mp4", 0, 6), _scene("x.mp4", 0, 5)
    tl = _timeline([bad], frames=90)  # needs 3 s
    new, n = apply_scene_curation(tl, included=[short, long_], excluded=[bad])
    assert n == 1 and Path(new.clips[0].source_path).name == "l.mp4"


def test_curation_noop_and_all_excluded():
    a, b = _scene("a.mp4", 0, 5), _scene("b.mp4", 0, 5)
    tl = _timeline([a])
    same, n = apply_scene_curation(tl, included=[a], excluded=[b])
    assert n == 0 and same is tl
    with pytest.raises(ValueError):
        apply_scene_curation(tl, included=[], excluded=[a])


def test_adapter_reports_cancellation_not_failure(tmp_path):
    adapter = StudioBackendAdapter()
    tl = _timeline([_scene("a.mp4", 0, 5)])
    adapter._execute_deliver = MagicMock(side_effect=InterruptedError("stop"))  # type: ignore[method-assign]
    res = adapter.submit_deliver_job(tl, tmp_path, MagicMock()).result
    assert isinstance(res, DeliverResult)
    assert res.success is False and res.cancelled is True


def test_adapter_pipeline_cancellation_flag(tmp_path):
    adapter = StudioBackendAdapter()
    adapter._execute_pipeline = MagicMock(side_effect=InterruptedError("stop"))  # type: ignore[method-assign]
    res = adapter.submit_job(tmp_path, None, tmp_path, MagicMock()).result
    assert isinstance(res, PipelineExecutionResult)
    assert res.cancelled is True and res.success is False


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_gui_apply_curation_updates_all_timelines_and_cancel_label(qapp, monkeypatch):
    from src.ui.main_window import StudioMainWindow

    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.question", lambda *a, **k: 0)
    w = StudioMainWindow()
    try:
        a, b = _scene("a.mp4", 0, 5), _scene("b.mp4", 0, 5)
        w.scene_grid.set_scenes([a, b])
        tls = {"master": _timeline([a, b, a, b]), "cinematic": _timeline([b, a])}
        w._edit_timelines = dict(tls)
        w.timeline_view.set_edit_timelines(tls, active_key="master", tempo_bpm=0.0)
        w.scene_grid.set_scene_included(1, False)  # exclude b

        w._on_apply_scene_curation()
        out = w.timeline_view.get_edit_timelines()
        for tl in out.values():
            assert all(Path(c.source_path).name == "a.mp4" for c in tl.clips)
        assert out["master"].total_frames == tls["master"].total_frames
        assert w.project_manager.is_dirty

        w._on_deliver_finished(DeliverResult(success=False, cancelled=True, error_message="Cancelled by user."))
        assert "cancelled" in w.lbl_stage.text().lower()
        assert "error" not in w.console.toPlainText().lower().split("[user]")[-1]
    finally:
        w.close()


def test_scene_hotkey_without_selection_does_nothing(qapp):
    from src.ui.main_window import StudioMainWindow

    w = StudioMainWindow()
    try:
        w.scene_grid.set_scenes([_scene("a.mp4", 0, 5), _scene("b.mp4", 0, 5)])
        w.scene_grid.table.setCurrentCell(-1, -1)
        w._on_shortcut_scene_action("reject")
        assert w.scene_grid.is_scene_included(0) and w.scene_grid.is_scene_included(1)
        assert not w.undo_stack.canUndo()
    finally:
        w.close()

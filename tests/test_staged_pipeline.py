"""Staged workflow (Phase 23 B1/B2/B4): rough cut -> save -> re-open -> sequence, with real media."""

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import pytest
import soundfile as sf
from click.testing import CliRunner

from src.core.config import PipelineConfig
from src.core.service import StudioBackendAdapter
from src.core.types import (
    EditClip,
    EditTimeline,
    PipelineExecutionResult,
    SceneCut,
    ScoredScene,
)
from src.modules.edit_timeline import apply_scene_curation
from src.modules.rough_cut_cleaner import RoughCutCleaner
from src.ui.controllers.project_manager import (
    ProjectData,
    ProjectManager,
    load_project_file,
    save_result_as_project,
)

# ------------------------------------------------------------------ synthetic media


def make_footage(path: Path, seconds_per_scene: float = 4.0, scenes: int = 3, fps: float = 30.0) -> Path:
    """Hard cuts between visually different, gently moving scenes (detected by PySceneDetect)."""
    w, h = 320, 240
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    palette = [(40, 90, 160), (170, 60, 40), (50, 150, 70), (150, 150, 40)]
    n = int(seconds_per_scene * fps)
    for s in range(scenes):
        base = np.full((h, w, 3), palette[s % len(palette)], dtype=np.uint8)
        rng = np.random.default_rng(s)
        noise = rng.integers(0, 60, (h, w, 1), dtype=np.uint8)
        base = cv2.add(base, np.repeat(noise, 3, axis=2))
        for i in range(n):
            frame = base.copy()
            cv2.circle(frame, (40 + int(i * 0.8), 90 + 20 * s), 28, (230, 230, 230), -1)
            cv2.putText(frame, f"S{s}", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            out.write(frame)
    out.release()
    return path


def write_click_track(path: Path, bpm: float, bpb: int, seconds: float, sr: int = 22050) -> None:
    """Accented sine bursts: strong on beat 1 of the bar, soft on the others."""
    y = np.zeros(int(seconds * sr), dtype=np.float32)
    period = 60.0 / bpm
    burst_t = np.arange(int(0.05 * sr)) / sr
    i = 0
    while i * period < seconds - 0.1:
        down = i % bpb == 0
        freq, amp = (1000.0, 0.9) if down else (700.0, 0.35)
        burst = amp * np.sin(2 * np.pi * freq * burst_t) * np.exp(-burst_t * 60.0)
        s = int(i * period * sr)
        y[s : s + burst.size] += burst[: max(0, min(burst.size, y.size - s))]
        i += 1
    sf.write(str(path), y, sr)


def staged_config(tmp_path: Path, **kw) -> PipelineConfig:
    return PipelineConfig(
        cache_dir=tmp_path / "cache",
        enable_audio_fix=False,
        enable_vlm_tagging=False,
        enable_transcription=False,
        enable_teaser=False,
        auto_normalize_vfr=False,
        **kw,
    )


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("staged_media")
    footage = root / "footage"
    footage.mkdir()
    make_footage(footage / "cam_a.mp4", seconds_per_scene=4.0, scenes=3)
    music = root / "click.wav"
    write_click_track(music, 120.0, 4, 24.0)
    return {"footage": footage, "music": music}


def run_stage(tmp_path: Path, media: dict[str, Path], stage: str, **kw):
    cfg = kw.pop("config", None) or staged_config(tmp_path)
    soundtrack = kw.pop("soundtrack", media["music"])
    handle = StudioBackendAdapter().submit_job(
        media["footage"], soundtrack, kw.pop("out", tmp_path / "out"), cfg, stage=stage, **kw
    )
    return handle.result, cfg


# ------------------------------------------------------------------ rough cut stage


def test_rough_cut_stage_needs_no_soundtrack_and_only_makes_rough_cut(tmp_path, media):
    stages: list[tuple[str, int, int]] = []
    from src.core.service import JobCallbacks

    cb = JobCallbacks(on_stage_changed=lambda n, i, t: stages.append((n, i, t)))
    cfg = staged_config(tmp_path)
    res = StudioBackendAdapter().submit_job(
        media["footage"], None, tmp_path / "out", cfg, callbacks=cb, stage="rough_cut"
    ).result
    assert res.success, res.error_message
    assert res.stage == "rough_cut"
    assert list(res.edit_timelines) == ["rough_cut"]
    tl = res.edit_timelines["rough_cut"]
    assert tl.kind == "rough_cut" and tl.variant_key == "rough_cut" and tl.soundtrack_path is None
    assert res.timeline_variants == {} and res.audio_analysis is None
    # dynamic stage plan: ingest, clean, score, synth
    assert [s[1] for s in stages] == [1, 2, 3, 4] and {s[2] for s in stages} == {4}
    assert len(res.cleaned_segments) == len(tl.clips) >= 2


def test_golden_scenes_are_scored_on_the_normalised_scale_with_metadata(tmp_path, media):
    res, cfg = run_stage(tmp_path, media, "rough_cut", soundtrack=None)
    assert res.success, res.error_message
    scenes = res.scored_scenes
    assert len(scenes) == len(res.cleaned_segments)
    for sc in scenes:
        assert 0.0 <= sc.composite_score <= 1.0  # VisualScorer scale, not raw Tenengrad
        g = sc.metadata["golden"]
        assert set(g) >= {
            "source_in", "source_out", "defects", "requires_deflicker",
            "original_scene_duration", "trimmed_sec",
        }
        assert g["source_in"] == pytest.approx(sc.scene.start_time, abs=1e-3)
        assert g["source_out"] == pytest.approx(sc.scene.end_time, abs=1e-3)
        assert isinstance(sc.metadata["is_long_shot"], bool)
        assert sc.metadata["scored_by"] == "visual_scorer"
        json.dumps(sc.metadata)  # JSON-safe
        for d in g["defects"]:
            assert set(d) == {"type", "start", "end", "description"}
    # not every score can be the raw Tenengrad mean (that is >> 1 for textured footage)
    assert max(sc.composite_score for sc in scenes) <= 1.0


def test_long_shot_flag_follows_threshold(tmp_path, media):
    cfg = staged_config(tmp_path, extended_shot_threshold_sec=3.0)
    res, _ = run_stage(tmp_path, media, "rough_cut", soundtrack=None, config=cfg)
    assert any(sc.metadata["is_long_shot"] for sc in res.scored_scenes)
    cfg2 = staged_config(tmp_path / "b", extended_shot_threshold_sec=60.0)
    res2, _ = run_stage(tmp_path / "b", media, "rough_cut", soundtrack=None, config=cfg2)
    assert not any(sc.metadata["is_long_shot"] for sc in res2.scored_scenes)


def test_second_rough_cut_run_hits_the_cache(tmp_path, media):
    cfg = staged_config(tmp_path)
    scans: list[tuple[float, float]] = []
    real = RoughCutCleaner._scan_scene

    def spy(self, scene, clip_path, cancel_event):
        scans.append((scene.start_time, scene.end_time))
        return real(self, scene, clip_path, cancel_event)

    with patch.object(RoughCutCleaner, "_scan_scene", spy):
        t0 = time.perf_counter()
        first, _ = run_stage(tmp_path, media, "rough_cut", soundtrack=None, config=cfg)
        cold = time.perf_counter() - t0
        assert first.success and scans
        n_first = len(scans)
        t0 = time.perf_counter()
        second, _ = run_stage(tmp_path, media, "rough_cut", soundtrack=None, config=cfg, out=tmp_path / "out2")
        warm = time.perf_counter() - t0
    assert second.success
    assert len(scans) == n_first  # no new per-frame scan on the second run
    assert [c.source_start_time for c in second.cleaned_segments] == [
        c.source_start_time for c in first.cleaned_segments
    ]
    print(f"rough cut cold {cold:.2f}s warm {warm:.2f}s")
    # the cache stores 4 Hz series
    files = list((Path(cfg.cache_dir) / "analysis" / "rough_cut").glob("*.json"))
    assert files
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    sample = next(iter(entry.values()))
    assert sample["series"]["hz"] == 4.0
    assert {"flow", "sharpness", "dark_ratio", "clip_ratio"} <= set(sample["series"])
    assert len(sample["series"]["flow"]) <= int(sample["scene_dur"] * 4) + 2


def test_cache_key_depends_on_detection_params(tmp_path, media):
    cfg = staged_config(tmp_path)
    run_stage(tmp_path, media, "rough_cut", soundtrack=None, config=cfg)
    scans: list[int] = []
    real = RoughCutCleaner._scan_scene

    def spy(self, scene, clip_path, cancel_event):
        scans.append(1)
        return real(self, scene, clip_path, cancel_event)

    cfg2 = staged_config(tmp_path, max_shake_threshold=55.0)
    with patch.object(RoughCutCleaner, "_scan_scene", spy):
        run_stage(tmp_path, media, "rough_cut", soundtrack=None, config=cfg2, out=tmp_path / "o2")
    assert scans  # a relevant parameter changed -> miss


def test_cancel_mid_clean_returns_cancelled(tmp_path, media):
    cfg = staged_config(tmp_path, enable_cache=False)
    adapter = StudioBackendAdapter()
    started = threading.Event()
    real = RoughCutCleaner._scan_scene

    def slow(self, scene, clip_path, cancel_event):
        started.set()
        # wait until cancelled, then let the real scan notice the event
        assert cancel_event.wait(timeout=10)
        return real(self, scene, clip_path, cancel_event)

    with patch.object(RoughCutCleaner, "_scan_scene", slow):
        handle = adapter.submit_job(
            media["footage"], None, tmp_path / "out", cfg, run_async=True, stage="rough_cut"
        )
        assert started.wait(timeout=60)
        handle.cancel()
        assert handle.wait(timeout=60)
    assert handle.result.cancelled is True and handle.result.success is False
    assert handle.result.stage == "rough_cut"


def test_cleaner_cancel_event_raises_between_scenes_and_frames(tmp_path, media):
    cfg = staged_config(tmp_path, enable_cache=False)
    sc = SceneCut(media["footage"] / "cam_a.mp4", 0.0, 4.0, 0, 120, 4.0)
    ev = threading.Event()
    ev.set()
    with pytest.raises(InterruptedError):
        RoughCutCleaner(cfg).inspect_scene(sc, cancel_event=ev)
    with pytest.raises(InterruptedError):
        RoughCutCleaner(cfg).clean_batch([sc], cancel_event=ev)
    # mid-scene: the event is set after a few frames are decoded
    ev2 = threading.Event()
    real_cap = cv2.VideoCapture
    calls = {"n": 0}

    class Cap:
        def __init__(self, *a):
            self._c = real_cap(*a)

        def __getattr__(self, name):
            return getattr(self._c, name)

        def read(self):
            calls["n"] += 1
            if calls["n"] == 12:
                ev2.set()
            return self._c.read()

    with patch("src.modules.rough_cut_cleaner.cv2.VideoCapture", Cap), pytest.raises(InterruptedError):
        RoughCutCleaner(cfg).inspect_scene(sc, cancel_event=ev2)
    assert calls["n"] < 60  # stopped long before the 120 frames


# ------------------------------------------------------------------ save -> reload -> sequence


def test_sequence_from_saved_golden_scenes_does_not_ingest(tmp_path, media):
    cfg = staged_config(tmp_path)
    rough, _ = run_stage(tmp_path, media, "rough_cut", soundtrack=None, config=cfg)
    assert rough.success
    proj = save_result_as_project(rough, cfg, tmp_path / "p.tteproj", [media["footage"]], None, tmp_path / "out")
    data = load_project_file(proj)
    assert data.pipeline_stage == "rough_cut" and data.active_timeline_key == "rough_cut"
    scenes = [ProjectManager.deserialize_scored_scene(s)[0] for s in data.scored_scenes]
    existing = data.get_edit_timelines()
    assert list(existing) == ["rough_cut"]

    from src.modules.ingest_detector import IngestDetector

    with patch.object(
        IngestDetector, "batch_process_videos", side_effect=AssertionError("must not re-ingest")
    ) as spy:
        res, _ = run_stage(
            tmp_path, media, "sequence", config=cfg, scenes=scenes, existing_timelines=existing,
            out=tmp_path / "out_seq",
        )
    assert not spy.called
    assert res.success, res.error_message
    assert res.stage == "sequence"
    assert {"rough_cut", "master"} <= set(res.edit_timelines)
    assert res.edit_timelines["rough_cut"] is existing["rough_cut"]  # merged, not rebuilt
    assert res.edit_timelines["master"].kind == "music" and res.edit_timelines["master"].soundtrack_path
    assert len(res.edit_timelines) >= 3  # + at least one variant
    assert res.audio_analysis is not None and res.bar_grid is not None
    # every scene stays in the result (curation list); golden metadata survives the round trip
    assert len(res.scored_scenes) == len(scenes)
    assert all("golden" in s.metadata for s in res.scored_scenes)
    clip_sources = {c.source_path.resolve() for c in res.edit_timelines["master"].clips}
    assert clip_sources == {(media["footage"] / "cam_a.mp4").resolve()}


def test_sequence_respects_excluded_scenes_and_requires_soundtrack(tmp_path, media):
    cfg = staged_config(tmp_path)
    rough, _ = run_stage(tmp_path, media, "rough_cut", soundtrack=None, config=cfg)
    scenes = rough.scored_scenes
    keep, drop = scenes[:-1], scenes[-1]
    drop.metadata["included"] = False
    res, _ = run_stage(tmp_path, media, "sequence", config=cfg, scenes=scenes, out=tmp_path / "o")
    assert res.success, res.error_message
    used = {round(c.scene_start, 2) for c in res.edit_timelines["master"].clips if c.scene_start is not None}
    assert round(drop.scene.start_time, 2) not in used or len(keep) == 0
    assert len(res.scored_scenes) == len(scenes)  # excluded scene kept in the list, flagged
    assert any(s.metadata.get("included") is False for s in res.scored_scenes)

    bad = StudioBackendAdapter().submit_job(
        media["footage"], None, tmp_path / "o2", cfg, stage="sequence", scenes=scenes
    ).result
    assert bad.success is False and "soundtrack" in bad.error_message

    empty = StudioBackendAdapter().submit_job(
        media["footage"], media["music"], tmp_path / "o3", cfg, stage="sequence", scenes=[]
    ).result
    assert empty.success is False and "scenes" in empty.error_message


def test_sequence_without_scenes_ingests_as_a_convenience_without_cleaner(tmp_path, media):
    cfg = staged_config(tmp_path)
    with patch.object(RoughCutCleaner, "clean_batch", side_effect=AssertionError("no cleaner")):
        res, _ = run_stage(tmp_path, media, "sequence", config=cfg)
    assert res.success, res.error_message
    assert res.stage == "sequence" and "rough_cut" not in res.edit_timelines
    assert res.cleaned_segments == []


def test_old_master_and_variants_are_replaced_on_resequence(tmp_path, media):
    cfg = staged_config(tmp_path)
    rough, _ = run_stage(tmp_path, media, "rough_cut", soundtrack=None, config=cfg)
    stale = EditTimeline("old", 30.0, 320, 240, clips=[], variant_key="master")
    stale_var = EditTimeline("oldv", 30.0, 320, 240, clips=[], variant_key="ghost")
    rc = rough.edit_timelines["rough_cut"]
    res, _ = run_stage(
        tmp_path, media, "sequence", config=cfg, scenes=rough.scored_scenes,
        existing_timelines={"rough_cut": rc, "master": stale, "ghost": stale_var}, out=tmp_path / "o",
    )
    assert res.success, res.error_message
    assert res.edit_timelines["master"] is not stale and "ghost" not in res.edit_timelines
    assert res.edit_timelines["rough_cut"] is rc


# ------------------------------------------------------------------ stage "all"


def test_stage_all_flag_false_is_the_classic_flow(tmp_path, media):
    cfg = staged_config(tmp_path)
    assert cfg.run_rough_cut_in_all is False
    with patch.object(RoughCutCleaner, "clean_batch", side_effect=AssertionError("no cleaner")):
        res, _ = run_stage(tmp_path, media, "all", config=cfg)
    assert res.success, res.error_message
    assert res.stage == "all" and "rough_cut" not in res.edit_timelines
    assert res.cleaned_segments == [] and res.audio_analysis is not None
    assert all("golden" not in s.metadata for s in res.scored_scenes)


def test_stage_all_with_rough_cut_flag_runs_both(tmp_path, media):
    from src.core.service import JobCallbacks

    stages: list[tuple[str, int, int]] = []
    cb = JobCallbacks(on_stage_changed=lambda n, i, t: stages.append((n, i, t)))
    cfg = staged_config(tmp_path, run_rough_cut_in_all=True)
    res = StudioBackendAdapter().submit_job(
        media["footage"], media["music"], tmp_path / "out", cfg, callbacks=cb, stage="all"
    ).result
    assert res.success, res.error_message
    assert {"rough_cut", "master"} <= set(res.edit_timelines)
    assert res.edit_timelines["rough_cut"].kind == "rough_cut"
    assert res.cleaned_segments
    assert [s[1] for s in stages] == [1, 2, 3, 4, 5, 6, 7] and {s[2] for s in stages} == {7}
    assert all("golden" in s.metadata for s in res.scored_scenes)
    assert res.audio_analysis is not None


def test_legacy_rough_cut_workflow_mode_is_unchanged(tmp_path, media):
    cfg = staged_config(tmp_path)
    cfg.apply_workflow_mode("rough_cut")
    res = StudioBackendAdapter().submit_job(media["footage"], None, tmp_path / "out", cfg).result
    assert res.success, res.error_message
    assert list(res.edit_timelines) == ["master"] and res.edit_timelines["master"].kind == "rough_cut"
    assert res.stage == "all"


def test_unknown_stage_is_rejected(tmp_path, media):
    with pytest.raises(ValueError):
        StudioBackendAdapter().submit_job(
            media["footage"], None, tmp_path / "out", staged_config(tmp_path), stage="bogus"  # type: ignore[arg-type]
        )


# ------------------------------------------------------------------ apply_scene_curation (kind aware)


def _clip(path: str, start: float, frames: int = 30) -> EditClip:
    return EditClip(
        source_path=Path(path), source_in=start, frames=frames, available=5.0,
        scene_start=start, scene_end=start + 5.0,
    )


def _scene(path: str, start: float) -> ScoredScene:
    return ScoredScene(SceneCut(Path(path), start, start + 5.0, 0, 150, 5.0), 1.0, 1.0, 1.0)


def test_curation_on_rough_cut_drops_excluded_clips_without_refill():
    a, b, c = _scene("a.mp4", 0.0), _scene("a.mp4", 10.0), _scene("b.mp4", 0.0)
    rough = EditTimeline(
        "rc", 30.0, 1920, 1080, kind="rough_cut", variant_key="rough_cut",
        clips=[_clip("a.mp4", 0.0), _clip("a.mp4", 10.0), _clip("b.mp4", 0.0)],
    )
    out, n = apply_scene_curation(rough, included=[a, c], excluded=[b])
    assert n == 1 and out.kind == "rough_cut" and out.variant_key == "rough_cut"
    assert [cl.scene_start for cl in out.clips] == [0.0, 0.0]
    assert [cl.source_path.name for cl in out.clips] == ["a.mp4", "b.mp4"]
    assert len(rough.clips) == 3  # input untouched
    with pytest.raises(ValueError):
        apply_scene_curation(rough, included=[], excluded=[a, b, c])


def test_curation_on_music_timeline_still_refills():
    a, b = _scene("a.mp4", 0.0), _scene("b.mp4", 0.0)
    tl = EditTimeline("m", 30.0, 1920, 1080, clips=[_clip("a.mp4", 0.0), _clip("b.mp4", 0.0)])
    out, n = apply_scene_curation(tl, included=[a], excluded=[b])
    assert n == 1 and len(out.clips) == 2
    assert all(cl.source_path.name == "a.mp4" for cl in out.clips)


# ------------------------------------------------------------------ project persistence (B2)


def test_project_roundtrip_keeps_real_beats_bar_grid_and_stage(tmp_path, media):
    cfg = staged_config(tmp_path, run_rough_cut_in_all=True)
    res = StudioBackendAdapter().submit_job(
        media["footage"], media["music"], tmp_path / "out", cfg, stage="all"
    ).result
    assert res.success, res.error_message
    proj = save_result_as_project(res, cfg, tmp_path / "p.tteproj", [media["footage"]], media["music"], tmp_path / "out")
    raw = json.loads(proj.read_text(encoding="utf-8"))
    assert raw["version"] == "2.3.0" and raw["pipeline_stage"] == "sequence"
    st = raw["audio_state"]
    assert st["beats"] and any(b["is_downbeat"] for b in st["beats"])
    assert st["bar_grid"]["bar_times"] and st["cut_timestamps"] and st["tempo_bpm"] > 0

    data = load_project_file(proj)
    audio = data.get_audio_state()
    assert audio is not None
    assert [b.timestamp for b in audio.beats] == pytest.approx([b.timestamp for b in res.audio_analysis.beats])
    assert [b.is_downbeat for b in audio.beats] == [b.is_downbeat for b in res.audio_analysis.beats]
    assert audio.bar_grid == res.bar_grid
    assert audio.sections == res.sections
    assert audio.cut_timestamps == pytest.approx(res.cut_timestamps)
    assert audio.cadence_cuts == res.audio_analysis.cadence_cuts
    assert audio.tempo_bpm == pytest.approx(res.audio_bpm)
    # timelines + golden scene metadata + included flags
    assert {"rough_cut", "master"} <= set(data.get_edit_timelines())
    assert all("golden" in s["metadata"] and s["included"] is True for s in data.scored_scenes)
    assert data.active_timeline_key == "master"


def test_old_project_versions_load_without_audio_state(tmp_path):
    for version in ("2.0.0", "2.1.0", "2.2.0"):
        p = tmp_path / f"{version}.tteproj"
        p.write_text(json.dumps({"project_name": "old", "version": version}), encoding="utf-8")
        data = load_project_file(p)
        assert data.version == version  # a stored version string is kept
        assert data.pipeline_stage == "ingest" and data.audio_state == {}
        assert data.get_audio_state() is None
    assert ProjectData().version == "2.3.0"
    bad = ProjectData.from_dict({"pipeline_stage": "nonsense", "audio_state": {"beats": [{"x": 1}]}})
    assert bad.pipeline_stage == "ingest" and bad.get_audio_state() is None  # invalid -> ignored


def test_project_from_result_flags_excluded_scenes(tmp_path):
    cut = SceneCut(tmp_path / "a.mp4", 0.0, 5.0, 0, 150, 5.0)
    keep, drop = ScoredScene(cut, 1.0, 1.0, 1.0), ScoredScene(cut, 1.0, 1.0, 1.0, metadata={"included": False})
    res = PipelineExecutionResult(
        success=True, total_duration=1.0, clip_count=1, scenes_detected=2, audio_bpm=0.0, sections_detected=0,
        fcpxml_path=None, premiere_xml_path=None, otio_path=None, capcut_draft_path=None, render_path=None,
        encoder_used=None, resolve_status=None, scored_scenes=[keep, drop],
        edit_timelines={"rough_cut": EditTimeline("rc", 30.0, 1920, 1080, kind="rough_cut", variant_key="rough_cut")},
        stage="rough_cut",
    )
    from src.ui.controllers.project_manager import project_from_result

    data = project_from_result(res, PipelineConfig(), "p", [tmp_path], None, tmp_path)
    assert [s["included"] for s in data.scored_scenes] == [True, False]
    assert data.included_scene_indices == [0]
    assert data.pipeline_stage == "rough_cut" and data.active_timeline_key == "rough_cut"
    assert data.audio_state == {} and data.audio_analysis == {}


# ------------------------------------------------------------------ CLI (B4)


def _cli_env(tmp_path, monkeypatch):
    import main

    captured: dict = {}

    def fake_submit(self, **kwargs):
        captured.update(kwargs)

        class H:
            result = PipelineExecutionResult(
                success=True, total_duration=1.0, clip_count=1, scenes_detected=1, audio_bpm=0.0,
                sections_detected=0, fcpxml_path=None, premiere_xml_path=None, otio_path=None,
                capcut_draft_path=None, render_path=None, encoder_used=None, resolve_status=None,
                edit_timelines={"rough_cut": EditTimeline("rc", 30.0, 1920, 1080, kind="rough_cut",
                                                          variant_key="rough_cut")},
                stage=kwargs.get("stage", "all"),
            )

        return H()

    monkeypatch.setattr(StudioBackendAdapter, "submit_job", fake_submit)
    monkeypatch.setattr(main, "_save_cli_project", lambda *a, **k: captured.setdefault("saved", True))
    footage = tmp_path / "f"
    footage.mkdir()
    return main, captured, footage


def test_cli_run_stage_rough_cut_needs_no_soundtrack(tmp_path, monkeypatch):
    main, captured, footage = _cli_env(tmp_path, monkeypatch)
    r = CliRunner().invoke(
        main.cli, ["run", "--input-dir", str(footage), "--stage", "rough_cut", "--output-dir", str(tmp_path / "o")]
    )
    assert r.exit_code == 0, r.output
    assert captured["stage"] == "rough_cut" and captured["soundtrack"] is None and captured["saved"]


def test_cli_run_requires_soundtrack_for_other_stages(tmp_path, monkeypatch):
    main, captured, footage = _cli_env(tmp_path, monkeypatch)
    for stage in ("sequence", "all"):
        r = CliRunner().invoke(
            main.cli, ["run", "--input-dir", str(footage), "--stage", stage, "--output-dir", str(tmp_path / "o")]
        )
        assert r.exit_code != 0 and "soundtrack" in r.output.lower()
    assert "stage" not in captured


def test_cli_run_maps_rough_cut_cadence_genre(tmp_path, monkeypatch):
    main, captured, footage = _cli_env(tmp_path, monkeypatch)
    music = tmp_path / "m.wav"
    music.write_bytes(b"x")
    base = ["run", "--input-dir", str(footage), "--soundtrack", str(music), "--output-dir", str(tmp_path / "o")]
    r = CliRunner().invoke(main.cli, [*base, "--rough-cut", "--cadence", "viral_burst", "--genre", "sports_extreme"])
    assert r.exit_code == 0, r.output
    cfg = captured["config"]
    assert captured["stage"] == "all"
    assert cfg.run_rough_cut_in_all is True and cfg.cadence_profile == "viral_burst"
    assert cfg.cadence_multiples is None  # explicit cadence beats the genre's multiples
    assert cfg.preset_name == "1080p30"  # genre drives pacing, not the canvas
    captured.clear()
    r = CliRunner().invoke(main.cli, [*base, "--genre", "sports_extreme"])
    assert r.exit_code == 0
    assert captured["config"].cadence_profile == "fast_syncopated"
    assert captured["config"].run_rough_cut_in_all is False
    captured.clear()
    r = CliRunner().invoke(main.cli, [*base, "--no-rough-cut"])
    assert r.exit_code == 0 and captured["config"].run_rough_cut_in_all is False


def test_cli_run_unknown_genre_and_cadence_are_bad_parameters(tmp_path, monkeypatch):
    main, captured, footage = _cli_env(tmp_path, monkeypatch)
    music = tmp_path / "m.wav"
    music.write_bytes(b"x")
    base = ["run", "--input-dir", str(footage), "--soundtrack", str(music), "--output-dir", str(tmp_path / "o")]
    r = CliRunner().invoke(main.cli, [*base, "--genre", "no_such_genre"])
    assert r.exit_code == 2 and "Unknown genre" in r.output
    r = CliRunner().invoke(main.cli, [*base, "--cadence", "warp_speed"])
    assert r.exit_code == 2
    assert "stage" not in captured


# ------------------------------------------------------------------ MCP (B4)


def test_mcp_execute_pipeline_job_passes_staged_options(tmp_path, monkeypatch):
    from src.modules import mcp_server

    captured: dict = {}

    class H:
        job_id = "job_x"
        result = PipelineExecutionResult(
            success=True, total_duration=1.0, clip_count=1, scenes_detected=1, audio_bpm=0.0,
            sections_detected=0, fcpxml_path=None, premiere_xml_path=None, otio_path=None,
            capcut_draft_path=None, render_path=None, encoder_used=None, resolve_status=None,
            edit_timelines={"rough_cut": EditTimeline("rc", 30.0, 1920, 1080, kind="rough_cut",
                                                      variant_key="rough_cut")},
            stage="rough_cut",
        )

        def wait(self, *a):
            return True

    def fake_submit(**kwargs):
        captured.update(kwargs)
        return H()

    monkeypatch.setattr(mcp_server.adapter, "submit_job", fake_submit)
    monkeypatch.setattr(
        "src.ui.controllers.project_manager.save_result_as_project", lambda *a, **k: captured.setdefault("saved", True)
    )
    footage = tmp_path / "f"
    footage.mkdir()
    out = mcp_server.execute_pipeline_job(
        str(footage), output_dir=str(tmp_path / "o"), stage="rough_cut", genre="travel_cinematic",
        cadence="lingering_cinematic", rough_cut=True,
    )
    assert out["success"] and out["stage"] == "rough_cut" and out["timelines"] == ["rough_cut"]
    assert captured["stage"] == "rough_cut" and captured["soundtrack"] is None and captured["saved"]
    cfg = captured["config"]
    assert cfg.run_rough_cut_in_all is True and cfg.cadence_profile == "lingering_cinematic"

    with pytest.raises(ValueError, match="soundtrack"):
        mcp_server.execute_pipeline_job(str(footage), output_dir=str(tmp_path / "o"))
    with pytest.raises(ValueError, match="Unknown genre"):
        mcp_server.execute_pipeline_job(str(footage), "x.wav", genre="nope")
    with pytest.raises(ValueError, match="Unknown stage"):
        mcp_server.execute_pipeline_job(str(footage), "x.wav", stage="bogus")
    doc = mcp_server.execute_pipeline_job.__doc__ or ""
    assert "Analysis only" in doc and "ONLY when the user asks" in doc

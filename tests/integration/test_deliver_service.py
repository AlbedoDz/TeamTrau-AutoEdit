"""Phase 22 step 7: the Deliver service with real ffmpeg (settings, retarget, variants, manifest)."""

import json
from pathlib import Path

import pytest

from src.core.config import PipelineConfig
from src.core.service import StudioBackendAdapter
from src.core.types import DeliverSettings, EditClip, EditTimeline, SentenceSegment
from tests.integration.conftest import stream_info


def _cfg(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(cache_dir=tmp_path / "cache", keep_video_audio=True, enable_teaser=False)


def _timeline(a: Path, b: Path, key: str = "master", fps: float = 29.97, w: int = 320, h: int = 180) -> EditTimeline:
    n = round(fps)
    return EditTimeline(
        name=f"Cut_{key}",
        fps=fps,
        canvas_width=w,
        canvas_height=h,
        clips=[EditClip(a, 0.0, n, 2.0, label="a"), EditClip(b, 0.0, n, 2.0, label="b")],
        variant_key=key,
    )


def _deliver(tmp_path: Path, tl: EditTimeline, out: Path, formats: set, **kw):
    handle = StudioBackendAdapter().submit_deliver_job(
        tl, out, _cfg(tmp_path), formats=formats, force_cpu=True, run_async=False, **kw
    )
    assert handle.result is not None
    return handle.result


def test_deliver_prores_master_nle_files_and_manifest(tmp_path, make_clip):
    a, b = make_clip("a.mp4"), make_clip("b.mp4")
    tl = _timeline(a, b)
    st = DeliverSettings(canvas_width=320, canvas_height=180, fps=29.97, master="prores_422", color="rec709")
    out = tmp_path / "out"
    res = _deliver(tmp_path, tl, out, {"mp4", "fcpxml", "premiere", "otio"}, settings=st, base_name="proj")

    assert res.success, res.error_message
    assert res.render_path is not None and res.render_path.suffix == ".mov"
    assert stream_info(res.render_path, "video")["codec_name"] == "prores"
    assert res.master_codec == "prores_422"
    assert res.fcpxml_path and res.premiere_xml_path and res.otio_path
    assert all(p.is_file() for p in (res.fcpxml_path, res.premiere_xml_path, res.otio_path))

    assert res.manifest_path == out / "deliver_manifest.json"
    manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
    assert manifest["settings"]["master"] == "prores_422"
    assert manifest["settings"]["fps"] == 29.97
    assert manifest["formats"] == ["fcpxml", "mp4", "otio", "premiere"]
    assert manifest["files"]["master"] == "proj.mov"
    assert manifest["files"]["fcpxml"] == "proj.fcpxml"
    assert manifest["exporters"]["fcpxml"] == "1.8"
    assert manifest["watermark_in_nle"] == {"requested": False, "applied": False}
    assert manifest["created_at"]


def test_retarget_to_25fps_and_vertical_canvas(tmp_path, make_clip):
    a, b = make_clip("a.mp4"), make_clip("b.mp4")
    tl = _timeline(a, b, fps=29.97)  # 2 x 30 frames = ~2.002 s
    st = DeliverSettings(canvas_width=1080, canvas_height=1920, fps=25.0, master="h264", fit_mode="fill")
    res = _deliver(tmp_path, tl, tmp_path / "out", {"mp4", "fcpxml"}, settings=st, base_name="vert")

    assert res.success, res.error_message
    video = stream_info(res.render_path, "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["r_frame_rate"] == "25/1"
    assert video["codec_name"] == "h264"
    fcpxml = res.fcpxml_path.read_text(encoding="utf-8")
    assert 'width="1080"' in fcpxml and 'height="1920"' in fcpxml
    assert 'frameDuration="1/25s"' in fcpxml


def test_variants_all_go_to_subfolders(tmp_path, make_clip):
    a, b = make_clip("a.mp4"), make_clip("b.mp4")
    active = _timeline(a, b, key="master")
    other = _timeline(b, a, key="cinematic")
    st = DeliverSettings(canvas_width=320, canvas_height=180, fps=29.97, variants=["all"])
    out = tmp_path / "out"
    res = _deliver(
        tmp_path, active, out, {"fcpxml", "otio"}, settings=st, base_name="proj",
        variant_timelines={"master": active, "cinematic": other},
    )

    assert res.success, res.error_message
    assert set(res.variant_results) == {"master", "cinematic"}
    assert (out / "master" / "proj_master.fcpxml").is_file()
    assert (out / "cinematic" / "proj_cinematic.fcpxml").is_file()
    assert (out / "cinematic" / "deliver_manifest.json").is_file()
    top = json.loads((out / "deliver_manifest.json").read_text(encoding="utf-8"))
    assert top["variants"] == {
        "master": "master/deliver_manifest.json",
        "cinematic": "cinematic/deliver_manifest.json",
    }


def test_active_variant_only_keeps_flat_layout(tmp_path, make_clip):
    a, b = make_clip("a.mp4"), make_clip("b.mp4")
    tl = _timeline(a, b)
    out = tmp_path / "out"
    res = _deliver(tmp_path, tl, out, {"otio"}, base_name="flat")  # no settings at all
    assert res.success and res.otio_path == out / "flat.otio"
    assert not res.variant_results
    assert json.loads((out / "deliver_manifest.json").read_text(encoding="utf-8"))["variant"] == "master"


def test_unknown_variant_fails_cleanly(tmp_path, make_clip):
    a, b = make_clip("a.mp4"), make_clip("b.mp4")
    st = DeliverSettings(canvas_width=320, canvas_height=180, fps=29.97, variants=["nope"])
    res = _deliver(tmp_path, _timeline(a, b), tmp_path / "out", {"otio"}, settings=st)
    assert not res.success and "Unknown variant 'nope'" in (res.error_message or "")


def test_subtitle_sidecars_written_beside_nle_files(tmp_path, make_clip):
    a, b = make_clip("a.mp4"), make_clip("b.mp4")
    st = DeliverSettings(canvas_width=320, canvas_height=180, fps=29.97, subtitles="srt")
    sentences = [SentenceSegment("xin chao", 0.0, 1.0, 1.0)]
    out = tmp_path / "out"
    res = _deliver(tmp_path, _timeline(a, b), out, {"otio"}, settings=st, sentences=sentences, base_name="sub")
    assert res.success, res.error_message
    assert res.subtitle_paths == [out / "sub.srt"]
    assert "xin chao" in (out / "sub.srt").read_text(encoding="utf-8")


def test_qa_uses_deliver_fps_not_config_fps(tmp_path, make_clip, monkeypatch):
    a, b = make_clip("a.mp4"), make_clip("b.mp4")
    seen: list[float] = []
    from src.modules import timeline_qa

    real_init = timeline_qa.TimelineQA.__init__

    def spy(self, config=None):
        real_init(self, config)
        seen.append(float(self.config.fps))

    monkeypatch.setattr(timeline_qa.TimelineQA, "__init__", spy)
    st = DeliverSettings(canvas_width=320, canvas_height=180, fps=25.0)
    res = _deliver(tmp_path, _timeline(a, b), tmp_path / "out", {"otio"}, settings=st)  # config fps = 30
    assert res.success, res.error_message
    assert seen == [25.0]


def test_variant_keys_resolution():
    resolve = StudioBackendAdapter.resolve_variant_keys
    tls = {"master": object(), "cinematic": object(), "social": object()}
    assert resolve("master", None, tls) == ["master"]
    only = DeliverSettings(canvas_width=320, canvas_height=180, fps=30.0, variants=["social", "active"])
    assert resolve("master", only, tls) == ["social", "master"]
    everything = DeliverSettings(canvas_width=320, canvas_height=180, fps=30.0, variants=["all"])
    assert resolve("cinematic", everything, tls) == ["cinematic", "master", "social"]
    bad = DeliverSettings(canvas_width=320, canvas_height=180, fps=30.0, variants=["zzz"])
    with pytest.raises(ValueError, match="zzz"):
        resolve("master", bad, tls)

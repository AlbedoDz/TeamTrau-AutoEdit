"""P5.5 (real ffmpeg): every smoke-tested hardware encoder renders a master; failures fall back to CPU."""

import pytest

from src.core.config import PipelineConfig
from src.core.platform_caps import get_caps
from src.core.types import DeliverSettings, EditClip, EditTimeline
from src.dispatchers.encoder_ladder import HW_FAMILIES
from src.dispatchers.nvenc_renderer import NvencRenderer
from tests.integration.conftest import stream_info

_CAPS = get_caps()
_HW = [
    (codec, enc)
    for codec, encs in (("h264", _CAPS.h264_encoders), ("hevc", _CAPS.hevc_encoders))
    for enc in encs
    if enc.rsplit("_", 1)[-1] in HW_FAMILIES
]


def _render(tmp_path, make_clip, family: str, master: str, color: str = "rec709"):
    clip = make_clip("a.mp4", duration=3.0)
    s = DeliverSettings(canvas_width=320, canvas_height=180, fps=30.0, master=master, color=color)
    tl = EditTimeline("t", s.fps, s.canvas_width, s.canvas_height, clips=[EditClip(clip, 0.0, 60, 2.0)])
    cfg = PipelineConfig(cache_dir=tmp_path / "cache", enable_teaser=False, hw_encoder=family)
    return NvencRenderer(cfg, settings=s).render_edit_timeline(tl, tmp_path / "out" / "m.mp4", settings=s)


@pytest.mark.skipif(not _HW, reason="no smoke-tested hardware encoder on this machine")
@pytest.mark.parametrize("codec, encoder", _HW)
def test_each_hardware_encoder_renders_a_master(tmp_path, make_clip, codec, encoder):
    family = encoder.rsplit("_", 1)[-1]
    res = _render(tmp_path, make_clip, family, codec)
    assert res.encoder_used == encoder
    v = stream_info(res.output_path, "video")
    assert v["codec_name"] == codec
    assert (v["width"], v["height"]) == (320, 180)


@pytest.mark.skipif(not _HW, reason="no smoke-tested hardware encoder on this machine")
def test_ten_bit_hdr_master_renders_on_hardware_or_falls_back_to_cpu(tmp_path, make_clip):
    hevc = [enc for codec, enc in _HW if codec == "hevc"]
    if not hevc:
        pytest.skip("no hardware HEVC encoder")
    family = hevc[-1].rsplit("_", 1)[-1]  # the lowest-priority one, e.g. AMF on an NVIDIA + AMD laptop
    res = _render(tmp_path, make_clip, family, "hevc", color="rec2020_hlg")
    assert res.encoder_used in (hevc[-1], "libx265")
    v = stream_info(res.output_path, "video")
    assert v["codec_name"] == "hevc" and v["pix_fmt"] == "yuv420p10le"

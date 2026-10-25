"""P5.5: renderer hardware encoder ladder (NVENC > VideoToolbox > QSV > AMF > CPU) from platform_caps."""

import pytest

from src.core.config import PipelineConfig
from src.core.ffmpeg_utils import is_hw_encoder_error
from src.core.platform_caps import PlatformCaps
from src.core.types import DeliverSettings
from src.dispatchers import nvenc_renderer
from src.dispatchers.encoder_ladder import HW_FAMILIES, hw_video_args, pick_hw_encoder
from src.dispatchers.nvenc_renderer import NvencRenderer


def test_families_in_ladder_order_without_vaapi():
    assert HW_FAMILIES == ("nvenc", "videotoolbox", "qsv", "amf")


@pytest.mark.parametrize(
    "codec, available, preference, expected",
    [
        ("hevc", ("hevc_nvenc", "hevc_amf", "libx265"), "auto", "hevc_nvenc"),
        ("hevc", ("hevc_amf", "libx265"), "auto", "hevc_amf"),
        ("h264", ("h264_videotoolbox", "libx264"), "auto", "h264_videotoolbox"),
        ("h264", ("h264_qsv", "h264_amf", "libx264"), "auto", "h264_qsv"),
        ("hevc", ("hevc_vaapi", "libx265"), "auto", None),
        ("hevc", ("libx265",), "auto", None),
        ("hevc", (), "auto", None),
        ("hevc", ("hevc_nvenc", "hevc_amf", "libx265"), "amf", "hevc_amf"),
        ("hevc", ("hevc_nvenc", "libx265"), "amf", None),
        ("hevc", ("hevc_nvenc", "hevc_amf", "libx265"), "none", None),
        ("h264", ("h264_amf", "libx264"), "nvenc", "h264_nvenc"),
        ("h264", (), "nvenc", "h264_nvenc"),
    ],
)
def test_pick_hw_encoder(codec, available, preference, expected):
    assert pick_hw_encoder(codec, available, preference) == expected


def _args(encoder, **kw):
    base = {"cq": 18, "bitrate": "60M", "maxrate": "80M", "bufsize": "120M", "ten_bit": False,
            "intra_only": False}
    base.update(kw)
    return hw_video_args(encoder, **base)


def _opt(args, flag):
    return args[args.index(flag) + 1]


def test_amf_constant_qp_quality_preset():
    a = _args("hevc_amf")
    assert a[:2] == ["-c:v", "hevc_amf"]
    assert (_opt(a, "-quality"), _opt(a, "-rc"), _opt(a, "-qp_i"), _opt(a, "-qp_p")) == ("quality", "cqp", "18", "18")
    assert (_opt(a, "-profile:v"), _opt(a, "-pix_fmt")) == ("main", "nv12")
    a10 = _args("hevc_amf", ten_bit=True)
    assert (_opt(a10, "-profile:v"), _opt(a10, "-pix_fmt")) == ("main10", "p010le")
    h = _args("h264_amf", intra_only=True)
    assert (_opt(h, "-qp_b"), _opt(h, "-profile:v"), _opt(h, "-g"), _opt(h, "-bf")) == ("18", "high", "1", "0")


def test_qsv_icq_quality():
    a = _args("hevc_qsv", ten_bit=True)
    assert a[:2] == ["-c:v", "hevc_qsv"]
    assert (_opt(a, "-global_quality"), _opt(a, "-profile:v"), _opt(a, "-pix_fmt")) == ("18", "main10", "p010le")
    h = _args("h264_qsv")
    assert (_opt(h, "-profile:v"), _opt(h, "-pix_fmt")) == ("high", "nv12")


def test_videotoolbox_bitrate_hvc1_tag_and_no_software_fallback():
    a = _args("hevc_videotoolbox", ten_bit=True)
    assert a[:2] == ["-c:v", "hevc_videotoolbox"]
    assert (_opt(a, "-b:v"), _opt(a, "-maxrate"), _opt(a, "-bufsize")) == ("60M", "80M", "120M")
    assert (_opt(a, "-profile:v"), _opt(a, "-tag:v"), _opt(a, "-allow_sw"), _opt(a, "-pix_fmt")) == (
        "main10", "hvc1", "0", "p010le")
    preview = _args("h264_videotoolbox", bitrate=None, maxrate=None, bufsize=None)
    assert _opt(preview, "-b:v") == "8M" and "-maxrate" not in preview and "-tag:v" not in preview


@pytest.mark.parametrize("encoder", ["hevc_nvenc", "libx265", "hevc_vaapi", "prores_ks"])
def test_hw_video_args_rejects_encoders_it_does_not_own(encoder):
    with pytest.raises(ValueError):
        _args(encoder)


@pytest.mark.parametrize(
    "stderr",
    [
        "[hevc_amf @ 0x1] 10-bit encoder is not supported by AMD GPU drivers versions lower than 23.30.",
        "[hevc_qsv @ 0x1] Error initializing an internal MFX session: unsupported (-3)",
        "[h264_videotoolbox @ 0x1] Error: cannot create compression session: -12908",
        "[h264_nvenc @ 0x1] OpenEncodeSessionEx failed: unsupported device (2)",
    ],
)
def test_hardware_failures_trigger_the_cpu_fallback(stderr):
    assert is_hw_encoder_error(stderr)


def test_plain_ffmpeg_errors_do_not_trigger_the_cpu_fallback():
    assert not is_hw_encoder_error("No such file or directory")


def _caps(h264, hevc):
    return PlatformCaps(os="windows", ffmpeg="ffmpeg", h264_encoders=h264, hevc_encoders=hevc,
                        hwaccels=(), torch_device="cpu")


def test_renderer_uses_the_caps_ladder_for_masters(monkeypatch):
    monkeypatch.setattr(nvenc_renderer, "get_caps", lambda: _caps(("h264_amf", "libx264"), ("hevc_amf", "libx265")))
    monkeypatch.setattr(nvenc_renderer, "check_nvenc_available", lambda: False)
    s = DeliverSettings(canvas_width=640, canvas_height=360, fps=30.0, master="hevc", color="rec709")
    r = NvencRenderer(PipelineConfig(), settings=s)
    assert r.hw_encoders == {"h264": "h264_amf", "hevc": "hevc_amf"}
    assert r.hardware_available is True
    enc, args = r.master_encoder_args(s, True)
    assert enc == "hevc_amf" and args[:2] == ["-c:v", "hevc_amf"]
    assert "-color_primaries" in args
    cpu, _ = r.master_encoder_args(s, False)
    assert cpu == "libx265"
    assert r.master_encoder_args(s, True, preview=True)[0] == "h264_amf"


def test_codec_without_a_selected_encoder_goes_to_cpu_not_nvenc(monkeypatch):
    monkeypatch.setattr(nvenc_renderer, "get_caps", lambda: _caps(("h264_amf", "libx264"), ("libx265",)))
    monkeypatch.setattr(nvenc_renderer, "check_nvenc_available", lambda: False)
    s = DeliverSettings(canvas_width=640, canvas_height=360, fps=30.0, master="hevc", color="rec709")
    assert NvencRenderer(PipelineConfig(), settings=s).master_encoder_args(s, True)[0] == "libx265"
    monkeypatch.setattr(nvenc_renderer, "check_nvenc_available", lambda: True)
    amf = NvencRenderer(PipelineConfig(hw_encoder="amf"), settings=s)
    assert amf.hw_encoders == {"h264": "h264_amf", "hevc": None}
    assert amf.master_encoder_args(s, True)[0] == "libx265"
    vt = NvencRenderer(PipelineConfig(hw_encoder="videotoolbox"), settings=s)
    assert vt.hardware_available is False
    assert vt.master_encoder_args(s, True)[0] == "libx265"


def test_preference_none_disables_hardware_and_nvenc_skips_the_probe(monkeypatch):
    def boom():
        raise AssertionError("caps must not be probed")

    monkeypatch.setattr(nvenc_renderer, "get_caps", boom)
    monkeypatch.setattr(nvenc_renderer, "check_nvenc_available", lambda: True)
    off = NvencRenderer(PipelineConfig(hw_encoder="none"))
    assert off.hw_encoders == {"h264": None, "hevc": None}
    assert off.hardware_available is False
    nv = NvencRenderer(PipelineConfig(hw_encoder="nvenc"))
    assert nv.hw_encoders == {"h264": "h264_nvenc", "hevc": "hevc_nvenc"}
    assert nv.hardware_available is True
    assert nv.encoder_args(True)[0] == "hevc_nvenc"


def test_hw_encoder_setting_is_a_closed_set():
    with pytest.raises(ValueError):
        PipelineConfig(hw_encoder="vaapi")

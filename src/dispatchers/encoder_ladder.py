"""Hardware encoder ladder selection and argument builders for VideoToolbox, QSV, and AMF."""

from typing import Literal

HwFamily = Literal["nvenc", "videotoolbox", "qsv", "amf"]
HW_FAMILIES: tuple[HwFamily, ...] = ("nvenc", "videotoolbox", "qsv", "amf")
_NON_NVENC_FAMILIES: frozenset[str] = frozenset({"videotoolbox", "qsv", "amf"})


def pick_hw_encoder(codec: str, available: tuple[str, ...], preference: str) -> str | None:
    """Select the preferred hardware encoder for ``codec`` ('h264' or 'hevc')."""
    if preference == "none":
        return None
    if preference == "nvenc":
        return f"{codec}_nvenc"
    prefix = f"{codec}_"
    for enc in available:
        if not enc.startswith(prefix):
            continue
        family = enc[len(prefix) :]
        if family not in HW_FAMILIES:
            continue
        if preference in ("auto", family):
            return enc
    return None


def hw_video_args(
    encoder: str,
    *,
    cq: int,
    bitrate: str | None,
    maxrate: str | None,
    bufsize: str | None,
    ten_bit: bool,
    intra_only: bool,
) -> list[str]:
    """Build ffmpeg video encoder arguments for VideoToolbox, QSV, or AMF."""
    if "_" not in encoder:
        raise ValueError(f"Unsupported encoder for hw_video_args: {encoder!r}")
    codec, family = encoder.rsplit("_", 1)
    if codec not in ("h264", "hevc") or family not in _NON_NVENC_FAMILIES:
        raise ValueError(f"Unsupported encoder for hw_video_args: {encoder!r}")

    is_hevc = codec == "hevc"
    is_10bit = is_hevc and ten_bit
    profile = ("main10" if is_10bit else "main") if is_hevc else "high"
    pix_fmt = "p010le" if is_10bit else "nv12"
    q_str = str(cq)

    args: list[str] = ["-c:v", encoder]
    if family == "amf":
        args += ["-quality", "quality", "-rc", "cqp", "-qp_i", q_str, "-qp_p", q_str]
        if not is_hevc:
            args += ["-qp_b", q_str]
    elif family == "qsv":
        args += ["-preset", "medium", "-global_quality", q_str]
    elif family == "videotoolbox":
        args += ["-b:v", bitrate or "8M"]
        if maxrate is not None:
            args += ["-maxrate", maxrate]
        if bufsize is not None:
            args += ["-bufsize", bufsize]
        if is_hevc:
            args += ["-tag:v", "hvc1"]
        args += ["-allow_sw", "0"]

    args += ["-profile:v", profile, "-pix_fmt", pix_fmt]
    if intra_only:
        args += ["-g", "1", "-bf", "0"]
    return args

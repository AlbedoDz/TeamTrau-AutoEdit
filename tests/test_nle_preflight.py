"""NLE pre-flight checks (fake probe, real paths)."""

import json
from fractions import Fraction
from pathlib import Path

from src.core.media_info import MediaInfo
from src.core.nle_preflight import preflight_timeline
from src.core.types import EditClip, EditTimeline


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def info(p: Path, **kw) -> MediaInfo:
    base = dict(path=p, has_video=True, has_audio=True, duration=10.0, width=1920, height=1080,
                fps=Fraction(30), audio_channels=2, audio_rate=48000)
    base.update(kw)
    return MediaInfo(**base)


def tl(*paths: Path, fps: float = 30.0, soundtrack: Path | None = None) -> EditTimeline:
    clips = [EditClip(source_path=p, source_in=0.0, frames=30, available=1.0) for p in paths]
    return EditTimeline(name="t", fps=fps, canvas_width=1920, canvas_height=1080, clips=clips,
                        soundtrack_path=soundtrack)


def probe_from(table: dict[Path, MediaInfo]):
    return lambda q: table[Path(q).resolve()]


def codes(report) -> set[str]:
    return {i.code for i in report.issues}


def test_clean_media_has_no_issues(tmp_path: Path):
    p = _touch(tmp_path / "a.mp4")
    r = preflight_timeline(tl(p), probe=probe_from({p.resolve(): info(p)}))
    assert r.issues == [] and r.ok and r.checked_files == 1


def test_missing_media_is_an_error(tmp_path: Path):
    r = preflight_timeline(tl(tmp_path / "gone.mp4"), probe=probe_from({}))
    assert codes(r) == {"MEDIA_MISSING"}
    assert not r.ok


def test_audio_layout_warnings(tmp_path: Path):
    mono = _touch(tmp_path / "mono.mp4")
    surround = _touch(tmp_path / "surround.mp4")
    table = {mono.resolve(): info(mono, audio_channels=1, audio_rate=44100),
             surround.resolve(): info(surround, audio_channels=6)}
    r = preflight_timeline(tl(mono, surround), probe=probe_from(table))
    assert codes(r) == {"AUDIO_MONO", "AUDIO_RATE_MISMATCH", "AUDIO_MULTICHANNEL"}
    assert r.ok


def test_audio_rate_follows_deliver_setting(tmp_path: Path):
    p = _touch(tmp_path / "a.mp4")
    r = preflight_timeline(tl(p), audio_rate=44100,
                           probe=probe_from({p.resolve(): info(p, audio_rate=44100)}))
    assert r.issues == []


def test_silent_clip_has_no_audio_issues(tmp_path: Path):
    p = _touch(tmp_path / "mute.mp4")
    table = {p.resolve(): info(p, has_audio=False, audio_channels=0, audio_rate=0)}
    assert preflight_timeline(tl(p), probe=probe_from(table)).issues == []


def test_vfr_and_fps_mismatch(tmp_path: Path):
    phone = _touch(tmp_path / "phone.mp4")
    ntsc = _touch(tmp_path / "ntsc.mp4")
    table = {phone.resolve(): info(phone, is_vfr=True),
             ntsc.resolve(): info(ntsc, fps=Fraction(30000, 1001))}
    r = preflight_timeline(tl(phone, ntsc, fps=30.0), probe=probe_from(table))
    assert codes(r) == {"VFR_SOURCE", "FPS_MISMATCH"}


def test_unicode_paths_round_trip(tmp_path: Path):
    p = _touch(tmp_path / "Lễ Cưới Đẹp 2026" / "clip 01 #1.mp4")
    r = preflight_timeline(tl(p), probe=probe_from({p.resolve(): info(p)}))
    assert r.issues == []


def test_each_media_is_probed_once(tmp_path: Path):
    p = _touch(tmp_path / "a.mp4")
    calls: list[Path] = []

    def probe(q):
        calls.append(q)
        return info(p)

    preflight_timeline(tl(p, p, p), probe=probe)
    assert len(calls) == 1


def test_soundtrack_is_checked(tmp_path: Path):
    p = _touch(tmp_path / "a.mp4")
    r = preflight_timeline(tl(p, soundtrack=tmp_path / "missing.mp3"),
                           probe=probe_from({p.resolve(): info(p)}))
    assert codes(r) == {"MEDIA_MISSING"}
    assert r.checked_files == 2


def test_report_is_json_safe(tmp_path: Path):
    p = _touch(tmp_path / "mono.mp4")
    r = preflight_timeline(tl(p), probe=probe_from({p.resolve(): info(p, audio_channels=1)}))
    data = json.loads(json.dumps(r.to_dict()))
    assert data["ok"] is True
    assert data["issues"][0]["code"] == "AUDIO_MONO"

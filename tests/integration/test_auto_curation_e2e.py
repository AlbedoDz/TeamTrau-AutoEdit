"""Folder -> curate -> assemble -> export on real ffmpeg media."""

import re
import shutil
import subprocess
from pathlib import Path

from src.core import curation
from src.core.service import StudioBackendAdapter

FFMPEG = shutil.which("ffmpeg")


def _run(cmd: list[str]) -> None:
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *cmd], check=True)


def _footage(root: Path) -> None:
    (root / "A-roll").mkdir(parents=True)
    (root / "B-roll").mkdir()
    (root / "Music").mkdir()
    _run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=15",
          "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=15",
          "-af", "volume='if(lt(mod(t,1),0.5),1,0)':eval=frame",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15",
          "-c:a", "aac", "-shortest", str(root / "A-roll" / "talk.mp4")])
    _run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=20",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15",
          str(root / "B-roll" / "scenic.mp4")])
    # 120 BPM click track, 30 s.
    _run(["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=30",
          "-af", "volume='if(lt(mod(t,0.5),0.06),1,0)':eval=frame",
          str(root / "Music" / "click.wav")])


def test_folder_to_nle_files(tmp_path: Path):
    root = tmp_path / "Dự án"
    _footage(root)
    adapter = StudioBackendAdapter()
    session, summary = curation.run_curation(adapter, root, "travel_daily_vlog",
                                             tmp_path / "work", timeout=900)
    assert summary["takes"] >= 2
    assert summary["by_role"].get("a_roll", 0) >= 1
    asm = curation.assemble_sequence(adapter, session, timeout=900)
    assert asm["status"] == "TIMELINE_ASSEMBLED_SUCCESS" and asm["total_cuts"] >= 1
    out = curation.export_session(adapter, session, tmp_path / "export", timeout=900)
    assert out["status"] == "EXPORT_COMPLETE"
    assert out["preflight_errors"] == 0
    for key in ("fcpxml_path", "premiere_xml_path", "capcut_draft_path"):
        assert out[key] and Path(out[key]).exists(), key
    fcpxml = Path(out["fcpxml_path"]).read_text(encoding="utf-8")
    srcs = re.findall(r'src="([^"]+)"', fcpxml)
    assert srcs and all(s.startswith("file:///") for s in srcs)

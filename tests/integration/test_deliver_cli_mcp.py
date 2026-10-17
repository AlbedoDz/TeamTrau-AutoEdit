"""Phase 22 step 7: `deliver` CLI and MCP tools driving real ffmpeg from a saved project."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import main as cli_module
from src.core.project_io import edit_timeline_to_dict
from src.core.types import EditClip, EditTimeline
from src.modules.mcp_server import export_timeline, render_master
from src.ui.controllers.project_manager import ProjectData
from tests.integration.conftest import stream_info


@pytest.fixture
def project(tmp_path, make_clip) -> Path:
    a, b = make_clip("a.mp4"), make_clip("b.mp4")

    def tl(key: str, first: Path, second: Path) -> EditTimeline:
        clips = [EditClip(first, 0.0, 30, 2.0, label="x"), EditClip(second, 0.0, 30, 2.0, label="y")]
        return EditTimeline(key.title(), 30.0, 320, 180, clips=clips, variant_key=key)

    data = ProjectData(
        project_name="p",
        output_dir=str(tmp_path / "out"),
        edit_timelines={
            "master": edit_timeline_to_dict(tl("master", a, b)),
            "cinematic": edit_timeline_to_dict(tl("cinematic", b, a)),
        },
        active_timeline_key="master",
    )
    path = tmp_path / "proj.tteproj"
    path.write_text(json.dumps(data.to_dict()), encoding="utf-8")
    return path


def test_cli_deliver_prores_retargeted_with_manifest(project, tmp_path):
    out = tmp_path / "cli_out"
    res = CliRunner().invoke(
        cli_module.cli,
        [
            "deliver", "--project", str(project), "--output-dir", str(out), "--format", "mp4,fcpxml,otio",
            "--master", "prores_422", "--fps", "25", "--canvas", "360x640", "--fit", "fill", "--no-nvenc",
        ],
    )
    assert res.exit_code == 0, res.output
    master = out / "proj_master.mov"
    assert master.is_file()
    video = stream_info(master, "video")
    assert video["codec_name"] == "prores" and (video["width"], video["height"]) == (360, 640)
    assert video["r_frame_rate"] == "25/1"
    assert "Master (prores_422)" in res.output
    assert (out / "proj_master.fcpxml").is_file() and (out / "proj_master.otio").is_file()
    manifest = json.loads((out / "deliver_manifest.json").read_text(encoding="utf-8"))
    assert manifest["settings"]["fps"] == 25.0 and manifest["files"]["master"] == "proj_master.mov"


def test_cli_deliver_all_variants_into_subfolders(project, tmp_path):
    out = tmp_path / "cli_all"
    res = CliRunner().invoke(
        cli_module.cli,
        ["deliver", "--project", str(project), "--output-dir", str(out), "--format", "otio", "--variants", "all"],
    )
    assert res.exit_code == 0, res.output
    assert (out / "master" / "proj_master.otio").is_file()
    assert (out / "cinematic" / "proj_cinematic.otio").is_file()
    assert "[cinematic]" in res.output


def test_mcp_export_timeline_with_settings(project, tmp_path):
    out = tmp_path / "mcp_nle"
    res = export_timeline(
        output_dir=str(out), project_path=str(project), canvas="1080x1920", fps="29.97",
        fit="fill", color="rec709", relink_original=True,
    )
    assert Path(res["fcpxml_path"]).is_file() and Path(res["premiere_xml_path"]).is_file()
    assert Path(res["otio_path"]).is_file() and Path(res["manifest_path"]).is_file()
    fcpxml = Path(res["fcpxml_path"]).read_text(encoding="utf-8")
    assert 'width="1080"' in fcpxml and 'height="1920"' in fcpxml and "1001/30000s" in fcpxml
    manifest = json.loads(Path(res["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["settings"]["canvas_height"] == 1920 and manifest["settings"]["fit_mode"] == "fill"


def test_mcp_render_master_prores_follows_container(project, tmp_path):
    target = tmp_path / "mcp_master" / "final.mp4"  # asks for .mp4, ProRes is a .mov
    res = render_master(
        output_file=str(target), project_path=str(project), no_nvenc=True, master="prores_422",
        canvas="320x180", fps="24",
    )
    out = Path(res["output_path"])
    assert out.suffix == ".mov" and out.stem == "final" and out.is_file()
    assert res["master_codec"] == "prores_422" and res["master_path"] == str(out)
    assert Path(res["manifest_path"]).is_file()
    assert stream_info(out, "video")["r_frame_rate"] == "24/1"


def test_mcp_render_master_variants_all_go_to_subfolders(project, tmp_path):
    target = tmp_path / "mcp_all" / "final.mp4"
    res = render_master(
        output_file=str(target), project_path=str(project), no_nvenc=True, master="h264", variants="all",
    )
    assert set(res["variants"]) == {"master", "cinematic"}
    for key, info in res["variants"].items():
        assert Path(info["master_path"]).parent.name == key and Path(info["master_path"]).is_file()


def test_mcp_settings_without_project_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="need project_path"):
        export_timeline(input_dir=str(tmp_path), soundtrack=str(tmp_path / "x.wav"), master="h264")
    with pytest.raises(ValueError, match="need project_path"):
        render_master(input_dir=str(tmp_path), soundtrack=str(tmp_path / "x.wav"), canvas="1080x1920")


def test_mcp_invalid_settings_message(project, tmp_path):
    with pytest.raises(ValueError, match="Invalid deliver settings: canvas_width must be even"):
        export_timeline(output_dir=str(tmp_path / "o"), project_path=str(project), canvas="321x180")
    with pytest.raises(ValueError, match="Invalid deliver settings: master must be one of"):
        render_master(output_file=str(tmp_path / "o" / "m.mp4"), project_path=str(project), master="gif")

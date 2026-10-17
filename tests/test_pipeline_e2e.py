"""End-to-end integration test validating the complete pipeline execution."""

import cv2
import numpy as np
import pytest
import soundfile as sf
from click.testing import CliRunner

from main import cli


@pytest.fixture
def sample_pipeline_workspace(tmp_path):
    """Generate workspace with synthetic 1080p source videos and rhythm soundtrack."""
    footage_dir = tmp_path / "footage"
    footage_dir.mkdir()
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    output_dir = tmp_path / "output"

    # Generate 2 synthetic 1080p video clips (2 seconds each, 30 fps = 60 frames)
    w, h, fps = 1920, 1080, 30.0
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")

    for idx, color in enumerate([(255, 120, 50), (50, 180, 255)], start=1):
        v_path = footage_dir / f"clip_{idx}.mp4"
        out = cv2.VideoWriter(str(v_path), fourcc, fps, (w, h))
        for f_idx in range(60):
            # Dynamic gradient pattern with motion
            frame = np.full((h, w, 3), color, dtype=np.uint8)
            cv2.circle(frame, (f_idx * 20, h // 2), 60, (255, 255, 255), -1)
            out.write(frame)
        out.release()

    # Generate 3-second soundtrack with distinct beats (120 BPM)
    sr = 22050
    duration_sec = 3.0
    total_samples = int(sr * duration_sec)
    audio = np.zeros(total_samples, dtype=np.float32)
    beat_step = int(sr * 0.5)
    for b in range(0, total_samples, beat_step):
        click_len = min(sr // 20, total_samples - b)
        t = np.linspace(0, 0.05, click_len, False)
        pulse = np.sin(2 * np.pi * 880 * t) * np.exp(-t * 80)
        audio[b : b + click_len] += pulse.astype(np.float32)

    soundtrack_path = audio_dir / "soundtrack.wav"
    sf.write(str(soundtrack_path), audio, sr)

    return {
        "footage_dir": footage_dir,
        "soundtrack_path": soundtrack_path,
        "output_dir": output_dir,
    }


def test_cli_list_presets():
    """Verify list-presets command runs and includes 1080p30 as default."""
    runner = CliRunner()
    result = runner.invoke(cli, ["list-presets"])
    assert result.exit_code == 0
    assert "1080p30" in result.output
    assert "4k30" in result.output
    assert "(DEFAULT)" in result.output


def test_cli_check_env():
    """Verify check-env command runs diagnostics without error."""
    runner = CliRunner()
    result = runner.invoke(cli, ["check-env"])
    assert result.exit_code == 0
    assert "AutoEdit Environment & Hardware Diagnostics" in result.output


def test_cli_run_full_pipeline_e2e(sample_pipeline_workspace):
    """Execute the full pipeline from raw media to FCPXML, Premiere XML, and NVENC master."""
    runner = CliRunner()
    footage = sample_pipeline_workspace["footage_dir"]
    soundtrack = sample_pipeline_workspace["soundtrack_path"]
    out_dir = sample_pipeline_workspace["output_dir"]

    result = runner.invoke(
        cli,
        [
            "run",
            "--input-dir",
            str(footage),
            "--soundtrack",
            str(soundtrack),
            "--output-dir",
            str(out_dir),
            "--preset",
            "1080p30",
            "--dry-run-resolve",
        ],
    )

    assert result.exit_code == 0, f"Pipeline failed:\n{result.output}"

    # Verify generated deliverables
    assert (out_dir / "scenes_metadata.csv").is_file()
    assert (out_dir / "scenes_metadata.edl").is_file()
    assert (out_dir / "timeline.fcpxml").is_file()
    assert (out_dir / "timeline_premiere.xml").is_file()
    assert (out_dir / "timeline.otio").is_file()
    assert (out_dir / "timeline_capcut_draft" / "draft_content.json").is_file()
    assert (out_dir / "master_1080p30.mp4").is_file()
    assert (out_dir / "pipeline_summary.json").is_file()

    # Check that master MP4 has content and non-zero size
    master_file = out_dir / "master_1080p30.mp4"
    assert master_file.stat().st_size > 1024


def test_cli_run_pipeline_with_duration_mode(sample_pipeline_workspace):
    """Verify CLI accepts --duration-mode and --action-drop-threshold without regression."""
    runner = CliRunner()
    footage = sample_pipeline_workspace["footage_dir"]
    soundtrack = sample_pipeline_workspace["soundtrack_path"]
    out_dir = sample_pipeline_workspace["output_dir"] / "mode_run"

    result = runner.invoke(
        cli,
        [
            "run",
            "--input-dir",
            str(footage),
            "--soundtrack",
            str(soundtrack),
            "--output-dir",
            str(out_dir),
            "--preset",
            "1080p30",
            "--duration-mode",
            "footage_driven",
            "--action-drop-threshold",
            "0.75",
            "--dry-run-resolve",
        ],
    )

    assert result.exit_code == 0, f"Pipeline failed:\n{result.output}"
    assert "Duration Mode    : footage_driven" in result.output
    assert (out_dir / "master_1080p30.mp4").is_file()


def test_cli_run_pipeline_with_transcription(sample_pipeline_workspace):
    """Verify CLI accepts --enable-transcription and runs cleanly."""
    runner = CliRunner()
    footage = sample_pipeline_workspace["footage_dir"]
    soundtrack = sample_pipeline_workspace["soundtrack_path"]
    out_dir = sample_pipeline_workspace["output_dir"] / "transcribe_run"

    result = runner.invoke(
        cli,
        [
            "run",
            "--input-dir",
            str(footage),
            "--soundtrack",
            str(soundtrack),
            "--output-dir",
            str(out_dir),
            "--preset",
            "1080p30",
            "--enable-transcription",
            "--whisper-model",
            "tiny",
            "--dry-run-resolve",
        ],
    )

    assert result.exit_code == 0, f"Pipeline failed:\n{result.output}"
    assert "Speech Dialogue" in result.output
    assert (out_dir / "master_1080p30.mp4").is_file()

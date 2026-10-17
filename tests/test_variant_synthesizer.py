"""Unit tests for MultiVariantTimelineSynthesizer."""

from pathlib import Path

import pytest

from src.core.config import PipelineConfig
from src.core.types import BeatMarker, SceneCut, ScoredScene
from src.modules.variant_synthesizer import MultiVariantTimelineSynthesizer


@pytest.fixture
def mock_scenes(tmp_path: Path) -> list[ScoredScene]:
    """Generate synthetic scored scenes with distinct attributes."""
    scenes: list[ScoredScene] = []
    dummy_clip = tmp_path / "clip.mp4"
    dummy_clip.touch()

    for i in range(12):
        sc = SceneCut(
            clip_path=dummy_clip,
            start_time=float(i * 3),
            end_time=float((i + 1) * 3),
            start_frame=i * 90,
            end_frame=(i + 1) * 90,
            duration=3.0,
        )
        scored = ScoredScene(
            scene=sc,
            sharpness_score=0.4 + (i * 0.05),
            aesthetic_score=0.9 - (i * 0.04),
            composite_score=0.6 + (i * 0.03),
            motion_energy=0.2 + ((i % 4) * 0.25),
        )
        scenes.append(scored)
    return scenes


@pytest.fixture
def mock_beats() -> list[BeatMarker]:
    """Generate 30 seconds of beats."""
    beats: list[BeatMarker] = []
    for f in range(1, 60):
        t = f * 0.5
        beats.append(
            BeatMarker(
                timestamp=t,
                frame_idx=int(t * 30),
                strength=0.8 if f % 4 == 0 else 0.5,
                is_downbeat=(f % 4 == 0),
            )
        )
    return beats


def test_synthesize_single_variant(mock_scenes, mock_beats, tmp_path):
    """Verify requesting 1 variant returns only beat_driven."""
    config = PipelineConfig()
    synthesizer = MultiVariantTimelineSynthesizer(config)
    soundtrack = tmp_path / "music.mp3"
    soundtrack.touch()

    variants = synthesizer.synthesize_variants(
        scored_scenes=mock_scenes,
        beats=mock_beats,
        soundtrack_path=soundtrack,
        variant_count=1,
    )
    assert len(variants) == 1
    assert "beat_driven" in variants
    v1 = variants["beat_driven"]
    assert v1.name == "beat_driven"
    assert v1.clip_count > 0
    assert v1.total_duration > 0
    assert v1.otio_timeline is not None


def test_synthesize_three_variants_and_export(mock_scenes, mock_beats, tmp_path):
    """Verify requesting 3 variants generates beat_driven, cinematic, social_teaser with exports."""
    config = PipelineConfig()
    synthesizer = MultiVariantTimelineSynthesizer(config)
    soundtrack = tmp_path / "music.mp3"
    soundtrack.touch()
    out_dir = tmp_path / "export_variants"

    variants = synthesizer.synthesize_variants(
        scored_scenes=mock_scenes,
        beats=mock_beats,
        soundtrack_path=soundtrack,
        variant_count=3,
        output_dir=out_dir,
    )

    assert len(variants) == 3
    assert set(variants.keys()) == {"beat_driven", "cinematic", "social_teaser"}

    # Verify each variant has OTIO and exported files
    for name, v in variants.items():
        assert v.clip_count > 0
        assert v.total_duration > 0
        assert v.otio_timeline is not None
        assert v.fcpxml_path is not None
        assert v.fcpxml_path.is_file()
        assert v.premiere_xml_path is not None
        assert v.premiere_xml_path.is_file()
        assert v.otio_path is not None
        assert v.otio_path.is_file()
        assert v.capcut_draft_path is not None
        assert v.capcut_draft_path.is_dir()

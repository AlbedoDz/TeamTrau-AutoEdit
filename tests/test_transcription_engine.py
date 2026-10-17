"""Unit tests for faster-whisper speech transcription and audio ducking calculation."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.config import PipelineConfig
from src.core.types import SentenceSegment
from src.modules.transcription_engine import TranscriptionEngine


@pytest.fixture
def mock_whisper_model():
    """Create a mock faster-whisper model returning structured words and segments."""
    model = MagicMock()

    # Mock Word 1 & 2
    w1 = MagicMock()
    w1.word = "Hello"
    w1.start = 0.5
    w1.end = 0.9
    w1.probability = 0.95

    w2 = MagicMock()
    w2.word = "world"
    w2.start = 1.0
    w2.end = 1.4
    w2.probability = 0.92

    # Mock Segment 1
    seg1 = MagicMock()
    seg1.text = "Hello world"
    seg1.start = 0.5
    seg1.end = 1.4
    seg1.words = [w1, w2]

    # Mock Segment 2
    w3 = MagicMock()
    w3.word = "AutoEdit"
    w3.start = 3.0
    w3.end = 3.8
    w3.probability = 0.88

    seg2 = MagicMock()
    seg2.text = "AutoEdit"
    seg2.start = 3.0
    seg2.end = 3.8
    seg2.words = [w3]

    info = MagicMock()
    info.language = "en"
    model.transcribe.return_value = ([seg1, seg2], info)
    return model


def test_transcribe_file_word_timestamps(tmp_path: Path, mock_whisper_model):
    """Verify word-level timestamps, sentence grouping, and probability aggregation."""
    test_media = tmp_path / "dialogue.wav"
    test_media.touch()

    cfg = PipelineConfig(enable_transcription=True)
    engine = TranscriptionEngine(cfg, model=mock_whisper_model)

    sentences = engine.transcribe_file(test_media)

    assert len(sentences) == 2
    s1 = sentences[0]
    assert s1.text == "Hello world"
    assert s1.start_time == 0.5
    assert s1.end_time == 1.4
    assert len(s1.words) == 2
    assert s1.words[0].word == "Hello"
    assert s1.words[0].start_time == 0.5
    assert s1.words[1].word == "world"
    assert s1.confidence == pytest.approx(0.935, abs=0.01)

    s2 = sentences[1]
    assert s2.text == "AutoEdit"
    assert s2.start_time == 3.0
    assert s2.end_time == 3.8


def test_transcribe_file_missing_file_raises_error():
    """Verify FileNotFoundError is raised when file does not exist."""
    cfg = PipelineConfig()
    engine = TranscriptionEngine(cfg, model=MagicMock())

    with pytest.raises(FileNotFoundError):
        engine.transcribe_file(Path("non_existent_audio.wav"))


def test_transcribe_file_fallback_when_model_inactive(tmp_path: Path):
    """Verify empty list is returned cleanly when model is inactive."""
    test_media = tmp_path / "speech.wav"
    test_media.touch()

    cfg = PipelineConfig(enable_transcription=False)
    engine = TranscriptionEngine(cfg, model=None)

    sentences = engine.transcribe_file(test_media)
    assert sentences == []


def test_get_ducking_intervals_merges_overlapping():
    """Verify audio ducking intervals merge when sentences are close together."""
    cfg = PipelineConfig()
    engine = TranscriptionEngine(cfg, model=MagicMock())

    # Two sentences spaced closely: [1.0, 2.0] and [2.3, 3.5]
    sentences = [
        SentenceSegment(
            text="First part",
            start_time=1.0,
            end_time=2.0,
            duration=1.0,
            words=[],
        ),
        SentenceSegment(
            text="Second part",
            start_time=2.3,
            end_time=3.5,
            duration=1.2,
            words=[],
        ),
        SentenceSegment(
            text="Isolated third part",
            start_time=8.0,
            end_time=9.0,
            duration=1.0,
            words=[],
        ),
    ]

    # pre_roll=0.2, post_roll=0.3
    # S1: [0.8, 2.3]
    # S2: [2.1, 3.8] -> overlaps with S1 (2.1 <= 2.3) -> merged: [0.8, 3.8]
    # S3: [7.8, 9.3] -> separate
    intervals = engine.get_ducking_intervals(sentences, pre_roll=0.2, post_roll=0.3)

    assert len(intervals) == 2
    assert intervals[0][0] == pytest.approx(0.8, abs=0.01)
    assert intervals[0][1] == pytest.approx(3.8, abs=0.01)
    assert intervals[1][0] == pytest.approx(7.8, abs=0.01)
    assert intervals[1][1] == pytest.approx(9.3, abs=0.01)

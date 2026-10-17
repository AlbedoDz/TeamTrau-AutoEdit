"""Unit tests for Kinetic ASS Subtitle Engine."""

from pathlib import Path

from src.core.types import SentenceSegment, WordSegment
from src.modules.kinetic_subtitle import (
    export_kinetic_ass,
    format_ass_time,
    generate_ass_header,
)


def test_format_ass_time():
    """Verify conversion of floating seconds into H:MM:SS.cc centisecond format."""
    assert format_ass_time(0.0) == "0:00:00.00"
    assert format_ass_time(65.45) == "0:01:05.45"
    assert format_ass_time(3661.80) == "1:01:01.80"
    assert format_ass_time(-5.0) == "0:00:00.00"


def test_generate_ass_header_safe_zone_margins():
    """Verify ASS header respects platform safe zones (320px vertical for reels/shorts)."""
    # Vertical (9:16)
    hdr_v = generate_ass_header(is_vertical=True)
    assert "PlayResX: 1080" in hdr_v
    assert "PlayResY: 1920" in hdr_v
    assert ",320,1" in hdr_v  # MarginV = 320
    assert "Montserrat" in hdr_v
    assert ",72," in hdr_v  # 72pt font size

    # Horizontal (16:9)
    hdr_h = generate_ass_header(is_vertical=False)
    assert "PlayResX: 1920" in hdr_h
    assert "PlayResY: 1080" in hdr_h
    assert ",60,1" in hdr_h  # MarginV = 60

    # Custom margin override
    hdr_custom = generate_ass_header(is_vertical=True, margin_v=280)
    assert ",280,1" in hdr_custom


def test_export_kinetic_ass_word_highlighting(tmp_path: Path):
    """Verify word-level karaoke active highlighting tags are injected into dialogue events."""
    words = [
        WordSegment(word="Automate", start_time=1.0, end_time=1.5, probability=0.98),
        WordSegment(word="Video", start_time=1.5, end_time=2.0, probability=0.99),
        WordSegment(word="Editing", start_time=2.0, end_time=2.8, probability=0.97),
    ]
    sentence = SentenceSegment(
        text="Automate Video Editing",
        start_time=1.0,
        end_time=2.8,
        duration=1.8,
        words=words,
    )

    out_file = tmp_path / "kinetic.ass"
    res_path = export_kinetic_ass([sentence], out_file, is_vertical=True)

    assert res_path.is_file()
    content = res_path.read_text(encoding="utf-8")

    # Check 3 distinct dialogue lines for the 3 words
    dialogue_lines = [line for line in content.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue_lines) == 3

    # First event highlights "Automate"
    assert "0:00:01.00,0:00:01.50" in dialogue_lines[0]
    assert r"{\c&H0000FFFF&}Automate{\c&H00FFFFFF&} Video Editing" in dialogue_lines[0]

    # Second event highlights "Video"
    assert "0:00:01.50,0:00:02.00" in dialogue_lines[1]
    assert r"Automate {\c&H0000FFFF&}Video{\c&H00FFFFFF&} Editing" in dialogue_lines[1]

    # Third event highlights "Editing"
    assert "0:00:02.00,0:00:02.80" in dialogue_lines[2]
    assert r"Automate Video {\c&H0000FFFF&}Editing{\c&H00FFFFFF&}" in dialogue_lines[2]


def test_export_kinetic_ass_fallback_sentence(tmp_path: Path):
    """Verify fallback to standard single dialogue event when sentence has no word boundaries."""
    sentence = SentenceSegment(
        text="No word timing here",
        start_time=3.0,
        end_time=5.5,
        duration=2.5,
        words=[],
    )

    out_file = tmp_path / "fallback.ass"
    export_kinetic_ass([sentence], out_file, is_vertical=True)

    content = out_file.read_text(encoding="utf-8")
    dialogue_lines = [line for line in content.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue_lines) == 1
    assert "0:00:03.00,0:00:05.50" in dialogue_lines[0]
    assert "No word timing here" in dialogue_lines[0]

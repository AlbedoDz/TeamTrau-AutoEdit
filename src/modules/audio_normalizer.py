"""Audio normalization and BWF metadata cleaning for DaVinci Resolve & NLE workflows."""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AudioNormalizationError(Exception):
    """Raised when audio inspection or normalization fails."""


class AudioNormalizer:
    """Non-destructive audio inspection and normalization service."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        """Initialize AudioNormalizer with a dedicated cache directory."""
        self.cache_dir = (cache_dir or Path(".cache/autoedit/audio_clean")).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
        self.ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"

    def inspect_audio(self, audio_path: Path) -> dict[str, Any]:
        """Probe audio file for codec, channels, sample rate, bit depth, and BWF time_reference.

        Returns:
            Dict containing metadata: codec, channels, sample_rate, bit_depth,
            time_reference, and requires_cleaning.
        """
        audio_path = Path(audio_path).resolve()
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        cmd = [
            self.ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,channels,sample_rate,bits_per_raw_sample:format_tags=time_reference",
            "-of",
            "json",
            str(audio_path),
        ]

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
            data = json.loads(res.stdout or "{}")
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
            logger.warning("ffprobe inspection failed on %s: %s", audio_path, exc)
            return {
                "codec": "unknown",
                "channels": 2,
                "sample_rate": 44100,
                "bit_depth": None,
                "time_reference": None,
                "requires_cleaning": False,
            }

        streams = data.get("streams") or []
        format_info = data.get("format") or {}
        format_tags = format_info.get("tags") or {}

        codec = "unknown"
        channels = 2
        sample_rate = 44100
        bit_depth = None

        if streams:
            stream = streams[0]
            codec = stream.get("codec_name", "unknown")
            channels = int(stream.get("channels", 2))
            sample_rate = int(stream.get("sample_rate", 44100))
            raw_bits = stream.get("bits_per_raw_sample")
            if raw_bits and str(raw_bits).isdigit():
                bit_depth = int(raw_bits)

        # Fallback bit depth deduction from codec name
        if bit_depth is None:
            if "16" in codec:
                bit_depth = 16
            elif "24" in codec:
                bit_depth = 24
            elif "32" in codec:
                bit_depth = 32

        time_ref = format_tags.get("time_reference")
        has_bwf_offset = bool(time_ref and str(time_ref).strip() not in ("0", ""))

        is_wav = audio_path.suffix.lower() == ".wav"
        requires_cleaning = False
        if is_wav:
            # WAV files with BWF time_reference offset cause DaVinci Resolve silence bugs
            # or non-24-bit WAVs that cause track inconsistency
            if has_bwf_offset or (bit_depth is not None and bit_depth != 24):
                requires_cleaning = True

        return {
            "path": audio_path,
            "codec": codec,
            "channels": channels,
            "sample_rate": sample_rate,
            "bit_depth": bit_depth,
            "time_reference": time_ref,
            "has_bwf_offset": has_bwf_offset,
            "requires_cleaning": requires_cleaning,
        }

    def clean_audio(
        self,
        audio_path: Path,
        force_clean: bool = False,
        target_sample_rate: int = 48000,
    ) -> Path:
        """Strip BWF time_reference metadata and normalize WAV to 24-bit PCM.

        Saves clean file non-destructively to cache_dir.
        If file does not require cleaning and force_clean is False, returns audio_path unchanged.

        Args:
            audio_path: Path to input audio track.
            force_clean: If True, forces re-encoding even if deemed clean.
            target_sample_rate: Output sample rate (standard NLE 48000 Hz).

        Returns:
            Path to clean audio file (cached or original).
        """
        audio_path = Path(audio_path).resolve()
        info = self.inspect_audio(audio_path)

        if not info["requires_cleaning"] and not force_clean:
            logger.debug("Audio file %s does not require cleaning.", audio_path.name)
            return audio_path

        clean_filename = f"{audio_path.stem}_clean_24bit.wav"
        clean_path = self.cache_dir / clean_filename

        # Reuse cache if exists and newer than source
        if clean_path.exists() and clean_path.stat().st_mtime >= audio_path.stat().st_mtime:
            logger.info("Using cached clean audio: %s", clean_path)
            return clean_path

        logger.info(
            "Normalizing audio %s (BWF time_reference=%s, bit_depth=%s) -> %s",
            audio_path.name,
            info.get("time_reference"),
            info.get("bit_depth"),
            clean_path.name,
        )

        cmd = [
            self.ffmpeg_bin,
            "-y",
            "-i",
            str(audio_path),
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-acodec",
            "pcm_s24le",
            "-ar",
            str(target_sample_rate),
            str(clean_path),
        ]

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
            logger.info("Successfully cleaned audio: %s", clean_path)
            return clean_path
        except (subprocess.SubprocessError, OSError) as exc:
            if clean_path.exists():
                clean_path.unlink(missing_ok=True)
            logger.error("Failed to clean audio %s: %s", audio_path, exc)
            raise AudioNormalizationError(f"Failed to normalize audio {audio_path}: {exc}") from exc

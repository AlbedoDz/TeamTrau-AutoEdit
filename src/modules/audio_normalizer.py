"""Audio normalization and BWF metadata cleaning for DaVinci Resolve & NLE workflows."""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from src.core.ffmpeg_utils import run_tool

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
            res = run_tool(cmd, check=True, timeout=60)
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
            run_tool(cmd, check=True, timeout=1800)
            logger.info("Successfully cleaned audio: %s", clean_path)
            return clean_path
        except (subprocess.SubprocessError, OSError) as exc:
            if clean_path.exists():
                clean_path.unlink(missing_ok=True)
            logger.error("Failed to clean audio %s: %s", audio_path, exc)
            raise AudioNormalizationError(f"Failed to normalize audio {audio_path}: {exc}") from exc

    def inspect_and_condition_camera_audio(
        self,
        media_path: Path,
        force_highpass: bool = False,
        rumble_threshold_db: float = -18.0,
        target_sample_rate: int = 48000,
    ) -> tuple[Path, bool]:
        """Inspect camera audio for low-frequency rumble and apply 80Hz high-pass conditioning.

        Args:
            media_path: Video or audio file path.
            force_highpass: Force 80Hz highpass filter without testing rumble.
            rumble_threshold_db: Threshold in dBFS for low frequency (<80Hz) energy.
            target_sample_rate: Standard NLE sample rate (48000 Hz).

        Returns:
            Tuple of (output_wav_path, filter_applied).
        """
        media_path = Path(media_path).resolve()
        if not media_path.exists():
            raise FileNotFoundError(f"Media file not found: {media_path}")

        has_rumble = force_highpass
        if not has_rumble:
            # Quick probe for low-frequency energy using ffmpeg astats
            try:
                cmd_probe = [
                    self.ffmpeg_bin,
                    "-v",
                    "info",
                    "-i",
                    str(media_path),
                    "-t",
                    "5.0",
                    "-af",
                    "lowpass=f=80,astats=metadata=1:reset=1",
                    "-f",
                    "null",
                    "-",
                ]
                res = run_tool(cmd_probe, timeout=60)
                for line in (res.stderr + res.stdout).splitlines():
                    if "RMS level dB" in line or "Peak level dB" in line:
                        try:
                            val = float(line.split(":")[-1].strip())
                            if val >= rumble_threshold_db and val < 0:
                                has_rumble = True
                                break
                        except ValueError:
                            pass
            except Exception as probe_err:
                logger.debug("Rumble probe failed on %s: %s", media_path.name, probe_err)

        suffix = "_conditioned_80hz.wav" if has_rumble else "_clean_24bit.wav"
        out_path = self.cache_dir / f"{media_path.stem}{suffix}"

        if out_path.exists() and out_path.stat().st_mtime >= media_path.stat().st_mtime:
            return out_path, has_rumble

        cmd = [
            self.ffmpeg_bin,
            "-y",
            "-i",
            str(media_path),
            "-vn",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
        ]
        if has_rumble:
            cmd.extend(["-af", "highpass=f=80,alimiter=limit=0.95"])

        cmd.extend([
            "-acodec",
            "pcm_s24le",
            "-ar",
            str(target_sample_rate),
            str(out_path),
        ])

        try:
            run_tool(cmd, check=True, timeout=1800)
            return out_path, has_rumble
        except (subprocess.SubprocessError, OSError) as exc:
            if out_path.exists():
                out_path.unlink(missing_ok=True)
            logger.error("Failed to condition audio for %s: %s", media_path.name, exc)
            raise AudioNormalizationError(f"Failed to condition audio {media_path}: {exc}") from exc

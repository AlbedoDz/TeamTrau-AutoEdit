"""Speech-to-Text and word-level dialogue boundary engine using faster-whisper."""

import dataclasses
import logging
from pathlib import Path
from typing import Any

from src.core.analysis_cache import AnalysisCache
from src.core.config import PipelineConfig, check_cuda_available
from src.core.types import SentenceSegment, WordSegment

logger = logging.getLogger(__name__)


class TranscriptionEngine:
    """Extracts exact word-level timecodes and groups spoken sentences via faster-whisper."""

    def __init__(self, config: PipelineConfig, model: Any | None = None) -> None:
        self.config = config
        self._model = model
        if self._model is None and self.config.enable_transcription:
            self._init_model()

    def _init_model(self) -> None:
        """Initialize faster-whisper model with automatic CUDA to CPU fallback."""
        try:
            from faster_whisper import WhisperModel

            model_size = self.config.whisper_model_size
            device = "cuda" if (self.config.whisper_device == "cuda" and check_cuda_available()) else "cpu"
            compute_type = self.config.whisper_compute_type if device == "cuda" else "int8"

            logger.info("Initializing faster-whisper model '%s' on %s (%s)...", model_size, device, compute_type)
            self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        except Exception as e:
            logger.warning("Could not initialize faster-whisper (%s). Running in mock/fallback mode.", e)
            self._model = None

    def unload_model(self) -> None:
        """Unload faster-whisper model from VRAM and run garbage collection."""
        if self._model is not None:
            del self._model
            self._model = None
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                logger.info("Evicted TranscriptionEngine whisper model from CUDA VRAM.")
        except ImportError:
            pass

    def transcribe_file(self, media_path: Path) -> list[SentenceSegment]:
        """Transcribe audio/video file and extract complete sentences with word-level boundaries."""
        if not media_path.is_file():
            raise FileNotFoundError(f"Media file not found for transcription: {media_path}")

        if self._model is None:
            logger.info("Transcription skipped: faster-whisper model inactive.")
            return []

        cache = AnalysisCache(self.config)
        cache_key = cache.make_key(
            media_path,
            model=self.config.whisper_model_size,
            vad=400,
            words=True,
        )
        cached = cache.load("whisper", media_path, cache_key)
        if cached is not None:
            try:
                restored = [
                    SentenceSegment(**{**d, "words": [WordSegment(**w) for w in d.get("words", [])]})
                    for d in cached
                ]
                logger.info("Transcript cache hit for %s (%d sentences).", media_path.name, len(restored))
                return restored
            except (TypeError, KeyError):
                logger.debug("Ignoring incompatible transcript cache for %s", media_path.name)

        try:
            logger.info("Transcribing speech from %s with word-level timestamps...", media_path.name)
            segments, _ = self._model.transcribe(
                str(media_path),
                word_timestamps=True,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=400),
            )

            sentences: list[SentenceSegment] = []
            for seg in segments:
                words_list: list[WordSegment] = []
                if hasattr(seg, "words") and seg.words:
                    for w in seg.words:
                        w_text = getattr(w, "word", str(w)).strip()
                        w_start = float(getattr(w, "start", seg.start))
                        w_end = float(getattr(w, "end", seg.end))
                        w_prob = float(getattr(w, "probability", 1.0))
                        words_list.append(
                            WordSegment(
                                word=w_text,
                                start_time=w_start,
                                end_time=w_end,
                                probability=w_prob,
                            )
                        )

                seg_text = seg.text.strip()
                if not seg_text:
                    continue

                duration = max(0.1, float(seg.end - seg.start))
                avg_prob = (
                    float(sum(w.probability for w in words_list) / len(words_list))
                    if words_list
                    else 0.9
                )

                # Heuristic importance score: combination of speech confidence and length
                importance = min(1.0, avg_prob * (0.8 + 0.2 * min(1.0, duration / 3.0)))

                sentences.append(
                    SentenceSegment(
                        text=seg_text,
                        start_time=float(seg.start),
                        end_time=float(seg.end),
                        duration=duration,
                        words=words_list,
                        confidence=avg_prob,
                        importance_score=importance,
                    )
                )

            logger.info("Transcribed %d sentence segments from %s.", len(sentences), media_path.name)
            cache.save("whisper", media_path, cache_key, [dataclasses.asdict(x) for x in sentences])
            return sentences
        except Exception as e:
            logger.warning("Transcription failed on %s: %s", media_path.name, e)
            return []

    def get_ducking_intervals(
        self,
        sentences: list[SentenceSegment],
        pre_roll: float = 0.2,
        post_roll: float = 0.3,
    ) -> list[tuple[float, float]]:
        """Calculate continuous time ranges where background audio should be ducked."""
        if not sentences:
            return []

        raw_ranges: list[tuple[float, float]] = []
        for s in sentences:
            start_t = max(0.0, s.start_time - pre_roll)
            end_t = s.end_time + post_roll
            raw_ranges.append((start_t, end_t))

        # Merge overlapping ducking ranges
        raw_ranges.sort(key=lambda r: r[0])
        merged: list[tuple[float, float]] = [raw_ranges[0]]

        for curr_start, curr_end in raw_ranges[1:]:
            last_start, last_end = merged[-1]
            if curr_start <= last_end:
                merged[-1] = (last_start, max(last_end, curr_end))
            else:
                merged.append((curr_start, curr_end))

        return merged

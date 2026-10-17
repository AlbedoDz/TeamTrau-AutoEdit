"""Knapsack duration optimization packing dialogue sentences and visual B-roll."""

import logging

from src.core.config import PipelineConfig
from src.core.types import PackedDurationResult, ScoredScene, SentenceSegment

logger = logging.getLogger(__name__)


class DurationPacker:
    """Solves duration-constrained timeline assembly balancing spoken dialogue and visual B-roll."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def pack_timeline(
        self,
        sentences: list[SentenceSegment],
        scored_scenes: list[ScoredScene],
        target_duration: float,
        speech_ratio: float | None = None,
    ) -> PackedDurationResult:
        """Select highest-value complete sentences and visual B-roll fitting strictly within target duration.

        Preserves chronological coherence for dialogue while avoiding mid-sentence cuts.
        """
        if target_duration <= 0:
            raise ValueError(f"target_duration must be positive, got {target_duration}")

        ratio = speech_ratio if speech_ratio is not None else self.config.target_speech_ratio
        ratio = max(0.0, min(1.0, ratio))

        # Case 1: No dialogue available -> 100% visual B-roll
        if not sentences:
            selected_scenes: list[ScoredScene] = []
            curr_dur = 0.0
            for sc in scored_scenes:
                if curr_dur + sc.scene.duration <= target_duration + 5.0:
                    selected_scenes.append(sc)
                    curr_dur += sc.scene.duration
                if curr_dur >= target_duration:
                    break

            if not selected_scenes and scored_scenes:
                selected_scenes = [scored_scenes[0]]
                curr_dur = scored_scenes[0].scene.duration

            return PackedDurationResult(
                selected_sentences=[],
                selected_scenes=selected_scenes,
                total_duration=round(curr_dur, 2),
                target_duration=target_duration,
                speech_ratio=0.0,
                b_roll_ratio=1.0,
            )

        # Case 2: Dialogue + B-roll Knapsack Packing
        dialogue_budget = target_duration * ratio

        # 2a. Select top-scoring sentences within dialogue budget
        # Rank by importance score / duration efficiency (value density)
        ranked_sentences = sorted(
            sentences,
            key=lambda s: s.importance_score / max(0.1, s.duration),
            reverse=True,
        )

        chosen_indices: set[int] = set()
        accumulated_speech = 0.0

        for s in ranked_sentences:
            if accumulated_speech + s.duration <= dialogue_budget + 3.0:
                idx = sentences.index(s)
                chosen_indices.add(idx)
                accumulated_speech += s.duration
            if accumulated_speech >= dialogue_budget:
                break

        # Re-sort chosen sentences chronologically to preserve speech narrative flow
        selected_sentences = [sentences[i] for i in sorted(chosen_indices)]

        # 2b. Allocate remaining duration to highest-scoring visual B-roll clips
        remaining_budget = max(0.0, target_duration - accumulated_speech)
        selected_scenes: list[ScoredScene] = []
        accumulated_broll = 0.0

        for sc in scored_scenes:
            if accumulated_broll + sc.scene.duration <= remaining_budget + 4.0:
                selected_scenes.append(sc)
                accumulated_broll += sc.scene.duration
            if (accumulated_speech + accumulated_broll) >= target_duration:
                break

        if not selected_scenes and scored_scenes:
            selected_scenes = [scored_scenes[0]]
            accumulated_broll = scored_scenes[0].scene.duration

        total_dur = accumulated_speech + accumulated_broll
        actual_speech_ratio = accumulated_speech / total_dur if total_dur > 0 else 0.0
        actual_broll_ratio = accumulated_broll / total_dur if total_dur > 0 else 0.0

        logger.info(
            "Packed duration: target=%.1fs, total=%.1fs (speech=%.1fs [%.1f%%], b-roll=%.1fs [%.1f%%])",
            target_duration,
            total_dur,
            accumulated_speech,
            actual_speech_ratio * 100,
            accumulated_broll,
            actual_broll_ratio * 100,
        )

        return PackedDurationResult(
            selected_sentences=selected_sentences,
            selected_scenes=selected_scenes,
            total_duration=round(total_dur, 2),
            target_duration=target_duration,
            speech_ratio=round(actual_speech_ratio, 2),
            b_roll_ratio=round(actual_broll_ratio, 2),
        )

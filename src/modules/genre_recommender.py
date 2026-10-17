"""Smart Pre-Recommendation Engine for automated video sub-genre selection.

Combines DSP audio rhythm analysis (BPM, structural sections, energy variance) with
local LLM inference (e.g. Qwen / Ollama) or a zero-dependency deterministic classifier.
"""

import json
import logging
from dataclasses import dataclass

from src.core.config import PipelineConfig
from src.core.genre_presets import (
    GENRE_PRESETS,
    SubGenrePreset,
    get_preset_by_key,
)
from src.core.ontology import CameraRole

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenreRecommendation:
    """Actionable recommendation emitted before video synthesis."""

    subgenre_key: str
    major_genre: str
    display_name: str
    confidence: float
    target_pacing: str
    reasoning: str
    bpm: float
    preset: SubGenrePreset


class GenreRecommender:
    """Analyzes audio rhythm and media assets to recommend optimal genre preset."""

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()

    def recommend(
        self,
        bpm: float,
        duration: float = 60.0,
        section_count: int = 1,
        camera_roles: list[str | CameraRole] | None = None,
        is_vertical: bool = False,
        use_llm: bool = False,
    ) -> GenreRecommendation:
        """Recommend optimal sub-genre preset using LLM if enabled, falling back to DSP rules."""
        roles = [r.value if isinstance(r, CameraRole) else str(r) for r in (camera_roles or [])]

        if use_llm and getattr(self.config, "enable_vlm_tagging", False):
            try:
                rec = self._recommend_via_llm(bpm, duration, section_count, roles, is_vertical)
                if rec:
                    return rec
            except Exception as exc:
                logger.warning("LLM recommendation failed (%s). Falling back to DSP classifier.", exc)

        return self._recommend_via_dsp(bpm, duration, section_count, roles, is_vertical)

    def _recommend_via_dsp(
        self,
        bpm: float,
        duration: float,
        section_count: int,
        camera_roles: list[str],
        is_vertical: bool,
    ) -> GenreRecommendation:
        """Deterministic rule-based audio DSP classifier."""
        has_drone = "drone" in camera_roles
        has_action = "action_cam" in camera_roles
        has_broll = "b_roll" in camera_roles
        has_cam_b = "cam_b" in camera_roles

        # 1. High-Energy / Electronic / Fast Paced (BPM >= 126)
        if bpm >= 126.0:
            if is_vertical or duration <= 45.0:
                key = "social_tiktok_fast"
                reason = f"High tempo ({bpm:.1f} BPM) and short/vertical format match viral fast pacing."
                conf = 0.94
            elif has_action:
                key = "sports_extreme"
                reason = f"Aggressive {bpm:.1f} BPM tempo paired with ActionCam POV assets."
                conf = 0.92
            elif has_drone:
                key = "sports_fpv_drone"
                reason = f"Fast {bpm:.1f} BPM rhythm with aerial drone footage for fluid kinetic flow."
                conf = 0.88
            else:
                key = "sports_racing"
                reason = f"High energetic {bpm:.1f} BPM tempo with high motion dynamics."
                conf = 0.85

        # 2. Upbeat / Driving Paced (105 <= BPM < 126)
        elif bpm >= 105.0:
            if is_vertical:
                key = "social_reels_aesthetic"
                reason = f"Upbeat {bpm:.1f} BPM rhythm fits Instagram aesthetic beat-drop timing."
                conf = 0.90
            elif has_action and has_drone:
                key = "sports_outdoor"
                reason = f"Moderate upbeat {bpm:.1f} BPM with mixed outdoor and drone media."
                conf = 0.86
            elif has_drone or has_broll:
                key = "travel_roadtrip"
                reason = f"Driving {bpm:.1f} BPM tempo ideal for travel roadtrip montages."
                conf = 0.88
            else:
                key = "commercial_brand_promo"
                reason = f"Dynamic {bpm:.1f} BPM build matches commercial brand storytelling."
                conf = 0.82

        # 3. Moderate / Groovy Paced (78 <= BPM < 105)
        elif bpm >= 78.0:
            if has_drone and not has_action:
                key = "travel_cinematic"
                reason = f"Smooth {bpm:.1f} BPM cadence with aerial drone footage suits cinematic film."
                conf = 0.91
            elif has_broll and not has_action:
                key = "travel_food"
                reason = f"Sensual {bpm:.1f} BPM tempo matches culinary and macro detail inserts."
                conf = 0.84
            elif has_cam_b:
                key = "doc_bts"
                reason = f"Natural {bpm:.1f} BPM tempo with multi-angle footage suits authentic BTS."
                conf = 0.83
            else:
                key = "travel_daily_vlog"
                reason = f"Balanced {bpm:.1f} BPM tempo fits narration-driven daily vlog structure."
                conf = 0.80

        # 4. Slow / Contemplative / Ambient (BPM < 78)
        else:
            if has_drone:
                key = "travel_nature"
                reason = f"Gentle {bpm:.1f} BPM tempo with sweeping landscape drone vistas."
                conf = 0.92
            elif has_broll:
                key = "travel_luxury"
                reason = f"Slow, elegant {bpm:.1f} BPM pacing with high aesthetic B-Roll."
                conf = 0.88
            else:
                key = "wedding_highlight"
                reason = f"Emotional {bpm:.1f} BPM tempo with romantic crescendo potential."
                conf = 0.85

        preset = get_preset_by_key(key)
        return GenreRecommendation(
            subgenre_key=preset.key,
            major_genre=preset.major_genre.value,
            display_name=preset.display_name,
            confidence=conf,
            target_pacing="fast" if bpm >= 120 else ("moderate" if bpm >= 85 else "cinematic"),
            reasoning=reason,
            bpm=bpm,
            preset=preset,
        )

    def _recommend_via_llm(
        self,
        bpm: float,
        duration: float,
        section_count: int,
        camera_roles: list[str],
        is_vertical: bool,
    ) -> GenreRecommendation | None:
        """Query local or cloud LLM for structured sub-genre classification."""
        from src.modules.vlm_assistant import VLMAssistant

        assistant = VLMAssistant(self.config)
        prompt = (
            f"Analyze this video project and select the best sub-genre preset:\n"
            f"- Audio BPM: {bpm:.1f}\n"
            f"- Music Duration: {duration:.1f} seconds\n"
            f"- Song Sections: {section_count}\n"
            f"- Detected Camera Roles: {camera_roles}\n"
            f"- Vertical 9:16 Canvas: {is_vertical}\n\n"
            f"Available sub-genres: {list(GENRE_PRESETS.keys())}\n"
            f"Return JSON strictly with fields: 'recommended_genre', 'confidence', 'reasoning'."
        )

        response = assistant.query_text(prompt)
        if not response:
            return None

        # Parse JSON from response
        try:
            start_idx = response.find("{")
            end_idx = response.rfind("}") + 1
            if start_idx != -1 and end_idx > start_idx:
                data = json.loads(response[start_idx:end_idx])
                key = data.get("recommended_genre", "").strip().lower()
                if key in GENRE_PRESETS:
                    preset = GENRE_PRESETS[key]
                    conf = float(data.get("confidence", 0.90))
                    reason = str(data.get("reasoning", "Recommended by AI model."))
                    return GenreRecommendation(
                        subgenre_key=preset.key,
                        major_genre=preset.major_genre.value,
                        display_name=preset.display_name,
                        confidence=min(1.0, max(0.0, conf)),
                        target_pacing="fast" if bpm >= 120 else "cinematic",
                        reasoning=reason,
                        bpm=bpm,
                        preset=preset,
                    )
        except Exception as exc:
            logger.debug("Failed parsing LLM recommendation JSON: %s", exc)

        return None

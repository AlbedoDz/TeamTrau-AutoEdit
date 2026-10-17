"""OpenAI-compatible VLM assistant module for semantic video scene understanding.

Supports OpenRouter, Ollama (Local/LAN), LM Studio (Local/LAN), and OpenAI endpoints,
with an automated heuristic fallback (zero direct heavy weights).
"""

import base64
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from src.core.config import PipelineConfig
from src.core.types import SceneVLMAnalysis, ScoredScene

logger = logging.getLogger(__name__)


class VLMAssistant:
    """Semantic scene analysis and tagging assistant using OpenAI-compatible REST endpoints.

    Integrates with OpenRouter, Ollama, LM Studio, or OpenAI vision models to extract
    high-level semantic tags, mood, pacing, and key moment indicators. Automatically
    falls back to rule-based heuristics when offline or without API keys.
    """

    KNOWN_PROVIDERS: dict[str, str] = {
        "openrouter": "https://openrouter.ai/api/v1",
        "ollama": "http://localhost:11434/v1",
        "lm_studio": "http://localhost:1234/v1",
        "openai": "https://api.openai.com/v1",
    }

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.provider = config.vlm_provider.lower()
        self.base_url = config.vlm_base_url
        self.api_key = config.vlm_api_key
        self.model = config.vlm_model
        self.timeout = config.vlm_timeout_sec
        self.client: Any = None

        self._resolve_provider_and_client()

    def _resolve_provider_and_client(self) -> None:
        """Resolve endpoint URL, API key, and instantiate OpenAI REST client."""
        # 1. Custom URL override
        if self.base_url:
            self.provider = "custom"
            api_key = self.api_key or os.environ.get("OPENAI_API_KEY", "dummy-key")
        elif self.provider == "auto":
            # Auto-discovery based on available environment credentials
            if os.environ.get("OPENROUTER_API_KEY"):
                self.provider = "openrouter"
                self.base_url = self.KNOWN_PROVIDERS["openrouter"]
                api_key = os.environ["OPENROUTER_API_KEY"]
            elif os.environ.get("OPENAI_API_KEY"):
                self.provider = "openai"
                self.base_url = self.KNOWN_PROVIDERS["openai"]
                api_key = os.environ["OPENAI_API_KEY"]
            elif os.environ.get("OLLAMA_HOST"):
                self.provider = "ollama"
                self.base_url = f"{os.environ['OLLAMA_HOST']}/v1"
                api_key = "ollama"
            else:
                self.provider = "heuristic"
                api_key = None
        elif self.provider in self.KNOWN_PROVIDERS:
            self.base_url = self.KNOWN_PROVIDERS[self.provider]
            if self.provider == "openrouter":
                api_key = self.api_key or os.environ.get("OPENROUTER_API_KEY")
            elif self.provider == "openai":
                api_key = self.api_key or os.environ.get("OPENAI_API_KEY")
            elif self.provider in ("ollama", "lm_studio"):
                api_key = self.api_key or "local"
            else:
                api_key = self.api_key or "dummy-key"
        else:
            self.provider = "heuristic"
            api_key = None

        # 2. Instantiate OpenAI REST client if credentials and provider resolved
        if self.provider != "heuristic" and api_key and self.base_url:
            try:
                from openai import OpenAI

                self.client = OpenAI(
                    base_url=self.base_url,
                    api_key=api_key,
                    timeout=self.timeout,
                )
                logger.info(
                    "Initialized VLMAssistant via %s (%s) with model %s.",
                    self.provider,
                    self.base_url,
                    self.model,
                )
            except Exception as exc:
                logger.warning(
                    "OpenAI client initialization failed (%s). Falling back to heuristic VLM.",
                    exc,
                )
                self.client = None
        else:
            logger.info("VLMAssistant operating in local heuristic rule-based mode.")
            self.client = None

    def encode_image_b64(self, frame: np.ndarray | Path | bytes) -> str:
        """Encode an image or keyframe array into base64 JPEG format."""
        if isinstance(frame, np.ndarray):
            h, w = frame.shape[:2]
            # Downscale if keyframe exceeds 1280px to optimize latency & network transfer
            if max(h, w) > 1280:
                scale = 1280.0 / float(max(h, w))
                frame = cv2.resize(
                    frame,
                    (int(w * scale), int(h * scale)),
                    interpolation=cv2.INTER_AREA,
                )
            success, encoded_buf = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), 80],
            )
            if not success:
                raise ValueError("Failed to encode image array to JPEG.")
            return base64.b64encode(encoded_buf.tobytes()).decode("utf-8")

        if isinstance(frame, (str, Path)):
            with open(frame, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")

        if isinstance(frame, bytes):
            return base64.b64encode(frame).decode("utf-8")

        raise TypeError(f"Unsupported frame type for encoding: {type(frame)}")

    def analyze_scene(
        self,
        frame: np.ndarray | Path | bytes | None,
        scene_index: int = 0,
        motion_energy: float = 0.5,
        sharpness_score: float = 0.5,
        duration: float = 2.0,
    ) -> SceneVLMAnalysis:
        """Analyze a scene keyframe via VLM REST endpoint with automatic heuristic fallback."""
        if self.client is None or frame is None:
            raw_frame = frame if isinstance(frame, np.ndarray) else None
            return self._heuristic_fallback(
                scene_index=scene_index,
                motion_energy=motion_energy,
                sharpness_score=sharpness_score,
                duration=duration,
                frame=raw_frame,
            )

        try:
            b64_img = self.encode_image_b64(frame)
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are an expert video editor AI. Analyze the given video scene keyframe.\n"
                        "Return a valid JSON object strictly matching this schema:\n"
                        "{\n"
                        '  "description": "<concise 1-sentence description>",\n'
                        '  "tags": ["<tag1>", "<tag2>", ...],\n'
                        '  "mood": "energetic" | "calm" | "cinematic" | "tense" | "joyful" | "neutral",\n'
                        '  "suggested_pacing": "fast" | "medium" | "slow",\n'
                        '  "is_key_moment": true | false\n'
                        "}"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Scene #{scene_index} (duration={duration:.1f}s, "
                                f"motion_energy={motion_energy:.2f}, sharpness={sharpness_score:.2f}). "
                                "Extract scene tags and semantic mood."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_img}",
                                "detail": "low",
                            },
                        },
                    ],
                },
            ]

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.config.vlm_temperature,
                response_format={"type": "json_object"},
            )

            raw_content = response.choices[0].message.content or "{}"
            # Clean possible markdown wrap
            cleaned = raw_content.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]

            parsed = json.loads(cleaned.strip())
            return SceneVLMAnalysis(
                scene_index=scene_index,
                description=str(parsed.get("description", f"Scene {scene_index}")),
                tags=[str(t).lower() for t in parsed.get("tags", ["general"])],
                mood=str(parsed.get("mood", "cinematic")).lower(),
                suggested_pacing=str(parsed.get("suggested_pacing", "medium")).lower(),
                is_key_moment=bool(parsed.get("is_key_moment", False)),
                confidence=0.95,
            )
        except Exception as exc:
            logger.warning(
                "VLM REST API call failed (%s). Falling back to heuristic analysis.",
                exc,
            )
            raw_frame = frame if isinstance(frame, np.ndarray) else None
            return self._heuristic_fallback(
                scene_index=scene_index,
                motion_energy=motion_energy,
                sharpness_score=sharpness_score,
                duration=duration,
                frame=raw_frame,
            )

    def query_text(self, prompt: str) -> str | None:
        """Query text completion from configured OpenAI/Ollama client."""
        if not self.client:
            return None
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a professional video editor AI."},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.config.vlm_temperature,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content
        except Exception as exc:
            logger.warning("VLM assistant text query failed: %s", exc)
            return None

    def _heuristic_fallback(
        self,
        scene_index: int,
        motion_energy: float,
        sharpness_score: float,
        duration: float,
        frame: np.ndarray | None = None,
    ) -> SceneVLMAnalysis:
        """Rule-based heuristic fallback estimating tags, mood, and pacing without network calls."""
        tags: list[str] = []

        # 1. Motion energy heuristic
        if motion_energy >= 0.70:
            tags.extend(["action", "dynamic", "high_energy"])
            mood = "energetic"
            pacing = "fast"
        elif motion_energy <= 0.30:
            tags.extend(["scenic", "ambient", "establishing"])
            mood = "calm"
            pacing = "slow"
        else:
            tags.extend(["narrative", "steady"])
            mood = "cinematic"
            pacing = "medium"

        # 2. Sharpness & focal heuristic
        if sharpness_score >= 0.70:
            tags.append("crisp_focus")
        elif sharpness_score <= 0.30:
            tags.append("soft_focus")

        # 3. Luminance analysis if frame array provided
        if frame is not None and frame.size > 0:
            mean_luminance = float(np.mean(frame))
            if mean_luminance > 180.0:
                tags.append("high_key")
            elif mean_luminance < 60.0:
                tags.append("low_key")

        # 4. Key moment heuristic
        is_key_moment = (
            (motion_energy >= 0.75 and sharpness_score >= 0.65)
            or (sharpness_score >= 0.85)
            or (duration >= 4.0 and sharpness_score >= 0.70)
        )
        if is_key_moment:
            tags.append("highlight")

        description = (
            f"Scene #{scene_index}: {mood.capitalize()} {pacing}-paced segment "
            f"(motion={motion_energy:.2f}, sharpness={sharpness_score:.2f})."
        )

        return SceneVLMAnalysis(
            scene_index=scene_index,
            description=description,
            tags=list(dict.fromkeys(tags)),  # Deduplicate while preserving order
            mood=mood,
            suggested_pacing=pacing,
            is_key_moment=is_key_moment,
            confidence=0.60,
        )

    @staticmethod
    def extract_keyframe(clip_path: Path | str, timestamp_sec: float) -> np.ndarray | None:
        """Extract a single frame from video file at the given timestamp."""
        p = Path(clip_path)
        if not p.exists():
            return None
        try:
            cap = cv2.VideoCapture(str(p))
            try:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp_sec * 1000.0))
                ret, frame = cap.read()
                if ret and frame is not None and frame.size > 0:
                    return frame
            finally:
                cap.release()
        except Exception as exc:
            logger.debug("Could not extract frame at %.2fs from %s: %s", timestamp_sec, p.name, exc)
        return None

    def tag_scored_scenes(
        self,
        scenes: list[ScoredScene],
        keyframes: dict[int, np.ndarray] | None = None,
        concurrency: int | None = None,
    ) -> list[ScoredScene]:
        """Enrich a list of ScoredScene instances with semantic VLM tags and metadata concurrently."""
        keyframes = keyframes or {}
        if not scenes:
            return scenes

        max_workers = concurrency or getattr(self.config, "vlm_concurrency", 4)
        max_workers = max(1, min(max_workers, len(scenes)))

        def _process_single(scene_idx: int, sc: ScoredScene) -> tuple[int, SceneVLMAnalysis]:
            frame = keyframes.get(scene_idx)
            if frame is None and sc.scene.clip_path:
                mid_t = (sc.scene.start_time + sc.scene.end_time) / 2.0
                frame = self.extract_keyframe(sc.scene.clip_path, mid_t)

            analysis = self.analyze_scene(
                frame=frame,
                scene_index=scene_idx,
                motion_energy=sc.motion_energy,
                sharpness_score=sc.sharpness_score,
                duration=sc.scene.duration,
            )
            return scene_idx, analysis

        results_map: dict[int, SceneVLMAnalysis] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(_process_single, idx, sc): idx
                for idx, sc in enumerate(scenes)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    # Timeout guard: per-request timeout + 5s buffer
                    _, analysis = future.result(timeout=self.timeout + 5.0)
                    results_map[idx] = analysis
                except Exception as exc:
                    logger.warning(
                        "VLM worker failed or timed out for scene %d (%s). Falling back to heuristic.",
                        idx,
                        exc,
                    )
                    sc = scenes[idx]
                    results_map[idx] = self._heuristic_fallback(
                        scene_index=idx,
                        motion_energy=sc.motion_energy,
                        sharpness_score=sc.sharpness_score,
                        duration=sc.scene.duration,
                        frame=keyframes.get(idx),
                    )

        for scene_idx, scored_scene in enumerate(scenes):
            analysis = results_map.get(scene_idx)
            if analysis is None:
                analysis = self._heuristic_fallback(
                    scene_index=scene_idx,
                    motion_energy=scored_scene.motion_energy,
                    sharpness_score=scored_scene.sharpness_score,
                    duration=scored_scene.scene.duration,
                )

            # Store in dataclass
            scored_scene.vlm_analysis = analysis
            scored_scene.metadata["vlm_tags"] = analysis.tags
            scored_scene.metadata["vlm_description"] = analysis.description
            scored_scene.metadata["vlm_mood"] = analysis.mood
            scored_scene.metadata["vlm_pacing"] = analysis.suggested_pacing
            scored_scene.metadata["vlm_key_moment"] = analysis.is_key_moment

            # Slight score adjustment for detected key moments (+0.05 bonus)
            if analysis.is_key_moment:
                scored_scene.composite_score = min(
                    1.0, scored_scene.composite_score + 0.05
                )

        return scenes

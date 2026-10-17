"""Domain dataclasses and typed structures for the AutoEdit pipeline."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class VideoMetadata:
    """Metadata describing a video source file."""

    path: Path
    width: int
    height: int
    fps: float
    duration: float
    total_frames: int
    codec: str = "unknown"

    @property
    def aspect_ratio(self) -> float:
        """Calculate aspect ratio (width / height)."""
        return self.width / self.height if self.height > 0 else 1.0


@dataclass(frozen=True)
class SceneCut:
    """Non-destructive scene interval extracted from raw source footage."""

    clip_path: Path
    start_time: float
    end_time: float
    start_frame: int
    end_frame: int
    duration: float

    def __post_init__(self) -> None:
        if self.end_time < self.start_time:
            raise ValueError(
                f"end_time ({self.end_time}) must be >= start_time ({self.start_time})"
            )
        if self.duration <= 0:
            object.__setattr__(self, "duration", max(0.0, self.end_time - self.start_time))


@dataclass(frozen=True)
class ActiveSegment:
    """Segment evaluated for camera motion, activity, and speech."""

    start_time: float
    end_time: float
    motion_score: float
    is_active: bool


@dataclass(frozen=True)
class BeatMarker:
    """Audio beat boundary synchronized to music rhythm."""

    timestamp: float
    frame_idx: int
    strength: float
    is_downbeat: bool


class DurationMode(str, Enum):
    """Operational mode for soundtrack and video duration decoupling."""

    FIXED = "fixed"
    FOOTAGE_DRIVEN = "footage_driven"
    FULL_TRACK = "full_track"


class SongSectionType(str, Enum):
    """Categorical classification of musical song sections."""

    INTRO = "INTRO"
    BUILDUP = "BUILDUP"
    DROP = "DROP"
    OUTRO = "OUTRO"


@dataclass(frozen=True)
class SongSection:
    """Classified structural section of a musical track."""

    section_type: SongSectionType
    start_time: float
    end_time: float
    duration: float
    avg_energy: float
    chroma_profile: list[float] = field(default_factory=list)
    is_climax: bool = False


@dataclass(frozen=True)
class CropWindow:
    """Bounding box coordinates for dynamic cropping in video reframing."""

    x: int
    y: int
    width: int
    height: int
    confidence: float = 1.0
    tracking_subject: str = "person"


@dataclass(frozen=True)
class WordSegment:
    """Exact word-level boundary with confidence score."""

    word: str
    start_time: float
    end_time: float
    probability: float


@dataclass(frozen=True)
class SentenceSegment:
    """Grouped spoken sentence/phrase preserving natural speech boundaries."""

    text: str
    start_time: float
    end_time: float
    duration: float
    words: list[WordSegment] = field(default_factory=list)
    confidence: float = 1.0
    importance_score: float = 1.0


@dataclass(frozen=True)
class SceneVLMAnalysis:
    """Semantic VLM scene understanding and tagging result."""

    scene_index: int
    description: str
    tags: list[str]
    mood: str
    suggested_pacing: str
    is_key_moment: bool
    confidence: float = 1.0


WorkflowMode = Literal["standard", "build_timelapse", "unboxing_narration"]
WatermarkPosition = Literal["top-left", "top-right", "bottom-left", "bottom-right", "center"]


class LayoutMode(str, Enum):
    """Layout and reframing mode for 9:16 vertical canvas."""

    SMART_CROP = "smart_crop"
    BLUR_BACKDROP = "blur_backdrop"
    AUTO_HYBRID = "auto_hybrid"


class SubtitleStyle(str, Enum):
    """Subtitle presentation format and typography styling."""

    CLEAN_SRT = "clean_srt"
    KINETIC_ASS = "kinetic_ass"


@dataclass
class WatermarkConfig:
    """Configuration for brand watermark overlay."""

    enabled: bool = False
    image_path: Path | None = None
    position: WatermarkPosition = "top-right"
    opacity: float = 0.85
    margin: int = 40


@dataclass
class SpeedRampingConfig:
    """Configuration for dynamic playback speed ramping."""

    enabled: bool = False
    max_speed: float = 8.0
    min_speed: float = 1.0
    high_score_speed: float = 1.0
    medium_score_speed: float = 2.0
    low_score_speed: float = 4.0


@dataclass
class ScoredScene:
    """Scene paired with objective and aesthetic visual metrics."""

    scene: SceneCut
    sharpness_score: float
    aesthetic_score: float
    composite_score: float
    motion_energy: float = 0.5
    speed: float = 1.0
    crop_window: CropWindow | None = None
    vlm_analysis: SceneVLMAnalysis | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PackedDurationResult:
    """Knapsack packing result allocating time between dialogue and visual B-roll."""

    selected_sentences: list[SentenceSegment]
    selected_scenes: list[ScoredScene]
    total_duration: float
    target_duration: float
    speech_ratio: float
    b_roll_ratio: float


@dataclass(frozen=True)
class RenderPreset:
    """Project resolution, canvas geometry, and framerate preset."""

    name: str
    width: int
    height: int
    fps: float
    description: str


@dataclass(frozen=True)
class TimelineExportResult:
    """Output metadata after timeline synthesis and export."""

    fcpxml_path: Path | None
    premiere_xml_path: Path | None
    clip_count: int
    total_duration: float
    otio_path: Path | None = None
    capcut_draft_path: Path | None = None


@dataclass(frozen=True)
class RenderJobResult:
    """Result of standalone headless video rendering."""

    output_path: Path
    encoder_used: str
    duration: float
    file_size_bytes: int
    return_code: int = 0


@dataclass
class PipelineExecutionResult:
    """Unified result structure produced by StudioBackendAdapter pipeline execution."""

    success: bool
    total_duration: float
    clip_count: int
    scenes_detected: int
    audio_bpm: float
    sections_detected: int
    fcpxml_path: Path | None
    premiere_xml_path: Path | None
    otio_path: Path | None
    capcut_draft_path: Path | None
    render_path: Path | None
    encoder_used: str | None
    resolve_status: dict[str, Any] | None
    dialogue_sentences: int = 0
    error_message: str | None = None
    scored_scenes: list[ScoredScene] = field(default_factory=list)

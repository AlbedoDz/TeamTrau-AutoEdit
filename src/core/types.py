"""Domain dataclasses and typed structures for the AutoEdit pipeline."""

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from src.core.timebase import frame_rate_fraction

if TYPE_CHECKING:  # pragma: no cover - typing only (config imports this module)
    from src.core.config import PipelineConfig


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
    camera_role: str = "cam_a"
    source_folder: Path | None = None
    is_vfr: bool = False
    rotation: int = 0
    has_audio: bool = True

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
    # User's original media when ``clip_path`` is a render proxy (VFR->CFR cache file).
    original_path: Path | None = None

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


class CadenceProfile(str, Enum):
    """How quickly shots turn over relative to the musical bar (see ``src/core/cadence.py``)."""

    FAST_SYNCOPATED = "fast_syncopated"
    BALANCED_NARRATIVE = "balanced_narrative"
    LINGERING_CINEMATIC = "lingering_cinematic"
    VIRAL_BURST = "viral_burst"


@dataclass
class MusicalBarGrid:
    """Bar/phrase lattice of a soundtrack (seconds on the soundtrack/timeline clock).

    ``phrase_N`` lists the bar times that START an N-bar phrase. Phrases restart at every
    SongSection boundary, so a phrase never spans two sections. ``confidence`` is in [0, 1];
    below ~0.4 callers should fall back to the legacy beat filter.
    """

    tempo_bpm: float
    beats_per_bar: int = 4
    bar_times: list[float] = field(default_factory=list)
    phrase_2: list[float] = field(default_factory=list)
    phrase_4: list[float] = field(default_factory=list)
    phrase_8: list[float] = field(default_factory=list)
    confidence: float = 0.0
    bar_confidence: list[float] = field(default_factory=list)


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


WorkflowMode = Literal["standard", "build_timelapse", "unboxing_narration", "rough_cut"]
WatermarkPosition = Literal["top-left", "top-right", "bottom-left", "bottom-right", "center"]


class DefectType(str, Enum):
    """Categorical classification of raw footage defects."""

    HEAD_TAIL_WOBBLE = "HEAD_TAIL_WOBBLE"
    CAMERA_SHAKE = "CAMERA_SHAKE"
    WHIP_PAN = "WHIP_PAN"
    GROUND_CARRYING = "GROUND_CARRYING"
    DEFOCUS_BLUR = "DEFOCUS_BLUR"
    LENS_OCCLUSION = "LENS_OCCLUSION"
    EXPOSURE_CLIPPING = "EXPOSURE_CLIPPING"
    FLICKER = "FLICKER"
    AUDIO_RUMBLE = "AUDIO_RUMBLE"
    MICRO_TAKE = "MICRO_TAKE"


@dataclass(frozen=True)
class DefectSpan:
    """Interval within a clip flagged with a specific defect."""

    start_time: float
    end_time: float
    defect_type: DefectType
    severity: float = 1.0
    description: str = ""


@dataclass
class CleanedSegment:
    """Non-destructively trimmed golden segment free from operator/environmental defects."""

    scene: SceneCut
    clip_path: Path
    start_time: float
    end_time: float
    duration: float
    source_start_time: float
    source_end_time: float
    defects_removed: list[DefectSpan] = field(default_factory=list)
    audio_filter: str | None = None
    requires_deflicker: bool = False
    sharpness_score: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


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
    camera_role: str = "cam_a"
    metadata: dict[str, Any] = field(default_factory=dict)


# Container extensions the ingest scan, media browser and CLI accept as footage (lowercase).
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4", ".mov", ".mkv", ".m4v", ".mxf", ".avi", ".mts", ".m2ts",
        ".webm", ".mpg", ".mpeg", ".wmv", ".3gp", ".flv",
    }
)
# Camera RAW containers ffmpeg cannot decode: skipped during ingest with a warning.
UNSUPPORTED_RAW_EXTENSIONS: frozenset[str] = frozenset({".braw", ".r3d"})

DeliverFormat = Literal["mp4", "fcpxml", "premiere", "otio", "capcut"]
ALL_DELIVER_FORMATS: tuple[DeliverFormat, ...] = ("mp4", "fcpxml", "premiere", "otio", "capcut")


@dataclass
class EditClip:
    """One clip placed on the edit timeline (the single source of truth for Deliver).

    ``frames`` is the clip's length on the timeline at ``EditTimeline.fps``; ``available``
    is how many source seconds exist from ``source_in`` (if the clip needs more, the
    renderer holds the last frame and NLE exports insert a gap).
    """

    source_path: Path
    source_in: float
    frames: int
    available: float
    speed: float = 1.0
    crop_window: CropWindow | None = None
    ken_burns: str | None = None
    transition_in_frames: int = 0
    is_teaser: bool = False
    label: str = ""
    markers: list[dict[str, Any]] = field(default_factory=list)
    # The user's original media when ``source_path`` is a render proxy (e.g. a VFR->CFR cache
    # file). NLE exports link to it so projects never point into the .cache folder.
    original_path: Path | None = None
    # Set by retarget_timeline when the canvas aspect changed: ``crop_window`` was computed for
    # the previous canvas and exporters/renderers should recompute (or ignore) it.
    crop_needs_recompute: bool = False
    # Identity of the originating scene (source seconds). Set when a clip is built from a scene so
    # curation/variants do not depend on ``source_in`` once clips use arbitrary sub-windows.
    scene_start: float | None = None
    scene_end: float | None = None

    def media_path(self, relink_original: bool = True) -> Path:
        """Path an NLE file should reference: the original media when known, else the source."""
        if relink_original and self.original_path is not None:
            return Path(self.original_path)
        return Path(self.source_path)

    @property
    def source_path_resolved(self) -> Path:
        return Path(self.source_path).resolve()

    # Backwards-compatible aliases used by the renderer.
    @property
    def source(self) -> Path:
        return self.source_path


@dataclass
class EditTimeline:
    """Ordered, frame-exact edit decision list produced by analysis and edited by the user."""

    name: str
    fps: float
    canvas_width: int
    canvas_height: int
    clips: list[EditClip] = field(default_factory=list)
    soundtrack_path: Path | None = None
    soundtrack_in: float = 0.0
    sections: list[SongSection] = field(default_factory=list)
    variant_key: str = "master"
    kind: Literal["music", "rough_cut"] = "music"
    # Musical lattice used to place cuts (for the ruler) and the largest distance, in seconds,
    # between an ideal cadence cut and the frame it was snapped to.
    bar_grid: MusicalBarGrid | None = None
    cut_snap_error_max_sec: float | None = None

    @property
    def total_frames(self) -> int:
        return sum(c.frames for c in self.clips)

    @property
    def duration(self) -> float:
        return self.total_frames / self.fps if self.fps > 0 else 0.0

    def cut_points(self) -> list[float]:
        """Clip boundaries in timeline seconds (0, ..., duration)."""
        pts = [0.0]
        acc = 0
        for c in self.clips:
            acc += c.frames
            pts.append(acc / self.fps)
        return pts


DeliverFitMode = Literal["auto", "fit", "fill", "stretch"]
DeliverColor = Literal["rec709", "rec2020_hlg", "rec2020_pq", "passthrough"]
DeliverMaster = Literal["hevc", "h264", "prores_422", "prores_hq", "dnxhr_hqx"]
DeliverSubtitles = Literal["none", "srt", "ass", "burn"]
VALID_AUDIO_RATES: tuple[int, ...] = (44100, 48000, 96000)


@dataclass
class DeliverSettings:
    """Everything chosen at Deliver time (canvas, timebase, colour, audio, master, extras).

    Validated on construction; call :meth:`validate` again after mutating fields.
    ``fps`` stays a float for compatibility - use :attr:`fps_fraction` for exact arithmetic.
    """

    canvas_width: int
    canvas_height: int
    fps: float
    fit_mode: DeliverFitMode = "auto"
    color: DeliverColor = "rec709"
    audio_rate: int = 48000
    audio_channels: int = 2
    master: DeliverMaster = "hevc"
    subtitles: DeliverSubtitles = "none"
    watermark_in_nle: bool = False
    relink_original: bool = True
    variants: list[str] = field(default_factory=lambda: ["active"])
    fcpxml_version: str = "1.8"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Raise ValueError with a clear message for any invalid field."""
        for name in ("canvas_width", "canvas_height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")
            if value % 2:
                raise ValueError(f"{name} must be even (video encoders need even sizes), got {value}")
        try:
            fps_ok = float(self.fps) > 0
        except (TypeError, ValueError):
            fps_ok = False
        if not fps_ok:
            raise ValueError(f"fps must be a positive number, got {self.fps!r}")
        if self.audio_rate not in VALID_AUDIO_RATES:
            raise ValueError(
                f"audio_rate must be one of {list(VALID_AUDIO_RATES)}, got {self.audio_rate!r}"
            )
        if self.audio_channels not in (1, 2):
            raise ValueError(f"audio_channels must be 1 or 2, got {self.audio_channels!r}")
        for name, literal in (
            ("fit_mode", DeliverFitMode),
            ("color", DeliverColor),
            ("master", DeliverMaster),
            ("subtitles", DeliverSubtitles),
        ):
            value = getattr(self, name)
            allowed = get_args(literal)
            if value not in allowed:
                raise ValueError(f"{name} must be one of {list(allowed)}, got {value!r}")
        if not isinstance(self.variants, list) or not self.variants or not all(
            isinstance(v, str) and v for v in self.variants
        ):
            raise ValueError(
                f"variants must be a non-empty list of variant keys (or ['all']), got {self.variants!r}"
            )
        if not isinstance(self.fcpxml_version, str) or not self.fcpxml_version.strip():
            raise ValueError("fcpxml_version must be a non-empty string such as '1.8'")

    @property
    def fps_fraction(self) -> Fraction:
        """Exact frame rate (29.97 -> 30000/1001)."""
        return frame_rate_fraction(self.fps)

    @property
    def canvas(self) -> tuple[int, int]:
        return (self.canvas_width, self.canvas_height)

    @classmethod
    def from_timeline(
        cls, edit_tl: "EditTimeline", config: "PipelineConfig | None" = None, **overrides: Any
    ) -> "DeliverSettings":
        """Settings matching ``edit_tl`` (canvas, fps); the rest from ``config`` defaults."""
        kwargs: dict[str, Any] = {}
        if config is not None:
            if getattr(config, "burn_hardsubs", False):
                kwargs["subtitles"] = "burn"
            elif getattr(config, "enable_transcription", False) and getattr(
                config, "export_subtitles_srt", False
            ):
                kwargs["subtitles"] = "srt"
        kwargs.update(overrides)
        return cls(
            canvas_width=edit_tl.canvas_width,
            canvas_height=edit_tl.canvas_height,
            fps=edit_tl.fps,
            **kwargs,
        )


@dataclass
class DeliverResult:
    """Outcome of an explicit user-requested Deliver/Export job."""

    success: bool
    output_dir: Path | None = None
    render_path: Path | None = None
    encoder_used: str | None = None
    duration: float = 0.0
    fcpxml_path: Path | None = None
    premiere_xml_path: Path | None = None
    otio_path: Path | None = None
    capcut_draft_path: Path | None = None
    resolve_status: dict[str, Any] | None = None
    is_preview: bool = False
    error_message: str | None = None
    cancelled: bool = False
    # Phase 22 step 7
    manifest_path: Path | None = None
    subtitle_paths: list[Path] = field(default_factory=list)
    master_codec: str | None = None  # DeliverSettings.master used for the render
    settings: "DeliverSettings | None" = None
    # Per-variant outcomes (key -> result) when several timelines were delivered into subfolders.
    variant_results: dict[str, "DeliverResult"] = field(default_factory=dict)


@dataclass
class TimelineVariant:
    """Distinct narrative timeline cut generated from the same source footage."""

    name: str
    display_name: str
    description: str = ""
    target_pacing: str = "moderate"
    clip_count: int = 0
    total_duration: float = 0.0
    otio_timeline: Any = None
    fcpxml_path: Path | None = None
    premiere_xml_path: Path | None = None
    otio_path: Path | None = None
    capcut_draft_path: Path | None = None
    render_path: Path | None = None
    sequence_scenes: list["ScoredScene"] = field(default_factory=list)
    edit_timeline: EditTimeline | None = None


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
    timeline_variants: dict[str, TimelineVariant] = field(default_factory=dict)
    active_variant_name: str | None = None
    failed_files: list[tuple[Path, str]] = field(default_factory=list)
    cancelled: bool = False
    edit_timelines: dict[str, EditTimeline] = field(default_factory=dict)
    active_soundtrack: Path | None = None
    cut_timestamps: list[float] = field(default_factory=list)
    sections: list[SongSection] = field(default_factory=list)
    source_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Staged workflow (Phase 23 B1): which stage produced this result, the golden takes (rough cut)
    # and the REAL soundtrack analysis (an ``AudioAnalysisResult``; typed Any to avoid an import cycle).
    stage: Literal["rough_cut", "sequence", "all"] = "all"
    cleaned_segments: list[CleanedSegment] = field(default_factory=list)
    audio_analysis: Any = None

    @property
    def bar_grid(self) -> "MusicalBarGrid | None":
        return getattr(self.audio_analysis, "bar_grid", None)

    @property
    def beats(self) -> list[BeatMarker]:
        return list(getattr(self.audio_analysis, "beats", None) or [])


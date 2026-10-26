"""Central configuration dataclass and presets for the AutoEdit pipeline."""

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, Field

from src.core.ffmpeg_utils import run_tool
from src.core.platform_caps import detect_os, resolve_paths
from src.core.timebase import frame_rate_fraction  # noqa: F401  (re-exported)
from src.core.types import RenderPreset, WatermarkPosition, WorkflowMode

# Predefined standard industry presets. Default is 1080p30.
PRESETS: Final[dict[str, RenderPreset]] = {
    "1080p30": RenderPreset(
        name="1080p30",
        width=1920,
        height=1080,
        fps=30.0,
        description="Full HD 1080p at 30 fps (Default Standard Deliverable)",
    ),
    "1080p60": RenderPreset(
        name="1080p60",
        width=1920,
        height=1080,
        fps=60.0,
        description="Full HD 1080p at 60 fps (High Motion / Smooth)",
    ),
    "1080p24": RenderPreset(
        name="1080p24",
        width=1920,
        height=1080,
        fps=24.0,
        description="Full HD 1080p at 24 fps (Cinematic Standard)",
    ),
    "4k30": RenderPreset(
        name="4k30",
        width=3840,
        height=2160,
        fps=30.0,
        description="Ultra HD 4K at 30 fps (4K Master Deliverable)",
    ),
    "4k60": RenderPreset(
        name="4k60",
        width=3840,
        height=2160,
        fps=60.0,
        description="Ultra HD 4K at 60 fps (4K Action / Smooth)",
    ),
    "4k24": RenderPreset(
        name="4k24",
        width=3840,
        height=2160,
        fps=24.0,
        description="Ultra HD 4K at 24 fps (Cinematic 4K Master)",
    ),
    "social_vertical": RenderPreset(
        name="social_vertical",
        width=1080,
        height=1920,
        fps=30.0,
        description="Vertical 9:16 at 30 fps (TikTok, Reels, Shorts)",
    ),
    "social_vertical_60": RenderPreset(
        name="social_vertical_60",
        width=1080,
        height=1920,
        fps=60.0,
        description="Vertical 9:16 at 60 fps (TikTok, Reels, Shorts High Motion)",
    ),
    "cinema_4k": RenderPreset(
        name="cinema_4k",
        width=4096,
        height=2160,
        fps=24.0,
        description="DCI 4K at 24 fps (Theatrical Cinema Standard)",
    ),
    # --- Phase 22 additions: NTSC broadcast rates and extra canvases -----------------------
    "1080p23.976": RenderPreset(
        name="1080p23.976",
        width=1920,
        height=1080,
        fps=23.976,
        description="Full HD 1080p at 23.976 fps (NTSC Cinematic)",
    ),
    "1080p29.97": RenderPreset(
        name="1080p29.97",
        width=1920,
        height=1080,
        fps=29.97,
        description="Full HD 1080p at 29.97 fps (NTSC Broadcast)",
    ),
    "1080p59.94": RenderPreset(
        name="1080p59.94",
        width=1920,
        height=1080,
        fps=59.94,
        description="Full HD 1080p at 59.94 fps (NTSC High Motion)",
    ),
    "4k23.976": RenderPreset(
        name="4k23.976",
        width=3840,
        height=2160,
        fps=23.976,
        description="Ultra HD 4K at 23.976 fps (NTSC Cinematic 4K)",
    ),
    "4k29.97": RenderPreset(
        name="4k29.97",
        width=3840,
        height=2160,
        fps=29.97,
        description="Ultra HD 4K at 29.97 fps (NTSC Broadcast 4K)",
    ),
    "4k59.94": RenderPreset(
        name="4k59.94",
        width=3840,
        height=2160,
        fps=59.94,
        description="Ultra HD 4K at 59.94 fps (NTSC 4K High Motion)",
    ),
    "portrait_2k": RenderPreset(
        name="portrait_2k",
        width=1440,
        height=2560,
        fps=30.0,
        description="Vertical 9:16 2K (1440x2560) at 30 fps",
    ),
    "portrait_4k": RenderPreset(
        name="portrait_4k",
        width=2160,
        height=3840,
        fps=30.0,
        description="Vertical 9:16 4K (2160x3840) at 30 fps",
    ),
    "square_1080": RenderPreset(
        name="square_1080",
        width=1080,
        height=1080,
        fps=30.0,
        description="Square 1:1 (1080x1080) at 30 fps (Instagram / Facebook feed)",
    ),
    "4x5_1080x1350": RenderPreset(
        name="4x5_1080x1350",
        width=1080,
        height=1350,
        fps=30.0,
        description="Portrait 4:5 (1080x1350) at 30 fps (Instagram feed)",
    ),
    "ultrawide_2560x1080": RenderPreset(
        name="ultrawide_2560x1080",
        width=2560,
        height=1080,
        fps=30.0,
        description="Ultrawide 21:9 (2560x1080) at 30 fps",
    ),
    "dci_2k_2048x1080": RenderPreset(
        name="dci_2k_2048x1080",
        width=2048,
        height=1080,
        fps=24.0,
        description="DCI 2K (2048x1080) at 24 fps (Theatrical)",
    ),
}

DEFAULT_PRESET_NAME: Final[str] = "1080p30"


def find_default_resolve_script_path() -> Path:
    """Discover DaVinci Resolve Scripting module path for the current OS."""
    os_name = detect_os()
    paths = resolve_paths(os_name)
    default_modules = paths.api_dir / "Modules"
    env_api = os.environ.get("RESOLVE_SCRIPT_API")
    candidates: list[Path] = []
    if env_api:
        candidates.append(Path(env_api) / "Modules")
    candidates.append(default_modules)
    if os_name == "windows":
        candidates.append(
            Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Developer\Scripting\Modules")
        )
    for p in candidates:
        if p.exists() and (p / "DaVinciResolveScript.py").exists():
            return p
    return default_modules


def find_default_resolve_exe() -> Path:
    """Discover DaVinci Resolve executable for the current OS."""
    candidates = resolve_paths(detect_os()).exe_candidates
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def _default_cache_dir() -> Path:
    """Return default cache directory, honoring TEAMTRAU_CACHE_DIR when set."""
    env_cache = os.environ.get("TEAMTRAU_CACHE_DIR")
    return Path(env_cache) if env_cache else Path(".cache/autoedit")


def check_cuda_available() -> bool:
    """Check if NVIDIA CUDA runtime is accessible."""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except ImportError:
        pass

    # Fallback to nvidia-smi probe
    try:
        res = subprocess.run(
            ["nvidia-smi"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=3,
        )
        return res.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=1)
def check_nvenc_available() -> bool:
    """Verify that hevc_nvenc can actually encode (probed once per process).

    Distro ffmpeg builds list the NVENC encoders even on machines without an NVIDIA GPU,
    so a listed encoder is confirmed with a one-frame test encode.
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return False
    try:
        res = run_tool([ffmpeg_bin, "-hide_banner", "-encoders"], timeout=10)
        if "hevc_nvenc" not in (res.stdout or ""):
            return False
        trial = run_tool(
            [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=black:s=256x256:d=0.1", "-frames:v", "1", "-c:v", "hevc_nvenc",
             "-f", "null", "-"],
            timeout=20,
        )
        return trial.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


class PipelineConfig(BaseModel):
    """Central configuration parameters for the AutoEdit pipeline."""

    # Canvas & Preset Settings (Default: 1080p30)
    preset_name: str = Field(default=DEFAULT_PRESET_NAME)
    canvas_width: int = Field(default=1920)
    canvas_height: int = Field(default=1080)
    fps: float = Field(default=30.0)

    # Workflow Mode (Standard, Build / Timelapse, Unboxing / Narration)
    workflow_mode: WorkflowMode = Field(default="standard")
    keep_video_audio: bool = Field(default=True)
    enable_audio_fix: bool = Field(default=True)

    # Dynamic Speed Ramping (1x - 8x)
    enable_speed_ramping: bool = Field(default=False)
    max_speed_multiplier: float = Field(default=8.0)
    min_speed_multiplier: float = Field(default=1.0)

    # Watermark / Branding Engine
    watermark_enabled: bool = Field(default=False)
    watermark_path: Path | None = Field(default=None)
    watermark_position: WatermarkPosition = Field(default="top-right")
    watermark_opacity: float = Field(default=0.85)
    watermark_margin: int = Field(default=40)

    # Ingestion & Detection Constants
    adaptive_threshold: float = Field(default=3.0)
    min_scene_len_sec: float = Field(default=0.8)
    auto_editor_motion_threshold: float = Field(default=0.05)
    auto_editor_margin: str = Field(default="0.2s")
    auto_normalize_vfr: bool = Field(default=True)
    # Analyse footage wider than this through a GPU-made copy of that width (0/False: originals).
    analysis_proxy: bool = Field(default=True)
    analysis_proxy_width: int = Field(default=1920)

    # Audio & Rhythm Constants
    min_beat_interval: float = Field(default=0.4)
    standard_min_beat_interval: float = Field(default=0.80)
    climax_min_beat_interval: float = Field(default=0.40)
    audio_sample_rate: int = Field(default=22050)
    enable_structural_sync: bool = Field(default=True)
    duration_mode: str = Field(default="fixed")  # "fixed", "footage_driven", "full_track"
    target_duration: float | None = Field(default=None)
    audio_fade_out_sec: float = Field(default=2.5)
    action_drop_threshold: float = Field(default=0.70)

    # Rhythmic cadence engine (Phase 23): cuts on bar/half-bar points with a per-section curve.
    # ``use_cadence_engine=False`` keeps the legacy beat filter (0.8 s / 0.4 s) as the pipeline.
    use_cadence_engine: bool = Field(default=True)
    cadence_profile: str = Field(default="balanced_narrative")
    cadence_multiples: list[float] | None = Field(default=None)  # None = take from the profile
    cadence_seed: int = Field(default=7)
    enable_rough_cut_pruning: bool = Field(default=True)
    # Staged workflow: ``submit_job(stage="all")`` runs the rough-cut cleaner first only when True
    # (the GUI / agents set it from the "Rough cut pruning" checkbox); False = classic one-pass flow.
    run_rough_cut_in_all: bool = Field(default=False)
    extended_shot_threshold_sec: float = Field(default=12.0)
    max_scene_len_sec: float = Field(default=16.0)
    allow_multi_segment_for_long_shots: bool = Field(default=True)
    golden_window_min_sec: float = Field(default=3.0)
    golden_window_max_sec: float = Field(default=8.0)
    auto_classify_roles: bool = Field(default=True)
    enable_golden_windows: bool = Field(default=True)

    # Visual Scoring Constants
    sharpness_weight: float = Field(default=0.5)
    aesthetic_weight: float = Field(default=0.5)
    keyframes_per_scene: int = Field(default=3)

    # Parallel Ingestion & Cache Optimization (Kaizen)
    max_workers: int = Field(default=4)
    cache_dir: Path = Field(default_factory=_default_cache_dir)
    enable_cache: bool = Field(default=True)

    # Dead-Air & Freeze Detection (mazsola2k/ai-video-editor logic)
    enable_dead_air_detection: bool = Field(default=True)
    silence_threshold_db: float = Field(default=-35.0)
    freeze_threshold: float = Field(default=0.02)
    dead_air_min_duration: float = Field(default=3.0)

    # Perceptual Hash Deduplication
    enable_deduplication: bool = Field(default=True)
    duplicate_hash_threshold: int = Field(default=6)

    # Retention Teaser Hook & Multi-Variant Synthesis
    enable_teaser: bool = Field(default=True)
    teaser_duration: float = Field(default=6.0)
    generate_variants: int = Field(default=3, ge=1, le=3)

    # AI Smart 9:16 Re-framing & Backdrop Blur
    enable_smart_reframing: bool = Field(default=True)
    reframing_mode: str = Field(default="auto_hybrid")
    reframer_model: str = Field(default="yolov8n.pt")
    reframer_confidence_threshold: float = Field(default=0.35)
    reframer_smoothing_alpha: float = Field(default=0.20)
    reframer_sample_frames: int = Field(default=5)

    # Visual Continuity & Exposure Guard
    enable_exposure_guard: bool = Field(default=True)
    exposure_guard_threshold: float = Field(default=0.45)
    cross_dissolve_frames: int = Field(default=10)
    enable_ken_burns: bool = Field(default=True)
    ken_burns_motion_threshold: float = Field(default=0.20)
    ken_burns_scale_factor: float = Field(default=1.08)

    # Speech-to-Text & Subtitle Formatter
    enable_transcription: bool = Field(default=False)
    whisper_model_size: str = Field(default="base")
    whisper_device: str = Field(default="cuda")
    whisper_compute_type: str = Field(default="float16")
    dialogue_importance_threshold: float = Field(default=0.50)
    target_speech_ratio: float = Field(default=0.60)
    ducking_attenuation_db: float = Field(default=-14.0)
    subtitle_cpl: int = Field(default=37)
    subtitle_style: str = Field(default="kinetic_ass")
    subtitle_margin_v_vertical: int = Field(default=320)
    subtitle_margin_v_horizontal: int = Field(default=60)
    min_clip_frames_threshold: int = Field(default=6)
    export_subtitles_srt: bool = Field(default=True)
    burn_hardsubs: bool = Field(default=False)

    # CapCut Export Settings (Phase 4)
    enable_capcut_export: bool = Field(default=True)
    capcut_draft_dir: Path | None = Field(default=None)

    # LLM / VLM Context Integration (Phase 5)
    enable_vlm_tagging: bool = Field(default=True)
    vlm_provider: str = Field(default="auto")
    vlm_base_url: str | None = Field(default=None)
    vlm_api_key: str | None = Field(default=None)
    vlm_model: str = Field(default="openai/gpt-4o-mini")
    vlm_temperature: float = Field(default=0.2)
    vlm_timeout_sec: float = Field(default=15.0)
    vlm_concurrency: int = Field(default=4, ge=1, le=16)

    # Universal AI Agent & MCP Hub Integration
    agent_provider: str = Field(default="auto")  # "auto", "openrouter", "deepseek", "openai", "ollama", "lm_studio", "custom"
    agent_base_url: str | None = Field(default=None)
    agent_api_key: str | None = Field(default=None)
    agent_model: str = Field(default="deepseek/deepseek-chat")
    agent_temperature: float = Field(default=0.2)
    agent_max_steps: int = Field(default=10)
    mcp_transport: str = Field(default="stdio")  # "stdio", "sse"
    mcp_host: str = Field(default="127.0.0.1")
    mcp_port: int = Field(default=8000)

    # NVENC Render Settings
    hw_encoder: Literal["auto", "nvenc", "videotoolbox", "qsv", "amf", "none"] = Field(default="auto")
    nvenc_preset: str = Field(default="p5")  # p7 = slowest/best; p5 is ~2x faster at near-identical quality
    nvenc_cq: int = Field(default=18)
    nvenc_hwdecode: bool = Field(default=True)  # NVDEC (-hwaccel cuda); frames stay in system memory
    nvenc_bitrate: str = Field(default="60M")
    nvenc_maxrate: str = Field(default="80M")
    nvenc_bufsize: str = Field(default="120M")
    nvenc_pix_fmt: str = Field(default="yuv420p")
    cpu_encoder_preset: str = Field(default="medium")
    render_workers: int = Field(default=2, ge=1, le=8)
    camera_audio_mix_db: float = Field(default=-12.0)
    # Deliver master (Phase 22 step 6)
    master_audio_limit: float = Field(default=0.95, gt=0.0, le=1.0)  # final alimiter ceiling (linear)
    master_audio_bitrate: str = Field(default="320k")  # AAC bitrate for mp4 masters
    burn_subtitle_font: str = Field(default="Arial")  # covers Vietnamese diacritics
    burn_subtitle_font_size: int = Field(default=0, ge=0, le=96)  # 0 = auto (libass 384x288 units)

    # Rough-Cut & Defect Pruning Constants
    enable_head_tail_trim: bool = Field(default=True)
    head_prune_sec: float = Field(default=0.8)
    tail_prune_sec: float = Field(default=0.5)
    head_tail_motion_ratio: float = Field(default=2.5)
    enable_shake_pruning: bool = Field(default=True)
    max_shake_threshold: float = Field(default=80.0)  # px/frame
    max_shake_min_duration: float = Field(default=0.3)  # sec
    enable_ground_carrying_pruning: bool = Field(default=True)
    enable_focus_guard: bool = Field(default=True)
    focus_drop_ratio: float = Field(default=0.65)
    focus_drop_min_duration: float = Field(default=0.5)
    enable_occlusion_guard: bool = Field(default=True)
    occlusion_area_ratio: float = Field(default=0.40)
    occlusion_luma_threshold: int = Field(default=15)
    occlusion_min_duration: float = Field(default=0.4)
    enable_exposure_clipping_guard: bool = Field(default=True)
    exposure_clip_ratio: float = Field(default=0.30)
    enable_flicker_repair: bool = Field(default=True)
    enable_audio_conditioning: bool = Field(default=True)
    audio_rumble_threshold_db: float = Field(default=-18.0)
    min_take_duration_sec: float = Field(default=1.5)
    keep_camera_audio: bool = Field(default=True)

    # External NLE Paths
    resolve_script_path: Path = Field(default_factory=find_default_resolve_script_path)
    resolve_exe_path: Path = Field(default_factory=find_default_resolve_exe)

    def apply_preset(self, preset_name: str) -> None:
        """Apply a named preset to update resolution and framerate."""
        if preset_name not in PRESETS:
            available = ", ".join(PRESETS.keys())
            raise ValueError(f"Unknown preset '{preset_name}'. Available presets: {available}")
        preset = PRESETS[preset_name]
        self.preset_name = preset.name
        self.canvas_width = preset.width
        self.canvas_height = preset.height
        self.fps = preset.fps

    def apply_workflow_mode(self, mode: WorkflowMode) -> None:
        """Apply workflow mode configuration (standard, build_timelapse, unboxing_narration, rough_cut)."""
        if mode == "build_timelapse":
            self.workflow_mode = "build_timelapse"
            self.enable_speed_ramping = True
            self.keep_video_audio = False
        elif mode == "unboxing_narration":
            self.workflow_mode = "unboxing_narration"
            self.enable_speed_ramping = False
            self.keep_video_audio = True
            self.enable_dead_air_detection = True
        elif mode == "standard":
            self.workflow_mode = "standard"
            self.keep_video_audio = True
        elif mode == "rough_cut":
            self.workflow_mode = "rough_cut"
            self.enable_speed_ramping = False
            self.enable_teaser = False
            self.enable_structural_sync = False
            self.keep_video_audio = True
            self.keep_camera_audio = True
            self.enable_dead_air_detection = False
        else:
            raise ValueError(f"Unknown workflow mode: '{mode}'. Must be one of: standard, build_timelapse, unboxing_narration, rough_cut")

    @classmethod
    def from_preset(cls, preset_name: str = DEFAULT_PRESET_NAME, **overrides) -> "PipelineConfig":
        """Instantiate PipelineConfig initialized with a designated preset."""
        config = cls(**overrides)
        config.apply_preset(preset_name)
        return config

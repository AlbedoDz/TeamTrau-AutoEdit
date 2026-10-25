"""Production-Grade Automated Video Editing Orchestrator CLI."""

import json
import logging
import os
import secrets
import sys
from pathlib import Path

import click
import uvicorn

from src.core import curation
from src.core.config import (
    DEFAULT_PRESET_NAME,
    PRESETS,
    PipelineConfig,
    check_cuda_available,
    check_nvenc_available,
)
from src.core.platform_caps import detect_os
from src.core.platform_paths import prepend_tool_dir, resolve_data_dir
from src.core.service import StudioBackendAdapter
from src.core.stage_options import CADENCE_NAMES, apply_staged_options
from src.engine_api.ai_settings import AiSettingsStore
from src.engine_api.app import build_server_config, create_app
from src.engine_api.sessions import SessionStore
from src.modules.ingest_detector import IngestDetector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("autoedit")


@click.group()
def cli() -> None:
    """TeamTrau AutoEdit: Production-Grade Automated Video Editing Orchestrator."""


@cli.command("list-presets")
def list_presets() -> None:
    """List all available project canvas & framerate presets."""
    click.echo("\nAvailable Presets:")
    click.echo("-" * 70)
    for name, p in PRESETS.items():
        is_default = " (DEFAULT)" if name == DEFAULT_PRESET_NAME else ""
        click.echo(f"  * {name:<18} : {p.width}x{p.height} @ {p.fps:.1f} fps{is_default}")
        click.echo(f"    {p.description}")
    click.echo("-" * 70)


@cli.command("clear-cache")
def clear_cache_cmd() -> None:
    """Remove all cached scene detection metadata."""
    cfg = PipelineConfig()
    detector = IngestDetector(cfg)
    count = detector.clear_cache()
    click.echo(f"Successfully cleared {count} cached scene records from {cfg.cache_dir}")


@cli.command("check-env")
def check_env() -> None:
    """Verify hardware acceleration, external tools, and NLE API bindings."""
    click.echo("\n=== AutoEdit Environment & Hardware Diagnostics ===")
    click.echo(f"Python Runtime      : {sys.version.split()[0]} ({sys.executable})")

    cuda_ok = check_cuda_available()
    click.echo(f"NVIDIA CUDA Support : {'[ONLINE]' if cuda_ok else '[OFFLINE]'}")
    if cuda_ok:
        try:
            import torch

            if torch.cuda.is_available():
                click.echo(f"CUDA Device Name    : {torch.cuda.get_device_name(0)}")
        except Exception:
            pass

    nvenc_ok = check_nvenc_available()
    click.echo(f"FFmpeg hevc_nvenc   : {'[ONLINE]' if nvenc_ok else '[OFFLINE]'}")

    cfg = PipelineConfig()
    resolve_script = cfg.resolve_script_path
    resolve_exe = cfg.resolve_exe_path

    resolve_installed = resolve_exe.is_file()
    resolve_api_ready = (resolve_script / "DaVinciResolveScript.py").is_file()

    click.echo(
        f"DaVinci Resolve App : {'[FOUND] ' + str(resolve_exe) if resolve_installed else '[NOT FOUND]'}"
    )
    click.echo(
        f"Resolve Script API  : {'[READY] ' + str(resolve_script) if resolve_api_ready else '[NOT FOUND]'}"
    )
    click.echo("====================================================\n")


@cli.command("mcp")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"]),
    default="stdio",
    help="Transport protocol for MCP server (stdio or sse).",
)
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host address for SSE transport (default: 127.0.0.1).",
)
@click.option(
    "--port",
    default=8000,
    type=int,
    help="Port number for SSE transport (default: 8000).",
)
@click.option(
    "--export-config",
    type=click.Choice(["antigravity", "claude_desktop", "cursor", "all"]),
    default=None,
    help="Print MCP configuration snippet for external AI agents and exit.",
)
def mcp_cmd(transport: str, host: str, port: int, export_config: str | None) -> None:
    """Run Model Context Protocol (MCP) server or generate AI agent integration configs."""
    import json

    from src.modules.mcp_server import generate_mcp_config, run_mcp_sse, run_mcp_stdio

    if export_config:
        cfg = generate_mcp_config(client_name=export_config, transport=transport, host=host, port=port)
        click.echo(json.dumps(cfg, indent=2))
        return

    logger.info("Starting TeamTrau AutoEdit MCP server (transport=%s, host=%s, port=%d)...", transport, host, port)
    if transport == "sse":
        run_mcp_sse(host=host, port=port)
    else:
        run_mcp_stdio()


@cli.command("agent")
@click.argument("prompt", type=str)
@click.option(
    "--provider",
    type=click.Choice(["auto", "openrouter", "deepseek", "openai", "ollama", "lm_studio", "custom"]),
    default="auto",
    help="AI LLM/VLM Provider (default: auto).",
)
@click.option(
    "--model",
    default="deepseek/deepseek-chat",
    help="Target model identifier (e.g. deepseek/deepseek-chat, anthropic/claude-3.5-sonnet, gpt-4o).",
)
@click.option(
    "--api-key",
    default=None,
    help="API Secret key for cloud provider.",
)
@click.option(
    "--base-url",
    default=None,
    help="Base URL for local or custom endpoint (e.g. http://localhost:11434/v1).",
)
def agent_cmd(
    prompt: str,
    provider: str,
    model: str,
    api_key: str | None,
    base_url: str | None,
) -> None:
    """Run Autonomous AI Agent to execute video editing tasks using MCP tools."""
    from src.modules.agent_controller import (
        AgentCallbacks,
        AgentConfig,
        AutoEditAgentController,
    )

    cfg = AgentConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )
    agent = AutoEditAgentController(cfg)

    click.echo(f"\n[AI Agent Active] Provider: {agent.config.provider} | Model: {agent.config.model}")
    click.echo(f"Prompt: {prompt}\n" + "-" * 60)

    callbacks = AgentCallbacks(
        on_thought=lambda t: click.echo(f"\n🧠 [Thought]: {t}"),
        on_tool_call=lambda name, args: click.echo(f"⚡ [Tool Call]: {name}({args})"),
        on_tool_result=lambda name, res: click.echo(f"✓ [Tool Result]: {str(res)[:160]}..."),
        on_agent_response=lambda r: click.echo(f"\n💬 [Agent Response]:\n{r}\n"),
        on_error=lambda err: click.echo(f"❌ [Error]: {err}", err=True),
    )

    res = agent.run(prompt, callbacks=callbacks)
    click.echo("-" * 60)
    click.echo(f"Completed in {res.duration_sec:.2f}s (Success: {res.success})\n")


@cli.command("gui")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Verify PySide6 initialization and main window without entering event loop.",
)
def gui_cmd(dry_run: bool) -> None:
    """Launch the TeamTrau AutoEdit PySide6 Asynchronous Desktop Studio."""
    from src.ui.app import run_studio_app

    logger.info("Starting Desktop Studio GUI (dry_run=%s)...", dry_run)
    code = run_studio_app(dry_run=dry_run)
    if code != 0:
        raise click.ClickException(f"GUI application exited with code {code}")


@cli.command("run")
@click.option(
    "--input-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    required=True,
    help="Directory containing raw source video files.",
)
@click.option(
    "--soundtrack",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help="Soundtrack audio file (.wav, .mp3, .flac). Optional only for --stage rough_cut.",
)
@click.option(
    "--stage",
    type=click.Choice(["rough_cut", "sequence", "all"], case_sensitive=False),
    default="all",
    show_default=True,
    help="rough_cut: ingest + defect pruning (no soundtrack); sequence: ingest + music edit from the "
    "footage; all: the classic one-pass analysis.",
)
@click.option(
    "--rough-cut/--no-rough-cut",
    "rough_cut",
    default=None,
    help="With --stage all: run the defect-pruning rough cut first (default: off, classic flow).",
)
@click.option(
    "--cadence",
    type=click.Choice(list(CADENCE_NAMES), case_sensitive=False),
    default=None,
    help="Cadence profile (how fast shots turn over relative to the musical bar).",
)
@click.option(
    "--genre",
    default=None,
    help="Genre preset key (e.g. travel_daily_vlog, travel_cinematic, sports_extreme, social_tiktok_fast); drives pacing.",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=Path("./output"),
    help="Directory to save timelines, metadata, and master renders.",
)
@click.option(
    "--preset",
    type=click.Choice(list(PRESETS.keys()), case_sensitive=False),
    default=DEFAULT_PRESET_NAME,
    help=f"Project canvas preset (default: {DEFAULT_PRESET_NAME}).",
)
@click.option("--fps", type=float, default=None, help="Custom framerate override.")
@click.option("--workers", type=int, default=4, help="Maximum concurrent worker threads for ingestion.")
@click.option("--no-cache", is_flag=True, default=False, help="Disable scene detection disk cache.")
@click.option("--clear-cache", is_flag=True, default=False, help="Clear existing scene detection cache.")
@click.option("--no-nvenc", is_flag=True, default=False, help="Force CPU libx265 encoding.")
@click.option(
    "--render",
    "render_now",
    is_flag=True,
    default=False,
    help="Also render the master MP4 right away (default: stop at 'Timeline ready').",
)
@click.option(
    "--export-nle",
    is_flag=True,
    default=False,
    help="Also export FCPXML / Premiere XML / OTIO / CapCut right away.",
)
@click.option("--no-render", is_flag=True, default=False, hidden=True, help="Deprecated: rendering is now opt-in.")
@click.option("--no-dead-air", is_flag=True, default=False, help="Disable dual silence/freeze dead-air detection.")
@click.option("--no-dedup", is_flag=True, default=False, help="Disable perceptual dHash scene deduplication.")
@click.option("--no-teaser", is_flag=True, default=False, help="Disable retention teaser hook at sequence start.")
@click.option("--no-reframe", is_flag=True, default=False, help="Disable AI smart 9:16 re-framing for vertical presets.")
@click.option(
    "--target-duration",
    type=float,
    default=None,
    help="Target runtime in seconds (e.g. 60.0). Extracts optimal musical section with parametric fade-out.",
)
@click.option(
    "--duration-mode",
    type=click.Choice(["fixed", "footage_driven", "full_track"], case_sensitive=False),
    default="fixed",
    help="Decoupling mode: fixed (target runtime), footage_driven (snaps to footage length), or full_track.",
)
@click.option(
    "--action-drop-threshold",
    type=float,
    default=0.70,
    help="Motion energy threshold (0.0 to 1.0) to map dynamic clips into musical drops.",
)
@click.option(
    "--enable-transcription",
    is_flag=True,
    default=False,
    help="Enable faster-whisper speech-to-text dialogue condensation.",
)
@click.option(
    "--whisper-model",
    type=click.Choice(["tiny", "base", "small", "medium", "large-v3"], case_sensitive=False),
    default="base",
    help="faster-whisper model size (default: base).",
)
@click.option(
    "--no-capcut",
    is_flag=True,
    default=False,
    help="Disable CapCut Desktop native draft export.",
)
@click.option(
    "--enable-vlm",
    is_flag=True,
    default=False,
    help="Enable semantic VLM scene tagging and understanding.",
)
@click.option(
    "--vlm-provider",
    type=click.Choice(
        ["auto", "openrouter", "ollama", "lm_studio", "openai", "heuristic"],
        case_sensitive=False,
    ),
    default="auto",
    help="VLM provider endpoint (default: auto).",
)
@click.option(
    "--vlm-model",
    type=str,
    default="openai/gpt-4o-mini",
    help="VLM model identifier (default: openai/gpt-4o-mini).",
)
@click.option(
    "--resolve",
    is_flag=True,
    default=False,
    help="Automate DaVinci Resolve project creation and render queue.",
)
@click.option(
    "--dry-run-resolve",
    is_flag=True,
    default=False,
    help="Simulate DaVinci Resolve dispatch without opening GUI.",
)
@click.option(
    "--workflow-mode",
    type=click.Choice(["standard", "build_timelapse", "unboxing_narration"], case_sensitive=False),
    default="standard",
    help="Workflow mode: standard, build_timelapse (speed-ramped), or unboxing_narration (1.0x narration).",
)
@click.option(
    "--enable-speed-ramping",
    is_flag=True,
    default=False,
    help="Enable dynamic playback speed ramping (1x - 8x) based on visual activity.",
)
@click.option(
    "--max-speed",
    type=float,
    default=8.0,
    help="Maximum speed multiplier for speed ramping (default: 8.0).",
)
@click.option(
    "--watermark-path",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to transparent PNG logo for watermark overlay.",
)
@click.option(
    "--watermark-pos",
    type=click.Choice(["top-left", "top-right", "bottom-left", "bottom-right", "center"]),
    default="top-right",
    help="Watermark position on canvas (default: top-right).",
)
@click.option(
    "--watermark-opacity",
    type=float,
    default=0.85,
    help="Watermark opacity between 0.0 and 1.0 (default: 0.85).",
)
@click.option(
    "--watermark-margin",
    type=int,
    default=40,
    help="Watermark margin in pixels from edge (default: 40).",
)
@click.option(
    "--no-audio-fix",
    is_flag=True,
    default=False,
    help="Disable BWF timecode metadata stripping and 24-bit PCM normalization.",
)
def run_pipeline(
    input_dir: Path,
    soundtrack: Path | None,
    output_dir: Path,
    preset: str,
    fps: float | None,
    workers: int,
    no_cache: bool,
    clear_cache: bool,
    no_nvenc: bool,
    render_now: bool,
    export_nle: bool,
    no_render: bool,
    no_dead_air: bool,
    no_dedup: bool,
    no_teaser: bool,
    no_reframe: bool,
    no_capcut: bool,
    target_duration: float | None,
    duration_mode: str,
    action_drop_threshold: float,
    enable_transcription: bool,
    whisper_model: str,
    enable_vlm: bool,
    vlm_provider: str,
    vlm_model: str,
    resolve: bool,
    dry_run_resolve: bool,
    workflow_mode: str = "standard",
    enable_speed_ramping: bool = False,
    max_speed: float = 8.0,
    watermark_path: Path | None = None,
    watermark_pos: str = "top-right",
    watermark_opacity: float = 0.85,
    watermark_margin: int = 40,
    no_audio_fix: bool = False,
    stage: str = "all",
    rough_cut: bool | None = None,
    cadence: str | None = None,
    genre: str | None = None,
) -> None:
    """Analyze footage and auto-edit a timeline (saved as a .tteproj project).

    Nothing is exported until you ask for it: run `deliver` on the saved project, or pass
    --render / --export-nle to do it in one go. `--stage rough_cut` only prunes defects
    (no soundtrack needed) and saves the rough cut timeline.
    """
    stage = stage.lower()
    if no_render:
        click.echo("Note: --no-render is deprecated; rendering is now opt-in via --render.")
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = PipelineConfig.from_preset(preset)
    if fps is not None and fps > 0:
        cfg.fps = fps
    if workers > 0:
        cfg.max_workers = workers
    if no_cache:
        cfg.enable_cache = False
    if no_dead_air:
        cfg.enable_dead_air_detection = False
    if no_dedup:
        cfg.enable_deduplication = False
    if no_teaser:
        cfg.enable_teaser = False
    if no_reframe:
        cfg.enable_smart_reframing = False
    if no_capcut:
        cfg.enable_capcut_export = False
    if target_duration is not None and target_duration > 0:
        cfg.target_duration = target_duration
    cfg.duration_mode = duration_mode.lower()
    if 0.0 <= action_drop_threshold <= 1.0:
        cfg.action_drop_threshold = action_drop_threshold
    if enable_transcription:
        cfg.enable_transcription = True
        cfg.whisper_model_size = whisper_model
    if enable_vlm:
        cfg.enable_vlm_tagging = True
        cfg.vlm_provider = vlm_provider.lower()
        cfg.vlm_model = vlm_model

    if workflow_mode != "standard":
        cfg.apply_workflow_mode(workflow_mode)  # type: ignore
    if enable_speed_ramping:
        cfg.enable_speed_ramping = True
    if max_speed > 0:
        cfg.max_speed_multiplier = max_speed
    if watermark_path is not None:
        cfg.watermark_enabled = True
        cfg.watermark_path = watermark_path
        cfg.watermark_position = watermark_pos  # type: ignore
        cfg.watermark_opacity = watermark_opacity
        cfg.watermark_margin = watermark_margin
    if no_audio_fix:
        cfg.enable_audio_fix = False
    try:
        apply_staged_options(cfg, genre=genre, cadence=cadence, rough_cut=rough_cut)
    except ValueError as exc:
        param = "--cadence" if cadence is not None and "cadence" in str(exc).lower() else "--genre"
        raise click.BadParameter(str(exc), param_hint=param) from exc
    if soundtrack is None and stage != "rough_cut" and cfg.workflow_mode != "rough_cut":
        raise click.UsageError("--soundtrack is required unless --stage rough_cut (or the rough cut workflow) is used.")

    if clear_cache:
        detector = IngestDetector(cfg)
        cleared = detector.clear_cache()
        click.echo(f"Cleared {cleared} scene cache records.")

    adapter = StudioBackendAdapter()
    handle = adapter.submit_job(
        input_dir=input_dir,
        soundtrack=soundtrack,
        output_dir=output_dir,
        config=cfg,
        callbacks=None,
        run_async=False,
        no_render=not render_now,
        export_nle=export_nle,
        force_cpu=no_nvenc,
        resolve=resolve,
        dry_run_resolve=dry_run_resolve,
        stage=stage,  # type: ignore[arg-type]
    )
    result = handle.result
    if not result or not result.success:
        err = result.error_message if result else "Unknown pipeline error."
        raise click.ClickException(err)

    click.echo("\n=== Pipeline Execution Summary ===")
    click.echo("Status           : SUCCESS")
    click.echo(
        f"Canvas Preset    : {cfg.preset_name} ({cfg.canvas_width}x{cfg.canvas_height} @ {cfg.fps}fps)"
    )
    click.echo(f"Duration Mode    : {cfg.duration_mode}")
    click.echo(
        f"Smart Reframing  : {'ACTIVE (9:16 Auto Tracking)' if cfg.enable_smart_reframing and (cfg.canvas_width < cfg.canvas_height) else 'OFF'}"
    )
    if cfg.enable_transcription:
        click.echo(
            f"Speech Dialogue  : {result.dialogue_sentences} sentences transcribed (Whisper: {cfg.whisper_model_size})"
        )
    if cfg.enable_vlm_tagging:
        click.echo(f"VLM Understanding: ACTIVE ({cfg.vlm_provider} / {cfg.vlm_model})")
    click.echo(f"Stage            : {result.stage}")
    if result.cleaned_segments:
        click.echo(f"Golden Takes     : {len(result.cleaned_segments)} kept by the rough cut")
    click.echo(f"Song Sections    : {result.sections_detected} classified")
    click.echo(f"Music Tempo      : {result.audio_bpm:.1f} BPM")
    click.echo(f"Assembled Cuts   : {result.clip_count}")
    click.echo(f"Sequence Length  : {result.total_duration:.2f}s")
    if result.fcpxml_path:
        click.echo(f"DaVinci FCPXML   : {result.fcpxml_path}")
    if result.premiere_xml_path:
        click.echo(f"Premiere Pro XML : {result.premiere_xml_path}")
    if result.capcut_draft_path:
        click.echo(f"CapCut Draft     : {result.capcut_draft_path}")
    if result.render_path:
        click.echo(f"Headless Master  : {result.render_path} ({result.encoder_used})")
    for bad, reason in result.failed_files:
        click.echo(f"Skipped Clip     : {bad.name} ({reason})")

    project_path = output_dir / f"{input_dir.resolve().name or 'project'}.tteproj"
    _save_cli_project(result, cfg, project_path, [input_dir], soundtrack, output_dir)
    click.echo(f"Timeline Ready   : {', '.join(result.edit_timelines)}")
    click.echo(f"Project Saved    : {project_path}")
    if not result.render_path:
        click.echo(
            f'Export when ready: main.py deliver --project "{project_path}" --format mp4,fcpxml'
        )
    click.echo("==================================\n")


def _save_cli_project(
    result, cfg: PipelineConfig, path: Path, input_dirs: list[Path], soundtrack: Path | None, output_dir: Path
) -> None:
    from src.ui.controllers.project_manager import save_result_as_project

    save_result_as_project(result, cfg, path, input_dirs, soundtrack, output_dir)


@cli.command("deliver")
@click.option(
    "--project",
    "project_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Saved .tteproj project produced by `run` or the Desktop Studio.",
)
@click.option("--variant", default=None, help="Active timeline to export (master, beat_driven, cinematic, social_teaser).")
@click.option(
    "--format",
    "formats",
    default="mp4",
    show_default=True,
    help="Comma separated: mp4,fcpxml,premiere,otio,capcut",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Deliverables folder (default: the project's output folder).",
)
@click.option("--preview", is_flag=True, default=False, help="Fast 540p proxy render instead of the master.")
@click.option("--no-nvenc", is_flag=True, default=False, help="Force CPU encoding.")
@click.option("--resolve", is_flag=True, default=False, help="Also push the FCPXML into DaVinci Resolve.")
@click.option("--preset", default=None, help="Deliver canvas + frame rate from a preset (see list-presets).")
@click.option("--canvas", default=None, help="Deliver canvas as WxH, e.g. 1080x1920 (even sizes).")
@click.option("--fps", default=None, help="Deliver frame rate: 23.976, 24, 25, 29.97, 30, 50, 59.94, 60, 119.88 or 30000/1001.")
@click.option(
    "--fit",
    type=click.Choice(["auto", "fit", "fill", "stretch"], case_sensitive=False),
    default=None,
    help="How footage fills a canvas of another aspect ratio.",
)
@click.option(
    "--color",
    type=click.Choice(["rec709", "rec2020_hlg", "rec2020_pq", "passthrough"], case_sensitive=False),
    default=None,
    help="Colour space of the master and NLE timeline.",
)
@click.option(
    "--master",
    type=click.Choice(["hevc", "h264", "prores_422", "prores_hq", "dnxhr_hqx"], case_sensitive=False),
    default=None,
    help="Master codec (ProRes / DNxHR write .mov).",
)
@click.option(
    "--subtitles",
    type=click.Choice(["none", "srt", "ass", "burn"], case_sensitive=False),
    default=None,
    help="Subtitle sidecars / burn-in (needs a transcript).",
)
@click.option("--audio-rate", type=click.Choice(["44100", "48000", "96000"]), default=None, help="Master audio sample rate.")
@click.option("--audio-channels", type=click.Choice(["1", "2"]), default=None, help="Master audio channels.")
@click.option("--variants", default=None, help="Timelines to deliver: 'active', 'all' or a comma list of keys (one subfolder each).")
@click.option("--no-relink", is_flag=True, default=False, help="Link NLE files to the render proxies instead of the original media.")
@click.option(
    "--watermark-in-nle/--no-watermark-in-nle",
    default=None,
    help="Request the watermark in NLE files (not implemented yet; recorded in the manifest).",
)
def deliver_cmd(
    project_path: Path,
    variant: str | None,
    formats: str,
    output_dir: Path | None,
    preview: bool,
    no_nvenc: bool,
    resolve: bool,
    preset: str | None,
    canvas: str | None,
    fps: str | None,
    fit: str | None,
    color: str | None,
    master: str | None,
    subtitles: str | None,
    audio_rate: str | None,
    audio_channels: str | None,
    variants: str | None,
    no_relink: bool,
    watermark_in_nle: bool | None,
) -> None:
    """Export / render a saved timeline (no re-analysis).

    Deliver settings default to the ones saved in the project (or the timeline's own canvas and
    frame rate); every option above overrides one of them for this export only.
    """
    from src.core.deliver_options import build_deliver_settings
    from src.core.types import ALL_DELIVER_FORMATS, DeliverSettings
    from src.ui.controllers.project_manager import ProjectManager, load_project_file

    data = load_project_file(project_path)
    timelines = data.get_edit_timelines()
    if not timelines:
        raise click.ClickException("Project has no edit timeline. Run analysis first.")
    key = variant or data.active_timeline_key
    if key not in timelines:
        raise click.ClickException(f"Unknown timeline '{key}'. Available: {', '.join(timelines)}")

    wanted = {f.strip().lower() for f in formats.split(",") if f.strip()}
    bad = wanted - set(ALL_DELIVER_FORMATS)
    if bad:
        raise click.BadParameter(f"Unknown format(s): {', '.join(sorted(bad))}", param_hint="--format")

    cfg = ProjectManager().deserialize_pipeline_config(data.config) if data.config else PipelineConfig()
    base = data.get_deliver_settings() or DeliverSettings.from_timeline(timelines[key], cfg)
    try:
        settings = build_deliver_settings(
            base,
            preset=preset,
            canvas=canvas,
            fps=fps,
            fit=fit,
            color=color,
            master=master,
            subtitles=subtitles,
            audio_rate=int(audio_rate) if audio_rate else None,
            audio_channels=int(audio_channels) if audio_channels else None,
            variants=variants,
            relink=False if no_relink else None,
            watermark_in_nle=watermark_in_nle,
        )
        keys = StudioBackendAdapter.resolve_variant_keys(key, settings, timelines)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc

    out_dir = output_dir or Path(data.output_dir or project_path.parent)
    adapter = StudioBackendAdapter()
    handle = adapter.submit_deliver_job(
        timelines[key],
        out_dir,
        cfg,
        formats=wanted,  # type: ignore[arg-type]
        force_cpu=no_nvenc,
        preview=preview,
        resolve=resolve,
        # One variant keeps the historic "<project>_<key>" file names; several go to <key>/ subfolders.
        base_name=f"{project_path.stem}_{key}" if len(keys) == 1 or preview else project_path.stem,
        settings=settings,
        variant_timelines=timelines,
    )
    res = handle.result
    if not res or not res.success:
        raise click.ClickException(res.error_message if res else "Deliver failed.")
    _print_deliver_summary(res, key if len(keys) == 1 else ", ".join(keys), settings, preview)


def _print_deliver_summary(res, label: str, settings, preview: bool) -> None:
    """Echo the produced files, naming the master's codec."""
    click.echo(f"Delivered '{label}' ({res.duration:.2f}s)")
    if not preview:
        click.echo(
            f"Deliver settings : {settings.canvas_width}x{settings.canvas_height} @ {settings.fps:g}fps, "
            f"colour {settings.color}, audio {settings.audio_rate}Hz/{settings.audio_channels}ch"
        )
    results = res.variant_results or {"": res}
    for variant_key, sub in results.items():
        prefix = f"[{variant_key}] " if variant_key else ""
        master_label = f"Master ({sub.master_codec})" if sub.master_codec and not preview else "Master MP4"
        for name, path in (
            (master_label, sub.render_path),
            ("FCPXML", sub.fcpxml_path),
            ("Premiere XML", sub.premiere_xml_path),
            ("OTIO", sub.otio_path),
            ("CapCut Draft", sub.capcut_draft_path),
            ("Manifest", sub.manifest_path),
        ):
            if path:
                click.echo(f"{prefix}{name:<22}: {path}")
        for sub_path in sub.subtitle_paths:
            click.echo(f"{prefix}{'Subtitles':<22}: {sub_path}")


@cli.command("curate")
@click.option(
    "--input-dir",
    "-i",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    required=True,
    help="Directory containing raw source media (supports A-roll, B-roll, Music role folders).",
)
@click.option(
    "--genre",
    "-g",
    default=None,
    help="Genre preset key (e.g. travel_daily_vlog, travel_cinematic, sports_extreme, social_tiktok_fast) driving pacing.",
)
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=Path("./output"),
    show_default=True,
    help="Directory to save curation summary and exports.",
)
@click.option(
    "--top",
    type=int,
    default=12,
    show_default=True,
    help="Number of top takes to include in summary.",
)
@click.option(
    "--export/--no-export",
    "export",
    default=False,
    help="Export NLE projects (FCPXML, Premiere XML, CapCut draft) for curated selects.",
)
def curate_cmd(
    input_dir: Path,
    genre: str | None,
    output_dir: Path,
    top: int,
    export: bool,
) -> None:
    """Execute rough-cut curation and defect pruning on source footage."""
    try:
        adapter = StudioBackendAdapter()
        session, summary = curation.run_curation(
            adapter=adapter,
            input_dir=Path(input_dir),
            genre=genre,
            output_dir=Path(output_dir),
            top_n=top,
        )
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        summary_file = out_path / "curation_summary.json"
        summary_file.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        click.echo(json.dumps(summary, indent=2, ensure_ascii=False))

        if export:
            export_res = curation.export_session(
                adapter=adapter,
                session=session,
                output_dir=Path(output_dir),
            )
            if isinstance(export_res, dict):
                click.echo("\nExported NLE Project Files:")
                for key, label in (
                    ("fcpxml_path", "FCPXML"),
                    ("premiere_xml_path", "Premiere XML"),
                    ("otio_path", "OTIO"),
                    ("capcut_draft_path", "CapCut Draft"),
                    ("preview_mp4", "Preview MP4"),
                ):
                    val = export_res.get(key)
                    if val:
                        click.echo(f"  * {label:<16}: {val}")
    except (ValueError, FileNotFoundError) as exc:
        raise click.BadParameter(str(exc)) from exc


@cli.command("serve")
@click.option("--port", default=8765, type=int, help="Port to listen on (default: 8765).")
@click.option(
    "--token-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to token file (default: <data_dir>/engine_token).",
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Engine data directory for token, settings, and cache.",
)
@click.option(
    "--tools-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Directory containing bundled ffmpeg/ffprobe binaries to prepend to PATH.",
)
def serve_cmd(
    port: int,
    token_file: Path | None,
    data_dir: Path | None,
    tools_dir: Path | None,
) -> None:
    """Run the TeamTrau Engine API loopback service."""
    if tools_dir is not None:
        prepend_tool_dir(Path(tools_dir), os.environ)

    resolved_data = resolve_data_dir(
        str(data_dir) if data_dir is not None else None,
        os.environ,
        detect_os(),
        Path.home(),
    )
    cache_path = resolved_data / "cache"
    cache_path.mkdir(parents=True, exist_ok=True)
    os.environ["TEAMTRAU_CACHE_DIR"] = str(cache_path)

    if token_file is not None:
        token_path = Path(token_file)
        if not token_path.is_file():
            raise click.BadParameter(f"Token file not found: {token_path}")
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise click.BadParameter(f"Token file is empty: {token_path}")
    else:
        token_path = resolved_data / "engine_token"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        if os.name != "nt":
            os.chmod(token_path, 0o600)

    click.echo(f"Engine token file: {token_path}")
    click.echo(f"Starting TeamTrau Engine API on http://127.0.0.1:{port}...")

    ai_settings = AiSettingsStore(resolved_data / "ai_settings.json")
    sessions = SessionStore()
    app = create_app(token=token, sessions=sessions, ai_settings=ai_settings)
    server_cfg = build_server_config(app, port=port)
    server = uvicorn.Server(server_cfg)
    server.run()


if __name__ == "__main__":
    cli()

"""Production-Grade Automated Video Editing Orchestrator CLI."""

import logging
import sys
from pathlib import Path

import click

from src.core.config import (
    DEFAULT_PRESET_NAME,
    PRESETS,
    PipelineConfig,
    check_cuda_available,
    check_nvenc_available,
)
from src.core.service import StudioBackendAdapter
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
    required=True,
    help="Soundtrack audio file (.wav, .mp3, .flac).",
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
@click.option("--no-render", is_flag=True, default=False, help="Skip standalone MP4 render.")
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
    soundtrack: Path,
    output_dir: Path,
    preset: str,
    fps: float | None,
    workers: int,
    no_cache: bool,
    clear_cache: bool,
    no_nvenc: bool,
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
) -> None:
    """Execute the end-to-end automated editing and delivery pipeline."""
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
        no_render=no_render,
        force_cpu=no_nvenc,
        resolve=resolve,
        dry_run_resolve=dry_run_resolve,
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
    click.echo("==================================\n")


if __name__ == "__main__":
    cli()

"""Comprehensive unit and integration tests for Model Context Protocol (MCP) server.

Verifies tool registration, tool execution, resource reading, prompt generation,
and configuration exports for Antigravity, Claude Desktop, Cursor, and SSE.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.modules.mcp_server import (
    check_system_environment,
    generate_mcp_config,
    list_editing_presets,
    list_workflow_presets,
    load_project,
    mcp_server,
    prompt_cinematic_edit,
    prompt_high_retention_video,
    resource_presets,
    resource_system_environment,
    resource_workflows,
    save_project,
)


def test_mcp_tools_and_resources_registered():
    """Verify that all core AutoEdit tools, resources, and prompts are registered."""
    tools = mcp_server._tool_manager.list_tools()
    tool_names = [t.name for t in tools]

    assert len(tools) >= 20
    assert "check_system_environment" in tool_names
    assert "list_editing_presets" in tool_names
    assert "list_workflow_presets" in tool_names
    assert "probe_media_file" in tool_names
    assert "detect_scenes" in tool_names
    assert "batch_detect_scenes" in tool_names
    assert "analyze_soundtrack" in tool_names
    assert "normalize_audio_file" in tool_names
    assert "transcribe_speech" in tool_names
    assert "score_visual_scenes" in tool_names
    assert "reframe_video" in tool_names
    assert "evaluate_cut_continuity" in tool_names
    assert "generate_kinetic_subtitles" in tool_names
    assert "export_timeline" in tool_names
    assert "render_master" in tool_names
    assert "validate_timeline_qa" in tool_names
    assert "execute_pipeline_job" in tool_names
    assert "get_pipeline_job_status" in tool_names
    assert "cancel_pipeline_job" in tool_names
    assert "save_project" in tool_names
    assert "load_project" in tool_names
    assert "tag_scene_with_vlm" in tool_names
    assert "generate_mcp_config" in tool_names

    resources = mcp_server._resource_manager.list_resources()
    resource_uris = [r.uri for r in resources]
    assert "system://environment" in resource_uris
    assert "config://presets" in resource_uris
    assert "config://workflows" in resource_uris

    prompts = mcp_server._prompt_manager.list_prompts()
    prompt_names = [p.name for p in prompts]
    assert "autoedit_high_retention" in prompt_names
    assert "autoedit_cinematic" in prompt_names


def test_tool_check_system_environment():
    """Verify system environment diagnostic tool returns structured metrics."""
    res = check_system_environment()
    assert isinstance(res, dict)
    assert "python_version" in res
    assert "python_executable" in res
    assert "cuda_available" in res
    assert "nvenc_available" in res
    assert "status" in res


def test_tool_list_editing_and_workflow_presets():
    """Verify presets tools return standard resolutions and workflows."""
    presets_res = list_editing_presets()
    assert presets_res["default_preset"] == "1080p30"
    assert "1080p30" in presets_res["presets"]
    assert "social_vertical" in presets_res["presets"]
    assert presets_res["presets"]["social_vertical"]["width"] == 1080
    assert presets_res["presets"]["social_vertical"]["height"] == 1920

    workflows_res = list_workflow_presets()
    modes = workflows_res["workflow_modes"]
    assert "standard" in modes
    assert "build_timelapse" in modes
    assert "unboxing_narration" in modes


def test_tool_generate_mcp_config():
    """Verify configuration generation for Antigravity, Claude, and SSE."""
    # Antigravity config
    ag_cfg = generate_mcp_config(client_name="antigravity", transport="stdio")
    assert ag_cfg["client"] == "antigravity"
    assert "mcpServers" in ag_cfg["config"]
    assert "teamtrau-autoedit" in ag_cfg["config"]["mcpServers"]
    assert Path(ag_cfg["config"]["mcpServers"]["teamtrau-autoedit"]["command"]).name.startswith("python")

    # Claude Desktop config
    claude_cfg = generate_mcp_config(client_name="claude_desktop", transport="stdio")
    assert claude_cfg["client"] == "claude_desktop"
    assert "mcpServers" in claude_cfg["config"]

    # SSE config
    sse_cfg = generate_mcp_config(client_name="antigravity", transport="sse", host="192.168.1.50", port=9000)
    assert sse_cfg["config"]["mcpServers"]["teamtrau-autoedit"]["url"] == "http://192.168.1.50:9000/sse"

    # All clients export
    all_cfg = generate_mcp_config(client_name="all")
    assert "clients" in all_cfg
    assert "antigravity" in all_cfg["clients"]
    assert "claude_desktop" in all_cfg["clients"]
    assert "cursor" in all_cfg["clients"]


def test_tool_project_save_and_load(tmp_path: Path):
    """Verify save_project and load_project MCP tools."""
    proj_file = tmp_path / "test_mcp_project.tteproj"

    save_res = save_project(
        project_path=str(proj_file),
        input_dir="/footage/sample",
        soundtrack_path="/music/beat.mp3",
        preset="social_vertical",
        project_name="MCP Test Viral Reel",
    )
    assert save_res["status"] == "saved"
    assert proj_file.is_file()

    load_res = load_project(str(proj_file))
    assert load_res["status"] == "loaded"
    assert load_res["project_data"]["project_name"] == "MCP Test Viral Reel"
    assert load_res["project_data"]["preset_name"] == "social_vertical"
    assert load_res["project_data"]["soundtrack_path"] == "/music/beat.mp3"


def test_mcp_resources_and_prompts():
    """Verify MCP resource getters and prompt templates."""
    env_str = resource_system_environment()
    env_data = json.loads(env_str)
    assert "python_version" in env_data

    presets_str = resource_presets()
    presets_data = json.loads(presets_str)
    assert "presets" in presets_data

    workflows_str = resource_workflows()
    workflows_data = json.loads(workflows_str)
    assert "workflow_modes" in workflows_data

    p_hr = prompt_high_retention_video()
    assert "Lead Video Editor" in p_hr
    assert "retention teaser hook" in p_hr

    p_cine = prompt_cinematic_edit()
    assert "Bhattacharyya luminance distance" in p_cine
    assert "Ken Burns" in p_cine

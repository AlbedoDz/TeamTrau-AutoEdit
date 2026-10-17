"""Unit and integration tests for AutoEditAgentController and ReAct loop."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from src.core.config import PipelineConfig
from src.modules.agent_controller import (
    AgentCallbacks,
    AgentConfig,
    AutoEditAgentController,
)


def test_agent_controller_initialization():
    """Verify provider resolution and default configurations."""
    # Heuristic default
    agent_h = AutoEditAgentController(AgentConfig(provider="heuristic"))
    assert agent_h.config.provider == "heuristic"
    assert agent_h.client is None

    # OpenRouter
    agent_or = AutoEditAgentController(
        AgentConfig(
            provider="openrouter",
            api_key="sk-or-test-key",
            model="deepseek/deepseek-chat",
        )
    )
    assert agent_or.config.provider == "openrouter"
    assert agent_or.config.base_url == "https://openrouter.ai/api/v1"
    assert agent_or.client is not None

    # DeepSeek Cloud
    agent_ds = AutoEditAgentController(
        AgentConfig(
            provider="deepseek",
            api_key="sk-ds-test-key",
            model="deepseek-chat",
        )
    )
    assert agent_ds.config.provider == "deepseek"
    assert agent_ds.config.base_url == "https://api.deepseek.com/v1"

    # From PipelineConfig
    pipe_cfg = PipelineConfig(
        agent_provider="ollama",
        agent_base_url="http://192.168.1.10:11434/v1",
        agent_model="qwen2.5:7b",
    )
    agent_pipe = AutoEditAgentController(pipe_cfg)
    assert agent_pipe.config.provider == "custom"
    assert agent_pipe.config.base_url == "http://192.168.1.10:11434/v1"


def test_agent_get_openai_tools_schema():
    """Verify that MCP tools are correctly converted to OpenAI tool calling format."""
    agent = AutoEditAgentController(AgentConfig(provider="heuristic"))
    tools = agent.get_openai_tools_schema()

    assert len(tools) >= 20
    assert all(t["type"] == "function" for t in tools)

    names = [t["function"]["name"] for t in tools]
    assert "check_system_environment" in names
    assert "list_editing_presets" in names
    assert "export_timeline" in names
    assert "render_master" in names

    env_tool = next(t for t in tools if t["function"]["name"] == "check_system_environment")
    assert "description" in env_tool["function"]
    assert "parameters" in env_tool["function"]


def test_agent_execute_tool():
    """Verify direct tool dispatch by name."""
    agent = AutoEditAgentController(AgentConfig(provider="heuristic"))

    res = agent.execute_tool("check_system_environment", {})
    assert isinstance(res, dict)
    assert "cuda_available" in res

    res_presets = agent.execute_tool("list_editing_presets", {})
    assert "default_preset" in res_presets

    res_missing = agent.execute_tool("non_existent_tool_xyz", {})
    assert res_missing["status"] == "not_found"


def test_agent_heuristic_run():
    """Verify heuristic rule-based planner execution and callbacks."""
    agent = AutoEditAgentController(AgentConfig(provider="heuristic"))

    thoughts = []
    tool_calls = []
    responses = []

    callbacks = AgentCallbacks(
        on_thought=lambda t: thoughts.append(t),
        on_tool_call=lambda name, args: tool_calls.append(name),
        on_agent_response=lambda r: responses.append(r),
    )

    res = agent.run("Kiểm tra môi trường phần cứng và danh sách preset khung hình", callbacks=callbacks)

    assert res.success is True
    assert len(thoughts) >= 1
    assert "check_system_environment" in tool_calls
    assert "list_editing_presets" in tool_calls
    assert len(responses) == 1
    assert "Heuristic Agent đã xử lý" in responses[0]


def test_agent_mocked_llm_react_loop():
    """Verify multi-turn tool calling and response handling with mocked OpenAI client."""
    agent = AutoEditAgentController(
        AgentConfig(
            provider="openrouter",
            api_key="dummy-key",
            model="deepseek/deepseek-chat",
        )
    )

    # Mock tool call in Turn 1
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_abc123"
    mock_tool_call.type = "function"
    mock_tool_call.function.name = "check_system_environment"
    mock_tool_call.function.arguments = "{}"

    mock_msg_1 = MagicMock()
    mock_msg_1.content = "I will inspect the system environment first."
    mock_msg_1.tool_calls = [mock_tool_call]
    mock_choice_1 = MagicMock(message=mock_msg_1)
    mock_resp_1 = MagicMock(choices=[mock_choice_1])

    # Mock final answer in Turn 2
    mock_msg_2 = MagicMock()
    mock_msg_2.content = "System has NVIDIA RTX GPU and NVENC hardware encoder ready."
    mock_msg_2.tool_calls = []
    mock_choice_2 = MagicMock(message=mock_msg_2)
    mock_resp_2 = MagicMock(choices=[mock_choice_2])

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [mock_resp_1, mock_resp_2]
    agent.client = mock_client

    recorded_tools = []
    recorded_thoughts = []
    callbacks = AgentCallbacks(
        on_thought=lambda t: recorded_thoughts.append(t),
        on_tool_call=lambda name, args: recorded_tools.append(name),
    )

    result = agent.run("Check system status and report back", callbacks=callbacks)

    assert result.success is True
    assert result.response == "System has NVIDIA RTX GPU and NVENC hardware encoder ready."
    assert "check_system_environment" in recorded_tools
    assert len(result.steps) == 2
    assert result.steps[0].tool_calls[0]["name"] == "check_system_environment"


def test_agent_cancellation():
    """Verify that setting the cancel event stops the agent reasoning loop."""
    agent = AutoEditAgentController(
        AgentConfig(
            provider="openrouter",
            api_key="dummy-key",
            model="deepseek/deepseek-chat",
        )
    )
    cancel_event = threading.Event()
    cancel_event.set()  # Cancelled before first step

    result = agent.run("Please edit video", cancel_event=cancel_event)
    assert result.success is False
    assert "cancelled" in result.error_message.lower()

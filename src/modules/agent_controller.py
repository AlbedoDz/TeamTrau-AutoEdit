"""Universal AI Agent Controller linking MCP with LLM/VLM providers.

Supports OpenRouter, DeepSeek v4/v3/R1, Claude 3.5 Sonnet, GPT-4o,
local Ollama, LM Studio, or custom OpenAI-compatible endpoints.
Allows autonomous AI Agents to reason, select tools, and control all
functions across the AutoEdit video editing ecosystem.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from src.core.config import PipelineConfig
from src.modules.mcp_server import mcp_server

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Configuration parameters for the AI Agent Controller."""

    provider: str = "auto"  # 'auto', 'openrouter', 'deepseek', 'openai', 'ollama', 'lm_studio', 'custom'
    base_url: str | None = None
    api_key: str | None = None
    model: str = "deepseek/deepseek-chat"
    temperature: float = 0.2
    max_steps: int = 10
    timeout_sec: float = 30.0
    auto_approve: bool = True


@dataclass
class AgentStep:
    """Individual execution step in an agent reasoning loop."""

    step_number: int
    thought: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentResult:
    """Final deliverable of an agent task execution."""

    success: bool
    response: str
    steps: list[AgentStep] = field(default_factory=list)
    error_message: str | None = None
    provider_used: str = "heuristic"
    model_used: str = "heuristic"
    duration_sec: float = 0.0


@dataclass
class AgentCallbacks:
    """Callback hooks for observing agent execution lifecycle in real-time."""

    on_thought: Callable[[str], None] | None = None
    on_tool_call: Callable[[str, dict[str, Any]], None] | None = None
    on_tool_result: Callable[[str, Any], None] | None = None
    on_agent_response: Callable[[str], None] | None = None
    on_error: Callable[[str], None] | None = None


DEFAULT_SYSTEM_PROMPT = """You are the Principal AI Video Editor and Autonomous Pipeline Orchestrator for TeamTrau AutoEdit.
You have direct Model Context Protocol (MCP) tool access to control the entire video editing studio:
- Environment & Hardware: check_system_environment, list_editing_presets, list_workflow_presets
- Media Ingestion: probe_media_file, detect_scenes, batch_detect_scenes
- Audio & Speech: analyze_soundtrack, normalize_audio_file, transcribe_speech
- Visual & Quality: score_visual_scenes, reframe_video (9:16 vertical/blur backdrop), evaluate_cut_continuity
- Subtitles: generate_kinetic_subtitles (ASS karaoke highlight)
- Timeline & Handoffs: export_timeline (DaVinci Resolve FCPXML, Premiere XML, CapCut draft), render_master (NVENC HEVC)
- Quality Assurance: validate_timeline_qa (clamp flash frames, audio pop crossfade)
- Project & Jobs: execute_pipeline_job, get_pipeline_job_status, cancel_pipeline_job, save_project, load_project

INSTRUCTIONS:
1. When asked to edit, diagnose, or process media, plan step-by-step and call appropriate tools.
2. For short-form social media (TikTok/Reels/Shorts), prefer 'social_vertical' preset, 'blur_backdrop' or 'auto_hybrid' layout, and 'kinetic_ass' subtitles.
3. For cinematic edits, evaluate visual continuity and keep aspect ratio.
4. Reply concisely in Vietnamese or English based on the user's input, summarizing decisions clearly.
"""


class AutoEditAgentController:
    """Controller orchestrating AI Agent interaction with MCP tools."""

    KNOWN_PROVIDERS: dict[str, str] = {
        "openrouter": "https://openrouter.ai/api/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "openai": "https://api.openai.com/v1",
        "ollama": "http://localhost:11434/v1",
        "lm_studio": "http://localhost:1234/v1",
    }

    def __init__(self, config: AgentConfig | PipelineConfig | None = None) -> None:
        if isinstance(config, PipelineConfig):
            self.config = AgentConfig(
                provider=config.agent_provider,
                base_url=config.agent_base_url,
                api_key=config.agent_api_key,
                model=config.agent_model,
                temperature=config.agent_temperature,
                max_steps=config.agent_max_steps,
            )
        elif isinstance(config, AgentConfig):
            self.config = config
        else:
            self.config = AgentConfig()

        self.client: Any = None
        self._resolve_provider_and_client()

    def _resolve_provider_and_client(self) -> None:
        """Resolve API endpoint credentials and instantiate OpenAI REST client."""
        provider = self.config.provider.lower()
        base_url = self.config.base_url
        api_key = self.config.api_key

        if base_url:
            provider = "custom"
            api_key = api_key or os.environ.get("OPENAI_API_KEY", "dummy-key")
        elif provider == "auto":
            if os.environ.get("OPENROUTER_API_KEY"):
                provider = "openrouter"
                base_url = self.KNOWN_PROVIDERS["openrouter"]
                api_key = os.environ["OPENROUTER_API_KEY"]
            elif os.environ.get("DEEPSEEK_API_KEY"):
                provider = "deepseek"
                base_url = self.KNOWN_PROVIDERS["deepseek"]
                api_key = os.environ["DEEPSEEK_API_KEY"]
            elif os.environ.get("OPENAI_API_KEY"):
                provider = "openai"
                base_url = self.KNOWN_PROVIDERS["openai"]
                api_key = os.environ["OPENAI_API_KEY"]
            elif os.environ.get("OLLAMA_HOST"):
                provider = "ollama"
                base_url = f"{os.environ['OLLAMA_HOST']}/v1"
                api_key = "ollama"
            else:
                provider = "heuristic"
                base_url = None
                api_key = None
        elif provider in self.KNOWN_PROVIDERS:
            base_url = self.KNOWN_PROVIDERS[provider]
            if provider == "openrouter":
                api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
            elif provider == "deepseek":
                api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
            elif provider == "openai":
                api_key = api_key or os.environ.get("OPENAI_API_KEY")
            elif provider in ("ollama", "lm_studio"):
                api_key = api_key or "local"
            else:
                api_key = api_key or "dummy-key"
        else:
            provider = "heuristic"
            base_url = None
            api_key = None

        self.config.provider = provider
        self.config.base_url = base_url
        self.config.api_key = api_key

        if provider != "heuristic" and api_key and base_url:
            try:
                from openai import OpenAI

                self.client = OpenAI(
                    base_url=base_url,
                    api_key=api_key,
                    timeout=self.config.timeout_sec,
                )
                logger.info(
                    "AutoEditAgentController initialized via %s (%s) with model %s",
                    provider,
                    base_url,
                    self.config.model,
                )
            except Exception as exc:
                logger.warning("Failed to initialize OpenAI client for %s: %s", provider, exc)
                self.client = None

    def get_openai_tools_schema(self) -> list[dict[str, Any]]:
        """Extract all registered MCP tools and format them into OpenAI function calling schema."""
        tools: list[dict[str, Any]] = []
        for t in mcp_server._tool_manager.list_tools():
            tools.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.parameters or {"type": "object", "properties": {}},
                },
            })
        return tools

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Look up tool by name in MCP registry and execute with parameters."""
        for t in mcp_server._tool_manager.list_tools():
            if t.name == name:
                try:
                    res = t.fn(**arguments)
                    if isinstance(res, dict):
                        return res
                    return {"result": res}
                except Exception as exc:
                    logger.error("Error executing tool %s: %s", name, exc, exc_info=True)
                    return {"error": str(exc), "status": "failed"}

        return {"error": f"Tool '{name}' not found in MCP registry", "status": "not_found"}

    def test_connection(self) -> tuple[bool, float, str]:
        """Test round-trip latency to the configured agent endpoint."""
        import urllib.request

        if not self.config.base_url:
            return False, 0.0, "No base URL configured (Heuristic mode active)"

        target = self.config.base_url.rstrip("/")
        probe_url = f"{target}/models"
        req = urllib.request.Request(probe_url)
        if self.config.api_key:
            req.add_header("Authorization", f"Bearer {self.config.api_key}")

        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                latency = (time.perf_counter() - t0) * 1000.0
                return True, latency, f"Connected to {self.config.provider} ({resp.status})"
        except Exception:
            try:
                t0 = time.perf_counter()
                with urllib.request.urlopen(target, timeout=5.0) as resp:
                    latency = (time.perf_counter() - t0) * 1000.0
                    return True, latency, f"Connected to {self.config.provider} root ({resp.status})"
            except Exception as exc:
                latency = (time.perf_counter() - t0) * 1000.0
                return False, latency, str(exc)

    def run(
        self,
        prompt: str,
        callbacks: AgentCallbacks | None = None,
        cancel_event: threading.Event | None = None,
        system_prompt: str | None = None,
    ) -> AgentResult:
        """Execute autonomous agent reasoning and tool-calling loop.

        Args:
            prompt: User goal or instruction (e.g. 'Dựng video dọc 9:16 từ folder footage').
            callbacks: Lifecycle observer hooks for thoughts, tool calls, and results.
            cancel_event: Optional cancellation token.
            system_prompt: Optional custom system directive.

        Returns:
            AgentResult with final response, step trajectory, and execution duration.
        """
        start_time = time.perf_counter()
        cb = callbacks or AgentCallbacks()

        # Fallback to intelligent heuristic planner if no client is available
        if self.client is None or self.config.provider == "heuristic":
            return self._heuristic_plan_and_execute(prompt, cb, start_time)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        tools = self.get_openai_tools_schema()
        steps: list[AgentStep] = []

        for step_idx in range(1, self.config.max_steps + 1):
            if cancel_event and cancel_event.is_set():
                msg = "Agent execution cancelled by user."
                if cb.on_error:
                    cb.on_error(msg)
                return AgentResult(
                    success=False,
                    response=msg,
                    steps=steps,
                    error_message=msg,
                    provider_used=self.config.provider,
                    model_used=self.config.model,
                    duration_sec=time.perf_counter() - start_time,
                )

            try:
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    tools=tools if tools else None,
                    temperature=self.config.temperature,
                )
            except Exception as exc:
                logger.warning(
                    "LLM call failed (%s). Falling back to heuristic planner: %s",
                    self.config.provider,
                    exc,
                )
                if cb.on_thought:
                    cb.on_thought(f"Network call to {self.config.provider} failed: {exc}. Switching to local heuristic.")
                return self._heuristic_plan_and_execute(prompt, cb, start_time)

            choice = response.choices[0]
            msg = choice.message
            content = msg.content or ""
            tool_calls = msg.tool_calls or []

            current_step = AgentStep(step_number=step_idx, thought=content)
            if content and cb.on_thought:
                cb.on_thought(content)

            # If no tool calls, agent provided final answer
            if not tool_calls:
                steps.append(current_step)
                if cb.on_agent_response:
                    cb.on_agent_response(content)
                return AgentResult(
                    success=True,
                    response=content,
                    steps=steps,
                    provider_used=self.config.provider,
                    model_used=self.config.model,
                    duration_sec=time.perf_counter() - start_time,
                )

            # Append assistant message with tool calls to conversation history
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })

            # Execute each tool call
            for tc in tool_calls:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except Exception:
                    tool_args = {}

                current_step.tool_calls.append({"name": tool_name, "args": tool_args})
                if cb.on_tool_call:
                    cb.on_tool_call(tool_name, tool_args)

                # Tool execution
                tool_output = self.execute_tool(tool_name, tool_args)
                current_step.tool_results.append({"name": tool_name, "result": tool_output})
                if cb.on_tool_result:
                    cb.on_tool_result(tool_name, tool_output)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_output),
                })

            steps.append(current_step)

        # Max steps reached without final response
        summary = f"Agent completed maximum {self.config.max_steps} steps of autonomous actions."
        if cb.on_agent_response:
            cb.on_agent_response(summary)
        return AgentResult(
            success=True,
            response=summary,
            steps=steps,
            provider_used=self.config.provider,
            model_used=self.config.model,
            duration_sec=time.perf_counter() - start_time,
        )

    def _heuristic_plan_and_execute(
        self,
        prompt: str,
        cb: AgentCallbacks,
        start_time: float,
    ) -> AgentResult:
        """Intelligent rule-based planner used when offline or without active LLM API credentials."""
        steps: list[AgentStep] = []
        p_lower = prompt.lower()
        thought = "Chế độ Heuristic Planner nội bộ: Đang phân tích yêu cầu người dùng và kích hoạt các MCP tool tương ứng."
        if cb.on_thought:
            cb.on_thought(thought)

        step = AgentStep(step_number=1, thought=thought)

        # 1. Environment check intent
        if any(w in p_lower for w in ("kiểm tra", "môi trường", "check-env", "hardware", "phần cứng", "system")):
            tool_name = "check_system_environment"
            if cb.on_tool_call:
                cb.on_tool_call(tool_name, {})
            res = self.execute_tool(tool_name, {})
            step.tool_calls.append({"name": tool_name, "args": {}})
            step.tool_results.append({"name": tool_name, "result": res})
            if cb.on_tool_result:
                cb.on_tool_result(tool_name, res)

        # 2. Presets query intent
        if any(w in p_lower for w in ("preset", "độ phân giải", "tỉ lệ", "khung hình")):
            tool_name = "list_editing_presets"
            if cb.on_tool_call:
                cb.on_tool_call(tool_name, {})
            res = self.execute_tool(tool_name, {})
            step.tool_calls.append({"name": tool_name, "args": {}})
            step.tool_results.append({"name": tool_name, "result": res})
            if cb.on_tool_result:
                cb.on_tool_result(tool_name, res)

        # 3. MCP Config request
        if any(w in p_lower for w in ("mcp config", "cấu hình mcp", "claude", "antigravity", "cursor")):
            tool_name = "generate_mcp_config"
            args = {"client_name": "all"}
            if cb.on_tool_call:
                cb.on_tool_call(tool_name, args)
            res = self.execute_tool(tool_name, args)
            step.tool_calls.append({"name": tool_name, "args": args})
            step.tool_results.append({"name": tool_name, "result": res})
            if cb.on_tool_result:
                cb.on_tool_result(tool_name, res)

        steps.append(step)

        final_msg = (
            f"Heuristic Agent đã xử lý yêu cầu: '{prompt}'.\n"
            f"Đã thực thi thành công {len(step.tool_calls)} tác vụ hệ thống.\n"
            f"Để có năng lực tự lập kế hoạch nâng cao (DeepSeek v4, Claude 3.5 Sonnet, GPT-4o), "
            f"vui lòng cấu hình API Key trong mục Preferences > AI Agent & MCP Hub."
        )
        if cb.on_agent_response:
            cb.on_agent_response(final_msg)

        return AgentResult(
            success=True,
            response=final_msg,
            steps=steps,
            provider_used="heuristic",
            model_used="rule-based",
            duration_sec=time.perf_counter() - start_time,
        )

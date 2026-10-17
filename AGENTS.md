# AGENTS.md - Agent Instructions & Collaboration Protocols

This document is the **Single Source of Truth** for any AI Coding Agent (Gemini, Claude, Codex, etc.) entering this project. You must read this file first and adhere strictly to its rules.

---

## 🚀 1. Bootstrapping / Onboarding Sequence (At Session Start)

Upon initialization or when switching to this project, you **MUST** execute the following steps immediately:
1.  **Read `task.md`** at the project root to understand the task checklist, active items (`[/]`), and completed items (`[x]`).
2.  **Check Machine Memory** by running:
    ```bash
    python .agent/scripts/memory_tool.py status
    ```
3.  **Search Lessons Learned** related to the current task:
    ```bash
    python .agent/scripts/memory_tool.py search <keyword>
    ```
    *Note: Always query keywords like `windows`, `batch`, `process`, `git`, or `linter` if your task involves shell script wrappers, background daemons, git file tracking, or configuration exclusions to avoid recurring OS-specific setup pitfalls.*

---

## 💾 2. Session Handover & Memory Saving (At Session End)

Before stopping work or handing over to another agent, you **MUST** save the context to prevent "context amnesia":
1.  **Update `task.md`:** Check off completed tasks and mark in-progress tasks.
2.  **Update Goal Progress:**
    ```bash
    python .agent/scripts/memory_tool.py update-goal --status "in_progress" --progress <percentage_complete>
    ```
3.  **Sync modified files mapping:**
    ```bash
    python .agent/scripts/memory_tool.py sync-git
    ```
4.  **Record Lessons Learned** if you resolved a complex issue or discovered a new constraint:
    ```bash
    python .agent/scripts/memory_tool.py add-lesson "Topic Title" "Detailed lesson description and fix steps" --tags "tag1,tag2"
    ```
5.  **Compress Memory** to minimize token load for subsequent sessions:
    ```bash
    python .agent/scripts/memory_tool.py compress
    ```


---

## 👥 2.1. Multi-Agent Delegation Library (18 Specialized Subagents)

When splitting or parallelizing tasks using `invoke_subagent`, use the matching prompts inside `.agent/subagents/`:
- **coder**: Write PEP8 compliant Python code (async, Django, FastAPI) using `python-pro` and `async-python-patterns`.
- **linter**: Clean styling and fix code quality warnings using `lint-and-validate`.
- **tester**: Run automated tests using `pytest`.
- **planner**: Brainstorm checklist items and system design structures (`brainstorming`, `concise-planning`, `kaizen`).
- **designer**: Design graphical interfaces, wireframes, and layouts (`canvas-design`, `algorithmic-art`).
- **frontend**: Implement responsive frontend templates and layouts (`frontend-design`, `tailwind-patterns`, `react-patterns`, `nextjs-best-practices`).
- **backend**: Develop API architectures, database models, and validation rules (`python-pro`, `andrej-karpathy`).
- **writer**: Draft user guides, marketing copy, and SEO articles (`content-creator`, `copy-editing`, `programmatic-seo`, `email-sequence`).
- **debugger**: Troubleshoot defects and trace call stacks systematically (`systematic-debugging`).
- **auditor**: Check code safety, compliance, and design patterns (`vibe-code-auditor`, `kaizen`).
- **refactorer**: Simplify code smells and reduce line counts (`simplify-code`, `andrej-karpathy`).
- **git_manager**: Commit and push releases using conventional git structures (`git-pushing`).
- **coordinator**: Track subagent write collisions, sync checklists (`subagent-orchestrator`, `project-memory`).
- **llm_app_developer**: Build prompt pipelines, manage prompt caching, RAG retrieval (`llm-app-patterns`, `prompt-caching`, `rag-implementation`, `context-window-management`, `langfuse`).
- **agent_architect**: Build stateful graphs, MCP tools, and evaluate benchmarks (`ai-agents-architect`, `langgraph`, `mcp-builder`, `prompt-engineering`, `rag-engineer`, `agent-evaluation`).
- **security_engineer**: Run vulnerability scans, penetration testing, threat auditing (`security-auditor`, `vulnerability-scanner`, `cloud-penetration-testing`, `ethical-hacking-methodology`, `linux-privilege-escalation`, `top-web-vulnerabilities`, `burp-suite-testing`).
- **security_developer**: Write secure code patterns, configure API headers, rate-limiting (`frontend-security-coder`, `backend-security-coder`, `api-security-best-practices`, `auth-implementation-patterns`, `cc-skill-security-review`, `pci-compliance`).
- **marketing_growth**: Setup A/B tests, GA4/PostHog events, crawler SEO audits (`ab-test-setup`, `analytics-tracking`, `seo-audit`, `content-creator`).

---

## 🛠️ 3. Coding Philosophy & System Design

Adhere strictly to these two core engineering frameworks:

### 💡 Andrej Karpathy's Philosophy (First-Principles & Vibe Coding)
-   **Build from Scratch First:** Write core algorithms in pure Python before using heavy external packages. This builds 100x better debugging intuition.
-   **Modern Type Hints:** Always use explicit type annotations (e.g., `list[str]`, `dict[str, Any]`, `str | None`).
-   **Simplicity is King:** Write clean, legible code and math. Avoid over-complicating architectures.

### 🔄 Kaizen Philosophy (Continuous Improvement & Poka-Yoke)
-   **Poka-Yoke (Error-proofing by Design):**
    -   Use strict type constraints (like `Literal` or `Enum` instead of free strings) to catch bugs at compile/type-check time.
    -   Validate data strictly at system boundaries.
-   **Just-In-Time & YAGNI:** Build only what is needed *now*. Do not write "speculative" code.
-   **Incremental Iterations:**
    1.  *Step 1 (Make it work):* Solve the immediate problem.
    2.  *Step 2 (Make it clear):* Refactor for readability.
    3.  *Step 3 (Make it robust):* Add validations and performance checks.
-   **Verify First:** Run tests (`pytest`) after every small edit to prevent regression.

---

## 🎯 4. Useful Commands for Portable Environment (Windows & Unix)

All tasks should be executed inside the isolated virtual environment using `uv run`:
-   **Run Linter & Formatter:** `uv run ruff check` and `uv run ruff format`
-   **Run Unit Tests:** `uv run pytest`
-   **Clean Workspace Caches:** Run `clean.bat` at the root folder.
-   **Upgrade Dependencies:** Run `update.bat` at the root folder.

---

## 🛡️ 5. SECURITY GUARDRAIL & EXECUTION POLICY (IMMUTABLE)

### A. MANDATORY CONTEXT BOUNDARY
- You are STRICTLY FORBIDDEN from executing any file system operation or terminal command outside the current project root directory.
- Never touch, scan, or analyze any paths containing system directories, cloud sync paths, or global user profiles (e.g., `C:/Windows`, `C:/Users/Administrator`, `/etc/`, `/var/`).

### B. TERMINAL COMMAND BLACKLIST (STRICT BAN)
You are absolutely prohibited from generating or running the following destructive commands under any circumstances. If a task implies their usage, STOP immediately and demand human intervention:
- **POSIX / WSL2 / Docker Environment Banned Commands:**
  - `rm -rf /` or `rm -rf *` (or any recursive deletion above the local subdirectory level).
  - `chmod 777` or `chown` on root levels.
  - `docker system prune -a` or `docker rmi` without explicit target hashes.
  - `kill -9` on system PIDs.
- **Native Windows Environment Banned Commands:**
  - `rd /s /q c:\` or `rd /s /q` on any parent directories.
  - `del /f /s /q` targeted at wildcard extensions outside `.venv` or temporary caches.
  - `format`, `diskpart`, `cipher`.
  - `Remove-Item -Recurse -Force` targeted at root or user profile paths.

### C. SAFE DELETION PROTOCOL (POKA-YOKE)
When tasked with cleaning up files, directories, or optimizing code structures:
1. **Verification Turn:** You must perform a `git status` or file list check first to log exactly what files are targeted.
2. **Local Scope Isolation:** Deletion is only permitted inside specific volatile directories: `.pytest_cache/`, `__pycache__/`, or files explicitly added by the active sub-task.
3. **No Global Cleanup:** Never attempt to resolve Python library bloat by clearing system-wide folders. Always operate within the local `.venv` using `uv`.

### D. SECURITY INTERCEPTOR VIOLATION TRIGGER
If any generated plan or subagent routine attempts to bypass these rules, you must:
- Immediately abort the active orchestration loop.
- Write a high-priority alert block into `AUDIT_LOG.md` detailing the blocked token payload.
- Return control to the user with a `CRITICAL_SECURITY_EXCEPTION` message.

---

## 🛠️ 6. Memory Harness Pro Upgrade (Prototype Stage)

If you are active on the `feature/memory-harness-upgrade` branch, the 5 advanced memory solutions are currently in prototype stage. Do not modify `watcher.py` or `memory_tool.py` directly without reviewing this harness.
- **Harness Scripts:**
  - Prototype Code: [.agent/scripts/test_memory_upgrade.py](file:///C:/Users/firef/Documents/antigravity/joyful-babbage/.agent/scripts/test_memory_upgrade.py)
  - Unit Tests: [tests/test_memory_harness.py](file:///C:/Users/firef/Documents/antigravity/joyful-babbage/tests/test_memory_harness.py)
- **Harness Commands:**
  - Execute Prototype Benchmark: `uv run python .agent/scripts/test_memory_upgrade.py`
  - Execute Harness Unit Tests: `uv run pytest tests/test_memory_harness.py`
- **Documentation:**
  - Architectural Decision Record: [ADR_memory_upgrade.md](file:///C:/Users/firef/Documents/antigravity/joyful-babbage/docs/ADR_memory_upgrade.md)
  - Vietnamese Step-by-Step Guide: [huong_dan_memory_upgrade.md](file:///C:/Users/firef/Documents/antigravity/joyful-babbage/docs/huong_dan_memory_upgrade.md)

---

## 🎬 7. TeamTrau-AutoEdit Project Specifications & Architecture

This project is an **Automated 4K/1080p Non-Destructive Video Editing Toolkit Orchestrator** on Windows 11 leveraging NVIDIA GPU acceleration (CUDA, NVENC).

### Core Pipeline Flow:
1. **Ingestion & Scene Detection (`src/modules/ingest_detector.py`)**:
   - Non-destructive PySceneDetect (`detect-adaptive`) shot boundary detection.
   - Dual FFmpeg native filter dead-air elimination (`silencedetect` + `freezedetect`).
   - Auto-Editor CLI / native OpenCV frame-difference motion pre-trimming.
   - Non-destructive export to CSV and CMX 3600 EDL.
2. **Audio & Rhythm Tracking (`src/modules/audio_analyzer.py`)**:
   - Librosa beat tracking, onset envelope calculation, 4/4 downbeat isolation.
   - Strict `min_beat_interval >= 0.4s` filtering to prevent micro-cuts.
3. **Visual & Aesthetic Scoring (`src/modules/visual_scorer.py`)**:
   - Keyframe sampling, Tenengrad/Laplacian sharpness evaluation.
   - MobileNetV3 PyTorch model accelerated by NVIDIA CUDA (fallback: OpenCV multi-metric).
   - 64-bit Perceptual dHash deduplication with Hamming distance thresholding to drop duplicate camera angles.
4. **Sequence Synthesizer (`src/modules/timeline_builder.py`)**:
   - OpenTimelineIO multi-track sequence assembly with optional rapid Retention Teaser Hook (3-5 fast cuts of highest-scoring scenes).
   - Native interchange export: DaVinci Resolve (`.fcpxml`), Premiere Pro (`.xml` via FCP 7 XML), OpenTimelineIO (`.otio`).
5. **Dual Dispatchers**:
   - **DaVinci Resolve Studio API (`src/dispatchers/resolve_dispatcher.py`)**: Windows scripting integration (`DaVinciResolveScript.py`, `fusionscript.dll`), project canvas configuration, timeline import, and render queue management.
   - **Standalone FFmpeg NVENC (`src/dispatchers/nvenc_renderer.py`)**: Headless video encoding using dynamic `filter_complex_script` (bypassing Windows 8191 CLI character limit) with `hevc_nvenc` (preset `p7`, CQ 18, 60M bitrate), teaser hook generation, and automatic CPU `libx265` fallback.

### Presets (`src/core/config.py`):
- Default: `1080p30` (1920x1080 @ 30.0 fps).
- Presets: `1080p30`, `1080p60`, `1080p24`, `4k30`, `4k60`, `4k24`, `social_vertical` (1080x1920), `social_vertical_60`, `cinema_4k` (4096x2160 @ 24 fps).

### Key Execution Commands:
- Diagnostic Check: `.venv\Scripts\python.exe main.py check-env`
- List Presets: `.venv\Scripts\python.exe main.py list-presets`
- Run Pipeline:
  ```powershell
  .venv\Scripts\python.exe main.py run --input-dir "path/to/footage" --soundtrack "path/to/audio.mp3" --output-dir "./output" --preset 1080p30
  ```
- Run Test Suite: `.venv\Scripts\python.exe -m pytest tests -v --tb=short`
- Run Linter: `.venv\Scripts\python.exe -m ruff check src tests main.py`



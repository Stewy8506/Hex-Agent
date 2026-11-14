# Product Roadmap: Custom Autonomous AI Harness

**Baseline Completed:**
- Stages 1–5 End-to-End Baseline Prototype (10/10 acceptance tests passing).
- Copy-on-Write (CoW) APFS clones & 0ms Call-Level Gateway Journaling.
- Active Graphify AST Blast-Radius Interception & Test Pinning.
- NVIDIA SkillSpector upstream engine vendored & wired.
- Google Research Harness² vendored & wired behind safety gates.
- Fast-Path Express Lane, Speculative Branching, Proof-Carrying Artifacts.
- Local Web Control Surface, FastAPI REST API, and CLI.

---

## Milestone M1 & M2: Core Engine & Durability [100% COMPLETE]

```
[x] Phase 01: PostgreSQL Async Engine & Alembic Migrations
[x] Phase 02: Linux OverlayFS Provider & Container Sandbox Isolation
[x] Phase 03: External MCP Server Client & Dynamic Tool Gateway
[x] Phase 04: Real Apple Silicon MLX Runner & Cloud API Connectors
[x] Phase 05: Multi-Turn Autonomous Repair & Dynamic DAG Replanning
[x] Phase 06: Distributed Worker Daemon & Cross-Machine CoW Sync
[x] Phase 07: Production Packaging, Approval Webhooks & Operator Runbooks
```

---

## Milestone M3: The Finished Product Layer (The 5 Product Pillars)

```
[x] Phase 08: Interactive Terminal UI (`hex chat`) & Onboarding Polish (Pillars 1 & 2)
[x] Phase 09: Bidirectional Multi-Platform Chat Gateways (Telegram & Discord Bots) (Pillar 3)
[x] Phase 10: Git & GitHub Autopilot (Auto-PRs, Conventional Commits & Webhook Bot) (Pillar 4)
[x] Phase 11: Autonomous Background Automations & Autonomous Self-Synthesizing Skills (Pillar 5)
```

---

## Milestone M4: Advanced Autonomy & Advanced Graphify Ergonomics

```
[x] Phase 12: Developer Ergonomics & Full-Text Search Parity (Doctor, Recipes, FTS5 Search, Backup/Dump & Graphify Cache)
```

---

### Phase 12: Developer Ergonomics & Full-Text Search Parity
- **Goal:** Incorporate top developer ergonomics identified from reverse-engineering Nous Research HEX Agent: `hex doctor` diagnostic suite, deep multi-framework recipe detection, SQLite FTS5 full-text transcript search, single-command state backup and diagnostic dump, and optimized multi-language Graphify caching.
- **Must-haves:**
  - `hex doctor` with `--fix` and `--live` diagnosing Python runtime, M4/GPU acceleration, CoW drivers, sandboxes, and API keys.
  - Multi-framework recipe detector in `src/harness_core/recipes.py` discovering bootstrap/build/test commands across Python, Node.js, Rust, Go, Makefile, and Docker.
  - SQLite FTS5 full-text search across past tasks, tool call journals, and transcripts via `hex search "<query>"`.
  - State backup and plain-text support dump via `hex backup` and `hex dump`.
  - Incremental disk caching and multi-language support in `ActiveGraphifyAdapter`.
- **Verification:** Unit and integration tests demonstrating doctor self-healing, recipe detection across multiple project layouts, FTS5 search queries with BM25 ranking, and archive backup/restore verification.

---

### Phase 08: Interactive Terminal UI (`hex chat`) & Onboarding Polish (Pillars 1 & 2)
- **Goal:** Provide an interactive terminal pair-programming console (`hex chat`) with live streaming markdown, syntax-highlighted side-by-side diffs, one-key keyboard approvals (`[y] Approve`, `[n] Reject`, `[d] Diff`, `[s] Sandbox`), in-chat slash commands (`/status`, `/diff`, `/rollback`, `/fork`, `/skills`, `/model`, `/budget`), and complete `hex onboard` / `hex init` packaging.
- **Must-haves:**
  - `hex chat` console REPL backed by `rich` and `prompt_toolkit`.
  - Progressive streaming token output with code syntax highlighting.
  - Interactive unified & side-by-side diff renderer for proposed CoW patches.
  - One-key approval prompts on `ApprovalRequiredInterrupt`.
  - Slash command handlers in chat session.
  - `hex init` generating project-scoped `.hex/config.yaml`.
- **Verification:** Interactive and automated tests proving TUI streaming, diff rendering, keyboard approval resume, and `hex init` project creation.

### Phase 09: Bidirectional Multi-Platform Chat Gateways (Telegram & Discord Bots) (Pillar 3)
- **Goal:** Transform one-way outbound approval webhooks into full bidirectional conversational bots on Telegram and Discord, allowing operators to command tasks, query live status, and decide gated approvals from mobile chat.
- **Must-haves:**
  - `hex gateway start` daemon running Telegram long-polling/webhook listener and Discord gateway.
  - Conversational task delegation (e.g. DM `"Fix issue #42"` $\to$ starts CoW task).
  - Mobile approval cards with inline interactive buttons (`[Approve]`, `[Reject]`, `[View Diff]`, `[Cancel]`).
  - Strict user/chat ID whitelist pairing and audit logging.
- **Verification:** Unit and integration tests with mocked Telegram/Discord API transports simulating full conversation: task creation, progress stream, button click approval, and completion broadcast.

### Phase 10: Git & GitHub Autopilot (Auto-PRs, Conventional Commits & Webhook Bot) (Pillar 4)
- **Goal:** Enable HEX to operate natively in Git and GitHub workflows: auto-branching, atomic conventional commits, opening pull requests via GitHub CLI (`gh pr create`) with Proof-Carrying Artifact summaries, and running as a GitHub Action / webhook responder.
- **Must-haves:**
  - `hex run "<goal>" --pr` auto-branching (`hex/task-<id>-<slug>`), running CoW verification, committing, and opening a PR.
  - Formatted PR description containing executive summary, Active Graphify AST impact matrix, and test proof hashes.
  - GitHub Action / issue comment responder triggered by `@hex-bot fix ...`.
- **Verification:** Tests verifying branch naming, commit formatting, GitHub API/CLI command generation, and PR markdown body compilation.

### Phase 11: Autonomous Background Automations & Autonomous Self-Synthesizing Skills (Pillar 5)
- **Goal:** Enable 24/7 autonomous background operation via native cron scheduler (`hex schedule`), systemd/launchd daemon installation (`hex daemon install`), and a autonomous learning loop synthesizing verified solutions into reusable skills gated by NVIDIA SkillSpector.
- **Must-haves:**
  - `hex schedule add <cron> --goal <goal>` cron scheduler running tasks in isolated CoW sandboxes.
  - `hex daemon install` generating `LaunchAgent` plist (macOS) and `systemd` service unit (Linux).
  - autonomous self-synthesizing skill creator extracting reusable patterns from successful executions.
  - Automated NVIDIA SkillSpector security gate verifying synthesized skills before registration.
- **Verification:** Tests proving cron triggers, daemon unit generation, skill synthesis, and SkillSpector quarantine verification of untrusted or malformed synthesized skills.

---

### Phase 01: PostgreSQL Async Engine & Alembic Migrations
- **Goal:** Elevate `LedgerStore` from local SQLite WAL to production PostgreSQL 16+ with connection pooling, non-blocking async operations, `SELECT ... FOR UPDATE SKIP LOCKED` queue concurrency, and forward-only Alembic migrations.
- **Must-haves:**
  - Dual-mode `LedgerStore` (PostgreSQL async driver + SQLite local dev profile).
  - Working Alembic migration environment (`migrations/versions/`).
  - Concurrent worker lease claiming tests demonstrating zero lock contention.
- **Verification:** Unit and integration tests demonstrating PostgreSQL concurrent lease claims and schema migration rollback checks.

### Phase 02: Linux OverlayFS Provider & Container Sandbox Isolation
- **Goal:** Expand CoW durability beyond macOS APFS to Linux Desktop/Server hosts via OverlayFS and Btrfs subvolumes, and enclose untrusted tool runs inside container sandbox boundaries.
- **Must-haves:**
  - `OverlayFSSnapshotProvider` implementing `WorkspaceSnapshotProvider`.
  - Docker/Podman or bubblewrap execution sandbox mounting attempt's CoW directory.
  - Zero-escape verification on destructive shell commands (`rm -rf`, host escapes).
- **Verification:** Automated tests creating OverlayFS layers, executing bounded commands, and reverting to pristine base state.

### Phase 03: External MCP Server Client & Dynamic Tool Gateway
- **Goal:** Enable the Tool Gateway to discover, mediate, and journal external Model Context Protocol (MCP) servers over stdio and SSE transports without bypassing harness policy.
- **Must-haves:**
  - `MCPClientAdapter` for stdio subprocess and HTTP/SSE MCP servers.
  - Dynamic tool schema ingestion with mandatory `R0–R4` risk mapping.
  - SkillSpector pre-validation of MCP server manifest definitions.
  - 0 ms call-level journaling on all external MCP tool invocations.
- **Verification:** Spin up a local MCP test server, execute composite calls, simulate crash, and prove 0ms journal replay.

### Phase 04: Real Apple Silicon MLX Runner & Cloud API Connectors
- **Goal:** Replace simulated model responses with live quantized local inference via `mlx-lm` on the MacBook Air M4 and secure cloud API connectors (Anthropic Claude, OpenAI, Gemini) for Tier-2 reasoning.
- **Must-haves:**
  - `LocalMLXModelProvider` executing quantized models on Apple Silicon unified memory.
  - Live log compression benchmarks achieving >70% token savings on real pytest traces.
  - Cloud API client with per-task budget ceilings, secret redaction, and privacy gating.
- **Verification:** Feed a live 500-line pytest failure into local MLX runner and verify compressed summary retains failing assertion; test privacy routing blocks restricted data from cloud.

### Phase 05: Multi-Turn Autonomous Repair & Dynamic DAG Replanning
- **Goal:** Transform single-turn step execution into an intelligent multi-turn repair loop that consumes compressed failure traces and blast-radius warnings to autonomously fix coding bugs within budget.
- **Must-haves:**
  - Multi-turn repair loop within `RUNNING` -> `VERIFYING` cycle.
  - Dynamic replanning: mutates only invalidated subgraphs when a step produces unexpected side effects.
  - Automatic escalation to `NEEDS_ATTENTION` when retry budget is exhausted.
- **Verification:** Autonomous repair benchmark: inject broken code, verify harness diagnoses failure from test log, applies fix, verifies downstream tests, and emits signed proof bundle.

### Phase 06: Distributed Worker Daemon & Cross-Machine CoW Sync
- **Goal:** Allow tasks to be dispatched across heterogeneous machines (MacBook Air M4, Desktop GPU, future AI server) with automated CoW delta sync and lease migration.
- **Must-haves:**
  - `harness worker` standalone daemon connecting to control plane over authenticated mTLS / Tailscale.
  - CoW delta archive sync: worker reconstructs workspace from git base + CAS delta.
  - Safe lease handover when a worker sleeps or disconnects.
- **Verification:** Simulate worker disconnect mid-task; verify lease expires cleanly and second worker resumes from exact sealed tool call and CoW tree.

### Phase 07: Production Packaging, Approval Webhooks & Operator Runbooks
- **Goal:** Polish the system into a hardened, production-ready personal workbench with remote approval webhooks, production ASGI deployment, and comprehensive operational tooling.
- **Must-haves:**
  - Outbound approval webhooks (Telegram bot & Discord webhook) for `R3` approval requests with clickable preview.
  - Production ASGI configuration (`uvicorn`/FastAPI) with graceful shutdown and health checks.
  - Operator runbook for disaster recovery, database restore, quarantined skill forensics, and harness rollback.
- **Verification:** Trigger an `R3` action, verify webhook notification arrives with proof preview, approve via webhook/API, and confirm task finishes successfully.


---

## Milestone M5: Interactive Auto-Setup, Web Search Tool & Real-Time Environment Hydration

```
[x] Phase 13: Interactive Auto-Setup, Web Search Tool & Real-Time Environment Hydration
    [x] 13-01: Terminal UI Selector, Environment Hydration & Autonomous Web Search
    [x] 13-02: Interactive Onboarding Wizard, Auto-Setup Interceptor & Harness Polish
[x] Phase 14: Antigravity Interactive OAuth, Model Selection & TUI Crash Hardening
    [x] 14-01: Token Probing, Browser PKCE OAuth & Model Selection
    [x] 14-02: TUI Crash Resilience, Automatic OAuth Refresh & Test Suite
[x] Phase 15: Live API Model Discovery & Project-Scoped Workspaces
    [x] 15-01: Live Dynamic Model Discovery Engine
    [x] 15-02: Project Workspace Registry & Daily Project Switcher
```

### Phase 13: Interactive Auto-Setup, Web Search Tool & Real-Time Environment Hydration
- **Goal:** Upgrade HEX onboarding into an interactive terminal arrow-key experience (`[•]` radio selection), implement autonomous web search tool, wire remote messaging gateways into setup, and auto-intercept unconfigured launches.
- **Must-haves:**
  - `TerminalUISelector` with POSIX termios/tty arrow-key navigation (`↑`/`↓`), `[•]` radio display, and masked secret input.
  - Universal environment & configuration loader (`load_hex_environment`) injecting `~/.hex/credentials.env` and `config.yaml` on boot.
  - Multi-engine autonomous Web Search tool (`web_search`) supporting DuckDuckGo (zero-key fallback), Tavily, and Brave Search registered in `ToolGateway`.
  - 4-step interactive setup wizard in `run_onboard_wizard`: Model Provider, Web Search Engine, Remote Messaging Gateways, and Hardware Profile.
  - First-run startup interceptor in `cli.py` detecting zero-key unconfigured state and guiding setup.
  - Purged hardcoded dummy artifacts (`v1.3.0`, `M4 MLX`, `wrk_m4_air_local`, `mock-key`).
- **Verification:** Unit and integration test suites passing in `tests/test_ui_selector_and_web_search.py` and `tests/test_auto_setup_and_polish.py`, with 100% green full-repository verification.

### Phase 14: Antigravity Interactive OAuth, Model Selection & TUI Crash Hardening
- **Goal:** Resolve live operator issues during first-run onboarding: validate Antigravity tokens before accepting, provide browser PKCE OAuth sign-in fallback, replace fictitious model strings with real production models, simplify search prompts, and prevent TUI crashes on provider authentication errors.
- **Must-haves:**
  - `_probe_antigravity_token()` validating discovered tokens against Google Cloud companion API before declaring active.
  - Interactive browser PKCE OAuth 2.0 flow (`webbrowser.open`) or manual token entry in onboarding when unauthenticated.
  - Production model selection (`gemini-2.5-pro` default, `gemini-2.5-flash`, `claude-3-5-sonnet`) across `onboard.py`, `router.py`, `doctor.py`, and `cli.py`.
  - Simplified search label (`Disabled`) and silent hardware profiling without redundant sandbox questions.
  - TUI crash resilience: catch `ProviderUnavailableError` and HTTP 401 cleanly with remediation commands (`hex auth antigravity --auth-url`, `/model`, `hex onboard`).
  - Automatic token refresh in `AntigravityModelProvider` upon HTTP 401 when `refresh_token` is present.
- **Verification:** Unit and integration tests in `tests/test_antigravity_oauth_and_tui_resilience.py` (7/7 tests passing) and 422/422 repository tests 100% green.

### Phase 15: Live API Model Discovery & Project-Scoped Workspaces
- **Goal:** Invert provider onboarding to authenticate first, query live provider API endpoints for dynamic model discovery with offline fallbacks, and introduce a global project registry with folder-scoped context isolation and daily project switcher.
- **Must-haves:**
  - Authenticate-first onboarding sequence across Antigravity, Anthropic, OpenAI, Gemini, and Local/Ollama.
  - Live model discovery querying `fetchAvailableModels` (Antigravity), `v1/models` (Anthropic & OpenAI), `v1beta/models` (Gemini), and `api/tags` (Ollama).
  - Clear `[offline fallback]` badge on curated fallback models when unreachable or offline.
  - Interactive `/model` command in TUI querying live provider models via `TerminalUISelector`.
  - Global project registry in `~/.hex/projects.json` tracking registered projects and last active timestamps.
  - Daily project switcher ("What project do you want to work on today?") when launched outside a workspace.
  - Automatic project binding when launched inside a git repo or `.hex/` project folder.
  - Complete folder-scoped context isolation: `<project_dir>/.hex/` with `config.yaml`, `graph.json`, `reflections/LESSONS.md`, and `data/`.
  - CLI command `hex project [list|add|use|switch]`.
- **Verification:** Comprehensive unit and integration test suites in `tests/test_live_model_discovery.py` and `tests/test_project_registry_and_workspace.py`.

---

## Milestone M7: Polyglot LSP Gateway & Real-Time Hands-Free Voice

```
[x] Phase 16: Polyglot Language Server Protocol (LSP) Gateway & Compiler Diagnostic Interceptor
    [x] 16-01: Polyglot LSP Client Core & Stdio Transport
    [x] 16-02: Tool Gateway Registration & Autonomous Repair Pre-Flight Interceptor
[x] Phase 17: Real-Time Hands-Free Voice & Speech Pair-Programming
    [x] 17-01: Audio Capture, Speech-to-Text & Text-to-Speech Engine
    [x] 17-02: Interactive TUI Voice Mode & Hands-Free Conversation Loop
```

### Phase 16: Polyglot Language Server Protocol (LSP) Gateway & Compiler Diagnostic Interceptor
- **Goal:** Implement native Language Server Protocol (LSP) client support for all major programming languages (Python, TypeScript/JavaScript, Go, Rust, C/C++, Java), expose compiler diagnostics and navigation through `ToolGateway`, and integrate compiler pre-flight validation directly into the autonomous repair loop.
- **Must-haves:**
  - Multi-language server detection & lifecycle manager (`LSPManager`) supporting `pyright`/`pylsp`/`ruff` (Python), `typescript-language-server`/`vtsls` (TS/JS), `gopls` (Go), `rust-analyzer` (Rust), `clangd` (C/C++), and `jdtls` (Java).
  - Stdio JSON-RPC 2.0 transport with initialization handshake, document synchronization (`didOpen`, `didChange`), and diagnostic tracking (`publishDiagnostics`).
  - Read-only tools in `ToolGateway`: `lsp_diagnostics` (file/workspace error & warning aggregation), `lsp_definition` (go-to-definition), `lsp_references` (find call sites), and `lsp_hover` (type signatures and docstrings).
  - Compiler pre-flight gate in `HarnessOrchestrator`: validate repaired files against language server diagnostics before running slow verification suites.
  - CLI command `hex lsp [diagnostics|status|definition]` and TUI `/lsp` command.
- **Verification:** Unit and integration test suites proving JSON-RPC message framing, mock/real LSP server lifecycle, diagnostic extraction, definition navigation, and repair pre-flight gating.

### Phase 17: Real-Time Hands-Free Voice & Speech Pair-Programming
- **Goal:** Add real-time hands-free voice conversation to HEX, enabling speech-to-text voice commands, spoken audio responses from the agent, and seamless switching between voice and keyboard modes.
- **Must-haves:**
  - Modular audio capture and speech recognition engine (`VoiceEngine`) supporting native platform speech (macOS `NSSpeechRecognizer` / AVFoundation / `say`), local Whisper STT, or cloud speech streaming.
  - Speech synthesis (TTS) for agent responses with configurable speed, voice selection, and instant mute toggle (`Ctrl+M` / `/voice mute`).
  - Interactive voice mode in TUI: `/voice` slash command, `hex chat --voice`, and push-to-talk / continuous voice activity detection (VAD).
  - Audio diagnostic in `hex doctor` validating microphone access and audio output hardware.
- **Verification:** Unit and integration tests for audio pipeline framing, transcript dispatch, TTS synthesis invocation, and TUI voice mode state transitions.

---

## Milestone M8: Deep Upstream Vendor Integration [100% COMPLETE]

```
[x] Phase 18: Google Research Harness² Deep Wiring
    [x] 18-01: Experience Library (INDEX.md / EXPERIENCE.md) integration into HarnessOrchestrator
    [x] 18-02: OrchestratorBenchmarkAdapter & Propose-Probe-Compose task sweep runner
[x] Phase 19: NVIDIA SkillSpector Upstream Behavioral AST & Live OSV Scanner
    [x] 19-01: Python AST dangerous call analyzer & taint tracking integration
    [x] 19-02: Live OSV.dev batch client with offline cache fallback
[x] Phase 20: Graphify Upstream CLI & Tree-Sitter Adapter
    [x] 20-01: Upstream graphify CLI runner & graphify-out/graph.json ingest adapter
    [x] 20-02: Polyglot graph unification & multi-language blast-radius verification
```

### Phase 18: Google Research Harness² Deep Wiring
- **Goal:** Deeply wire Google Research's Harness² engine into the workbench by integrating `harness2.core.library.Library` into `HarnessOrchestrator` for cross-task experience persistence, building an `OrchestratorBenchmarkAdapter` satisfying `BenchmarkAdapter`, and enabling real Propose-Probe-Compose task sweeps.
- **Must-haves:**
  - `HarnessOrchestrator` writes structured entries to `harness2.core.library.Library` upon successful task completion (`EXPERIENCE.md`, `INDEX.md`, cue/lesson/evidence).
  - Pre-task experience retrieval: inject past domain lessons and adopted prompt/skill configurations into agent context.
  - `OrchestratorBenchmarkAdapter` implementing `harness2.core.spec.BenchmarkAdapter` protocol to run candidate rollouts through `HarnessOrchestrator`.
  - CLI subcommand `hex harness2-eval` to execute real Propose-Probe-Compose sweeps via `harness2.core.recursion`.
- **Verification:** Unit and integration tests for experience recording, domain index rebuild, retrieval in orchestrator, adapter rollout execution, and trajectory capture.

### Phase 19: NVIDIA SkillSpector Upstream Behavioral AST & Live OSV Scanner
- **Goal:** Connect upstream NVIDIA SkillSpector's `behavioral_ast` and `python_ast` analyzers to inspect skill script code for dangerous execution patterns, and wire `osv_client` to query live OSV.dev batch API for dependency vulnerabilities.
- **Must-haves:**
  - `SkillSpectorScanner` parses Python scripts in skill bundles via `skillspector.python_ast` and evaluates dangerous AST nodes via `skillspector.nodes.analyzers.behavioral_ast`.
  - `SkillSpectorScanner` queries live OSV.dev batch endpoint via `skillspector.nodes.analyzers.osv_client` for manifest packages, falling back gracefully to offline database.
  - Zero-dependency fallback: retain fast regex scanning when optional upstream dependencies are absent.
- **Verification:** Unit and integration tests proving AST detection of dangerous calls (eval, exec, subprocess, dynamic imports, sockets) and live/mocked OSV batch vulnerability detection.

### Phase 20: Graphify Upstream CLI & Tree-Sitter Adapter
- **Goal:** Add a pluggable upstream Graphify runner and artifact ingester so projects can invoke `graphify run` for full tree-sitter polyglot AST parsing (33+ languages) and ingest `graphify-out/graph.json` into our active blast-radius and architecture engine.
- **Must-haves:**
  - `GraphifyUpstreamRunner` to discover or invoke upstream `graphify run` via subprocess or CLI.
  - Ingestion adapter in `ActiveGraphifyAdapter` to load `graphify-out/graph.json` or `.hex/graph.json` generated by upstream tree-sitter.
  - Unified blast-radius calculation across native Python AST and upstream tree-sitter graph representations.
- **Verification:** Unit and integration tests demonstrating upstream graph ingestion, multi-language node resolution, and blast-radius tracking.

---

## Milestone M9: Parity & Next-Gen Capabilities (Tiers C, B, A) [100% COMPLETE]

```
[x] Phase 21: In-Flight Sub-Agent Delegation & Multi-Agent Swarm Coordinator (Tier C)
    [x] 21-01: SubagentManager Core & Role-Constrained CoW Sandboxing
    [x] 21-02: ToolGateway Delegate Tools & TUI /subagents Command
[x] Phase 22: Interactive Headless Browser & Multimodal Vision Engine (Tier B)
    [x] 22-01: CDP Headless Browser Runner & Navigation Tools
    [x] 22-02: Multimodal Image Inspection & Visual Testing Engine
[x] Phase 23: Agent Client Protocol (ACP) Server for Zed, Neovim & VSCode (Tier A)
    [x] 23-01: ACP JSON-RPC 2.0 Server Core & Stdio Transport
    [x] 23-02: Editor Lifecycle Handshake & Tool Approval Bridge
```

### Phase 21: In-Flight Sub-Agent Delegation & Multi-Agent Swarm Coordinator (Tier C)
- **Goal:** Enable the primary agent to spawn focused, budget-bounded child sub-agents (`delegate`) operating in isolated CoW workspaces with specialized roles (`researcher`, `coder`, `tester`, `reviewer`, `planner`), returning structured findings and diffs without polluting parent context.
- **Must-haves:**
  - `SubagentManager` with role-based tool gating, spawn depth limits, and CoW workspace branching.
  - `delegate`, `subagent_status`, and `subagent_wait` tools registered in `ToolGateway`.
  - TUI `/subagents` slash command in `hex chat`.
- **Verification:** Unit and integration tests in `tests/test_subagent_delegation.py` demonstrating role isolation, synchronous and asynchronous execution, budget inheritance, and depth guards.

---

## Milestone M10: Finished Product Parity & Execution Excellence (Hermes Parity)

```
[x] Phase 24: Continuous Multi-Turn ReAct Tool Loop (Hermes Parity Pillar 1)
    [x] 24-01: ReActLoop Engine Core & Tool Execution Dispatcher
    [x] 24-02: Orchestrator Integration, Streaming Events & TUI Loop Mode
[x] Phase 25: Native Rust Terminal Cockpit (Ratatui + Crossterm) & Protocol Crate (Hermes Parity Pillar 2)
    [x] 25-01: Cargo Workspace & `crates/hex-protocol` (ACP JSON-RPC Engine & Models)
    [x] 25-02: `crates/hex-tui` (Ratatui Alt-Screen Cockpit, Differential Renderer & CLI Launcher)
[x] Phase 26: 4-Phase Context Compactor & Prompt Cache Locking (Hermes Parity Pillar 3)
    [x] 26-01: Stale Tool Demotion & Atomic Tool-Boundary Preservation
    [x] 26-02: Middle-Turn Summarizer & Immutable Prompt-Cache Prefixing
[ ] Phase 27: PTY Background Process Supervisor & Stream Registry (Hermes Parity Pillar 4)
    [ ] 27-01: PTY ProcessRegistry & Circular Ring Buffer
    [ ] 27-02: Gateway Process Tools (start_process, read_terminal, kill_process)
[ ] Phase 28: Persistent REPL Code Kernel with Cross-Turn State (Hermes Parity Pillar 5)
    [ ] 28-01: Persistent In-Memory Python Kernel & State Isolation
    [ ] 28-02: Gateway code_kernel Tool & Workspace File Sync
```

### Phase 24: Continuous Multi-Turn ReAct Tool Loop (Hermes Parity Pillar 1)
- **Goal:** Implement an autonomous multi-turn ReAct loop in `src/harness_core/react_loop.py` capable of executing up to 50 continuous turns per session, invoking tools iteratively through `ToolGateway`, appending outputs to dialogue history, and running until task completion or error boundary.
- **Must-haves:**
  - `ReActLoop` class with cycle limits, token budget monitoring, and early-termination guards (`<finish>` or no remaining tool calls).
  - Integration with `HarnessOrchestrator.run_agent_loop` and `HexConsole`.
  - Call journal integration ensuring every turn in the ReAct loop is sealed into SQLite/PostgreSQL `LedgerStore`.
- **Verification:** Comprehensive unit and integration tests verifying multi-turn cycles, tool call handling, termination conditions, and error recovery.

### Phase 25: Native Rust Terminal Cockpit (Ratatui + Crossterm) & Protocol Crate (Hermes Parity Pillar 2)
- **Goal:** Build a blazing-fast, 60 FPS, differential-rendered terminal cockpit in Rust using Ratatui and Crossterm, sharing protocol types and ACP client logic in `crates/hex-protocol` that directly bridges to our future GPUI desktop app.
- **Must-haves:**
  - Cargo workspace with `crates/hex-protocol` (shared domain models, JSON-RPC 2.0 ACP client, diff parser) and `crates/hex-tui` (Ratatui binary).
  - Instant $<2\text{ ms}$ startup, dual-buffer zero-flicker differential rendering on alt-screen.
  - Interactive multi-pane cockpit layout: sticky header with CoW status and model, active streaming dialogue viewport with collapsible tool call accordions, colorized unified/side-by-side git diff pane, and interactive text composer with command autocompletion.
  - Python launcher integration: `hex tui` or `hex chat --ratatui` launching the native binary connected over stdio to `src/harness_core/acp.py`.
- **Verification:** `cargo test --workspace` unit tests for JSON-RPC message serialization/deserialization, state transitions, and integration tests running `hex-tui --check`.

### Phase 26: 4-Phase Context Compactor & Prompt Cache Locking (Hermes Parity Pillar 3)
- **Goal:** Implement a production-grade 4-phase context compactor in `src/harness_core/context_compressor.py` matching `agent/context_compressor.py` in Hermes: stale tool output demotion, atomic tool-pair boundary protection, middle-turn compression, and static prompt-cache prefix locking.
- **Must-haves:**
  - `ContextCompactor` with `compact_messages(messages, budget_tokens)` preserving system prompt head and recent turn tail.
  - Strict preservation of `<tool_call>` and corresponding `<tool_result>` pairs (never splitting an atomic pair).
  - Stale tool output truncation (replacing verbose stdout with summarized head+tail and line count).
  - Prompt cache prefix alignment to ensure static system instructions and tool definitions remain unchanged across turns for >90% KV cache hit rates.
- **Verification:** Unit tests verifying token budget adherence, atomic pair preservation, stale tool pruning, and cache prefix stability.

### Phase 27: PTY Background Process Supervisor & Stream Registry (Hermes Parity Pillar 4)
- **Goal:** Implement an asynchronous pseudo-terminal (PTY) process registry in `src/harness_core/process_registry.py` matching `tools/process_registry.py` in Hermes, allowing long-running background tasks (e.g. dev servers, build processes) to run and be inspected.
- **Must-haves:**
  - `ProcessRegistry` supporting process creation, PTY allocation, circular ring buffer for stdout/stderr, and graceful termination.
  - Gateway tools: `start_process`, `read_terminal`, `kill_process`, `list_processes`.
  - Automatic cleanup and exit hook on session shutdown.
- **Verification:** Unit tests verifying background process startup, non-blocking tail reads from ring buffer, process listing, and safe termination.

### Phase 28: Persistent REPL Code Kernel with Cross-Turn State (Hermes Parity Pillar 5)
- **Goal:** Implement an interactive persistent Python code kernel in `src/harness_core/code_kernel.py` matching `tools/code_kernel.py` in Hermes, enabling persistent variables, functions, and imports across consecutive turns within the task CoW workspace.
- **Must-haves:**
  - `PersistentCodeKernel` executing code snippets with state preservation, capturing stdout/stderr and expression values.
  - Gateway tool: `code_kernel` with timeout protection and memory isolation.
  - Direct integration with CoW workspace path resolution.
- **Verification:** Unit tests verifying cross-execution variable persistence, exception formatting, stdout capture, and kernel reset.






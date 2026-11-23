<div align="center">

```text
       ██╗  ██╗███████╗██╗  ██╗          █████╗  ██████╗ ███████╗███╗   ██╗████████╗
       ██║  ██║██╔════╝╚██╗██╔╝         ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝
       ███████║█████╗   ╚███╔╝  █████╗  ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   
       ██╔══██║██╔══╝   ██╔██╗  ╚════╝  ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   
       ██║  ██║███████╗██╔╝ ██╗         ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   
       ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝         ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   
                                          anv.dev
```

### The Autonomous AI Engineering Agent That Tests Before It Touches Your Code

[![Version](https://img.shields.io/badge/version-0.22.0-ffb703?style=flat-square&logo=semanticrelease&logoColor=white)](pyproject.toml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux-blueviolet?style=flat-square)](install.sh)
[![CI](https://github.com/Stewy8506/Harness/actions/workflows/ci.yml/badge.svg)](https://github.com/Stewy8506/Harness/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square)](LICENSE)

<p align="center">
  <a href="#-quick-install"><strong>Quick Install</strong></a> ·
  <a href="#-getting-started-in-3-steps"><strong>Getting Started</strong></a> ·
  <a href="#-native-macos-gpui-desktop-app"><strong>Native GPUI Desktop</strong></a> ·
  <a href="#-terminal-cockpit"><strong>Terminal Cockpit</strong></a> ·
  <a href="#-the-3-modes-agent--plan--code"><strong>The 3 Modes</strong></a> ·
  <a href="#-supported-models"><strong>Models</strong></a> ·
  <a href="#-daily-commands"><strong>Commands</strong></a> ·
  <a href="#-documentation-library"><strong>Documentation</strong></a>
</p>

</div>

---

### What is HEX?

**HEX** is an autonomous AI pair-programmer in your terminal and desktop designed to make AI coding reliable, fast, and safe.

Most AI coding assistants edit your files in place. If an agent hallucinates or produces broken syntax, your working branch is trashed, git history is cluttered with dirty diffs, and you spend valuable time cleaning up.

**HEX does the opposite:**
- **Zero-Risk Scratchpad**: Every code change and test runs inside an ultra-fast, isolated Copy-on-Write sandbox (<10ms).
- **Proof Before Commit**: HEX executes your project's test suite. Only when tests pass are patches applied to your code.
- **Native Cockpits**: A Ratatui terminal TUI is the supported default. A native macOS GPUI desktop app is **experimental**: it runs real chat sessions over ACP, but ships from source only.
- **Model Freedom**: Connect directly to your existing Google Antigravity OAuth quota, 26 cloud providers, or run 100% offline with local models (Ollama, Apple Silicon Metal, NVIDIA CUDA).

<table>
<tr><td><b>Isolated Sandboxing</b></td><td>Instant Copy-on-Write staging (macOS APFS / Linux OverlayFS in &lt;10ms). Broken patches are discarded with zero host pollution.</td></tr>
<tr><td><b>Proof Before Commit</b></td><td>Verifies tests and diagnostics inside the sandbox before touching a single line of your working branch.</td></tr>
<tr><td><b>Native GPUI Desktop Cockpit (experimental)</b></td><td>Native macOS app built with Zed's GPUI crate. Streams real ACP chat sessions with tool cards, approvals, cancellation, and per-session drafts. Source builds only; not yet a supported daily surface.</td></tr>
<tr><td><b>Native Rust Terminal Cockpit</b></td><td>Ultra-responsive 60 FPS Ratatui terminal interface with multiline editing, fuzzy slash autocomplete, and side-by-side split diffs.</td></tr>
<tr><td><b>Timeline Rewind & Branching</b></td><td>Roll back conversation turns and code states with <code>/rewind &lt;turn | -N&gt;</code> (Python console), preserving historical attempts in soft branches.</td></tr>
<tr><td><b>Mixture-of-Agents (MoA)</b></td><td>Core proposer/aggregator engine ships in the ReAct loop, enabled via configuration. A <code>/moa</code> toggle command is not yet exposed on any client surface.</td></tr>
<tr><td><b>Temporal Approval Leases</b></td><td>Auto-approve trusted command prefixes or directory paths for 15m, 1h, or custom TTL (<code>/lease</code> in the Python console) without tedious confirmation friction.</td></tr>
<tr><td><b>Multi-Key Resilience Pool</b></td><td>Round-robin credential routing, automatic HTTP 429 rotation with jittered backoff, model cooldown queues, and 30s endpoint blackholing.</td></tr>
<tr><td><b>Active Cognition & Memory</b></td><td>JIT AST-scoped memory injection into tool observations (100% frozen KV-cache prompt), negative [AVOID] guardrails, and executable <code>verify:</code> skill hooks.</td></tr>
<tr><td><b>Storage GC & Retention</b></td><td>Automated background TTL sweeps, reference-counted CAS pruning, task pinning, and zero-downtime incremental vacuum (<code>hex gc</code>).</td></tr>
<tr><td><b>Model Independence</b></td><td>Use any cloud provider or local offline LLM. Switch anytime with <code>/model</code>—no code changes, zero lock-in.</td></tr>
<tr><td><b>Editor Integration</b></td><td>Built-in Agent Client Protocol (ACP) server for seamless use inside Zed, Neovim, and VS Code.</td></tr>
</table>

---

## ⚡ Quick Install

### macOS & Linux

```bash
curl -fsSL https://raw.githubusercontent.com/Stewy8506/Harness/main/install.sh | bash
```

### Windows (Native PowerShell)

```powershell
irm https://raw.githubusercontent.com/Stewy8506/Harness/main/install.ps1 | iex
```

*Or install with `pipx` or `uv tool` (isolated, zero dependency conflicts):*

```bash
pipx install "git+https://github.com/Stewy8506/Harness.git[tui]"
# or
uv tool install "git+https://github.com/Stewy8506/Harness.git[tui]"
```

**What gets installed (the install contract):**

- The Python package (`hex-ai`) ships the CLI, ACP server, and vendored
  engines in the wheel — no compiler or source checkout required.
- The native Ratatui cockpit (`hex-tui`) is delivered as a checksum-verified
  release artifact into `~/.hex/bin/hex-tui` by `install.sh` / `install.ps1`.
  Building it from source is an explicit developer option
  (`./install.sh --build-native`, `HEX_BUILD_NATIVE=1` on Windows, or
  `hex tui --build`), never an automatic fallback.
- If the native cockpit is absent, HEX says so plainly and the Python
  console (`hex --legacy`) remains fully supported.
- `hex uninstall` removes the CLI wrappers and native binaries; add
  `--keep-data` to preserve `~/.hex` configuration and history, or `--purge`
  to delete them.

---

## 🚀 Getting Started in 3 Steps

### 1. Launch in Any Repository
Navigate to any codebase on your machine and launch HEX:

```bash
cd your-project

# Launch the native terminal cockpit (default):
hex

# Experimental GPUI desktop (from a source checkout, macOS only):
cargo run -p hex-desktop
```

> The desktop app is experimental and runs from a source checkout only; there
> is no `hex desktop` subcommand or packaged binary yet. It starts the Python
> engine for you (see the desktop section below).

### 2. Choose Your Model (One-Time Setup)
On your very first run, HEX opens an interactive terminal setup menu:

```text
◈ Welcome to HEX Agent
  Select your primary model brain:

  [•] Google Antigravity OAuth (Included Subscription Quota / Auto-detects IDE session)
  [ ] Anthropic Claude (Claude 3.7 Sonnet / Opus)
  [ ] OpenAI (GPT-4o / o3-mini)
  [ ] Google Gemini API (Gemini 2.5 Pro / Flash)
  [ ] Local Offline Model (Ollama / Apple Metal / NVIDIA CUDA / CPU)
```

> **Tip:** If you already use Google Antigravity, HEX auto-detects your active credentials with zero API keys required.

### 3. Ask Anything or Assign a Task
Type your request or task goal:

```text
❯ Add rate-limiting middleware to our FastAPI authentication router and verify tests pass
```

HEX will clone an isolated scratchpad, implement the code, run your test runner, and show you the clean diff once verified.

---

## 🖥️ Native macOS GPUI Desktop Cockpit (`hex-desktop`) — Experimental

> **Status: experimental.** The desktop runs a working chat flow against the
> Python engine over ACP, covered end to end by `App/hex-desktop/tests/test_chat_flow.rs`.
> It is distributed from source only, and some panels are not yet fed by live
> data (see *Not yet live* below). Performance figures (FPS, RAM) have not been
> benchmarked and are not claimed. The mockup below shows the target design.

```bash
cargo run -p hex-desktop
# If Metal shader compilation fails, point at the full Xcode toolchain:
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer cargo run -p hex-desktop
```

The app attaches to `/tmp/hex-acp.sock` if present, otherwise it spawns
`python -m harness_core.client.cli acp`. The interpreter is chosen from
`HEX_PYTHON_BIN`, then `$VIRTUAL_ENV`, then the repo's `.venv/bin/python`,
then `python3`.

The desktop is built with Zed's **GPUI** framework (`App/hex-desktop`):

```text
┌───────────────────────────┬────────────────────────────────────────────────────────┐
│  HEX DESKTOP  ● ● ●       │  Workspace: /Development/my-app  git:(main)  ● Claude   │
├───────────────────────────┼────────────────────────────────────────────────────────┤
│  [+] New Session (⌘N)     │  ┌──────────────────────────────────────────────────┐  │
│                           │  │ ❯ Add rate-limiting middleware to FastAPI router │  │
│  TODAY                    │  └──────────────────────────────────────────────────┘  │
│  ● Auth Rate Limiter  12m │  ┌─ 💭 Thinking Process (3.2s) ──────────────────────┐ │
│  ● Database Schema    2h  │  │  Analyzing dependencies in pyproject.toml...      │ │
│                           │  └───────────────────────────────────────────────────┘ │
│  YESTERDAY                │  ┌─ 🔧 Tool: run_command (git status) ───────────────┐ │
│  ● Timeline Rewind    1d  │  │  Status: [Completed] · Code: 0                   │ │
│  ● GPUI Port M15      1d  │  └───────────────────────────────────────────────────┘ │
│                           │  ┌─ 📝 Diff: src/router.py ──────────────────────────┐ │
│  WORKSPACE                │  │  + from slowapi import Limiter                    │ │
│  Files: 142               │  │  + limiter = Limiter(key_func=get_remote_address) │ │
│  Sandboxes: 1 active      │  └───────────────────────────────────────────────────┘ │
│                           ├────────────────────────────────────────────────────────┤
│                           │  ❯ Type message...                                ⌘↵   │
│                           │  ▕████████░░░░░░░░░░▏ 42.1k / 200k tok · claude-3-7   │
└───────────────────────────┴────────────────────────────────────────────────────────┘
```

- **Hardware-Accelerated Metal Rendering**: GPU-accelerated GPUI rendering with blurred vibrancy (`WindowBackgroundAppearance::Blurred`) and native macOS traffic lights.
- **Ultra-Lean Resource Footprint**: Native Rust + Metal stack with no bundled browser runtime; memory usage is far below Electron-based desktop apps.
- **Dark Zinc Design Tokens**: Canvas `#09090b`, card `#18181b`, elevated `#27272a`, structural borders `#3f3f46`, sage green diffs `#8fbf93`, rose red diffs `#c98b8b`, electric blue accents `#7aa7ff`.
- **Live Chat Feed**: Streaming replies, tool execution cards with running/completed/failed status, fenced code rendered as code blocks, and explicit cancelled, failed, and disconnected states. Assistant messages show the model the runtime reported, never a guessed one.
- **Composer**: Enter sends, Shift+Enter inserts a newline, `⌘A`/`⌘C`/`⌘X`/`⌘V` edit text, and drafts are kept per session when you switch away and back. `Esc` cancels a running turn.
- **Sessions**: `⌘N` starts a session, the sidebar and palette switch between them, and new sessions get an automatic title from the first prompt.
- **Modals**: Each modal takes keyboard focus while open and hands it back to the composer on close.
  - **Command Palette (`⌘K`)**: Fuzzy search over sessions and actions (new chat, toggle sidebar, clear history). Actions the desktop cannot perform yet say so instead of pretending.
  - **Tool Approval**: Allow once (`y`/`1`), allow for this session (`a`/`2`), or deny (`n`/`3`); decisions are sent to the runtime via `session/approvalDecision`. The 15-minute lease toggle (`l`) is sent along but not yet honored by the server.
  - **Model Picker**: Filterable catalog; the chosen model is used for sessions started afterwards.
- **Message Actions**: Copy a reply to the clipboard or regenerate it from the preceding prompt.
- **ACP Integration (stdio)**: Speaks the canonical `session/update` wire contract shared with the Python server and `hex-tui` (see [ACP Integration](docs/reference/ACP_INTEGRATION.md)).
- **Not yet live**: The token gauge shows a placeholder budget, `<think>` reasoning folds and inline diff views are implemented but not yet fed by ACP events, and Settings (`⌘,`) is not available.

---

## 📟 Native Rust Terminal Cockpit (`hex-tui`)

For terminal purists, HEX includes a blazingly fast Ratatui terminal cockpit (`hex` or `hex tui`):

- **60 FPS Dual-Buffer Rendering**: Ultra-responsive alt-screen terminal experience consuming <20MB of RAM.
- **Natural History Scrolling**: Use `PageUp` / `PageDown` or arrow keys to scroll conversation history directly without leaving your input prompt.
- **Fuzzy Slash Autocomplete**: Type `/` to open fuzzy command suggestions (`/ast` matches `/blast`, `/rb` matches `/rollback`). Press `Tab` to accept suggestions.
- **Side-by-Side Split Diff (`/sbs`)**: Toggle synchronized two-column split diff views alongside unified diffs.
- **Light & Dark Terminal Skins**: 7 themes featuring signature **Sapphire** monochrome (`sapphire` [default], `amber`, `cyberpunk`, `matrix`, `nord`, `mono`, `light`) with automatic background luminance detection.
- **Interactive Security Approvals**: Centered modal overlay for high-risk operations (e.g. git pushes) with action keys (`[y] Approve`, `[n] Reject`, `[d] Diff`).

---

## 🎯 The 3 Modes: `agent`, `plan`, `code`

HEX separates developer intent into three focused interaction modes. Press **`Tab`** on an empty prompt line to cycle between them instantly:

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│ ❯ Ask me anything...                                                        │
╰─ agent · my-project · main · claude-3-7-sonnet ────── enter send · Tab mode ╯
```

| Mode | Visual Badge | Best For | What Happens |
| :--- | :--- | :--- | :--- |
| **`agent`** | **Cyan / White** | Bug fixes, new features, multi-file refactoring | **Full Autonomy**. Edits code inside isolated sandboxes, runs tests, inspects diagnostics, and iterates until passing. |
| **`plan`** | **Golden / Amber** | Architecture, design reviews, code explanations | **Read-Only Guardrails**. Inspects files and AST relationships without modifying a single line of code. |
| **`code`** | **Purple** | Direct code snippets, targeted patch creation | **Direct Synthesis**. Fast targeted code generation without conversational overhead. |

> **Mode Commands:** Switch modes anytime using `/agent`, `/plan`, `/code`, or `/mode`.

---

## ⏪ Conversational Timeline Rewind & Soft Branching

Ever have an AI agent take a wrong path 5 turns into an architectural refactoring? Traditional agents make you delete your entire session and start over.

HEX features **Conversational Timeline Rewind** (`/rewind`, available in the Python console `hex chat`):
- **Turn-Targeted Rollback**: Execute `/rewind 3` or `/rewind -2` to rewind dialogue buffers back to any previous turn.
- **Soft Branching in SQLite**: Historical turns are never deleted; instead, they are preserved under a unique `branch_id` in the `tasks` table with cryptographic `TIMELINE_REWOUND` audit events.
- **CoW Sandbox Synchronization**: Sandbox file modifications and step journals are reset in sync with conversation turns, giving you a clean slate to steer the agent in a new direction.

---

## 🤝 Mixture-of-Agents (MoA) Multi-Model Synthesis

For critical architectural decisions, schema migrations, or subtle concurrency bugs, single models often suffer from blind spots.

HEX ships a **Mixture-of-Agents** engine (`harness_core.core.moa`) that runs inside the ReAct loop when enabled via configuration:
- **Parallel Proposer Generation**: Queries multiple diverse frontier models (e.g. Claude 3.7 Sonnet, GPT-4o, Gemini 2.5 Pro) in parallel scratchpads.
- **Aggregator Synthesis**: Feeds candidate proposals and critiques to an aggregator model that cross-examines edge cases and compiles the optimal solution.
- **Resilient Fallback**: If any secondary proposer times out or encounters rate limits, the aggregator seamlessly synthesizes available proposals without stalling the interaction.
- **Ledger Evidence**: MoA synthesis steps and token metrics are recorded as `mixture_of_agents` tool events in the proof ledger.

> **Status:** The engine is configuration-driven today. A `/moa` slash-command toggle is not yet implemented on any client surface.

---

## ⏱️ Temporal Auto-Approval Leases (`/lease`)

Eliminate prompt fatigue during extended autonomous workflows without sacrificing security (available today in the Python console, `hex chat`):
- **Time-Bounded Leases**: Use `/lease grant npm test 3600` to grant temporal auto-approvals for specific command prefixes or directory paths.
- **Automatic Expiration**: Leases automatically expire after their TTL, restoring default confirmation prompts.
- **Desktop UI Integration (partial)**: The Desktop Tool Approval Modal's lease toggle sends a `lease_duration:900` reason with the decision, but the server does not yet grant a lease from it.

---

## 🛡️ Multi-Key Credential Pool & HTTP 429 Resilience

Production agent reliability requires resilient upstream connectivity:
- **Multi-Key Ingestion**: Configure multiple API keys per provider (e.g. `ANTHROPIC_API_KEY_1`, `ANTHROPIC_API_KEY_2`) with round-robin or least-used balancing.
- **Intelligent 429 Failover**: Automatically rotates keys when HTTP 429 (Rate Limited) errors occur, calculating jittered exponential backoff based on upstream `Retry-After` headers.
- **Model Cooldown Queues**: Places throttled `(provider, model)` pairs on cooldown queues without blocking other models.
- **30-Second Endpoint Blackholing**: Temporarily blackholes unreachable or failing endpoints for 30s to prevent request pile-ups.
- **Local Socket Probing**: Automatically detects and queries local inference engines on `localhost` (Ollama `/api/show`, LM Studio `/api/v0/models`, llama.cpp `/props`, vLLM `/v1/models`).
- **OS Keychain Vault**: Store sensitive credentials securely in your operating system's native keychain (`keyring`).

---

## 🏗️ Clean Subpackage Architecture

HEX uses an open repository-root layout: Python domains are visible at the root, while the public package namespace remains `harness_core`. Representative layout:

```text
Harness/
├── harness_core/    # Package anchor: __init__.py, shared configuration/provider/gateway modules, SQL migrations
├── agent/           # Orchestrator, ReAct loop, router, subagents, MoA, approval leases
├── storage/         # DB, CoW sandboxes, rewind, GC, Keychain, project_init.py
├── cognition/       # Memory, reflections, SkillSpector, title synthesizer, repetition sentry
├── runtime/         # Process registry, code execution kernels, daemon, task scheduler
├── hex_cli/         # CLI, ACP, TUI, onboarding; commands/, terminal/, onboarding/ helpers
├── hex_platform/    # hardware.py, diagnostics.py
├── tools/           # filesystem.py implementation behind the gateway
├── skill_engine/    # Python skill compiler, execution, and retrieval engine
├── resources/       # Resource package support, not skill-engine code
├── skills/builtin/  # Packaged built-in skill data
├── optional-skills/ # Packaged optional skill data
├── capabilities/    # Browser, vision, voice, search, git autopilot, GitHub actions, gateways
├── providers/       # Provider integrations
├── models/          # Model metadata, credential pool, provider routing
├── lsp/             # Language-server integration
├── gateways/        # Gateway integration
├── App/hex-desktop/ # Experimental macOS GPUI desktop crate
├── crates/          # hex-protocol and hex-tui; root Cargo workspace remains
└── tests/           # Python tests grouped by subsystem
```

Supported setuptools `package_dir` mappings connect these physical directories to the unchanged `harness_core.*` imports and CLI entry points (for example, `harness_core.core` maps to `agent/` and `harness_core.client` to `hex_cli/`). The `harness_core/` anchor contains substantive shared modules, not forwarding wrappers. Root manifests, installers, assets, and shared test fixtures stay in place; this relocation does not imply a wholesale split of large core or storage modules.

For development, install from the repository root into an activated virtual environment:

```bash
python -m pip install -e ".[full]"
# Desktop development (macOS; requires the Metal/Xcode toolchain):
cargo run -p hex-desktop
```

Re-run the editable install after the relocation, even in an existing environment, to refresh setuptools' editable package mappings. Adding the repository root to `PYTHONPATH` is not a substitute for this install. See [Contributing](docs/development/CONTRIBUTING.md) for setup and tests.

---

## 🤖 26 Supported Providers & Universal Model Routing

HEX is completely provider- and model-agnostic. Switch models instantly on the fly with `/model` or `--provider`:

- **Frontier & Multi-Model**: Google Antigravity (Included quota via OAuth), Anthropic (Claude 3.7 Sonnet / 3.5 Sonnet / Haiku), OpenAI (GPT-4o, o1, o3-mini), Google Gemini (Gemini 2.5 Pro / Flash), DeepSeek (V3 & R1), OpenRouter, xAI Grok.
- **High-Speed Open-Weight Cloud**: Groq LPU (Llama 3.3 70B, QwQ 32B), Cerebras (2,000+ tok/s), Mistral AI (Codestral, Mistral Large), Together AI, Fireworks AI, NVIDIA NIM, Alibaba Qwen (Qwen 2.5 Coder), MiniMax (1M context), Kimi / Moonshot, Z.AI / GLM.
- **Enterprise Hyperscalers**: Microsoft Azure OpenAI & Foundry, AWS Bedrock, Google Cloud Vertex AI.
- **Aggregators & Gateways**: Vercel AI Gateway, Hugging Face Serverless.
- **100% Private Offline Local AI**: Ollama (`localhost:11434`), LM Studio (`localhost:1234`), native Apple Silicon Metal acceleration via MLX, and custom OpenAI-compatible endpoints (vLLM, SGLang, LocalAI).

> 📖 **Full Catalog & Configs:** See [`docs/reference/SUPPORTED_MODELS.md`](docs/reference/SUPPORTED_MODELS.md) for the complete directory of 26 providers, model IDs, and environment variables.

---

## 🧠 Next-Gen Cognition & Self-Evolution

Traditional AI coding agents either dump flat bullet lists into system prompts until context runs out, or lose memory the moment you restart your terminal.

HEX replaces flat memory files with an active **Cognitive Self-Evolution Engine** designed for long-lived codebases and local hardware:

```text
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                            HEX COGNITIVE LOOP                               │
  │                                                                             │
  │   [ ~/.hex/USER.md ]       [ .hex/MEMORY.md ]       [ .hex/GUARDRAILS.md ]  │
  │      Machine DNA              Repo Invariants           Negative [AVOID]    │
  │           │                          │                         │            │
  │           └───────────┬──────────────┴─────────────────────────┘            │
  │                       ▼                                                     │
  │            Slot #4: Turn-0 Frozen System Prompt (100% KV-Cache Invariant)   │
  │                       │                                                     │
  │       [ Tool Read ]   ▼   [ Tool Edit ]                                     │
  │    JIT AST Node Gotchas Injected into Tool Output (Keeps prompt frozen!)     │
  │                       │                                                     │
  │   [ Friction Detected: ≥3 retries ] ──► Synthesizes .hex/reflections/       │
  │   [ Deterministic Skill Compiler  ] ──► Generates SKILL.md with `verify:`   │
  │   [ NVIDIA SkillSpector AST Gate  ] ──► Quarantines Malicious/Unvetted Code │
  └─────────────────────────────────────────────────────────────────────────────┘
```

1. **AST-Scoped Memory Injection (JIT Graph Retrieval)**: File- and function-specific gotchas are injected directly into `read_file` tool observations—never polluting the system prompt. System prompt stays tiny and **100% KV-cache frozen**.
2. **Negative Constraint Memory ("Anti-Patterns & Guardrails")**: Automatically writes `[AVOID]` invariants to `.hex/memories/GUARDRAILS.md` when tool executions or tests fail repeatedly, cutting repeat hallucination loops.
3. **Zettelkasten Linked Memory & Contradiction Resolution**: Follows the A-MEM pattern (`ADD`, `UPDATE`, `DELETE`, `NOOP`) to update obsolete facts rather than accumulating conflicting bullet points.
4. **Confidence Decay & Lifecycle**: Automatic promotion of proven reflection lessons to `MEMORY.md` and demotion of cold knowledge (>30d unreferenced) to SQLite FTS5 archival search.
5. **Deterministic Verification Hooks on Skills**: Learned skills in `SKILL.md` carry executable `verify:` shell checks, turning documentation into automated regression tests.
6. **Repetition Sentry & `<think>` Tag Scrubber**: Automatically detects token repetition and tool thrashing with out-of-band steering injection, and purifies reasoning artifacts from model responses.

---

## ⌨️ Daily Commands

Slash commands differ by surface. Availability is declared once in
[`hex_cli/command_catalog.json`](hex_cli/command_catalog.json) —
the single source of truth that drives console help/completion, the Rust TUI's
embedded autocomplete, and ACP dispatch (consistency-tested on both sides).
The table below is a human-friendly summary of that catalog.

| Command | Action | Python console | Rust TUI | ACP (editors) |
| :--- | :--- | :---: | :---: | :---: |
| **`/help`** | Display full table of slash commands | ✅ | ✅ | ✅ |
| **`/status`** | Inspect session and task state | ✅ | ✅ | ✅ |
| **`/model`** | Inspect active model routing profile or switch providers | ✅ | ✅ | ✅ |
| **`/budget`** | Inspect spend against configured budget | ✅ | ✅ | ✅ |
| **`/quota`** | Inspect provider quota windows | ✅ | ✅ | ✅ |
| **`/doctor`** | Run system health diagnostics and repair configurations | ✅ | ✅ | ✅ |
| **`/diff`** | View unified colorized diff of uncommitted sandbox changes | ✅ | ✅ | ✅ |
| **`/sbs`** | Side-by-side split diff view | ✅ | ✅ | ✅ |
| **`/proof`** | View test results and cryptographic proof for the completed task | ✅ | ✅ | ✅ |
| **`/blast`** | Inspect blast-radius analysis of pending changes | ✅ | ✅ | ✅ |
| **`/rollback`** | Discard sandbox changes and reset to active branch state | ✅ | ✅ | ✅ |
| **`/fork`** | Spawn speculative branches for a task | ✅ | ✅ | ✅ |
| **`/skills`** | List registered skill bundles | ✅ | ✅ | ✅ |
| **`/scan`** | Scan and register a SKILL.md directory via SkillSpector | ✅ | ✅ | ✅ |
| **`/memory`** | Inspect governed memory notes and guardrails | ✅ | ✅ | ✅ |
| **`/remember`** | Write a memory note | ✅ | ✅ | ✅ |
| **`/reflect`** | Trigger reflection synthesis | ✅ | ✅ | ✅ |
| **`/search`** | Full-text search across ledger tasks | ✅ | ✅ | ✅ |
| **`/subagents`** | Inspect active and completed child subagents | ✅ | ✅ | ✅ |
| **`/react [on\|off]`** | Toggle continuous multi-turn autonomous ReAct tool loop | ✅ | ✅ | ✅ |
| **`/recipe`** | Detect project build/test recipe | ✅ | ✅ | ✅ |
| **`/lsp`** | Inspect language-server status | ✅ | ✅ | ✅ |
| **`/tools`** | List gateway tools | — | ✅ | — |
| **`/clear`** | Clear the visible conversation buffer | ✅ | ✅ | — |
| **`/quit`** | Exit the console | ✅ | ✅ | — |
| **`/mode`** | Cycle interaction mode (`agent`, `plan`, `code`) | ✅ | ✅ | — |
| **`/plan`**, **`/agent`**, **`/code`** | Jump straight to a specific interaction mode | ✅ | ✅ | — |
| **`/compressor [mode]`** | Inspect or switch the context compressor mode | ✅ | ✅ | ✅ |
| **`/rewind <turn>`** | Roll back conversation history and sandbox state to a previous turn | ✅ | — | — |
| **`/lease grant <prefix> [ttl]`** | Grant temporary auto-approval lease for trusted commands or paths | ✅ | — | — |
| **`/gc`** | Run on-demand storage garbage collection sweep | ✅ | — | — |
| **`/learn [target]`** | Compile reusable verified `SKILL.md` bundle with regression hooks | ✅ | — | — |
| **`/rename <name>`** | Set persona or agent name across session and global `SOUL.md` | ✅ | — | — |
| **`/skin`** | Switch color theme (`sapphire`, `amber`, `cyberpunk`, `matrix`, `nord`, `mono`, `light`) | ✅ | — | — |

> The GPUI desktop cockpit is experimental and does not yet expose slash commands (see the desktop section above). `/moa` is not implemented on any surface; MoA runs via configuration in the ReAct loop.

---

## 📚 Documentation Library

For comprehensive guides, architecture deep-dives, and production integrations:

- 📖 **[Operator Runbook](docs/guides/RUNBOOK.md)** — Production operations, CLI command reference, desktop build instructions, database backups, disaster recovery, and storage GC maintenance.
- 🏗️ **[System Architecture](docs/architecture/ARCHITECTURE.md)** — GPUI Metal desktop architecture, Copy-on-Write sandboxing, Python package layout, cryptographic SHA-256 chain, and AST code intelligence.
- 🤖 **[Supported Models & 26 Providers](docs/reference/SUPPORTED_MODELS.md)** — Complete catalog of 26 providers, curated model IDs, and environment variables.
- 🤖 **[Sub-Agent Delegation](docs/guides/SUBAGENTS.md)** — Hierarchical child workers, role-based tool gating, and sandbox scratchpads.
- 🌐 **[Browser & Vision](docs/guides/BROWSER_AND_VISION.md)** — Headless browser automation, `ariaSnapshot` semantic DOM trees, and visual regression testing.
- 🔌 **[IDE Integration (ACP)](docs/reference/ACP_INTEGRATION.md)** — Configure HEX inside Zed, Neovim (`avante.nvim`), and VS Code via the Agent Client Protocol.
- 🤝 **[Contributing Guide](CONTRIBUTING.md)** — Development setup, running benchmark evaluations (`hex eval`), and pre-commit checks.

---

<div align="center">

Crafted with care by [**anv.dev**](https://anvv.tech) · Licensed under [Apache-2.0](LICENSE)

</div>

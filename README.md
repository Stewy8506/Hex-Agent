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

### The Autonomous AI Terminal Workbench That Tests Before It Touches Your Code

[![Version](https://img.shields.io/badge/version-0.21.0-ffb703?style=flat-square&logo=semanticrelease&logoColor=white)](pyproject.toml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux%20%7C%20Windows-blueviolet?style=flat-square)](install.sh)
[![Tests Passing](https://img.shields.io/badge/tests-848%20passing%20(100%25)-10b981?style=flat-square&logo=pytest&logoColor=white)](tests/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square)](LICENSE)

<p align="center">
  <a href="#-quick-install"><strong>Quick Install</strong></a> ·
  <a href="#-getting-started-in-3-steps"><strong>Getting Started</strong></a> ·
  <a href="#-the-3-modes-agent--plan--code"><strong>The 3 Modes</strong></a> ·
  <a href="#-terminal-cockpit"><strong>Terminal Cockpit</strong></a> ·
  <a href="#-supported-models"><strong>Models</strong></a> ·
  <a href="#-daily-commands"><strong>Commands</strong></a> ·
  <a href="#-documentation-library"><strong>Documentation</strong></a>
</p>

</div>

---

### What is HEX?

**HEX** is an autonomous AI pair-programmer in your terminal designed to make AI coding reliable, fast, and safe.

Most AI coding assistants edit your files in place. If an agent hallucinates or produces broken syntax, your working branch is trashed, git history is cluttered with dirty diffs, and you spend valuable time cleaning up.

**HEX does the opposite:**
- **Zero-Risk Scratchpad**: Every code change and test runs inside a high-speed, isolated sandbox.
- **Proof Before Commit**: HEX executes your project's test suite. Only when tests pass are patches applied to your code.
- **Model Freedom**: Connect directly to your existing Google Antigravity OAuth quota ($0 marginal billing), frontier cloud models (Claude 3.7 Sonnet, GPT-4o, Gemini 2.5 Pro), or run 100% offline with local models (Ollama, Apple Metal, NVIDIA CUDA).

<table>
<tr><td><b>Isolated Sandboxing</b></td><td>Instant Copy-on-Write staging (macOS APFS / Linux OverlayFS in &lt;10ms). Broken patches are discarded with zero host pollution.</td></tr>
<tr><td><b>Proof Before Commit</b></td><td>Verifies tests and diagnostics inside the sandbox before touching a single line of your working branch.</td></tr>
<tr><td><b>Native Rust Cockpit</b></td><td>Ultra-responsive 60 FPS Ratatui terminal interface with multiline editing, fuzzy slash autocomplete, and side-by-side split diffs.</td></tr>
<tr><td><b>Model Independence</b></td><td>Use any cloud provider or local offline LLM. Switch anytime with <code>/model</code>—no code changes, zero lock-in.</td></tr>
<tr><td><b>Editor Integration</b></td><td>Built-in Agent Client Protocol (ACP) server for seamless use inside Zed, Neovim, and VS Code.</td></tr>
<tr><td><b>Headless Browser & Vision</b></td><td>Automate web testing, inspect DOM accessibility hierarchies, and evaluate visual UI regressions directly from the CLI.</td></tr>
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

---

## 🚀 Getting Started in 3 Steps

### 1. Launch in Any Repository
Navigate to any codebase on your machine and launch HEX:

```bash
cd your-project
hex
```

### 2. Choose Your Model (One-Time Setup)
On your very first run, HEX opens an interactive terminal setup menu:

```text
◈ Welcome to HEX — Autonomous AI Workbench
  Select your primary model brain:

  [•] Google Antigravity OAuth ($0 Day-1 Free Tier / Auto-detects IDE session)
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

> **Mode Commands:** You can also switch modes anytime using `/agent`, `/plan`, `/code`, or `/mode`.

---

## 🖥️ Terminal Cockpit

HEX includes a native Rust Ratatui cockpit (`hex tui`) designed for high-performance terminal workflows:

- **60 FPS Dual-Buffer Rendering**: Ultra-responsive alt-screen terminal experience consuming <20MB of RAM.
- **Natural History Scrolling**: Use `PageUp` / `PageDown` or arrow keys to scroll conversation history directly without leaving your input prompt.
- **Fuzzy Slash Autocomplete**: Type `/` to open fuzzy command suggestions (`/ast` matches `/blast`, `/rb` matches `/rollback`). Press `Tab` to accept suggestions.
- **Side-by-Side Split Diff (`/sbs`)**: Toggle synchronized two-column split diff views alongside unified diffs.
- **Light & Dark Terminal Skins**: 6 themes (`amber`, `cyberpunk`, `matrix`, `nord`, `mono`, `light`) with automatic background luminance detection.
- **Policy Approvals**: Centered modal overlay for high-risk operations (e.g. git pushes) with action keys (`[y] Approve`, `[n] Reject`, `[d] Diff`).

---

## 🤖 26 Supported Providers & Universal Model Routing

HEX is completely provider- and model-agnostic. Switch models instantly on the fly with `/model` or `--provider`:

- **Frontier & Multi-Model**: Google Antigravity ($0 Day-1 marginal billing via OAuth), Anthropic (Claude 3.7 Sonnet / 3.5 Sonnet / Haiku), OpenAI (GPT-4o, o1, o3-mini), Google Gemini (Gemini 2.5 Pro / Flash), DeepSeek (V3 & R1), OpenRouter, xAI Grok.
- **High-Speed Open-Weight Cloud**: Groq LPU (Llama 3.3 70B, QwQ 32B), Cerebras (2,000+ tok/s), Mistral AI (Codestral, Mistral Large), Together AI, Fireworks AI, NVIDIA NIM, Alibaba Qwen (Qwen 2.5 Coder), MiniMax (1M context), Kimi / Moonshot, Z.AI / GLM.
- **Enterprise Hyperscalers**: Microsoft Azure OpenAI & Foundry, AWS Bedrock, Google Cloud Vertex AI.
- **Aggregators & Gateways**: Vercel AI Gateway, Hugging Face Serverless.
- **100% Private Offline Local AI**: Ollama (`localhost:11434`), LM Studio (`localhost:1234`), native Apple Silicon Metal acceleration via MLX, and custom OpenAI-compatible endpoints (vLLM, SGLang, LocalAI).

> 📖 **Full Catalog & Configs:** See [`docs/SUPPORTED_MODELS.md`](docs/SUPPORTED_MODELS.md) for the complete directory of 26 providers, model IDs, and environment variables.

---

## ⚡ Built-in Power Capabilities

Beyond conversational coding, HEX includes production engineering superpowers built directly into the core:

- **80+ Built-in Skills & NVIDIA SkillSpector**: Over 80 modular capabilities spanning software development, GitHub automation, Apple ecosystems, MLOps, and productivity. Third-party skills are scanned for AST vulnerabilities and OSV malware before execution (`hex scan-skill <dir>`).
- **Graphify AST Knowledge Graph**: Polyglot code graph (Python, TypeScript, Rust, Go, C++) indexing functions, classes, and call hierarchies. Inspect blast-radius before editing (`hex graphify affected <symbol>`) or export interactive D3 architecture trees (`hex graphify tree`).
- **Autonomous Git Autopilot**: Automatically branches, stages atomic commits, and creates formatted GitHub Pull Requests upon task completion (`hex run "Goal" --pr`).
- **Polyglot Language Server Diagnostics**: Real-time compiler syntax errors, type mismatches, and hover documentation (`hex lsp diagnostics`).
- **Hands-Free Voice Console**: Interactive spoken pair-programming with real-time vocal feedback (`hex voice` or `hex --voice`).
- **Self-Healing Diagnostics**: Inspect system health, verify virtual environment integrity, and automatically repair configurations (`hex doctor --fix`).
- **Closed-Loop Autonomous Learning & Memory**: Synthesize reusable skills with deterministic `verify:` hooks (`hex learn <target>`), detect repair friction to auto-generate post-mortems in `.hex/reflections/LESSONS.md`, inject symbol invariants via AST-scoped memory, and enforce negative guardrails (`GUARDRAILS.md`).

---

## ⌨️ Daily Commands

Inside the interactive console, type `/` to access commands:

| Command | Action |
| :--- | :--- |
| **`/mode`** | Cycle interaction mode (`agent`, `plan`, `code`) — or press `Tab` on empty line |
| **`/diff`** | View unified colorized diff of uncommitted sandbox changes |
| **`/sbs`** | Toggle side-by-side terminal split diff view |
| **`/proof`** | View test results and cryptographic proof for the completed task |
| **`/subagents`** | Inspect active and completed child subagents |
| **`/react [on\|off]`** | Toggle continuous multi-turn autonomous ReAct tool loop |
| **`/rollback`** | Discard sandbox changes and reset to active branch state |
| **`/learn [target]`** | Compile reusable verified `SKILL.md` bundle with regression test hooks |
| **`/memory`** | Inspect active notes, negative guardrails, and rolling snapshot history |
| **`/rename <name>`** | Set persona or agent name across session and global `SOUL.md` |
| **`/model`** | Inspect active model routing profile or switch providers |
| **`/doctor`** | Run system health diagnostics and repair configurations |
| **`/skin`** | Switch color theme (`amber`, `cyberpunk`, `matrix`, `nord`, `mono`, `light`) |
| **`/help`** | Display full table of slash commands |

---

## 📚 Documentation Library

For comprehensive guides, architecture deep-dives, and production integrations:

- 🤖 **[Supported Models & 26 Providers](docs/SUPPORTED_MODELS.md)** — Complete catalog of 26 providers, curated model IDs, and environment variables.
- 🏗️ **[System Architecture](docs/ARCHITECTURE.md)** — Copy-on-Write sandboxing, PostgreSQL/SQLite dual-driver storage, cryptographic hash chaining, and AST code intelligence.
- ⚖️ **[Harness Comparison](docs/COMPARISON.md)** — In-depth architectural comparison against Hermes Agent, OpenClaw, Claude Code, and Aider.
- 🤖 **[Sub-Agent Delegation](docs/SUBAGENTS.md)** — Hierarchical child workers, role-based tool gating, and sandbox scratchpads.
- 🌐 **[Browser & Vision](docs/BROWSER_AND_VISION.md)** — Headless browser automation, `ariaSnapshot` semantic DOM trees, and visual regression testing.
- 🔌 **[IDE Integration (ACP)](docs/ACP_INTEGRATION.md)** — Configure HEX inside Zed, Neovim (`avante.nvim`), and VS Code via the Agent Client Protocol.
- 🤝 **[Contributing Guide](docs/CONTRIBUTING.md)** — Development setup, running benchmark evaluations (`hex eval`), and pre-commit checks.

---

<div align="center">

Crafted with care by [**anv.dev**](https://anvv.tech) · Licensed under [Apache-2.0](LICENSE)

</div>

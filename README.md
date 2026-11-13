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

[![Version](https://img.shields.io/badge/version-0.20.1-ffb703?style=flat-square&logo=semanticrelease&logoColor=white)](pyproject.toml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux%20%7C%20Windows-blueviolet?style=flat-square)](install.sh)
[![Tests Passing](https://img.shields.io/badge/tests-passing%20(100%25)-10b981?style=flat-square&logo=pytest&logoColor=white)](tests/)
[![Day-1 Antigravity](https://img.shields.io/badge/quota-Google%20Antigravity%20OAuth-4285F4?style=flat-square&logo=google&logoColor=white)](src/harness_core/providers/antigravity_provider.py)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square)](LICENSE)

<p align="center">
  <a href="#-quickstart-in-3-steps"><strong>Quickstart</strong></a> ·
  <a href="#-the-3-modes-plan--agent--code"><strong>The 3 Modes</strong></a> ·
  <a href="#-the-terminal-cockpit"><strong>Terminal Cockpit</strong></a> ·
  <a href="#-sub-agent-delegation-tier-c"><strong>Sub-Agents</strong></a> ·
  <a href="#-headless-browser--multimodal-vision-tier-b"><strong>Browser & Vision</strong></a> ·
  <a href="#-agent-client-protocol-acp-for-ides-tier-a"><strong>IDE Integration</strong></a> ·
  <a href="#-slash-command-reference"><strong>Commands</strong></a>
</p>

</div>

---

### What is HEX?

**HEX** is a developer's autonomous AI pair-programmer in the terminal.

Most AI coding assistants edit your files in place. If they get stuck in a hallucination loop, your working branch is trashed, uncommitted changes are mixed up, and you spend the next hour cleaning up git diffs.

**HEX does the opposite:**
1. **Isolated Sandboxing**: Every edit and test runs in an instant copy-on-write scratchpad.
2. **Proof Before Commit**: HEX runs your actual test suite. If the patch passes, it applies cleanly. If it fails, your code is never touched.
3. **Multi-Model Freedom**: Connect your existing Google Antigravity OAuth quota ($0 marginal billing), frontier cloud models (Claude 3.7 Sonnet, GPT-4o), or run completely offline with Local AI (Apple Metal, NVIDIA CUDA, Ollama).

---

## ⚡ See It in Action

<div align="center">
  <img src="assets/hex-tui-demo.svg" alt="HEX Interactive Terminal TUI Demo" width="100%" />
</div>

---

## 🚀 Quickstart in 3 Steps

### 1. Install HEX

Run the one-line universal installer (macOS & Linux):

```bash
curl -fsSL https://raw.githubusercontent.com/Stewy8506/Harness/main/install.sh | bash
```

*Or install with `pipx` or `uv tool` (isolated, zero dependency conflicts):*

```bash
pipx install "git+https://github.com/Stewy8506/Harness.git[tui]"
# or
uv tool install "git+https://github.com/Stewy8506/Harness.git[tui]"
```

---

### 2. Launch in Any Repository

Navigate to any codebase and launch HEX:

```bash
cd your-project
hex
```

---

### 3. Connect Your Model (One-Time Setup)

On your very first run, HEX automatically opens an interactive terminal menu:

```text
◈ Welcome to HEX — Autonomous AI Workbench
  Select your primary model brain:

  [•] Google Antigravity OAuth ($0 Day-1 Free Tier / Auto-detects IDE session)
  [ ] Anthropic Claude (Claude 3.7 Sonnet / Opus)
  [ ] OpenAI (GPT-4o / o3-mini)
  [ ] Google Gemini API (Gemini 2.5 Pro / Flash)
  [ ] Local Offline Model (Ollama / Apple Metal / NVIDIA CUDA / CPU)
```

> **Tip:** If you already use Google Antigravity, HEX auto-detects your active session with zero API keys required.

---

## 🎯 The 3 Modes: `plan`, `agent`, `code`

HEX separates developer intent into three focused interaction modes. Press **`Tab`** on an empty prompt line to cycle between them instantly:

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│ ❯ Ask me anything...                                                        │
╰─ plan · Harness · main · gemini-3.8-flash-tiered ── enter send · Tab mode ──╯
```

| Mode | Visual Badge | Best For | What Happens |
| :--- | :--- | :--- | :--- |
| **`plan`** | **Golden / Amber** | Architecture, design reviews, code explanations, refactoring specs | **Read-Only Guardrails**. Fast-path analysis. Inspects files, AST, and dependencies without modifying a single line of code. |
| **`agent`** | **Cyan / White** | Bug fixing, new features, multi-file refactoring, autonomous repair | **Full Autonomy**. Edits code inside isolated sandboxes, runs your test suites, inspects diagnostics, and iterates until tests pass. |
| **`code`** | **Purple** | Direct code generation, targeted snippets, surgical patch creation | **Direct Synthesis**. Fast targeted code edits without conversational overhead. |

> **Mode Commands:** You can also switch modes anytime using `/plan`, `/agent`, `/code`, or `/mode`.

---

## 🖥️ The Terminal Cockpit

 HEX features an unboxed, distraction-free terminal layout:

```text
    did you know · Press Tab on an empty line to cycle between plan, agent, and code modes.

           ██╗  ██╗███████╗██╗  ██╗          █████╗  ██████╗ ███████╗███╗   ██╗████████╗            
           ██║  ██║██╔════╝╚██╗██╔╝         ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝            
           ███████║█████╗   ╚███╔╝  █████╗  ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║               
           ██╔══██║██╔══╝   ██╔██╗  ╚════╝  ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║               
           ██║  ██║███████╗██╔╝ ██╗         ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║               
           ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝         ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝               
                                              anv.dev                                               

                ▄▄▀▀▀▀▀▀▄▄                HEX Autonomous AI Workbench  v0.20.1  (upstream 97a6ff2)
             ▄▀▀  ▄▄▄▄▄▄  ▀▀▄             gemini-3.8-flash-tiered · Google Antigravity
          ▄▀▀  ▄▀▀      ▀▀▄  ▀▀▄          ~/Development/Harness
        ▄▀   ▄▀    ▄▄▄▄    ▀▄   ▀▄        Session: 5f64fe
       ▐▌   ▐▌   ▄▀    ▀▄   ▐▌   ▐▌
       ▐▌   ▐▌  ▐▌  ██  ▐▌  ▐▌   ▐▌       tools
       ▐▌   ▐▌  ▐▌ ████ ▐▌  ▐▌   ▐▌         file: list_files, patch, read_file, write_file
       ▐▌   ▐▌   ▀▄ ▀▀ ▄▀   ▐▌   ▐▌         search: search_files, web_extract, web_search
        ▀▄   ▀▄    ▀▀▀▀    ▄▀   ▄▀          lsp_compiler: lsp_definition, lsp_diagnostics, lsp_hover, lsp_references
          ▀▄▄  ▀▄▄      ▄▄▀  ▄▄▀            ast_graph: query_graphify
            ▐▌ ▀▀▄▄▀▀▀▀▄▄▀▀ ▐▌              shell_exec: process, terminal
            ▐▌     ▀▀▀▀     ▐▌              verification: run_tests
             ▀▄▄          ▄▄▀               +6 more
               ▀▀▄▄    ▄▄▀▀           
                  ▀▀▀▀▀▀                  26 tools · 83 skills · /help
              ───═══╬═══───           
                                          type a message   /help for commands
```

### Highlights:
- **No Clutter, No Cages**: Floating negative space without rigid double boxes.
- **Docked Input Frame**: Visual status pills for active mode, project directory, git branch, and active model.
- **Dynamic Ghost Suggestions**: Rotating prompt ideas that clear the moment you start typing.
- **5 Terminal Themes**: Switch anytime with `/skin <name>` (`amber`, `cyberpunk`, `matrix`, `nord`, `mono`).

### 🦀 Native Rust Ratatui Cockpit (`hex tui` / `hex chat --ratatui`)

For an ultra-responsive 60 FPS alt-screen terminal experience with dual-buffer differential rendering:

```bash
# Launch native Ratatui cockpit (builds on-demand with cargo if not compiled)
hex tui

# Or start chat with the native Ratatui backend
hex chat --ratatui

# Headless verification / self-check (exits 0)
hex tui --check
```

- **Sticky Status Telemetry**: Live model routing, CoW snapshot mode, monotonic fencing token, and ACP connection status.
- **Scrollable Dialogue & Collapsible Tool Calls**: Role badges (`[USER]`, `[ASSISTANT]`, `[SYSTEM]`, `[TOOL]`), expandable tool invocations with millisecond execution durations and status indicators.
- **Unified Diff Viewer**: Colorized split view rendering added, deleted, and context lines alongside conversation history.
- **Interactive Slash Commands**: Dynamic floating popup menu above input prompt when typing `/`.
- **R3/R4 Policy Approval Intercepts**: Centered modal overlay for operator approvals with risk classifications and action buttons (`[y] Approve`, `[n] Reject`, `[d] Diff`, `[Esc] Dismiss`).

---

## 💡 Why HEX Feels Different

```
┌───────────────────────────────┐      ┌───────────────────────────────┐
│     Typical AI Agents         │      │      HEX AI Workbench         │
├───────────────────────────────┤      ├───────────────────────────────┤
│ ❌ Edits live files in place   │      │  Edits in Copy-on-Write CoW   │
│ ❌ Dumps 50KB test error logs │      │  Compresses logs to core diff │
│ ❌ Locks you to one provider   │      │  Antigravity, Claude, Local   │
│ ❌ Context rots as code moves  │      │  AST memory staleness checks  │
│ ❌ Re-runs crashed tool calls │      │ ⚡ 0ms replay from journal     │
└───────────────────────────────┘      └───────────────────────────────┘
```

### 1. Copy-on-Write Sandbox (Your Repo Stays Safe)
When you ask HEX to fix a bug or add a feature, it clones your repository state in **<10ms** using native filesystem Copy-on-Write (APFS on macOS, OverlayFS on Linux). It writes changes and executes your test runner inside the sandbox. Only when verification succeeds are the changes applied to your actual workspace.

### 2. Smart Log & Context Compression
Nothing burns tokens faster than a 500-line pytest traceback or large build logs. HEX’s deterministic log compressor extracts the exact failing assertions and line diffs while dropping redundant system trace noise—saving **75% to 88%** of your context window.

### 3. AST-Aware Precision
Instead of blindly shoving entire 1,500-line files into the prompt, HEX’s code graph indexes functions, classes, and cross-file imports. It extracts only the relevant function bodies and their direct callers, keeping prompts laser-focused and responses accurate.

### 4. Zero-Friction Antigravity Integration
HEX was designed from Day 1 to connect to **Google Antigravity OAuth**. If you have an active Antigravity session, you get full access to leading frontier reasoning with zero marginal API charges.

---

## 🤖 Supported Models & Local AI

HEX is completely model-agnostic. Switch models on the fly with `/model`:

### Cloud Frontiers
- **Google Antigravity**: Included quota routing ($0 marginal billing).
- **Anthropic**: Claude 3.7 Sonnet, Claude 3.5 Sonnet, Claude 3.5 Haiku, Claude 3 Opus.
- **OpenAI**: GPT-4o, GPT-4o-mini, o1, o3-mini.
- **Google Gemini**: Gemini 2.5 Pro, Gemini 2.5 Flash.

### Cross-Platform Local AI
Run 100% air-gapped without data leaving your device:
- **Ollama**: Connects directly to local models (`llama3.2`, `qwen2.5-coder`, `deepseek-r1`) on `localhost:11434`.
- **Apple Silicon (Metal)**: High-throughput local acceleration.
- **NVIDIA GPUs (CUDA)**: Native GPU inference on Linux and Windows.
- **Host CPU Fallback**: Universal POSIX CPU execution.

---

## 🤖 In-Flight Sub-Agent Delegation (Tier C)

HEX can spawn focused, budget-bounded child agents on the fly to perform parallel research, specialized coding, or test verification without polluting your primary conversation history.

```text
Primary Agent (Task)
   ├── 🔍 Researcher Subagent (read-only AST & web investigation)
   ├── 💻 Coder Subagent (surgical implementation in isolated CoW workspace)
   └── 🧪 Tester Subagent (test execution & diagnostic validation)
```

- **Least-Privilege Roles**: Subagents run under strict role-constrained tool boundaries:
  - `researcher`: Read-only file inspection, Graphify AST queries, and web research.
  - `coder`: Code editing, search/replace patching, and test execution.
  - `tester`: Bounded test runners and compiler diagnostics.
  - `reviewer`: Blast-radius analysis and code diff auditing.
  - `planner`: Task decomposition and checklist management.
  - `general`: Standard toolset with child delegation disabled.
- **Copy-on-Write Workspace Isolation**: Each subagent gets its own instant CoW scratchpad at `.hex/subagents/<id>`. Experimental changes never touch your working directory until verified.
- **Recursion Safety**: Depth-capped (default max depth = 2) to eliminate runaway infinite child spawning.
- **Live TUI Observability**: Inspect in-flight and completed subagents with `/subagents`.

---

## 🌐 Interactive Headless Browser & Multimodal Vision (Tier B)

Automate web applications, inspect UI layouts, and diagnose frontend visual regressions directly from your terminal.

- **Headless Browser (`browser`)**:
  - **Accessibility Snapshots (`ariaSnapshot`)**: Extracts compact, token-efficient DOM hierarchies with interactive element references (`@e1`, `@e2`, `@e3`).
  - **Full Interaction**: Navigate to URLs, click elements, type into forms, take screenshots, and evaluate JavaScript expressions.
  - **Zero-Dependency Fallback**: Works out of the box with zero external browser binaries required.
- **Multimodal Visual Inspection (`image_inspect`)**:
  - Analyzes local images (PNG, JPG, WEBP, GIF, SVG) and remote URLs.
  - Automatic dimension detection, base64 encoding, and multimodal model reasoning for visual regression testing and UI mockup verification.

```bash
# Example tool calls executed by the agent
browser(action="navigate", url="http://localhost:3000")
browser(action="snapshot")
browser(action="click", selector="@e3")
browser(action="screenshot", output_path="login.png")
image_inspect(image_path="login.png", prompt="Verify if the submit button is properly aligned")
```

---

## 🔌 Agent Client Protocol (ACP) for Zed, Neovim & VSCode (Tier A)

Use HEX directly inside your favorite code editor! HEX implements the **Agent Client Protocol (ACP)** over standard JSON-RPC 2.0 stdio transport.

### 1. Zed Editor Configuration
Add HEX as an external agent in your `.zed/settings.json`:

```json
{
  "agent": {
    "name": "HEX",
    "command": "hex",
    "args": ["acp"],
    "protocol": "acp"
  }
}
```

### 2. Neovim (`avante.nvim` / `codecompanion`)
```lua
provider = "acp",
acp_command = { "hex", "acp" },
```

### 3. Editor Setup Diagnostics
Validate your editor bridge instantly:

```bash
# Verify ACP readiness and environment health
hex acp --check

# Generate ready-to-paste editor snippets
hex acp --setup
```

---

## ⌨️ Daily Workflows & Slash Commands

Inside the interactive console, type `/` to see available commands:

| Command | Action |
| :--- | :--- |
| **`/mode`** | Cycle or switch interaction mode (`plan`, `agent`, `code`) |
| **`/diff`** | View unified colorized diff of uncommitted sandbox changes |
| **`/sbs <file>`** | Open a side-by-side terminal diff for a specific file |
| **`/proof`** | View the cryptographic verification proof and test results for a completed task |
| **`/subagents`** | Inspect active and completed child subagents and delegation tree |
| **`/react [on|off]`** | Toggle continuous multi-turn autonomous ReAct tool loop |
| **`/rollback`** | Instantly discard changes and reset the scratchpad |
| **`/model`** | Inspect active model profile or switch providers |
| **`/compressor`** | Toggle log compressor mode (`deterministic`, `cloud`, `local`, `off`) |
| **`/project`** | Switch between registered projects in your global registry |
| **`/doctor`** | Run system health diagnostics and auto-repair broken dependencies |
| **`/voice`** | Toggle hands-free voice mode and audio feedback |
| **`/skin`** | Change color theme (`amber`, `cyberpunk`, `matrix`, `nord`, `mono`) |
| **`/help`** | Display full table of slash commands |

---

## 🛠️ CLI Quick Reference

HEX can also be run directly from scripts or your shell:

```bash
# Run an autonomous task with automatic git branching and commit
hex run "Refactor authentication flow to use refresh tokens" --branch

# Run continuous multi-turn autonomous ReAct tool loop
hex run "Audit repository security dependencies" --react
hex chat --react

# Start Agent Client Protocol (ACP) server for Zed, Neovim, and VSCode
hex acp --check
hex acp --setup
hex acp

# Launch TUI pair-programming console with specific theme
hex --skin cyberpunk

# Inspect system health and repair misconfigurations automatically
hex doctor --fix

# Manage workspace projects
hex project list
hex project add /path/to/my-repo
hex project switch

# Polyglot Language Server diagnostics
hex lsp status
hex lsp diagnostics
```

---

## 🤝 Contributing & Community

Contributions are warmly welcome! Whether fixing a bug, adding a new language parser, or designing a skin:

```bash
git clone https://github.com/Stewy8506/Harness.git
cd Harness
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[tui,test]"
python -m unittest discover -s tests
```

---

<div align="center">

Crafted with care by [**anv.dev**](https://anvv.tech) · Licensed under [Apache-2.0](LICENSE)

</div>

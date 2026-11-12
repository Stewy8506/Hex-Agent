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

[![Version](https://img.shields.io/badge/version-0.1.0-ffb703?style=flat-square&logo=semanticrelease&logoColor=white)](pyproject.toml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux%20%7C%20Windows-blueviolet?style=flat-square)](install.sh)
[![Tests Passing](https://img.shields.io/badge/tests-passing%20(100%25)-10b981?style=flat-square&logo=pytest&logoColor=white)](tests/)
[![Day-1 Antigravity](https://img.shields.io/badge/quota-Google%20Antigravity%20OAuth-4285F4?style=flat-square&logo=google&logoColor=white)](src/harness_core/providers/antigravity_provider.py)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square)](LICENSE)

<p align="center">
  <a href="#-quickstart-in-3-steps"><strong>Quickstart</strong></a> ·
  <a href="#-the-3-modes-plan--agent--code"><strong>The 3 Modes</strong></a> ·
  <a href="#-the-terminal-cockpit"><strong>Terminal Cockpit</strong></a> ·
  <a href="#-why-hex-feels-different"><strong>Why HEX</strong></a> ·
  <a href="#-supported-models--local-ai"><strong>Models & Local AI</strong></a> ·
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

                ▄▄▀▀▀▀▀▀▄▄                HEX Autonomous AI Workbench  v0.1.0  (upstream 864ebce)
             ▄▀▀  ▄▄▄▄▄▄  ▀▀▄             gemini-3.8-flash-tiered · Google Antigravity
          ▄▀▀  ▄▀▀      ▀▀▄  ▀▀▄          ~/Development/Harness
        ▄▀   ▄▀    ▄▄▄▄    ▀▄   ▀▄        Session: 5f64fe
       ▐▌   ▐▌   ▄▀    ▀▄   ▐▌   ▐▌
       ▐▌   ▐▌  ▐▌  ██  ▐▌  ▐▌   ▐▌       tools
       ▐▌   ▐▌  ▐▌ ████ ▐▌  ▐▌   ▐▌         file: list_files, read_file, write_file
       ▐▌   ▐▌   ▀▄ ▀▀ ▄▀   ▐▌   ▐▌         lsp: lsp_diagnostics, lsp_definition, lsp_hover
        ▀▄   ▀▄    ▀▀▀▀    ▄▀   ▄▀          search: web_search
          ▀▄▄  ▀▄▄      ▄▄▀  ▄▄▀            ast: query_graphify
            ▐▌ ▀▀▄▄▀▀▀▀▄▄▀▀ ▐▌              test: run_tests
            ▐▌     ▀▀▀▀     ▐▌              git: git_push_remote
             ▀▄▄          ▄▄▀
               ▀▀▄▄    ▄▄▀▀               12 tools · 15 skills · /help
                  ▀▀▀▀▀▀
              ───═══╬═══───               type a message   / for commands
```

### Highlights:
- **No Clutter, No Cages**: Floating negative space without rigid double boxes.
- **Docked Input Frame**: Visual status pills for active mode, project directory, git branch, and active model.
- **Dynamic Ghost Suggestions**: Rotating prompt ideas that clear the moment you start typing.
- **5 Terminal Themes**: Switch anytime with `/skin <name>` (`amber`, `cyberpunk`, `matrix`, `nord`, `mono`).

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

## ⌨️ Daily Workflows & Slash Commands

Inside the interactive console, type `/` to see available commands:

| Command | Action |
| :--- | :--- |
| **`/mode`** | Cycle or switch interaction mode (`plan`, `agent`, `code`) |
| **`/diff`** | View unified colorized diff of uncommitted sandbox changes |
| **`/sbs <file>`** | Open a side-by-side terminal diff for a specific file |
| **`/proof`** | View the cryptographic verification proof and test results for a completed task |
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

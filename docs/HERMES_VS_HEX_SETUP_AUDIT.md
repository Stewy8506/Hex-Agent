# Architectural Audit: Hermes Agent Setup vs. HEX Onboarding

**Document Version:** 1.0.0  
**Author:** Antigravity Engineering (HEX Core Team)  
**Date:** November 15, 2026  
**Scope:** Comparison of initial configuration, model reasoning effort control, autonomous self-setup, and operator onboarding UX between **Hermes Agent** and **HEX Agent**.

---

## Executive Summary

A critical audit of the setup and onboarding subsystems in **Hermes Agent** (`benchmarks/hermes-agent/`) and **HEX** (`src/harness_core/`) reveals a stark architectural contrast in developer experience:

- **Hermes Agent treats onboarding as an active, agent-driven conversational product journey.** The agent itself participates in its own setup. It features a dedicated onboarding toolset (`setup_beat`), environment awareness (`Facts`), a wire-clamping reasoning effort engine (`agent.reasoning_effort`), seamless auto-setup capabilities (*"Just finish setting yourself up"*), and culminates in a tangible First Task (`build` beat) that renders live results in a desktop/terminal preview pane.
- **HEX treats onboarding as a rigid procedural terminal script.** [onboard.py](file:///Users/anv./Development/Harness/src/harness_core/onboard.py) is a linear sequence of blocking curses/readline selection menus (`select_option`). The HEX agent does not exist until the wizard completely exits. There is **zero support for model reasoning effort**, no ability for the agent to autonomously configure itself, no missing-items pass (`--quick`), no minimal installation preset (`--blank-slate`), and setup concludes with a static ASCII banner leaving the operator at an unguided, empty prompt.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               HERMES AGENT SETUP ARCHITECTURE                           │
├────────────────────────────────────────────────────────────────────────────────────────┤
│  CLI Track:                                                                            │
│    hermes setup [model|tts|terminal|gateway|tools|telemetry|agent]                     │
│    Flags: --quick (missing only) | --blank-slate (minimal) | --portal (one-shot OAuth) │
│                                                                                        │
│  Conversational Track (In-Agent First-Run):                                            │
│    Agent greeting -> [setup_beat Tool] -> Interactive Cards:                           │
│      1. Name -> 2. Theme -> 3. Layout -> 4. Connectors -> 5. Plugins -> 6. First Task  │
│    Operator says: "Just make it work" -> Agent inspects host & self-configures         │
│                                                                                        │
│  Reasoning Effort Control:                                                             │
│    Ladder: (none, low, medium, high, xhigh, max) -> Clamped per provider/model wire   │
│    Runtime: /reasoning <level> | CLI: hermes model --reasoning <level>                 │
└────────────────────────────────────────────────────────────────────────────────────────┘
                                            vs.
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 HEX ONBOARDING ARCHITECTURE                            │
├────────────────────────────────────────────────────────────────────────────────────────┤
│  Procedural Wizard:                                                                    │
│    hex onboard / hex setup                                                             │
│    Step 1: Hardware probe (Darwin/Linux, RAM, Metal/CUDA, CoW backend)                 │
│    Step 2: 26-option uncurated raw provider menu (select_option)                       │
│    Step 3: API key / Base URL prompt -> Live model query -> Model picker               │
│    Step 4: Context compressor selection                                               │
│    Step 5: Web search engine selection                                                 │
│    Step 6: Telegram/Discord gateway prompts                                            │
│    Step 7: Writes credentials.env & config.yaml -> Static ASCII output -> Exit         │
│                                                                                        │
│  GAPS:                                                                                 │
│    ✖ No reasoning effort setting (OpenAI o-series / Claude 3.7 / Gemini 2.5)           │
│    ✖ Agent cannot self-configure ("finish setting yourself up" is impossible)          │
│    ✖ No --quick missing items pass | No --blank-slate minimal preset                   │
│    ✖ No first-task live verification or aha-moment                                     │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Deep-Dive: Model Reasoning Effort Control

### 1.1 The Hermes Implementation
In Hermes, reasoning effort is a first-class citizen across configuration, CLI, TUI, gateways, and provider dispatch:

1. **Standard Internal Ladder (`benchmarks/hermes-agent/agent/reasoning_effort.py`):**
   ```python
   EFFORT_LADDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
   ```
2. **Wire Vocabulary Clamping Engine:**
   Different model providers reject unknown effort parameters with HTTP 400 errors. Hermes implements an intelligent clamping function (`clamp_effort`) that maps the canonical effort to the exact wire specification of each model:
   - **OpenAI / Codex (o1, o3-mini, gpt-5.6, gpt-6):** Mapped to `"low" | "medium" | "high"` (and `"xhigh" | "max"` on supported tiers).
   - **Anthropic (Claude 3.7 Sonnet):** Automatically converts effort into token thinking budgets (`thinking: {"type": "enabled", "budget_tokens": ...}`).
   - **Google Gemini (Gemini 2.0/2.5 Thinking):** Automatically sets `thinkingConfig: {"thinkingBudget": ...}` or `thinkingLevel`.
   - **Open-Weight / SGLang / vLLM / DeepSeek / Kimi / Qwen:** Mapped to native parameters (e.g., Kimi K3 overrides, DeepSeek V4 clamps).
3. **Interactive Prompt After Model Selection (`hermes_cli/main_provider_setup.py:648`):**
   Whenever a model is selected (via `hermes setup model`, `hermes model`, or `/model`), Hermes calls `_offer_reasoning_after_pick()`:
   - It queries `_main_model_reasoning_efforts(model, provider)`.
   - If the model supports reasoning, it immediately prompts:
     ```text
     Reasoning effort [low / medium / high / max / none] (Press Enter to keep current):
     ```
   - Persists the selection directly to `agent.reasoning_effort` in `~/.hermes/config.yaml`.
4. **Runtime & Task Overrides:**
   - Slash command: `/reasoning <level>` or `/reasoning toggle` on the fly.
   - Per-task override: `task.reasoning_effort`.
   - Cron override: `hermes cron create --reasoning-effort high ...`.

### 1.2 The HEX Gap
- **No `reasoning_effort` in configuration:** [config_loader.py](file:///Users/anv./Development/Harness/src/harness_core/config_loader.py) and `config.yaml` have no `reasoning_effort` field under `model:`.
- **No setup prompt:** [onboard.py](file:///Users/anv./Development/Harness/src/harness_core/onboard.py) lines 373–670 prompt for provider, API key, base URL, and model ID, but **completely omit reasoning effort**.
- **No wire dispatch in router:** [router.py](file:///Users/anv./Development/Harness/src/harness_core/router.py) formats calls for Anthropic, OpenAI, and Gemini without passing thinking budgets or reasoning effort payloads. When using Claude 3.7 or OpenAI o-series, HEX runs on arbitrary provider defaults without user influence.
- **No runtime controls:** The HEX Cockpit TUI has no `/reasoning` slash command.

---

## 2. Deep-Dive: Autonomous Agent Self-Setup ("Make It Just Work")

### 2.1 The Hermes Implementation
In Hermes, first-run onboarding is not a shell script; it is **an interactive conversation executed by the agent itself**:

1. **The `Facts` System (`benchmarks/hermes-agent/agent/onboarding_flow.py`):**
   On boot, Hermes gathers non-invasive machine facts:
   - Operating System (`macos`, `windows`, `linux`), Architecture (`arm64`, `x86_64`).
   - Hardware Model (e.g., `Apple Silicon Mac`, `NVIDIA RTX Spark`, `DGX Spark`).
   - Installed developer tools and package managers (`brew`, `winget`, `flatpak`, `lipo`, etc.).
2. **The `setup_beat` Tool (`benchmarks/hermes-agent/tools/setup_beat_tool.py`):**
   The agent is equipped with a specialized tool `setup_beat(beat, ...)` that drives the onboarding flow across sequential beats:
   `('name', 'theme', 'layout', 'connectors', 'plugins', 'tour', 'build')`.
3. **Conversational Agency & Self-Setup Handling:**
   If the user says:
   > *"Just set yourself up so that it works."*  
   > *"Pick the best defaults for my Mac."*  
   > *"Skip the cards and get ready."*
   
   The opening system instructions (`benchmarks/hermes-agent/agent/onboarding_text.py:116`) explicitly instruct the agent:
   - *"If they ask you to pick (a colour, a layout), pick from what you know and say why in one line."*
   - *"You may look around this computer when they say yes: installed apps, developer tools, the OS and the hardware, read with the terminal."*
   - *"If they skip a beat in words, call setup_beat(beat=<that beat>, skip=true), take the default, and never ask it again in the same form."*
   The agent can execute terminal probes, pick optimal local or cloud models, configure plugins, and conclude setup without the human touching another button.

### 2.2 The HEX Gap
- **Procedural Disconnection:** HEX's onboarding ([onboard.py](file:///Users/anv./Development/Harness/src/harness_core/onboard.py)) is implemented as Python terminal menus using `curses` / readline (`select_option()`). The agent process **is not running**.
- **Zero Natural Language Input:** The operator cannot speak to HEX during onboarding. There is no chat interface, no intent parsing, and no way to say "use standard defaults and finish".
- **Rigid Gatekeeper:** If an unconfigured user runs `hex`, [cli.py](file:///Users/anv./Development/Harness/src/harness_core/cli.py#L752-L761) intercepts execution and halts at `select_option("Select Primary Model Provider & Brain:")`. If stdin is not a TTY or the user wants automated bootstrapping, HEX fails or hangs unless `--defaults-only` is passed via CLI flag.

---

## 3. Deep-Dive: Setup Modularity & Workflow Ergonomics

| Ergonomic Feature | Hermes Agent | HEX Agent | Assessment |
|---|---|---|---|
| **Sectional Setup** | `hermes setup model\|tts\|terminal\|gateway\|tools\|telemetry\|agent` | `hex setup [model\|search\|messaging]` | Hermes offers finer granularity (TTS, tools, telemetry, terminal backends). |
| **Missing-Items Pass** | `hermes setup --quick` (prompts only for unconfigured keys) | ✖ None (re-runs full menus) | **Major HEX Gap**: Re-running setup in HEX forces operators through existing choices. |
| **Minimal / Low-Overhead Preset** | `hermes setup --blank-slate` (strips tools down to `file`, `terminal`, `vision`, `skills`) | ✖ None | **Major HEX Gap**: No lightweight profile for resource-constrained containers. |
| **One-Shot Cloud OAuth** | `hermes portal` (single OAuth login for 300+ models & gateway) | `hex auth antigravity` (auto-detects IDE SQLite state) | **HEX Parity**: HEX's Antigravity IDE auto-import is excellent, but manual cloud requires manual API keys. |
| **Factory Reset** | `hermes setup --reset` | ✖ None (must manually delete `~/.hex/`) | Minor HEX Gap. |
| **Provider Selection UX** | Curated recommendations + Categorized selection | Uncurated raw list of **26 providers** in one terminal menu | **Major HEX Gap**: HEX causes decision paralysis on fresh installs. |

---

## 4. Deep-Dive: The "Aha Moment" (First-Task Build Beat vs. Static Text)

### 4.1 Hermes: Immediate Tangible Value
Hermes does not consider setup complete when a config file is written. The final beat of onboarding is `build`:
- The agent offers 4 personalized first tasks based on the host facts (e.g. *"Explain how Hermes works"*, *"Tune my games for this PC"*, *"Build a 3D scene in Blender"*, *"Brief me on today"*).
- The agent creates a living, interactive project in `~/hermes-first-task/` (such as a GSAP/D3 animated architectural diagram or live data brief).
- It calls `desktop_preview` to pop open the visual preview pane right beside the chat.
- The operator experiences a live demonstration of tool calling, file creation, and visual UI rendering within 2 minutes of installing.

### 4.2 HEX: Static Console Output
In HEX, [onboard.py](file:///Users/anv./Development/Harness/src/harness_core/onboard.py#L860-L895) concludes with:
```text
✓ HEX has been successfully initialized!
  • Configuration: ~/.hex/config.yaml
  • Credentials:   ~/.hex/credentials.env (chmod 0600)
  • Model Provider: antigravity (gemini-2.5-pro)
  • Web Search:    duckduckgo
  • Storage Dir:   ~/.hex/data
  • Seeded Skills: 2 verified skills registered

Commands:
  • Start Cockpit TUI:            hex   (or hex chat)
  • Start Messaging Gateways:     hex gateway start
  • Run autonomous coding task:   hex run "Your goal here"
```
The program exits or enters the Cockpit TUI with an empty task backlog. The operator has no interactive feedback that tools, compilers, or local CoW snapshots are functioning correctly.

---

## 5. Detailed Feature Comparison Scorecard

| Dimension | Hermes Agent | HEX Agent | Winner |
|---|---|---|---|
| **Model Reasoning Control** | First-class ladder (`none`..`max`), wire clamping, interactive picker, `/reasoning` | Not implemented | **Hermes** (10/10 vs 0/10) |
| **Agent Self-Setup** | Full conversational agency (`setup_beat`), environment awareness, self-configuration | Procedural terminal menu only | **Hermes** (10/10 vs 2/10) |
| **Missing-Items Flow (`--quick`)** | Supported across CLI and root profile | Missing (requires walking all steps) | **Hermes** (9/10 vs 3/10) |
| **Minimal / Blank Slate Setup** | `--blank-slate` minimal toolsets and memory footprint | Missing | **Hermes** (9/10 vs 2/10) |
| **IDE Quota / Subscription Auth** | Nous Portal subscription OAuth | Google Antigravity IDE SQLite state auto-import | **HEX** (10/10 vs 8/10 for IDE devs) |
| **Provider Selection Hierarchy** | Tiered (Portal / Curated / Custom) | 26-option flat menu (cognitive overload) | **Hermes** (9/10 vs 4/10) |
| **First-Run Verification Task** | Automated First Task in `~/hermes-first-task/` + live preview | Static ASCII banner output | **Hermes** (10/10 vs 3/10) |
| **Hardware & Sandbox Discovery** | Non-invasive facts probe | Deep Darwin/Linux probe (Metal, CoW, Sandboxing) | **HEX** (9/10 vs 8/10) |
| **Settings Portability / Import** | Scans Cursor, Claude Code, Codex, OpenCode | Migration scripts exist, but not in onboarding | **Hermes** (9/10 vs 4/10) |
| **Overall Setup UX & Flow** | Intuitive, adaptive, conversational, modern | Robust infrastructure, but rigid and manual | **Hermes** (9.3 / 10 vs 4.5 / 10) |

---

## 6. Actionable Implementation Blueprint for HEX

To achieve parity with and surpass Hermes's setup experience, HEX should execute the following 4-phase enhancement plan:

### Phase 1: Model Reasoning Effort Engine (Immediate Priority)
1. **Schema & Config**:
   - Add `reasoning_effort` (`low`, `medium`, `high`, `xhigh`, `max`, `none`) to `model:` section in [config.yaml](file:///Users/anv./Development/Harness/src/harness_core/config_loader.py).
   - Support `HEX_REASONING_EFFORT` in `credentials.env`.
2. **Wire Clamping in Router (`router.py`)**:
   - For **OpenAI**: Pass `"reasoning_effort": effort` for `o1`, `o3-mini`, `gpt-5.6`.
   - For **Anthropic**: Convert effort into `thinking: {"type": "enabled", "budget_tokens": ...}` (e.g., low=2048, medium=8192, high=16384, max=32768) for Claude 3.7.
   - For **Gemini / Antigravity**: Pass `thinkingConfig` parameters.
3. **Onboarding Integration**:
   - In [onboard.py](file:///Users/anv./Development/Harness/src/harness_core/onboard.py), immediately after model selection, check if the model supports reasoning. If yes, display the reasoning effort selector.
4. **Runtime Cockpit Control**:
   - Add `/reasoning [level]` slash command to [tui.py](file:///Users/anv./Development/Harness/src/harness_core/tui.py).

### Phase 2: Autonomous Agent Self-Setup (`hex onboard --auto` & In-Chat Setup)
1. **Host Capabilities Inspection**:
   - Extend `HardwareProfile.detect()` to inspect installed developer tools (`git`, `docker`, `node`, `python`, `clang`, `rustc`).
2. **Autonomous Self-Configuration Protocol**:
   - Add `hex setup --auto`: The agent inspects hardware, selects the optimal detected provider (Antigravity if IDE logged in, Ollama/MLX if offline, or env vars), selects recommended compression, and initializes `.hex/` without human intervention.
3. **Conversational Setup Intercept**:
   - If an unconfigured user launches `hex`, start a conversational onboarding session rather than a curses loop:
     > *"Hi, I'm HEX. I detected Apple Silicon M4 with Metal acceleration and an active Antigravity IDE session. Would you like me to configure Gemini 2.5 Pro with deterministic compression, or do you have a specific provider in mind?"*

### Phase 3: Setup Modularity & Ergonomic Enhancements
1. **`--quick` Flag**: Add `hex setup --quick` to only prompt for keys that are unset or invalid.
2. **`--blank-slate` Flag**: Add `hex setup --blank-slate` for a minimal, zero-overhead agent configuration (essential for lightweight CI runners).
3. **Curated Provider Hierarchy**: Group the 26 providers into 4 intuitive tiers:
   - *Tier 1: Recommended / Included* (Google Antigravity IDE OAuth, Anthropic, OpenAI).
   - *Tier 2: Unified Gateway* (OpenRouter, Vercel AI Gateway).
   - *Tier 3: Local Offline* (Ollama, LM Studio, MLX).
   - *Tier 4: Enterprise & Specialized* (Bedrock, Azure, Vertex, Groq, Cerebras, DeepSeek).

### Phase 4: Guided First-Task Verification
1. **Live Initial Run**:
   - At the conclusion of `hex onboard`, offer:
     `[y] Run quick system self-test and hello-world verification task`
   - Spawns an isolated CoW workspace (`active_workspaces/ws_onboard_verify`), creates a simple diagnostics test file, executes it in the restricted sandbox, verifies CAS hash storage in `ledger.db`, and displays a green verification checkmark.
   - Proves to the operator with empirical evidence that the entire harness stack is operational.

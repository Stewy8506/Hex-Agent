# HEX Product Pillars: The Path to a Finished Autonomous Workbench

> **Authoritative Specification:** This document establishes the **final 5 product pillars** required to transform the verified, enterprise-grade core engine into a polished, self-contained developer product named **HEX**, on par with **Hermes Agent** and **OpenClaw**.
>
> **Scope Closure Guarantee:** These 5 pillars represent the **complete and exhaustive scope** of user-facing capabilities. There are **no additional steps, layers, or milestones needed** beyond these 5 pillars to deliver the finished product.

---

## Executive Overview: From Engine to Product

The codebase has achieved **100% architectural and security compliance** (verified across all 18 sections of `PRD.md` with 186/186 passing tests, true Copy-on-Write durability, default-deny R0–R4 Tool Gateway, 0ms call journal replay, Active Graphify AST blast-radius calculation, and NVIDIA SkillSpector supply-chain scanning).

The engine is bulletproof. The final transformation brings the **interaction layer, distribution layer, and developer workflow autopilot** to match the seamless user experiences of Hermes Agent and OpenClaw under the unified brand **HEX**:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              HEX PRODUCT LAYER                               │
├──────────────┬──────────────┬──────────────┬──────────────┬──────────────────┤
│   Pillar 1   │   Pillar 2   │   Pillar 3   │   Pillar 4   │     Pillar 5     │
│ Zero-Friction│ Interactive  │ Bidirectional│ Git & GitHub │    Autonomous    │
│ Onboarding & │ Terminal UI  │ Multi-Channel│  Autopilot   │ Automations &    │
│ Distribution │ (TUI / REPL) │ Bot Gateways │ (Auto-PRs &  │ Self-Synthesizing│
│  (`onboard`) │   (`chat`)   │(TG & Discord)│   Review)    │  Skills (Hermes) │
├──────────────┴──────────────┴──────────────┴──────────────┴──────────────────┤
│                    VERIFIED HEX ENGINE (PHASES 01–07)                        │
│ CoW APFS/OverlayFS │ 16-Table Ledger │ SkillSpector │ Two-Tier Router │ ASGI │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Pillar 1: Zero-Friction Distribution & Interactive Onboarding

### 1.1 Objective
Eliminate all configuration friction. A developer should go from zero to a running, hardware-optimized HEX workbench in less than 60 seconds using a single command.

### 1.2 User Experience
```bash
# 1-liner installation (macOS & Linux)
curl -fsSL https://get.hex.ai/install.sh | bash

# Guided interactive onboarding wizard
hex onboard
```

### 1.3 Architecture & Implementation Details
1. **Interactive Setup Wizard (`src/harness_core/onboard.py`, `hex onboard`):**
   - **Hardware Profiling:** Probes CPU architecture (`arm64` vs `x86_64`), unified memory/RAM size, and detects Apple Silicon Metal / NVIDIA CUDA availability.
   - **Storage Discovery:** Probes available snapshot providers (`APFS` on macOS, `OverlayFS`/`Btrfs` on Linux, falling back to portable snapshotting). Defaults to local SQLite WAL for 1-click zero-config setup; prompts for PostgreSQL connection string if multi-worker clustering is requested.
   - **Sandbox Detection:** Probes system for `bubblewrap`, `docker`, and `podman`, setting the optimal execution boundary automatically.
   - **Model Configuration:**
     - *Local Profile:* Configures Apple Silicon M4 `mlx-lm` local engine or Ollama local endpoint.
     - *Cloud Profile:* Interactively prompts for and tests API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`).
   - **Credentials & Config Management:**
     - Writes configuration to `~/.hex/config.yaml`.
     - Writes sensitive API tokens to `~/.hex/credentials.env` with strict `0600` POSIX file permissions.
2. **Project Initialization (`hex init`):**
   - Detects repository root (Git, Python, Node, Rust, Go).
   - Generates project-scoped `.hex/config.yaml` with pre-configured verification commands (`pytest`, `npm test`, `cargo test`), file boundaries, and blast-radius depth limits.
3. **Packaging & Global CLI (`pyproject.toml`):**
   - Configures console script entrypoint `hex = harness_core.cli:main` (with `harness` maintained as an alias).
   - Standalone `install.sh` creating hermetic virtual environment and symlinking binary into `~/.local/bin/hex` or `/usr/local/bin/hex`.

### 1.4 Verification & Acceptance Criteria
- Running `hex onboard` in an empty environment prompts the user, detects M4 hardware, creates valid configuration files, and passes a self-test ping in `<10s`.
- Running `hex init` in any Git repository produces a valid `.hex/config.yaml` tailored to the detected stack.

---

## Pillar 2: Interactive Terminal UI (TUI / REPL: `hex chat`)

### 2.1 Objective
Provide a real-time, interactive console interface for conversational pair programming, live token streaming, visual diff inspection, and one-touch keyboard approvals.

### 2.2 User Experience
```
$ hex chat

  ╭────────────────────────────────────────────────────────────────────────╮
  │                     HEX: Autonomous AI Workbench                       │
  │ Model: claude-3-5-sonnet | Compressor: M4 MLX Local | Sandbox: bwrap   │
  ╰────────────────────────────────────────────────────────────────────────╯

> @hex Fix the token expiry race condition in auth.py

● Task task_8a1 started | Attempt 1 in isolated CoW workspace
├─ Active Graphify: Analyzed auth.py (Found 2 impacted downstream callers)
├─ Running pinned verification tests... [FAILED]
├─ Local M4 MLX Compressor: Distilled 180 lines of pytest logs to 14 lines (-92%)
├─ Prompt Caching: Reused stable AST context (1,840 cached tokens, 90% discount)
├─ Proposed Patch:
│
│  diff --git a/src/auth.py b/src/auth.py
│  --- a/src/auth.py
│  +++ b/src/auth.py
│  @@ -42,3 +42,5 @@ def refresh_token(user_id: str) -> str:
│  +    with self.lock:
│  +        token = self._issue(user_id)
│
🚨 Approval Required: git_commit (R3)
   Impact: Modifies core authentication locking mechanism
   [y] Approve   [n] Reject   [d] Full Diff   [s] Sandbox Shell   [q] Abort
```

### 2.3 Architecture & Implementation Details
1. **Interactive Console Engine (`src/harness_core/tui.py`):**
   - Built on `rich` and `prompt_toolkit`.
   - **Streaming Token Rendering:** Live Markdown parser rendering tokens progressively with code syntax highlighting.
   - **Side-by-Side & Unified Diff Visualizer:** Renders colorized syntax diffs for proposed file operations before committing to disk.
   - **Active Graphify Blast-Radius Tree:** Displays interactive call graph tree showing affected callers directly in the console.
2. **One-Key Interactive Approval Prompt:**
   - Intercepts `ApprovalRequiredInterrupt` directly in the TUI session.
   - Single-key actions:
     - `y`: Approves action and automatically resumes DAG execution.
     - `n`: Rejects action and prompts for operator guidance.
     - `d`: Expands full multi-file unified diff.
     - `s`: Drops into an ephemeral interactive sandbox shell inside the CoW attempt workspace for manual inspection.
3. **In-Chat Slash Commands:**
   - `/status`: Live task, worker, and lease telemetry.
   - `/diff`: Display pending changes across the current CoW branch.
   - `/rollback`: Discard current attempt and revert workspace to pristine base state.
   - `/fork`: Split current attempt into two speculative execution branches.
   - `/skills`: List and inspect active SkillSpector-scanned skills.
   - `/model`: Switch model providers on the fly (e.g. `/model sonnet` $\to$ `/model local`).
   - `/budget`: Inspect session token usage, cache hit ratio, and USD spend.

### 2.4 Verification & Acceptance Criteria
- TUI supports live token streaming, renders multi-file colorized diffs, catches R3 approval interrupts with single-key confirmation, and completes full repair cycles within the interactive session.

---

## Pillar 3: Bidirectional Multi-Platform Chat Gateways (Telegram & Discord)

### 3.1 Objective
Allow operators to collaborate with, command, and supervise HEX from mobile chat apps (Telegram and Discord) through full bidirectional conversation—not just one-way notification alerts.

### 3.2 User Experience
- **Operator (Telegram/Discord DM):**
  > `"Hey HEX, investigate why the worker heartbeat test is flaking in CI."`
- **HEX Bot:**
  > `"Task task_9c2 created. Inspecting tests/test_worker_daemon.py in CoW workspace... Found ResourceWarning on unclosed HTTPError. Generating patch..."`
- **HEX Bot (with Inline Buttons):**
  > `"🚨 Approval Required (R3): Apply patch to tests/test_worker_daemon.py?`  
  > `[✅ Approve]   [❌ Reject]   [🔍 View Diff]   [⏹ Cancel Task]"`
- **Operator:** Taps `[✅ Approve]`.
- **HEX Bot:**
  > `"Approval recorded. Executing verification... 186/186 tests passed! Proof bundle sha256:4a8f... Branch hex/task_9c2 created."`

### 3.3 Architecture & Implementation Details
1. **Bidirectional Bot Daemon (`src/harness_core/gateways/`):**
   - **Telegram Gateway (`telegram_bot.py`):** Uses Telegram Bot API long-polling or webhook receiver. Parses conversational instructions, slash commands (`/task`, `/status`, `/cancel`, `/approve`), and manages inline keyboard button callbacks.
   - **Discord Gateway (`discord_bot.py`):** Uses Discord WebSockets / slash commands (`/hex task`, `/hex status`) with interactive button message components.
2. **Access Control & Operator Pairing:**
   - Enforces strict whitelist pairing: only messages from approved `allowed_chat_ids` or `allowed_user_ids` are processed.
   - Unauthorized messages are dropped with security audit logging.
3. **Natural-Language Command Dispatcher:**
   - Translates incoming chat messages into task specifications (`TaskSpec`).
   - Supports natural-language status queries (`"How is task 8 doing?"`, `"What tasks are waiting for approval?"`).
4. **Interactive Inline Approval Callbacks:**
   - Tapping `[Approve]` triggers an authenticated callback calling `LedgerStore.decide_approval()` and `HarnessOrchestrator.resume_task()`.
   - Edits the chat message in-place to reflect the decision, preventing duplicate clicks.

### 3.4 Verification & Acceptance Criteria
- Sending a message to the Telegram/Discord bot spawns an autonomous task in a CoW workspace.
- Gated R3 tools send an inline message button; clicking `[Approve]` in Telegram/Discord resumes and finishes the task without terminal interaction.

---

## Pillar 4: Git & GitHub Autopilot Integration

### 4.1 Objective
Turn HEX into an autonomous software engineer that lives natively inside standard Git and GitHub workflows, automatically managing branches, commits, pull requests, and PR review comments.

### 4.2 User Experience
```bash
# Execute a task and automatically create a pull request
hex run "Refactor database query parameter translation to use prepared statements" --pr

# Run as a GitHub Action on issue mentions or PR reviews
# "@hex-bot please fix the failing test in test_gateway.py"
```

### 4.3 Architecture & Implementation Details
1. **Git Automation Engine (`src/harness_core/git_autopilot.py`):**
   - **Auto-Branching:** Automatically creates clean feature branches: `hex/task-<task_id>-<slug>`.
   - **Conventional Commits:** Generates structured, atomic commit messages following `type(scope): description` with full task trace metadata.
   - **PR Creation (`hex run --pr`):**
     - Invokes GitHub CLI (`gh pr create`) or GitHub REST API.
     - Formats PR body with:
       - Problem statement & executive summary of fix.
       - Active Graphify Impact Matrix: modified symbols and downstream callers verified.
       - Proof-Carrying Artifact Bundle: attached verification test runner output, execution duration, and Merkle tree SHA-256 signatures.
2. **GitHub Action & Webhook Receiver Mode (`hex gh-action`):**
   - Runs as a standard GitHub Action or webhook listener on `issue_comment.created` and `pull_request.opened`.
   - Responds to mentions (e.g. `@hex-bot fix issue #42`):
     - Checks out repository into CoW sandbox.
     - Implements requested fix.
     - Pushes commits directly to the branch.
     - Posts a comment on the PR/issue with the signed proof bundle.

### 4.4 Verification & Acceptance Criteria
- Running `hex run "..." --pr` completes the task in CoW isolation, passes all pinned tests, creates a git branch, commits changes, and opens a GitHub Pull Request with a Proof-Carrying Artifact table.

---

## Pillar 5: Autonomous Background Automations & Hermes-Style Self-Synthesizing Skills

### 5.1 Objective
Enable HEX to operate continuously as an autonomous background resident that executes scheduled automations and synthesizes its own reusable, verified skills over time.

### 5.2 User Experience
```bash
# Register a scheduled background automation
hex schedule add "0 2 * * *" --goal "Run SkillSpector supply-chain audit on all dependencies and verify CVE database"

# Inspect active schedules
hex schedule list

# Install continuous OS background service
hex daemon install
```

### 5.3 Architecture & Implementation Details
1. **Native Task Scheduler (`src/harness_core/scheduler.py`):**
   - Built-in lightweight cron-parser and background scheduling engine.
   - Executes scheduled jobs in CoW isolation without blocking the interactive server or worker daemon.
   - Automatic execution history and alert dispatch to Telegram/Discord on failure or security alert.
2. **OS Service Auto-Installer (`hex daemon install`):**
   - **macOS:** Generates and loads `~/Library/LaunchAgents/ai.hex.daemon.plist` with automatic reboot persistence.
   - **Linux:** Generates and activates `/etc/systemd/system/hex.service` with graceful `SIGTERM` draining.
3. **Hermes-Style Learning Loop & Self-Synthesizing Skills:**
   - When HEX successfully resolves a complex, multi-step engineering task (e.g., configuring an unfamiliar framework or solving a multi-file dependency issue), the orchestrator triggers skill synthesis:
     - Distills the execution trace, tool calls, and verified solutions into a structured `SKILL.md` bundle.
     - **Mandatory NVIDIA SkillSpector Gate:** The newly synthesized skill is automatically scanned by `SkillSecurityScanner` for malicious AST patterns, prompt injection, and unauthorized network calls *before* saving.
     - If clean, the skill is committed to `skills/custom/<skill-name>/` and indexed into the skill registry for future prompt grounding.

### 5.4 Verification & Acceptance Criteria
- Scheduled jobs trigger reliably according to cron expressions in CoW sandboxes.
- Successfully synthesized skills are scanned and validated by NVIDIA SkillSpector before being made available to future task runs.

---

## Definition of Done: The Finished Product Matrix

When all 5 pillars are implemented, the product delivery checklist is 100% complete:

| Pillar | Core Deliverables | User-Facing Command | Product State |
|---|---|---|---|
| **Pillar 1: Onboarding** | Setup wizard, hardware profiling, `install.sh`, packaging | `hex onboard`, `hex init` | Zero-friction installation on any machine |
| **Pillar 2: Interactive TUI** | Real-time REPL, token streaming, diff viewer, 1-key approvals | `hex chat` | Pair programming console with full visual feedback |
| **Pillar 3: Chat Gateways** | Telegram & Discord bidirectional bots with mobile approvals | `hex gateway start` | Complete remote control via mobile chat apps |
| **Pillar 4: Git Autopilot** | Auto-branching, conventional commits, automated PR generation | `hex run --pr`, GitHub Action | Autonomous junior engineer for GitHub |
| **Pillar 5: Automations** | Cron scheduler, systemd/launchd daemon, self-synthesized skills | `hex schedule`, `hex daemon` | 24/7 background operation & growing skill library |

**No further steps, frameworks, or rewrites are required. Delivering Pillars 1 through 5 completes the vision of HEX: a private, durable, production-grade autonomous AI workbench.**

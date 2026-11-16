# HEX System Architecture

This document provides a detailed overview of the core architectural subsystems powering **HEX Agent**.

---

## 1. High-Level Architecture Overview

HEX is structured into five primary architectural planes:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                       PRESENTATION & INTERACTION LAYER                      │
│   Native macOS GPUI Cockpit (experimental) · Native Ratatui TUI             │
│          Agent Client Protocol (ACP) JSON-RPC 2.0 · Web Control Surface     │
├─────────────────────────────────────────────────────────────────────────────┤
│                        AUTONOMOUS ORCHESTRATION CORE                        │
│       ReAct Loop · Task Scheduler · Mixture-of-Agents (MoA) Multi-Model     │
│  Timeline Rewind & Soft Branching · Approval Leases · Repetition Sentry     │
├─────────────────────────────────────────────────────────────────────────────┤
│                         ACTIVE COGNITION & MEMORY                           │
│     Slot #4 Frozen Turn-0 Prompt · AST-Scoped Memory · Negative Guardrails   │
│     Zettelkasten Contradiction Resolution · SkillSpector Gate · Verify Hooks │
├─────────────────────────────────────────────────────────────────────────────┤
│                        MODEL RESILIENCE & INTEGRATION                       │
│    Multi-Key CredentialPool · HTTP 429 Jittered Backoff · Model Cooldowns   │
│     30s Endpoint Blackholing · Local Socket Probes · OS Keychain Vault      │
├─────────────────────────────────────────────────────────────────────────────┤
│                          STORAGE & ISOLATION ENGINE                         │
│     Copy-on-Write (CoW APFS/OverlayFS) · PostgreSQL 16 + SQLite WAL Dual    │
│     SHA-256 Hash Chaining · Scoped TTL Retention & CAS Mark-and-Sweep GC    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Native macOS GPUI Desktop Cockpit (`crates/hex-desktop`) — Experimental

HEX features a state-of-the-art native desktop application built using Zed's GPU-accelerated **GPUI** framework (`crates/hex-desktop`):

```text
┌───────────────────────────┬────────────────────────────────────────────────────────┐
│  HEX DESKTOP  ● ● ●       │  Workspace: /Development/Harness  git:(main)  ● Claude │
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

### 2.1 Hardware-Accelerated Rendering & macOS Vibrancy
- **Metal Pipeline**: Renders natively on Apple Silicon Metal shaders via Zed's GPUI crate, eliminating the entire Chromium/Node.js stack.
- **Blurred Vibrancy**: Configured with `WindowBackgroundAppearance::Blurred` and `TitlebarOptions` with transparent titlebar and traffic lights offset at `(14.0, 16.0)`.
- **Memory Footprint**: Native Rust + Metal stack with no bundled browser runtime; substantially lighter than Electron-based agent desktop apps.

> **Status:** The desktop cockpit is experimental and distributed from source only. `ChatView` (`chat.rs`) is the root view: it owns the composer editor, focus, keybindings, and the `AcpBridge`, and runs a working chat flow covered end to end by `tests/test_chat_flow.rs`. The token gauge, reasoning folds, and inline diff views are implemented but not yet fed by live ACP events.

### 2.2 Dark Zinc Design Tokens (`theme.rs`)
Implements a Dark Zinc visual hierarchy:
- **Canvas / Background**: `#09090b` (true zinc black)
- **Card / Surface**: `#18181b` (subtle elevation)
- **Elevated / Popover**: `#27272a` (modal cards and dropdowns)
- **Structural Borders**: `#3f3f46` (crisp 1px hairline dividers)
- **Typography**: `#fafafa` (high-contrast primary), `#a1a1aa` (muted secondary)
- **Diff Colors**: `#8fbf93` (soft sage green insertions), `#c98b8b` (muted rose red deletions)
- **Accent**: `#7aa7ff` (electric blue focus rings and active pills)

### 2.3 Component Tree & Interactive Modals
- **`ChatView`**: Root GPUI view. Routes ACP events to the session they belong to, reconciles streamed updates with the authoritative prompt response, keeps per-session drafts, and moves focus between the composer and any open modal (`Composer` and `Modal` key contexts).
- **`AppShell`**: Manages the dual-pane layout, active modals, and per-session feeds.
- **`SidebarView` & `SessionListView`**: 260px collapsible sidebar with workspace path, git branch tag, provider status indicator, and date-grouped session history (`Today`, `Yesterday`, `Previous 7 Days`).
- **`MessageFeed`**: Renders user prompts, streaming assistant turns labelled with the runtime-reported model, fenced code blocks, tool execution cards with status badges, and system notices for cancelled, failed, and disconnected states. Collapsible `<think>` folds and syntax-colored diffs are available as components but not yet fed by ACP events.
- **`ComposerView` & `TokenGaugeView`**: Multiline prompt editor (`Enter` sends, `Shift+Enter` newline, `⌘Enter` also sends), active model selector pill, and ASCII progress bar gauge (`▕██████░░░░░░░░░░▏`, placeholder budget for now).
- **`ToolApprovalModal`**: 3-way security confirmation (`[y]` allow once, `[a]` allow for session, `[n]` deny) with a **15-Minute Auto-Approval Lease Toggle** (`[l]`). Decisions are sent via `session/approvalDecision`; the lease reason is transmitted but not yet honored server-side.
- **`CommandPaletteModal`**: Fuzzy searchable command overlay (`⌘K`) for switching sessions and running desktop actions; unavailable actions report that plainly.
- **`ModelPickerModal`**: Multi-provider model browser with context window capacities; the chosen model applies to sessions started afterwards.
- **`AcpBridge`**: Handles JSON-RPC 2.0 over a stdio subprocess (or `/tmp/hex-acp.sock`), using the canonical wire contract shared with the Python server (see [ACP Integration](ACP_INTEGRATION.md)).

---

## 3. Clean Domain Subpackage Layout (`src/harness_core/`)

HEX's Python core is organized into 7 clean domain subpackages, eliminating monolithic files and circular dependencies:

```text
src/harness_core/
├── core/
│   ├── orchestrator.py        # Central coordination engine
│   ├── react_loop.py          # Multi-turn autonomous tool execution loop
│   ├── router.py              # Model selection and token routing
│   ├── subagent.py            # Hierarchical child agent worker pools
│   ├── moa.py                 # Mixture-of-Agents proposer/aggregator engine
│   ├── approval_lease.py      # Temporal auto-approval lease manager
│   ├── reasoning_effort.py    # Dynamic reasoning effort budgets
│   └── models.py              # Task and execution data models
├── storage/
│   ├── db.py                  # Dual-driver PostgreSQL 16 + SQLite WAL ledger
│   ├── cow.py                 # APFS clonefile / Linux OverlayFS sandboxing
│   ├── sandbox.py             # Ephemeral workspace lifecycle management
│   ├── timeline_rewind.py     # Conversational rewind and soft-branching
│   ├── vault_keychain.py      # OS Keychain secure credential storage
│   ├── gc.py                  # Scoped TTL retention and CAS garbage collection
│   ├── backup.py              # Online backup and point-in-time recovery
│   └── project_registry.py    # Multi-workspace directory tracking
├── cognition/
│   ├── soul.py                # Machine DNA and persona identity (SOUL.md)
│   ├── memory.py              # Ast-scoped JIT memory and A-MEM CRUD
│   ├── reflection.py          # Automatic failure friction post-mortems
│   ├── skillspector.py        # NVIDIA AST security and malware gate
│   ├── skill_synthesizer.py   # Closed-loop skill compiler with verify: hooks
│   ├── prompt_builder.py      # 5-slot frozen Turn-0 prompt compiler
│   ├── title_synthesizer.py   # Asynchronous 2-stage LLM session titling
│   ├── repetition_sentry.py   # Loop detection and <think> tag scrubber
│   └── context_compressor.py  # Two-tier AST and log traceback compression
├── runtime/
│   ├── process_registry.py    # Subprocess tracking and PID fencing
│   ├── code_kernel.py         # Persistent Jupyter/Python execution kernel
│   ├── daemon.py              # Background service daemon (launchd/systemd)
│   ├── worker_daemon.py       # Distributed task worker process
│   └── scheduler.py           # Scheduled cron and delayed automations
├── client/
│   ├── acp.py                 # Agent Client Protocol JSON-RPC 2.0 server
│   ├── tui.py                 # Terminal user interface controller
│   ├── tui_launcher.py        # TUI launcher and terminal mode detection
│   ├── ui_selector.py         # Interactive UI selector (TUI vs Desktop vs CLI)
│   ├── dashboard.py           # Zero-dependency local web dashboard
│   ├── asgi.py                # FastAPI REST API and SSE event endpoints
│   ├── cli.py                 # Main CLI command dispatcher
│   ├── onboard.py             # Interactive terminal onboarding wizard
│   ├── auto_setup.py          # One-click environment autodetection
│   └── doctor.py              # Self-healing diagnostic repair engine
├── capabilities/
│   ├── browser.py             # Playwright headless browser with ariaSnapshot
│   ├── vision.py              # Multimodal image and DOM visual diffing
│   ├── voice.py               # Hands-free spoken pair programming console
│   ├── search.py              # Fast ripgrep and semantic code search
│   ├── git_autopilot.py       # Autonomous git branching, staging, and PRs
│   ├── proof.py               # Cryptographic verification certificates
│   └── gateways/              # Telegram & Discord webhook listeners
└── models/
    ├── credential_pool.py     # Multi-key pool, 429 rotation & cooldowns
    └── model_metadata.py      # Provider catalog, socket probes, and specs
```

---

## 4. Copy-on-Write (CoW) Filesystem Isolation

Rather than editing source code in place, HEX spawns an ephemeral sandbox for every task or subagent attempt:

- **macOS (APFS)**: Uses native `clonefile()` system calls to reflink directory trees in **<10ms**. Blocks share storage until modified.
- **Linux**: Uses kernel-level `OverlayFS` mounts (`lowerdir` = repository root, `upperdir` = ephemeral scratchpad).
- **Windows / Fallback**: Uses hard-link mirrors and temporary directory staging.

### Sandbox Lifecycle:
1. **Clone**: An instantaneous sandbox is initialized under `.hex/sandboxes/<task_id>`.
2. **Execute**: The agent modifies files, installs temporary dependencies, and executes your test runner.
3. **Verify**: The orchestrator inspects the return code and test suite output.
4. **Commit or Rollback**:
   - If tests **pass**: The file diff is atomically applied to your active branch.
   - If tests **fail**: The sandbox is discarded with **0ms** host pollution.

---

## 5. Model Resilience: Credential Pools, Socket Probes & Keychain

Production reliability requires resilient handling of upstream provider rate limits, network outages, and credential management:

```text
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                         CREDENTIAL POOL ROUTING                             │
  │                                                                             │
  │  [ Key Pool: k1, k2, k3 ] ──► Round-Robin / Least-Used Selection            │
  │                                     │                                       │
  │                                     ▼                                       │
  │                            Execute Upstream Call                            │
  │                                     │                                       │
  │            ┌────────────────────────┴────────────────────────┐              │
  │            ▼ (HTTP 200 OK)                                   ▼ (HTTP 429)   │
  │       Success Return                               Rotate to Next Key       │
  │                                              Calculate Jittered Backoff     │
  │                                              Queue (Provider, Model) Cooldown│
  │                                              30s Endpoint Blackholing Guard │
  └─────────────────────────────────────────────────────────────────────────────┘
```

### 5.1 Multi-Key Ingestion & Rotation (`credential_pool.py`)
- **Key Pools**: Supports multiple keys per provider (e.g. `ANTHROPIC_API_KEY_1`, `ANTHROPIC_API_KEY_2`).
- **Load Balancing**: Distributes calls across keys via round-robin or least-used balancing.
- **Automatic 429 Rotation**: When an HTTP 429 occurs, the pool immediately shifts traffic to alternate keys while marking the failing key with an exponential backoff timer incorporating jitter.
- **Model Cooldown Queues**: Throttled `(provider, model)` combinations enter a cooldown queue without impacting unrelated models on the same account.
- **30s Endpoint Blackholing**: If an endpoint suffers consecutive connection timeouts, it is blackholed for 30s to prevent worker stalls.

### 5.2 Local Socket Probes (`model_metadata.py`)
HEX automatically queries local inference daemons to discover active models and context capacities:
- **Ollama**: Queries `http://localhost:11434/api/show` for parameter sizes and context windows.
- **LM Studio**: Queries `http://localhost:1234/v1/models` for loaded model identifiers.
- **llama.cpp**: Queries `http://localhost:8080/props` for hardware acceleration state.
- **vLLM**: Queries `http://localhost:8000/v1/models` for active served models.

### 5.3 OS Keychain Vault (`vault_keychain.py`)
API keys and OAuth tokens can be saved directly in the operating system's native secure credential storage (macOS Keychain, Linux Secret Service, Windows Credential Manager) via the `keyring` library, eliminating unencrypted plaintext secrets on disk.

---

## 6. Conversational Workflow: Rewind, MoA & Leases

### 6.1 Conversational Timeline Rewind (`timeline_rewind.py`)
- **Turn-Targeted Rollback**: Users can invoke `/rewind <turn | -N>` to restore conversation history to any historical checkpoint.
- **Soft Branching in SQLite**: Historical turns are never hard-deleted; instead, they are preserved under a unique `branch_id` in the `tasks` table with cryptographic `TIMELINE_REWOUND` audit events.
- **Sandbox State Synchronization**: Truncates dialogue buffers, resets the ReAct tool call journal, and reverts sandbox file modifications to match the selected turn.

### 6.2 Mixture-of-Agents (MoA) Engine (`moa.py`)
- **Proposer Layer**: Dispatches candidate generation to multiple diverse LLMs (e.g., Claude 3.7 Sonnet, GPT-4o, Gemini 2.5 Pro) in parallel.
- **Aggregator Layer**: Feeds candidate proposals and critiques to an aggregator model that cross-examines solutions and compiles an optimal final output.
- **Graceful Degradation**: Enforces per-proposer timeouts so slow or failing models do not stall the user experience.

### 6.3 Temporal Approval Leases (`approval_lease.py`)
- **Lease Creation**: Users grant time-bounded auto-approval leases (e.g., `/lease npm test 1h`, `/lease git status 15m`).
- **Prefix & Path Matching**: Evaluates command prefixes and target directories against active leases.
- **TTL Expiration**: Expired leases automatically lapse, restoring mandatory human-in-the-loop confirmation prompts.

### 6.4 Repetition Sentry & `<think>` Scrubber (`repetition_sentry.py`)
- **Loop Detection**: Monitors sliding token n-grams and tool call signatures to identify infinite loops and repetitive failure patterns.
- **Steering Prompt Injection**: Injects out-of-band corrective prompts (`"You are repeating actions without making progress. Stop and change strategy."`).
- **Thinking Tag Purification**: Strips raw `<think>...</think>` artifacts from model outputs before committing to conversation history.

---

## 7. Storage & Cryptographic Event Ledger

HEX utilizes a dual-driver relational architecture designed for enterprise-grade durability and concurrency:

### Dual-Driver DB (`harness_core.storage.db.LedgerStore`)
- **PostgreSQL 16+**: High-throughput distributed mode with `asyncpg` connection pooling and `SELECT ... FOR UPDATE SKIP LOCKED` task queue claiming.
- **SQLite 3.35+ (WAL)**: Zero-config local embedded mode with Write-Ahead Logging and automatic checkpointing.
- **Alembic Schema Parity**: All database migrations are fully reversible and tracked via Alembic revisions.

### Cryptographic Event Chaining
Every lifecycle state change (task submission, tool execution, policy approval, completion) is recorded in `task_events` as an immutable cryptographic hash chain:

$$\text{hash}_k = \text{SHA-256}\left(\text{hash}_{k-1} \parallel \text{task\_id} \parallel \text{seq\_no} \parallel \text{event\_type} \parallel \text{payload}\right)$$

Any tampering with stored event logs breaks chain continuity and is caught by `store.verify_chain_integrity()` and `scripts/verify_event_chain.py`.

---

## 8. AST Code Intelligence (Graphify)

HEX incorporates an active code intelligence engine powered by tree-sitter:

- **Polyglot Parsing**: Indexes Python, TypeScript, JavaScript, Rust, Go, and C/C++.
- **Knowledge Graph**: Maintains a directed graph of classes, functions, interfaces, and cross-file import relationships.
- **Blast-Radius Analysis**: Before editing a function, HEX queries `graphify affected <symbol>` to identify all downstream callers and tests that must be validated.

---

## 9. Next-Gen Cognition & Self-Evolution Architecture

HEX fundamentally redesigns agent self-learning away from static flat text buffers into a multi-tiered, active cognitive loop:

```text
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                    5-SLOT SYSTEM PROMPT ARCHITECTURE                        │
  │                                                                             │
  │  Slot #1: SOUL.md Identity (Global ~/.hex/SOUL.md or Repo .hex/SOUL.md)    │
  │           Capped token budget (~300–500 tokens). Defines tone and persona. │
  │                                                                             │
  │  Slot #2: Dynamic Mode Modulator                                            │
  │           Punchy 3–4 line constraint overrides: plan / agent / code.        │
  │                                                                             │
  │  Slot #3: Environment & Tool Capabilities                                   │
  │           Tool schemas, R0–R4 safety tiers, and platform execution context. │
  │                                                                             │
  │  Slot #4: Curated Memory Snapshot (Fenced in <memory-context>)              │
  │           Frozen Turn-0 prefix snapshot: USER.md + MEMORY.md + GUARDRAILS.md│
  │           Guarantees 100% byte-for-byte prefix invariance (KV-Cache freeze).│
  │                                                                             │
  │  Slot #5: Dynamic Context, Active Files & Skills                            │
  │           Task intake, selected SKILL.md bundles, and dynamic execution.    │
  └─────────────────────────────────────────────────────────────────────────────┘
```

1. **AST-Scoped Memory Injection**: Injects symbol-specific invariants directly into `read_file` tool observation outputs rather than system prompts, keeping the prompt 100% KV-cache frozen.
2. **Negative Constraint Memory**: Maintains `.hex/memories/GUARDRAILS.md` populated with `[AVOID]` invariants when commands fail repeatedly.
3. **Zettelkasten Contradiction Resolution**: Implements the A-MEM pattern (`ADD`, `UPDATE`, `DELETE`, `NOOP`) with FTS5 keyword indexing to reconcile conflicting facts.
4. **Confidence Decay**: Promotes validated lessons to `MEMORY.md` while demoting stale knowledge (>30d unreferenced) to cold SQLite FTS5 search storage.
5. **Deterministic `verify:` Skill Hooks**: Executes automated validation assertions attached to `SKILL.md` bundles after agent editing.
6. **Cross-Repo DNA**: Separates machine-wide engineering habits (`~/.hex/USER.md`) from repository-specific architecture rules (`.hex/MEMORY.md`).

---

## 10. Supply-Chain Security & Hardware Protection

### NVIDIA SkillSpector AST Safety Gate
Before any synthesized or third-party skill bundle is written to disk, it is staged in an isolated scratchpad and scanned by the upstream NVIDIA SkillSpector engine:
- **AST Behavioral Analysis**: Runs Python AST taint checks on bundled scripts to detect socket exfiltration, shell spawns, and unauthorized reflection.
- **Prompt Injection & Steganography**: Scans for zero-width Unicode characters, bidirectional overrides, and instruction override markers.
- **OSV CVE Lookup**: Scans bundled dependencies against the live OSV advisory database.
- **Deterministic Quarantine**: Any skill scoring High or Critical risk (`gate_decision == "QUARANTINE"`) is rejected and quarantined to `.hex/quarantine/<name>/` with a forensic `audit.json`.

### Hardware Gating (`auto_reflect = "cloud_only" | "idle" | "off"`)
Spawning background reflection subagents on local hardware competes for memory bandwidth. HEX enforces strict hardware profiling:
- Under `cloud_only` or `off`, background LLM reflection is bypassed.
- Candidate friction lessons are queued to `.hex/reflections/pending_reflections.json` for offline processing.

---

## 11. Storage Lifecycle & Garbage Collection (`hex gc`)

Task execution continuously produces CoW snapshots, CAS delta tarballs, and proof bundles. To prevent unbounded disk growth while preserving auditability, HEX implements an automated, scoped retention engine (`src/harness_core/storage/gc.py`):

```text
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                           GARBAGE COLLECTION SWEEP                          │
  │                                                                             │
  │   [ active_workspaces/ ]   [ cow_snapshots/ ]            [ cas/ ]           │
  │      Ephemeral Clones         Step Snapshots         Deduplicated Tarballs  │
  │             │                       │                         │             │
  │             ▼                       ▼                         ▼             │
  │     Query ledger.db        Query ledger.db            Mark-and-Sweep:       │
  │   status in terminal &   status in terminal &    Reference set against all  │
  │   updated_at > 24h &     updated_at > 24h &      active/pinned/recent tasks │
  │     pinned == 0            pinned == 0                        │             │
  │             │                       │                         ▼             │
  │             ▼                       ▼                Delete unreferenced    │
  │       Safe Deletion           Safe Deletion          blobs older than 7d    │
  │             │                       │                         │             │
  │             └───────────────────────┼─────────────────────────┘             │
  │                                     ▼                                       │
  │                      PRAGMA incremental_vacuum;                             │
  │               (Non-blocking; full VACUUM only via hex gc)                   │
  └─────────────────────────────────────────────────────────────────────────────┘
```

1. **Active & Pinned Task Immunity**: Tasks in `RUNNING`, `PAUSED`, `PENDING`, or `pinned = True` are strictly exempt from TTL deletion.
2. **Reference-Counted CAS Mark-and-Sweep**: Prunes only unreferenced blobs older than `7d`.
3. **Orphan Directory Race Guard**: Ephemeral scratchpads created before database commit are protected by a minimum age threshold (`mtime > 1h`).
4. **Non-Blocking Concurrency Policy**: Automated sweeps run `PRAGMA incremental_vacuum;` to reclaim unused database pages without acquiring exclusive table locks.

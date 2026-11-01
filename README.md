<div align="center">

<img src="assets/hex-hero-banner.svg" alt="HEX — Autonomous AI Workbench" width="100%" />

<br />

[![Version](https://img.shields.io/badge/version-0.1.0-ffb703?style=flat-square&logo=semanticrelease&logoColor=white)](pyproject.toml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-185%2B%20passing%20(100%25)-10b981?style=flat-square&logo=pytest&logoColor=white)](AUDIT_REPORT.md)
[![Security Gate](https://img.shields.io/badge/security-NVIDIA%20SkillSpector-76b900?style=flat-square&logo=nvidia&logoColor=white)](vendor/skillspector/README.md)
[![AST Engine](https://img.shields.io/badge/AST%20graph-Graphify%20(71.5x%20compression)-00f5d4?style=flat-square)](vendor/graphify/README.md)
[![Self-Improvement](https://img.shields.io/badge/synthesis-Google%20Harness²-4285F4?style=flat-square&logo=google&logoColor=white)](vendor/harness2/README.md)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square)](pyproject.toml)

**A local-first, proof-carrying autonomous software engineering and operations workbench.**  
*Zero-copy CoW sandboxes · 0ms journaled tool replay · Active AST blast-radius interception · Two-tier M4 MLX + Cloud prefix caching.*

[**Quickstart**](#-30-second-quickstart) · [**Interactive TUI**](#-interactive-tui--hermes-grade-cockpit) · [**5-Stage Architecture**](#-5-stage-proof-carrying-architecture) · [**Token Economics**](#-token-economics--empirical-benchmarks) · [**Security & Sandboxing**](#️-supply-chain-security--kernel-sandboxing) · [**API & Operations**](#-distributed-workers-webhooks--restsse-api) · [**Audit Report**](AUDIT_REPORT.md)

</div>

---

> [!IMPORTANT]
> **Why HEX Exists:** Traditional AI coding agents (`OpenClaw`, `Hermes`, shell-wrapper loops) execute directly on your mutable working tree, re-run expensive tool calls from scratch after crashes, ship raw 50 KB test tracebacks directly to frontier cloud APIs, and rely on passive markdown memory. **HEX** replaces best-effort chat loops with a **transactional, kernel-sandboxed, AST-aware state machine** that proves every patch passes verification before touching your mainline branch.

---

## ⚡ See It in Action

<div align="center">
  <img src="assets/hex-tui-demo.svg" alt="HEX Interactive Terminal TUI Demo" width="100%" />
</div>

---

## 🚀 30-Second Quickstart

### 1. Install `hex` (Editable or Production Wheel)

```bash
git clone https://github.com/your-org/harness.git
cd harness

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Run Interactive Onboarding & Initialize Your Repository

```bash
# 1. Interactive 4-step wizard (detects Apple Silicon M4 MLX, API keys, and CoW drivers)
hex onboard

# 2. Build the zero-token Tree-sitter AST dependency graph (.harness/graph.json)
hex init .

# 3. Launch the full-screen Hermes-grade interactive TUI cockpit
hex tui --skin amber
```

### 3. Or Dispatch a Proof-Carrying Task Directly from CLI

```bash
# Fast-Path single-shot execution (<15ms overhead for read-only / R0-R1 tasks)
hex chat -Q "Show the blast radius of modifying src/harness_core/router.py"

# Full autonomous CoW + AST + Verification loop
hex run "Fix the retry ceiling edge case in orchestrator.py and run unit tests"
```

---

## 🏗️ 5-Stage Proof-Carrying Architecture

<div align="center">
  <img src="assets/hex-architecture.svg" alt="HEX 5-Stage Proof-Carrying Architecture" width="100%" />
</div>

<br />

HEX integrates three battle-tested upstream engines directly into `src/harness_core/` (vendored and detached from `.git` for 100% air-gapped reproducibility):

| Subsystem | Engine / Module | What It Guarantees |
| :--- | :--- | :--- |
| **1. Supply-Chain Quarantine** | [`vendor/skillspector`](vendor/skillspector/README.md) → [`skillspector.py`](src/harness_core/skillspector.py) | Blocks prompt-injected skills, malicious hooks, Pickle/exec payloads, and OSV CVEs in **<50ms** (static) and **<2s** (M4 semantic) with **100% quarantine recall**. |
| **2. CoW Workbench & Sandbox** | [`cow.py`](src/harness_core/cow.py) + [`sandbox.py`](src/harness_core/sandbox.py) | Instant zero-copy filesystem clones via **macOS APFS (`cp -c -R`)**, **Linux OverlayFS**, or **Btrfs**, isolated inside **Bubblewrap (`bwrap`)**, **rootless Docker/Podman**, or sanitized subprocesses. |
| **3. 0ms Call-Level Journal** | [`gateway.py`](src/harness_core/gateway.py) | Hashes `(tool_name, canonical_args, snapshot_id)`. Crashed or resumed tasks replay prior tool calls from the journal in **0ms (<15ms SLA)** without re-executing side effects. |
| **4. Active AST Blast-Radius** | [`vendor/graphify`](vendor/graphify/README.md) → [`graphify.py`](src/harness_core/graphify.py) | Pre-flight Tree-sitter AST interceptor computes downstream callers (`affected_nodes`) **before** any file write or git commit and injects targeted `pytest` test selectors. |
| **5. Two-Tier Model Router** | [`mlx_provider.py`](src/harness_core/providers/mlx_provider.py) + [`cloud_provider.py`](src/harness_core/providers/cloud_provider.py) | Compresses noisy test/build logs locally on Apple Silicon M4 MLX (`>70%` reduction) and pins the stable AST graph in Cloud Prompt Prefix Cache (`90%` input token discount). |
| **6. Speculative Tree-of-Worktrees & Harness²** | [`vendor/harness2`](vendor/harness2/README.md) → [`proof.py`](src/harness_core/proof.py) | Forks $N$ parallel CoW snapshots for competing synthesis strategies, runs verification suites in parallel, and promotes only the verified winner (`ProofArtifact`). |

---

## 💰 Token Economics & Empirical Benchmarks

HEX is engineered to achieve **5x–10x lower cloud token spend** than single-tier autonomous agents (`OpenClaw`, `Hermes Agent`) through four compounding mechanisms:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  1. $0 AST Graph Build       Tree-sitter local pass builds .harness/graph.json (0 LLM) │
│  2. >70% Log Compression     Tier-1 M4 MLX (or Flash/Haiku) strips stacktrace noise    │
│  3. 90% Prefix Cache Hit     Stable System + AST Prefix pinned via cache_control       │
│  4. 0-Token Crash Replay     Resumed runs replay tool outputs from SQLite/PG journal   │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### Empirical Token Reduction Across Real Corpora (Graphify + Tier-1 Compressor)

| Corpus / Workload | Raw Input | HEX Compressed | Token Reduction | Mechanism |
| :--- | ---: | ---: | ---: | :--- |
| **52-file Mixed Repository** | 418,400 tok | **5,850 tok** | **71.5x fewer tokens** | Local Tree-sitter AST graph + leiden communities |
| **6-file Code + Research Corpus** | 124,200 tok | **2,290 tok** | **54.2x fewer tokens** | Structural God-node & BFS path querying |
| **Verbose Pytest / Build Failure Logs** | 14,200 chars | **1,800 chars** | **87.3% reduction** | Tier-1 `Tier1LogCompressor` preserving exact assertions |
| **Multi-Turn Repair Loop (Turns 2–5)** | 100% input price | **10% cached price** | **90.0% cost savings** | Ephemeral prompt prefix caching (`stable → context → volatile`) |

> [!TIP]
> **Flexible Local or Cloud Log Compression:** Have an Apple Silicon Mac (M1/M2/M3/M4)? Set `compressor_mode="local"` (default) to compress logs at **$0.00 cost** using unified memory (`Qwen-2.5-Coder-7B-4bit`). Running on a headless Linux box without a local GPU? Switch to `compressor_mode="cloud"` to route log compression through a fast cheap cloud tier (`Gemini Flash` / `Claude Haiku`)—while HEX's **Privacy Gate** automatically forces `SENSITIVE` or `SECRET` logs back to local processing so credentials never leave your machine.

---

## 🖥️ Interactive TUI & Hermes-Grade Cockpit

Launch the interactive terminal interface with `hex tui` (or `hex chat`):

- **24-Bit TrueColor Retro Banner & 3D Isometric Hex-Core Emblem:** High-contrast splash card displaying active model tiers, CoW driver status, session ID, available tools, and taxonomy-grouped skills.
- **5 Built-In Theme Skins (`--skin` or `/skin <name>`):**
  - `amber` *(default)* — Gold/Amber retro-futuristic workbench (`#FFD700` / `#FF8700`)
  - `cyberpunk` — Neon magenta & electric cyan (`#FF007F` / `#00F5D4`)
  - `matrix` — Phosphor terminal green (`#00FF66`)
  - `nord` — Arctic frost blue (`#88C0D0` / `#81A1C1`)
  - `mono` — High-contrast monochrome for minimal terminals
- **Live Persistent Bottom Status Bar:**
  ```text
  ⚕ gemini-3.5-pro │ 🗜️ local:m4-mlx │ 🌿 main ✓ │ 💲$0.0018 (90% cached) │ ctx 6.4k/32.0k (78% saved) │ [██░░░░░░░░] 20%
  ```
  The `[██░░░░░░░░] 20%` gauge tracks real-time **context window utilization** (`tokens_in / max_context_tokens`), changing color from **Green (<50%)** → **Amber (50–80%)** → **Red (>80%)** when `/compress` is recommended.
- **Interactive R2/R3 Keyboard Approval Cards:** When an agent requests a mutating shell command or outside-workspace write, inspect the colorized unified diff and press:
  - `[y]` Approve once · `[n]` Deny · `[d]` Toggle full unified diff · `[s]` Session-allow tool · `[q]` Abort task

<details>
<summary><strong>📋 Click to expand all 21 Interactive TUI Slash Commands</strong></summary>

| Slash Command | Category | Description |
| :--- | :--- | :--- |
| `/help` | Navigation | Render full categorized slash command reference table |
| `/status` | Telemetry | Show active task state, CoW snapshot ID, lease epoch, and DB health |
| `/model [tier2]` | Model Routing | Inspect or switch the active Tier-2 cloud synthesis model |
| `/compressor [local\|cloud]` | Model Routing | Switch log compressor between local M4 MLX (`$0`) and cloud (`Flash`/`Haiku`) |
| `/compress` | Context Window | Force immediate Tier-1 context compaction to free context window capacity |
| `/usage` | Telemetry | Show token usage breakdown (raw vs. compressed, cache hits, USD spend) |
| `/skin [name]` | Appearance | Live-switch TUI color theme (`amber`, `cyberpunk`, `matrix`, `nord`, `mono`) |
| `/clear` | Navigation | Clear terminal viewport and redraw the HEX splash card |
| `/compact` | Appearance | Toggle compact `┊` activity feed mode |
| `/tools` | Capabilities | List registered gateway tools with R0–R4 risk levels |
| `/skills` | Capabilities | List verified skills grouped by domain (`architecture`, `security-gates`, etc.) |
| `/graph [file]` | AST Engine | Query `.harness/graph.json` for a file's blast radius and downstream callers |
| `/cow` | Sandbox | Inspect active Copy-on-Write worktree snapshots and delta byte sizes |
| `/diff` | Sandbox | Render colorized unified diff of uncommitted changes in the active CoW snapshot |
| `/rollback [snap_id]` | Sandbox | Instantly roll back the worktree to a clean CoW snapshot |
| `/journal` | Reliability | View memoized tool calls in the 0ms replay journal |
| `/security` | Security | Run on-demand NVIDIA SkillSpector scan across loaded skills and MCP servers |
| `/proof` | Verification | Display the cryptographic `ProofArtifact` for the latest completed run |
| `/workers` | Distributed | List active distributed workers, CAS fencing epochs, and heartbeats |
| `/history` | Session | Browse conversation turn history and journal checkpoints |
| `/quit` / `/exit` | Session | Cleanly release leases, flush WAL, and exit the TUI |

</details>

---

## 🛡️ Supply-Chain Security & Kernel Sandboxing

Every tool call and external skill passes through a **Default-Deny Tool Gateway** ([`src/harness_core/gateway.py`](src/harness_core/gateway.py)) and **NVIDIA SkillSpector** ([`src/harness_core/skillspector.py`](src/harness_core/skillspector.py)):

### R0–R4 Risk Classification Policy Matrix

| Risk Tier | Classification | Examples | Gateway Enforcement |
| :---: | :--- | :--- | :--- |
| **R0** | Read-Only | `read_file`, `query_graphify`, `git status` | Auto-approved · Fast-Path eligible (`<15ms`) · 0ms journal cached |
| **R1** | Scoped Workspace Write | `write_file` inside CoW clone, `run_tests` | Auto-approved inside active CoW snapshot · Pre-flight AST blast-radius check |
| **R2** | Mutating Local / Shell | `git commit`, package install, subprocess build | Requires interactive TUI `[y/n/d/s/q]` or remote Webhook approval (unless scoped allowlist) |
| **R3** | External Network / Egress | `curl`, external MCP server mutation, API push | Strict egress gate · Secret scrubber (`redact_secrets`) · Mandatory human approval |
| **R4** | Destructive / Policy Mutation | `rm -rf /`, modifying `.harness/policy.json`, raw credential read | **Hard-blocked by default** (`PolicyViolationError`) |

### Multi-Stage SkillSpector Quarantine Pipeline
1. **Stage 1 Static Scan (`<50ms`):** 71 vulnerability rules across AST patterns (`eval`, `exec`, `pickle.loads`, `os.system`, `__subclasses__`), YARA signatures, Unicode bidirectional homoglyphs, and OSV CVE advisory lookup.
2. **Stage 2 Local M4 Semantic Audit (`<2s`):** Evaluates skill instructions and MCP server tool manifests (`scan_mcp_manifest`) for indirect prompt injection, covert exfiltration instructions, and privilege escalation before registering any tool.

---

## 🌐 Distributed Workers, Webhooks & REST/SSE API

HEX scales seamlessly from a single macOS laptop (`SQLite WAL`) to a multi-node fleet (`PostgreSQL 16+` with `SELECT ... FOR UPDATE SKIP LOCKED` and monotonic Compare-And-Swap fencing tokens).

```bash
# Start the production ASGI control plane (Uvicorn / HTTP + SSE streaming)
hex serve --host 127.0.0.1 --port 8080

# Start a distributed worker daemon with cross-machine CoW delta tar sync
hex worker --worker-id worker-mac-m4-01 --poll-interval 1.0

# Start the bidirectional Telegram / Discord chat gateway
hex gateway --telegram --discord
```

<details>
<summary><strong>🔌 Click to expand Control Plane REST &amp; SSE Endpoints (<code>/v1/*</code>)</strong></summary>

| Method & Path | Description |
| :--- | :--- |
| `GET /livez` | Lightweight liveness probe (`{"status": "alive"}`) |
| `GET /healthz` | Deep readiness probe verifying DB pool, CoW storage canary, and active worker heartbeats (`200` / `503`) |
| `POST /v1/tasks` | Submit a new task (`Idempotency-Key` supported, automatic R0/R1 Fast-Path routing) |
| `GET /v1/tasks/{id}` | Retrieve task state, lease epoch, CoW snapshot metadata, and `ProofArtifact` |
| `GET /v1/tasks/{id}/events` | Real-time **Server-Sent Events (SSE)** stream of state transitions and tool journal events (`Last-Event-ID` replay) |
| `POST /v1/tasks/{id}:cancel` | Cooperatively cancel a running task and roll back its unverified CoW snapshot |
| `GET\|POST /v1/approvals/{id}:decide` | Interactive human-in-the-loop approval endpoint (`decision=approve\|deny`, renders mobile-friendly HTML confirmation card and auto-resumes paused task) |
| `POST /v1/skills:scan` | Run NVIDIA SkillSpector static + semantic quarantine scan on a skill bundle or MCP manifest |

</details>

<details>
<summary><strong>🗄️ Click to expand the 16 Authoritative Database Tables (Alembic Migration <code>0001</code>)</strong></summary>

Managed via [`migrations/versions/0001_initial_schema.py`](migrations/versions/0001_initial_schema.py) across both PostgreSQL 16+ and SQLite WAL:
1. `tasks` — Authoritative state machine (`CREATED` → `PLANNING` → `RUNNING` → `VERIFYING` → `COMPLETED` / `NEEDS_ATTENTION`)
2. `task_steps` — DAG execution nodes and dependency edges
3. `task_leases` — Monotonic `lease_epoch` CAS fencing tokens preventing split-brain workers
4. `tool_call_journal` — Deterministic `(tool_name, args_hash, snapshot_id)` memoization store for `0ms` replay
5. `cow_snapshots` — APFS / OverlayFS / Btrfs snapshot lineage and delta archive hashes
6. `approval_requests` — R2/R3 human approval records with scrubbed parameters and expiry timestamps
7. `proof_artifacts` — Cryptographically hashed verification bundles (test logs, AST blast radius, diff hash)
8. `skill_quarantine_logs` — NVIDIA SkillSpector scan verdicts and triggered rule IDs
9. `mcp_servers` — Registered external Stdio and HTTP/SSE MCP transports
10. `model_usage_ledger` — Per-call token telemetry (`input_tokens`, `cached_input_tokens`, `output_tokens`, `cost_usd`)
11. `task_budgets` — Per-task token and USD ceiling enforcement (`TaskBudgetTracker`)
12. `ast_graph_nodes` — Indexed Tree-sitter symbols and module metadata
13. `ast_graph_edges` — Directed call/import dependency edges for blast-radius calculation
14. `harness2_revisions` — Immutable self-improvement revision registry (`propose` / `probe` / `compose`)
15. `worker_nodes` — Distributed worker heartbeats, capabilities, and active lease assignments
16. `audit_events` — Append-only security and state transition audit trail

</details>

---

## 🧪 Verification & Compliance Suite

Every architectural invariant in [`PRD.md`](PRD.md) is backed by empirical tests. Run the full verification suite locally:

```bash
# 1. Full unit & integration test suite (185+ tests passing, 0 regressions)
.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v

# 2. Authoritative 5-Stage PRD Section 16 Acceptance Suite
.venv/bin/python -m unittest tests/test_harness_stages.py -v

# 3. Two-Tier M4 MLX + Cloud Prefix Cache & Privacy Gate Benchmark
.venv/bin/python -m unittest tests/test_two_tier_compression_benchmark.py -v

# 4. Multi-Worker Concurrency & Monotonic CAS Lease Fencing Stress Test
.venv/bin/python -m unittest tests/test_postgres_concurrency.py -v
```

> [!NOTE]
> For the complete section-by-section architectural verification matrix, empirical test timings, and security audit findings, see [**`AUDIT_REPORT.md`**](AUDIT_REPORT.md) and the operator disaster-recovery guide in [**`docs/RUNBOOK.md`**](docs/RUNBOOK.md).

---

## 📁 Repository Layout

```text
harness/
├── assets/                        # Animated SVG banners, TUI replay demo, and architecture diagrams
├── docs/
│   └── RUNBOOK.md                 # Production operations, backup/restore, and quarantine forensics
├── migrations/                    # Alembic schema migrations (16 authoritative tables)
├── src/harness_core/
│   ├── asgi.py                    # ASGI 3.0 production application & lifespan manager
│   ├── chat_gateway.py            # Bidirectional Telegram & Discord conversational gateway
│   ├── cli.py                     # Unified `hex` and `harness` CLI entrypoints
│   ├── cow.py                     # macOS APFS, Linux OverlayFS & Btrfs Copy-on-Write engine
│   ├── db.py                      # Dual-driver PostgreSQL 16 (SKIP LOCKED) & SQLite WAL engine
│   ├── gateway.py                 # Default-Deny R0–R4 Tool Gateway, secret scrubber & 0ms journal
│   ├── graphify.py                # Active Tree-sitter AST blast-radius interceptor
│   ├── mcp_client.py              # Stdio & HTTP/SSE external MCP client with dynamic risk tagging
│   ├── onboarding.py              # Interactive `hex onboard` hardware & credential wizard
│   ├── orchestrator.py            # Multi-turn repair loop, DAG replanner & speculative worktrees
│   ├── proof.py                   # Proof-carrying artifact generator & Google Harness² loop
│   ├── router.py                  # Two-tier model router with local/cloud compressor & privacy gate
│   ├── sandbox.py                 # Bubblewrap, rootless Docker/Podman & restricted subprocesses
│   ├── server.py                  # REST + SSE control plane (/v1/*, /livez, /healthz)
│   ├── skillspector.py            # NVIDIA SkillSpector static + OSV + M4 semantic security gate
│   ├── tui.py                     # Hermes-grade 24-bit TrueColor TUI engine, 5 skins & 21 slash commands
│   ├── webhooks.py                # Outbound Telegram & Discord interactive approval cards
│   ├── worker_daemon.py           # Distributed worker daemon with CAS fencing & delta tar sync
│   └── providers/
│       ├── base.py                # ModelProvider protocol & cached token accounting
│       ├── cloud_provider.py      # Anthropic, OpenAI & Gemini provider with 90% prefix caching
│       └── mlx_provider.py        # Apple Silicon M4 MLX local runner & >70% log compressor
├── tests/                         # 24 test modules (185+ unit, stage, concurrency & TUI tests)
├── vendor/                        # Detached upstream engines (graphify, harness2, skillspector)
├── AUDIT_REPORT.md                # Principal Systems Architect & Security Compliance Verdict
├── PRD.md                         # Authoritative Product Requirements Document (18 sections)
└── pyproject.toml                 # Package metadata & `hex` / `harness` console scripts
```

---

<div align="center">

Built with **Copy-on-Write Determinism**, **Active AST Intelligence**, and **Zero-Trust Supply-Chain Gates**.  
Licensed under the [Apache License 2.0](pyproject.toml).

</div>

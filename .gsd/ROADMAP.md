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
[ ] Phase 11: Autonomous Background Automations & Hermes-Style Self-Synthesizing Skills (Pillar 5)
```

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

### Phase 11: Autonomous Background Automations & Hermes-Style Self-Synthesizing Skills (Pillar 5)
- **Goal:** Enable 24/7 autonomous background operation via native cron scheduler (`hex schedule`), systemd/launchd daemon installation (`hex daemon install`), and a Hermes-style learning loop synthesizing verified solutions into reusable skills gated by NVIDIA SkillSpector.
- **Must-haves:**
  - `hex schedule add <cron> --goal <goal>` cron scheduler running tasks in isolated CoW sandboxes.
  - `hex daemon install` generating `LaunchAgent` plist (macOS) and `systemd` service unit (Linux).
  - Hermes-style self-synthesizing skill creator extracting reusable patterns from successful executions.
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


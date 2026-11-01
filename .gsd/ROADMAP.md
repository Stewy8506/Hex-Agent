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

## Phases to Finished Product [100% COMPLETE]

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


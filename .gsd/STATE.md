# Current Project State: HEX Autonomous AI Workbench

**Date:** 2026-11-01  
**Current Milestone:** M3 (The Finished Product Layer: Pillars 1–5)  
**Status:** In Progress — Phase 10 Complete (305/305 tests green); Phase 11 Next  
**Active Phase:** Phase 11: Autonomous Background Automations & Hermes-Style Self-Synthesizing Skills (Pillar 5)  

---

## 1. Accomplishments to Date

1. **Clean Codebase Foundation:**
   - Full 16-table PostgreSQL schema designed and implemented (`migrations/0001_initial_schema.sql`).
   - Transactional `LedgerStore` with hash-chained task events, CAS lease fencing, and 0ms call journal (`src/harness_core/db.py`).
   - Native macOS APFS Copy-on-Write workspace snapshots (`cp -c -R`) and Merkle tree hashing (`src/harness_core/cow.py`).
   - Default-deny Tool Gateway with secret redaction and Code Mode composition (`src/harness_core/gateway.py`).
   - Active Graphify AST indexer with 1-hop/2-hop blast-radius detection and test pinning (`src/harness_core/graphify.py`).
   - Two-Tier model router & governed memory service (`src/harness_core/router.py`).
   - Speculative execution branching (`fork`) with winner promotion and Proof-Carrying Artifacts (`src/harness_core/proof.py`).
   - Core orchestrator, Fast-Path Express Lane, and state machine (`src/harness_core/orchestrator.py`).
   - Local-first Web Control Surface UI (`src/harness_core/dashboard.py`), REST + SSE API server (`src/harness_core/server.py`), and CLI (`src/harness_core/cli.py`).
   - Built-in Git-backed skills (`skills/repo_understanding`, `skills/technical_research`).

2. **Upstream Integrations Vendored & Wired:**
   - **Google Research Harness²:** Vendored in `vendor/harness2` and integrated in `src/harness_core/proof.py` behind an immutable revision registry and safety gate.
   - **NVIDIA SkillSpector:** Cloned from `NVIDIA/SkillSpector` (v2.12.0) into `vendor/skillspector` (detached from git history) and directly wired into `src/harness_core/skillspector.py` with 64+ agent-native patterns, YARA rules, and local semantic alignment.

3. **Empirical Verification:**
   - Complete 10/10 acceptance test suite passing in `tests/test_harness_stages.py` covering all 5 stages.

4. **Phase 01 Migrations Baseline Complete (Plan 01-01):**
   - Configured Alembic migration infrastructure (`alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`).
   - Authored initial migration `migrations/versions/0001_initial_schema.py` representing all 16 authoritative tables with reverse foreign key downgrades.
   - Verified automated upgrade to head and downgrade to base in `tests/test_alembic_migrations.py` (13/13 test suite passing).

5. **Phase 01 Dual-Driver Engine Complete (Plan 01-02):**
   - Implemented dual-driver routing (`postgresql://`, `postgres://`, `postgresql+asyncpg://` -> PostgreSQL; otherwise SQLite WAL) in `src/harness_core/db.py`.
   - Maintained zero-dependency SQLite local profile while integrating `asyncpg` connection pool with configurable limits.
   - Built query parameter translation (`?` -> `$1, $2, ...`), canonical JSON serialization, and background `_SyncRunner` bridge.
   - Implemented 27 async methods across `LedgerStore` with full sync parity and verified cryptographic hash chaining.
   - Authored comprehensive test suite in `tests/test_db_dual_driver.py` (27/27 total repository tests passing).

6. **Phase 01 Concurrent Lease Claims & Concurrency Control Complete (Plan 01-03):**
   - Implemented `claim_next_task` and `claim_next_task_async` in `src/harness_core/db.py` using `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` (Postgres) and atomic `BEGIN IMMEDIATE` (SQLite).
   - Monotonic fencing token increments and atomic CAS transitions to `RUNNING` with `STATE_RUNNING` (`QUEUE_CLAIMED`) hash-chained task events.
   - Hardened `begin_tool_call` and `seal_tool_call` against stale fencing tokens, raising `ConcurrencyFenceError`.
   - Built comprehensive multi-worker concurrency stress test suite in `tests/test_postgres_concurrency.py` (35/35 repository tests passing).

7. **Phase 02 CoW Providers Complete (Plan 02-01):**
   - Implemented `OverlayFSSnapshotProvider` with lower, upper, work, and merged layer mounts, unshare user namespace/FUSE support, and instant rollback.
   - Implemented `BtrfsSnapshotProvider` with read-only snapshots and subvolume branching.
   - Built platform detection factory `get_default_snapshot_provider` and dependency injection in `HarnessOrchestrator`.
   - Authored contract test suite in `tests/test_cow_providers.py` (58/58 repository tests passing).

8. **Phase 02 Sandbox Isolation & ToolGateway Complete (Plan 02-02):**
   - Implemented `BubblewrapSandbox`, `DockerPodmanSandbox`, `RestrictedSubprocessSandbox`, and `get_default_sandbox` in `src/harness_core/sandbox.py`.
   - Wired sandbox containment and secret redaction into `ToolGateway._tool_run_tests` and `HarnessOrchestrator`.
   - Hardened `_resolve_confined_path` with strict `Path.relative_to` verification.
   - Authored zero-escape verification test suite in `tests/test_sandbox_isolation.py` (76/76 total repository tests passing).

9. **Phase 03 MCP Client Adapters & Dynamic Tool Discovery Complete (Plan 03-01):**
   - Implemented `MCPClientAdapter`, `StdioMCPTransport`, `HttpSSEMCPTransport`, and dynamic `R0–R4` risk mapping with heuristic rules and per-server overrides in `src/harness_core/mcp_client.py`.
   - Built thread-safe JSON-RPC 2.0 message demuxing, stdout reader thread, rolling stderr diagnostics, and timeout enforcement.
   - Authored mock MCP server and unit test suite in `tests/test_mcp_client.py` verifying handshake, discovery, tool execution, timeouts, and clean process teardown (82/82 repository tests passing).

10. **Phase 03 SkillSpector Manifest Validation & 0ms Call Journaling Complete (Plan 03-02):**
    - Enhanced `SkillSecurityScanner` in `src/harness_core/skillspector.py` with `scan_mcp_manifest`, SHA-256 manifest pinning, and injection/semantic quarantine rules.
    - Integrated `register_mcp_server` into `ToolGateway` in `src/harness_core/gateway.py` with R0–R4 default-deny policy, secret redaction, and 0ms call-level journal memoization.
    - Authored end-to-end integration test suite in `tests/test_mcp_gateway_journal.py` verifying prompt-injection defense, 0ms replay (< 15 ms, 0 subprocess calls), approval interrupts, and R0–R4 policy enforcement (92/92 repository tests passing).

11. **Phase 04 Local MLX Model Provider & Log Compression Engine Complete (Plan 04-01):**
    - Implemented `ModelProvider` base protocol, `ModelUsage`, `ModelResponse`, and `ProviderCapabilities` in `src/harness_core/providers/base.py`.
    - Implemented `LocalMLXModelProvider` in `src/harness_core/providers/mlx_provider.py` with Darwin ARM64 discovery, unified memory detection, lazy `mlx_lm` loading, and pure-Python zero-dependency fallback generation.
    - Implemented `Tier1LogCompressor` in `src/harness_core/providers/mlx_provider.py` with secret redaction, multi-pattern trace and assertion extraction, local AST source context inspection, and >70% token reduction (91.03% verified).
    - Authored unit and contract test suite in `tests/test_model_providers.py` (107/107 total repository tests passing).

12. **Phase 04 Cloud API Connectors, Per-Task Budget Ceilings, Privacy Gating & Router Integration Complete (Plan 04-02):**
    - Implemented `CloudModelProvider` in `src/harness_core/providers/cloud_provider.py` supporting Anthropic Claude, OpenAI, and Google Gemini.
    - Implemented `TaskBudgetTracker` with hard ceilings for task USD cost, output tokens, and request counts, raising `BudgetExceededError`.
    - Enforced strict privacy gating blocking `sensitive` and `secret` classifications from cloud egress, raising `PolicyBlockedError`.
    - Integrated outbound secret scrubbing with `redact_secrets` before serialization.
    - Enhanced `TwoTierModelRouter` and `RouteDecision` in `src/harness_core/router.py` to record latency, cost, tokens, and compression ratio.
    - Authored live 500-line pytest compression and privacy benchmark in `tests/test_two_tier_compression_benchmark.py` and cloud tests in `tests/test_model_providers.py` (121/121 total repository tests passing).

13. **Phase 05 Multi-Turn Autonomous Repair Loop & Retry Escalation Complete (Plan 05-01):**
    - Implemented `generate_repair_patch` and `_parse_patch_operations` in `src/harness_core/orchestrator.py` integrating Tier-1 M4 log compression, Active Graphify AST blast-radius warnings, and model router dispatch.
    - Built iterative multi-turn repair loop in `HarnessOrchestrator._execute_standard_dag` cycling `VERIFYING -> RUNNING -> VERIFYING` with isolated CoW patch composition and verification re-execution.
    - Implemented retry exhaustion and budget ceiling handling: freezes dirty workspace, creates immutable forensic CoW snapshot and checkpoint, protects base workspace, and safely escalates to `NEEDS_ATTENTION` with `REPAIR_RETRIES_EXHAUSTED` or `BUDGET_EXHAUSTED`.
    - Hardened `LedgerStore.transition_task` and `transition_task_async` to preserve `terminal_reason` on interrupts.
    - Authored comprehensive test suite in `tests/test_autonomous_repair.py` covering happy path repairs, retry exhaustion, compound repairs, and budget escalation (127/127 total repository tests passing).

14. **Phase 05 Dynamic DAG Replanning & Selective Subgraph Invalidation Complete (Plan 05-02):**
    - Implemented `replan_dag` in `src/harness_core/orchestrator.py` with directed reachability analysis, unexpected side-effects detection (`actual - declared`), and dynamic DAG dependency resolver.
    - Built selective subgraph invalidation: marks only transitive downstream steps as `INVALIDATED` for re-execution while preserving independent completed steps as `COMPLETED`.
    - Integrated dynamic blast-radius test pinning via Active Graphify into `blast_radius_checks`.
    - Maintained monotonic plan revision increments (`revision`, `supersedes_revision`) and cryptographically hash-chained `PLAN_REVISED` task events.
    - Enhanced `LedgerStore` in `src/harness_core/db.py` with `PlanNode` serialization, `get_plan_revision`, and `list_plans`.
    - Authored comprehensive test suite in `tests/test_dag_replanning.py` (132/132 total repository tests passing with zero failures).

15. **Phase 06 Distributed Worker Daemon & Heartbeat Loop Complete (Plan 06-01):**
    - Implemented atomic `renew_lease`, `release_lease`, and `heartbeat_worker` in `src/harness_core/db.py` with CAS fencing validation and monotonic lease extensions.
    - Added REST endpoints in `src/harness_core/server.py` (`POST /v1/workers/register`, `POST /v1/workers/heartbeat`, `POST /v1/tasks/claim`, `POST /v1/leases/{id}:renew`, `POST /v1/leases/{id}:release`) with case-insensitive `X-Worker-Token` auth and HTTP 409 Conflict mapping.
    - Implemented `WorkerDaemon` architecture in `src/harness_core/worker_daemon.py` with machine profiling (M4 Air, GPU, Server), CoW backend discovery, periodic heartbeat loop, task polling with `--once` support, and signal handling (`SIGINT`/`SIGTERM`).
    - Implemented dual client transports: `DirectLedgerWorkerClient` and `RestWorkerClient`.
    - Added `harness worker` CLI command in `src/harness_core/cli.py`.
    - Authored comprehensive test suite in `tests/test_worker_daemon.py` (140/140 total repository tests passing with zero failures).

16. **Phase 06 Cross-Machine CoW Delta Sync, Lease Handover & Fencing Defense Complete (Plan 06-02):**
    - Implemented `import_delta_archive` and `export_delta_archive` across all snapshot providers in `src/harness_core/cow.py` with tar-slip traversal protection and Merkle tree SHA verification.
    - Added CAS artifact transfer endpoints in `src/harness_core/server.py` (`POST /v1/artifacts/delta`, `GET /v1/artifacts/{id}/file`) and methods in `src/harness_core/worker_daemon.py`.
    - Implemented `reap_expired_leases` in `src/harness_core/db.py` for automated re-queuing of abandoned tasks upon lease expiration.
    - Implemented lease handover with strictly monotonic fencing token increments (1 -> 2) and `LEASE_TAKEOVER` task events.
    - Integrated remote workspace reconstruction from CAS delta archives in `src/harness_core/orchestrator.py` with tree SHA validation.
    - Enforced monotonic fencing token defense rejecting stale workers on `begin_tool_call`, `seal_tool_call`, `transition_task`, and `renew_lease` (`ConcurrencyFenceError` / HTTP 409 Conflict).
    - Replayed previously executed tool calls in 0ms (< 15ms) via `ToolGateway` memoization from call journal.
    - Authored comprehensive test suite in `tests/test_cross_machine_cow_sync.py` (152/152 total repository tests passing with zero regressions).

17. **Phase 07 Remote Approval Webhooks & Interactive Decision Endpoints Complete (Plan 07-01):**
    - Implemented `WebhookConfig` and `WebhookNotifier` in `src/harness_core/webhooks.py` supporting Telegram Bot API and Discord Webhooks with dry-run mocking and resilient HTTP dispatch.
    - Added Telegram and Discord approval formatters featuring secret scrubbing, risk color codes (R3 orange, R4 red), parameter length truncation, inline buttons, and direct action URLs.
    - Enhanced `/v1/approvals/{approval_id}:decide` in `src/harness_core/server.py` supporting `GET` and `POST` methods with query params, normalization, automatic task resumption (`resume_task`), and styled HTML / JSON content negotiation.
    - Integrated outbound approval notifications into `ToolGateway` upon `PolicyDecisionType.REQUIRE_APPROVAL` and decision broadcasts upon completion.
    - Authored unit and integration test suite in `tests/test_webhooks.py` (167/167 repository tests passing with zero regressions).

18. **Phase 07 Production ASGI Server, Health Checks & Operator Runbook Complete (Plan 07-02):**
    - Added process uptime tracking (`start_time`) and `is_draining` state flag to `HarnessAPIApp`.
    - Implemented standard container probes: `/livez` liveness check (200 OK with uptime and version) and `/healthz` multi-component readiness check (database, CoW storage canary, worker heartbeats).
    - Added immediate load-shedding behavior: returns HTTP 503 Service Unavailable when draining or degraded while `/livez` remains 200 OK.
    - Implemented production `HarnessASGIAdapter` in `src/harness_core/asgi.py` implementing the complete ASGI 3.0 specification for deployment via `uvicorn` and FastAPI runtimes, supporting HTTP requests and lifespan startup/shutdown events.
    - Built `GracefulServerRunner` in `src/harness_core/server.py` intercepting `SIGINT`/`SIGTERM` to initiate graceful draining before terminating server threads without socket deadlocks.
    - Authored comprehensive 280+ line `docs/RUNBOOK.md` detailing architecture topology, DR backup/restore, SkillSpector quarantine forensics, task cancellation, `ConcurrencyFenceError` recovery, Alembic rollbacks, and systemd deployment.
    - Authored unit and integration test suite in `tests/test_production_server.py` (179/179 repository tests passing with zero regressions).

19. **Phase 08 Interactive TUI Engine & Slash Commands Complete (Plan 08-01):**
    - Implemented unified diff visualizer with colorized chunk/file headers, additions, and deletions in `src/harness_core/tui.py`.
    - Implemented two-column side-by-side diff visualizer with vertical divider line.
    - Implemented Active Graphify AST blast-radius tree renderer displaying hierarchical impact matrices with R0-R4 risk classification badges.
    - Built progressive `StreamingTokenRenderer` with live syntax highlighting and code block detection.
    - Implemented `HexConsole` pair-programming console managing session state, prompt loop (`hex> `), command history, active task tracking, and token/cost telemetry.
    - Built 10 interactive slash command handlers (`/help`, `/status`, `/diff`, `/rollback`, `/fork`, `/skills`, `/model`, `/budget`, `/clear`, `/quit`).
    - Wired `hex chat` subcommand into `src/harness_core/cli.py`.
    - Authored comprehensive test suite in `tests/test_tui.py` (210/210 repository tests passing with zero regressions).

20. **Phase 08 One-Key Keyboard Approvals, Sandbox Shell & hex init Polish Complete (Plan 08-02):**
    - Implemented `handle_approval_prompt` in `HexConsole` intercepting `ApprovalRequiredInterrupt` for privileged R3/R4 operations.
    - Built interactive one-key prompt loop: `[y] Approve`, `[n] Reject`, `[d] Full Diff`, `[s] Sandbox Shell`, `[q] Abort` with auto-resumption and clean rollback.
    - Implemented `detect_project_stack` in `src/harness_core/onboard.py` auto-discovering Python, Node/TS, Rust, and Go repository environments.
    - Implemented `init_project` scaffolding project-scoped `.hex/config.yaml` and `.hex/.gitignore` with `--force` preservation.
    - Enhanced `hex init` in `src/harness_core/cli.py` with `--workspace`, `--force`, and `--data-dir` support.
    - Added `create_approval(risk_class=...)` and `get_approval` to `LedgerStore` in `src/harness_core/db.py`.
    - Authored comprehensive test suite in `tests/test_tui_approvals_and_init.py` (225/225 total repository tests green with zero regressions).

21. **Phase 09 Telegram Bidirectional Bot Gateway Complete (Plan 09-01):**
    - Built `src/harness_core/gateways/__init__.py` and `TelegramConfig` supporting environment variable configuration.
    - Implemented `TelegramTransport` protocol with production `HttpTelegramTransport` and in-memory `MockTelegramTransport`.
    - Enforced strict operator whitelist access control (`allowed_user_ids`, `allowed_chat_ids`) with security audit logging and update drop.
    - Implemented conversational task delegation and slash command dispatch (`/start`, `/help`, `/task <goal>`, `/status [id]`, `/diff [id]`, `/cancel [id]`) in `TelegramBotGateway`.
    - Implemented interactive inline keyboard callback engine (`approve:<id>`, `reject:<id>`, `diff:<id>`, `cancel:<id>`) with in-place message edits, callback query answering, ledger recording, and automatic task resumption / CoW workspace rollback.
    - Authored unit and integration test suite in `tests/test_telegram_gateway.py` (242/242 repository tests passing with zero regressions).

22. **Phase 09 Discord Bidirectional Bot Gateway & Supervisor Complete (Plan 09-02):**
    - Implemented `DiscordConfig` and `DiscordBotGateway` in `src/harness_core/gateways/discord_bot.py` supporting `PING/PONG`, slash commands (`/task`, `/status`, `/diff`, `/cancel`, `/help`), and interactive button callbacks (`hex:approve`, `hex:reject`, `hex:diff`, `hex:cancel`) with in-place Type 7 message updates.
    - Implemented `GatewayManager` in `src/harness_core/gateways/manager.py` coordinating Telegram and Discord lifecycles with graceful signal handling (`SIGINT`/`SIGTERM`).
    - Wired `hex gateway start` and `hex gateway status` CLI subcommands in `src/harness_core/cli.py`.
    - Authored unit and integration test suite in `tests/test_discord_and_gateway_manager.py` (263/263 repository tests passing with zero regressions).

23. **Phase 10 Git & GitHub Autopilot Integration Complete (Plans 10-01 & 10-02):**
    - Implemented `GitAutopilotConfig` and `GitAutopilot` in `src/harness_core/git_autopilot.py` with automated feature branching (`hex/task-<id>-<slug>`), atomic conventional commit formatting (`type(scope): description`), rich Markdown Pull Request body generation featuring the Active Graphify AST impact matrix and Proof-Carrying Artifact verification tables, and pluggable GitHub transports (`SubprocessGitHubTransport` and `MockGitHubTransport`).
    - Wired `--pr`, `--branch`, `--base-branch`, and `--dry-run` into `hex run` and `hex exec` in `src/harness_core/cli.py`.
    - Implemented `GitHubActionConfig`, `TriggerMatch`, and `GitHubActionRunner` in `src/harness_core/github_action.py` supporting `issue_comment`, `pull_request`, `issues`, and `workflow_dispatch` payloads, bot mention detection (`@hex-bot fix ...`, `/hex fix ...`), and automatic proof-carrying response comments.
    - Added `hex gh-action` CLI subcommand supporting file/stdin event input and dry-run execution.
    - Authored unit and integration test suites in `tests/test_git_autopilot.py` (17 tests) and `tests/test_github_action_mode.py` (21 tests) (305/305 total repository tests passing with zero regressions).

---

## 2. Blockers & Dependencies

- None. Full repository test suite passing cleanly with zero failures (305/305 tests green).
- Phase 10 complete. Ready for Phase 11: Autonomous Background Automations & Hermes-Style Self-Synthesizing Skills.

---

## 3. Next Actions

- Plan Phase 11: Autonomous Background Automations & Hermes-Style Self-Synthesizing Skills (Pillar 5) via `gsd-planner`.
- Execute Plan 11-01 (Native Cron Scheduler & Daemon Installer).
- Execute Plan 11-02 (Hermes-Style Learning Loop & Skill Synthesizer with NVIDIA SkillSpector).





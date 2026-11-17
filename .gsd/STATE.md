# Current Project State: HEX Autonomous AI Workbench

**Date:** 2026-11-16  
**Current Milestone:** M15 (Resilience, Restructuring & Conversational Workflow Supremacy) — IN PROGRESS  
**Status:** Phase 38 Plan 03 Complete & Verified on branch `experimental-features` (Modals, ACP Socket Bridge & macOS Shortcuts)  
**Active Phase:** Phase 38: Native macOS GPUI Desktop Cockpit (Plans 01, 02, 03 Complete — Ready for Phase Verification)  
**Completed Milestones:** M1 through M14 Complete & Verified; Phase 36 & Phase 37 Complete; Phase 38 Implementation Complete  

---

## 1. Accomplishments to Date

1. **Clean Codebase Foundation & Prior Milestones (M1–M10):**
   - Full 16-table PostgreSQL schema, transactional LedgerStore, CoW snapshots, Active Graphify, SkillSpector, Google Research Harness², real-time hands-free voice engine, autonomous repair, 4-phase context compactor, PTY process supervisor, persistent REPL code kernel, and comprehensive fit-and-finish polish.
2. **Cognition & Self-Evolution Research (Docs & Architectural Spec):**
   - Completed exhaustive code trace of Hermes Agent soul and memory loops in `docs/research/HERMES_LEARNING_AND_SOUL_ANALYSIS.md`.
   - Designed 3-phase cognition overhaul: Multi-Tier Soul Engine (Phase 30), AST-Scoped Memory & Guardrails (Phase 31), and Closed-Loop Autonomous Learning (Phase 32).

2. **Milestone M9: Parity & Next-Gen Capabilities (Tiers C, B, A):**
   - **Tier C (Phase 21: In-Flight Sub-Agent Delegation):**
     - Role-constrained subagent lifecycle (`RESEARCHER`, `CODER`, `TESTER`, `REVIEWER`, `PLANNER`, `GENERAL`) with least-privilege tool filtering.
     - Isolated CoW workspace branching (`.hex/subagents/<subagent_id>`).
     - Gateway delegation tools (`delegate`, `subagent_status`, `subagent_wait`) and TUI `/subagents` registry view.
   - **Tier B (Phase 22: Interactive Headless Browser & Multimodal Vision Engine):**
     - Headless browser automation with accessibility trees (`ariaSnapshot` with `@e1`, `@e2` refs), click, type, screenshot, and evaluate.
     - Multimodal `VisionEngine` and `image_inspect` tool for local image files and remote URLs.
   - **Tier A (Phase 23: Agent Client Protocol (ACP) Server):**
     - Standard JSON-RPC 2.0 ACP stdio server for Zed, Neovim, VSCode, and Cursor.
     - CLI subcommand `hex acp` with `--check` diagnostics and `--setup` editor config generator.

3. **Empirical Verification:**
   - Complete 10/10 acceptance test suite passing in `tests/integration/test_harness_stages.py` covering all 5 stages.

4. **Phase 01 Migrations Baseline Complete (Plan 01-01):**
   - Configured Alembic migration infrastructure (`alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`).
   - Authored initial migration `migrations/versions/0001_initial_schema.py` representing all 16 authoritative tables with reverse foreign key downgrades.
   - Verified automated upgrade to head and downgrade to base in `tests/storage/test_alembic_migrations.py` (13/13 test suite passing).

5. **Phase 01 Dual-Driver Engine Complete (Plan 01-02):**
   - Implemented dual-driver routing (`postgresql://`, `postgres://`, `postgresql+asyncpg://` -> PostgreSQL; otherwise SQLite WAL) in `src/harness_core/db.py`.
   - Maintained zero-dependency SQLite local profile while integrating `asyncpg` connection pool with configurable limits.
   - Built query parameter translation (`?` -> `$1, $2, ...`), canonical JSON serialization, and background `_SyncRunner` bridge.
   - Implemented 27 async methods across `LedgerStore` with full sync parity and verified cryptographic hash chaining.
   - Authored comprehensive test suite in `tests/storage/test_db_dual_driver.py` (27/27 total repository tests passing).

6. **Phase 01 Concurrent Lease Claims & Concurrency Control Complete (Plan 01-03):**
   - Implemented `claim_next_task` and `claim_next_task_async` in `src/harness_core/db.py` using `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` (Postgres) and atomic `BEGIN IMMEDIATE` (SQLite).
   - Monotonic fencing token increments and atomic CAS transitions to `RUNNING` with `STATE_RUNNING` (`QUEUE_CLAIMED`) hash-chained task events.
   - Hardened `begin_tool_call` and `seal_tool_call` against stale fencing tokens, raising `ConcurrencyFenceError`.
   - Built comprehensive multi-worker concurrency stress test suite in `tests/storage/test_postgres_concurrency.py` (35/35 repository tests passing).

7. **Phase 02 CoW Providers Complete (Plan 02-01):**
   - Implemented `OverlayFSSnapshotProvider` with lower, upper, work, and merged layer mounts, unshare user namespace/FUSE support, and instant rollback.
   - Implemented `BtrfsSnapshotProvider` with read-only snapshots and subvolume branching.
   - Built platform detection factory `get_default_snapshot_provider` and dependency injection in `HarnessOrchestrator`.
   - Authored contract test suite in `tests/storage/test_cow_providers.py` (58/58 repository tests passing).

8. **Phase 02 Sandbox Isolation & ToolGateway Complete (Plan 02-02):**
   - Implemented `BubblewrapSandbox`, `DockerPodmanSandbox`, `RestrictedSubprocessSandbox`, and `get_default_sandbox` in `src/harness_core/sandbox.py`.
   - Wired sandbox containment and secret redaction into `ToolGateway._tool_run_tests` and `HarnessOrchestrator`.
   - Hardened `_resolve_confined_path` with strict `Path.relative_to` verification.
   - Authored zero-escape verification test suite in `tests/storage/test_sandbox_isolation.py` (76/76 total repository tests passing).

9. **Phase 03 MCP Client Adapters & Dynamic Tool Discovery Complete (Plan 03-01):**
   - Implemented `MCPClientAdapter`, `StdioMCPTransport`, `HttpSSEMCPTransport`, and dynamic `R0–R4` risk mapping with heuristic rules and per-server overrides in `src/harness_core/mcp_client.py`.
   - Built thread-safe JSON-RPC 2.0 message demuxing, stdout reader thread, rolling stderr diagnostics, and timeout enforcement.
   - Authored mock MCP server and unit test suite in `tests/tools/test_mcp_client.py` verifying handshake, discovery, tool execution, timeouts, and clean process teardown (82/82 repository tests passing).

10. **Phase 03 SkillSpector Manifest Validation & 0ms Call Journaling Complete (Plan 03-02):**
    - Enhanced `SkillSecurityScanner` in `src/harness_core/skillspector.py` with `scan_mcp_manifest`, SHA-256 manifest pinning, and injection/semantic quarantine rules.
    - Integrated `register_mcp_server` into `ToolGateway` in `src/harness_core/gateway.py` with R0–R4 default-deny policy, secret redaction, and 0ms call-level journal memoization.
    - Authored end-to-end integration test suite in `tests/tools/test_mcp_gateway_journal.py` verifying prompt-injection defense, 0ms replay (< 15 ms, 0 subprocess calls), approval interrupts, and R0–R4 policy enforcement (92/92 repository tests passing).

11. **Phase 04 Local MLX Model Provider & Log Compression Engine Complete (Plan 04-01):**
    - Implemented `ModelProvider` base protocol, `ModelUsage`, `ModelResponse`, and `ProviderCapabilities` in `src/harness_core/providers/base.py`.
    - Implemented `LocalMLXModelProvider` in `src/harness_core/providers/mlx_provider.py` with Darwin ARM64 discovery, unified memory detection, lazy `mlx_lm` loading, and pure-Python zero-dependency fallback generation.
    - Implemented `Tier1LogCompressor` in `src/harness_core/providers/mlx_provider.py` with secret redaction, multi-pattern trace and assertion extraction, local AST source context inspection, and >70% token reduction (91.03% verified).
    - Authored unit and contract test suite in `tests/providers/test_model_providers.py` (107/107 total repository tests passing).

12. **Phase 04 Cloud API Connectors, Per-Task Budget Ceilings, Privacy Gating & Router Integration Complete (Plan 04-02):**
    - Implemented `CloudModelProvider` in `src/harness_core/providers/cloud_provider.py` supporting Anthropic Claude, OpenAI, and Google Gemini.
    - Implemented `TaskBudgetTracker` with hard ceilings for task USD cost, output tokens, and request counts, raising `BudgetExceededError`.
    - Enforced strict privacy gating blocking `sensitive` and `secret` classifications from cloud egress, raising `PolicyBlockedError`.
    - Integrated outbound secret scrubbing with `redact_secrets` before serialization.
    - Enhanced `TwoTierModelRouter` and `RouteDecision` in `src/harness_core/router.py` to record latency, cost, tokens, and compression ratio.
    - Authored live 500-line pytest compression and privacy benchmark in `tests/integration/test_two_tier_compression_benchmark.py` and cloud tests in `tests/providers/test_model_providers.py` (121/121 total repository tests passing).

13. **Phase 05 Multi-Turn Autonomous Repair Loop & Retry Escalation Complete (Plan 05-01):**
    - Implemented `generate_repair_patch` and `_parse_patch_operations` in `src/harness_core/orchestrator.py` integrating Tier-1 M4 log compression, Active Graphify AST blast-radius warnings, and model router dispatch.
    - Built iterative multi-turn repair loop in `HarnessOrchestrator._execute_standard_dag` cycling `VERIFYING -> RUNNING -> VERIFYING` with isolated CoW patch composition and verification re-execution.
    - Implemented retry exhaustion and budget ceiling handling: freezes dirty workspace, creates immutable forensic CoW snapshot and checkpoint, protects base workspace, and safely escalates to `NEEDS_ATTENTION` with `REPAIR_RETRIES_EXHAUSTED` or `BUDGET_EXHAUSTED`.
    - Hardened `LedgerStore.transition_task` and `transition_task_async` to preserve `terminal_reason` on interrupts.
    - Authored comprehensive test suite in `tests/core/test_autonomous_repair.py` covering happy path repairs, retry exhaustion, compound repairs, and budget escalation (127/127 total repository tests passing).

14. **Phase 05 Dynamic DAG Replanning & Selective Subgraph Invalidation Complete (Plan 05-02):**
    - Implemented `replan_dag` in `src/harness_core/orchestrator.py` with directed reachability analysis, unexpected side-effects detection (`actual - declared`), and dynamic DAG dependency resolver.
    - Built selective subgraph invalidation: marks only transitive downstream steps as `INVALIDATED` for re-execution while preserving independent completed steps as `COMPLETED`.
    - Integrated dynamic blast-radius test pinning via Active Graphify into `blast_radius_checks`.
    - Maintained monotonic plan revision increments (`revision`, `supersedes_revision`) and cryptographically hash-chained `PLAN_REVISED` task events.
    - Enhanced `LedgerStore` in `src/harness_core/db.py` with `PlanNode` serialization, `get_plan_revision`, and `list_plans`.
    - Authored comprehensive test suite in `tests/core/test_dag_replanning.py` (132/132 total repository tests passing with zero failures).

15. **Phase 06 Distributed Worker Daemon & Heartbeat Loop Complete (Plan 06-01):**
    - Implemented atomic `renew_lease`, `release_lease`, and `heartbeat_worker` in `src/harness_core/db.py` with CAS fencing validation and monotonic lease extensions.
    - Added REST endpoints in `src/harness_core/server.py` (`POST /v1/workers/register`, `POST /v1/workers/heartbeat`, `POST /v1/tasks/claim`, `POST /v1/leases/{id}:renew`, `POST /v1/leases/{id}:release`) with case-insensitive `X-Worker-Token` auth and HTTP 409 Conflict mapping.
    - Implemented `WorkerDaemon` architecture in `src/harness_core/worker_daemon.py` with machine profiling (M4 Air, GPU, Server), CoW backend discovery, periodic heartbeat loop, task polling with `--once` support, and signal handling (`SIGINT`/`SIGTERM`).
    - Implemented dual client transports: `DirectLedgerWorkerClient` and `RestWorkerClient`.
    - Added `harness worker` CLI command in `src/harness_core/cli.py`.
    - Authored comprehensive test suite in `tests/runtime/test_worker_daemon.py` (140/140 total repository tests passing with zero failures).

16. **Phase 06 Cross-Machine CoW Delta Sync, Lease Handover & Fencing Defense Complete (Plan 06-02):**
    - Implemented `import_delta_archive` and `export_delta_archive` across all snapshot providers in `src/harness_core/cow.py` with tar-slip traversal protection and Merkle tree SHA verification.
    - Added CAS artifact transfer endpoints in `src/harness_core/server.py` (`POST /v1/artifacts/delta`, `GET /v1/artifacts/{id}/file`) and methods in `src/harness_core/worker_daemon.py`.
    - Implemented `reap_expired_leases` in `src/harness_core/db.py` for automated re-queuing of abandoned tasks upon lease expiration.
    - Implemented lease handover with strictly monotonic fencing token increments (1 -> 2) and `LEASE_TAKEOVER` task events.
    - Integrated remote workspace reconstruction from CAS delta archives in `src/harness_core/orchestrator.py` with tree SHA validation.
    - Enforced monotonic fencing token defense rejecting stale workers on `begin_tool_call`, `seal_tool_call`, `transition_task`, and `renew_lease` (`ConcurrencyFenceError` / HTTP 409 Conflict).
    - Replayed previously executed tool calls in 0ms (< 15ms) via `ToolGateway` memoization from call journal.
    - Authored comprehensive test suite in `tests/integration/test_cross_machine_cow_sync.py` (152/152 total repository tests passing with zero regressions).

17. **Phase 07 Remote Approval Webhooks & Interactive Decision Endpoints Complete (Plan 07-01):**
    - Implemented `WebhookConfig` and `WebhookNotifier` in `src/harness_core/webhooks.py` supporting Telegram Bot API and Discord Webhooks with dry-run mocking and resilient HTTP dispatch.
    - Added Telegram and Discord approval formatters featuring secret scrubbing, risk color codes (R3 orange, R4 red), parameter length truncation, inline buttons, and direct action URLs.
    - Enhanced `/v1/approvals/{approval_id}:decide` in `src/harness_core/server.py` supporting `GET` and `POST` methods with query params, normalization, automatic task resumption (`resume_task`), and styled HTML / JSON content negotiation.
    - Integrated outbound approval notifications into `ToolGateway` upon `PolicyDecisionType.REQUIRE_APPROVAL` and decision broadcasts upon completion.
    - Authored unit and integration test suite in `tests/capabilities/test_webhooks.py` (167/167 repository tests passing with zero regressions).

18. **Phase 07 Production ASGI Server, Health Checks & Operator Runbook Complete (Plan 07-02):**
    - Added process uptime tracking (`start_time`) and `is_draining` state flag to `HarnessAPIApp`.
    - Implemented standard container probes: `/livez` liveness check (200 OK with uptime and version) and `/healthz` multi-component readiness check (database, CoW storage canary, worker heartbeats).
    - Added immediate load-shedding behavior: returns HTTP 503 Service Unavailable when draining or degraded while `/livez` remains 200 OK.
    - Implemented production `HarnessASGIAdapter` in `src/harness_core/asgi.py` implementing the complete ASGI 3.0 specification for deployment via `uvicorn` and FastAPI runtimes, supporting HTTP requests and lifespan startup/shutdown events.
    - Built `GracefulServerRunner` in `src/harness_core/server.py` intercepting `SIGINT`/`SIGTERM` to initiate graceful draining before terminating server threads without socket deadlocks.
    - Authored comprehensive 280+ line `docs/guides/RUNBOOK.md` detailing architecture topology, DR backup/restore, SkillSpector quarantine forensics, task cancellation, `ConcurrencyFenceError` recovery, Alembic rollbacks, and systemd deployment.
    - Authored unit and integration test suite in `tests/client/test_production_server.py` (179/179 repository tests passing with zero regressions).

19. **Phase 08 Interactive TUI Engine & Slash Commands Complete (Plan 08-01):**
    - Implemented unified diff visualizer with colorized chunk/file headers, additions, and deletions in `src/harness_core/tui.py`.
    - Implemented two-column side-by-side diff visualizer with vertical divider line.
    - Implemented Active Graphify AST blast-radius tree renderer displaying hierarchical impact matrices with R0-R4 risk classification badges.
    - Built progressive `StreamingTokenRenderer` with live syntax highlighting and code block detection.
    - Implemented `HexConsole` pair-programming console managing session state, prompt loop (`hex> `), command history, active task tracking, and token/cost telemetry.
    - Built 10 interactive slash command handlers (`/help`, `/status`, `/diff`, `/rollback`, `/fork`, `/skills`, `/model`, `/budget`, `/clear`, `/quit`).
    - Wired `hex chat` subcommand into `src/harness_core/cli.py`.
    - Authored comprehensive test suite in `tests/client/test_tui.py` (210/210 repository tests passing with zero regressions).

20. **Phase 08 One-Key Keyboard Approvals, Sandbox Shell & hex init Polish Complete (Plan 08-02):**
    - Implemented `handle_approval_prompt` in `HexConsole` intercepting `ApprovalRequiredInterrupt` for privileged R3/R4 operations.
    - Built interactive one-key prompt loop: `[y] Approve`, `[n] Reject`, `[d] Full Diff`, `[s] Sandbox Shell`, `[q] Abort` with auto-resumption and clean rollback.
    - Implemented `detect_project_stack` in `src/harness_core/onboard.py` auto-discovering Python, Node/TS, Rust, and Go repository environments.
    - Implemented `init_project` scaffolding project-scoped `.hex/config.yaml` and `.hex/.gitignore` with `--force` preservation.
    - Enhanced `hex init` in `src/harness_core/cli.py` with `--workspace`, `--force`, and `--data-dir` support.
    - Added `create_approval(risk_class=...)` and `get_approval` to `LedgerStore` in `src/harness_core/db.py`.
    - Authored comprehensive test suite in `tests/client/test_tui_approvals_and_init.py` (225/225 total repository tests green with zero regressions).

21. **Phase 09 Telegram Bidirectional Bot Gateway Complete (Plan 09-01):**
    - Built `src/harness_core/gateways/__init__.py` and `TelegramConfig` supporting environment variable configuration.
    - Implemented `TelegramTransport` protocol with production `HttpTelegramTransport` and in-memory `MockTelegramTransport`.
    - Enforced strict operator whitelist access control (`allowed_user_ids`, `allowed_chat_ids`) with security audit logging and update drop.
    - Implemented conversational task delegation and slash command dispatch (`/start`, `/help`, `/task <goal>`, `/status [id]`, `/diff [id]`, `/cancel [id]`) in `TelegramBotGateway`.
    - Implemented interactive inline keyboard callback engine (`approve:<id>`, `reject:<id>`, `diff:<id>`, `cancel:<id>`) with in-place message edits, callback query answering, ledger recording, and automatic task resumption / CoW workspace rollback.
    - Authored unit and integration test suite in `tests/capabilities/test_telegram_gateway.py` (242/242 repository tests passing with zero regressions).

22. **Phase 09 Discord Bidirectional Bot Gateway & Supervisor Complete (Plan 09-02):**
    - Implemented `DiscordConfig` and `DiscordBotGateway` in `src/harness_core/gateways/discord_bot.py` supporting `PING/PONG`, slash commands (`/task`, `/status`, `/diff`, `/cancel`, `/help`), and interactive button callbacks (`hex:approve`, `hex:reject`, `hex:diff`, `hex:cancel`) with in-place Type 7 message updates.
    - Implemented `GatewayManager` in `src/harness_core/gateways/manager.py` coordinating Telegram and Discord lifecycles with graceful signal handling (`SIGINT`/`SIGTERM`).
    - Wired `hex gateway start` and `hex gateway status` CLI subcommands in `src/harness_core/cli.py`.
    - Authored unit and integration test suite in `tests/capabilities/test_discord_and_gateway_manager.py` (263/263 repository tests passing with zero regressions).

23. **Phase 10 Git & GitHub Autopilot Integration Complete (Plans 10-01 & 10-02):**
    - Implemented `GitAutopilotConfig` and `GitAutopilot` in `src/harness_core/git_autopilot.py` with automated feature branching (`hex/task-<id>-<slug>`), atomic conventional commit formatting (`type(scope): description`), rich Markdown Pull Request body generation featuring the Active Graphify AST impact matrix and Proof-Carrying Artifact verification tables, and pluggable GitHub transports (`SubprocessGitHubTransport` and `MockGitHubTransport`).
    - Wired `--pr`, `--branch`, `--base-branch`, and `--dry-run` into `hex run` and `hex exec` in `src/harness_core/cli.py`.
    - Implemented `GitHubActionConfig`, `TriggerMatch`, and `GitHubActionRunner` in `src/harness_core/github_action.py` supporting `issue_comment`, `pull_request`, `issues`, and `workflow_dispatch` payloads, bot mention detection (`@hex-bot fix ...`, `/hex fix ...`), and automatic proof-carrying response comments.
    - Added `hex gh-action` CLI subcommand supporting file/stdin event input and dry-run execution.
    - Authored unit and integration test suites in `tests/capabilities/test_git_autopilot.py` (17 tests) and `tests/capabilities/test_github_action_mode.py` (21 tests) (305/305 total repository tests passing with zero regressions).

24. **Phase 11 Native Cron Scheduler & Daemon Installer Complete (Plan 11-01):**
    - Implemented zero-dependency 5-field `CronParser` in `src/harness_core/scheduler.py` supporting wildcards, steps, ranges, lists, step ranges, and jump evaluation.
    - Implemented `ScheduledJob` and `TaskScheduler` engine with atomic JSON persistence (`schedules.json`), due job execution via `HarnessOrchestrator`, and webhook alert dispatch on failure.
    - Added `send_alert` method in `src/harness_core/webhooks.py` for Telegram HTML and Discord embed failure alerting.
    - Implemented `DaemonConfig` and `DaemonInstaller` in `src/harness_core/daemon.py` generating macOS LaunchAgent plist XML and Linux systemd service units with environment forwarding and dry-run safety.
    - Wired `hex schedule` (add, list, remove, run-due) and `hex daemon` (install, status) subcommands into `src/harness_core/cli.py`.
    - Authored 25 unit and integration tests in `tests/runtime/test_scheduler_and_daemon.py` (330/330 total repository tests passing with zero regressions).

25. **Phase 11 Autonomous Learning Loop & Skill Synthesizer Complete (Plan 11-02):**
    - Implemented `SynthesizedSkill` and `SkillSynthesizer` in `src/harness_core/skill_synthesizer.py` extracting task execution context, tool call journals, git diffs, AST impact, and test verification standards.
    - Implemented structured `SKILL.md` generator with YAML frontmatter (`name`, `purpose`, `risk_class`, `allowed_tools`, `trigger`) and markdown workflow body.
    - Integrated **Mandatory NVIDIA SkillSpector Gate** (`SkillSecurityScanner`) validating static rules, prompt injection (`SS-INJ-`), shell exfiltration (`SS-EXF-`), and semantic alignment before registry writes.
    - Routed clean skills (`gate_decision == 'ALLOW'`, score < 25) to `skills/custom/<name>/SKILL.md` with active ledger registration; routed adversarial/suspicious skills (`gate_decision in ('QUARANTINE', 'REQUIRE_APPROVAL')`, score >= 25) to `.hex/quarantine/<name>/SKILL.md` with structured `audit.json`.
    - Wired `hex skill` (`synthesize`, `list`, `scan`) subcommands into `src/harness_core/cli.py` with SkillSpector status badges (`[ACTIVE]`, `[QUARANTINED]`, `[PENDING]`).
    - Authored 16 unit and integration tests in `tests/skills/test_skill_synthesis.py` (346/346 total repository tests passing with zero regressions).

26. **Phase 12 System Diagnostics, Multi-Framework Recipes & Graphify Persistent Cache Complete (Plan 12-01):**
    - Implemented `HexDoctor`, `DoctorReport`, and `DoctorCheckResult` in `src/harness_core/doctor.py` diagnosing Python runtime, Apple Silicon M4 / GPU, CoW snapshot providers, sandboxes, credentials, git, storage, and workbench directories.
    - Added `--fix` self-healing and `--live` bounded connectivity pings.
    - Implemented `Recipe` and `detect_recipe` in `src/harness_core/recipes.py` for Python (uv, poetry, pytest, django), Node.js (pnpm, yarn, bun, npm, Next.js, Vite), Rust (cargo), Go (go test), Makefile, and Docker Compose.
    - Wired recipe auto-detection into `ToolGateway._tool_run_tests` and `HarnessOrchestrator`.
    - Wired `hex doctor` and `hex recipe` CLI subcommands in `src/harness_core/cli.py`.
    - Implemented `.hex/graph.json` persistent disk caching (< 30ms warm reloads) and polyglot symbol extraction in `src/harness_core/graphify.py`.
    - Authored 11 unit and integration tests in `tests/client/test_doctor_and_recipes.py` (370/370 repository tests passing with zero regressions).

27. **Phase 12 SQLite FTS5 Full-Text Task Search & State Backup/Dump Complete (Plan 12-02):**
    - Implemented `TaskSearchIndex` in `src/harness_core/search.py` with `task_search_fts` SQLite FTS5 virtual table, unicode61 tokenizer diacritic removal, BM25 ranking, and highlighted match snippet extraction.
    - Built robust query sanitization `sanitize_fts5_query` defending against special character syntax injection while preserving quoted phrases and prefix tokens.
    - Integrated automatic task and tool call transcript indexing into `LedgerStore` (`src/harness_core/db.py`) with full historical `reindex_all`.
    - Implemented state backup and restore in `src/harness_core/backup.py` (`create_backup` timestamped archives with WAL checkpointing, `restore_backup` with strict Zip-Slip path traversal defense, and `generate_dump` plain-text system diagnostic report with secret scrubbing).
    - Wired `hex search`, `hex backup`, `hex restore`, and `hex dump` subcommands into `src/harness_core/cli.py`.
    - Authored 16 unit and integration tests in `tests/storage/test_search_and_backup.py` (386/386 full repository tests passing with zero regressions).

28. **Phase 13 Interactive Auto-Setup, Web Search Tool & Real-Time Environment Hydration Complete (Plans 13-01 & 13-02):**
    - Implemented `TerminalUISelector` in `src/harness_core/ui_selector.py` with POSIX termios/tty arrow-key navigation (`↑`/`↓` and `k`/`j`), `[•]` radio display, multi-select `[✓]`, and masked secret input (`••••••••`).
    - Implemented `load_hex_environment` in `src/harness_core/config_loader.py` discovering `credentials.env` and nested `config.yaml` without external dependencies, hydrating `os.environ` on every CLI invocation.
    - Implemented autonomous multi-engine `web_search` in `src/harness_core/web_search.py` (DuckDuckGo zero-key fallback, Tavily, and Brave Search) and registered as an R1 tool in `ToolGateway`.
    - Upgraded `run_onboard_wizard` in `src/harness_core/onboard.py` into a 4-step arrow-key wizard (Model Provider, Web Search Engine, Remote Messaging Gateways, and Hardware Profile).
    - Wired first-run auto-setup interceptor into `cli.py` to automatically detect unconfigured first-run states on bare `hex` or `hex chat` and guide setup cleanly.
    - Completely purged all hardcoded mock artifacts (`v1.3.0`, `M4 MLX`, `wrk_m4_air_local`, `mock-key`).
    - Authored 21 unit and integration tests across `tests/capabilities/test_ui_selector_and_web_search.py` and `tests/client/test_auto_setup_and_polish.py` (415/415 total repository tests passing with zero regressions).

29. **Phase 14 Antigravity Interactive OAuth, Model Selection & TUI Crash Hardening Complete (Plans 14-01 & 14-02):**
    - Implemented `_probe_antigravity_token` in `src/harness_core/onboard.py` to validate OAuth tokens before declaring active session (included subscription quota).
    - Added interactive browser PKCE OAuth 2.0 handoff (`webbrowser.open`) and manual token entry in onboarding when unauthenticated.
    - Added interactive model selection menus for Antigravity (`gemini-2.5-pro` default, `gemini-2.5-flash`, `claude-3-5-sonnet`), Anthropic, OpenAI, and Gemini; purged all fictitious `gemini-3.5-pro` references.
    - Streamlined setup menus: simplified search option label to `"Disabled"` and removed redundant execution sandbox interactive prompt.
    - Hardened TUI against provider errors: `handle_user_prompt` in `src/harness_core/tui.py` catches `ProviderUnavailableError` and HTTP 401 exceptions, rendering a styled error card with remediation tips without crashing the REPL loop.
    - Added automatic token refresh in `AntigravityModelProvider.generate()` on HTTP 401 when `refresh_token` exists, silently recovering expired sessions.
    - Authored 7 unit and integration tests in `tests/integration/test_antigravity_oauth_and_tui_resilience.py` (422/422 total repository tests passing with zero regressions).

30. **Phase 15 Live Model Discovery, Quota Dashboard & Project Workspaces Complete (Plans 15-01 & 15-02):**
    - Implemented live model discovery via official provider APIs (`fetch_available_models`) with in-memory caching and fallback to curated lists for Antigravity, Anthropic, OpenAI, and Gemini.
    - Implemented `AntigravityQuotaSnapshot`, sliding-window quota calculation, and reset timer estimation. Added `hex quota` CLI and `/quota` TUI commands.
    - Added OAuth token acquisition flow in onboarding wizard before presenting model choice.
    - Implemented persistent project workspace registry `ProjectRegistry` in `src/harness_core/project_registry.py` backed by `~/.hex/projects.json`.
    - Added enclosing project auto-detection (`detect_enclosing_project`) and interactive daily project switcher ("What project do you want to work on today?").
    - Enforced folder-scoped context isolation under `<repo>/.hex/` with `config.yaml`, `graph.json`, `reflections/LESSONS.md`, and `data/`.
    - Added `hex project` CLI (`list`, `add`, `use`, `switch`) and TUI `/project` command.
    - Globally migrated command from `hex onboard` to `hex setup` (preserving `hex onboard` as backwards-compatible alias).
    - Authored comprehensive test suite in `tests/storage/test_project_registry_and_workspace.py` (20 tests) and `tests/client/test_live_model_discovery.py` (13 tests) (455/455 total repository tests passing with zero regressions).

31. **Phase 16 Polyglot Language Server Protocol (LSP) Gateway Complete (Plans 16-01 & 16-02):**
    - Implemented zero-dependency `StdioLSPTransport` with HTTP-style `Content-Length` framing, request/response demuxing, and background IO threads (`src/harness_core/lsp/client.py`).
    - Built `LanguageServerRegistry` supporting 6 major language families: Python (`pyright`, `pylsp`, `ruff`), TypeScript/JavaScript (`typescript-language-server`, `vtsls`), Go (`gopls`), Rust (`rust-analyzer`), C/C++ (`clangd`), and Java (`jdtls`).
    - Implemented `LSPManager` managing per-workspace server lifecycles, document sync (`didOpen`, `didChange`), and diagnostic aggregation (`publishDiagnostics`).
    - Registered 4 read-only R1 tools in `ToolGateway`: `lsp_diagnostics`, `lsp_definition`, `lsp_references`, and `lsp_hover`.
    - Integrated compiler pre-flight gate into `HarnessOrchestrator` repair loop: short-circuits test runs on compiler syntax/type errors and records hash-chained `LSP_COMPILER_PREFLIGHT_FAILED` task events.
    - Added CLI command `hex lsp` (`status`, `diagnostics`, `definition`, `hover`) and TUI `/lsp` slash command.
    - Authored 25 unit and integration tests across `tests/lsp/test_lsp_client.py` and `tests/lsp/test_lsp_gateway_and_repair.py` (480/480 repository tests passing with zero regressions).

32. **Phase 17 Real-Time Hands-Free Voice & Speech Pair-Programming Complete (Plans 17-01 & 17-02):**
    - Implemented `VoiceConfig` and asynchronous `VoiceEngine` worker thread with zero external C/audio dependencies (`src/harness_core/voice.py`).
    - Built platform speech synthesis supporting macOS `say`, Linux `espeak`/`aplay`, and mock fallback, with sequential non-blocking playback, rate clamping (80-400 wpm), and instant mute toggles.
    - Implemented `clean_text_for_speech` sanitizing markdown headers, bold/italic markers, raw URLs, and replacing raw code fences with natural spoken placeholders.
    - Added 9th diagnostic category ("Audio Hardware & Speech") to `HexDoctor` in `src/harness_core/doctor.py`.
    - Added `/voice` TUI slash command suite (`on`, `off`, `mute`, `status`, `rate`, `voice`) and dynamic status bar badges (`🎙️ ON`, `🔇 MUTED`, `🎙️ OFF`).
    - Added CLI standalone command `hex voice` and `--voice`, `--voice-name`, `--speech-rate` options across `hex` and `hex chat`.
    - Authored unit and integration tests in `tests/capabilities/test_voice_engine.py` (491/491 total repository tests passing with zero regressions).

33. **Phase 18 Google Research Harness² Deep Wiring Complete (Plans 18-01 & 18-02):**
    - Integrated `harness2.core.library.Library` directly into `HarnessOrchestrator` (`src/harness_core/orchestrator.py`).
    - Stored domain experience libraries under `.hex/harness2_library/` with automated post-task experience recording (`cue`, `lesson`, `evidence`, and adopted tree), rebuilding `INDEX.md` and `EXPERIENCE.md`.
    - Wired pre-task domain experience retrieval into autonomous repair loops and reasoning prompts, preserving strict domain isolation.
    - Implemented `OrchestratorBenchmarkAdapter` (`src/harness_core/harness2_adapter.py`) strictly conforming to `harness2.core.spec.BenchmarkAdapter`.
    - Supported isolated rollout workspaces, `[ACTION]/[OBSERVATION]/[ERROR]` trajectory logs, deliverable checks, and scoring.
    - Connected Propose-Probe-Compose multi-rollout sweeps (`run_parallel`, `run_sequential`) and exposed `hex harness2-eval` CLI command.
    - Authored 17 unit and integration tests across `tests/integration/test_harness2_library_wiring.py` and `tests/integration/test_harness2_benchmark_adapter.py` (508/508 repository tests passing with zero regressions).

34. **Phase 19 NVIDIA SkillSpector Behavioral AST & Live OSV Scanner Complete (Plans 19-01 & 19-02):**
    - Integrated `skillspector.python_ast.get_python_ast` and `skillspector.nodes.analyzers.behavioral_ast._analyze_python` into `SkillSpectorScanner.scan_bundle()` (`src/harness_core/skillspector.py`).
    - Replaced regex heuristics with AST taint tracking for dangerous builtins (`eval`, `exec`), subprocess invocation (`os.system`, `subprocess.Popen`), insecure deserialization (`pickle.loads`), dynamic imports, and reflective sinks (`getattr(os, "system")`).
    - Wired `skillspector.nodes.analyzers.osv_client.query_batch()` into manifest parsing (`requirements.txt`, `manifest.yaml`, `SKILL.md`) for real-time OSV.dev batch CVE querying with offline cache fallback to `KNOWN_OSV_VULNS`.
    - Authored 10 unit and integration tests across `tests/skills/test_skillspector_ast_wiring.py` and `tests/skills/test_skillspector_osv_wiring.py` (529/529 repository tests passing with zero regressions).

35. **Phase 20 Graphify Upstream CLI & Tree-Sitter Adapter Complete (Plans 20-01 & 20-02):**
    - Implemented `GraphifyUpstreamRunner` in `src/harness_core/graphify.py`: binary discovery (`which graphify`), `sys.executable -m graphify` with `vendor/graphify` on `PYTHONPATH`, timeout protection, and in-process fallback using `graphify.watch._rebuild_code`.
    - Implemented `ActiveGraphifyAdapter.ingest_upstream_graph`: ingests `graphify-out/graph.json` or `.hex/graph.json` into NetworkX `DiGraph`, reconciling nodes, edges, `SymbolDef` instances, callers, and qualification paths, preserving fast 0ms native Python AST fallback when absent.
    - Added full `hex graphify` CLI suite (`status`, `update`, `ingest`, `god-nodes`, `affected`, `tree`) in `src/harness_core/cli.py`.
    - Unified polyglot blast-radius tracking in `ActiveGraphifyAdapter.compute_blast_radius` across Python, TypeScript/JavaScript, Go, Rust, Java, and C/C++, with multi-language test detection (`_is_test_file`).
    - Wired `ActiveBlastRadiusInterceptor` and `ToolGateway.invoke` for `write_file` to intercept polyglot file writes and journal pinned tests into the active ledger plan.
    - Authored 10 unit and integration tests across `tests/tools/test_graphify_upstream_wiring.py` and `tests/tools/test_graphify_polyglot_blast_radius.py` (542/542 repository tests passing with zero regressions).

36. **Phase 24: Continuous Multi-Turn ReAct Tool Loop Complete (Plans 24-01 & 24-02):**
    - Implemented `ReActConfig`, `ToolCallRequest`, `ToolCallResult`, `ReActTurn`, `ReActSessionResult`, and `ReActLoop` in `src/harness_core/react_loop.py` with multi-format tool call parser (XML tags, markdown code blocks, JSON dictionaries, embedded JSON, and raw strings) and observation history chaining.
    - Added `run_agent_loop` to `HarnessOrchestrator` in `src/harness_core/orchestrator.py` backed by router dispatch and deterministic fallback with valid `LedgerStore` CAS state transitions (`SUBMITTED -> VALIDATING -> RUNNING -> VERIFYING -> SUCCEEDED / FAILED`).
    - Added `/react` slash command, live tool invocation/observation terminal progress cards, and `react_mode` to `HexConsole` in `src/harness_core/tui.py`.
    - Added `--react` flag to root `hex`, `hex run`, and `hex chat` in `src/harness_core/cli.py`.
    - Authored 31 unit and integration tests in `tests/core/test_react_loop.py` (646/646 full repository tests passing with zero regressions).

37. **Phase 25: Native Rust Terminal Cockpit & Protocol Crate Complete (Plans 25-01 & 25-02):**
    - Established Cargo workspace root with `crates/hex-protocol` and `crates/hex-tui`.
    - Implemented `crates/hex-protocol` with strongly typed JSON-RPC 2.0 ACP client (`AcpClient`), models (`Role`, `ContentBlock`, `Message`, `SessionUpdateNotification`, etc.), and unified/side-by-side diff parser (`UnifiedDiff`, `DiffHunk`, `DiffFile`, `SideBySideRow`).
    - Implemented `crates/hex-tui` in Rust using Ratatui 0.29 and Crossterm 0.28:
      - Sticky header with styled ASCII banner, model routing, CoW snapshot mode, monotonic fencing token, and ACP connection status.
      - Scrollable dialogue history viewport with markdown styles, role badges (`[USER]`, `[ASSISTANT]`, `[SYSTEM]`, `[TOOL]`), and collapsible tool call blocks showing arguments, duration in milliseconds, and exit status.
      - Colorized unified diff viewer rendering added lines in green, deleted lines in red, context lines, and file headers.
      - Multi-line interactive command prompt with cursor position, history navigation (`↑`/`↓`), and slash-command suggestion popup (`/help`, `/status`, `/diff`, `/rollback`, `/fork`, `/skills`, `/model`, `/budget`, `/react`, `/clear`, `/quit`).
      - Bottom status bar with keybinding hints (`[Enter] Submit`, `[Tab] Focus`, `[F1] Help`, `[Ctrl+C] Quit`, `[y/n] Approve/Reject`).
      - Centered modal dialog for R3/R4 operator approval interrupts.
      - Headless verification `--check` flag rendering a virtual test frame into `ratatui::backend::TestBackend` and exiting 0.
    - Built Python integration in `src/harness_core/cli.py` and `src/harness_core/tui_launcher.py`:
      - Registered `hex tui` subcommand and `--ratatui` flag to `hex chat`.
      - Binary resolver checking `HEX_TUI_BIN`, build targets, PATH, and on-demand `cargo build -p hex-tui`.
    - Authored comprehensive test suites:
      - 26 Rust tests across `hex-protocol` and `hex-tui` (`cargo test --workspace`).
      - 8 Python launcher integration tests in `tests/client/test_rust_tui_launcher.py`.
      - 654/654 Python repository tests passing cleanly with zero regressions.

38. **Phase 26: 4-Phase Context Compactor & Prompt Cache Locking Complete (Plans 26-01 & 26-02):**
    - Implemented production-grade 4-phase context compactor matching Hermes Agent (`src/harness_core/context_compressor.py`):
      - **Phase 1 (Stale Tool Demotion):** Truncates bulky tool outputs older than protected recent rounds (`keep_tool_rounds=4`) exceeding `min_demote_chars=1000`, inserting non-imitable elision markers (`⟪HEX-CONTEXT-COMPRESSION: ...⟫`), head+tail previews, and exact omitted line/char metrics. Tool failure outputs (`is_error=True`, `Traceback`, `Error:`) are strictly preserved.
      - **Phase 2 (Atomic Tool-Pair Boundary Protection):** Discovers atomic call-and-response clusters across structured JSON tool calls and XML tags (`<tool_call>` <-> `<observation>`). Forward and backward boundary alignments (`align_boundary_forward`, `align_boundary_backward`) ensure no cut index splits an assistant tool invocation from its responses, preventing provider HTTP 400 Bad Request errors.
      - **Phase 3 (Middle-Turn Summarization & Core Engine):** Dynamically plans compaction windows between protected system instructions and recent tail turns, summarizing intermediate turns using a structured model prompt or zero-dependency deterministic fallback digest. Still-active user objectives are restated past the summary boundary so the agent never abandons work. Anti-thrashing guards revert to original history if compaction fails to reduce tokens.
      - **Phase 4 (Immutable Prompt-Cache Prefixing & Provider Breakpoints):** Enforces byte-for-byte prefix stability (`stabilize_prompt_cache_prefix`) with canonical system instructions at index 0 and deterministic tool schema JSON serialization, achieving 100% constant SHA-256 signatures across turns (>90% KV cache hit rate). Automatically injects Anthropic ephemeral cache control breakpoints (`"cache_control": {"type": "ephemeral"}`) onto the last tool, system prompt, middle summary, and tail turn within the 4-breakpoint limit.
    - Integrated across the runtime:
      - `ReActLoop`: Automatic compaction when token usage reaches 80% of budget.
      - `TwoTierModelRouter`: `get_context_compactor()` and `compact_context()`.
      - `HarnessOrchestrator`: Wired into `run_agent_loop`.
      - `HexConsole` TUI: `/compress` slash command and compaction status indicators.
    - Authored comprehensive test suites:
      - 8 unit tests in `tests/cognition/test_context_compressor_tail_pruning.py`.
      - 6 integration tests in `tests/cognition/test_context_compressor_integration.py`.
      - Verified 685 Python tests and 34 Rust tests passing cleanly with zero regressions.

39. **Phase 33 Plan 33-01: Execution Gates & Session Isolation Complete (Fixes F-01, F-04, F-08):**
    - **Deterministic Skill Verification Hooks (F-01):**
      - Wired post-edit verification hook execution into `HarnessOrchestrator._execute_standard_dag()` during task verification and repair loops.
      - Enforced sandbox execution (`RestrictedSubprocessSandbox`, Bubblewrap, Docker) and minimal allowlisted environment stripping all provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`), cloud credentials, database URLs, and SSH configs.
      - Integrated SkillSpector SHA-256 bundle hash validation before hook execution, rejecting tampered or unapproved bundles with `SecurityError`.
      - Gated task transition on `must_pass: True`: hook failure records `SKILL_VERIFICATION_FAILED` and blocks `SUCCEEDED`.
    - **Session-Scoped Turn-0 Memory Snapshots (F-04):**
      - Keyed `MemorySnapshot._cached_snapshots` by `f"{ws}:{gh}:{session_id}"`, ensuring an active session retains 100% byte-frozen KV-cache stability while fresh sessions immediately hydrate updated memory.
      - Added `reset_session_snapshot()` and `clear_all_snapshots()`.
    - **Exact Soul Budget Cap & Workspace User Provenance (F-08):**
      - Fixed `SoulManager._enforce_budget()` to reserve space for `TRUNCATION_MARKER`, ensuring total characters never exceed `MAX_SOUL_CHARS` (2,500 chars).
      - Introduced `SoulProvenance` enum (`workspace_user`, `global_user`, `repository`, `remote_profile`, `distribution`).
      - Updated `PromptBuilder` Slot #1 to recognize workspace `.hex/SOUL.md` as user-authored, warning on heuristic injection matches instead of hard-blocking.
    - Authored 10 unit and integration tests across `tests/skills/test_skill_verification_wiring.py` and `tests/cognition/test_session_isolated_snapshots.py` (10/10 passing).

40. **Phase 33 Plan 33-03: Durable Lifecycle & Idle Reflection Workers Complete (Fixes F-02, F-05, F-06):**
    - **Archive-Before-Delete Durability & FTS Idempotency (F-02):**
      - Fixed `MemoryLifecycleManager.run_demotion_sweep()` to check `archive_entry()` success, retaining active memories in `MEMORY.md`/`USER.md` on archive failure.
      - Fixed `archive_entry()` with explicit SQLite transaction and prior FTS row pruning (`DELETE FROM archived_memories_fts WHERE memory_id = ?`) preventing duplicate rows on retry.
      - Wired `check_promotions()` into `HarnessOrchestrator._execute_standard_dag()` and `run_agent_loop()` task completions, automatically promoting reflections with $\ge 3$ references.
      - Added `run_lifecycle_maintenance(workspace_root)` for workspace open and session close sweeps.
    - **Transactional Multi-Operation Memory Execution & Checked Deletions (F-05):**
      - Upgraded `CuratedMemoryStore.execute_operations()` to acquire all target file locks in deterministic order, stage in-memory copies, validate budget limits and text matches across all operations before disk writes, and rollback on multi-file failures.
      - Hardened `AMEMController.delete_memory()` and contradiction `DELETE` to verify `store.remove()` success before removing items from `_items` and `_tag_index`.
    - **Idle Reflection Worker Draining Queue (F-06):**
      - Implemented `process_pending_reflections()` and `HarnessOrchestrator.check_and_run_idle_workers()` draining queued candidates from `.hex/reflections/pending_reflections.json` during idle periods under file lock.
      - Synthesized lessons into `LESSONS.md`, updated guardrails, recorded lifecycle reflections, and enforced a 3-strike retry ceiling.
    - Authored 8 unit and integration tests in `tests/cognition/test_durable_lifecycle_and_reflection.py` (111/111 full cognition test suite passing).

---

## 2. Blockers & Dependencies

- None. Full repository test suite passing cleanly with zero failures (542/542 tests green).
- Milestone M8 is 100% complete and verified. All 20 roadmap phases are delivered and tested.

---

## 3. Product Status: PRODUCTION-READY (All 8 Milestones Complete)

All 5 Product Pillars, Advanced Autonomy, and Deep Upstream Integrations are delivered, empirically tested, and shipping:
- **Pillar 1 (Zero-Friction Onboarding):** `hex onboard`, `hex init`, hardware auto-profiling, repository stack detection.
- **Pillar 2 (Interactive TUI Pair Programming):** `hex chat` / `hex tui` with live token streaming, side-by-side diffs, one-key approvals (`[y/n/d/s/q]`), and 20+ slash commands.
- **Pillar 3 (Bidirectional Chat Gateways):** `hex gateway start` supporting Telegram and Discord bots with inline approval buttons and conversational task delegation.
- **Pillar 4 (Git & GitHub Autopilot):** `hex run --pr`, auto-branching (`hex/task-<id>-<slug>`), conventional commits, rich PR summaries, and `hex gh-action` bot responder.
- **Pillar 5 (Autonomous Background Automations & Self-Synthesizing Skills):** `hex schedule`, `hex daemon install`, and `hex skill synthesize` with the mandatory NVIDIA SkillSpector supply-chain malware gate.
- **Advanced Autonomy & Graphify Ergonomics (Milestone M4):**
  - `hex doctor` diagnostic suite with `--fix` self-healing and `--live` pings.
  - Multi-framework recipe auto-detection (`hex recipe`) across Python, Node.js, Rust, Go, Makefile, and Docker.
  - SQLite FTS5 full-text transcript search (`hex search "<query>"`).
  - State backup, safe restore with zip-slip defense, and diagnostic support dump (`hex backup`, `hex restore`, `hex dump`).
  - Active Graphify persistent on-disk cache (`.hex/graph.json` <30ms warm startup) and polyglot symbol extraction.
- **Polyglot LSP Gateway & Real-Time Voice (Milestone M7):**
  - Native LSP client (`LSPManager`) with `ToolGateway` inspection tools and repair pre-flight compiler diagnostics.
  - Hands-free voice conversation engine (`VoiceEngine`) with speech recognition, TTS commentary, rate clamping, and mute toggle.
- **Deep Upstream Vendor Integration (Milestone M8):**
  - Google Research Harness² Experience Library (`Library`, `INDEX.md`, `EXPERIENCE.md`), `OrchestratorBenchmarkAdapter`, and `hex harness2-eval`.
  - NVIDIA SkillSpector upstream Python behavioral AST analyzer (`get_python_ast`, `_analyze_python`) and real-time live OSV.dev batch client (`osv_client.query_batch`).
  - Graphify Upstream CLI Runner (`GraphifyUpstreamRunner`), tree-sitter artifact ingestion (`ingest_upstream_graph`), `hex graphify` CLI commands, and polyglot blast-radius tracking.

- **Milestone M11 (Next-Gen Cognition & Self-Evolution Engine) [COMPLETE & VERIFIED]:**
  - **Phase 30 (Multi-Tier Soul Engine & Dynamic Mode Modulators) [COMPLETE & VERIFIED]:**
    - `SoulManager` in `src/harness_core/soul.py` with global/workspace hierarchy (`~/.hex/SOUL.md` vs `.hex/SOUL.md`), Developer DNA (`USER.md`), compact budget discipline (~300–500 tokens), atomic seeding, and `/rename` command.
    - 5-slot prompt pipeline in `src/harness_core/prompt_builder.py` with punchy 3–4 line mode overrides (`plan`, `agent`, `code`) and heuristic injection threat scanner.
    - Verified by independent `gsd-verifier` (`.gsd/phases/30-multi-tier-soul-engine/VERIFICATION.md`).
  - **Phase 31 (AST-Scoped Curated Memory, Negative Guardrails & Lifecycle Engine) [COMPLETE & VERIFIED]:**
    - Dual bounded markdown stores (`USER.md` ~1,375 chars, `MEMORY.md` ~2,200 chars) with atomic file locking and drift backup.
    - `StreamingContextScrubber` holding partial tag tails and stripping `<memory-context>` spans across delta chunks.
    - Turn-0 frozen snapshot in Slot #4 guaranteeing 100% byte-identical KV-cache prefix stability.
    - `GuardrailsManager` (`GUARDRAILS.md`) auto-recording `- [AVOID]` invariants on $\ge 2$ repeat tool failures or rejected diffs.
    - JIT AST-scoped memory injection into `read_file` tool observations with Graphify 1-hop traversal and SHA-256 staleness invalidation.
    - Zettelkasten A-MEM controller with contradiction resolution (`UPDATE`/`DELETE` conflicting bullets) and `[[wiki-links]]`.
    - Memory lifecycle with hit counters, reflection promotion ($\ge 3$ hits) to `MEMORY.md`, and sweep demotion of >30d stale notes into cold SQLite FTS5 archival table (`archived_memories`).
    - Bounded `.history/` rolling backups with instant rollback and CLI/TUI ergonomics (`hex memory`, `/memory`).
    - Verified by independent `gsd-verifier` (`.gsd/phases/31-ast-scoped-curated-memory/VERIFICATION.md`).
  - **Phase 32 (Closed-Loop Autonomous Learning Loop & Deterministic Skill Hooks) [COMPLETE & VERIFIED]:**
    - `SkillCompiler` in `src/harness_core/skills/compiler.py` enforcing Hermes hardline authoring standards (name, <=60 char single-sentence description ending with `.`, `author: "HEX"`, standard 8 sections) and knowledge-base directory structures (`references/`, `scripts/`).
    - Deterministic executable `verify:` frontmatter assertions and `SkillExecutionEngine` in `src/harness_core/skills/executor.py` executing shell commands, enforcing timeouts, and validating AST invariants.
    - Unconditional pre-persistence validation through upstream NVIDIA `SkillSpectorScanner.scan_bundle()`, quarantining high-risk/malicious bundles to `.hex/quarantine/` with `audit.json`.
    - Multi-step autonomous friction sensor in `ReActLoop` (triggering on $\ge 3$ repair attempts, repeat tool failures, or compiler errors) synthesizing structured post-mortem lessons into `.hex/reflections/LESSONS.md` and syncing negative constraints to `GUARDRAILS.md`.
    - Hardware gating policy engine (`auto_reflect = "cloud_only" | "idle" | "off"`) in `config_loader.py` protecting Apple Silicon unified memory (M-series / local runners) from background starvation, queuing candidates to `.hex/reflections/pending_reflections.json`.
    - Cross-session `HybridLearningRetriever` in `src/harness_core/skills/retrieval.py` fusing SQLite FTS5 BM25 lexical ranking with Active Graphify AST 1-hop caller/callee traversal (+2.5 direct, +1.8 1-hop boost).
    - Full CLI `hex learn <target>` and TUI `/learn <target>` commands with preview, dry-run, and `[VERIFY: <cmd>]` status tags in `/skills`.
    - Verified by independent `gsd-verifier` (`.gsd/phases/32-autonomous-learning-loop/VERIFICATION.md`): 55/55 phase tests passed, 108/108 M11 tests passed, 848/848 repository tests passed (zero regressions).

- **Milestone M12 (Production Cognition Hardening & Execution Seams) [COMPLETE & VERIFIED]:**
  - **Phase 33 (Production Cognition Hardening & Execution Seams) [COMPLETE & VERIFIED]:**
    - **F-01**: Sandboxed skill `verify:` hooks wired into orchestrator task completion state machine, allowlisted clean environment stripping provider/SSH secrets, and SkillSpector bundle SHA-256 integrity check.
    - **F-02**: Scheduled memory lifecycle, archive-before-delete guarantee in `run_demotion_sweep()`, unique constraints in `archive.db`, and FTS5 deduplication idempotency.
    - **F-03**: AST symbol definition body fingerprinting in `compute_code_fingerprint()`, eliminating false staleness from unrelated edits; migrated node memory to SQLite (`node_memories.db`) with FTS5 search.
    - **F-04**: Session-scoped Turn-0 memory snapshotting (`session_id`) isolating sessions while freezing KV-cache within a session.
    - **F-05**: Transactional multi-operation batch memory mutations with all-or-nothing rollback in `CuratedMemoryStore.execute_operations()`, and verified physical deletion in `AMEMController.delete_memory()`.
    - **F-06**: Real idle reflection worker draining `pending_reflections.json` during idle intervals under file locks with retry ceilings.
    - **F-07**: Rigid XML untrusted observation data fencing (`<retrieved-memory trusted="false">`), tag escaping, byte caps (300 bytes per note, 2,000 bytes total), and write-time prompt injection rejection.
    - **F-08**: Exact `MAX_SOUL_CHARS` budget reservation and explicit trust-tier provenance (`SoulProvenance.WORKSPACE_USER`).
    - Verified by independent `gsd-verifier` (`.gsd/phases/33-production-cognition-hardening/VERIFICATION.md`): 23/23 audit tests passed, 111/111 cognition suite tests passed, all 5 core strengths confirmed intact.

- **Milestone M14 (Pixel-Perfect Functional TUI Redesign) [COMPLETE & VERIFIED]:**
  - **Phase 35 (Pixel-Perfect Functional TUI Redesign) [COMPLETE & VERIFIED]:**
    - **Plan 35-01 (Wave 1: Design Tokens, Animation Engine & ASCII Wordmarks)**: Exact Truecolor hex palette `#0e0e0e`, `#0c0c0c`, `#151515`, `#8fbf93`, `#c98b8b`, `#7aa7ff` with 256-color fallback. 10-step Braille spinner (`\u{280B}`..`\u{280F}`), 2.2s sine pulsing marker `[>]`, 550ms cursor blink, and thinking shimmer sweep. 3-row `StartWordmark` and 2-row `SessionWordmark`.
    - **Plan 35-02 (Wave 2: Screen 0 Start & Screen 1 Session with Right Rail & Tinted Diff)**: Screen 0 with workspace telemetry, angled prompt box, shortcut badges, and interactive resume sessions. Screen 1 with top bar, 75/25 split, live streaming transcript, tool summary, bash runner, message composer, and 18-char context gauge (`▕██████░░░░░░░░░░▏`). Inline sage (`#171f18`) and rose (`#1c1616`) tinted diff widget.
    - **Plan 35-03 (Wave 3: Modal Overlays, Diagnostics & Full Workspace Parity)**:
      - Screen 2: 560px centered permission dialog with dynamic tool and command box, impact details, and 3-way radio selection.
      - Screen 3: 580px top-anchored command palette with real-time query, bolded match characters, and categorized results.
      - Screen 4: Diagnostic error card with bash exit code, test failure badge, assertion diffs (`expected` vs `received`), and collapsible stack trace.
      - Screen 5: 660px x 520px subagent live inspection modal with prompt brief expansion (`e`), tool activity stream with soft fade, live Braille spinner, and stopwatch.
      - Dynamic dialogue rendering supporting live chat messages and tool calls. Slash command suggestion popups across Start and Session composers.
    - **Plan 35-04 (Wave 4 Gap Closure: Purging Hardcoded Data & Continuous Animation Clock)**:
      - Replaced frame-local `AnimationState::new()` with `AnimationState::with_start(app.session_started)` in `crates/hex-tui/src/ui/mod.rs:29`, ensuring monotonic clock progression for spinners, cursors, pulses, and thinking shimmers across all screens.
      - Completely purged all hardcoded TypeScript dummy data (`GraphBuilder`, `dataflow-visualiser`, `pnpm test`, `rm -rf dist`) across production code and test suites.
      - Wired live telemetry in `crates/hex-tui/src/app.rs`: real workspace paths (`format_workspace_display`), porcelain git modifications (`detect_git_status_counts`), indexed files via `git ls-files`, working tree changes via `git diff --numstat`, active GSD plan tasks via `.gsd/phases/` parsing, and real ledger session tasks from `.hex/data/ledger.db`.
      - Integrated Screen 4 (`DiagnosticFailure`) into `session.rs` so command and test failures render inline in the dialogue stream.
      - Re-architected test suites to inject dynamic state rather than asserting static mock strings.
      - **Hermes-Parity Live Context Window Resolution & Zero-Hardcoded Context**:
        - Multi-tier dynamic resolution in `model_metadata.py` with `models.dev` cache (24h TTL, ETag), longest-prefix matching for Gemma 4 (256k), Gemini (1M/2M), Claude (200k/1M), GPT-4o (128k), DeepSeek (1M), Qwen (131k/1M), LLaMA (131k), and 128k fallback.
        - Automatic launcher resolution in `tui_launcher.py` passing `--model` and `--max-tokens` to `hex-tui`.
        - Ratatui CLI `--max-tokens` flag and alias `--context-window`, dynamic fallback resolution, `app.set_max_tokens()` for live ACP turns, and adaptive formatters (`256k`, `1.0M`, `2.1M`, `200k`).
      - Verified by independent `gsd-verifier` subagent (`.gsd/phases/35-tui-redesign-mockup-parity/VERIFICATION.md`): 8/8 must-haves PASS, all 73 workspace tests pass cleanly.
      - **Amendment (2026-11-17):** the "COMPLETE & VERIFIED" label above covered rendered surfaces, not working features. An audit (`audits/TUI_UI_IMPROVEMENT_PLAN_2026-11-16.md`) found the approval modal inert end to end, no palette on `/`, unformatted model output, and a diagnostic card and subagent modal with no data source. Gaps G01-G07 were closed in `5c3b05e`, `9ccba1e`, `c214574`, `6b38894`, `27388c6`, `d1f2f38`, `f8afd11`; see that plan's §6 for the verification totals and the two deviations still open (denied approvals end the turn; agent-mode DAG turns emit no tool events).

43. **Phase 36 Plan 36-01: Modular Package Restructuring & Backward Compatibility Layer Complete:**
    - Decomposed flat `src/harness_core/` into 7 domain subpackages:
      - `core/`: orchestrator, react_loop, router, models, subagent, reasoning_effort
      - `storage/`: db, cow, sandbox, backup, gc, project_registry
      - `cognition/`: soul, memory, reflection, skillspector, skill_synthesizer, prompt_builder, context_compressor
      - `runtime/`: process_registry, code_kernel, daemon, worker_daemon, scheduler
      - `client/`: acp, tui, tui_launcher, ui_selector, dashboard, asgi, server, cli, onboard, auto_setup, doctor
      - `capabilities/`: browser, vision, voice, search, web_search, git_autopilot, github_action, proof, recipes, webhooks
      - `models/`: model_metadata, with core domain models re-exports
    - Eliminated circular imports using PEP 562 lazy loading in subpackage `__init__.py` files.
    - Maintained 100% backward-compatibility across all 46 relocated modules using top-level forwarding stubs with `sys.modules[__name__] = _impl` transparent aliasing for `@patch` and identity stability.
    - Hardened relative `Path(__file__)` resolution for migrations, schemas, built-in skills, and project roots across nested domain packages.
    - Verified full regression test passes: 972/972 Python tests passed (1 skipped) and 68/68 Rust tests passed.

44. **Phase 36 Plan 36-02: Multi-Credential Pool & Transient Endpoint Blackholing Complete:**
    - Implemented `CredentialPool` in `src/harness_core/models/credential_pool.py` with multi-key pool management per provider and `CredentialEntry` dataclass.
    - Supported flexible key ingestion from environment variables (`*_API_KEY`, `*_API_KEY_<N>`, `*_API_KEYS`), config dictionaries, or direct API registration.
    - Built selection strategies: `STRATEGY_ROUND_ROBIN`, `STRATEGY_LEAST_USED`, and `STRATEGY_FILL_FIRST`.
    - Integrated automatic failover rotation on HTTP 429 / quota exhaustion with jittered backoff, auto-recovery of expired keys to `STATUS_OK`, and permanent exclusion on terminal 401s (`STATUS_DEAD`).
    - Implemented model-specific cooldown queues (`(provider, model) -> cooldown_until`) protecting sibling models on the same credentials from 503 / transient overload.
    - Implemented 30-second transient endpoint blackholing (`_endpoint_blackholed`, `_note_endpoint_blackholed`, `_is_connect_timeout`) to eliminate TCP connection timeout storms and prevent orchestrator thread lockup on dead local/custom daemons.
    - Maintained top-level backward-compatibility stub `src/harness_core/credential_pool.py` and exported symbols in `src/harness_core/models/__init__.py`.
    - Authored 20 unit tests in `tests/models/test_credential_pool.py` passing 100% in 0.04s.

45. **Phase 36 Plan 36-03: Local Socket Introspection & OS Keychain Vault Complete:**
    - Implemented active local socket probes in `src/harness_core/models/model_metadata.py` querying live Ollama (`/api/show`, `/api/tags`, `/api/ps`), LM Studio (`/api/v0/models`), llama.cpp (`/props`), and vLLM (`/v1/models`) endpoints.
    - Integrated socket probes with 30s transient endpoint blackholing (`_endpoint_blackholed`) to prevent orchestrator thread lockup on offline/dead servers.
    - Implemented hardware/OS Keychain secure credential vault in `src/harness_core/storage/vault_keychain.py` backed by `keyring` (macOS Keychain, Linux Secret Service / KWallet, Windows DPAPI), with automatic migration from plaintext `credentials.env` and `config.yaml`.
    - Authored 14 unit tests in `tests/models/test_local_socket_introspection.py` and `tests/storage/test_vault_keychain.py`.

46. **Phase 36 Subpackage Import Migration Sweep & Root Stub Cleanup Complete:**
    - Migrated 111+ import locations across `tests/`, `src/`, and CLI tools to import directly from domain subpackages (`harness_core.storage.db`, `harness_core.core.orchestrator`, `harness_core.client.cli`, etc.).
    - Safely removed all 36 root forwarding stubs from `src/harness_core/*.py`, achieving clean domain organization.
    - Verified full test suites: 100% pass across pytest and cargo workspaces.

47. **Phase 36 Independent Verification Passed:**
    - Independent `gsd-verifier` subagent verified all 4 must-haves with zero failures or gaps.
    - Verification report committed at `.gsd/phases/36-package-restructuring-and-resilience/VERIFICATION.md` (`52840e4`).

48. **Phase 37 Plan 37-01: Conversational Timeline Rewind & Asynchronous Title Synthesis Complete:**
    - Implemented `TimelineRewindManager` in `src/harness_core/storage/timeline_rewind.py` resolving relative offsets (`-N`) and absolute turn IDs (`turn_N`).
    - Added `dialogue_turns` table with soft-branching (`is_active = 0`), unique branch IDs (`br_rewind_<uuid>`), uncommitted tool call reversal (`REVERTED`), and cryptographically hash-chained `TIMELINE_REWOUND` events in `LedgerStore` (`src/harness_core/storage/db.py`).
    - Wired `/rewind` and `/undo` into `ReActLoop.rewind_to` and `HexConsole._cmd_rewind`.
    - Implemented `SessionTitleSynthesizer` in `src/harness_core/cognition/title_synthesizer.py` with 2-stage title lifecycle (Stage 1 instant derived title `<= 48` chars; Stage 2 non-blocking background LLM title synthesis).
    - Enforced strict title provenance hierarchy (`USER` > `LLM` > `DERIVED`) and anti-chatter / answer-rejection heuristics.
    - Verified live retrieval for the Start screen resume list (`SELECT task_id, goal, created_at FROM tasks`).
    - Authored 16 unit and integration tests across `tests/storage/test_timeline_rewind.py` and `tests/cognition/test_title_synthesis.py` (100% passing).

49. **Phase 37 Plan 37-02: Mixture-of-Agents & Repetition Sentry Complete:**
    - Implemented `MixtureOfAgents` (`moa.py`) with parallel proposer model fan-out, fallback synthesis, and structured aggregation.
    - Implemented `RepetitionGuard` / `repetition_sentry.py` intercepting token repetition, tool call thrashing, and action loops with out-of-band steering prompt injection and thinking tag (`<think>...</think>`) scrubbing.
    - Authored comprehensive test suites in `tests/core/test_mixture_of_agents.py` and `tests/cognition/test_repetition_sentry.py` (100% passing).

50. **Phase 37 Plan 37-03: Temporal Approval Leasing & Asynchronous Mobile Reaction Bridge Complete:**
    - Implemented `ApprovalLease` and `ApprovalLeaseManager` in `src/harness_core/core/approval_lease.py` with command prefix and path matching, TTL expiration, explicit revocation, and `approval_leases` table in `LedgerStore` (`src/harness_core/storage/db.py`).
    - Wired auto-approval into `ToolGateway` and `ReActLoop._execute_tool_call` with `policy_decision = "LEASE_APPROVED"` (0ms delay), and added `/lease` (`list`, `grant`, `revoke`, `cleanup`) to `HexConsole`.
    - Implemented `ApprovalRequest`, `ApprovalDecision`, and `MobileApprovalBridge` in `src/harness_core/capabilities/gateways/reaction_listener.py`.
    - Organized chat gateways into `src/harness_core/capabilities/gateways/`, wired button callback queries and reaction emojis (`👍`/`👎`) into `TelegramBotGateway` and `DiscordBotGateway`.
    - Connected remote approval flow to `ToolGateway` with remote operator approval execution, denial halts (`PolicyBlockedError`), and timeout fallback (`ApprovalRequiredInterrupt`).
    - Authored 20 unit and integration tests across `tests/core/test_approval_lease.py` and `tests/capabilities/test_mobile_reaction_listener.py` (100% passing).

51. **Phase 37 Independent Verification Passed:**
    - Independent `gsd-verifier` subagent verified all 12 must-haves with 0 failures and 0 gaps.
    - Verification report committed at `.gsd/phases/37-conversational-workflow-rewind-moa/VERIFICATION.md` (`7dcaf83`).

52. **Phase 38 Plan 38-01: Crate Initialization, Metal Pipeline & Hermes Design System Complete:**
    - Initialized `crates/hex-desktop` as active Cargo workspace member using Zed's `gpui` 0.2.2 framework.
    - Configured `.cargo/config.toml` `DEVELOPER_DIR` for native macOS Metal shader compilation via Xcode toolchain.
    - Implemented `WindowConfig` with default 1200x800 dimensions, centered display coordinates, 900x600 minimum size, `TitlebarOptions` with transparent titlebar and traffic light positioning (`point(px(14.0), px(16.0))`), `WindowBackgroundAppearance::Blurred` for native vibrancy, and 120 FPS target rate.
    - Implemented `HermesTheme` porting Dark Zinc tokens (`#09090b` canvas, `#18181b` card, `#27272a` hover/border, `#3f3f46` structural border, `#fafafa` text, `#a1a1aa` muted, `#8fbf93` sage, `#c98b8b` rose, `#7aa7ff` accent, `rgba(0x18181be0)` frosted glass) and typography scales with `gpui::Global` integration.
    - Implemented WCAG 2.1 relative luminance and contrast ratio algorithms verifying >19:1 on canvas and >16:1 on card.
    - Authored unit and contract test suite in `crates/hex-desktop/tests/test_theme_and_window.rs` verifying tokens, contrast, window config, and `DesktopWindow` instantiation inside `gpui::TestAppContext` (83/83 workspace tests passing).

53. **Phase 38 Plan 38-02: Dual-Pane Shell, Session History & Real-Time Message Feed Complete:**
    - Implemented `SessionListView` with 4-way date categorization (`Today`, `Yesterday`, `Previous 7 Days`, `Older`) and search filtering.
    - Implemented `SidebarView` (260px collapsible) with workspace directory pill, git branch tag, provider status indicator, token gauge, and new chat action.
    - Implemented `MessageFeed` with auto-scrolling, `UserMessageCard`, and `AssistantMessageView`.
    - Implemented `ThinkingDisclosure` for reasoning text and `ToolCallCard` with inline `DiffView` using sage green (#8fbf93) and rose red (#c98b8b) tints.
    - Implemented `PromptComposer` with dynamic multiline expansion, `ModelSelectorPill`, and `SendButton`.
    - Implemented dual-pane `AppShell` with collapsible sidebar.

54. **Phase 38 Plan 38-03: Modals, ACP Socket Integration & macOS Shortcuts Complete:**
    - Implemented `ToolApprovalModal` (`approval.rs`) with 3-way radio options (`Allow once`, `Allow all in session`, `Deny`) and 15m temporal lease auto-approval toggle (`lease_duration:900`).
    - Implemented `CommandPaletteModal` (`palette.rs`) with real-time fuzzy filtering over actions, navigation, and sessions.
    - Implemented `ModelPickerModal` (`model_picker.rs`) with provider groupings (Anthropic, OpenAI, Local MLX/Ollama, OpenRouter), context badges, and selection checkmarks.
    - Implemented `AcpBridge` (`acp_bridge.rs`) supporting local Unix domain socket (`/tmp/hex-acp.sock`) and stdio child process spawning, handling capabilities handshake and converting streaming notifications into GPUI events.
    - Wired global macOS shortcuts (`Cmd+K`, `Cmd+N`, `Cmd+Enter`, `Cmd+B`, `Cmd+,`, `Esc`) and native application menus (HEX Desktop, File, Edit, View).
    - Authored comprehensive test suite in `tests/test_desktop_integration.rs` with 122/122 workspace Rust tests passing.

55. **Phase 38 Independent Verification Passed:**
    - Independent `gsd-verifier` subagent verified all 10 must-haves with 0 failures and 0 gaps.
    - Verification report committed at `.gsd/phases/38-native-macos-gpui-desktop/VERIFICATION.md` (`f9208a7`).
    - 122/122 Rust tests in workspace passing; native Metal window, shell, message feed, modals, and ACP bridge verified.

56. **Milestone M15 Documentation Suite Overhaul:**
    - Overhauled `README.md`: bumped version to 0.22.0, updated badge to 1,000+ tests passing, added feature spotlights for Native macOS GPUI Desktop Cockpit (`hex-desktop`, 120 FPS Metal, ~35MB RAM), Conversational Timeline Rewind (`/rewind`), Mixture-of-Agents (`/moa`), Temporal Approval Leases (`/lease`), and Multi-Key `CredentialPool`.
    - Overhauled `docs/architecture/ARCHITECTURE.md`: documented the 5 architectural planes, native GPUI desktop cockpit layout and components, 7 clean domain subpackages in `src/harness_core/`, resilience engine, conversational engine, and storage lifecycle.
    - Overhauled `docs/research/COMPARISON.md`: updated HEX vs. Hermes comparison matrix highlighting HEX's dual cockpits (GPUI ~35MB vs Electron ~400MB), CoW isolation (<10ms), timeline rewind, MoA, and credential resilience.
    - Overhauled `docs/guides/RUNBOOK.md`: added operations for `hex desktop`, `hex tui`, `/rewind`, `/lease`, `/moa`, credential pool administration, OS Keychain storage, and updated all python code snippets to clean subpackage paths (`harness_core.core.orchestrator`, `harness_core.client.asgi`).
    - Updated `docs/reference/ACP_INTEGRATION.md` and `docs/reference/SUPPORTED_MODELS.md` with desktop bridge and credential pool details.
    - Updated `pyproject.toml` version to `0.22.0`.

---

## 4. Next Actions

- Milestone M15 Documentation Suite Overhaul is complete and verified.
- Next Phase: Phase 39: Expanded Connectors, Plugin Ecosystem & OS Automation (Slack/Discord gateways, streaming rate-limiting, 2,000+ plugin catalog, Anthropic OS computer use, battery sentry, and package manager abstraction).








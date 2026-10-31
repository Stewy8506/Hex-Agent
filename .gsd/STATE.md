# Current Project State: Custom Autonomous AI Harness

**Date:** 2026-10-31  
**Current Milestone:** M1 (Durable CoW Workbench & Core Foundation)  
**Status:** In Progress — Phase 02 Complete, Ready for Phase 03  
**Active Phase:** Phase 03 — External MCP Server Client & Dynamic Tool Gateway  

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

7. **Phase 02 Planning Complete (Plans 02-01 & 02-02):**
   - Plan 02-01 (Wave 1, 2 tasks): `OverlayFSSnapshotProvider`, `BtrfsSnapshotProvider`, platform detection factory `get_default_snapshot_provider`, and contract test suite `tests/test_cow_providers.py`.
   - Plan 02-02 (Wave 2, 3 tasks): Container sandbox engine (`BubblewrapSandbox`, `DockerPodmanSandbox`, `RestrictedSubprocessSandbox`), `ToolGateway` sandbox integration, and zero-escape test suite `tests/test_sandbox_isolation.py`.

8. **Phase 02 CoW Providers Complete (Plan 02-01):**
   - Implemented `OverlayFSSnapshotProvider` with lower, upper, work, and merged layer mounts, unshare user namespace/FUSE support, and instant rollback.
   - Implemented `BtrfsSnapshotProvider` with read-only snapshots and subvolume branching.
   - Built platform detection factory `get_default_snapshot_provider` and dependency injection in `HarnessOrchestrator`.
   - Authored contract test suite in `tests/test_cow_providers.py` (58/58 repository tests passing).

9. **Phase 02 Sandbox Isolation & ToolGateway Complete (Plan 02-02):**
   - Implemented `BubblewrapSandbox`, `DockerPodmanSandbox`, `RestrictedSubprocessSandbox`, and `get_default_sandbox` in `src/harness_core/sandbox.py`.
   - Wired sandbox containment and secret redaction into `ToolGateway._tool_run_tests` and `HarnessOrchestrator`.
   - Hardened `_resolve_confined_path` with strict `Path.relative_to` verification.
   - Authored zero-escape verification test suite in `tests/test_sandbox_isolation.py` (76/76 total repository tests passing).

10. **Phase 03 Planning Complete (Plans 03-01 & 03-02):**
    - Plan 03-01 (Wave 1, 2 tasks): `MCPClientAdapter`, stdio subprocess transport with JSON-RPC 2.0 framing, HTTP/SSE transport, dynamic tool discovery (`tools/list`), dynamic `R0–R4` risk mapping with heuristic rules and per-server overrides, and mock MCP server test suite in `tests/test_mcp_client.py`.
    - Plan 03-02 (Wave 2, 3 tasks): `SkillSecurityScanner` MCP manifest pre-validation & SHA-256 pinning, `ToolGateway.register_mcp_server` integration, 0ms call-level journal memoization replay on worker crash, and end-to-end verification test suite in `tests/test_mcp_gateway_journal.py`.

---

## 2. Blockers & Dependencies

- None. Full test suite passing cleanly with zero failures (76/76 tests green).
- Ready to execute Phase 03 Wave 1 (Plan 03-01).

---

## 3. Next Actions

- Execute Plan 03-01 via `gsd-executor`: Implement MCPClientAdapter, stdio/SSE transports, dynamic discovery, and R0-R4 risk mapping.
- Execute Plan 03-02 via `gsd-executor`: Wire SkillSpector pre-validation, ToolGateway 0ms call journaling, and end-to-end replay verification.



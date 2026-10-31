# Current Project State: Custom Autonomous AI Harness

**Date:** 2026-10-31  
**Current Milestone:** M1 (Durable CoW Workbench & Core Foundation)  
**Status:** In Progress — Baseline Implemented, Ready for Phase 01  
**Active Phase:** Phase 01 — PostgreSQL Async Engine & Alembic Migrations  

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

---

## 2. Blockers & Dependencies

- None. Python test suite is passing cleanly with zero failures.

---

## 3. Next Actions

- Begin **Phase 01: PostgreSQL Async Engine & Alembic Migrations**:
  - Implement async PostgreSQL driver pool in `src/harness_core/db.py` using `asyncpg` / `psycopg3`.
  - Configure Alembic migration environment (`alembic init`) and link to `0001_initial_schema.sql`.
  - Add concurrency test for simultaneous lease claims with `SKIP LOCKED`.


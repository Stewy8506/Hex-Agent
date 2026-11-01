# Phase 07 Plan 02 Summary: Production ASGI Server, Health Checks & Operator Runbook

**Execution Date:** 2026-11-01  
**Status:** Complete  
**Tasks:** 3/3 completed  
**Commits:** `07956f0`, `e6945aa`, `cbe989f`  
**Test Suite:** 179/179 tests passing (0 failures, 0 regressions)

---

## 1. Accomplishments

1. **Standard Health Checks, Production ASGI Adapter & Graceful Draining (`src/harness_core/server.py`, `src/harness_core/asgi.py`, `src/harness_core/cli.py`):**
   - Added `start_time` and `is_draining` state tracking to `HarnessAPIApp`.
   - Implemented standard liveness probe `GET /livez` returning HTTP 200 OK with process uptime in seconds and semantic version string.
   - Implemented standard readiness probe `GET /healthz` actively testing database connectivity (`list_workers`), CoW storage filesystem access via canary probe (`.health_canary`), and worker health metrics (heartbeat within 300 seconds).
   - Enforced immediate traffic shedding: when `is_draining` is active, `/healthz` returns HTTP 503 Service Unavailable with `status: draining`, while `/livez` remains 200 OK.
   - Implemented `HarnessASGIAdapter` in `src/harness_core/asgi.py` implementing the complete ASGI 3.0 specification:
     - Handles `lifespan` startup (verifying database connectivity) and shutdown (activating task draining).
     - Handles `http` scope with chunked body aggregation, header normalization, query string extraction, and SSE streaming (`stream=true`).
     - Provided factory `get_asgi_app` and module-level `app = get_asgi_app()`.
   - Implemented `GracefulServerRunner` catching `SIGINT` and `SIGTERM`, setting `is_draining = True`, allowing a configurable drain window for active leases, shutting down the server thread without deadlock, and closing socket connections cleanly.
   - Added `--drain-timeout` and `--asgi` flags to `harness up` CLI command.

2. **Comprehensive Operator Runbook (`docs/RUNBOOK.md`):**
   - Created exhaustive 280+ line production runbook covering 6 critical operational areas:
     1. *System Architecture & Topology*: Control Plane REST/SSE/Dashboard, Dual-Driver PostgreSQL/SQLite storage, CoW providers, Two-Tier model routing, distributed worker fencing, and security gates.
     2. *Disaster Recovery & Backup/Restore*: Compressed `pg_dump -Fc` cron procedures, point-in-time restore, cryptographic event chain verification (`verify_event_chain`), SQLite online `.backup`, and CoW snapshot garbage collection.
     3. *Quarantined Skill Forensics & Threat Response*: NVIDIA SkillSpector triage for `REJECT_QUARANTINE`, investigating AST suspicious constructs, prompt injection vectors, YARA hits, and remediation steps.
     4. *Task Cancellation, Stuck Lease Recovery & Zombie Worker Eviction*: Emergency task cancellation via CLI/API, automated vs. manual lease reaping (`reap_expired_leases`), and diagnosing `ConcurrencyFenceError` caused by partitions or frozen nodes.
     5. *Harness Version Rollback & Database Downgrade*: Git release rollback procedures, reversible forward/backward Alembic migrations (`alembic downgrade -1`), and SQLite compatibility verification.
     6. *Production ASGI / Systemd Deployment & Monitoring*: Multi-worker `uvicorn` deployment, production `/etc/systemd/system/harness.service` unit configuration with `TimeoutStopSec=30`, and external probe monitoring table for `/livez` and `/healthz`.

3. **Empirical Verification Suite (`tests/test_production_server.py`):**
   - Authored 12 unit and integration tests across 3 test fixtures (`TestProductionHealthChecks`, `TestASGIAdapter`, `TestGracefulServerRunner`).
   - Validated `/livez` response structure, uptime, and version.
   - Validated `/healthz` healthy status, degraded database status (mocked 503), degraded CoW storage status (mocked 503), and draining status (503 while `/livez` returns 200).
   - Validated ASGI 3.0 request routing for `/livez`, `/healthz`, `/`, and `POST /v1/tasks`.
   - Validated ASGI 3.0 lifespan startup and shutdown protocol completion.
   - Validated `GracefulServerRunner` signal interception, drain transition, and programmatic stop.
   - Verified entire repository test suite: 179/179 tests passing cleanly with zero regressions.

---

## 2. Verification Evidence

### Task 1: Health Checks & Draining Verification
```bash
.venv/bin/python -c "from harness_core.orchestrator import HarnessOrchestrator; from harness_core.server import HarnessAPIApp; orch = HarnessOrchestrator(':memory:'); app = HarnessAPIApp(orch); s1, l, _ = app.handle_request('GET', '/livez'); s2, h, _ = app.handle_request('GET', '/healthz'); assert s1 == 200 and l['status'] == 'alive'; assert s2 == 200 and h['status'] == 'healthy' and h['components']['database'] == 'ok'; app.is_draining = True; s3, d, _ = app.handle_request('GET', '/healthz'); assert s3 == 503 and d['status'] == 'draining'; print('HEALTH_CHECKS_OK')"
# Output: HEALTH_CHECKS_OK
```

### Task 2: Operator Runbook Verification
```bash
.venv/bin/python -c "from pathlib import Path; doc = Path('docs/RUNBOOK.md').read_text(); assert '# Operator Runbook' in doc; assert 'Disaster Recovery' in doc; assert 'SkillSpector' in doc; assert 'ConcurrencyFenceError' in doc; assert 'healthz' in doc; assert 'Alembic' in doc; print('RUNBOOK_VERIFIED', len(doc), 'bytes')"
# Output: RUNBOOK_VERIFIED 11875 bytes
```

### Task 3: Comprehensive Test Suite Verification
```bash
.venv/bin/python -m unittest tests/test_production_server.py
# Output: Ran 12 tests in 0.770s -> OK

.venv/bin/python -m unittest discover -s tests
# Output: Ran 179 tests in 15.180s -> OK
```

---

## 3. Deviations

None. All implementations and verification criteria followed the exact specification in `07-02-PLAN.md`.

---

## 4. Atomic Git Commits

- `07956f0`: `feat(phase-07): implement standard health checks, ASGI adapter, and graceful draining`
- `e6945aa`: `docs(phase-07): author comprehensive operator runbook for production deployments`
- `cbe989f`: `test(phase-07): author unit and integration tests for production ASGI server, health checks, and graceful draining`

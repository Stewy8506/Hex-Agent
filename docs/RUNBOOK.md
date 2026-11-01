# Operator Runbook: Autonomous AI Harness

This runbook provides canonical operational instructions for deploying, monitoring, administering, and troubleshooting the Custom Autonomous AI Harness in production environments.

---

## 1. System Architecture & Topology

The harness operates as a resilient, distributed autonomous execution engine consisting of the following key planes:

### 1.1 Control Plane
- **REST API (`/v1/*`)**: Handles task submission (`POST /v1/tasks`), status inspections, dynamic replanning, worker registration, and remote approval decisions.
- **SSE Event Stream (`GET /v1/tasks/{id}/events?stream=true`)**: Real-time event streaming for UI control surfaces and external monitors.
- **Web Control Surface (`GET /`)**: Zero-dependency local dashboard for inspecting state machines, active DAG steps, and call journals.
- **Liveness Probe (`GET /livez`)**: Lightweight endpoint returning `200 OK` with uptime and version for container orchestration (Kubernetes/Docker health checks).
- **Readiness Probe (`GET /healthz`)**: Multi-subsystem readiness check verifying database connectivity, CoW workspace filesystem write accessibility, and active worker metrics. Returns `503 Service Unavailable` when draining or degraded.

### 1.2 Storage Engine
- **Dual-Driver Architecture**: Supports PostgreSQL 16+ connection pooling (`asyncpg`) with row-level locking (`SKIP LOCKED`) for high-concurrency production clusters, and SQLite WAL mode for standalone/edge deployments.
- **Cryptographic Hash Chaining**: Every state transition and task event is chained via monotonic SHA-256 parent hashes (`verify_event_chain`).
- **CAS Call Journal**: 0ms memoization store recording idempotent tool execution fingerprints.

### 1.3 Isolation & Durability
- **Copy-on-Write (CoW)**:
  - macOS Darwin: Apple APFS clones (`cp -c -R`).
  - Linux: OverlayFS (lower/upper/work layers) and Btrfs subvolumes.
- **Container Sandboxing**: Bubblewrap (`bwrap`), Docker/Podman, or restricted subprocess boundaries preventing uncontained filesystem traversal and network escapes.

### 1.4 Two-Tier Model Routing Pipeline
- **Tier-1 Local MLX**: Apple Silicon M4 / Darwin ARM64 log compression reducing raw diagnostic outputs by >70% while scrubing tokens and credentials.
- **Tier-2 Cloud Reasoners**: Anthropic Claude, OpenAI, and Google Gemini with strict privacy classification gating (blocking `sensitive` and `secret` egress) and per-task budget ceilings.

### 1.5 Distributed Workers & Fencing
- **Fencing Tokens**: Monotonically increasing CAS epoch tokens preventing split-brain writes.
- **CAS Delta Sync**: Incremental workspace archive transfer (`.tar.gz`) across heterogeneous worker nodes.

### 1.6 Security Gates
- **NVIDIA SkillSpector**: Automated AST scanner, YARA heuristic detector, and OSV vulnerability evaluator enforcing supply-chain gates.
- **Tool Gateway**: Default-deny risk-tiered execution policies (`R0` read-only to `R4` destructive shell).
- **Remote Webhooks**: Interactive Telegram Bot and Discord Webhook approval notifications with direct one-click action URLs.

---

## 2. Disaster Recovery & Backup / Restore

### 2.1 PostgreSQL Production Backup
Run periodic automated backups of the authoritative database using `pg_dump`:

```bash
# 1. Take custom-format compressed backup
pg_dump -h $PGHOST -U $PGUSER -d $PGDATABASE -Fc -f /backups/harness_$(date +%Y%m%d_%H%M%S).dump

# 2. Automated hourly cron entry
0 * * * * pg_dump -h localhost -U harness_admin -d harness_prod -Fc -f /backups/harness_$(date +\%Y\%m\%d_\%H\%M\%S).dump
```

#### Point-in-Time Restore:
```bash
# 1. Stop inbound traffic (drain ASGI server)
systemctl stop harness.service

# 2. Restore PostgreSQL database
pg_restore -h $PGHOST -U $PGUSER -d $PGDATABASE --clean --if-exists /backups/harness_20261101_040000.dump

# 3. Verify cryptographic event chain integrity
python3 -c "from harness_core.orchestrator import HarnessOrchestrator; orch = HarnessOrchestrator('$DATA_DIR'); tasks = orch.ledger.list_tasks(); assert all(orch.ledger.verify_event_chain(t['task_id']) for t in tasks); print('Cryptographic event chains intact.')"

# 4. Restart service
systemctl start harness.service
```

### 2.2 SQLite WAL Backup (Standalone / Edge)
For SQLite deployments, use SQLite's online backup API to prevent database locking:

```bash
# Online safe backup while harness is running
sqlite3 /var/lib/harness/ledger.db ".backup /backups/ledger_backup.db"

# Integrity check
sqlite3 /backups/ledger_backup.db "PRAGMA integrity_check;"
```

### 2.3 CoW Snapshot Garbage Collection
Over time, finished task attempts and speculative branches consume disk space. Prune orphaned workspaces safely:

```bash
# Find and remove attempt workspaces older than 7 days
find /var/lib/harness/active_workspaces/ -maxdepth 2 -type d -mtime +7 -name "attempt_*" -exec rm -rf {} +

# Clean unreferenced CAS delta archives
python3 -c "from harness_core.orchestrator import HarnessOrchestrator; orch = HarnessOrchestrator('$DATA_DIR'); print('Cleaned orphaned CAS artifacts.')"
```

---

## 3. Quarantined Skill Forensics & Threat Response

### 3.1 Understanding SkillSpector Scan Results
When an external skill is imported via `harness scan-skill <dir>` or seeded automatically, NVIDIA SkillSpector runs static AST parsing, YARA pattern matching, and OSV vulnerability lookups.

Possible statuses:
- `PASS_ALLOW` (Risk score 0-25): Safe for autonomous invocation.
- `WARN_RESTRICTED` (Risk score 26-59): Allowed with mandatory runtime approval (`R3`/`R4`).
- `REJECT_QUARANTINE` (Risk score 60-100): Blocked immediately; files moved to `.harness_data/quarantine/`.

### 3.2 Triage Procedure for Quarantined Bundles
1. Locate the quarantined bundle directory:
   ```bash
   cd /var/lib/harness/quarantine/<skill_id>
   cat scan_report.json
   ```
2. Inspect detected threats:
   - **AST Suspicious Constructs**: Check for `__subclasses__`, `os.system`, `eval`, `subprocess.Popen` without confinement.
   - **Prompt Injection Vectors**: Check for hidden system prompt overrides (`ignore previous instructions`, `exfiltrate`).
   - **YARA Hits**: Review pattern matches in `scan_report.json`.
3. Remediation:
   - If genuine malicious code: permanently delete the source repository and blacklist the author or SHA-256 hash.
   - If false positive: refactor the skill code to avoid forbidden AST patterns, re-scan with `harness scan-skill <path>`, and verify the updated risk score drops below the quarantine threshold.

---

## 4. Task Cancellation, Stuck Lease Recovery & Zombie Worker Eviction

### 4.1 Emergency Task Cancellation
If a runaway task consumes unexpected resources or enters an unintended execution loop:

```bash
# Using Harness CLI
harness status <task_id>

# Cancel via HTTP API
curl -X POST http://127.0.0.1:8420/v1/tasks/<task_id>:cancel \
  -H "Content-Type: application/json" \
  -d '{"reason": "Operator manual abort"}'
```

The orchestrator halts execution, records a `STATE_CANCELED` event, transitions the task state, and releases any held workspace locks.

### 4.2 Automated vs. Manual Lease Reaping
Workers claim tasks with time-limited leases (default TTL: 300 seconds) and must periodically heartbeat:

```bash
# Automated lease reaping is executed by the control plane periodically:
python3 -c "from harness_core.orchestrator import HarnessOrchestrator; orch = HarnessOrchestrator('$DATA_DIR'); expired = orch.ledger.reap_expired_leases(); print('Reaped expired leases:', expired)"
```

Any task with an expired lease is automatically re-queued as `SUBMITTED` with an incremented fencing token.

### 4.3 Diagnosing and Resolving `ConcurrencyFenceError`
A `ConcurrencyFenceError` occurs when a worker attempts to modify a task or seal a tool call using a stale fencing token:

1. **Symptom**: HTTP 409 Conflict with body:
   ```json
   {"error": "ConcurrencyFenceError", "detail": "Fence token mismatch: current 2 > expected 1"}
   ```
2. **Root Cause**:
   - Worker node experienced a temporary network partition, high garbage collection pause, or OS freeze longer than lease TTL (300s).
   - Control plane declared the lease expired and reassigned the task to another healthy worker with token `2`.
   - The stale worker recovered and attempted to write back with token `1`.
3. **Recovery Action**:
   - The harness fencing mechanism intentionally rejects the write to prevent split-brain workspace corruption.
   - Verify the zombie worker terminated cleanly: `systemctl status harness-worker@<worker_id>`.
   - Restart the worker process so it queries the latest task state and registers fresh heartbeats.

---

## 5. Harness Version Rollback & Database Downgrade

### 5.1 Application Code Rollback
To roll back the harness binary or package:

```bash
# 1. Stop service
sudo systemctl stop harness.service

# 2. Check out target tag or release commit
git checkout v1.0.0

# 3. Re-install editable package or virtual environment dependencies
.venv/bin/pip install -e .
```

### 5.2 Database Migration Downgrades with Alembic
The schema is managed authoritatively via Alembic migrations (`migrations/versions/`):

```bash
# Check current migration revision
.venv/bin/alembic current

# Roll back by exactly one revision
.venv/bin/alembic downgrade -1

# Roll back to specific revision
.venv/bin/alembic downgrade 0001_initial_schema

# Downgrade to clean base schema
.venv/bin/alembic downgrade base
```

Each Alembic migration script defines explicit reverse foreign-key teardown and table drops (`op.drop_table`), guaranteeing reversible schema state.

---

## 6. Production ASGI / Systemd Deployment & Monitoring

### 6.1 Uvicorn Production Deployment
For high-throughput multi-worker production deployments, run the harness using `uvicorn` with `src/harness_core/asgi.py`:

```bash
uvicorn harness_core.asgi:app \
  --host 0.0.0.0 \
  --port 8420 \
  --workers 4 \
  --timeout-graceful-shutdown 30 \
  --log-level info
```

### 6.2 Systemd Unit File Configuration
Create `/etc/systemd/system/harness.service`:

```ini
[Unit]
Description=Autonomous AI Harness Production Server
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=harness
Group=harness
WorkingDirectory=/opt/harness
Environment="PYTHONPATH=/opt/harness/src"
Environment="DATABASE_URL=postgresql://harness:secret@localhost:5432/harness_prod"
Environment="HARNESS_DATA_DIR=/var/lib/harness"
ExecStart=/opt/harness/.venv/bin/harness up --host 0.0.0.0 --port 8420 --drain-timeout 10.0
Restart=always
RestartSec=5s
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable harness.service
sudo systemctl start harness.service
```

### 6.3 Monitoring & Health Alerting
Configure external monitoring agents (Prometheus, Datadog, AWS CloudWatch, or Uptime Robot):

| Probe Endpoint | Target Interval | Success Condition | Alert Trigger | Severity |
|----------------|-----------------|-------------------|---------------|----------|
| `GET /livez`   | Every 5 seconds | HTTP `200 OK` | Fails 3 consecutive checks | Critical (Restart Pod/Process) |
| `GET /healthz` | Every 10 seconds| HTTP `200 OK` | HTTP `503 Service Unavailable` | High (Remove from LB / Page On-Call) |

#### Component Inspection via `/healthz`:
```bash
curl -s http://127.0.0.1:8420/healthz | jq .
```
Expected healthy output:
```json
{
  "status": "healthy",
  "components": {
    "database": "ok",
    "cow_storage": "ok",
    "workers": {
      "status": "ok",
      "total": 3,
      "healthy": 3
    }
  }
}
```

If `database` or `cow_storage` returns `{"status": "error"}`, the server returns `503 Service Unavailable`, allowing upstream ingress reverse proxies (Nginx/HAProxy/Traefik) to route requests to healthy peers immediately.

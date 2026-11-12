# Operator Runbook: Autonomous AI Harness (HEX)

This runbook provides canonical operational instructions for deploying, monitoring, administering, and troubleshooting the **HEX Autonomous AI Workbench & Harness** in development and production environments.

---

## 1. System Architecture & Topology

HEX operates as a resilient, distributed autonomous execution engine consisting of the following key planes:

### 1.1 Control Plane
- **CLI (`hex`)**: The primary command interface with specialized subcommands (`run`, `exec`, `task`, `chat`, `tui`, `voice`, `auth`, `quota`, `doctor`, `project`, `recipe`, `graphify`, `harness2-eval`, `gateway`, `server`, `daemon`, `uninstall`).
- **REST API (`/v1/*`)**: Handles task submission (`POST /v1/tasks`), status inspection (`GET /v1/tasks/{id}`), dynamic DAG replanning, worker registration, and remote approval decisions on default port `8420`.
- **SSE Event Stream (`GET /v1/tasks/{id}/events?stream=true`)**: Real-time event streaming for UI control surfaces and external monitors.
- **Web Control Surface (`GET /`)**: Zero-dependency local dashboard for inspecting state machines, active DAG steps, and call journals.
- **Liveness Probe (`GET /livez`)**: Returns `200 OK` with uptime and version for container orchestration (Kubernetes/Docker health checks).
- **Readiness Probe (`GET /healthz`)**: Multi-subsystem readiness check verifying database connectivity, CoW workspace filesystem write accessibility, and active worker metrics. Returns `503 Service Unavailable` when draining or degraded.

### 1.2 Storage Engine
- **Dual-Driver Architecture**: Supports PostgreSQL 16+ connection pooling (`asyncpg`) with row-level locking (`SELECT ... FOR UPDATE SKIP LOCKED`) for high-concurrency production clusters, and SQLite WAL mode for standalone/edge deployments.
- **Cryptographic Hash Chaining**: Every state transition and task event is chained via monotonic SHA-256 parent hashes (`verify_event_chain`).
- **CAS Call Journal**: 0ms memoization store recording idempotent tool execution fingerprints.

### 1.3 Isolation & Durability
- **Copy-on-Write (CoW)**:
  - macOS Darwin: Apple APFS clones (`cp -c -R`).
  - Linux: OverlayFS (lower/upper/work layers) and Btrfs subvolumes.
- **Container Sandboxing**: Bubblewrap (`bwrap`), Docker/Podman, or restricted subprocess boundaries preventing uncontained filesystem traversal and network escapes.

### 1.4 Two-Tier Model Routing Pipeline
- **Tier-1 Local / Free Tier**:
  - Apple Silicon M4 / Darwin ARM64 log compression reducing raw diagnostic outputs by >70% while scrubbing tokens and credentials.
  - Google Antigravity OAuth integration importing IDE tokens from `state.vscdb` for **$0 Day-1 cloud reasoning**.
- **Tier-2 Cloud Reasoners**: Anthropic Claude, OpenAI, and Google Gemini with strict privacy classification gating (blocking `sensitive` and `secret` egress) and per-task budget ceilings.

### 1.5 Distributed Workers & Fencing
- **Fencing Tokens**: Monotonically increasing CAS epoch tokens preventing split-brain writes.
- **CAS Delta Sync**: Incremental workspace archive transfer (`.tar.gz`) across heterogeneous worker nodes.

### 1.6 Security Gates & Self-Improvement
- **NVIDIA SkillSpector**: Automated AST scanner, YARA heuristic detector, and OSV vulnerability evaluator enforcing supply-chain gates on external skills.
- **Google Research Harness² (`vendor/harness2`)**: Content-addressed rollout caching (`runmark`), dual-substrate support (`OpencodeSubstrate` / `CodexSubstrate`), Propose–Probe–Compose sweeps, judging, and persistent domain Experience Libraries (`EXPERIENCE.md`).
- **Tool Gateway**: Default-deny risk-tiered execution policies (`R0` read-only to `R4` destructive shell).
- **Remote Webhooks**: Interactive Telegram Bot and Discord Webhook approval notifications with direct one-click action URLs.

---

## 2. Core CLI Operations Reference

### 2.1 Workspace & Project Administration

```bash
# List all registered projects
hex project list

# Register a new project directory
hex project add /path/to/project --name "my-service"

# Switch active workspace
hex project use my-service

# Prune dead or deleted project paths from ~/.hex/projects.json
hex project prune
```

### 2.2 Model Provider Authentication & Quota Management

```bash
# Check active authentication session (Antigravity OAuth / API keys)
hex auth --status

# Automatically import Google Antigravity OAuth session from local IDE
hex auth --import-ide

# Generate PKCE authorization URL for browser-based OAuth login
hex auth --auth-url

# Inspect live Google Antigravity model quota windows and reset times
hex quota

# Check system health, dependencies, and sandbox capabilities
hex doctor
```

### 2.3 Task Execution & Workflows

```bash
# Submit an autonomous coding task in the active workspace
hex run "Implement JWT validation middleware with unit tests"

# Execute single quick command with approval bypass on read-only tools
hex exec "ls -la && git status"

# Inspect task lifecycle, steps, and execution journal
hex task status <task_id>

# Cancel a running task safely
hex task cancel <task_id> --reason "Operator manual abort"
```

### 2.4 Controlled Self-Improvement (Google Research Harness²)

```bash
# Run parallel Propose-Probe-Compose sweep (k=2 iterations)
hex harness2-eval --task-id demo-1 --mode parallel --k 2

# Run sequential sweep with Codex substrate and live LLM proposer
hex harness2-eval --task-id ledger-1 --mode sequential --substrate codex --proposer llm --k 1 --json
```

### 2.5 Polyglot Knowledge Graph & Blast-Radius Engine (Graphify)

```bash
# Check Graphify CLI status and indexed graph stats
hex graphify status

# Trigger AST update on the workspace (extracts code files without LLM)
hex graphify update

# Ingest an existing graphify-out/graph.json into the active workbench
hex graphify ingest --graph graphify-out/graph.json

# Identify architectural hubs and god-nodes (top N connected symbols)
hex graphify god-nodes --top 10

# Query 1-hop and 2-hop affected blast-radius for a symbol or file
hex graphify affected calculate_discount

# Export self-contained interactive D3 architecture tree visualization
hex graphify tree --out architecture.html
```

### 2.6 Daemon & Service Management

```bash
# Start background worker daemon (processes background tasks and schedules)
hex daemon start

# Check background daemon status
hex daemon status

# Tail background daemon logs
hex daemon logs -n 50

# Stop background daemon
hex daemon stop

# Uninstall daemon launchd/systemd service
hex daemon uninstall
```

### 2.6 Full System Teardown & Uninstallation

```bash
# Dry-run inspection of uninstall actions
hex uninstall --dry-run

# Interactive uninstall preserving data directories
hex uninstall

# Complete purge removing all services, symlinks, and stored data (~/.hex, /var/lib/harness)
hex uninstall -y --purge
```

---

## 3. Disaster Recovery & Backup / Restore

### 3.1 PostgreSQL Production Backup
Run periodic automated backups of the authoritative database using `pg_dump`:

```bash
# 1. Take custom-format compressed backup
pg_dump -h $PGHOST -U $PGUSER -d $PGDATABASE -Fc -f /backups/hex_$(date +%Y%m%d_%H%M%S).dump

# 2. Automated hourly cron entry
0 * * * * pg_dump -h localhost -U hex_admin -d hex_prod -Fc -f /backups/hex_$(date +\%Y\%m\%d_\%H\%M\%S).dump
```

#### Point-in-Time Restore:
```bash
# 1. Stop inbound traffic (drain ASGI server)
sudo systemctl stop hex.service

# 2. Restore PostgreSQL database
pg_restore -h $PGHOST -U $PGUSER -d $PGDATABASE --clean --if-exists /backups/hex_20261101_040000.dump

# 3. Verify cryptographic event chain integrity
python3 -c "from harness_core.orchestrator import HarnessOrchestrator; orch = HarnessOrchestrator('$DATA_DIR'); tasks = orch.ledger.list_tasks(); assert all(orch.ledger.verify_event_chain(t['task_id']) for t in tasks); print('Cryptographic event chains intact.')"

# 4. Restart service
sudo systemctl start hex.service
```

### 3.2 SQLite WAL Backup (Standalone / Edge)
For SQLite deployments, use SQLite's online backup API to prevent database locking:

```bash
# Online safe backup while harness is running
sqlite3 /var/lib/harness/ledger.db ".backup /backups/ledger_backup.db"

# Integrity check
sqlite3 /backups/ledger_backup.db "PRAGMA integrity_check;"
```

### 3.3 CoW Snapshot Garbage Collection
Over time, finished task attempts and speculative branches consume disk space. Prune orphaned workspaces safely:

```bash
# Clean stale projects and verify integrity via CLI
hex project prune

# Find and remove attempt workspaces older than 7 days
find /var/lib/harness/active_workspaces/ -maxdepth 2 -type d -mtime +7 -name "attempt_*" -exec rm -rf {} +
```

---

## 4. Quarantined Skill Forensics & Threat Response

### 4.1 Understanding SkillSpector Scan Results
When an external skill is imported via `hex scan-skill <dir>` or seeded automatically, NVIDIA SkillSpector runs static AST parsing, YARA pattern matching, and OSV vulnerability lookups.

Possible statuses:
- `PASS_ALLOW` (Risk score 0-25): Safe for autonomous invocation.
- `WARN_RESTRICTED` (Risk score 26-59): Allowed with mandatory runtime approval (`R3`/`R4`).
- `REJECT_QUARANTINE` (Risk score 60-100): Blocked immediately; files moved to quarantine.

### 4.2 Triage Procedure for Quarantined Bundles
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
   - If false positive: refactor the skill code to avoid forbidden AST patterns, re-scan with `hex scan-skill <path>`, and verify the updated risk score drops below the quarantine threshold.

---

## 5. Task Cancellation, Stuck Lease Recovery & Zombie Worker Eviction

### 5.1 Emergency Task Cancellation
If a runaway task consumes unexpected resources or enters an unintended execution loop:

```bash
# Using HEX CLI
hex task status <task_id>
hex task cancel <task_id> --reason "Operator manual abort"

# Or cancel via HTTP API
curl -X POST http://127.0.0.1:8420/v1/tasks/<task_id>:cancel \
  -H "Content-Type: application/json" \
  -d '{"reason": "Operator manual abort"}'
```

The orchestrator halts execution, records a `STATE_CANCELED` event, transitions the task state, and releases any held workspace locks.

### 5.2 Automated vs. Manual Lease Reaping
Workers claim tasks with time-limited leases (default TTL: 300 seconds) and must periodically heartbeat:

```bash
# Automated lease reaping is executed by the control plane periodically:
python3 -c "from harness_core.orchestrator import HarnessOrchestrator; orch = HarnessOrchestrator('$DATA_DIR'); expired = orch.ledger.reap_expired_leases(); print('Reaped expired leases:', expired)"
```

Any task with an expired lease is automatically re-queued as `SUBMITTED` with an incremented fencing token.

### 5.3 Diagnosing and Resolving `ConcurrencyFenceError`
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
   - Verify the zombie worker terminated cleanly: `systemctl status hex-worker@<worker_id>`.
   - Restart the worker process so it queries the latest task state and registers fresh heartbeats.

---

## 6. Version Rollback & Database Downgrade

### 6.1 Application Code Rollback
To roll back the harness binary or package:

```bash
# 1. Stop service
sudo systemctl stop hex.service

# 2. Check out target tag or release commit
git checkout v1.0.0

# 3. Re-install virtual environment dependencies
.venv/bin/pip install -e .
```

### 6.2 Database Migration Downgrades with Alembic
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

## 7. Production ASGI / Systemd Deployment & Monitoring

### 7.1 Production Server Launch via CLI
HEX provides a built-in production server command with graceful shutdown:

```bash
hex server --host 0.0.0.0 --port 8420 --workers 4 --drain-timeout 10.0
```

Or using `uvicorn` with `src/harness_core/asgi.py`:

```bash
uvicorn harness_core.asgi:app \
  --host 0.0.0.0 \
  --port 8420 \
  --workers 4 \
  --timeout-graceful-shutdown 30 \
  --log-level info
```

### 7.2 Systemd Unit File Configuration (Linux)
Create `/etc/systemd/system/hex.service`:

```ini
[Unit]
Description=HEX Autonomous AI Workbench & Harness Production Server
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=hex
Group=hex
WorkingDirectory=/opt/hex
Environment="PYTHONPATH=/opt/hex/src"
Environment="DATABASE_URL=postgresql://hex:secret@localhost:5432/hex_prod"
Environment="HARNESS_DATA_DIR=/var/lib/harness"
ExecStart=/opt/hex/.venv/bin/hex server --host 0.0.0.0 --port 8420 --drain-timeout 10.0
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
sudo systemctl enable hex.service
sudo systemctl start hex.service
```

### 7.3 Launchd Daemon Configuration (macOS Darwin)
HEX includes built-in macOS daemon registration:

```bash
# Install and activate launchd user agent
hex daemon install

# Check status
hex daemon status

# Inspect service logs
hex daemon logs
```

### 7.4 Monitoring & Health Alerting
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

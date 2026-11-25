# `migrations`

**Authoritative SQL schema and Alembic migration history for the HEX ledger database (PostgreSQL 16+ & SQLite WAL).**

Configured by the root [`alembic.ini`](../alembic.ini) (`script_location = migrations`), this directory maintains schema parity between zero-config SQLite deployments and PostgreSQL production clusters.

## Contents

| File | Role |
| :--- | :--- |
| `0001_initial_schema.sql` | Full 18-table authoritative SQL DDL (`tasks`, `task_events`, `attempts`, `plans`, `checkpoints`, `tool_calls`, `approvals`, `artifacts`, `proof_bundles`, `skills`, `skill_scans`, `workers`, `leases`, `memories`, `harness_revisions`, `evaluations`, `approval_leases`, `dialogue_turns`) and lookup indexes. Byte-identical to the wheel-packaged copy in [`harness_core/migrations/`](../harness_core/migrations/0001_initial_schema.sql). |
| `env.py` | Alembic runtime environment resolving URLs from `HARNESS_DATABASE_URL` → `DATABASE_URL` → `alembic.ini` (`sqlite:///migrations/alembic_dev.db`). Supports offline SQL generation and both sync and async (`+asyncpg`/`+aiosqlite` with `greenlet`) online execution, enabling `render_as_batch=True` on SQLite. |
| `script.py.mako` | Alembic revision template. |
| `versions/0001_initial_schema.py` | Revision `0001_initial_schema` (`down_revision = None`): creates the initial 16 tables + `idx_tool_calls_lookup`. |
| `versions/0002_feature_schema_parity.py` | Revision `0002_feature_schema_parity`: adds `tasks.pinned`, `tasks.title_provenance`, `dialogue_turns` (+ `idx_dialogue_turns_lookup`), and `approval_leases` (+ `idx_approval_leases_lookup`) so fresh PostgreSQL installs match runtime SQLite tables. |

## Verification & Operations

```bash
# Check Alembic heads vs active ledger and verify SHA-256 event hash chain
python scripts/verify_event_chain.py

# Run migration parity tests
python -m pytest tests/storage/test_alembic_migrations.py -v
```

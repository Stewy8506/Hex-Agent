# `storage` (`harness_core.storage`)

**Durable task/event ledger (SQLite WAL & PostgreSQL), Copy-on-Write workspace isolation, execution sandboxes, OS keychain secrets, backup/restore, GC retention, project registry/init, and conversation-only timeline rewind.**

Mapped to `harness_core.storage` in [`pyproject.toml`](../pyproject.toml), this subsystem persists all harness state, isolates mutating tool runs, and protects credentials.

## Modules

| File | Purpose |
| :--- | :--- |
| `__init__.py` | PEP 562 lazy exports for 8 submodules (`db`, `cow`, `sandbox`, `backup`, `gc`, `project_registry`, `timeline_rewind`, `vault_keychain`). Note: `project_init.py` is omitted from `__all__` and must be imported directly as `harness_core.storage.project_init`. |
| `db.py` | `LedgerStore`, `ChainIntegrityReport`, `ClaimTransactionError`, `ConcurrencyFenceError`, `InvalidTransitionError`, `canonical_json()`, `sha256_hex()`, `compute_event_hash()`, `normalize_query()`, `normalize_param()`, `validate_dag_topology()`. Dual SQLite WAL (auto-initialized from [`harness_core/migrations/0001_initial_schema.sql`](../harness_core/migrations/0001_initial_schema.sql)) and PostgreSQL 16+ (`asyncpg` + `_SyncRunner` sync bridge, `FOR UPDATE SKIP LOCKED` claims) store managing tasks, SHA-256 hash-chained `task_events`, CAS fencing/leases, plans, checkpoints, idempotent `tool_calls` journal, approvals, proof bundles, FTS5 `search_index`, `dialogue_turns`, soft timeline branches, `approval_leases`, and GC queries. |
| `cow.py` | `WorkspaceSnapshotProvider`, `APFSAndPortableCoWProvider` (`ApfsSnapshotProvider`), `OverlayFSSnapshotProvider`, `BtrfsSnapshotProvider`, `get_default_snapshot_provider()`, `SnapshotMetadata`, `PublicationConflictError`, `IGNORE_DIRS`, `compute_tree_sha()`, `export_delta_archive()`, `import_delta_archive()`, `compute_workspace_diff()`, and `publish_delta()` with concurrent-edit conflict detection and rollback backups. |
| `sandbox.py` | `ExecutionSandbox`, `BubblewrapSandbox` (Linux `bwrap`), `DockerPodmanSandbox`, `RestrictedSubprocessSandbox` (trusted non-confined fallback), `get_default_sandbox()`, `SandboxResult`, `ConfinementUnavailableError`, and `sanitize_environment()`. Setting `HEX_REQUIRE_CONFINEMENT=1` (or `require_confinement=True`) fails closed instead of falling back to host execution. |
| `vault_keychain.py` | Native OS keyring integration (`SERVICE = "HEX Autonomous AI Workbench"`) rejecting insecure stubs (`fail`, `null`, `keyrings.alt` -> `KeychainUnavailable`): `os_keyring()`, `is_keychain_available()`, `backend_label()`, `store_secret()`, `get_secret()`, `delete_secret()`, and `migrate_plaintext_credentials()` (scrubs plaintext keys from `~/.hex/config.yaml`, `~/.hex/credentials.env`, and `./.hex/credentials.env`). |
| `backup.py` | `create_backup()`, `restore_backup()` (enforces strict zip-slip traversal validation raising `SecurityViolationError`), and `generate_dump()` (redacted diagnostics using `SECRET_ENV_KEYS` and [`hex_platform/diagnostics.py`](../hex_platform/README.md)). |
| `gc.py` | `GarbageCollector`, `GCReport`, `parse_ttl_seconds()`, `format_bytes()`. Sweeps expired `active_workspaces/` and `cow_snapshots/`, mark-and-sweeps `cas/` and `cas_artifacts/`, never collects active or pinned tasks, and runs SQLite vacuum. Defaults: `workspace_ttl="24h"`, `cas_ttl="7d"`, `auto_vacuum=True`, `orphan_guard_seconds=min(3600, workspace_ttl)`. |
| `project_registry.py` | `ProjectRegistry`, `ProjectRecord`, `compute_project_id()`. Persists workspace registrations in `~/.hex/projects.json` (respecting `HEX_PROJECTS_REGISTRY`) and initializes `.hex/reflections/LESSONS.md`. |
| `project_init.py` | `detect_project_stack()` (`python`, `node`, `rust`, `go`, `generic`) and `init_project()`, scaffolding `<repo>/.hex/{config.yaml,.gitignore,reflections/LESSONS.md,data/}` and registering the project. |
| `timeline_rewind.py` | `TimelineRewindManager`, `RewindTarget`, `RewindResult`, `group_messages_by_turn()`. Implements `/rewind` by marking superseded `dialogue_turns` inactive (`is_active=0`) under a `br_rewind_*` `branch_id` and reverting uncommitted tool calls via `LedgerStore.record_timeline_branch()`. Strictly conversation-only (workspace files are never modified). |

## Persistence, Schema & Planned Extensions

- **Local Paths**: `<data_dir>/ledger.db` (or Postgres DSN via `HARNESS_DATABASE_URL` / `DATABASE_URL`), `<data_dir>/{cow_snapshots,active_workspaces,cas,cas_artifacts}/` (standalone `get_default_snapshot_provider()` defaults to `~/.autonomous_harness/cow_snapshots`), `~/.hex/projects.json`, `~/.hex/data/task_search.db` (Postgres FTS5 fallback), and OS Keychain (`HEX Autonomous AI Workbench`).
- **Alembic & DDL**: Schema migrations for PostgreSQL live in [`migrations/`](../migrations/README.md).
- **Optional Dependencies**: PostgreSQL support uses the `postgres` extra (`asyncpg`, `sqlalchemy`, `alembic`); keyring uses `keyring`.
- **Planned (`BACKEND_PLAN.md`)**: `export_events_since(seq)` and `merge_remote_chain(events)` in `db.py` and Team Mesh replica snapshot pinning in `cow.py`/`gc.py` are not yet implemented.

## Verification

Tests live in [`tests/storage/`](../tests/storage).

```bash
python -m pytest tests/storage/ -v
```

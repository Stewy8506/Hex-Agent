# Phase 12 Plan 02: SQLite FTS5 Full-Text Task Search & State Backup/Dump Summary

**Plan:** 12-02-PLAN.md  
**Status:** Complete  
**Commits:** `a0819c6`, `8146fb2`, `dd5499d`  
**Test Suite:** 16/16 new tests green, 386/386 full regression test suite green  

---

## 1. Accomplishments

### A. SQLite FTS5 Full-Text Search Engine (`src/harness_core/search.py`)
- Created `TaskSearchIndex` managing a dedicated SQLite FTS5 virtual table (`task_search_fts`) with unicode61 tokenizer diacritic removal.
- Implemented `sanitize_fts5_query`:
  - Cleans input queries against FTS5 special operators and syntax (`+{}():^@/#&|~[]<>,;!?$=\'`).
  - Preserves balanced double-quoted phrases and trailing prefix wildcards (e.g. `auth*`).
  - Normalizes tokens, handles unmatched quotes without syntax crashes, and strips dangling boolean operators (`AND`, `OR`, `NOT`).
  - Wraps hyphenated and dotted tokens in quotes to prevent tokenizer splitting.
- Implemented `TaskSearchResult` with BM25 relevance scoring, column-specific match classification (`goal`, `tool_calls`, `execution_logs`), and snippet generation with `<b>...</b>` highlight tags.
- Built `reindex_all(ledger)` to rebuild the virtual table from historical task, tool call, and event log records.

### B. LedgerStore Synchronization & CLI Search (`src/harness_core/db.py`, `src/harness_core/cli.py`)
- Integrated `search_index` property into `LedgerStore` with lazy initialization and attachment to the SQLite WAL database.
- Added automatic FTS5 index updates on `submit_task` (and `submit_task_async`), `transition_task` (status updates), and `seal_tool_call` (tool invocation transcripts).
- Added `search_tasks(query, project_id, limit)` method on `LedgerStore`.
- Added `record_tool_call` convenience helper to `LedgerStore`.
- Wired `hex search <query>` subcommand in `src/harness_core/cli.py` supporting `--project`, `--limit`, `--reindex`, and `--json` output formats.

### C. State Backup, Safe Restore with Zip-Slip Defense & Support Dump (`src/harness_core/backup.py`)
- Implemented `create_backup`:
  - Creates timestamped `hex-backup-<YYYYMMDD-HHMMSS>.zip` archives.
  - Automatically executes `PRAGMA wal_checkpoint(TRUNCATE)` on active SQLite databases.
  - Archives configurations (`config.yaml`), databases (`hex.db`, `harness.db`), cron schedules (`schedules.json`), and custom skills.
  - Excludes transient data: `.hex/cow_workspaces/`, `.hex/cache/`, virtualenvs (`.venv/`, `env/`, `venv/`), `__pycache__`, `.git`, socket/lock files, and files > 50MB.
  - Supports `--quick` mode archiving strictly database and configuration files.
  - Computes SHA-256 archive checksum.
- Implemented `restore_backup`:
  - Enforces strict Zip-Slip path traversal defense: inspects every `ZipInfo` entry, rejecting leading slashes, backslashes, `..` path segments, and resolving canonical destination paths against `target_dir`.
  - Raises `SecurityViolationError` on traversal attempts.
  - Supports `--dry-run` inspection mode.
- Implemented `generate_dump`:
  - Produces an uncolored, deterministic plain-text system diagnostic profile across 7 structured sections:
    1. HEADER & SYSTEM INFO
    2. HARDWARE & ACCELERATION (Apple Silicon chip, MLX, Metal, CPU)
    3. STORAGE & LEDGER METRICS (DB engine, task count, tool call count, completion ratio)
    4. WORKSPACE & COW ISOLATION (APFS/OverlayFS/Btrfs snapshotting, sandbox engines, disk space)
    5. ACTIVE INTEGRATIONS & RECEPTORS (model credentials scrubbed to `[CONFIGURED - REDACTED]`)
    6. SCHEDULES & CUSTOM SKILLS (cron expressions, skill bundles)
    7. RECENT ERRORS (terminal reasons and failed task diagnostics)
- Wired `hex backup`, `hex restore <archive>`, and `hex dump` subcommands into `src/harness_core/cli.py`.

### D. Verification & Test Suite (`tests/test_search_and_backup.py`)
- Authored 16 comprehensive unit and integration tests covering:
  - FTS5 virtual table creation and unicode61 tokenizer.
  - Query sanitization against special characters, unmatched quotes, prefix tokens, and boolean edge cases.
  - BM25 ranking and field-specific snippet extraction.
  - Tool call transcript indexing and searching.
  - LedgerStore search synchronization and full reindexing.
  - CLI `hex search` in text and JSON modes.
  - Backup creation with core inclusion and transient directory exclusion.
  - Quick backup mode.
  - Restore round-trip integrity.
  - Strict zip-slip defense against path traversal attacks.
  - Restore dry-run mode.
  - Diagnostic dump generation, secret scrubbing, and recent failure summaries.
  - CLI `backup`, `restore`, and `dump` subcommands.

---

## 2. Verification Proof

- **Plan Unit & Integration Tests:**
  `python3 -m unittest tests/test_search_and_backup.py`
  Result: 16/16 tests passing (OK in 1.20s).

- **Full Regression Test Suite:**
  `python3 -m unittest discover tests`
  Result: 386/386 tests passing (OK in 30.62s). Zero regressions.

---

## 3. Deviations & Corrections

- **State Transition Guard:** In `TestLedgerSearchIntegration` and `TestDiagnosticDump`, tasks were created with initial state `QUEUED` / `RUNNING` to satisfy strict state machine transition validation rules (`QUEUED -> RUNNING` and `RUNNING -> FAILED`).
- **Database Discovery in CLI:** Enhanced `cli.py` search handler to detect existing `.db` files in custom `--data-dir` directories even when non-default database file names are used.

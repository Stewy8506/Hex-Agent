# `scripts`

**Standalone operational verification and benchmarking utilities.**

## Scripts

| Script | Purpose | Usage |
| :--- | :--- | :--- |
| `verify_event_chain.py` | Audits `LedgerStore.verify_chain_integrity()` across all SHA-256 hash-chained `task_events` and checks Alembic `current` vs `heads` (auto-stamping existing initialized DBs if unversioned). Exits `0` if valid, `1` on tamper or schema drift. | `python scripts/verify_event_chain.py` (uses `HARNESS_DATABASE_URL`, `HEX_DATABASE_URL`, `~/.hex/data/ledger.db`, or `migrations/alembic_dev.db`) |
| `tui_memory_bench.py` | PTY process-tree RSS memory sampler (`ps -axo pid=,ppid=,rss=,command=`) measuring `hex-tui` (and child ACP Python process) peak/final RSS bytes without sending any model requests. Outputs JSON telemetry. | `python scripts/tui_memory_bench.py -- <command...>` |

## Tests

- `tests/storage/test_event_chain_verification.py`
- `tests/client/test_tui_memory_benchmark.py`

# Plan 31.03: Zettelkasten A-MEM Controller, Memory Lifecycle & Lightweight Rollback — Summary

**Execution Date:** 2026-11-14  
**Status:** Complete  
**Verification:** All 10 tests in `tests/cognition/test_amem_and_lifecycle.py` passing, all 27 tests in `tests/cognition/test_ast_scoped_memory.py` and `tests/cognition/test_curated_memory.py` green (37 total tests passed in 3.17s).

---

## Accomplishments

### Task 1: Zettelkasten A-MEM Controller & Contradiction Resolution
- Implemented `MemoryItem` structured dataclass with `id`, `text`, `tags` (`[[concept-tag]]`), `scope` (`memory` | `user` | `guardrails`), `hit_counter`, `created_at`, and `last_referenced_at`.
- Implemented `extract_wiki_tags` regex parser extracting and normalizing wiki-style `[[tag]]` concept links.
- Implemented `AMEMController` in `harness_core/memory.py`:
  - CRUD Decision Engine supporting `ADD`, `UPDATE`, `DELETE`, and `NOOP`.
  - Inverted tag index (`_tag_index: dict[str, set[str]]`) mapping concept tags to memory item IDs.
  - Neighborhood retrieval `get_linked_neighborhood(tag: str) -> list[MemoryItem]` allowing localized retrieval of conceptual neighborhoods without prompt token explosion.
  - Automated contradiction resolution in `detect_contradiction` and `process_memory`:
    - Evaluates transition markers (e.g. "instead of X", "rather than X", "migrated from X to Y", "switched DB to Y", "replaces X", "deprecated X", "removed X").
    - Detects polarity shifts and topic conflicts under common concept tags (e.g. dark vs light mode).
    - Automatically executes in-place `UPDATE` on superseded notes and `DELETE` on removed/deprecated notes rather than appending conflicting bullets.
- Committed as `feat(phase-31): implement zettelkasten a-mem controller and contradiction resolution` (`9880f20`).

### Task 2: Memory Lifecycle Engine & SQLite FTS5 Cold Archival
- Implemented `MemoryLifecycleManager` in `harness_core/memory.py`:
  - Hit counter and reference tracking with `record_reference(entry_id: str)` updating timestamps and incrementing usage.
  - Tracking reflections from `.hex/reflections/LESSONS.md` via `record_reflection` and JSON metadata synchronization.
  - Promotion protocol in `check_promotions() -> list[dict]` and `promote_entry(entry_id, target)`: reflections with $\ge 3$ hits qualify for promotion into active `MEMORY.md` (or flag for skill compilation when $\ge 5$).
  - Cold archival storage using SQLite database (`archive.db`) and FTS5 virtual table (`archived_memories_fts`) with unicode61 tokenizer.
  - Demotion protocol in `run_demotion_sweep(retention_days=30)`: prunes unreferenced (>30d) or AST-diverged notes from active markdown stores (`MEMORY.md` / `USER.md`) and indexes them into the cold archive.
  - Ranked BM25 lexical search over cold archived notes with `search_archived_memories(query, limit)`.
- Committed as `feat(phase-31): implement confidence decay hit-counter promotion and sqlite fts5 cold archival` (`72eb71c`).

### Task 3: Lightweight .history/ Snapshots & Rollback CLI/TUI Ergonomics
- Implemented `HistorySnapshotManager` in `harness_core/memory.py`:
  - Saves timestamped `<filename>.<timestamp>.bak` snapshots into `.hex/memories/.history/` on every write operation in `CuratedMemoryStore` and `GuardrailsManager`.
  - Automatically bounds snapshot history to 20 per file with FIFO pruning.
  - Provides inspection via `list_history(target) -> list[dict]` and instant restoration via `rollback(target, timestamp) -> bool`.
- Extended CLI subcommands in `harness_core/cli.py`:
  - `hex memory list [--target user|memory|guardrails] [--json]`
  - `hex memory search "<query>" [--limit <n>] [--json]`
  - `hex memory history [--target <target>] [--json]`
  - `hex memory rollback <target> <timestamp>`
- Extended TUI slash commands in `harness_core/tui.py`:
  - `/memory`: displays governed curated memory token/char summary and active guardrail count.
  - `/memory history [target]`: lists timestamped snapshots.
  - `/memory rollback <timestamp>`: executes safe rollback to chosen snapshot.
  - `/memory <query>`: searches across active memories, orchestrator memories, and SQLite FTS5 cold archive.
- Authored test suite covering CRUD, contradiction resolution, wiki-links, hit counters, promotion, FTS5 demotion, history snapshots, and CLI/TUI commands in `tests/cognition/test_amem_and_lifecycle.py`.
- Committed as `feat(phase-31): implement history snapshots and rollback cli and tui ergonomics` (`d51f6db`).

---

## Empirical Verification

1. `tests/cognition/test_amem_and_lifecycle.py`:
   ```
   tests/cognition/test_amem_and_lifecycle.py::TestAMEMController::test_amem_crud PASSED
   tests/cognition/test_amem_and_lifecycle.py::TestAMEMController::test_contradiction PASSED
   tests/cognition/test_amem_and_lifecycle.py::TestAMEMController::test_wiki_links PASSED
   tests/cognition/test_amem_and_lifecycle.py::TestMemoryLifecycleManager::test_demotion_fts5 PASSED
   tests/cognition/test_amem_and_lifecycle.py::TestMemoryLifecycleManager::test_lifecycle_hit_counter PASSED
   tests/cognition/test_amem_and_lifecycle.py::TestMemoryLifecycleManager::test_promotion PASSED
   tests/cognition/test_amem_and_lifecycle.py::TestHistoryAndRollback::test_history_snapshots_and_rollback PASSED
   tests/cognition/test_amem_and_lifecycle.py::TestCliAndTuiMemory::test_cli_memory_history_and_rollback PASSED
   tests/cognition/test_amem_and_lifecycle.py::TestCliAndTuiMemory::test_cli_memory_list_and_search PASSED
   tests/cognition/test_amem_and_lifecycle.py::TestCliAndTuiMemory::test_tui_memory_slash_commands PASSED
   ============================== 10 passed in 2.41s ==============================
   ```

2. Existing AST-Scoped and Curated Memory Suites (`tests/cognition/test_ast_scoped_memory.py`, `tests/cognition/test_curated_memory.py`):
   ```
   ============================== 27 passed in 1.39s ==============================
   ```

3. Full Combined Phase 31 Memory Suite (37 tests):
   ```
   ============================== 37 passed in 3.17s ==============================
   ```

---

## Commits Landed

- `9880f20`: `feat(phase-31): implement zettelkasten a-mem controller and contradiction resolution`
- `72eb71c`: `feat(phase-31): implement confidence decay hit-counter promotion and sqlite fts5 cold archival`
- `d51f6db`: `feat(phase-31): implement history snapshots and rollback cli and tui ergonomics`

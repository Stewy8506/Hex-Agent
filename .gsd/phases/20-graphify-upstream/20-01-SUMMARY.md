# Plan 20-01 Summary: Upstream Graphify CLI Runner, Ingestion Adapter & CLI Wiring

**Date:** 2026-11-12  
**Commit:** Pending  
**Status:** Complete  

## Accomplishments
1. **Implemented `GraphifyUpstreamRunner` in `src/harness_core/graphify.py`**:
   - `is_cli_available() -> bool`: checks for `graphify` executable via `shutil.which` or vendored directory in `vendor/graphify`.
   - `run_command(cmd, args, cwd, timeout)`: executes `graphify <cmd>` if executable binary is present, or falls back to `[sys.executable, "-m", "graphify", cmd, *args]` with `vendor/graphify` prepended to `PYTHONPATH`. Handles timeouts cleanly returning `(-1, "", "Timeout expired")`.
   - `run_update(workspace_root, force=True, no_cluster=True)`: executes CLI update, checking for `graphify-out/graph.json`, and falls back in-process to `graphify.watch._rebuild_code(watch_path=ws, force=force, no_cluster=no_cluster, acquire_lock=False)`.
   - `run_extract(workspace_root, force=True)`: executes CLI extract or in-process `_rebuild_code(..., no_cluster=True)`.

2. **Implemented `ActiveGraphifyAdapter.ingest_upstream_graph` in `src/harness_core/graphify.py`**:
   - Discovers candidate graph path at `graphify-out/graph.json` or `.hex/graph.json`, returning `{"ingested": False, ...}` if missing (preserving fast 0ms native Python AST fallback).
   - Ingests nodes into `self.graph` (NetworkX DiGraph), reconciling file and symbol nodes into `self.symbols_by_file`, `self.symbols_by_name`, and `self.file_of_qualified` with `SymbolDef` records.
   - Ingests edges/links into `self.graph` with relation and confidence metadata, reconciling `self.callers_of` for reverse dependency traversal.
   - Enhanced `export_interactive_html` to support optional output path and fallback to ingested graph nodes.

3. **Registered `hex graphify` CLI Subcommand Suite in `src/harness_core/cli.py`**:
   - `hex graphify status`: reports CLI availability, vendor path, active graph file, and indexed node/edge statistics.
   - `hex graphify update`: triggers CLI/in-process AST rebuild and ingests result.
   - `hex graphify ingest`: ingests graph from file or workspace output.
   - `hex graphify god-nodes`: computes core high-degree architectural abstractions.
   - `hex graphify affected <target>`: queries 1-hop and 2-hop blast-radius impact.
   - `hex graphify tree`: generates interactive D3 HTML architecture explorer.

4. **Empirical Verification**:
   - Authored comprehensive test suite in `tests/test_graphify_upstream_wiring.py` covering runner discovery, in-process fallback, JSON ingestion, missing graph fallback, and CLI commands (5/5 tests passing).
   - Ran full test suite: 537/537 tests passing with zero regressions across the repository.

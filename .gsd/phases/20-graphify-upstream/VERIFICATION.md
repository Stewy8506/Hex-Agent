# Phase 20 Verification: Graphify Upstream CLI & Tree-Sitter Adapter

**Phase:** Phase 20 - Graphify Upstream CLI & Tree-Sitter Adapter  
**Date:** 2026-11-12  
**Verdict:** PASS  
**Must-Haves Met:** 5/5  
**Test Suite:** 542/542 tests passing (100% green)  

---

## 1. Executive Summary

Phase 20 integrates the upstream Graphify tree-sitter polyglot AST parser and knowledge graph into the HEX Autonomous AI Workbench. It delivers:
1. `GraphifyUpstreamRunner` execution bridge with dynamic CLI binary discovery, `sys.executable -m graphify` fallback with vendored directory injection on `PYTHONPATH`, and an in-process fallback using `graphify.watch._rebuild_code`.
2. `ActiveGraphifyAdapter.ingest_upstream_graph` ingesting `graphify-out/graph.json` or `.hex/graph.json` into the NetworkX `DiGraph`, reconciling node and edge schemas, populating symbol definitions, callers, and file qualification tables, while preserving 0ms native Python AST fallback when absent.
3. Full CLI surface: `hex graphify` with `status`, `update`, `ingest`, `god-nodes`, `affected`, and `tree`.
4. Unified polyglot blast-radius tracking across Python, TypeScript/JavaScript, Go, Rust, Java, and C/C++, with multi-language test detection and automatic ledger test pinning in `ToolGateway` on `write_file`.

All 5 must-have specifications were empirically verified with real command executions and tests.

---

## 2. Must-Have Proof & Empirical Evidence

### Must-Have 1: `GraphifyUpstreamRunner` Discovery, Execution Bridge & In-Process Fallback

**Requirement:**
- Discovers `graphify` CLI executable or runs via Python module `-m graphify` using `vendor/graphify` on PYTHONPATH, with in-process fallback using `graphify.watch._rebuild_code`.
- Exposes `is_cli_available()`, `run_command()`, `run_update()`, `run_extract()`.

**Verification Command:**
```bash
.venv/bin/python -c "from harness_core.graphify import GraphifyUpstreamRunner; r = GraphifyUpstreamRunner(); print('is_cli_available:', r.is_cli_available()); print('has run_command:', callable(getattr(r, 'run_command', None))); print('has run_update:', callable(getattr(r, 'run_update', None))); print('has run_extract:', callable(getattr(r, 'run_extract', None)))"
```

**Output:**
```
is_cli_available: True
has run_command: True
has run_update: True
has run_extract: True
```

**Unit Test Evidence:**
```bash
.venv/bin/python -m unittest tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_runner_discovery_and_command tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_runner_inprocess_fallback -v
```
Output:
```
test_runner_discovery_and_command (tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_runner_discovery_and_command) ... ok
test_runner_inprocess_fallback (tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_runner_inprocess_fallback) ... ok
```
**Verdict:** PASS

---

### Must-Have 2: Upstream Graph Ingestion Adapter in `ActiveGraphifyAdapter`

**Requirement:**
- `ingest_upstream_graph(path: Path | str | None = None) -> dict[str, Any]` loads `graphify-out/graph.json` or `.hex/graph.json`.
- Populates `self.graph` (NetworkX DiGraph), `self.symbols_by_file`, `self.callers_of`, `self.file_of_qualified`, reconciling nodes and edges.
- Retains 0ms native Python AST fallback when upstream graph is not present.

**Verification Command:**
```bash
.venv/bin/python -c "import tempfile, json, pathlib; from harness_core.graphify import ActiveGraphifyAdapter;
td = tempfile.TemporaryDirectory(); p = pathlib.Path(td.name);
adapter = ActiveGraphifyAdapter(p);
res_missing = adapter.ingest_upstream_graph();
assert res_missing['ingested'] is False, 'Should not ingest missing graph';
out_dir = p / 'graphify-out'; out_dir.mkdir();
sample_graph = {'version': 1, 'nodes': [{'id': 'app.py', 'label': 'app.py', 'kind': 'file', 'source_file': 'app.py', 'file_type': 'code'}, {'id': 'app.py:main', 'label': 'main', 'kind': 'function', 'source_file': 'app.py', 'file_type': 'code', 'lineno': 10}], 'links': [{'source': 'app.py:caller', 'target': 'app.py:main', 'relation': 'calls'}]};
(out_dir / 'graph.json').write_text(json.dumps(sample_graph));
res_ingest = adapter.ingest_upstream_graph();
assert res_ingest['ingested'] is True;
assert len(adapter.graph.nodes) >= 2;
assert 'app.py:main' in adapter.file_of_qualified;
assert 'app.py:main' in adapter.callers_of;
print('Ingest verified successfully:', res_ingest);
td.cleanup()"
```

**Output:**
```
Ingest verified successfully: {'ingested': True, 'path': '/private/var/folders/69/sjdvdg8j0qn8qgdf65s_62440000gn/T/tmpd0md9qzq/graphify-out/graph.json', 'node_count': 3, 'edge_count': 1}
```

**Unit Test Evidence:**
```bash
.venv/bin/python -m unittest tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_ingest_upstream_graph_valid_json tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_ingest_upstream_graph_missing_fallback -v
```
Output:
```
test_ingest_upstream_graph_missing_fallback (tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_ingest_upstream_graph_missing_fallback) ... ok
test_ingest_upstream_graph_valid_json (tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_ingest_upstream_graph_valid_json) ... ok
```
**Verdict:** PASS

---

### Must-Have 3: CLI Subcommand Suite `hex graphify`

**Requirement:**
- CLI exposure: `hex graphify` (`status`, `update`, `ingest`, `god-nodes`, `affected`, `tree`) in `src/harness_core/cli.py`.

**Verification Commands & Outputs:**

1. **Help & Subcommands:**
```bash
.venv/bin/python -m harness_core.cli graphify --help
```
Output:
```
usage: hex graphify [-h] {status,update,ingest,god-nodes,affected,tree} ...

positional arguments:
  {status,update,ingest,god-nodes,affected,tree}
                        Graphify subcommands
    status              Inspect Graphify CLI discovery, active graph paths,
                        and node/edge statistics
    update              Run Graphify incremental update on workspace (CLI or
                        in-process fallback)
    ingest              Ingest graphify-out/graph.json or .hex/graph.json into
                        active architecture engine
    god-nodes           Analyze core architectural abstractions and god nodes
    affected            Query blast-radius and affected nodes for a symbol or
                        file
    tree                Generate self-contained interactive D3 architecture
                        tree visualization HTML
```

2. **`hex graphify status --json`:**
```bash
.venv/bin/python -m harness_core.cli graphify status --json
```
Output:
```json
{
  "cli_available": true,
  "vendor_directory": "/Users/anv./Development/Harness/vendor/graphify",
  "workspace": "/Users/anv./Development/Harness",
  "active_graph_path": "/Users/anv./Development/Harness/.hex/graph.json",
  "node_count": 25239,
  "edge_count": 155911,
  "upstream_graphify_library": true
}
```

3. **`hex graphify god-nodes --top 3`:**
```bash
.venv/bin/python -m harness_core.cli graphify god-nodes --top 3
```
Output:
```
Top 3 Architectural God Nodes:
  1. vendor.graphify.tests.fixtures.sample.get (degree: 3290)
  2. vendor.graphify.graphify.extract.extract (degree: 1194)
  3. vendor.harness2.harness2.install.patch (degree: 840)
```

4. **`hex graphify affected`:**
```bash
.venv/bin/python -m harness_core.cli graphify affected "vendor.graphify.graphify.extract.extract" --depth 1
```
Output:
```
Blast-Radius Impact for 'vendor.graphify.graphify.extract.extract' (depth 1):
  - vendor/graphify/graphify/extract.py (depth: 1, via: defines)
```

5. **`hex graphify ingest --json`:**
```bash
.venv/bin/python -m harness_core.cli graphify ingest --json
```
Output:
```json
{
  "ingested": true,
  "path": "/Users/anv./Development/Harness/.hex/graph.json",
  "node_count": 33906,
  "edge_count": 155867
}
```

6. **`hex graphify tree --output /tmp/test_hex_tree.html --json`:**
```bash
.venv/bin/python -m harness_core.cli graphify tree --output /tmp/test_hex_tree.html --json
```
Output:
```json
{
  "html_file": "/private/tmp/test_hex_tree.html"
}
```

**Verdict:** PASS

---

### Must-Have 4: Unified Polyglot Blast-Radius Tracking

**Requirement:**
- `ActiveGraphifyAdapter.compute_blast_radius` handles polyglot code files (Python, TypeScript, Go, Rust, Java, C++).
- `ActiveGraphifyAdapter._is_test_file` detects test files across all supported extensions while rejecting production files.
- `ActiveBlastRadiusInterceptor` and `ToolGateway.invoke` for `write_file` trigger blast-radius calculation on polyglot modifications and pin tests to the ledger.

**Verification Command:**
```bash
.venv/bin/python -m unittest tests/test_graphify_polyglot_blast_radius.py -v
```

**Output:**
```
test_active_blast_radius_interceptor_contract (tests.test_graphify_polyglot_blast_radius.TestGraphifyPolyglotBlastRadius.test_active_blast_radius_interceptor_contract)
Validates ActiveBlastRadiusInterceptor helper contract and non-code file exclusion. ... ok
test_compute_blast_radius_native_java_and_cpp (tests.test_graphify_polyglot_blast_radius.TestGraphifyPolyglotBlastRadius.test_compute_blast_radius_native_java_and_cpp)
Validates native polyglot AST indexing and blast-radius tracking for Java and C++ files. ... ok
test_compute_blast_radius_upstream_polyglot (tests.test_graphify_polyglot_blast_radius.TestGraphifyPolyglotBlastRadius.test_compute_blast_radius_upstream_polyglot)
Builds a multi-language graph and verifies 1-hop/2-hop dependents and impacted tests. ... ok
test_gateway_interceptor_polyglot_write_file (tests.test_graphify_polyglot_blast_radius.TestGraphifyPolyglotBlastRadius.test_gateway_interceptor_polyglot_write_file)
Verifies ToolGateway with polyglot file (Go/TS/Rust) triggers ast_blast_radius and updates ledger. ... ok
test_is_test_file_polyglot_extensions (tests.test_graphify_polyglot_blast_radius.TestGraphifyPolyglotBlastRadius.test_is_test_file_polyglot_extensions)
Validates _is_test_file across Python, TS/JS, Go, Rust, Java, C++, rejecting production files. ... ok

----------------------------------------------------------------------
Ran 5 tests in 0.307s

OK
```

**Verdict:** PASS

---

### Must-Have 5: Zero Failures Across Test Suites

**Requirement:**
- `tests/test_graphify_upstream_wiring.py` (5/5 tests passing)
- `tests/test_graphify_polyglot_blast_radius.py` (5/5 tests passing)
- Full test suite passes cleanly with zero regressions.

**1. Wiring Suite:**
```bash
.venv/bin/python -m unittest tests/test_graphify_upstream_wiring.py -v
```
Output:
```
test_cli_graphify_commands (tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_cli_graphify_commands) ... ok
test_ingest_upstream_graph_missing_fallback (tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_ingest_upstream_graph_missing_fallback) ... ok
test_ingest_upstream_graph_valid_json (tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_ingest_upstream_graph_valid_json) ... ok
test_runner_discovery_and_command (tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_runner_discovery_and_command) ... ok
test_runner_inprocess_fallback (tests.test_graphify_upstream_wiring.TestGraphifyUpstreamWiring.test_runner_inprocess_fallback) ... ok

----------------------------------------------------------------------
Ran 5 tests in 3.055s

OK
```

**2. Polyglot Suite:**
```bash
.venv/bin/python -m unittest tests/test_graphify_polyglot_blast_radius.py -v
```
Output:
```
Ran 5 tests in 0.307s

OK
```

**3. Full Repository Test Suite:**
```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```
Output:
```
Ran 542 tests in 161.263s

OK
```

**Verdict:** PASS

---

## 3. Verdict Summary

| Check | Requirement | Result |
|---|---|---|
| Must-Have 1 | `GraphifyUpstreamRunner` Discovery & In-Process Fallback | PASS |
| Must-Have 2 | Upstream Graph Ingestion Adapter in `ActiveGraphifyAdapter` | PASS |
| Must-Have 3 | `hex graphify` CLI Subcommands (`status`, `update`, `ingest`, `god-nodes`, `affected`, `tree`) | PASS |
| Must-Have 4 | Unified Polyglot Blast-Radius Tracking & Test Pinning | PASS |
| Must-Have 5 | Zero Failures Across Wiring, Polyglot, and Full Test Suite (542/542) | PASS |

**Final Phase Verdict:** PASS

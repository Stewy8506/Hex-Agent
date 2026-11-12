# Plan 20-02 Summary: Polyglot Graph Unification & Multi-Language Blast-Radius Verification

**Date:** 2026-11-12  
**Commit:** Pending  
**Status:** Complete  

## Accomplishments
1. **Polyglot Extension Expansion & Symbol Extraction in `src/harness_core/graphify.py`**:
   - Expanded `SUPPORTED_CODE_EXTENSIONS` to include Java (`.java`) and C/C++ (`.c`, `.cpp`, `.cc`, `.cxx`, `.h`, `.hpp`) alongside Python, TypeScript/JavaScript, Go, and Rust.
   - Enhanced `_index_polyglot_file` with regex extractors for Java imports, classes/records, methods, and generic calls, and C/C++ includes, classes/structs, functions, and generic calls.
   - Updated `ActiveGraphifyAdapter._is_test_file(rel_path: str) -> bool`:
     - Python tests: `test_*.py`, `*_test.py`
     - TypeScript / JavaScript tests: `*.test.ts`, `*.test.tsx`, `*.spec.ts`, `*.spec.tsx`, `*.test.js`, `*.spec.js`, `*.test.jsx`, `*.spec.jsx`, `*.test.mjs`, `*.spec.mjs`
     - Go tests: `*_test.go`
     - Rust tests: `test_*.rs`, `*_test.rs`, paths under `tests/*.rs`
     - Java tests: `*Test.java`, `*Tests.java`, `*TestCase.java`, `Test*.java`
     - C/C++ tests: `test_*.cpp`, `test_*.cc`, `*_test.cpp`, `*_unittest.cc`, `test_*.c`, `*_test.cc`
     - Directory path recognition: detects test files in `tests/`, `__tests__/`, and `spec/`.
     - Strict production file rejection: correctly rejects production source files (`auth.ts`, `router.go`, `OrderService.java`, `parser.rs`, `crypto.cpp`, `api.py`).

2. **Polyglot Blast-Radius Calculation in `ActiveGraphifyAdapter.compute_blast_radius`**:
   - Unified polyglot file resolution: non-Python modified files resolve symbols and file nodes from `self.graph` and `self.symbols_by_file`.
   - Traverses 1-hop and 2-hop dependents across both `upstream_affected.affected_nodes` (if active) and direct NetworkX `in_edges` / `predecessors` traversal.
   - Preserves fast native `self.callers_of` lookup and test import/stem matching.
   - Automatically re-ingests `graphify-out/graph.json` upon refresh if present, merging tree-sitter polyglot extracted graphs.
   - Maps affected node IDs to their source files across all supported languages and pins affected test files into `impacted_tests`.
   - Generates descriptive warning messages detailing polyglot modified files, 1-hop/2-hop dependents, and impacted tests.

3. **ActiveBlastRadiusInterceptor & ToolGateway R2 Write Hook in `src/harness_core/gateway.py`**:
   - Defined `ActiveBlastRadiusInterceptor` wrapping `ActiveGraphifyAdapter.compute_blast_radius` with `inspect_file_modification(ws_path, rel_p, ...)` filtering on `SUPPORTED_CODE_EXTENSIONS` and returning `BlastRadiusReport | None`.
   - Updated `ToolGateway.invoke` for `write_file`: replaced Python-only check with `Path(rel_p).suffix.lower() in SUPPORTED_CODE_EXTENSIONS`.
   - Attached `ast_blast_radius` to the sanitized tool invocation result.
   - When `br.impacted_tests` or `br.hop1_dependents` are detected, automatically calls `self.ledger.append_blast_radius_checks(task_id, br.impacted_tests, br.warning_message)`.

4. **Empirical Verification**:
   - Authored comprehensive test suite in `tests/test_graphify_polyglot_blast_radius.py` with 5 test cases covering:
     1. `test_is_test_file_polyglot_extensions`: validates test file recognition across Python, TS/JS, Go, Rust, Java, C++, and directories (`tests/`, `__tests__/`, `spec/`), rejecting production files.
     2. `test_compute_blast_radius_upstream_polyglot`: builds a multi-language graph (Go backend calling database, tested by `db_test.go`; TypeScript frontend calling API service, tested by `Profile.test.tsx`), invoking `compute_blast_radius` and verifying 1-hop/2-hop dependents and pinned tests.
     3. `test_compute_blast_radius_native_java_and_cpp`: validates native polyglot symbol extraction and test pinning for Java (`OrderServiceTest.java`) and C++ (`test_crypto.cpp`).
     4. `test_gateway_interceptor_polyglot_write_file`: verifies `ToolGateway` with Go, TypeScript, and Rust files triggers `ast_blast_radius` and records pinned tests into active ledger plans.
     5. `test_active_blast_radius_interceptor_contract`: verifies helper contract and non-code file exclusion (`README.md`).
   - Ran complete repository test suite: 542/542 tests passing with zero regressions across the codebase.

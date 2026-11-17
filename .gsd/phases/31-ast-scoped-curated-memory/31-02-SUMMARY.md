# Plan 31.02: JIT AST-Scoped Memory Injection into Tool Observations — Summary

**Execution Date:** 2026-11-14  
**Status:** Complete  
**Verification:** All 8 tests in `tests/cognition/test_ast_scoped_memory.py` passing, all 52 existing tests in `tests/cognition/test_curated_memory.py` and `tests/core/test_react_loop.py` green (60 total tests passed).

---

## Accomplishments

### Task 1: Node-Bound AST Memory Model & Graphify 1-Hop Traversal
- Implemented `ASTNodeMemory` dataclass with `id`, `symbol_name`, `rel_path`, `note`, `category` (`"gotcha"` | `"invariant"` | `"post_mortem"`), `code_fingerprint` (SHA-256), `created_at`, and `updated_at`.
- Implemented `compute_code_fingerprint` and `is_fingerprint_stale` supporting both full-file and symbol definition snippet hashing.
- Implemented `ASTNodeMemoryStore` with atomic locked JSON persistence (`.hex/memories/ast_memories.json`), CRUD operations (`record_node_memory`, `get_node_memories`, `list_all_node_memories`, `delete_node_memory`), and staleness detection (`is_memory_stale`).
- Implemented `query_jit_memories_for_file` integrating `ActiveGraphifyAdapter` symbol indexing, 1-hop caller lookup (`callers_of`), 1-hop callee lookup (`SymbolDef.calls`), staleness filtering (`include_stale`), and `GuardrailsManager` token-matching rules.
- Implemented `format_jit_memories_block` rendering surgical markdown blocks:
  ```markdown
  [AST Memory & Invariants for <rel_path>]
  - Gotcha (<symbol>): <note> (SHA: <sha[:8]>)
  - 1-Hop Caller Warning (<caller_symbol> -> <symbol>): <note>
  - Guardrail: <relevant [AVOID] rule>
  ```
- Committed as `feat(phase-31): node-bound AST memory model and 1-hop traversal` (`1a98a35`).

### Task 2: JIT Observation Injection & KV-Cache Immutability Verification
- Enhanced `ToolGateway._tool_read_file` in `harness_core/gateway.py` to resolve AST symbols and query JIT memories/guardrails for the target file, dynamically appending the formatted AST memory block to `res["content"]` and populating `res["ast_memories"]`. Clean un-annotated files return unmodified content without overhead.
- Enhanced `ReActLoop._execute_tool_call` in `harness_core/react_loop.py` with strict KV-cache prefix freeze guards: captures pre-execution `system_prompt` and initial `messages[0]` system instruction, asserting 100% byte-identity in `finally:` blocks.
- Added comprehensive unit and integration tests in `tests/cognition/test_ast_scoped_memory.py` covering:
  - AST memory CRUD, persistence, and querying by symbol and path.
  - 1-hop caller and callee invariant resolution.
  - Staleness detection and automatic omission on code mutation.
  - `read_file` observation injection with file content + gotcha block.
  - Un-annotated file pass-through without extra keys or overhead.
  - Multi-turn (5 turns) KV-cache prompt stability asserting byte-for-byte SHA-256 equivalence before Turn 1 and after Turn 5, with gotchas delivered strictly within tool observation messages.
- Committed as `feat(phase-31): JIT observation injection and KV-cache immutability` (`60d2684`).

---

## Empirical Verification

1. `tests/cognition/test_ast_scoped_memory.py`:
   ```
   ============================== 8 passed in 1.74s ===============================
   ```
2. `tests/cognition/test_curated_memory.py` + `tests/core/test_react_loop.py`:
   ```
   ============================== 52 passed in 2.32s ==============================
   ```
3. Full Combined Suite (60 tests):
   ```
   ============================== 60 passed in 3.26s ==============================
   ```

---

## Commits Landed

- `1a98a35`: `feat(phase-31): node-bound AST memory model and 1-hop traversal`
- `60d2684`: `feat(phase-31): JIT observation injection and KV-cache immutability`

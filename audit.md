# HEX Cognition, Memory, and Skill-System Audit

**Audit date:** 2026-11-15  
**Scope:** `docs/HERMES_LEARNING_AND_SOUL_ANALYSIS.md` and the implementation paths for the Soul/Persona engine, curated and AST-scoped memory, self-learning/skill synthesis, verification hooks, and autonomous reflection.  
**Result:** No application source was changed by this audit.

## Verification performed

The focused implementation suite passed:

```text
103 passed in 9.92s
```

Command:

```sh
.venv/bin/python -m pytest -q \
  tests/test_soul_manager.py \
  tests/test_prompt_builder.py \
  tests/test_curated_memory.py \
  tests/test_ast_scoped_memory.py \
  tests/test_amem_and_lifecycle.py \
  tests/test_skill_synthesis.py \
  tests/test_autonomous_learning.py
```

`python -m compileall -q src` and `git diff --check` also passed. The test suite demonstrates that the individual components work under their intended unit/integration test setup. Several of the findings below concern missing production wiring; those do not necessarily fail the isolated tests.

## Executive assessment

HEX has materially stronger primitives than the Hermes architecture described in the design document:

- multi-tier soul resolution, prompt-slot assembly, and injection heuristics;
- bounded, human-readable memories with locking and history snapshots;
- AST-aware retrieval, guardrail capture, memory lifecycle classes, and FTS archival;
- a security scanner and a structured `verify:` hook engine; and
- friction detection plus hardware-aware reflection policy.

The central risk is **feature completeness at the execution boundary**. Several headline capabilities are implemented as reusable classes and tested in isolation, but are not invoked by the normal task, skill, or daemon paths. Until those edges are connected, HEX should not claim automatic verification, automatic memory promotion/demotion, or fully automatic idle reflection.

## Finding summary

| ID | Severity | Area | Finding |
|---|---|---|---|
| F-01 | High | Skill verification | `verify:` hooks are not called from production skill execution and run unsandboxed when called directly. |
| F-02 | High | Memory lifecycle | Promotion and demotion have no production scheduler/caller; demotion can delete memory after archive failure. |
| F-03 | High | AST memory | Default symbol memories fingerprint the entire file, not the symbol; storage is JSON rather than the described SQLite/Graphify index. |
| F-04 | High | KV-cache snapshots | Frozen memory is process/workspace cached rather than session scoped, so a fresh session can receive stale Turn-0 memory. |
| F-05 | Medium | Memory mutation | Claimed atomic batch operations can partially commit; deletion can report success despite a failed disk mutation. |
| F-06 | Medium | Autonomous learning | `auto_reflect: idle` is always invoked as non-idle, and pending reflections have no consumer. |
| F-07 | Medium | Memory security | JIT notes are concatenated into a tool observation without untrusted-data fencing or write-time injection validation. |
| F-08 | Low | Soul engine | The soul cap is character-based and exceeds its own cap after suffixing; workspace souls are treated more strictly than the stated local-user policy. |

---

## F-01: Verification hooks are not automatic and are unsafe when invoked directly

**Severity:** High  
**Design promise affected:** Deterministic verification hooks attached to learned skills.

### Evidence

- Hook parsing and execution live in `src/harness_core/skills/executor.py`.
- `rg` found `run_skill_verification()` and `assert_skill_regression_free()` referenced only in their unit tests and package export, not from a task completion, skill router, tool gateway, or orchestrator completion path.
- The command executes with `shell=True` and starts from a copy of the complete parent environment:

```python
# src/harness_core/skills/executor.py, lines 199-220
env = os.environ.copy()
...
proc = subprocess.run(
    cmd,
    shell=True,
    cwd=str(ws),
    env=env,
    capture_output=True,
    text=True,
    timeout=hook.timeout_seconds,
)
```

### Impact

The implementation currently provides an opt-in library function, not the claimed automatic regression gate. A successful skill-assisted change can be marked complete without executing its `verify:` assertion.

If a caller invokes the executor on a skill that bypassed SkillSpector registration, a frontmatter command can access inherited credentials, the user environment, and the filesystem available to the process. `cwd` is not a sandbox boundary.

### How to fix

1. Add a single post-edit verification integration point in the task/skill completion flow.
2. Resolve the skill through its registered record and reject execution unless its current bundle hash matches the hash approved by SkillSpector.
3. Run the hook through the existing workspace sandbox rather than `subprocess.run(..., shell=True)`.
4. Use a minimal allowlisted environment. Do not inherit provider keys, cloud credentials, SSH configuration, or arbitrary `PATH` entries.
5. Treat `must_pass: true` as a task transition gate: `VERIFYING` -> `FAILED` on failure; preserve stdout/stderr as an artifact.
6. Add an end-to-end test that starts a skill-bearing task, edits a file, and proves that a failing `verify:` blocks `SUCCEEDED`.

Suggested ownership: `orchestrator.py` owns state transition; `gateway.py`/sandbox owns command execution; `skillspector.py` owns bundle identity validation.

---

## F-02: Memory lifecycle is passive, and archive failure can lose active memory

**Severity:** High  
**Design promise affected:** Confidence decay and automatic promotion/demotion.

### Evidence

- `MemoryLifecycleManager.check_promotions()` is defined in `src/harness_core/memory.py` but has no production caller.
- `MemoryLifecycleManager.run_demotion_sweep()` is also only found in tests.
- In `run_demotion_sweep()`, deletion proceeds whether `archive_entry()` succeeds or fails:

```python
# src/harness_core/memory.py, lines 2222-2232
self.archive_entry(...)
self.controller.delete_memory(item.id, target=item.scope)
```

### Impact

The documented lifecycle is manual/test-only in practice:

- reflected lessons do not automatically promote after three cross-session references;
- unreferenced or stale notes do not automatically demote; and
- a locked/corrupt/unavailable archive database can cause a note to be removed from active memory without a cold-copy guarantee.

### How to fix

1. Add a lifecycle job to the daemon or a controlled session-end maintenance phase.
2. Make its schedule explicit and observable: for example, promotion check at successful session close; demotion daily or on workspace open.
3. Capture the boolean result of `archive_entry()`. Only delete active content after it returns `True` and the archival transaction has committed.
4. On archival failure, retain active memory and record a retryable maintenance event in the ledger.
5. Add an idempotency key/unique archive constraint so a retry cannot create duplicated FTS rows.
6. Add tests for archive failure, daemon scheduling, and restart-safe promotion/demotion.

---

## F-03: “AST-scoped” staleness is file-scoped by default

**Severity:** High  
**Design promise affected:** JIT graph retrieval and AST hash based memory decay.

### Evidence

`ASTNodeMemoryStore.record_node_memory()` has no Graphify adapter parameter. Its default fingerprint call therefore passes `graphify_adapter=None`:

```python
# src/harness_core/memory.py, lines 1062-1075
if code_fingerprint is None:
    code_fingerprint = compute_code_fingerprint(
        self.workspace_root, rel_path, symbol_name
    )
```

`compute_code_fingerprint()` hashes only a symbol snippet when a Graphify adapter is supplied; otherwise it hashes the full file. The resulting memory is marked stale after any unrelated edit in that file.

The AST-node store is also persisted as `.hex/memories/ast_memories.json`, while the design calls for SQLite/Graphify-backed node-bound memory and FTS retrieval.

### Impact

A valuable note attached to `authenticate()` disappears from normal recall if a docstring or unrelated function in the same module changes. This creates false staleness and makes the feature noisier/less reliable exactly in large frequently edited modules.

The JSON store also prevents efficient indexed lookup, robust concurrent writes, and the described relationship between graph traversal and memory retrieval.

### How to fix

1. Require a Graphify symbol reference when creating a node-bound memory: stable qualified name, relative path, language, and definition span.
2. Compute and persist a hash of the resolved symbol body, not the file.
3. During retrieval, refresh the Graphify adapter before resolving the target file; do not merely refresh when its maps are empty.
4. Migrate node-memory records to SQLite with indexes on `(workspace, rel_path, qualified_symbol)` and FTS over notes/tags.
5. Preserve the human-readable Markdown summary, but treat SQLite as the indexed source of truth.
6. Add a regression test: change an unrelated function and confirm the target symbol memory remains fresh; then change the target symbol and confirm it becomes stale.

---

## F-04: “Turn-0” snapshots are process-global instead of session-local

**Severity:** High  
**Design promise affected:** Frozen KV-cache prefix with new-session hydration.

### Evidence

`MemorySnapshot` keeps a class-level cache keyed only by workspace/global-home:

```python
# src/harness_core/memory.py, lines 743-760
_cached_snapshots: Dict[str, str] = {}
...
cache_key = f"{ws}:{gh}"
if cache_key in cls._cached_snapshots:
    return cls._cached_snapshots[cache_key]
```

`PromptBuilder.build_system_prompt()` obtains this shared cache without a session identifier. There is no production new-session call to `MemorySnapshot.reset_cache()`; the only non-test reset observed is memory-history rollback.

### Impact

The current implementation does preserve prefix bytes during one process lifetime, but it can preserve them too long. A newly opened task/session in the same process can read a stale snapshot after `USER.md`, `MEMORY.md`, or `GUARDRAILS.md` changes.

That differs from the desired behavior: immutable within a session, refreshed at an intentional session boundary.

### How to fix

1. Move frozen memory from the class-level global cache to a session context object.
2. Build it exactly once when creating a ReAct/task session and pass the frozen value into `PromptBuilder` for that session.
3. Add a deliberate `/new` or session-rotation action that creates a new snapshot; never mutate the existing one.
4. If multiple sessions share a process, keep independent snapshots keyed by opaque `session_id`, not workspace path.
5. Add an integration test with two sessions in one process: mutate memory between sessions and assert only session two sees the change.

---

## F-05: Memory mutation transactions can partially commit

**Severity:** Medium  
**Design promise affected:** A-MEM CRUD correctness and transactional curation.

### Evidence

`CuratedMemoryStore.execute_operations()` claims atomicity, but calls `add`, `replace`, `patch`, and `remove` one at a time. Each operation independently writes to disk. A later failure returns an error without rolling earlier changes back.

```python
# src/harness_core/memory.py, lines 693-731
for op in operations:
    ...
    res = self.add(...)  # each call can commit independently
    ...
    if not res.get("ok"):
        return {"ok": False, ...}
```

`AMEMController.delete_memory()` also ignores `self.store.remove()`'s result before deleting its in-memory metadata and reporting success (`memory.py`, lines 1846-1862). The same unchecked remove pattern appears in the automatic `DELETE` path.

### Impact

An LLM-generated multi-operation contradiction-resolution plan can leave memory half-updated: for example, an obsolete rule removed but its replacement rejected for exceeding the character budget. In a concurrent or drift scenario, metadata and Markdown can diverge.

### How to fix

1. Acquire all target locks in a stable order.
2. Load target files, apply every operation to in-memory copies, and validate all budgets/matches before writing anything.
3. Write every affected file atomically only after validation succeeds. If multi-file atomicity is not feasible, journal a transaction and rollback already-written files on failure.
4. Propagate failed `remove()` results and do not mutate metadata unless physical persistence succeeds.
5. Add a test where operation one is valid and operation two fails; assert no physical or metadata change survives.

---

## F-06: Idle auto-reflection is permanently deferred

**Severity:** Medium  
**Design promise affected:** Hardware/RAM-protected autonomous reflection.

### Evidence

`should_execute_background_reflection()` correctly understands `idle` policy, but the production caller passes `is_idle=False` unconditionally:

```python
# src/harness_core/orchestrator.py, lines 426-431
allowed, reason = should_execute_background_reflection(
    policy=policy,
    router=getattr(self, "router", None),
    is_idle=False,
)
```

Suppressed candidates are appended to `.hex/reflections/pending_reflections.json`, but no production code consumes that file.

### Impact

For `learning.auto_reflect: idle`, every reflection is queued forever. For local hardware this is safe, but it is not an autonomous learning loop.

### How to fix

1. Define what “idle” means: no running task, low system load, no active local inference, and optionally an operator-configured delay.
2. Have the daemon/worker call the reflection policy with real idle state.
3. Drain `pending_reflections.json` transactionally, marking each item `running`, then `synthesized` or retryable `failed`.
4. Deduplicate by task ID and impose retention/size limits on pending candidates.
5. Add an integration test that creates a pending record, transitions to idle, and verifies one lesson/guardrail is produced.

---

## F-07: JIT memory recall lacks an untrusted-data boundary

**Severity:** Medium  
**Design promise affected:** Safe observation-level AST memory injection.

### Evidence

The read-file handler appends formatted notes directly to source content:

```python
# src/harness_core/gateway.py, lines 1226-1229
memories = query_jit_memories_for_file(...)
jit_block = format_jit_memories_block(memories, rel_p)
injected_content = f"{content}\n\n{jit_block}"
```

`format_jit_memories_block()` formats stored text directly. Unlike `GovernedMemoryService.retrieve_memories()`, it does not use a quoted evidence wrapper, provenance/confidence attributes, or a clear “not instructions” label.

### Impact

Any poisoned or malformed learned note becomes prominent model-visible text immediately before an edit. It can attempt to override tool usage, request secret handling, or undermine the agent’s normal task constraints. This matters particularly because reflection output and user-authored memory may be persisted and later recalled.

### How to fix

1. Store provenance, author/source, confidence, and safety-review status on JIT node memory.
2. Scan proposed note content on write using the prompt-injection heuristics; quarantine or require approval for suspicious content.
3. Render recall in a rigid data fence, for example `<retrieved-memory trusted="false">`, with explicit language that it is reference data and cannot change policy or tool authority.
4. Cap total injected bytes and individual note bytes; deduplicate and prioritize direct fresh symbol notes over broad guardrails.
5. Add adversarial tests with phrases such as “ignore previous instructions” in a stored note and assert it is blocked, escaped, or displayed as quarantined data.

---

## F-08: Soul budget and trust-tier behavior do not fully match the specification

**Severity:** Low  
**Design promise affected:** 300–500-token soul discipline and local-user trust tier.

### Evidence

The soul cap is 2,500 characters, then the truncation suffix is appended:

```python
# src/harness_core/soul.py, lines 160-170
return content[:MAX_SOUL_CHARS].rstrip() + "\n... [truncated to fit 500-token budget]"
```

This can exceed the cap and does not reliably represent 500 tokens across languages/models.

In `PromptBuilder`, a workspace soul is considered non-user-authored:

```python
# src/harness_core/prompt_builder.py, lines 143-145
user_authored = not is_distribution_profile and source_scope in ("global", "default")
```

It will therefore be hard-blocked on a heuristic match, whereas the architecture text describes local user-authored souls as warning-only.

### Impact

The budget mismatch is small but weakens an explicit local-model constraint. The trust-tier difference is either a documentation mismatch or an intentional security hardening decision; it should be made explicit rather than ambiguous.

### How to fix

1. Reserve space for a truncation marker, or use a model-aware token counter with a conservative character fallback.
2. Define an explicit source provenance enum: `global_user`, `workspace_user`, `repository`, `remote_profile`, and `distribution`.
3. Apply warning/block policy from provenance, not from the path alone.
4. Update the architecture document to reflect the chosen posture. Treating workspace files as untrusted is defensible, but should be stated.

---

## Strengths to preserve

Do not lose these while fixing the production seams:

- `PromptBuilder` maintains a clear, testable slot order for soul, mode, environment, memory, and project rules.
- Curated memory writes use lock files and atomic replacement, with history snapshots.
- Guardrails capture repeated failed tool calls and rejected/rolled-back diffs.
- SkillSpector has deterministic risk gates and quarantine behavior.
- Reflection synthesis has a deterministic fallback, which keeps the learning path operational without a cloud model.
- The focused test suite covers a broad set of happy paths and important negative cases.

## Recommended implementation order

1. **Wire verification hooks into the task state machine**, through the sandbox and registered SkillSpector hash.
2. **Make memory lifecycle durable and scheduled**, including archive-before-delete semantics.
3. **Correct AST memory identity/fingerprints**, then migrate node memory to SQLite/FTS.
4. **Make snapshots session-owned**, preserving KV-cache stability without cross-session staleness.
5. **Make A-MEM batches transactional** and harden deletion error handling.
6. **Implement the pending-reflection worker** for actual idle execution.
7. **Fence and validate JIT memory** before it reaches model-visible observations.
8. Resolve soul provenance/budget documentation and add end-to-end regression tests for every completed wiring path.

## Definition of done for “superior to Hermes”

The system can credibly claim the specified advantages once the following are true in production, not only in isolated tests:

- Every approved learned skill with `verify:` is automatically checked after its associated change; a failed required check prevents task success.
- Symbol memories survive unrelated file edits and become stale only when their bound symbol/semantic identity changes.
- New sessions hydrate new Turn-0 memory while active sessions keep a byte-stable prefix.
- Promotion, demotion, and idle reflection execute through a durable scheduler and never lose content on a failed archive.
- Retrieved memory is provenance-tagged reference data that cannot silently become executable instructions.
- Multi-step memory curation either commits entirely or has no visible effect.

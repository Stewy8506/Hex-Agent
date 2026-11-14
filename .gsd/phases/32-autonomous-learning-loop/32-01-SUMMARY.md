# Phase 32 Plan 01: Explicit `/learn` Skill Compiler with Deterministic `verify:` Hooks — Summary

**Execution Date:** 2026-11-14 / 2026-11-15  
**Milestone:** M11 (Next-Gen Cognition & Self-Evolution Engine)  
**Phase:** 32 (Closed-Loop Autonomous Learning Loop & Deterministic Skill Hooks)  
**Plan:** `32-01`  
**Status:** COMPLETE & VERIFIED  

---

## 1. Executive Summary

Implemented the explicit `/learn` skill compilation engine (`SkillCompiler`) and deterministic verification hook execution engine (`SkillExecutionEngine`), turning documentation and solved procedures into standardized, verified `SKILL.md` bundles with regression assertions, gated by NVIDIA SkillSpector AST safety checks.

### Key Architectural Deliverables:
1. **Hermes Hardline Authoring Invariants Enforced**:
   - `name`: Lowercase-hyphenated, strictly `<= 64` characters, spaces and non-alphanumeric converted.
   - `description`: Single sentence, strictly `<= 60` characters ending with a period (`.`), marketing buzzwords (*powerful, comprehensive, seamless, advanced, robust*) stripped.
   - `author`: Locked to literal `"HEX"` (never host username or git config).
   - `version`: Defaulting to `"0.1.0"`.
   - Standard 8-section Hermes structure (`# Title`, `## When to Use`, `## Prerequisites`, `## How to Run`, `## Quick Reference`, `## Procedure`, `## Pitfalls`, `## Verification`).
   - Knowledge-base skill layout support: Lean `SKILL.md` index + auxiliary `references/` docs and `scripts/` (executable with `0o755` permissions).
2. **Deterministic `verify:` YAML Frontmatter Hooks**:
   - Concise string command format (`verify: pytest tests/test_rpc.py`) or rich structured dictionary (`command`, `timeout_seconds`, `must_pass`, `ast_invariant`, `test_filter`, `env_vars`).
   - Zero-dependency YAML frontmatter parser and scalar unescaper in `parse_skill_frontmatter`.
3. **Mandatory NVIDIA SkillSpector Safety Gate**:
   - Candidate bundles are scanned in isolated staging directories via `SkillSpectorScanner.scan_bundle()` prior to filesystem persistence.
   - High-risk or malicious bundles (`gate_decision == "QUARANTINE"`) are rejected, quarantined to `.hex/quarantine/<name>/` with `audit.json`, and raise `SkillSecurityViolationError` (inheriting from `PermissionError`).
4. **Deterministic `SkillExecutionEngine`**:
   - Extracts and normalizes `verify:` hooks.
   - Executes commands inside the workspace root with configurable subprocess timeout protection and environment overrides.
   - Validates AST invariants (e.g. `test_rpc_schema_sync in tests/test_rpc.py`) via Python AST traversal.
   - Provides `assert_skill_regression_free` helper for orchestrator and regression verification pipelines.

---

## 2. Tasks Executed & Git Commits

| Task | Description | Commits | Status |
|------|-------------|---------|--------|
| **Task 1** | SkillCompiler with Hermes Invariants, `verify:` Frontmatter & SkillSpector Gate | `68921ed` | Verified |
| **Task 2** | Deterministic `verify:` Execution Hook Engine & Test Suite | `44ca952` | Verified |

### Commit Confirmations:
- `68921ed`: `feat(phase-32): implement SkillCompiler with verify hooks and SkillSpector gate`
- `44ca952`: `feat(phase-32): implement SkillExecutionEngine and skill synthesis test suite`

---

## 3. Empirical Verification Evidence

### Test Suite Execution:
```bash
$ .venv/bin/pytest tests/test_skill_synthesis.py -v
============================== 30 passed in 3.36s ==============================
```

#### Breakdown of Passed Verification Tests:
- `TestSkillCompilerUnit::test_compiler_hermes_invariants`: PASSED
- `TestSkillCompilerUnit::test_compiler_knowledge_base_references_and_scripts`: PASSED
- `TestSkillCompilerUnit::test_compiler_global_vs_project_scopes`: PASSED
- `TestSkillCompilerUnit::test_compiler_dry_run_mode`: PASSED
- `TestSkillFrontmatterVerifyParsing::test_frontmatter_nested_verify_block`: PASSED
- `TestSkillFrontmatterVerifyParsing::test_frontmatter_concise_string_verify`: PASSED
- `TestSkillCompilerSkillspectorGate::test_skillspector_gate_clean_allow`: PASSED
- `TestSkillCompilerSkillspectorGate::test_skillspector_gate_quarantine_malicious_script`: PASSED
- `TestSkillExecutionEngineVerifyHooks::test_executor_extract_verification_hook`: PASSED
- `TestSkillExecutionEngineVerifyHooks::test_executor_run_passing_verification_hook`: PASSED
- `TestSkillExecutionEngineVerifyHooks::test_executor_run_failing_verification_hook`: PASSED
- `TestSkillExecutionEngineVerifyHooks::test_executor_timeout_protection`: PASSED
- `TestSkillExecutionEngineVerifyHooks::test_executor_ast_invariant_pass_and_fail`: PASSED
- `TestSkillExecutionEngineVerifyHooks::test_executor_assert_skill_regression_free`: PASSED
- Existing 16 tests in `test_skill_synthesis.py`: PASSED

### Regression Checks:
```bash
$ .venv/bin/pytest tests/test_skillspector_ast_wiring.py tests/test_skillspector_osv_wiring.py -v
============================== 10 passed in 5.77s ==============================
```

---

## 4. Truths & Must-Haves Delivered

| Must-Have | Status | Evidence |
|-----------|--------|----------|
| SkillCompiler synthesizes SKILL.md bundles adhering to Hermes hardline authoring standards (name <= 64 chars, description <= 60 chars, Hermes author, standardized sections). | Delivered | Tested in `test_compiler_hermes_invariants` |
| Skills can be saved to project scope (`.hex/skills/<name>/SKILL.md`) or machine-wide global scope (`~/.hex/skills/<name>/SKILL.md`). | Delivered | Tested in `test_compiler_global_vs_project_scopes` |
| Frontmatter parser and compiler support optional `verify:` hooks with executable commands, timeout_seconds, and must_pass assertions. | Delivered | Tested in `test_frontmatter_nested_verify_block` and `test_frontmatter_concise_string_verify` |
| Every synthesized skill bundle is validated by SkillSpector AST and security gate before saving to disk; high-risk or malicious code is quarantined/rejected. | Delivered | Tested in `test_skillspector_gate_clean_allow` and `test_skillspector_gate_quarantine_malicious_script` |
| Skill execution engine executes the deterministic `verify:` hook post-execution, turning skill documentation into an automated regression assertion. | Delivered | Tested in `test_executor_run_passing_verification_hook`, `test_executor_timeout_protection`, `test_executor_ast_invariant_pass_and_fail`, and `test_executor_assert_skill_regression_free` |

---

## 5. Artifacts Produced
- `src/harness_core/skills/__init__.py`
- `src/harness_core/skills/compiler.py`
- `src/harness_core/skills/executor.py`
- `src/harness_core/skillspector.py` (updated frontmatter parsing and OSV checking)
- `tests/test_skill_synthesis.py` (30 test cases)

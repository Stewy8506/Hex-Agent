# Phase 32 Plan 03 Summary: Cross-Session FTS5 + Graphify Hybrid Retrieval & CLI/TUI Ergonomics

**Execution Date:** 2026-11-14  
**Status:** Completed & Empirically Verified (55/55 tests passing)  
**Commits:**
- `13847ca`: `feat(phase-32): implement HybridLearningRetriever with FTS5 and Graphify AST ranking` (Task 1)
- `dc1bfc8`: `feat(phase-32): implement CLI hex learn, TUI /learn, and integration test coverage` (Task 2)

---

## 1. Objectives & Must-Haves Delivered

| Must-Have Requirement | Status | Evidence / Verification |
|---|---|---|
| **HybridLearningRetriever** combines SQLite FTS5 BM25 ranking with Active Graphify AST 1-hop caller/callee traversal | **Delivered** | `src/harness_core/skills/retrieval.py`, `tests/test_autonomous_learning.py::test_hybrid_retrieval_lexical_and_ast_ranking` |
| Querying learned skills and memories surfaces both lexical keyword matches and connected AST caller/callee gotchas | **Delivered** | `HybridLearningRetriever.query()` fuses BM25 lexical scores with structural AST proximity boost (`2.5` direct, `1.8` 1-hop caller/callee) |
| CLI command `hex learn <target>` compiles skills with `--name`, `--global`, `--verify`, `--dry-run`, and `--json` flags | **Delivered** | `src/harness_core/cli.py`, 4 unit tests in `tests/test_skill_synthesis.py` |
| TUI slash command `/learn <target>` provides interactive skill preview, dry-run toggle, and status indicators in HexConsole | **Delivered** | `src/harness_core/tui.py`, `tests/test_autonomous_learning.py::test_tui_learn_slash_command` |
| `/skills` command displays `[VERIFY: <cmd>]` tag when a skill carries a `verify:` hook | **Delivered** | `src/harness_core/tui.py::_cmd_skills()`, `tests/test_autonomous_learning.py::test_tui_skills_display_with_verify_tag` |
| Complete test suites in `tests/test_skill_synthesis.py` and `tests/test_autonomous_learning.py` pass 100% cleanly | **Delivered** | 55/55 tests green (0 regressions across 800+ repo tests) |

---

## 2. Key Architecture & Implementation Details

1. **Hybrid Cross-Session Retrieval (`src/harness_core/skills/retrieval.py`):**
   - **`RetrievalMatch` Dataclass**: Exposes `item_type` (`"skill" | "lesson" | "memory"`), `identifier`, `title`, `content`, `score`, `match_type` (`"lexical_fts5" | "ast_1hop" | "hybrid"`), `source_file`, and `associated_symbols`.
   - **`HybridLearningRetriever`**:
     - *Step 1 (Lexical FTS5 Retrieval)*: Scans and indexes workspace skills (`.hex/skills/`, `skills/`), global skills (`~/.hex/skills/`), reflections (`.hex/reflections/LESSONS.md`), active markdown memories (`MEMORY.md`, `GUARDRAILS.md`, `USER.md`), and cold archived memories (`archive.db`) into an in-memory SQLite FTS5 table with BM25 ranking (`unicode61` tokenizer).
     - *Step 2 (AST 1-Hop Traversal)*: Given a target symbol, file path, or tokens extracted from `query_text`, queries `ActiveGraphifyAdapter` for defining files, symbol definitions, 1-hop callers (`callers_of`), and 1-hop callees (`SymbolDef.calls`). Discovers all skills, lessons, or memories citing any node in this 1-hop neighborhood.
     - *Step 3 (Score Fusion & Re-Ranking)*: Merges BM25 lexical scores with AST structural proximity boosts (`2.5` direct, `1.8` 1-hop caller/callee), assigning `match_type="hybrid"` when an item satisfies both, and sorts top-K candidates.
     - `find_skills_for_task(goal, target_files)`: Specialized query for task intake and autonomous replanning.

2. **CLI Ergonomics (`src/harness_core/cli.py`):**
   - Implemented `hex learn <target>` with arguments:
     - `target`: File, directory, past task ID, or quoted description.
     - `--name`: Validated lowercase-hyphenated skill name.
     - `--global`: Persists to `~/.hex/skills/` instead of project `.hex/skills/`.
     - `--verify`: Shell command for regression verification assertion hook.
     - `--dry-run`: Simulates compilation, frontmatter parsing, and SkillSpector scan without disk modification.
     - `--json`: Structured JSON emission of compilation result and scan report.
   - Outputs colorized formatted card showing name, description, scope, disk location, verification command, and SkillSpector gate decision.

3. **TUI Ergonomics (`src/harness_core/tui.py`):**
   - Added `/learn [target] [--global] [--dry-run]` slash command to `HexConsole`. If no target is passed, synthesizes from recent dialogue history.
   - Updated `_cmd_help` to document `/learn <target>`.
   - Updated `_cmd_skills` to dynamically inspect `verify` hooks and display `[VERIFY: <cmd>]` tag alongside SkillSpector risk badge.

---

## 3. Empirical Verification Results

```bash
.venv/bin/pytest tests/test_skill_synthesis.py tests/test_autonomous_learning.py -v
```
Output:
```
============================= test session starts ==============================
collected 55 items

tests/test_skill_synthesis.py::TestSkillSynthesizerUnit::test_determine_risk_class PASSED
tests/test_skill_synthesis.py::TestSkillSynthesizerUnit::test_extract_task_context_completed_task PASSED
tests/test_skill_synthesis.py::TestSkillSynthesizerUnit::test_extract_task_context_nonexistent_task PASSED
tests/test_skill_synthesis.py::TestSkillSynthesizerUnit::test_extract_task_context_not_completed PASSED
tests/test_skill_synthesis.py::TestSkillSynthesizerUnit::test_generate_skill_markdown PASSED
tests/test_skill_synthesis.py::TestSkillSynthesizerUnit::test_slugify_helper PASSED
tests/test_skill_synthesis.py::TestSkillSynthesizerGating::test_adversarial_prompt_injection_quarantined PASSED
tests/test_skill_synthesis.py::TestSkillSynthesizerGating::test_clean_skill_synthesis PASSED
tests/test_skill_synthesis.py::TestSkillSynthesizerGating::test_clean_skill_synthesis_dry_run PASSED
tests/test_skill_synthesis.py::TestSkillSynthesizerGating::test_shell_exfiltration_pattern_quarantined PASSED
tests/test_skill_synthesis.py::TestSkillListing::test_list_skills_enumeration PASSED
tests/test_skill_synthesis.py::TestSkillCLI::test_cli_learn_dry_run_json PASSED
tests/test_skill_synthesis.py::TestSkillCLI::test_cli_learn_formatted_output PASSED
tests/test_skill_synthesis.py::TestSkillCLI::test_cli_learn_security_quarantine PASSED
tests/test_skill_synthesis.py::TestSkillCLI::test_cli_learn_with_verify_hook PASSED
tests/test_skill_synthesis.py::TestSkillCLI::test_cli_skill_list PASSED
tests/test_skill_synthesis.py::TestSkillCLI::test_cli_skill_scan_bundle PASSED
tests/test_skill_synthesis.py::TestSkillCLI::test_cli_skill_synthesize_dry_run PASSED
tests/test_skill_synthesis.py::TestSkillCLI::test_cli_skill_synthesize_missing_task_error PASSED
tests/test_skill_synthesis.py::TestSkillCLI::test_cli_skill_synthesize_persistence PASSED
tests/test_skill_synthesis.py::TestSkillCompilerUnit::test_compiler_dry_run_mode PASSED
tests/test_skill_synthesis.py::TestSkillCompilerUnit::test_compiler_global_vs_project_scopes PASSED
tests/test_skill_synthesis.py::TestSkillCompilerUnit::test_compiler_hermes_invariants PASSED
tests/test_skill_synthesis.py::TestSkillCompilerUnit::test_compiler_knowledge_base_references_and_scripts PASSED
tests/test_skill_synthesis.py::TestSkillFrontmatterVerifyParsing::test_frontmatter_concise_string_verify PASSED
tests/test_skill_synthesis.py::TestSkillFrontmatterVerifyParsing::test_frontmatter_nested_verify_block PASSED
tests/test_skill_synthesis.py::TestSkillCompilerSkillspectorGate::test_skillspector_gate_clean_allow PASSED
tests/test_skill_synthesis.py::TestSkillCompilerSkillspectorGate::test_skillspector_gate_quarantine_malicious_script PASSED
tests/test_skill_synthesis.py::TestSkillExecutionEngineVerifyHooks::test_executor_assert_skill_regression_free PASSED
tests/test_skill_synthesis.py::TestSkillExecutionEngineVerifyHooks::test_executor_ast_invariant_pass_and_fail PASSED
tests/test_skill_synthesis.py::TestSkillExecutionEngineVerifyHooks::test_executor_extract_verification_hook PASSED
tests/test_skill_synthesis.py::TestSkillExecutionEngineVerifyHooks::test_executor_run_failing_verification_hook PASSED
tests/test_skill_synthesis.py::TestSkillExecutionEngineVerifyHooks::test_executor_run_passing_verification_hook PASSED
tests/test_skill_synthesis.py::TestSkillExecutionEngineVerifyHooks::test_executor_timeout_protection PASSED
tests/test_autonomous_learning.py::test_hardware_gating_policy_parsing PASSED
tests/test_autonomous_learning.py::test_hardware_gating_decision_off PASSED
tests/test_autonomous_learning.py::test_hardware_gating_decision_idle PASSED
tests/test_autonomous_learning.py::test_hardware_gating_decision_cloud_only_local PASSED
tests/test_autonomous_learning.py::test_hardware_gating_decision_cloud_only_cloud PASSED
tests/test_autonomous_learning.py::test_friction_sensor_retry_count PASSED
tests/test_autonomous_learning.py::test_friction_sensor_repeat_tool_failure PASSED
tests/test_autonomous_learning.py::test_friction_sensor_resolved_tool_failure PASSED
tests/test_autonomous_learning.py::test_friction_sensor_compiler_errors PASSED
tests/test_autonomous_learning.py::test_react_session_result_exposes_friction_info PASSED
tests/test_autonomous_learning.py::test_orchestrator_friction_handling_suppressed PASSED
tests/test_autonomous_learning.py::test_lesson_synthesis_llm PASSED
tests/test_autonomous_learning.py::test_lesson_synthesis_deterministic PASSED
tests/test_autonomous_learning.py::test_append_to_lessons_md PASSED
tests/test_autonomous_learning.py::test_lifecycle_integration_auto_registration PASSED
tests/test_autonomous_learning.py::test_lifecycle_integration_hit_counter_and_promotion PASSED
tests/test_autonomous_learning.py::test_lifecycle_integration_promote_to_skill PASSED
tests/test_autonomous_learning.py::test_hybrid_retrieval_lexical_and_ast_ranking PASSED
tests/test_autonomous_learning.py::test_tui_learn_slash_command PASSED
tests/test_autonomous_learning.py::test_tui_skills_display_with_verify_tag PASSED
tests/test_autonomous_learning.py::test_tui_help_command_lists_learn PASSED

============================== 55 passed in 6.76s ==============================
```

All 55 tests passed cleanly with zero failures.

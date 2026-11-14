# Phase 32 Plan 02: Autonomous Friction Sensor & Hardware-Gated Background Reflection — Summary

**Execution Date:** 2026-11-14 / 2026-11-15  
**Milestone:** M11 (Next-Gen Cognition & Self-Evolution Engine)  
**Phase:** 32 (Closed-Loop Autonomous Learning Loop & Deterministic Skill Hooks)  
**Plan:** `32-02`  
**Status:** COMPLETE & VERIFIED  

---

## 1. Executive Summary

Implemented the autonomous closed-loop friction sensing and hardware-gated reflection subsystem, enabling HEX to detect debugging struggles and retry escalation ($\ge 3$ repair cycles, repeated tool errors, or compiler syntax failures), synthesize structured post-mortem lessons into `.hex/reflections/LESSONS.md`, and safely coordinate with `MemoryLifecycleManager` and `GuardrailsManager`.

To protect Apple Silicon unified memory (e.g. M4 / M-series MacBook Air) and local inference runners (MLX, Ollama, local vLLM) against memory thrashing and background starvation, the engine enforces strict hardware gating (`auto_reflect = "cloud_only" | "idle" | "off"`). When running locally under `cloud_only` or `off`, background LLM reflection calls are bypassed, and candidates are safely queued into `.hex/reflections/pending_reflections.json` for offline processing.

### Key Architectural Deliverables:
1. **Hardware Gating Policy Engine (`config_loader.py`)**:
   - `get_auto_reflect_policy(workspace_root=None) -> str`: Resolves `HEX_AUTO_REFLECT` env var, local workspace `.hex/config.yaml`, global `~/.hex/config.yaml`, defaulting to `"cloud_only"`.
   - `should_execute_background_reflection(policy, router=None, is_idle=False) -> tuple[bool, str]`:
     - If `policy == "off"`: Returns `(False, "Autonomous reflection disabled (policy=off)")`.
     - If `policy == "idle"`: Returns `(True, "Executing during idle")` only if `is_idle`, else `(False, "Deferred until idle")`.
     - If `policy == "cloud_only"`: Inspects router provider and host hardware. Suppresses local inference runners (`mlx`, `ollama`, `local`, `vllm`, Darwin ARM64 unified memory without cloud routing) returning `(False, "Suppressed background reflection on local hardware to prevent memory starvation")`. Authorizes cloud providers (`anthropic`, `openai`, `gemini`, `antigravity`) returning `(True, "Cloud-routed reasoner active")`.
2. **ReActLoop Multi-Step Friction Sensor (`react_loop.py`)**:
   - Tracks `turn_repair_count`, `repair_attempts`, `retry_count`, and `compiler_errors_count`.
   - Detects friction when `retry_count >= 3`, `turn_repair_count >= 3`, `repair_attempts >= 3`, same tool fails $\ge 2$ times before succeeding, or repeated compiler/syntax errors occur.
   - Emits `friction_detected` lifecycle event via `_emit_event`.
   - Attaches structured `friction_info: dict[str, Any]` to `ReActSessionResult`.
3. **Orchestrator Hardware-Gated Reflection Routing (`orchestrator.py`)**:
   - In `_execute_standard_dag` and `run_agent_loop`: When a task succeeds after friction (`repair_attempt >= 3` or `friction_info["friction_detected"]`), evaluates `should_execute_background_reflection`.
   - If permitted: Synthesizes structured lesson and appends to `.hex/reflections/LESSONS.md`.
   - If suppressed (e.g. local M-series hardware under `cloud_only`): Writes candidate to `.hex/reflections/pending_reflections.json` with forensic context, error traces, and suppression reason without stalling active inference.
4. **Structured Reflection Synthesizer & Memory Lifecycle Sync (`reflection.py`, `memory.py`)**:
   - `FrictionLesson`: Dataclass containing `lesson_id`, `task_id`, `symptom`, `root_cause`, `verified_solution`, `avoid_invariant`, `source_nodes`, and `created_at`.
   - `ReflectionSynthesizer`: LLM-guided synthesis when cloud reasoners are active; deterministic structural fallback when offline.
   - `append_to_lessons_md`: Appends standard markdown lesson format to `.hex/reflections/LESSONS.md`.
   - Automatically registers reflection in `MemoryLifecycleManager.record_reflection` and negative guardrail in `GuardrailsManager.record_avoid_invariant` (`GUARDRAILS.md`).
   - Integrated with promotion pipeline: reflections referenced $\ge 3$ times promote to `MEMORY.md`, and reflections with $\ge 5$ hits or targeted for skills compile to reusable `SKILL.md` bundles via `SkillCompiler`.

---

## 2. Tasks Executed & Git Commits

| Task | Description | Commits | Status |
|------|-------------|---------|--------|
| **Task 1** | Autonomous Friction Sensor & Hardware Gating in ReActLoop and Orchestrator | `bc826e9` | Verified |
| **Task 2** | Background Reflection Synthesizer & MemoryLifecycleManager Integration | `47fda84` | Verified |

### Commit Confirmations:
- `bc826e9`: `feat(phase-32): implement friction sensor and hardware-gated reflection in react_loop and orchestrator`
- `47fda84`: `feat(phase-32): implement ReflectionSynthesizer and MemoryLifecycleManager integration`

---

## 3. Empirical Verification Evidence

### Test Suite Execution:
```bash
$ .venv/bin/pytest tests/test_autonomous_learning.py -v
============================== 17 passed in 0.87s ==============================
```

#### Detailed Test Results:
1. `test_hardware_gating_policy_parsing`: PASSED (config file, boolean/off normalization, env var priority)
2. `test_hardware_gating_decision_off`: PASSED (unconditional suppression with explanation)
3. `test_hardware_gating_decision_idle`: PASSED (deferred when active, allowed when idle)
4. `test_hardware_gating_decision_cloud_only_local`: PASSED (suppression of MLX, Ollama, and local providers)
5. `test_hardware_gating_decision_cloud_only_cloud`: PASSED (cloud-routed Anthropic/Gemini active authorization)
6. `test_friction_sensor_retry_count`: PASSED (friction flag and event on `retry_count >= 3`)
7. `test_friction_sensor_repeat_tool_failure`: PASSED (friction flag on same tool failure $\ge 2$ times)
8. `test_friction_sensor_resolved_tool_failure`: PASSED (friction flag when tool with repeat failures succeeds)
9. `test_friction_sensor_compiler_errors`: PASSED (friction flag on repeated syntax/compiler errors)
10. `test_react_session_result_exposes_friction_info`: PASSED (session result returns full `friction_info` dict)
11. `test_orchestrator_friction_handling_suppressed`: PASSED (candidate logged to `.hex/reflections/pending_reflections.json`)
12. `test_lesson_synthesis_llm`: PASSED (structured lesson parsed from LLM JSON response)
13. `test_lesson_synthesis_deterministic`: PASSED (fallback symptom, cause, solution, and `[AVOID]` extraction)
14. `test_append_to_lessons_md`: PASSED (standard markdown formatting and title preserved in `LESSONS.md`)
15. `test_lifecycle_integration_auto_registration`: PASSED (auto-registration in `MemoryLifecycleManager` and `GUARDRAILS.md`)
16. `test_lifecycle_integration_hit_counter_and_promotion`: PASSED (hit counters increment and promote to `MEMORY.md` at $\ge 3$ references)
17. `test_lifecycle_integration_promote_to_skill`: PASSED (promotion of reflection to `SKILL.md` bundle via `SkillCompiler`)

### Regression Verification:
```bash
$ .venv/bin/pytest tests/test_ast_scoped_memory.py tests/test_curated_memory.py tests/test_react_loop.py tests/test_react_loop_resilience.py tests/test_skill_synthesis.py tests/test_skillspector_ast_wiring.py tests/test_skillspector_osv_wiring.py
============================= 109 passed in 11.72s =============================
```
Zero regressions across all existing memory, react loop, and skillspector suites.

---

## 4. Must-Haves Verification

- [x] **Truth 1:** `ReActLoop` and `HarnessOrchestrator` detect friction when operations take $\ge 3$ repair attempts, repeat tool failures, or overcome compiler syntax errors.
- [x] **Truth 2:** Hardware gating is strictly enforced via config `auto_reflect` (`"cloud_only"` | `"idle"` | `"off"`) or `HEX_AUTO_REFLECT` env var.
- [x] **Truth 3:** When running on local hardware (Apple Silicon MLX, Ollama, local vLLM) with `auto_reflect` set to `"cloud_only"` or `"off"`, heavy background reflection inference is bypassed, protecting unified RAM from memory starvation.
- [x] **Truth 4:** Post-mortem friction reflections synthesize structured lessons (Symptom, Root Cause, Verified Solution, Negative Guardrail, Linked AST Nodes) into `.hex/reflections/LESSONS.md`.
- [x] **Truth 5:** Lessons are automatically registered in `MemoryLifecycleManager` for hit-counter tracking, contradiction checking, and promotion ($\ge 3$ hits) to `MEMORY.md` or `SKILL.md`.

# `agent` (`harness_core.core`)

**Core autonomous orchestration engine, ReAct tool loop, two-tier model router, governed memory service, subagent delegation, temporal approval leases, Mixture-of-Agents synthesis, and cross-provider reasoning controls.**

Mapped to `harness_core.core` in [`pyproject.toml`](../pyproject.toml), `agent/` owns task lifecycle transitions, CoW-isolated plan/execution flows, crash-safe replay, and policy-gated LLM loops.

## Architecture & Modules

| File | Purpose |
| :--- | :--- |
| `__init__.py` | PEP 562 lazy exports across all 8 submodules and core runtime classes. |
| `models.py` | Authoritative task state machine and contracts: **17** `TaskState` values + `VALID_TRANSITIONS`, `RiskClass` (`R0`–`R4`), `PolicyDecisionType` (`ALLOW`, `DENY`, `REQUIRE_APPROVAL`), `ExecutionMode` (`STANDARD`, `FAST_PATH`), `TaskBudget` defaults (`max_steps=20`, `max_tool_calls=50`, `max_retries_per_step=2`, `max_speculative_branches=2`, `max_wall_seconds=1800`, `max_cost_usd=2.0`), `PlanNode`, `select_runnable_step()`, `TaskSubmitRequest`, and `utcnow_iso()`. |
| `orchestrator.py` | `HarnessOrchestrator` plus lifecycle helpers (`run_lifecycle_maintenance()`, `process_pending_reflections()`) and errors (`ProviderUnavailableError`, `TerminalTaskReentryError`, `DemoGoalNotAchievedError`). Handles `submit_task()`, `submit_and_run()`, `execute_task()`, `fast_path` vs DAG execution in CoW clones, crash recovery + 0 ms sealed replay, bounded repair (`generate_repair_patch()`, `replan_dag()`), compiler/skill preflight, speculative CoW branching (`fork_speculative_branches()`), pause/resume/cancel, project switching, delta export/import, `vendor/harness2` `EXPERIENCE.md` reuse, and pending reflection queueing. |
| `react_loop.py` | `ReActLoop` and `ReActConfig` (`max_turns=30`, `temperature=0.2`, `budget_tokens=100_000`, `moa_config`), `ToolCallRequest`, `ToolCallResult`, `ReActTurn`, `ReActSessionResult`. Drives iterative thought/tool/observation cycles with KV-cache prefix stability, multi-format `parse_tool_calls()`, optional per-turn MoA synthesis, `ActionLoopDetector`/`RepetitionSentry` guards, mandatory `ToolGateway.invoke()` execution, 4-phase `ContextCompactor` compaction + checkpoints, `dialogue_turns` persistence, timeline rewind, and async `SessionTitleSynthesizer`. |
| `router.py` | `TwoTierModelRouter`, `RouteDecision`, `GovernedMemoryService`, `PONYTAIL_YAGNI_DIRECTIVE`, and `_detect_default_local_model()`. Routes Tier-1 local (`LocalMLXModelProvider`/`LocalModelProvider`/`Tier1LogCompressor`) vs Tier-2 cloud/OAuth (`CloudModelProvider`, `AntigravityModelProvider`), blocks `"sensitive"`/`"secret"` cloud egress, wraps MoA via `generate_for_route()`, creates `ContextCompactor` instances, and verifies memory staleness against Graphify SHA-256 hashes + `LESSONS.md`. |
| `subagent.py` | `SubagentManager`, `SubagentRole` (`RESEARCHER`, `CODER`, `TESTER`, `REVIEWER`, `PLANNER`, `GENERAL`), `ROLE_ALLOWED_TOOLS`, `SubagentStatus`, `SubagentRecord`, `SubagentResult`, `normalize_role()`, and typed limit exceptions. Enforces `max_depth=2` recursion bounds, concurrency slots, isolated CoW child branches (`<parent_ws>/.hex/subagents/<subagent_id>`), gateway-routed child tool calls, and fan-out cancellation. |
| `approval_lease.py` | `ApprovalLease` (`ttl_seconds=900`, `scope="temporal"\|"session"`, `match_mode="prefix"\|"exact"`) and `ApprovalLeaseManager` backed by `LedgerStore` (`approval_leases`), SHA-256 `fingerprint_args()`, prefix/path scoping, and explicit revocation/expiry cleanup. |
| `moa.py` | `MixtureOfAgents`, `MoAConfig` (`min_proposers_required=1`, `max_parallel_workers=4`, `timeout_seconds=30.0`), `ProposerConfig`, `ModelCaller`, `ProposerOutput`, `MoAResult`, `MoACancelledError`, `MoAAggregationError`. Fans out bounded concurrent proposer calls with per-candidate/global deadlines, fallback, and aggregator synthesis. |
| `reasoning_effort.py` | Cross-provider reasoning controls: `EFFORT_LADDER` (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`), provider matrices (`OPENAI_EFFORTS`, `ANTHROPIC_EFFORTS`, `GEMINI_EFFORTS`, `DEEPSEEK_EFFORTS`), `ANTHROPIC_THINKING_BUDGETS`, `GEMINI_THINKING_BUDGETS`, `clamp_effort()`, `effort_to_anthropic_thinking()`, `effort_to_gemini_thinking()`, `model_supports_reasoning()`, and `resolve_reasoning_effort()`. |

## Execution Flows & Persistence

1. **Task Intake**: `HarnessOrchestrator` creates a task in `LedgerStore` (`data_dir/ledger.db`), snapshots/clones the workspace under `data_dir/{cow_snapshots,active_workspaces}`, and executes either `FAST_PATH` (single-turn/ReAct flow) or multi-node `STANDARD` DAG plans with proof generation ([`capabilities/proof.py`](../capabilities/README.md)).
2. **State & Recovery**: Every transition is recorded on the hash-chained `task_events` table; interrupted tasks recover from checkpoints and replay previously sealed `tool_calls` in 0 ms without repeating side effects.
3. **Reflections**: Pending friction reflections queue under `<ws>/.hex/reflections/pending_reflections.json` and flush asynchronously or during idle maintenance.

## Current Scope vs Planned Extensions

Per [`BACKEND_PLAN.md`](../BACKEND_PLAN.md) and [`ROADMAP.md`](../ROADMAP.md), several team/cloud orchestration hooks are still planned rather than active:

- **Mixture of Agents (`/moa` surface)**: `MixtureOfAgents` is implemented and configurable programmatically via `ReActConfig(moa_config=...)` and `TwoTierModelRouter`, but `harness_core/config_loader.py` does not parse a YAML `moa:` block yet and no `/moa` CLI/slash command exists.
- **Team Mesh & Cloud Workers**: `ExecutionMode` currently defines only `STANDARD` and `FAST_PATH` (no `TEAM_PRIMARY_HOST`, `TEAM_REPLICA_PEER`, or `CLOUD_WORKER` modes yet); `orchestrator.py` does not yet invoke `cloud/team_mesh.py` `TurnCommitHook`s, and `approval_lease.py` does not yet enforce Team RBAC roles.

## Verification

Tests cover state transitions, ReAct loop cancellation/compaction, MoA fan-out, approval leases, subagent recursion/deadlines, DAG replanning, mandatory gateway enforcement, and reasoning effort mapping in [`tests/core/`](../tests/core) and [`tests/integration/`](../tests/integration).

```bash
python -m pytest tests/core/ -v
```

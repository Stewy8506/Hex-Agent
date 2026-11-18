# Hex vs Hermes: implementation quality, fit and finish, and product readiness

**Review date:** 2026-11-18  
**Hex revision:** `f4ebae90ea847796e9c223eef79263697aa9ab31`  
**Hermes revision:** `f7e2898aaefc8f967dbf7345e3999a65672fe60e`

> **Review status:** Consolidated the original core review and the completed TUI, desktop, planning, subagent/MoA, persistence/runtime, integrations, and CLI/approval/ACP reviews. The dedicated memory/context/cognition reviewer failed with an inference resource/access error and returned no findings. That area is explicitly outstanding; this is not an exhaustive audit. New findings are source-level, not newly reproduced. Both repository revisions were reconfirmed during compilation.

## Executive assessment

Hex has substantial engineering behind its task ledger, hash-chained events, fencing and leases, copy-on-write workspaces, selective DAG replanning, verification artifacts, and native clients. These are genuine differentiators, not merely feature names. Its strongest product direction is a **verified engineering-task harness**, rather than a smaller clone of Hermes's personal-agent platform.

The main weakness is **inconsistent contracts between execution paths and product surfaces**. A successful standard task can destructively publish its workspace; ReAct bypasses gateway authorization and can manufacture success after inference failure; approval leases can grant more authority than the operator intended. Separately, subagents do not run a tool loop, vision does not send pixels, browser automation is an HTML-fetch simulation, and multiple visible desktop controls do not change the backend behavior they represent.

Fit and finish is similarly uneven. The Rust TUI has thoughtful layout, keymap, history budgeting, and wire-contract work, but input editing, paste, resume, and approval recovery are much thinner. The GPUI desktop is a promising native vertical slice, not yet a release-ready cross-platform client: restart persistence, supervision, settings, packaging, and some approval semantics are missing or broken.

Hermes is ahead in **feature lifecycle integration**: real child-agent execution, interrupt propagation, persistent conversational state and repair, MCP registration, actual browser/vision tools, richer TUI editing, packaged desktop delivery, and broader platform gateways. It is not assumed bug-free or uniformly safe. Its ordinary local terminal is not a security sandbox; `/plan` is prompt-driven, not an enforced read-only engine; background delegation is process-local; and its very large module families and recovery stacks impose substantial maintenance and audit cost.

**Recommendation:** fix safety and truthfulness before feature parity. Preserve Hex's compact verified-task architecture, but make authorization, cancellation, completion, and recovery contracts identical at every entry point. Do not add integrations to compensate for simulated or unwired existing ones.

### Feature readiness at a glance

These are source-informed judgments, not measured performance scores.

| Feature | Hex implementation quality / readiness | Comparison with Hermes |
|---|---|---|
| Verified task execution | Strong ledger/proof foundations; publication blocks safe use of the affected path | Hex has a distinctive verification-oriented architecture; Hermes has more cohesive conversational execution |
| Sandbox and approvals | Useful gateway policy when invoked; fallback is trusted host execution; leases over-grant | Hermes centralizes more lifecycle checks, but its local backend is not confinement either |
| Planning and todo | Revisioned, checkpointed DAG is substantive; interactive planning is shallow; todo contract broken | Hex wins durable DAG revisioning; Hermes wins todo/kanban lifecycle and plan-artifact UX |
| Subagents and MoA | Delegation largely a single-call facade; lifecycle limits and diversity incomplete | Hermes has real child loops, enforced narrowing, and interruptible orchestration |
| Rust TUI | Attractive, compact, contract-tested cockpit; basic editor and recovery defects | Hermes has much richer editing, busy-input modes, attachments, and recovery |
| Native desktop | GPUI vertical slice with real ACP streaming; not packaged or resilient | Hermes is substantially more complete, at the cost of Electron footprint and code sprawl |
| Sessions, checkpoints, storage | Strong task-state concurrency; crash/replay interaction and schema drift undermine recovery | Hermes is stronger in conversational durability, repair, retention, and rollback UX |
| Scheduler and daemon | Persisted schedules and worker fencing exist; ticks/supervision/Windows incomplete | Hermes has locking, catch-up policy, missed-run accounting, and sleep-aware scheduling |
| CLI, onboarding, ACP | Good setup/config-write ergonomics; private ACP dialect and cancellation gaps | Hermes has official ACP SDK integration and deeper interrupt/approval workflows |
| Skills, MCP, integrations | Verification hooks, Telegram, web search, Git automation and LSP are real; several other features are unwired/simulated | Hermes offers wider, actually registered integrations and plugin lifecycle |
| Memory, context, reflection | **Not assessed by the completed feature review** | No comparative conclusion; dedicated review remains outstanding |

### Highest-priority findings

| Priority | Finding | Why it comes first |
|---|---|---|
| P0 | B01: destructive workspace publication | Can delete Git metadata and concurrent user work on the normal success path |
| P1 | B02, B04, B45, B46, B52: authorization/confinement inconsistencies | Tool reachability, approval authority, and filesystem authority do not match the advertised boundary |
| P1 | B03, B10–B11, B51: fabricated or unsupported capability outcomes | Users and automation can treat work that did not happen as success |
| P1 | B12, B47: ineffective cancellation | A canceled operation can remain live and later overwrite its cancellation state |
| P1 | B36–B37: crash/replay fidelity and database schema drift | Durability claims fail at recovery and deployment boundaries |
| P2 | B19–B25, B27–B34: client correctness and recovery | Visible polish is not enough if approvals, input, resume, models, or reconnect misbehave |

## Scope, evidence, and limitations

This is a **source-level comparative implementation review with targeted local probes inherited from the original review**, not an exhaustive security audit, a live-model benchmark, or a measured UI/performance evaluation. Sources are pinned to the revisions above. No remote branch, shared service, or application code was changed for this review.

Evidence labels:

- **Reproduced:** the original reviewer executed the relevant module against disposable local data.
- **Isolated-method reproduced:** the original reviewer compiled and ran the exact source function with inert collaborators because the complete dependency environment was unavailable. This verifies that function, not a full application launch.
- **Source-confirmed:** an explicit source path establishes the stated behavior or mismatch. The new feature reviewers did not run applications or reproduce these findings end to end.
- **Gap:** a missing integration, narrower capability, intentional limitation, or missing lifecycle contract; not a proven defect.

Priorities are engineering priorities, not CVSS scores:

- **P0:** block release/use of the affected path until fixed; data-loss potential.
- **P1:** major correctness or safety guarantee broken.
- **P2:** meaningful functionality, reliability, or usability failure.
- **P3:** polish or lower-impact inconsistency.

The original probe environment was Python 3.15.1 on native Windows. `python -m pytest --version` failed because pytest was not installed; `cargo --version` failed because Cargo was unavailable. No dependencies were installed, real provider credentials used, or live integrations contacted. No native terminal or desktop interaction was exercised. Test assessments below describe inspected test coverage, **not suites executed successfully**. Raw test-file counts and source-line counts are intentionally not used as quality scores.

The original B01–B05 identifiers are preserved. New bugs are B06 onward; gaps are described separately rather than counted as confirmed bugs. Files and source locations are linked to the local checked-out repositories. Absence claims refer to the sampled entry points and their call sites, not proof that no alternative implementation exists anywhere.

## 1. Core execution and task completion

### Quality and comparative fit

Hex has distinct execution routes rather than one uniform runtime:

| Route | Implementation | Consequence |
|---|---|---|
| Standard task | Snapshot, stored DAG, gateway invocation, verification, publication | Strongest intended engineering guarantees, but publication has a data-loss defect |
| Fast path | Predetermined read-only steps followed by response synthesis | Useful lightweight interaction; not an autonomous edit/test loop |
| Explicit ReAct | Iterative model/tool loop against the supplied workspace | More agent-like behavior, but does not inherit the standard path's complete policy, cancellation, snapshot, or verification guarantees |

Sources: [agent/orchestrator.py:938](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/agent/orchestrator.py#L938), `agent/orchestrator.py:963`, `:1885`, and `:608`.

Explicit task states, checkpoints, proof artifacts, and gateway journaling are valuable foundations. However, the ReAct path treats a model completion marker as success. Passing through a `VERIFYING` state is not equivalent to executing the standard verification pipeline.

### B01 — Successful workspace publication can delete original repository metadata

**P0 · Reproduced at snapshot-provider level; publication call site source-confirmed.**

After successful standard-task verification, `modified_files` causes `clone_to_workspace(attempt_ws, source_ws)` to run at `agent/orchestrator.py:2517`. The APFS/portable provider delegates to `_cow_copy_tree`, which removes an existing destination directory at [storage/cow.py:249](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/storage/cow.py#L249). Snapshots exclude `.git`, `.hex`, and `.venv` (`storage/cow.py:26,365`).

**Trigger:** an R2-modifying standard task reaches publication using the APFS/portable provider. This is not merely a rollback edge case.

**Impact:** the source directory is recreated from a snapshot lacking Git metadata, local Hex state, and its virtual environment. Files created or changed by the user after the snapshot are also lost or replaced. The portable result is not extrapolated to every native snapshot backend.

```text
backend: portable_cow_copy
verified file content: present
git_preserved: false
hex_preserved: false
venv_preserved: false
concurrent_user_work_preserved: false
```

**Fix:** separate fresh-directory cloning from publication into an existing workspace. Publish only the verified delta with base-version/conflict checks; preserve unrelated files and metadata; make publication recoverable. Merely retaining `.git` while still replacing concurrent user work is insufficient.

**Regression:** exercise complete standard-task publication in a temporary Git repository containing `.hex`, environment metadata, unrelated files, and concurrent edits. Metadata must survive; conflicts must stop publication. The existing fresh-directory clone test at [tests/storage/test_cow_providers.py:61](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/tests/storage/test_cow_providers.py#L61) tests a different contract.

### B02 — ReAct handler dispatch bypasses gateway policy

**P1 · Isolated-method reproduced.**

[agent/react_loop.py:774](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/agent/react_loop.py#L774) selects a registered handler and executes it directly at lines 798/800 instead of calling `ToolGateway.invoke`. Allowlist, R3 approval, and R4 denial rules live in [harness_core/gateway.py:981](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/harness_core/gateway.py#L981); `invoke` begins at line 1042.

**Trigger:** explicit ReAct, including `hex run --react` at [hex_cli/cli.py:957](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/hex_cli/cli.py#L957), with a tool whose restriction relies on the gateway.

**Impact:** registration alone is enough for this loop to call a handler. Individual handlers may still enforce checks, so this is not a claim that every tool effect is unrestricted. Separate journaling is not authorization.

**Probe:** an inert gateway whose `invoke` always denied was paired with a dummy registered handler. Exact ReAct dispatch executed the handler, returned `is_error: false`, and never called `invoke`.

Related direct dispatch exists in ACP `tools/call` at [hex_cli/acp.py:1078](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/hex_cli/acp.py#L1078). This is a local protocol consistency problem, not a demonstrated remotely reachable exploit.

**Hermes:** managed execution explicitly routes through scope, plugin, argument, and guardrail checks at [benchmarks/hermes-agent/agent/tool_executor.py:676](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/tool_executor.py#L676).

**Fix/regression:** make the gateway the mandatory execution boundary. Test CLI/ReAct and ACP with a denied dummy tool; assert zero side effects and policy parity with standard tasks.

### B03 — Provider failure can become fabricated ReAct success

**P1 · Isolated-method reproduced; success interpretation source-confirmed.**

The nested model caller catches provider exceptions at `agent/orchestrator.py:675` and silently enters deterministic fallback. Its final branch returns `<finish>Completed goal: ...</finish>` at line 750 even if no tool ran. With an observation, lines 700–711 claim completion without checking whether the tool failed. `agent/react_loop.py:1444` interprets the marker as `success = True`.

```text
Provider raises simulated outage:
<finish>Completed goal: Implement rate limiting for authentication</finish>

Observation reports {"ok": false, "exit_code": 1}:
<finish>Goal 'Implement rate limiting for authentication' completed successfully.
Observation from run_tests: {"ok":false,"exit_code":1}</finish>
```

**Impact:** automation can receive exit zero and a success record for work not performed or failed tests. Canned file-writing fallback must not silently become a production agent after provider failure.

**Hermes:** [benchmarks/hermes-agent/agent/turn_recovery.py:1185](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/turn_recovery.py#L1185) returns failed-turn outcomes with reason/retryability rather than inventing completion.

**Fix/regression:** inference unavailable must mean explicit failure/degradation. Select and label any demo mode explicitly. Provider absence, provider exception, failed tool observation, and a zero-tool implementation request must not produce verified success.

## 2. Sandboxing and workspace safety

### Quality and comparative fit

Hex's gateway is a good boundary **when callers use it**: it rechecks policy on replay, journals pending calls before effects, and redacts results. Bubblewrap and Docker show intent to provide genuine isolation. But a CoW snapshot protects a working copy; it does not confine arbitrary processes. Clean environment, timeout, and valid `cwd` are separate protections, not a filesystem boundary.

The broad “every code change and test” / “zero host pollution” wording at [README.md:42](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/README.md#L42) exceeds the reviewed paths' guarantees.

### B04 — Fallback sandbox permits writes outside the workspace

**P1 · Reproduced using the actual module.**

[storage/sandbox.py:198](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/storage/sandbox.py#L198) validates `cwd` and launches an ordinary subprocess. It has no filesystem namespace, restricted token, or equivalent boundary. Fallback selection occurs at `storage/sandbox.py:507` when stronger backends are unavailable.

**Probe:** Python launched inside an allowed temporary workspace changed a sibling sentinel through `../outside-sentinel.txt`.

```text
exit_code: 0
violation_detected: false
outside_workspace_value: changed
backend: restricted_subprocess
```

**Impact:** commands retain the host user's filesystem authority. No real credential access was tested.

**Hermes:** distinguishes local execution from Docker/SSH/other environments at [benchmarks/hermes-agent/tools/terminal_tool_backends.py:132](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/terminal_tool_backends.py#L132). Its local backend is not a secure sandbox either; configurable Docker networking is enabled by default in the reviewed table.

**Fix/regression:** fail closed when confinement is required but unavailable, or explicitly label the fallback trusted local execution. A constrained mode must prevent child reads/writes of a sibling sentinel even with valid `cwd`; invalid-directory tests alone do not establish containment.

## 3. Providers and terminal execution

### Quality and comparative fit

Hex has meaningful provider abstractions, usage records, local/cloud routing, streaming hooks, reasoning-effort handling, and provider integrations. However, [providers/base.py:98](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/providers/base.py#L98) takes a prompt string and returns a `ModelResponse`; ReAct flattens message history before routing (`agent/orchestrator.py:677`). Text-oriented interfaces plus custom tool parsing need end-to-end message/tool tests. Provider breadth is not evidence of reliable autonomous execution.

B03 is the most consequential provider defect identified. Live auth, quota rotation, local inference, throughput, provider-native tool compatibility, and multimodal performance were not tested. MoA route diversity is covered under B14 and vision under B51.

### B05 — Native Windows compound commands choose a nonexistent POSIX shell

**P2 · Isolated-method reproduced with an actual subprocess backend.**

`harness_core/gateway.py:1765` chooses `['/bin/sh', '-c', command]` for recognized shell operators. Native Windows process creation does not resolve that Unix path merely because the caller runs inside Git Bash.

```text
command: printf audit | cat
platform: win32
ok: false
exit_code: -1
stderr: [WinError 2] The system cannot find the file specified
```

**Impact:** pipelines, redirections, and chained commands fail on an advertised platform; simple argument-vector commands can still work.

**Hermes:** resolves an installed Bash through its shell resolver and gives an actionable missing-shell error at [benchmarks/hermes-agent/tools/environments/local.py:597](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/environments/local.py#L597).

**Fix/regression:** use an explicit platform-aware shell contract. Test native-Windows pipeline, redirect, quoted paths with spaces, and cancellation using the production launcher. Skill hooks have a separate occurrence in B56.

## 4. Planning, DAG decomposition, todo, and kanban

### Implementation quality and fit/finish

**Hex strength:** planning is a real durable execution ledger. Revisions have monotonic numbering and supersession; replanning selectively invalidates downstream nodes while preserving independent completed work. Graphify blast-radius tests are pinned into proof verification. Checkpoints carry revision and completed/pending steps.

Evidence: [storage/db.py:2421](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/storage/db.py#L2421), `storage/db.py:2456–2510`; `agent/orchestrator.py:1392–1537,1912–1923,2109–2121`; [capabilities/proof.py:114](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/capabilities/proof.py#L114).

**Hex weakness:** this is not yet an interactive plan-then-approve product. The default DAG is a hardcoded inspect/graph pair (`agent/orchestrator.py:1941–1957`); actual decomposition comes from caller-provided `steps` ([hex_cli/server.py:185](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/hex_cli/server.py#L185)). No reviewed path asks a model to build that DAG.

**Hermes:** `/plan` is explicitly prompt-only, “no engine,” at [benchmarks/hermes-agent/agent/plan_prompt.py:3](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/plan_prompt.py#L3). It asks for a markdown artifact under `.hermes/plans/` and an offer to execute without starting (`:20–26,54–56`). It is wired across CLI/TUI/gateway, but read-only behavior and artifact creation are model-dependent, not enforced. Hermes's stronger machinery is its revisioned, bounded todo store and kanban terminal-handoff/turn-recovery protocols.

`plan_limits.py` and `plan_snapshot_sync.py` in Hermes are **billing/subscription plan code**, not implementation-planning limits; they should not be credited as planning features.

### Bugs

| ID | Priority / evidence | Trigger and impact | Source / fix direction |
|---|---|---|---|
| B06 | P1 · Source-confirmed | Cyclic or dangling DAG dependencies leave no runnable node; execution falls back to `pending[0]`, violating dependency ordering instead of refusing the plan | `agent/orchestrator.py:2014–2018`; `storage/db.py:2403–2454` lacks matching validation. Validate topology at save and pause/fail on unsatisfied dependencies |
| B07 | P2 · Source-confirmed | Tool schema advertises todo `complete`, but handler has no `complete` branch and returns `ok: true`; the checklist remains unchanged | `harness_core/gateway.py:186,1270–1339`. Align schema/handler and reject unsupported actions |
| B08 | P2 · Source-confirmed | Todo writes swallow `OSError` and still succeed; corrupt JSON silently resets state. Non-atomic saves can lose task state. Secondary P3 issue: `len(todos)+1` collides after deletion, and status strings are unchecked | `harness_core/gateway.py:1260–1266,1284,1291,1306,1328–1339`. Atomic write, propagate failures, unique IDs, finite statuses |
| B09 | P2 · Source-confirmed | After `max_replans=20`, continued divergence no longer invalidates downstream work; execution continues without an attention state | `agent/orchestrator.py:1996–1997,2096–2107`. Stop/pause when the replan budget is exhausted while divergence persists |

### Gaps against Hermes

- **Plan approval and display — P2 gap.** `PLANNING→WAITING_APPROVAL` exists at [agent/models.py:66](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/agent/models.py#L66), but reviewed waits arise from risky tools, not approving a plan. Legacy TUI plan mode is a prefix plus fast path at [hex_cli/tui.py:3464](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/hex_cli/tui.py#L3464); Rust TUI prefixes are at [crates/hex-tui/src/main.rs:1402](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/crates/hex-tui/src/main.rs#L1402). There is no reviewed DAG approval/editor or persisted plan-artifact UX. The dashboard claims dynamic DAG visualization but renders proof/events JSON at [hex_cli/dashboard.py:315](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/hex_cli/dashboard.py#L315).
- **Todo continuity — P2 gap.** Hermes reinjects bounded todo state after compression and hydrates it from history: [benchmarks/hermes-agent/tools/todo_tool.py:102](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/todo_tool.py#L102), [benchmarks/hermes-agent/run_agent.py:1075](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/run_agent.py#L1075). Equivalent Hex lifecycle wiring was not found in this area review; this does not assess Hex's full compressor.
- **Kanban — P2 gap.** Hermes has a board/handoff toolset, stop gates against narrated exits, and typed in-place recovery: [benchmarks/hermes-agent/tools/kanban_tools.py:1332](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/kanban_tools.py#L1332), [benchmarks/hermes-agent/agent/kanban_stop.py:64](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/kanban_stop.py#L64), [benchmarks/hermes-agent/agent/kanban_turn_recovery.py:140](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/kanban_turn_recovery.py#L140).

**Fairness:** Hermes's `/plan` is not an enforced planning sandbox. Todo is in memory with transcript hydration, not Hex-style transactional DAG storage. Hex's revisioning, downstream invalidation, pinned verification, and checkpointed DAG resume are stronger in those dimensions.

**Tests / acceptance:** Hex's [tests/core/test_dag_replanning.py](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/tests/core/test_dag_replanning.py) covers real selective invalidation and revision chains, but the sampled coverage does not establish cycle rejection, todo `complete`, or budget exhaustion. Add those contract tests plus a plan-display/approval path. Hermes has broader todo/kanban lifecycle tests, including [benchmarks/hermes-agent/tests/tools/test_todo_tool.py](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tests/tools/test_todo_tool.py) and [benchmarks/hermes-agent/tests/agent/test_kanban_turn_recovery.py](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tests/agent/test_kanban_turn_recovery.py).

## 5. Subagents, delegation, MoA, and background workers

### Implementation quality and fit/finish

Hex creates child CoW workspaces and exposes role records, background threads, and result structures. However, [agent/subagent.py:425](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/agent/subagent.py#L425) performs **one routed text-generation call**, not a child tool loop. Allowed tools are merely listed in the prompt; artifacts/findings remain empty. Calling this coding/testing delegation overstates what the production path does.

There are genuine foundations: child workspace copies (`agent/subagent.py:252–267,394–405`), worker lease/fencing renewals at [runtime/worker_daemon.py:471](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/runtime/worker_daemon.py#L471), bounded process output buffers at [runtime/process_registry.py:47](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/runtime/process_registry.py#L47), and per-proposer error isolation plus MoA ledger turns at [agent/moa.py:222](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/agent/moa.py#L222) and `agent/react_loop.py:1027–1095`. A workspace copy is not process confinement; B04 still applies.

Hermes runs real child agents with enforced toolset narrowing and server-owned limits, plus lifecycle handles and teardown: [benchmarks/hermes-agent/tools/delegate_tool_toolsets.py:56](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/delegate_tool_toolsets.py#L56), [benchmarks/hermes-agent/agent/subagent_lifecycle.py:392](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/subagent_lifecycle.py#L392), [benchmarks/hermes-agent/tools/delegate_tool.py:651](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/delegate_tool.py#L651).

### Bugs and substantive implementation shortcomings

| ID | Priority / evidence | Trigger and impact | Source / fix direction |
|---|---|---|---|
| B10 | P1 · Source-confirmed | CODER/TESTER delegation produces analysis text only; it cannot edit, test, populate artifacts, or enforce role permissions through dispatch | `agent/subagent.py:413–441`; stored gateway/ledger collaborators at `:220–223` are unused by execution. Implement a child tool loop through the shared gateway with an enforced role subset, or rename the feature analysis-only |
| B11 | P1 · Source-confirmed | Gateway constructs a manager without a router; the no-router path manufactures “processed goal” and marks COMPLETED. Likewise, direct worker execution without an orchestrator marks a task SUCCEEDED without executing it | `harness_core/gateway.py:1957–1964`; `agent/subagent.py:442–453`; `runtime/worker_daemon.py:214–217`. Missing execution backend must fail closed |
| B12 | P1 · Source-confirmed | Delegation timeout/budget are accepted but not enforced. Interrupt is checked only before the call, has no reviewed production caller, and a late result can overwrite INTERRUPTED with COMPLETED; parent cancellation does not fan out to children | `agent/subagent.py:238–239,360,407,419–422,453–470`; `harness_core/gateway.py:1973–1987`; `agent/orchestrator.py:2655–2679`. Enforce deadlines/budgets, wire cancellation, and recheck terminal state before publishing results |
| B13 | P2 · Source-confirmed | Every async spawn creates an unbounded daemon thread. Depth is taken from model-supplied `current_depth`, not trusted parent state | `agent/subagent.py:299–307`; `harness_core/gateway.py:1984` versus schema `:434–449`. Server-owned depth and bounded concurrency |
| B14 | P2 · Source-confirmed | Wired MoA caller accepts one argument; candidate model/provider/temperature settings are dropped. “Diversity” becomes repeated calls to the same route. Timeout config is unused, executor shutdown waits for outstanding calls, and TypeError signature probing can repeat a call whose body raised TypeError | `agent/orchestrator.py:665,777`; `agent/moa.py:179–200,253–288`. Typed caller contract, pass route settings, deadline-aware nonblocking cancellation |
| B15 | P2 · Source-confirmed | Heartbeat detects lost fencing authority but execution/claim loop does not check the flag, so a fenced worker can continue work | `runtime/worker_daemon.py:485–493,497–538`. Stop claims and cancel current execution on fence |
| B16 | P2 · Source-confirmed | On Windows, process teardown targets the parent PID without POSIX-style group isolation; grandchildren can remain running | `runtime/process_registry.py:236–238,272–274,479–489`. Use a Windows process-tree ownership mechanism such as Job Objects |
| B17 | P3 · Source-confirmed | A temporary child workspace infers its parent by walking three directories up, producing a diff against an unrelated directory instead of its actual parent | `agent/subagent.py:267,446–451`. Store and use the explicit parent workspace |

### Gaps against Hermes

- **Enforced child authority:** subset-of-parent checks at `benchmarks/hermes-agent/agent/subagent_lifecycle.py:468–470`; Hex's current role list is a prompt, not an execution boundary. B10 is not proof of a side-effecting child policy bypass: the present child cannot invoke tools at all.
- **Identity and capabilities:** child-process scope fencing at [benchmarks/hermes-agent/agent/delegation_context.py:122](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/delegation_context.py#L122), one-shot worker policy binding at [benchmarks/hermes-agent/agent/safe_worker_policy.py:52](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/safe_worker_policy.py#L52), and lifecycle capability handles.
- **Cancellation and teardown:** active-child hard-interrupt fan-out (`subagent_lifecycle.py:355–360`); orphan drain and process-group cleanup at [benchmarks/hermes-agent/agent/managed_worker.py:199](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/managed_worker.py#L199); interruptible MoA with partial-cache protection at [benchmarks/hermes-agent/agent/moa_loop.py:686](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/moa_loop.py#L686).

**Fairness:** Hermes children share the parent cwd by design; Hex's separate child copy is useful. Hermes background delegation is process-local and cannot promise restart durability. Its active synchronous children and detached background units have deliberately different stop semantics; an ordinary turn interrupt is not a global kill of detached background work. The lifecycle API also has a fixed worker pool and rejects per-launch timeout overrides (`subagent_lifecycle.py:160,228`). Its MoA lifecycle/cache machinery is considerably more complex.

**Tests / acceptance:** Hex's [tests/core/test_subagent_delegation.py:63](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/tests/core/test_subagent_delegation.py#L63) checks role-table membership, not enforced child dispatch; tests at `:96–110` accept the no-tool completion path. Add a real edit-and-test child, denied-tool side-effect test, missing-router failure, deadline enforcement, cancellation race, bounded spawn, distinct proposer routing, and fenced-worker stop test. Hermes's delegate and MoA tests cover more lifecycle/error interactions; file count alone does not prove correctness.

## 6. Rust TUI and terminal interactive experience

### Implementation quality and fit/finish

Hex's Ratatui cockpit shows deliberate work: a shared keymap drives help, frame pacing favors input, adaptive sidebar preferences persist atomically, and transcript eviction stops on archive failure. Actual server wire fixtures are tested. These are stronger engineering choices than purely visual snapshots.

Evidence: [crates/hex-tui/src/ui/keymap.rs:55](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/crates/hex-tui/src/ui/keymap.rs#L55), [crates/hex-tui/src/event.rs:72](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/crates/hex-tui/src/event.rs#L72), [crates/hex-tui/src/sidebar.rs:243](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/crates/hex-tui/src/sidebar.rs#L243), [crates/hex-tui/src/history.rs:10](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/crates/hex-tui/src/history.rs#L10), `crates/hex-tui/src/main.rs:489–495,1029–1239,1579–1755`.

The input layer is much narrower: a single-line composer, in-memory command recall, no bracketed-paste handling, no external editor/attachment UX, and no busy queue/steer mode. Several async operations announce success before it is known.

Hermes's React/Ink TUI has multiline editing, paste attachments/chips, external editor, persisted prompt history, virtualized transcript, approval paging, and budgeted gateway recovery. Source examples: [benchmarks/hermes-agent/ui-tui/src/app/useSubmission.ts:275](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/ui-tui/src/app/useSubmission.ts#L275), [benchmarks/hermes-agent/apps/shared/src/node/prompt-history.ts:1](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/apps/shared/src/node/prompt-history.ts#L1), [benchmarks/hermes-agent/ui-tui/src/app/gatewayRecovery.ts:7](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/ui-tui/src/app/gatewayRecovery.ts#L7).

### Bugs

| ID | Priority / evidence | Trigger and impact | Source / fix direction |
|---|---|---|---|
| B18 | P2 · Source-confirmed missing filter; host manifestation untested | All key kinds are processed. On terminals emitting release/repeat events, one physical key can cause duplicate input/submission; native Windows behavior needs confirmation | `crates/hex-tui/src/event.rs:44`; `crates/hex-tui/src/main.rs:379–381,645–661`. Filter the intended `KeyEventKind` and test on Windows. Do not treat duplicate Windows input as reproduced |
| B19 | P2 · Source-confirmed | Bracketed paste is not enabled, paste events are ignored, and Enter submits. A multiline paste can submit a fragment instead of inserting one editable prompt | `crates/hex-tui/src/main.rs:87,832–841`; `event.rs:47`. Enable bracketed paste and insert payload as text; explicitly handle multiline input |
| B20 | P2 · Source-confirmed | Approval modal closes and prints success while RPC errors are discarded; cancel RPC errors are also discarded. User gets false acknowledgement and a potentially stuck busy turn | `crates/hex-tui/src/main.rs:518–520,563–573`. Restore pending approval on failure; show error and truthful cancel state |
| B21 | P2 · Source-confirmed | History recall sets cursor to UTF-8 byte length while editing treats it as a character index; non-ASCII recalled prompts misposition cursor/backspace | [crates/hex-tui/src/app.rs:2493](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/crates/hex-tui/src/app.rs#L2493), `:2504` versus `:2440–2480`. Use consistent character/grapheme indexing |
| B22 | P2 · Source-confirmed | A final response is dropped if any earlier assistant message contains its first 40 characters; repeated boilerplate can suppress the current answer | `crates/hex-tui/src/main.rs:399–417`. Associate deltas/final text with a turn ID/message index, not content-prefix dedupe |
| B23 | P2 · Source-confirmed | Start-screen resume loads local history for display without a `session/load` handshake. Missing server metadata can lead to an empty runtime session with old transcript still visible | `crates/hex-tui/src/app.rs:1724–1760`; `hex_cli/acp.py:762–775`. Use the same load path as `--session` (`main.rs:240–281`), with explicit failure/degraded-context notice |
| B24 | P2 · Source-confirmed | Resume enumeration and local tool-result paging require an external `sqlite3` binary; candidate paths are Unix-oriented and failures silently yield an empty list | `crates/hex-tui/src/app.rs:464–504,842–856`. Use a SQLite library or expose the missing dependency, especially on Windows |
| B25 | P2 · Source-confirmed | Approval renderer returns early below 12 rows/40 columns while approval keys remain active; a user can act on an invisible approval | [crates/hex-tui/src/ui/screens/permission.rs:13](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/crates/hex-tui/src/ui/screens/permission.rs#L13); `main.rs:722–738`. Compact fallback or disable decisions until the request is visible |
| B26 | P3 · Source-confirmed | Tool/skill counts are hardcoded, and `/model` updates the label before backend confirmation | `crates/hex-tui/src/app.rs:1349–1350`; `main.rs:1255–1259`. Render discovered counts and confirmed model state |

### Gaps against Hermes

| Capability | Hex | Hermes reference |
|---|---|---|
| Multiline composer / external editor | Single-line input at [crates/hex-tui/src/ui/input.rs:113](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/crates/hex-tui/src/ui/input.rs#L113) | [ui-tui/src/app/useComposerState.ts](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/ui-tui/src/app/useComposerState.ts), [ui-tui/src/lib/editor.ts](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/ui-tui/src/lib/editor.ts) |
| Busy input | “Please wait”; no queue/steer mode (`main.rs:833–836`) | Queue/steer/interrupt submission modes (`useSubmission.ts:275–279`) |
| Prompt recall across restart | History is archived but not reloaded into command recall (`history.rs:634–648`) | Shared persisted prompt-history store |
| Approval readability | Live renderer ellipsizes payload; richer risk/wrapped renderer is unwired | [ui-tui/src/__tests__/approvalPromptPaging.test.tsx:42](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/ui-tui/src/__tests__/approvalPromptPaging.test.tsx#L42) checks paged commands before choice |
| Slash discovery | Compile-time catalog (`app.rs:417–447`) | [tui_gateway/command_discovery.py](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tui_gateway/command_discovery.py) and fuzzy discovery |
| Attachments / clipboard / OSC52 | No corresponding reviewed native flow | [ui-tui/src/lib/imageAttachments.ts](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/ui-tui/src/lib/imageAttachments.ts), [ui-tui/src/lib/osc52.ts](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/ui-tui/src/lib/osc52.ts) |
| i18n | Hardcoded English in reviewed client | Backend catalog loader at [ui-tui/src/i18n/loader.ts](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/ui-tui/src/i18n/loader.ts); bundled TS fallback is English |
| Disconnect recovery | Connection-closed notice, no reattach (`main.rs:339–341`) | Budgeted crash recovery (`gatewayRecovery.ts:7–8`) |
| Modes | Rust client sends prompt prefix, not a typed mode field (`main.rs:1402–1406`) | Session-mode lifecycle exists, but `/plan` still does not constitute enforced read-only execution |

The richer Hex approval module at [crates/hex-tui/src/ui/approval.rs](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/crates/hex-tui/src/ui/approval.rs) should not be credited as live UI merely because it exists; the current screen is `ui/screens/permission.rs`.

**Fairness:** Hermes pays for richer UX with a vendored Ink fork, React terminal runtime, and extra gateway layers. Its [ui-tui/README.md:13](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/ui-tui/README.md#L13) and [ui-tui/CANONICAL_GATEWAY.md:3](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/ui-tui/CANONICAL_GATEWAY.md#L3) disagree about client gateway spawning; canonical notes at `:60–77` acknowledge remaining secret/sudo, clarification, legacy RPC, and subscription-lifecycle gaps. Its memory/virtualization work shows runtime pressure, not measured proof of inferior performance.

**Tests / acceptance:** Hex has substantive keymap, history, rendering, state-machine and real-server wire tests. Add native key-kind/paste tests, Unicode recall, failed approval/cancel RPC, repeated-prefix final responses, short-terminal approvals, and start-screen resume/context agreement. Hermes has broader terminal parsing/resize/recovery and PTY coverage; neither native client was exercised in this review.

## 7. Native desktop application

### Implementation quality and fit/finish

Hex's desktop is **Rust/GPUI**, not Electron or Tauri. It shares a typed protocol crate with the TUI and normally talks over stdio to a spawned Python backend. The client has byte-budgeted lossless notification machinery and child cleanup at [crates/hex-protocol/src/client.rs:180](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/crates/hex-protocol/src/client.rs#L180), `:234,368`. Desktop has real streaming/cancel/approval calls, multiline composer, per-session drafts, and actual wire-fixture tests at [App/hex-desktop/src/acp_bridge.rs:577](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/App/hex-desktop/src/acp_bridge.rs#L577).

It is a promising vertical slice, not a complete desktop product. Some resilience already available in the protocol layer is not used by the UI. Native GPU rendering is an architectural strength, but no memory/startup/frame-time comparison was performed.

Hermes's Electron desktop has supervised backend lifecycle, authenticated local/remote/SSH/cloud connections, session reattachment, tray, notifications, gated updates/backups, and OS packaging. Sources include [benchmarks/hermes-agent/apps/desktop/electron/main.ts:9958](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/apps/desktop/electron/main.ts#L9958), [electron/dashboard-token.ts:84](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/apps/desktop/electron/dashboard-token.ts#L84), and [electron-builder.config.cjs:182](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/apps/desktop/electron-builder.config.cjs#L182).

### Bugs

| ID | Priority / evidence | Trigger and impact | Source / fix direction |
|---|---|---|---|
| B27 | P2 · Source-confirmed | “Allow all in session” sends `allow_session`, which the server does not recognize; it becomes DENY while UI pre-announces approval and response says applied | `App/hex-desktop/src/acp_bridge.rs:390`; [App/hex-desktop/src/chat.rs:746](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/App/hex-desktop/src/chat.rs#L746); [hex_cli/acp_approval.py:22](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/hex_cli/acp_approval.py#L22), `:104–110`; `hex_cli/acp.py:1116–1123`. Use canonical `approve_scope` and typed outcome semantics. Fix B45 before making broad scope grants easier |
| B28 | P2 · Source-confirmed | Startup is one-shot; disconnect only adds a notice. Reconnect helper is unused and stops after first success, leaving the app offline after backend loss | `App/hex-desktop/src/chat.rs:233–251,309–313`; `acp_bridge.rs:407–419`. Supervise/reconnect with explicit session recovery |
| B29 | P2 · Source-confirmed | Opening another modal or receiving a second approval overwrites the active approval; lost request waits until timeout denial | [App/hex-desktop/src/ui/shell.rs:96](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/App/hex-desktop/src/ui/shell.rs#L96), `:109–111`; `hex_cli/acp_approval.py:95–100`. Queue approvals and prevent replacing unresolved requests |
| B30 | P2 · Source-confirmed | “15-minute lease” puts `lease_duration:900` in reason text; server never parses it. Scoped lease remains 3600 seconds; “Once” creates no lease | `App/hex-desktop/src/acp_bridge.rs:394–398`; `hex_cli/acp_approval.py:36,105–122,140–148`. Add a validated TTL field or remove the misleading control |
| B31 | P2 · Source-confirmed | UI ignores broadcast `Lagged` events; authoritative final response only fills an empty message. Lost deltas leave a truncated answer presented as final | `App/hex-desktop/src/chat.rs:291,626–628`. Use lossless subscription or reconcile every streamed answer against its authoritative final payload |
| B32 | P2 · Source-confirmed | Model catalog is hardcoded; picker changes label, not active-session backend. New model is only sent for a future new session | [App/hex-desktop/src/ui/modals/model_picker.rs:79](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/App/hex-desktop/src/ui/modals/model_picker.rs#L79); `chat.rs:503–506,801–810`. Fetch actual options and apply or explicitly label deferred change |
| B33 | P2 · Source-confirmed build-path issue; packaging gap | Ungated `tokio::net::UnixStream` prevents a Windows build; interpreter probe uses `.venv/bin/python` rather than Windows layout. No desktop installer/bundle path was found | `App/hex-desktop/src/acp_bridge.rs:185`; `crates/hex-protocol/src/client.rs:341`. Platform-gate transports, resolve Windows interpreter, add supported bundling. This does not prove GPUI cannot support Linux/macOS |
| B34 | P1 · Source-confirmed trust gap; exploit untested | Auto-connect prefers predictable `/tmp/hex-acp.sock` if present, without peer authentication. A local process able to create that socket can impersonate the backend and receive prompts/decisions | `App/hex-desktop/src/acp_bridge.rs:209–217`. Remove unused auto-adoption or use a private user runtime directory and verify peer identity. Normal spawned stdio does not have this exposure |
| B35 | P3 · Source-confirmed | Child stderr is drained to a sink; startup blocks on Git lookup on the UI thread; late approval acknowledgements can look successful despite expired pending requests | `crates/hex-protocol/src/client.rs:241–244,375–378`; `App/hex-desktop/src/chat.rs:176–183,768`; `hex_cli/acp.py:1119–1123`. Preserve diagnostic logs, async lookup, distinguish expired/ignored from applied decisions |

### Gaps against Hermes

- **Persistence/resume — P2:** protocol/server implement `session/list/load/history` (`hex_cli/acp.py:551–562`; `crates/hex-protocol/src/client.rs:512–524`), but the reviewed desktop never calls them. Desktop session/transcript state does not survive restart. Hermes tests reattach at [apps/desktop/e2e/core/reattach-running-turn.spec.ts](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/apps/desktop/e2e/core/reattach-running-turn.spec.ts).
- **Settings/workspace — P2:** settings is a notice (`App/hex-desktop/src/chat.rs:914–917`); cwd is chosen at startup at [App/hex-desktop/src/main.rs:14](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/App/hex-desktop/src/main.rs#L14), with no reviewed workspace-picker flow.
- **Tray/notifications/update — P2/P3:** no corresponding Hex implementation was found. Hermes has [electron/tray-host.ts](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/apps/desktop/electron/tray-host.ts), [electron/notification-registry.ts](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/apps/desktop/electron/notification-registry.ts), [electron/app-updater.ts](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/apps/desktop/electron/app-updater.ts), [electron/update-gate.ts](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/apps/desktop/electron/update-gate.ts), and pre-update backup modules.
- **Reasoning/diffs — P3:** `ThinkingDisclosure` and `DiffView` components exist at [App/hex-desktop/src/ui/message.rs:163](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/App/hex-desktop/src/ui/message.rs#L163) and [App/hex-desktop/src/ui/tool_card.rs:62](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/App/hex-desktop/src/ui/tool_card.rs#L62), but no reviewed live event populates them.
- **Delivery — P2:** Hermes builder config includes macOS bundles and Windows packaging (`electron-builder.config.cjs:182,235–274`), Linux package scripts at [apps/desktop/package.json:46](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/apps/desktop/package.json#L46), and a separate bootstrap installer. Availability in config is not a measured installer reliability result.

**Fairness:** Hex's small native app and stdio-first architecture are attractive. Hermes's large Electron entry point and broad backend/update/auth surface are audit liabilities; its modern Node engine pin is a compatibility burden. A larger feature set does not justify copying its complexity wholesale.

**Tests / acceptance:** Hex has real wire fixtures and GPUI test-platform interaction tests, but [App/hex-desktop/tests/test_desktop_integration.rs:189](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/App/hex-desktop/tests/test_desktop_integration.rs#L189) uses an echo mock returning a fabricated grant status, missing the real server enum/TTL mismatch. Test real Python approval outcomes, concurrent modals, timeout/late decisions, lagged streaming, disconnect/reconnect, restart resume, and actual platform builds. Hermes has broader E2E and packaged smoke coverage; none was run here.

## 8. Sessions, persistence, checkpoints, and recovery

### Implementation quality and fit/finish

Hex's task ledger is a genuine strength: hash-chained events, fencing-token CAS, SQLite immediate-transaction/version-CAS claims, PostgreSQL `SKIP LOCKED`, lease reaping, and reconstructable CoW checkpoints. Sources: `storage/db.py:34–51,645–653,1729–1783,1864–1955`; `agent/orchestrator.py:1899–1923,2110–2121`.

GC covers workspace/CAS lifecycle with race guards at [storage/gc.py:1](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/storage/gc.py#L1). Backup does WAL checkpointing and archive-path validation at [storage/backup.py:162](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/storage/backup.py#L162), `:246–270`. These are substantive foundations, but task durability and chat durability are not the same contract.

Hermes is ahead in conversational replay repair, tool-call UID pairing, turn leases, persistence failure recovery, retention, and per-turn rollback. Examples: [benchmarks/hermes-agent/agent/session_persistence.py:321](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/session_persistence.py#L321), [benchmarks/hermes-agent/session_rules/turns.py:1403](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/session_rules/turns.py#L1403), [benchmarks/hermes-agent/session_rescue.py:10](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/session_rescue.py#L10).

### Bugs

| ID | Priority / evidence | Trigger and impact | Source / fix direction |
|---|---|---|---|
| B36 | P1 · Source-confirmed interaction; end-to-end probe outstanding | A crash inside a multi-call step rolls the tree back to the last completed-step checkpoint, but sealed calls replay from journal without handlers. A rolled-back write can replay as “done” while the file is absent | `agent/orchestrator.py:2110–2121,2197–2218`; `harness_core/gateway.py:1112–1142,1216–1237`; `storage/db.py:2797–2825`. Bind journal replay to restored tree/version or checkpoint/reapply sealed side effects; do not infer full application reproduction |
| B37 | P1 · Source-confirmed | SQLite runtime adds schema ad hoc while Alembic head omits later fields/tables. PostgreSQL initialization relies on migrations and misses runtime-only approval/dialogue schema | `storage/db.py:180–200,332–376`; [migrations/versions/0001_initial_schema.py:21](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/migrations/versions/0001_initial_schema.py#L21); [tests/storage/test_alembic_migrations.py:24](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/tests/storage/test_alembic_migrations.py#L24). One migration source for both drivers; migrate runtime DDL and verify feature tables after clean deployment |
| B40 | P2 · Source-confirmed | `/rewind` truncates conversation and marks journal entries REVERTED but does not undo filesystem effects; UI says tools were reverted. Dialogue-recording API has no reviewed production caller, so durable cross-session rewind is incomplete | `hex_cli/tui.py:2160–2187`; `storage/db.py:4048,4221–4239`; [storage/timeline_rewind.py:156](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/storage/timeline_rewind.py#L156). Implement turn snapshot rollback or describe conversation-only rewind honestly; persist dialogue turns |
| B41 | P2 · Source-confirmed | ACP rebuilds model context from only the last ten turns' user/assistant text. Tool results/reasoning are dropped; pending bridge approvals are memory-only | `hex_cli/acp.py:402,810–816`. Restore structured row types/tool pairing and explicitly recover or withdraw pending approvals |
| B42 | P3 · Source-confirmed | SQLite claim swallows `BEGIN IMMEDIATE` failure and proceeds; later CAS failure can leave orphan claim/attempt records | `storage/db.py:1865–1868`. Fail/retry transaction acquisition instead of continuing in a weaker transaction mode |
| B44 | P2 · Source-confirmed state-guard gap | Resume/execute lack an early terminal-task guard, allowing a SUCCEEDED task to be re-entered. Orphan RUNNING recovery relies on a later worker claim rather than startup reconciliation | `agent/orchestrator.py:938–961,2638–2653`; `storage/db.py:1863`. Explicit rerun contract, terminal-state checks, and startup recovery |

B38–B39 and B43 are in the scheduler feature below; numbering remains global. B36 is distinct from destructive publication B01: it is a journal/snapshot consistency failure during recovery.

### Gaps against Hermes

- **Transcript repair:** Hermes removes unanswered tool calls/stray results and repairs role runs at replay (`session_rules/turns.py:1403–1424`); adopts unanswered turns on retry (`session_persistence.py:112–145`). Hex's task journal does not substitute for a repaired conversational transcript.
- **Persistence failure recovery:** Hermes can heal missing session rows, adopt compression tips, and divert to JSONL on store replacement (`session_persistence.py:415–487`). Hex ACP logs persistence failure at `hex_cli/acp.py:456–458` without equivalent repair.
- **User-facing rollback:** Hermes has per-turn shadow-Git checkpoints, pruning, and rollback/undo guidance at [agent/checkpoints/manager.py:1](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/checkpoints/manager.py#L1), [agent/checkpoints/undo_hint.py:1](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/checkpoints/undo_hint.py#L1). Hex checkpoints are chiefly task-internal.
- **Chat retention:** Hex workspace/CAS GC does not prune ACP history. Hermes has session archive/prune and spillover reference handling at [session_retention.py:1](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/session_retention.py#L1).
- **SQLite operations:** Hermes includes network-filesystem WAL fallback, WAL limits, and writer acquisition retry at [sqlite_support.py:33](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/sqlite_support.py#L33), `:95–99,125–129`, and [persistence/sqlite/_holder.py:811](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/persistence/sqlite/_holder.py#L811). Hex's simpler WAL/RLock setup (`storage/db.py:214–216`) has fewer operational protections.

**Fairness:** Hermes's healing, rescue, schema, and transcript stacks are large and difficult to audit. Hex's event-chain/fencing design is compact and distinctive; breadth of Hermes recovery code is not evidence it has no corruption bugs.

**Tests / acceptance:** Hex has meaningful concurrency/event-chain/GC/backup tests. But [tests/integration/test_harness_stages.py:125](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/tests/integration/test_harness_stages.py#L125) covers crash rollback separately from replay at `:326–371`; their interaction in B36 is missed. Add multi-call crash→rollback→resume tests, clean Alembic installs on both DB drivers, real tool-result context restoration, file rollback vs rewind assertions, and terminal-task reentry tests. Existing migration tests asserting an obsolete table set do not establish current deployment readiness.

## 9. Scheduling, daemon supervision, and background runtime

### Implementation quality and fit/finish

Hex has persisted cron-like schedules, atomic replacement of schedule files, worker lease renewal, and bounded process output. These are real mechanisms, but atomic file replacement does not provide cross-process tick serialization.

Hermes has cross-process tick locking, missed-run/catch-up policy, timeout handling, sleep-aware clocks, and shared scheduling threads. Sources: [benchmarks/hermes-agent/cron/scheduler.py:3](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/cron/scheduler.py#L3), [cron/missed_runs.py:1](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/cron/missed_runs.py#L1), [agent/sleep_clock.py:1](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/sleep_clock.py#L1), [agent/periodic_scheduler.py:1](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/periodic_scheduler.py#L1), [agent/idle_schedule.py:1](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/idle_schedule.py#L1).

### Bugs

| ID | Priority / evidence | Trigger and impact | Source / fix direction |
|---|---|---|---|
| B38 | P2 · Source-confirmed | Schedule tick loads/runs/saves without an inter-process lock: overlapping ticks can double-fire or lose updates. `next_run` advances only after synchronous execution, so crash can refire and long jobs block others. Missing/unparseable `next_run` is always due | [runtime/scheduler.py:333](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/runtime/scheduler.py#L333), `:357–418`; atomic replace `:271–282` is insufficient. Locked durable claim/tick, explicit retry/catch-up semantics, non-overlap and deadline policy |
| B39 | P2 · Source-confirmed | launchd uses `KeepAlive=true` for one-shot `schedule run-due`, causing immediate restart rather than interval scheduling. systemd hardcodes restart timing. Windows install falls through to systemd instead of a working platform scheduler | [runtime/daemon.py:28](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/runtime/daemon.py#L28), `:75–97,160–161,222–223`; `hex_cli/cli.py:1344–1346`. Use proper launchd/systemd timer contracts and a Windows Task Scheduler backend |
| B43 | P3 · Source-confirmed | Restricted day-of-month and day-of-week are combined with AND instead of conventional Vixie OR semantics | `runtime/scheduler.py:144–150,170`. Implement standard semantics or explicitly document an intentional nonstandard grammar |

Worker fence enforcement and Windows descendant teardown are B15/B16. These findings should be tested together with scheduled task cancellation rather than treated as independent convenience utilities.

**Fairness:** Hermes's scheduler and cron module families are sprawling; extra recovery policy adds maintenance cost. Hex should adopt explicit locking/claims/catch-up semantics without cloning every scheduler abstraction.

**Tests / acceptance:** Hex's [tests/runtime](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/tests/runtime) contains parser, daemon-text/dry-run, worker and process tests, but the reviewed coverage does not exercise concurrent ticks, crash/refire, launchd restart behavior, or native Windows installation. Add those and long-job non-overlap tests. Hermes has broader cron behavior tests, not just generated unit-file assertions.

## 10. CLI, onboarding, configuration, approvals, interrupts, and ACP

### Implementation quality and fit/finish

Hex has good first-run ergonomics: credential-gated setup before interactive launch and auto/wizard/blank-slate choices (`hex_cli/cli.py:545–587`). Hardware/environment setup is real at [hex_cli/auto_setup.py:116](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/hex_cli/auto_setup.py#L116). Config parsing reports path/line; writes are atomic and refuse to clobber unparseable existing files at [harness_core/config_loader.py:80](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/harness_core/config_loader.py#L80), `:126–163`.

ACP has useful internal robustness: paged/byte-bounded history, bounded tool previews, parameter errors, threaded prompt turns so cancellation/decisions remain reachable, and denial on approval timeout (`hex_cli/acp.py:37–132,268–315,595–597,1680–1730`; `hex_cli/acp_approval.py:95–100`). Reachable cancel RPC, however, is not the same as a canceled executing loop.

Hermes uses the official ACP SDK and standard session/permission types at [benchmarks/hermes-agent/acp_adapter/server.py:18](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/acp_adapter/server.py#L18), `:572–730`. Its CLI has richer permission policies, batch re-gating, soft/hard interrupt, redirect/steer, and a global scheduling pause.

### Bugs

| ID | Priority / evidence | Trigger and impact | Source / fix direction |
|---|---|---|---|
| B45 | P1 · Source-confirmed | “Approve for project” grants by binary basename, so approval of one `git` command can cover unrelated privileged `git` commands for an hour. Lease matching ignores task/session scope. A no-command approval grants workspace paths and `cwd` is counted as a matching path, broadly approving later workspace calls | `hex_cli/acp_approval.py:131–150`; [agent/approval_lease.py:167](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/agent/approval_lease.py#L167), `:89–103,196–218,338`; `harness_core/gateway.py:1001–1012`. Bind grant to intended command/arguments, task/session and operation; do not treat `cwd` as authority over arbitrary effects |
| B46 | P1 · Source-confirmed endpoint behavior; exposure untested | Approval-decide accepts GET and defaults an omitted decision to APPROVED, with auto-resume true. A request lacking explicit operator intent can approve a privileged action | `hex_cli/server.py:262–291`. POST-only, required finite decision, deny/reject missing input. No remote reachability/CSRF exploit was demonstrated |
| B47 | P1 · Source-confirmed | ReAct turn loop never checks cancellation. `cancel_task` sets an unread flag and rolls back while execution remains live; finalization can overwrite CANCELED | `agent/react_loop.py:1359`; `agent/orchestrator.py:776–811,2660–2669`. Propagate cancel/deadline checks through model/tool waits, join/quiesce before rollback, terminal-state CAS |
| B48 | P2 · Source-confirmed | ACP cancel does not wake a pending blocking approval wait; user can wait until the default 60-second timeout | `hex_cli/acp.py:1020–1047`; `hex_cli/acp_approval.py:47,87`. Cancellation must withdraw/deny all matching pending approvals immediately |
| B49 | P2 · Source-confirmed | Headless Ctrl-C prints/exits without cancel/rollback/lease cleanup; ACP EOF/interrupt exits without orderly turn-thread shutdown | `hex_cli/cli.py:63–68`; `hex_cli/acp.py:1722,1731–1732`. Signal/EOF shutdown hooks with bounded worker cleanup and explicit interrupted state |
| B50 | P3 · Source-confirmed | Doctor checks generic connectivity/Antigravity rather than every configured provider; “active model quota” is not probed; some paths hardcode `~/.hex` despite configurable homes | [hex_cli/doctor.py:168](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/hex_cli/doctor.py#L168), `:533–608`. Probe selected provider/auth paths or label checks honestly; honor configured directories |

### Gaps against Hermes

- **ACP interoperability — P2 gap:** Hex advertises editor integrations (`hex_cli/acp.py:3`; `hex_cli/cli.py:488–490,716–726`) but speaks a private dialect: MCP-style `protocolVersion:"2024-11-05"`, custom approval/update payloads, and no standard authenticate/permission/fs/terminal/mode surface in the reviewed dispatcher (`acp.py:546–582,602–621,848–896`). Hex's own Rust client matching it is not proof of Zed/VSCode compatibility. Adopt official schemas/SDK or distinguish the internal protocol from standard ACP. Hermes's official adapter is a materially better reference.
- **Configuration validation — P2 gap:** parser/write safety is good, but load warns/falls back on malformed YAML and accepts arbitrary provider/model/effort strings (`config_loader.py:308–341`); env-file read errors are swallowed (`:75–76`). Hermes validates values, reports unknown keys, and refuses unsafe writes at [hermes_cli/config_value_checks.py:9](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/hermes_cli/config_value_checks.py#L9) and [hermes_cli/config_read_errors.py:125](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/hermes_cli/config_read_errors.py#L125). Reject malformed known enums without forbidding legitimate custom model IDs.
- **Interrupt workflows — P2 gap:** no corresponding Hex ESTOP pause/resume, host cancel scope, or steer/redirect lifecycle was found. Hermes has [agent/estop.py:1](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/estop.py#L1), [agent/interrupt_control.py:160](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/interrupt_control.py#L160), [agent/interrupt_scope.py:27](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/interrupt_scope.py#L27).
- **Approval policy UX — P2/P3 gap:** Hex lacks comparable manual/smart/off modes, deny rules, guardian circuit breaker, and batch slot re-gating. Sources: [hermes_cli/approval_mode.py:16](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/hermes_cli/approval_mode.py#L16), [tools/approval.py:64](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/approval.py#L64), [agent/terminal_approval_batch.py:105](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/agent/terminal_approval_batch.py#L105). Legacy Hex TUI prompt has neither timeout nor remembered scope (`hex_cli/tui.py:3212–3219`), unlike its ACP bridge.

**Fairness:** Hermes's CLI manual approval still uses synchronous input (`tools/approval.py:1087`); smart approval adds inference cost/latency and possible false denies. Its ESTOP deliberately does not cancel already in-flight work (`agent/estop.py:1–9`); it is not a universal kill switch. Official ACP has a finite worker pool (`acp_adapter/server.py:50`). Hex's fail-closed ACP timeout is a genuine strength even though cancel cannot currently wake it.

**Tests / acceptance:** Hex has happy-path setup/config/approval wire tests, including timeout-deny at [tests/integration/test_acp_approval_bridge.py:116](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/tests/integration/test_acp_approval_bridge.py#L116) and lease tests at [tests/core/test_approval_lease.py](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/tests/core/test_approval_lease.py). Add negative cross-session and different-command lease tests, omitted-decision GET rejection, ReAct cancel during tool/model waits, cancel during approval, EOF cleanup, and standard ACP interoperability against a real client. Current lease-matching tests can pass while testing over-broad semantics.

## 11. Skills, plugins, MCP, browser, vision, voice, gateways, and developer tools

### Implementation quality and fit/finish

Several Hex integrations are genuinely useful and wired:

- **Skill verification hooks:** SHA-256 approval gate before execution at [skill_engine/executor.py:257](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/skill_engine/executor.py#L257), allowlisted environment (`:35–78`), and orchestrator sandbox wiring (`agent/orchestrator.py:1815–1823`). This deterministic verification approach is a differentiator, subject to the actual sandbox backend's limits in B04.
- **Git autopilot/GitHub runner:** real CLI/API behavior at [capabilities/git_autopilot.py:219](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/capabilities/git_autopilot.py#L219), CLI wiring `hex_cli/cli.py:983,1214–1260`.
- **Telegram:** real ingress transport, user/chat allowlists, orchestrator submission, and approval cards at [capabilities/gateways/telegram_bot.py:126](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/capabilities/gateways/telegram_bot.py#L126), `:246–284,359–373,538–564`.
- **Web search/webhooks:** DDG/Tavily/Brave requests with surfaced errors at [capabilities/web_search.py:114](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/capabilities/web_search.py#L114); outbound secret scrubbing at [capabilities/webhooks.py:76](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/capabilities/webhooks.py#L76), `:219–241`.
- **LSP:** real JSON-RPC server registry and registered tools at [lsp/registry.py:66](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/lsp/registry.py#L66), `harness_core/gateway.py:790–821`.

Conversely, module presence is misleading for browser, vision, STT, Discord ingress, MCP startup, and hybrid skill retrieval. These need implementation or honest capability downgrading, not more documentation claiming parity.

### Bugs and capability mismatches

| ID | Priority / evidence | Trigger and impact | Source / fix direction |
|---|---|---|---|
| B51 | P1 · Source-confirmed | `image_inspect` constructs vision without a router and returns canned “Visual structure processed cleanly” as success. Even the routed branch sends filename/size/dimensions, not pixels; the base64 value is not transmitted | `harness_core/gateway.py:2071`; [capabilities/vision.py:121](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/capabilities/vision.py#L121), `:126–151`. Real multimodal content blocks; unavailable route must fail, not invent analysis |
| B52 | P1 · Source-confirmed | Browser navigation accepts `file://` and reads local files without the workspace containment used by `read_file` | [capabilities/browser.py:136](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/capabilities/browser.py#L136), `:136–145`; `harness_core/gateway.py:2029–2034`. Remove scheme or apply the same resolved-path authority checks; no real sensitive file was read in this review |
| B53 | P2 · Source-confirmed | “Interactive headless browser” is urllib HTML fetch; typing updates a dictionary, screenshot saves HTML, evaluate is regex logic. JS/forms/auth automation is not actually executed | `capabilities/browser.py:147–154,250,260–303`; schema `harness_core/gateway.py:904–905`. Drive an actual browser engine or expose honest HTML-fetch/parse tools |
| B54 | P2 · Source-confirmed missing integration | Discord interaction handler has no reviewed production ingress caller; manager starts no receive loop. Send-side behavior exists but commands/reactions do not arrive | [capabilities/gateways/discord_bot.py:345](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/capabilities/gateways/discord_bot.py#L345); [capabilities/gateways/manager.py:87](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/capabilities/gateways/manager.py#L87). Implement authenticated Discord ingress or mark send-only |
| B55 | P2 · Source-confirmed | Native STT returns `None` even with a microphone; Windows TTS selects a silent mock because no Windows provider branch exists | [capabilities/voice.py:331](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/capabilities/voice.py#L331), `:312–315,378–382`. Capture/transcribe for real or remove hands-free claim; explicit unsupported result instead of mock speech |
| B56 | P2 · Source-confirmed Windows issue; environment hardening gap | Skill sandbox hook hardcodes `/bin/sh -c` and Unix PATH. Frontmatter env overrides can reintroduce loader/startup variables not in its blocklist, weakening the claimed clean environment | `skill_engine/executor.py:49–51,70–77,323`. Platform shell resolution; explicit policy for `LD_PRELOAD`, `NODE_OPTIONS`, `BASH_ENV` and equivalents. Skill hash approval is relevant authorization context: this is not a demonstrated untrusted-skill exploit |
| B57 | P3 · Source-confirmed | `list_tool_definitions()` returns the static list, so even externally registered dynamic/MCP tools are not advertised by ACP's discovery call | `harness_core/gateway.py:636–638`; `hex_cli/acp.py:1075`. Build definitions from live authorized registry and test a dynamic tool through discovery and invocation |

### Capability gaps against Hermes

| Capability | Hex status | Hermes status / evidence |
|---|---|---|
| MCP client | Transport/adapter library exists at [harness_core/mcp_client.py:140](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/harness_core/mcp_client.py#L140), `:419,542`; registration hook at `gateway.py:650–667`; no reviewed production startup/config loader calls it | Actually registered at [tools/mcp_tool_registration.py:387](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/mcp_tool_registration.py#L387); profile trust, safe env, auth/circuit-breaker machinery |
| Plugins | No comparable general plugin lifecycle found | Loader/catalog and API boundary checks: [plugins/plugin_loader.py](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/plugins/plugin_loader.py), plugin catalog |
| Skill retrieval | Hybrid retriever exists at [skill_engine/retrieval.py:59](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/skill_engine/retrieval.py#L59), `:531`, but has no reviewed production caller; orchestrator loads whole skill context (`orchestrator.py:399–403`) | Reachable skill tools/slash injection, hub install/locking/curation; fail-closed locks at [tools/skill_lock.py:1](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/skill_lock.py#L1) |
| Browser | HTML simulation (B53), not browser automation | Real CDP/browser-harness execution at [tools/browser_use_cli.py:1046](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/browser_use_cli.py#L1046) |
| Vision | Metadata/canned digest (B51) | Native image embeddings and auxiliary model paths at [tools/vision_tools.py:1023](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/vision_tools.py#L1023) |
| Voice | STT stub; OS-limited native TTS | Registered TTS with multiple providers at [tools/tts_tool.py:687](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/tts_tool.py#L687), [tools/tts_tool_providers.py:196](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/tts_tool_providers.py#L196), platform voice intake |
| Image/video generation | No equivalent wired capability found | Registered generation tools and toolset entries at [tools/image_generation_tool.py:1063](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/image_generation_tool.py#L1063), [tools/video_generation_tool.py](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/video_generation_tool.py), [toolsets.py:84](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/toolsets.py#L84) |
| Chat gateways | Telegram real; Discord ingress incomplete | Broad platform-plugin system; sampled Telegram ingress at [plugins/platforms/telegram/adapter.py:6622](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/plugins/platforms/telegram/adapter.py#L6622), media/flood handling |
| Web search | Three actual providers | Wider provider registry at [tools/web_tools.py:103](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/web_tools.py#L103) |
| LSP | Real model-facing tools | Also edit-time lint/diagnostics wiring at [tools/file_operations_lint.py:338](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/file_operations_lint.py#L338) |
| Proof / skill regression hooks | Deterministic verification hooks and task proof bundle are strengths | No equivalent sampled SHA-approved deterministic skill-regression-hook contract; ordinary skill effects rely on normal tool/terminal policy |

Other lower-impact skill issues: substring selection can over-match skills (`agent/orchestrator.py:1792`), while an unapproved workspace skill can hard-fail verification (`skill_engine/executor.py:281–285`). The latter is a fail-closed policy choice, not automatically a bug; improve discovery/approval UX rather than bypass the safety gate.

**Fairness / Hermes concern:** legacy MCP entries without `trust` retain compatibility “full” trust at [tools/mcp_tool_handlers.py:163](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/benchmarks/hermes-agent/tools/mcp_tool_handlers.py#L163). That is weaker than Hex's designed scan gate, although Hex's gate is not yet wired into normal MCP startup. It is a default-policy concern, not a reproduced compromise. Hermes integration breadth also carries auth, plugin, subprocess, and transport complexity that must remain scoped and tested.

**Tests / acceptance:** Hex has integration, skill scanning, MCP library, and LSP tests, but [tests/capabilities/test_browser_and_vision.py:151](file:///C:/Users/dasan/OneDrive/Documents/Hex-Agent/tests/capabilities/test_browser_and_vision.py#L151), `:186,205` accept canned visual summaries and HTML “screenshots.” Those tests validate a simulation rather than the advertised feature. Add real-page form/JS/screenshot tests, denied `file://` reads, pixel-dependent image questions, explicit unavailable-provider errors, microphone/Windows voice paths, real Discord ingress, and startup-configured MCP discovery. Hermes's tests cover more trust/error/lifecycle interactions, but no live external integration was exercised here.

## 12. Memory, context compression, reflection, learning, and persona

**Status: outstanding review — no conclusions claimed.**

The dedicated reviewer failed before returning findings. This compilation does not invent a quality rating or infer runtime integration from filenames. The todo/compression and transcript/resume observations above are scoped findings from completed planning/persistence reviews, not a full cognition assessment.

A follow-up should verify actual call sites for memory retrieval/injection, scope separation, memory write policy, compressor thresholds and durability, tool-call pairing after compression, reflection/skill synthesis reachability, repetition detection, persona loading, and prompt-cache behavior. Hermes's numerous compression/memory module families must likewise be traced to live entry points before crediting them. No feature-parity conclusion for this area is warranted yet.

## 13. Cross-cutting quality and test strategy

### Where Hex is genuinely well engineered

1. **Task evidence:** hash-chained ledger, explicit transitions, checkpoints, pinned blast-radius verification, proof artifacts.
2. **Distributed-task primitives:** CAS claims/fencing and tested dual-driver concurrency contracts.
3. **Native client foundations:** typed shared wire models, real-server fixtures, bounded transcript/rendering machinery, honest startup failures.
4. **Focused integrations:** real Telegram, web search, Git automation, LSP, and skill verification hooks.

### Where fit/finish hides incomplete implementation

1. **UI state is mistaken for backend state:** desktop model picker, approval enums/TTL, TUI model labels, success notices before RPC acknowledgement.
2. **Capability names exceed effects:** subagents do not use tools, vision does not see pixels, browser does not run a browser, STT does not transcribe.
3. **Recovery mechanisms are composed without a common version contract:** journal replay versus rolled-back tree, displayed history versus runtime context, terminal task versus reentry.
4. **Platform support is inferred from source presence:** Unix shell/socket/interpreter assumptions survive in Windows-advertised flows.
5. **Tests approve placeholders or mocks:** role-table assertions do not enforce child permission; echo approval does not verify server semantics; HTML output does not verify a screenshot.

### Lessons to borrow from Hermes, without borrowing its entire architecture

- A single mandatory authorization boundary and trusted child scope.
- Typed execution outcomes distinguishing failure, interruption, narration, and verified completion.
- Explicit interrupt ownership and cleanup for parent, child, tool wait, approval wait, and detached work.
- Durable transcript/tool identity and replay repair, separate from the engineering-task ledger.
- Config-driven feature registration and capability checks: a library is not a shipped feature.
- Behavior tests against actual entry points, OS-native execution, and real wire schema.
- Packaging/reconnect/retention as part of feature delivery, not post-polish chores.

Hermes should not be copied indiscriminately: large entry points, parallel compatibility stacks, prompt-only planning, process-local background work, local terminal authority, and permissive legacy MCP defaults are cautionary examples.

## 14. Recommended remediation order and acceptance gates

### Phase 1 — Protect user work and authority

- **B01:** conflict-aware delta publication, metadata preservation, recoverability.
- **B02/B04/B45/B46/B52:** uniform gateway dispatch, explicit trusted-local versus confined modes, narrowly bound approvals, explicit POST decisions, browser path authority.
- **B12/B47/B48/B49:** effective cancellation with approval wake-up and quiescence before rollback.

**Gate:** denied/canceled tasks cause zero later effects; sibling access fails in claimed confined modes; successful publication preserves metadata/concurrent work or stops on conflict. Run this through standard, ReAct, ACP, CLI, TUI, and desktop entry points.

### Phase 2 — Make completion and recovery truthful

- **B03/B10/B11/B51/B53/B55:** fail on unavailable execution/inference; implement actual child/browser/vision/voice behavior or label narrower capabilities.
- **B06/B07/B09/B36/B37/B41/B44:** validated DAGs, correct todo action, budget attention states, tree-consistent replay, schema parity, structured context restore, terminal-task safeguards.
- **B14/B15:** actual proposer routing and enforcement of lost worker authority.

**Gate:** no successful implementation claim without the required effects/evidence; crash-resume reproduces the intended tree and transcript; clean SQLite and PostgreSQL installations support all current feature tables.

### Phase 3 — Finish primary user surfaces

- TUI input/paste/Unicode, final-answer association, resume handshake, approval visibility/RPC recovery.
- Desktop typed approval/TTL/model outcomes, lossless final text, queued modals, supervised reconnect, persistence, authenticated transports, supported OS builds and packages.
- Standard ACP interop or explicit renaming of the private protocol; actionable diagnostics.

**Gate:** exercise real native terminal/desktop golden paths and edge cases on supported platforms, including backend crash and reconnect. Source tests alone do not establish UI readiness.

### Phase 4 — Expand from real lifecycle integrations

- Locked scheduler/claims, correct platform daemon/timer delivery, catch-up/non-overlap policy.
- Config-driven MCP loading/discovery, real Discord ingress, wired skill retrieval.
- Finish memory/context/cognition review before making comparative claims or changing those subsystems.

**Gate:** startup-configured features are discoverable and callable through the same policy boundary, scheduled work survives defined crash/retry cases, and unsupported integrations fail explicitly rather than simulate success.

## Validation receipts retained from the original review

These receipts are inherited from the attached original report; they were **not rerun during compilation**. All filesystem probes used disposable `tempfile.TemporaryDirectory` data and actual checked-out modules loaded via `importlib.util.spec_from_file_location`. Neither repository's working files were used as probe targets.

| Probe | Method | Observed outcome |
|---|---|---|
| Publication preserves source metadata | Actual `APFSAndPortableCoWProvider`, snapshot → attempt → publish | Failed: `.git`, `.hex`, `.venv`, and newer user file removed |
| Restricted workspace confines child writes | Actual `RestrictedSubprocessSandbox(allowed_root=workspace)` | Failed: sibling sentinel changed, no violation reported |
| ReAct respects gateway policy | Exact `_execute_tool_call_inner`, inert denied gateway | Failed: registered handler executed directly |
| Provider outage yields failure | Exact `_react_model_caller`, provider raises | Failed: fabricated `<finish>` returned |
| Failed observation prevents fallback success | Same method, failed test observation | Failed: successful-completion text returned |
| Native Windows compound command | Exact `_tool_terminal`, actual portable backend | Failed: `/bin/sh` not found |
| Missing pytest falsely reports test success | Exact `_tool_run_tests`, real recipe detection, intentionally failing pytest-style function | **Not reproduced on Python 3.15.1:** unittest ran zero tests and exited 5, yielding `ok: false`. Do not count this as a confirmed bug |

Extracted-method probes ran with `python -B`, standard-library AST compilation and inert collaborators; unrelated imports, provider calls, and application boot were not exercised. Convert the confirmed findings into normal integration tests in a configured environment before accepting fixes.

## Overall conclusion

Hex has a credible, differentiated verified-engineering core, but this revision is not uniformly production-ready across its execution paths or clients. Hermes's main advantage is not simply having more features: its sampled capabilities are more often wired through a complete authorization, interruption, persistence, discovery, and delivery lifecycle. Hex should close those lifecycle and truthfulness gaps while retaining its more compact ledger/proof design; memory/context/cognition remains an explicit follow-up, not an assumed completed comparison.

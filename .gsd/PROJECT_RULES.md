# GSD Project Rules — HEX Autonomous AI Workbench

> **Single Source of Truth** for Get Shit Done methodology within this repository.

---

## Core Protocol: SPEC → PLAN → EXECUTE → VERIFY → COMMIT → DOCUMENT

1. **SPEC**: Define requirements in `.gsd/SPEC.md` until status is `FINALIZED`.
2. **PLAN**: Decompose into phases in `.gsd/ROADMAP.md`, then detailed atomic plans in `.gsd/phases/XX/PLAN.md`.
3. **EXECUTE**: Implement with atomic commits per task (maximum 2-3 tasks per plan).
4. **VERIFY**: Prove completion with empirical evidence (test outputs, command logs, proof bundles).
5. **COMMIT**: One task = one commit, message format: `type(scope): description`.
6. **DOCUMENT**: Update `README.md` after each phase to keep project documentation, usage commands, architecture tables, and verification metrics synchronized with shipping code.

**Planning Lock**: No implementation code until SPEC.md contains "Status: FINALIZED".

---

## Proof Requirements

Every change requires verification evidence:

| Change Type | Required Proof |
|-------------|----------------|
| API endpoint | curl / HTTP response |
| UI change | Screenshot / DOM inspection / TUI render output |
| Build / compile | Command exit code & build log |
| Test suite | Test runner output (`unittest` / `pytest`) |
| Config / Env | Verification command or loader test |

**Never accept**: "It looks correct", "This should work", "I've done similar before".  
**Always require**: Captured output, test result, or cryptographic proof bundle.

---

## Delegation Protocol

Where `invoke_subagent` exists (Antigravity 2.0+), heavy work runs in dedicated subagents:

| Work | Subagent |
|------|----------|
| Plan authoring | `gsd-planner` |
| Plan execution | `gsd-executor` (one per PLAN.md) |
| Phase verification | `gsd-verifier` |
| Mapping and research | `gsd-researcher` |
| Bug diagnosis | `gsd-debugger` |

- A subagent inherits no conversation history — include all needed facts in prompt.
- Pass paths, never full file contents.
- Never re-read an artifact a subagent already summarized.

---

## Atomic Execution & Wave Structure

- **2-3 tasks per plan max. No exceptions.**
- Each plan must complete within ~50% of an executor context window.
- Every task carries an executable `<verify>` block and a `<done>` criterion.
- Commit conventions: `type(scope): description` (e.g., `feat(setup): add terminal ui selector`).
- Confirm each commit with `git log -1 --oneline`. Never use `--allow-empty`.
- Shell discipline: One command per invocation. Never chain with `&&` or `||`.

---

## Documentation Parity

- **Update README.md after each phase**: Document new CLI commands, tools, configuration flags, and architecture changes as part of the phase closing ceremony.
- Update `.gsd/STATE.md` and `.gsd/ROADMAP.md` after every task and wave.

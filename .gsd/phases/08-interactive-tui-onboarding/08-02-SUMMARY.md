# Phase 08 Plan 02 Summary: One-Key Keyboard Approvals, Interactive Sandbox Shell & `hex init` Polish

**Phase:** 08 — Interactive Terminal UI (`hex chat`) & Onboarding Polish  
**Plan:** 08-02 (Wave 2)  
**Status:** Complete  
**Commits:** `df28736`, `b9dfea4`, `44d2459`  
**Tests:** 225/225 passed (15 new tests in `tests/test_tui_approvals_and_init.py`, 0 regressions)

---

## 1. Accomplishments

1. **One-Key Keyboard Approvals & Sandbox Shell (`src/harness_core/tui.py`):**
   - Implemented `handle_approval_prompt` in `HexConsole` to intercept `ApprovalRequiredInterrupt` when privileged R3/R4 actions (such as `git_push`, remote command execution, or destructive operations) require operator intervention.
   - Designed a formatted ANSI alert panel presenting Approval ID, Task ID, Step ID, Risk Class badge (`[R0]` through `[R4]`), Blast Radius Summary, and secret-scrubbed action parameters via `redact_secrets`.
   - Built single-key interactive prompt loop supporting:
     - `[y]` / `approve`: records `APPROVED` in `LedgerStore` via `decide_approval` and automatically resumes task execution via `orchestrator.resume_task`.
     - `[n]` / `reject`: prompts operator for optional rejection comment and records `REJECTED` in `LedgerStore`.
     - `[d]` / `diff`: looks up active attempt workspace and base checkpoint snapshot, calculates dirty file diff via `cow.compute_workspace_diff`, and renders formatted colorized unified diff via `render_unified_diff`.
     - `[s]` / `shell`: locates isolated CoW attempt directory and spawns an interactive subshell (or custom test runner) allowing manual operator inspection inside the sandbox.
     - `[q]` / `abort`: cancels the task via `orchestrator.cancel_task` and rolls back uncommitted CoW changes cleanly.
   - Handled non-interactive / EOF fallbacks safely by canceling the task without unhandled exceptions.
   - Wrapped prompt execution in `handle_user_prompt` with approval intercept and resumption.

2. **Repository Stack Detection & Project Scaffolding (`src/harness_core/onboard.py`, `src/harness_core/cli.py`):**
   - Implemented `detect_project_stack` in `src/harness_core/onboard.py` auto-detecting canonical markers for:
     - Python: `pyproject.toml`, `setup.py`, `requirements.txt`, `pytest.ini` (`pytest`, `.py`).
     - Node / TypeScript: `package.json`, `tsconfig.json` (`npm test`, `.js`, `.ts`, `.jsx`, `.tsx`).
     - Rust: `Cargo.toml` (`cargo test`, `.rs`).
     - Go: `go.mod` (`go test ./...`, `.go`).
     - Generic fallback: `make test`.
   - Implemented `init_project` generating project-scoped `.hex/config.yaml` and `.hex/.gitignore` tailored to the detected stack. Preserves existing configuration unless `--force` is specified.
   - Enhanced `hex init` in `src/harness_core/cli.py` with `--workspace` and `--force` flags, and added `--data-dir` support to the subparser. Outputs structured JSON with project metadata and seeded skills.

3. **Database Ledger Support (`src/harness_core/db.py`):**
   - Enhanced `create_approval` and `create_approval_async` to accept `risk_class` and keyword arguments.
   - Added `get_approval` and `get_approval_async` methods to `LedgerStore` with dual-driver SQLite and PostgreSQL support.

4. **Unit & Integration Test Suite (`tests/test_tui_approvals_and_init.py`):**
   - Authored 15 tests covering all approval keyboard flows (`[y]`, `[n]`, `[d]`, `[s]`, `[q]`), EOF handling, stack detection across all supported languages, project file creation, `--force` overwrite behavior, and CLI `hex init` execution.

---

## 2. Verification Evidence

### Task 1 Verify Command:
```bash
.venv/bin/python -c "from harness_core.tui import HexConsole; from harness_core.orchestrator import HarnessOrchestrator; from harness_core.gateway import ApprovalRequiredInterrupt; orch = HarnessOrchestrator(':memory:'); t = orch.ledger.submit_task({'goal': 'test_apr'}); apr = orch.ledger.create_approval(task_id=t['task_id'], attempt_id='att1', step_id='s1', action_type='git_commit', action_params={'message': 'test'}, blast_radius_summary='commit changes', risk_class='R3'); console = HexConsole(orch); res = console.handle_approval_prompt(ApprovalRequiredInterrupt(apr), prompt_fn=lambda _: 'y'); assert res['decision'] == 'APPROVED'; rec = orch.ledger.get_approval(apr['approval_id']); assert rec['decision'] == 'APPROVED'; print('KEYBOARD_APPROVAL_OK')"
```
**Output:**
```
🚨 Approval Required: git_commit (R3)
──────────────────────────────────────────────────────────────────────
  Approval ID:   apr_3211a4d82e80
  Task ID:       tsk_1a03fc06fa2f
  Step ID:       s1
  Risk Class:    [R3 - High Risk]
  Blast Radius:  commit changes
  Parameters:
    {
      "message": "test"
    }
──────────────────────────────────────────────────────────────────────
  Options: [y] Approve   [n] Reject   [d] Full Diff   [s] Sandbox Shell   [q] Abort

✓ Approval recorded. Resuming task execution...
KEYBOARD_APPROVAL_OK
```

### Task 2 Verify Command:
```bash
.venv/bin/python -c "import tempfile, json; from pathlib import Path; from harness_core.onboard import detect_project_stack, init_project; d = Path(tempfile.mkdtemp()); (d / 'Cargo.toml').write_text('[package]\nname=\"foo\"\n'); stack = detect_project_stack(d); assert stack['stack'] == 'rust' and stack['test_cmd'] == 'cargo test'; res = init_project(d); assert (d / '.hex' / 'config.yaml').is_file(); assert (d / '.hex' / '.gitignore').is_file(); res2 = init_project(d, force=False); assert res2['created'] is False; print('PROJECT_INIT_OK')"
```
**Output:**
```
PROJECT_INIT_OK
```

### Task 3 Verify Command:
```bash
.venv/bin/python -m unittest tests/test_tui_approvals_and_init.py
```
**Output:**
```
Ran 15 tests in 0.186s
OK
```

### Full Repository Regression Verification:
```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```
**Output:**
```
Ran 225 tests in 19.824s
OK
```

---

## 3. Deviations Handled Automatically

1. **`risk_class` in `create_approval` & `get_approval` (`src/harness_core/db.py`):**
   - Added `risk_class` and `**extra` keyword argument handling to `create_approval` / `create_approval_async`, and added `get_approval` and `get_approval_async` to `LedgerStore` to satisfy contract expectations during approval retrieval.
2. **`--data-dir` on `hex init` subparser (`src/harness_core/cli.py`):**
   - Added `--data-dir` with `default=argparse.SUPPRESS` to `p_init` to allow `hex init --workspace <dir> --data-dir :memory:` to parse seamlessly when `--data-dir` is positioned after the subcommand.

---

## 4. Commits

- `df28736`: `feat(phase-08): implement one-key keyboard approvals and sandbox shell in HexConsole`
- `b9dfea4`: `feat(phase-08): implement repository stack detection and polish hex init`
- `44d2459`: `feat(phase-08): author unit and integration test suite for approvals, subshell, and project init`

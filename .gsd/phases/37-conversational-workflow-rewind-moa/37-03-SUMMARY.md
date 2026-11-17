# Phase 37 Plan 03 Summary: Temporal Approval Leasing & Mobile Reaction Bridge

## 1. Overview & Objectives

Phase 37 Plan 03 implements two high-leverage operator velocity systems:
1. **Temporal Approval Leasing (`approval_lease.py`)**: Grants time-bounded auto-approval leases for safe command prefixes (e.g. `pytest`, `cargo test`) and working directories, eliminating repetitive interactive approval prompts while strictly enforcing TTL expiration, explicit revocation, and ledger persistence.
2. **Asynchronous Mobile Reaction Listener Bridge (`reaction_listener.py`)**: Connects conversational bot gateways (Telegram and Discord) to the tool approval flow, allowing remote operators to approve or deny privileged actions via inline keyboard buttons or reaction emojis (`👍`/`👎`), with a safe fallback timeout mechanism that prevents hanging.

---

## 2. Tasks Completed

| Task | Name | Outcome | Commits |
|---|---|---|---|
| **Task 1** | Implement Temporal Approval Leasing Engine | Implemented `ApprovalLease` dataclass and `ApprovalLeaseManager` with command/path normalization, prefix matching, TTL expiration, and ledger persistence. Wired into `ToolGateway`, `ReActLoop`, and `HexConsole` (`/lease` slash command). | `14af192` |
| **Task 2** | Implement Asynchronous Mobile Reaction Listener Bridge | Created `ApprovalRequest`, `ApprovalDecision`, and `MobileApprovalBridge`. Reorganized bot gateways into `src/harness_core/capabilities/gateways/`, integrated button callbacks and reaction emojis (`👍`/`👎`), wired into `ToolGateway` with remote operator resolution and timeout fallback. | `ed3f4f0` |

---

## 3. Implementation Details

### Temporal Approval Leasing (`approval_lease.py`)
- **`ApprovalLease`**: Stores `lease_id`, `task_id`, `command_prefix`, `allowed_paths`, `granted_at`, `ttl_seconds`, `expires_at`, `scope`, and `is_revoked`.
- **`ApprovalLeaseManager`**:
  - `grant_lease`: Creates time-bounded leases, registers in-memory, and persists to `approval_leases` table in `LedgerStore`.
  - `check_lease`: Normalizes command line invocations (extracting from `command`, `cmd`, `CommandLine`, `args`, or `script`) and target paths, validating boundary-safe prefixes and subpath containment.
  - `revoke_lease` & `cleanup_expired`: Marks revoked leases and purges expired entries from both memory and SQLite/PostgreSQL ledger tables.
- **`ReActLoop` & `ToolGateway`**:
  - Automatically recognizes matching active leases for tool calls.
  - Grants auto-approval with `policy_decision = "LEASE_APPROVED"`, executing with 0ms delay without blocking for human input.
- **`HexConsole`**:
  - Added `/lease` command suite: `/lease list`, `/lease grant <prefix> [ttl]`, `/lease revoke <id>`, and `/lease cleanup`.

### Asynchronous Mobile Reaction Listener Bridge (`reaction_listener.py`)
- **`MobileApprovalBridge`**:
  - `request_approval` & `request_approval_async`: Dispatches rich notification cards to configured Telegram and Discord channels, creates thread-safe resolution events, and awaits operator response up to configurable timeout (`HEX_APPROVAL_TIMEOUT`, default 60s).
  - `resolve_approval`: Validates responder identity against `allowed_user_ids` whitelist, records decision provenance, sets completion event, and updates remote message cards in-place with outcome badges.
  - `handle_reaction`: Intercepts mobile reaction emojis (e.g. `👍` / `+1` / `✅` to approve; `👎` / `-1` / `❌` to deny) and maps them directly to pending approval requests.
  - `handle_callback`: Intercepts inline button callback queries (`approve:<req_id>`, `deny:<req_id>`).
- **`ToolGateway` Approval Wiring**:
  - Privileged R3 operations dispatch `ApprovalRequest` to `MobileApprovalBridge`.
  - `APPROVE` decision automatically marks approval as `APPROVED` in the ledger and continues execution seamlessly.
  - `DENY` decision records `REJECTED` in the ledger and halts tool execution with `PolicyBlockedError`.
  - `TIMEOUT` decision safely falls back to standard local CLI `ApprovalRequiredInterrupt` without deadlock.

---

## 4. Verification Evidence

### Pytest Verification Suite
```
.venv/bin/pytest tests/core/test_approval_lease.py tests/capabilities/test_mobile_reaction_listener.py -v
============================= test session starts ==============================
collected 20 items

tests/core/test_approval_lease.py::test_approval_lease_dataclass_defaults_and_validation PASSED [  5%]
tests/core/test_approval_lease.py::test_approval_lease_manager_grant_and_list PASSED [ 10%]
tests/core/test_approval_lease.py::test_command_prefix_matching PASSED        [ 15%]
tests/core/test_approval_lease.py::test_multi_word_prefix_matching PASSED     [ 20%]
tests/core/test_approval_lease.py::test_path_based_lease_matching PASSED      [ 25%]
tests/core/test_approval_lease.py::test_lease_expiration_behavior PASSED      [ 30%]
tests/core/test_approval_lease.py::test_explicit_lease_revocation PASSED      [ 35%]
tests/core/test_approval_lease.py::test_ledger_store_approval_lease_persistence PASSED [ 40%]
tests/core/test_approval_lease.py::test_react_loop_auto_approval_with_active_lease PASSED [ 45%]
tests/core/test_approval_lease.py::test_tool_gateway_policy_evaluation_with_lease PASSED [ 50%]
tests/core/test_approval_lease.py::test_hex_console_slash_lease PASSED        [ 55%]
tests/capabilities/test_mobile_reaction_listener.py::test_approval_request_and_decision_models PASSED [ 60%]
tests/capabilities/test_mobile_reaction_listener.py::test_mobile_approval_bridge_resolve_and_unblock PASSED [ 65%]
tests/capabilities/test_mobile_reaction_listener.py::test_mobile_approval_bridge_denial PASSED [ 70%]
tests/capabilities/test_mobile_reaction_listener.py::test_mobile_approval_bridge_timeout_fallback PASSED [ 75%]
tests/capabilities/test_mobile_reaction_listener.py::test_telegram_callback_and_reaction_integration PASSED [ 80%]
tests/capabilities/test_mobile_reaction_listener.py::test_discord_interaction_and_reaction_integration PASSED [ 85%]
tests/capabilities/test_mobile_reaction_listener.py::test_tool_gateway_mobile_bridge_approval_execution PASSED [ 90%]
tests/capabilities/test_mobile_reaction_listener.py::test_tool_gateway_mobile_bridge_denial_halts_execution PASSED [ 95%]
tests/capabilities/test_mobile_reaction_listener.py::test_tool_gateway_mobile_bridge_timeout_fallback_to_interrupt PASSED [100%]

============================== 20 passed in 2.70s ==============================
```

### Gateway Regression Verification Suite
```
.venv/bin/pytest tests/capabilities/test_telegram_gateway.py tests/capabilities/test_discord_and_gateway_manager.py
============================== 38 passed in 17.52s =============================
```

### Rust Workspace Verification Suite
```
cargo test --workspace
...
test result: ok. 13 passed (hex_protocol)
test result: ok. 1 passed (live_acp_test)
test result: ok. 3 passed (protocol_test)
test result: ok. 16 passed (hex_tui lib)
test result: ok. 3 passed (test_context_window)
test result: ok. 8 passed (test_design_system)
test result: ok. 4 passed (test_screens_overlays_and_errors)
test result: ok. 5 passed (test_screens_start_and_session)
test result: ok. 4 passed (test_session_management_and_resume)
test result: ok. 8 passed (test_ui)
test result: ok. 2 passed (test_virtual_render)
test result: ok. 9 passed (ui_rendering_test)
All 68 workspace tests passed cleanly.
```

---

## 5. Artifacts Produced & Modified

- `src/harness_core/core/approval_lease.py`: Core leasing engine (`ApprovalLease`, `ApprovalLeaseManager`).
- `src/harness_core/core/__init__.py`: Exported symbols and lazy module mapping.
- `src/harness_core/storage/db.py`: `approval_leases` table schema, persistence, and audit logging.
- `migrations/0001_initial_schema.sql` & `src/harness_core/migrations/0001_initial_schema.sql`: Initial schema migrations.
- `src/harness_core/core/react_loop.py`: Auto-approval lease checks and `_execute_tool` alias.
- `src/harness_core/gateway.py`: Policy evaluation with `LEASE_APPROVED`, remote bridge integration.
- `src/harness_core/client/tui.py`: `/lease` interactive slash command suite.
- `src/harness_core/capabilities/gateways/reaction_listener.py`: Remote approval listener bridge.
- `src/harness_core/capabilities/gateways/__init__.py`, `telegram_bot.py`, `discord_bot.py`, `manager.py`: Gateway domain subpackage.
- `src/harness_core/capabilities/__init__.py`: Capabilities package re-exports.
- `src/harness_core/gateways/*`: Backward-compatibility stubs.
- `tests/core/test_approval_lease.py`: Unit and integration test suite (11 tests).
- `tests/capabilities/test_mobile_reaction_listener.py`: Integration test suite (9 tests).

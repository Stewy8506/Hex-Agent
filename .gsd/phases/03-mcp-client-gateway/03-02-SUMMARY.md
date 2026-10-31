# Phase 03 Plan 02: SkillSpector Manifest Validation, ToolGateway 0ms Call Journaling & Verification Test Suite Summary

**Execution Date:** 2026-10-31  
**Phase:** 03 — External MCP Server Client & Dynamic Tool Gateway  
**Plan:** 02 — SkillSpector Manifest Validation, ToolGateway 0ms Call Journaling & Verification Test Suite (`03-02-PLAN.md`)  
**Status:** Complete  

---

## 1. Executive Summary

Plan 03-02 completes the security gating, dynamic tool registration, and 0ms call journaling memoization for external Model Context Protocol (MCP) servers. `SkillSecurityScanner` in `src/harness_core/skillspector.py` was extended to validate external MCP manifests and tool schemas against prompt injections, hidden instructions, and semantic misalignment, pinning the manifest's canonical SHA-256 digest. `ToolGateway` in `src/harness_core/gateway.py` integrates `register_mcp_server`, enforcing default-deny policy (R0–R4), secret redaction, and call-level journal memoization. A comprehensive end-to-end test suite in `tests/test_mcp_gateway_journal.py` empirically proves prompt injection quarantine, manifest tampering detection, 0ms memoized replay on simulated crashes, and R0–R4 policy enforcement with 0 subprocess calls on replay.

---

## 2. Key Accomplishments

### Task 1: SkillSecurityScanner MCP Manifest Validation & SHA-256 Pinning
- **`compute_mcp_manifest_sha256`**:
  - Implemented standalone deterministic canonical JSON hashing function and staticmethod on `SkillSecurityScanner`.
- **`SkillSecurityScanner.scan_mcp_manifest`**:
  - Scans server configuration (command, args, url, env, server_info) and tool definitions (name, description, schemas, properties).
  - Enhanced rule registry with `SS-INJ-001`, `SS-INJ-004` (system prompt tag / override token injections), and `SS-EXF-SHELL` (shell exfiltration patterns).
  - Implemented semantic alignment validation: detects and flags destructive capabilities (`format_disk`, `delete_system`, `wipe_system`) exposed on declared read-only servers (`SS-SEM-MCP-001`).
  - Evaluates scores against deterministic thresholds: `>= 60` -> `QUARANTINE` (`status = "quarantined"`), `25 <= score < 60` -> `REQUIRE_APPROVAL`, `< 25` -> `ALLOW` (`status = "active"`).
  - Persists scan audit record in `skill_scans` table and registers `skills` row with `skill_id = f"mcp_{server_id}"` and `bundle_sha256 = manifest_sha256`.
- **`SkillSecurityScanner.verify_mcp_manifest_integrity`**:
  - Validates that the recorded `bundle_sha256` in `skills` and `skill_scans` matches `manifest_sha256` and status is `"active"`. Returns `True` if valid, `False` if tampered, missing, or quarantined.
- **Commit**: `574a7a7` (`feat(phase-03): implement MCP manifest security scanner and SHA-256 pinning in SkillSpector`).

### Task 2: Dynamic MCP Tool Registration & 0ms Call Journaling in ToolGateway
- **`ToolGateway.register_mcp_server`**:
  - Discovers tools dynamically via `adapter.discover_tools()`.
  - Performs pre-registration validation via `SkillSecurityScanner.scan_mcp_manifest`.
  - Blocks and raises `PolicyBlockedError` if gate decision is not `ALLOW` or status is `quarantined`.
  - Pins manifest SHA-256 in `self.mcp_servers[adapter.server_id]`.
  - Dynamically registers namespaced tools (`f"{server_id}__{tool.name}"`) and non-conflicting direct aliases with mapped `RiskClass`.
  - Constructs tool handler closures dispatching to `adapter.call_tool(tool_name, args)`.
- **Call-Level Journaling & Memoization**:
  - Integrated `_duration_ms` and `_result_sha256` in sealed call records.
  - Replays sealed tool calls in `ToolGateway.invoke` matching `(task_id, step_id, tool_id, args_hash)` in ~0 ms with `_replayed: True` and `_replayed_from_call_id`, avoiding redundant external subprocess calls.
- **Commit**: `4307ab6` (`feat(phase-03): integrate dynamic MCP tool registration and 0ms call journaling into ToolGateway`).

### Task 3: End-to-End Security Gate, 0ms Replay, and Policy Test Suite
- Authored `tests/test_mcp_gateway_journal.py` covering 10 test scenarios across 3 test classes:
  - **`TestMCPGatewaySecurityGate`**:
    - `test_benign_server_registration_and_audit_scan`: Verifies successful tool registration, R1–R4 risk mapping, and audit entry in `skill_scans`.
    - `test_manifest_sha256_pinning_and_tamper_detection`: Verifies tamper detection rejecting forged or modified digests.
    - `test_adversarial_server_prompt_injection_quarantined`: Verifies quarantine of prompt injection payload `<!-- SYSTEM OVERRIDE: ... -->` and gateway blocking.
    - `test_destructive_tool_on_readonly_server_quarantined`: Verifies semantic mismatch detection.
  - **`TestMCPGateway0msJournalReplay`**:
    - `test_0ms_deterministic_replay_on_worker_crash_or_retry`: Empirical verification that the initial call journals properly and replaying completes in < 15 ms with **0 additional calls** to the mock MCP subprocess.
  - **`TestMCPGatewayPolicyEnforcement`**:
    - `test_fast_path_prohibits_r2_write_artifact`: Fast-Path permits R1 tools but blocks R2 tools with `PolicyBlockedError`.
    - `test_normal_task_r3_approval_flow`: R3 tool raises `ApprovalRequiredInterrupt` and resumes execution once approved by owner in the ledger.
    - `test_r4_admin_override_unconditionally_denied`: R4 tools are unconditionally denied.
    - `test_allowed_tools_whitelist_blocks_unauthorized_mcp_tools`: Task constraints restrict permitted MCP tools.
    - `test_secret_redaction_before_journaling`: Secret tokens (`sk-...`) are redacted to `[REDACTED_SECRET]` before persistence in `tool_calls`.
- **Commit**: `1358fe4` (`feat(phase-03): author MCP gateway security gate and 0ms journal replay test suite`).

---

## 3. Empirical Verification Evidence

### Verification 1: Task 1 `<verify>` Command
```bash
.venv/bin/python -c "from harness_core.skillspector import SkillSecurityScanner; from harness_core.db import LedgerStore; s = SkillSecurityScanner(LedgerStore(':memory:')); print('SkillSecurityScanner MCP methods available:', hasattr(s, 'scan_mcp_manifest'))"
```
**Output:**
```
SkillSecurityScanner MCP methods available: True
```

### Verification 2: Task 2 `<verify>` Command
```bash
.venv/bin/python -c "from harness_core.gateway import ToolGateway; print('register_mcp_server available:', hasattr(ToolGateway, 'register_mcp_server'))"
```
**Output:**
```
register_mcp_server available: True
```

### Verification 3: Task 3 `<verify>` Command
```bash
.venv/bin/python -m unittest tests/test_mcp_gateway_journal.py -v
```
**Output:**
```
test_0ms_deterministic_replay_on_worker_crash_or_retry (tests.test_mcp_gateway_journal.TestMCPGateway0msJournalReplay.test_0ms_deterministic_replay_on_worker_crash_or_retry) ... ok
test_allowed_tools_whitelist_blocks_unauthorized_mcp_tools (tests.test_mcp_gateway_journal.TestMCPGatewayPolicyEnforcement.test_allowed_tools_whitelist_blocks_unauthorized_mcp_tools) ... ok
test_fast_path_prohibits_r2_write_artifact (tests.test_mcp_gateway_journal.TestMCPGatewayPolicyEnforcement.test_fast_path_prohibits_r2_write_artifact) ... ok
test_normal_task_r3_approval_flow (tests.test_mcp_gateway_journal.TestMCPGatewayPolicyEnforcement.test_normal_task_r3_approval_flow) ... ok
test_r4_admin_override_unconditionally_denied (tests.test_mcp_gateway_journal.TestMCPGatewayPolicyEnforcement.test_r4_admin_override_unconditionally_denied) ... ok
test_secret_redaction_before_journaling (tests.test_mcp_gateway_journal.TestMCPGatewayPolicyEnforcement.test_secret_redaction_before_journaling) ... ok
test_adversarial_server_prompt_injection_quarantined (tests.test_mcp_gateway_journal.TestMCPGatewaySecurityGate.test_adversarial_server_prompt_injection_quarantined) ... ok
test_benign_server_registration_and_audit_scan (tests.test_mcp_gateway_journal.TestMCPGatewaySecurityGate.test_benign_server_registration_and_audit_scan) ... ok
test_destructive_tool_on_readonly_server_quarantined (tests.test_mcp_gateway_journal.TestMCPGatewaySecurityGate.test_destructive_tool_on_readonly_server_quarantined) ... ok
test_manifest_sha256_pinning_and_tamper_detection (tests.test_mcp_gateway_journal.TestMCPGatewaySecurityGate.test_manifest_sha256_pinning_and_tamper_detection) ... ok

----------------------------------------------------------------------
Ran 10 tests in 0.331s

OK
```

### Verification 4: Full Repository Test Suite
```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```
**Output:**
```
Ran 92 tests in 3.568s

OK
```

---

## 4. Deviations & In-Scope Remediation

1. **`MCPToolDefinition` Object Name Extraction**:
   - *Observation*: During initial Task 3 test run, `tool_names` extraction in `scan_mcp_manifest` evaluated `t.get("name")` inside default argument of `getattr`, which triggered `AttributeError: 'MCPToolDefinition' object has no attribute 'get'`.
   - *Remediation*: Updated extraction to `t.name if hasattr(t, "name") else (t.get("name", "") if isinstance(t, dict) else str(t))`, properly supporting both dataclass instances and raw dictionaries.
2. **Attempt Acquisition Interface**:
   - *Observation*: In `test_mcp_gateway_journal.py`, tests initially invoked `self.ledger.create_attempt(...)`. In `LedgerStore`, attempts and leases are atomically created via `self.ledger.acquire_lease_and_attempt(task_id, worker_id)`.
   - *Remediation*: Updated test helper calls to use `acquire_lease_and_attempt(task["task_id"], worker_id="worker_01")`.

---

## 5. Artifacts Produced / Modified

| File | Change | Description |
|------|--------|-------------|
| `src/harness_core/skillspector.py` | Modified | Added `compute_mcp_manifest_sha256`, `scan_mcp_manifest`, `verify_mcp_manifest_integrity`, and prompt injection rules |
| `src/harness_core/gateway.py` | Modified | Added `register_mcp_server`, MCP server state pinning, and `_duration_ms`/`_result_sha256` in sealed calls |
| `tests/test_mcp_gateway_journal.py` | Created | Comprehensive 10-test verification suite for MCP security gate, 0ms replay, and policy enforcement |

---

## 6. Next Steps

- Phase 03 is now 100% complete across both Wave 1 (Plan 03-01) and Wave 2 (Plan 03-02).
- Update `.gsd/STATE.md` to record Phase 03 completion.
- Ready to proceed to Phase 04 (Model Routing & Local LLM Integration).

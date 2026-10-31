# Phase 03 Plan 01: MCP Client Adapters (stdio & SSE) & Dynamic Tool Discovery with R0–R4 Risk Mapping Summary

**Execution Date:** 2026-10-31  
**Phase:** 03 — External MCP Server Client & Dynamic Tool Gateway  
**Plan:** 01 — MCP Client Adapters (stdio & SSE) & Dynamic Tool Discovery with R0–R4 Risk Mapping (`03-01-PLAN.md`)  
**Status:** Complete  

---

## 1. Executive Summary

Plan 03-01 establishes zero-dependency Model Context Protocol (MCP) JSON-RPC 2.0 client connectivity in `src/harness_core/mcp_client.py`. It implements `MCPClientAdapter`, `StdioMCPTransport`, `HttpSSEMCPTransport`, and dynamic `R0–R4` risk mapping with heuristic rules and per-server overrides. The client manages protocol handshake negotiation (`initialize` and `notifications/initialized`), dynamic tool listing (`tools/list`), input schema extraction, and tool invocation (`tools/call`) with execution timeouts and structured error handling. A mock MCP server and unit test suite were authored in `tests/test_mcp_client.py`, raising the total repository test suite to 82 tests, all passing with zero regressions.

---

## 2. Key Accomplishments

### Task 1: MCP Client Adapter, Stdio/SSE Transports, and Dynamic R0-R4 Risk Mapping
- **`MCPTransport` Protocol**:
  - Defines `send_request(method, params, timeout)`, `send_notification(method, params)`, and `close()` interface.
- **`StdioMCPTransport`**:
  - Launches MCP server subprocess with stdin/stdout/stderr pipes.
  - Implements thread-safe message correlation via monotonic integer request IDs, pending response events, and write locks.
  - Background daemon reader thread parses JSON-RPC 2.0 messages supporting both newline-delimited JSON and `Content-Length` header framing.
  - Rolling stderr buffer (`deque(maxlen=200)`) captures diagnostics for debugging and crash analysis.
  - Graceful teardown protocol (`close`): closes stdin, gracefully terminates child process with `SIGTERM`, waits up to 2.0s, and issues `kill` if process remains alive, closing stdout/stderr file descriptors and joining reader threads cleanly.
- **`HttpSSEMCPTransport`**:
  - Implements zero-dependency HTTP POST JSON-RPC 2.0 transport utilizing Python standard library `urllib.request`.
  - Handles response parsing, structured JSON-RPC error mapping, and notifications.
- **`classify_mcp_tool_risk`**:
  - Evaluates tool names and descriptions across prefix regexes and token sets.
  - Heuristic precedence:
    1. Per-server / caller overrides (`risk_overrides`).
    2. **R4 (Root / Destructive / Policy / Secret)**: `sudo`, `root`, `secret`, `auth`, `policy`, `revoke`, `grant`, `admin`, `eval`, `format_disk`.
    3. **R3 (Privileged / Execution / Shell / Network Outbound)**: `exec`, `shell`, `bash`, `cmd`, `command`, `publish`, `deploy`, `git_push`, `post`, `send_email`, `external_call`.
    4. **R2 (Workspace Write / Build / Mutate / Test)**: `write`, `create`, `update`, `modify`, `edit`, `append`, `patch`, `delete`, `remove`, `run_test`, `compile`, `build`.
    5. **R1 (Read-Only Inspection / Query / Search)**: `get`, `read`, `list`, `fetch`, `search`, `query`, `describe`, `view`, `find`, `check`, `inspect`.
    6. **Safe Default-Deny**: Unrecognized tools default to `RiskClass.R2` (never R0 or R1).
- **`MCPClientAdapter`**:
  - Orchestrates handshake initialization, storing server capabilities, version, and server info.
  - Discovers tools dynamically via `tools/list`, populating `MCPToolDefinition` instances with input schemas and classified risk classes.
  - Executes tools via `tools/call`, returning structured dictionary: `{"ok": bool, "output": str, "content": list, "raw": dict}`.
  - Produces manifest snapshot dictionaries via `get_manifest_data()` for SHA-256 manifest pinning and SkillSpector security validation.
- **Commit**: `837ebbe` (`feat(phase-03): implement MCPClientAdapter, stdio and SSE transports, and risk classifier`).

### Task 2: Mock MCP Server and Protocol Compliance Test Suite
- **Mock MCP Subprocess Server**:
  - Implemented portable Python script executing as a child subprocess via `sys.executable`.
  - Handles `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, and unknown methods (`-32601`).
  - Implements mock tools: `read_weather` (R1), `save_note` (R2), `exec_bash` (R3), `admin_wipe_system` (R4), `slow_delay` (timeout testing), and `failing_tool` (`isError: True`).
- **Test Suite (`tests/test_mcp_client.py`)**:
  - `test_dynamic_risk_classification`: Tests R1-R4 heuristics and default-deny fallback.
  - `test_risk_classification_overrides`: Tests caller overrides taking precedence over heuristics.
  - `test_stdio_handshake_and_tool_discovery`: Verifies handshake negotiation, capabilities extraction, tool definitions, schemas, and manifest structure.
  - `test_stdio_tool_execution`: Verifies success execution, tool failure (`isError: True`), and unknown method errors.
  - `test_stdio_timeout_and_graceful_teardown`: Verifies timeout enforcement and process termination without zombie processes.
  - `test_http_sse_transport_end_to_end`: Hermetically verifies HTTP transport request/response cycle, notification, and error handling.
- **Commit**: `b8ff3c1` (`feat(phase-03): author MCP protocol compliance and transport test suite`).

---

## 3. Empirical Verification Evidence

### Verification 1: Risk Mapping & Transport Module Imports
```bash
.venv/bin/python -c "from harness_core.mcp_client import MCPClientAdapter, StdioMCPTransport, classify_mcp_tool_risk; from harness_core.models import RiskClass; print('Risk mapping test:', classify_mcp_tool_risk('get_file_info', 'returns file metadata'))"
```
**Output:**
```
Risk mapping test: RiskClass.R1
```

### Verification 2: MCP Protocol Compliance Test Suite
```bash
python3 -m unittest tests/test_mcp_client.py -v
```
**Output:**
```
test_dynamic_risk_classification (tests.test_mcp_client.TestMCPClient.test_dynamic_risk_classification) ... ok
test_http_sse_transport_end_to_end (tests.test_mcp_client.TestMCPClient.test_http_sse_transport_end_to_end) ... ok
test_risk_classification_overrides (tests.test_mcp_client.TestMCPClient.test_risk_classification_overrides) ... ok
test_stdio_handshake_and_tool_discovery (tests.test_mcp_client.TestMCPClient.test_stdio_handshake_and_tool_discovery) ... ok
test_stdio_timeout_and_graceful_teardown (tests.test_mcp_client.TestMCPClient.test_stdio_timeout_and_graceful_teardown) ... ok
test_stdio_tool_execution (tests.test_mcp_client.TestMCPClient.test_stdio_tool_execution) ... ok

----------------------------------------------------------------------
Ran 6 tests in 0.289s

OK
```

### Verification 3: Full Repository Regression Test Suite
```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
```
**Output:**
```
Ran 82 tests in 3.196s

OK
```

---

## 4. Deviations & Remediation

1. **Hermetic HTTP Transport Mocking**:
   - *Observation*: Binding live TCP sockets (`HTTPServer`) during unit tests encounters sandbox permission restrictions (`Errno 1 Operation not permitted`) in macOS sandbox environments.
   - *Remediation*: Mocked `urllib.request.urlopen` with a dedicated request dispatcher in `test_http_sse_transport_end_to_end`, allowing fully hermetic verification of serialization, status codes, and JSON-RPC protocol error translation without network socket requirements.
2. **File Descriptor Resource Cleanup**:
   - *Observation*: Subprocess pipes for stdout and stderr emitted `ResourceWarning: unclosed file` if not explicitly closed before thread joining during test teardown.
   - *Remediation*: Hardened `StdioMCPTransport.close()` to close `stdout` and `stderr` handles explicitly and join reader threads within a bounded timeout.

---

## 5. Artifacts Produced / Modified

| File | Change | Description |
|------|--------|-------------|
| `src/harness_core/mcp_client.py` | Created | `MCPClientAdapter`, `StdioMCPTransport`, `HttpSSEMCPTransport`, `classify_mcp_tool_risk` |
| `tests/test_mcp_client.py` | Created | Mock MCP server and unit test suite verifying handshake, discovery, invocation, timeouts, and teardown |

---

## 6. Next Steps

- Proceed to Plan 03-02 (Wave 2):
  - Pre-validate MCP tool manifests using `SkillSecurityScanner` and SHA-256 pinning before gateway registration.
  - Implement `ToolGateway.register_mcp_server` and dynamic dispatch.
  - Implement 0ms call-level journal memoization replay on worker crashes.
  - Author end-to-end integration test suite in `tests/test_mcp_gateway_journal.py`.

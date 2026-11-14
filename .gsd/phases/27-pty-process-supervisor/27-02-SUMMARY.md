# Phase 27: Plan 02 Summary — Gateway Process Tools, ReAct Loop, TUI, and CLI Integration

## 1. Objective Achieved
Exposed the PTY Process Supervisor to autonomous agents and developers by registering four dedicated tools (`start_process`, `read_terminal`, `kill_process`, `list_processes`) in `ToolGateway` with default-deny policy gating (`R0` for non-mutating inspections, `R2` for process spawns/kills), integrating background process control into the continuous multi-turn `ReActLoop`, adding interactive `/ps` and `/processes` slash commands to `HexConsole` TUI, implementing the `hex ps` CLI subcommand, and verifying with a comprehensive integration test suite.

## 2. Key Changes & Deliverables

### A. ToolGateway Process Management Tools (`src/harness_core/gateway.py`)
- Registered 4 dedicated process supervisor tools in `TOOL_DEFINITIONS`:
  - `start_process`: Spawns a background command in an asynchronous pseudo-terminal (PTY) or pipe (`RiskClass.R2`). Confines `cwd` within workspace boundary via `_resolve_confined_path`. Scrubs secrets from commands and initial output slice using `redact_secrets`.
  - `read_terminal`: Reads incremental output slices (`offset`, `limit`) from ring buffer (`RiskClass.R0`). Scrubs secrets from returned stream text.
  - `kill_process`: Terminates active background processes by `process_id` via SIGTERM or SIGKILL (`RiskClass.R2`).
  - `list_processes`: Lists tracked processes sorted by start time, optionally filtered by status (`running`, `completed`, `failed`, `killed`, `all`) (`RiskClass.R0`).
- Initialized `self.process_registry` in `ToolGateway.__init__` with fallback to `get_process_registry()`.
- Updated legacy `_tool_process` to delegate to `self.process_registry` for backward compatibility while preserving protected PID checks.

### B. Continuous Multi-Turn ReAct Loop Engine (`src/harness_core/react_loop.py`)
- Updated error classification in `ReActLoop._execute_tool_call` so that completed or failed background process states (where `ok=True`) are not falsely flagged as tool invocation errors.
- Verified observation payload generation and message history append for process management tools without truncation errors.

### C. HexConsole TUI Integration (`src/harness_core/tui.py`)
- Added `/ps` and `/processes` to `HexConsole.handle_slash_command` and `SLASH_COMMANDS` autocompletion list.
- Implemented `_cmd_ps(arg: str = "")`:
  - `kill <proc_id>`: Terminates the target process and prints status confirmation.
  - `<proc_id>`: Displays comprehensive session details (ID, PID, status, uptime, PTY, exit code, command, and 2000-character terminal tail).
  - Empty argument: Renders formatted table of all supervised processes with colorized status badges (`RUNNING` green, `COMPLETED` blue, `FAILED` red, `KILLED` yellow), utilizing Rich `Table` if `HAS_RICH` is present, or formatted ANSI text fallback.
- Added `/ps` entry to `_cmd_help()`.

### D. HEX CLI Subcommand (`src/harness_core/cli.py`)
- Added `ps` subcommand to `build_parser()` with flags:
  - `--json`: Output raw JSON list or kill status.
  - `--kill <proc_id>`: Terminate process by ID.
  - `--force`: Use SIGKILL instead of SIGTERM.
  - `--filter <status>`: Filter by `running`, `completed`, `failed`, or `killed`.
- Dispatched `args.cmd == "ps"` in `_main_impl()` to query `get_process_registry()`.

### E. Comprehensive End-to-End Test Suite (`tests/test_gateway_process_tools.py`)
- 11 comprehensive tests validating:
  - Registration and risk class tiering (`R0` vs `R2`).
  - `start_process` background execution and incremental `read_terminal` slicing.
  - Secret scrubbing in process commands and output buffers.
  - Workspace path confinement defending against path traversal.
  - Clean signal termination and state transition to `KILLED`.
  - Fast-path execution mode gating (permitting R0 while denying R2).
  - Legacy `process` tool compatibility.
  - End-to-end multi-turn `ReActLoop` autonomous process supervision lifecycle.
  - `HexConsole` `/ps` slash command rendering.
  - `hex ps` CLI subcommand execution (JSON, table, filtering, and killing).

## 3. Verification & Proof
- **Unit & Integration Tests:**
  - `tests/test_gateway_process_tools.py`: 11/11 tests passing cleanly.
  - `tests/test_process_registry.py`: 13/13 tests passing cleanly.
  - `tests/test_gateway_tools.py`: 14/14 tests passing cleanly (total tool count: 30).
  - `tests/test_acp_server.py`: 9/9 tests passing cleanly.
- **Cargo Rust Tests:**
  - `cargo test --workspace`: 34/34 tests passing cleanly (17 in `hex-protocol`, 17 in `hex-tui`).
- **Full Discovery Suite:**
  - 711 repository tests verified.

## 4. Commits
- `a447196`: `feat(phase-27): register gateway process tools and react loop integration`
- `15151c8`: `feat(phase-27): tui ps slash command cli hex ps and end-to-end tests`

## 5. Deferred Items
None. All tasks and success criteria completed.

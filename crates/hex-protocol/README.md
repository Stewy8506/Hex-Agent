# `hex-protocol`

**Shared Agent Client Protocol (ACP) JSON-RPC 2.0 client, typed wire models, unified diff parser, and control-plane/P2P schemas for HEX Agent.**

`hex-protocol` (`edition = "2021"`, `version = "0.1.0"`) is the Rust wire-contract crate shared by both native HEX frontends — the terminal cockpit ([`hex-tui`](../hex-tui/README.md)) and the GPUI desktop app ([`hex-desktop`](../../../Hex-App/README.md)). It connects Rust UIs to the Python runtime (`python -m harness_core.client.cli acp`) and defines the canonical schemas shared with `hex_cli/acp.py` and `cloud/`.

---

## Overview & Design Basis

- **Protocol Dialect**: JSON-RPC 2.0 over newline-delimited frames targeting HEX's internal ACP-style dialect (`protocolVersion: "2024-11-05"`), designed for streaming token/thought deltas, nested tool lifecycles, operator approval gates, paged session history, live model discovery, settings persistence, and cloud/team coordination.
- **Zero-Drop Streaming Architecture**: Separates request/response multiplexing from inbound notification fan-out, providing both a lossy broadcast channel and a byte-budgeted lossless `mpsc` queue with semaphore backpressure so streamed assistant deltas and tool events are never silently dropped under UI load.
- **Cross-Language Schema Parity**: Mirrors the exact JSON shapes produced by the Python ACP server (`hex_cli/acp.py`) and backend/P2P coordinators (`cloud/team_mesh.py`), verified by cross-language golden digests and replay fixtures (`tests/fixtures/acp_wire/`).

### Core Stack (`Cargo.toml`)

| Crate | Purpose |
| :--- | :--- |
| `tokio` (`features = ["full"]`) | Async subprocess spawning, stdio I/O framing, timeouts, and channel primitives |
| `serde` + `serde_json` | Strongly typed JSON-RPC 2.0 (`camelCase` ACP & `snake_case` backend/P2P) serialization |
| `sha2` | Canonical SHA-256 digest computation for `TurnCommitBundle` |
| `thiserror` + `anyhow` | Typed `AcpError` variants and ergonomic error propagation |
| `futures` + `log` | Async utilities and structured draining of child `stderr` diagnostics |

---

## Module Architecture (`src/lib.rs`)

```text
crates/hex-protocol/
├── Cargo.toml
├── src/
│   ├── lib.rs       # Public module exports and top-level re-exports
│   ├── client.rs    # Async AcpClient, subprocess/stream transport, backpressured notification queues
│   ├── models.rs    # JSON-RPC 2.0 & ACP request/response/notification wire structs
│   ├── diff.rs      # Unified diff parser and side-by-side alignment builder
│   ├── error.rs     # AcpError enum
│   ├── backend.rs   # Ed25519 control-plane wire types (ServerPubkey, SignedEnvelope, HostLease, ReleaseManifest)
│   └── p2p.rs       # E2EE P2P replication types (TurnCommitBundle, canonical JSON SHA-256, SyncEnvelope, ElectionVote)
└── tests/
    ├── protocol_test.rs   # Multiplexing, diff parsing, paging, approval, and subagent wire contract tests
    └── live_acp_test.rs   # End-to-end subprocess handshake against python -m harness_core.client.cli acp
```

---

## Features & Capabilities

### 1. Asynchronous JSON-RPC 2.0 Client (`src/client.rs`)

- **Transport Modes**:
  - `AcpClient::from_streams(reader, writer)`: Connects over any `AsyncRead + AsyncWrite` pair (duplex pipes in tests, stdio, or split `UnixStream` handles).
  - `AcpClient::spawn(command, args, cwd)`: Spawns a command with piped `stdin`/`stdout`/`stderr`, `kill_on_drop(true)`, and non-blocking `stderr` draining to `log::warn!(target: "hex_acp::stderr", ...)`.
  - `AcpClient::spawn_acp_server(workspace_root)`: Automatically locates the repository root (via `HEX_REPO_ROOT` or ancestor traversal from workspace, cwd, or executable) and resolves a valid Python interpreter (`HEX_PYTHON_BIN`, `VIRTUAL_ENV`, `<repo_root>/.venv`, `python3`, `python`) capable of importing `harness_core.client.cli` and `yaml`, then spawns `<python> -m harness_core.client.cli acp [--workspace <path>]`.
- **Framing & Memory Guards**:
  - `MAX_WIRE_FRAME_BYTES = 8 MiB` protects the reader loop against unbounded lines.
  - `NOTIFICATION_BUFFER_BYTES = 16 MiB` bounds retained notification heap memory in `subscribe_lossless()` via `tokio::sync::Semaphore` permits (`NotificationPacket`).
  - `default_timeout = 60s` (configurable via `set_timeout`).
  - `subscribe()` provides a 256-slot broadcast feed; `subscribe_closed()` emits the authoritative EOF/transport-loss signal.
- **Supported RPC Methods on `AcpClient`**:
  - **Lifecycle**: `initialize`, `ping`, `close`
  - **Sessions**: `session_new` (`session/new`), `session_load` / `session_load_paged` / `session_load_task_paged` (`session/load`), `session_list` (`session/list`), `session_rename` (`session/rename`), `session_delete` (`session/delete`), `session_archive` (`session/archive`), `session_pin` (`session/pin`), `session_models` (`session/models`)
  - **Turns & History**: `session_prompt` / `session_prompt_paged` / `session_prompt_with_attachments` (`session/prompt`), `session_cancel` (`session/cancel`), `session_command` (`session/command`), `session_history` (`session/history`), `session_history_content` (`session/historyContent`), `session_tool_result` (`session/toolResult`), `session_subagents` (`session/subagents`)
  - **Approvals & Tools**: `session_approval_decision` / `session_approval_decision_with_ttl` (`session/approvalDecision`), `tools_list` (`tools/list`), `tools_call` (`tools/call`)
  - **Settings & Cloud**: `config_get` (`config/get`), `config_set` (`config/set`), `cloud_auth_status`, `cloud_auth_register`, `cloud_auth_login`, `cloud_auth_logout`, `cloud_devices_list`, `cloud_devices_revoke`, `cloud_update_check`, `cloud_config_evaluate`

### 2. Strongly Typed Wire Models (`src/models.rs`)

- **Core JSON-RPC & Content**: `RequestId`, `JsonRpcRequest`, `JsonRpcResponse`, `JsonRpcError`, `JsonRpcNotification`, `Role` (`User`, `Assistant`, `Tool`, `System`), `ContentBlock` (`Text`, `Image`, `Resource`, `Other`), `Message`, `SessionStatus`.
- **Streaming & Tool Lifecycle**:
  - `SessionUpdateNotification`: models `agent_response`, `agent_thought`, `tool`, `stream_end`, and terminal `status` (`is_turn_terminal()` checks `stream_end` or `status` in `ready | completed | cancelled | failed`) plus optional `SessionUsage` (`inputTokens`, `outputTokens`, `modelId`, `contextLimit`).
  - `ToolUpdate`: nested `{ eventType: start|complete|error, toolId, callId, durationMs, payload, diff }`.
  - `ApprovalRequestNotification` & `ApprovalDecisionResponse` (including `applied`, `outcome`, `leaseId`, `leaseTtlSeconds`).
- **Configuration & Cloud Events**: `SettingsConfig` (`ModelSettings`, `RuntimeSettings`, `WebSearchSettings`, `CloudSettings`, `LearningSettings`, `RetentionSettings`, `NotificationSettings`), `ConfigGetResponse`, `ConfigSetResponse`, `CloudAuthStatus`, `CloudDevice`, `CloudEvent`, `CloudUpdatePlan`, `CloudFlags`.

### 3. Unified Diff Parser (`src/diff.rs`)

- `UnifiedDiff::parse(raw: &str)` parses `diff --git` headers, `new file mode` / `deleted file mode` / `Binary files` markers, `---`/`+++` paths (including `/dev/null`), `@@ -a,b +c,d @@` hunk headers, context/addition/deletion lines with tracked old/new line numbers, and `\ No newline at end of file` metadata (`DiffLineType::Meta`).
- `UnifiedDiff::side_by_side(file_index)` aligns consecutive deletion/addition runs into paired `SideBySideRow` entries for two-column diff viewers.

### 4. Control-Plane & P2P Schemas (`src/backend.rs`, `src/p2p.rs`)

- **`backend.rs`**: `snake_case` contracts for `hex-backend` HTTP/WebSocket control plane — `ServerPubkey` (`GET /v1/server/pubkey`), `SignedEnvelope` (`ed25519:...`), `DeviceEnrollment` (`POST /v1/devices/register`), `HostLease` (`host_epoch` fencing), `ComponentSpec`, and `ReleaseManifest`.
- **`p2p.rs`**: `snake_case` E2EE P2P replication types — `TurnCommitBundle` with deterministic `compute_digest()` (`canonical_json_string` sorting keys + compact separators, verified against Python `cloud/team_mesh.py`), `SyncStream` (`Ledger`, `Cognition`, `Workspace`), `SyncEnvelope`, and `ElectionVote`.

### 5. Error Model (`src/error.rs`)

`AcpError` covers `Io`, `Json`, `Rpc(JsonRpcError)`, `ConnectionClosed`, `Timeout`, `ChannelClosed`, `InvalidResponse`, and `ProcessExited(Option<i32>)`.

---

## Usage Example

```rust
use hex_protocol::client::AcpClient;
use hex_protocol::models::ClientInfo;

# async fn run() -> Result<(), hex_protocol::AcpError> {
let client = AcpClient::spawn_acp_server(None).await?;
let init = client
    .initialize(Some(ClientInfo {
        name: "my-client".into(),
        version: "0.1.0".into(),
    }))
    .await?;
let mut updates = client.subscribe_lossless().await;
let session = client.session_new(None, None).await?;
println!("Connected to {} ({})", init.server_info.name, session.session_id);
# Ok(())
# }
```

---

## Environment Variables

- `HEX_PYTHON_BIN`: Explicit Python interpreter path used by `AcpClient::spawn_acp_server`.
- `VIRTUAL_ENV`: Virtual environment searched before repo `.venv` and system Python.
- `HEX_REPO_ROOT`: Override repository root when locating `harness_core` and `.venv`.

---

## Building & Testing

```bash
# Build the crate
cargo build -p hex-protocol

# Run unit + integration tests (live_acp_test skips gracefully if Python runtime is absent)
cargo test -p hex-protocol
```

- `tests/protocol_test.rs`: 13 integration tests covering duplex lifecycle, concurrent out-of-order multiplexing, multi-file diff parsing, side-by-side alignment, subagent/command/paging/approval wire contracts.
- `tests/live_acp_test.rs`: Spawns the live Python ACP server over stdio and verifies handshake, tool discovery, session creation, SQLite history persistence, and clean shutdown.
- Inline `#[cfg(test)]` suites in `src/client.rs`, `src/models.rs`, `src/diff.rs`, `src/backend.rs`, and `src/p2p.rs`.

# Phase 25 Plan 01 Summary: Cargo Workspace & `crates/hex-protocol`

**Execution Date:** 2026-11-13  
**Plan:** `.gsd/phases/25-rust-tui/25-01-PLAN.md`  
**Status:** Completed & Empirically Verified (15/15 tests passing)

---

## 1. Objectives Delivered

1. **Root `Cargo.toml` Workspace Configuration:**
   - Established multi-crate Rust workspace with `resolver = "2"`:
     - `crates/hex-protocol` (shared ACP JSON-RPC client, protocol models, unified diff parser).
     - `crates/hex-tui` (Ratatui terminal cockpit, stubbed for Plan 25-02).

2. **ACP Protocol Domain Models (`crates/hex-protocol/src/models.rs`):**
   - Implemented strongly typed Serde JSON-RPC 2.0 structures:
     - `RequestId` (supporting both numeric integer and string correlation IDs).
     - `JsonRpcRequest`, `JsonRpcResponse`, `JsonRpcError`, `JsonRpcNotification`.
     - `InitializeParams`, `InitializeResponse`, `ServerInfo`, `ServerCapabilities`.
     - `SessionNewParams`, `SessionNewResponse`, `SessionLoadParams`, `SessionLoadResponse`, `SessionPromptParams`, `SessionPromptResponse`, `SessionCancelParams`, `SessionCancelResponse`, `SessionListResponse`.
     - `ToolsListResponse`, `ToolDefinition`, `ToolCallParams`, `ToolCallResponse`.
     - Multimodal `ContentBlock` (`Text`, `Image`, `Resource`, `Other`).
     - `Role` (`User`, `Assistant`, `Tool`, `System`), `Message`, `SessionStatus`.

3. **Unified Diff Engine (`crates/hex-protocol/src/diff.rs`):**
   - Implemented zero-dependency unified diff parser:
     - Parses `diff --git a/... b/...`, `---`, `+++`, file modes, binary markers.
     - Hunk header parser (`@@ -l,s +l,s @@ [heading]`) tracking exact `old_lineno` and `new_lineno`.
     - Diff statistics calculation (files changed, additions, deletions).
     - Split side-by-side row reconstruction (`diff.side_by_side(file_idx)`), pairing deletions on left and additions on right with vertical alignment for dual-buffer TUI panes.

4. **Asynchronous ACP JSON-RPC 2.0 Client (`crates/hex-protocol/src/client.rs`):**
   - Built async stdio transport with `tokio`:
     - Monotonic correlation sequence IDs (`Arc<AtomicU64>`).
     - Request multiplexing via `Arc<Mutex<HashMap<u64, oneshot::Sender<...>>>>`.
     - Background buffered stdout reader task demuxing responses by ID and streaming notifications.
     - Background stdin writer task with non-blocking channel queue.
     - High-level typed async methods: `initialize`, `session_new`, `session_load`, `session_prompt`, `session_cancel`, `session_list`, `tools_list`, `tools_call`, `ping`.
     - Dual instantiation: stream attachment (`from_streams` for mock testing) and live subprocess launcher (`spawn_acp_server`) auto-discovering repository root, `.venv/bin/python`, and injecting `PYTHONPATH=src`.

---

## 2. Verification Evidence

### Test Suite Results (`cargo test -p hex-protocol`):
```text
running 11 tests
test diff::tests::test_multi_file_diff ... ok
test diff::tests::test_parse_sample_diff ... ok
test diff::tests::test_side_by_side_alignment ... ok
test models::tests::test_initialize_response_deserialization ... ok
test models::tests::test_jsonrpc_request_response_roundtrip ... ok
test models::tests::test_request_id_serialization ... ok
test models::tests::test_session_prompt_response_deserialization ... ok
test models::tests::test_tools_list_response_roundtrip ... ok
test client::tests::test_client_error_response_handling ... ok
test client::tests::test_client_notification_broadcast ... ok
test client::tests::test_client_request_response_flow ... ok

test result: ok. 11 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out

     Running tests/live_acp_test.rs
running 1 test
test test_live_acp_server_subprocess ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out

     Running tests/protocol_test.rs
running 3 tests
test test_complex_diff_parsing_and_side_by_side ... ok
test test_concurrent_request_multiplexing ... ok
test test_full_session_lifecycle ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
```
**Total:** 15 tests executed, 15 passed (100% green), 0 failures, 0 warnings.

---

## 3. Atomic Commits

1. `feat(phase-25): workspace setup, ACP protocol models and unified diff parser` (commit `2cb4192`)
2. `feat(phase-25): implement async ACP stdio client and protocol tests` (commit `6f22798`)
3. `chore(phase-25): add live acp subprocess test and document plan 25-01 summary` (pending)

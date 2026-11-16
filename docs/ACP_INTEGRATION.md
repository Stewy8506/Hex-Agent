# Agent Client Protocol (ACP) Integration

HEX implements the **Agent Client Protocol (ACP)** over standard JSON-RPC 2.0 stdio transport, allowing you to use HEX as an intelligent, autonomous partner directly inside your favorite code editors.

---

## 1. Zed Editor

Add HEX as an external assistant in your `.zed/settings.json` (or global `~/.config/zed/settings.json`):

```json
{
  "agent": {
    "name": "HEX",
    "command": "hex",
    "args": ["acp"],
    "protocol": "acp"
  }
}
```

Once configured, toggle the assistant in Zed (`Cmd+?` or `Ctrl+?`) and select **HEX**.

---

## 2. Neovim (`avante.nvim` / `codecompanion`)

In your Neovim Lua configuration:

```lua
-- avante.nvim configuration
require('avante').setup({
  provider = "acp",
  acp_command = { "hex", "acp" },
})
```

---

## 3. Native macOS GPUI Desktop Cockpit (experimental)

The **HEX Desktop Cockpit** (`crates/hex-desktop`) embeds an ACP client (`AcpBridge`) that spawns the Python engine as a local stdio subprocess:
- Streaming assistant deltas render into the native Metal UI today; tool-call events and authoritative completion handling follow the canonical wire contract below (F04).
- Operator approval round-trips (`session/approvalDecision`) exist on the server; desktop modal wiring is in progress (F02).
- Launch with `cargo run -p hex-desktop`; there is no `hex desktop` subcommand yet.

---

## 4. Canonical `session/update` Wire Contract

The Python server (`src/harness_core/client/acp.py`) is the source of truth. Both Rust clients (`hex-tui`, `hex-desktop`) consume these exact shapes, typed in `hex_protocol::models` (`SessionUpdateNotification`, `ToolUpdate`, `SessionPromptResponse`).

| `updateType` | Payload | Meaning |
| --- | --- | --- |
| `agent_response` | `delta`: text chunk | Streamed assistant token/text delta. |
| `tool` | `tool`: `{eventType, toolId, callId, durationMs, payload}` | Tool lifecycle. `eventType` is `start` (payload = arguments, truncated to 300 chars), `complete`, or `error`. `callId` correlates start/complete (`<toolId>_<ms>`). `durationMs` is set on completion. |
| `stream_end` | `status`: `Ready` | Emitted after a streamed turn finishes. Terminal. |
| `status` | `status`: `Ready`/`Completed`/`Cancelled`/`Failed`; optional `delta` (full answer when nothing streamed) | Turn status. Terminal when status is a completion state. |

Operator approvals arrive as a separate notification method, `session/approvalRequest` (payload includes `approvalId`); clients respond with the `session/approvalDecision` request.

The final `session/prompt` response is authoritative: `{sessionId, status: "completed"|..., content: [{type: "text", text}], taskId}`. Clients must treat the response (not just notification timing) as turn completion.

**Shared fixtures:** `tests/fixtures/acp_wire/*.jsonl` are captured from the real Python server running a scripted orchestrator and are replayed by both Rust client test suites. Regenerate with:

```bash
python tests/test_acp_wire_contract.py --regenerate
```

`tests/test_acp_wire_contract.py` fails if the server's emitted shapes drift from the checked-in fixtures; the Rust suites fail if client parsing drifts.

### Session lifecycle (F05)

- **Durable transcripts.** Sessions and their turn history are persisted under `<workspace>/.hex/data/acp_sessions/`. `session/load` resumes a session even in a fresh server process and returns the full `history` (prompt/response turns) so clients can replay the dialogue; `session/list` includes durable sessions. The TUI honors `--session <id>` by loading and replaying that transcript before falling back to a new session.
- **Conversation context.** Follow-up `session/prompt` turns carry prior dialogue (`constraints.conversation_history`, last 10 turns) into task execution, so the model sees the conversation, not just the latest prompt.
- **Concurrent cancellation.** `session/prompt` executes on a worker thread while the stdio loop keeps reading. `session/cancel` during a turn sets the cooperative cancel flag, transitions the ledger task to `CANCELED`, and rolls back the uncommitted CoW layer; execution stops at the next tool/step/synthesis boundary. The turn then emits `updateType=status` with `status=Cancelled` and a final response with `status="cancelled"`. Cancellation is per-turn: the next prompt runs normally.
- **Authoritative completion.** Clients seal a turn on `stream_end`, a terminal `status` (`Ready`/`Completed`/`Cancelled`/`Failed`), or the final `session/prompt` response — never on a delta-gap timer.

---

## 5. Diagnostics & Environment Setup

Verify that the ACP server is operational and your environment is ready:

```bash
# Verify ACP readiness and environment health
hex acp --check

# Generate ready-to-paste editor configuration snippets
hex acp --setup

# Start ACP server manually over stdio (JSON-RPC 2.0)
hex acp
```

### Supported ACP Features:
- Multi-turn conversational chat with live token streaming.
- Workspace file reading and editing in isolated Copy-on-Write staging.
- Over 20+ slash commands accessible directly through editor prompts.
- Interactive operator policy approvals for high-risk tool actions (R3/R4).

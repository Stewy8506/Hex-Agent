# `hex_cli` (`harness_core.client`)

**Command-line interface, interactive Python REPL console, native TUI launcher, ACP JSON-RPC 2.0 server, local HTTP/SSE API, web dashboard, onboarding, and diagnostics for HEX Agent.**

`hex_cli/` maps to the canonical `harness_core.client` Python package in [`pyproject.toml`](../pyproject.toml) (and its subpackages `commands/`, `onboarding/`, and `terminal/`). It serves as the primary operator control plane for HEX Agent and is installed as the `hex` and `harness` console scripts.

---

## Overview & Design Basis

- **Unified Entry Surface**: Provides both interactive terminal sessions (defaulting to the native Rust `hex-tui` binary when installed, with automatic fallback to the Python `HexConsole` REPL) and headless automation commands (`run`, `submit`, `eval`, `worker`, `gateway`, `schedule`, `daemon`, `up`, `acp`).
- **Single Source of Truth for Slash Commands**: [`command_catalog.json`](command_catalog.json) declares all 52 slash commands and their availability across `console` (46), `tui` (35), and `acp` (23), compiled into `hex-tui` and enforced by parity tests on both Python and Rust sides.
- **Offline-First & Self-Contained**: Built with Python's standard library (`argparse`, `http.server`, `sqlite3`, `termios`, `tty`) for CLI, ACP, dashboard, and pure ASGI 3.0 serving, with optional `rich`, `uvicorn`/`fastapi`, and cloud/team extensions layered on top.

---

## Package Layout

```text
hex_cli/
├── __init__.py              # Lazy exports for harness_core.client
├── cli.py                   # Top-level `hex` / `harness` parser, subcommand dispatch, orderly shutdown
├── command_catalog.json     # Single source of truth for 52 slash commands across console / tui / acp
├── command_catalog.py       # Loader, surface filter, and /help formatter
├── tui_launcher.py          # Native `hex-tui` binary resolver, optional cargo build, os.execve handoff
├── tui.py                   # Interactive Python REPL (`HexConsole`), 6 skins, raw-TTY composer, 46 slash cmds
├── ui_selector.py           # Zero-dependency raw-TTY single/multi-select & masked secret prompts
├── acp.py                   # Stdio JSON-RPC 2.0 ACP server (33 methods, _ThinkSplitter, SQLiteHistory)
├── acp_approval.py          # Blocking ACP tool approval bridge + scoped session lease validator
├── server.py                # ThreadingHTTPServer REST/SSE API (`HarnessAPIApp`, `GracefulServerRunner`)
├── asgi.py                  # Pure ASGI 3.0 adapter (`HarnessASGIAdapter`, `get_asgi_app`, `app`)
├── dashboard.py             # Self-contained dark-theme HTML/JS web control surface (`DASHBOARD_HTML`)
├── onboard.py               # Interactive & scripted setup wizard (`hex setup` / `hex onboard`)
├── auto_setup.py            # Hardware, CLI toolchain, IDE token, and local LLM daemon auto-discovery
├── doctor.py                # `HexDoctor` extending `SystemDiagnostics` across 10 health categories
├── commands/
│   ├── antigravity.py       # `hex auth antigravity` (PKCE OAuth / IDE import / manual token) & `hex quota`
│   ├── cloud_account.py     # `hex cloud` (register, login, logout, status, devices, revoke)
│   ├── projects.py          # `hex project` (list, add, use/switch, prune)
│   └── update.py            # `hex update` (signed release manifest & update compatibility check)
├── onboarding/
│   └── verification.py      # Real local CoW, CAS, toolchain, and hash-chain first-task verification
└── terminal/
    ├── ansi.py              # 24-bit TrueColor ANSI helpers & visible-width text padding/truncation
    └── review.py            # Unified diff, side-by-side diff, and Graphify blast-radius tree renderers
```

---

## Entry Points

| Invocation | Target | Behavior |
| :--- | :--- | :--- |
| `hex` / `harness` | `harness_core.client.cli:entrypoint` | Interactive launch (native `hex-tui` if available, else Python `HexConsole` REPL; runs first-run setup if unconfigured on TTY) |
| `python -m harness_core.client.cli` | `hex_cli/cli.py` | Direct module entry matching `entrypoint()` |
| `hex acp` / `python -m harness_core.client.cli acp` | `hex_cli/acp.py` (`ACPServer.run_stdio`) | Starts the JSON-RPC 2.0 stdio server used by `hex-tui`, `hex-desktop`, Zed, and Neovim |
| `hex up` / `hex up --asgi` | `hex_cli/server.py` / `hex_cli/asgi.py` | Starts the REST/SSE API and web dashboard on `127.0.0.1:8420` (pure ASGI app at `harness_core.client.asgi:app`) |
| `hex setup` / `hex onboard` | `hex_cli/onboard.py` | Runs the setup wizard (`--auto`, `--quick`, `--blank-slate`, `--reset`) |
| `hex doctor` | `hex_cli/doctor.py` | Audits Python, hardware, CoW, sandboxes, credentials, git, storage, directories, audio, and live endpoints |
| `hex tui` / `hex chat` / `hex voice` | `tui_launcher.py` / `tui.py` | Launches native Ratatui cockpit (`--legacy` forces Python REPL; `voice` enables spoken TTS loop) |

**Data Directory Resolution (`cli.py`)**: Checks `HEX_DATA_DIR` / `HARNESS_DATA_DIR`, `./.hex_data`, `./.harness_data`, `<repo>/.hex_data`, `<repo>/.harness_data`, `~/.hex/data`, and binds to `<workspace>/.hex/data` when initialized (or an isolated temp directory under test runners).

---

## CLI Subcommands & Flags

### Top-Level Options

`--version`/`-v`, `--data-dir`, `--project`, `--workspace`, `--provider`, `--model`, `--base-url`, `--skin` (`sapphire`, `amber`, `cyberpunk`, `matrix`, `nord`, `mono`), `-Q`/`--quiet`, `--no-stream`, `--voice`, `--voice-name`, `--speech-rate`, `--react`, `--legacy`, `--ratatui`, `--check`.

### Subcommands

- **Setup & Diagnostics**:
  - `setup` / `onboard [stage]` (`--auto`, `--quick`, `--blank-slate`, `--reset`, `--reasoning-effort`, `--provider`, `--model`, `--base-url`, `--config-dir`, `--non-interactive`, `--defaults`)
  - `init` (`--workspace`, `--force`, `--data-dir`)
  - `doctor` (`--fix`, `--live`, `--json`, `--no-color`, `--workspace`)
  - `recipe [workspace]` (`--json`)
  - `uninstall` (`-y`/`--yes`, `--purge`, `--keep-data`, `--dry-run`, `--data-dir`)
- **Interactive & Server Surfaces**:
  - `chat`, `tui` (`--check`, `--cow`, `--session`, `--build`, `--release`), `voice` (`--muted`, `--voice-name`, `--speech-rate`)
  - `acp` (`--check`, `--setup`, `--workspace`)
  - `up` (`--host`, `--port`, `--drain-timeout`, `--asgi`)
- **Autonomous Execution & Verification**:
  - `run` / `exec <goal>` (`--mode`, `--branch`, `--pr`, `--base-branch`, `--dry-run`, `--react`, `--keep-artifacts`/`--pinned`)
  - `submit <goal>` (`--mode`, `--keep-artifacts`/`--pinned`)
  - `status [task_id]`, `pin <task_id>`, `unpin <task_id>`
  - `eval` (`--suite {all,core,resilience,compaction,tui}`, `-v`, `--json`)
  - `harness2-demo`, `harness2-eval` (`--task-id`, `--mode`, `--k`, `--substrate`, `--proposer`, `--runs-root`, `--json`)
- **Projects, Auth, Cloud & Updates**:
  - `project {list,add,use|switch,prune|clean}`
  - `auth [antigravity]` (`--status`, `--import-ide`, `--token`, `--refresh-token`, `--project-id`, `--auth-url`, `--client-id`, `--code`, `--verifier`, `--auth-file`)
  - `quota` (`--auth-file`, `--json`)
  - `cloud [status|register|login|logout|devices|revoke]` (`--email`, `--password`, `--device`, `--include-revoked`, `--base-url`, `--json`)
  - `update` (`--channel`, `--manifest`, `--base-url`, `--json`)
- **Memory, Code Intelligence, Skills & Storage**:
  - `memory [list|search|history|rollback]`, `reflect`
  - `search <query>` (SQLite FTS5 task journal search)
  - `lsp [status|diagnostics|definition|hover]`
  - `graphify [status|update|ingest|god-nodes|affected|tree]`
  - `skill {synthesize,list,scan}`, `learn <target>`, `scan-skill <path>`
  - `backup`, `restore <archive>`, `dump`, `gc` (`--dry-run`, `--workspace-ttl`, `--cas-ttl`, `--full-vacuum`, `--no-vacuum`)
- **Background Runtime & Gateways**:
  - `ps` (`--kill`, `--force`, `--filter`), `worker`, `gateway {start,status}`, `gh-action`, `schedule {add,list,remove,run-due}`, `daemon {install,status,uninstall}`

---

## Core Components in Detail

### Native TUI Launcher (`tui_launcher.py`)

Follows the HEX install contract: release artifacts are placed in `~/.hex/bin/hex-tui` by `install.sh` / `install.ps1` (never downloaded lazily at launch time). Resolution order in `find_hex_tui_binary()`:
1. `$HEX_TUI_BIN`
2. `<repo_root>/target/<profile>/hex-tui` (`debug` or `release`) and `<repo_root>/crates/hex-tui/target/<profile>/hex-tui`
3. Alternate Cargo profile under `target/`
4. `~/.hex/bin/hex-tui`
5. `hex-tui` on `PATH` and `~/.local/bin/hex-tui`

Cargo compilation (`cargo build -p hex-tui [--release]`) runs only when `--build` or `HEX_DEV_BUILD` is explicitly set. Exports `HEX_PYTHON_BIN`, `HEX_REPO_ROOT`, and `VIRTUAL_ENV`, then hands off via `os.execve` on interactive POSIX TTYs (or falls back to `HexConsole` on error).

### Python Interactive Console (`tui.py`)

`HexConsole` (aliased as `HarnessTUI`, `CockpitTUI`) provides the full-featured fallback REPL and voice console:
- 6 TrueColor `TUISkin` themes (`sapphire`, `amber`, `cyberpunk`, `matrix`, `nord`, `mono`) switchable via `/skin`.
- Raw-TTY docked input frame with mode cycling (`Tab`: `plan` → `agent` → `code`), rotating prompt hints, slash completion across 46 commands, and `StreamingTokenRenderer` + `ActivitySpinner`.
- Interactive `[y/n/d/s/q]` tool approval loop (including dropping into a sandbox shell `[s]` inside the isolated attempt directory).
- Console-exclusive commands not yet in the Rust TUI/ACP: `/rewind`, `/lease`, `/learn`, `/reasoning`, `/compress`, `/rename`, `/skin`, `/voice`, `/project`, `/banner`, `/workers`, `/ps`, `/kernel`, `/backup`, `/dump`, `/gc`, `/harness2`.

### ACP JSON-RPC 2.0 Server (`acp.py`, `acp_approval.py`)

- **Stdio Transport**: Reads newline-delimited JSON or `Content-Length` framed messages on `stdin`, routes logging to `stderr`, and dispatches turns on worker threads (`_turn_threads`) so `session/cancel` and `session/approvalDecision` never block behind an active generation. Orderly EOF/interrupt shutdown cancels active tasks and revokes approval leases.
- **33 Methods**: `initialize`, `ping`, `session/{new,load,list,rename,delete,archive,pin,models,prompt,cancel,command,history,historyContent,toolResult,subagents,approvalDecision}`, `tools/{list,call}`, `config/{get,set}`, `cloud/auth/{status,register,login,logout}`, `cloud/devices/{list,revoke}`, `cloud/update/check`, `cloud/config/evaluate`, and `cloud/signal/{start,stop,status}`.
- **Streaming & Fidelity**:
  - `_ThinkSplitter` separates `<think>...</think>` blocks (even across split chunks) into `updateType: "agent_thought"` vs `"agent_response"`.
  - Tool updates (`updateType: "tool"`) preserve bounded unified diffs (`tool.diff`, capped at 16,000 chars) alongside 300-char normal / 4,000-char error argument/result previews.
  - Terminal `status: "Ready"` notifications include router `usage` (`inputTokens`, `outputTokens`, `modelId`, `contextLimit`).
  - `session/prompt` accepts file `attachments` (up to 10 files, 64 KiB each, 256 KiB total) and builds conversation history from the last 10 turns.
- **Session Storage**: Persists metadata in `<workspace>/.hex/data/acp_sessions/<session_id>.json` and paged turns in `<workspace>/.hex/data/acp_sessions/history.db` (`SQLiteHistory`, 64 KiB metadata budget, BLOB-sliced `session/historyContent`).
- **Config Whitelist (`config/get`, `config/set`)**: Reads/writes whitelisted camelCase keys mapped to `config.yaml` (`model.*`, `runtime.max_turns`, `web_search.provider`, `cloud.base_url`, `learning.auto_reflect`), rejecting secret-named fields and writing atomically.
- **Approval Bridge (`acp_approval.py`)**: Emits `session/approvalRequest`, waits up to `HEX_APPROVAL_TIMEOUT` (default 60s, fail-closed `DENY`), and handles `approve` (`y`), `approve_scope` (`a`), or `deny` (`n`) with optional exact-match scoped `leaseTtlSeconds` (`60..86400`).

### REST API, Pure ASGI & Web Dashboard (`server.py`, `asgi.py`, `dashboard.py`)

- **Endpoints**: `GET /` (`DASHBOARD_HTML`), `GET /livez`, `GET /healthz` (DB + CoW canary + active workers), `POST/GET /v1/tasks`, `POST /v1/tasks/claim`, `GET /v1/tasks/{id}/{events,proof,artifacts}` (with SSE `?stream=true`), `POST /v1/tasks/{id}:{pause,resume,cancel,fork,invoke-skill,transition}`, `GET/POST /v1/approvals/{id}:decide` (safe GET confirmation form, explicit POST decision), `GET/POST /v1/workers*`, `POST /v1/leases/*`, `GET/POST /v1/skills*`, `POST /v1/artifacts/delta`, `GET /v1/artifacts/{id}/file`.
- **Auth & Lifecycle**: Optional `auth_token` (`X-Worker-Token`, `Authorization: Bearer`, `?token=`) protecting worker/lease/write endpoints; `GracefulServerRunner` flips `/healthz` to `503` on `SIGINT`/`SIGTERM` and drains in-flight requests.
- **`asgi.py`**: Zero-framework ASGI 3.0 adapter wrapping `HarnessAPIApp` (supports `lifespan` and SSE streaming under `uvicorn`).

---

## Testing

All client and packaging tests live in [`tests/client/`](../tests/client) and [`tests/packaging/`](../tests/packaging):

```bash
# Run all client tests (CLI, REPL, ACP server, catalog parity, dashboard/API, doctor, onboarding)
python -m pytest tests/client/ -v

# Verify slash-command catalog parity across console, TUI, and ACP
python -m pytest tests/client/test_command_catalog.py -v

# Run install contract & packaging checks
python -m pytest tests/packaging/ -v
```

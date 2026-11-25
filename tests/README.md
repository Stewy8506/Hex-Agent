# `tests`

**Python subsystem unit, integration, contract, and packaging test suites (`151` `test_*.py` files across `16` subpackages).**

Configured via `[tool.pytest.ini_options] testpaths = ["tests"]` in [`pyproject.toml`](../pyproject.toml).

## Structure

| Subpackage / Folder | Focus |
| :--- | :--- |
| `fixtures/acp_wire/` | Canonical JSONL wire traces (`turn_nonstreamed.jsonl`, `turn_streamed.jsonl`) captured from `hex_cli/acp.py` and shared with Rust client test suites. |
| `capabilities/` (12) | Browser/vision, path authority, Discord/Telegram gateways, mobile reaction listener, Git autopilot, GitHub Actions, voice TTS/STT, web search, webhooks. |
| `client/` (29) | CLI, ACP server/context/config/feed fidelity/session management/wire contract (`test_acp_wire_contract.py --regenerate`), REPL console, Rust TUI launcher, auto-setup, doctor, pure ASGI/HTTP server, command catalog parity. |
| `cloud/` (5) | `hex-backend` integration, Ed25519 signed envelopes, cloud sessions, WebSocket `SignalLink`, update & remote config evaluation. |
| `cognition/` (17) | A-MEM CRUD, AST-scoped JIT memory & staleness, curated memory caps, PromptBuilder 5-slot KV-cache freezing, repetition sentry, context compressor, soul manager, reflection, title synthesis. |
| `configuration/` (1) | Config YAML / `.env` loading and overrides (`test_config_parsing.py`). |
| `core/` (23) | Orchestrator state transitions, ReAct loop/cancellation/resilience, MoA, approval leases & bindings, subagent delegation/deadlines, DAG replanning, durable compaction, journal replay, mandatory gateway boundary, reasoning effort. |
| `integration/` (13) | End-to-end ACP approval bridge, Antigravity OAuth, CoW recursion/sync, Harness2 benchmark adapter & sweeps, memory isolation, audit & two-tier compression benchmarks. |
| `lsp/` (2) | Zero-dependency LSP stdio client and gateway repair integration. |
| `models/` (3) | `CredentialPool` 429 rotation/blackholing, local inference socket probes, `model_metadata` lookups. |
| `packaging/` (4) | Install contract, wheel/editable mapping checks, `hex uninstall`, and version identity parity. |
| `platform/` (2) | Shared `SystemDiagnostics`/`HardwareProfile` services and cross-platform shell resolution. |
| `providers/` (4) | Antigravity OAuth/provider, extended 26-provider specs, local/MLX providers. |
| `runtime/` (4) | Persistent `CodeKernel`, `ProcessRegistry`, `TaskScheduler`, `DaemonInstaller`, `WorkerDaemon`. |
| `skills/` (6) | Bundled skill resource resolution, env hardening, `SkillCompiler`, `verify:` hooks, SkillSpector AST + OSV scanning. |
| `storage/` (14) | Dual SQLite/PostgreSQL ledger, claim transactions, CoW providers, Alembic migrations, GC, sandbox isolation, FTS5 search, backup, timeline rewind, OS Keychain vault. |
| `tools/` (12) | Confined filesystem tools, code kernel & process gateway tools, Graphify AST/polyglot blast radius, MCP client & discovery. |

## Running Tests

```bash
# Requires editable install from repository root:
python -m pip install -e ".[full]"

# Run full Python suite
python -m pytest tests -v

# Run a single subsystem or regenerate shared ACP fixtures
python -m pytest tests/client/ -v
python tests/client/test_acp_wire_contract.py --regenerate
```

# `harness_core`

**Top-level Python package facade, policy-enforcing `ToolGateway`, universal config loader, Graphify AST code-intelligence adapter, zero-dependency MCP client, and evaluation/benchmark runners.**

In [`pyproject.toml`](../pyproject.toml), this folder maps 1:1 to the `harness_core` Python package, while sibling engine directories at the repository root map into `harness_core.*` subpackages (`harness_core.core`, `harness_core.cognition`, `harness_core.storage`, `harness_core.runtime`, `harness_core.client`, `harness_core.providers`, etc.) and [`setup.py`](../setup.py) (`BuildPyWithVendor`) bundles third-party vendor code into `harness_core/vendor/`. Because of that flat-root `package-dir` mapping, importing `harness_core` modules requires installing the package (for example `python -m pip install -e ".[full]"` from the repository root).

## Modules

| File / Folder | Purpose |
| :--- | :--- |
| `__init__.py` | Defines `__version__` (`importlib.metadata.version("hex-ai")`, fallback `"0.22.0"`) and PEP 562 lazy exports for core namespaces (`capabilities`, `client`, `cognition`, `core`, `models`, `runtime`, `storage`) and root symbols (`HarnessOrchestrator`, `LedgerStore`, `TwoTierModelRouter`, `ToolGateway`). |
| `gateway.py` | Central mandatory tool boundary (`ToolGateway(FileToolsMixin)`): defines **31** built-in `TOOL_DEFINITIONS` across risk tiers `R0`–`R4`, default-deny policy enforcement, secret scrubbing (`SECRET_PATTERNS`, `redact_secrets`), 0 ms idempotent CAS call replay, temporal approval leasing + webhook/interactive approval dispatch, Active Graphify & JIT-memory blast-radius enrichment, `execute_code_mode_composition()`, and dynamic MCP routing. |
| `config_loader.py` | Universal configuration and environment loader merging global (`~/.hex/{credentials.env,config.yaml,config.toml}`) and workspace (`<ws>/.hex/...`) settings via `_deep_merge`. Keeps `_MODEL_SELECTION_ENV_KEYS` gated by `project_override: true` and translates `cloud:`, `learning.auto_reflect`, `[retention]`, `mcp_servers`, and `model.reasoning_effort`. |
| `graphify.py` | `ActiveGraphifyAdapter`, `GraphifyUpstreamRunner`, `SymbolDef`, and `BlastRadiusReport` wrapping `vendor/graphify` (`nx.DiGraph`). Maintains an mtime/stat-cached polyglot AST & call graph across **15** file extensions (`.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.go`, `.rs`, `.java`, `.c`, `.cpp`, `.cc`, `.cxx`, `.h`, `.hpp`), provides `extract_surgical_context()`, `analyze_architecture()`, `export_interactive_html()`, and records architectural reflections. |
| `mcp_client.py` | Zero-dependency JSON-RPC 2.0 Model Context Protocol client (`MCPClientAdapter`, `MCPTransport`, `StdioMCPTransport`, `HttpSSEMCPTransport`, `MCPToolDefinition`) plus `classify_mcp_tool_risk()` for automatic `R0`–`R4` mapping. |
| `harness2_adapter.py` | Integrates Google Research `vendor/harness2` Propose-Probe-Compose sweep/benchmark pipelines (`OrchestratorBenchmarkAdapter`, `LocalRouterProposer`, `LLMRouterProposer`, `CLAUDE_SURFACE`, `GEMINI_SURFACE`, `ClaudeSubstrate`, `GeminiSubstrate`, `OpencodeSubstrate`, `CodexSubstrate`, `resolve_substrate`, `run_orchestrator_sweep`, `runmark`). Distinct from `Harness2ControlledAdapter` in [`capabilities/proof.py`](../capabilities/README.md). |
| `eval_runner.py` | Runs `hex eval` benchmark test suites (`EVAL_SUITES`: `"core"`, `"resilience"`, `"compaction"`, `"tui"`, `"all"`) via `run_eval_suite()`. |
| `vendor_paths.py` | Resolves vendored engine paths (`get_vendor_dir()`, `get_graphify_dir()`, `get_skillspector_dir()`, `get_harness2_dir()`) across `HEX_VENDOR_DIR`/`HARNESS_VENDOR_DIR`, packaged `vendor/`, repo `vendor/`, `~/.hex/vendor`, and `sys.prefix/share/hex/vendor`. |
| `migrations/0001_initial_schema.sql` | Wheel-packaged authoritative 18-table SQL schema auto-executed by `LedgerStore` on SQLite WAL initialization; byte-identical to [`migrations/0001_initial_schema.sql`](../migrations/0001_initial_schema.sql). |
| `cloud_provider.py`, `local_provider.py`, `mlx_provider.py` | Compatibility re-export shims forwarding to [`providers/`](../providers/README.md) (`harness_core.providers.*`). |
| `graphify_adapter.py` | Compatibility re-export shim from `.graphify` (note: currently references unexported names `get_god_nodes` and `detect_languages_in_workspace`). |
| `optional_skills/` | Stale legacy directory containing compiled `__pycache__/*.pyc` remnants only; active optional skill bundles live in [`optional-skills/`](../optional-skills/README.md). |

## Built-In Tool Risk Tiers (`gateway.py`)

Every tool invocation enters `ToolGateway.invoke()` and is classified into one of five risk tiers before execution:

- **`R0` (Read-Only / Inspection)**: `read_file`, `list_files`, `search_files`, `todo`, `clarify`, `session_search`, `subagent_status`, `read_terminal`, `list_processes`
- **`R1` (Scoped Queries / Local Intelligence)**: `query_graphify`, `web_search`, `web_extract`, `memory`, `lsp_diagnostics`, `lsp_definition`, `lsp_references`, `lsp_hover`, `subagent_wait`, `image_inspect`
- **`R2` (Workspace / Runtime Mutation)**: `write_file`, `patch`, `run_tests`, `terminal`, `process`, `delegate`, `browser`, `start_process`, `kill_process`, `code_kernel`
- **`R3` (External Side Effects)**: `git_push_remote` *(currently a stub returning `{ok: True, ...}` without invoking `git`)*
- **`R4` (Policy / System Mutation)**: `modify_system_policy`

> **Current Implementation Note**: The `browser` tool (backed by [`capabilities/browser.py`](../capabilities/README.md)) currently performs HTTP HTML fetch + ARIA tree parsing (`navigate`, `snapshot`, `close`) and returns explicit unsupported errors for interactive actions (`click`, `type`, `screenshot`, `evaluate`).

## Integration & Verification

- Consumes [`agent/`](../agent/README.md), [`cognition/`](../cognition/README.md), [`storage/`](../storage/README.md), [`runtime/`](../runtime/README.md), [`tools/`](../tools/README.md), [`lsp/`](../lsp/README.md), [`capabilities/`](../capabilities/README.md), and [`providers/`](../providers/README.md).
- Tests live in [`tests/tools/`](../tests/tools), [`tests/configuration/`](../tests/configuration), [`tests/integration/`](../tests/integration), and [`tests/packaging/`](../tests/packaging).

```bash
python -m pytest tests/tools/ tests/configuration/ tests/packaging/ -v
```

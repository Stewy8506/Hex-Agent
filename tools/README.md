# `tools` (`harness_core.tools`)

**Filesystem tool implementations backing the `ToolGateway`.**

`tools/` maps to `harness_core.tools` in [`pyproject.toml`](../pyproject.toml). It houses the confined workspace filesystem tool implementations mixed into `ToolGateway` (`harness_core/gateway.py`).

## Modules

| File | Purpose |
| :--- | :--- |
| `__init__.py` | Exports `FileToolsMixin`. |
| `filesystem.py` | Defines `DIFF_MAX_LINES = 240`, `unified_diff_bounded()`, and `FileToolsMixin` implementing `_tool_read_file` (32k-char cap + Active Graphify JIT AST memory injection), `_tool_list_files`, `_tool_search_files`, `_tool_write_file`, and `_tool_patch` (exact `find`/`replace` or unified diff application + blast-radius inspection). |

## Integration & Tests

- `ToolGateway(FileToolsMixin)` in [`harness_core/gateway.py`](../harness_core/gateway.py) exposes these methods as the `read_file`, `list_files`, `search_files`, `write_file`, and `patch` gateway tools, with bounded unified diffs forwarded by `hex_cli/acp.py` to desktop and TUI clients.
- All file operations pass through `_resolve_confined_path()` so paths cannot escape the active sandbox/workspace root.
- Tests live in [`tests/tools/`](../tests/tools).

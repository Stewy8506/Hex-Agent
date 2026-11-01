# Phase 08 Plan 01 Summary: Interactive Terminal UI Engine, Token Streaming REPL & Slash Commands

**Phase:** 08 — Interactive Terminal UI (`hex chat`) & Onboarding Polish  
**Plan:** 08-01 (Wave 1)  
**Status:** Complete  
**Commits:** `eaf3bf5`, `d252ebc`, `2db7f06`  
**Tests:** 210/210 passed (24 new tests in `tests/test_tui.py`, 0 regressions)

---

## 1. Accomplishments

1. **Core TUI Rendering Engine & Diff Visualizers (`src/harness_core/tui.py`):**
   - Implemented terminal rendering with optional `rich` support and robust, zero-dependency ANSI fallbacks (`\033[32m` green, `\033[31m` red, `\033[33m` yellow, `\033[36m` cyan, `\033[1m` bold, `\033[0m` reset) with Unicode box-drawing characters.
   - Built `render_unified_diff` highlighting chunk headers (`@@`) in cyan, additions (`+`) in green, deletions (`-`) in red, and file headers (`---`, `+++`) in bold yellow.
   - Built `render_side_by_side_diff` aligning old and new content into two side-by-side columns with a vertical divider `│` and colorized line replacements.
   - Built `render_blast_radius_tree` displaying Active Graphify AST impact matrices with hierarchical branches for modified symbols, 1-hop direct callers, 2-hop transitive callers, pinned verification tests, and colorized risk badges (`[R0]` through `[R4]`).
   - Implemented `StreamingTokenRenderer` for progressive chunk streaming, code-fence detection, and syntax highlighting.
   - Built `render_session_banner` displaying current model, compressor status, sandbox backend, and CoW store in a double-bordered ANSI header.

2. **Interactive `HexConsole` REPL & Slash Commands (`src/harness_core/tui.py`, `src/harness_core/cli.py`):**
   - Implemented `HexConsole` pair-programming console managing session state, prompt loop (`hex> `), command history, active task tracking, and token/cost telemetry.
   - Built 10 interactive slash command handlers:
     - `/help`: prints formatted table of commands with syntax and purpose.
     - `/status`: inspects active/last task, Proof-Carrying Artifact bundle, and active worker count.
     - `/diff`: checks uncommitted workspace changes against base snapshot using `compute_workspace_diff` and renders unified diff.
     - `/rollback`: cooperatively cancels active task and rolls back dirty CoW workspace layer.
     - `/fork`: branches active task into parallel speculative attempt branches via `fork_speculative_branches`.
     - `/skills`: lists all scanned skills in `LedgerStore` with risk badges, Gate decisions, and quarantine status.
     - `/model [name]`: inspects active model router config or dynamically updates model routing profile.
     - `/budget`: displays session input/output tokens, compression savings ratio, and estimated USD spend.
     - `/clear`: clears the terminal screen via ANSI `\033[H\033[2J`.
     - `/quit` / `/exit`: cleanly terminates console session.
   - Integrated natural language prompt dispatch submitting `TaskSubmitRequest` to `HarnessOrchestrator`, live token streaming, and Proof-Carrying Artifact verification display.
   - Wired `hex chat` subcommand into `src/harness_core/cli.py` with flags `--project`, `--workspace`, `--model`, `--no-stream`, and `--data-dir`.

3. **Database & CoW Enhancements (`src/harness_core/db.py`, `src/harness_core/cow.py`):**
   - Added `list_skills` and `list_skills_async` to `LedgerStore` querying registered skills and latest scan reports.
   - Implemented `compute_workspace_diff` across `WorkspaceSnapshotProvider` implementations (`APFSAndPortableCoWProvider`, `OverlayFSSnapshotProvider`, `BtrfsSnapshotProvider`).

4. **Unit & Integration Test Suite (`tests/test_tui.py`):**
   - 24 comprehensive tests covering unified diff, side-by-side diff, workspace diff calculation, Graphify AST tree rendering across R0-R4 risk classes, streaming token accumulation, session banner formatting, all 10 slash commands, prompt loop execution, task submission, and CLI `hex chat` argument parsing.

---

## 2. Verification Evidence

### Task 1 Verify Command:
```bash
.venv/bin/python -c "from harness_core.tui import render_unified_diff, render_side_by_side_diff, render_blast_radius_tree, render_session_banner; d = render_unified_diff('--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,3 @@\n def f():\n-    return 1\n+    val = 2\n+    return val\n'); print(d); s = render_side_by_side_diff('a = 1\n', 'a = 2\n'); print(s); tree = render_blast_radius_tree({'modified_files': ['foo.py'], 'direct_callers': ['bar.py:call_f'], 'pinned_tests': ['test_foo.py'], 'risk_class': 'R2'}); print(tree); banner = render_session_banner(); assert 'HEX' in banner; print('TUI_VISUALIZERS_OK')"
```
**Output:**
```
┌── Proposed Changes ────────────────────────────────────────────────────────┐
│ --- a/foo.py                                                                 │
│ +++ b/foo.py                                                                 │
│ @@ -1,2 +1,3 @@                                                              │
│  def f():                                                                    │
│ -    return 1                                                                │
│ +    val = 2                                                                 │
│ +    return val                                                              │
└────────────────────────────────────────────────────────────────────────────────┘
┌─ Old: file                            ┬─ New: file                            ─┐
│ a = 1                                │ a = 2                                │
└──────────────────────────────────────┴──────────────────────────────────────┘
Active Graphify AST Impact Matrix [R2 - Medium Risk]
├── 📁 Modified Files / Symbols (1)
│   └── foo.py
├── 🔗 1-Hop Direct Callers (1)
│   └── bar.py:call_f
├── 🌐 2-Hop Transitive Callers (0)
│   └── (none)
└── 🧪 Pinned Verification Tests (1)
    └── test_foo.py
TUI_VISUALIZERS_OK
```

### Task 2 Verify Command:
```bash
.venv/bin/python -c "from harness_core.cli import main; import sys; from io import StringIO; sys.stdin = StringIO('/help\n/skills\n/model\n/budget\n/status\n/quit\n'); res = main(['chat', '--data-dir', ':memory:']); assert res == 0; print('CLI_CHAT_REPL_OK')"
```
**Output:**
```
╔════════════════════════════════════════════════════════════════════════════╗
║  ⚡ HEX Autonomous AI Workbench — Interactive Terminal Console            ║
║  Model: claude-3-5-sonnet  │  Compressor: M4 MLX Local                   ║
║  Sandbox: bwrap  │  CoW Store: apfs_clone                                ║
║  Type /help for slash commands, or enter a natural language prompt.      ║
╚════════════════════════════════════════════════════════════════════════════╝
hex> 
Available HEX Slash Commands:
  /help            - Show table of all available slash commands
  /status          - Display active/recent tasks, proof bundle, and worker state
  /diff            - Inspect uncommitted changes in active workspace via unified diff
  /rollback        - Roll back workspace changes or cancel active task
  /fork            - Fork active task into speculative attempt branches
  /skills          - List registered skills, risk scores, and gate decisions
  /model [name]    - Inspect or switch active model routing profile
  /budget          - Display session token usage, compression ratio, and cost
  /clear           - Clear the terminal screen
  /quit, /exit     - Exit interactive console session

hex> No registered skills found.
hex> 
Active Model Routing Configuration:
  Profile:          hybrid (Two-Tier MLX Local + Cloud)
  Tier-1 Local:     mlx-community/Qwen2.5-Coder-7B-Instruct-4bit
  Tier-2 Cloud:     gemini-3.5-pro

hex> 
Session Budget & Token Telemetry:
  Input Tokens:      0
  Output Tokens:     0
  Tokens Saved:      0
  Compression Ratio: 0.0%
  Session Cost:      $0.0000 USD

hex> 
HEX Console Session Status:
  Project ID:     default
  Workspace Root: /Users/anv./Development/Harness
  Streaming:      Enabled
  Active Task ID: (none)
  Active Workers: 1
    • wrk_m4_air_local [m4_air] (unknown)

hex> Exiting HEX console.
CLI_CHAT_REPL_OK
```

### Task 3 Verify Command:
```bash
.venv/bin/python -m unittest tests/test_tui.py
```
**Output:**
```
Ran 24 tests in 0.274s
OK
```

### Full Repository Regression Verification:
```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```
**Output:**
```
Ran 210 tests in 19.726s
OK
```

---

## 3. Deviations Handled Automatically

1. **`compute_workspace_diff` Implementation:**
   - Implemented `compute_workspace_diff` in `src/harness_core/cow.py` walking directory trees while respecting `IGNORE_DIRS`, computing text diff chunks with `difflib.unified_diff`, and attaching the method to `WorkspaceSnapshotProvider` protocol and all 3 provider implementations (`APFSAndPortableCoWProvider`, `OverlayFSSnapshotProvider`, `BtrfsSnapshotProvider`).
2. **`list_skills` and `list_skills_async` in `LedgerStore`:**
   - Added `list_skills` and `list_skills_async` in `src/harness_core/db.py` to enable `/skills` slash command and CLI introspection.
3. **CLI Subcommand Argument Placement:**
   - Added `--data-dir` with `default=argparse.SUPPRESS` to `p_chat` so that invocations like `main(['chat', '--data-dir', ':memory:'])` are cleanly accepted by `argparse` without error.

---

## 4. Commits

- `eaf3bf5`: `feat(phase-08): implement core TUI rendering engine and diff visualizers`
- `d252ebc`: `feat(phase-08): implement HexConsole interactive REPL and wire hex chat CLI`
- `2db7f06`: `feat(phase-08): author unit and integration tests for TUI and slash commands`


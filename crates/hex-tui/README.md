# `hex-tui`

**Native Rust terminal cockpit for HEX Agent — built with Ratatui, Crossterm, and `hex-protocol`.**

`hex-tui` (`version = "0.1.0"`, `edition = "2021"`) is the default interactive terminal interface for HEX. It renders a responsive alt-screen workspace with a Start screen, split-pane Session cockpit, virtualized streaming markdown, unified and side-by-side diffs, a customizable right rail, fuzzy slash-command palette, fail-closed permission dialogs, SQLite resume history, and bounded on-disk transcript paging.

---

## Overview & Design Basis

- **Design Language**: Implements the terminal visual specifications in [`docs/design/mockups-tui/`](../../docs/design/mockups-tui) (Start screen, Active Session, Command Palette, Permission Modal, Diagnostic Error Card, and Subagent Inspection Modal).
- **Ratatui Owns Every Cell**: Instead of printing pre-rendered Python ANSI strings, `hex-tui` owns layout, colors, Unicode width wrapping, animations, scrolling, and mouse hit-testing directly in Rust via `ratatui` + `crossterm`.
- **Two-Process ACP Split**: Communicates with the autonomous agent engine (`python -m harness_core.client.cli acp`) over newline-delimited JSON-RPC 2.0 via [`hex-protocol`](../hex-protocol/README.md), while gracefully supporting standalone/offline operation (including local SQLite resume browsing) if the backend fails to spawn.
- **Memory-Bounded Long Sessions**: Combines a 2 MiB LRU block render cache with an opt-in, append-only on-disk `TranscriptStore` so multi-thousand-turn sessions do not grow RAM unboundedly.

### Core Dependencies (`Cargo.toml`)

| Crate | Purpose |
| :--- | :--- |
| `ratatui` `0.29` | Terminal cell buffer rendering, layout splits, rounded blocks, and `TestBackend` verification |
| `crossterm` `0.28` (`event-stream`) | Raw mode, alt-screen, mouse capture, bracketed paste, and async terminal event stream |
| `tokio` `1.0` (`full`) | Biased `select!` event loop (`Key`/`Paste`/`Mouse` > 50ms `Tick` > ACP backlog) |
| `clap` `4.4` (`derive`) | CLI flag parsing (`--check`, `--skin`, `--workspace`, `--session`, etc.) |
| `rusqlite` `0.37` (`bundled`) | Direct SQLite reads/deletes against `.hex/data/ledger.db` (no external `sqlite3` library needed) |
| `unicode-width` + `unicode-segmentation` | Display-column wrapping, grapheme cursor motion, and wide-emoji alignment |
| `hex-protocol` | Async ACP JSON-RPC 2.0 client, wire models, and unified diff parser |
| `termwiz` `=0.23.3` + `windows-sys` `0.61.2` | Windows-only raw console VT/`win32-input-mode` input thread (`CONIN$`/`CONOUT$`) |

---

## Directory & Module Layout

```text
crates/hex-tui/
├── Cargo.toml
├── src/
│   ├── lib.rs                 # Library crate `hex_tui` exports
│   ├── main.rs                # `hex-tui` binary, clap Args, ACP startup, FramePacer loop, --check mode
│   ├── app.rs                 # Core App state, SQLite ledger queries, focus/mode logic, catalog sync
│   ├── event.rs               # AppEvent enum, biased EventHandler, 50ms FramePacer
│   ├── history.rs             # TranscriptStore: opt-in append-only disk archive (2 MiB RAM budget)
│   ├── sidebar.rs             # SidebarState, sections, width/drag persistence (.hex/data/tui-sidebar.json)
│   ├── windows_input.rs       # Windows Console VT + win32-input-mode reader with UTF-16 surrogate & paste decoding
│   └── ui/
│       ├── mod.rs             # Top-level draw() stack and F1 help overlay
│       ├── animation.rs       # Single-clock spinners, pulse, shimmer, entrance stagger, HEX_REDUCED_MOTION
│       ├── style.rs           # HexTheme (COLORTERM truecolor / 256-color fallback)
│       ├── design.rs          # 8 named color skins, banners, tips, fallback tool/skill catalogs
│       ├── keymap.rs          # Authoritative BINDINGS table driving footers, badges, help, and tests
│       ├── input.rs           # Multiline prompt composer, grapheme caret, horizontal/vertical cursor layout
│       ├── markdown.rs        # Dependency-free streaming-tolerant markdown renderer
│       ├── transcript.rs      # Virtualized viewport slicing, 2 MiB render cache, incremental StreamingMarkdown
│       ├── diff.rs            # Unified, side-by-side (/sbs), and inline Sage/Rose diff views
│       ├── right_rail.rs      # Context, Plan, Changed files, Subagents rail + F4 settings overlay
│       ├── approval.rs        # Approval widget helpers
│       ├── dialogue.rs        # Legacy/helper dialogue card utilities
│       ├── footer.rs          # Footer layout helper
│       ├── header.rs          # Header layout helper
│       ├── status_bar.rs      # Status bar helper
│       ├── widgets/
│       │   └── ascii_logo.rs  # StartWordmark (4x22) and SessionWordmark (2x15)
│       └── screens/
│           ├── start.rs       # Screen 0: Start screen, wordmark, composer, resumable sessions
│           ├── session.rs     # Screen 1: Active cockpit split (transcript + composer + right rail)
│           ├── palette.rs     # Fuzzy slash-command & navigation palette (/) / Ctrl+P
│           ├── permission.rs  # Fail-closed operator approval modal
│           ├── subagent.rs    # Subagent drill-down modal (/subagents)
│           └── error_view.rs  # Inline DiagnosticFailure card for command/test failures
└── tests/                     # 23 integration & visual-parity test suites
```

---

## Features & Surfaces

### 1. Screens & Modals

- **Start Screen (`ui/screens/start.rs`)**:
  - Vertically centered 4×22 geometric `HEX` pixel wordmark (`StartWordmark`), workspace path, git branch, modified file count, and indexed file count.
  - 5-row rounded prompt composer with rotating placeholder prompts, hover-animated border, mode/model badge (`agent` / `plan` / `code`), and shortcut hint badges.
  - **Resumable Sessions List**: Queries `.hex/data/ledger.db` directly for past tasks, displays top 3 collapsed or a 10-row sliding window expanded (`+ show all` / `− show less`), and supports mouse/keyboard selection plus two-step cascading deletion (`d` / `Delete` / `✕` click across 13 ledger tables).
- **Session Cockpit (`ui/screens/session.rs`)**:
  - Outer rounded frame with workspace/branch header, left scrollable transcript + 5-row composer + footer hints, and a draggable-divider right rail (automatically hidden below `92` terminal columns).
  - Smooth 240ms screen reveal transition from Start to Session (disabled when `HEX_REDUCED_MOTION=1`).
- **Right Rail & Sidebar Customization (`sidebar.rs`, `ui/right_rail.rs`)**:
  - Four toggleable/reorderable/collapsible sections: **Context** (ASCII progress bar + model token budget), **Plan** (live checklist parsed from assistant messages, `.gsd/phases/*/PLAN.md`, or SQLite `plans.dag_json`), **Changed** (`git diff --numstat` with Sage `+N` / Rose `−N` counts and preview expansion), and **Agents** (subagent telemetry + approval-blocked indicator).
  - Independent scroll wheel support and click hit-targets (`[f4]` settings, `▸`/`▾` collapse, `+ N more` files).
  - Preferences persist atomically (with symlink protection) to `.hex/data/tui-sidebar.json`.
- **Command & Navigation Palette (`ui/screens/palette.rs`)**:
  - Opened via `/` on an empty prompt or `Ctrl+P` anywhere without disturbing buffered composer text.
  - Populated at compile time from [`hex_cli/command_catalog.json`](../../hex_cli/command_catalog.json) (35 TUI-supported slash commands) plus navigation actions (`Switch session`, `Customize sidebar`). Supports grouped browsing, subsequence fuzzy matching with bold match highlighting, and typed command arguments.
- **Fail-Closed Permission Modal (`ui/screens/permission.rs`)**:
  - Displays request ID, tool name, risk level, `cwd`, parameters, reason, and blast radius.
  - Enforces a per-frame `approval_can_allow` invariant: `y` (*Allow once*), `a` (*Allow matching calls in this task*), and `Enter` are disabled whenever pending or when any authority-bearing field/control is clipped on a tiny terminal (falling back to resize/deny-only instructions).
- **Subagent Inspection Modal (`ui/screens/subagent.rs`)**:
  - Opened with `/subagents` (`session/subagents` ACP RPC). Shows prompt brief (`e` toggles 2 vs 6 rows), scrollable activity stream with top fade, live shimmer (`Executing <name>...`), and elapsed timer/outcome badge (`pass` / `fail`).
- **Inline Diagnostic Error Card (`ui/screens/error_view.rs`)**:
  - Parses failed tool JSON (`exit_code`, `stdout`, `stderr`, `error`), pytest assertions (`FAILED`, `E   assert ... == ...`), and Python/JS stack traces into an inline transcript card with Sage `expected`, Rose `received`, stack preview, and a `1 failing` footer counter.

### 2. Virtualized Transcript, Streaming Markdown & Diffs

- **Incremental Virtualized Rendering (`ui/transcript.rs`, `ui/markdown.rs`)**:
  - Dependency-free streaming-tolerant markdown parser (headings, fenced code blocks, blockquotes, horizontal rules, ordered/unordered nested lists, `**bold**`, `*italic*` preserving `snake_case`, `~~strike~~`, inline code, `[links](url)`, and Unicode display-width wrapping).
  - Memoizes item height signatures, caches rendered lines within a 2 MiB (`256`-entry) LRU budget, binary-searches visible blocks for viewport slicing, anchors scroll on reflow, and seals completed markdown blocks incrementally while streaming tokens.
- **Diff Viewer (`ui/diff.rs`)**:
  - Supports unified diffs, full-screen synchronized two-column side-by-side diffs (`/sbs` or `F2`), and inline Sage/Rose diff previews in the session stream. Diffs exceeding 64 KiB or 512 lines are archived to `TranscriptStore` with a pointer to `/transcript <id>`.

### 3. Durable Persistence (`src/app.rs`, `src/history.rs`)

- **SQLite Ledger (`.hex/data/ledger.db`)**: Read-only queries load resumable sessions, dialogue turns, plan DAGs, and paged tool results (`tool_calls`); session deletion executes an atomic `Immediate` transaction across 13 related tables.
- **Native Archive (`TranscriptStore` in `.hex/data/tui_transcripts/<session_id>/`)**: Opt-in append-only `<id>.txt` + `<id>.json` archive (`0700`/`0600` permissions on POSIX, symlink-checked and file-locked). Keeps active RAM capped at `TRANSCRIPT_BUDGET` (`2 MiB`), truncates displayed message bodies at `64 KiB` with `/transcript <id>` hints, caps command history at `256 KiB`, and serves 4 KiB UTF-8-safe `/transcript` and 32-record `/history` pages.

### 4. Themes, Skins & Animations (`ui/style.rs`, `ui/design.rs`, `ui/animation.rs`)

- **Truecolor & 256-Color Auto-Detection**: Inspects `COLORTERM` (`truecolor` / `24bit`) to select 24-bit RGB or xterm-256 fallback tokens.
- **8 Skins**: `hex` / `hex_dark` (via `design::skin`), plus CLI `--skin` options `sapphire`, `amber`, `cyberpunk`, `matrix`, `nord`, `mono`, and `light` (auto-selected when `COLORFGBG` indicates a light background).
- **Motion Engine**: Single-clock 10-step 90ms Braille spinner, 1.1s cursor blink, 2.2s sine pulse, 2.4s shimmer sweep, 600ms staggered entrance rise, and 240ms screen reveal — all stilled when `HEX_REDUCED_MOTION` is truthy (`1`, `true`, `yes`, `on`).

---

## CLI Options (`hex-tui`)

```text
Usage: hex-tui [OPTIONS]

Options:
      --check                        Run headless virtual verification and exit 0
  -p, --project <PROJECT>            Project ID [default: default]
  -w, --workspace <WORKSPACE>        Workspace path override
  -m, --model <MODEL>                Active model provider override
      --skin <SKIN>                  HEX visual skin [possible values: sapphire, amber, cyberpunk, matrix, nord, mono, light]
      --cow <COW>                    CoW snapshot provider backend [default: OverlayFS]
  -s, --session <SESSION>            Session ID to attach or resume
      --name <NAME>                  Assistant display name (default: HEX)
      --max-tokens <MAX_TOKENS>      Maximum context window tokens [alias: --context-window]
  -h, --help                         Print help
  -V, --version                      Print version
```

---

## Keybindings (`src/ui/keymap.rs`)

| Context | Key | Action |
| :--- | :--- | :--- |
| **Global** | `Ctrl+C` | Cancel running turn, then quit |
| | `F1` | Toggle help & command overlay |
| | `F2` | Toggle diff viewer pane |
| | `F3` | Cycle mode (`agent` → `plan` → `code`) |
| | `F4` | Toggle sidebar customization panel |
| **Start Screen** | `/` | Open command palette (on empty prompt or resume list) |
| | `Up` / `Down` | Move between prompt composer and resumable sessions |
| | `PageUp` / `PageDown` | Page through resumable sessions |
| | `Enter` | Submit prompt or resume selected session |
| | `d` / `Delete` | Two-step delete selected session (`Esc` cancels) |
| | `Tab` | Cycle mode |
| **Session** | `Ctrl+P` | Open command palette |
| | `/` | Open command palette when composer is empty |
| | `Tab` | Cycle mode |
| | `Enter` / `Shift+Enter` | Send prompt / insert newline |
| | `Esc` | Interrupt active turn or close modal |
| | `PageUp` / `PageDown` | Scroll transcript (5 lines) |
| | `Up` / `Down` | Command history (when composer non-empty) or scroll |
| | `Ctrl+Up` / `Ctrl+Down` | Scroll right-rail sidebar |
| | Mouse | Wheel scrolls pane under cursor; drag `│` divider to resize right rail; click sidebar toggles |
| **Palette** | `Up` / `Down` / `Tab` / `Enter` / `Esc` | Navigate, run command/argument, or dismiss |
| **Approval** | `y` / `a` / `n` / `d` / `Enter` / `Esc` | Allow once / allow task scope / deny / toggle diff / confirm |
| **Subagents** | `Up` / `Down` / `e` / `Esc` / `Tab` | Scroll activity tail / expand prompt brief / close |
| **Sidebar Settings** | `Up` / `Down` / `Space` / `c` / `,` `.` / `+` `-` / `a` / `r` | Select, show/hide, collapse, reorder, resize width, auto-width, reset |

---

## Slash Commands (`hex_cli/command_catalog.json`)

The palette exposes all **35** TUI-supported commands from the shared catalog plus two navigation entries (`Switch session`, `Customize sidebar`):

- **Local TUI**: `/mode`, `/plan`, `/agent`, `/code`, `/diff`, `/sbs`, `/history`, `/transcript`, `/tool-result`, `/tools`, `/clear`, `/quit`
- **ACP / RPC-Backed**: `/help`, `/status`, `/session-history`, `/history-turn`, `/rollback`, `/fork`, `/skills`, `/scan`, `/model`, `/compressor`, `/budget`, `/quota`, `/blast`, `/proof`, `/memory`, `/remember`, `/reflect`, `/search`, `/doctor`, `/recipe`, `/lsp`, `/subagents`, `/react`

---

## Environment Variables

- `HEX_AGENT_NAME`: Override assistant display name (`--name` takes precedence).
- `HEX_REDUCED_MOTION`: Set to `1`, `true`, `yes`, or `on` to disable spinners, shimmers, blinking, and transitions.
- `COLORTERM`: Set to `truecolor` or `24bit` for 24-bit RGB palette rendering.
- `COLORFGBG`: Auto-selects the `light` skin when background index is light (`>= 7`).
- `HEX_PYTHON_BIN` / `VIRTUAL_ENV` / `HEX_REPO_ROOT`: Passed through to `hex-protocol` when spawning the ACP backend.

---

## Building, Running & Testing

```bash
# Build native TUI binary
cargo build --release -p hex-tui

# Run interactively (usually invoked via `hex` / `hex tui` launcher)
cargo run -p hex-tui -- --workspace /path/to/repo

# Run headless TestBackend verification across Start, Session, and Approval screens
cargo run -p hex-tui -- --check

# Run all unit + 23 integration test suites
cargo test -p hex-tui

# Run the ignored counting-allocator memory benchmark
cargo test -p hex-tui --test test_memory_benchmark -- --ignored --nocapture
```

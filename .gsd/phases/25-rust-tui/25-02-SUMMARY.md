# Phase 25 Plan 02: `crates/hex-tui` (Ratatui Alt-Screen Cockpit & CLI Launcher) Summary

**Phase:** Phase 25 — Native Rust Terminal Cockpit (Ratatui + Crossterm) & Protocol Crate (Hermes Parity Pillar 2)  
**Task:** 25-02  
**Wave:** 2  
**Status:** Completed & Empirically Verified  

---

## 1. Objectives Completed

1. **`crates/hex-tui/Cargo.toml`:**
   - Configured crate dependencies: `ratatui = "0.29"`, `crossterm = { version = "0.28", features = ["event-stream"] }`, `tokio = { version = "1.0", features = ["full"] }`, `hex-protocol = { path = "../hex-protocol" }`, `clap = { version = "4.4", features = ["derive"] }`, `anyhow = "1.0"`, `futures = "0.3"`, `unicode-width = "0.1"`, `serde`, and `serde_json`.
   - Declared both `[lib]` (`hex_tui`) and `[[bin]]` (`hex-tui`) targets.

2. **Native Rust Ratatui Cockpit Architecture:**
   - **`src/main.rs`:** CLI parsing with `clap`, alternate-screen terminal lifecycle with `crossterm`, safe panic hook restoring normal terminal state upon crash, async ACP client connection/spawning, and headless `--check` virtual test mode.
   - **`src/app.rs`:** Application state machine managing dialogue history, collapsible tool calls (with duration in ms and exit status), diff viewer state, command history navigation (`↑`/`↓`), slash-command suggestions, and R3/R4 operator approval interrupts.
   - **`src/event.rs`:** Asynchronous event loop combining Crossterm key and mouse events with 20 FPS ticks and incoming ACP JSON-RPC notifications.
   - **Modular UI Widgets (`src/ui/`):**
     - `header.rs`: Sticky top header with stylized ASCII banner, active model, CoW snapshot mode, fencing lease token, and connection status badge.
     - `dialogue.rs`: Scrollable dialogue history viewport with markdown code blocks, role badges (`[USER]`, `[ASSISTANT]`, `[SYSTEM]`, `[TOOL]`), and expandable/collapsible tool calls.
     - `diff.rs`: Colorized unified diff viewer rendering added lines in green, deleted lines in red, context lines, and file headers.
     - `input.rs`: Interactive command prompt with cursor positioning, history recall, and floating slash-command suggestion popup.
     - `status_bar.rs`: Bottom bar displaying keybindings (`[Enter] Submit`, `[Tab] Focus`, `[F1] Help`, `[Ctrl+C] Quit`, `[y/n] Approve/Reject`).
     - `approval.rs`: Centered modal dialog for approval interrupts showing risk level, tool name, parameters, and action buttons (`[y] Approve`, `[n] Reject`, `[d] Diff`, `[Esc] Dismiss`).

3. **Headless Verification Flag (`--check`):**
   - Supports `hex-tui --check` that runs headlessly using `ratatui::backend::TestBackend::new(120, 36)`, renders full layout frames and approval modals, verifies buffer content, prints confirmation, and exits 0 without opening interactive raw terminal.

4. **Python CLI Integration & Launcher (`src/harness_core/cli.py` & `src/harness_core/tui_launcher.py`):**
   - Added dedicated `hex tui` subcommand.
   - Added `--ratatui` flag to `hex chat`.
   - Extracted `build_parser()` in `cli.py` for programmatic inspection.
   - Implemented binary resolver in `src/harness_core/tui_launcher.py`: checks `HEX_TUI_BIN` env var, target release/debug build paths, standard PATH via `which`, or compiles on demand with `cargo build -p hex-tui`.

5. **Empirical Verification:**
   - Authored unit and rendering tests in `crates/hex-tui/tests/ui_rendering_test.rs` and `src/app.rs`.
   - Authored Python launcher test suite in `tests/test_rust_tui_launcher.py`.
   - `cargo test --workspace`: **26/26 tests passing** (11 unit + 1 live + 3 protocol in `hex-protocol`; 5 lib + 6 rendering in `hex-tui`).
   - `.venv/bin/python -m unittest tests/test_rust_tui_launcher.py`: **8/8 tests passing**.
   - Full repository test suite: **654/654 tests passing** with zero failures.

---

## 2. Commit History

- `feat(phase-25): implement hex-tui crate and ratatui widgets` (commit `d59d87c`)
- `feat(phase-25): wire hex-tui launcher into python cli and add integration tests` (commit `4a2530d`)
- `feat(phase-25): complete phase 25 ratatui terminal cockpit and update documentation` (upcoming)

---

## 3. Verification Artifacts

```
$ cargo test --workspace
test result: ok. 11 passed (hex_protocol unit)
test result: ok. 1 passed (live_acp_test)
test result: ok. 3 passed (protocol_test)
test result: ok. 5 passed (hex_tui unit)
test result: ok. 6 passed (ui_rendering_test)
Total: 26 passed; 0 failed; 0 ignored.

$ .venv/bin/python -m unittest tests/test_rust_tui_launcher.py
Ran 8 tests in 0.471s
OK

$ .venv/bin/python -m harness_core.cli tui --check
HEX TUI headless verification passed: 1 virtual frame rendered successfully.
```

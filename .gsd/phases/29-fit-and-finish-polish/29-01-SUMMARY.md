# Phase 29 Plan 01 Summary: Rust TUI Cockpit Polish, Widget Wiring & Dialogue Scrolling Fix

**Execution Date:** 2026-11-14  
**Plan:** `.gsd/phases/29-fit-and-finish-polish/29-01-PLAN.md`  
**Status:** Completed & Empirically Verified (39/39 Rust workspace tests green, 8/8 Python launcher tests green)

---

## 1. Objectives Delivered

1. **Cockpit Widget Wiring & 4-Tier Vertical Layout (`crates/hex-tui/src/ui/mod.rs`):**
   - Implemented responsive 4-tier vertical constraint stack in `draw()`:
     - Top telemetry header: `Constraint::Length(4)` (renders `header::render(frame, app, chunks[0])`). When terminal height < 18 rows, collapses header constraint to `Constraint::Length(0)` preserving maximum dialogue height for constrained viewports.
     - Transcript & diff main viewport: `Constraint::Min(6)` (renders dialogue and optional diff viewer).
     - Persistent status bar: `Constraint::Length(1)` (renders `status_bar::render(frame, app, chunks[2])` immediately above composer).
     - Composer input box: `Constraint::Length(3)` (renders `input::render(frame, app, chunks[3])`).
   - Updated help modal keybindings documentation with `[Tab]` viewport focus cycling and `[F3]` execution mode switching.

2. **Visual Wrapped Line Scrolling Calculation (`crates/hex-tui/src/ui/dialogue.rs`):**
   - Replaced naive logical line count calculation (`lines.len()`) with dynamic visual wrapped row computation across all lines against `area.width`:
     ```rust
     let width = area.width.max(1) as usize;
     let mut total_visual_lines = 0;
     for line in &lines {
         let line_len: usize = line.spans.iter().map(|s| s.content.chars().count()).sum();
         let rows = if line_len == 0 {
             1
         } else {
             (line_len + width - 1) / width
         };
         total_visual_lines += rows;
     }
     let max_scroll = total_visual_lines.saturating_sub(area.height as usize);
     ```
   - Clamped scroll position against `max_scroll` and pinned bottom view cleanly when `app.follow_dialogue` is active, completely eliminating assistant response line clipping.

3. **Dialogue & Diff Scrolling Ergonomics (`crates/hex-tui/src/app.rs`):**
   - Updated `scroll_up()` and `scroll_down()` to handle both `Focus::Dialogue` and `Focus::Input`. Mouse wheel and Up/Down navigation (when input buffer is empty) scroll the transcript naturally without requiring explicit pane focus switching.
   - Preserved `self.follow_dialogue = false` on upward scroll to prevent snapping back to the bottom during manual inspection.
   - Added `handle_page_up()` and `handle_page_down()` stepping scroll by 5 rows per keypress.
   - Added `handle_tab()` cycling viewport focus between `Input` -> `Dialogue` -> `Diff` (when diff pane is open), while continuing to accept slash autocompletions when suggestions are active.

4. **Keybindings & Event Loop Enhancements (`crates/hex-tui/src/main.rs`):**
   - Rebound `Tab` key to `app.handle_tab()` for instant focus cycling and slash suggestion acceptance.
   - Bound `KeyCode::F(3)` to `app.cycle_mode()` for cycling execution mode (`agent` -> `plan` -> `code`).
   - Wired `KeyCode::PageUp` and `KeyCode::PageDown` to 5-row scroll step methods.
   - Maintained smooth double-step mouse wheel scrolling (`app.scroll_up()` / `app.scroll_down()` twice per event).

5. **Comprehensive Integration Test Suite (`crates/hex-tui/tests/test_ui.rs`):**
   - Authored 5 dedicated integration tests using `ratatui::backend::TestBackend`:
     - `test_cockpit_full_layout_rendering`: Asserts header banner, branch, model, status bar, dialogue messages, and input prefix render without collision.
     - `test_dialogue_scrolling_with_input_focus`: Validates scrolling transcript while remaining in `Focus::Input`, confirming scroll delta and `follow_dialogue` updates.
     - `test_tab_key_focus_cycling`: Tests `Tab` cycling through `Input` -> `Dialogue` -> `Diff` -> `Input` and slash suggestion acceptance.
     - `test_page_up_page_down_steps`: Verifies 5-row scroll jump for both dialogue and diff viewports.
     - `test_visual_wrapped_lines_scroll_calculation`: Verifies line wrapping in constrained widths and scroll calculations.

---

## 2. Verification Evidence

### Rust Workspace Test Suite (`cargo test --workspace`):
```text
running 13 tests (hex-protocol unit) ... ok
running 1 test (live_acp_test) ... ok
running 3 tests (protocol_test) ... ok
running 6 tests (hex-tui unit) ... ok
running 5 tests (crates/hex-tui/tests/test_ui.rs) ... ok
running 11 tests (crates/hex-tui/tests/ui_rendering_test.rs) ... ok
Total: 39 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
```

### Headless Virtual Verification (`cargo run -p hex-tui -- --check`):
```text
HEX TUI headless verification passed: 1 virtual frame rendered successfully.
```

### Python Launcher Integration Tests (`python3 -m unittest tests/test_rust_tui_launcher.py`):
```text
Ran 8 tests in 0.148s
OK
```

---

## 3. Atomic Commits

1. `feat(phase-29): wire cockpit header and status bar, fix wrapped line scrolling` (commit `008ba47`)
2. `feat(phase-29): implement Tab focus cycling, PageUp/Down scroll steps, and UI integration tests` (commit `08ef212`)

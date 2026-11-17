# Phase 38 Plan 02 Summary: Dual-Pane Shell, Session Picker & Message Feed

## 1. Overview
Phase 38 Plan 02 builds the native GPUI dual-pane desktop shell and streaming conversation cockpit directly copying the Hermes Desktop UI in Zed's `gpui` framework (v0.2.2). It implements the collapsible 260px left sidebar (workspace header, git branch pill, "New Chat" button with `⌘N` badge, session search, pinned items, grouped chronological history, and provider status dot) alongside the main conversation view (message feed, collapsible `<think>` reasoning folds with live elapsed timers, distinct tool call cards with sage/rose inline diffs, and bottom multiline prompt composer with ASCII token gauge).

Key accomplishments:
1. **Dual-Pane Shell & Left Sidebar Navigation (`AppShell`, `SidebarView`, `SessionListView`)**:
   - Built 260px collapsible sidebar with horizontal split-view and low-contrast divider (`border_subtle`).
   - Workspace header rendering project directory name (`Harness`), folder icon, and git branch pill (`experimental-features`).
   - Prominent elevated "New Chat" trigger card (`#27272a`) with compose icon and `⌘N` shortcut badge.
   - Session search filter with instant query filtering.
   - Chronological session grouping into "Today", "Yesterday", "Previous 7 Days", and "Older" categories.
   - Human-readable relative time formatting using `chrono` ("just now", "12m ago", "3h ago", "2d ago", "Nov 12").
   - Pinned sessions section, active session selection highlight (`#27272a` background with `#7aa7ff` electric blue left border), rename, archive, and delete operations.
   - Provider status chip with live state dot: Green (`#8fbf93` Connected), Amber (`#f59e0b` Generating), Red (`#c98b8b` Offline), and active model label (`Anthropic Claude 3.7 Sonnet`).
   - Bottom Settings (`⚙️`) and Profile (`👤 Operator`) footer.
2. **Message Feed, Reasoning Folds & Tool Call Cards (`MessageFeed`, `UserMessageCard`, `AssistantMessageView`, `ThinkingDisclosure`, `ToolCallCard`, `DiffView`)**:
   - `UserMessageCard`: Right/center aligned card with `#18181b` card surface, rounded corners, user avatar badge, and timestamp.
   - `AssistantMessageView`: Left aligned response with assistant avatar (`⚡`), active model label, action bar (Copy, Regenerate, Branch), and streaming pulse indicator.
   - `ThinkingDisclosure`: Collapsible `<think>` fold with caret (`▾` / `▸`), brain glyph (`🧠`), and dynamic Hermes headers: "Thinking..." with live pulse while streaming, "Thought briefly" (< 1s), "Thought for 6s" (>= 1s), and "Thought" when settled.
   - `ToolCallCard`: Tool header with tool-specific icons (`⚡` run_command, `📝` write_file, `📄` read_file, `🌐` web_search, `🤖` delegate), execution status badge (`✓` sage green, `✗` rose red, `⏳` amber), duration in milliseconds, collapsible input JSON parameters, and collapsible output observation box.
   - `DiffView`: Integrated `hex_protocol::diff::UnifiedDiff`, file header with `+adds` / `-dels` summary, dual old/new line number gutters, gutter prefix symbols (`+`, `-`, `@@`), and truecolor highlighting: sage green (`#8fbf93` on 12% tint) for additions and rose red (`#c98b8b` on 10% tint) for deletions.
3. **Bottom Multiline Composer, ASCII Token Gauge & Model Pill (`PromptComposer`, `TokenGaugeWidget`, `ModelSelectorPill`, `SendButton`)**:
   - Auto-growing multiline input area dynamically sizing from 1 line (36px) up to 8 lines (200px) before internal scrolling.
   - Key event handlers: `Enter` to submit, `Shift+Enter` for newline break, `Cmd+Enter` forced submit, and clear button (`×`).
   - `TokenGaugeWidget`: Visual ASCII block progress bar `▕██████░░░░░░░░░░▏` with engineering format (`24.5k / 128k (19%)`) and 3-stage threshold color transitions (< 70% Normal in `#7aa7ff`, 70-90% Warning in `#f59e0b`, > 90% Critical in `#c98b8b`).
   - `ModelSelectorPill`: Compact toolbar pill with provider brand dot (Anthropic amber `#d97706`, OpenAI emerald `#10b981`, Google blue `#3b82f6`), model name label, and chevron down (`▾`).
   - `SendButton`: Circular send button with `↑` icon (active `#7aa7ff`) and stop button `⏹` icon (active `#c98b8b` during streaming).

---

## 2. Tasks Completed

### Task 1: Implement Dual-Pane Shell Layout & Left Sidebar Navigation
- **Files Modified / Created:**
  - `App/hex-desktop/src/ui/mod.rs`: Created UI module root.
  - `App/hex-desktop/src/ui/session_list.rs`: Implemented `SessionItem`, `SessionListView`, `SessionGroupCategory`, relative time formatting, and date classification.
  - `App/hex-desktop/src/ui/sidebar.rs`: Implemented `SidebarView`, `ProviderState`, `ProviderStatus`, workspace header, new chat button, search input, and status chip.
  - `App/hex-desktop/src/ui/shell.rs`: Implemented `AppShell`, `ShellView`, 260px collapsible sidebar layout, and split divider.
  - `App/hex-desktop/src/lib.rs`: Re-exported UI types.
  - `App/hex-desktop/src/window.rs`: Integrated `AppShell` into `DesktopWindow`.
- **Commit:** `ef08011 feat(desktop): implement dual-pane shell layout and left sidebar navigation`

### Task 2: Implement Message Feed, Collapsible Reasoning Folds & Tool Call Cards
- **Files Modified / Created:**
  - `App/hex-desktop/src/ui/diff_view.rs`: Implemented `DiffView` with sage green/rose red additions/deletions, line number gutters, and hunk headers.
  - `App/hex-desktop/src/ui/reasoning.rs`: Implemented `ThinkingDisclosure` with elapsed timer formatting and dynamic Hermes labels.
  - `App/hex-desktop/src/ui/tool_card.rs`: Implemented `ToolCallCard`, `ToolStatus`, icon detection, collapsible input/output, and integrated diff view.
  - `App/hex-desktop/src/ui/message.rs`: Implemented `UserMessageCard` and `AssistantMessageView`.
  - `App/hex-desktop/src/ui/feed.rs`: Implemented `MessageFeed` and `MessageItem`.
  - `App/hex-desktop/src/ui/shell.rs`: Connected `MessageFeed` into `AppShell`'s main conversation pane.
  - `App/hex-desktop/src/ui/mod.rs` & `App/hex-desktop/src/lib.rs`: Re-exported components.
- **Commit:** `df51bfb feat(desktop): implement message feed, collapsible reasoning folds and tool call cards`

### Task 3: Implement Bottom Composer, Token Gauge & Model Selector Pill
- **Files Modified / Created:**
  - `App/hex-desktop/src/ui/token_gauge.rs`: Implemented `TokenGaugeWidget`, `render_ascii_progress_bar`, `format_token_number`, and `GaugeThreshold`.
  - `App/hex-desktop/src/ui/model_pill.rs`: Implemented `ModelSelectorPill`, `provider_dot_color`, and `SendButton`.
  - `App/hex-desktop/src/ui/composer.rs`: Implemented `PromptComposer` with multiline auto-growth, Enter/Shift+Enter key handling, and controls toolbar.
  - `App/hex-desktop/src/ui/shell.rs`: Connected `PromptComposer` into `AppShell`'s bottom container.
  - `App/hex-desktop/src/ui/mod.rs` & `App/hex-desktop/src/lib.rs`: Re-exported composer types.
  - `App/hex-desktop/tests/test_feed_and_composer.rs`: Comprehensive integration and component test suite covering token gauge, reasoning folds, diff classification, feed streaming, composer key handling, and GPUI `TestAppContext`.
- **Commit:** `527a69a feat(desktop): implement bottom composer, token gauge, and model selector pill`

---

## 3. Verification & Empirical Proof

### 1. Dedicated Feed & Composer Test Suite
```bash
cargo test -p hex-desktop --test test_feed_and_composer
```
**Output:**
```
     Running tests/test_feed_and_composer.rs (target/debug/deps/test_feed_and_composer-04c29005209af5e7)

running 6 tests
test test_reasoning_fold_toggle_and_labels ... ok
test test_token_gauge_formatting_and_thresholds ... ok
test test_diff_line_classification_and_colors ... ok
test test_prompt_composer_and_model_pill ... ok
test test_message_feed_and_tool_cards ... ok
test test_full_app_shell_in_gpui_context ... ok

test result: ok. 6 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
```

### 2. Hex-Desktop Package Test Suite
```bash
cargo test -p hex-desktop
```
**Output:**
```
     Running unittests src/lib.rs (target/debug/deps/hex_desktop-f411a606bcdf940d)
running 24 tests: 24 passed; 0 failed

     Running tests/test_feed_and_composer.rs (target/debug/deps/test_feed_and_composer-04c29005209af5e7)
running 6 tests: 6 passed; 0 failed

     Running tests/test_theme_and_window.rs (target/debug/deps/test_theme_and_window-332b6a3de01a2bac)
running 4 tests: 4 passed; 0 failed

test result: ok. 34 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s
```

### 3. Full Workspace Test Suite
```bash
cargo test --workspace
```
**Output:**
```
     Running unittests src/lib.rs (hex_desktop): 24 passed; 0 failed
     Running tests/test_feed_and_composer.rs (hex_desktop): 6 passed; 0 failed
     Running tests/test_theme_and_window.rs (hex_desktop): 4 passed; 0 failed
     Running unittests src/lib.rs (hex_protocol): 13 passed; 0 failed
     Running tests/live_acp_test.rs (hex_protocol): 1 passed; 0 failed
     Running tests/protocol_test.rs (hex_protocol): 3 passed; 0 failed
     Running unittests src/lib.rs (hex_tui): 16 passed; 0 failed
     Running tests/test_context_window.rs (hex_tui): 3 passed; 0 failed
     Running tests/test_design_system.rs (hex_tui): 8 passed; 0 failed
     Running tests/test_screens_overlays_and_errors.rs (hex_tui): 4 passed; 0 failed
     Running tests/test_screens_start_and_session.rs (hex_tui): 5 passed; 0 failed
     Running tests/test_session_management_and_resume.rs (hex_tui): 4 passed; 0 failed
     Running tests/test_ui.rs (hex_tui): 8 passed; 0 failed
     Running tests/test_virtual_render.rs (hex_tui): 2 passed; 0 failed
     Running tests/ui_rendering_test.rs (hex_tui): 9 passed; 0 failed

Total Rust Workspace Tests: 106 passed; 0 failed; 0 regressions.
```

---

## 4. Deviations & Discoveries
1. **GPUI Overflow Styling in Div**: In `gpui-0.2.2`, the `Styled` macro generates `overflow_hidden()`, `overflow_x_hidden()`, and `overflow_y_hidden()` methods rather than an arbitrary `overflow_y_scroll()`. Containers in the feed and sidebar use `overflow_y_hidden()` combined with flex layout to maintain clean bounds without overflowing outside the window.
2. **Trailing Newlines in Prompt Line Counting**: Rust's standard library `str::lines()` treats a trailing newline `\n` as a line terminator rather than creating an empty trailing line (so `"foo\n".lines().count() == 1`). Using `input_text.split('\n').count()` accurately counts trailing newlines when users press `Shift+Enter` to add a new empty line in the composer.
3. **Macro Recursion with Test Attributes**: In `App/hex-desktop`, `gpui` defines a `test` macro for async UI tests. In standard synchronous unit test modules, importing `use core::prelude::v1::test;` and configuring `#![recursion_limit = "256"]` in `src/lib.rs` prevents compiler macro expansion recursion limits and allows standard unit tests and GPUI `TestAppContext` tests to coexist seamlessly.

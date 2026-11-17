# Deep Fit & Finish Audit: Terminal Cockpit, Visual Polish, Animations & TUI Ergonomics

**Audit Reference:** Hermes Agent (`benchmarks/hermes-agent/ui-tui/`)  
**Audit Target:** HEX Autonomous AI Workbench (`crates/hex-tui/`, `harness_core/tui.py`, `harness_core/ui_selector.py`)\
**Audit Scope:** DOMAIN 2 — Terminal Cockpit, Visual Polish, Animations & TUI Ergonomics  
**Date:** November 2026  
**Auditor:** GSD Deep Research Agent  

---

## Executive Summary & Scorecard

| Dimension | Hermes Agent Reference | HEX Target Cockpit | Parity / Fit & Finish Score | Key Gap / Finding |
| :--- | :--- | :--- | :---: | :--- |
| **1. Visual Polish & Aesthetics** | 36-token WCAG theme system, Dark/Light duality, ANSI-256 luminance normalization, Yoga flexbox padding & margins | 5 dark skins (`AMBER`, `CYBERPUNK`, `MATRIX`, `NORD`, `MONO`), Ratatui card containers, ASCII intro banner | **78 / 100** | Zero light terminal support; Telemetry header widget (`header.rs`) is disconnected and omitted from `draw()` |
| **2. Animations & Responsiveness** | 14 Braille spinners, bounded shimmer clock (30s budget), single-pass diff optimizer (`optimizer.ts`), O(tail) incremental markdown parsing | 10-frame braille array, Python phrase morphing wave, Ratatui 20 FPS differential buffer | **72 / 100** | Dialogue re-parses all markdown lines on every 50ms tick; no frame timing or backpressure metrics |
| **3. Keyboard Navigation & Scrolling** | Sub-row precision wheel acceleration, exact Yoga virtual heights, scroll-lock pinning | Ratatui scroll state + in-place ANSI windowing in `ui_selector.py` | **54 / 100** | **CRITICAL BUG:** `app.focus` defaults to `Input` and `switch_focus` is never bound in `main.rs`, making mouse and PgUp/PgDn scrolling completely dead |
| **4. Interactive Widgets** | 5-stage interactive modal Model Picker with fuzzy search & reasoning ladder; full overlay ecosystem | Split-pane Diff viewer, R3/R4 Approval modal, floating Slash autocomplete, Python `select_option` | **70 / 100** | No interactive Model Picker widget in Rust cockpit (`/model` is text-only); no fuzzy matching in slash autocomplete |
| **OVERALL RATING** | **94 / 100 (Polished Production)** | **68.5 / 100 (Alpha / Feature-Rich but Rough Edges)** | **Grade: C+ (Needs Fit & Finish Fixes)** | **Immediate blockers in scrolling and layout wiring require remediation** |

---

## 1. Visual Polish & Aesthetics

### 1.1 Color Schemes & Theming System

#### Hermes Agent Reference
- **Theme Architecture:** Defined in `benchmarks/hermes-agent/ui-tui/src/theme.ts#L6-L51`. `ThemeColors` exports a comprehensive 36-token semantic palette (`primary`, `accent`, `border`, `text`, `muted`, `completionBg`, `completionCurrentBg`, `completionMetaBg`, `label`, `ok`, `error`, `warn`, `tool`, `thinking`, `syntaxString`, `syntaxNumber`, `syntaxKeyword`, `syntaxComment`, `prompt`, `sessionLabel`, `sessionBorder`, `statusBg`, `statusFg`, `statusGood`, `statusWarn`, `statusBad`, `statusCritical`, `selectionBg`, `diffAdded`, `diffRemoved`, `diffAddedWord`, `diffRemovedWord`, `shellDollar`).
- **Contrast & Luminance Math:** Implements WCAG 2.1 color contrast calculation (`contrastRatio`, `ensureContrast`, `mix`, `relativeLuminance` in `theme.ts#L70-L79`).
- **Light Terminal Compatibility:** Normalizes dark foregrounds for light terminals (`theme.ts#L80-L100`) via `normalizeThemeForAnsiLightTerminal`, enforcing `ANSI_LIGHT_MAX_LUMINANCE = 0.72`, `ANSI_LIGHT_TARGET_LUMINANCE = 0.34`, and `ANSI_LIGHT_MIN_SATURATION = 0.22`.
- **Flash-Free Boot (`themeBoot.ts#L1-L60`):** Caches the last resolved theme and background color to `~/.hermes/tui-theme-boot.json`. This completely eliminates startup color flicker while awaiting asynchronous OSC-11 terminal background queries or gateway skin synchronization.

#### HEX Target Implementation
- **Skin Architecture:** Defined in `crates/hex-tui/src/ui/design.rs#L6-L57` and mirrored in Python `harness_core/tui.py#L136-L275`. Exports 5 skins:
  1. `AMBER`: Warm gold (`#F59E0B`), bronze shadow, amber border (`#B4762A`), dark surface (`#1C1917`), prompt `⚕`.
  2. `CYBERPUNK`: Neon cyan (`#00F5FF`), magenta (`#F472B6`), slate surface (`#0F172A`), prompt `⚡`.
  3. `MATRIX`: Phosphor emerald (`#34D399`), jade category, deep green surface (`#061C14`), prompt `⌘`.
  4. `NORD`: Polar slate (`#81A1C1`), glacier cyan, arctic surface (`#2E3440`), prompt `❄`.
  5. `MONO`: Monochrome grayscale (`#FFFFFF` to `#828282`), surface (`#1E1E1E`), prompt `>`.
- **Gaps & Deficits:**
  - **Zero Light Terminal Support:** All 5 HEX skins specify dark surfaces (`#1C1917`, `#0F172A`, `#061C14`, `#2E3440`, `#1E1E1E`) and bright foregrounds. When executed in macOS Terminal or iTerm with a white/light profile, HEX does not detect terminal luminance, causing amber/yellow text on white background with unusable sub-1.5:1 contrast ratios.
  - **No Dynamic Background Query:** HEX lacks OSC-11 (`\x1b]11;?\x1b\\`) query support or startup cache to determine background mode.
  - **Limited Tokens:** HEX `Skin` struct in `design.rs#L7-L20` has only 12 color fields and lacks syntax highlight tokens (`syntaxKeyword`, `syntaxString`), diff word-level tokens, or completion background tokens.

---

### 1.2 Layout Hierarchy, Padding & Borders

#### Hermes Agent Reference
- **Yoga Flexbox Engine:** Uses `packages/hermes-ink/src/ink/layout/yoga.ts` and `layout/node.ts` to implement true CSS Flexbox in the terminal. Supports flex wrap, dynamic stretch, paddings, margins, and nested tree layouts.
- **Hierarchical Tree Rendering (`thinking.tsx#L59-L88`):** Implements `TreeRow`, `TreeTextRow`, and `TreeRails` with branching symbols (`├─ `, `└─ `, `│ `). Accompanying `NoSelect` wrapper allows users to select text without copying gutter tree stems.
- **Overlay Dialogs (`overlayPrimitives.tsx`):** Clamped floating dialogs (`clampOverlayWidth`) with margin buffers, rounded borders, chip pills, and subtle backdrop dimming.

#### HEX Target Implementation
- **Layout System (`crates/hex-tui/src/ui/mod.rs#L24-L53`):** Ratatui constraint-based layout.
  - Dialogue transcript uses flexible height `Constraint::Min(6)`.
  - Composer prompt is locked to bottom with `Constraint::Length(3)`.
  - Split-view diff uses horizontal percentage split `[Constraint::Percentage(55), Constraint::Percentage(45)]`.
- **User Message Cards (`crates/hex-tui/src/ui/dialogue.rs#L72-L105`):** User messages are rendered inside dedicated rounded bubble cards (`╭─ ❯ You ───────────── [timestamp] ─╮`, `│ message │`, `╰──────────────────────────────────╯`), creating a clear visual distinction from the assistant's flow.
- **Tool Invocations (`crates/hex-tui/src/ui/dialogue.rs#L254-L335`):** Rendered as branch items (`├─ ⚙ run_tests (suite=all) · 45ms [OK]`).
- **CRITICAL ARCHITECTURAL DEFICIT — The Disconnected Cockpit Header:**
  - `crates/hex-tui/src/ui/header.rs` defines an elaborate 2-line telemetry header displaying HEX logo, git branch, project ID, session ID, active model badge, CoW provider, tools count, skills count, and connection status (`header.rs#L11-L105`).
  - `crates/hex-tui/src/ui/status_bar.rs` defines a persistent telemetry status bar (`status_bar.rs#L12-L71`).
  - **However, in `crates/hex-tui/src/ui/mod.rs#L20-L63`, neither `header::render()` nor `status_bar::render()` is called!**
  - Line 26-30 splits the screen into *only two* chunks: transcript and input. `header::render` and `status_bar::render` are completely dead code in `crates/hex-tui`. As a result, the primary cockpit telemetry bar is never rendered to the screen.

---

### 1.3 Prompt Styling & Composer

#### Hermes Agent Reference
- **Multiline Composer (`textInput.tsx#L1-L600`):**
  - Fully dynamic multiline editor with automatic vertical expansion.
  - Synchronized cursor positioning tested via `textInputCursorSourceOfTruth.test.ts`.
  - Ghost auto-suggestions rendered in muted gray with inline completion.
  - In-memory clipboard paste expansion (`pasteExpand.ts`) with preview cards for large pastes (> 5 lines).
  - Masked password/token input mode (`maskedPrompt.tsx`).

#### HEX Target Implementation
- **Fixed Composer (`crates/hex-tui/src/ui/input.rs#L13-L104`):**
  - Fixed 3-line bottom box (`Constraint::Length(3)`) with rounded borders.
  - Border color reflects focus (`skin.border` when focused, `skin.muted` when unfocused).
  - Title footer bar (`input.rs#L42-L55`) embeds mode badge (`agent`, `plan`, `code`), workspace name, git branch, active model name, and hotkey hints (`enter send · tab mode · /help`).
  - Input field uses `❯ ` in skin accent color with cycling placeholder prompts (`PROMPTS`) when empty (`input.rs#L74-L84`).
  - Cursor blinking controlled by animation clock `app.animation_frame % 20 < 12` (`input.rs#L97-L100`).
  - Horizontal line scrolling (`input_window` in `input.rs#L106-L136`) with `…` ellipsis when input string exceeds terminal width.
- **Python CLI Composer (`harness_core/tui.py#L1450-L1530`):**
  - `_read_docked_input` with raw terminal input handling, custom redraw loop (`_redraw`), in-line cursor positioning, and slash command autocompletion.
- **Fit & Finish Gap:**
  - `hex-tui` does not support multiline input. Pressing Enter immediately submits; Shift+Enter or Ctrl+J does not insert a newline.
  - Paste handling does not detect large blocks: pasting a 100-line script feeds 100 Enters into the raw event loop.

---

## 2. Animations & Responsiveness

### 2.1 Spinners & Activity Indicators

#### Hermes Agent Reference
- **Braille Animations Library:** Imports `unicode-animations` (`thinking.tsx#L4`, `L42-L44`). Features 7 distinct braille animations for reasoning (`helix`, `breathe`, `orbit`, `dna`, `waverows`, `snake`, `pulse`) and 7 for tool execution (`cascade`, `scan`, `diagswipe`, `fillsweep`, `rain`, `columns`, `sparkle`).
- **Shimmer Loader Primitive (`loaders.tsx#L1-L50`):** Animated ASCII loading primitive (`Shimmer`, `ShimmerRows`) for skeleton states. Sweeps a highlight band across block characters (`▁`) with pure geometry math (`shimmerSegments`).
- **Shared Animation Clock (`loaders.tsx#L52-L98`):** A single centralized interval (`TICK_MS = 90`) drives all mounted shimmer components via `subscribeShimmerClock`.
- **CPU Throttling & Animation Budget (`loaders.tsx#L66`, `L101-L125`):** Bounded by `SHIMMER_ANIMATE_MS = 30_000`. If a session remains loading/lazy for over 30 seconds, the animation automatically freezes in place, ensuring an idle TUI never consumes background CPU.

#### HEX Target Implementation
- **Rust Cockpit Spinner (`crates/hex-tui/src/ui/design.rs#L89-L97`):**
  - 10-frame braille array: `["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]`.
  - 6 rotating status verbs: `["Thinking", "Analyzing", "Exploring", "Formulating", "Reflecting", "Synthesizing"]`.
  - Driven by `AppEvent::Tick` in `main.rs#L303-L305` calling `app.advance_animation()`.
- **Python CLI Morphing Spinner (`harness_core/tui.py#L671-L753`):**
  - Highly sophisticated `ActivitySpinner` in Python with matrix-like braille shimmer wave transitions (`BRAILLE_SHIMMER`).
  - Seamlessly morphs between phrases (e.g. `Thinking...` -> `Analyzing...`) over a 3.5s display + 0.35s transition duration.
- **Fit & Finish Gap:**
  - The superior braille shimmer morphing wave from `tui.py` was **not ported** to `crates/hex-tui`. The Rust cockpit uses a basic modulo frame counter (`(i / 7) % VERBS.len()`) without smooth text morphing.
  - No CPU budget: `crates/hex-tui` ticks every 50ms indefinitely as long as the process runs, even when idle (though Ratatui's buffer diffing avoids unnecessary stdout writes when the buffer is identical).

---

### 2.2 Frame Rates, Differential Rendering & Flicker Prevention

#### Hermes Agent Reference
- **Differential Patch Engine (`packages/hermes-ink/src/ink/frame.ts`, `render-to-screen.ts`):**
  - Custom React-to-terminal reconciler with dual screen buffers (`Screen`, `CharPool`, `StylePool`).
  - Single-pass diff optimizer (`optimizer.ts#L18-L70`): Merges adjacent `cursorMove`, cancels no-op `cursorMove(0,0)`, purges empty stdout, concatenates style transitions, and dedupes hyperlink patches.
  - DECSTBM hardware scrolling hints (`scrollHint`) for native terminal GPU scrolling.
  - Backpressure awareness (`prevFrameDrainMs`, `backpressure: boolean` in `frame.ts#L48-L55`).
  - Integrated 30-frame windowed FPS monitoring atom (`fpsStore.ts#L8-L51`).

#### HEX Target Implementation
- **Ratatui Double-Buffering:** Ratatui uses a double-buffered differential rendering engine (`Terminal::draw`). During each render pass, Ratatui compares cells in the current `Buffer` against the previous `Buffer` and emits ANSI sequences only for modified cells. This provides clean, zero-flicker differential rendering.
- **Tick Rate:** `crates/hex-tui/src/main.rs#L236` configures `EventHandler::new(50)` -> 20 FPS (50ms interval).
- **Fit & Finish Gap:**
  - 20 FPS (50ms) is lower than Hermes's 60 FPS target. While 20 FPS is adequate for text, fast cursor movement and scrolling can feel slightly stepped.
  - Lacks frame latency profiling, drop-frame tracking, and FPS diagnostic overlay (`$fpsState`).

---

### 2.3 Streaming Token Frame Pacing & Markdown Re-Parsing

#### Hermes Agent Reference
- **Incremental Streaming Markdown (`streamingMarkdown.tsx#L1-L120`):**
  - In-flight LLM assistant tokens arrive in deltas. Re-parsing the entire markdown AST on every delta is an $O(N^2)$ operation.
  - Hermes implements an incremental forward scanner `StreamScanState`:
    - Code fences (` ``` `) and math blocks (`$$`) are tracked in a scan state.
    - Completed top-level blocks separated by blank lines (`\n\n`) are frozen into an append-only list of memoized `<Md>` components.
    - Settled blocks tokenize exactly once.
    - Only the trailing incomplete line (tail) is parsed on new token arrival ($O(\text{tail})$ cost).
- **Visual Stability (`streamingAssistant.tsx#L44-L65`):** Tracks predecessor message anchors (`prevMsg`) to prevent layout jumping when streaming deltas commit into history.

#### HEX Target Implementation
- **Delta Handling (`crates/hex-tui/src/app.rs#L369-L379`):**
  ```rust
  pub fn append_assistant_delta(&mut self, delta: &str) {
      if let Some(DialogueItem::Message(m)) = self.dialogue_items.last_mut() {
          if m.role == Role::Assistant {
              m.content.push_str(delta);
              return;
          }
      }
      self.add_assistant_message(delta);
  }
  ```
- **Markdown Parsing in Viewport (`crates/hex-tui/src/ui/dialogue.rs#L127-L233`):**
  - On every single render pass (every 50ms tick and every incoming delta), `dialogue.rs` iterates through `for raw_line in msg.content.lines()`.
  - It runs string slicing, inline span tokenization (`parse_inline_spans`), code block boundary detection, and list parsing from line 0 to line $N$ every frame.
- **Performance Deficit:**
  - As dialogue history grows (e.g. 1,000+ lines across multiple turns), re-parsing the entire dialogue string array from scratch every 50ms causes noticeable CPU spikes and input lag on lower-spec machines.
  - Lack of block memoization or incremental line tokenization causes unnecessary allocations in the render path.

---

## 3. Keyboard Navigation & Scrolling

### 3.1 Viewport Scrolling & The "Focus Lock" Defect

#### Hermes Agent Reference
- **Precision Wheel (`precisionWheel.ts`, `wheelAccel.ts`):**
  - Sub-row wheel accumulation: Floating point wheel deltas accumulate until reaching an integer cell threshold, allowing silky-smooth trackpad gestures.
  - Adaptive non-linear acceleration curve: Fast gestures accelerate scrolling quadratically, while slow gestures step line-by-line.
- **Scroll Pager (`transcriptPager.ts`, `viewportStore.ts`):**
  - True virtual height awareness (`virtualHeights.ts`): Computes line layout based on actual Yoga measured element heights.
  - Scroll pinning: When the operator is at the bottom, the viewport remains pinned to new streaming tokens. If the operator scrolls up even 1 row, auto-scroll unlocks immediately. Scrolling back to bottom automatically re-engages follow mode.

#### HEX Target Implementation
- **Ratatui Paragraph Scroll (`crates/hex-tui/src/ui/dialogue.rs#L356-L366`):**
  - Ratatui's `Paragraph` widget supports vertical scrolling via `.scroll((scroll as u16, 0))`.
  - `App` struct tracks `dialogue_scroll: usize` and `follow_dialogue: bool`.

- **SHOWSTOPPER DEFECT 1 — Focus Lock Paralyzes Scrolling (`crates/hex-tui/src/app.rs#L689-L712`):**
  - In `app.rs`, `scroll_up()` and `scroll_down()` are implemented as follows:
    ```rust
    pub fn scroll_up(&mut self) {
        match self.focus {
            Focus::Dialogue => {
                self.dialogue_scroll = self.dialogue_scroll.saturating_sub(1);
                self.follow_dialogue = false;
            }
            Focus::Diff => {
                self.diff_scroll = self.diff_scroll.saturating_sub(1);
            }
            _ => {}
        }
    }
    ```
  - When the application starts, `app.focus` is initialized to `Focus::Input`.
  - In `crates/hex-tui/src/main.rs`:
    - Mouse scroll events (`main.rs#L260-L270`) call `app.scroll_up()` / `app.scroll_down()`.
    - PageUp / PageDown keys (`main.rs#L421-L433`) call `app.scroll_up()` / `app.scroll_down()`.
    - Up / Down arrow keys with empty input buffer (`main.rs#L474-L487`) call `app.scroll_up()` / `app.scroll_down()`.
  - **In ALL these cases, `self.focus` is `Focus::Input`!**
  - Therefore, `scroll_up()` hits the wildcard match `_ => {}` and **does absolutely nothing!**
  - What's worse: `switch_focus(&mut self)` exists in `app.rs#L524`, but **it is never called anywhere in `main.rs`!** There is no keybinding to switch focus to `Focus::Dialogue`.
  - **Verdict:** In the interactive binary `hex-tui`, mouse wheel scrolling, PageUp/PageDown, and Up/Down navigation are 100% dead. The user can never scroll the transcript up.

- **SHOWSTOPPER DEFECT 2 — Wrapped Line Height Undercount (`crates/hex-tui/src/ui/dialogue.rs#L356`):**
  - In `dialogue.rs#L356`:
    ```rust
    let max_scroll = lines.len().saturating_sub(area.height as usize);
    let scroll = if app.follow_dialogue {
        max_scroll
    } else {
        app.dialogue_scroll.min(max_scroll)
    };
    ```
  - `lines` is `Vec<Line>`, representing the un-wrapped logical lines.
  - But `Paragraph` wraps text with `.wrap(Wrap { trim: false })`.
  - If a message contains 10 long lines that each wrap into 4 visual rows (40 rows rendered), `lines.len()` is 10, while visual height is 40!
  - `max_scroll` calculates `10 - area.height` instead of `40 - area.height`.
  - **Verdict:** When assistant responses wrap across columns, `follow_dialogue` scrolls to the wrong position, cutting off the bottom lines of the response below the visible viewport!

---

### 3.2 Python CLI Viewport Windowing (`harness_core/ui_selector.py`)

- In `harness_core/ui_selector.py#L60-L76` and `L138-L186`, HEX has an exemplary terminal windowing implementation for terminal selection prompts:
  - `_adjust_window`: Clamps the visible window around the active cursor.
  - Overflow indicators: Renders `▲ X more above (↑/k/PgUp)` and `▼ Y more below (↓/j/PgDn)` when options exceed terminal budget.
  - Keyboard mappings (`_UP_KEYS`, `_DOWN_KEYS`, `_PGUP_KEYS`, `_PGDN_KEYS`, `_HOME_KEYS`, `_END_KEYS`).
  - In-place ANSI redraw via `\033[{lines}A` and `\r\033[K`.
- **Finding:** The windowing logic in `ui_selector.py` is clean and robust, but it operates as a separate console modal rather than an integrated component of the Ratatui cockpit.

---

## 4. Interactive Widgets

### 4.1 Model Selector

#### Hermes Agent Reference
- **Interactive Multi-Stage Modal (`modelPicker.tsx#L1-L160`):**
  - 5 distinct operational stages: `provider` -> `key` -> `model` -> `reasoning` -> `disconnect`.
  - Fuzzy model filtering via `fuzzyRank` and `modelSearchText`.
  - Model capabilities inspection (`pickerOffersReasoning`).
  - Reasoning effort ladder selection: 6 levels (`low`, `medium`, `high`, `max`, `none`, `keepCurrent`).
  - Scope selection: `--global` (persisted to config) vs `--session` (in-memory override).
  - Telemetry badges: Plan tier limits (`planLimitLine`), top-tier badges (`hasTopTier`).

#### HEX Target Implementation
- **Rust Cockpit (`crates/hex-tui`):**
  - **No interactive model picker widget exists.**
  - Typing `/model` displays current model as text.
  - Typing `/model <name>` switches the active string.
  - No dropdown, no fuzzy selection popup, no reasoning effort selector.
- **Python CLI (`harness_core/tui.py#L2013-L2088`):**
  - If `/model` is typed without arguments in interactive mode, `_cmd_model` dynamically fetches live models via `fetch_available_models(active_provider)`.
  - Presents them using `select_option` from `ui_selector.py` with arrow-key selection.
  - Automatically updates orchestrator router, provider model ID, and saves persistent project overrides.
- **Fit & Finish Gap:**
  - The interactive model picker is available in the Python CLI fallback, but is completely absent from the native Rust cockpit.

---

### 4.2 Slash Command Autocompletion

#### Hermes Agent Reference
- **Autocomplete Engine (`prompts.tsx`, `slashCmd.ts`):**
  - Comprehensive catalog divided into functional domains (`core`, `ops`, `session`).
  - Contextual parameter suggestions and syntax documentation.
  - Highlighted fuzzy matches (`searchHighlight.ts`).
  - Full localization support across 20+ languages (`src/i18n/`).

#### HEX Target Implementation
- **Rust Cockpit Autocomplete (`crates/hex-tui/src/ui/input.rs#L138-L180`):**
  - Rendered as a floating popup positioned immediately above the docked composer (`Rect::new(composer.x + 2, composer.y - height, width, height)`).
  - Clear backdrop (`Clear`) with rounded border block titled `" Commands "`.
  - Selection highlighted with `skin.accent` background and inverted foreground text (`input.rs#L154-L167`).
  - 14 registered commands (`AVAILABLE_SLASH_COMMANDS` in `app.rs#L61-L121`): `/help`, `/status`, `/diff`, `/sbs`, `/blast`, `/proof`, `/rollback`, `/fork`, `/skills`, `/scan`, `/model`, `/budget`, `/agent`, `/plan`.
  - Keyboard navigation: Up/Down arrow keys navigate list (`main.rs#L440-L450`); Tab or Enter accepts the suggestion (`main.rs#L411-L418`, `L454-L461`).
- **Fit & Finish Gap:**
  - Uses simple prefix filtering (`cmd.name.starts_with(&text)`). Typing `/bl` suggests `/blast`, but typing `/ast` does not match.
  - Autocomplete does not support argument completion (e.g. `/model <tab>` does not suggest model names; `/mode <tab>` does not suggest `plan|agent|code`).

---

### 4.3 Diff Viewer

#### Hermes Agent Reference
- **Markdown & Code Diffs (`markdown.tsx#L797-L835`):**
  - Full line-level background styling: `diffAdded` green background with `diffAddedWord` high-contrast text; `diffRemoved` red background with `diffRemovedWord` text.
  - Hunk headers (`@@ -1,5 +1,6 @@`) highlighted in `t.color.muted`.
- **Agents Overlay Diff (`agentsOverlay.tsx#L589-L645`):** Tree-level metric diffs comparing descendant counts, tool executions, max tree depth, execution duration, and token usage between subagent runs.

#### HEX Target Implementation
- **Split Diff Cockpit (`crates/hex-tui/src/ui/diff.rs#L1-L163`):**
  - Toggled via `F2` key or `/diff` slash command.
  - Split-screen layout (55% dialogue, 45% unified diff).
  - Title shows focus status: `Diff Viewer [FOCUSED - ↑/↓ to Scroll]` vs `Diff Viewer`.
  - Formats filenames (`--- a/...` in LightRed, `+++ b/...` in LightGreen).
  - Formats hunks (`@@ -old,count +new,count @@` in Cyan bold).
  - Colorized line types (`DiffLineType::Added` in LightGreen, `DiffLineType::Deleted` in LightRed, `DiffLineType::Context` in DarkGray).
- **Python CLI Side-by-Side Diff (`harness_core/tui.py#L392-L450`):**
  - Includes `render_side_by_side_diff` producing genuine 2-column side-by-side terminal diffs with line numbers, addition markers (`+`), and deletion markers (`-`).
- **Fit & Finish Gap:**
  - `crates/hex-tui` only renders unified diffs, not side-by-side diffs (even though `/sbs` is in the slash command list!).
  - Diff view lacks syntax highlighting inside changed lines (e.g. diff keywords like `fn`, `import` are plain colored text without language syntax tokens).

---

### 4.4 Multi-Turn Chat Layout & Operator Interventions

#### Hermes Agent Reference
- Full cockpit dashboard with widget grid (`widgetGrid.tsx`), active session switcher (`activeSessionSwitcher.tsx`), todo panel (`todoPanel.tsx`), and rewind browse (`rewindBrowse.tsx`).

#### HEX Target Implementation
- **Operator Approval Modal (`crates/hex-tui/src/ui/approval.rs#L1-L140`):**
  - Triggered by ACP notification `session/approvalRequest` (`main.rs#L731-L750`).
  - High-visibility modal centered over entire terminal with double borders (`BorderType::Double`).
  - Dynamic risk styling: Red border for R4/Destructive (`approval.rs#L20-L22`), Yellow for R3/Privileged.
  - 5-part vertical layout:
    1. Risk level badge (`[R4 - DESTRUCTIVE]`)
    2. Privileged tool name and approval ID
    3. Policy warning / reason
    4. Scrollable payload & arguments box (`Paragraph` with border)
    5. Action button bar: `[y] Approve` (green button), `[n] Reject` (red button), `[d] View Diff` (cyan button), `[Esc] Dismiss`.
  - Keyboard events (`main.rs#L353-L399`) handle `y`/`n`/`d`/`Esc` and asynchronously dispatch JSON-RPC approval decisions back to the ACP server.
- **Help Modal Overlay (`crates/hex-tui/src/ui/mod.rs#L65-L135`):**
  - Centered popup (`centered_rect(70, 70, area)`).
  - Documents navigation, hotkeys, and slash commands.
  - Dismissible via `Esc`, `Enter`, or `q`.
- **Intro Banner & Identity (`crates/hex-tui/src/ui/dialogue.rs#L370-L449`):**
  - Renders 6-line 3D ASCII banner (`BANNER`) in skin gradient when terminal width $\ge 94$.
  - Switches gracefully to compact centered text banner on narrow terminals ($< 94$).
  - Displays rotating engineering tips (`TIPS`), provider name, active model, workspace path, tools count, and skills count.

---

## 5. Detailed Gap Analysis & Remediation Action Plan

### Tier 1: Showstopper Defects (Must Fix Immediately)

1. **Fix Transcript Scrolling Focus Lock:**
   - **File:** `crates/hex-tui/src/app.rs#L689-L712` and `crates/hex-tui/src/main.rs#L421-L433`
   - **Issue:** Mouse wheel, PageUp/PageDown, and Up/Down when input is empty fail because `scroll_up()` requires `self.focus == Focus::Dialogue`, but focus is permanently locked to `Focus::Input`.
   - **Fix:** Allow `scroll_up()` and `scroll_down()` to default to dialogue scroll when input is empty or when mouse wheel events fire. Additionally, bind a hotkey (e.g. `Ctrl+P` / `Ctrl+N` or `Up` arrow on empty input) to enter `Focus::Dialogue`.

2. **Fix Wrapped Line Height Calculation in Dialogue Viewport:**
   - **File:** `crates/hex-tui/src/ui/dialogue.rs#L356`
   - **Issue:** `max_scroll` uses `lines.len()`, which ignores visual wrapping produced by `Wrap { trim: false }`. This causes auto-scroll (`follow_dialogue`) to leave the bottom of assistant responses truncated offscreen.
   - **Fix:** Calculate visual line count by measuring wrapped text against `area.width` (or use a layout helper) before computing `max_scroll`.

3. **Re-Wire the Missing Cockpit Header:**
   - **File:** `crates/hex-tui/src/ui/mod.rs#L24-L35`
   - **Issue:** `header::render()` exists in `header.rs` but is never called in `ui::draw()`, leaving the top telemetry bar completely invisible.
   - **Fix:** Update `root_chunks` in `mod.rs` to include a 4-line header constraint `Constraint::Length(4)` and render `header::render(frame, app, root_chunks[0])`.

---

### Tier 2: Visual Polish & Parity Gaps

4. **Light Terminal Theme & Contrast Normalization:**
   - **File:** `crates/hex-tui/src/ui/design.rs`
   - **Fix:** Add a `LIGHT` skin preset and implement OSC-11 background query on terminal initialization to detect light terminal backgrounds. Fall back to dark text on light surfaces.

5. **Incremental Markdown Tokenizer for Streaming:**
   - **File:** `crates/hex-tui/src/ui/dialogue.rs`
   - **Fix:** Introduce a block-level memoization cache in `App`. When assistant deltas stream in, freeze settled blocks separated by `\n\n` into pre-rendered Ratatui `Line` vectors, tokenizing only the active streaming tail line.

6. **Interactive Model Picker Widget:**
   - **File:** `crates/hex-tui/src/ui/`
   - **Fix:** Implement a modal popup widget for `/model` in Ratatui, featuring fuzzy search over available models, reasoning effort selection (`low|medium|high`), and provider badges.

---

### Tier 3: Ergonomic & Delight Enhancements

7. **Port Python Shimmer Wave Morphing to Rust Cockpit:**
   - **File:** `crates/hex-tui/src/ui/status_bar.rs` and `design.rs`
   - **Fix:** Port the 3.5s + 0.35s braille wave morphing transition from `harness_core/tui.py#L671-L740` into `hex-tui` for smooth status transitions between "Thinking...", "Analyzing...", and "Synthesizing...".

8. **Side-by-Side Diff Implementation (`/sbs`):**
   - **File:** `crates/hex-tui/src/ui/diff.rs`
   - **Fix:** Port the two-column diff layout from `harness_core/tui.py#L392-L450` into `crates/hex-tui` to provide side-by-side file comparison in split view.

9. **Fuzzy Search for Slash Commands:**
   - **File:** `crates/hex-tui/src/app.rs#L641-L655`
   - **Fix:** Replace `starts_with` with subsequence or Levenshtein fuzzy matching so typing `/ast` matches `/blast` and `/rb` matches `/rollback`.

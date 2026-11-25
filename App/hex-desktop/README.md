# `hex-desktop`

**Native GPU-accelerated desktop cockpit for HEX Agent — built in Rust with Zed's GPUI (`0.2.2`), `hex-protocol`, embedded brand typography, and a spring-physics motion engine.**

`hex-desktop` (`version = "0.1.0"`, `edition = "2021"`) provides an experimental native desktop workbench (no Electron/Chromium runtime) communicating with the autonomous agent engine over the Agent Client Protocol (ACP) JSON-RPC 2.0 wire contract.

> **Project Status**: Distributed from source only (`cargo run -p hex-desktop`; there is no `hex desktop` CLI subcommand or prebuilt release binary yet). Core chat, streaming, markdown/code blocks, `<think>` reasoning folds, inline tool diffs, tool cards, file attachments, context gauge, session management (create/resume/rename/pin/archive/delete/search/title hydration), settings (`config/get` + `config/set`), model picker, tool approvals, and cloud account/update flows are live. Secondary workspace panes (`Files`, `Browser`, `Terminal`, `Extensions`, `Notebooks`, `Automations`, `Team`) are intentionally hidden until backed by real runtime handlers.

---

## Overview & Design Basis

- **Rendering Engine**: Built on Zed's GPU-accelerated **GPUI** (`0.2.2`), rendering an opaque `#141414` canvas (`WindowBackgroundAppearance::Opaque`) with a transparent titlebar and custom macOS traffic-light placement (`(14.0, 16.0)`), default `1440×900` window bounds (min `900×600`).
- **Design Language & Mockup Parity**: Implements the color, spacing, layout, icon, and typographic specifications in [`docs/design/mockups-desktop/`](../../docs/design/mockups-desktop) and [`docs/development/desktop-design-parity-plan.md`](../../docs/development/desktop-design-parity-plan.md):
  - Embedded **Apfel Grotezk** (`400`, `700`) and **Geist Mono** (`400`, `500`) static TTF fonts (`src/fonts.rs`) — pre-converted from WOFF2 so GPUI DirectWrite on Windows also accepts them.
  - 30 stroke SVG icons (`assets/icons/`, `16×16`, `stroke="currentColor"`, `1.3px` stroke width).
  - Exact palette tokens (`src/theme.rs`): canvas `#141414`, body `#0a0a0a`, sidebar `#101010`, surface `#171717`, hover `#1e1e1e`, row hover `#1a1a1a`, pill `#1f1f1f` (`#272727` hover), electric blue accent `#7aa7ff`, sage diff additions `#8fbf93` / `#c4e0c7`, rose diff deletions `#c98b8b` / `#d4a9a9`, amber warnings `#f59e0b`.
- **Two-Tier Physics Motion Engine (`src/ui/motion/`)**:
  - **Tier 1 (Per-Window Spring Simulator)**: `MotionSystem` global driving semi-implicit Euler springs (`SMOOTH` `0.45s / ζ=1.0`, `SNAPPY` `0.18s / ζ=1.0`, `BOUNCY` `0.45s / bounce=0.30`) with mid-flight velocity handoff and targeted `window.on_next_frame` scheduling (zero idle frame loop when springs settle).
  - **Tier 2 (Ambient Deterministic Curves)**: `breathe_opacity` (5s), `pulse_opacity` (2s), `spinner_frame` (0.9s, 10-frame braille), `blink_visible` (1.1s), and `rise_progress` (`0.9s` cubic-bezier).
  - **Reduced Motion**: Queries OS accessibility settings (`NSWorkspace.accessibilityDisplayShouldReduceMotion` on macOS, `SPI_GETCLIENTAREAANIMATION` via `user32` on Windows), snapping spatial motion (`MotionKind::Spatial`) and flattening/shortening opacity fades (`REDUCED_OPACITY_FADE = 180ms`).
- **Platform Notes**: macOS is the primary target (`objc2-app-kit` dependency, macOS menu bar, `cmd-*` keybindings, `80px` traffic-light gutter); Windows-ready font SFNT conversion, `user32` reduced-motion probing, `ctrl K` sidebar hint, and Python venv discovery hooks are already in place.

---

## Crate Dependencies (`Cargo.toml`)

| Crate | Purpose |
| :--- | :--- |
| `gpui` `0.2.2` (+ `test-support` dev) | GPU-accelerated UI tree, input handling, text shaping, layout, and headless GPUI tests |
| `hex-protocol` | Async JSON-RPC 2.0 ACP client, wire models, and unified diff parser |
| `tokio` (`full`) | Background multi-thread runtime for ACP subprocess I/O, reconnects, and git probes |
| `rfd` `0.15` | Native OS file picker dialog for composer attachments |
| `serde` + `serde_json` | Protocol payload and settings serialization |
| `chrono` (`clock`, `std`) | Relative session timestamps and transcript export stamps |
| `unicode-segmentation` `1.10` | Grapheme-accurate multiline editor cursor and selection |
| `anyhow` / `futures` / `log` | Error handling, async utilities, and stderr warning diagnostics |
| `objc2` `0.6`, `objc2-app-kit` `0.3.2`, `raw-window-handle` `0.6` (macOS) | System `accessibilityDisplayShouldReduceMotion()` query and native `NSWindow` drag handling (`platform.rs`) |

---

## Directory & Module Architecture

```text
App/hex-desktop/
├── Cargo.toml
├── assets/
│   ├── fonts/               # ApfelGrotezk-{Regular,Bold}.ttf, GeistMono-{Regular,Medium}.ttf
│   └── icons/               # 30 stroke SVG icons (16×16, currentColor)
├── src/
│   ├── main.rs              # Application entrypoint, FsAssets loader, StderrLogger, window boot
│   ├── lib.rs               # Public library exports for tests and embedding
│   ├── chat.rs              # Root ChatView (EntityInputHandler, focus, ACP event pump, rendering)
│   ├── acp_bridge.rs        # AcpBridge, AcpEvent, lossless AcpEventBus, backoff reconnect loop
│   ├── window.rs            # WindowConfig (1440×900 opaque canvas, centering, titlebar options)
│   ├── theme.rs             # HermesTheme palette, MockupTypeScale, contrast ratio helpers
│   ├── fonts.rs             # Compile-time font embedding and GPUI text-system registration
│   ├── shortcuts.rs         # Global GPUI actions and desktop_keybindings()
│   ├── menu.rs              # Native macOS application menu hierarchy
│   ├── platform.rs          # Native OS window drag shim (AppKit performWindowDragWithEvent)
│   └── ui/
│       ├── mod.rs           # UI exports and icon_svg() helper
│       ├── shell.rs         # AppShell cross-session state container & modal priority queue
│       ├── home.rs          # Start/Home screen blocks (ASCII_LOGO, ContinueRow cards)
│       ├── sidebar.rs       # 296px/260px collapsible project navigator, Needs-you badge, Account footer
│       ├── session_list.rs  # SessionItem model, sorting, filtering, date grouping helpers
│       ├── composer.rs      # Glow-card composer container, PromptComposer state, context_bar
│       ├── model_pill.rs    # ModelSelectorPill, provider_dot_color, circular Send/Stop button
│       ├── pill.rs          # Shared .pill and 32×32 .ib icon button primitives
│       ├── feed.rs          # Bottom-anchored ListState feed, 28px top fade, in-feed ApprovalCard
│       ├── message.rs       # UserMessageCard, AssistantMessageView (Copy/Retry/Tokens/Changes)
│       ├── markdown.rs      # Custom block & inline Markdown parser + safe URL filtering
│       ├── diff_view.rs     # Single-gutter inline diff cards (+N / −N U+2212, sage/rose tints)
│       ├── tool_card.rs     # Collapsible Parameters/Output tool cards with live status & embedded diffs
│       ├── reasoning.rs     # ThinkingDisclosure (<think> fold with live elapsed timer)
│       ├── token_gauge.rs   # Token engineering formatting, gauge thresholds, ASCII bar widget
│       ├── payload.rs       # Bounded 400-line / 24k-char scrollable payloads and Copy buttons
│       ├── motion/          # 2-tier spring + ambient motion engine (presets, spring, system, lerp, reduced, interaction, ambient)
│       └── modals/          # Modal container (scrim + queue) & overlays:
│           ├── mod.rs       # ActiveModal enum, modal_press, render_modal_container, About view
│           ├── palette.rs   # CommandPaletteModal (Cmd+K)
│           ├── approval.rs  # ToolApprovalModal (3-way radio + 15m lease toggle)
│           ├── model_picker.rs # ModelPickerModal (live catalog grouped by provider)
│           ├── settings.rs  # SettingsModal (10 whitelisted config keys via config/get + config/set)
│           ├── account.rs   # AccountModal (Login / Register / Enrolled Devices & Revocation)
│           ├── session_actions.rs # SessionActionModal (Rename/Pin/Archive/Delete) + SessionSearchModal
│           └── text_input.rs # Shared modal text input & button helpers
└── tests/                   # 7 integration & GPUI acceptance test suites
```

---

## Implemented Features & Surfaces

### Live Surfaces

1. **Home / Start Screen (`src/ui/home.rs`, `src/chat.rs`)**:
   - Top bar with sidebar toggle, live ACP transport chip (`"{provider} · {state}"`), Command Palette search trigger, Bell button (with pulsing dot when approvals are pending), and Settings trigger.
   - Centered `HEX` block wordmark (`"█ █ █▀▀ ▀▄▀\n█▀█ ██▄ ▄▀▄"`), glowing prompt card, and `"Continue"` list of the 5 most recent non-archived sessions (pinned first, pulsing dot while streaming, `"Review"` accent chip on pending approval else `"This Mac"`).
2. **Conversation Feed & Turn Fidelity (`src/ui/feed.rs`, `message.rs`, `markdown.rs`, `reasoning.rs`, `diff_view.rs`, `tool_card.rs`)**:
   - Bottom-anchored 560px feed column with 28px top canvas fade and spring-animated session crossfades.
   - **Markdown & Safe Links**: Headings, lists, nested blockquotes, 2D-scrollable tables, horizontal rules, inline code chips (`.cd`), fenced code blocks (`400`-line / `24,000`-char bounded preview + Copy), and `http://`/`https://`-validated clickable links.
   - **Live `<think>` Reasoning Disclosure (`ThinkingDisclosure`)**: Driven by `AcpEvent::ThoughtChunk` (`agent_thought`), displaying `"Thinking..."` while active and `"Thought briefly"` / `"Thought for Ns"` once complete, with rotatable caret and copyable mono body.
   - **Tool Call Cards (`ToolCallCard`) & Inline Diffs (`DiffView`)**: Shows tool-specific icons, millisecond latency, live status (`Running` braille spinner, `Success` sage check, `Failed` rose cross), collapsible `Parameters` and `Output` folds, embedded single-gutter diffs on file-edit tools, and a turn-level `"Changes"` summary fold (`"N files, +A −D"`).
   - **In-Feed `ApprovalCard`**: Mirrors pending/approved/denied operator decisions in the feed stream (`Needs you` → `Approved` / `Denied`), flipping state only upon runtime confirmation.
   - **Message Actions & Export**: One-click Copy, Retry (regenerates from the preceding user prompt), actual token usage badge, and Markdown transcript export (`<workspace>/.hex/exports/<session>-<stamp>.md`).
3. **Multiline Composer, Attachments & Context Gauge (`src/ui/composer.rs`, `model_pill.rs`, `token_gauge.rs`, `chat.rs`)**:
   - Custom `EntityInputHandler` editor (`1–8` lines, IME marked text, mouse drag selection, 100-step grouped undo/redo, clipboard ops) inside the `#7aa7ff`-haloed `glow_card`.
   - Native file attachment picker (`rfd::AsyncFileDialog`) with removable attachment chips preserved per session and sent via `session/prompt` (`attachments`).
   - Circular Send/Stop button (`arrow-up` idle, `■` rose stop button to cancel while streaming) and `ModelSelectorPill`.
   - Context bar (`"Context N% full"`) fed live by `AcpEvent::TurnUsage` (hidden until real token usage arrives).
4. **Collapsible Project Sidebar & Session Management (`src/ui/sidebar.rs`, `session_list.rs`, `modals/session_actions.rs`)**:
   - `296px` sidebar (`260px` below `1100px` viewport width) with `"New chat"`, `"Search chats"` (`SessionSearchModal`), collapsible workspace project group, pinned-first active sessions with hover `more` menu (`SessionActionModal`: Rename, Pin/Unpin, Archive/Unarchive, Delete confirmation), `"ARCHIVED"` section, live `"Needs you"` approval count row, and Account footer.
   - Background title hydration (concurrency `4`) reads turn-0 prompts for untitled historical sessions and persists inferred titles via `session/rename`.
   - Per-session drafts, feeds, attachments, and selected model IDs are preserved across session switches.
5. **Modal Suite & Priority Queue (`src/ui/modals/*`, `src/ui/shell.rs`)**:
   - `AppShell::modal_queue` queues secondary modals behind unresolved approvals and deduplicates repeated approval deliveries.
   - **Command Palette (`⌘K`)**: Fuzzy subsequence search over `New Chat`, `Clear History`, `Export Transcript`, `Switch Model`, `Open Settings`, `Toggle Sidebar`, and all sessions.
   - **Tool Approval Modal**: Shows tool, risk badge (`R3`/`R4`), scrollable command/parameters, 3-way choice (`[y]`/`[1]` Once, `[a]`/`[2]` Session, `[n]`/`[3]` Deny), and a 15-minute (`900s`) lease toggle (`[l]`, attached as `leaseTtlSeconds: Some(900)` when `Session` is selected), resolving via `session/approvalDecision`.
   - **Model Picker**: Live catalog fetched via `session/models`, grouped by provider with `Local`/`Cloud` badges and inline retry on failure.
   - **Settings Modal (`⌘,`)**: Backed by ACP `config/get` + `config/set` for 10 whitelisted settings across `Model`, `Runtime`, `Web search`, `Cloud`, and `Learning`, computing dirty diffs on save.
   - **Account & Cloud Modal**: Sign in, register, view enrolled devices (`host-eligible`/`member`, `trusted`/`revoked`), revoke devices, sign out, and receive live `cloud/event` notices.
   - **Update Banner & Notification Bell**: Checks `cloud/update/check` when signed in and displays optional (sage) or mandatory (rose) update banners plus header bell badge counts.

### Intentionally Omitted / Partial Surfaces

- **Dock & Sidebar Feature Destinations**: `Files`, `Browser`, `Terminal`, `Extensions`, `Notebooks`, `Automations`, `Team`, and `Design` panels (along with composer Auto/Mic controls) are **not rendered** in the current UI; only `Chat` (`segment_chat`), `Needs you`, `Account`, and `Settings` are active (though their icons remain in `assets/icons/`).
- **`cloud/config/evaluate`**: `AcpBridge::cloud_config_evaluate` is implemented on the bridge, not yet invoked by `ChatView`.
- **Palette `Custom` Actions**: Any `PaletteAction::Custom(label)` reports `"{label} is not available in the desktop yet."`.

---

## Backend Connection (`AcpBridge`)

At startup, `ChatView::start_backend` runs `AcpBridge::connect_auto()` (`src/acp_bridge.rs`):

- **Spawns Trusted Child Process**: `connect_auto()` executes `AcpClient::spawn_acp_server(workspace_root)` -> `<python> -m harness_core.client.cli acp [--workspace <path>]` over stdio (`connect_socket` for Unix domain sockets exists only as an explicit API, never auto-adopted, preventing local socket impersonation).
- **Python Resolution Order** (`crates/hex-protocol/src/client.rs`):
  1. `HEX_PYTHON_BIN`
  2. `VIRTUAL_ENV` (`Scripts/python.exe` or `bin/python`)
  3. `<repo_root>/.venv` (found via `HEX_REPO_ROOT` or ancestor search for `pyproject.toml` + `harness_core` / `.venv`)
  4. `python3`, then `python`
  Each candidate is validated with `import harness_core.client.cli, yaml`.
- **Resilience**: Consumes backpressured lossless ACP notifications (`16 MiB` byte budget), batches UI updates over 16ms ticks with delta coalescing, supervises disconnects with exponential backoff (`500ms` → `30s`), and recovers active sessions via paged `session/load`.

---

## Keyboard Shortcuts & Menus

### Global & Menu Shortcuts (`src/shortcuts.rs`, `src/menu.rs`)

| Shortcut | Action |
| :--- | :--- |
| `⌘K` | Toggle Command Palette |
| `⌘N` | Start new chat session |
| `⌘B` | Toggle sidebar |
| `⌘,` | Open Settings modal |
| `⌘Enter` | Submit prompt |
| `Esc` | Dismiss active modal or cancel running turn |
| `⌘Z` / `⇧⌘Z` | Undo / Redo (composer & modal inputs) |
| `⌘X` / `⌘C` / `⌘V` / `⌘A` | Cut / Copy (selection or last assistant reply) / Paste / Select All |
| `⌘W` / `⌘H` / `⌘Q` | Close Window / Hide / Quit |

*(macOS application menu bar exposes `HEX Desktop`, `File`, `Edit`, and `View`.)*

### Context-Specific Keys (`src/chat.rs`, `src/ui/modals/*`)

- **Composer**: `Enter` sends, `Shift+Enter` inserts newline, arrow keys / `Home` / `End` / `Shift+Left` / `Shift+Right` move/select.
- **Approval Modal**: `y`/`1` (Allow once), `a`/`2` (Allow for session), `n`/`3` (Deny), `l` (Toggle 15m lease), `Enter` (Confirm), `Esc` (Deny).
- **Settings Modal**: `↑`/`↓`/`Tab`/`⇧Tab` navigate rows, `←`/`→` cycle choices, `Enter` edit/commit/cycle/save, `Esc` cancel edit or close.
- **Palette / Model Picker / Session Search / Session Actions / Account**: `↑`/`↓` navigate, `Enter` select/submit, `Esc` dismiss.

---

## Building, Running & Testing

Run commands from the Cargo workspace root:

```bash
# Run the desktop app (macOS)
cargo run -p hex-desktop

# If Metal shader compilation fails with CommandLineTools, point DEVELOPER_DIR at Xcode:
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer cargo run -p hex-desktop

# Fast type-check and lint
cargo check -p hex-desktop
cargo clippy -p hex-desktop

# Run all unit + GPUI integration tests
cargo test -p hex-desktop

# Run unit tests only
cargo test -p hex-desktop --lib
```

### Test Suites (`App/hex-desktop/tests/`)

| Test File | Coverage |
| :--- | :--- |
| `test_chat_flow.rs` | End-to-end `ChatView` vertical slice over duplex ACP (streaming, tool cards, copy/retry, palette, session switching/drafts, cancel, failures, modals, links, undo/redo, viewport clamping) |
| `test_reconnect_catalog_resume.rs` | Startup model identity, supervised reconnect, Home-to-Session model forwarding, live catalog refresh/switch, durable paged resume, out-of-order guards, bounded title hydration |
| `test_desktop_integration.rs` | Mock ACP handshake/stdio, `AppShell` event handling, 15m approval lease payload, Command Palette, Model Picker, shortcuts & menus |
| `test_feed_and_composer.rs` | Token gauge formatting/thresholds, `ThinkingDisclosure`, `DiffView`, feed items, `ApprovalCard`, attachments, `PromptComposer` |
| `test_theme_and_window.rs` | Byte-exact palette tokens, `MockupTypeScale`, WCAG contrast ratios, `WindowConfig` bounds & `WindowOptions` |
| `test_fonts.rs` | Embedded font registration and SFNT header/`OS/2` weight validation for DirectWrite |
| `test_icons.rs` | Validates all 30 bundled SVG icons match the `16×16` `stroke="currentColor"` convention |

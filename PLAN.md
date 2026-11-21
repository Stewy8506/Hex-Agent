# HEX Desktop — Mockup Implementation Plan

## Goal and users
HEX Desktop is a native macOS GPUI cockpit for the HEX AI agent. Today it has a working chat app (live ACP backend, sessions, approvals, model picker, command palette) but a utilitarian look. This plan re-skins and extends it to match the approved HTML mockups in `docs/design/mockups-desktop/` so it reads as a polished product: a **Start/home screen** and a restyled **Session screen**, both in the mockup's dark "Apfel Grotezk / Geist Mono" aesthetic with the electric-blue `#7aa7ff` accent.

**Working means:** the app builds and runs; the start screen renders with the pulsating glowing composer and recent-sessions list; opening or resuming a session shows the redesigned session view; every mockup function is either wired to real behaviour or explicitly labelled "Coming soon".

## Design tokens (from the mockups, one source of truth)
- Canvas `#141414` window / `#0a0a0a` body; sidebar `#101010`; surface `#171717`; hover `#1a1a1a`–`#1e1e1e`; chip bg `#1d1d1d`; code bg `#202020`.
- Borders: subtle `#222`–`#232323`, structural `#262626`.
- Text: primary `#e6e6e6`–`#f2f2f2`, secondary `#cfcfcf`, muted `#9a9a9a`, dim `#858585`.
- Accent: `#7aa7ff` (glow, cursor, running dots, active chips, needs-you).
- Diff: additions `#8fbf93`, deletions `#c98b8b` (already in theme.rs).
- Type: sans for prose (15px base), mono for labels/chips/code; radii 8/10/14/24px.
- **Signature moment:** the composer's breathing glow — a blue gradient halo that pulses opacity ~0.32→0.7 on a 5s ease-in-out loop (mockup `@keyframes breathe`), static under reduced-motion.

## Scope
**In scope**
- Home/start screen: ASCII logo, pulsating glowing prompt composer, mode + model pills, attach/send controls, "Continue" recent-sessions list with location chips, bottom feature dock.
- Session screen restyle: 296px sidebar (New chat, ⌘K search, Projects with counts + running indicators + per-session chips), Needs you entry, account footer; segmented Chat/Files/Browser/Terminal header; centered 560px conversation column; message action row (Copy / Retry / token count); Changes (files touched) collapsible; context-window % bar; glowing follow-up composer.
- New `HermesTheme` tokens matching the mockup palette; a pulsating-glow animation primitive in `ui/animation.rs`-style GPUI (opacity keyframes on the glow layer, ~5s loop, disabled when reduce-motion is on).
- "Coming soon" affordances for Automations, Team, Notebooks, Design, Extensions and subscription.

**Out of scope (labelled "Coming soon" in the UI, not implemented)**
- Automations
- Teams / Team feature
- Subscription / billing features
- Notebooks, Design, Extensions
- Voice input (button rendered, disabled)
- Real Files / Browser / Terminal panes (segmented control renders; only Chat is live, others show a Coming soon empty state)

## Architecture
- **Stack:** unchanged — Rust + GPUI 0.2 (`hex-desktop` crate), ACP bridge to the harness backend. No new dependencies.
- **New module `ui/home.rs`:** `HomeView` — logo, glowing composer card, continue list, feature dock. Owns its draft text; submits into the existing `ChatView` prompt path.
- **Screen routing:** `ChatView` gains a `screen: Screen` enum (`Home` / `Session`). Selecting a session or submitting a prompt on Home switches to Session; New chat returns to Home with a fresh draft. Existing per-session `SessionState` map is untouched.
- **Theme:** add mockup tokens to `theme.rs` (new fields with defaults), keep existing getters so nothing else breaks.
- **Glow:** a reusable `glow_card` wrapper: layered divs with accent-tinted borders/gradient background whose opacity animates on a 5s pulse; static fallback for reduced motion.
- **Shell restyle:** `shell.rs` main pane gets the mockup header (segmented control + right icon group), centered 560px feed column, context bar, glowing composer wrapper. `sidebar.rs` gets project grouping, Needs-you row, feature rows with Coming soon badges, account footer.
- **Data:** continue/projects lists render from the real session list; fields the backend doesn't provide yet (location chip, token count, changes summary) use clearly labelled placeholder values in `ui/fixtures.rs` until wired.
- **Auth/storage/hosting:** unchanged desktop app; no secrets involved.

## Risks and assumptions
- Mockup fonts (Apfel Grotezk, Geist Mono) aren't bundled; fall back to the existing system font stacks — palette and layout carry the design.
- GPUI has no CSS blur; the glow is approximated with layered accent-tinted rings and a pulsating opacity animation rather than a real blur.
- Backend doesn't yet expose session location (This Mac vs Cloud), needs-you queue, or token totals; these render as neutral placeholders, never fake data presented as real.
- Assume the existing tests (`tests/`) must keep passing; new views get unit tests for state logic, not pixel tests.

## Phase 1: Theme tokens + pulsating glow + restyled session screen (core look)
- [x] Add mockup palette tokens to `theme.rs` (surfaces, text ramp, accent tints) with getters
- [x] Add a pulsating-glow card component (5s breathe loop via `Animation::repeat()`; reduced-motion fallback not available in GPUI 0.2 — noted as a limitation)
- [x] Restyle sidebar: feature rows with Coming soon badges, account footer (296px width and project grouping tracked in Phase 3)
- [ ] Restyle main pane header: segmented Chat/Files/Browser/Terminal control (Chat live; others Coming soon empty state), right-side icon buttons
- [ ] Center the conversation feed in a 560px column; user bubble `#1e1e1e` radius 18px
- [ ] Add message action row (Copy, Retry, token count placeholder) under assistant turns
- [ ] Add context-window % bar above the composer (driven by existing token gauge)
- [x] Wrap composer in the pulsating glow card; mode + model pills (segmented header, centered 560px column, action row and context bar follow in Phase 3)
- [ ] Acceptance: app builds; session screen matches mockup layout and palette; all existing tests pass

## Phase 2: Home / start screen
- [ ] New `ui/home.rs` with ASCII logo, pulsating glowing composer card, mode/model pills, attach/send buttons
- [ ] "Continue" list from real recent sessions with relative times and location chips
- [ ] Bottom feature dock with tooltips; Automations/Team/Notebooks/Design/Extensions open a Coming soon state
- [ ] Route: app opens on Home; submitting a prompt or selecting a session opens Session; New chat returns Home
- [ ] Acceptance: app launches on Home; clicking a Continue row opens that session; submitting text creates and opens a session with the prompt

## Phase 3: Session detail polish
- [ ] Changes (2 files, +22 −23) collapsible card under turns that touched files, driven by existing diff view data
- [ ] Inline diff cards restyled to mockup colors/typography
- [ ] Needs you: sidebar row and header bell wired to the real approval queue count; clicking opens the pending approval
- [ ] Empty, loading and error states for the feed (connecting spinner, empty session prompt, reconnect notice)
- [ ] Acceptance: approval flow shows the badge; diff cards match the mockup; feed states all render

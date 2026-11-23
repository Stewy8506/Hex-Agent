# HEX Desktop — Exact Design-Parity Plan

**Status:** the visual-parity reference for the desktop workstream; implementation is
tracked as DX1–DX7 in [`ROADMAP.md`](../../ROADMAP.md) (DX1–DX3 landed as of 2026-11-23).
Supersedes the root `PLAN.md` (removed 2026-11-23; still in git history).

**Goal:** make `App/hex-desktop` pixel-identical to the approved mockups in
`docs/design/mockups-desktop/` — colours, sizing, spacing, typography, icons, layout,
and every animation — while keeping all existing behaviour (ACP backend, sessions,
approvals, model picker, palette) working and all tests green.

**Driving convention (standing workflow):** one commit per sub-phase, each fix ships a
red-proof regression test (write test → watch it fail on stashed source → restore → green),
`cargo test -p hex-desktop` green at every commit, no push/merge without explicit request.
Do not touch `crates/hex-tui/src/history.rs` (user's in-progress work).

---

## 1. Source of truth

| Artifact | Path |
|---|---|
| Start screen | `docs/design/mockups-desktop/Hex agent · desktop start-html/DesktopStart.dc.html` |
| Session screen | `docs/design/mockups-desktop/Hex agent · desktop session-html/DesktopSession.dc.html` |
| Font assets (Apfel Grotezk 400/700 woff2) | `assets/` subfolder of each mockup directory |
| Live app | `App/hex-desktop/src/` (GPUI 0.2.2, Windows Direct3D 11 renderer) |

The HTML/CSS is authoritative (extracted verbatim below). Artboard: **1440×900**,
`$preview` props confirm both screens. Verification loop in §7.

---

## 2. Design specification (extracted verbatim from mockups)

### 2.1 Colour tokens

| Token | Value | Usage |
|---|---|---|
| body | `#0a0a0a` | page behind window (app: unused, window fills screen) |
| canvas | `#141414` | window background, both screens |
| window border | `#262626` 1px, radius 12 | artboard frame (OS chrome in app — see §5) |
| sidebar | `#101010` | session sidebar; diff card bg |
| surface | `#171717` | composer card bg |
| row hover | `#1a1a1a` | `.row:hover`, search box bg |
| row active | `#1e1e1e` | `.act`, user bubble bg, icon-button hover bg |
| chip bg | `#1d1d1d` | `.chip` |
| code chip bg | `#202020` | `.cd` inline code |
| pill bg | `#1f1f1f` | `.pill`; hover `#272727` |
| dock / segmented bg | `#191919` | dock nav, header segmented container |
| dock / segmented border | `#232323` | 1px |
| segmented active bg | `#262626` | active segment button (icon tint stays `#9a9a9a`) |
| send / track | `#2a2a2a` | send circle bg, context track bg |
| avatar / tooltip bg | `#262626` | account avatar circle, `.dk` tooltip |
| border subtle | `#222` | sidebar right border, features top border, diff card, Changes button |
| text base | `#d9d9d9` | prose 15/26 |
| text primary | `#e6e6e6` | titles, active session, user bubble, tooltip, code chip, "Changes" |
| text bright | `#f2f2f2` | ASCII logo |
| text white | `#fff` | bold lead-ins in assistant prose |
| text secondary | `#cfcfcf` | pills, feature labels, collapsed project names, context fill, attach/send icons |
| text muted | `#9a9a9a` | default icon tint, chips, context label, Changes summary, collapsed folder icons |
| text dim | `#858585` | placeholders, counts, times, line numbers, action bar, account subline, chevrons |
| session inactive | `#bdbdbd` | non-active session row titles |
| accent | `#7aa7ff` | caret, running spinner/dots, Needs-you chip fg, glow |
| accent border | `rgba(122,167,255,.3)` | composer card border 1px |
| accent chip bg | `rgba(122,167,255,.14)` | Review / Needs-you chips |
| glow gradient | `linear-gradient(120deg, rgba(122,167,255,.6), rgba(122,167,255,.08) 45%, rgba(122,167,255,.4))` | halo, blur 16 |
| diff add fg | `#c4e0c7` | added line text |
| diff add bg | `rgba(143,191,147,.1)` | added line tint |
| diff del fg | `#d4a9a9` | deleted line text |
| diff del bg | `rgba(201,139,139,.09)` | deleted line tint |
| diff stat add | `#8fbf93` | `+N` in diff header |
| diff stat del | `#c98b8b` | `−N` (U+2212) in diff header |
| diff lineno | `#858585` | gutter numbers |

### 2.2 Typography

Families: **Apfel Grotezk** (sans, weights 400/700), **Geist Mono** (mono, 400/500).
Fallback stacks: `'Helvetica Neue', Arial, sans-serif` / `ui-monospace, Menlo, monospace`.
Neither family is currently loaded by the app (§3) — name-only stacks silently fall back.

| Role | Family | Size/lh | Weight | Colour |
|---|---|---|---|---|
| Base prose | Apfel | 15/26 | 400 | `#d9d9d9` |
| Bold lead-in | Apfel | 15/26 | 700 | `#fff` |
| ASCII logo | Geist Mono | 56/56 | 500 | `#f2f2f2` |
| Composer placeholder | Apfel | 16/— | 400 | `#858585` |
| Pill | Apfel | 14/20 | 400 | `#cfcfcf` |
| Chip | Apfel | 11.5/20 | 400 | `#9a9a9a` |
| Inline code `.cd` | Geist Mono | 12.5/— | 400 | `#e6e6e6` |
| Diff header + lines | Geist Mono | 12.5/21 (lines) | 400 | per §2.1 |
| Sidebar search placeholder | Apfel | 14.5/— | 400 | `#858585` |
| `cmd K` hint | Geist Mono | 11.5/— | 400 | `#858585` |
| Projects header | Apfel | 13.5/— | 400 | `#858585` |
| Project name | Apfel | 15/26 | 400 | `#e6e6e6` expanded / `#cfcfcf` collapsed |
| Project count | Apfel | 13/— | 400 | `#858585` |
| Session title | Apfel | 14.5/— | 400 | `#e6e6e6` active / `#bdbdbd` else, ellipsis |
| Running spinner | Geist Mono | 13/— | 400 | `#7aa7ff`, width 1ch in 16px slot |
| Feature label | Apfel | 14.5/— | 400 | `#cfcfcf` |
| Account name / sub | Apfel | 14/— / 12/18 | 400 | `#e6e6e6` / `#858585` |
| Avatar letter | Apfel | 13/— | 400 | base `#d9d9d9` |
| Continue header `.sl` | Apfel | 13/— | 400 | `#858585` |
| Continue title | Apfel | 15/26 | 400 | `#e6e6e6` |
| Continue project | Geist Mono | 12/— | 400 | `#858585` |
| Continue time | Apfel | 13/— | 400 | `#858585`, w34 right; compact `now/5m/2h/3h` (no "ago") |
| Action bar | Apfel | 13.5/— | 400 | `#858585` |
| Token count | Apfel | 13/— | 400 | `#858585` |
| Changes button | Apfel | 14/— | 400 | label `#e6e6e6`, summary `#9a9a9a` |
| Context label | Apfel | 13/— | 400 | `#9a9a9a` |
| Tooltip | Apfel | 12.5/20 | 400 | `#e6e6e6` |
| User bubble | Apfel | 15/26 | 400 | `#e6e6e6` |

### 2.3 Radii

window 12 · row 10 · search box 10 · chip 6 · code chip 6 · tooltip 6 · pill 99 (full) ·
icon button 8 · glow halo 26 · composer card 24 · user bubble 18 · diff card 14 ·
Changes button 14 · dock 16 · dock item 12 · segmented 12 · segment button 8 ·
context track 2 · avatar/send full circle.

### 2.4 Layout metrics

**Shared primitives**
- `.row`: flex, gap 10, padding 6px 10px, radius 10, hover `#1a1a1a` (.2s), `.act` `#1e1e1e`.
- `.ib`: 32×32, radius 8, icon tint `#9a9a9a`; hover bg `#1e1e1e`, tint `#e6e6e6` (.2s both).
- `.pill`: inline-flex gap 6, bg `#1f1f1f`, radius 99, padding 5px 14px, 14/20 `#cfcfcf`, hover `#272727`.
- `.chip`: 11.5/20, `#9a9a9a` on `#1d1d1d`, radius 6, padding 0 8px, flex-none.
- `.cd`: mono 12.5, `#e6e6e6` on `#202020`, radius 6, padding 1px 6px.
- `.dk` tooltip: on hover, bottom 50px, centered, bg `#262626`, 12.5/20 `#e6e6e6`, padding 2px 10px, radius 6, nowrap.
- All SVGs: viewBox `0 0 16 16`, fill none, stroke currentColor, **stroke-width 1.3**, round caps/joins.

**Start screen**
- Top bar: h60, padding 0 20px; left = panel-toggle ib (sidebar icon); right gap 4 = search ib, bell ib (+ pulsing 7px accent dot, top 6 right 7, shown when needs-you > 0), settings ib.
- Center column: flex-1, centered, **padding-bottom 96px**.
- Logo: exact text `█ █ █▀▀ ▀▄▀\n█▀█ ██▄ ▄▀▄`, Geist Mono 56/56 w500 `#f2f2f2`, margin-bottom 46, `rise` entrance.
- Glow composer: **w640**; card bg `#171717`, border 1px `rgba(122,167,255,.3)`, radius 24, padding 20px 22px 14px 22px, column gap 30.
  - Input row: placeholder 16px `#858585` "Describe what to build or fix" + caret (2×20 `#7aa7ff`, margin-left 1, blink).
  - Toolbar gap 8: attach ib (plus icon **18px** `#cfcfcf`) · pill "Auto" + chev-down 12 `#858585` · pill "Sonnet" + chev · flex spacer · mic ib (16px `#9a9a9a`) · send 36 circle `#2a2a2a` arrow-up 16 `#cfcfcf`.
- Continue: **w600, margin-top 48**; header `.sl` padding 0 14px 4px 33px, 13px `#858585`.
  - Rows: padding 7px 14px, gap 12: 7px dot slot (empty, or pulsing accent dot when running) · title flex-1 `#e6e6e6` · project mono 12 `#858585` · chip (This Mac / Cloud / Review-accent) · time 13px `#858585` w34 right.
- Dock: absolute, left 50% translateX(-50%), **bottom 28**, gap 2, bg `#191919`, border 1px `#232323`, radius 16, padding 5; items 42×42 radius 12, icons 18 `#9a9a9a`, tooltips; order **Notebooks, Design, Extensions, Automations, Team**.

**Session screen**
- Sidebar: **w296**, bg `#101010`, border-right 1px `#222`, padding 18px 12px 14px 12px, column.
  - New chat `.row` margin-bottom 8: plus 16 `#e6e6e6` · label flex-1 `#e6e6e6` · folder 16 `#858585`.
  - Search: bg `#1a1a1a`, radius 10, padding 7px 12px, gap 10, margin-bottom 22: search 16 `#858585` · "Search chats" 14.5 `#858585` flex-1 · "cmd K" mono 11.5 `#858585`.
  - Projects header: padding 0 10px 4px, 13.5 `#858585`, plus 14 `#858585` right.
  - Project list: flex-1, gap 2, scrollable.
    - Project `.row`: folder 16 (`#e6e6e6` expanded / `#9a9a9a` collapsed) · name flex-1 · count 13 `#858585` **or** pulsing 7px accent dot (when any session running) · chev-down/right 14 `#858585`.
    - Session `.row` padding-left 18: 16px leading slot (empty, or braille spinner mono 13 `#7aa7ff` centered when running) · title 14.5 ellipsis (`#e6e6e6` on `.act` row, else `#bdbdbd`) · chip right. Active row = `.act` bg `#1e1e1e`, **no border indicator**.
  - Features block: border-top 1px `#222`, padding-top 10, margin 0 −12px, padding-x 12, gap 1; standard `.row`s, icons 16 `#9a9a9a`, labels 14.5 `#cfcfcf`; order **Needs you (accent chip count), Team, Notebooks, Design, Extensions, Automations**; **no badges**.
  - Account footer: padding 12px 8px 0, gap 10: avatar 30 circle `#262626` letter 13 · column lh18 (name 14 `#e6e6e6`, sub 12 `#858585`) · gear 16 `#858585`.
- Header: h60, padding 0 20px, relative: hamburger ib left · segmented **absolute-centered** (bg `#191919`, border `#232323`, radius 12, padding 3, gap 2; four 32×32 ibs: Chat bubble / Files / Browser globe / Terminal; active = bg `#262626`, icon tint unchanged `#9a9a9a`) · right gap 4: Device ib, Team ib, More ib.
- Feed: flex-1, overflow hidden, justify-center, padding-top 10, **top mask fade 28px** (`linear-gradient(to bottom, transparent 0, #000 28px)`); inner align-self flex-end; column **w560, gap 20, padding-bottom 6**, bottom-anchored.
  - User bubble: align-self flex-end, bg `#1e1e1e`, radius 18, padding 10px 18px, max-width 440, `#e6e6e6` 15/26. **No border, no avatar, no header, no timestamp.**
  - Assistant: plain 15/26 `#d9d9d9`; bold lead-ins `#fff` 700; inline `.cd` chips. **No badge row, no avatar, no timestamp.**
  - Diff card: bg `#101010`, border 1px `#222`, radius 14, overflow hidden.
    - Header: flex space-between, padding 8px 16px, border-bottom 1px `#222`, mono 12.5: filename plain · `+N` `#8fbf93` space `−N` `#c98b8b` (U+2212).
    - Body padding 6px 0; lines mono 12.5/21, padding 0 16px, **single gutter**: ` {lineno} {±}    {code}`; lineno `#858585`; del bg `rgba(201,139,139,.09)` fg `#d4a9a9`; add bg `rgba(143,191,147,.1)` fg `#c4e0c7`.
  - Action row: gap 18; buttons gap 6, 13.5px `#858585`, icons 15: **Copy** (files icon + label) · thumbs-up (icon only) · thumbs-down (same icon rotated 180°) · **Retry** (rotate-ccw icon + label + chev-down 12) · token count 13px `#858585` ("12.4k tokens").
  - Changes button: border 1px `#222`, radius 14, padding 12px 16px, gap 10, 14px: files 16 `#858585` · "Changes" `#e6e6e6` · summary flex-1 `#9a9a9a` ("2 files, +22 −33") · chev-down 14 `#858585`. Collapsed by default.
- Composer area: padding 20px 0 26px, gap 10, centered.
  - Context row: w560, gap 10, 13px `#9a9a9a`, padding-left 6: track **88×4** radius 2 `#2a2a2a` with fill (38%) `#cfcfcf` radius 2 · "Context 38% full".
  - Glow composer: **w560**, otherwise identical to Start (placeholder "Ask a follow-up...").

### 2.5 Icon manifest

Existing assets in `App/hex-desktop/assets/icons/` verified **verbatim-identical** to mockup
paths: arrow-up, attach, bell, bolt, chevron-down, design, extensions, mic, notebooks,
pencil, plus, search, settings, sidebar, team. (bell/settings/search match both screens;
the account gear reuses `settings.svg`.)

**Missing — add with these exact paths** (all viewBox 16, stroke 1.3 round, fill none):

| Asset | Path data | Used at |
|---|---|---|
| `folder.svg` | `M2 4.5V12a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1H8L6.5 3.5H3a1 1 0 0 0-1 1z` | New chat, project rows (16) |
| `chevron-right.svg` | `M6 4l4 4-4 4` | collapsed projects (14) |
| `hamburger.svg` | `M2.5 4h11M2.5 8h11M2.5 12h11` | session header left (16) |
| `chat.svg` | `M3 3.5h10a1 1 0 0 1 1 1V10a1 1 0 0 1-1 1H7l-3 2.5V11H3a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1z` | segmented Chat (16); fixes invisible palette icon |
| `files.svg` | `<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/>` + `M10.5 3.5h-6A1.5 1.5 0 0 0 3 5v6` | segmented Files, Copy (15), Changes (16) |
| `browser.svg` | `<circle cx="8" cy="8" r="5.5"/>` + `M2.5 8h11M8 2.5c-2 2-2 9 0 11 2-2 2-9 0-11` | segmented Browser (16) |
| `terminal.svg` | `M3 4.5l4 3.5-4 3.5M8.5 12h5` | segmented Terminal (16) |
| `device.svg` | `<rect x="5" y="2" width="6" height="12" rx="1.2"/>` + `M7.2 12h1.6` | header Device (16) |
| `more.svg` | circles (3.5,8,r.9) (8,8,r.9) (12.5,8,r.9) | header More (16) |
| `thumbs-up.svg` | `M5 7v6H3V7zM5 7l2.5-4.5a1.3 1.3 0 0 1 2.2 1.2L9.2 6H13a1 1 0 0 1 1 1.2l-1 4.8a1.5 1.5 0 0 1-1.5 1H5` | action bar (15) |
| `thumbs-down.svg` | same path with `transform="rotate(180 8 8)"` baked in | action bar (15) |
| `retry.svg` | `M13 8a5 5 0 1 1-1.5-3.6M13 2.5v2.8h-2.8` | action bar Retry (15) |
| `trash.svg` | derived (not in mockup): `M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.7 9h5.6l.7-9` | palette delete; fixes invisible icon |
| `check.svg` | derived: `M3.5 8.5l3 3 6-7` | tool-card success status |
| `x.svg` | derived: `M4 4l8 8M12 4l-8 8` | tool-card failure status |

Icon tinting: GPUI SVGs are monochrome sprites tinted by the element's `text_color` —
set `.text_color(...)` on the svg element per context (default `#9a9a9a` via `icon_svg`).

### 2.6 Motion spec

| Name | Params | Applies to |
|---|---|---|
| `breathe` | 5s ease-in-out infinite; opacity .32 @0/100 → .7 @50 | glow halo (base opacity .5, overridden by animation) |
| `blink` | 1.1s **steps(1)** infinite; opacity 1 @0–55%, 0 @56–100% | composer caret |
| `pl` | 2s ease-in-out infinite; opacity .35 → 1 → .35 | 7px accent dots (bell badge, running project/Continue rows) |
| `spin` | .9s **steps(1)** infinite; 10 braille frames at 0,10,…,90%: `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` (U+280B 2819 2839 2838 283C 2834 2826 2827 2807 280F) | running-session spinner, width 1ch |
| `rise` | .9s **cubic-bezier(.2,.7,.2,1)** both; opacity 0→1 + translateY(6px)→0 | Start logo only, once |
| hover | background .2s, color .2s | rows, pills, icon buttons |
| reduced motion | `@media (prefers-reduced-motion: reduce)` → animation:none on rise, glow, spinner, pl, caret | all of the above |

---

## 3. Current-state audit (deviations, with evidence)

Live render path is `ChatView` (`src/chat.rs`); `AppShell`/`DesktopWindow` are legacy
(test-harness) renders. `theme.rs` already has `mockup_*` tokens (:121–178) but **every
live view still uses the legacy zinc getters** (canvas `#09090b`, card `#18181b`,
hover `#27272a`, borders `#27272a`/`#3f3f46`, text `#fafafa`/`#a1a1aa`/`#71717a`/`#52525b`).

### Confirmed defects (must-fix)

| # | Defect | Evidence |
|---|---|---|
| D1 | **Fonts never loaded** — stacks name Apfel Grotezk/Geist Mono but no `add_fonts` call; woff2 assets unusable by GPUI anyway (TTF/OTF only) | `main.rs` init; `theme.rs` font stacks |
| D2 | **Wrong ASCII logo glyphs** | `ui/home.rs:13` `█ ▀ ██▀▀ ▀▄▀` vs mockup `█ █ █▀▀ ▀▄▀` |
| D3 | **Dock renders literal icon-name text** ("notebooks", "design"…) instead of SVGs | `ui/home.rs:74–96` `.child(glyph)` where glyph is the name string |
| D4 | **Session composer has no glow; Home has double card** | `chat.rs:2393` calls `render_composer` bare; `chat.rs:1029` wraps it in `glow_card` which itself contains a card |
| D5 | **Emoji action bar** "📋 Copy" "🔄 Retry" "· · ·" | `chat.rs:2082–2123` |
| D6 | **Feed not scrollable** (`overflow_y_hidden`, no bottom anchoring, no fade) | `ui/feed.rs` |
| D7 | **12+ missing icons**; palette references nonexistent `chat`/`trash` → invisible icons | `assets/icons/` (15 files); `palette.rs:147,152` |
| D8 | **No caret blink; placeholder wrong colour** (`#52525b` vs `#858585`) | `chat.rs` ComposerElement :2776–2996 |
| D9 | **Glow halo wrong**: hard 2px ring + static shadow, border `#262626` instead of accent .3 | `ui/composer.rs:41–96` |
| D10 | **Header 40px, text segmented labels, "▶"/"◀" text toggle, bell-as-count-text** | `chat.rs:2264–2439` |
| D11 | **Sidebar 260px zinc**, wrong structure (workspace/git pill, provider chip, "Coming soon" badges, Operator footer, amber Needs-you) | `ui/sidebar.rs` |
| D12 | **Session rows**: chronological date groups, two-line zinc rows, left accent border, no projects/chips/spinners, no scroll | `ui/session_list.rs:300–450` |
| D13 | **User bubble** has border + avatar/You/time header, max 460, wrong padding | `ui/message.rs` UserMessageCard |
| D14 | **Assistant badge row** (bolt+HEX+model+time), no inline markdown/code chips | `ui/message.rs` |
| D15 | **Diff view**: sans text_xs, dual 36+36+18 gutters, wrong fg (`#8fbf93`/`#c98b8b` as line fg), 12%/10% tints, radius 6, semibold filename + notebooks icon, "-" not "−" | `ui/diff_view.rs` |
| D16 | **Continue rows**: hover `#1e1e1e` (vs `#1a1a1a`), no dot slot, no project column, chip dim, time 12px "…ago" | `ui/home.rs:107–150`; `chat.rs:874–1024` |
| D17 | **Home layout**: no top bar, Continue mt28 w640, dock gap8 pb18 no container, no pb96 centering | `chat.rs:874–1024` |
| D18 | **Composer internals**: p_4 zinc border-top wrapper, full-width text context bar, inner card `#18181b`/`#27272a` radius xl, input 14/20, zinc divider, ASCII gauge, clear ×, model pill left | `chat.rs:2125–2262` |
| D19 | **Model pill**: h28 radius 6 zinc + provider dot + "▾" text | `ui/model_pill.rs` |
| D20 | **Window** 1200×800 Blurred vibrancy vs 1440×900 opaque `#141414` | `window.rs` |
| D21 | **Peripherals all zinc**: reasoning ▾/▸ folds, tool cards ⏳✓✗ glyphs + `#18181b`/`#0e0e11`, ASCII token gauge ▕█░▏, modals `#18181b`/`#3f3f46` backdrop `rgba(9,9,11,.7)` | `ui/reasoning.rs`, `ui/tool_card.rs`, `ui/token_gauge.rs`, `ui/modals.rs` |
| D22 | **No motion**: no rise, no pl dots, no braille spinner, no hover fades, no reduced-motion handling | throughout |
| D23 | **Changes card** radius 6, always expanded, no icon/chevron, "-" vs "−" | `ui/message.rs` |
| D24 | **Typography defaults** title 15 SEMIBOLD / body 13 / code 12 / caption 11 vs mockup base 15/26 NORMAL | `theme.rs` TypographyTokens |

### Already matching (keep)
Send button (36 circle `#2a2a2a` arrow-up), `icon_svg` default tint `#9a9a9a`, FsAssets
base path, breathe math (5s .32–.7 smoothstep) in `composer.rs`, 15 existing icon assets,
`mockup_*` token values in `theme.rs`, diff stat colours `#8fbf93`/`#c98b8b`.

---

## 4. GPUI 0.2.2 technique map

Verified against gpui-0.2.2 source (`~/.cargo/registry/src/…/gpui-0.2.2/src`).

| Requirement | Technique | Notes / limits |
|---|---|---|
| Custom fonts | `cx.text_system().add_fonts(vec![Cow::Borrowed(include_bytes!(…))])` in init (text_system.rs:104) | **TTF/OTF only** — Windows DirectWrite `CreateInMemoryFontFileReference`. Convert Apfel woff2→ttf (fontTools + brotli); source Geist Mono OFL variable TTF (google/fonts `ofl/geistmono`) and instance statics 400/500 (fontTools instancer). Embed under `assets/fonts/`. |
| 3-stop glow gradient | `linear_gradient(120., from, to)` supports **exactly 2 stops** (color.rs:765) | Approximate 3-stop halo: gradient-filled halo div (inset −2, radius 26) + stacked `BoxShadow`s (blur 16, accent alphas) for the soft falloff CSS `blur(16)` provides; animate whole layer opacity. Verify visually vs mockup; tune stops/alphas. |
| breathe / pl | `with_animation(id, Animation::new(dur).repeat().with_easing(ease_in_out), …)` + `pulsating_between(min,max)` (animation.rs:223,249) | breathe .32–.7 5s; pl .35–1 2s. |
| blink / spin (steps) | `Animation::new(1.1s/0.9s).repeat()` linear; **quantize in closure**: blink `if delta<0.56 {1.} else {0.}`; spin `frames[(delta*10.) as usize % 10]` | Braille glyph coverage: verify Geist Mono renders U+2800 block; else fall back to system mono for the spinner only. |
| rise (custom bezier) | Non-repeating `with_animation` + small cubic-bezier(.2,.7,.2,1) solver (Newton iteration) in closure; animate `.opacity()` + wrapper `.mt()` 6→0 | GPUI has no arbitrary-bezier easing and no translateY style; margin-top shift is layout-equivalent for a one-shot entrance. |
| Hover .2s fades | `on_hover` sets state; `with_animation` 200ms linear interpolating bg/fg between rest/hover values | Only the hovered row animates (state-driven) — cheap on long lists. Helper `hover_row()` in a shared `ui/motion.rs`. |
| Reduced motion | **No GPUI API.** Windows: `SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION)` via windows-sys → `theme.motion_enabled` flag; gate every animation helper | macOS: no equivalent wired (documented limitation); flag defaults true. |
| Inline bold + code chips | Custom `RichTextElement` (model on ComposerElement): parse `**bold**`/`` `code` ``; `shape_line` with `TextRun{font, color, …}`; paint rounded `PaintQuad`s (corner_radii 6, `#202020`) behind code runs | GPUI has no Markdown renderer; `TextRun.background_color` is square — rounded chips need custom paint. Bold runs: weight 700, `#fff`. |
| Bottom-anchored scrolling feed | `list(ListState, …)` with **`ListAlignment::Bottom`** auto-stick (list.rs:78,509) | Custom RichTextElement must implement `request_layout` with measured height. Fallback: `overflow_y_scroll` + scroll-to-bottom on new content. |
| Top 28px fade | Absolute overlay div h28 `.bg(linear_gradient(180., #141414, rgba(20,20,20,0)))` above feed | **No gradient masks / backdrop blur** in GPUI (rect ContentMask only). Plain divs without mouse handlers don't capture clicks — overlay is safe. |
| Window | `WindowConfig` bounds 1440×900, `window_background: Opaque`, root bg `#141414` | **No corner-radius API** — OS chrome rounds corners; do not draw a fake inner frame. Keep `appears_transparent` for mac traffic lights. |
| SVG tint | `.text_color(...)` on the svg element (sprite tinted by parent text colour) | Stroke baked into asset (1.3 round) — no stroke-width API needed. |
| Scrollbars | Style via list/scrollbar API where exposed; target thumb `#2a2a2a`, track transparent, ~6px | Verify API surface in P5; document if not stylable. |

---

## 5. Data honesty & deviations (mockup shows placeholder data)

Policy: **exact visuals, honest data.** Where the mockup shows backend-less placeholder
content, render the real value or an honest neutral; never fake numbers.

| Mockup element | App behaviour |
|---|---|
| "Unlimited credits" subline | "Local mode" until a billing backend exists (flagged deviation) |
| `cmd K` hint | Platform-aware: `ctrl K` on Windows/Linux, `cmd K` on macOS |
| "Cloud" chip | Only when backend reports remote execution; today local-only → "This Mac" |
| "Review" accent chip | Sessions with a pending approval |
| Project names / counts | Workspace directory basename / real session count |
| Running dots / braille spinner | Real `running_turns` state |
| "12.4k tokens" | Real gauge total, compact format; hidden when unknown |
| "Context 38% full" | Real context gauge percentage |
| "Auto" pill | Mode selector if ACP exposes modes; else coming-soon notice |
| "Sonnet" pill | Existing real model picker, restyled |
| Attach / mic buttons | Rendered per mockup; disabled with tooltip/notice until backend support |
| Device / Team / More / feature rows | Notice surfaces (no backend); More menu holds Settings |
| Bell | Home top bar (+pl dot when needs-you>0) and sidebar Needs-you row — both per mockup; session header right stays Device/Team/More |
| "Coming soon" badges | Removed (not in mockup); click→notice behaviour retained |
| Provider chip, message avatars/timestamps/badges, ASCII gauge, clear × | Removed (not in mockup) |
| Streaming indicator | Braille `.sp` spinner at feed tail while streaming (derived from mockup's running-session spinner) |
| Tool cards, reasoning folds, modals, fenced code blocks, empty states, notices | Not in mockup — styled from the same token language (derived, documented) |
| Window frame border/radius | OS chrome; root bg `#141414`, title "HEX" |
| Continue time column | Compact `now/5m/2h/3d/Mon d` (mockup format, no "ago") |

---

## 6. Phased implementation

Each sub-phase = one commit with red-proof regression test(s); suite green at every commit.
Legacy zinc tokens stay until P6 so existing theme tests remain green mid-flight.

### P0 — Foundations
1. **Fonts**: convert Apfel Grotezk 400/700 woff2→ttf; source Geist Mono OFL variable TTF,
   instance 400/500; embed 4 TTFs in `assets/fonts/`; `add_fonts` in init; verify family
   resolution. *Test:* loaded families resolve to embedded fonts (not fallback).
2. **Tokens + type scale**: complete `mockup_*` getters (add row_hover `#1a1a1a`, pill
   `#1f1f1f`/`#272727`, dock `#191919`/`#232323`, seg_active `#262626`, track `#2a2a2a`,
   inactive `#bdbdbd`, accent_border, accent_chip_bg, diff line fg/tints, base `#d9d9d9`);
   add mockup type-scale constants (15/26 base etc.) without mutating legacy defaults.
   *Test:* new token values byte-exact.
3. **Icons**: add the 15 assets from §2.5; fix `palette.rs` chat/trash references.
   *Test:* every referenced icon name exists and parses as SVG.
4. **Window**: 1440×900 default, Opaque, root `#141414`. *Test:* config assertions.
5. **Motion module** `ui/motion.rs`: breathe/blink/pl/spin/rise helpers + cubic-bezier
   solver + reduced-motion detection (Windows SPI). *Tests:* blink duty quantization,
   spin frame sequence, bezier endpoints, breathe range.

### P1 — Glow + shared composer
- Rebuild `glow_card`: border accent .3, halo = gradient div + stacked blur-16 shadows,
  breathe .32–.7; single card (fix D4 double-card / missing session glow).
- ComposerElement: caret 2×20 accent **blink**, placeholder 16px `#858585`, input `#e6e6e6`.
- Toolbar exact: attach 18 `#cfcfcf`, Auto pill, model pill, spacer, mic 16, send 36.
- Rebuild pill component (`#1f1f1f` radius 99 5×14 14/20 `#cfcfcf` chev SVG, hover `#272727`).
- Context bar: 88×4 track radius 2 `#2a2a2a`, fill `#cfcfcf`, label 13px `#9a9a9a`
  "Context N% full", w560 pl6 — session only. Remove ASCII gauge + clear × + zinc wrapper.
- *Tests:* pill tokens/geometry, glow border colour, context label format, composer
  renders one card on both screens (element-tree smoke via TestAppContext).

### P2 — Start screen exact
- Top bar h60 px20: panel toggle left; search/bell(+pl dot)/settings right gap4.
- Logo: fix glyphs (D2), Geist Mono 56/56 w500, mb46, **rise** entrance.
- Center pb96; Continue w600 mt48, `.sl` header; rows exact (dot slot/pl, title, project
  mono 12, chip variants incl. Review accent, compact time w34); hover `#1a1a1a`.
- Dock: container bg `#191919` border `#232323` radius 16 p5 gap2 bottom28 centered;
  items 42 radius12 icons 18 `#9a9a9a` (fix D3 literal text); tooltips exact (`#262626`,
  12.5/20, 2×10, radius 6, bottom 50).
- *Tests:* `ASCII_LOGO` exact string; compact relative-time fn (`now/5m/2h/3d/Mon d`);
  dock uses svg assets not name text; Continue row token assertions.

### P3 — Session shell
- Sidebar 296 `#101010` border-r `#222` padding 18/12/14: New chat row, search box
  (`#1a1a1a` radius10 7×12 mb22, platform-aware shortcut hint), Projects header + plus,
  project groups (expand/collapse, folder tint states, count vs pl dot, chevrons),
  session rows (pl18, 16px slot/braille spinner, 14.5 ellipsis titles, chips, `.act`),
  features block (exact order, no badges, Needs-you accent chip), account footer
  (avatar 30, Account/Local-mode, gear). Scrollable project list.
- `SessionListView` model: group by workspace basename; running/location/review state;
  keep pin/rename/archive/delete logic.
- Header h60: hamburger toggle; absolute-centered icon segmented (Chat/Files/Browser/
  Terminal, active bg `#262626`); right Device/Team/More gap4.
- *Tests:* project grouping logic, spinner frame integration, chip mapping
  (This Mac/Review), sidebar/header metric tokens.

### P4 — Feed, messages, diff, actions
- Feed: `list` Bottom-anchored w560 gap20 pb6 pt10 + 28px gradient fade overlay;
  reconnect/empty/notice states restyled to tokens.
- User bubble exact (440 max, 10×18, radius 18, `#1e1e1e`, no header/border).
- Assistant: `RichTextElement` — 15/26 `#d9d9d9`, bold `#fff` lead-ins, rounded `.cd`
  chips (mono 12.5, `#202020`, radius 6, 1×6); fenced blocks restyled; streaming spinner.
- Action bar: Copy (files 15 + label), thumbs-up/down (15, down rotated), Retry
  (retry 15 + label + chev 12), real token count 13px; gap 18/6; **no emoji** (D5).
- Changes button: collapsed, border `#222` radius 14 12×16 gap10, files 16, summary
  "N files, +A −D" (U+2212), chev 14; expand → diff cards.
- Diff view rebuild: bg `#101010` radius 14; header mono 12.5 plain filename + `+N`/`−N`;
  body 6×0; lines mono 12.5/21 px16 single gutter ` num ± code`; exact fg/tints per §2.1.
- *Tests:* rich-text parser (bold/code run boundaries), diff line format + colours,
  action bar contains no emoji codepoints, Changes summary format, fade overlay present.

### P5 — Peripheral surfaces
- Tool cards: token-language card (surface/border subtle/radius 14), check/x/retry icons
  instead of ⏳✓✗, mono input/output blocks `#101010`.
- Reasoning folds: chevrons instead of ▾/▸, dim labels, token surfaces.
- Modals (approval/palette/picker): backdrop scrim over `#141414`, card surface
  `#171717` border `#262626` radius 14–24, rows `.row` style, accent focus, mono hints;
  palette chat/trash icons now resolve.
- Settings surface + scrollbar styling (thumb `#2a2a2a`) where API allows.
- *Tests:* modal token assertions, palette icon resolution, tool-card status icon names.

### P6 — Legacy retirement + token prune
- Restyle or retire `AppShell`/`DesktopWindow` legacy renders onto the same components.
- Remove legacy zinc getters + legacy typography defaults; **intentionally update**
  `tests/test_theme_and_window.rs` and `theme.rs` inline tests to the canonical mockup
  palette (single documented commit; red-proof the new assertions first).
- *Tests:* updated theme suite; grep proves no zinc hex remains in `src/`.

### P7 — Polish + verification
- Hover .2s fades on all rows/pills/ibs; reduced-motion gating audit (every helper).
- Screenshot comparison loop (§7) at 1440×900 for both screens; fix residual deltas.
- Full `cargo test -p hex-desktop` + clippy; final commit.

---

## 7. Verification protocol

1. **Build/test:** `cargo test -p hex-desktop` (and `cargo clippy -p hex-desktop`) green
   at every commit; `HEX_TESTING=1` where harness requires.
2. **Ground truth:** serve `docs/design/mockups-desktop` (`python -m http.server`), open
   each artboard in the in-app Browser panel at 1440×900, capture full-page screenshots.
3. **App capture:** `cargo run -p hex-desktop` (window defaults 1440×900 after P0);
   PowerShell `System.Drawing.CopyFromScreen` of the window rect → PNG.
4. **Compare:** side-by-side visual review per region (top bar, logo, glow, Continue,
   dock / sidebar, header, feed, diff, actions, composer); optional Python/PIL region
   pixel-diff for colour/geometry checks. Iterate until no observable delta.
5. **Motion check:** observe breathe/blink/pl/spin/rise live; toggle Windows
   "Show animations in Windows" off → all motion stops (reduced-motion path).
6. **Behaviour regression:** send a prompt, stream a reply, approve a tool, switch
   sessions, open palette/model picker — all existing flows still work.

## 8. Risks & limitations

- **Font conversion fidelity**: woff2→ttf is lossless for outlines; verify Apfel licence
  permits embedding (user-supplied design asset). Geist Mono is OFL — safe.
- **3-stop halo**: GPUI gradients are 2-stop; stacked-shadow approximation must be
  tuned visually (P1). No backdrop blur exists — shadow blur is the closest primitive.
- **Braille coverage**: if Geist Mono lacks U+2800 glyphs, spinner falls back to system
  mono (13px, accent) — visually near-identical at 1ch.
- **Custom element heights**: RichTextElement must measure correctly for `list`
  virtualization; fallback to `overflow_y_scroll` + manual bottom-stick if needed.
- **Reduced motion on macOS**: not wired (no GPUI API); documented limitation.
- **Window corners**: OS-managed; the artboard's 1px `#262626` frame is not reproducible
  inside the client area and is intentionally not faked.
- **Legacy test churn**: zinc assertions are updated deliberately in P6, not silently.
# HEX TUI — UI Improvement Plan (2026-11-16)

Scope: the Ratatui cockpit (`crates/hex-tui`) measured against the six mockups in
`docs/design/mockups/` and against what a user actually sees today. Companion to
`docs/audits/FIT_AND_FINISH_HERMES_VS_HEX_2026-11-16.md`.

Design sources (all read): `Hex agent · session-html/Main.dc.html`,
`permission-html/Permission.dc.html`, `palette-html/Palette.dc.html`,
`error-html/Error.dc.html`, `subagent-html/SubAgent.dc.html`, `start-html/Start.dc.html`.

---

## 0. The headline finding

**The overlays you asked for already exist as pixels — they are simply never
reached in production, and the transcript never received the parity pass.**

Phase 35 (` .gsd/phases/35-tui-redesign-mockup-parity/`) built and "verified" the
permission modal, command palette, subagent modal and diagnostic card by
*injecting state directly in tests*. No production code path sets that state:

| Surface | Renders? | Production trigger | Evidence |
|---|---|---|---|
| Permission modal | yes (`ui/screens/permission.rs`) | **none** — server never emits the request | `acp.py:623` `request_approval()` defined, zero callers; no `WAITING_APPROVAL` handling in `acp.py` |
| Command palette | yes (`ui/screens/palette.rs`) | **none** — no `Ctrl+P` handler, `show_palette` set only in tests | `main.rs` has only `Ctrl+C` at :448; grep `show_palette = true` in `src/` → 0 hits |
| Subagent modal | yes (`ui/screens/subagent.rs`) | **none** — `active_subagent_modal` set only in tests | `ui/mod.rs:53-55` |
| Diagnostic card | yes (`ui/screens/error_view.rs`) | **none** — `active_diagnostic` only ever `None` | `app.rs:1104`; tool errors just call `complete_tool_call(..., true)` (`main.rs:936-938`) |
| Slash popup | yes, small box above composer | works, but it is **not** the palette you designed | `session.rs:337-373`, `start.rs:176-212` |

Meanwhile the transcript — the thing you stare at 95% of the time — renders each
message as **one raw `Line`** with no markdown and no separation
(`session.rs:163-171`), which is exactly the wall of `**bold**` text in your
screenshot. Phase 35-02's "pixel-matched session" claim was chrome-only.

So the plan is not "build new UI". It is: **format the transcript, give the
existing overlays real triggers and real keys, and make the footer stop
advertising bindings that do not exist.**

---

## 1. Design → terminal translation table

Mockups are 1200×760 px, Geist Mono 13px / 21px line-height. One terminal row ≈
one 21px line; one column ≈ one 13px-mono char (~7.8px). Use these budgets:

| Design value | Terminal equivalent |
|---|---|
| dialogue gap 14px between message blocks | **1 blank row** between blocks |
| left column padding `20px 28px` | 1 row top, **3 cols** left/right inside the canvas |
| assistant prose indent `2ch` | 2 cols (already present as `Span::raw("  ")` — keep) |
| user prefix `> ` at column 0 | keep as-is |
| input box padding `10px 14px`, radius 6 | composer **5 rows**: border + 1 pad row + content + 1 pad row + border; 2 cols inner left pad (start screen already does this; session is 3 rows today, `session.rs:118`) |
| footer 34px, padding `0 28px` | 1 row, 3 cols left/right pad |
| modal width 560px / palette 580px / subagent 660px | **60 / 62 / 70 cols**, clamped to `area.width - 8` |
| modal padding 16px, column gap 12px | 1 row / 2 cols inner pad; 1 blank row between modal sections |
| option row padding `2px 10px`, radius 4 | 1 row per option, 2 cols inner pad, selected row bg `#1a1a1a` |
| overlay dim `rgba(10,10,10,.62)` | keep existing dim fill; palette anchored **top** (pad ≈ 4 rows below header), permission **centered** |
| code/diff tinted full-bleed rows | reuse the existing diff tint (`rgba(143,191,147,.09)` / `rgba(201,139,139,.08)` equivalents in `style.rs`) for code fences |

Colors already exist in `ui/style.rs` from phase 35-01 (`#0e0e0e` app bg,
`#d6d6d6` body, `#bdbdbd` prose, `#8c8c8c` dim, `#7d7d7d` extra-dim, `#7aa7ff`
blue, `#8fbf93` green, `#c98b8b` red, `#1f1f1f` rules, `#333` modal border).
**No new tokens needed** except `#151515` (chip/option hover) and `#1a1a1a`
(selected option) — add both to `HexTheme`.

---

## 2. Gap register

### G01 — Model output is unformatted raw text (your screenshot 2)
- **Symptom:** `**Core engineering**`, `- bullets`, headings and code fences all
  arrive as literal characters in one wrapped paragraph.
- **Evidence:** `session.rs:168-171` pushes `Span::styled(&m.content, …)` as a
  single `Line`; `Paragraph::wrap` at :242-244 is the only structure. No markdown
  parser anywhere in the crate (grep pulldown/comrak/cmark → 0).
- **Irony:** the *legacy Python console* already has GFM→ANSI
  (`hex_cli/tui.py:547 format_terminal_markdown`, streaming
  fence-aware renderer at :656). The Rust cockpit regressed relative to it.
- **Fix:** new `crates/hex-tui/src/ui/markdown.rs` turning message text into
  `Vec<Line>` in the mockup's visual grammar (mono, restrained color):
  - `**bold**` / `__bold__` → `Modifier::BOLD`, markers removed; `*em*` → italic-or-dim.
  - `# heading` → bright + bold, one blank row before.
  - `- item` / `1. item` → hanging indent: marker col 2, continuation col 4.
  - ```` ```lang ```` fences → full-bleed tinted block (reuse diff tint), language
    label extra-dim on the opening row; unclosed fence during streaming stays
    tinted until closed.
  - inline `` `code` `` → tinted span.
  - plain prose → `#bdbdbd`, wrapped at canvas width minus indent.
  - Parser: `pulldown-cmark` (pure Rust, no C deps) event stream → Lines; cache
    parsed Lines per message and reparse only the appending tail while streaming.
- **Acceptance:** buffer-render test asserting a message containing
  `**bold**`, a bullet list and a fence produces BOLD spans, hanging indents and
  tinted rows, and contains no literal `**` or ```` ``` ````.

### G02 — No spacing / rhythm in the transcript (your screenshot 2)
- **Symptom:** messages and tool rows are back-to-back; the dialogue touches the
  window border; composer is cramped.
- **Evidence:** `session.rs:159-202` pushes items with zero separators; no
  `Padding` anywhere in the file; canvas constraints `session.rs:114-121`.
- **Fix:** 1 blank row between message blocks (G01 output already groups a
  message into a block); 3-col left/right canvas padding; 1-row top pad;
  composer 3→5 rows with inner padding; footer 3-col side padding; tool result
  lines keep their 4-col indent but gain a 1-row gap before the next block.
- **Acceptance:** render test at 120×40 asserting row indices: first prose row ≥
  header+2, blank row between two messages, composer inner height 3.

### G03 — `/` does not open the palette you designed (your image 2)
- **Symptom:** typing `/` shows a small 8-row suggestion box glued above the
  composer, not the top-anchored fuzzy panel with grouped results, highlighted
  match characters and a `3 results · enter select · esc close` footer.
- **Evidence:** small popup `session.rs:337-373`; palette panel exists
  (`palette.rs`) but substring-only filter (:81-94), no trigger, no query editing.
- **Fix (behaviour per your clarification):**
  1. `/` at an empty composer (session **and** start) opens the palette overlay
     with an empty query; further typing goes into `palette_query`, not the
     composer. `Esc` closes and restores the composer; `Enter` runs the selected
     command; `Backspace` on empty query closes.
  2. Restyle `palette.rs` to `Palette.dc.html`: 62-col panel anchored ~4 rows
     below the header; search header `> query█` with border-bottom; group
     headers (`agents`, `session`, …) extra-dim; rows = blue `>` marker when
     selected + label with **fuzzy-matched chars bright/bold** + right-aligned
     key hint; selected row bg `#1a1a1a`; footer `N results` | `enter select · esc close`.
  3. Fuzzy = subsequence match returning matched indices (extend the existing
     `is_subsequence` at `app.rs:2181` to return positions); rank prefix matches
     first (reuse the ranking at `app.rs:2007-2013`), then match density.
  4. `Ctrl+P` opens the same panel with empty query (footer hint becomes true).
  5. Delete the small slash popup from both screens; update
     `test_slash_command_suggestion_rendering` and the start-screen tests to
     assert the palette instead. One surface, not two.
- **Acceptance:** tests for (a) `/` then `sw` yields "Switch agent / Switch
  model / Switch session" with matched chars highlighted, (b) `Enter` executes,
  (c) `Esc` restores composer text, (d) `Ctrl+P` parity, (e) no literal popup
  remains.

### G04 — Permission modal never appears, and its radio is dead (your image 1)
- **Symptom:** risky commands run without asking; when the modal is force-shown
  in tests, `up/down`, `Enter` and `a` do nothing.
- **Evidence:** server `request_approval()` (`acp.py:623-657`) has **no callers**;
  `orchestrator.execute_task` is invoked with no approval hook (`acp.py:412-416`);
  `ApprovalRequiredInterrupt` parks the task in `WAITING_APPROVAL`
  (`core/orchestrator.py:2112-2140`) but nothing notifies the client;
  `session/approvalDecision` is recorded into `pending_approvals` and never
  consumed (`acp.py:602-621`). Client-side, `selected_permission_index` is only
  ever initialised (`app.rs:1092`) — never mutated.
- **Fix, server first:**
  1. Wire the orchestrator's approval interrupt into the ACP turn: emit
     `session/approvalRequest` with `{tool, args, blast_radius, cwd}`, suspend the
     turn, and resume on `session/approvalDecision` (allow once / allow scope /
     deny). Persist scope grants in the existing `approval_leases` table so
     "Allow rm in this project" survives the turn.
  2. Deny → tool result `denied by operator`, turn continues; timeout → deny.
- **Fix, client:**
  3. Keys in `main.rs`: `Up/Down` move `selected_permission_index` over the three
     options, `Enter` confirms the selected, `y`/`a`/`n` direct-select, `Esc` =
     deny. Send the matching decision scope.
  4. Restyle `permission.rs` to `Permission.dc.html` / your image 1: title row
     "Permission required" + tool name right; `$ command` chip on `#151515`;
     blast-radius sentence dim; rule; three option rows with right-aligned
     `y / a / n`; selected row `#1a1a1a` + blue `>`.
  5. Waiting state: composer placeholder "Waiting for permission" at half
     intensity (already present, `session.rs:316-320`), pending tool row verb
     blue + `· waiting for permission`, footer hints swap to
     `up/down select · enter confirm · esc deny`, rail agent line
     `blocked / waiting on you`.
- **Acceptance:** hermetic Python test that a risky tool call emits
  `approvalRequest` and blocks until a decision; Rust tests that arrows/Enter/`a`
  change selection and emit the right decision; pty capture of the modal.

### G05 — Failure card and subagent modal are orphaned
- **Evidence:** `active_diagnostic` never assigned in production (`app.rs:1104`);
  `active_subagent_modal` set only in tests; footer advertises `tab agents`
  (`session.rs:395`) while `Tab` actually cycles mode (`main.rs:553-557`).
- **Fix:**
  1. On a tool `error` event, populate `active_diagnostic` from the tool result
     (exit code, expected/received, stack frames) so `error_view.rs` renders
     inline per `Error.dc.html`; footer flips to red `1 failing` (already wired).
  2. **Decision needed:** design says `tab agents`; code says `Tab` = mode cycle.
     Recommendation: in session, `Tab` opens the agents/subagent modal (matching
     the design and the palette's "Switch agent … tab"); move mode cycling to a
     palette command (`Switch mode`) and keep `tab agent` on the start screen.
     Update the start-screen hint tests accordingly.
  3. Subagent modal restyle to `SubAgent.dc.html`: 70-col centered, prompt
     clamped 2 rows + `e expand`, transcript gap 1 row, footer
     `up/down scroll · e expand prompt · esc close` + spinner/elapsed.
- **Acceptance:** tool-failure render test shows the card inline; `Tab` opens the
  modal; `e`/`Esc`/arrows work.

### G06 — Footer hints advertise non-existent bindings
- **Evidence:** `session.rs:395` prints `ctrl+p commands · tab agents` today,
  before either exists.
- **Fix:** derive the hint string from the actual keymap (single source of truth
  in `main.rs`), so a hint can never outlive its binding again. Lands with G03/G05.

### G07 — Motion polish (last, optional)
- Line entrance rise + stagger, 16px top fade mask on the transcript, tool-row
  hover `#141414`, cursor blink 1.1s steps. Animation engine already exists
  (`ui/animation.rs`); add a reduced-motion env flag. Purely additive.

---

## 3. Phasing

| Phase | Contents | Why this order | Est. |
|---|---|---|---|
| **P1** | G01 + G02 (markdown module, spacing, composer/footer padding) | fixes both complaints visible in every screenshot; no protocol changes | 1 session |
| **P2** | G03 + G06 (palette via `/` and `Ctrl+P`, fuzzy highlight, truthful hints) | your explicit request; self-contained client work | 1 session |
| **P3** | G04 (approval protocol end-to-end + modal keys/restyle) | only phase touching the Python server; needs the ledger lease semantics | 1–2 sessions |
| **P4** | G05 (diagnostic wiring, agents modal, Tab decision) | depends on P2's palette for mode cycling | 1 session |
| **P5** | G07 motion | cosmetic, after structure is right | ½ session |

Each phase: implement → hermetic tests → pty screenshot compare against the
mockup → **commit** (per your standing rule).

## 4. Verification recipe

First run `.venv/bin/python -m pip install -e ".[full]"` from the checkout root, including after relocation, to refresh editable package mappings before running from a temporary workspace.

- Rust: `cargo test -p hex-tui -p hex-protocol` plus new buffer-render tests per
  gap (assert exact rows/spans, not just "renders").
- Python (P3): hermetic run —
  `TMPHOME=$(mktemp -d); TMPWS=$(mktemp -d); cd "$TMPWS"; HOME="$TMPHOME" HEX_TESTING=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 <repo>/.venv/bin/python -m pytest <repo>/tests -p no:cacheprovider -q`
  (must leave `.hex/data/ledger.db` task count unchanged).
- Visual: pty capture at 40×120 with CUP reconstruction (the recipe used for the
  start-screen verification), diffed against the mockup's region layout.
- No network in any test; approval tests use a fake risky tool.

## 5. Risks / open decisions

1. **Markdown crate**: `pulldown-cmark` adds a dependency to `Cargo.lock`;
   alternative is a ~200-line hand-rolled parser. Recommendation: pulldown-cmark
   (correct GFM tables/lists for free), wrapped so streaming tails reparse cheaply.
2. **Tab semantics** (G05.2) — needs your call; recommendation above.
3. **Approval resume semantics**: suspending a turn mid-stream must not lose
   partial output; checkpoint the dialogue before emitting the request.
4. **Two slash surfaces → one**: deleting the small popup changes existing
   tests; they must be rewritten, not deleted silently.
5. Phase 35's STATE.md marks 35-04 "COMPLETE & VERIFIED" while diagnostic wiring
   and palette fuzziness are absent — correct the record when P2/P4 land.

---

## 6. Closure record (2026-11-17)

All seven gaps are implemented, tested and committed on `main`, one commit per
gap as requested:

| Gap | Commit | What landed |
|---|---|---|
| G01 | `5c3b05e` | model output rendered as markdown (headings, lists, code, tables) |
| G02 | `9ccba1e` | transcript/composer/footer spacing and block rhythm |
| G03 | `c214574` | 62-column top-anchored palette on `/` and `Ctrl+P`, fuzzy match highlight |
| G06 | `6b38894` | footer hints derived from the key handler's real bindings |
| G04 | `27388c6` | approval end to end: ACP request/decision, leases, working modal keys |
| G05 | `d1f2f38` | failure card and Tab agents drill-down fed by real server data |
| G07 | `f8afd11` | entrance rise + stagger, one-row top mask, tool-row hover, reduced motion |

Verification at closure: 235 Rust tests (`hex-tui` + `hex-protocol`), 1177 Python
tests passed / 1 skipped in the hermetic recipe, `cargo check -p hex-desktop`
clean, and pty captures with per-cell colour reconstruction at 120x24 and 80x24
(full motion and `HEX_REDUCED_MOTION=1`), 14/14 checks each. The real ledger was
left untouched at 75 tasks throughout.

### Decisions taken
- **Tab** opens the agents/subagent modal (your call). Mode cycling lives on
  `F3` and the palette's `/mode`, so the two surfaces no longer compete.

### Deviations from this plan, still open
1. **G04 deny semantics.** Denying travels the existing `PolicyBlockedError`
   path: the CoW snapshot rolls back and the turn ends FAILED with the denial
   reason. The plan asked for "tool result denied by operator, turn continues";
   continuing the turn needs ReAct-loop surgery (`react_loop.py` classifies
   `PolicyBlockedError` as non-transient). Not done.
2. **Tool events only on the fast path.** Only `_execute_fast_path` consumes
   `tool_callback`, so agent-mode DAG turns still emit no tool rows and the
   failure card has nothing to attach to on those turns. G05 fixed the events
   that exist; it did not add them to the DAG executor.
3. **Risk 5 discharged.** The phase-35 record was corrected in `4508ddc` and
   again at closure; `.gsd/STATE.md` now carries the amendment.

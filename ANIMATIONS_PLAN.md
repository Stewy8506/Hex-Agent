# Animations plan

**Status:** PLANNED — not started. Standalone from `ROADMAP.md` (DX track) and `BACKEND_PLAN.md` (cloud phases).
**Track:** AN1 → AN2 → AN3 → AN4. One commit per phase in the desktop repo (`feat(hex-desktop): AN1 …`); shared protocol changes land in `Harness`. Never push unless asked. `backend/` and runtime `data/` are never touched. AN-U (optional gpui upgrade spike) is deferred and gated — see §10; it is not on the critical path.
**Subject:** the GPUI desktop app (`../Hex-App`, Rust). The TUI (`crates/hex-tui`) and the Python harness are out of scope — terminal cells and JSON-RPC have no spring surface. No audio: this is a productivity cockpit, sound does not belong to it.

The goal is Apple-style physics-based motion: springs over curves, mandatory interruptibility, velocity handoff, subtle critically-damped defaults, transform+opacity choreography, and reduced-motion respect. This plan translates those principles into what **gpui 0.2.2 actually exposes** (verified against the vendored registry source), extends the existing `ui/motion.rs` foundation rather than replacing it, and assigns every animated surface a preset, a variant, and an interruption contract.

---

## 1. Ground truth — gpui 0.2.2 animation capabilities

Source: `~/.cargo/registry/src/index.crates.io-*/gpui-0.2.2/src/`. These facts drive every decision below.

| Capability | API (verified) | Consequence for this plan |
|---|---|---|
| Time-based element animation | `Animation::new(Duration).with_easing(impl Fn(f32)->f32)`; `AnimationExt::with_animation(id, anim, \|el, delta\| …)` (`elements/animation.rs`) | Custom easing means a **spring step-response can be plugged in as the easing function** — no manual driver needed for one-shot entrances. |
| Eased-delta assertion | `debug_assert!((0.0..=1.0).contains(&delta))` (`animation.rs:166`) | **Underdamped springs (bounce > 0) overshoot past 1.0 and would panic in debug builds inside `AnimationElement`.** Only monotonic, critically-damped curves are safe there. Bouncy motion must use the per-frame driver (§4, Tier B). |
| Animation state persistence | `AnimationState { start, animation_ix }` stored per element id via `window.with_element_state` (`animation.rs:140`) | Re-rendering with the **same id does not restart** the timeline; a new id starts a fresh one at delta 0. `AnimationElement` therefore **cannot retarget mid-flight with velocity** — it is for fire-and-forget entrances/loops, not interactive state. |
| Per-frame scheduling | `Window::on_next_frame(FnOnce(&mut Window, &mut App))` (`window.rs:1644`), `Window::request_animation_frame()` (`window.rs:1654`) | A spring simulator can tick every frame and re-register itself while any spring is active. |
| Targeted repaint | `App::notify(EntityId)` (`app.rs:2034`), `Context::entity()` / `entity_id()` (`app/context.rs:45,50`) | The driver repaints only the entities that own active springs — not the whole app. |
| Shared mutable state | `cx.global::<G>()`, `global_mut::<G>()`, `set_global`, `default_global` (`app.rs:1457–1497`) | A single `MotionSystem` global holds all springs, keyed by owner + name; one driver per window. |
| Opacity | `Styled::opacity(f32)` (`styled.rs:652`) | The primary GPU-friendly animated property. |
| **Div transforms** | **None.** No `scale`/`translate`/`rotate`/`transform` on `Div`/`Styled`. `TransformationMatrix` exists only for SVG (`elements/svg.rs:35 with_transformation`, `Transformation::rotate`) and internal paint paths. | **Scale/translate must be emulated.** Translation = positional offset (`.mt(px(...))`, `.top()`, `.left()` — macro-generated via `gpui_macros::style_helpers!`). The existing home rise already proves the `.mt(px(offset))` + opacity pattern (`ui/home.rs:106`). Press "scale" becomes opacity compression + a 1px offset (§4.5). This is an honest constraint, not a shortcut. |
| Pointer events | `on_mouse_down`, `on_mouse_up`, `on_mouse_move`, `on_mouse_down_out` (`elements/div.rs:121–263`) | Press feedback and (future) 1:1 drag tracking are feasible. |
| Color interpolation | No `Interpolate` trait in 0.2.2. `Hsla { h, s, l, a }` fields are `pub` (`color.rs:272`). | Manual component lerp with shortest-hue path (§4.4). |

**Net architecture decision:** two execution tiers.
- **Tier A — `AnimationElement` + spring easing:** one-shot entrances and ambient loops where the curve is monotonic (critically damped `smooth`, or opacity-only fades). Cheap, no entity state, debug-assert safe.
- **Tier B — `MotionSystem` global + per-frame spring simulator:** the **default for all interactive state** (modals open/close, toggles, expands, chips, press feedback, sidebar, session switch). Full interruptibility, velocity handoff, and bounce > 0 without the assertion constraint.

---

## 2. Existing foundation — keep and extend

`../Hex-App/src/ui/motion.rs` already exists (mockup-parity §2.6) and is **not** greenfield:

- Ambient periodic helpers, each gated on `reduced_motion()`: `breathe_opacity` (5s glow halo), `pulse_opacity` (2s accent dots), `blink_visible` (1.1s caret), `spinner_frame` (0.9s braille), `rise_progress` (0.9s entrance, `cubic_bezier(.2,.7,.2,1)`).
- `cubic_bezier(x1,y1,x2,y2,x)` bisection evaluator — reusable.
- `reduced_motion()` — Windows `SPI_GETCLIENTAREAANIMATION` probe via `user32`, fail-open to motion allowed (`motion.rs:98`). **This is the reduced-motion source of truth; reuse it, do not reinvent.**
- ~10 animation sites already gate on it (commit `cf9125c` "gate the last animation"): `chat.rs:1241,4505`, `composer.rs:176`, `feed.rs:443`, `home.rs:95,216`, `message.rs:666`, `sidebar.rs:433,515`, `tool_card.rs:145`.
- Unit tests for every helper (`motion.rs:128–208`).

**Policy:** ambient loops (breathe/pulse/blink/spinner) stay as they are — they are mockup parity and already reduced-motion gated. The interactive/entrance easing (`rise_progress` cubic-bezier) is what gets replaced by springs. The module grows into `ui/motion/` with the existing public API re-exported unchanged so no current consumer breaks.

---

## 3. Spring model and presets

Physics: `F = -k·x - c·v`, mass `m = 1`. Perceptual inputs `duration` (settling time) and `bounce` ∈ [0,1] convert exactly as specified:

```
stiffness k = (2π / duration)²
damping   c = (1 - bounce) · (4π / duration)
```

With `m = 1` this yields the clean identities the engine relies on:

```
natural frequency  ωₙ = √k = 2π / duration
damping ratio      ζ  = c / (2√k) = 1 - bounce
```

So **bounce 0 ⇒ ζ = 1 (critically damped, zero overshoot)**, bounce 0.15 ⇒ ζ = 0.85, bounce 0.3 ⇒ ζ = 0.7.

### 3.1 The three presets (standardized app-wide)

| Preset | duration | bounce | k (≈) | c (≈) | ζ | Use |
|---|---|---|---|---|---|---|
| `smooth` | 0.45s | 0.0 | 195.0 | 27.93 | 1.00 | **Default (~80%)**: modals, sheets, page/session transitions, layout shifts, entrance rises. |
| `snappy` | 0.35s | 0.15 | 322.3 | 30.47 | 0.85 | Micro-interactions: toggles, tab/segment indicators, dropdowns, button press/state, chips, hover. |
| `bouncy` | 0.45s | 0.30 | 195.0 | 19.55 | 0.70 | Sparingly: notification/banner entrance, drag snaps, celebratory moments. |

### 3.2 Analytic step response (Tier A easing)

For a unit step target, normalized time `t ∈ [0,1]` mapped to seconds `τ = t · duration`:

- **Underdamped (ζ < 1):** `ω_d = ωₙ·√(1-ζ²)`;
  `s(τ) = 1 - e^(-ζ·ωₙ·τ) · ( cos(ω_d·τ) + (ζ·ωₙ/ω_d)·sin(ω_d·τ) )`
- **Critically damped (ζ = 1):**
  `s(τ) = 1 - e^(-ωₙ·τ) · (1 + ωₙ·τ)`

`spring_easing(preset, t)` returns `s`. For Tier A (`AnimationElement`) only `smooth` (ζ=1, monotonic 0→1) is used, and the result is clamped to [0,1] to satisfy the debug assert. Tier B evaluates the same response but **integrates state** so velocity survives retargeting (§4.2).

---

## 4. Architecture — `ui/motion/` module

Split the current `motion.rs` into a directory, re-exporting the existing API from `mod.rs` (zero breakage for the ~10 current consumers):

```
ui/motion/
  mod.rs        — re-exports ambient helpers + reduced_motion + new spring API
  ambient.rs    — existing breathe/pulse/blink/spinner/rise + cubic_bezier (moved verbatim)
  presets.rs    — SpringPreset { duration, bounce }, SMOOTH/SNAPPY/BOUNCY, derived k/c/ζ/ωₙ
  spring.rs     — Spring simulator + analytic step response + spring_easing
  system.rs     — MotionSystem global + per-window frame driver
  lerp.rs       — f32 / px / Hsla interpolation helpers
  reduced.rs    — reduced_motion() probe (moved) + MotionPolicy
```

### 4.1 `SpringPreset` / `Spring`

```rust
pub struct SpringPreset { pub duration: f32, pub bounce: f32 }
impl SpringPreset {
    pub fn stiffness(&self) -> f32 { (2.0*PI / self.duration).powi(2) }
    pub fn damping(&self)   -> f32 { (1.0 - self.bounce) * 4.0*PI / self.duration }
    pub fn zeta(&self)      -> f32 { 1.0 - self.bounce }
    pub fn omega_n(&self)   -> f32 { 2.0*PI / self.duration }
}

pub struct Spring {
    pub pos: f32, pub vel: f32, pub target: f32,
    pub preset: SpringPreset,
}
```

### 4.2 Integrator — interruptibility and velocity handoff

Semi-implicit (symplectic) Euler, stable for stiff springs, with a dt clamp so a stalled frame never explodes:

```
dt = min(frame_dt, 1.0/30.0)
a  = -k·(pos - target) - c·vel
vel += a·dt
pos += vel·dt
settled when |pos - target| < ε_pos (1e-3) AND |vel| < ε_vel (1e-2)
```

- **`retarget(new_target)` keeps `pos` and `vel`** — this is the mandatory interruptibility contract: a reversal mid-flight continues from the current position *and* momentum, never snaps or zeroes velocity.
- **`handoff(velocity)`** sets `vel` from a gesture release before settling (§4.6).
- On settle, snap `pos = target`, `vel = 0`, mark inactive.

### 4.3 `MotionSystem` global + frame driver

```rust
pub struct MotionSystem {
    springs: HashMap<SpringKey, Spring>,   // SpringKey = (EntityId, &'static str)
    reduced: bool,                          // cached from reduced_motion() at startup
}
```

- `set_target(cx, key, target, preset)` — creates or retargets a spring; if `reduced`, snaps spatial value to target instantly (opacity handled separately, §4.7); otherwise marks active and **lazily starts the window driver**.
- `value(cx, key) -> f32` — read during render; returns `target` when no spring exists (so first paint is correct, never 0).
- Driver: `window.on_next_frame(move |window, cx| { … })` — computes dt from `Instant`, steps every active spring, calls `cx.notify(owner_entity)` for each owner whose springs moved, and **re-registers itself only while at least one spring is active**. When all settle, the loop stops (no idle frame churn).
- Keys are namespaced per surface (`"modal_scrim"`, `"modal_card"`, `"sidebar_w"`, `"chip_3"`, `"send_press"`, …) so unrelated animations never collide.

### 4.4 Interpolation helpers (`lerp.rs`)

- `lerp(a, b, t)` for f32; `lerp_px(a, b, t) -> Pixels`.
- `lerp_hsla(a, b, t)` — component lerp with **shortest-path hue** (wrap-aware) so color transitions never spin the long way around the wheel. Used for hover/state color springs.

### 4.5 Press feedback without div scale (honest emulation)

gpui 0.2.2 cannot scale a `Div`. The tactile press is therefore:
- `on_mouse_down`: spring `press` 0→1 with `snappy`; render `opacity(1.0 - 0.18·press)` and a `1px` downward offset (`.mt(px(press))`).
- `on_mouse_up` / `on_mouse_down_out`: retarget `press`→0; the spring carries any residual velocity back.
- SVG icons that genuinely need scale may use `Transformation` (`svg.rs:35`); buttons use the opacity+offset emulation. This reads as a compression tap, not a cartoon bounce, and stays within the no-div-transform constraint.

### 4.6 Gesture spec (core support; no drag UI built this track)

No drag surface exists today. The engine still ships the contract so DX5+ (file tree, future reorder) inherits it correctly:
- **1:1 tracking:** during drag, position follows the pointer directly via `on_mouse_move` (no spring lag).
- **Rubber-banding past a boundary:** `offset = (distance · 0.55 · dimension) / (dimension + 0.55 · distance)` (asymptotic resistance, never a hard stop).
- **Velocity handoff:** on release, compute pointer velocity from the last move samples and call `spring.handoff(v)` so momentum carries into the settle.

### 4.7 Reduced motion (`reduced.rs`)

- Source of truth: existing `reduced_motion()` Windows probe, cached once in `MotionSystem` at startup (re-probe on settings change if an override is added later).
- When reduced: **spatial springs snap** to target (no translate/width motion); **opacity-only crossfades** run 0.15–0.2s via Tier A (`AnimationElement`, monotonic `ease_out`/linear — debug-assert safe). Ambient loops already gate themselves and stay gated.
- Every new animated surface must route through `MotionSystem`/`MotionPolicy` so the gate is centralized, not re-implemented per call site.

---

## 5. Surface choreography (audit → treatment)

Every surface below currently swaps state instantly unless noted. Anchors are real.

| Surface | Current behavior | Preset | Variant (opacity + offset unless noted) | Interruption contract | Anchor |
|---|---|---|---|---|---|
| **Modal card** (Approval, Palette, ModelPicker, Account, SessionAction, Settings, SessionSearch) | instant appear/disappear | `smooth` | opacity 0→1 + translateY 12px→0 (`.mt`/`.top`); exit reverses | Esc/close mid-open retargets from current pos+vel; queued modals animate in on promotion | `ui/modals/mod.rs:80` `render_modal_container`; `chat.rs:4156` `render_modal_layer`, `:4203` `modal_overlay`; queue `shell.rs` `push_modal`/`dismiss_modal` |
| **Modal scrim** | instant | `smooth` | opacity 0→1 (`MODAL_SCRIM`) | fades with card; reverses on close | `ui/modals/mod.rs:84` |
| **Sidebar collapse** | conditional render, instant | `smooth` | animate width 296→0 via spring-driven `.w(px)`; content opacity fades | toggle spam retargets width spring (see §8 layout exception) | `shell.rs:233` `toggle_sidebar`; `sidebar.rs:105` `SIDEBAR_WIDTH=296`, `:868` `.w(px(...))`; `chat.rs:4138` gate |
| **Session switch** | feed swap instant | `smooth` | outgoing feed opacity→0, incoming opacity 0→1 + 8px rise | rapid switching retargets crossfade | `chat.rs` `switch_session` |
| **ThinkingDisclosure expand** | `is_expanded` bool instant | `smooth` | revealed content opacity 0→1 + 8px rise; caret rotation via SVG `Transformation` | toggle mid-expand retargets | `ui/reasoning.rs:40,74` |
| **ToolCard input/output expand** | bool instant | `smooth` | same reveal treatment | toggle mid-flight retargets | `ui/tool_card.rs:83,128,132` |
| **Feed message entrance** | instant append | `smooth` | new message opacity 0→1 + 8px rise (container only, **not** per streaming chunk) | n/a (one-shot Tier A entrance) | `ui/feed.rs` add_* paths |
| **Approval card state flip** (DX4) | label/color instant | `snappy` | state color crossfade (`lerp_hsla`) + label opacity | re-decision retargets color | `ui/feed.rs` `ApprovalCardState`, `update_approval_state` |
| **Attachment chip add/remove** (DX4) | instant | `snappy` | add: opacity 0→1 + scale-emulated rise; remove: opacity→0 then drop from list | remove mid-add retargets to exit | `chat.rs` `open_file_picker`/`remove_attachment`, composer chips |
| **Send / attach / icon buttons** | instant color | `snappy` | press feedback (§4.5) + hover color spring | press/release spam retargets | `ui/model_pill.rs:79` `SendButton`; `ui/pill.rs:97` `group_hover`; `chat.rs` `composer_attach` |
| **Dock items** (home) | instant | `snappy` | press feedback + hover tint spring; coming-soon toast entrance `bouncy` | press spam retargets | `ui/home.rs:148` `render_dock_item` |
| **Update banner** (Phase C) | instant appear | `bouncy` | opacity 0→1 + 10px rise, slight overshoot (Tier B) | dismiss mid-entrance retargets to exit | `chat.rs` `render_main_pane` banner |
| **Home logo rise** | cubic-bezier(.2,.7,.2,1) entrance | `smooth` | **replace** `rise_progress` bezier with spring step-response (Tier A) | one-shot | `ui/home.rs:95–110`, `motion.rs:60` |
| **Ambient loops** (glow halo, pulse dots, caret, spinner) | already animated + gated | — | **unchanged** (mockup parity) | — | `motion.rs` ambient helpers; 10 gated sites |

**Choreography rules applied everywhere:** combine opacity with a small translate (8–16px) so nothing pops in flat; exits reverse the entrance; shared-element continuity where a thing expands (disclosure/tool card reveal from its own header, not a disconnected crossfade); `bouncy` only on the banner/toast delight moments.

---

## 6. Phases — one commit each

### AN1 — Motion core (foundation, no broad UI change)
- Build `ui/motion/` (presets, spring sim, `MotionSystem` global + frame driver, lerp helpers, reduced policy); move ambient + probe verbatim; re-export existing API.
- Implement `spring_easing` (analytic) + Tier B integrator with retarget/handoff/settle.
- **First consumer proof:** convert the home logo rise (`home.rs:95`) from cubic-bezier to `smooth` spring; keep `reduced_motion()` gate.
- Tests: §7 math + frame-simulation suites.
- Acceptance: existing `cargo test -p hex-desktop --lib` stays green; new motion tests pass; home rise visually springs (manual QA); no idle frame loop when nothing animates.
- Commit: `feat(hex-desktop): AN1 motion core — spring presets, interruptible MotionSystem, reduced-motion policy`.

### AN2 — Modals & overlays
- Route all 7 modal types through one animated container: scrim + card springs (`smooth`), entrance/exit, interruptible Esc, queue-promotion entrance.
- Press feedback on modal action buttons (§4.5).
- Acceptance: open/close/Esc-spam retargets smoothly (no snap/restart); scrim and card stay in sync; reduced motion → opacity-only fast fade; focus tracking (`modal_overlay`) unchanged.
- Commit: `feat(hex-desktop): AN2 spring modals — interruptible entrance/exit, scrim, press feedback`.

### AN3 — Feed & composer micro-interactions
- Message entrance, approval-card state crossfade, attachment chip add/remove, thinking/tool-card expand reveals, send/attach/icon press + hover springs.
- Acceptance: rapid toggle/press/chip-remove spam interrupts cleanly; streaming text is **not** animated per chunk; expands reveal from their own header; reduced motion snaps spatial, fades opacity.
- Commit: `feat(hex-desktop): AN3 feed & composer springs — reveals, chips, press feedback, state crossfades`.

### AN4 — Shell navigation & layout
- Sidebar collapse width spring, session-switch crossfade, dock item press/hover + coming-soon toast (`bouncy`), update banner entrance (`bouncy`).
- Acceptance: sidebar toggle spam retargets width without layout jump; session crossfade smooth; banner overshoot subtle; reduced motion respected; driver stops when idle.
- Commit: `feat(hex-desktop): AN4 shell springs — sidebar, session crossfade, dock & banner motion`.

> **AN-U (optional, deferred):** a gated gpui upgrade spike, defined in §10.4. It is **not** part of the AN1→AN4 critical path and runs only if a stable gpui release ships element transforms/`Interpolate`, or the user explicitly accepts git-main risk. The spring engine (§4) is gpui-version-agnostic, so AN1–AN4 are unaffected either way.

---

## 7. Verification & QA

**Math property tests (`motion/spring.rs`, `presets.rs`):**
- `ζ = 1 - bounce` and `k/c` match the conversion formulas for all three presets.
- Critically damped (`smooth`): monotonic 0→1, **zero overshoot** (max ≤ 1 + 1e-6), settles within `duration + ε`.
- `snappy`/`bouncy`: bounded overshoot consistent with ζ (0.85 / 0.7), settle within duration + ε.
- `spring_easing` endpoints: s(0)=0, s(1)≈1; smooth output stays in [0,1] (Tier A assert safety).
- **Retarget continuity:** mid-flight `retarget` preserves `pos` and `vel` exactly (no snap, no zeroing).
- **Velocity handoff:** `handoff(v)` sets vel; subsequent settle carries momentum (position passes target before returning for underdamped).
- **dt-clamp stability:** a 500ms stalled frame does not diverge (pos stays bounded).
- Settle snaps pos=target, vel=0, marks inactive.

**Frame-simulation tests (`motion/system.rs`):**
- Drive `MotionSystem::tick` with synthetic dt; assert driver active-flag drops to false once all springs settle (no idle churn).
- Reduced policy: spatial spring snaps to target; opacity path still produces a 0.15–0.2s monotonic fade.
- Key namespacing: two surfaces animating simultaneously do not collide.

**Per-phase suites:** `cargo test -p hex-desktop --lib` + integration binaries; `cargo check -p hex-desktop` clean (no new warnings). Existing motion ambient tests must remain green after the module split.

**Manual QA checklist (user runs — GPUI is not headless-click-testable):**
- Spam-open/close a modal and hit Esc mid-entrance → smooth reversal, no snap.
- Rapid sidebar toggle → width retargets, no jump.
- Add then immediately remove an attachment chip → exit retargets from current state.
- Toggle thinking/tool-card expand repeatedly → reveal reverses cleanly.
- Press/hold/release send + dock buttons → compression tap, spring back.
- Windows "Show animations" off → spatial motion gone, only fast opacity fades remain.

**Honesty:** verification is compile + unit + math + frame-simulation. Visual smoothness is confirmed by the user on the running app; this plan does not claim headless UI click-testing.

---

## 8. Performance & accessibility rules

- **Animate opacity + positional offsets only** per frame (the gpui-feasible subset of "GPU-only transform+opacity"). No animating `width/height/top/left/margin` as a default.
- **One documented layout exception:** the sidebar collapse animates container width (§5). Mitigation: keep the spring on a single small container, fade content opacity in parallel so reflow is cheap; if profiling shows jank, fall back to translate+clip (slide the sidebar out under a fixed-width mask) instead of width interpolation. Flagged, not hidden.
- Driver **stops requesting frames when all springs settle**; dt clamped to 1/30s; springs keyed per surface so unrelated animations never force extra repaints.
- **Never animate per streaming chunk** — message containers animate on entrance only; token/thought deltas append instantly (animating them would lag the stream).
- **Reduced motion is mandatory and centralized** via `MotionPolicy`; every new surface routes through it. Ambient loops keep their existing gates.
- No `ease-in-out` cubic-bezier for interactive state — springs only. Bezier remains solely for the unchanged ambient mockup loops.

## 9. Out of scope

- TUI (`crates/hex-tui`), Python harness, `backend/` — no spring surface.
- Audio/sound — does not belong to a productivity cockpit.
- Drag-and-drop UI — none exists today; the gesture contract (§4.6) ships in core for future DX5+ use, but no drag surface is built this track.
- gpui upgrade — assessed in §10; **not on the critical path**. The opacity+offset emulation (§4.5) satisfies the tactile intent within 0.2.2; a gated, optional upgrade spike (AN-U) is defined for if/when a stable release ships div transforms.
- In-app "Reduce motion" settings override — optional later; the OS probe is the source of truth for this track.

---

## 10. GPUI version & upgrade assessment (verified)

The user asked whether upgrading gpui would help and, if so, to add it to the plan. This section records the evidence and the decision so the choice is explicit rather than assumed.

### 10.1 Facts (checked against crates.io + the vendored 0.2.2 source)

- **0.2.2 is the newest published gpui.** crates.io version history is `0.2.0`, `0.2.1`, `0.2.2` (published 2025-10-22) plus yanked `*-test*` builds; there is **no 0.3.x or newer stable release**. `cargo info gpui` reports `version: 0.2.2`. So a normal `cargo update`/version bump has **nowhere to go** — the app is already on the latest stable.
- **0.2.2 has no div/subtree transform.** The only public paint API that accepts a `TransformationMatrix` is `Window::paint_svg(bounds, path, transformation, color, cx)` (`window.rs:3065`). `paint_quad`/`paint_path`/`paint_glyph` carry no transform (glyph hardcodes `TransformationMatrix::unit()` at `window.rs:2996`), and there is no public "apply transform to this child subtree" scope. True scale/translate of arbitrary `Div` content is therefore **not achievable on 0.2.2** without manually transforming every child paint call — fragile and not worth it. SVG icons *can* scale via `Svg::with_transformation` (`svg.rs:35`), which §4.5 already uses.
- **Newer animation ergonomics live only in zed's git main** (`github.com/zed-industries/zed`, `crates/gpui`), unpublished: e.g. an `Interpolate` trait and ongoing animation/transform work. Reaching them means depending on git, not on a release.

### 10.2 What a git-main upgrade would buy vs. cost

**Potential upside**
- An `Interpolate` trait (cleaner color/px/scalar interpolation than the manual `lerp.rs`).
- Possibly real element transforms (scale/translate/rotate) → literal Apple-style press scale and shared-element bounds animation instead of opacity+offset emulation.

**Concrete risks (why it is not free)**
- **Monorepo coupling:** git-main gpui depends on sibling zed crates (`collections`, `util`, `refineable`, `sum_tree`, `http_client`, `gpui_macros`, …) via workspace path deps. Standalone git consumption frequently fails dependency resolution or drags in a large, churning tree; the published 0.2.2 crate exists precisely to decouple those.
- **API churn across the whole app:** the desktop binds ~4,500 lines of `chat.rs` plus `shell`, seven modals, `feed`, `message`, `sidebar`, `composer`, `home` against 0.2.2. zed main has restructured `Context`/`Window`/`App`/`Entity`/action APIs; a bump could surface **hundreds of compile errors** unrelated to animation.
- **Toolchain + platform sensitivity:** gpui is edition 2024 with a Direct3D/HLSL Windows backend; git main may demand a different toolchain or introduce Windows render regressions. This app is Windows-first.
- **Moving target:** pinning a git commit freezes a fork-like dependency that diverges from future published releases and must be re-migrated later.

### 10.3 Decision

**Do not upgrade as part of this track.** The motion spec is fully satisfiable on 0.2.2: Tier B springs give interruptibility, velocity handoff, and bounce; opacity+offset choreography (§4.5, §5) delivers the tactile and spatial intent; SVG icons get true scale where it matters. The marginal visual gain from git-main transforms (literal scale vs. an opacity-compression tap) does **not** justify destabilizing a working app or blocking AN1–AN4 behind a high-risk migration. **AN1–AN4 proceed on 0.2.2 as planned.**

### 10.4 AN-U — optional, deferred upgrade spike (gated; not on the critical path)

Revisit **only** when one of these triggers fires:
1. A **stable** gpui release (> 0.2.2) ships element transforms and/or `Interpolate`; or
2. The user explicitly accepts the git-main risk for true GPU transforms.

If triggered, run a **time-boxed spike in an isolated git worktree** (never on the working tree):
- Pin a specific gpui commit/release; `cargo check -p hex-desktop` and **count + categorize** the compile breakage (animation-related vs. unrelated API churn).
- Verify Windows render parity (fonts, SVG tints, shadows, modal scrim) against the 0.2.2 baseline.
- Prototype a single `Scale`/`Transform` wrapper on one surface (e.g. send-button press) to confirm the transform API actually works end-to-end.
- **Adopt only if** the migration is tractable (bounded, mostly-mechanical fixes), all existing suites stay green, and Windows rendering is unchanged. Otherwise **abort the spike** and keep the 0.2.2 emulation — no sunk-cost adoption.
- If adopted, it lands as its own commit (`chore(hex-desktop): bump gpui to <ver> + migrate`) **before** revisiting §4.5/§5 to replace emulation with real transforms; AN1–AN4 spring logic is unaffected (the `MotionSystem` driver is gpui-version-agnostic).

**Lower-risk partial win available now (no upgrade):** use `Svg::with_transformation` for icon-centric micro-interactions that genuinely benefit from scale (e.g. the thinking-disclosure caret rotation, dock icon press). Already noted in §4.5; this is the only true-transform surface 0.2.2 exposes and it costs nothing.
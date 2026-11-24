//! Pulsating glow card + mockup-styled composer state for HEX Desktop.
//!
//! Mirrors the mockup's `.glow` composer: a rounded card ringed by an
//! electric-blue halo whose opacity breathes on a ~5s loop
//! (`@keyframes breathe` in DesktopSession.dc.html). The halo is the mockup's
//! `.glow::before` (inset -2px, radius 26, 120° accent gradient, blur 16px)
//! approximated with GPUI primitives: a gradient div plus two stacked
//! blur-16 box shadows, with a short one-shot entrance fade. The halo rests at
//! a static opacity afterward so an idle composer does not request frames.

use std::sync::{Arc, Mutex};

use gpui::*;

use crate::theme::HermesTheme;
use crate::ui::motion;
use crate::ui::token_gauge::TokenGaugeWidget;

/// One-shot glow entrance duration in milliseconds.
pub const GLOW_ENTRANCE_DURATION_MS: u64 = 600;

/// Minimum glow alpha (exhale) from the mockup's breathe keyframes.
pub const GLOW_MIN_ALPHA: f32 = 0.32;

/// Maximum glow alpha (inhale) from the mockup's breathe keyframes.
pub const GLOW_MAX_ALPHA: f32 = 0.70;

/// Halo overhang past the card edge (mockup `inset:-2px`).
pub const HALO_INSET: f32 = 2.0;

/// Halo corner radius (mockup `border-radius:26px`).
pub const HALO_RADIUS: f32 = 26.0;

/// Halo blur emulated by stacked shadows (mockup `filter:blur(16px)`).
pub const HALO_BLUR: f32 = 16.0;

/// Halo gradient angle in CSS convention (0° = up, clockwise).
pub const GLOW_ANGLE_DEG: f32 = 120.0;

/// Gradient start alpha (mockup `rgba(122,167,255,.6)`).
pub const GLOW_START_ALPHA: f32 = 0.6;

/// Gradient end alpha (mockup's far stop `rgba(122,167,255,.4)`; the middle
/// `.08` dip is approximated away — GPUI gradients have exactly two stops).
pub const GLOW_END_ALPHA: f32 = 0.4;

/// Static halo opacity used under reduced motion (mockup pre-animation
/// `opacity:.5`).
pub const HALO_STATIC_OPACITY: f32 = 0.5;

/// Composer card corner radius (mockup `border-radius:24px`).
pub const CARD_RADIUS: f32 = 24.0;

/// Composer card padding (mockup `padding:20px 22px 14px 22px`).
pub const CARD_PADDING_TOP: f32 = 20.0;
pub const CARD_PADDING_X: f32 = 22.0;
pub const CARD_PADDING_BOTTOM: f32 = 14.0;

/// Gap between the composer input row and its toolbar (mockup `gap:30px`).
pub const COMPOSER_COLUMN_GAP: f32 = 30.0;

/// Context bar width (session screen, mockup `width:560px`).
pub const CONTEXT_BAR_WIDTH: f32 = 560.0;
/// Context track width / height / radius (mockup 88×4, radius 2).
pub const CONTEXT_TRACK_WIDTH: f32 = 88.0;
pub const CONTEXT_TRACK_HEIGHT: f32 = 4.0;
pub const CONTEXT_TRACK_RADIUS: f32 = 2.0;
/// Gap between track and label, and the row's left padding.
pub const CONTEXT_GAP: f32 = 10.0;
pub const CONTEXT_PADDING_LEFT: f32 = 6.0;
/// Context label size (13px `#9a9a9a`).
pub const CONTEXT_LABEL_SIZE: f32 = 13.0;

/// Session-screen composer placeholder (the Start screen uses the
/// `PromptComposer` default).
pub const SESSION_PLACEHOLDER: &str = "Ask a follow-up...";

/// Maps a linear animation phase to the mockup's ease-in-out breathing curve.
pub fn breathe_easing(t: f32) -> f32 {
    let x = t.clamp(0.0, 1.0);
    // Triangle wave 0..2 so the glow inhales then exhales within one loop.
    let tri = if x < 0.5 { x * 2.0 } else { (1.0 - x) * 2.0 };
    // Ease-in-out smoothing on each leg.
    let s = tri * tri * (3.0 - 2.0 * tri);
    s.clamp(0.0, 1.0)
}

/// Interpolates the glow alpha for an animation phase.
pub fn glow_alpha(phase: f32) -> f32 {
    GLOW_MIN_ALPHA + (GLOW_MAX_ALPHA - GLOW_MIN_ALPHA) * breathe_easing(phase)
}

/// Opacity for the finite glow entrance, ending at the resting halo value.
pub fn glow_entrance_opacity(phase: f32) -> f32 {
    GLOW_MIN_ALPHA + (HALO_STATIC_OPACITY - GLOW_MIN_ALPHA) * phase.clamp(0.0, 1.0)
}

/// The two gradient stops backing [`glow_halo_gradient`], exposed for tests
/// (GPUI `Background` fields are crate-private).
pub fn glow_gradient_stops(accent: Rgba) -> (Hsla, Hsla) {
    (
        rgba_to_hsla(accent, GLOW_START_ALPHA),
        rgba_to_hsla(accent, GLOW_END_ALPHA),
    )
}

/// The halo's 120° accent wash.
pub fn glow_halo_gradient(accent: Rgba) -> Background {
    let (start, end) = glow_gradient_stops(accent);
    linear_gradient(
        GLOW_ANGLE_DEG,
        linear_color_stop(start, 0.0),
        linear_color_stop(end, 1.0),
    )
}

/// Two stacked blur-16 shadows extending the gradient rim into a diffuse
/// skirt, standing in for the mockup's blurred `::before`.
pub fn glow_shadow_stack(accent: Rgba) -> Vec<BoxShadow> {
    vec![
        BoxShadow {
            color: rgba_to_hsla(accent, 0.45),
            offset: point(px(0.0), px(0.0)),
            blur_radius: px(HALO_BLUR),
            spread_radius: px(0.0),
        },
        BoxShadow {
            color: rgba_to_hsla(accent, 0.18),
            offset: point(px(0.0), px(0.0)),
            blur_radius: px(HALO_BLUR),
            spread_radius: px(6.0),
        },
    ]
}

/// Painted geometry of one glow card, recorded for the one-card smoke tests.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct GlowCardPaint {
    /// Bounds of the composer card itself.
    pub card: Bounds<Pixels>,
    /// Bounds of the halo behind it (card ± 2px).
    pub halo: Bounds<Pixels>,
}

/// Probe installed by tests; production never sets this global, so glow_card's
/// paint listener is a no-op read of a missing global.
#[derive(Default)]
pub struct GlowCardProbe(pub Arc<Mutex<Vec<GlowCardPaint>>>);

impl Global for GlowCardProbe {}

/// Wraps the composer in the mockup's pulsating glow card: the single
/// `#171717` card with the accent border, halo behind it, and the mockup's
/// card padding (20px 22px 14px 22px). `inner` is the composer column
/// (input row + toolbar); the column supplies its own 30px gap.
pub fn glow_card(inner: Div, theme: &HermesTheme) -> Div {
    let accent = theme.accent_blue();

    let card = div()
        .relative()
        .w_full()
        .rounded(px(CARD_RADIUS))
        .bg(theme.mockup_surface)
        .border_1()
        .border_color(theme.mockup_accent_border)
        .pt(px(CARD_PADDING_TOP))
        .px(px(CARD_PADDING_X))
        .pb(px(CARD_PADDING_BOTTOM))
        .child(inner);

    let halo = div()
        .absolute()
        .top(px(-HALO_INSET))
        .bottom(px(-HALO_INSET))
        .left(px(-HALO_INSET))
        .right(px(-HALO_INSET))
        .rounded(px(HALO_RADIUS))
        .bg(glow_halo_gradient(accent))
        .shadow(glow_shadow_stack(accent));

    let halo = if motion::reduced_motion() {
        halo.opacity(HALO_STATIC_OPACITY).into_any_element()
    } else {
        halo.with_animation(
            "glow-entrance",
            Animation::new(std::time::Duration::from_millis(GLOW_ENTRANCE_DURATION_MS))
                .with_easing(|phase| phase * phase * (3.0 - 2.0 * phase)),
            |el, phase| el.opacity(glow_entrance_opacity(phase)),
        )
        .into_any_element()
    };

    div()
        .relative()
        .w_full()
        .on_children_prepainted(|children, _window, cx| {
            if let Some(probe) = cx.try_global::<GlowCardProbe>() {
                if let (Some(&halo), Some(&card)) = (children.first(), children.get(1)) {
                    probe.0.lock().unwrap().push(GlowCardPaint { card, halo });
                }
            }
        })
        .child(halo)
        .child(card)
}

/// Formats the mockup context-bar label: "Context N% full" with an integer
/// percent.
pub fn context_label(fraction: f64) -> String {
    format!("Context {}% full", (fraction * 100.0).round() as i64)
}

/// Session-only context bar: 88×4 track (radius 2, `#2a2a2a`) with a `#cfcfcf`
/// fill and the 13px `#9a9a9a` "Context N% full" label, 560px wide with 6px
/// left padding.
pub fn context_bar(fraction: f64, theme: &HermesTheme) -> Div {
    let fill = fraction.clamp(0.0, 1.0) as f32;
    div()
        .w(px(CONTEXT_BAR_WIDTH))
        .max_w_full()
        .flex()
        .flex_row()
        .items_center()
        .gap(px(CONTEXT_GAP))
        .pl(px(CONTEXT_PADDING_LEFT))
        .text_size(px(CONTEXT_LABEL_SIZE))
        .text_color(theme.mockup_text_muted)
        .child(
            div()
                .w(px(CONTEXT_TRACK_WIDTH))
                .h(px(CONTEXT_TRACK_HEIGHT))
                .rounded(px(CONTEXT_TRACK_RADIUS))
                .bg(theme.mockup_track)
                .child(
                    div()
                        .h_full()
                        .w(relative(fill))
                        .rounded(px(CONTEXT_TRACK_RADIUS))
                        .bg(rgb(0xcfcfcf)),
                ),
        )
        .child(context_label(fraction))
}

/// Converts an Rgba to an Hsla with the given alpha (for borders/shadows).
fn rgba_to_hsla(c: Rgba, alpha: f32) -> Hsla {
    let hsla: Hsla = c.into();
    hsla.alpha(alpha)
}

// Directly mirrors Hermes Desktop composer (`app/chat/composer/`):
// - Key shortcuts: Enter to submit, Shift+Enter for newline, Cmd+Enter forced submit
// - Integrated `TokenGaugeWidget` tracking context usage
// - Integrated `ModelSelectorPill`
// (Rendering now lives in `ChatView::render_composer`; this struct holds the
// shared composer state.)

use crate::ui::model_pill::ModelSelectorPill;

/// Prompt composer state shared by the Home and Session screens.
#[derive(Debug, Clone, PartialEq)]
pub struct PromptComposer {
    /// Currently buffered prompt input text.
    pub input_text: String,
    /// Placeholder display text when buffer is empty.
    pub placeholder: String,
    /// Context token gauge tracking session capacity.
    pub token_gauge: TokenGaugeWidget,
    /// Model selector pill button.
    pub model_pill: ModelSelectorPill,
    /// Whether the agent is currently streaming generation.
    pub is_streaming: bool,
}

impl Default for PromptComposer {
    fn default() -> Self {
        Self::new()
    }
}

impl PromptComposer {
    pub fn new() -> Self {
        Self {
            input_text: String::new(),
            placeholder: "Describe what to build or fix".to_string(),
            token_gauge: TokenGaugeWidget::default(),
            model_pill: ModelSelectorPill::default(),
            is_streaming: false,
        }
    }

    pub fn with_tokens(mut self, current: usize, max: usize) -> Self {
        self.token_gauge.set_tokens(current, max);
        self
    }

    pub fn set_text(&mut self, text: impl Into<String>) {
        self.input_text = text.into();
    }

    pub fn append_text(&mut self, chunk: &str) {
        self.input_text.push_str(chunk);
    }

    pub fn clear(&mut self) {
        self.input_text.clear();
    }

    pub fn set_streaming(&mut self, streaming: bool) {
        self.is_streaming = streaming;
    }

    pub fn can_send(&self) -> bool {
        !self.input_text.trim().is_empty() && !self.is_streaming
    }
}

#[cfg(test)]
mod glow_tests {
    use super::{
        breathe_easing, context_label, glow_alpha, glow_entrance_opacity, glow_gradient_stops,
        glow_shadow_stack, GLOW_END_ALPHA, GLOW_ENTRANCE_DURATION_MS, GLOW_MAX_ALPHA,
        GLOW_MIN_ALPHA, GLOW_START_ALPHA, HALO_BLUR, HALO_STATIC_OPACITY,
    };

    #[test]
    fn test_glow_alpha_pulsates_between_bounds() {
        let a0 = glow_alpha(0.0);
        let a_peak = glow_alpha(0.5);
        let a_end = glow_alpha(1.0);
        assert!((a0 - GLOW_MIN_ALPHA).abs() < 1e-4);
        assert!((a_peak - GLOW_MAX_ALPHA).abs() < 1e-4);
        assert!((a_end - GLOW_MIN_ALPHA).abs() < 1e-4);
        assert!(a_peak > a0);
    }

    #[test]
    fn test_breathe_easing_is_smooth_and_bounded() {
        for i in 0..=100 {
            let t = i as f32 / 100.0;
            let e = breathe_easing(t);
            assert!((0.0..=1.0).contains(&e));
        }
        // Monotonic on the inhale leg.
        assert!(breathe_easing(0.1) < breathe_easing(0.2));
    }

    #[test]
    fn test_glow_entrance_is_finite_and_monotonic() {
        assert!(GLOW_ENTRANCE_DURATION_MS <= 1_000);
        assert!((glow_entrance_opacity(0.0) - GLOW_MIN_ALPHA).abs() < 1e-4);
        assert!((glow_entrance_opacity(1.0) - HALO_STATIC_OPACITY).abs() < 1e-4);
        for i in 0..=100 {
            let phase = i as f32 / 100.0;
            let next = (i + 1) as f32 / 100.0;
            assert!(glow_entrance_opacity(phase) <= glow_entrance_opacity(next));
        }
    }

    #[test]
    fn test_glow_gradient_stops_match_mockup() {
        let accent = gpui::rgb(0x7aa7ff);
        let (start, end) = glow_gradient_stops(accent);
        assert!((start.a - GLOW_START_ALPHA).abs() < 1e-4);
        assert!((end.a - GLOW_END_ALPHA).abs() < 1e-4);
        let start_rgb: gpui::Rgba = gpui::Rgba::from(start);
        assert_eq!((start_rgb.r * 255.0).round() as u8, 0x7a);
    }

    #[test]
    fn test_glow_shadow_stack_is_stacked_blur_16() {
        let accent = gpui::rgb(0x7aa7ff);
        let shadows = glow_shadow_stack(accent);
        assert_eq!(shadows.len(), 2);
        for shadow in &shadows {
            assert_eq!(shadow.blur_radius, gpui::px(HALO_BLUR));
            assert_eq!(shadow.offset.x, gpui::px(0.0));
            assert_eq!(shadow.offset.y, gpui::px(0.0));
        }
        assert_eq!(shadows[0].spread_radius, gpui::px(0.0));
        assert_eq!(shadows[1].spread_radius, gpui::px(6.0));
        assert!(shadows[0].color.a > shadows[1].color.a);
    }

    #[test]
    fn test_context_label_formats_integer_percent() {
        assert_eq!(context_label(0.38), "Context 38% full");
        assert_eq!(context_label(0.0), "Context 0% full");
        assert_eq!(context_label(1.0), "Context 100% full");
        assert_eq!(context_label(0.376), "Context 38% full");
    }
}

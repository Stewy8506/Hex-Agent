//! Pulsating glow card + mockup-styled composer wrapper for HEX Desktop.
//!
//! Mirrors the mockup's `.glow` composer: a rounded card ringed by an
//! electric-blue halo whose opacity breathes on a ~5s loop
//! (`@keyframes breathe` in DesktopSession.dc.html). Implemented with GPUI's
//! `with_animation` + `Animation::repeat()`, pulsating opacity between 0.32
//! and 0.7.

use gpui::*;

use crate::theme::HermesTheme;

/// Breathing glow period in seconds, matching the mockup's 5s `breathe` loop.
pub const GLOW_PERIOD_SECS: f64 = 5.0;

/// Minimum glow alpha (exhale) from the mockup's breathe keyframes.
pub const GLOW_MIN_ALPHA: f32 = 0.32;

/// Maximum glow alpha (inhale) from the mockup's breathe keyframes.
pub const GLOW_MAX_ALPHA: f32 = 0.70;

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

/// Wraps the composer in the mockup's pulsating glow card.
///
/// The glow is three stacked accent-tinted rings (GPUI has no CSS blur), each
/// rendered on an animated layer whose opacity breathes with the phase.
pub fn glow_card(inner: Div, theme: &HermesTheme, id: impl Into<ElementId>) -> Div {
    let accent = theme.accent_blue();

    // Static halo ring (subtle, always present so the glow never disappears).
    let static_ring = blend_over(accent, theme.mockup_surface, 0.18);

    let card = div()
        .id(id.into())
        .relative()
        .w_full()
        .rounded(px(24.0))
        .bg(theme.mockup_surface)
        .border_1()
        .border_color(theme.mockup_border_strong)
        .shadow(vec![BoxShadow {
            color: hsla_from_rgba(accent, 0.12),
            offset: point(px(0.0), px(0.0)),
            blur_radius: px(28.0),
            spread_radius: px(2.0),
        }])
        .child(inner);

    // Animated outer halo: opacity pulses with the breathe curve.
    let halo = div()
        .absolute()
        .top(px(-2.0))
        .bottom(px(-2.0))
        .left(px(-2.0))
        .right(px(-2.0))
        .rounded(px(26.0))
        .border_2()
        .border_color(hsla_from_rgba(accent, 0.6));

    let halo = halo
        .with_animation(
            "glow-breathe",
            Animation::new(std::time::Duration::from_secs_f64(GLOW_PERIOD_SECS)).repeat(),
            |el, phase| el.opacity(glow_alpha(phase)),
        )
        .into_any_element();

    div()
        .relative()
        .w_full()
        .child(card)
        .child(halo)
        .child(
            // Inner static tint ring keeps a visible glow even at exhale.
            div()
                .absolute()
                .inset_0()
                .rounded(px(24.0))
                .border_1()
                .border_color(static_ring),
        )
}

/// Blends `fg` over `bg` at `alpha`, returning an opaque Rgba.
fn blend_over(fg: Rgba, bg: Rgba, alpha: f32) -> Rgba {
    let base = Rgba { r: bg.r, g: bg.g, b: bg.b, a: 1.0 };
    let tint = Rgba { r: fg.r, g: fg.g, b: fg.b, a: alpha };
    base.blend(tint)
}

/// Converts an Rgba to an Hsla with the given alpha (for borders/shadows).
fn hsla_from_rgba(c: Rgba, alpha: f32) -> Hsla {
    let hsla: Hsla = c.into();
    hsla.alpha(alpha)
}

#[cfg(test)]
mod glow_tests {
    use super::{breathe_easing, glow_alpha, GLOW_MAX_ALPHA, GLOW_MIN_ALPHA};

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
}
// Directly mirrors Hermes Desktop composer (`app/chat/composer/`):
// - Auto-growing multiline input area (from 1 line ~40px up to 8 lines ~200px)
// - Rotating or default placeholder: "Ask HEX anything, or type / for slash commands..."
// - Key shortcuts: Enter to submit, Shift+Enter for newline, Cmd+Enter forced submit
// - Inline clear button (`×`) when text is present
// - Integrated `TokenGaugeWidget` showing ASCII block progress bar
// - Integrated `ModelSelectorPill` and circular `SendButton` (`↑` / `⏹`)

use crate::ui::model_pill::{ModelSelectorPill, SendButton};
use crate::ui::token_gauge::TokenGaugeWidget;

/// Prompt composer component.
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
            placeholder: "Ask HEX anything, or type / for slash commands...".to_string(),
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

    /// Number of lines currently in the input text.
    pub fn line_count(&self) -> usize {
        if self.input_text.is_empty() {
            1
        } else {
            self.input_text.split('\n').count().max(1)
        }
    }

    /// Calculates container height dynamically: 1 line = 40px, up to 8 lines = 200px.
    pub fn calculate_input_height_px(&self) -> Pixels {
        let lines = self.line_count().min(8);
        let base_h = 36.0;
        let line_h = 20.0;
        px(base_h + ((lines.saturating_sub(1)) as f32 * line_h))
    }

    /// Handles an Enter key event.
    ///
    /// If Shift is held: appends a newline.
    /// If Shift is not held and input is non-empty: submits prompt and returns `Some(text)`.
    pub fn handle_enter_key(&mut self, shift_held: bool) -> Option<String> {
        if shift_held {
            self.input_text.push('\n');
            None
        } else if self.can_send() {
            let submitted = self.input_text.trim().to_string();
            self.input_text.clear();
            Some(submitted)
        } else {
            None
        }
    }

    /// Handles a Cmd+Enter key event (forced submit).
    pub fn handle_cmd_enter(&mut self) -> Option<String> {
        if !self.input_text.trim().is_empty() {
            let submitted = self.input_text.trim().to_string();
            self.input_text.clear();
            Some(submitted)
        } else {
            None
        }
    }

    /// Renders the complete composer view container.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        let input_h = self.calculate_input_height_px();
        let has_text = !self.input_text.is_empty();
        let send_btn = SendButton::new(self.is_streaming, self.can_send());

        div()
            .w_full()
            .flex()
            .flex_col()
            .p_3()
            .rounded_xl()
            .bg(theme.card_bg())
            .border_1()
            .border_color(theme.border_subtle())
            // Top Section: Text Area Row
            .child(
                div()
                    .w_full()
                    .min_h(input_h)
                    .max_h(px(200.0))
                    .flex()
                    .flex_row()
                    .items_start()
                    .justify_between()
                    .gap_2()
                    .child(
                        div()
                            .flex_1()
                            .text_sm()
                            .text_color(if has_text {
                                theme.text_primary()
                            } else {
                                theme.text_extra_dim()
                            })
                            .child(if has_text {
                                self.input_text.clone()
                            } else {
                                self.placeholder.clone()
                            }),
                    )
                    .child(
                        // Clear button (visible when text is present)
                        if has_text {
                            div()
                                .cursor_pointer()
                                .px_1()
                                .text_xs()
                                .text_color(theme.text_dim())
                                .hover(|s| s.text_color(theme.text_primary()))
                                .child("×")
                        } else {
                            div()
                        },
                    ),
            )
            // Bottom Toolbar Row: Model Pill (Left) and Token Gauge + Send Button (Right)
            .child(
                div()
                    .w_full()
                    .flex()
                    .flex_row()
                    .items_center()
                    .justify_between()
                    .pt_2()
                    .border_t_1()
                    .border_color(theme.border_subtle())
                    // Left Side: Model Selector Pill
                    .child(self.model_pill.render(theme))
                    // Right Side: Token Gauge & Send Button
                    .child(
                        div()
                            .flex()
                            .flex_row()
                            .items_center()
                            .gap_3()
                            .child(self.token_gauge.render(theme))
                            .child(send_btn.render(theme)),
                    ),
            )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_prompt_composer_line_count_and_height() {
        let mut composer = PromptComposer::new();
        assert_eq!(composer.line_count(), 1);
        assert_eq!(composer.calculate_input_height_px(), px(36.0));

        composer.set_text("Line 1\nLine 2\nLine 3");
        assert_eq!(composer.line_count(), 3);
        assert_eq!(composer.calculate_input_height_px(), px(36.0 + 40.0));
    }

    #[test]
    fn test_prompt_composer_key_handling() {
        let mut composer = PromptComposer::new();

        // Shift+Enter inserts newline
        assert_eq!(composer.handle_enter_key(true), None);
        assert_eq!(composer.input_text, "\n");

        composer.clear();
        composer.set_text("Build a compiler");

        // Normal Enter submits prompt
        let submitted = composer.handle_enter_key(false);
        assert_eq!(submitted.as_deref(), Some("Build a compiler"));
        assert!(composer.input_text.is_empty());
    }
}

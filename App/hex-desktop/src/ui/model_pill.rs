//! Model selector pill button and send button controls for HEX Desktop.
//!
//! Mirrors Hermes Desktop composer model pill (`app/chat/composer/model-pill.tsx`):
//! - Compact pill button embedded in composer bottom bar (mockup `.pill`)
//! - Model name label and chevron-down SVG
//! - Click toggle for model selection modal
//! - Dedicated Send/Stop action button
//! (`provider_dot_color` stays available for the model-picker rows.)

use gpui::*;

use crate::theme::HermesTheme;
use crate::ui::icon_svg;

/// Provider brand dot color mapping.
pub fn provider_dot_color(provider_name: &str, theme: &HermesTheme) -> Rgba {
    match provider_name.to_lowercase().as_str() {
        "anthropic" | "claude" => rgb(0xd97706), // Amber-600
        "openai" | "gpt" => rgb(0x10b981),       // Emerald-500
        "google" | "gemini" => rgb(0x3b82f6),    // Blue-500
        _ => theme.accent_blue(),                // Electric Blue #7aa7ff
    }
}

/// Model selector pill embedded in the composer toolbar.
#[derive(Debug, Clone, PartialEq)]
pub struct ModelSelectorPill {
    pub provider_name: String,
    pub model_name: String,
    pub is_picker_open: bool,
}

impl Default for ModelSelectorPill {
    fn default() -> Self {
        // Neutral until the runtime reports the real model; never fabricate
        // a model identity before session adoption.
        Self::new("HEX", "Select model")
    }
}

impl ModelSelectorPill {
    pub fn new(provider_name: impl Into<String>, model_name: impl Into<String>) -> Self {
        Self {
            provider_name: provider_name.into(),
            model_name: model_name.into(),
            is_picker_open: false,
        }
    }

    pub fn set_model(&mut self, provider_name: impl Into<String>, model_name: impl Into<String>) {
        self.provider_name = provider_name.into();
        self.model_name = model_name.into();
    }

    pub fn toggle_picker(&mut self) {
        self.is_picker_open = !self.is_picker_open;
    }

    pub fn dot_color(&self, theme: &HermesTheme) -> Rgba {
        provider_dot_color(&self.provider_name, theme)
    }

    pub fn display_label(&self) -> &str {
        &self.model_name
    }

    /// Renders the compact model pill button (mockup `.pill`: bg `#1f1f1f`,
    /// radius 99, 5×14 padding, 14/20 `#cfcfcf`, chevron 12px `#858585`,
    /// hover `#272727`).
    pub fn render(&self, theme: &HermesTheme) -> Div {
        crate::ui::pill::mockup_pill(theme)
            .child(self.model_name.clone())
            .child(crate::ui::pill::mockup_pill_chevron())
    }
}

/// Send / Stop action button for the composer toolbar.
#[derive(Debug, Clone, PartialEq)]
pub struct SendButton {
    pub is_streaming: bool,
    pub can_send: bool,
}

impl SendButton {
    pub fn new(is_streaming: bool, can_send: bool) -> Self {
        Self {
            is_streaming,
            can_send,
        }
    }

    pub fn icon(&self) -> &'static str {
        if self.is_streaming {
            "⏹"
        } else {
            "↑"
        }
    }

    /// Renders the circular send/stop button (mockup: 36px circle, #2a2a2a bg).
    pub fn render(&self, theme: &HermesTheme) -> Div {
        let bg_color: Hsla = if self.is_streaming {
            theme.rose_fg().into()
        } else {
            rgb(0x2a2a2a).into()
        };
        self.render_with_background(bg_color)
    }

    pub fn render_with_background(&self, bg_color: Hsla) -> Div {
        div()
            .w(px(36.0))
            .h(px(36.0))
            .flex()
            .items_center()
            .justify_center()
            .rounded_full()
            .bg(bg_color)
            .cursor_pointer()
            .child(
                // Mockup: 16px stroke arrow-up (#cfcfcf), stop glyph while streaming.
                if self.is_streaming {
                    div()
                        .text_xs()
                        .font_weight(FontWeight::BOLD)
                        .text_color(rgb(0xcfcfcf))
                        .child("■")
                } else {
                    div()
                        .flex()
                        .items_center()
                        .justify_center()
                        .child(icon_svg("arrow-up", 16.0).text_color(rgb(0xcfcfcf)))
                },
            )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_model_pill_dot_colors() {
        let theme = HermesTheme::default();

        let anthropic_dot = provider_dot_color("Anthropic", &theme);
        assert_eq!((anthropic_dot.r * 255.0).round() as u8, 0xd9);

        let openai_dot = provider_dot_color("OpenAI", &theme);
        assert_eq!((openai_dot.r * 255.0).round() as u8, 0x10);

        let google_dot = provider_dot_color("Google", &theme);
        assert_eq!((google_dot.r * 255.0).round() as u8, 0x3b);
    }

    #[test]
    fn test_send_button_icons_and_states() {
        let disabled_send = SendButton::new(false, false);
        assert_eq!(disabled_send.icon(), "↑");

        let active_send = SendButton::new(false, true);
        assert_eq!(active_send.icon(), "↑");

        let stop_btn = SendButton::new(true, false);
        assert_eq!(stop_btn.icon(), "⏹");
    }
}

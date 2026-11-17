//! Bottom multiline prompt composer with token gauge and model selector for HEX Desktop.
//!
//! Directly mirrors Hermes Desktop composer (`app/chat/composer/`):
//! - Auto-growing multiline input area (from 1 line ~40px up to 8 lines ~200px)
//! - Rotating or default placeholder: "Ask HEX anything, or type / for slash commands..."
//! - Key shortcuts: Enter to submit, Shift+Enter for newline, Cmd+Enter forced submit
//! - Inline clear button (`×`) when text is present
//! - Integrated `TokenGaugeWidget` showing ASCII block progress bar
//! - Integrated `ModelSelectorPill` and circular `SendButton` (`↑` / `⏹`)

use gpui::*;

use crate::theme::HermesTheme;
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

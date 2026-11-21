//! Collapsible `<think>` reasoning fold with elapsed activity timer for HEX Desktop.
//!
//! Directly mirrors Hermes Desktop reasoning disclosure (`components/assistant-ui/thread/message-parts.tsx`):
//! - Dynamic header state:
//!   - While generating: "Thinking..." with pulse indicator
//!   - When finished with < 1s: "Thought briefly"
//!   - When finished with >= 1s: "Thought for 6s" (or formatted duration)
//!   - When finished without timer: "Thought"
//! - Collapsible disclosure with caret (`▾` / `▸`) and brain glyph (`🧠`)
//! - Expandable content box with monospace font, muted zinc-400 text, and left accent border

use gpui::*;

use crate::theme::HermesTheme;
use crate::ui::icon_svg;

/// Formats an elapsed duration in seconds into human-readable timing text.
pub fn format_reasoning_elapsed(seconds: f64) -> String {
    if seconds < 1.0 {
        "briefly".to_string()
    } else if seconds < 60.0 {
        format!("{:.0}s", seconds)
    } else {
        let mins = (seconds / 60.0).floor() as u64;
        let rem_secs = (seconds % 60.0).round() as u64;
        if rem_secs > 0 {
            format!("{}m {}s", mins, rem_secs)
        } else {
            format!("{}m", mins)
        }
    }
}

/// Thinking disclosure component representing internal chain-of-thought reasoning.
#[derive(Debug, Clone, PartialEq)]
pub struct ThinkingDisclosure {
    /// Full reasoning or chain-of-thought text.
    pub thought_text: String,
    /// Whether the model is actively streaming tokens.
    pub is_generating: bool,
    /// Elapsed execution duration in seconds, if measured.
    pub elapsed_seconds: Option<f64>,
    /// Whether the reasoning disclosure fold is expanded.
    pub is_expanded: bool,
}

impl Default for ThinkingDisclosure {
    fn default() -> Self {
        Self::new("")
    }
}

impl ThinkingDisclosure {
    pub fn new(thought_text: impl Into<String>) -> Self {
        Self {
            thought_text: thought_text.into(),
            is_generating: false,
            elapsed_seconds: None,
            is_expanded: false,
        }
    }

    pub fn with_generating(mut self, generating: bool) -> Self {
        self.is_generating = generating;
        self
    }

    pub fn with_duration(mut self, duration_secs: f64) -> Self {
        self.elapsed_seconds = Some(duration_secs);
        self
    }

    pub fn with_expanded(mut self, expanded: bool) -> Self {
        self.is_expanded = expanded;
        self
    }

    pub fn toggle(&mut self) {
        self.is_expanded = !self.is_expanded;
    }

    /// Dynamic header text according to Hermes specifications.
    pub fn header_label(&self) -> String {
        if self.is_generating {
            "Thinking...".to_string()
        } else {
            match self.elapsed_seconds {
                None => "Thought".to_string(),
                Some(s) if s < 1.0 => "Thought briefly".to_string(),
                Some(s) => format!("Thought for {}", format_reasoning_elapsed(s)),
            }
        }
    }

    /// Appends streaming reasoning tokens.
    pub fn append_chunk(&mut self, chunk: &str) {
        self.thought_text.push_str(chunk);
    }

    /// Sets generation completed with final duration.
    pub fn finish(&mut self, final_duration_secs: f64) {
        self.is_generating = false;
        self.elapsed_seconds = Some(final_duration_secs);
    }

    /// Renders the collapsible thinking fold.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        let caret = if self.is_expanded { "▾" } else { "▸" };
        let label = self.header_label();

        let mut container = div()
            .w_full()
            .flex()
            .flex_col()
            .gap_1();

        // Header Row
        let header = div()
            .flex()
            .flex_row()
            .items_center()
            .gap_2()
            .py_1()
            .px_2()
            .rounded_md()
            .cursor_pointer()
            .hover(|s| s.bg(theme.card_hover()))
            .child(
                div()
                    .text_xs()
                    .text_color(theme.text_dim())
                    .child(caret),
            )
            .child(
                div()
                    .text_xs()
                    .text_color(theme.text_dim())
                    .child(icon_svg("design", 14.0)),
            )
            .child(
                div()
                    .text_xs()
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(if self.is_generating {
                        theme.accent_blue()
                    } else {
                        theme.text_muted()
                    })
                    .child(label),
            );

        container = container.child(header);

        // Expandable Content Body
        if self.is_expanded {
            let body = div()
                .w_full()
                .ml_2()
                .p_3()
                .rounded_md()
                .bg(theme.card_bg())
                .border_l_2()
                .border_color(theme.border_structural())
                .text_xs()
                .text_color(theme.text_muted())
                .child(self.thought_text.clone());

            container = container.child(body);
        }

        container
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_reasoning_elapsed_formatting() {
        assert_eq!(format_reasoning_elapsed(0.4), "briefly");
        assert_eq!(format_reasoning_elapsed(6.2), "6s");
        assert_eq!(format_reasoning_elapsed(25.0), "25s");
        assert_eq!(format_reasoning_elapsed(72.0), "1m 12s");
        assert_eq!(format_reasoning_elapsed(120.0), "2m");
    }

    #[test]
    fn test_thinking_disclosure_header_states() {
        let mut d = ThinkingDisclosure::new("Analyzing user request...");

        // While generating
        d.is_generating = true;
        assert_eq!(d.header_label(), "Thinking...");

        // Settled with 0.5s -> Thought briefly
        d.finish(0.5);
        assert_eq!(d.header_label(), "Thought briefly");

        // Settled with 6.0s -> Thought for 6s
        d.finish(6.0);
        assert_eq!(d.header_label(), "Thought for 6s");

        // Settled without timer -> Thought
        d.elapsed_seconds = None;
        assert_eq!(d.header_label(), "Thought");
    }

    #[test]
    fn test_thinking_disclosure_expansion_toggle() {
        let mut d = ThinkingDisclosure::new("Reasoning trace");
        assert!(!d.is_expanded);

        d.toggle();
        assert!(d.is_expanded);

        d.toggle();
        assert!(!d.is_expanded);
    }
}

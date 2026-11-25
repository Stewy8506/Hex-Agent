//! Collapsible `<think>` reasoning fold with elapsed activity timer for HEX Desktop.
//!
//! Header states (Hermes semantics): "Thinking..." while generating, "Thought
//! briefly" / "Thought for 6s" / "Thought" once settled. Styling follows the
//! mockup token language (plan P5, derived surface): chevron SVG carets
//! instead of ▾/▸ (D21), dim 12.5px labels, and a mono `#101010` body.

use std::rc::Rc;

use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::icon_svg;
use crate::ui::motion::{RevealMotion, RevealValues};
use crate::ui::payload::{
    copy_button, preview_notice, preview_text, ScrollablePayload, TextCopyHandler,
    PAYLOAD_MAX_HEIGHT,
};

pub type DisclosureClickHandler = Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>;

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
        self.render_with_click(theme, None, None)
    }

    pub fn render_with_click(
        &self,
        theme: &HermesTheme,
        id: Option<ElementId>,
        on_click: Option<DisclosureClickHandler>,
    ) -> Div {
        self.render_with_click_and_copy(theme, id, on_click, None, "reasoning")
    }

    pub fn render_with_click_and_copy(
        &self,
        theme: &HermesTheme,
        id: Option<ElementId>,
        on_click: Option<DisclosureClickHandler>,
        on_copy: Option<TextCopyHandler>,
        copy_id: &str,
    ) -> Div {
        self.render_inner(theme, id, on_click, on_copy, copy_id, None)
    }

    pub fn render_reveal(
        &self,
        theme: &HermesTheme,
        id: Option<ElementId>,
        on_click: Option<DisclosureClickHandler>,
        reveal: RevealMotion,
        cx: &App,
    ) -> Div {
        self.render_reveal_with_copy(theme, id, on_click, None, "reasoning", reveal, cx)
    }

    pub fn render_reveal_with_copy(
        &self,
        theme: &HermesTheme,
        id: Option<ElementId>,
        on_click: Option<DisclosureClickHandler>,
        on_copy: Option<TextCopyHandler>,
        copy_id: &str,
        reveal: RevealMotion,
        cx: &App,
    ) -> Div {
        self.render_inner(theme, id, on_click, on_copy, copy_id, Some((reveal, cx)))
    }

    fn render_inner(
        &self,
        theme: &HermesTheme,
        id: Option<ElementId>,
        on_click: Option<DisclosureClickHandler>,
        on_copy: Option<TextCopyHandler>,
        copy_id: &str,
        reveal: Option<(RevealMotion, &App)>,
    ) -> Div {
        let reveal_values = reveal.map(|(reveal, cx)| reveal.values(cx));
        let expanded_progress = reveal_values
            .map(|values| values.spatial)
            .unwrap_or(f32::from(self.is_expanded));
        let caret = icon_svg("chevron-right", 14.0)
            .text_color(theme.mockup_text_dim)
            .with_transformation(Transformation::rotate(radians(
                expanded_progress.clamp(0.0, 1.0) * std::f32::consts::FRAC_PI_2,
            )));
        let label = self.header_label();

        let mut container = div().w_full().flex().flex_col().gap(px(6.0));

        let header = div()
            .flex()
            .flex_row()
            .items_center()
            .gap(px(8.0))
            .py(px(4.0))
            .px(px(8.0))
            .rounded(px(8.0))
            .child(caret)
            .child(
                div()
                    .flex_1()
                    .min_w_0()
                    .text_size(px(MockupTypeScale::INLINE_CODE_SIZE))
                    .text_color(if self.is_generating {
                        theme.accent_blue()
                    } else {
                        theme.mockup_text_dim
                    })
                    .child(label),
            )
            .child(copy_button(
                copy_id,
                &self.thought_text,
                on_copy.as_ref(),
                theme,
            ));
        let header = if let (Some(id), Some(on_click)) = (id, on_click) {
            let selector = id.to_string();
            header
                .id(id)
                .debug_selector(move || selector)
                .cursor_pointer()
                .hover(|s| s.bg(theme.mockup_hover))
                .on_click(move |event, window, cx| on_click(event, window, cx))
                .into_any_element()
        } else {
            header.into_any_element()
        };

        container = container.child(header);

        let body_visible = reveal_values.map_or(self.is_expanded, RevealValues::is_visible);
        if body_visible {
            let (preview, truncated) = on_copy
                .as_ref()
                .map(|_| preview_text(&self.thought_text, 400))
                .unwrap_or_else(|| (self.thought_text.clone(), false));
            let mut body = div()
                .w_full()
                .max_h(px(PAYLOAD_MAX_HEIGHT))
                .scroll_y(&format!("reasoning_{copy_id}_scroll"))
                .rounded(px(8.0))
                .bg(theme.mockup_sidebar)
                .border_l_2()
                .border_color(theme.mockup_border_strong)
                .px(px(12.0))
                .py(px(10.0))
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(MockupTypeScale::INLINE_CODE_SIZE))
                .line_height(px(MockupTypeScale::DIFF_LINE_HEIGHT))
                .whitespace_normal()
                .text_color(theme.mockup_text_muted)
                .child(preview);
            if let Some(values) = reveal_values {
                body = values.apply(body);
            }
            container = container.child(body);
            if truncated {
                container = container.child(preview_notice(theme));
            }
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

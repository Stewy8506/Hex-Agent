//! Message views for User queries and Assistant responses in HEX Desktop.
//!
//! Mirrors Hermes Desktop message components (`components/assistant-ui/thread/`):
//! - `UserMessageCard`: Right/center aligned card with `#18181b` background, rounded borders, avatar, timestamp.
//! - `AssistantMessageView`: Left aligned response with markdown prose, code blocks, thinking folds, tool cards, and action bar.

use chrono::{DateTime, Utc};
use gpui::*;

use crate::theme::HermesTheme;
use crate::ui::reasoning::ThinkingDisclosure;
use crate::ui::tool_card::ToolCallCard;

/// Formats a message timestamp into a compact HH:MM AM/PM string.
pub fn format_message_time(dt: DateTime<Utc>) -> String {
    dt.format("%l:%M %p").to_string().trim().to_string()
}

/// A stretch of assistant content: plain prose or a fenced code block.
#[derive(Debug, Clone, PartialEq)]
pub enum ContentSegment {
    Prose(String),
    Code { language: String, code: String },
}

/// Splits assistant text into prose and fenced code blocks. An unterminated
/// fence (still streaming) renders its accumulated lines as code rather than
/// leaking raw backticks into prose.
pub fn split_content_segments(content: &str) -> Vec<ContentSegment> {
    let mut segments = Vec::new();
    let mut prose = String::new();
    let mut code: Option<(String, String)> = None;

    for line in content.split_inclusive('\n') {
        let trimmed = line.trim_start();
        if let Some((language, body)) = code.as_mut() {
            if trimmed.trim_end() == "```" {
                segments.push(ContentSegment::Code {
                    language: language.clone(),
                    code: body.trim_end_matches('\n').to_string(),
                });
                code = None;
            } else {
                body.push_str(line);
            }
        } else if trimmed.starts_with("```") {
            if !prose.is_empty() {
                segments.push(ContentSegment::Prose(std::mem::take(&mut prose)));
            }
            code = Some((trimmed[3..].trim().to_string(), String::new()));
        } else {
            prose.push_str(line);
        }
    }

    if let Some((language, body)) = code {
        segments.push(ContentSegment::Code {
            language,
            code: body.trim_end_matches('\n').to_string(),
        });
    }
    if !prose.is_empty() {
        segments.push(ContentSegment::Prose(prose));
    }
    segments
}

/// User message query card.
#[derive(Debug, Clone, PartialEq)]
pub struct UserMessageCard {
    pub id: String,
    pub content: String,
    pub timestamp: DateTime<Utc>,
    pub avatar_glyph: String,
}

impl UserMessageCard {
    pub fn new(id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            content: content.into(),
            timestamp: Utc::now(),
            avatar_glyph: "👤".to_string(),
        }
    }

    pub fn with_timestamp(mut self, timestamp: DateTime<Utc>) -> Self {
        self.timestamp = timestamp;
        self
    }

    /// Renders the user message card aligned to the right/center.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        let time_str = format_message_time(self.timestamp);

        div()
            .w_full()
            .flex()
            .flex_row()
            .justify_end()
            .py_2()
            .child(
                div()
                    .max_w(px(680.0))
                    .flex()
                    .flex_col()
                    .gap_1()
                    .p_3()
                    .rounded_lg()
                    .bg(theme.card_bg()) // #18181b
                    .border_1()
                    .border_color(theme.border_subtle())
                    // Header: Avatar & Timestamp
                    .child(
                        div()
                            .flex()
                            .flex_row()
                            .items_center()
                            .justify_between()
                            .gap_2()
                            .child(
                                div()
                                    .flex()
                                    .flex_row()
                                    .items_center()
                                    .gap(px(6.0))
                                    .child(
                                        div()
                                            .text_xs()
                                            .child(self.avatar_glyph.clone()),
                                    )
                                    .child(
                                        div()
                                            .text_xs()
                                            .font_weight(FontWeight::SEMIBOLD)
                                            .text_color(theme.text_primary())
                                            .child("You"),
                                    ),
                            )
                            .child(
                                div()
                                    .text_xs()
                                    .text_color(theme.text_dim())
                                    .child(time_str),
                            ),
                    )
                    // Content Body
                    .child(
                        div()
                            .text_sm()
                            .text_color(theme.text_primary())
                            .child(self.content.clone()),
                    ),
            )
    }
}

/// Assistant response view hosting prose, reasoning, tool calls, and action bar.
#[derive(Debug, Clone, PartialEq)]
pub struct AssistantMessageView {
    pub id: String,
    pub content: String,
    pub reasoning: Option<ThinkingDisclosure>,
    pub tool_calls: Vec<ToolCallCard>,
    pub is_streaming: bool,
    pub timestamp: DateTime<Utc>,
    pub model_name: String,
}

impl AssistantMessageView {
    pub fn new(id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            content: content.into(),
            reasoning: None,
            tool_calls: Vec::new(),
            is_streaming: false,
            timestamp: Utc::now(),
            // Unknown until the runtime reports it; never invent a model.
            model_name: String::new(),
        }
    }

    pub fn with_reasoning(mut self, reasoning: ThinkingDisclosure) -> Self {
        self.reasoning = Some(reasoning);
        self
    }

    pub fn with_tool_call(mut self, tool_call: ToolCallCard) -> Self {
        self.tool_calls.push(tool_call);
        self
    }

    pub fn with_streaming(mut self, streaming: bool) -> Self {
        self.is_streaming = streaming;
        self
    }

    pub fn with_model(mut self, model: impl Into<String>) -> Self {
        self.model_name = model.into();
        self
    }

    /// Renders the static action bar (Copy, Regenerate).
    ///
    /// Branch is intentionally absent: the ACP backend has no branch operation,
    /// so the interactive `ChatView` bar only offers actions it can perform.
    fn render_action_bar(&self, theme: &HermesTheme) -> Div {
        div()
            .flex()
            .flex_row()
            .items_center()
            .gap_3()
            .pt_1()
            .text_xs()
            .child(
                div()
                    .cursor_pointer()
                    .text_color(theme.text_dim())
                    .hover(|s| s.text_color(theme.text_primary()))
                    .child("📋 Copy"),
            )
            .child(
                div()
                    .cursor_pointer()
                    .text_color(theme.text_dim())
                    .hover(|s| s.text_color(theme.text_primary()))
                    .child("🔄 Regenerate"),
            )
    }

    /// Renders the assistant message view aligned to the left.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        self.render_with_action_bar(theme, Some(self.render_action_bar(theme)))
    }

    /// Renders the message body with a caller-supplied action bar (or none).
    /// `ChatView` passes an interactive bar wired to clipboard/regeneration.
    pub fn render_with_action_bar(&self, theme: &HermesTheme, action_bar: Option<Div>) -> Div {
        let time_str = format_message_time(self.timestamp);

        let mut badge = div()
            .flex()
            .flex_row()
            .items_center()
            .gap_2()
            .child(div().text_sm().child("⚡"))
            .child(
                div()
                    .text_xs()
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.text_primary())
                    .child("HEX"),
            );
        if !self.model_name.is_empty() {
            badge = badge.child(
                div()
                    .text_xs()
                    .text_color(theme.text_dim())
                    .child(self.model_name.clone()),
            );
        }

        let mut card = div()
            .w_full()
            .flex()
            .flex_col()
            .gap_2()
            .py_2()
            // Top Header: Assistant Badge & Model & Time
            .child(
                div()
                    .flex()
                    .flex_row()
                    .items_center()
                    .justify_between()
                    .child(badge)
                    .child(
                        div()
                            .text_xs()
                            .text_color(theme.text_dim())
                            .child(time_str),
                    ),
            );

        // 1. Thinking / Reasoning Fold
        if let Some(reasoning) = &self.reasoning {
            card = card.child(reasoning.render(theme));
        }

        // 2. Tool Call Cards
        for tool in &self.tool_calls {
            card = card.child(tool.render(theme));
        }

        // 3. Prose / Text Content (fenced code blocks render as structured blocks)
        for segment in split_content_segments(&self.content) {
            match segment {
                ContentSegment::Prose(text) => {
                    if !text.trim().is_empty() {
                        card = card.child(
                            div()
                                .w_full()
                                .text_sm()
                                .text_color(theme.text_primary())
                                .child(text),
                        );
                    }
                }
                ContentSegment::Code { language, code } => {
                    let mut block = div()
                        .w_full()
                        .flex()
                        .flex_col()
                        .gap_1()
                        .p_3()
                        .rounded_lg()
                        .bg(theme.card_bg())
                        .border_1()
                        .border_color(theme.border_subtle());
                    if !language.is_empty() {
                        block = block.child(
                            div()
                                .text_xs()
                                .text_color(theme.text_dim())
                                .child(language),
                        );
                    }
                    card = card.child(block.child(
                        div()
                            .text_sm()
                            .font_family("Menlo")
                            .text_color(theme.text_primary())
                            .child(code),
                    ));
                }
            }
        }

        // 4. Streaming Pulse Indicator
        if self.is_streaming {
            card = card.child(
                div()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap_1()
                    .child(
                        div()
                            .w(px(6.0))
                            .h(px(6.0))
                            .rounded_full()
                            .bg(theme.accent_blue()),
                    )
                    .child(
                        div()
                            .text_xs()
                            .text_color(theme.accent_blue())
                            .child("Streaming..."),
                    ),
            );
        }

        // 5. Action Bar (only when not streaming)
        if !self.is_streaming {
            if let Some(bar) = action_bar {
                card = card.child(bar);
            }
        }

        card
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_user_and_assistant_message_creation() {
        let u = UserMessageCard::new("msg_1", "How do I implement GPUI views?");
        assert_eq!(u.id, "msg_1");
        assert_eq!(u.content, "How do I implement GPUI views?");

        let a = AssistantMessageView::new("msg_2", "GPUI views implement the Render trait.")
            .with_model("Claude 3.7 Sonnet")
            .with_streaming(true);

        assert_eq!(a.id, "msg_2");
        assert!(a.is_streaming);
        assert_eq!(a.model_name, "Claude 3.7 Sonnet");
    }

    #[test]
    fn test_assistant_model_identity_defaults_to_unknown() {
        let a = AssistantMessageView::new("msg_1", "hello");
        assert_eq!(a.model_name, "", "must not invent a model name");
    }

    #[test]
    fn test_split_content_segments_prose_and_code() {
        let content = "Here is a fix:\n```rust\nfn main() {}\n```\nRun it with cargo.";
        let segments = split_content_segments(content);
        assert_eq!(
            segments,
            vec![
                ContentSegment::Prose("Here is a fix:\n".to_string()),
                ContentSegment::Code {
                    language: "rust".to_string(),
                    code: "fn main() {}".to_string(),
                },
                ContentSegment::Prose("Run it with cargo.".to_string()),
            ]
        );
    }

    #[test]
    fn test_split_content_segments_unterminated_fence_streams_as_code() {
        let segments = split_content_segments("Writing code:\n```python\nprint(1)\n");
        assert_eq!(
            segments,
            vec![
                ContentSegment::Prose("Writing code:\n".to_string()),
                ContentSegment::Code {
                    language: "python".to_string(),
                    code: "print(1)".to_string(),
                },
            ]
        );
    }

    #[test]
    fn test_split_content_segments_plain_prose_unchanged() {
        let segments = split_content_segments("Just prose.\nSecond line.");
        assert_eq!(
            segments,
            vec![ContentSegment::Prose("Just prose.\nSecond line.".to_string())]
        );
        assert!(split_content_segments("").is_empty());
    }

    #[test]
    fn test_message_render_smoke_with_code_and_unknown_model() {
        let theme = HermesTheme::default();
        let view = AssistantMessageView::new(
            "msg_1",
            "Answer:\n```bash\nls -la\n```\nDone.",
        );
        let _ = view.render(&theme);
        let _ = view.with_model("claude-3-7-sonnet").render(&theme);
    }
}

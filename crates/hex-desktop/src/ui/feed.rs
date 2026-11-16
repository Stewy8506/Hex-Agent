//! Scrollable conversation message feed for HEX Desktop.
//!
//! Directly mirrors Hermes Desktop conversation thread (`components/assistant-ui/thread/`):
//! - Scrollable thread layout with auto-scroll to bottom on token arrival
//! - User message bubbles and streaming assistant responses
//! - Collapsible reasoning folds with elapsed timing
//! - Distinct tool call cards with diff visualizer

use gpui::*;

use crate::theme::HermesTheme;
use crate::ui::message::{AssistantMessageView, UserMessageCard};
use crate::ui::tool_card::{ToolCallCard, ToolStatus};

/// Items rendered within the conversation feed.
#[derive(Debug, Clone, PartialEq)]
pub enum MessageItem {
    User(UserMessageCard),
    Assistant(AssistantMessageView),
    SystemNotice(String),
}

/// Scrollable message feed component.
#[derive(Debug, Clone, PartialEq)]
pub struct MessageFeed {
    pub messages: Vec<MessageItem>,
    pub auto_scroll_bottom: bool,
    pub is_streaming: bool,
}

impl Default for MessageFeed {
    fn default() -> Self {
        Self::new()
    }
}

impl MessageFeed {
    pub fn new() -> Self {
        Self {
            messages: Vec::new(),
            auto_scroll_bottom: true,
            is_streaming: false,
        }
    }

    pub fn with_messages(messages: Vec<MessageItem>) -> Self {
        Self {
            messages,
            auto_scroll_bottom: true,
            is_streaming: false,
        }
    }

    pub fn len(&self) -> usize {
        self.messages.len()
    }

    pub fn is_empty(&self) -> bool {
        self.messages.is_empty()
    }

    pub fn add_user_message(&mut self, content: impl Into<String>) -> String {
        let id = format!("msg_{}", chrono::Utc::now().timestamp_millis());
        let card = UserMessageCard::new(id.clone(), content);
        self.messages.push(MessageItem::User(card));
        self.auto_scroll_bottom = true;
        id
    }

    pub fn add_assistant_message(&mut self, content: impl Into<String>) -> String {
        let id = format!("msg_{}", chrono::Utc::now().timestamp_millis());
        let view = AssistantMessageView::new(id.clone(), content);
        self.messages.push(MessageItem::Assistant(view));
        self.auto_scroll_bottom = true;
        id
    }

    pub fn add_system_notice(&mut self, notice: impl Into<String>) {
        self.messages.push(MessageItem::SystemNotice(notice.into()));
    }

    pub fn start_assistant_streaming(&mut self) -> String {
        let id = format!("msg_{}", chrono::Utc::now().timestamp_millis());
        let view = AssistantMessageView::new(id.clone(), "").with_streaming(true);
        self.messages.push(MessageItem::Assistant(view));
        self.is_streaming = true;
        self.auto_scroll_bottom = true;
        id
    }

    pub fn append_streaming_chunk(&mut self, chunk: &str) {
        if let Some(MessageItem::Assistant(ref mut assistant)) = self.messages.last_mut() {
            assistant.content.push_str(chunk);
            self.auto_scroll_bottom = true;
        }
    }

    pub fn finish_streaming(&mut self) {
        if let Some(MessageItem::Assistant(ref mut assistant)) = self.messages.last_mut() {
            assistant.is_streaming = false;
        }
        self.is_streaming = false;
    }

    pub fn add_tool_call(&mut self, tool: ToolCallCard) {
        if let Some(MessageItem::Assistant(ref mut assistant)) = self.messages.last_mut() {
            assistant.tool_calls.push(tool);
        } else {
            let mut view = AssistantMessageView::new(format!("msg_{}", chrono::Utc::now().timestamp_millis()), "");
            view.tool_calls.push(tool);
            self.messages.push(MessageItem::Assistant(view));
        }
    }

    pub fn update_tool_result(&mut self, call_id: &str, output: &str, is_error: bool) {
        for msg in self.messages.iter_mut().rev() {
            if let MessageItem::Assistant(ref mut assistant) = msg {
                for tool in &mut assistant.tool_calls {
                    if tool.call_id == call_id {
                        tool.output_text = Some(output.to_string());
                        tool.status = if is_error {
                            ToolStatus::Failed
                        } else {
                            ToolStatus::Success
                        };
                        return;
                    }
                }
            }
        }
    }

    pub fn clear(&mut self) {
        self.messages.clear();
        self.is_streaming = false;
    }

    /// Renders the complete scrollable conversation feed.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        self.render_inner(theme, |view| view.render(theme))
    }

    /// Renders the feed with caller-supplied action bars per assistant message
    /// (`None` hides the bar). `ChatView` uses this to wire Copy/Regenerate.
    pub fn render_with_bars(
        &self,
        theme: &HermesTheme,
        mut bar_for: impl FnMut(&AssistantMessageView) -> Option<Div>,
    ) -> Div {
        self.render_inner(theme, |view| {
            view.render_with_action_bar(theme, bar_for(view))
        })
    }

    fn render_inner(
        &self,
        theme: &HermesTheme,
        mut render_assistant: impl FnMut(&AssistantMessageView) -> Div,
    ) -> Div {
        let mut feed_container = div()
            .flex_1()
            .w_full()
            .flex()
            .flex_col()
            .gap_4()
            .p_4()
            .overflow_y_hidden();

        if self.messages.is_empty() {
            return feed_container.child(
                div()
                    .flex_1()
                    .flex()
                    .flex_col()
                    .items_center()
                    .justify_center()
                    .gap_2()
                    .child(
                        div()
                            .text_lg()
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(theme.text_primary())
                            .child("HEX Autonomous AI Workbench"),
                    )
                    .child(
                        div()
                            .text_xs()
                            .text_color(theme.text_muted())
                            .child("Ask a question, run a command, or inspect code diffs."),
                    ),
            );
        }

        for item in &self.messages {
            match item {
                MessageItem::User(card) => {
                    feed_container = feed_container.child(card.render(theme));
                }
                MessageItem::Assistant(view) => {
                    feed_container = feed_container.child(render_assistant(view));
                }
                MessageItem::SystemNotice(notice) => {
                    feed_container = feed_container.child(
                        div()
                            .w_full()
                            .flex()
                            .justify_center()
                            .py_1()
                            .child(
                                div()
                                    .px_3()
                                    .py(px(2.0))
                                    .rounded_md()
                                    .bg(theme.card_bg())
                                    .border_1()
                                    .border_color(theme.border_subtle())
                                    .text_xs()
                                    .text_color(theme.text_dim())
                                    .child(notice.clone()),
                            ),
                    );
                }
            }
        }

        feed_container
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_feed_streaming_and_append() {
        let mut feed = MessageFeed::new();
        assert!(feed.is_empty());

        let user_id = feed.add_user_message("Hello, HEX!");
        assert_eq!(feed.len(), 1);
        assert!(user_id.starts_with("msg_"));

        let assistant_id = feed.start_assistant_streaming();
        assert_eq!(feed.len(), 2);
        assert!(feed.is_streaming);

        feed.append_streaming_chunk("I am ready ");
        feed.append_streaming_chunk("to assist you.");
        feed.finish_streaming();

        assert!(!feed.is_streaming);
        if let MessageItem::Assistant(assistant) = &feed.messages[1] {
            assert_eq!(assistant.id, assistant_id);
            assert_eq!(assistant.content, "I am ready to assist you.");
            assert!(!assistant.is_streaming);
        } else {
            panic!("Expected Assistant message");
        }
    }
}

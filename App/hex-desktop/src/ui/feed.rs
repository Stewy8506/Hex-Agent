//! Mockup conversation feed for HEX Desktop (parity plan §2.4 / §6 P4).
//!
//! Mirrors the DesktopSession mockup's feed: a 560px column (gap 20,
//! padding-top 10, padding-bottom 6) that bottoms out at the newest turn,
//! inside a full-height area with a 28px canvas fade across its top edge.
//! Scrolling is a GPUI `list` anchored to `ListAlignment::Bottom`, so short
//! conversations hug the composer and long ones scroll internally.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::message::{ActionBarHandlers, AssistantMessageView, UserMessageCard};
use crate::ui::motion;
use crate::ui::tool_card::{ToolCallCard, ToolStatus};

/// Vertical gap between feed items (mockup `gap:20px`).
pub const FEED_GAP: f32 = 20.0;
/// Feed area top padding (mockup `padding-top:10px`).
pub const FEED_PAD_TOP: f32 = 10.0;
/// Feed column bottom padding (mockup `padding-bottom:6px`).
pub const FEED_PAD_BOTTOM: f32 = 6.0;
/// Height of the canvas fade across the feed's top edge (mockup mask fade).
pub const FEED_FADE_HEIGHT: f32 = 28.0;
/// Feed column width (mockup `width:560px`).
pub const FEED_COLUMN_WIDTH: f32 = 560.0;

/// Painted bounds of the feed fade overlay, recorded for tests. Production
/// never sets this global, so the paint listener is a no-op read.
#[derive(Debug, Clone, Default)]
pub struct FeedPaintProbe(pub Arc<Mutex<Vec<Bounds<Pixels>>>>);

impl Global for FeedPaintProbe {}

/// The two gradient stops backing the top fade: opaque canvas at the edge
/// blending to fully transparent at [`FEED_FADE_HEIGHT`].
pub fn feed_fade_stops(canvas: Rgba) -> (Hsla, Hsla) {
    let base: Hsla = canvas.into();
    (base, base.alpha(0.0))
}

/// Items rendered within the conversation feed.
#[derive(Debug, Clone, PartialEq)]
pub enum MessageItem {
    User(UserMessageCard),
    Assistant(AssistantMessageView),
    SystemNotice(String),
}

/// Message feed state shared with `ChatView`; rendering happens through
/// [`MessageFeed::render_scrolled`] (session screen) or [`MessageFeed::render`]
/// (static shell path).
#[derive(Debug, Clone, PartialEq)]
pub struct MessageFeed {
    pub messages: Vec<MessageItem>,
    pub auto_scroll_bottom: bool,
    pub is_streaming: bool,
    /// Transport is dialing a new session; the feed shows a connecting spinner.
    pub is_connecting: bool,
    /// Set while the supervised reconnect with backoff is in progress; the
    /// feed shows an inline reconnect notice instead of failing silently.
    pub reconnect_notice: Option<String>,
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
            is_connecting: false,
            reconnect_notice: None,
        }
    }

    pub fn with_messages(messages: Vec<MessageItem>) -> Self {
        Self {
            messages,
            auto_scroll_bottom: true,
            is_streaming: false,
            is_connecting: false,
            reconnect_notice: None,
        }
    }

    /// Sets the inline reconnect/error notice shown above the feed content.
    pub fn set_reconnect_notice(&mut self, notice: Option<String>) {
        self.reconnect_notice = notice;
    }

    /// Marks the feed as dialing a session (connecting spinner state).
    pub fn set_connecting(&mut self, connecting: bool) {
        self.is_connecting = connecting;
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

    /// Renders the full feed area: the 560px bottom-anchored scroll column,
    /// the 28px top fade, and the notice / empty / connecting states.
    ///
    /// `bars` carries per-assistant click handlers keyed by message id;
    /// `token_count` is the honest gauge total ("24.5k tokens") or `None`
    /// while unknown (hidden).
    pub fn render_scrolled(
        &self,
        theme: &HermesTheme,
        list_state: &ListState,
        bars: HashMap<String, ActionBarHandlers>,
        token_count: Option<String>,
    ) -> Div {
        let mut root = div()
            .flex_1()
            .min_h_0()
            .w_full()
            .flex()
            .flex_col()
            .overflow_hidden();

        // Reconnect notice: inline above the feed while the supervised
        // reconnect with backoff is in progress.
        if let Some(notice) = &self.reconnect_notice {
            root = root.child(
                div()
                    .w_full()
                    .flex()
                    .justify_center()
                    .pb_2()
                    .child(
                        div()
                            .rounded(px(8.0))
                            .bg(theme.mockup_diff_del_bg)
                            .px_3()
                            .py(px(6.0))
                            .text_size(px(MockupTypeScale::HINT_SIZE))
                            .text_color(theme.rose_fg())
                            .child(notice.clone()),
                    ),
            );
        }

        let count = self.messages.len();
        let messages = self.messages.clone();
        let list_theme = theme.clone();
        let token_count = token_count.clone();

        let area = div()
            .relative()
            .flex_1()
            .min_h_0()
            .w_full()
            .flex()
            .flex_col()
            .justify_center()
            .pt(px(FEED_PAD_TOP))
            .on_children_prepainted(|children, _window, cx| {
                if let Some(probe) = cx.try_global::<FeedPaintProbe>() {
                    if let Some(fade) = children.last() {
                        probe.0.lock().unwrap().push(*fade);
                    }
                }
            });

        let mut column = div()
            .w(px(FEED_COLUMN_WIDTH))
            .max_w_full()
            .h_full()
            .flex()
            .flex_col();

        if self.messages.is_empty() && !self.is_connecting {
            let empty = div()
                .flex_1()
                .flex()
                .flex_col()
                .items_center()
                .justify_center()
                .gap(px(8.0))
                .child(
                    div()
                        .text_size(px(MockupTypeScale::BASE_SIZE))
                        .line_height(px(MockupTypeScale::BASE_LINE_HEIGHT))
                        .font_weight(FontWeight::SEMIBOLD)
                        .text_color(theme.mockup_text_bright)
                        .child("HEX Autonomous AI Workbench"),
                )
                .child(
                    div()
                        .text_size(px(13.0))
                        .text_color(theme.mockup_text_dim)
                        .child("Ask a question, run a command, or inspect code diffs."),
                );
            column = column.child(empty);
        } else if self.is_connecting {
            let spinner_el: AnyElement = if motion::reduced_motion() {
                div()
                    .font_family(theme.font_mono()[0].clone())
                    .text_size(px(MockupTypeScale::SPINNER_SIZE))
                    .text_color(theme.accent_blue())
                    .child(motion::spinner_frame(Duration::ZERO).to_string())
                    .into_any_element()
            } else {
                div()
                    .font_family(theme.font_mono()[0].clone())
                    .text_size(px(MockupTypeScale::SPINNER_SIZE))
                    .text_color(theme.accent_blue())
                    .with_animation(
                        "feed_connect_spin",
                        Animation::new(motion::SPIN_PERIOD).repeat(),
                        |el, phase| {
                            let elapsed =
                                Duration::from_secs_f32(phase * motion::SPIN_PERIOD.as_secs_f32());
                            el.child(motion::spinner_frame(elapsed).to_string())
                        },
                    )
                    .into_any_element()
            };
            column = column.child(
                div()
                    .flex_1()
                    .flex()
                    .flex_col()
                    .items_center()
                    .justify_center()
                    .gap(px(10.0))
                    .child(spinner_el)
                    .child(
                        div()
                            .text_size(px(13.0))
                            .text_color(theme.mockup_text_muted)
                            .child("Connecting to session…"),
                    ),
            );
        } else {
            let list = list(
                list_state.clone(),
                move |ix, _window, _cx| {
                    let theme = list_theme.clone();
                    let item = match messages.get(ix) {
                        Some(MessageItem::User(card)) => card.render(&theme),
                        Some(MessageItem::Assistant(view)) => {
                            let handlers = bars.get(&view.id);
                            view.render_with_action_bar(&theme, handlers, token_count.clone())
                        }
                        Some(MessageItem::SystemNotice(notice)) => div()
                            .w_full()
                            .flex()
                            .justify_center()
                            .text_size(px(MockupTypeScale::HINT_SIZE))
                            .line_height(px(MockupTypeScale::CHIP_LINE_HEIGHT))
                            .text_color(theme.mockup_text_dim)
                            .child(notice.clone()),
                        None => div(),
                    };
                    if ix + 1 >= count {
                        item.into_any_element()
                    } else {
                        item.pb(px(FEED_GAP)).into_any_element()
                    }
                },
            );
            column = column.child(list.flex_1());
        }

        let area = area.child(column).child(fade_overlay(theme));

        root.child(area)
    }

    /// Legacy static render (no list, no handlers) used by the shell's
    /// non-session surfaces.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        self.render_inner(theme)
    }

    fn render_inner(&self, theme: &HermesTheme) -> Div {
        let mut feed_container = div()
            .flex_1()
            .min_h_0()
            .w_full()
            .flex()
            .flex_col()
            .gap(px(FEED_GAP))
            .p_4()
            .overflow_y_hidden();

        if let Some(notice) = &self.reconnect_notice {
            feed_container = feed_container.child(
                div()
                    .w_full()
                    .flex()
                    .justify_center()
                    .child(
                        div()
                            .rounded(px(8.0))
                            .bg(theme.mockup_diff_del_bg)
                            .px_3()
                            .py(px(6.0))
                            .text_size(px(MockupTypeScale::HINT_SIZE))
                            .text_color(theme.rose_fg())
                            .child(notice.clone()),
                    ),
            );
        }

        if self.messages.is_empty() {
            if self.is_connecting {
                return feed_container.child(
                    div()
                        .flex_1()
                        .flex()
                        .flex_col()
                        .items_center()
                        .justify_center()
                        .gap(px(10.0))
                        .child(
                            div()
                                .font_family(theme.font_mono()[0].clone())
                                .text_size(px(MockupTypeScale::SPINNER_SIZE))
                                .text_color(theme.accent_blue())
                                .child(motion::spinner_frame(Duration::ZERO).to_string()),
                        )
                        .child(
                            div()
                                .text_size(px(13.0))
                                .text_color(theme.mockup_text_muted)
                                .child("Connecting to session…"),
                        ),
                );
            }
            return feed_container.child(
                div()
                    .flex_1()
                    .flex()
                    .flex_col()
                    .items_center()
                    .justify_center()
                    .gap(px(8.0))
                    .child(
                        div()
                            .text_size(px(MockupTypeScale::BASE_SIZE))
                            .line_height(px(MockupTypeScale::BASE_LINE_HEIGHT))
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(theme.mockup_text_bright)
                            .child("HEX Autonomous AI Workbench"),
                    )
                    .child(
                        div()
                            .text_size(px(13.0))
                            .text_color(theme.mockup_text_dim)
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
                    feed_container = feed_container.child(view.render(theme));
                }
                MessageItem::SystemNotice(notice) => {
                    feed_container = feed_container.child(
                        div()
                            .w_full()
                            .flex()
                            .justify_center()
                            .text_size(px(MockupTypeScale::HINT_SIZE))
                            .line_height(px(MockupTypeScale::CHIP_LINE_HEIGHT))
                            .text_color(theme.mockup_text_dim)
                            .child(notice.clone()),
                    );
                }
            }
        }

        feed_container
    }
}

/// The 28px canvas-to-transparent fade painted across the feed's top edge.
/// Painted last so it sits above the scrolling content; it has no hitbox, so
/// clicks and scroll wheel pass through to the feed.
pub fn fade_overlay(theme: &HermesTheme) -> Div {
    let (opaque, transparent) = feed_fade_stops(theme.mockup_canvas);
    div()
        .absolute()
        .top_0()
        .left_0()
        .right_0()
        .h(px(FEED_FADE_HEIGHT))
        .bg(linear_gradient(180.0, linear_color_stop(opaque, 0.0), linear_color_stop(transparent, 1.0)))
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

    #[test]
    fn test_feed_fade_stops_run_canvas_to_transparent() {
        let canvas = gpui::rgb(0x141414);
        let (opaque, transparent) = feed_fade_stops(canvas);
        let opaque_rgb: gpui::Rgba = gpui::Rgba::from(opaque);
        let transparent_rgb: gpui::Rgba = gpui::Rgba::from(transparent);
        assert_eq!((opaque_rgb.r * 255.0).round() as u8, 0x14);
        assert_eq!((opaque_rgb.g * 255.0).round() as u8, 0x14);
        assert_eq!((opaque_rgb.b * 255.0).round() as u8, 0x14);
        assert!((opaque.a - 1.0).abs() < 1e-4);
        assert!(transparent.a.abs() < 1e-4);
        // Same hue, only alpha differs.
        assert_eq!(opaque_rgb.r, transparent_rgb.r);
        assert_eq!(opaque_rgb.g, transparent_rgb.g);
        assert_eq!(opaque_rgb.b, transparent_rgb.b);
    }
}

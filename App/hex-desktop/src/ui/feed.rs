//! Mockup conversation feed for HEX Desktop (parity plan §2.4 / §6 P4).
//!
//! Mirrors the DesktopSession mockup's feed: a 560px column (gap 20,
//! padding-top 10, padding-bottom 6) that bottoms out at the newest turn,
//! inside a full-height area with a 28px canvas fade across its top edge.
//! Scrolling is a GPUI `list` anchored to `ListAlignment::Bottom`, so short
//! conversations hug the composer and long ones scroll internally.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::icon_svg;
use crate::ui::message::{ActionBarHandlers, AssistantMessageView, UserMessageCard};
use crate::ui::motion;
use crate::ui::tool_card::{tool_icon, ToolCallCard, ToolStatus};

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
    Approval(ApprovalCard),
}

/// Operator decision state of an approval surfaced in the feed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ApprovalCardState {
    /// Awaiting the operator's decision (the modal holds the controls).
    Pending,
    /// Runtime confirmed the approval.
    Approved,
    /// Runtime confirmed the denial.
    Denied,
}

impl ApprovalCardState {
    pub fn label(&self) -> &'static str {
        match self {
            ApprovalCardState::Pending => "Needs you",
            ApprovalCardState::Approved => "Approved",
            ApprovalCardState::Denied => "Denied",
        }
    }

    pub fn color(&self, theme: &HermesTheme) -> Hsla {
        match self {
            ApprovalCardState::Pending => theme.warning_amber().into(),
            ApprovalCardState::Approved => theme.sage_fg().into(),
            ApprovalCardState::Denied => theme.rose_fg().into(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ApprovalVisual {
    pub from_state: ApprovalCardState,
    pub state: ApprovalCardState,
    pub from_color: Hsla,
}

impl ApprovalVisual {
    pub fn color(&self, theme: &HermesTheme, progress: f32) -> Hsla {
        motion::lerp_hsla(self.from_color, self.state.color(theme), progress)
    }
}

/// An in-feed mirror of a tool approval request. The interactive decision
/// controls stay in the approval modal; the card keeps the stream honest
/// about what was asked and how it resolved.
#[derive(Debug, Clone, PartialEq)]
pub struct ApprovalCard {
    pub approval_id: String,
    pub tool_name: String,
    /// Bounded parameters / command summary from the runtime.
    pub summary: String,
    pub risk_level: String,
    pub state: ApprovalCardState,
}

impl ApprovalCard {
    pub fn new(
        approval_id: impl Into<String>,
        tool_name: impl Into<String>,
        summary: impl Into<String>,
        risk_level: impl Into<String>,
    ) -> Self {
        Self {
            approval_id: approval_id.into(),
            tool_name: tool_name.into(),
            summary: summary.into(),
            risk_level: risk_level.into(),
            state: ApprovalCardState::Pending,
        }
    }

    /// Renders the approval card in the mockup token language (derived —
    /// approvals-as-feed-cards are not in the mockup): `#171717` surface,
    /// `#262626` border, amber risk accent while pending.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        self.render_card(theme, self.static_label(theme))
    }

    pub fn render_motion(&self, theme: &HermesTheme, visual: ApprovalVisual, progress: f32) -> Div {
        let progress = progress.clamp(0.0, 1.0);
        let label = if visual.from_state == visual.state {
            self.static_label(theme)
        } else {
            div()
                .relative()
                .w(px(64.0))
                .h(px(16.0))
                .flex_none()
                .child(
                    div()
                        .absolute()
                        .left_0()
                        .whitespace_nowrap()
                        .text_size(px(crate::ui::tool_card::TOOL_META_SIZE))
                        .text_color(visual.color(theme, progress))
                        .opacity(progress)
                        .child(visual.state.label()),
                )
                .child(
                    div()
                        .absolute()
                        .left_0()
                        .whitespace_nowrap()
                        .text_size(px(crate::ui::tool_card::TOOL_META_SIZE))
                        .text_color(visual.color(theme, progress))
                        .opacity(1.0 - progress)
                        .child(visual.from_state.label()),
                )
        };
        self.render_card(theme, label)
    }

    fn static_label(&self, theme: &HermesTheme) -> Div {
        div()
            .text_size(px(crate::ui::tool_card::TOOL_META_SIZE))
            .text_color(self.state.color(theme))
            .child(self.state.label())
    }

    fn render_card(&self, theme: &HermesTheme, label: Div) -> Div {
        div()
            .w_full()
            .flex()
            .flex_col()
            .rounded(px(crate::ui::tool_card::TOOL_CARD_RADIUS))
            .border_1()
            .border_color(theme.mockup_border_strong)
            .bg(theme.mockup_surface)
            .overflow_hidden()
            .child(
                div()
                    .w_full()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap_2()
                    .px(px(16.0))
                    .py(px(8.0))
                    .child(
                        icon_svg(tool_icon(&self.tool_name), 14.0)
                            .text_color(theme.mockup_text_muted),
                    )
                    .child(
                        div()
                            .font_family(theme.font_mono()[0].clone())
                            .text_size(px(crate::ui::tool_card::TOOL_NAME_SIZE))
                            .font_weight(FontWeight::MEDIUM)
                            .text_color(theme.mockup_text)
                            .child(self.tool_name.clone()),
                    )
                    .child(div().flex_1())
                    .child(label),
            )
            .child(
                div()
                    .font_family(theme.font_mono()[0].clone())
                    .text_size(px(crate::ui::tool_card::TOOL_BODY_SIZE))
                    .line_height(px(crate::ui::tool_card::TOOL_BODY_LINE_HEIGHT))
                    .px(px(16.0))
                    .pb(px(10.0))
                    .whitespace_nowrap()
                    .text_color(theme.mockup_text_muted)
                    .child(self.summary.clone()),
            )
    }
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
    pending_entrances: HashSet<usize>,
    revision: u64,
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
            pending_entrances: HashSet::new(),
            revision: 0,
        }
    }

    pub fn with_messages(messages: Vec<MessageItem>) -> Self {
        Self {
            messages,
            auto_scroll_bottom: true,
            is_streaming: false,
            is_connecting: false,
            reconnect_notice: None,
            pending_entrances: HashSet::new(),
            revision: 0,
        }
    }

    /// Clones the feed for an outgoing transition without replaying row entrances.
    pub fn snapshot_without_entrances(&self) -> Self {
        let mut snapshot = self.clone();
        snapshot.pending_entrances.clear();
        snapshot
    }

    pub fn revision(&self) -> u64 {
        self.revision
    }

    pub(crate) fn mark_changed(&mut self) {
        self.revision = self.revision.wrapping_add(1);
    }

    pub fn streaming_row_index(&self) -> Option<usize> {
        self.messages.iter().rposition(|item| {
            matches!(
                item,
                MessageItem::Assistant(assistant) if assistant.is_streaming
            )
        })
    }

    fn mark_entrance(&mut self) {
        self.mark_changed();
        self.pending_entrances
            .insert(self.messages.len().saturating_sub(1));
    }

    /// Removes a row while preserving entrance markers for shifted rows.
    pub fn remove_message_at(&mut self, index: usize) -> MessageItem {
        let removed = self.messages.remove(index);
        self.mark_changed();
        let mut shifted = HashSet::with_capacity(self.pending_entrances.len());
        for pending in self.pending_entrances.drain() {
            if pending < index {
                shifted.insert(pending);
            } else if pending > index {
                shifted.insert(pending - 1);
            }
        }
        self.pending_entrances = shifted;
        removed
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
        self.mark_entrance();
        self.auto_scroll_bottom = true;
        id
    }

    /// Adds a user message carrying attached file names (chip display).
    pub fn add_user_message_with_attachments(
        &mut self,
        content: impl Into<String>,
        attachments: Vec<String>,
    ) -> String {
        let id = format!("msg_{}", chrono::Utc::now().timestamp_millis());
        let card = UserMessageCard::new(id.clone(), content).with_attachments(attachments);
        self.messages.push(MessageItem::User(card));
        self.mark_entrance();
        self.auto_scroll_bottom = true;
        id
    }

    pub fn add_assistant_message(&mut self, content: impl Into<String>) -> String {
        let id = format!("msg_{}", chrono::Utc::now().timestamp_millis());
        let view = AssistantMessageView::new(id.clone(), content);
        self.messages.push(MessageItem::Assistant(view));
        self.mark_entrance();
        self.auto_scroll_bottom = true;
        id
    }

    pub fn add_system_notice(&mut self, notice: impl Into<String>) {
        self.messages.push(MessageItem::SystemNotice(notice.into()));
        self.mark_entrance();
    }

    pub fn start_assistant_streaming(&mut self) -> String {
        let id = format!("msg_{}", chrono::Utc::now().timestamp_millis());
        let view = AssistantMessageView::new(id.clone(), "").with_streaming(true);
        self.messages.push(MessageItem::Assistant(view));
        self.mark_entrance();
        self.is_streaming = true;
        self.auto_scroll_bottom = true;
        id
    }

    pub fn append_streaming_chunk(&mut self, chunk: &str) {
        if let Some(MessageItem::Assistant(ref mut assistant)) = self.messages.last_mut() {
            assistant.content.push_str(chunk);
            self.mark_changed();
            self.auto_scroll_bottom = true;
        }
    }

    pub fn finish_streaming(&mut self) {
        if let Some(MessageItem::Assistant(ref mut assistant)) = self.messages.last_mut() {
            assistant.is_streaming = false;
        }
        self.is_streaming = false;
        self.mark_changed();
    }

    pub fn add_tool_call(&mut self, tool: ToolCallCard) {
        if let Some(MessageItem::Assistant(ref mut assistant)) = self.messages.last_mut() {
            assistant.tool_calls.push(tool);
            self.mark_changed();
        } else {
            let mut view = AssistantMessageView::new(
                format!("msg_{}", chrono::Utc::now().timestamp_millis()),
                "",
            );
            view.tool_calls.push(tool);
            self.messages.push(MessageItem::Assistant(view));
            self.mark_entrance();
        }
    }

    pub fn update_tool_result(
        &mut self,
        call_id: &str,
        output: &str,
        is_error: bool,
        diff: Option<&str>,
    ) {
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
                        if let Some(patch) = diff {
                            tool.diff = Some(crate::ui::diff_view::DiffView::from_patch(patch));
                        }
                        self.mark_changed();
                        return;
                    }
                }
            }
        }
    }

    /// Appends an approval card to the feed (approvals surface in the stream
    /// as well as the decision modal).
    pub fn add_approval_card(&mut self, card: ApprovalCard) {
        self.messages.push(MessageItem::Approval(card));
        self.mark_entrance();
        self.auto_scroll_bottom = true;
    }

    /// Marks every card matching `approval_id` with the confirmed outcome.
    /// Returns true when at least one card was updated.
    pub fn update_approval_state(&mut self, approval_id: &str, state: ApprovalCardState) -> bool {
        let mut updated = false;
        for msg in self.messages.iter_mut() {
            if let MessageItem::Approval(card) = msg {
                if card.approval_id == approval_id {
                    card.state = state;
                    updated = true;
                }
            }
        }
        if updated {
            self.mark_changed();
        }
        updated
    }

    pub fn clear(&mut self) {
        self.messages.clear();
        self.pending_entrances.clear();
        self.is_streaming = false;
        self.mark_changed();
    }

    /// Renders the full feed area: the 560px bottom-anchored scroll column,
    /// the 28px top fade, and the notice / empty / connecting states.
    ///
    /// `bars` carries per-assistant click handlers keyed by message id;
    /// `token_count` is the honest gauge total ("24.5k tokens") or `None`
    /// while unknown (hidden).
    pub fn render_scrolled(
        &mut self,
        theme: &HermesTheme,
        list_state: &ListState,
        bars: HashMap<String, ActionBarHandlers>,
        token_count: Option<String>,
        cx: &mut App,
        motion_owner: EntityId,
        approval_visuals: &HashMap<String, ApprovalVisual>,
    ) -> Div {
        let pending_entrances = std::mem::take(&mut self.pending_entrances);
        let entrance_policy = motion::MotionSystem::policy(cx);
        let approval_visuals = approval_visuals.clone();
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
                div().w_full().flex().justify_center().pb_2().child(
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
            let list = list(list_state.clone(), move |ix, window, cx| {
                let theme = list_theme.clone();
                let entrance_id = match messages.get(ix) {
                    Some(MessageItem::User(card)) => format!("user_{}", card.id),
                    Some(MessageItem::Assistant(view)) => format!("assistant_{}", view.id),
                    Some(MessageItem::SystemNotice(notice)) => {
                        format!("notice_{}_{ix}", motion::slot(notice))
                    }
                    Some(MessageItem::Approval(card)) => format!("approval_{}", card.approval_id),
                    None => format!("empty_{ix}"),
                };
                let item: Div = match messages.get(ix) {
                    Some(MessageItem::User(card)) => card.render(&theme),
                    Some(MessageItem::Assistant(view)) => {
                        let handlers = bars.get(&view.id);
                        view.render_with_action_bar_motion(
                            &theme,
                            handlers,
                            token_count.clone(),
                            window,
                            cx,
                            motion_owner,
                        )
                    }
                    Some(MessageItem::SystemNotice(notice)) => div()
                        .w_full()
                        .flex()
                        .justify_center()
                        .text_size(px(MockupTypeScale::HINT_SIZE))
                        .line_height(px(MockupTypeScale::CHIP_LINE_HEIGHT))
                        .text_color(theme.mockup_text_dim)
                        .child(notice.clone()),
                    Some(MessageItem::Approval(card)) => {
                        if let Some(visual) = approval_visuals.get(&card.approval_id) {
                            let key = motion::SpringKey::for_slot(
                                motion_owner,
                                "approval_state",
                                motion::slot(&card.approval_id),
                            );
                            card.render_motion(
                                &theme,
                                *visual,
                                motion::MotionSystem::value_or(cx, key, 1.0),
                            )
                        } else {
                            card.render(&theme)
                        }
                    }
                    None => div(),
                };
                let item: AnyElement = if pending_entrances.contains(&ix) {
                    let policy = entrance_policy;
                    let reduced = policy.is_reduced();
                    let preset = policy.preset(motion::MotionKind::Opacity, motion::SMOOTH);
                    let duration = policy.duration(
                        motion::MotionKind::Opacity,
                        std::time::Duration::from_secs_f32(motion::SMOOTH.duration),
                    );
                    item.with_animation(
                        ElementId::Name(SharedString::from(format!("feed_entrance_{entrance_id}"))),
                        Animation::new(duration),
                        move |element, phase| {
                            let progress = motion::spring_easing(preset, phase);
                            if reduced {
                                element.opacity(progress)
                            } else {
                                element
                                    .opacity(progress)
                                    .mt(px(motion::REVEAL_RISE * (1.0 - progress)))
                            }
                        },
                    )
                    .into_any_element()
                } else {
                    item.into_any_element()
                };
                if ix + 1 >= count {
                    item
                } else {
                    div().pb(px(FEED_GAP)).child(item).into_any_element()
                }
            });
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
                div().w_full().flex().justify_center().child(
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
                MessageItem::Approval(card) => {
                    feed_container = feed_container.child(card.render(theme));
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
        .bg(linear_gradient(
            180.0,
            linear_color_stop(opaque, 0.0),
            linear_color_stop(transparent, 1.0),
        ))
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
    fn streaming_revision_tracks_only_the_active_row() {
        let mut feed = MessageFeed::new();
        feed.add_user_message("Hello");
        let initial_revision = feed.revision();
        feed.start_assistant_streaming();
        assert_eq!(feed.streaming_row_index(), Some(1));
        let streaming_revision = feed.revision();
        assert!(streaming_revision > initial_revision);

        feed.append_streaming_chunk("First");
        assert!(feed.revision() > streaming_revision);
        assert_eq!(feed.streaming_row_index(), Some(1));

        feed.finish_streaming();
        assert_eq!(feed.streaming_row_index(), None);
    }

    #[test]
    fn test_removing_a_placeholder_shifts_pending_entrances() {
        let mut feed = MessageFeed::new();
        feed.add_user_message("Hello");
        feed.start_assistant_streaming();
        feed.add_system_notice("No response");
        assert_eq!(feed.pending_entrances, HashSet::from([0, 1, 2]));

        feed.remove_message_at(1);

        assert_eq!(feed.len(), 2);
        assert_eq!(feed.pending_entrances, HashSet::from([0, 1]));
        assert!(matches!(feed.messages[1], MessageItem::SystemNotice(_)));
    }

    #[test]
    fn test_only_new_feed_rows_request_an_entrance() {
        let mut feed = MessageFeed::new();
        feed.add_user_message("Hello");
        feed.start_assistant_streaming();
        assert_eq!(feed.pending_entrances, HashSet::from([0, 1]));

        feed.append_streaming_chunk("first");
        feed.append_streaming_chunk(" second");
        feed.finish_streaming();
        assert_eq!(feed.pending_entrances, HashSet::from([0, 1]));

        let restored = MessageFeed::with_messages(feed.messages.clone());
        assert!(restored.pending_entrances.is_empty());

        feed.pending_entrances.clear();
        feed.append_streaming_chunk(" third");
        assert!(feed.pending_entrances.is_empty());
    }

    #[test]
    fn test_crossfade_snapshot_does_not_replay_entrances() {
        let mut feed = MessageFeed::new();
        feed.add_user_message("Hello");
        feed.start_assistant_streaming();
        assert_eq!(feed.pending_entrances, HashSet::from([0, 1]));

        let snapshot = feed.snapshot_without_entrances();

        assert!(snapshot.pending_entrances.is_empty());
        assert_eq!(snapshot.messages, feed.messages);
        assert_eq!(feed.pending_entrances, HashSet::from([0, 1]));
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

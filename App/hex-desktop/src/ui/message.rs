//! Message views for User queries and Assistant responses in HEX Desktop.
//!
//! Mirrors the mockup conversation thread (`docs/design/mockups-desktop`):
//! - User bubble: right-aligned `#1e1e1e` pill, radius 18, padding 10px 18px,
//!   max-width 440 — no border, avatar, header, or timestamp.
//! - Assistant: plain 15/26 `#d9d9d9` prose with bold `#fff` lead-ins and
//!   inline `.cd` code chips (mono 12.5 on `#202020`), a mockup action bar
//!   (Copy / thumbs / Retry, 13.5px `#858585`, no emoji), and a collapsed
//!   "Changes" button that expands into the turn's diff cards.

use std::ops::Range;
use std::rc::Rc;

use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::feed::FEED_GAP;
use crate::ui::motion;
use crate::ui::reasoning::ThinkingDisclosure;
use crate::ui::tool_card::ToolCallCard;

/// Handler signature for the interactive message action bar.
pub type ClickHandler = Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>;

/// Per-assistant action handlers wired by `ChatView`. A `None` field renders
/// the corresponding button without a click action (legacy static path).
#[derive(Clone, Default)]
pub struct ActionBarHandlers {
    pub copy: Option<ClickHandler>,
    pub retry: Option<ClickHandler>,
    pub changes: Option<ClickHandler>,
}

/// Gap between action-bar buttons (mockup `gap:18px`).
pub const ACTION_BAR_GAP: f32 = 18.0;
/// Gap between a button's icon and its label (mockup `gap:6px`).
pub const ACTION_BUTTON_GAP: f32 = 6.0;
/// Action-bar icon size (mockup `width:15`).
pub const ACTION_ICON_SIZE: f32 = 15.0;
/// Retry chevron size (mockup `width:12`).
pub const ACTION_CHEV_SIZE: f32 = 12.0;
/// Changes-card icon size (mockup `width:16`).
pub const CHANGES_ICON_SIZE: f32 = 16.0;
/// Changes-card chevron size (mockup `width:14`).
pub const CHANGES_CHEV_SIZE: f32 = 14.0;
/// Changes-card padding (mockup `padding:12px 16px`).
pub const CHANGES_PADDING_Y: f32 = 12.0;
pub const CHANGES_PADDING_X: f32 = 16.0;
/// Changes-card gap between label and summary (mockup `gap:10px`).
pub const CHANGES_GAP: f32 = 10.0;
/// Changes-card corner radius (mockup `border-radius:14px`).
pub const CHANGES_RADIUS: f32 = 14.0;
/// User bubble corner radius (mockup `border-radius:18px`).
pub const USER_BUBBLE_RADIUS: f32 = 18.0;
/// User bubble padding (mockup `padding:10px 18px`).
pub const USER_BUBBLE_PADDING_Y: f32 = 10.0;
pub const USER_BUBBLE_PADDING_X: f32 = 18.0;
/// User bubble max width (mockup `max-width:440px`).
pub const USER_BUBBLE_MAX_WIDTH: f32 = 440.0;

/// Mockup action-bar labels — plain text, never emoji (D5).
pub const ACTION_BAR_LABELS: [&str; 2] = ["Copy", "Retry"];

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
        } else if let Some(rest) = trimmed.strip_prefix("```") {
            if !prose.is_empty() {
                segments.push(ContentSegment::Prose(std::mem::take(&mut prose)));
            }
            code = Some((rest.trim().to_string(), String::new()));
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

// --------------------------------------------------------------------------
// Inline rich text (bold lead-ins + `.cd` code chips)
// --------------------------------------------------------------------------

/// The flavour of an inline rich-text span in assistant prose.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RichSpanKind {
    Bold,
    Code,
}

/// One styled span within the flattened rich text; ranges index the flattened
/// string (markers stripped).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RichSpan {
    pub kind: RichSpanKind,
    pub range: Range<usize>,
}

/// Parses assistant prose into flattened text plus bold/`code` spans.
/// `**bold**` and `` `code` `` markers are stripped; unmatched markers stay
/// literal (streaming), and spans may nest (code inside bold).
pub fn parse_inline_rich(content: &str) -> (String, Vec<RichSpan>) {
    let mut out = String::with_capacity(content.len());
    let mut spans: Vec<RichSpan> = Vec::new();
    let bytes = content.as_bytes();
    let mut i = 0usize;
    let mut open_bold: Option<usize> = None;
    let mut open_code: Option<usize> = None;

    while i < bytes.len() {
        let bold_marker = bytes[i] == b'*' && i + 1 < bytes.len() && bytes[i + 1] == b'*';
        let code_marker = bytes[i] == b'`';
        // Consume an opener only when it has a closer; otherwise it stays
        // literal so a half-streamed message never loses characters.
        let close_bold = bold_marker && (open_bold.is_some() || find_marker(content, i + 2, "**").is_some());
        let close_code = code_marker && (open_code.is_some() || content[i + 1..].contains('`'));

        if close_bold {
            if let Some(start) = open_bold.take() {
                if out.len() > start {
                    spans.push(RichSpan {
                        kind: RichSpanKind::Bold,
                        range: start..out.len(),
                    });
                }
            } else {
                open_bold = Some(out.len());
            }
            i += 2;
        } else if close_code {
            if let Some(start) = open_code.take() {
                if out.len() > start {
                    spans.push(RichSpan {
                        kind: RichSpanKind::Code,
                        range: start..out.len(),
                    });
                }
            } else {
                open_code = Some(out.len());
            }
            i += 1;
        } else {
            let ch = content[i..].chars().next().unwrap();
            out.push(ch);
            i += ch.len_utf8();
        }
    }

    (out, spans)
}

fn find_marker(content: &str, from: usize, marker: &str) -> Option<usize> {
    if from > content.len() {
        return None;
    }
    content[from..].find(marker).map(|rel| from + rel)
}

/// Typography and palette used to shape [`rich_text_runs`]: plain prose 15px
/// sans `#d9d9d9`, bold weight 700 `#fff`, code mono 12.5px `#e6e6e6` on the
/// `#202020` chip background.
pub struct RichTextStyle {
    pub base_font: Font,
    pub mono_font: Font,
    pub base_color: Hsla,
    pub bold_color: Hsla,
    pub code_color: Hsla,
    pub code_bg: Hsla,
}

impl RichTextStyle {
    /// The mockup's prose style resolved from the theme.
    pub fn mockup(theme: &HermesTheme) -> Self {
        Self {
            base_font: gpui::font(theme.font_sans()[0].clone()),
            mono_font: gpui::font(theme.font_mono()[0].clone()),
            base_color: theme.mockup_text_base.into(),
            bold_color: rgb(0xffffff).into(),
            code_color: theme.mockup_text.into(),
            code_bg: theme.mockup_inline_code_bg.into(),
        }
    }
}

/// Maps rich spans to shaped `TextRun`s covering exactly `text`.
pub fn rich_text_runs(
    text: &str,
    spans: &[RichSpan],
    style: &RichTextStyle,
) -> Vec<TextRun> {
    let style_at = |pos: usize| -> (bool, bool) {
        let bold = spans
            .iter()
            .any(|s| s.kind == RichSpanKind::Bold && s.range.contains(&pos));
        let code = spans
            .iter()
            .any(|s| s.kind == RichSpanKind::Code && s.range.contains(&pos));
        (bold, code)
    };

    let make_run = |len: usize, bold: bool, code: bool| -> TextRun {
        let mut font = style.base_font.clone();
        let mut color = style.base_color;
        let mut background_color = None;
        if code {
            font = style.mono_font.clone();
            color = style.code_color;
            background_color = Some(style.code_bg);
        }
        if bold {
            font = font.bold();
            if !code {
                color = style.bold_color;
            }
        }
        TextRun {
            len,
            font,
            color,
            background_color,
            underline: None,
            strikethrough: None,
        }
    };

    let mut runs = Vec::new();
    let mut current: Option<(bool, bool)> = None;
    let mut run_len = 0usize;
    for (idx, ch) in text.char_indices() {
        let style = style_at(idx);
        match current {
            None => current = Some(style),
            Some(prev) if prev != style => {
                runs.push(make_run(run_len, prev.0, prev.1));
                run_len = 0;
                current = Some(style);
            }
            _ => {}
        }
        run_len += ch.len_utf8();
    }
    if let Some(prev) = current {
        if run_len > 0 {
            runs.push(make_run(run_len, prev.0, prev.1));
        }
    }
    runs
}

/// Renders one assistant prose paragraph at the mockup's 15/26 `#d9d9d9` with
/// bold lead-ins and inline code chips.
pub fn render_rich_prose(content: &str, theme: &HermesTheme) -> Div {
    let (text, spans) = parse_inline_rich(content);
    let runs = rich_text_runs(&text, &spans, &RichTextStyle::mockup(theme));
    div()
        .w_full()
        .text_size(px(MockupTypeScale::BASE_SIZE))
        .line_height(px(MockupTypeScale::BASE_LINE_HEIGHT))
        .text_color(theme.mockup_text_base)
        .child(StyledText::new(text).with_runs(runs))
}

/// Renders one fenced code block: mono 12.5/21 on `#101010` with a `#222`
/// border, no language label (mockup parity).
fn render_code_block(code: &str, theme: &HermesTheme) -> Div {
    div()
        .w_full()
        .rounded(px(8.0))
        .border_1()
        .border_color(theme.mockup_border)
        .bg(theme.mockup_sidebar)
        .px(px(12.0))
        .py(px(10.0))
        .font_family(theme.font_mono()[0].clone())
        .text_size(px(MockupTypeScale::DIFF_SIZE))
        .line_height(px(MockupTypeScale::DIFF_LINE_HEIGHT))
        .text_color(theme.mockup_text_base)
        .whitespace_nowrap()
        .child(code.to_string())
}

// --------------------------------------------------------------------------
// Action bar + Changes card
// --------------------------------------------------------------------------

/// An action-bar SVG icon: gpui does not flow container text color into svg
/// children, so the icon carries its own tint and brightens via group_hover
/// on the enclosing button group (hover restyle reaches the stroke).
fn action_icon(name: &str, size: f32, group: &str, tint: Rgba, hover_tint: Rgba) -> Svg {
    svg()
        .path(format!("icons/{name}.svg"))
        .w(px(size))
        .h(px(size))
        .flex_none()
        .text_color(tint)
        .group_hover(SharedString::from(group.to_string()), move |s| {
            s.text_color(hover_tint)
        })
}

/// One action-bar button: flex row (icon, optional label, optional trailing
/// chevron) at 13.5px `#858585`; becomes a cursor-pointer target when a
/// handler is wired.
fn action_button(
    theme: &HermesTheme,
    key: String,
    handler: Option<&ClickHandler>,
    icon: &str,
    label: Option<&str>,
    trailing_icon: Option<(&str, f32)>,
) -> AnyElement {
    let group_name = format!("actb_{key}");
    let mut button = div()
        .group(SharedString::from(group_name.clone()))
        .flex()
        .flex_row()
        .items_center()
        .gap(px(ACTION_BUTTON_GAP))
        .text_size(px(MockupTypeScale::ACTION_BAR_SIZE))
        .text_color(theme.mockup_text_dim)
        .hover(|s| s.text_color(theme.mockup_text))
        .child(action_icon(
            icon,
            ACTION_ICON_SIZE,
            &group_name,
            theme.mockup_text_dim,
            theme.mockup_text,
        ));
    if let Some(label) = label {
        button = button.child(label.to_string());
    }
    if let Some((icon, size)) = trailing_icon {
        button = button.child(action_icon(
            icon,
            size,
            &group_name,
            theme.mockup_text_dim,
            theme.mockup_text,
        ));
    }
    match handler {
        Some(handler) => {
            let handler = handler.clone();
            button
                .id(SharedString::from(key))
                .cursor_pointer()
                .on_click(move |event, window, cx| handler(event, window, cx))
                .into_any_element()
        }
        None => button.into_any_element(),
    }
}

/// Builds the mockup action bar for one assistant message: Copy (files icon +
/// label), thumbs-up, thumbs-down (rotation baked into the SVG), Retry
/// (rotate-ccw + label + chevron-down), then the real token count (13px,
/// hidden when unknown). No emoji anywhere (D5).
pub fn mockup_action_bar(
    theme: &HermesTheme,
    id: &str,
    token_count: Option<String>,
    handlers: &ActionBarHandlers,
) -> Div {
    let mut row = div()
        .w_full()
        .flex()
        .flex_row()
        .items_center()
        .gap(px(ACTION_BAR_GAP));

    row = row.child(action_button(
        theme,
        format!("action_copy_{id}"),
        handlers.copy.as_ref(),
        "files",
        Some(ACTION_BAR_LABELS[0]),
        None,
    ));
    row = row.child(action_button(
        theme,
        format!("action_up_{id}"),
        None,
        "thumbs-up",
        None,
        None,
    ));
    row = row.child(action_button(
        theme,
        format!("action_down_{id}"),
        None,
        "thumbs-down",
        None,
        None,
    ));
    row = row.child(action_button(
        theme,
        format!("action_retry_{id}"),
        handlers.retry.as_ref(),
        "retry",
        Some(ACTION_BAR_LABELS[1]),
        Some(("chevron-down", ACTION_CHEV_SIZE)),
    ));

    if let Some(tokens) = token_count {
        row = row.child(
            div()
                .text_size(px(MockupTypeScale::TOKEN_COUNT_SIZE))
                .text_color(theme.mockup_text_dim)
                .child(tokens),
        );
    }
    row
}

/// Formats the Changes-button summary: `"N files, +A −D"` with a U+2212 minus.
pub fn changes_summary(file_count: usize, adds: usize, dels: usize) -> String {
    format!(
        "{file_count} file{}, +{adds} \u{2212}{dels}",
        if file_count == 1 { "" } else { "s" }
    )
}

// --------------------------------------------------------------------------
// Message models
// --------------------------------------------------------------------------

/// Best-effort file label for the Changes card: the path from a diff, else the
/// tool's own name.
fn file_label_from_tool(tool: &ToolCallCard) -> String {
    if let Some(view) = tool.diff_view() {
        if let Some(file) = view.diff.files.first() {
            return file.path().to_string();
        }
    }
    tool.tool_name.clone()
}

/// User message query bubble.
#[derive(Debug, Clone, PartialEq)]
pub struct UserMessageCard {
    pub id: String,
    pub content: String,
    /// Names of files attached to this prompt (display-only chips).
    pub attachments: Vec<String>,
}

impl UserMessageCard {
    pub fn new(id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            content: content.into(),
            attachments: Vec::new(),
        }
    }

    pub fn with_attachments(mut self, attachments: Vec<String>) -> Self {
        self.attachments = attachments;
        self
    }

    /// Renders the exact mockup bubble: right-aligned `#1e1e1e` pill, radius
    /// 18, padding 10px 18px, max-width 440, 15/26 `#e6e6e6` — no border,
    /// avatar, header, or timestamp. Attached files render as small mono
    /// chips above the prose (derived — not in the mockup).
    pub fn render(&self, theme: &HermesTheme) -> Div {
        let mut bubble = div()
            .max_w(px(USER_BUBBLE_MAX_WIDTH))
            .rounded(px(USER_BUBBLE_RADIUS))
            .bg(theme.mockup_hover)
            .px(px(USER_BUBBLE_PADDING_X))
            .py(px(USER_BUBBLE_PADDING_Y))
            .text_size(px(MockupTypeScale::BASE_SIZE))
            .line_height(px(MockupTypeScale::BASE_LINE_HEIGHT))
            .text_color(theme.mockup_text);
        if !self.attachments.is_empty() {
            let mut chips = div()
                .w_full()
                .flex()
                .flex_wrap()
                .gap(px(6.0))
                .pb(px(6.0));
            for name in &self.attachments {
                chips = chips.child(
                    div()
                        .font_family(theme.font_mono()[0].clone())
                        .text_size(px(MockupTypeScale::HINT_SIZE))
                        .px(px(8.0))
                        .py(px(2.0))
                        .rounded(px(6.0))
                        .bg(theme.mockup_chip)
                        .text_color(theme.mockup_text_muted)
                        .child(file_display_name(name)),
                );
            }
            bubble = bubble.child(chips);
        }
        bubble = bubble.child(self.content.clone());
        div().w_full().flex().flex_row().justify_end().child(bubble)
    }
}

/// Strips a path down to its file name for chip display.
fn file_display_name(path: &str) -> String {
    path.rsplit(['/', '\\'])
        .next()
        .filter(|s| !s.is_empty())
        .unwrap_or(path)
        .to_string()
}

/// Assistant response view hosting prose, reasoning, tool calls, action bar,
/// and the Changes card.
#[derive(Debug, Clone, PartialEq)]
pub struct AssistantMessageView {
    pub id: String,
    pub content: String,
    pub reasoning: Option<ThinkingDisclosure>,
    pub tool_calls: Vec<ToolCallCard>,
    pub is_streaming: bool,
    pub model_name: String,
    /// Whether the collapsed Changes button is expanded into diff cards.
    pub changes_expanded: bool,
}

impl AssistantMessageView {
    pub fn new(id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            content: content.into(),
            reasoning: None,
            tool_calls: Vec::new(),
            is_streaming: false,
            // Unknown until the runtime reports it; never invent a model.
            model_name: String::new(),
            changes_expanded: false,
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

    /// Renders the static action bar (legacy `AppShell` path): mockup-styled
    /// buttons without click actions, no token count (unknown).
    fn render_action_bar(&self, theme: &HermesTheme) -> Div {
        mockup_action_bar(theme, &self.id, None, &ActionBarHandlers::default())
    }

    /// Renders the assistant message view aligned to the left.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        self.render_with_action_bar(theme, None, None)
    }

    /// Renders the message body with the mockup action bar wired through
    /// `handlers` (or the static bar when `None`), and the Changes card.
    pub fn render_with_action_bar(
        &self,
        theme: &HermesTheme,
        handlers: Option<&ActionBarHandlers>,
        token_count: Option<String>,
    ) -> Div {
        let mut card = div().w_full().flex().flex_col().gap(px(FEED_GAP));

        // 1. Thinking / Reasoning Fold
        if let Some(reasoning) = &self.reasoning {
            card = card.child(reasoning.render(theme));
        }

        // 2. Tool Call Cards
        for tool in &self.tool_calls {
            card = card.child(tool.render(theme));
        }

        // 3. Prose / Text Content (fenced code blocks render as structured
        // blocks; prose gets bold lead-ins and inline code chips).
        for segment in split_content_segments(&self.content) {
            match segment {
                ContentSegment::Prose(text) => {
                    if !text.trim().is_empty() {
                        card = card.child(render_rich_prose(&text, theme));
                    }
                }
                ContentSegment::Code { code, .. } => {
                    card = card.child(render_code_block(&code, theme));
                }
            }
        }

        // 4. Streaming braille spinner at the tail (mockup `.sp`).
        if self.is_streaming {
            card = card.child(self.render_streaming_spinner(theme));
        }

        // 5. Action bar + Changes button (only when not streaming).
        if !self.is_streaming {
            let bar = match handlers {
                Some(h) => mockup_action_bar(theme, &self.id, token_count, h),
                None => self.render_action_bar(theme),
            };
            card = card.child(bar);
            let changes_click = handlers.and_then(|h| h.changes.as_ref());
            if let Some(changes) = self.render_changes_button(theme, changes_click) {
                card = card.child(changes);
            }
        }

        card
    }

    /// The mockup `.sp` braille spinner (900ms steps(1)); reduced motion keeps
    /// the first frame static.
    fn render_streaming_spinner(&self, theme: &HermesTheme) -> AnyElement {
        let spinner = div()
            .font_family(theme.font_mono()[0].clone())
            .text_size(px(MockupTypeScale::SPINNER_SIZE))
            .text_color(theme.accent_blue());
        if motion::reduced_motion() {
            spinner
                .child(motion::spinner_frame(std::time::Duration::ZERO).to_string())
                .into_any_element()
        } else {
            spinner
                .with_animation(
                    ElementId::Name(SharedString::from(format!("feed_sp_{}", self.id))),
                    Animation::new(motion::SPIN_PERIOD).repeat(),
                    |el, phase| {
                        let elapsed =
                            std::time::Duration::from_secs_f32(phase * motion::SPIN_PERIOD.as_secs_f32());
                        el.child(motion::spinner_frame(elapsed).to_string())
                    },
                )
                .into_any_element()
        }
    }

    /// Collapsed "Changes" button per the mockup (border `#222`, radius 14,
    /// padding 12px 16px, gap 10): files icon 16, label `#e6e6e6`, summary
    /// "N files, +A −D" flex-1 `#9a9a9a`, chevron-down 14. Expanded, the
    /// turn's diff cards render below the button. Returns `None` when the
    /// turn touched no files.
    fn render_changes_button(&self, theme: &HermesTheme, on_toggle: Option<&ClickHandler>) -> Option<AnyElement> {
        let mut files: Vec<(String, usize, usize)> = Vec::new();
        for tool in &self.tool_calls {
            let Some(view) = tool.diff_view() else {
                continue;
            };
            let label = file_label_from_tool(tool);
            let entry = (label, view.total_additions(), view.total_deletions());
            if !files.iter().any(|f| f.0 == entry.0) {
                files.push(entry);
            }
        }
        if files.is_empty() {
            return None;
        }
        let file_count = files.len();
        let adds: usize = files.iter().map(|f| f.1).sum();
        let dels: usize = files.iter().map(|f| f.2).sum();

        let button = div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .gap(px(CHANGES_GAP))
            .px(px(CHANGES_PADDING_X))
            .py(px(CHANGES_PADDING_Y))
            .rounded(px(CHANGES_RADIUS))
            .border_1()
            .border_color(theme.mockup_border)
            .text_size(px(MockupTypeScale::CHANGES_SIZE))
            .text_color(theme.mockup_text_muted)
            .group("changes_hdr")
            .child(action_icon(
                "files",
                CHANGES_ICON_SIZE,
                "changes_hdr",
                theme.mockup_text_dim,
                theme.mockup_text,
            ))
            .child(
                div()
                    .text_color(theme.mockup_text)
                    .child("Changes"),
            )
            .child(div().flex_1().child(changes_summary(file_count, adds, dels)))
            .child(action_icon(
                "chevron-down",
                CHANGES_CHEV_SIZE,
                "changes_hdr",
                theme.mockup_text_dim,
                theme.mockup_text,
            ));
        let button: AnyElement = match on_toggle {
            Some(handler) => {
                let handler = handler.clone();
                button
                    .id(SharedString::from(format!("changes_{}", self.id)))
                    .cursor_pointer()
                    .on_click(move |event, window, cx| handler(event, window, cx))
                    .into_any_element()
            }
            None => button.into_any_element(),
        };

        let mut column = div().w_full().flex().flex_col().gap(px(8.0)).child(button);
        if self.changes_expanded {
            for tool in &self.tool_calls {
                if let Some(view) = tool.diff_view() {
                    column = column.child(view.render(theme));
                }
            }
        }
        Some(column.into_any_element())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::diff_view::DiffView;
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
        assert!(!a.changes_expanded, "Changes must start collapsed");
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
    fn test_parse_inline_rich_bold_and_code_boundaries() {
        let content = "**The change:** `GraphBuilder.edges()` now streams.";
        let (text, spans) = parse_inline_rich(content);
        assert_eq!(text, "The change: GraphBuilder.edges() now streams.");
        // The space between the bold lead-in and the code chip is preserved
        // (mockup: `<b>The change:</b> <span class="cd">…</span>`).
        assert_eq!(
            spans,
            vec![
                RichSpan {
                    kind: RichSpanKind::Bold,
                    range: 0..11,
                },
                RichSpan {
                    kind: RichSpanKind::Code,
                    range: 12..32,
                },
            ]
        );
    }

    #[test]
    fn test_parse_inline_rich_nested_code_inside_bold() {
        let (text, spans) = parse_inline_rich("**bold `code` tail** rest");
        assert_eq!(text, "bold code tail rest");
        let bold = spans.iter().find(|s| s.kind == RichSpanKind::Bold).unwrap();
        let code = spans.iter().find(|s| s.kind == RichSpanKind::Code).unwrap();
        assert_eq!(bold.clone().range, 0..14);
        assert_eq!(code.clone().range, 5..9);
    }

    #[test]
    fn test_parse_inline_rich_unmatched_markers_stay_literal() {
        for content in ["a ** b", "**bold", "tick ` x", "lone `"] {
            let (text, spans) = parse_inline_rich(content);
            assert_eq!(text, content, "markers must survive when unmatched");
            assert!(spans.is_empty(), "{content:?} must produce no spans");
        }
    }

    #[test]
    fn test_rich_text_runs_cover_text_with_styles() {
        let (text, spans) = parse_inline_rich("**Lead:** `x()` plain");
        let style = RichTextStyle {
            base_font: gpui::font("Apfel Grotezk"),
            mono_font: gpui::font("Geist Mono"),
            base_color: rgb(0xd9d9d9).into(),
            bold_color: rgb(0xffffff).into(),
            code_color: rgb(0xe6e6e6).into(),
            code_bg: rgb(0x202020).into(),
        };
        let runs = rich_text_runs(&text, &spans, &style);
        let total: usize = runs.iter().map(|r| r.len).sum();
        assert_eq!(total, text.len(), "runs must cover the flattened text");

        // First run = bold lead-in ("Lead:"), white + bold sans.
        assert_eq!(&text[..runs[0].len], "Lead:");
        assert_eq!(runs[0].font.weight, FontWeight::BOLD);
        let bold_rgb = gpui::Rgba::from(runs[0].color);
        assert!((bold_rgb.r * 255.0).round() as u8 == 0xff);
        // The code run uses the mono font with the chip background.
        let code_run = runs
            .iter()
            .find(|r| r.background_color.is_some())
            .expect("code run must carry the chip background");
        assert_eq!(code_run.font.family.as_ref(), "Geist Mono");
        let chip_bg = gpui::Rgba::from(code_run.background_color.unwrap());
        assert_eq!((chip_bg.r * 255.0).round() as u8, 0x20);
    }

    #[test]
    fn test_changes_summary_format() {
        assert_eq!(changes_summary(2, 22, 33), "2 files, +22 \u{2212}33");
        assert_eq!(changes_summary(1, 2, 3), "1 file, +2 \u{2212}3");
    }

    #[test]
    fn test_action_bar_labels_contain_no_emoji() {
        // D5: the action bar must never regress to "📋 Copy" / "🔄 Retry".
        for label in ACTION_BAR_LABELS {
            assert!(
                label.is_ascii(),
                "action-bar label {label:?} must be plain ASCII text"
            );
        }
        assert_eq!(ACTION_BAR_LABELS, ["Copy", "Retry"]);
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
        let streaming = AssistantMessageView::new("msg_2", "partial").with_streaming(true);
        let _ = streaming.render(&theme);
    }

    #[test]
    fn test_changes_button_smoke_expanded_and_collapsed() {
        let theme = HermesTheme::default();
        let tool = ToolCallCard::new("tc_1", "edit_file", "{}").with_diff(DiffView::from_patch(
            "--- a/src/main.rs\n+++ b/src/main.rs\n@@ -1,1 +1,1 @@\n-old\n+new\n",
        ));
        let collapsed = AssistantMessageView::new("msg_1", "").with_tool_call(tool.clone());
        let _ = collapsed.render(&theme);
        let expanded = AssistantMessageView::new("msg_1", "")
            .with_tool_call(tool)
            .with_streaming(false);
        let mut expanded = expanded;
        expanded.changes_expanded = true;
        let _ = expanded.render(&theme);
    }
}

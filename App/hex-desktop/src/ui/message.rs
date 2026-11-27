//! Message views for User queries and Assistant responses in HEX Desktop.
//!
//! Mirrors the mockup conversation thread (`docs/design/mockups-desktop`):
//! - User bubble: right-aligned `#1e1e1e` pill, radius 18, padding 10px 18px,
//!   max-width 440 — no border, avatar, header, or timestamp.
//! - Assistant: plain 15/26 `#d9d9d9` prose with bold `#fff` lead-ins and
//!   inline `.cd` code chips (mono 12.5 on `#202020`), a mockup action bar
//!   (Copy / thumbs / Retry, 13.5px `#858585`, no emoji), and a collapsed
//!   "Changes" button that expands into the turn's diff cards.

use std::rc::Rc;

use gpui::prelude::{InteractiveElement, StatefulInteractiveElement};
use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::feed::FEED_GAP;
use crate::ui::markdown::{parse_inline, parse_markdown, InlineToken, MarkdownBlock};
use crate::ui::motion::{self, RevealMotion, RevealValues};
use crate::ui::payload::{
    copy_button, preview_notice, preview_text, ScrollablePayload, TextCopyHandler,
    PAYLOAD_MAX_HEIGHT,
};
use crate::ui::reasoning::{DisclosureClickHandler, ThinkingDisclosure};
use crate::ui::tool_card::{ToolCallCard, ToolFoldClickHandler};

/// Handler signature for the interactive message action bar.
pub type ClickHandler = Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>;

/// Per-assistant action handlers wired by `ChatView`. A `None` field renders
/// the corresponding button without a click action (legacy static path).
#[derive(Clone, Default)]
pub struct ActionBarHandlers {
    pub copy: Option<ClickHandler>,
    pub retry: Option<ClickHandler>,
    pub changes: Option<ClickHandler>,
    pub reasoning: Option<DisclosureClickHandler>,
    pub tool_input: Option<ToolFoldClickHandler>,
    pub tool_output: Option<ToolFoldClickHandler>,
    pub copy_payload: Option<TextCopyHandler>,
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

// --------------------------------------------------------------------------
// Structured Markdown
// --------------------------------------------------------------------------

fn prose_base(theme: &HermesTheme, level: u8) -> Div {
    let size = match level {
        1 => 24.0,
        2 => 20.0,
        3 => 17.0,
        _ => MockupTypeScale::BASE_SIZE,
    };
    let mut prose = div()
        .text_size(px(size))
        .line_height(px(size * 1.45))
        .text_color(if level > 0 {
            theme.mockup_text
        } else {
            theme.mockup_text_base
        });
    if level > 0 {
        prose = prose.font_weight(FontWeight::BOLD);
    }
    prose
}

fn prose_style(theme: &HermesTheme, level: u8) -> Div {
    prose_base(theme, level).w_full().min_w_0()
}

fn split_inline_token_lines(tokens: &[InlineToken]) -> Vec<Vec<InlineToken>> {
    let mut lines: Vec<Vec<InlineToken>> = vec![Vec::new()];
    for token in tokens {
        let mut parts = token.text.split('\n');
        if let Some(first) = parts.next() {
            if !first.is_empty() {
                let mut part_tok = token.clone();
                part_tok.text = first.to_string();
                lines.last_mut().expect("non-empty lines").push(part_tok);
            }
        }
        for next_part in parts {
            let mut new_line = Vec::new();
            if !next_part.is_empty() {
                let mut part_tok = token.clone();
                part_tok.text = next_part.to_string();
                new_line.push(part_tok);
            }
            lines.push(new_line);
        }
    }
    if lines.len() > 1 && lines.last().is_some_and(Vec::is_empty) {
        lines.pop();
    }
    lines
}

fn build_text_runs(
    tokens: &[InlineToken],
    theme: &HermesTheme,
    default_bold: bool,
    default_color: Rgba,
) -> (String, Vec<TextRun>) {
    let sans_family = theme
        .font_sans()
        .first()
        .cloned()
        .unwrap_or_else(|| SharedString::from(".SystemFont"));
    let mono_family = theme
        .font_mono()
        .first()
        .cloned()
        .unwrap_or_else(|| SharedString::from("Menlo"));

    let mut full_text = String::new();
    let mut runs = Vec::new();
    for token in tokens {
        if token.text.is_empty() {
            continue;
        }
        full_text.push_str(&token.text);
        let is_bold = default_bold || token.bold;
        let mut font = if token.code {
            gpui::font(mono_family.clone())
        } else {
            gpui::font(sans_family.clone())
        };
        if is_bold {
            font.weight = FontWeight::BOLD;
        }
        if token.italic {
            font.style = FontStyle::Italic;
        }
        let color: Rgba = if token.link.is_some() {
            theme.accent_blue()
        } else if token.code || is_bold {
            theme.mockup_text
        } else {
            default_color
        };
        let underline = token.link.as_ref().map(|_| UnderlineStyle {
            thickness: px(1.0),
            color: Some(theme.accent_blue().into()),
            wavy: false,
        });
        runs.push(TextRun {
            len: token.text.len(),
            font,
            color: color.into(),
            background_color: if token.code {
                Some(theme.mockup_inline_code_bg.into())
            } else {
                None
            },
            underline,
            strikethrough: None,
        });
    }
    (full_text, runs)
}

fn split_inline_segments(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0usize;
    let mut saw_non_ws = false;
    for (idx, ch) in text.char_indices() {
        if ch.is_whitespace() {
            if saw_non_ws {
                // Keep trailing spaces attached to the word or standalone cleanly without mid-word wraps.
                continue;
            }
        } else if idx > start && text[start..idx].chars().all(char::is_whitespace) {
            out.push(&text[start..idx]);
            start = idx;
            saw_non_ws = true;
        } else if !saw_non_ws {
            saw_non_ws = true;
        } else if idx > start {
            let prev = text[start..idx].chars().next_back();
            if prev.is_some_and(char::is_whitespace) {
                out.push(&text[start..idx]);
                start = idx;
            }
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

fn style_inline_node(
    token: &InlineToken,
    theme: &HermesTheme,
    default_bold: bool,
    default_color: Rgba,
) -> Div {
    let is_bold = default_bold || token.bold;
    let mut view = div()
        .flex_none()
        .max_w_full()
        .whitespace_normal()
        .text_color(default_color);
    if token.code {
        view = view
            .px(px(3.0))
            .py(px(1.0))
            .rounded(px(4.0))
            .bg(theme.mockup_inline_code_bg)
            .font_family(theme.font_mono()[0].clone())
            .text_size(px(MockupTypeScale::INLINE_CODE_SIZE))
            .text_color(theme.mockup_text);
    }
    if is_bold {
        view = view
            .font_weight(FontWeight::BOLD)
            .text_color(theme.mockup_text);
    }
    if token.italic {
        view = view.italic();
    }
    view
}

fn render_inline_tokens_styled(
    tokens: &[InlineToken],
    theme: &HermesTheme,
    id_prefix: &str,
    default_bold: bool,
    default_color: Rgba,
) -> Div {
    let has_links = tokens.iter().any(|t| t.link.is_some());
    let has_code = tokens.iter().any(|t| t.code);
    if !has_links && !has_code {
        let (full_text, runs) = build_text_runs(tokens, theme, default_bold, default_color);
        if full_text.is_empty() {
            return div().w_full().min_w_0();
        }
        return div()
            .w_full()
            .min_w_0()
            .child(StyledText::new(full_text).with_runs(runs));
    }

    // Interactive and code-bearing paragraphs use wrapping, non-shrinking inline
    // boxes so Taffy cannot collapse them while links and long tokens still wrap.
    let lines = split_inline_token_lines(tokens);
    let mut container = div().w_full().min_w_0().flex().flex_col();
    let mut token_index = 0usize;

    for line_tokens in lines {
        let mut row = div()
            .w_full()
            .min_w_0()
            .flex()
            .flex_row()
            .flex_wrap()
            .items_baseline();
        if line_tokens.is_empty() {
            row = row.min_h(px(14.0));
        } else {
            for token in &line_tokens {
                if let Some(url) = &token.link {
                    let selector = format!("markdown_link_{id_prefix}_{token_index}");
                    let url = url.clone();
                    let mut link = style_inline_node(token, theme, default_bold, default_color)
                        .id(SharedString::from(selector.clone()))
                        .debug_selector(move || selector)
                        .text_color(theme.accent_blue())
                        .underline()
                        .cursor_pointer()
                        .hover(|this| this.text_color(theme.mockup_text))
                        .child(token.text.clone());
                    link.interactivity()
                        .on_click(move |_, _, cx| cx.open_url(&url));
                    row = row.child(link);
                } else if token.code {
                    let selector = format!("markdown_code_{id_prefix}_{token_index}");
                    row = row.child(
                        style_inline_node(token, theme, default_bold, default_color)
                            .debug_selector(move || selector)
                            .child(token.text.clone()),
                    );
                } else {
                    for (segment_index, seg) in
                        split_inline_segments(&token.text).into_iter().enumerate()
                    {
                        let selector =
                            format!("markdown_text_{id_prefix}_{token_index}_{segment_index}");
                        row = row.child(
                            style_inline_node(token, theme, default_bold, default_color)
                                .debug_selector(move || selector)
                                .child(seg.to_string()),
                        );
                    }
                }
                token_index += 1;
            }
        }
        container = container.child(row);
    }

    container
}

fn render_inline_tokens(tokens: &[InlineToken], theme: &HermesTheme, id_prefix: &str) -> Div {
    render_inline_tokens_styled(tokens, theme, id_prefix, false, theme.mockup_text_base)
}

fn render_markdown_table(
    headers: &[String],
    rows: &[Vec<String>],
    theme: &HermesTheme,
    id_prefix: &str,
) -> Div {
    let columns = headers
        .len()
        .max(rows.iter().map(Vec::len).max().unwrap_or(0))
        .max(1);
    let width = (columns as f32 * 180.0).max(320.0);
    let mut inner = div()
        .w_full()
        .min_w(px(width))
        .flex()
        .flex_col()
        .text_size(px(MockupTypeScale::DIFF_SIZE))
        .line_height(px(20.0));

    let mut header = div()
        .w_full()
        .flex()
        .flex_row()
        .bg(theme.mockup_sidebar);
    for index in 0..columns {
        let text = headers.get(index).map(String::as_str).unwrap_or("");
        let cell_id = format!("md_table_cell_{id_prefix}_header_{index}");
        let mut cell = div()
            .debug_selector(move || cell_id)
            .flex_1()
            .min_w(px(140.0))
            .px(px(10.0))
            .py(px(7.0))
            .font_weight(FontWeight::BOLD)
            .text_color(theme.mockup_text)
            .child(render_inline_tokens_styled(
                &parse_inline(text),
                theme,
                &format!("{id_prefix}_header_{index}"),
                true,
                theme.mockup_text,
            ));
        if index + 1 < columns {
            cell = cell.border_r_1().border_color(theme.mockup_border);
        }
        header = header.child(cell);
    }
    inner = inner.child(header);

    for (row_index, row) in rows.iter().enumerate() {
        let mut table_row = div().w_full().flex().flex_row();
        for column in 0..columns {
            let text = row.get(column).map(String::as_str).unwrap_or("");
            let cell_id = format!("md_table_cell_{id_prefix}_row_{row_index}_{column}");
            let mut cell = div()
                .debug_selector(move || cell_id)
                .flex_1()
                .min_w(px(140.0))
                .px(px(10.0))
                .py(px(6.0))
                .border_t_1()
                .border_color(theme.mockup_border)
                .text_color(theme.mockup_text_base)
                .child(render_inline_tokens_styled(
                    &parse_inline(text),
                    theme,
                    &format!("{id_prefix}_row_{row_index}_{column}"),
                    false,
                    theme.mockup_text_base,
                ));
            if column + 1 < columns {
                cell = cell.border_r_1().border_color(theme.mockup_border);
            }
            table_row = table_row.child(cell);
        }
        inner = inner.child(table_row);
    }

    div()
        .w_full()
        .min_w_0()
        .scroll_x(&format!("table_{id_prefix}_scroll"))
        .rounded(px(8.0))
        .border_1()
        .border_color(theme.mockup_border)
        .child(inner)
}

fn render_markdown_block(
    block: &MarkdownBlock,
    theme: &HermesTheme,
    id_prefix: &str,
    on_copy: Option<&TextCopyHandler>,
) -> Div {
    match block {
        MarkdownBlock::Paragraph(text) => {
            prose_style(theme, 0).child(render_inline_tokens(&parse_inline(text), theme, id_prefix))
        }
        MarkdownBlock::Heading { level, text } => {
            let level = (*level).min(4);
            prose_style(theme, level).child(render_inline_tokens_styled(
                &parse_inline(text),
                theme,
                id_prefix,
                level > 0,
                if level > 0 {
                    theme.mockup_text
                } else {
                    theme.mockup_text_base
                },
            ))
        }
        MarkdownBlock::List { items } => {
            let mut list = div().w_full().min_w_0().flex().flex_col().gap_1();
            for (index, item) in items.iter().enumerate() {
                let marker = if item.ordered {
                    format!("{}.", item.number)
                } else {
                    "•".to_string()
                };
                let row_sel = format!("md_list_row_{id_prefix}_{index}");
                let body_sel = format!("md_list_body_{id_prefix}_{index}");
                list = list.child(
                    div()
                        .debug_selector(move || row_sel)
                        .w_full()
                        .min_w_0()
                        .flex()
                        .flex_row()
                        .items_start()
                        .gap_2()
                        .pl(px((item.indent.min(8) as f32) * 16.0))
                        .child(
                            div()
                                .w(px(24.0))
                                .flex_none()
                                .text_size(px(MockupTypeScale::BASE_SIZE))
                                .line_height(px(MockupTypeScale::BASE_LINE_HEIGHT))
                                .text_color(theme.mockup_text_dim)
                                .child(marker),
                        )
                        .child(
                            prose_base(theme, 0)
                                .debug_selector(move || body_sel)
                                .flex_1()
                                .min_w_0()
                                .child(render_inline_tokens(
                                    &parse_inline(&item.text),
                                    theme,
                                    &format!("{id_prefix}_{index}"),
                                )),
                        ),
                );
            }
            list
        }
        MarkdownBlock::BlockQuote(blocks) => div()
            .w_full()
            .min_w_0()
            .flex()
            .flex_col()
            .gap_3()
            .border_l_2()
            .border_color(theme.mockup_border_strong)
            .pl(px(12.0))
            .text_color(theme.mockup_text_muted)
            .child(render_markdown_blocks(
                blocks,
                theme,
                &format!("{id_prefix}_quote"),
                on_copy,
            )),
        MarkdownBlock::Code { language, code } => {
            render_code_block(code, language, &format!("code_{id_prefix}"), theme, on_copy)
        }
        MarkdownBlock::Table { headers, rows } => {
            render_markdown_table(headers, rows, theme, id_prefix)
        }
        MarkdownBlock::Rule => div()
            .w_full()
            .h(px(1.0))
            .my(px(4.0))
            .bg(theme.mockup_border),
    }
}

fn render_markdown_blocks(
    blocks: &[MarkdownBlock],
    theme: &HermesTheme,
    id_prefix: &str,
    on_copy: Option<&TextCopyHandler>,
) -> Div {
    let mut container = div().w_full().min_w_0().flex().flex_col().gap_3();
    for (index, block) in blocks.iter().enumerate() {
        container = container.child(render_markdown_block(
            block,
            theme,
            &format!("{id_prefix}_{index}"),
            on_copy,
        ));
    }
    container
}

pub fn render_markdown(
    content: &str,
    theme: &HermesTheme,
    id_prefix: &str,
    on_copy: Option<&TextCopyHandler>,
) -> Div {
    render_markdown_blocks(&parse_markdown(content), theme, id_prefix, on_copy)
}

fn render_code_block(
    code: &str,
    language: &str,
    id: &str,
    theme: &HermesTheme,
    on_copy: Option<&TextCopyHandler>,
) -> Div {
    let (preview, truncated) = on_copy
        .map(|_| preview_text(code, 400))
        .unwrap_or_else(|| (code.to_string(), false));
    let label = if language.is_empty() {
        "code"
    } else {
        language
    };
    let mut block = div()
        .w_full()
        .min_w_0()
        .flex()
        .flex_col()
        .rounded(px(8.0))
        .border_1()
        .border_color(theme.mockup_border)
        .bg(theme.mockup_sidebar)
        .overflow_hidden()
        .child(
            div()
                .w_full()
                .flex()
                .flex_row()
                .items_center()
                .justify_between()
                .gap_2()
                .px(px(10.0))
                .py(px(5.0))
                .border_b_1()
                .border_color(theme.mockup_border)
                .text_size(px(MockupTypeScale::HINT_SIZE))
                .text_color(theme.mockup_text_dim)
                .child(div().min_w_0().truncate().child(label.to_string()))
                .child(copy_button(id, code, on_copy, theme)),
        )
        .child(
            div()
                .w_full()
                .min_w_0()
                .max_h(px(PAYLOAD_MAX_HEIGHT))
                .scroll_both(&format!("code_{id}_scroll"))
                .px(px(12.0))
                .py(px(10.0))
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(MockupTypeScale::DIFF_SIZE))
                .line_height(px(MockupTypeScale::DIFF_LINE_HEIGHT))
                .text_color(theme.mockup_text_base)
                .whitespace_nowrap()
                .child(preview),
        );
    if truncated {
        block = block.child(preview_notice(theme));
    }
    block
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

/// Builds the mockup action bar for one assistant message: Copy, Retry, then
/// the real token count when known. Controls without handlers stay noninteractive.
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
            let mut chips = div().w_full().flex().flex_wrap().gap(px(6.0)).pb(px(6.0));
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
        self.render_inner(theme, handlers, token_count, None)
    }

    pub fn render_with_action_bar_motion(
        &self,
        theme: &HermesTheme,
        handlers: Option<&ActionBarHandlers>,
        token_count: Option<String>,
        window: &mut Window,
        cx: &mut App,
        owner: EntityId,
    ) -> Div {
        self.render_inner(theme, handlers, token_count, Some((window, cx, owner)))
    }

    fn render_inner(
        &self,
        theme: &HermesTheme,
        handlers: Option<&ActionBarHandlers>,
        token_count: Option<String>,
        mut motion: Option<(&mut Window, &mut App, EntityId)>,
    ) -> Div {
        let reduced = match motion.as_ref() {
            Some((_, cx, _)) => motion::MotionSystem::is_reduced(cx),
            None => true,
        };
        let mut card = div().w_full().flex().flex_col().gap(px(FEED_GAP));

        if let Some(reasoning) = &self.reasoning {
            let id = Some(ElementId::Name(SharedString::from(format!(
                "reasoning_{}",
                self.id
            ))));
            let on_click = handlers.and_then(|h| h.reasoning.clone());
            let on_copy = handlers.and_then(|h| h.copy_payload.clone());
            if let Some((window, cx, owner)) = motion.as_mut() {
                let reveal = RevealMotion::for_identity(
                    *owner,
                    "reasoning_reveal",
                    &format!("reasoning_{}", self.id),
                );
                reveal.ensure(window, cx, f32::from(reasoning.is_expanded));
                card = card.child(reasoning.render_reveal_with_copy(
                    theme,
                    id,
                    on_click,
                    on_copy,
                    &format!("reasoning_{}", self.id),
                    reveal,
                    cx,
                ));
            } else {
                card = card.child(reasoning.render_with_click_and_copy(
                    theme,
                    id,
                    on_click,
                    on_copy,
                    &format!("reasoning_{}", self.id),
                ));
            }
        }

        for tool in &self.tool_calls {
            let on_input = handlers.and_then(|h| h.tool_input.clone());
            let on_output = handlers.and_then(|h| h.tool_output.clone());
            let on_copy = handlers.and_then(|h| h.copy_payload.clone());
            if let Some((window, cx, owner)) = motion.as_mut() {
                card = card.child(tool.render_with_fold_handlers_motion(
                    theme, on_input, on_output, on_copy, window, cx, *owner,
                ));
            } else {
                card =
                    card.child(tool.render_with_fold_handlers(theme, on_input, on_output, on_copy));
            }
        }

        if !self.content.trim().is_empty() {
            card = card.child(render_markdown(
                &self.content,
                theme,
                &self.id,
                handlers.and_then(|h| h.copy_payload.as_ref()),
            ));
        }

        // 4. Streaming braille spinner at the tail (mockup `.sp`).
        if self.is_streaming {
            card = card.child(self.render_streaming_spinner(theme, reduced));
        }

        // 5. Action bar + Changes button (only when not streaming).
        if !self.is_streaming {
            let bar = match handlers {
                Some(h) => mockup_action_bar(theme, &self.id, token_count, h),
                None => self.render_action_bar(theme),
            };
            card = card.child(bar);
            let changes_click = handlers.and_then(|h| h.changes.as_ref());
            let copy_payload = handlers.and_then(|h| h.copy_payload.clone());
            if let Some((window, cx, owner)) = motion.as_mut() {
                if let Some(changes) = self.render_changes_button_motion(
                    theme,
                    changes_click,
                    copy_payload,
                    window,
                    cx,
                    *owner,
                ) {
                    card = card.child(changes);
                }
            } else if let Some(changes) =
                self.render_changes_button(theme, changes_click, copy_payload)
            {
                card = card.child(changes);
            }
        }

        card
    }

    /// The mockup `.sp` braille spinner (900ms steps(1)); reduced motion keeps
    /// the first frame static.
    fn render_streaming_spinner(&self, theme: &HermesTheme, reduced: bool) -> AnyElement {
        let spinner = div()
            .font_family(theme.font_mono()[0].clone())
            .text_size(px(MockupTypeScale::SPINNER_SIZE))
            .text_color(theme.accent_blue());
        if reduced {
            spinner
                .child(motion::spinner_frame(std::time::Duration::ZERO).to_string())
                .into_any_element()
        } else {
            spinner
                .with_animation(
                    ElementId::Name(SharedString::from(format!("feed_sp_{}", self.id))),
                    Animation::new(motion::SPIN_PERIOD).repeat(),
                    |el, phase| {
                        let elapsed = std::time::Duration::from_secs_f32(
                            phase * motion::SPIN_PERIOD.as_secs_f32(),
                        );
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
    fn render_changes_button(
        &self,
        theme: &HermesTheme,
        on_toggle: Option<&ClickHandler>,
        on_copy: Option<TextCopyHandler>,
    ) -> Option<AnyElement> {
        self.render_changes_button_inner(theme, on_toggle, on_copy, None)
    }

    fn render_changes_button_motion(
        &self,
        theme: &HermesTheme,
        on_toggle: Option<&ClickHandler>,
        on_copy: Option<TextCopyHandler>,
        window: &mut Window,
        cx: &mut App,
        owner: EntityId,
    ) -> Option<AnyElement> {
        let reveal =
            RevealMotion::for_identity(owner, "changes_reveal", &format!("changes_{}", self.id));
        reveal.ensure(window, cx, f32::from(self.changes_expanded));
        self.render_changes_button_inner(theme, on_toggle, on_copy, Some((reveal, cx)))
    }

    fn render_changes_button_inner(
        &self,
        theme: &HermesTheme,
        on_toggle: Option<&ClickHandler>,
        on_copy: Option<TextCopyHandler>,
        reveal: Option<(RevealMotion, &App)>,
    ) -> Option<AnyElement> {
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
        let values = reveal.map(|(reveal, cx)| reveal.values(cx));
        let caret: AnyElement = if let Some(values) = values {
            svg()
                .path("icons/chevron-right.svg")
                .w(px(CHANGES_CHEV_SIZE))
                .h(px(CHANGES_CHEV_SIZE))
                .flex_none()
                .text_color(theme.mockup_text_dim)
                .with_transformation(Transformation::rotate(radians(
                    values.spatial.clamp(0.0, 1.0) * std::f32::consts::FRAC_PI_2,
                )))
                .into_any_element()
        } else {
            action_icon(
                "chevron-down",
                CHANGES_CHEV_SIZE,
                "changes_hdr",
                theme.mockup_text_dim,
                theme.mockup_text,
            )
            .into_any_element()
        };

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
            .child(div().text_color(theme.mockup_text).child("Changes"))
            .child(
                div()
                    .flex_1()
                    .child(changes_summary(file_count, adds, dels)),
            )
            .child(caret);
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
        if values.map_or(self.changes_expanded, RevealValues::is_visible) {
            let mut body = div().w_full().flex().flex_col().gap(px(8.0));
            for (index, tool) in self.tool_calls.iter().enumerate() {
                if let Some(view) = tool.diff_view() {
                    body = body.child(view.render_with_copy(
                        theme,
                        on_copy.as_ref(),
                        &format!("changes_{}_{}", self.id, index),
                    ));
                }
            }
            if let Some(values) = values {
                body = values.apply(body);
            }
            column = column.child(body);
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
    fn test_markdown_content_renders_structured_blocks() {
        let content =
            "# Result\n\n**Done** with `cargo test`.\n\n- one\n- two\n\n```rust\nfn main() {}\n```";
        let blocks = parse_markdown(content);
        assert!(matches!(blocks[0], MarkdownBlock::Heading { level: 1, .. }));
        assert!(matches!(blocks[1], MarkdownBlock::Paragraph(_)));
        assert!(matches!(blocks[2], MarkdownBlock::List { .. }));
        assert!(matches!(blocks[3], MarkdownBlock::Code { .. }));

        let theme = HermesTheme::default();
        let view = AssistantMessageView::new("msg_markdown", content);
        let _ = view.render(&theme);
    }

    #[test]
    fn test_inline_tokens_preserve_streaming_markers() {
        let tokens = parse_inline("**partial `code");
        let text = tokens
            .iter()
            .map(|token| token.text.as_str())
            .collect::<String>();
        assert_eq!(text, "**partial `code");
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
        let view = AssistantMessageView::new("msg_1", "Answer:\n```bash\nls -la\n```\nDone.");
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

    #[test]
    fn test_multiline_inline_tokens_split_across_separate_rows() {
        let tokens = parse_inline("read\nfile: /tmp/a_b.py\n**bold _italic_**");
        let lines = split_inline_token_lines(&tokens);
        assert_eq!(lines.len(), 3);
        assert_eq!(lines[0][0].text, "read");
        assert_eq!(lines[1][0].text, "file: /tmp/a_b.py");
        assert!(lines[2].iter().any(|t| t.text == "bold " && t.bold && !t.italic));
        assert!(lines[2].iter().any(|t| t.text == "italic" && t.bold && t.italic));

        let theme = HermesTheme::default();
        let view = AssistantMessageView::new(
            "msg_multiline",
            "read\nfile: /Users/example/ANIMATIONS_PLAN.md",
        );
        let _ = view.render(&theme);
    }

    #[test]
    fn test_styled_runs_match_byte_lengths_and_inline_segments() {
        let theme = HermesTheme::default();
        let tokens = parse_inline("**Bold** `read_file` _em_ [docs](https://example.com)");
        let (text, runs) = build_text_runs(&tokens, &theme, false, theme.mockup_text_base);
        let sum_len: usize = runs.iter().map(|r| r.len).sum();
        assert_eq!(text, "Bold read_file em docs");
        assert_eq!(sum_len, text.len());
        assert_eq!(runs[0].font.weight, FontWeight::BOLD);
        assert_eq!(runs[2].font.family, theme.font_mono()[0]);
        assert!(runs[2].background_color.is_some());
        assert_eq!(runs[4].font.style, FontStyle::Italic);
        assert!(runs[6].underline.is_some());

        let segs = split_inline_segments("See ARCHITECTURE.md for details");
        assert_eq!(segs.concat(), "See ARCHITECTURE.md for details");
        assert!(segs.len() >= 4);
    }

    struct MarkdownLayoutHarness {
        content: String,
        width: f32,
    }

    impl Render for MarkdownLayoutHarness {
        fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
            let theme = HermesTheme::default();
            div()
                .debug_selector(|| "markdown_layout_harness".to_string())
                .w(px(self.width))
                .p(px(24.0))
                .bg(theme.mockup_body)
                .child(AssistantMessageView::new("msg_layout", &self.content).render(&theme))
        }
    }

    #[gpui::test]
    fn test_table_and_ordered_list_never_collapse_or_overlap_in_gpui_layout(
        cx: &mut gpui::TestAppContext,
    ) {
        let md = concat!(
            "### HEX Tech Stack\n\n",
            "| Layer | Components & Choices |\n",
            "| --- | --- |\n",
            "| **LLM Providers** | Gemini, Groq, Cerebras, Ollama (`MLX`, etc.) |\n",
            "| **Auth & Routing** | Multi-key pools, OS Keychain (`keyring`), 429 rotation with jittered backoff |\n",
            "| **AST & Indexing** | Tree-sitter (`Graphify`) — polyglot AST, blast-radius analysis |\n",
            "| **Context & Memory** | 5-slot frozen prompt, AST-scoped memory, A-MEM Zettelkasten, SkillSpector AST gate |\n\n",
            "### Key Architectural Planes\n\n",
            "1. **Presentation** — GPUI desktop (experimental), Ratatui TUI, ACP server\n",
            "2. **Orchestration** — `ReActLoop`, MoA, timeline rewind, approval leases\n",
            "3. **Execution & Capabilities** — `ToolGateway`, sandboxed shell, `file_tools`, MCP adapters\n",
            "4. **Context & Memory** — `PromptBuilder` 5-slot freeze, Graphify surgical slicing, guardrails\n",
            "5. **Storage/Isolation** — CoW sandboxes, dual-driver DB, SHA-256 event chaining, GC\n",
        );

        let (_entity, vc) = cx.add_window_view(|_, _| MarkdownLayoutHarness {
            content: md.to_string(),
            width: 760.0,
        });
        vc.simulate_resize(size(px(800.0), px(900.0)));
        vc.update(|window, _| window.refresh());
        vc.run_until_parked();

        // Table block index is 1: header + 4 rows, 2 columns each
        let mut prev_row_bottom = px(0.0);
        for row_idx in 0..4 {
            let c0 = vc
                .debug_bounds(Box::leak(
                    format!("md_table_cell_msg_layout_1_row_{row_idx}_0").into_boxed_str(),
                ))
                .expect("table col 0 bounds");
            let c1 = vc
                .debug_bounds(Box::leak(
                    format!("md_table_cell_msg_layout_1_row_{row_idx}_1").into_boxed_str(),
                ))
                .expect("table col 1 bounds");
            assert!(
                c0.size.width >= px(140.0),
                "row {row_idx} col 0 collapsed horizontally: {:?}",
                c0
            );
            assert!(
                c1.size.width >= px(140.0),
                "row {row_idx} col 1 collapsed horizontally: {:?}",
                c1
            );
            assert!(
                c0.size.height <= px(90.0) && c1.size.height <= px(90.0),
                "row {row_idx} stacked vertically into 1-char column: c0={c0:?} c1={c1:?}"
            );
            assert!(
                c0.origin.y >= prev_row_bottom - px(0.5),
                "row {row_idx} overlapped previous row bottom {prev_row_bottom:?}: {c0:?}"
            );
            prev_row_bottom = c0.bottom().max(c1.bottom());
        }

        // Ordered list block index is 3: items 0..5
        let mut prev_item_bottom = px(0.0);
        for item_idx in 0..5 {
            let row_bounds = vc
                .debug_bounds(Box::leak(
                    format!("md_list_row_msg_layout_3_{item_idx}").into_boxed_str(),
                ))
                .expect("list row bounds");
            let body_bounds = vc
                .debug_bounds(Box::leak(
                    format!("md_list_body_msg_layout_3_{item_idx}").into_boxed_str(),
                ))
                .expect("list body bounds");
            assert!(
                body_bounds.size.width >= px(450.0),
                "list item {item_idx} collapsed horizontally: {body_bounds:?}"
            );
            assert!(
                row_bounds.size.height <= px(80.0),
                "list item {item_idx} stacked vertically: {row_bounds:?}"
            );
            assert!(
                row_bounds.origin.y >= prev_item_bottom - px(0.5),
                "list item {item_idx} overlapped previous item bottom {prev_item_bottom:?}: {row_bounds:?}"
            );
            prev_item_bottom = row_bounds.bottom();
        }
    }

    #[gpui::test]
    fn test_link_labels_and_unbroken_tokens_wrap_in_narrow_panes(cx: &mut gpui::TestAppContext) {
        let long_label = "A deliberately long link label with enough words to wrap across several lines in a narrow pane";
        let long_token = "abcdefghijklmnopqrstuvwxyz0123456789".repeat(8);
        let second_paragraph = format!("See [docs](https://example.com) and {long_token}");
        let (long_token_index, long_segment_index) = parse_inline(&second_paragraph)
            .iter()
            .enumerate()
            .find_map(|(token_index, token)| {
                split_inline_segments(&token.text)
                    .iter()
                    .position(|segment| *segment == long_token)
                    .map(|segment_index| (token_index, segment_index))
            })
            .expect("unbroken token parsed");
        let content = format!("[{long_label}](https://example.com)\n\n{second_paragraph}");
        let (_entity, vc) = cx.add_window_view(|_, _| MarkdownLayoutHarness {
            content,
            width: 260.0,
        });
        vc.simulate_resize(size(px(320.0), px(900.0)));
        vc.update(|window, _| window.refresh());
        vc.run_until_parked();

        let root = vc
            .debug_bounds(Box::leak(
                "markdown_layout_harness".to_string().into_boxed_str(),
            ))
            .expect("layout harness bounds");
        let pane_left = root.origin.x + px(24.0);
        let pane_right = root.right() - px(24.0);
        let pane_width = pane_right - pane_left;
        let link = vc
            .debug_bounds(Box::leak(
                "markdown_link_msg_layout_0_0".to_string().into_boxed_str(),
            ))
            .expect("long link label bounds");
        let unbroken_selector =
            format!("markdown_text_msg_layout_1_{long_token_index}_{long_segment_index}");
        let unbroken = vc
            .debug_bounds(Box::leak(unbroken_selector.into_boxed_str()))
            .expect("unbroken inline token bounds");

        for (name, bounds) in [("link", link), ("unbroken token", unbroken)] {
            assert!(
                bounds.size.width <= pane_width + px(0.5),
                "{name} overflowed narrow pane: {bounds:?} pane={pane_width:?}"
            );
            assert!(
                bounds.origin.x >= pane_left - px(0.5) && bounds.right() <= pane_right + px(0.5),
                "{name} escaped pane horizontally: {bounds:?} pane=({pane_left:?}, {pane_right:?})"
            );
            assert!(
                bounds.size.height > px(30.0),
                "{name} did not wrap: {bounds:?}"
            );
        }
    }

    #[gpui::test]
    fn test_inline_code_chips_match_with_and_without_links(cx: &mut gpui::TestAppContext) {
        let second_paragraph = "See [docs](https://example.com) and `read_file`";
        let code_index = parse_inline(second_paragraph)
            .iter()
            .position(|token| token.code)
            .expect("inline code parsed");
        let content = format!("`read_file`\n\n{second_paragraph}");
        let (_entity, vc) = cx.add_window_view(|_, _| MarkdownLayoutHarness {
            content,
            width: 760.0,
        });
        vc.simulate_resize(size(px(800.0), px(900.0)));
        vc.update(|window, _| window.refresh());
        vc.run_until_parked();

        let plain_code = vc
            .debug_bounds(Box::leak(
                "markdown_code_msg_layout_0_0".to_string().into_boxed_str(),
            ))
            .expect("inline code without link bounds");
        let linked_code_selector = format!("markdown_code_msg_layout_1_{code_index}");
        let linked_code = vc
            .debug_bounds(Box::leak(linked_code_selector.into_boxed_str()))
            .expect("inline code with link bounds");

        assert!(
            (plain_code.size.width - linked_code.size.width).abs() <= px(0.5),
            "inline code width changed near a link: {plain_code:?} vs {linked_code:?}"
        );
        assert!(
            (plain_code.size.height - linked_code.size.height).abs() <= px(0.5),
            "inline code height changed near a link: {plain_code:?} vs {linked_code:?}"
        );
    }
}

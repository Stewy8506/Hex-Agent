use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

use ratatui::buffer::Buffer;
use ratatui::layout::{Position, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Widget};

use super::animation::{self, AnimationState, MAX_ENTRANCE_BLOCKS, RISE_STAGGER_MS, TOOL_HOVER_BG};
use super::markdown::{render_markdown, wrap_line};
use super::style::HexTheme;
use crate::app::{App, DialogueItem};
use hex_protocol::models::Role;

#[derive(Default)]
pub struct TranscriptLayout {
    width: u16,
    entries: Vec<Entry>,
    requested_scroll: usize,
    resolved_scroll: usize,
    prefix_rows: usize,
}

impl TranscriptLayout {
    pub fn requested_scroll(&self) -> usize {
        self.requested_scroll
    }
}

struct Entry {
    signature: u64,
    start: usize,
    height: usize,
}

fn signature(app: &App, index: usize, diagnostic: Option<usize>, theme: &HexTheme) -> u64 {
    let mut hash = DefaultHasher::new();
    match &app.dialogue_items[index] {
        DialogueItem::Message(m) => {
            m.content.hash(&mut hash);
            format!("{:?}", m.role).hash(&mut hash);
        }
        DialogueItem::ToolCall(t) => {
            t.id.hash(&mut hash);
            t.tool.hash(&mut hash);
            t.args.hash(&mut hash);
            t.result.hash(&mut hash);
            t.collapsed.hash(&mut hash);
            t.duration_ms.hash(&mut hash);
            t.exit_status.hash(&mut hash);
            app.active_approval.is_some().hash(&mut hash);
        }
    }
    if index == 0 {
        app.current_thought.is_some().hash(&mut hash);
    }
    matches!(
        app.dialogue_items.get(index.wrapping_sub(1)),
        Some(DialogueItem::ToolCall(_))
    )
    .hash(&mut hash);
    matches!(
        app.dialogue_items.get(index + 1),
        Some(DialogueItem::ToolCall(_))
    )
    .hash(&mut hash);
    if diagnostic == Some(index) {
        format!("{:?}", app.active_diagnostic).hash(&mut hash);
    }
    format!("{:?}", theme).hash(&mut hash);
    hash.finish()
}

fn item_lines(
    app: &App,
    index: usize,
    diagnostic: Option<usize>,
    width: u16,
    theme: &HexTheme,
    anim: &AnimationState,
) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    let continues_tools = matches!(app.dialogue_items[index], DialogueItem::ToolCall(_))
        && matches!(
            app.dialogue_items.get(index.wrapping_sub(1)),
            Some(DialogueItem::ToolCall(_))
        );
    if (index > 0 || app.current_thought.is_some()) && !continues_tools {
        lines.push(Line::raw(""));
    }
    match &app.dialogue_items[index] {
        DialogueItem::Message(m) if m.role == Role::User => {
            for (i, text) in m.content.split('\n').enumerate() {
                lines.extend(wrap_line(
                    Line::from(vec![
                        Span::styled(
                            if i == 0 { "> " } else { "  " },
                            Style::default().fg(theme.text_dim),
                        ),
                        Span::styled(text.to_string(), Style::default().fg(theme.text_bright)),
                    ]),
                    width as usize,
                ));
            }
        }
        DialogueItem::Message(m) => {
            let mut rendered = render_markdown(&m.content, width as usize, theme);
            if m.role == Role::System {
                for line in &mut rendered {
                    line.style = line.style.add_modifier(Modifier::ITALIC);
                }
            }
            lines.extend(rendered);
        }
        DialogueItem::ToolCall(t) => {
            let next = matches!(
                app.dialogue_items.get(index + 1),
                Some(DialogueItem::ToolCall(_))
            );
            let duration = t
                .duration_ms
                .map(|d| format!(" · {:.1}s", d as f64 / 1000.0))
                .unwrap_or_default();
            let status = t
                .exit_status
                .as_ref()
                .map(|s| format!(" · {s}"))
                .unwrap_or_default();
            let awaiting = app.active_approval.is_some() && t.result.is_none();
            let failed = t
                .exit_status
                .as_deref()
                .is_some_and(|s| s.starts_with("exit ") && s != "exit 0");
            let verb = if awaiting {
                theme.accent_blue
            } else if failed {
                theme.rose_fg
            } else {
                theme.text_dim
            };
            lines.extend(wrap_line(
                Line::from(vec![
                    Span::raw("    "),
                    Span::styled(
                        if next { "├ " } else { "└ " },
                        Style::default().fg(theme.accent_blue),
                    ),
                    if t.result.is_none() {
                        Span::styled(
                            format!("{} ", anim.spinner_frame()),
                            Style::default().fg(theme.accent_blue),
                        )
                    } else {
                        Span::raw("  ")
                    },
                    Span::styled(format!("{:<7} ", t.tool), Style::default().fg(verb)),
                    Span::styled(
                        crate::app::tool_args_label(&t.args),
                        Style::default().fg(theme.text_bright),
                    ),
                    Span::styled(
                        format!(
                            "{duration}{status}{}",
                            if awaiting {
                                " · waiting for permission"
                            } else {
                                ""
                            }
                        ),
                        Style::default().fg(theme.text_extra_dim),
                    ),
                ]),
                width as usize,
            ));
            if let Some(result) = &t.result {
                if !t.collapsed {
                    for text in result.lines().take(4) {
                        lines.extend(wrap_line(
                            Line::from(vec![
                                Span::raw("    "),
                                Span::styled(
                                    if next { "│   " } else { "    " },
                                    Style::default().fg(theme.accent_blue),
                                ),
                                Span::styled(
                                    text.to_string(),
                                    Style::default().fg(theme.text_extra_dim),
                                ),
                            ]),
                            width as usize,
                        ));
                    }
                }
            }
            if diagnostic == Some(index) {
                if let Some(diag) = &app.active_diagnostic {
                    for line in
                        super::screens::error_view::diagnostic_lines(diag, theme, width as usize)
                    {
                        lines.extend(wrap_line(line, width as usize));
                    }
                }
            }
        }
    }
    lines
}

pub fn render(app: &App, theme: &HexTheme, anim: &AnimationState, area: Rect, buf: &mut Buffer) {
    if area.is_empty() {
        return;
    }
    let mut prefix = vec![
        Line::from(Span::styled(
            "█ █ █▀▀ ▀▄▀",
            Style::default().fg(theme.text_bright),
        )),
        Line::from(Span::styled(
            "█▀█ ██▄ ▄▀▄",
            Style::default().fg(theme.text_bright),
        )),
        Line::from(Span::styled(
            format!("0.4.2 · session {}", app.session_id),
            Style::default().fg(theme.text_dim),
        )),
        Line::raw(""),
    ];
    let empty = app.dialogue_items.is_empty()
        && app.active_diff.is_none()
        && app.current_thought.is_none()
        && app.active_diagnostic.is_none();
    if empty {
        prefix.push(Line::from(Span::styled(
            "> Ready for instructions. Type your goal or /help for interactive commands.",
            Style::default().fg(theme.text_dim),
        )));
    } else if let Some(thought) = &app.current_thought {
        prefix.push(Line::from(Span::styled(
            format!("  {thought}"),
            Style::default().fg(theme.text_muted),
        )));
    }
    let prefix: Vec<_> = prefix
        .into_iter()
        .flat_map(|line| wrap_line(line, area.width as usize))
        .collect();
    let diagnostic = app.active_diagnostic.as_ref().and_then(|diag| {
        let status = format!("exit {}", diag.exit_code);
        app.dialogue_items.iter().rposition(|item| matches!(item, DialogueItem::ToolCall(t) if t.exit_status.as_deref() == Some(&status)))
    });
    let mut tail = Vec::new();
    if diagnostic.is_none() {
        if let Some(diag) = &app.active_diagnostic {
            if !app.dialogue_items.is_empty() || app.current_thought.is_some() {
                tail.push(Line::raw(""));
            }
            tail.extend(super::screens::error_view::diagnostic_lines(
                diag,
                theme,
                area.width as usize,
            ));
        }
    }
    if app.is_busy || app.thinking_status.is_some() {
        if !app.dialogue_items.is_empty() || app.current_thought.is_some() {
            tail.push(Line::raw(""));
        }
        let mut spans = vec![Span::raw("  ")];
        if app.is_busy {
            spans.push(Span::styled(
                format!("{} ", anim.spinner_frame()),
                Style::default().fg(theme.accent_blue),
            ));
        }
        let verbs = [
            "Thinking",
            "Reading workspace context",
            "Synthesizing plan & code",
            "Verifying public API surface",
        ];
        let status = if app.active_approval.is_some() {
            "Waiting for permission"
        } else if let Some(status) = &app.thinking_status {
            status
        } else {
            verbs[(anim.elapsed().as_secs() / 3) as usize % verbs.len()]
        };
        spans.extend(anim.render_shimmer(status, theme.text_dim, theme.text_bright));
        if app.is_busy {
            if let Some(start) = app.busy_started_at {
                spans.push(Span::styled(
                    format!(" · {:.1}s", start.elapsed().as_secs_f64()),
                    Style::default().fg(theme.text_extra_dim),
                ));
            }
        }
        tail.push(Line::from(spans));
    }
    let tail: Vec<_> = tail
        .into_iter()
        .flat_map(|line| wrap_line(line, area.width as usize))
        .collect();
    let mut layout = app.transcript_layout.borrow_mut();
    let anchor = if !app.follow_dialogue && layout.requested_scroll == app.dialogue_scroll {
        layout.entries.iter().enumerate().find_map(|(i, entry)| {
            (layout.resolved_scroll >= entry.start
                && layout.resolved_scroll < entry.start + entry.height)
                .then(|| (i, layout.resolved_scroll - entry.start))
        })
    } else {
        None
    };
    let reflow = layout.width != area.width || layout.prefix_rows != prefix.len();
    let mut cursor = prefix.len();
    for index in 0..app.dialogue_items.len() {
        let key = signature(app, index, diagnostic, theme);
        let old = layout.entries.get(index);
        let height = if !reflow && old.is_some_and(|entry| entry.signature == key) {
            old.unwrap().height
        } else {
            item_lines(app, index, diagnostic, area.width, theme, anim).len()
        };
        let entry = Entry {
            signature: key,
            start: cursor,
            height,
        };
        if index < layout.entries.len() {
            layout.entries[index] = entry;
        } else {
            layout.entries.push(entry);
        }
        cursor = cursor.saturating_add(height);
    }
    layout.entries.truncate(app.dialogue_items.len());
    let total = cursor.saturating_add(tail.len());
    let max_scroll = total.saturating_sub(area.height as usize);
    let scroll = if app.follow_dialogue {
        max_scroll
    } else if reflow {
        anchor
            .and_then(|(i, row)| {
                layout
                    .entries
                    .get(i)
                    .map(|entry| entry.start + row.min(entry.height.saturating_sub(1)))
            })
            .unwrap_or(app.dialogue_scroll)
            .min(max_scroll)
    } else {
        app.dialogue_scroll.min(max_scroll)
    };
    layout.width = area.width;
    layout.prefix_rows = prefix.len();
    layout.requested_scroll = app.dialogue_scroll;
    layout.resolved_scroll = scroll;
    app.dialogue_metrics.set((total, area.height as usize));
    app.dialogue_view_offset.set(scroll);
    let end = scroll.saturating_add(area.height as usize);
    let mut visible = Vec::with_capacity(area.height as usize);
    let mut tool_rows = Vec::new();
    for (row, line) in prefix.into_iter().enumerate() {
        if row >= scroll && row < end {
            visible.push(line);
        }
    }
    let (seen, born) = app.observe_dialogue_entrance(app.dialogue_items.len());
    let first_rising = seen.max(app.dialogue_items.len().saturating_sub(MAX_ENTRANCE_BLOCKS));
    let first = layout
        .entries
        .partition_point(|entry| entry.start + entry.height <= scroll);
    for index in first..layout.entries.len() {
        let entry = &layout.entries[index];
        if entry.start >= end {
            break;
        }
        let mut lines = item_lines(app, index, diagnostic, area.width, theme, anim);
        let tool_header = if matches!(app.dialogue_items[index], DialogueItem::ToolCall(_))
            && (index > 0 || app.current_thought.is_some())
            && !matches!(
                app.dialogue_items.get(index.wrapping_sub(1)),
                Some(DialogueItem::ToolCall(_))
            ) {
            1
        } else {
            0
        };
        if !anim.is_reduced() && index >= first_rising && seen < app.dialogue_items.len() {
            let factor = animation::rise_factor(
                born.elapsed(),
                std::time::Duration::from_millis(RISE_STAGGER_MS * (index - first_rising) as u64),
            );
            let blank = usize::from(lines.first().is_some_and(|line| line.width() == 0));
            for line in lines.iter_mut().skip(blank) {
                animation::apply_entrance_fade(line, factor, theme.window_bg);
            }
        }
        for (row, line) in lines.into_iter().enumerate() {
            let absolute = entry.start + row;
            if absolute >= scroll && absolute < end {
                if matches!(app.dialogue_items[index], DialogueItem::ToolCall(_))
                    && row == tool_header
                {
                    tool_rows.push(visible.len());
                }
                visible.push(line);
            }
        }
    }
    for (row, line) in tail.into_iter().enumerate() {
        if cursor + row >= scroll && cursor + row < end {
            visible.push(line);
        }
    }
    Paragraph::new(visible).render(area, buf);
    if scroll > 0 {
        animation::apply_top_fade(area, 1, theme.window_bg, buf);
    }
    if let Some((x, y)) = app.mouse_pos {
        if area.contains(Position::new(x, y)) && tool_rows.contains(&((y - area.y) as usize)) {
            for col in area.left()..area.right() {
                buf[(col, y)].set_bg(TOOL_HOVER_BG);
            }
        }
    }
}

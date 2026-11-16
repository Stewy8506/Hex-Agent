//! Screen 5: Subagent Live Inspection Modal matching the HEX Agent HTML mockup.
//!
//! Opens on Tab in a session. Every row comes from the server's subagent
//! record; an empty registry renders the empty state, never invented activity.

use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Widget};

use crate::app::{App, SubagentModalState};
use crate::ui::animation::AnimationState;
use crate::ui::keymap::{self, Scope};
use crate::ui::style::HexTheme;

/// Mockup: 660px of a 1200px canvas at 10px per column, 520px tall at 21px/row.
const MODAL_WIDTH: u16 = 66;
const MODAL_HEIGHT: u16 = 24;
/// The prompt brief is clamped to two rows until `e` expands it.
const PROMPT_ROWS_CLAMPED: usize = 2;
const PROMPT_ROWS_EXPANDED: usize = 6;

pub fn render(
    _app: &App,
    modal_state: &SubagentModalState,
    theme: &HexTheme,
    anim: &AnimationState,
    area: Rect,
    buf: &mut Buffer,
) {
    if area.height < 16 || area.width < 50 {
        return;
    }

    // 1. Dim the backdrop
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            let cell = &mut buf[(x, y)];
            cell.set_style(Style::default().fg(theme.text_extra_dim).bg(theme.canvas_bg));
        }
    }

    let modal_width = MODAL_WIDTH.min(area.width.saturating_sub(4));
    let modal_height = MODAL_HEIGHT.min(area.height.saturating_sub(2));
    let modal_x = area.x + (area.width.saturating_sub(modal_width)) / 2;
    let modal_y = area.y + (area.height.saturating_sub(modal_height)) / 2;
    let modal_rect = Rect::new(modal_x, modal_y, modal_width, modal_height);

    Clear.render(modal_rect, buf);

    let modal_block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme.border_modal))
        .style(Style::default().bg(theme.card_bg));
    let inner = modal_block.inner(modal_rect);
    modal_block.render(modal_rect, buf);

    let empty = modal_state.name.is_empty();
    let prompt_rows = if empty {
        0
    } else if modal_state.is_expanded {
        PROMPT_ROWS_EXPANDED
    } else {
        PROMPT_ROWS_CLAMPED
    };

    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(prompt_rows as u16 + if empty { 0 } else { 1 }), // header + brief
            Constraint::Length(if empty { 0 } else { 1 }),                      // divider
            Constraint::Min(4),                                                 // activity stream
            Constraint::Length(1),                                              // divider
            Constraint::Length(1),                                              // footer
        ])
        .split(inner);

    let width = inner.width as usize;

    // 2. Prompt brief: header row, then the clamped (or expanded) goal text.
    if !empty {
        let header_line = Line::from(vec![
            Span::styled("prompt", theme.apfel_header_style()),
            Span::raw(" ".repeat(width.saturating_sub(6 + 10))),
            Span::styled("e ", Style::default().fg(theme.text_bright)),
            Span::styled(
                if modal_state.is_expanded { "collapse" } else { "expand" },
                Style::default().fg(theme.text_dim),
            ),
        ]);
        buf.set_line(chunks[0].x, chunks[0].y, &header_line, chunks[0].width);

        let mut wrapped = wrap_words(&modal_state.prompt, width);
        let truncated = wrapped.len() > prompt_rows;
        wrapped.truncate(prompt_rows);
        if truncated {
            if let Some(last) = wrapped.last_mut() {
                // A wrapped row already fills the width, so the marker has to
                // replace its last character rather than extend past it.
                let head: String = last.chars().take(width.saturating_sub(1)).collect();
                *last = format!("{head}…");
            }
        }
        for (i, row) in wrapped.iter().enumerate() {
            let line = Line::from(vec![Span::styled(row.clone(), Style::default().fg(theme.text_muted))]);
            buf.set_line(chunks[0].x, chunks[0].y + 1 + i as u16, &line, chunks[0].width);
        }
        draw_divider(chunks[1], theme, buf);
    }

    // 3. Activity stream: one gap row under the divider, newest line at the
    //    bottom, arrows scrolling back through the tail.
    let stream_area = chunks[2];
    let body = Rect::new(
        stream_area.x,
        stream_area.y + if empty { 0 } else { 1 },
        stream_area.width,
        stream_area.height.saturating_sub(if empty { 0 } else { 1 }),
    );

    if empty {
        let message = Line::from(vec![Span::styled(
            "no subagents in this session",
            Style::default().fg(theme.text_dim),
        )]);
        let hint = Line::from(vec![Span::styled(
            "delegated work appears here while a turn runs",
            Style::default().fg(theme.text_extra_dim),
        )]);
        let mid = body.y + body.height / 2;
        buf.set_line(body.x, mid, &message, body.width);
        if mid + 1 < body.bottom() {
            buf.set_line(body.x, mid + 1, &hint, body.width);
        }
    } else {
        let mut rows: Vec<Line> = modal_state
            .activity_lines
            .iter()
            .map(|text| {
                Line::from(vec![Span::styled(
                    ellipsize(text, body.width as usize),
                    Style::default().fg(theme.text_bright),
                )])
            })
            .collect();
        if modal_state.is_running {
            let shimmer = format!("Executing {}...", modal_state.name);
            let owned: Vec<Span<'static>> = anim
                .render_shimmer(&shimmer, theme.text_dim, theme.text_bright)
                .into_iter()
                .map(|s| Span::styled(s.content.into_owned(), s.style))
                .collect();
            rows.push(Line::from(owned));
        }

        let total = rows.len();
        let scroll = modal_state.activity_scroll.min(total.saturating_sub(1));
        // Scrolled back: the bottom row becomes the "newer lines" marker.
        let visible = (body.height as usize).saturating_sub(if scroll > 0 { 1 } else { 0 });
        let end = total.saturating_sub(scroll);
        let start = end.saturating_sub(visible);
        for (i, line) in rows[start..end].iter().enumerate() {
            buf.set_line(body.x, body.y + i as u16, line, body.width);
        }
        if start > 0 {
            crate::ui::animation::apply_top_fade(body, 1, theme.card_bg, buf);
        }
        if scroll > 0 {
            let marker = Line::from(vec![Span::styled(
                format!("↓ {} newer", scroll),
                Style::default().fg(theme.text_extra_dim),
            )]);
            buf.set_line(body.x, body.bottom().saturating_sub(1), &marker, body.width);
        }
    }

    draw_divider(chunks[3], theme, buf);

    // 4. Footer: the bindings this modal really handles, plus the live clock.
    let nav_hint = keymap::footer_hints(Scope::Agents);
    let nav_len = nav_hint.chars().count();
    let right_spans = if modal_state.is_running {
        vec![
            Span::styled(anim.spinner_frame(), Style::default().fg(theme.accent_blue)),
            Span::styled(format!(" {}s", modal_state.elapsed_seconds), Style::default().fg(theme.text_dim)),
        ]
    } else {
        let (word, color) = match modal_state.status.as_str() {
            "completed" => ("pass", theme.sage_fg),
            "failed" | "timed_out" | "interrupted" => ("fail", theme.rose_fg),
            other => (other, theme.text_dim),
        };
        vec![
            Span::styled(word, Style::default().fg(color)),
            Span::styled(format!(" {}s", modal_state.elapsed_seconds), Style::default().fg(theme.text_dim)),
        ]
    };
    let right_len: usize = right_spans.iter().map(|s| s.content.chars().count()).sum();
    let spaces = (chunks[4].width as usize).saturating_sub(nav_len + right_len);

    let mut footer_spans = vec![
        Span::styled(nav_hint, Style::default().fg(theme.text_dim)),
        Span::raw(" ".repeat(spaces)),
    ];
    footer_spans.extend(right_spans);
    buf.set_line(chunks[4].x, chunks[4].y, &Line::from(footer_spans), chunks[4].width);
}

fn draw_divider(area: Rect, theme: &HexTheme, buf: &mut Buffer) {
    if area.height == 0 {
        return;
    }
    for x in area.left()..area.right() {
        buf[(x, area.y)].set_symbol("─");
        buf[(x, area.y)].set_fg(theme.border_low);
    }
}

/// Word wrap for the prompt brief; a single over-long word is hard-split.
fn wrap_words(text: &str, width: usize) -> Vec<String> {
    if width == 0 {
        return Vec::new();
    }
    let mut rows: Vec<String> = Vec::new();
    let mut current = String::new();
    for word in text.split_whitespace() {
        let mut word = word;
        while word.chars().count() > width {
            let head: String = word.chars().take(width).collect();
            let consumed = head.len();
            if !current.is_empty() {
                rows.push(std::mem::take(&mut current));
            }
            rows.push(head);
            word = &word[consumed..];
        }
        if current.is_empty() {
            current = word.to_string();
        } else if current.chars().count() + 1 + word.chars().count() <= width {
            current.push(' ');
            current.push_str(word);
        } else {
            rows.push(std::mem::take(&mut current));
            current = word.to_string();
        }
    }
    if !current.is_empty() {
        rows.push(current);
    }
    rows
}

fn ellipsize(text: &str, width: usize) -> String {
    if text.chars().count() <= width {
        return text.to_string();
    }
    if width == 0 {
        return String::new();
    }
    let head: String = text.chars().take(width - 1).collect();
    format!("{head}…")
}
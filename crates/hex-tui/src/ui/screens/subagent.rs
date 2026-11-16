//! Screen 5: Subagent Live Inspection Modal matching the HEX Agent HTML mockup.

use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Widget};

use crate::app::{App, SubagentModalState};
use crate::ui::animation::AnimationState;
use crate::ui::style::HexTheme;

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

    // 2. Centered Modal (width ~66 cols, height ~20 rows)
    let modal_width = 66_u16.min(area.width.saturating_sub(4));
    let modal_height = 20_u16.min(area.height.saturating_sub(2));

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

    let prompt_height = if modal_state.is_expanded {
        5_u16
    } else {
        3_u16
    };

    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(prompt_height), // Prompt Brief
            Constraint::Length(1),             // Divider
            Constraint::Min(6),                // Live tool activity stream
            Constraint::Length(1),             // Divider
            Constraint::Length(1),             // Footer: nav hints & live stopwatch
        ])
        .split(inner);

    // 1. Prompt Brief Section
    let header_line = Line::from(vec![
        Span::styled("prompt", theme.apfel_header_style()),
        Span::raw(" ".repeat((chunks[0].width as usize).saturating_sub(6 + 8))),
        Span::styled("e ", Style::default().fg(theme.text_bright)),
        Span::styled(
            if modal_state.is_expanded { "collapse" } else { "expand" },
            Style::default().fg(theme.text_dim),
        ),
    ]);
    buf.set_line(chunks[0].x, chunks[0].y, &header_line, chunks[0].width);

    let prompt_body = Line::from(vec![Span::styled(&modal_state.prompt, Style::default().fg(theme.text_muted))]);
    buf.set_line(chunks[0].x, chunks[0].y + 1, &prompt_body, chunks[0].width);

    // Divider under prompt
    for x in chunks[1].left()..chunks[1].right() {
        buf[(x, chunks[1].y)].set_symbol("─");
        buf[(x, chunks[1].y)].set_fg(theme.border_low);
    }

    // 2. Activity Stream Section
    let stream_area = chunks[2];
    let mut act_y = stream_area.y;

    if modal_state.activity_lines.is_empty() {
        let awaiting_line = Line::from(vec![Span::styled(
            "awaiting subagent execution...",
            Style::default().fg(theme.text_extra_dim),
        )]);
        buf.set_line(stream_area.x, act_y, &awaiting_line, stream_area.width);
        act_y += 1;
    } else {
        for line_str in &modal_state.activity_lines {
            if act_y >= stream_area.bottom() - 1 {
                break;
            }
            let line = Line::from(vec![Span::styled(
                line_str.as_str(),
                Style::default().fg(theme.text_bright),
            )]);
            buf.set_line(stream_area.x, act_y, &line, stream_area.width);
            act_y += 1;
        }
    }

    // Thinking line with shimmer sweep
    if act_y < stream_area.bottom() {
        let shimmer_text = format!("Executing {}...", modal_state.name);
        let shimmer_spans = anim.render_shimmer(&shimmer_text, theme.text_dim, theme.text_bright);
        buf.set_line(stream_area.x, act_y, &Line::from(shimmer_spans), stream_area.width);
    }

    if modal_state.activity_lines.len() > stream_area.height as usize {
        crate::ui::animation::apply_top_fade(stream_area, 2, theme.card_bg, buf);
    }

    // Divider above footer
    for x in chunks[3].left()..chunks[3].right() {
        buf[(x, chunks[3].y)].set_symbol("─");
        buf[(x, chunks[3].y)].set_fg(theme.border_low);
    }

    // 3. Footer
    let nav_hint = Span::styled(
        "up/down scroll · e expand prompt · esc close",
        Style::default().fg(theme.text_dim),
    );

    let right_spans = vec![
        Span::styled(anim.spinner_frame(), Style::default().fg(theme.accent_blue)),
        Span::styled(format!(" {}s", modal_state.elapsed_seconds), Style::default().fg(theme.text_dim)),
    ];

    let right_len = 1 + 1 + format!("{}", modal_state.elapsed_seconds).len() + 1;
    let spaces = (chunks[4].width as usize).saturating_sub(44 + right_len);

    let mut footer_spans = vec![nav_hint, Span::raw(" ".repeat(spaces))];
    footer_spans.extend(right_spans);

    buf.set_line(chunks[4].x, chunks[4].y, &Line::from(footer_spans), chunks[4].width);
}

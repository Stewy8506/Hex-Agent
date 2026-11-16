//! Screen 3: Command & Navigation Palette Modal matching the HEX Agent HTML mockup.

use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Widget};

use crate::app::App;
use crate::ui::animation::AnimationState;
use crate::ui::style::HexTheme;

pub fn render(app: &App, theme: &HexTheme, anim: &AnimationState, area: Rect, buf: &mut Buffer) {
    if area.height < 14 || area.width < 45 {
        return;
    }

    // 1. Dim the backdrop
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            let cell = &mut buf[(x, y)];
            cell.set_style(Style::default().fg(theme.text_extra_dim).bg(theme.canvas_bg));
        }
    }

    // 2. Top-anchored modal (width ~58 cols, anchored 2-4 rows below header)
    let modal_width = 58_u16.min(area.width.saturating_sub(4));
    let modal_height = 14_u16.min(area.height.saturating_sub(6));

    let modal_x = area.x + (area.width.saturating_sub(modal_width)) / 2;
    let modal_y = area.y + 3; // Top-anchored 72px equivalent
    let modal_rect = Rect::new(modal_x, modal_y, modal_width, modal_height);

    Clear.render(modal_rect, buf);

    let modal_block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme.border_modal))
        .style(Style::default().bg(theme.card_bg));
    let inner = modal_block.inner(modal_rect);
    modal_block.render(modal_rect, buf);

    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1), // Search input
            Constraint::Length(1), // Divider
            Constraint::Min(4),    // Categorized results
            Constraint::Length(1), // Divider
            Constraint::Length(1), // Footer: "3 results" ... "enter select · esc close"
        ])
        .split(inner);

    // 1. Search Bar
    let cursor_span = if anim.cursor_visible() {
        Span::styled("█", Style::default().fg(theme.accent_blue))
    } else {
        Span::raw(" ")
    };

    let query_display = &app.palette_query;

    let search_line = Line::from(vec![
        Span::styled("> ", Style::default().fg(theme.text_dim)),
        Span::styled(query_display, Style::default().fg(theme.text_bright).add_modifier(Modifier::BOLD)),
        cursor_span,
    ]);
    buf.set_line(chunks[0].x, chunks[0].y, &search_line, chunks[0].width);

    // Divider under search bar
    for x in chunks[1].left()..chunks[1].right() {
        buf[(x, chunks[1].y)].set_symbol("─");
        buf[(x, chunks[1].y)].set_fg(theme.border_low);
    }

    // 2. Categorized Results
    let results_area = chunks[2];
    let mut current_y = results_area.y;

    let q_lower = app.palette_query.to_lowercase();
    let filtered_items: Vec<_> = app
        .palette_items
        .iter()
        .filter(|item| {
            if q_lower.is_empty() {
                true
            } else {
                item.title.to_lowercase().contains(&q_lower)
                    || item.category.to_lowercase().contains(&q_lower)
                    || item.shortcut.to_lowercase().contains(&q_lower)
            }
        })
        .collect();

    // Group items by category
    let mut current_cat = "";
    for (idx, item) in filtered_items.iter().enumerate() {
        if current_y >= results_area.bottom() {
            break;
        }

        // Category Header
        if item.category != current_cat {
            current_cat = &item.category;
            let cat_line = Line::from(vec![Span::styled(&item.category, theme.apfel_header_style())]);
            buf.set_line(results_area.x + 1, current_y, &cat_line, results_area.width.saturating_sub(2));
            current_y += 1;
        }

        if current_y >= results_area.bottom() {
            break;
        }

        let is_selected = idx == app.selected_palette_index;
        let row_bg = if is_selected {
            theme.selection_bg
        } else {
            theme.card_bg
        };

        for x in results_area.left()..results_area.right() {
            buf[(x, current_y)].set_bg(row_bg);
        }

        let marker = if is_selected {
            Span::styled("> ", Style::default().fg(theme.accent_blue).bg(row_bg))
        } else {
            Span::styled("  ", Style::default().bg(row_bg))
        };

        // Highlight matched characters in title
        let title_spans = highlight_matches(&item.title, query_display, theme, row_bg);

        let mut row_spans = vec![marker];
        row_spans.extend(title_spans);

        let spaces = (results_area.width as usize).saturating_sub(item.title.len() + 4 + item.shortcut.len());
        row_spans.push(Span::raw(" ".repeat(spaces)));
        row_spans.push(Span::styled(&item.shortcut, Style::default().fg(theme.text_dim).bg(row_bg)));

        buf.set_line(results_area.x, current_y, &Line::from(row_spans), results_area.width);
        current_y += 1;
    }

    // Divider above footer
    for x in chunks[3].left()..chunks[3].right() {
        buf[(x, chunks[3].y)].set_symbol("─");
        buf[(x, chunks[3].y)].set_fg(theme.border_low);
    }

    // 3. Footer
    let count_span = Span::styled(
        format!("{} results", filtered_items.len()),
        Style::default().fg(theme.text_dim),
    );
    let nav_span = Span::styled("enter select · esc close", Style::default().fg(theme.text_dim));

    let footer_spans = vec![
        count_span,
        Span::raw(" ".repeat((inner.width as usize).saturating_sub(9 + 25))),
        nav_span,
    ];
    buf.set_line(chunks[4].x, chunks[4].y, &Line::from(footer_spans), chunks[4].width);
}

fn highlight_matches<'a>(text: &'a str, query: &str, theme: &HexTheme, bg: ratatui::style::Color) -> Vec<Span<'a>> {
    let q_lower = query.to_lowercase();
    let mut spans = Vec::new();

    for c in text.chars() {
        let is_match = q_lower.contains(c.to_ascii_lowercase());
        if is_match {
            spans.push(Span::styled(
                c.to_string(),
                Style::default()
                    .fg(theme.text_bright)
                    .bg(bg)
                    .add_modifier(Modifier::BOLD),
            ));
        } else {
            spans.push(Span::styled(c.to_string(), Style::default().fg(theme.text_muted).bg(bg)));
        }
    }

    spans
}

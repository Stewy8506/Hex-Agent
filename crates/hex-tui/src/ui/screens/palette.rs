//! Screen 3: Command & Navigation Palette matching the HEX Agent HTML mockup.
//!
//! Opened with `/` on an empty composer or Ctrl+P. While it is open the query
//! owns the keyboard, so the composer underneath is never disturbed.

use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Widget};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::app::App;
use crate::ui::animation::AnimationState;
use crate::ui::style::HexTheme;

const PANEL_MIN_WIDTH: u16 = 80;
const PANEL_MAX_WIDTH: u16 = 120;
const PANEL_TOP: u16 = 4;

enum Row<'a> {
    Header(&'a str),
    Item(usize),
}

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

    let results = app.palette_results();
    let grouped = app.palette_query.trim().trim_start_matches('/').is_empty();

    // Flatten to display rows: one per result, plus a header per category when
    // browsing, so the selection can be scrolled into view as a single unit.
    let mut rows: Vec<Row<'_>> = Vec::new();
    let mut last_category = "";
    for (idx, m) in results.iter().enumerate() {
        let item = &app.palette_items[m.item_index];
        if grouped && item.category != last_category {
            rows.push(Row::Header(&item.category));
            last_category = &item.category;
        }
        rows.push(Row::Item(idx));
    }

    // 2. Top-anchored modal, sized to its content.
    let modal_width = ((u32::from(area.width) * 3 / 5) as u16)
        .clamp(PANEL_MIN_WIDTH, PANEL_MAX_WIDTH)
        .min(area.width.saturating_sub(4));
    let chrome = 6u16; // border x2, search, divider, divider, footer
    let max_height = area.height.saturating_sub(PANEL_TOP + 1);
    let modal_height = ((rows.len() as u16).saturating_add(chrome))
        .min(max_height)
        .max(chrome + 2);

    let modal_x = area.x + (area.width.saturating_sub(modal_width)) / 2;
    let modal_rect = Rect::new(modal_x, area.y + PANEL_TOP, modal_width, modal_height);

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
            Constraint::Min(1),    // Results
            Constraint::Length(1), // Divider
            Constraint::Length(1), // Footer
        ])
        .split(inner);

    // 1. Search bar
    let cursor_span = if anim.cursor_visible() {
        Span::styled("█", Style::default().fg(theme.accent_blue))
    } else {
        Span::raw(" ")
    };
    let search_line = Line::from(vec![
        Span::styled("> ", Style::default().fg(theme.text_dim)),
        Span::styled(
            app.palette_query.as_str(),
            Style::default().fg(theme.text_bright).add_modifier(Modifier::BOLD),
        ),
        cursor_span,
    ]);
    buf.set_line(chunks[0].x + 1, chunks[0].y, &search_line, chunks[0].width.saturating_sub(2));

    draw_rule(buf, chunks[1], theme.border_low);

    // 2. Results, scrolled so the selection is always on screen.
    let results_area = chunks[2];
    let visible = results_area.height as usize;
    let selected_pos = rows
        .iter()
        .position(|r| matches!(r, Row::Item(i) if *i == app.selected_palette_index))
        .unwrap_or(0);
    let max_offset = rows.len().saturating_sub(visible);
    let offset = selected_pos
        .saturating_sub(visible.saturating_sub(1))
        .min(max_offset);

    let mut current_y = results_area.y;
    for row in rows.iter().skip(offset) {
        if current_y >= results_area.bottom() {
            break;
        }
        let idx = match row {
            Row::Header(category) => {
                let header = Line::from(Span::styled(
                    *category,
                    Style::default().fg(theme.text_extra_dim),
                ));
                buf.set_line(results_area.x + 1, current_y, &header, results_area.width.saturating_sub(2));
                current_y += 1;
                continue;
            }
            Row::Item(idx) => *idx,
        };
        let m = &results[idx];
        let item = &app.palette_items[m.item_index];

        let is_selected = idx == app.selected_palette_index;
        let row_bg = if is_selected { theme.selection_bg } else { theme.card_bg };
        for x in results_area.left()..results_area.right() {
            buf[(x, current_y)].set_bg(row_bg);
        }

        let title_max = results_area.width.saturating_sub(3) as usize;
        let shown: Vec<char> = if item.title.width() > title_max {
            let mut chars = Vec::new();
            let mut used = 0;
            for c in item.title.chars() {
                let width = c.width().unwrap_or(0);
                if used + width > title_max.saturating_sub(1) {
                    break;
                }
                chars.push(c);
                used += width;
            }
            chars.push('…');
            chars
        } else {
            item.title.chars().collect()
        };

        let mut spans = vec![if is_selected {
            Span::styled("> ", Style::default().fg(theme.accent_blue).bg(row_bg))
        } else {
            Span::styled("  ", Style::default().bg(row_bg))
        }];
        spans.extend(highlight(&shown, &m.title_matched, theme, row_bg, theme.text_muted));

        buf.set_line(results_area.x, current_y, &Line::from(spans), results_area.width);
        current_y += 1;
    }

    if results.is_empty() && results_area.height > 0 {
        let empty = Line::from(Span::styled(
            "no matching command",
            Style::default().fg(theme.text_extra_dim),
        ));
        buf.set_line(results_area.x + 1, results_area.y, &empty, results_area.width.saturating_sub(2));
    }

    draw_rule(buf, chunks[3], theme.border_low);

    // 3. Footer
    let count = format!(
        "{} result{}",
        results.len(),
        if results.len() == 1 { "" } else { "s" }
    );
    let nav = crate::ui::keymap::footer_hints(crate::ui::keymap::Scope::Palette);
    let footer_width = chunks[4].width as usize;
    let gap = footer_width.saturating_sub(count.chars().count() + nav.chars().count() + 2);
    let footer = Line::from(vec![
        Span::styled(" ", Style::default()),
        Span::styled(count, Style::default().fg(theme.text_dim)),
        Span::raw(" ".repeat(gap)),
        Span::styled(nav, Style::default().fg(theme.text_dim)),
        Span::styled(" ", Style::default()),
    ]);
    buf.set_line(chunks[4].x, chunks[4].y, &footer, chunks[4].width);
}

fn draw_rule(buf: &mut Buffer, area: Rect, color: Color) {
    for x in area.left()..area.right() {
        buf[(x, area.y)].set_symbol("─");
        buf[(x, area.y)].set_fg(color);
    }
}

/// Bold the characters the query actually matched, in order — not every letter
/// that happens to appear in the query.
fn highlight(
    chars: &[char],
    matched: &[usize],
    theme: &HexTheme,
    bg: Color,
    base_fg: Color,
) -> Vec<Span<'static>> {
    let mut spans: Vec<Span<'static>> = Vec::new();
    for (i, c) in chars.iter().enumerate() {
        let is_match = matched.contains(&i);
        let style = if is_match {
            Style::default().fg(theme.text_bright).bg(bg).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(base_fg).bg(bg)
        };
        match spans.last_mut() {
            Some(last) if last.style == style => last.content.to_mut().push(*c),
            _ => spans.push(Span::styled(c.to_string(), style)),
        }
    }
    spans
}
//! Screen 2: Tool & Bash Permission Approval Modal matching the HEX Agent HTML mockup.

use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Widget};

use crate::app::App;
use crate::ui::style::HexTheme;

pub fn render(app: &App, theme: &HexTheme, area: Rect, buf: &mut Buffer) {
    if area.height < 12 || area.width < 40 {
        return;
    }

    let req = match app.active_approval.as_ref() {
        Some(r) => r,
        None => return,
    };

    // 1. Dim the entire backdrop (semi-transparent overlay emulation)
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            let cell = &mut buf[(x, y)];
            cell.set_style(Style::default().fg(theme.text_extra_dim).bg(theme.canvas_bg));
        }
    }

    // 2. Centered Modal Dialog (56 cols wide, ~14 rows high)
    let modal_width = 56_u16.min(area.width.saturating_sub(4));
    let modal_height = 14_u16.min(area.height.saturating_sub(2));

    let modal_x = area.x + (area.width.saturating_sub(modal_width)) / 2;
    let modal_y = area.y + (area.height.saturating_sub(modal_height)) / 2;
    let modal_rect = Rect::new(modal_x, modal_y, modal_width, modal_height);

    // Clear underlying buffer
    Clear.render(modal_rect, buf);

    // Outer modal block with border (#333333) and inner background (#0c0c0c)
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
            Constraint::Length(1), // Title line: "Permission required" ... "bash"
            Constraint::Length(1), // spacing
            Constraint::Length(3), // Command preview box
            Constraint::Length(1), // Description (Deletes 214 files in ./dist...)
            Constraint::Length(1), // Divider line
            Constraint::Length(3), // 3-way radio options
            Constraint::Min(0),
        ])
        .split(inner);

    // 1. Title line
    let tool_name = &req.tool_name;

    let title_line = Line::from(vec![
        Span::styled("Permission required", theme.apfel_title_style()),
        Span::raw(" ".repeat((inner.width as usize).saturating_sub(19 + tool_name.len()))),
        Span::styled(tool_name.as_str(), Style::default().fg(theme.text_dim)),
    ]);
    Paragraph::new(title_line).render(chunks[0], buf);

    // 2. Command Preview Box (bg #151515, border-radius 4px)
    let cmd_str = &req.parameters;

    let cmd_box_block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme.border_low))
        .style(Style::default().bg(theme.hover_bg));
    let cmd_inner = cmd_box_block.inner(chunks[2]);
    cmd_box_block.render(chunks[2], buf);

    let cmd_text = head_ellipsize(cmd_str, cmd_inner.width as usize);
    let mut cmd_spans = vec![
        Span::styled("$ ", Style::default().fg(theme.text_dim)),
        Span::styled(cmd_text.as_str(), Style::default().fg(theme.text_bright)),
    ];
    // The working directory rides along on the right when there is room for it.
    let cwd = req.cwd.trim();
    if !cwd.is_empty() {
        let used = 2 + cmd_text.chars().count();
        let room = (cmd_inner.width as usize).saturating_sub(used + 2);
        let cwd_len = cwd.chars().count();
        if cwd_len <= room {
            cmd_spans.push(Span::raw(" ".repeat(room - cwd_len + 2)));
            cmd_spans.push(Span::styled(cwd, Style::default().fg(theme.text_extra_dim)));
        }
    }
    buf.set_line(cmd_inner.x, cmd_inner.y, &Line::from(cmd_spans), cmd_inner.width);

    // 3. Impact Description
    let impact = if req.blast_radius.trim().is_empty() {
        req.reason.as_str()
    } else {
        req.blast_radius.as_str()
    };
    let desc_str = head_ellipsize(impact, inner.width as usize);
    let desc_line = Line::from(vec![Span::styled(desc_str.as_str(), Style::default().fg(theme.text_dim))]);
    Paragraph::new(desc_line).render(chunks[3], buf);

    // 4. Divider
    for x in chunks[4].left()..chunks[4].right() {
        buf[(x, chunks[4].y)].set_symbol("─");
        buf[(x, chunks[4].y)].set_fg(theme.border_low);
    }

    // 5. Radio Options List
    let bin_name = req.parameters
        .split_whitespace()
        .next()
        .map(|p| std::path::Path::new(p).file_name().and_then(|f| f.to_str()).unwrap_or(p))
        .unwrap_or(&req.tool_name);
    let allow_project_label = format!("Allow {} in this project", bin_name);

    let options = [
        ("Allow once", "y"),
        (allow_project_label.as_str(), "a"),
        ("Deny", "n"),
    ];

    let selected = app.selected_permission_index.min(options.len() - 1);
    for (idx, (label, hotkey)) in options.iter().enumerate() {
        let opt_y = chunks[5].y + idx as u16;
        if opt_y >= chunks[5].bottom() {
            break;
        }

        let is_selected = idx == selected;
        let row_bg = if is_selected {
            theme.selection_bg
        } else {
            theme.card_bg
        };

        for x in inner.left()..inner.right() {
            buf[(x, opt_y)].set_bg(row_bg);
        }

        let (marker, title_color) = if is_selected {
            (">", theme.text_bright)
        } else {
            (" ", theme.text_muted)
        };

        let mut spans = vec![
            Span::styled(
                format!("{} ", marker),
                Style::default()
                    .fg(if is_selected { theme.accent_blue } else { row_bg })
                    .bg(row_bg),
            ),
            Span::styled(*label, Style::default().fg(title_color).bg(row_bg)),
        ];

        let spaces = (inner.width as usize).saturating_sub(label.chars().count() + 2 + hotkey.chars().count());
        spans.push(Span::styled(" ".repeat(spaces), Style::default().bg(row_bg)));
        spans.push(Span::styled(*hotkey, Style::default().fg(theme.text_dim).bg(row_bg)));

        buf.set_line(inner.x, opt_y, &Line::from(spans), inner.width);
    }
}

/// Clip a single-line string to `width` columns, marking the cut with an ellipsis.
fn head_ellipsize(text: &str, width: usize) -> String {
    if width == 0 {
        return String::new();
    }
    if text.chars().count() <= width {
        return text.to_string();
    }
    let kept: String = text.chars().take(width.saturating_sub(1)).collect();
    format!("{kept}…")
}

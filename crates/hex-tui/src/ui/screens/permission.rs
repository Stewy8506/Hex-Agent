//! Tool permission approval, including compact and fail-closed resize layouts.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Widget};
use unicode_segmentation::UnicodeSegmentation;

use crate::app::App;
use crate::ui::style::HexTheme;

pub fn render(app: &App, theme: &HexTheme, area: Rect, buf: &mut Buffer) {
    app.approval_can_allow.set(false);
    if app.active_approval.is_none() || area.is_empty() {
        return;
    }

    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            buf[(x, y)].set_style(
                Style::default()
                    .fg(theme.text_extra_dim)
                    .bg(theme.canvas_bg),
            );
        }
    }

    // Drop decoration before sacrificing review context.
    let width = 80_u16.min(area.width.saturating_sub(4));
    let height = area.height.saturating_sub(2);
    if let Some(lines) = approval_lines(
        app,
        theme,
        width.saturating_sub(2),
        height.saturating_sub(2),
    ) {
        let modal_height = lines.len() as u16 + 2;
        let modal = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - modal_height) / 2,
            width,
            modal_height,
        );
        Clear.render(modal, buf);
        let block = Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(theme.border_modal))
            .style(Style::default().bg(theme.card_bg));
        let inner = block.inner(modal);
        block.render(modal, buf);
        Paragraph::new(lines).render(inner, buf);
        app.approval_can_allow.set(!app.approval_pending);
        return;
    }

    Clear.render(area, buf);
    Block::default()
        .style(Style::default().bg(theme.card_bg))
        .render(area, buf);
    if let Some(lines) = approval_lines(app, theme, area.width, area.height) {
        Paragraph::new(lines).render(area, buf);
        app.approval_can_allow.set(!app.approval_pending);
    } else {
        render_resize_fallback(app, theme, area, buf);
    }
}

// Allow only when every authority-bearing field and control will be painted.
fn approval_lines(
    app: &App,
    theme: &HexTheme,
    width: u16,
    height: u16,
) -> Option<Vec<Line<'static>>> {
    if width < 4 || height == 0 {
        return None;
    }
    let req = app.active_approval.as_ref()?;
    let mut lines = Vec::new();
    let text_style = Style::default().fg(theme.text_bright).bg(theme.card_bg);
    for (text, style) in [
        (
            format!("Permission required ({})", req.id),
            theme.apfel_title_style(),
        ),
        (
            format!("Tool: {}  Risk: {}", req.tool_name, req.risk_level),
            text_style,
        ),
        (format!("Cwd: {}", req.cwd), text_style),
        (
            format!("Parameters: {}", req.parameters),
            text_style.bg(theme.hover_bg),
        ),
        (format!("Reason: {}", req.reason), text_style),
        (format!("Impact: {}", req.blast_radius), text_style),
    ] {
        push_wrapped(&mut lines, &text, style, width, height)?;
    }

    if let Some(error) = &app.approval_error {
        push_wrapped(
            &mut lines,
            &format!("Error: {error}"),
            text_style,
            width,
            height,
        )?;
    }
    if app.approval_pending {
        push_wrapped(
            &mut lines,
            "Submitting decision; please wait.",
            text_style,
            width,
            height,
        )?;
        push_wrapped(
            &mut lines,
            "Approval controls disabled while pending.",
            text_style,
            width,
            height,
        )?;
    } else {
        if app.approval_error.is_some() {
            push_wrapped(
                &mut lines,
                "Retry: choose a decision below.",
                text_style,
                width,
                height,
            )?;
        }
        let selected = app.selected_permission_index.min(2);
        for (index, (label, key)) in [
            ("Allow once", "y"),
            ("Allow matching calls in this task", "a"),
            ("Deny", "n"),
        ]
        .into_iter()
        .enumerate()
        {
            let bg = if index == selected {
                theme.selection_bg
            } else {
                theme.card_bg
            };
            let style = Style::default()
                .fg(if index == selected {
                    theme.text_bright
                } else {
                    theme.text_muted
                })
                .bg(bg);
            let marker = if index == selected { ">" } else { " " };
            let spaces = (width as usize)
                .saturating_sub(2 + Span::raw(label).width() + 1)
                .max(1);
            let first_row = lines.len();
            push_wrapped(
                &mut lines,
                &format!("{marker} {label}{}{key}", " ".repeat(spaces)),
                style,
                width,
                height,
            )?;
            let content = lines[first_row].spans[0].content.to_string();
            lines[first_row].spans = vec![
                Span::styled(
                    content[..2].to_string(),
                    style.fg(if index == selected {
                        theme.accent_blue
                    } else {
                        bg
                    }),
                ),
                Span::styled(content[2..].to_string(), style),
            ];
            let last = lines.last_mut().unwrap();
            let tail = last.spans.last_mut().unwrap();
            let content = tail.content.to_string();
            tail.content = content[..content.len() - key.len()].to_string().into();
            last.spans.push(Span::styled(key, style.fg(theme.text_dim)));
        }
        push_wrapped(
            &mut lines,
            "Enter confirm; Esc deny",
            text_style,
            width,
            height,
        )?;
    }
    Some(lines)
}

// Invisible or clipped context cannot authorize execution.
fn push_wrapped(
    lines: &mut Vec<Line<'static>>,
    text: &str,
    style: Style,
    width: u16,
    height: u16,
) -> Option<()> {
    for hard_line in text.split('\n') {
        let mut row = String::new();
        let mut used = 0;
        for grapheme in hard_line.graphemes(true) {
            let columns = Span::raw(grapheme).width();
            if grapheme.chars().any(char::is_control) || columns == 0 || columns > width as usize {
                return None;
            }
            if used + columns > width as usize {
                lines.push(Line::styled(std::mem::take(&mut row), style));
                if lines.len() >= height as usize {
                    return None;
                }
                used = 0;
            }
            row.push_str(grapheme);
            used += columns;
        }
        lines.push(Line::styled(row, style));
        if lines.len() > height as usize {
            return None;
        }
    }
    Some(())
}

fn render_resize_fallback(app: &App, theme: &HexTheme, area: Rect, buf: &mut Buffer) {
    let style = Style::default().fg(theme.text_bright).bg(theme.card_bg);
    let pending = app.approval_pending;
    let headline = if pending {
        "Approval pending"
    } else if app.approval_error.is_some() {
        "Approval failed"
    } else {
        "Permission required"
    };
    let action = if pending {
        ["Wait; resize to review", "Wait; resize", "Wait", "P"]
    } else {
        ["n Deny / Esc deny; resize", "n Deny; resize", "n Deny", "n"]
    };
    // Even a single cell advertises deny (or pending), never an allow key.
    let fitting = |choices: &[&str]| {
        choices
            .iter()
            .find(|text| Span::raw(**text).width() <= area.width as usize)
            .unwrap_or(&choices[choices.len() - 1])
            .to_string()
    };
    if area.height == 1 {
        Paragraph::new(fitting(&action))
            .style(style)
            .render(area, buf);
        return;
    }
    let title = fitting(&[headline, "Approval", "!"]);
    Paragraph::new(title)
        .style(style)
        .render(Rect::new(area.x, area.y, area.width, 1), buf);
    if area.height > 2 {
        let resize = if app.approval_error.is_some() && !pending {
            fitting(&[
                "Resize to review and retry",
                "Resize to retry",
                "Resize",
                "!",
            ])
        } else {
            fitting(&[
                "Resize to review; allow disabled",
                "Resize to review",
                "Resize",
                "!",
            ])
        };
        Paragraph::new(resize)
            .style(style)
            .render(Rect::new(area.x, area.y + 1, area.width, 1), buf);
        if area.height > 3 {
            if let Some(error) = &app.approval_error {
                // A clipped error is diagnostic only: the allow gate stays false.
                Paragraph::new(format!("Error: {error}"))
                    .style(style)
                    .render(Rect::new(area.x, area.y + 2, area.width, 1), buf);
            }
        }
    }
    Paragraph::new(fitting(&action))
        .style(style)
        .render(Rect::new(area.x, area.bottom() - 1, area.width, 1), buf);
}

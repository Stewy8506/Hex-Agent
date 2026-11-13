//! The always-bottom HEX composer. Output never moves the operator's cursor.

use ratatui::layout::{Position, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, List, ListItem, Paragraph};
use ratatui::Frame;
use unicode_width::UnicodeWidthStr;

use crate::app::{App, Focus};
use crate::ui::design::{skin, Skin, PROMPTS};

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let skin = skin(&app.skin_name);
    let focused = app.focus == Focus::Input;
    let border = if focused { skin.border } else { skin.muted };

    let (mode_fg, mode_bg, mode_text) = match app.mode.as_str() {
        "plan" => (skin.surface, skin.heading, " plan "),
        "code" => (ratatui::style::Color::White, ratatui::style::Color::Magenta, " code "),
        _ => (skin.surface, skin.accent, " agent "),
    };

    let action_span = if app.is_busy {
        Span::styled(
            format!("── [{}] ", app.status_message),
            Style::default().fg(skin.accent).add_modifier(Modifier::BOLD),
        )
    } else {
        Span::styled("── enter send · tab mode · /help ", Style::default().fg(skin.muted))
    };

    let footer = Line::from(vec![
        Span::styled(
            mode_text,
            Style::default().fg(mode_fg).bg(mode_bg).add_modifier(Modifier::BOLD),
        ),
        Span::styled(" · ", Style::default().fg(skin.muted)),
        Span::styled(format!("{} ", app.project_id), Style::default().fg(skin.text)),
        Span::styled("· ", Style::default().fg(skin.muted)),
        Span::styled(format!("{} ", app.git_branch), Style::default().fg(skin.accent)),
        Span::styled("· ", Style::default().fg(skin.muted)),
        Span::styled(
            format!("{} ", app.model_name),
            Style::default().fg(skin.heading).add_modifier(Modifier::BOLD),
        ),
        action_span,
    ]);

    let title = if focused {
        if app.is_busy {
            " Command Prompt [Reasoning...] "
        } else {
            " Command Prompt [Enter to Send, / for Commands] "
        }
    } else {
        " Command Prompt "
    };

    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(border))
        .title(Span::styled(
            title,
            Style::default()
                .fg(if focused { skin.heading } else { skin.muted })
                .add_modifier(Modifier::BOLD),
        ))
        .title_bottom(footer);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.height == 0 || inner.width == 0 {
        return;
    }

    let prefix = "❯ ";
    let available = inner
        .width
        .saturating_sub(UnicodeWidthStr::width(prefix) as u16) as usize;
    let cursor_char = app.cursor_pos.min(app.input_buffer.chars().count());
    let (display, cursor_col) = input_window(&app.input_buffer, cursor_char, available);
    let line = if app.input_buffer.is_empty() {
        let hint = PROMPTS[app.suggestion_index % PROMPTS.len()];
        Line::from(vec![
            Span::styled(
                prefix,
                Style::default()
                    .fg(skin.accent)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(hint, Style::default().fg(skin.muted)),
        ])
    } else {
        Line::from(vec![
            Span::styled(
                prefix,
                Style::default()
                    .fg(skin.accent)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(display, Style::default().fg(skin.text)),
        ])
    };
    frame.render_widget(Paragraph::new(line), inner);
    if focused && app.animation_frame % 20 < 12 {
        let x = inner.x + UnicodeWidthStr::width(prefix) as u16 + cursor_col as u16;
        if x < inner.right() {
            frame.set_cursor_position(Position::new(x, inner.y));
        }
    }
    render_suggestions(frame, app, area, skin);
}

fn input_window(text: &str, cursor: usize, width: usize) -> (String, usize) {
    if width == 0 {
        return (String::new(), 0);
    }
    let chars: Vec<char> = text.chars().collect();
    let full: String = chars.iter().collect();
    if UnicodeWidthStr::width(full.as_str()) <= width {
        let before: String = chars[..cursor].iter().collect();
        return (full, UnicodeWidthStr::width(before.as_str()));
    }
    let mut start = cursor.saturating_sub(width.saturating_sub(2));
    while start < cursor {
        let before: String = chars[start..cursor].iter().collect();
        if UnicodeWidthStr::width(before.as_str()) <= width.saturating_sub(1) {
            break;
        }
        start += 1;
    }
    let mut out = String::from("…");
    let mut used = 1usize;
    for ch in &chars[start..] {
        let w = UnicodeWidthStr::width(ch.encode_utf8(&mut [0; 4]));
        if used + w > width {
            break;
        }
        out.push(*ch);
        used += w;
    }
    let before: String = chars[start..cursor].iter().collect();
    (out, 1 + UnicodeWidthStr::width(before.as_str()))
}

fn render_suggestions(frame: &mut Frame, app: &App, composer: Rect, skin: Skin) {
    if app.focus != Focus::Input || app.slash_suggestions.is_empty() {
        return;
    }
    let height = (app.slash_suggestions.len() as u16 + 2).min(7);
    let width = composer.width.saturating_sub(4).min(58);
    if width < 18 || composer.y < height {
        return;
    }
    let popup = Rect::new(composer.x + 2, composer.y - height, width, height);
    frame.render_widget(Clear, popup);
    let items = app
        .slash_suggestions
        .iter()
        .enumerate()
        .map(|(i, cmd)| {
            let selected = i == app.selected_slash_index;
            let bg = if selected { skin.accent } else { skin.surface };
            let fg = if selected { skin.surface } else { skin.text };
            ListItem::new(Line::from(vec![
                Span::styled(
                    format!("{:<11}", cmd.name),
                    Style::default()
                        .fg(if selected { skin.surface } else { skin.accent })
                        .bg(bg)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(cmd.description, Style::default().fg(fg).bg(bg)),
            ]))
            .style(Style::default().bg(skin.surface))
        })
        .collect::<Vec<_>>();
    frame.render_widget(
        List::new(items).block(
            Block::default()
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(skin.border))
                .title(" Commands "),
        ),
        popup,
    );
}

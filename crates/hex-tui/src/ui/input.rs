//! The always-bottom HEX composer. Output never moves the operator's cursor.

use ratatui::layout::{Alignment, Position, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Paragraph};
use ratatui::Frame;
use unicode_width::UnicodeWidthStr;

use crate::app::{App, Focus};
use crate::ui::design::{skin, PROMPTS, SPINNER};

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let skin = skin(&app.skin_name);
    let focused = app.focus == Focus::Input;
    let border = if focused { skin.border } else { skin.muted };

    let ws_name = if app.project_id == "default" && !app.workspace_path.is_empty() {
        std::path::Path::new(&app.workspace_path)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(&app.project_id)
    } else {
        &app.project_id
    };

    let mode_span = match app.mode.as_str() {
        "plan" => Span::styled("plan", Style::default().fg(skin.heading).add_modifier(Modifier::BOLD)),
        "code" => Span::styled("code", Style::default().fg(ratatui::style::Color::Magenta).add_modifier(Modifier::BOLD)),
        _ => Span::styled("agent", Style::default().fg(skin.accent).add_modifier(Modifier::BOLD)),
    };

    let left_footer = Line::from(vec![
        Span::raw(" "),
        mode_span,
        Span::styled(" · ", Style::default().fg(skin.muted)),
        Span::styled(format!("{} ", ws_name), Style::default().fg(skin.accent).add_modifier(Modifier::BOLD)),
        Span::styled("· ", Style::default().fg(skin.muted)),
        Span::styled(format!("{} ", app.git_branch), Style::default().fg(skin.accent)),
        Span::styled("· ", Style::default().fg(skin.muted)),
        Span::styled(
            format!("{} ", app.model_label()),
            Style::default().fg(skin.heading),
        ),
    ]).alignment(Alignment::Left);

    let right_footer = if app.is_busy {
        let elapsed_str = crate::app::fmt_elapsed(
            app.busy_started_at.map(|t| t.elapsed()).unwrap_or_default()
        );
        let spin_char = SPINNER[(app.animation_frame as usize / 2) % SPINNER.len()];
        Line::from(vec![
            Span::styled(
                format!(" [{} {} · {}] ", spin_char, app.status_message, elapsed_str),
                Style::default().fg(skin.accent).add_modifier(Modifier::BOLD),
            ),
        ]).alignment(Alignment::Right)
    } else {
        Line::from(vec![
            Span::styled(" enter send · tab mode · /help ", Style::default().fg(skin.muted)),
        ]).alignment(Alignment::Right)
    };

    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(border))
        .title_bottom(left_footer)
        .title_bottom(right_footer);
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


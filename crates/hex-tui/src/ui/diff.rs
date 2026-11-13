//! Colorized unified diff viewer widget rendering added, deleted, and context lines.

use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::Frame;

use hex_protocol::diff::DiffLineType;

use crate::app::{App, Focus};

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let is_focused = app.focus == Focus::Diff;
    let border_color = if is_focused {
        Color::Green
    } else {
        Color::DarkGray
    };

    let title = if is_focused {
        " Diff Viewer [FOCUSED - ↑/↓ to Scroll] "
    } else {
        " Diff Viewer "
    };

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border_color))
        .title(Span::styled(
            title,
            Style::default()
                .fg(if is_focused { Color::Green } else { Color::White })
                .add_modifier(if is_focused { Modifier::BOLD } else { Modifier::empty() }),
        ));

    let mut lines: Vec<Line> = Vec::new();

    if let Some(diff) = &app.active_diff {
        if diff.files.is_empty() {
            lines.push(Line::from(vec![
                Span::styled("No file changes in current diff.", Style::default().fg(Color::DarkGray)),
            ]));
        }

        for file in &diff.files {
            let old_str = file.old_path.as_deref().unwrap_or("/dev/null");
            let new_str = file.new_path.as_deref().unwrap_or("/dev/null");

            lines.push(Line::from(vec![
                Span::styled(format!("--- a/{}", old_str), Style::default().fg(Color::LightRed).add_modifier(Modifier::BOLD)),
            ]));
            lines.push(Line::from(vec![
                Span::styled(format!("+++ b/{}", new_str), Style::default().fg(Color::LightGreen).add_modifier(Modifier::BOLD)),
            ]));

            for hunk in &file.hunks {
                let heading_str = hunk.heading.as_deref().unwrap_or("");
                let header_text = format!(
                    "@@ -{},{} +{},{} @@ {}",
                    hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count, heading_str
                );
                lines.push(Line::from(vec![
                    Span::styled(header_text, Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
                ]));

                for diff_line in &hunk.lines {
                    match diff_line.line_type {
                        DiffLineType::Added => {
                            lines.push(Line::from(vec![
                                Span::styled(format!("+ {}", diff_line.content), Style::default().fg(Color::LightGreen)),
                            ]));
                        }
                        DiffLineType::Deleted => {
                            lines.push(Line::from(vec![
                                Span::styled(format!("- {}", diff_line.content), Style::default().fg(Color::LightRed)),
                            ]));
                        }
                        DiffLineType::Context => {
                            lines.push(Line::from(vec![
                                Span::styled(format!("  {}", diff_line.content), Style::default().fg(Color::DarkGray)),
                            ]));
                        }
                        _ => {
                            lines.push(Line::from(vec![
                                Span::styled(format!("  {}", diff_line.content), Style::default().fg(Color::DarkGray)),
                            ]));
                        }
                    }
                }
            }
            lines.push(Line::raw(""));
        }
    } else if let Some(raw) = &app.raw_diff {
        for raw_line in raw.lines() {
            if raw_line.starts_with('+') && !raw_line.starts_with("+++") {
                lines.push(Line::from(vec![
                    Span::styled(raw_line, Style::default().fg(Color::LightGreen)),
                ]));
            } else if raw_line.starts_with('-') && !raw_line.starts_with("---") {
                lines.push(Line::from(vec![
                    Span::styled(raw_line, Style::default().fg(Color::LightRed)),
                ]));
            } else if raw_line.starts_with("@@") {
                lines.push(Line::from(vec![
                    Span::styled(raw_line, Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
                ]));
            } else if raw_line.starts_with("---") || raw_line.starts_with("+++") {
                lines.push(Line::from(vec![
                    Span::styled(raw_line, Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
                ]));
            } else {
                lines.push(Line::from(vec![
                    Span::styled(raw_line, Style::default().fg(Color::DarkGray)),
                ]));
            }
        }
    } else {
        lines.push(Line::from(vec![
            Span::styled("No diff available. Propose changes or execute a task to view unified diff.", Style::default().fg(Color::DarkGray)),
        ]));
    }

    let paragraph = Paragraph::new(lines)
        .block(block)
        .wrap(Wrap { trim: false })
        .scroll((app.diff_scroll as u16, 0));

    frame.render_widget(paragraph, area);
}

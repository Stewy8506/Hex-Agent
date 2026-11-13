//! Dialogue viewport widget rendering messages, role badges, and collapsible tool calls.

use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::Frame;

use hex_protocol::models::Role;

use crate::app::{App, DialogueItem, Focus};

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let is_focused = app.focus == Focus::Dialogue;
    let border_color = if is_focused {
        Color::Cyan
    } else {
        Color::DarkGray
    };

    let title = if is_focused {
        " Dialogue History [FOCUSED - ↑/↓ to Scroll] "
    } else {
        " Dialogue History "
    };

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border_color))
        .title(Span::styled(
            title,
            Style::default()
                .fg(if is_focused { Color::Cyan } else { Color::White })
                .add_modifier(if is_focused { Modifier::BOLD } else { Modifier::empty() }),
        ));

    let mut lines: Vec<Line> = Vec::new();

    for item in &app.dialogue_items {
        match item {
            DialogueItem::Message(msg) => {
                let (badge_text, badge_style) = match msg.role {
                    Role::User => ("[USER]", Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
                    Role::Assistant => ("[ASSISTANT]", Style::default().fg(Color::Green).add_modifier(Modifier::BOLD)),
                    Role::System => ("[SYSTEM]", Style::default().fg(Color::Yellow)),
                    Role::Tool => ("[TOOL]", Style::default().fg(Color::Magenta)),
                };

                let header_line = Line::from(vec![
                    Span::styled(badge_text, badge_style),
                    Span::raw("  "),
                    Span::styled(&msg.timestamp, Style::default().fg(Color::DarkGray)),
                ]);
                lines.push(header_line);

                // Message body lines
                let mut in_code_block = false;
                for raw_line in msg.content.lines() {
                    if raw_line.trim_start().starts_with("```") {
                        in_code_block = !in_code_block;
                        lines.push(Line::from(vec![
                            Span::styled(format!("  {}", raw_line), Style::default().fg(Color::DarkGray)),
                        ]));
                        continue;
                    }

                    if in_code_block {
                        lines.push(Line::from(vec![
                            Span::styled(format!("    {}", raw_line), Style::default().fg(Color::LightCyan)),
                        ]));
                    } else if raw_line.trim_start().starts_with('#') {
                        lines.push(Line::from(vec![
                            Span::styled(format!("  {}", raw_line), Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
                        ]));
                    } else if raw_line.trim_start().starts_with("- ") || raw_line.trim_start().starts_with("* ") {
                        lines.push(Line::from(vec![
                            Span::styled("  • ", Style::default().fg(Color::Cyan)),
                            Span::raw(&raw_line.trim_start()[2..]),
                        ]));
                    } else {
                        lines.push(Line::from(vec![
                            Span::raw(format!("  {}", raw_line)),
                        ]));
                    }
                }
                lines.push(Line::raw(""));
            }

            DialogueItem::ToolCall(tc) => {
                let arrow = if tc.collapsed { "▶" } else { "▼" };
                let status_style = match tc.exit_status.as_deref() {
                    Some("OK") => Style::default().fg(Color::Green).add_modifier(Modifier::BOLD),
                    Some("ERROR") => Style::default().fg(Color::Red).add_modifier(Modifier::BOLD),
                    _ => Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD),
                };
                let status_text = tc.exit_status.as_deref().unwrap_or("PENDING");
                let duration_text = tc
                    .duration_ms
                    .map(|d| format!(" ({}ms)", d))
                    .unwrap_or_default();

                let header_line = Line::from(vec![
                    Span::styled(format!("  {} [TOOL CALL] ", arrow), Style::default().fg(Color::Magenta).add_modifier(Modifier::BOLD)),
                    Span::styled(&tc.tool, Style::default().fg(Color::White).add_modifier(Modifier::BOLD)),
                    Span::styled(duration_text, Style::default().fg(Color::DarkGray)),
                    Span::raw("  "),
                    Span::styled(format!("[{}]", status_text), status_style),
                    Span::raw("  "),
                    Span::styled(
                        if tc.collapsed { "[collapsed]" } else { "" },
                        Style::default().fg(Color::DarkGray),
                    ),
                ]);
                lines.push(header_line);

                if !tc.collapsed {
                    // Show arguments
                    lines.push(Line::from(vec![
                        Span::styled("     Args: ", Style::default().fg(Color::DarkGray)),
                        Span::styled(&tc.args, Style::default().fg(Color::Gray)),
                    ]));

                    // Show result if available
                    if let Some(res) = &tc.result {
                        for res_line in res.lines().take(8) {
                            lines.push(Line::from(vec![
                                Span::styled("     │ ", Style::default().fg(Color::DarkGray)),
                                Span::styled(res_line, Style::default().fg(Color::LightGreen)),
                            ]));
                        }
                        if res.lines().count() > 8 {
                            lines.push(Line::from(vec![
                                Span::styled(format!("     │ ... ({} lines omitted)", res.lines().count() - 8), Style::default().fg(Color::DarkGray)),
                            ]));
                        }
                    }
                }
                lines.push(Line::raw(""));
            }
        }
    }

    let paragraph = Paragraph::new(lines)
        .block(block)
        .wrap(Wrap { trim: false })
        .scroll((app.dialogue_scroll as u16, 0));

    frame.render_widget(paragraph, area);
}

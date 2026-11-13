//! Dialogue viewport widget rendering messages, role badges, and collapsible tool calls.

use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::Frame;

use hex_protocol::models::Role;

use crate::app::{App, DialogueItem, Focus};
use crate::ui::design::{skin, BANNER, TIPS};

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let is_focused = app.focus == Focus::Dialogue;

    let block = Block::default().borders(Borders::NONE);

    let mut lines: Vec<Line> = Vec::new();

    // Top status guide line when focused or scrolling
    if is_focused {
        lines.push(Line::from(vec![
            Span::styled(
                "─ Transcript ",
                Style::default()
                    .fg(Color::Cyan)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                "[FOCUSED · ↑/↓ to Scroll · 'c' to toggle tools] ",
                Style::default().fg(Color::DarkGray),
            ),
            Span::styled(
                "─".repeat(area.width.saturating_sub(58) as usize),
                Style::default().fg(Color::DarkGray),
            ),
        ]));
        lines.push(Line::raw(""));
    }

    // The Python implementation's splash was passed over ACP as rendered ANSI
    // text. Render the same identity natively instead, so colour, clipping,
    // and responsive composition are owned by Ratatui.
    lines.extend(render_hex_intro(app, area.width));

    // 2. Render dialogue conversation items
    for (idx, item) in app.dialogue_items.iter().enumerate() {
        // The intro above replaces the seed welcome messages.
        if idx < 2 {
            if let DialogueItem::Message(m) = item {
                if m.role == Role::System
                    && (m.content.contains("Welcome to HEX")
                        || m.content.contains("Type your goal"))
                {
                    continue;
                }
            }
        }

        match item {
            DialogueItem::Message(msg) => {
                let (badge_text, badge_style) = match msg.role {
                    Role::User => (
                        "❯ User  ",
                        Style::default()
                            .fg(Color::Yellow)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Role::Assistant => (
                        "◈ Assistant  ",
                        Style::default()
                            .fg(Color::Green)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Role::System => (
                        "◈ System  ",
                        Style::default()
                            .fg(Color::DarkGray)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Role::Tool => (
                        "▶ Tool  ",
                        Style::default()
                            .fg(Color::Magenta)
                            .add_modifier(Modifier::BOLD),
                    ),
                };

                let header_line = Line::from(vec![
                    Span::styled(badge_text, badge_style),
                    Span::styled(&msg.timestamp, Style::default().fg(Color::DarkGray)),
                ]);
                lines.push(header_line);

                // Message body lines
                let mut in_code_block = false;
                for raw_line in msg.content.lines() {
                    if raw_line.trim_start().starts_with("```") {
                        in_code_block = !in_code_block;
                        lines.push(Line::from(vec![
                            Span::styled("  │ ", Style::default().fg(Color::DarkGray)),
                            Span::styled(
                                raw_line.trim(),
                                Style::default()
                                    .fg(Color::DarkGray)
                                    .add_modifier(Modifier::ITALIC),
                            ),
                        ]));
                        continue;
                    }

                    if in_code_block {
                        lines.push(Line::from(vec![
                            Span::styled("  │ ", Style::default().fg(Color::Cyan)),
                            Span::styled(raw_line, Style::default().fg(Color::LightCyan)),
                        ]));
                    } else if raw_line.trim_start().starts_with('#') {
                        lines.push(Line::from(vec![Span::styled(
                            format!("  {}", raw_line),
                            Style::default()
                                .fg(Color::Yellow)
                                .add_modifier(Modifier::BOLD),
                        )]));
                    } else if raw_line.trim_start().starts_with("- ")
                        || raw_line.trim_start().starts_with("* ")
                    {
                        lines.push(Line::from(vec![
                            Span::styled("  • ", Style::default().fg(Color::Cyan)),
                            Span::raw(&raw_line.trim_start()[2..]),
                        ]));
                    } else {
                        lines.push(Line::from(vec![Span::raw(format!("  {}", raw_line))]));
                    }
                }
                lines.push(Line::raw(""));
            }

            DialogueItem::ToolCall(tc) => {
                let arrow = if tc.collapsed { "▶" } else { "▼" };
                let (status_text, status_style) = match tc.exit_status.as_deref() {
                    Some("OK") => (
                        "OK",
                        Style::default()
                            .fg(Color::Green)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Some("ERROR") => (
                        "ERROR",
                        Style::default().fg(Color::Red).add_modifier(Modifier::BOLD),
                    ),
                    _ => (
                        "RUNNING",
                        Style::default()
                            .fg(Color::Yellow)
                            .add_modifier(Modifier::BOLD),
                    ),
                };
                let duration_text = tc
                    .duration_ms
                    .map(|d| format!(" ({}ms)", d))
                    .unwrap_or_default();

                let header_line = Line::from(vec![
                    Span::styled(
                        format!("  {} [TOOL CALL] ", arrow),
                        Style::default()
                            .fg(Color::Magenta)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(
                        &tc.tool,
                        Style::default()
                            .fg(Color::Cyan)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Span::raw(" "),
                    Span::styled(&tc.args, Style::default().fg(Color::DarkGray)),
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
                    // Show arguments if not fully rendered in title
                    if !tc.args.is_empty() {
                        lines.push(Line::from(vec![
                            Span::styled("      Args: ", Style::default().fg(Color::DarkGray)),
                            Span::styled(&tc.args, Style::default().fg(Color::Gray)),
                        ]));
                    }

                    // Show result if available
                    if let Some(res) = &tc.result {
                        for res_line in res.lines().take(12) {
                            lines.push(Line::from(vec![
                                Span::styled("      │ ", Style::default().fg(Color::DarkGray)),
                                Span::styled(res_line, Style::default().fg(Color::LightGreen)),
                            ]));
                        }
                        if res.lines().count() > 12 {
                            lines.push(Line::from(vec![Span::styled(
                                format!("      │ ... ({} lines omitted)", res.lines().count() - 12),
                                Style::default().fg(Color::DarkGray),
                            )]));
                        }
                    }
                }
                lines.push(Line::raw(""));
            }
        }
    }

    let max_scroll = lines.len().saturating_sub(area.height as usize);
    let scroll = if app.follow_dialogue {
        max_scroll
    } else {
        app.dialogue_scroll.min(max_scroll)
    };
    let paragraph = Paragraph::new(lines)
        .block(block)
        .wrap(Wrap { trim: false })
        .scroll((scroll as u16, 0));

    frame.render_widget(paragraph, area);
}

fn render_hex_intro(app: &App, width: u16) -> Vec<Line<'static>> {
    let s = skin(&app.skin_name);
    let mut out = vec![
        Line::from(vec![
            Span::styled(
                "did you know",
                Style::default().fg(s.category).add_modifier(Modifier::BOLD),
            ),
            Span::styled(" · ", Style::default().fg(s.muted)),
            Span::styled(
                TIPS[(app.animation_frame as usize / 120) % TIPS.len()].to_string(),
                Style::default().fg(s.muted),
            ),
        ]),
        Line::raw(""),
    ];
    if width >= 94 {
        for (i, row) in BANNER.iter().enumerate() {
            out.push(Line::from(Span::styled(
                (*row).to_string(),
                Style::default()
                    .fg(s.gradient[i])
                    .add_modifier(Modifier::BOLD),
            )));
        }
        out.push(Line::from(Span::styled(
            "                                      anv.dev",
            Style::default().fg(s.category).add_modifier(Modifier::BOLD),
        )));
    } else {
        out.push(Line::from(Span::styled(
            "                 ◈  H E X   A G E N T  ◈",
            Style::default().fg(s.heading).add_modifier(Modifier::BOLD),
        )));
        out.push(Line::from(Span::styled(
            "                       anv.dev",
            Style::default().fg(s.category),
        )));
    }
    out.push(Line::raw(""));
    out.push(Line::from(vec![
        Span::styled(
            "     ◈ HEX Autonomous AI Workbench",
            Style::default().fg(s.heading).add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!("  ·  {}", app.provider_name),
            Style::default().fg(s.category),
        ),
    ]));
    out.push(Line::from(vec![
        Span::styled(
            format!("     {}", app.model_name),
            Style::default().fg(s.accent).add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!("  ·  {}", app.workspace_path),
            Style::default().fg(s.muted),
        ),
    ]));
    out.push(Line::from(vec![
        Span::styled(
            "     tools",
            Style::default().fg(s.category).add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!(
                "  {} tools · {} skills · CoW:{}",
                app.tools_count, app.skills_count, app.cow_mode
            ),
            Style::default().fg(s.text),
        ),
    ]));
    out.push(Line::from(Span::styled(
        "     type a message   /help for commands",
        Style::default().fg(s.muted),
    )));
    out.push(Line::raw(""));
    out
}

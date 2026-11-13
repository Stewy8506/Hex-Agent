//! Dialogue viewport widget rendering messages, role badges, and collapsible tool calls.

use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::Frame;

use hex_protocol::models::Role;

use crate::app::{App, DialogueItem, Focus};
use crate::ui::design::{skin, Skin, BANNER, SPINNER, TIPS, VERBS};

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let skin = skin(&app.skin_name);
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
    let mut in_assistant_turn = false;
    let mut had_tool_in_turn = false;

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
                if msg.role == Role::User {
                    in_assistant_turn = false;
                    had_tool_in_turn = false;

                    lines.push(Line::raw(""));
                    let card_width = (area.width.saturating_sub(2)).max(30) as usize;
                    let prefix = "╭─ ❯ You ";
                    let suffix = format!(" {} ─╮", msg.timestamp);
                    let prefix_len = prefix.chars().count();
                    let suffix_len = suffix.chars().count();
                    let dash_count = card_width.saturating_sub(prefix_len + suffix_len).max(2);

                    lines.push(Line::from(vec![
                        Span::styled("╭─ ", Style::default().fg(skin.accent)),
                        Span::styled("❯ You ", Style::default().fg(skin.accent).add_modifier(Modifier::BOLD)),
                        Span::styled("─".repeat(dash_count), Style::default().fg(skin.accent)),
                        Span::styled(format!(" {} ", msg.timestamp), Style::default().fg(skin.muted)),
                        Span::styled("─╮", Style::default().fg(skin.accent)),
                    ]));

                    let inner_width = card_width.saturating_sub(4);
                    let wrapped = wrap_text(&msg.content, inner_width);
                    for wline in wrapped {
                        let wlen = wline.chars().count();
                        let pad = inner_width.saturating_sub(wlen);
                        lines.push(Line::from(vec![
                            Span::styled("│ ", Style::default().fg(skin.accent)),
                            Span::styled(wline, Style::default().fg(skin.heading).add_modifier(Modifier::BOLD)),
                            Span::raw(" ".repeat(pad)),
                            Span::styled(" │", Style::default().fg(skin.accent)),
                        ]));
                    }

                    let bot_dashes = card_width.saturating_sub(2);
                    lines.push(Line::from(Span::styled(
                        format!("╰{}╯", "─".repeat(bot_dashes)),
                        Style::default().fg(skin.accent),
                    )));
                    lines.push(Line::raw(""));
                    continue;
                }

                if msg.role == Role::Assistant {
                    if !in_assistant_turn {
                        lines.push(Line::from(vec![
                            Span::styled("◈ Assistant", Style::default().fg(skin.heading).add_modifier(Modifier::BOLD)),
                        ]));
                    }
                    if had_tool_in_turn {
                        lines.push(Line::from(Span::styled("  │", Style::default().fg(skin.muted))));
                    }
                    lines.push(Line::from(vec![
                        Span::styled("  ▼   ", Style::default().fg(skin.accent).add_modifier(Modifier::BOLD)),
                        Span::styled(&msg.timestamp, Style::default().fg(skin.muted)),
                    ]));
                    lines.push(Line::raw(""));

                    in_assistant_turn = false;
                    had_tool_in_turn = false;

                    // Message body lines with rich markdown formatting
                    let mut in_code_block = false;
                    for raw_line in msg.content.lines() {
                        let trimmed = raw_line.trim_start();

                        if trimmed.starts_with("```") {
                            in_code_block = !in_code_block;
                            if in_code_block {
                                let lang = trimmed.trim_start_matches('`').trim();
                                lines.push(Line::from(vec![
                                    Span::styled("  ┌── ", Style::default().fg(skin.muted)),
                                    Span::styled(
                                        if lang.is_empty() { "[code] ".to_string() } else { format!("[{}] ", lang) },
                                        Style::default().fg(skin.accent).add_modifier(Modifier::BOLD),
                                    ),
                                    Span::styled("─".repeat(40), Style::default().fg(skin.muted)),
                                ]));
                            } else {
                                lines.push(Line::from(vec![
                                    Span::styled(format!("  └──{}", "─".repeat(48)), Style::default().fg(skin.muted)),
                                ]));
                            }
                            continue;
                        }

                        if in_code_block {
                            lines.push(Line::from(vec![
                                Span::styled("  │ ", Style::default().fg(skin.muted)),
                                Span::styled(raw_line.to_string(), Style::default().fg(skin.accent)),
                            ]));
                            continue;
                        }

                        // Headings
                        if trimmed.starts_with("### ") {
                            let mut spans = vec![
                                Span::styled("  ● ", Style::default().fg(skin.heading).add_modifier(Modifier::BOLD)),
                            ];
                            spans.extend(parse_inline_spans(trimmed[4..].trim(), &skin));
                            lines.push(Line::from(spans));
                            continue;
                        } else if trimmed.starts_with("## ") {
                            let mut spans = vec![
                                Span::styled("  ◆ ", Style::default().fg(skin.accent).add_modifier(Modifier::BOLD)),
                            ];
                            spans.extend(parse_inline_spans(trimmed[3..].trim(), &skin));
                            lines.push(Line::from(spans));
                            continue;
                        } else if trimmed.starts_with("# ") {
                            let mut spans = vec![
                                Span::styled("  ■ ", Style::default().fg(skin.heading).add_modifier(Modifier::BOLD)),
                            ];
                            spans.extend(parse_inline_spans(trimmed[2..].trim(), &skin));
                            lines.push(Line::from(spans));
                            continue;
                        }

                        // Numbered list items (e.g. "1. ")
                        let is_num_list = trimmed.chars().next().map(|c| c.is_ascii_digit()).unwrap_or(false)
                            && trimmed.find(". ").is_some();

                        if is_num_list {
                            if let Some(dot_idx) = trimmed.find(". ") {
                                let num_str = &trimmed[..dot_idx];
                                if num_str.chars().all(|c| c.is_ascii_digit()) {
                                    let rest = trimmed[dot_idx + 2..].trim();
                                    let mut spans = vec![
                                        Span::styled(
                                            format!("  {}. ", num_str),
                                            Style::default().fg(skin.heading).add_modifier(Modifier::BOLD),
                                        ),
                                    ];
                                    spans.extend(parse_inline_spans(rest, &skin));
                                    lines.push(Line::from(spans));
                                    continue;
                                }
                            }
                        }

                        // Bullet list items
                        if trimmed.starts_with("- ") || trimmed.starts_with("* ") {
                            let rest = trimmed[2..].trim();
                            let mut spans = vec![
                                Span::styled("  • ", Style::default().fg(skin.accent)),
                            ];
                            spans.extend(parse_inline_spans(rest, &skin));
                            lines.push(Line::from(spans));
                            continue;
                        }

                        // Blockquotes
                        if trimmed.starts_with("> ") {
                            let rest = trimmed[2..].trim();
                            let mut spans = vec![
                                Span::styled("  │ ", Style::default().fg(skin.accent)),
                            ];
                            spans.extend(parse_inline_spans(rest, &skin));
                            lines.push(Line::from(spans));
                            continue;
                        }

                        // Normal prose
                        let mut spans = vec![Span::raw("  ")];
                        spans.extend(parse_inline_spans(raw_line, &skin));
                        lines.push(Line::from(spans));
                    }
                    lines.push(Line::raw(""));
                    continue;
                }

                // System / Tool message
                in_assistant_turn = false;
                had_tool_in_turn = false;

                let (badge_text, badge_style) = match msg.role {
                    Role::System => (
                        "◈ System  ",
                        Style::default()
                            .fg(skin.muted)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Role::Tool => (
                        "▶ Tool  ",
                        Style::default()
                            .fg(skin.category)
                            .add_modifier(Modifier::BOLD),
                    ),
                    _ => (
                        "◈ ",
                        Style::default().fg(skin.muted),
                    ),
                };

                let header_line = Line::from(vec![
                    Span::styled(badge_text, badge_style),
                    Span::styled(&msg.timestamp, Style::default().fg(skin.muted)),
                ]);
                lines.push(header_line);
                for raw_line in msg.content.lines() {
                    let mut spans = vec![Span::raw("  ")];
                    spans.extend(parse_inline_spans(raw_line, &skin));
                    lines.push(Line::from(spans));
                }
                lines.push(Line::raw(""));
            }

            DialogueItem::ToolCall(tc) => {
                if !in_assistant_turn {
                    lines.push(Line::from(vec![
                        Span::styled("◈ Assistant", Style::default().fg(skin.heading).add_modifier(Modifier::BOLD)),
                    ]));
                    in_assistant_turn = true;
                }
                had_tool_in_turn = true;

                let (status_icon, status_style) = match tc.exit_status.as_deref() {
                    Some("ERROR") | Some("FAILED") => (
                        "✖",
                        Style::default().fg(Color::Red).add_modifier(Modifier::BOLD),
                    ),
                    _ => (
                        "⚙",
                        Style::default().fg(skin.accent).add_modifier(Modifier::BOLD),
                    ),
                };
                let formatted_args = format_tool_args(&tc.args);

                let mut spans = vec![
                    Span::styled("  ├─ ", Style::default().fg(skin.muted)),
                    Span::styled(format!("{} ", status_icon), status_style),
                    Span::styled(
                        format!("{} ", tc.tool),
                        Style::default()
                            .fg(skin.category)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(formatted_args, Style::default().fg(skin.muted)),
                ];
                if tc.collapsed {
                    spans.push(Span::styled(" [collapsed]", Style::default().fg(skin.muted)));
                }
                lines.push(Line::from(spans));

                if !tc.collapsed {
                    if let Some(res) = &tc.result {
                        for res_line in res.lines().take(8) {
                            lines.push(Line::from(vec![
                                Span::styled("  │  └─ ", Style::default().fg(skin.muted)),
                                Span::styled(res_line, Style::default().fg(skin.text)),
                            ]));
                        }
                    }
                }
            }
        }
    }

    // In-dialogue Thinking/Reasoning animation when busy
    if app.is_busy {
        let last_is_streaming = if let Some(DialogueItem::Message(m)) = app.dialogue_items.last() {
            m.role == Role::Assistant && !m.content.is_empty()
        } else {
            false
        };

        if !last_is_streaming {
            if !in_assistant_turn {
                lines.push(Line::from(vec![
                    Span::styled("◈ Assistant", Style::default().fg(skin.heading).add_modifier(Modifier::BOLD)),
                ]));
            }
            let spin_char = SPINNER[(app.animation_frame as usize / 2) % SPINNER.len()];
            let verb_idx = (app.animation_frame as usize / 40) % VERBS.len();
            let verb = VERBS[verb_idx];
            let status_text = if !app.status_message.is_empty() && app.status_message != "Ready" {
                &app.status_message
            } else {
                verb
            };

            lines.push(Line::from(vec![
                Span::styled("  ├─ ", Style::default().fg(skin.muted)),
                Span::styled(format!("{} ", spin_char), Style::default().fg(skin.accent).add_modifier(Modifier::BOLD)),
                Span::styled(format!("{}...", status_text), Style::default().fg(skin.accent).add_modifier(Modifier::BOLD)),
            ]));
            lines.push(Line::raw(""));
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

fn parse_inline_spans(input: &str, skin: &Skin) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    let chars: Vec<char> = input.chars().collect();
    let len = chars.len();
    let mut i = 0;
    let mut current = String::new();

    while i < len {
        // Check for **bold**
        if i + 1 < len && chars[i] == '*' && chars[i + 1] == '*' {
            if let Some(end) = (i + 2..len).find(|&j| j + 1 < len && chars[j] == '*' && chars[j + 1] == '*') {
                if !current.is_empty() {
                    spans.push(Span::styled(std::mem::take(&mut current), Style::default().fg(skin.text)));
                }
                let bold_text: String = chars[i + 2..end].iter().collect();
                spans.push(Span::styled(
                    bold_text,
                    Style::default().fg(skin.text).add_modifier(Modifier::BOLD),
                ));
                i = end + 2;
                continue;
            }
        }

        // Check for `code`
        if chars[i] == '`' {
            if let Some(end) = (i + 1..len).find(|&j| chars[j] == '`') {
                if !current.is_empty() {
                    spans.push(Span::styled(std::mem::take(&mut current), Style::default().fg(skin.text)));
                }
                let code_text: String = chars[i + 1..end].iter().collect();
                spans.push(Span::styled(
                    code_text,
                    Style::default().fg(skin.accent),
                ));
                i = end + 1;
                continue;
            }
        }

        // Check for *italic*
        if chars[i] == '*' {
            if let Some(end) = (i + 1..len).find(|&j| chars[j] == '*') {
                if !current.is_empty() {
                    spans.push(Span::styled(std::mem::take(&mut current), Style::default().fg(skin.text)));
                }
                let italic_text: String = chars[i + 1..end].iter().collect();
                spans.push(Span::styled(
                    italic_text,
                    Style::default().fg(skin.text).add_modifier(Modifier::ITALIC),
                ));
                i = end + 1;
                continue;
            }
        }

        current.push(chars[i]);
        i += 1;
    }

    if !current.is_empty() {
        spans.push(Span::styled(current, Style::default().fg(skin.text)));
    }

    spans
}

fn wrap_text(text: &str, max_width: usize) -> Vec<String> {
    if max_width == 0 {
        return vec![text.to_string()];
    }
    let mut result = Vec::new();
    for raw_line in text.lines() {
        if raw_line.is_empty() {
            result.push(String::new());
            continue;
        }
        let mut cur = String::new();
        for word in raw_line.split_whitespace() {
            let word_len = word.chars().count();
            if cur.is_empty() {
                if word_len > max_width {
                    let chars: Vec<char> = word.chars().collect();
                    for chunk in chars.chunks(max_width) {
                        result.push(chunk.iter().collect());
                    }
                } else {
                    cur.push_str(word);
                }
            } else {
                let cur_len = cur.chars().count();
                if cur_len + 1 + word_len <= max_width {
                    cur.push(' ');
                    cur.push_str(word);
                } else {
                    result.push(cur);
                    cur = if word_len > max_width {
                        let chars: Vec<char> = word.chars().collect();
                        for chunk in chars.chunks(max_width) {
                            result.push(chunk.iter().collect());
                        }
                        String::new()
                    } else {
                        word.to_string()
                    };
                }
            }
        }
        if !cur.is_empty() {
            result.push(cur);
        }
    }
    if result.is_empty() {
        result.push(String::new());
    }
    result
}

fn format_tool_args(args: &str) -> String {
    let trimmed = args.trim();
    if trimmed.is_empty() || trimmed == "{}" {
        return "()".to_string();
    }
    if trimmed.starts_with('{') && trimmed.ends_with('}') {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(trimmed) {
            if let Some(map) = v.as_object() {
                if map.is_empty() {
                    return "()".to_string();
                }
                if map.len() == 1 {
                    let val = map.values().next().unwrap();
                    let s = match val {
                        serde_json::Value::String(s) => s.clone(),
                        _ => val.to_string(),
                    };
                    return format!("({})", s);
                }
                let parts: Vec<String> = map
                    .iter()
                    .take(3)
                    .map(|(k, v)| {
                        let v_str = match v {
                            serde_json::Value::String(s) => s.clone(),
                            _ => v.to_string(),
                        };
                        format!("{}={}", k, v_str)
                    })
                    .collect();
                return format!("({})", parts.join(", "));
            }
        }
    }
    if trimmed.starts_with('(') && trimmed.ends_with(')') {
        trimmed.to_string()
    } else {
        format!("({})", trimmed)
    }
}

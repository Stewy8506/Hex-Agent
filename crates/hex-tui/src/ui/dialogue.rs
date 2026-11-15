//! Dialogue viewport widget rendering messages, role badges, and collapsible tool calls.

use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::Frame;

use hex_protocol::models::Role;

use crate::app::{App, DialogueItem, Focus};
use crate::ui::design::{skin, Skin, BANNER, EMBLEM_ART, SPINNER, TIPS, VERBS};

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

                if msg.content.starts_with("Available Skills") || msg.content.starts_with("Available Tools") {
                    for (line_idx, raw_line) in msg.content.lines().enumerate() {
                        let trimmed = raw_line.trim();
                        if trimmed.is_empty() {
                            lines.push(Line::raw(""));
                            continue;
                        }
                        if line_idx == 0 {
                            lines.push(Line::from(vec![
                                Span::raw("  "),
                                Span::styled(
                                    trimmed.to_string(),
                                    Style::default().fg(skin.heading).add_modifier(Modifier::BOLD),
                                ),
                            ]));
                            continue;
                        }
                        if let Some(pos) = raw_line.find(": ") {
                            let cat = &raw_line[..pos];
                            let items = &raw_line[pos + 2..];
                            let mut spans = vec![
                                Span::raw("  "),
                                Span::styled(
                                    format!("{}: ", cat.trim()),
                                    Style::default().fg(skin.category).add_modifier(Modifier::BOLD),
                                ),
                            ];
                            if let Some(more_pos) = items.find(", +") {
                                spans.push(Span::styled(
                                    items[..more_pos].to_string(),
                                    Style::default().fg(skin.text),
                                ));
                                spans.push(Span::styled(
                                    items[more_pos..].to_string(),
                                    Style::default().fg(skin.muted),
                                ));
                            } else {
                                spans.push(Span::styled(
                                    items.to_string(),
                                    Style::default().fg(skin.text),
                                ));
                            }
                            lines.push(Line::from(spans));
                        } else {
                            lines.push(Line::from(vec![
                                Span::raw("  "),
                                Span::styled(raw_line.to_string(), Style::default().fg(skin.text)),
                            ]));
                        }
                    }
                    lines.push(Line::raw(""));
                    continue;
                }

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

    let width = area.width.max(1) as usize;
    let mut total_visual_lines = 0;
    for line in &lines {
        let line_len: usize = line.spans.iter().map(|s| s.content.chars().count()).sum();
        let rows = if line_len == 0 {
            1
        } else {
            (line_len + width - 1) / width
        };
        total_visual_lines += rows;
    }
    let max_scroll = total_visual_lines.saturating_sub(area.height as usize);
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

fn style_splash_text(splash: &str, s: &Skin, _area_width: u16) -> Vec<Line<'static>> {
    let mut out: Vec<Line<'static>> = Vec::new();
    let mut banner_row = 0usize;
    let mut emblem_row = 0usize;

    for raw_line in splash.lines() {
        let trimmed = raw_line.trim();
        if trimmed.is_empty() {
            out.push(Line::raw(""));
            continue;
        }

        if raw_line.contains("did you know") {
            let lead_spaces = raw_line.len() - raw_line.trim_start().len();
            let prefix = " ".repeat(lead_spaces);
            let rest = raw_line.trim_start();
            if let Some(pos) = rest.find(" · ") {
                let tag = &rest[..pos];
                let tip = &rest[pos + 3..];
                out.push(Line::from(vec![
                    Span::raw(prefix),
                    Span::styled(tag.to_string(), Style::default().fg(s.category).add_modifier(Modifier::BOLD)),
                    Span::styled(" · ", Style::default().fg(s.muted)),
                    Span::styled(tip.to_string(), Style::default().fg(s.muted)),
                ]));
            } else {
                out.push(Line::from(Span::styled(raw_line.to_string(), Style::default().fg(s.muted))));
            }
            continue;
        }

        if raw_line.contains('█') || raw_line.contains('╚') {
            let color = s.gradient[banner_row.min(5)];
            banner_row += 1;
            out.push(Line::from(Span::styled(
                raw_line.to_string(),
                Style::default().fg(color).add_modifier(Modifier::BOLD),
            )));
            continue;
        }

        if trimmed == "anv.dev" {
            let lead_spaces = raw_line.len() - raw_line.trim_start().len();
            let prefix = " ".repeat(lead_spaces);
            out.push(Line::from(vec![
                Span::raw(prefix),
                Span::styled("anv.dev", Style::default().fg(s.category).add_modifier(Modifier::BOLD)),
            ]));
            continue;
        }

        // Two-column section: Left has emblem art, right has workbench details
        let chars: Vec<char> = raw_line.chars().collect();
        let split_pos = if chars.len() > 42 { 42 } else { chars.len() };
        let left_part: String = chars[..split_pos].iter().collect();
        let right_part: String = if chars.len() > split_pos {
            chars[split_pos..].iter().collect()
        } else {
            String::new()
        };

        let mut spans: Vec<Span<'static>> = Vec::new();

        // Style left emblem part
        if !left_part.trim().is_empty() {
            let emb_color = s.gradient[(emblem_row / 3).min(s.gradient.len() - 1)];
            emblem_row += 1;
            spans.push(Span::styled(
                left_part,
                Style::default().fg(emb_color).add_modifier(Modifier::BOLD),
            ));
        } else {
            spans.push(Span::raw(left_part));
        }

        // Style right workbench details part
        let r_trimmed = right_part.trim();
        if !r_trimmed.is_empty() {
            if r_trimmed.contains("HEX Autonomous AI Workbench") {
                if let Some(pos) = right_part.find("  (upstream") {
                    spans.push(Span::styled(
                        right_part[..pos].to_string(),
                        Style::default().fg(s.heading).add_modifier(Modifier::BOLD),
                    ));
                    spans.push(Span::styled(
                        right_part[pos..].to_string(),
                        Style::default().fg(s.muted),
                    ));
                } else {
                    spans.push(Span::styled(
                        right_part,
                        Style::default().fg(s.heading).add_modifier(Modifier::BOLD),
                    ));
                }
            } else if r_trimmed.contains(" · ") && !r_trimmed.contains("tools") && !r_trimmed.contains("skills") {
                if let Some(pos) = right_part.find(" · ") {
                    spans.push(Span::styled(
                        right_part[..pos].to_string(),
                        Style::default().fg(s.accent).add_modifier(Modifier::BOLD),
                    ));
                    spans.push(Span::styled(" · ", Style::default().fg(s.muted)));
                    spans.push(Span::styled(
                        right_part[pos + 3..].to_string(),
                        Style::default().fg(s.category),
                    ));
                } else {
                    spans.push(Span::styled(right_part, Style::default().fg(s.text)));
                }
            } else if r_trimmed.starts_with("Available Tools") || r_trimmed.starts_with("Available Skills") || r_trimmed.starts_with("tools") {
                spans.push(Span::styled(
                    right_part,
                    Style::default().fg(s.heading).add_modifier(Modifier::BOLD),
                ));
            } else if r_trimmed.contains("tools · ") && r_trimmed.contains("skills") {
                spans.push(Span::styled(
                    right_part,
                    Style::default().fg(s.category).add_modifier(Modifier::BOLD),
                ));
            } else if r_trimmed.starts_with("type a message") {
                spans.push(Span::styled(right_part, Style::default().fg(s.muted)));
            } else if right_part.contains(':') && !r_trimmed.starts_with("Session:") {
                // Category line e.g. "  filesystem: list_files, patch, read_file, write_file"
                if let Some(pos) = right_part.find(':') {
                    spans.push(Span::styled(
                        right_part[..=pos].to_string(),
                        Style::default().fg(s.category).add_modifier(Modifier::BOLD),
                    ));
                    let rest = &right_part[pos + 1..];
                    if let Some(more_pos) = rest.find(", +") {
                        spans.push(Span::styled(
                            rest[..more_pos].to_string(),
                            Style::default().fg(s.text),
                        ));
                        spans.push(Span::styled(
                            rest[more_pos..].to_string(),
                            Style::default().fg(s.muted),
                        ));
                    } else {
                        spans.push(Span::styled(rest.to_string(), Style::default().fg(s.text)));
                    }
                } else {
                    spans.push(Span::styled(right_part, Style::default().fg(s.text)));
                }
            } else {
                // Session, workspace path, "+N more", etc.
                spans.push(Span::styled(right_part, Style::default().fg(s.muted)));
            }
        }

        out.push(Line::from(spans));
    }

    out
}

fn render_hex_intro_fallback(app: &App, width: u16) -> Vec<Line<'static>> {
    let s = skin(&app.skin_name);
    let mut out: Vec<Line<'static>> = Vec::new();

    if width < 80 {
        out.push(Line::raw(""));
        out.push(Line::from(Span::styled(
            "◈ HEX Autonomous AI Workbench",
            Style::default().fg(s.heading).add_modifier(Modifier::BOLD),
        )));
        out.push(Line::from(vec![
            Span::styled(format!("{} ", app.model_name), Style::default().fg(s.accent).add_modifier(Modifier::BOLD)),
            Span::styled("· ", Style::default().fg(s.muted)),
            Span::styled(app.provider_name.clone(), Style::default().fg(s.category)),
        ]));
        out.push(Line::from(Span::styled(
            format!("{} tools · {} skills", app.tools_count, app.skills_count),
            Style::default().fg(s.category).add_modifier(Modifier::BOLD),
        )));
        out.push(Line::raw(""));
        return out;
    }

    let total_w = width as usize;
    let block_w = 34 + 4 + 60; // left_w (34) + gap (4) + right_w (60) = 98
    let margin_l = if total_w > block_w { (total_w - block_w) / 2 } else { 2 };
    let margin_str = " ".repeat(margin_l);
    let gap_str = "    "; // 4 spaces

    // 1. Tip line
    let tip = TIPS[(app.animation_frame as usize / 120) % TIPS.len()];
    out.push(Line::raw(""));
    out.push(Line::from(vec![
        Span::raw(format!("{}    ", margin_str)),
        Span::styled("did you know", Style::default().fg(s.category).add_modifier(Modifier::BOLD)),
        Span::styled(" · ", Style::default().fg(s.muted)),
        Span::styled(tip.to_string(), Style::default().fg(s.muted)),
    ]));
    out.push(Line::raw(""));

    // 2. Banner lines (centered)
    let banner_w = 77;
    let banner_margin = if total_w > banner_w { (total_w - banner_w) / 2 } else { 0 };
    let b_margin_str = " ".repeat(banner_margin);
    for (i, row) in BANNER.iter().enumerate() {
        out.push(Line::from(vec![
            Span::raw(b_margin_str.clone()),
            Span::styled(
                (*row).to_string(),
                Style::default().fg(s.gradient[i]).add_modifier(Modifier::BOLD),
            ),
        ]));
    }

    // 3. anv.dev (centered)
    let anv_str = "anv.dev";
    let anv_margin = if total_w > anv_str.len() { (total_w - anv_str.len()) / 2 } else { 0 };
    out.push(Line::from(vec![
        Span::raw(" ".repeat(anv_margin)),
        Span::styled(anv_str, Style::default().fg(s.category).add_modifier(Modifier::BOLD)),
    ]));
    out.push(Line::raw(""));

    // 4. Two-column body
    let mut right_lines: Vec<Line<'static>> = Vec::new();
    right_lines.push(Line::from(vec![
        Span::styled("HEX Autonomous AI Workbench  v0.20.1", Style::default().fg(s.heading).add_modifier(Modifier::BOLD)),
        Span::styled(format!("  (upstream {})", app.git_sha), Style::default().fg(s.muted)),
    ]));
    right_lines.push(Line::from(vec![
        Span::styled(format!("{} ", app.model_name), Style::default().fg(s.accent).add_modifier(Modifier::BOLD)),
        Span::styled("· ", Style::default().fg(s.muted)),
        Span::styled(app.provider_name.clone(), Style::default().fg(s.category)),
    ]));
    right_lines.push(Line::from(Span::styled(app.workspace_path.clone(), Style::default().fg(s.muted))));
    right_lines.push(Line::from(Span::styled(format!("Session: {}", app.session_id), Style::default().fg(s.muted))));
    right_lines.push(Line::raw(""));
    right_lines.push(Line::from(Span::styled("Available Tools", Style::default().fg(s.heading).add_modifier(Modifier::BOLD))));
    right_lines.push(Line::from(vec![
        Span::styled("  filesystem: ", Style::default().fg(s.category).add_modifier(Modifier::BOLD)),
        Span::styled("list_files, patch, read_file, write_file", Style::default().fg(s.text)),
    ]));
    right_lines.push(Line::from(vec![
        Span::styled("  code-intelligence: ", Style::default().fg(s.category).add_modifier(Modifier::BOLD)),
        Span::styled("lsp_definition, lsp_diagnostics, lsp_hover, lsp_references", Style::default().fg(s.text)),
        Span::styled(", +1 more", Style::default().fg(s.muted)),
    ]));
    right_lines.push(Line::from(vec![
        Span::styled("  execution: ", Style::default().fg(s.category).add_modifier(Modifier::BOLD)),
        Span::styled("code_kernel, process, terminal, run_tests", Style::default().fg(s.text)),
    ]));
    right_lines.push(Line::from(vec![
        Span::styled("  search: ", Style::default().fg(s.category).add_modifier(Modifier::BOLD)),
        Span::styled("search_files, web_extract, web_search", Style::default().fg(s.text)),
    ]));
    right_lines.push(Line::from(vec![
        Span::styled("  cognition: ", Style::default().fg(s.category).add_modifier(Modifier::BOLD)),
        Span::styled("clarify, memory, session_search, todo", Style::default().fg(s.text)),
    ]));
    right_lines.push(Line::from(vec![
        Span::styled("  governance: ", Style::default().fg(s.category).add_modifier(Modifier::BOLD)),
        Span::styled("modify_system_policy, git_push_remote", Style::default().fg(s.text)),
    ]));
    right_lines.push(Line::from(Span::styled("  +1 more categories", Style::default().fg(s.muted))));
    right_lines.push(Line::raw(""));
    right_lines.push(Line::from(Span::styled(
        format!("{} tools · {} skills", app.tools_count, app.skills_count),
        Style::default().fg(s.category).add_modifier(Modifier::BOLD),
    )));
    right_lines.push(Line::raw(""));
    right_lines.push(Line::from(Span::styled("type a message   /help for commands", Style::default().fg(s.muted))));

    let max_rows = EMBLEM_ART.len().max(right_lines.len());
    for r in 0..max_rows {
        let mut spans: Vec<Span<'static>> = Vec::new();
        spans.push(Span::raw(margin_str.clone()));

        if r < EMBLEM_ART.len() {
            let emb_color = s.gradient[(r / 3).min(s.gradient.len() - 1)];
            spans.push(Span::styled(
                EMBLEM_ART[r].to_string(),
                Style::default().fg(emb_color).add_modifier(Modifier::BOLD),
            ));
        } else {
            spans.push(Span::raw(" ".repeat(34)));
        }

        spans.push(Span::raw(gap_str));

        if r < right_lines.len() {
            spans.extend(right_lines[r].spans.clone());
        }

        out.push(Line::from(spans));
    }

    out
}

fn render_hex_intro(app: &App, width: u16) -> Vec<Line<'static>> {
    let s = skin(&app.skin_name);
    if let Some(ref splash) = app.splash_text {
        if !splash.trim().is_empty() {
            return style_splash_text(splash, &s, width);
        }
    }
    render_hex_intro_fallback(app, width)
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

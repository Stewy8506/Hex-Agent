//! Screen 1: Active Cockpit Session matching the HEX Agent HTML mockup.

use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Paragraph, Widget, Wrap};

use hex_protocol::diff::DiffLineType;
use hex_protocol::models::Role;

use crate::app::{App, DialogueItem};
use crate::ui::animation::AnimationState;
use crate::ui::diff::{render_mockup_inline_diff, MockupDiffLine};
use crate::ui::right_rail;
use crate::ui::style::HexTheme;

pub fn render(app: &App, theme: &HexTheme, anim: &AnimationState, area: Rect, buf: &mut Buffer) {
    if area.height < 15 || area.width < 50 {
        let msg = Paragraph::new("HEX AGENT - Expand terminal to view Cockpit")
            .style(Style::default().fg(theme.text_bright));
        msg.render(area, buf);
        return;
    }

    // Fill entire window background (#0e0e0e)
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            buf[(x, y)].set_bg(theme.window_bg);
        }
    }

    // Outer window border (#232323)
    let window_block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme.border_mid))
        .style(Style::default().bg(theme.window_bg));
    let win_inner = window_block.inner(area);
    window_block.render(area, buf);

    // Root layout:
    // 1. Top header bar (1 row)
    // 2. Horizontal divider (1 row)
    // 3. Main split: Left canvas (75%) + Right rail (25%)
    let v_chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1), // Top bar
            Constraint::Length(1), // Divider
            Constraint::Min(10),   // Main viewport
        ])
        .split(win_inner);

    // ─────────────────────────────────────────────
    // 1. Top Header Bar
    // ─────────────────────────────────────────────
    render_top_bar(app, theme, v_chunks[0], buf);

    // Horizontal divider (#1c1c1c)
    for x in v_chunks[1].left()..v_chunks[1].right() {
        buf[(x, v_chunks[1].y)].set_symbol("─");
        buf[(x, v_chunks[1].y)].set_fg(theme.border_low);
    }

    // ─────────────────────────────────────────────
    // 2. Main Viewport: Canvas vs Right Rail
    // ─────────────────────────────────────────────
    let rail_width = ((v_chunks[2].width as f32 * 0.23).round() as u16)
        .clamp(28, 44)
        .min(v_chunks[2].width.saturating_sub(40));
    let h_chunks = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Min(40),               // Left canvas
            Constraint::Length(1),             // Vertical rail divider
            Constraint::Length(rail_width),    // Right rail
        ])
        .split(v_chunks[2]);

    // Draw vertical divider between canvas and right rail (#1c1c1c)
    let div_x = h_chunks[1].x;
    for y in h_chunks[1].top()..h_chunks[1].bottom() {
        buf[(div_x, y)].set_symbol("│");
        buf[(div_x, y)].set_fg(theme.border_low);
    }

    // Render Left Canvas
    render_left_canvas(app, theme, anim, h_chunks[0], buf);

    // Render Right Rail
    right_rail::render(app, theme, anim, h_chunks[2], buf);
}

fn render_top_bar(app: &App, theme: &HexTheme, area: Rect, buf: &mut Buffer) {
    let left_span = Span::styled("hex", Style::default().fg(theme.text_dim));
    let center_span = Span::styled(&app.workspace_path, Style::default().fg(theme.text_dim));
    let right_span = Span::styled(&app.git_branch, Style::default().fg(theme.text_extra_dim));

    buf.set_string(area.x, area.y, left_span.content, Style::default().fg(theme.text_dim));

    let center_x = area.x + (area.width.saturating_sub(center_span.content.len() as u16)) / 2;
    buf.set_string(center_x, area.y, center_span.content, Style::default().fg(theme.text_dim));

    let right_x = area.right().saturating_sub(right_span.content.len() as u16);
    buf.set_string(right_x, area.y, right_span.content, Style::default().fg(theme.text_extra_dim));
}

fn render_left_canvas(app: &App, theme: &HexTheme, anim: &AnimationState, area: Rect, buf: &mut Buffer) {
    // Vertical split for canvas:
    // 1. Scrollable Transcript & Activity Stream (Flexible)
    // 2. Message Composer Box (3 rows)
    // 3. Status Bar Footer (1 row)
    let c_chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(6),    // Transcript / diff stream
            Constraint::Length(3), // Prompt input box
            Constraint::Length(1), // Bottom telemetry / hint footer
        ])
        .split(area);

    let stream_area = c_chunks[0];
    let composer_area = c_chunks[1];
    let footer_area = c_chunks[2];

    // Render Transcript items
    let mut lines = Vec::new();

    // Mini Wordmark & Session Header
    lines.push(Line::from(vec![Span::styled("█ █ █▀▀ ▀▄▀", Style::default().fg(theme.text_bright))]));
    lines.push(Line::from(vec![Span::styled("█▀█ ██▄ ▄▀▄", Style::default().fg(theme.text_bright))]));
    lines.push(Line::from(vec![
        Span::styled("0.4.2 · session ", Style::default().fg(theme.text_dim)),
        Span::styled(&app.session_id, Style::default().fg(theme.text_dim)),
    ]));
    lines.push(Line::raw(""));

    let is_empty_session = app.dialogue_items.is_empty()
        && app.active_diff.is_none()
        && app.current_thought.is_none()
        && app.active_diagnostic.is_none();

    if is_empty_session {
        lines.push(Line::from(vec![
            Span::styled("> ", Style::default().fg(theme.text_dim)),
            Span::styled("Ready for instructions. Type your goal or /help for interactive commands.", Style::default().fg(theme.text_dim)),
        ]));
    } else {
        // Agent Thought / Commentary
        if let Some(ref thought) = app.current_thought {
            lines.push(Line::from(vec![
                Span::raw("  "),
                Span::styled(thought, Style::default().fg(theme.text_muted)),
            ]));
        }

        // Dialogue messages and tool invocations
        for item in &app.dialogue_items {
            match item {
                DialogueItem::Message(m) => {
                    if m.role == Role::User {
                        lines.push(Line::from(vec![
                            Span::styled("> ", Style::default().fg(theme.text_dim)),
                            Span::styled(&m.content, Style::default().fg(theme.text_bright)),
                        ]));
                    } else {
                        lines.push(Line::from(vec![
                            Span::raw("  "),
                            Span::styled(&m.content, Style::default().fg(theme.text_muted)),
                        ]));
                    }
                }
                DialogueItem::ToolCall(t) => {
                    let duration_part = t.duration_ms.map(|d| format!(" · {:.1}s", d as f64 / 1000.0)).unwrap_or_default();
                    let status_part = t.exit_status.as_ref().map(|s| format!(" · {}", s)).unwrap_or_default();
                    let spinner = if t.result.is_none() {
                        Span::styled(format!("{} ", anim.spinner_frame()), Style::default().fg(theme.accent_blue))
                    } else {
                        Span::raw("  ")
                    };

                    lines.push(Line::from(vec![
                        spinner,
                        Span::styled(format!("{:<7}", t.tool), Style::default().fg(theme.text_dim)),
                        Span::styled(&t.args, Style::default().fg(theme.text_bright)),
                        Span::styled(format!("{}{}", duration_part, status_part), Style::default().fg(theme.text_extra_dim)),
                    ]));

                    if let Some(ref res) = t.result {
                        if !t.collapsed {
                            for res_line in res.lines().take(4) {
                                lines.push(Line::from(vec![
                                    Span::raw("    "),
                                    Span::styled(res_line, Style::default().fg(theme.text_extra_dim)),
                                ]));
                            }
                        }
                    }
                }
            }
        }

        // Active Thinking & Progress Indicator with Braille Spinner & Shimmer Sweep
        if app.is_busy {
            let elapsed_str = if let Some(started) = app.busy_started_at {
                let s = started.elapsed().as_secs_f64();
                format!(" · {:.1}s", s)
            } else {
                String::new()
            };

            let spinner_span = Span::styled(
                format!("{} ", anim.spinner_frame()),
                Style::default().fg(theme.accent_blue),
            );

            let status_text = if let Some(ref status) = app.thinking_status {
                status.as_str()
            } else {
                let verbs = [
                    "Thinking",
                    "Reading workspace context",
                    "Synthesizing plan & code",
                    "Verifying public API surface",
                ];
                let idx = (anim.elapsed().as_secs() / 3) as usize % verbs.len();
                verbs[idx]
            };

            let mut spans = vec![Span::raw("  "), spinner_span];
            spans.extend(anim.render_shimmer(status_text, theme.text_dim, theme.text_bright));
            spans.push(Span::styled(elapsed_str, Style::default().fg(theme.text_extra_dim)));
            lines.push(Line::from(spans));
        } else if let Some(ref status) = app.thinking_status {
            let mut spans = vec![Span::raw("  ")];
            spans.extend(anim.render_shimmer(status, theme.text_dim, theme.text_bright));
            lines.push(Line::from(spans));
        }
    }

    let p = Paragraph::new(lines)
        .wrap(Wrap { trim: false })
        .scroll((app.dialogue_scroll as u16, 0));
    p.render(stream_area, buf);

    if app.dialogue_scroll > 0 {
        crate::ui::animation::apply_top_fade(stream_area, 2, theme.window_bg, buf);
    }

    // Active Diff (sage/rose tinted)
    if let Some(ref diff) = app.active_diff {
        for file in &diff.files {
            let mut diff_lines = Vec::new();
            for hunk in &file.hunks {
                for line in &hunk.lines {
                    diff_lines.push(MockupDiffLine {
                        line_no: line.old_lineno.or(line.new_lineno),
                        line_type: line.line_type,
                        content: line.content.clone(),
                    });
                }
            }
            let filename = file.new_path.as_deref().unwrap_or("diff");
            let diff_area = Rect::new(
                stream_area.x + 4,
                stream_area.bottom().saturating_sub(8),
                stream_area.width.saturating_sub(8),
                8,
            );
            render_mockup_inline_diff(
                filename,
                diff_lines.iter().filter(|l| l.line_type == DiffLineType::Added).count(),
                diff_lines.iter().filter(|l| l.line_type == DiffLineType::Deleted).count(),
                &diff_lines,
                theme,
                diff_area,
                buf,
            );
        }
    }

    // Inline Diagnostic Error Card (if any command or test failed)
    if let Some(ref diag) = app.active_diagnostic {
        let diag_area = Rect::new(
            stream_area.x + 4,
            stream_area.bottom().saturating_sub(10),
            stream_area.width.saturating_sub(8),
            10,
        );
        crate::ui::screens::error_view::render_diagnostic_card(diag, theme, diag_area, buf);
    }

    // ─────────────────────────────────────────────
    // 3. Message Composer Box
    // ─────────────────────────────────────────────
    let comp_border = crate::ui::animation::interpolate_color(
        theme.border_mid,
        Color::Rgb(102, 102, 102), // #666
        app.input_hover_level,
    );
    let comp_block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(comp_border))
        .style(Style::default().bg(theme.card_bg));
    let comp_inner = comp_block.inner(composer_area);
    comp_block.render(composer_area, buf);

    let cursor_span = if anim.cursor_visible() {
        Span::styled("█", Style::default().fg(theme.accent_blue))
    } else {
        Span::raw(" ")
    };

    let prompt_line = if app.active_approval.is_some() {
        Line::from(vec![
            Span::styled("> ", Style::default().fg(theme.text_dim)),
            Span::styled("Waiting for permission", Style::default().fg(theme.text_dim)),
        ])
    } else if app.input_buffer.is_empty() {
        Line::from(vec![
            Span::styled("> ", Style::default().fg(theme.text_dim)),
            Span::styled("Message hex", Style::default().fg(theme.text_extra_dim)),
            cursor_span,
        ])
    } else {
        Line::from(vec![
            Span::styled("> ", Style::default().fg(theme.text_bright)),
            Span::styled(&app.input_buffer, Style::default().fg(theme.text_bright)),
            cursor_span,
        ])
    };
    buf.set_line(comp_inner.x, comp_inner.y, &prompt_line, comp_inner.width);

    // Render slash command suggestions popup if typing slash
    if !app.slash_suggestions.is_empty() {
        let pop_height = (app.slash_suggestions.len() as u16 + 2).min(8);
        let pop_width = composer_area.width.saturating_sub(4).min(56);
        let pop_y = composer_area.y.saturating_sub(pop_height);
        let pop_rect = Rect::new(composer_area.x + 2, pop_y, pop_width, pop_height);

        let pop_block = Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(theme.border_modal))
            .style(Style::default().bg(theme.card_bg))
            .title(Span::styled(" Commands ", Style::default().fg(theme.text_dim)));
        let pop_inner = pop_block.inner(pop_rect);
        pop_block.render(pop_rect, buf);

        for (i, cmd) in app.slash_suggestions.iter().enumerate() {
            let row_y = pop_inner.y + i as u16;
            if row_y >= pop_inner.bottom() {
                break;
            }
            let is_sel = i == app.selected_slash_index;
            let (bg, name_fg, desc_fg) = if is_sel {
                (theme.hover_bg, theme.accent_blue, theme.text_bright)
            } else {
                (theme.card_bg, theme.text_bright, theme.text_dim)
            };

            for x in pop_inner.left()..pop_inner.right() {
                buf[(x, row_y)].set_bg(bg);
            }
            let line = Line::from(vec![
                Span::styled(format!(" {:<12}", cmd.name), Style::default().fg(name_fg).bg(bg)),
                Span::styled(cmd.description.as_str(), Style::default().fg(desc_fg).bg(bg)),
            ]);
            buf.set_line(pop_inner.x, row_y, &line, pop_inner.width);
        }
    }

    // ─────────────────────────────────────────────
    // 4. Status Bar Footer
    // ─────────────────────────────────────────────
    let status_tag = if app.active_diagnostic.is_some() {
        Span::styled("1 failing", Style::default().fg(theme.rose_fg))
    } else {
        Span::styled("build", Style::default().fg(theme.text_bright))
    };

    let mut footer_left = vec![
        status_tag,
        Span::styled(" · ", Style::default().fg(theme.text_extra_dim)),
        Span::styled(app.model_label(), Style::default().fg(theme.text_dim)),
        Span::styled(" · ", Style::default().fg(theme.text_extra_dim)),
        Span::styled(format!("{:.1}k tokens", app.tokens_used as f64 / 1000.0), Style::default().fg(theme.text_dim)),
    ];

    let footer_right = if app.active_approval.is_some() {
        "up/down select   ·   enter confirm   ·   esc deny"
    } else {
        "esc interrupt   ·   ctrl+p commands   ·   tab agents"
    };
    let left_len = 5 + 3 + app.model_label().chars().count() + 3 + 12;
    let spaces = (footer_area.width as usize).saturating_sub(left_len + footer_right.len());
    footer_left.push(Span::raw(" ".repeat(spaces)));
    footer_left.push(Span::styled(footer_right, Style::default().fg(theme.text_dim)));

    buf.set_line(footer_area.x, footer_area.y, &Line::from(footer_left), footer_area.width);
}

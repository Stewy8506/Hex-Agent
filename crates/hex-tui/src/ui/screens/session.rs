//! Screen 1: Active Cockpit Session matching the HEX Agent HTML mockup.

use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Direction, Layout, Position, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Paragraph, Widget, Wrap};

use hex_protocol::diff::DiffLineType;
use hex_protocol::models::Role;

use crate::app::{App, DialogueItem};
use crate::ui::animation::{
    self, apply_entrance_fade, rise_factor, AnimationState, MAX_ENTRANCE_BLOCKS, RISE_STAGGER_MS,
    TOOL_HOVER_BG,
};
use crate::ui::diff::{render_mockup_inline_diff, MockupDiffLine};
use crate::ui::keymap;
use crate::ui::right_rail;
use crate::ui::style::HexTheme;

/// Canvas breathing room: 3 columns each side, 1 row on top (mockup 28px/14px).
const CANVAS_PAD_X: u16 = 3;
const CANVAS_PAD_TOP: u16 = 1;
const TOOL_INDENT: usize = 4;
/// Below this window width the right rail is dropped instead of squeezing the
/// transcript into an unreadable column.
const MIN_WIDTH_FOR_RAIL: u16 = 92;

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
    let show_rail = v_chunks[2].width >= MIN_WIDTH_FOR_RAIL.saturating_sub(2);
    let rail_width = if show_rail {
        ((v_chunks[2].width as f32 * 0.23).round() as u16)
            .clamp(28, 44)
            .min(v_chunks[2].width.saturating_sub(40))
    } else {
        0
    };
    let h_chunks = Layout::default()
        .direction(Direction::Horizontal)
        .constraints(if show_rail {
            vec![
                Constraint::Min(40),            // Left canvas
                Constraint::Length(1),          // Vertical rail divider
                Constraint::Length(rail_width), // Right rail
            ]
        } else {
            vec![Constraint::Min(40)]
        })
        .split(v_chunks[2]);

    if show_rail {
        // Draw vertical divider between canvas and right rail (#1c1c1c)
        let div_x = h_chunks[1].x;
        for y in h_chunks[1].top()..h_chunks[1].bottom() {
            buf[(div_x, y)].set_symbol("│");
            buf[(div_x, y)].set_fg(theme.border_low);
        }
    }

    // Render Left Canvas
    render_left_canvas(app, theme, anim, h_chunks[0], buf);

    // Render Right Rail
    if show_rail {
        right_rail::render(app, theme, anim, h_chunks[2], buf);
    }
}

fn render_top_bar(app: &App, theme: &HexTheme, area: Rect, buf: &mut Buffer) {
    let left = "hex";
    let right = app.git_branch.as_str();
    let pad = CANVAS_PAD_X.min(area.width / 4);
    let usable = area.width.saturating_sub(pad * 2);
    let right_w = right.chars().count().min(usable as usize / 3) as u16;
    let center_max = usable.saturating_sub(left.len() as u16 + right_w + 4);

    let center = truncate_middle(&app.workspace_path, center_max as usize);

    buf.set_string(
        area.x + pad,
        area.y,
        left,
        Style::default().fg(theme.text_dim),
    );
    if center_max > 0 {
        let center_x = area.x + pad + left.len() as u16 + 2;
        buf.set_string(
            center_x,
            area.y,
            &center,
            Style::default().fg(theme.text_dim),
        );
    }
    if right_w > 0 {
        let tail: String = right
            .chars()
            .rev()
            .take(right_w as usize)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        buf.set_string(
            area.right().saturating_sub(pad + right_w),
            area.y,
            &tail,
            Style::default().fg(theme.text_extra_dim),
        );
    }
}

/// Keep the tail of a long path, marking the cut with an ellipsis.
fn truncate_middle(text: &str, max: usize) -> String {
    let count = text.chars().count();
    if count <= max || max == 0 {
        return if max == 0 { String::new() } else { text.to_string() };
    }
    if max <= 1 {
        return "…".to_string();
    }
    let tail: String = text.chars().skip(count - (max - 1)).collect();
    format!("…{tail}")
}

fn render_left_canvas(app: &App, theme: &HexTheme, anim: &AnimationState, area: Rect, buf: &mut Buffer) {
    // Vertical split for canvas:
    // 1. Scrollable Transcript & Activity Stream (Flexible)
    // 2. Message Composer Box (5 rows: border, pad, content, pad, border)
    // 3. Status Bar Footer (1 row)
    let c_chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(6),    // Transcript / diff stream
            Constraint::Length(5), // Prompt input box
            Constraint::Length(1), // Bottom telemetry / hint footer
        ])
        .split(area);

    let pad_x = CANVAS_PAD_X.min(c_chunks[0].width / 4);
    let stream_area = Rect::new(
        c_chunks[0].x + pad_x,
        c_chunks[0].y + CANVAS_PAD_TOP.min(c_chunks[0].height),
        c_chunks[0].width.saturating_sub(pad_x * 2),
        c_chunks[0].height.saturating_sub(CANVAS_PAD_TOP),
    );
    let composer_area = Rect::new(
        c_chunks[1].x + pad_x,
        c_chunks[1].y,
        c_chunks[1].width.saturating_sub(pad_x * 2),
        c_chunks[1].height,
    );
    let footer_area = c_chunks[2];

    // Render Transcript items
    let mut lines = Vec::new();
    // Plain text of each tool row, so a mouse hover can recognise one.
    let mut tool_rows: Vec<String> = Vec::new();

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
        let mut block_started = false;

        // The failure card belongs to the tool row that produced it, so it
        // scrolls with the transcript instead of covering the newest lines.
        let diag_target = app.active_diagnostic.as_ref().and_then(|diag| {
            let status = format!("exit {}", diag.exit_code);
            app.dialogue_items.iter().rposition(|item| {
                matches!(item, DialogueItem::ToolCall(t) if t.exit_status.as_deref() == Some(status.as_str()))
            })
        });
        let mut diag_placed = false;

        // Agent Thought / Commentary
        if let Some(ref thought) = app.current_thought {
            lines.push(Line::from(vec![
                Span::raw("  "),
                Span::styled(thought, Style::default().fg(theme.text_muted)),
            ]));
            block_started = true;
        }

        let mut block_starts: Vec<usize> = Vec::new();
        for (index, item) in app.dialogue_items.iter().enumerate() {
            let continues_tools = matches!(item, DialogueItem::ToolCall(_))
                && index > 0
                && matches!(&app.dialogue_items[index - 1], DialogueItem::ToolCall(_));
            if block_started && !continues_tools {
                lines.push(Line::raw(""));
            }
            block_started = true;
            block_starts.push(lines.len());
            match item {
                DialogueItem::Message(m) => {
                    if m.role == Role::User {
                        lines.push(Line::from(vec![
                            Span::styled("> ", Style::default().fg(theme.text_dim)),
                            Span::styled(&m.content, Style::default().fg(theme.text_bright)),
                        ]));
                    } else {
                        let mut message_lines = crate::ui::markdown::render_markdown(
                            &m.content,
                            stream_area.width as usize,
                            theme,
                        );
                        if m.role == Role::System {
                            for line in &mut message_lines {
                                line.style = line.style.add_modifier(Modifier::ITALIC);
                            }
                        }
                        lines.extend(message_lines);
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
                    // The call parked on an approval is the one the operator must act on.
                    let awaiting = app.active_approval.is_some() && t.result.is_none();
                    let failed = t.exit_status.as_deref().is_some_and(|s| s.starts_with("exit ") && s != "exit 0");
                    let wait_part = if awaiting { " · waiting for permission" } else { "" };
                    let verb_color = if awaiting {
                        theme.accent_blue
                    } else if failed {
                        theme.rose_fg
                    } else {
                        theme.text_dim
                    };

                    let has_next_tool = matches!(app.dialogue_items.get(index + 1), Some(DialogueItem::ToolCall(_)));
                    let branch = if has_next_tool { "├" } else { "└" };
                    let tool_line = Line::from(vec![
                        Span::raw(" ".repeat(TOOL_INDENT)),
                        Span::styled(format!("{branch} "), Style::default().fg(theme.accent_blue)),
                        spinner,
                        Span::styled(format!("{:<7} ", t.tool), Style::default().fg(verb_color)),
                        Span::styled(crate::app::tool_args_label(&t.args), Style::default().fg(theme.text_bright)),
                        Span::styled(format!("{}{}{}", duration_part, status_part, wait_part), Style::default().fg(theme.text_extra_dim)),
                    ]);
                    tool_rows.push(tool_line.spans.iter().map(|s| s.content.as_ref()).collect::<String>());
                    lines.push(tool_line);

                    if let Some(ref res) = t.result {
                        if !t.collapsed {
                            for res_line in res.lines().take(4) {
                                let continuation = if has_next_tool { "│   " } else { "    " };
                                lines.push(Line::from(vec![
                                    Span::raw(" ".repeat(TOOL_INDENT)),
                                    Span::styled(continuation, Style::default().fg(theme.accent_blue)),
                                    Span::styled(res_line, Style::default().fg(theme.text_extra_dim)),
                                ]));
                            }
                        }
                    }

                    if Some(index) == diag_target {
                        if let Some(ref diag) = app.active_diagnostic {
                            lines.extend(crate::ui::screens::error_view::diagnostic_lines(
                                diag,
                                theme,
                                stream_area.width as usize,
                            ));
                            diag_placed = true;
                        }
                    }
                }
            }
        }

        // Newest blocks rise into place, staggered in reading order.
        let dialogue_end = lines.len();
        let count = app.dialogue_items.len();
        let (seen, born_at) = app.observe_dialogue_entrance(count);
        if !anim.is_reduced() && seen < count {
            let first = seen.max(count.saturating_sub(MAX_ENTRANCE_BLOCKS));
            let age = born_at.elapsed();
            for i in first..count {
                let delay = std::time::Duration::from_millis(RISE_STAGGER_MS * (i - first) as u64);
                let factor = rise_factor(age, delay);
                if factor >= 1.0 {
                    continue;
                }
                let start = block_starts[i];
                let end = block_starts
                    .get(i + 1)
                    .copied()
                    .unwrap_or(dialogue_end)
                    .min(dialogue_end);
                for line in &mut lines[start..end] {
                    apply_entrance_fade(line, factor, theme.window_bg);
                }
            }
        }

        // A failure whose tool row is gone still gets its card.
        if !diag_placed {
            if let Some(ref diag) = app.active_diagnostic {
                if block_started {
                    lines.push(Line::raw(""));
                }
                lines.extend(crate::ui::screens::error_view::diagnostic_lines(
                    diag,
                    theme,
                    stream_area.width as usize,
                ));
            }
        }

        // Active Thinking & Progress Indicator with Braille Spinner & Shimmer Sweep
        if block_started && (app.is_busy || app.thinking_status.is_some()) {
            lines.push(Line::raw(""));
        }
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

            let status_text = if app.active_approval.is_some() {
                "Waiting for permission"
            } else if let Some(ref status) = app.thinking_status {
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

    let total_rows = lines.len();
    let viewport_rows = stream_area.height as usize;
    app.dialogue_metrics.set((total_rows, viewport_rows));
    let max_scroll = total_rows.saturating_sub(viewport_rows);
    let scroll = if app.follow_dialogue {
        max_scroll
    } else {
        app.dialogue_scroll.min(max_scroll)
    };

    let p = Paragraph::new(lines)
        .wrap(Wrap { trim: false })
        .scroll((scroll as u16, 0));
    p.render(stream_area, buf);

    if scroll > 0 {
        // The mockup masks the top 16px of the scroll container, under one cell row.
        animation::apply_top_fade(stream_area, 1, theme.window_bg, buf);
    }

    // Mockup `.hl:hover`: the tool row under the pointer takes #141414.
    if let Some((mx, my)) = app.mouse_pos {
        if stream_area.contains(Position::new(mx, my)) && !tool_rows.is_empty() {
            let row: String = (stream_area.left()..stream_area.right())
                .map(|x| buf[(x, my)].symbol())
                .collect();
            let text = row.trim_end();
            // Match on the row's own text: a wrapped tool line's first row still
            // carries its prefix, later rows and message rows do not.
            if !text.is_empty() && tool_rows.iter().any(|t| t.starts_with(text)) {
                for x in stream_area.left()..stream_area.right() {
                    buf[(x, my)].set_bg(TOOL_HOVER_BG);
                }
            }
        }
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
                stream_area.x,
                stream_area.bottom().saturating_sub(8),
                stream_area.width,
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
    // Content sits on the middle row with a 2-column inner left pad.
    let text_x = comp_inner.x + 2;
    let text_y = comp_inner.y + comp_inner.height / 2;
    let text_w = comp_inner.width.saturating_sub(4);
    buf.set_line(text_x, text_y, &prompt_line, text_w);

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

    let hint_scope = if app.active_approval.is_some() {
        keymap::Scope::Approval
    } else {
        keymap::Scope::Session
    };
    let footer_right = keymap::footer_hints(hint_scope);
    let footer_pad = CANVAS_PAD_X.min(footer_area.width / 4);
    let footer_width = footer_area.width.saturating_sub(footer_pad * 2) as usize;
    let left_len: usize = footer_left.iter().map(|s| s.content.chars().count()).sum();
    let right_len = footer_right.chars().count();
    if left_len + right_len + 2 <= footer_width {
        footer_left.push(Span::raw(" ".repeat(footer_width - left_len - right_len)));
        footer_left.push(Span::styled(footer_right, Style::default().fg(theme.text_dim)));
    }

    buf.set_line(
        footer_area.x + footer_pad,
        footer_area.y,
        &Line::from(footer_left),
        footer_width as u16,
    );
}

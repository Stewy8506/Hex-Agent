//! Screen 0: Welcome / Start Screen matching the HEX Agent HTML mockup.

use ratatui::buffer::Buffer;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Paragraph, Widget};

use crate::app::App;
use crate::ui::animation::AnimationState;
use crate::ui::style::HexTheme;
use crate::ui::widgets::ascii_logo::StartWordmark;

pub fn render(app: &App, theme: &HexTheme, anim: &AnimationState, area: Rect, buf: &mut Buffer) {
    // Fill entire screen with canvas background (pure black #000000)
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            buf[(x, y)].set_bg(theme.canvas_bg);
        }
    }

    if area.height < 16 || area.width < 45 {
        let msg = Paragraph::new("HEX AGENT - Expand terminal to view Start Screen")
            .style(Style::default().fg(theme.text_bright));
        msg.render(area, buf);
        return;
    }

    // Responsive width calculation:
    // In Start.dc.html, the prompt box is 620px on a 1200px viewport (~51.6%).
    // On a 241-col terminal, this produces ~125 cols.
    // On a 120-col terminal, this produces ~72 cols.
    // On an 80-col terminal, this produces ~64 cols.
    let content_width = if area.width <= 70 {
        area.width.saturating_sub(4)
    } else if area.width <= 100 {
        (area.width * 75 / 100).clamp(60, 76)
    } else if area.width <= 140 {
        (area.width * 60 / 100).clamp(72, 96)
    } else {
        (area.width * 52 / 100).clamp(80, 126)
    };

    let visible_sessions_count = if app.show_all_resume {
        app.resume_sessions.len().min(10)
    } else {
        app.resume_sessions.len().min(3)
    };
    let has_toggle = app.resume_sessions.len() > 3;
    let resume_rows = if app.resume_sessions.is_empty() {
        2
    } else {
        1 + visible_sessions_count as u16 + if has_toggle { 1 } else { 0 }
    };

    // Baseline content height for optical centering matching Start.dc.html:
    // 4 (Wordmark) + 2 (spacing) + 2 (Path & Telemetry) + 2 (spacing) +
    // 5 (Prompt Box) + 1 (Hints) + 2 (spacing) + 5 (Resume block baseline) = 23 rows.
    let content_height = 23_u16;

    // True vertical centering matching Start.dc.html (justify-content: center)
    let top_pad = area.height.saturating_sub(content_height) / 2;

    let v_chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(top_pad),
            Constraint::Length(StartWordmark::HEIGHT), // 1. Wordmark (4 rows)
            Constraint::Length(2),                     // spacing
            Constraint::Length(2),                     // 2. Workspace path + git telemetry
            Constraint::Length(2),                     // spacing
            Constraint::Length(5),                     // 3. Prompt Box (5 rows tall: rounded border, input, breathing space, footer)
            Constraint::Length(1),                     // 4. Keyboard shortcut hints
            Constraint::Length(2),                     // spacing
            Constraint::Length(resume_rows),           // 5. Resume sessions list (header + items + toggle)
            Constraint::Min(0),
        ])
        .split(area);

    // ─────────────────────────────────────────────
    // 1. ASCII Block Wordmark (5-row bold block font)
    // ─────────────────────────────────────────────
    let logo_area = center_horizontally(v_chunks[1], StartWordmark::WIDTH);
    StartWordmark::new(Style::default().fg(theme.text_bright)).render(logo_area, buf);

    // ─────────────────────────────────────────────
    // 2. Workspace Path + Telemetry
    // ─────────────────────────────────────────────
    let (ws_prefix, ws_name) = crate::app::format_workspace_display(&app.workspace_path, &app.project_id);
    let ws_line = Line::from(vec![
        Span::styled(ws_prefix, theme.apfel_header_style()),
        Span::styled(ws_name, theme.apfel_title_style()),
    ]);

    let telemetry_line = Line::from(vec![
        Span::styled(&app.git_branch, Style::default().fg(Color::Rgb(140, 140, 140))),
        Span::styled(" · ", Style::default().fg(Color::Rgb(74, 74, 74))),
        Span::styled(
            format!("{} modified", app.modified_files_count),
            Style::default().fg(Color::Rgb(140, 140, 140)),
        ),
        Span::styled(" · ", Style::default().fg(Color::Rgb(74, 74, 74))),
        Span::styled(
            format!("{} files indexed", app.indexed_files_count),
            Style::default().fg(Color::Rgb(140, 140, 140)),
        ),
    ]);

    let header_par = Paragraph::new(vec![ws_line, telemetry_line]).alignment(Alignment::Center);
    header_par.render(v_chunks[3], buf);

    // ─────────────────────────────────────────────
    // 3. Prompt Box Container (5 rows tall, rounded card)
    // ─────────────────────────────────────────────
    let prompt_area = center_horizontally(v_chunks[5], content_width);

    let border_color = crate::ui::animation::interpolate_color(
        Color::Rgb(42, 42, 42),
        Color::Rgb(102, 102, 102), // #666 transition: border-color .3s ease
        app.input_hover_level,
    );

    let prompt_block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(border_color))
        .style(Style::default().bg(Color::Rgb(12, 12, 12)));
    let p_inner = prompt_block.inner(prompt_area);
    prompt_block.render(prompt_area, buf);

    if p_inner.height >= 3 {
        // Cursor logic
        let cursor_span = if anim.cursor_visible() {
            Span::styled("█", Style::default().fg(theme.accent_blue))
        } else {
            Span::raw(" ")
        };

        // Top line: Prompt text + placeholder or active text
        let input_line = if app.input_buffer.is_empty() {
            Line::from(vec![
                Span::raw("  "),
                Span::styled("> ", Style::default().fg(Color::Rgb(140, 140, 140))),
                Span::styled(anim.placeholder_prompt(), Style::default().fg(Color::Rgb(140, 140, 140))),
                cursor_span,
            ])
        } else {
            Line::from(vec![
                Span::raw("  "),
                Span::styled("> ", Style::default().fg(Color::Rgb(242, 242, 242))),
                Span::styled(&app.input_buffer, Style::default().fg(Color::Rgb(242, 242, 242))),
                cursor_span,
            ])
        };

        // Bottom line: "build · sonnet" left, "enter send" right
        let mut bottom_spans = vec![
            Span::raw("  "),
            Span::styled("build", Style::default().fg(Color::Rgb(242, 242, 242))),
            Span::styled(" · ", Style::default().fg(Color::Rgb(74, 74, 74))),
            Span::styled(app.model_label(), Style::default().fg(Color::Rgb(140, 140, 140))),
        ];

        let left_len = 2 + 5 + 3 + app.model_label().chars().count();
        let right_label = "enter send  ";
        let spaces_needed = (p_inner.width as usize)
            .saturating_sub(left_len + right_label.len());
        bottom_spans.push(Span::raw(" ".repeat(spaces_needed)));
        bottom_spans.push(Span::styled(right_label, Style::default().fg(Color::Rgb(140, 140, 140))));

        let bottom_line = Line::from(bottom_spans);

        // Render input on top row of inner card
        buf.set_line(p_inner.x, p_inner.y, &input_line, p_inner.width);
        // Render footer on bottom row of inner card
        buf.set_line(p_inner.x, p_inner.y + p_inner.height - 1, &bottom_line, p_inner.width);
    }

    // Slash command suggestions popup if typing slash
    if !app.slash_suggestions.is_empty() {
        let pop_height = (app.slash_suggestions.len() as u16 + 2).min(8);
        let pop_width = prompt_area.width.min(56);
        let pop_y = prompt_area.y.saturating_sub(pop_height);
        let pop_rect = Rect::new(prompt_area.x, pop_y, pop_width, pop_height);

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
    // 4. Keyboard Shortcut Badges below prompt
    // ─────────────────────────────────────────────
    let hints_area = center_horizontally(v_chunks[6], content_width);
    let badges = [
        ("/", " commands    ", 0),
        ("@", " files    ", 1),
        ("tab", " agent    ", 2),
        ("ctrl+r", " history", 3),
    ];
    let mut hints_spans = Vec::new();
    for (key, label, idx) in badges {
        let factor = app.shortcut_hover_levels[idx];
        let key_color = crate::ui::animation::interpolate_color(
            Color::Rgb(242, 242, 242),
            theme.accent_blue,
            factor,
        );
        let label_color = crate::ui::animation::interpolate_color(
            Color::Rgb(140, 140, 140),
            Color::Rgb(220, 220, 220),
            factor,
        );
        let mut key_style = Style::default().fg(key_color).add_modifier(Modifier::BOLD);
        if factor > 0.4 {
            key_style = key_style.add_modifier(Modifier::UNDERLINED);
        }
        hints_spans.push(Span::styled(format!("{} ", key), key_style));
        hints_spans.push(Span::styled(label, Style::default().fg(label_color)));
    }
    Paragraph::new(Line::from(hints_spans))
        .alignment(Alignment::Center)
        .render(hints_area, buf);

    // ─────────────────────────────────────────────
    // 5. Resume Previous Sessions Section
    // ─────────────────────────────────────────────
    let resume_area = center_horizontally(v_chunks[8], content_width);
    let header_line = Line::from(vec![
        Span::raw("  "),
        Span::styled("resume", theme.apfel_header_style()),
    ]);
    buf.set_line(resume_area.x, resume_area.y, &header_line, resume_area.width);

    if app.resume_sessions.is_empty() {
        let empty_line = Line::from(vec![
            Span::raw("  "),
            Span::styled("no previous sessions found", Style::default().fg(Color::Rgb(74, 74, 74))),
        ]);
        buf.set_line(resume_area.x, resume_area.y + 1, &empty_line, resume_area.width);
    } else {
        for (idx, session) in app.resume_sessions.iter().take(visible_sessions_count).enumerate() {
            let row_y = resume_area.y + 1 + idx as u16;
            if row_y >= resume_area.bottom() {
                break;
            }

            // Smooth animated hover factor (from mouse hover or keyboard selection)
            let hover_factor = app.row_hover_levels.get(idx).copied().unwrap_or(
                if idx == app.selected_resume_index { 1.0 } else { 0.0 }
            );
            let del_hover_factor = app.delete_hover_levels.get(idx).copied().unwrap_or(0.0);

            // Row background smoothly lerps from canvas_bg to #151515 (.row:hover transition: background .2s ease)
            let row_bg = crate::ui::animation::interpolate_color(
                theme.canvas_bg,
                Color::Rgb(21, 21, 21), // #151515
                hover_factor,
            );

            for x in resume_area.left()..resume_area.right() {
                buf[(x, row_y)].set_bg(row_bg);
            }

            // Title color smoothly lerps from #bdbdbd to #f2f2f2 (.row:hover .nm transition: color .2s ease)
            let title_color = crate::ui::animation::interpolate_color(
                Color::Rgb(189, 189, 189),
                Color::Rgb(242, 242, 242),
                hover_factor,
            );

            let time_width = session.relative_time.chars().count();
            let right_text_width = time_width + 5; // time + "  ✕  " (2 spaces + 1 char + 2 spaces)
            let max_title_len = (resume_area.width as usize).saturating_sub(right_text_width + 4);
            let display_title = if session.title.chars().count() > max_title_len && max_title_len > 4 {
                let truncated: String = session.title.chars().take(max_title_len - 1).collect();
                format!("{}…", truncated)
            } else {
                session.title.clone()
            };

            let left_width = 2 + display_title.chars().count();
            let spaces = (resume_area.width as usize).saturating_sub(left_width + right_text_width);

            let mut spans = vec![
                Span::raw("  "),
                Span::styled(display_title, Style::default().fg(title_color)),
            ];

            // Delete icon ('✕') color:
            // Base state: dim #4a4a4a
            // Row hover state: warm rose #c98b8b
            // Direct delete hover state: glowing vibrant rose #ff7b72 with micro-pulse
            let base_del = crate::ui::animation::interpolate_color(
                Color::Rgb(74, 74, 74),
                Color::Rgb(201, 139, 139),
                hover_factor,
            );
            let del_color = if del_hover_factor > 0.01 {
                let glow_rose = anim.pulse_color(Color::Rgb(255, 123, 114), Color::Rgb(255, 180, 180));
                crate::ui::animation::interpolate_color(base_del, glow_rose, del_hover_factor)
            } else {
                base_del
            };
            let del_style = if del_hover_factor > 0.3 {
                Style::default().fg(del_color).add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(del_color)
            };

            spans.push(Span::raw(" ".repeat(spaces)));
            spans.push(Span::styled(&session.relative_time, Style::default().fg(Color::Rgb(140, 140, 140))));
            spans.push(Span::raw("  "));
            spans.push(Span::styled("✕", del_style));
            spans.push(Span::raw("  "));

            buf.set_line(resume_area.x, row_y, &Line::from(spans), resume_area.width);
        }

        if has_toggle {
            let toggle_y = resume_area.y + 1 + visible_sessions_count as u16;
            if toggle_y < resume_area.bottom() {
                let toggle_factor = app.toggle_hover_level;
                let toggle_bg = crate::ui::animation::interpolate_color(
                    theme.canvas_bg,
                    Color::Rgb(21, 21, 21),
                    toggle_factor,
                );
                let toggle_fg = crate::ui::animation::interpolate_color(
                    Color::Rgb(140, 140, 140),
                    theme.accent_blue,
                    toggle_factor,
                );

                for x in resume_area.left()..resume_area.right() {
                    buf[(x, toggle_y)].set_bg(toggle_bg);
                }

                let toggle_label = if app.show_all_resume {
                    "  − show less".to_string()
                } else {
                    format!("  + show all ({})", app.resume_sessions.len())
                };

                let toggle_style = if toggle_factor > 0.3 {
                    Style::default().fg(toggle_fg).add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(toggle_fg)
                };

                let toggle_line = Line::from(vec![
                    Span::styled(toggle_label, toggle_style),
                ]);
                buf.set_line(resume_area.x, toggle_y, &toggle_line, resume_area.width);
            }
        }
    }
}

fn center_horizontally(area: Rect, width: u16) -> Rect {
    let w = width.min(area.width);
    let x = area.x + (area.width.saturating_sub(w)) / 2;
    Rect {
        x,
        y: area.y,
        width: w,
        height: area.height,
    }
}

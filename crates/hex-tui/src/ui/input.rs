//! The always-bottom HEX composer. Output never moves the operator's cursor.

use ratatui::buffer::Buffer;
use ratatui::layout::{Alignment, Position, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Paragraph};
use ratatui::Frame;
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::app::{App, Focus};
use crate::ui::animation::AnimationState;
use crate::ui::style::HexTheme;
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

pub fn render_composer(
    app: &App,
    theme: &HexTheme,
    anim: &AnimationState,
    area: Rect,
    placeholder: &str,
    buf: &mut Buffer,
) {
    if area.width < 3 || area.height == 0 {
        return;
    }
    let (rows, (cursor_row, cursor_col)) = composer_rows(
        &app.input_buffer, app.cursor_pos, area.width.saturating_sub(3) as usize,
    );
    let first = cursor_row.saturating_sub(area.height as usize - 1);
    for (row, text) in rows.iter().skip(first).take(area.height as usize).enumerate() {
        let line = Line::from(vec![
            Span::styled(if first + row == 0 { "> " } else { "  " }, Style::default().fg(theme.text_dim)),
            if app.input_buffer.is_empty() {
                Span::styled(placeholder, Style::default().fg(theme.text_extra_dim))
            } else {
                Span::styled(text.as_str(), Style::default().fg(theme.text_bright))
            },
        ]);
        buf.set_line(area.x, area.y + row as u16, &line, area.width);
    }
    if app.focus == Focus::Input && !app.show_palette && !app.show_help && anim.cursor_visible() {
        buf[(area.x + 2 + cursor_col as u16, area.y + (cursor_row - first) as u16)]
            .set_fg(theme.card_bg).set_bg(theme.accent_blue);
    }
}

fn composer_rows(text: &str, cursor: usize, width: usize) -> (Vec<String>, (usize, usize)) {
    let mut rows = vec![String::new()];
    let mut col = 0;
    let mut position = (0, 0);
    let mut index = 0;
    for grapheme in text.graphemes(true) {
        let display = if grapheme == "\t" { "    " } else { grapheme };
        let char_width = Span::raw(display).width();
        let newline = matches!(grapheme, "\n" | "\r\n");
        if !newline && col + char_width > width && col > 0 {
            rows.push(String::new());
            col = 0;
        }
        let next_index = index + grapheme.chars().count();
        if (index..next_index).contains(&cursor) {
            position = (rows.len() - 1, col);
        }
        if newline {
            rows.push(String::new());
            col = 0;
        } else {
            rows.last_mut().unwrap().push_str(display);
            col += char_width.min(width);
        }
        index = next_index;
    }
    if cursor == text.chars().count() {
        position = (rows.len() - 1, col);
    }
    (rows, position)
}

#[cfg(test)]
mod composer_tests {
    use super::*;

    #[test]
    fn rows_preserve_newlines_unicode_width_and_tabs() {
        let (rows, cursor) = composer_rows("a界🙂\nb\tc", 5, 6);
        assert_eq!(rows, vec!["a界🙂", "b    c"]);
        assert_eq!(cursor, (1, 1));
        let (rows, cursor) = composer_rows("世界abc", 5, 4);
        assert_eq!(rows, vec!["世界", "abc"]);
        assert_eq!(cursor, (1, 3));
    }

    #[test]
    fn joined_emoji_and_combining_marks_match_ratatui_geometry() {
        let (rows, cursor) = composer_rows("👩‍💻x", 4, 4);
        assert_eq!(rows, vec!["👩‍💻x"]);
        assert_eq!(cursor, (0, Span::raw("👩‍💻x").width()));
        let (rows, cursor) = composer_rows("👩‍💻x", 4, 2);
        assert_eq!(rows, vec!["👩‍💻", "x"]);
        assert_eq!(cursor, (1, 1));
        let (rows, cursor) = composer_rows("e\u{301}x", 3, 1);
        assert_eq!(rows, vec!["e\u{301}", "x"]);
        assert_eq!(cursor, (1, 1));
    }

    #[test]
    fn visible_rows_follow_cursor_without_changing_the_draft() {
        let mut app = App::new("p", "m", "cow");
        app.insert_text("first\nsecond\nthird\nfourth");
        let theme = HexTheme::detect();
        let area = Rect::new(0, 0, 20, 2);
        let mut buf = Buffer::empty(area);
        render_composer(&app, &theme, &AnimationState::new(), area, "hint", &mut buf);
        let row = |y| (0..area.width).map(|x| buf[(x, y)].symbol()).collect::<String>();
        assert!(row(0).contains("third"));
        assert!(row(1).contains("fourth"));
        assert_eq!(app.input_buffer, "first\nsecond\nthird\nfourth");
    }

    #[test]
    fn cursor_uses_display_columns_at_the_actual_insertion_position() {
        let mut app = App::new("p", "m", "cow");
        app.insert_text("界x");
        app.cursor_pos = 1;
        let theme = HexTheme::detect();
        let area = Rect::new(0, 0, 10, 1);
        let mut buf = Buffer::empty(area);
        render_composer(&app, &theme, &AnimationState::new(), area, "hint", &mut buf);
        assert_eq!(buf[(4, 0)].symbol(), "x");
        assert_eq!(buf[(4, 0)].bg, theme.accent_blue);
    }

    #[test]
    fn tiny_composers_do_not_write_outside_their_area() {
        let mut app = App::new("p", "m", "cow");
        app.insert_text("界\t🙂\nlast");
        let theme = HexTheme::detect();
        for width in 0..8 {
            for height in 0..3 {
                let area = Rect::new(0, 0, width, height);
                let mut buf = Buffer::empty(area);
                render_composer(&app, &theme, &AnimationState::new(), area, "hint", &mut buf);
            }
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


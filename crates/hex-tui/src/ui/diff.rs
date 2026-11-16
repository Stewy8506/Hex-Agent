//! Colorized unified and side-by-side diff viewer widget rendering added, deleted, and context lines.

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Paragraph, Wrap};
use ratatui::Frame;

use hex_protocol::diff::{DiffLineType, UnifiedDiff};

use crate::app::{App, Focus};

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    if app.side_by_side {
        render_side_by_side(frame, app, area);
    } else {
        render_unified(frame, app, area);
    }
}

fn render_unified(frame: &mut Frame, app: &App, area: Rect) {
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
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(border_color))
        .title(Span::styled(
            title,
            Style::default()
                .fg(if is_focused {
                    Color::Green
                } else {
                    Color::White
                })
                .add_modifier(if is_focused {
                    Modifier::BOLD
                } else {
                    Modifier::empty()
                }),
        ));

    let mut lines: Vec<Line> = Vec::new();

    if let Some(diff) = &app.active_diff {
        if diff.files.is_empty() {
            lines.push(Line::from(vec![Span::styled(
                "No file changes in current diff.",
                Style::default().fg(Color::DarkGray),
            )]));
        }

        for file in &diff.files {
            let old_str = file.old_path.as_deref().unwrap_or("/dev/null");
            let new_str = file.new_path.as_deref().unwrap_or("/dev/null");

            lines.push(Line::from(vec![Span::styled(
                format!("--- a/{}", old_str),
                Style::default()
                    .fg(Color::LightRed)
                    .add_modifier(Modifier::BOLD),
            )]));
            lines.push(Line::from(vec![Span::styled(
                format!("+++ b/{}", new_str),
                Style::default()
                    .fg(Color::LightGreen)
                    .add_modifier(Modifier::BOLD),
            )]));

            for hunk in &file.hunks {
                let heading_str = hunk.heading.as_deref().unwrap_or("");
                let header_text = format!(
                    "@@ -{},{} +{},{} @@ {}",
                    hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count, heading_str
                );
                lines.push(Line::from(vec![Span::styled(
                    header_text,
                    Style::default()
                        .fg(Color::Cyan)
                        .add_modifier(Modifier::BOLD),
                )]));

                for diff_line in &hunk.lines {
                    match diff_line.line_type {
                        DiffLineType::Added => {
                            lines.push(Line::from(vec![Span::styled(
                                format!("+ {}", diff_line.content),
                                Style::default().fg(Color::LightGreen),
                            )]));
                        }
                        DiffLineType::Deleted => {
                            lines.push(Line::from(vec![Span::styled(
                                format!("- {}", diff_line.content),
                                Style::default().fg(Color::LightRed),
                            )]));
                        }
                        DiffLineType::Context => {
                            lines.push(Line::from(vec![Span::styled(
                                format!("  {}", diff_line.content),
                                Style::default().fg(Color::DarkGray),
                            )]));
                        }
                        _ => {
                            lines.push(Line::from(vec![Span::styled(
                                format!("  {}", diff_line.content),
                                Style::default().fg(Color::DarkGray),
                            )]));
                        }
                    }
                }
            }
            lines.push(Line::raw(""));
        }
    } else if let Some(raw) = &app.raw_diff {
        for raw_line in raw.lines() {
            if raw_line.starts_with('+') && !raw_line.starts_with("+++") {
                lines.push(Line::from(vec![Span::styled(
                    raw_line,
                    Style::default().fg(Color::LightGreen),
                )]));
            } else if raw_line.starts_with('-') && !raw_line.starts_with("---") {
                lines.push(Line::from(vec![Span::styled(
                    raw_line,
                    Style::default().fg(Color::LightRed),
                )]));
            } else if raw_line.starts_with("@@") {
                lines.push(Line::from(vec![Span::styled(
                    raw_line,
                    Style::default()
                        .fg(Color::Cyan)
                        .add_modifier(Modifier::BOLD),
                )]));
            } else if raw_line.starts_with("---") || raw_line.starts_with("+++") {
                lines.push(Line::from(vec![Span::styled(
                    raw_line,
                    Style::default()
                        .fg(Color::Yellow)
                        .add_modifier(Modifier::BOLD),
                )]));
            } else {
                lines.push(Line::from(vec![Span::styled(
                    raw_line,
                    Style::default().fg(Color::DarkGray),
                )]));
            }
        }
    } else {
        lines.push(Line::from(vec![Span::styled(
            "No diff available. Propose changes or execute a task to view unified diff.",
            Style::default().fg(Color::DarkGray),
        )]));
    }

    let paragraph = Paragraph::new(lines)
        .block(block)
        .wrap(Wrap { trim: false })
        .scroll((app.diff_scroll as u16, 0));

    frame.render_widget(paragraph, area);
}

fn render_side_by_side(frame: &mut Frame, app: &App, area: Rect) {
    let is_focused = app.focus == Focus::Diff;
    let border_color = if is_focused {
        Color::Green
    } else {
        Color::DarkGray
    };

    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
        .split(area);

    let left_title = if is_focused {
        " Diff Viewer (Old) [FOCUSED - ↑/↓ to Scroll] "
    } else {
        " Diff Viewer (Old) "
    };
    let right_title = " Diff Viewer (New) ";

    let left_block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(border_color))
        .title(Span::styled(
            left_title,
            Style::default()
                .fg(if is_focused {
                    Color::Green
                } else {
                    Color::White
                })
                .add_modifier(if is_focused {
                    Modifier::BOLD
                } else {
                    Modifier::empty()
                }),
        ));

    let right_block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(border_color))
        .title(Span::styled(
            right_title,
            Style::default()
                .fg(if is_focused {
                    Color::Green
                } else {
                    Color::White
                })
                .add_modifier(if is_focused {
                    Modifier::BOLD
                } else {
                    Modifier::empty()
                }),
        ));

    let mut left_lines: Vec<Line> = Vec::new();
    let mut right_lines: Vec<Line> = Vec::new();

    let parsed_diff: Option<UnifiedDiff> = app.active_diff.clone().or_else(|| {
        app.raw_diff.as_deref().map(UnifiedDiff::parse)
    });

    if let Some(diff) = &parsed_diff {
        if diff.files.is_empty() {
            left_lines.push(Line::from(vec![Span::styled(
                "No file changes in current diff.",
                Style::default().fg(Color::DarkGray),
            )]));
            right_lines.push(Line::from(vec![Span::styled(
                "No file changes in current diff.",
                Style::default().fg(Color::DarkGray),
            )]));
        }

        for (file_idx, file) in diff.files.iter().enumerate() {
            let old_str = file.old_path.as_deref().unwrap_or("/dev/null");
            let new_str = file.new_path.as_deref().unwrap_or("/dev/null");

            left_lines.push(Line::from(vec![Span::styled(
                format!("--- a/{}", old_str),
                Style::default()
                    .fg(Color::LightRed)
                    .add_modifier(Modifier::BOLD),
            )]));
            right_lines.push(Line::from(vec![Span::styled(
                format!("+++ b/{}", new_str),
                Style::default()
                    .fg(Color::LightGreen)
                    .add_modifier(Modifier::BOLD),
            )]));

            let rows = diff.side_by_side(file_idx);
            for row in rows {
                // Left side (Old / Deletion / Context)
                if let Some(content) = &row.left_content {
                    let lineno_str = row
                        .left_lineno
                        .map(|n| format!("{:4} ", n))
                        .unwrap_or_else(|| "     ".to_string());
                    let (marker, fg) = match row.left_type {
                        Some(DiffLineType::Deleted) => ("- ", Color::LightRed),
                        Some(DiffLineType::Context) => ("  ", Color::DarkGray),
                        _ => ("  ", Color::DarkGray),
                    };
                    left_lines.push(Line::from(vec![
                        Span::styled(lineno_str, Style::default().fg(Color::DarkGray)),
                        Span::styled(format!("{}{}", marker, content), Style::default().fg(fg)),
                    ]));
                } else {
                    left_lines.push(Line::raw(""));
                }

                // Right side (New / Addition / Context)
                if let Some(content) = &row.right_content {
                    let lineno_str = row
                        .right_lineno
                        .map(|n| format!("{:4} ", n))
                        .unwrap_or_else(|| "     ".to_string());
                    let (marker, fg) = match row.right_type {
                        Some(DiffLineType::Added) => ("+ ", Color::LightGreen),
                        Some(DiffLineType::Context) => ("  ", Color::DarkGray),
                        _ => ("  ", Color::DarkGray),
                    };
                    right_lines.push(Line::from(vec![
                        Span::styled(lineno_str, Style::default().fg(Color::DarkGray)),
                        Span::styled(format!("{}{}", marker, content), Style::default().fg(fg)),
                    ]));
                } else {
                    right_lines.push(Line::raw(""));
                }
            }
            left_lines.push(Line::raw(""));
            right_lines.push(Line::raw(""));
        }
    } else {
        left_lines.push(Line::from(vec![Span::styled(
            "No diff available. Propose changes or execute a task to view unified diff.",
            Style::default().fg(Color::DarkGray),
        )]));
        right_lines.push(Line::from(vec![Span::styled(
            "No diff available. Propose changes or execute a task to view unified diff.",
            Style::default().fg(Color::DarkGray),
        )]));
    }

    let left_paragraph = Paragraph::new(left_lines)
        .block(left_block)
        .wrap(Wrap { trim: false })
        .scroll((app.diff_scroll as u16, 0));

    let right_paragraph = Paragraph::new(right_lines)
        .block(right_block)
        .wrap(Wrap { trim: false })
        .scroll((app.diff_scroll as u16, 0));

    frame.render_widget(left_paragraph, cols[0]);
    frame.render_widget(right_paragraph, cols[1]);
}

use crate::ui::style::HexTheme;

/// Formatted line for the mockup's inline unified diff block.
#[derive(Debug, Clone)]
pub struct MockupDiffLine {
    pub line_no: Option<usize>,
    pub line_type: DiffLineType,
    pub content: String,
}

/// Renders a Sage/Rose tinted unified diff block matching the HEX Agent HTML mockup.
pub fn render_mockup_inline_diff(
    file_path: &str,
    additions: usize,
    deletions: usize,
    lines: &[MockupDiffLine],
    theme: &HexTheme,
    area: Rect,
    buf: &mut ratatui::buffer::Buffer,
) -> u16 {
    if area.height == 0 || area.width == 0 {
        return 0;
    }

    let mut y = area.y;

    // Header row: "edit   src/graph/builder.ts" ... "+18 −31"
    let mut header_spans = vec![
        Span::styled("edit   ", Style::default().fg(theme.text_dim)),
        Span::styled(file_path, Style::default().fg(theme.text_bright)),
    ];

    let counts_str = format!("+{} −{}", additions, deletions);
    let spaces = (area.width as usize).saturating_sub(7 + file_path.len() + counts_str.len());
    header_spans.push(Span::raw(" ".repeat(spaces)));
    header_spans.push(Span::styled(format!("+{}", additions), Style::default().fg(theme.sage_fg)));
    header_spans.push(Span::raw(" "));
    header_spans.push(Span::styled(format!("−{}", deletions), Style::default().fg(theme.rose_fg)));

    buf.set_line(area.x, y, &Line::from(header_spans), area.width);
    y += 1;

    // Divider line under header
    if y < area.bottom() {
        for x in area.left()..area.right() {
            buf[(x, y)].set_symbol("─");
            buf[(x, y)].set_fg(theme.border_low);
        }
        y += 1;
    }

    // Render diff lines with sage and rose line-by-line background tints
    for diff_line in lines {
        if y >= area.bottom() {
            break;
        }

        let (bg, marker_col, text_col, marker_symbol) = match diff_line.line_type {
            DiffLineType::Added => (theme.sage_bg, theme.sage_fg, theme.sage_text, "+"),
            DiffLineType::Deleted => (theme.rose_bg, theme.rose_fg, theme.rose_text, "−"),
            DiffLineType::Context => (theme.window_bg, theme.text_extra_dim, theme.text_extra_dim, " "),
            DiffLineType::HunkHeader | DiffLineType::Meta => (
                theme.window_bg,
                theme.text_extra_dim,
                theme.text_dim,
                "~",
            ),
        };

        // Fill background across entire line width for tinted diff
        for x in area.left()..area.right() {
            buf[(x, y)].set_bg(bg);
        }

        let line_num_str = if let Some(n) = diff_line.line_no {
            format!("{:3} ", n)
        } else {
            "    ".to_string()
        };

        let row_line = Line::from(vec![
            Span::styled(line_num_str, Style::default().fg(theme.text_extra_dim).bg(bg)),
            Span::styled(format!("{} ", marker_symbol), Style::default().fg(marker_col).bg(bg)),
            Span::styled(&diff_line.content, Style::default().fg(text_col).bg(bg)),
        ]);

        buf.set_line(area.x, y, &row_line, area.width);
        y += 1;
    }

    y - area.y
}

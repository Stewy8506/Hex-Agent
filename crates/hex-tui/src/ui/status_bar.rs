//! Bottom status bar displaying hotkey bindings, focus mode, and active status.

use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::app::App;

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let (mode_color, mode_str) = match app.mode.as_str() {
        "plan" => (Color::Yellow, "PLAN"),
        "code" => (Color::Magenta, "CODE"),
        _ => (Color::Cyan, "AGENT"),
    };

    let mut spans = vec![
        Span::styled(format!(" [{}] ", mode_str), Style::default().fg(Color::Black).bg(mode_color).add_modifier(Modifier::BOLD)),
        Span::raw(" "),
    ];

    if app.active_approval.is_some() {
        spans.push(Span::styled("[y] ", Style::default().fg(Color::Green).add_modifier(Modifier::BOLD)));
        spans.push(Span::styled("Approve  ", Style::default().fg(Color::White)));
        spans.push(Span::styled("[n] ", Style::default().fg(Color::Red).add_modifier(Modifier::BOLD)));
        spans.push(Span::styled("Reject  ", Style::default().fg(Color::White)));
    } else {
        spans.push(Span::styled("[Enter] ", Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)));
        spans.push(Span::styled("Send  ", Style::default().fg(Color::White)));

        spans.push(Span::styled("[Tab] ", Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)));
        spans.push(Span::styled("Mode  ", Style::default().fg(Color::White)));

        spans.push(Span::styled("[Scroll] ", Style::default().fg(Color::White).add_modifier(Modifier::BOLD)));
        spans.push(Span::styled("Transcript  ", Style::default().fg(Color::DarkGray)));

        spans.push(Span::styled("[F1] ", Style::default().fg(Color::Magenta).add_modifier(Modifier::BOLD)));
        spans.push(Span::styled("Help  ", Style::default().fg(Color::White)));

        if app.active_diff.is_some() || app.raw_diff.is_some() {
            spans.push(Span::styled("[F2/d] ", Style::default().fg(Color::Green).add_modifier(Modifier::BOLD)));
            spans.push(Span::styled("Diff  ", Style::default().fg(Color::White)));
        }

        spans.push(Span::styled("[Ctrl+C] ", Style::default().fg(Color::Red).add_modifier(Modifier::BOLD)));
        spans.push(Span::styled("Quit", Style::default().fg(Color::White)));
    }

    spans.push(Span::raw(" │ "));
    let status_style = if app.is_busy {
        Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(Color::DarkGray)
    };
    spans.push(Span::styled(&app.status_message, status_style));

    let paragraph = Paragraph::new(Line::from(spans)).style(Style::default().bg(Color::Rgb(20, 20, 25)));
    frame.render_widget(paragraph, area);
}

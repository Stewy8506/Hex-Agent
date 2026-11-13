//! Top header widget with system telemetry, model routing, git branch, and tools count.

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Paragraph};
use ratatui::Frame;

use crate::app::App;

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(Color::DarkGray))
        .title(Span::styled(
            " ◈ HEX AUTONOMOUS COCKPIT ◈ ",
            Style::default()
                .fg(Color::Yellow)
                .add_modifier(Modifier::BOLD),
        ));

    let inner = block.inner(area);
    frame.render_widget(block, area);

    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Length(1)])
        .split(inner);

    // Line 1: Logo, Project, Git Branch, and Session ID
    let banner_spans = vec![
        Span::styled(
            " HEX ",
            Style::default()
                .fg(Color::Black)
                .bg(Color::Yellow)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw(" "),
        Span::styled(
            "Autonomous AI Workbench",
            Style::default()
                .fg(Color::White)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw("  │  "),
        Span::styled("Branch: ", Style::default().fg(Color::DarkGray)),
        Span::styled(
            format!("{} ({})", app.git_branch, app.git_sha),
            Style::default()
                .fg(Color::Magenta)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw("  │  "),
        Span::styled("Project: ", Style::default().fg(Color::DarkGray)),
        Span::styled(&app.project_id, Style::default().fg(Color::Cyan)),
        Span::raw("  │  "),
        Span::styled("Session: ", Style::default().fg(Color::DarkGray)),
        Span::styled(&app.session_id, Style::default().fg(Color::White)),
    ];
    frame.render_widget(Paragraph::new(Line::from(banner_spans)), chunks[0]);

    // Line 2: Real Telemetry Badges (Model, CoW, Tools count, Skills count, Status)
    let conn_color = if app.connection_status.contains("Connected") {
        Color::Green
    } else {
        Color::Yellow
    };

    let telemetry_spans = vec![
        Span::styled("Model: ", Style::default().fg(Color::DarkGray)),
        Span::styled(
            format!("[{}] ", app.model_name),
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw(" "),
        Span::styled("CoW: ", Style::default().fg(Color::DarkGray)),
        Span::styled(
            format!("[{}] ", app.cow_mode),
            Style::default().fg(Color::Blue),
        ),
        Span::raw(" "),
        Span::styled("Tools: ", Style::default().fg(Color::DarkGray)),
        Span::styled(
            format!("[{}] ", app.tools_count),
            Style::default().fg(Color::Yellow),
        ),
        Span::raw(" "),
        Span::styled("Skills: ", Style::default().fg(Color::DarkGray)),
        Span::styled(
            format!("[{}] ", app.skills_count),
            Style::default().fg(Color::Magenta),
        ),
        Span::raw(" "),
        Span::styled("Status: ", Style::default().fg(Color::DarkGray)),
        Span::styled(
            format!("[● {}] ", app.connection_status),
            Style::default().fg(conn_color).add_modifier(Modifier::BOLD),
        ),
    ];
    frame.render_widget(Paragraph::new(Line::from(telemetry_spans)), chunks[1]);
}

//! Top header widget with system telemetry, model routing, git branch, and tools count.

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Paragraph};
use ratatui::Frame;

use crate::app::App;
use crate::ui::design::skin;

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let s = skin(&app.skin_name);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(s.border))
        .title(Span::styled(
            " ◈ HEX AGENT ◈ ",
            Style::default()
                .fg(s.accent)
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
                .fg(s.surface)
                .bg(s.accent)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw(" "),
        Span::styled(
            "Agent",
            Style::default()
                .fg(s.heading)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled("  │  ", Style::default().fg(s.border)),
        Span::styled("Branch: ", Style::default().fg(s.muted)),
        Span::styled(
            format!("{} ({})", app.git_branch, app.git_sha),
            Style::default()
                .fg(s.accent)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled("  │  ", Style::default().fg(s.border)),
        Span::styled("Project: ", Style::default().fg(s.muted)),
        Span::styled(&app.project_id, Style::default().fg(s.text)),
        Span::styled("  │  ", Style::default().fg(s.border)),
        Span::styled("Session: ", Style::default().fg(s.muted)),
        Span::styled(&app.session_id, Style::default().fg(s.muted)),
    ];
    frame.render_widget(Paragraph::new(Line::from(banner_spans)), chunks[0]);

    // Line 2: Real Telemetry Badges (Model, CoW, Tools count, Skills count, Status)
    let conn_color = if app.connection_status.contains("Connected") {
        s.accent
    } else {
        s.muted
    };

    let telemetry_spans = vec![
        Span::styled("Model: ", Style::default().fg(s.muted)),
        Span::styled(
            format!("[{}] ", app.model_label()),
            Style::default()
                .fg(s.accent)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw(" "),
        Span::styled("CoW: ", Style::default().fg(s.muted)),
        Span::styled(
            format!("[{}] ", app.cow_mode),
            Style::default().fg(s.category),
        ),
        Span::raw(" "),
        Span::styled("Tools: ", Style::default().fg(s.muted)),
        Span::styled(
            format!("[{}] ", app.tools_count.map_or_else(|| "?".to_string(), |n| n.to_string())),
            Style::default().fg(s.category),
        ),
        Span::raw(" "),
        Span::styled("Skills: ", Style::default().fg(s.muted)),
        Span::styled(
            format!("[{}] ", app.skills_count.map_or_else(|| "?".to_string(), |n| n.to_string())),
            Style::default().fg(s.category),
        ),
        Span::raw(" "),
        Span::styled("Status: ", Style::default().fg(s.muted)),
        Span::styled(
            format!("[● {}] ", app.connection_status),
            Style::default().fg(conn_color).add_modifier(Modifier::BOLD),
        ),
    ];
    frame.render_widget(Paragraph::new(Line::from(telemetry_spans)), chunks[1]);
}

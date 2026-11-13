//! Persistent telemetry immediately above the fixed composer.

use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::app::App;
use crate::ui::design::{skin, SPINNER, VERBS};

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let s = skin(&app.skin_name);
    let elapsed = app.session_started.elapsed().as_secs();
    let (mode, mode_color) = match app.mode.as_str() {
        "plan" => ("plan", s.warn),
        "code" => ("code", s.category),
        _ => ("agent", s.accent),
    };
    let activity = if app.is_busy {
        let i = app.animation_frame as usize;
        format!(
            "{} {}",
            SPINNER[i % SPINNER.len()],
            VERBS[(i / 7) % VERBS.len()]
        )
    } else {
        "ready".to_string()
    };
    let status_color = if app.is_busy { s.warn } else { s.muted };
    let line = Line::from(vec![
        Span::styled(
            format!(" {} ", mode),
            Style::default()
                .fg(s.surface)
                .bg(mode_color)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!(" {} ", app.project_id),
            Style::default().fg(s.heading),
        ),
        Span::styled("· ", Style::default().fg(s.muted)),
        Span::styled(
            format!("{} ", app.git_branch),
            Style::default().fg(s.category).add_modifier(Modifier::BOLD),
        ),
        Span::styled("· ", Style::default().fg(s.muted)),
        Span::styled(format!("{} ", app.model_name), Style::default().fg(s.text)),
        Span::styled("│ ", Style::default().fg(s.border)),
        Span::styled(
            activity,
            Style::default()
                .fg(status_color)
                .add_modifier(if app.is_busy {
                    Modifier::BOLD
                } else {
                    Modifier::empty()
                }),
        ),
        Span::styled(format!(" · {}s", elapsed), Style::default().fg(s.muted)),
        Span::styled(
            "  enter send · tab mode · /help",
            Style::default().fg(s.muted),
        ),
    ]);
    frame.render_widget(
        Paragraph::new(line).style(Style::default().bg(s.surface)),
        area,
    );
}

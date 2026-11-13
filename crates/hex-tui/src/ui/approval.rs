//! Modal approval dialog for R3/R4 privileged and destructive tool invocations.

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;

use crate::app::App;

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let req = match &app.active_approval {
        Some(r) => r,
        None => return,
    };

    let popup_area = centered_rect(65, 45, area);
    frame.render_widget(Clear, popup_area);

    let is_r4 = req.risk_level.contains("R4") || req.risk_level.to_uppercase().contains("DESTRUCTIVE");
    let border_color = if is_r4 { Color::Red } else { Color::Yellow };

    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Double)
        .border_style(Style::default().fg(border_color))
        .title(Span::styled(
            " ⚠️ OPERATOR APPROVAL REQUIRED ⚠️ ",
            Style::default().fg(border_color).add_modifier(Modifier::BOLD),
        ));

    let inner = block.inner(popup_area);
    frame.render_widget(block, popup_area);

    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1), // Risk badge
            Constraint::Length(1), // Tool name
            Constraint::Length(2), // Reason
            Constraint::Min(3),    // Parameters
            Constraint::Length(2), // Buttons
        ])
        .split(inner);

    // 1. Risk badge
    let risk_span = Span::styled(
        format!("Risk Level: [{}]", req.risk_level),
        Style::default().fg(border_color).add_modifier(Modifier::BOLD),
    );
    frame.render_widget(Paragraph::new(Line::from(risk_span)), chunks[0]);

    // 2. Tool name
    let tool_spans = vec![
        Span::styled("Privileged Tool: ", Style::default().fg(Color::DarkGray)),
        Span::styled(&req.tool_name, Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        Span::styled(format!("  (ID: {})", req.id), Style::default().fg(Color::DarkGray)),
    ];
    frame.render_widget(Paragraph::new(Line::from(tool_spans)), chunks[1]);

    // 3. Reason
    let reason_spans = vec![
        Span::styled("Policy Warning: ", Style::default().fg(Color::Yellow)),
        Span::styled(&req.reason, Style::default().fg(Color::White)),
    ];
    frame.render_widget(Paragraph::new(Line::from(reason_spans)).wrap(Wrap { trim: true }), chunks[2]);

    // 4. Parameters box
    let params_block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(Color::DarkGray))
        .title(Span::styled(" Payload & Arguments ", Style::default().fg(Color::DarkGray)));
    let params_p = Paragraph::new(req.parameters.as_str())
        .block(params_block)
        .wrap(Wrap { trim: false });
    frame.render_widget(params_p, chunks[3]);

    // 5. Action Buttons
    let actions = Line::from(vec![
        Span::styled(" [y] Approve ", Style::default().fg(Color::Black).bg(Color::Green).add_modifier(Modifier::BOLD)),
        Span::raw("   "),
        Span::styled(" [n] Reject ", Style::default().fg(Color::Black).bg(Color::Red).add_modifier(Modifier::BOLD)),
        Span::raw("   "),
        Span::styled(" [d] View Diff ", Style::default().fg(Color::Cyan)),
        Span::raw("   "),
        Span::styled(" [Esc] Dismiss ", Style::default().fg(Color::DarkGray)),
    ]);
    frame.render_widget(Paragraph::new(actions), chunks[4]);
}

fn centered_rect(percent_x: u16, percent_y: u16, r: Rect) -> Rect {
    let popup_layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Percentage((100 - percent_y) / 2),
            Constraint::Percentage(percent_y),
            Constraint::Percentage((100 - percent_y) / 2),
        ])
        .split(r);

    Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage((100 - percent_x) / 2),
            Constraint::Percentage(percent_x),
            Constraint::Percentage((100 - percent_x) / 2),
        ])
        .split(popup_layout[1])[1]
}

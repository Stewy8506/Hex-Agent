//! Top-level UI orchestration and layout splits.

pub mod approval;
pub mod dialogue;
pub mod diff;
pub mod header;
pub mod input;
pub mod status_bar;

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;

use crate::app::App;

/// Primary render entrypoint called on each frame.
pub fn draw(frame: &mut Frame, app: &App) {
    let size = frame.area();

    // Vertical top-level splits
    let root_chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(6),    // Main viewport (Dialogue and/or Diff)
            Constraint::Length(3), // Command input prompt
            Constraint::Length(1), // Bottom status bar
        ])
        .split(size);

    // 1. Main Viewport: Dialogue and Diff
    let main_area = root_chunks[0];
    let show_diff = app.show_diff && (app.active_diff.is_some() || app.raw_diff.is_some());

    if show_diff {
        let h_chunks = Layout::default()
            .direction(Direction::Horizontal)
            .constraints([
                Constraint::Percentage(55), // Dialogue history
                Constraint::Percentage(45), // Unified Diff viewer
            ])
            .split(main_area);

        dialogue::render(frame, app, h_chunks[0]);
        diff::render(frame, app, h_chunks[1]);
    } else {
        dialogue::render(frame, app, main_area);
    }

    // 2. Command Prompt
    input::render(frame, app, root_chunks[1]);

    // 3. Status Bar
    status_bar::render(frame, app, root_chunks[2]);

    // 5. Help Modal Overlay
    if app.show_help {
        render_help_overlay(frame, size);
    }

    // 6. Approval Dialog Overlay (highest z-index)
    if app.active_approval.is_some() {
        approval::render(frame, app, size);
    }
}

fn render_help_overlay(frame: &mut Frame, area: Rect) {
    let popup = centered_rect(70, 70, area);
    frame.render_widget(Clear, popup);

    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(Color::Cyan))
        .title(Span::styled(
            " ◈ HEX WORKBENCH HELP & KEYBINDINGS ◈ ",
            Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD),
        ));

    let inner = block.inner(popup);
    frame.render_widget(block, popup);

    let lines = vec![
        Line::from(vec![
            Span::styled("Navigation & Focus:", Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
        ]),
        Line::from(vec![
            Span::styled("  [Tab]            ", Style::default().fg(Color::Cyan)),
            Span::raw("Cycle execution mode: agent -> plan -> code"),
        ]),
        Line::from(vec![
            Span::styled("  [Mouse / PgUp/Dn]", Style::default().fg(Color::Cyan)),
            Span::raw("Scroll dialogue transcript naturally"),
        ]),
        Line::from(vec![
            Span::styled("  [↑ / ↓]          ", Style::default().fg(Color::Cyan)),
            Span::raw("Navigate command history or scroll when empty"),
        ]),
        Line::from(vec![
            Span::styled("  [F1]             ", Style::default().fg(Color::Cyan)),
            Span::raw("Toggle this Help modal"),
        ]),
        Line::from(vec![
            Span::styled("  [F2] / [d]       ", Style::default().fg(Color::Cyan)),
            Span::raw("Toggle proposed CoW diff pane"),
        ]),
        Line::from(vec![
            Span::styled("  [Ctrl+C]         ", Style::default().fg(Color::Cyan)),
            Span::raw("Quit the HEX terminal cockpit"),
        ]),
        Line::raw(""),
        Line::from(vec![
            Span::styled("Operator Approvals (R3/R4 Policy):", Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
        ]),
        Line::from(vec![
            Span::styled("  [y]              ", Style::default().fg(Color::Green)),
            Span::raw("Approve tool execution and resume agent loop"),
        ]),
        Line::from(vec![
            Span::styled("  [n]              ", Style::default().fg(Color::Red)),
            Span::raw("Reject tool execution and trigger rollback"),
        ]),
        Line::from(vec![
            Span::styled("  [Esc]            ", Style::default().fg(Color::DarkGray)),
            Span::raw("Dismiss active dialog"),
        ]),
        Line::raw(""),
        Line::from(vec![
            Span::styled("Interactive Slash Commands:", Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
        ]),
        Line::from(vec![
            Span::styled("  /help            ", Style::default().fg(Color::White)),
            Span::raw("Show keybindings and commands"),
        ]),
        Line::from(vec![
            Span::styled("  /status          ", Style::default().fg(Color::White)),
            Span::raw("Display ledger and session status"),
        ]),
        Line::from(vec![
            Span::styled("  /diff            ", Style::default().fg(Color::White)),
            Span::raw("Toggle unified diff viewer"),
        ]),
        Line::from(vec![
            Span::styled("  /rollback        ", Style::default().fg(Color::White)),
            Span::raw("Discard workspace modifications"),
        ]),
        Line::from(vec![
            Span::styled("  /skills          ", Style::default().fg(Color::White)),
            Span::raw("List NVIDIA SkillSpector registered skills"),
        ]),
        Line::from(vec![
            Span::styled("  /react           ", Style::default().fg(Color::White)),
            Span::raw("Toggle continuous multi-turn ReAct mode"),
        ]),
        Line::from(vec![
            Span::styled("  /clear           ", Style::default().fg(Color::White)),
            Span::raw("Clear conversation history"),
        ]),
        Line::from(vec![
            Span::styled("  /quit            ", Style::default().fg(Color::White)),
            Span::raw("Exit application"),
        ]),
        Line::raw(""),
        Line::from(vec![
            Span::styled("Press [Esc] or [F1] to close help.", Style::default().fg(Color::DarkGray)),
        ]),
    ];

    let p = Paragraph::new(lines).wrap(Wrap { trim: false });
    frame.render_widget(p, inner);
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

//! Top-level UI orchestration and layout splits.

pub mod approval;
pub mod design;
pub mod dialogue;
pub mod diff;
pub mod header;
pub mod input;
pub mod status_bar;

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;

use crate::app::App;

/// Primary render entrypoint called on each frame.
pub fn draw(frame: &mut Frame, app: &App) {
    let size = frame.area();

    // Clean, minimalist Hermes-style layout: open dialogue canvas + bottom composer
    let root_chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(6),    // Main viewport: Dialogue and Diff
            Constraint::Length(3), // Composer locked to terminal bottom with integrated status footer
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

    // 2. Command Prompt with integrated bottom status border
    input::render(frame, app, root_chunks[1]);

    // 3. Help Modal Overlay
    if app.show_help {
        render_help_overlay(frame, app, size);
    }

    // 4. Approval Dialog Overlay (highest z-index)
    if app.active_approval.is_some() {
        approval::render(frame, app, size);
    }
}

fn render_help_overlay(frame: &mut Frame, app: &App, area: Rect) {
    let popup = centered_rect(70, 70, area);
    frame.render_widget(Clear, popup);

    let s = design::skin(&app.skin_name);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(s.border))
        .title(Span::styled(
            " ◈ HEX AGENT HELP & KEYBINDINGS ◈ ",
            Style::default()
                .fg(s.accent)
                .add_modifier(Modifier::BOLD),
        ));

    let inner = block.inner(popup);
    frame.render_widget(block, popup);

    let lines = vec![
        Line::from(vec![Span::styled(
            "Navigation & Focus:",
            Style::default()
                .fg(s.accent)
                .add_modifier(Modifier::BOLD),
        )]),
        Line::from(vec![
            Span::styled("  [Tab]            ", Style::default().fg(s.accent)),
            Span::raw("Cycle viewport focus (Input -> Dialogue -> Diff)"),
        ]),
        Line::from(vec![
            Span::styled("  [F3]             ", Style::default().fg(s.accent)),
            Span::raw("Cycle execution mode: agent -> plan -> code"),
        ]),
        Line::from(vec![
            Span::styled("  [Mouse / PgUp/Dn]", Style::default().fg(s.accent)),
            Span::raw("Scroll dialogue transcript naturally"),
        ]),
        Line::from(vec![
            Span::styled("  [↑ / ↓]          ", Style::default().fg(s.accent)),
            Span::raw("Navigate command history or scroll when empty"),
        ]),
        Line::from(vec![
            Span::styled("  [F1]             ", Style::default().fg(s.accent)),
            Span::raw("Toggle this Help modal"),
        ]),
        Line::from(vec![
            Span::styled("  [F2] / [d]       ", Style::default().fg(s.accent)),
            Span::raw("Toggle proposed CoW diff pane"),
        ]),
        Line::from(vec![
            Span::styled("  [Ctrl+C]         ", Style::default().fg(s.accent)),
            Span::raw("Quit the HEX terminal cockpit"),
        ]),
        Line::raw(""),
        Line::from(vec![Span::styled(
            "Operator Approvals (R3/R4 Policy):",
            Style::default()
                .fg(s.accent)
                .add_modifier(Modifier::BOLD),
        )]),
        Line::from(vec![
            Span::styled("  [y]              ", Style::default().fg(s.success)),
            Span::raw("Approve tool execution and resume agent loop"),
        ]),
        Line::from(vec![
            Span::styled("  [n]              ", Style::default().fg(s.danger)),
            Span::raw("Reject tool execution and trigger rollback"),
        ]),
        Line::from(vec![
            Span::styled("  [Esc]            ", Style::default().fg(s.muted)),
            Span::raw("Dismiss active dialog"),
        ]),
        Line::raw(""),
        Line::from(vec![Span::styled(
            "Interactive Slash Commands:",
            Style::default()
                .fg(s.accent)
                .add_modifier(Modifier::BOLD),
        )]),
        Line::from(vec![
            Span::styled("  /help            ", Style::default().fg(s.category)),
            Span::raw("Show keybindings and commands"),
        ]),
        Line::from(vec![
            Span::styled("  /status          ", Style::default().fg(s.category)),
            Span::raw("Display ledger and session status"),
        ]),
        Line::from(vec![
            Span::styled("  /diff            ", Style::default().fg(s.category)),
            Span::raw("Toggle unified diff viewer"),
        ]),
        Line::from(vec![
            Span::styled("  /rollback        ", Style::default().fg(s.category)),
            Span::raw("Discard workspace modifications"),
        ]),
        Line::from(vec![
            Span::styled("  /skills          ", Style::default().fg(s.category)),
            Span::raw("List NVIDIA SkillSpector registered skills"),
        ]),
        Line::from(vec![
            Span::styled("  /react           ", Style::default().fg(s.category)),
            Span::raw("Toggle continuous multi-turn ReAct mode"),
        ]),
        Line::from(vec![
            Span::styled("  /clear           ", Style::default().fg(s.category)),
            Span::raw("Clear conversation history"),
        ]),
        Line::from(vec![
            Span::styled("  /quit            ", Style::default().fg(s.category)),
            Span::raw("Exit application"),
        ]),
        Line::raw(""),
        Line::from(vec![Span::styled(
            "Press [Esc] or [F1] to close help.",
            Style::default().fg(s.muted),
        )]),
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

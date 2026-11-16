//! Top-level UI orchestration and layout splits.

pub mod animation;
pub mod approval;
pub mod design;
pub mod dialogue;
pub mod diff;
pub mod footer;
pub mod header;
pub mod input;
pub mod right_rail;
pub mod screens;
pub mod status_bar;
pub mod style;
pub mod widgets;

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;

use crate::app::{App, ScreenState};

/// Primary render entrypoint called on each frame.
pub fn draw(frame: &mut Frame, app: &App) {
    let size = frame.area();
    let theme = style::HexTheme::detect();
    let anim = animation::AnimationState::with_start(app.session_started);

    // Side-by-side dedicated diff inspection mode if toggled
    if app.side_by_side && app.show_diff {
        diff::render(frame, app, size);
        return;
    }

    // 1. Render primary screen based on application screen state
    match app.screen_state {
        ScreenState::Start => {
            screens::start::render(app, &theme, &anim, size, frame.buffer_mut());
        }
        ScreenState::Session => {
            screens::session::render(app, &theme, &anim, size, frame.buffer_mut());
        }
    }

    // 2. Command Palette Modal Overlay
    if app.show_palette {
        screens::palette::render(app, &theme, &anim, size, frame.buffer_mut());
    }

    // 3. Subagent Inspection Modal Overlay
    if let Some(ref subagent_modal) = app.active_subagent_modal {
        screens::subagent::render(app, subagent_modal, &theme, &anim, size, frame.buffer_mut());
    }

    // 4. Permission / Approval Dialog Overlay (highest modal z-index)
    if app.active_approval.is_some() {
        screens::permission::render(app, &theme, size, frame.buffer_mut());
    }

    // 5. Help Modal Overlay
    if app.show_help {
        render_help_overlay(frame, app, size);
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

    let mut lines = vec![
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
            Span::raw("Cancel the active turn; press again to quit"),
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
    ];

    lines.push(Line::from(vec![Span::styled(
        "Interactive Slash Commands:",
        Style::default()
            .fg(s.accent)
            .add_modifier(Modifier::BOLD),
    )]));
    let commands = crate::app::available_slash_commands();
    let room = (inner.height as usize).saturating_sub(lines.len() + 2);
    let shown = commands.len().min(room);
    for cmd in &commands[..shown] {
        lines.push(Line::from(vec![
            Span::styled(
                format!("  {:<16}", cmd.name),
                Style::default().fg(s.category),
            ),
            Span::raw(cmd.description.clone()),
        ]));
    }
    if shown < commands.len() {
        lines.push(Line::from(Span::styled(
            format!(
                "  ... {} more - type / for full completion",
                commands.len() - shown
            ),
            Style::default().fg(s.muted),
        )));
    }
    lines.push(Line::raw(""));
    lines.push(Line::from(Span::styled(
        "Press [Esc] or [F1] to close help.",
        Style::default().fg(s.muted),
    )));

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

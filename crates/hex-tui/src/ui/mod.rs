//! Top-level UI orchestration and layout splits.

pub mod animation;
pub mod approval;
pub mod design;
pub mod dialogue;
pub mod diff;
pub mod footer;
pub mod header;
pub mod input;
pub mod keymap;
pub mod markdown;
pub mod right_rail;
pub mod screens;
pub mod status_bar;
pub mod style;
pub mod transcript;
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
    app.approval_can_allow.set(false);
    app.sidebar.clear_geometry();
    let theme = style::HexTheme::detect();
    let anim = animation::AnimationState::with_start(app.session_started);

    // Diff is a primary view, not an escape from the modal overlay stack.
    if app.side_by_side && app.show_diff {
        diff::render(frame, app, size);
    } else {
        match app.screen_state {
            ScreenState::Start => {
                screens::start::render(app, &theme, &anim, size, frame.buffer_mut());
            }
            ScreenState::Session => {
                screens::session::render(app, &theme, &anim, size, frame.buffer_mut());
            }
        }
    }

    if app.sidebar.show_settings {
        right_rail::render_settings(app, &theme, size, frame.buffer_mut());
    }

    // 2. Command Palette Modal Overlay
    if app.show_palette {
        screens::palette::render(app, &theme, &anim, size, frame.buffer_mut());
    }

    // 3. Subagent Inspection Modal Overlay
    if let Some(ref subagent_modal) = app.active_subagent_modal {
        screens::subagent::render(app, subagent_modal, &theme, &anim, size, frame.buffer_mut());
    }

    // 4. Help Modal Overlay
    if app.show_help {
        render_help_overlay(frame, app, size);
    }

    // 5. Approval is always last, including over help and dedicated diff views.
    if app.active_approval.is_some() {
        screens::permission::render(app, &theme, size, frame.buffer_mut());
    }
}

fn render_help_overlay(frame: &mut Frame, app: &App, area: Rect) {
    // Tall enough that the binding table does not squeeze out the command list.
    let popup = centered_rect(85, 95, area);
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

    let mut lines: Vec<Line<'static>> = Vec::new();
    for (scope, bindings) in keymap::help_sections() {
        lines.push(Line::from(Span::styled(
            scope.heading(),
            Style::default()
                .fg(s.accent)
                .add_modifier(Modifier::BOLD),
        )));
        let joined = bindings
            .iter()
            .map(|b| b.hint())
            .collect::<Vec<_>>()
            .join(" · ");
        lines.push(Line::from(Span::styled(
            format!("  {joined}"),
            Style::default().fg(s.text),
        )));
    }
    lines.push(Line::from(Span::styled(
        "Shift+Enter inserts a newline; bracketed paste stays editable until Enter sends.",
        Style::default().fg(s.muted),
    )));
    lines.push(Line::from(Span::styled(
        "Wheel scrolls the pane under the pointer; drag the sidebar divider to resize.",
        Style::default().fg(s.muted),
    )));
    lines.push(Line::raw(""));

    lines.push(Line::from(vec![Span::styled(
        "Interactive Slash Commands:",
        Style::default()
            .fg(s.accent)
            .add_modifier(Modifier::BOLD),
    )]));
    let commands = crate::app::available_slash_commands();
    // Paragraph wraps, so budget real rows rather than line count.
    let width = (inner.width as usize).max(1);
    let used: usize = lines
        .iter()
        .map(|l| {
            let chars: usize = l.spans.iter().map(|sp| sp.content.chars().count()).sum();
            chars.max(1).div_ceil(width)
        })
        .sum();
    let room = (inner.height as usize).saturating_sub(used + 3);
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
                "  ... {} more - press / for the command palette",
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

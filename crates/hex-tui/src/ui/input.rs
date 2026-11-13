//! Interactive command prompt widget with cursor positioning and slash command suggestions.

use ratatui::layout::{Position, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, List, ListItem, Paragraph};
use ratatui::Frame;

use crate::app::{App, Focus};

pub fn render(frame: &mut Frame, app: &App, area: Rect) {
    let is_focused = app.focus == Focus::Input;
    let border_color = if is_focused {
        Color::Yellow
    } else {
        Color::DarkGray
    };

    let title = if is_focused {
        " Command Prompt [FOCUSED - Enter to Send, / for Commands] "
    } else {
        " Command Prompt "
    };

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border_color))
        .title(Span::styled(
            title,
            Style::default()
                .fg(if is_focused { Color::Yellow } else { Color::White })
                .add_modifier(if is_focused { Modifier::BOLD } else { Modifier::empty() }),
        ));

    let prefix = "hex> ";
    let input_line = Line::from(vec![
        Span::styled(prefix, Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
        Span::raw(&app.input_buffer),
    ]);

    let paragraph = Paragraph::new(input_line).block(block);
    frame.render_widget(paragraph, area);

    // Render cursor if input is focused
    if is_focused {
        let cursor_x = area.x + 1 + (prefix.len() as u16) + (app.cursor_pos as u16);
        let cursor_y = area.y + 1;
        if cursor_x < area.x + area.width - 1 {
            frame.set_cursor_position(Position::new(cursor_x, cursor_y));
        }
    }

    // Render slash-command suggestion popup if active
    if !app.slash_suggestions.is_empty() && is_focused {
        let popup_height = (app.slash_suggestions.len() as u16 + 2).min(7);
        let popup_width = 50.min(area.width.saturating_sub(4));
        let popup_y = area.y.saturating_sub(popup_height);
        let popup_x = area.x + 2;

        let popup_area = Rect::new(popup_x, popup_y, popup_width, popup_height);
        frame.render_widget(Clear, popup_area);

        let items: Vec<ListItem> = app
            .slash_suggestions
            .iter()
            .enumerate()
            .map(|(idx, cmd)| {
                let is_selected = idx == app.selected_slash_index;
                let line = Line::from(vec![
                    Span::styled(
                        format!("{:<10} ", cmd.name),
                        if is_selected {
                            Style::default().fg(Color::Black).bg(Color::Cyan).add_modifier(Modifier::BOLD)
                        } else {
                            Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)
                        },
                    ),
                    Span::styled(
                        cmd.description,
                        if is_selected {
                            Style::default().fg(Color::Black).bg(Color::Cyan)
                        } else {
                            Style::default().fg(Color::White)
                        },
                    ),
                ]);

                if is_selected {
                    ListItem::new(line).style(Style::default().bg(Color::Cyan))
                } else {
                    ListItem::new(line)
                }
            })
            .collect();

        let list = List::new(items).block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Cyan))
                .title(Span::styled(
                    " Commands [Tab / ↑↓ to select] ",
                    Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD),
                )),
        );

        frame.render_widget(list, popup_area);
    }
}

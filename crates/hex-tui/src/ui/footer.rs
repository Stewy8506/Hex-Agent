//! Footer bar widgets and telemetry rendering.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};

use crate::app::App;
use crate::ui::style::HexTheme;

pub fn render_session_footer(app: &App, theme: &HexTheme, area: Rect, buf: &mut Buffer) {
    let mut footer_left = vec![
        Span::styled("build", Style::default().fg(theme.text_bright)),
        Span::styled(" · ", Style::default().fg(theme.text_extra_dim)),
        Span::styled(&app.model_name, Style::default().fg(theme.text_dim)),
        Span::styled(" · ", Style::default().fg(theme.text_extra_dim)),
        Span::styled(
            format!("{:.1}k tokens", app.tokens_used as f64 / 1000.0),
            Style::default().fg(theme.text_dim),
        ),
    ];

    let footer_right = "esc interrupt   ·   ctrl+p commands   ·   tab agents";
    let left_len = 5 + 3 + app.model_name.len() + 3 + 12;
    let spaces = (area.width as usize).saturating_sub(left_len + footer_right.len());
    footer_left.push(Span::raw(" ".repeat(spaces)));
    footer_left.push(Span::styled(footer_right, Style::default().fg(theme.text_dim)));

    buf.set_line(area.x, area.y, &Line::from(footer_left), area.width);
}

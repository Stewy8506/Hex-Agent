//! Screen 4: Diagnostic Test Failure and Error Card View matching the HEX Agent HTML mockup.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Widget};

use crate::ui::style::HexTheme;

pub use crate::app::DiagnosticFailure;

pub fn render_diagnostic_card(
    diag: &DiagnosticFailure,
    theme: &HexTheme,
    area: Rect,
    buf: &mut Buffer,
) -> u16 {
    if area.height < 8 || area.width < 40 {
        return 0;
    }

    let mut y = area.y;

    // 1. Command Run Header line with red bash badge
    let bash_header = Line::from(vec![
        Span::styled("bash   ", Style::default().fg(theme.rose_fg)),
        Span::styled(&diag.command, Style::default().fg(theme.text_bright)),
        Span::styled(
            format!(" · exit {} · {}", diag.exit_code, diag.duration_str),
            Style::default().fg(theme.text_extra_dim),
        ),
    ]);
    buf.set_line(area.x, y, &bash_header, area.width);
    y += 1;

    // 2. Error Diagnostic Card (border #2a2a2a, bg #0b0b0b)
    let card_height = 9_u16.min(area.bottom().saturating_sub(y));
    let card_rect = Rect::new(area.x, y, area.width, card_height);

    let card_block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme.border_mid))
        .style(Style::default().bg(theme.card_bg));
    let inner = card_block.inner(card_rect);
    card_block.render(card_rect, buf);

    let mut card_y = inner.y;

    // "fail cli.snapshot.test.ts"
    if card_y < inner.bottom() {
        let fail_line = Line::from(vec![
            Span::styled("fail  ", Style::default().fg(theme.rose_fg)),
            Span::styled(&diag.test_file, Style::default().fg(theme.text_bright)),
        ]);
        buf.set_line(inner.x, card_y, &fail_line, inner.width);
        card_y += 1;
    }

    // "prints edges in stable order"
    if card_y < inner.bottom() {
        let name_line = Line::from(vec![Span::styled(&diag.test_name, Style::default().fg(theme.text_dim))]);
        buf.set_line(inner.x, card_y, &name_line, inner.width);
        card_y += 1;
    }

    // Assertion Diffs:
    // "  expected  [...]" (sage)
    if card_y < inner.bottom() {
        let exp_line = Line::from(vec![
            Span::raw("  expected  "),
            Span::styled(&diag.expected, Style::default().fg(theme.sage_fg)),
        ]);
        buf.set_line(inner.x, card_y, &exp_line, inner.width);
        card_y += 1;
    }

    // "  received  [...]" (rose)
    if card_y < inner.bottom() {
        let rec_line = Line::from(vec![
            Span::raw("  received  "),
            Span::styled(&diag.received, Style::default().fg(theme.rose_fg)),
        ]);
        buf.set_line(inner.x, card_y, &rec_line, inner.width);
        card_y += 1;
    }

    // Divider
    if card_y < inner.bottom() {
        for x in inner.left()..inner.right() {
            buf[(x, card_y)].set_symbol("─");
            buf[(x, card_y)].set_fg(theme.border_low);
        }
        card_y += 1;
    }

    // Stack Trace & hidden frames
    if card_y < inner.bottom() && !diag.stack_trace.is_empty() {
        let frame_line = Line::from(vec![Span::styled(&diag.stack_trace[0], Style::default().fg(theme.text_extra_dim))]);
        buf.set_line(inner.x, card_y, &frame_line, inner.width);
        card_y += 1;
    }

    if card_y < inner.bottom() && diag.hidden_frames_count > 0 {
        let hidden_line = Line::from(vec![Span::styled(
            format!("... {} frames hidden", diag.hidden_frames_count),
            Style::default().fg(theme.text_extra_dim),
        )]);
        buf.set_line(inner.x, card_y, &hidden_line, inner.width);
    }

    y += card_height;

    // 3. Agent Commentary & Diagnosis
    if y < area.bottom() && !diag.diagnosis.is_empty() {
        let diag_line = Line::from(vec![Span::styled(&diag.diagnosis, Style::default().fg(theme.text_muted))]);
        buf.set_line(area.x, y, &diag_line, area.width);
        y += 1;
    }

    y - area.y
}

//! Screen 4: Diagnostic Test Failure and Error Card View matching the HEX Agent HTML mockup.
//!
//! The card is transcript content, not an overlay: `diagnostic_lines` feeds the
//! session stream so the failure scrolls with the tool call that produced it.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};

use crate::ui::style::HexTheme;

pub use crate::app::DiagnosticFailure;

/// Left indent of the card relative to the transcript (mockup `margin-left:2ch`).
const CARD_INDENT: usize = 2;

/// Clamp to a cell budget so the wrapping paragraph never reflows the box.
fn fit(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_string();
    }
    if max == 0 {
        return String::new();
    }
    let head: String = text.chars().take(max - 1).collect();
    format!("{head}…")
}

/// The failing command's header row: rose verb, command, exit code and duration.
pub fn diagnostic_header(diag: &DiagnosticFailure, theme: &HexTheme) -> Line<'static> {
    Line::from(vec![
        Span::styled("bash   ", Style::default().fg(theme.rose_fg)),
        Span::styled(diag.command.clone(), Style::default().fg(theme.text_bright)),
        Span::styled(
            format!(" · exit {} · {}", diag.exit_code, diag.duration_str),
            Style::default().fg(theme.text_extra_dim),
        ),
    ])
}

/// Card body as transcript lines: a rounded box holding the assertion or the
/// raw failure output, then the diagnosis. Every row is exactly `width` chars
/// so the wrapping paragraph never reflows the box.
pub fn diagnostic_lines(diag: &DiagnosticFailure, theme: &HexTheme, width: usize) -> Vec<Line<'static>> {
    let box_width = width.saturating_sub(CARD_INDENT);
    if box_width < 12 {
        return Vec::new();
    }
    let inner_width = box_width - 2;
    let pad = " ".repeat(CARD_INDENT);
    let border = Style::default().fg(theme.border_mid).bg(theme.card_bg);
    let surface = Style::default().bg(theme.card_bg);

    let mut body: Vec<Vec<Span<'static>>> = Vec::new();
    if !diag.test_file.is_empty() {
        body.push(vec![
            Span::styled("fail  ", Style::default().fg(theme.rose_fg).bg(theme.card_bg)),
            Span::styled(fit(&diag.test_file, inner_width - 6), Style::default().fg(theme.text_bright).bg(theme.card_bg)),
        ]);
    }
    if !diag.test_name.is_empty() {
        body.push(vec![Span::styled(fit(&diag.test_name, inner_width), Style::default().fg(theme.text_dim).bg(theme.card_bg))]);
    }
    if !diag.expected.is_empty() {
        body.push(vec![
            Span::styled("  expected  ", Style::default().fg(theme.text_dim).bg(theme.card_bg)),
            Span::styled(fit(&diag.expected, inner_width - 12), Style::default().fg(theme.sage_fg).bg(theme.card_bg)),
        ]);
    }
    if !diag.received.is_empty() {
        body.push(vec![
            Span::styled("  received  ", Style::default().fg(theme.text_dim).bg(theme.card_bg)),
            Span::styled(fit(&diag.received, inner_width - 12), Style::default().fg(theme.rose_fg).bg(theme.card_bg)),
        ]);
    }
    if diag.expected.is_empty() && diag.received.is_empty() {
        for detail in diag.detail_lines.iter().take(4) {
            body.push(vec![Span::styled(fit(detail, inner_width), Style::default().fg(theme.text_muted).bg(theme.card_bg))]);
        }
    }
    if !diag.stack_trace.is_empty() || diag.hidden_frames_count > 0 {
        body.push(vec![Span::styled(
            "─".repeat(inner_width),
            Style::default().fg(theme.border_low).bg(theme.card_bg),
        )]);
    }
    if let Some(frame) = diag.stack_trace.first() {
        body.push(vec![Span::styled(fit(frame, inner_width), Style::default().fg(theme.text_extra_dim).bg(theme.card_bg))]);
    }
    if diag.hidden_frames_count > 0 {
        body.push(vec![Span::styled(
            format!("... {} frames hidden", diag.hidden_frames_count),
            Style::default().fg(theme.text_extra_dim).bg(theme.card_bg),
        )]);
    }
    if body.is_empty() {
        body.push(vec![Span::styled("no failure detail reported", Style::default().fg(theme.text_extra_dim).bg(theme.card_bg))]);
    }

    let mut out = vec![Line::from(vec![
        Span::styled(pad.clone(), surface),
        Span::styled("╭", border),
        Span::styled("─".repeat(inner_width), border),
        Span::styled("╮", border),
    ])];
    for spans in body {
        let used: usize = spans.iter().map(|s| s.content.chars().count()).sum();
        let mut full = vec![Span::styled(pad.clone(), surface), Span::styled("│", border)];
        full.extend(spans);
        if used < inner_width {
            full.push(Span::styled(" ".repeat(inner_width - used), surface));
        }
        full.push(Span::styled("│", border));
        out.push(Line::from(full));
    }
    out.push(Line::from(vec![
        Span::styled(pad, surface),
        Span::styled("╰", border),
        Span::styled("─".repeat(inner_width), border),
        Span::styled("╯", border),
    ]));
    if !diag.diagnosis.is_empty() {
        let text = fit(&diag.diagnosis, width.saturating_sub(CARD_INDENT));
        let used = CARD_INDENT + text.chars().count();
        let mut spans = vec![
            Span::raw("  "),
            Span::styled(text, Style::default().fg(theme.text_muted)),
        ];
        if used < width {
            spans.push(Span::raw(" ".repeat(width - used)));
        }
        out.push(Line::from(spans));
    }
    out
}

/// Paint the header plus the card into a fixed rect (used by the error screen
/// and by tests that inspect the card on its own). Returns the rows consumed.
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
    buf.set_line(area.x, y, &diagnostic_header(diag, theme), area.width);
    y += 1;

    for line in diagnostic_lines(diag, theme, area.width as usize) {
        if y >= area.bottom() {
            break;
        }
        buf.set_line(area.x, y, &line, area.width);
        y += 1;
    }

    y - area.y
}
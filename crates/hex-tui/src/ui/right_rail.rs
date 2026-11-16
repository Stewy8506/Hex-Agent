//! Right Rail panel for the HEX Agent Cockpit matching the HTML mockup.
//!
//! Contains:
//! 1. Context window usage track: `▕██████░░░░░░░░░░▏` + `38% · 76k / 200k`
//! 2. GSD Plan step checklist with pulsing active `[>]` marker
//! 3. Changed files list with `+N` (sage) and `−M` (rose) diff counters
//! 4. Active multi-agent / subagent execution status with live Braille spinners

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};

use crate::app::{App, StepStatus};
use crate::ui::animation::AnimationState;
use crate::ui::style::HexTheme;

pub fn format_tokens(n: usize) -> String {
    if n >= 1_000_000 {
        let m = n as f64 / 1_000_000.0;
        if n % 1_000_000 == 0 {
            format!("{:.0}M", m)
        } else {
            format!("{:.1}M", m)
        }
    } else {
        format!("{}k", n / 1000)
    }
}

pub fn render(app: &App, theme: &HexTheme, anim: &AnimationState, area: Rect, buf: &mut Buffer) {
    if area.width < 12 || area.height < 10 {
        return;
    }

    // Fill background with window surface color (#0e0e0e)
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            buf[(x, y)].set_bg(theme.window_bg);
        }
    }

    let mut current_y = area.y + 1;

    // ─────────────────────────────────────────────
    // 1. Context Window Usage
    // ─────────────────────────────────────────────
    if current_y < area.bottom() {
        let label = Line::from(vec![Span::styled("context", theme.apfel_header_style())]);
        buf.set_line(area.x, current_y, &label, area.width);
        current_y += 1;
    }

    if current_y < area.bottom() {
        let pct = if app.max_tokens > 0 {
            (app.tokens_used as f64 / app.max_tokens as f64).clamp(0.0, 1.0)
        } else {
            0.0
        };

        // 16 slots between the caps
        let total_slots = 16_usize;
        let filled_slots = ((pct * total_slots as f64).round() as usize).min(total_slots);
        let unfilled_slots = total_slots.saturating_sub(filled_slots);

        let bar_str = format!("▕{}{}{}▏", "█".repeat(filled_slots), "░".repeat(unfilled_slots), "");
        let bar_line = Line::from(vec![Span::styled(bar_str, Style::default().fg(theme.text_bright))]);
        buf.set_line(area.x, current_y, &bar_line, area.width);
        current_y += 1;
    }

    if current_y < area.bottom() {
        let pct_int = if app.max_tokens > 0 {
            (app.tokens_used * 100) / app.max_tokens
        } else {
            0
        };
        let stats_line = Line::from(vec![
            Span::styled(format!("{}%", pct_int), Style::default().fg(theme.text_dim)),
            Span::styled("  ·  ", Style::default().fg(theme.text_extra_dim)),
            Span::styled(
                format!("{} / {}", format_tokens(app.tokens_used), format_tokens(app.max_tokens)),
                Style::default().fg(theme.text_dim),
            ),
        ]);
        buf.set_line(area.x, current_y, &stats_line, area.width);
        current_y += 2;
    }

    // Divider
    draw_divider(area, current_y, theme, buf);
    current_y += 2;

    // ─────────────────────────────────────────────
    // 2. GSD Plan Steps
    // ─────────────────────────────────────────────
    if current_y < area.bottom() {
        let label = Line::from(vec![Span::styled("plan", theme.apfel_header_style())]);
        buf.set_line(area.x, current_y, &label, area.width);
        current_y += 1;
    }

    if app.plan_steps.is_empty() {
        if current_y < area.bottom() {
            let empty_line = Line::from(vec![Span::styled("no active plan", Style::default().fg(theme.text_extra_dim))]);
            buf.set_line(area.x, current_y, &empty_line, area.width);
            current_y += 1;
        }
    } else {
        for step in &app.plan_steps {
            if current_y >= area.bottom() - 8 {
                break;
            }

            let line = match step.status {
                StepStatus::Completed => Line::from(vec![
                    Span::styled("[x] ", Style::default().fg(theme.text_extra_dim)),
                    Span::styled(&step.title, Style::default().fg(theme.text_dim)),
                ]),
                StepStatus::Active => {
                    let pulse_col = anim.pulse_color(theme.text_extra_dim, theme.accent_blue);
                    Line::from(vec![
                        Span::styled("[", Style::default().fg(theme.accent_blue)),
                        Span::styled(">", Style::default().fg(pulse_col)),
                        Span::styled("] ", Style::default().fg(theme.accent_blue)),
                        Span::styled(&step.title, Style::default().fg(theme.text_bright)),
                    ])
                }
                StepStatus::Pending => Line::from(vec![
                    Span::styled("[ ] ", Style::default().fg(theme.text_extra_dim)),
                    Span::styled(&step.title, Style::default().fg(theme.text_extra_dim)),
                ]),
            };

            buf.set_line(area.x, current_y, &line, area.width);
            current_y += 1;
        }
    }

    current_y += 1;
    // Divider
    draw_divider(area, current_y, theme, buf);
    current_y += 2;

    // ─────────────────────────────────────────────
    // 3. Changed Files
    // ─────────────────────────────────────────────
    if current_y < area.bottom() {
        let label = Line::from(vec![Span::styled("changed", theme.apfel_header_style())]);
        buf.set_line(area.x, current_y, &label, area.width);
        current_y += 1;
    }

    if app.changed_files.is_empty() {
        if current_y < area.bottom() {
            let empty_line = Line::from(vec![Span::styled("working tree clean", Style::default().fg(theme.text_extra_dim))]);
            buf.set_line(area.x, current_y, &empty_line, area.width);
            current_y += 1;
        }
    } else {
        for file in &app.changed_files {
            if current_y >= area.bottom() - 6 {
                break;
            }

            let name_color = if file.additions > 0 || file.deletions > 0 {
                theme.text_bright
            } else {
                theme.text_dim
            };

            let mut spans = vec![Span::styled(&file.path, Style::default().fg(name_color))];

            let right_str = if file.additions == 0 && file.deletions == 0 {
                "—".to_string()
            } else {
                format!("+{} −{}", file.additions, file.deletions)
            };

            let spaces = (area.width as usize).saturating_sub(file.path.len() + right_str.len());
            spans.push(Span::raw(" ".repeat(spaces)));

            if file.additions == 0 && file.deletions == 0 {
                spans.push(Span::styled("—", Style::default().fg(theme.text_extra_dim)));
            } else {
                spans.push(Span::styled(format!("+{}", file.additions), Style::default().fg(theme.sage_fg)));
                spans.push(Span::raw(" "));
                spans.push(Span::styled(format!("−{}", file.deletions), Style::default().fg(theme.rose_fg)));
            }

            buf.set_line(area.x, current_y, &Line::from(spans), area.width);
            current_y += 1;
        }
    }

    current_y += 1;
    // Divider
    draw_divider(area, current_y, theme, buf);
    current_y += 2;

    // ─────────────────────────────────────────────
    // 4. Live Agents / Subagents
    // ─────────────────────────────────────────────
    if current_y < area.bottom() {
        let label = Line::from(vec![Span::styled("agents", theme.apfel_header_style())]);
        buf.set_line(area.x, current_y, &label, area.width);
        current_y += 1;
    }

    if app.active_approval.is_some() && current_y < area.bottom() {
        let blocked = Line::from(vec![Span::styled(
            "blocked / waiting on you",
            Style::default().fg(theme.accent_blue),
        )]);
        buf.set_line(area.x, current_y, &blocked, area.width);
        current_y += 1;
    }

    if app.subagents.is_empty() {
        if current_y < area.bottom() {
            let empty_line = Line::from(vec![Span::styled("none active", Style::default().fg(theme.text_extra_dim))]);
            buf.set_line(area.x, current_y, &empty_line, area.width);
        }
    } else {
        for agent in &app.subagents {
            if current_y >= area.bottom() - 1 {
                break;
            }

            let mut top_spans = vec![Span::styled(&agent.name, Style::default().fg(theme.text_bright))];

            let status_len = if agent.is_running {
                1
            } else {
                agent.status.len()
            };
            let spaces = (area.width as usize).saturating_sub(agent.name.len() + status_len);
            top_spans.push(Span::raw(" ".repeat(spaces)));

            if agent.is_running {
                top_spans.push(Span::styled(anim.spinner_frame(), Style::default().fg(theme.accent_blue)));
            } else if agent.has_failed || agent.status == "fail" {
                top_spans.push(Span::styled(&agent.status, Style::default().fg(theme.rose_fg)));
            } else if agent.status == "pass" {
                top_spans.push(Span::styled(&agent.status, Style::default().fg(theme.sage_fg)));
            } else {
                top_spans.push(Span::styled(&agent.status, Style::default().fg(theme.text_extra_dim)));
            }

            buf.set_line(area.x, current_y, &Line::from(top_spans), area.width);
            current_y += 1;

            if current_y < area.bottom() && !agent.detail.is_empty() {
                let detail_line = Line::from(vec![Span::styled(&agent.detail, Style::default().fg(theme.text_dim))]);
                buf.set_line(area.x, current_y, &detail_line, area.width);
                current_y += 1;
            }
        }
    }
}

fn draw_divider(area: Rect, y: u16, theme: &HexTheme, buf: &mut Buffer) {
    if y < area.bottom() {
        for x in area.left()..area.right() {
            buf[(x, y)].set_symbol("─");
            buf[(x, y)].set_fg(theme.border_low);
        }
    }
}

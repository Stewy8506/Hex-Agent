//! Compact, independently scrollable sidebar and its customization overlay.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Widget};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::app::{App, ChangedFile, StepStatus};
use crate::sidebar::{Section, SidebarAction};
use crate::ui::animation::AnimationState;
use crate::ui::style::HexTheme;

const SIDE_PAD: u16 = 2;
const CHANGED_PREVIEW: usize = 6;

struct Row {
    line: Line<'static>,
    action: Option<SidebarAction>,
}

impl Row {
    fn text(line: impl Into<Line<'static>>) -> Self {
        Self {
            line: line.into(),
            action: None,
        }
    }

    fn action(line: Line<'static>, action: SidebarAction) -> Self {
        Self {
            line,
            action: Some(action),
        }
    }
}

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
    app.sidebar.area.set(area);
    app.sidebar.hit_targets.borrow_mut().clear();

    let area = area.intersection(buf.area);
    fill(area, theme.window_bg, theme.text_primary, buf);
    let width = area.width.saturating_sub(SIDE_PAD * 2) as usize;
    if width == 0 {
        app.sidebar.metrics.set((0, 0));
        app.sidebar.scroll.set(0);
        return;
    }

    // Build complete visual content before applying the independent viewport.
    let rows = content_rows(app, theme, anim, width);
    let slots = area.height.saturating_sub(1) as usize;
    let overflowing = rows.len() > slots;
    let footer = overflowing && slots > 0;
    let viewport = slots.saturating_sub(usize::from(footer));
    let scroll = if viewport == 0 {
        0
    } else {
        app.sidebar
            .scroll
            .get()
            .min(rows.len().saturating_sub(viewport))
    };
    app.sidebar.metrics.set((rows.len(), viewport));
    app.sidebar.scroll.set(scroll);

    if area.height == 0 {
        return;
    }
    let x = area.x + SIDE_PAD;
    render_toolbar(app, theme, x, area.y, width, buf);
    for (offset, row) in rows.iter().skip(scroll).take(viewport).enumerate() {
        let y = area.y + 1 + offset as u16;
        paint_line(x, y, width, &row.line, buf);
        if let Some(action) = row.action {
            let hit_width = row.line.width().min(width) as u16;
            if hit_width > 0 {
                app.sidebar
                    .hit_targets
                    .borrow_mut()
                    .push((Rect::new(x, y, hit_width, 1), action));
            }
        }
    }

    if footer {
        let start = if viewport == 0 { 0 } else { scroll + 1 };
        let end = (scroll + viewport).min(rows.len());
        let line = Line::from(vec![
            span(
                "↑",
                if scroll > 0 {
                    theme.accent_blue
                } else {
                    theme.text_extra_dim
                },
            ),
            span(format!(" {start}–{end}/{} ", rows.len()), theme.text_dim),
            span(
                "↓",
                if end < rows.len() {
                    theme.accent_blue
                } else {
                    theme.text_extra_dim
                },
            ),
        ]);
        paint_line(x, area.bottom() - 1, width, &line, buf);
    }
}

fn render_toolbar(app: &App, theme: &HexTheme, x: u16, y: u16, width: usize, buf: &mut Buffer) {
    if width < 4 {
        paint_line(
            x,
            y,
            width,
            &Line::from(span("sidebar", theme.text_dim)),
            buf,
        );
        return;
    }
    let label_width = width.saturating_sub(5);
    paint_line(
        x,
        y,
        label_width,
        &Line::from(span("sidebar", theme.text_dim)),
        buf,
    );
    let action_x = x + (width - 4) as u16;
    paint_line(
        action_x,
        y,
        4,
        &Line::from(span("[f4]", theme.accent_blue)),
        buf,
    );
    app.sidebar
        .hit_targets
        .borrow_mut()
        .push((Rect::new(action_x, y, 4, 1), SidebarAction::Settings));
}

fn content_rows(app: &App, theme: &HexTheme, anim: &AnimationState, width: usize) -> Vec<Row> {
    let mut rows = Vec::new();
    for preference in app
        .sidebar
        .preferences
        .sections
        .iter()
        .filter(|p| p.visible)
    {
        if !rows.is_empty() {
            rows.push(Row::text(""));
        }
        let count = match preference.section {
            Section::Context => String::new(),
            Section::Plan => format!(
                "{}/{}",
                app.plan_steps
                    .iter()
                    .filter(|s| s.status == StepStatus::Completed)
                    .count(),
                app.plan_steps.len()
            ),
            Section::Changed => app.changed_files.len().to_string(),
            Section::Agents => app.subagents.len().to_string(),
        };
        let mut header = vec![
            span(
                if preference.collapsed { "▸ " } else { "▾ " },
                theme.accent_blue,
            ),
            span(preference.section.label(), theme.text_dim),
        ];
        if !count.is_empty() {
            header.push(span(format!(" {count}"), theme.accent_blue));
        }
        rows.push(Row::action(
            Line::from(header),
            SidebarAction::ToggleSection(preference.section),
        ));
        if preference.collapsed {
            continue;
        }
        match preference.section {
            Section::Context => context_rows(app, theme, width, &mut rows),
            Section::Plan => plan_rows(app, theme, width, &mut rows),
            Section::Changed => changed_rows(app, theme, width, &mut rows),
            Section::Agents => agent_rows(app, theme, anim, width, &mut rows),
        }
    }
    rows
}

fn context_rows(app: &App, theme: &HexTheme, width: usize, rows: &mut Vec<Row>) {
    let progress = if app.max_tokens == 0 {
        0.0
    } else {
        (app.tokens_used as f64 / app.max_tokens as f64).clamp(0.0, 1.0)
    };
    let filled = ((progress * width as f64).round() as usize).min(width);
    rows.push(Row::text(Line::from(vec![
        span("█".repeat(filled), theme.accent_blue),
        span("░".repeat(width - filled), theme.text_extra_dim),
    ])));
    rows.push(Row::text(Line::from(vec![
        span(
            format!("{}%", (progress * 100.0).floor() as usize),
            theme.accent_blue,
        ),
        span(
            format!(
                " · {} / {}",
                format_tokens(app.tokens_used),
                format_tokens(app.max_tokens)
            ),
            theme.text_dim,
        ),
    ])));
}

fn plan_rows(app: &App, theme: &HexTheme, width: usize, rows: &mut Vec<Row>) {
    if app.plan_steps.is_empty() {
        rows.push(Row::text(span("no active plan", theme.text_extra_dim)));
        return;
    }
    for step in &app.plan_steps {
        let (marker, marker_color, title_color) = match step.status {
            StepStatus::Completed => ("[x] ", theme.text_extra_dim, theme.text_dim),
            StepStatus::Active => ("[>] ", theme.accent_blue, theme.text_bright),
            StepStatus::Pending => ("[ ] ", theme.text_extra_dim, theme.text_extra_dim),
        };
        // On very narrow rails, put the marker on its own row rather than lose the title.
        if width <= 4 {
            rows.push(Row::text(span(prefix(marker, width), marker_color)));
            push_wrapped(rows, &step.title, width, title_color);
            continue;
        }
        for (index, title) in wrap_text(&step.title, width - 4).into_iter().enumerate() {
            rows.push(Row::text(Line::from(vec![
                span(if index == 0 { marker } else { "    " }, marker_color),
                span(title, title_color),
            ])));
        }
    }
}

fn changed_rows(app: &App, theme: &HexTheme, width: usize, rows: &mut Vec<Row>) {
    if app.changed_files.is_empty() {
        rows.push(Row::text(span("working tree clean", theme.text_extra_dim)));
        return;
    }
    let shown = if app.sidebar.show_all_changed {
        app.changed_files.len()
    } else {
        app.changed_files.len().min(CHANGED_PREVIEW)
    };
    let additions_width = app
        .changed_files
        .iter()
        .map(|file| file.additions.to_string().len())
        .max()
        .unwrap_or(1);
    let deletions_width = app
        .changed_files
        .iter()
        .map(|file| file.deletions.to_string().len())
        .max()
        .unwrap_or(1);
    let digits = width.saturating_sub(5);
    let columns = if additions_width + deletions_width <= digits {
        (additions_width, deletions_width)
    } else {
        let additions = additions_width.min(digits.div_ceil(2));
        (
            additions,
            deletions_width.min(digits.saturating_sub(additions)),
        )
    };
    let columns = (
        app.changed_files
            .iter()
            .map(|file| UnicodeWidthStr::width(compact_number(file.additions, columns.0).as_str()))
            .max()
            .unwrap_or(0),
        app.changed_files
            .iter()
            .map(|file| UnicodeWidthStr::width(compact_number(file.deletions, columns.1).as_str()))
            .max()
            .unwrap_or(0),
    );
    for file in app.changed_files.iter().take(shown) {
        rows.push(Row::text(changed_line(file, theme, width, columns)));
    }
    if app.changed_files.len() > CHANGED_PREVIEW {
        let label = if app.sidebar.show_all_changed {
            "− show less".to_string()
        } else {
            format!("+ {} more", app.changed_files.len() - shown)
        };
        rows.push(Row::action(
            Line::from(span(label, theme.accent_blue)),
            SidebarAction::ToggleChanges,
        ));
    }
}

fn changed_line(
    file: &ChangedFile,
    theme: &HexTheme,
    width: usize,
    columns: (usize, usize),
) -> Line<'static> {
    let counters = if file.additions == 0 && file.deletions == 0 {
        vec![span("—", theme.text_extra_dim)]
    } else {
        let additions = compact_number(file.additions, columns.0);
        let deletions = compact_number(file.deletions, columns.1);
        vec![
            span(
                format!(
                    "+{}{additions}",
                    " ".repeat(
                        columns
                            .0
                            .saturating_sub(UnicodeWidthStr::width(additions.as_str()))
                    )
                ),
                theme.sage_fg,
            ),
            span(" ", theme.text_extra_dim),
            span(
                format!(
                    "−{}{deletions}",
                    " ".repeat(
                        columns
                            .1
                            .saturating_sub(UnicodeWidthStr::width(deletions.as_str()))
                    )
                ),
                theme.rose_fg,
            ),
        ]
    };
    let counter_width = counters.iter().map(Span::width).sum::<usize>();
    if counter_width + 2 > width {
        return fit_line(&Line::from(counters), width);
    }
    // Reserve the right column and a real gap before measuring/truncating the path.
    let path = middle_path(&file.path, width - counter_width - 1);
    let gap = width - counter_width - UnicodeWidthStr::width(path.as_str());
    let name_color = if file.additions > 0 || file.deletions > 0 {
        theme.text_bright
    } else {
        theme.text_dim
    };
    let mut spans = vec![
        span(path, name_color),
        span(" ".repeat(gap), theme.text_extra_dim),
    ];
    spans.extend(counters);
    Line::from(spans)
}

fn compact_number(n: usize, width: usize) -> String {
    let full = n.to_string();
    if full.len() <= width {
        return full;
    }
    let mut value = n;
    let mut unit = 0;
    let units = ["", "k", "M", "G", "T", "P", "E"];
    while value >= 1000 && unit + 1 < units.len() {
        value /= 1000;
        unit += 1;
    }
    let compact = format!("{value}{}", units[unit]);
    if compact.len() <= width {
        compact
    } else {
        prefix("…", width)
    }
}

fn agent_rows(
    app: &App,
    theme: &HexTheme,
    anim: &AnimationState,
    width: usize,
    rows: &mut Vec<Row>,
) {
    if app.active_approval.is_some() {
        push_wrapped(rows, "blocked / waiting on you", width, theme.accent_blue);
    }
    if app.subagents.is_empty() {
        rows.push(Row::text(span("none active", theme.text_extra_dim)));
        return;
    }
    for agent in &app.subagents {
        let status = if agent.is_running {
            anim.spinner_frame().to_string()
        } else {
            agent.status.clone()
        };
        let status_color = if agent.is_running {
            theme.accent_blue
        } else if agent.has_failed || agent.status == "fail" {
            theme.rose_fg
        } else if agent.status == "pass" {
            theme.sage_fg
        } else {
            theme.text_extra_dim
        };
        if status.is_empty() {
            push_wrapped(rows, &agent.name, width, theme.text_bright);
        } else if width < 3 {
            push_wrapped(rows, &agent.name, width, theme.text_bright);
            push_wrapped(rows, &status, width, status_color);
        } else {
            // A bounded status column leaves room for names, even with verbose statuses.
            let status_width = UnicodeWidthStr::width(single_line(&status).as_str())
                .max(1)
                .min((width / 3).max(1));
            let name_width = width - status_width - 1;
            let names = wrap_text(&agent.name, name_width);
            let statuses = wrap_text(&status, status_width);
            for index in 0..names.len().max(statuses.len()) {
                let name = names.get(index).map(String::as_str).unwrap_or("");
                let status = statuses.get(index).map(String::as_str).unwrap_or("");
                let gap = width - UnicodeWidthStr::width(name) - UnicodeWidthStr::width(status);
                rows.push(Row::text(Line::from(vec![
                    span(name.to_string(), theme.text_bright),
                    span(" ".repeat(gap), theme.text_extra_dim),
                    span(status.to_string(), status_color),
                ])));
            }
        }
        if !agent.detail.is_empty() {
            push_wrapped(rows, &agent.detail, width, theme.text_dim);
        }
    }
}

fn push_wrapped(rows: &mut Vec<Row>, text: &str, width: usize, color: Color) {
    rows.extend(
        wrap_text(text, width)
            .into_iter()
            .map(|text| Row::text(span(text, color))),
    );
}

fn span(text: impl Into<String>, color: Color) -> Span<'static> {
    Span::styled(text.into(), Style::default().fg(color))
}

fn single_line(text: &str) -> String {
    text.chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect()
}

// Include zero-width combining characters with their preceding terminal cell.
fn prefix(text: &str, width: usize) -> String {
    let mut used = 0;
    let mut end = 0;
    for (index, c) in text.char_indices() {
        let columns = c.width().unwrap_or(0);
        if used + columns > width {
            break;
        }
        used += columns;
        end = index + c.len_utf8();
    }
    if width == 0 {
        String::new()
    } else {
        text[..end].to_string()
    }
}

fn suffix(text: &str, width: usize) -> String {
    let mut used = 0;
    let mut start = text.len();
    for (index, c) in text.char_indices().rev() {
        let columns = c.width().unwrap_or(0);
        if used + columns > width {
            break;
        }
        used += columns;
        start = index;
    }
    let suffix = &text[start..];
    suffix
        .trim_start_matches(|c: char| c.width().unwrap_or(0) == 0)
        .to_string()
}

fn middle_path(path: &str, width: usize) -> String {
    let path = single_line(path);
    if UnicodeWidthStr::width(path.as_str()) <= width {
        return path;
    }
    if width <= 1 {
        return prefix("…", width);
    }
    let available = width - 1;
    let basename = path.rsplit(['/', '\\']).next().unwrap_or(&path);
    let basename_width = UnicodeWidthStr::width(basename);
    let tail_width = if basename_width <= available {
        (basename_width + usize::from(basename.len() < path.len())).min(available)
    } else {
        available.div_ceil(2)
    };
    let tail = suffix(&path, tail_width);
    let front = prefix(&path, available - UnicodeWidthStr::width(tail.as_str()));
    format!("{front}…{tail}")
}

fn wrap_text(text: &str, width: usize) -> Vec<String> {
    if width == 0 {
        return Vec::new();
    }
    let mut rows = Vec::new();
    for paragraph in text.split('\n') {
        let paragraph = single_line(paragraph);
        let mut remaining = paragraph.as_str();
        if remaining.is_empty() {
            rows.push(String::new());
        }
        while !remaining.is_empty() {
            if UnicodeWidthStr::width(remaining) <= width {
                rows.push(remaining.to_string());
                break;
            }
            let chunk = prefix(remaining, width);
            if chunk.is_empty() {
                // A double-width glyph cannot occupy a single-column viewport.
                let consumed = remaining.chars().next().unwrap().len_utf8();
                rows.push("�".to_string());
                remaining =
                    remaining[consumed..].trim_start_matches(|c: char| c.width().unwrap_or(0) == 0);
                continue;
            }
            let break_at = chunk
                .char_indices()
                .rev()
                .find(|(index, c)| *index > 0 && c.is_whitespace())
                .map(|(index, _)| index);
            let consumed = break_at.unwrap_or(chunk.len());
            rows.push(remaining[..consumed].trim_end().to_string());
            remaining = remaining[consumed..].trim_start();
        }
    }
    rows
}

fn fit_line(line: &Line<'_>, width: usize) -> Line<'static> {
    let mut left = width;
    let mut spans = Vec::new();
    for source in &line.spans {
        if left == 0 {
            break;
        }
        let content = prefix(&single_line(&source.content), left);
        let used = UnicodeWidthStr::width(content.as_str());
        spans.push(Span::styled(content, source.style));
        left = left.saturating_sub(used);
        if used < UnicodeWidthStr::width(source.content.as_ref()) {
            break;
        }
    }
    Line::from(spans).style(line.style)
}

fn paint_line(x: u16, y: u16, width: usize, line: &Line<'_>, buf: &mut Buffer) {
    if width > 0 {
        buf.set_line(x, y, &fit_line(line, width), width as u16);
    }
}

fn fill(area: Rect, background: Color, foreground: Color, buf: &mut Buffer) {
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            buf[(x, y)].reset();
            buf[(x, y)].set_style(Style::default().bg(background).fg(foreground));
        }
    }
}

/// Lowest modal overlay; the caller paints palette, approval and help above it.
pub fn render_settings(app: &App, theme: &HexTheme, area: Rect, buf: &mut Buffer) {
    let area = area.intersection(buf.area);
    let panel_width = area.width.min(56);
    let width = panel_width.saturating_sub(SIDE_PAD * 2) as usize;
    if width == 0 || area.height < 3 {
        return;
    }
    let mut rows = vec![
        Row::text(
            Line::from(span("Customize sidebar", theme.text_bright))
                .style(Style::default().add_modifier(Modifier::BOLD)),
        ),
        Row::text(""),
    ];
    for (index, preference) in app.sidebar.preferences.sections.iter().enumerate() {
        let selected = index == app.sidebar.selected_section;
        let line = Line::from(vec![
            span(if selected { "› " } else { "  " }, theme.accent_blue),
            span(
                if preference.visible { "[x] " } else { "[ ] " },
                if preference.visible {
                    theme.accent_blue
                } else {
                    theme.text_extra_dim
                },
            ),
            span(preference.section.label(), theme.text_primary),
            span(
                if preference.collapsed {
                    "  ▸ collapsed"
                } else {
                    "  ▾ expanded"
                },
                theme.text_dim,
            ),
        ])
        .style(Style::default().bg(if selected {
            theme.selection_bg
        } else {
            theme.card_bg
        }));
        rows.push(Row::text(line));
    }
    rows.push(Row::text(""));
    let width_label = app
        .sidebar
        .preferences
        .width
        .map(|w| format!("{w} cols"))
        .unwrap_or_else(|| "auto".to_string());
    rows.push(Row::text(Line::from(vec![
        span("width: ", theme.text_dim),
        span(width_label, theme.accent_blue),
    ])));
    rows.push(Row::text(""));
    for instruction in [
        "Up/Down select · space show/hide",
        "c collapse · ,/. reorder",
        "+/- width · a auto-width · r reset",
        "Tab cycles operating mode",
        "Esc/F4 close",
    ] {
        push_wrapped(&mut rows, instruction, width, theme.text_dim);
    }
    let panel_height = (rows.len() + 2).min(area.height as usize) as u16;
    let panel = Rect::new(
        area.x + (area.width - panel_width) / 2,
        area.y + (area.height - panel_height) / 2,
        panel_width,
        panel_height,
    );
    fill(panel, theme.card_bg, theme.text_primary, buf);
    Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(theme.border_modal))
        .render(panel, buf);
    for (index, row) in rows
        .iter()
        .take(panel_height.saturating_sub(2) as usize)
        .enumerate()
    {
        let y = panel.y + 1 + index as u16;
        if let Some(background) = row.line.style.bg {
            for x in panel.x + SIDE_PAD..panel.right() - SIDE_PAD {
                buf[(x, y)].set_bg(background);
            }
        }
        paint_line(panel.x + SIDE_PAD, y, width, &row.line, buf);
    }
}

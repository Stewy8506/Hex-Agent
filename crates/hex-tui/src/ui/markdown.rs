//! Markdown → styled Ratatui lines for the transcript.
//!
//! Model output arrives as markdown; rendering it verbatim leaks `**`, fences
//! and bullet markers into the prose. This module owns the block and inline
//! grammar the cockpit actually displays, with a span-aware word wrapper so
//! nothing is clipped at the canvas edge. It is deliberately dependency-free:
//! the streaming case (an unterminated fence or emphasis marker) must degrade
//! to something readable rather than waiting for a complete document.

use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::ui::style::HexTheme;

/// Assistant prose sits two columns in from the canvas edge.
const PROSE_INDENT: usize = 2;

/// Render a markdown message into lines that fit `width` columns.
pub fn render_markdown(text: &str, width: usize, theme: &HexTheme) -> Vec<Line<'static>> {
    render_markdown_indented(text, width, theme, PROSE_INDENT)
}

pub(super) fn render_markdown_indented(
    text: &str,
    width: usize,
    theme: &HexTheme,
    prose_indent: usize,
) -> Vec<Line<'static>> {
    let width = width.max(8);
    let mut out: Vec<Line<'static>> = Vec::new();
    let mut paragraph: Vec<String> = Vec::new();
    let mut fence: Option<char> = None;

    for raw in text.lines() {
        let trimmed = raw.trim_start();
        let indent_cols = raw.len() - trimmed.len();

        if let Some(marker) = fence {
            if is_fence_close(trimmed, marker) {
                fence = None;
            } else {
                push_code_row(&mut out, raw, width, theme);
            }
            continue;
        }

        if let Some(marker) = fence_open(trimmed) {
            flush_paragraph(&mut paragraph, &mut out, width, theme, prose_indent);
            fence = Some(marker);
            continue;
        }

        if trimmed.is_empty() {
            flush_paragraph(&mut paragraph, &mut out, width, theme, prose_indent);
            push_blank(&mut out);
            continue;
        }

        if let Some(level) = heading_level(trimmed) {
            flush_paragraph(&mut paragraph, &mut out, width, theme, prose_indent);
            push_blank(&mut out);
            let body = trimmed[level + 1..].trim();
            let style = Style::default()
                .fg(theme.text_bright)
                .add_modifier(Modifier::BOLD);
            let spans = inline_spans(body, style, theme);
            for row in wrap_spans(spans, width, prose_indent, prose_indent) {
                let mut line = vec![Span::raw(" ".repeat(prose_indent))];
                line.extend(row);
                out.push(Line::from(line));
            }
            continue;
        }

        if is_rule(trimmed) {
            flush_paragraph(&mut paragraph, &mut out, width, theme, prose_indent);
            push_blank(&mut out);
            out.push(Line::from(Span::styled(
                "─".repeat(width),
                Style::default().fg(theme.border_low),
            )));
            push_blank(&mut out);
            continue;
        }

        if let Some(item) = list_item(trimmed) {
            flush_paragraph(&mut paragraph, &mut out, width, theme, prose_indent);
            let depth = (indent_cols / 2).min(4);
            let marker_indent = prose_indent + depth * 2;
            let text_indent = marker_indent + item.marker_width + 1;
            let marker_style = Style::default().fg(theme.accent_blue);
            let spans = inline_spans(&item.body, Style::default().fg(theme.text_muted), theme);
            let rows = wrap_spans(spans, width, text_indent, text_indent);
            for (i, row) in rows.into_iter().enumerate() {
                let mut line = vec![Span::raw(" ".repeat(if i == 0 {
                    marker_indent
                } else {
                    text_indent
                }))];
                if i == 0 {
                    line.push(Span::styled(item.marker.clone(), marker_style));
                    line.push(Span::raw(" "));
                }
                line.extend(row);
                out.push(Line::from(line));
            }
            continue;
        }

        if let Some(quoted) = trimmed.strip_prefix("> ") {
            flush_paragraph(&mut paragraph, &mut out, width, theme, prose_indent);
            let spans = inline_spans(quoted, Style::default().fg(theme.text_muted), theme);
            for row in wrap_spans(spans, width, prose_indent + 2, prose_indent + 2) {
                let mut line = vec![
                    Span::raw(" ".repeat(prose_indent)),
                    Span::styled("│ ", Style::default().fg(theme.accent_blue)),
                ];
                line.extend(row);
                out.push(Line::from(line));
            }
            continue;
        }

        paragraph.push(trimmed.to_string());
    }

    flush_paragraph(&mut paragraph, &mut out, width, theme, prose_indent);
    while out.last().map(|l| l.width() == 0).unwrap_or(false) {
        out.pop();
    }
    out
}

fn flush_paragraph(
    paragraph: &mut Vec<String>,
    out: &mut Vec<Line<'static>>,
    width: usize,
    theme: &HexTheme,
    prose_indent: usize,
) {
    if paragraph.is_empty() {
        return;
    }
    let joined = paragraph.join(" ");
    paragraph.clear();
    let spans = inline_spans(&joined, Style::default().fg(theme.text_muted), theme);
    for row in wrap_spans(spans, width, prose_indent, prose_indent) {
        let mut line = vec![Span::raw(" ".repeat(prose_indent))];
        line.extend(row);
        out.push(Line::from(line));
    }
}

fn push_blank(out: &mut Vec<Line<'static>>) {
    if !out.is_empty() && out.last().map(|l| l.width() != 0).unwrap_or(false) {
        out.push(Line::raw(""));
    }
}

fn push_code_row(out: &mut Vec<Line<'static>>, raw: &str, width: usize, theme: &HexTheme) {
    let tint = Style::default().fg(theme.text_primary).bg(theme.hover_bg);
    let mut chars: Vec<(char, Style)> = raw.chars().map(|c| (c, tint)).collect();
    if chars.is_empty() {
        chars.push((' ', tint));
    }
    for row in wrap_chars(&chars, width, 0, 0) {
        let mut spans = group_chars(&row);
        let used: usize = row.iter().map(|(c, _)| c.width().unwrap_or(0)).sum();
        if used < width {
            spans.push(Span::styled(" ".repeat(width - used), tint));
        }
        out.push(Line::from(spans));
    }
}

fn fence_open(trimmed: &str) -> Option<char> {
    for marker in ['`', '~'] {
        if trimmed.starts_with(&marker.to_string().repeat(3)) {
            return Some(marker);
        }
    }
    None
}

fn is_fence_close(trimmed: &str, marker: char) -> bool {
    let run = trimmed.chars().take_while(|c| *c == marker).count();
    run >= 3 && trimmed.chars().all(|c| c == marker)
}

fn heading_level(trimmed: &str) -> Option<usize> {
    let hashes = trimmed.chars().take_while(|c| *c == '#').count();
    if (1..=6).contains(&hashes) && trimmed[hashes..].starts_with(' ') {
        Some(hashes)
    } else {
        None
    }
}

fn is_rule(trimmed: &str) -> bool {
    let compact: String = trimmed.chars().filter(|c| !c.is_whitespace()).collect();
    compact.len() >= 3
        && (compact.chars().all(|c| c == '-')
            || compact.chars().all(|c| c == '*')
            || compact.chars().all(|c| c == '_'))
}

struct ListItem {
    marker: String,
    marker_width: usize,
    body: String,
}

fn list_item(trimmed: &str) -> Option<ListItem> {
    for bullet in ["- ", "* ", "+ "] {
        if let Some(body) = trimmed.strip_prefix(bullet) {
            return Some(ListItem {
                marker: "•".to_string(),
                marker_width: 1,
                body: body.to_string(),
            });
        }
    }
    let digits: String = trimmed.chars().take_while(|c| c.is_ascii_digit()).collect();
    if !digits.is_empty() && digits.len() <= 9 {
        let rest = &trimmed[digits.len()..];
        for sep in [". ", ") "] {
            if let Some(body) = rest.strip_prefix(sep) {
                let marker = format!("{digits}.");
                return Some(ListItem {
                    marker_width: marker.width(),
                    marker,
                    body: body.to_string(),
                });
            }
        }
    }
    None
}

/// Split markdown inline markup into styled spans, dropping the markers.
fn inline_spans(text: &str, base: Style, theme: &HexTheme) -> Vec<Span<'static>> {
    let chars: Vec<char> = text.chars().collect();
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut plain = String::new();
    let mut i = 0;

    macro_rules! flush_plain {
        () => {
            if !plain.is_empty() {
                spans.push(Span::styled(std::mem::take(&mut plain), base));
            }
        };
    }

    while i < chars.len() {
        let c = chars[i];

        if c == '\\' && i + 1 < chars.len() && is_escapable(chars[i + 1]) {
            plain.push(chars[i + 1]);
            i += 2;
            continue;
        }

        if c == '`' {
            let ticks = run_len(&chars, i, '`');
            if let Some(end) = find_run(&chars, i + ticks, '`', ticks) {
                flush_plain!();
                let body: String = chars[i + ticks..end].iter().collect();
                spans.push(Span::styled(
                    body.trim().to_string(),
                    base.fg(theme.text_primary).bg(theme.hover_bg),
                ));
                i = end + ticks;
                continue;
            }
        }

        if c == '[' {
            if let Some((label, url, next)) = parse_link(&chars, i) {
                flush_plain!();
                spans.push(Span::styled(
                    label,
                    base.fg(theme.accent_blue).add_modifier(Modifier::UNDERLINED),
                ));
                if !url.is_empty() {
                    spans.push(Span::styled(
                        format!(" ({url})"),
                        base.fg(theme.text_extra_dim),
                    ));
                }
                i = next;
                continue;
            }
        }

        if let Some((marker_len, end, style)) = emphasis_at(&chars, i, base, theme) {
            let inner: String = chars[i + marker_len..end].iter().collect();
            flush_plain!();
            spans.extend(inline_spans(&inner, style, theme));
            i = end + marker_len;
            continue;
        }

        plain.push(c);
        i += 1;
    }
    flush_plain!();
    spans
}

/// Emphasis opener at `i`, returning (marker length, closing index, style).
/// Underscores only open emphasis at a word boundary so `snake_case` survives.
fn emphasis_at(
    chars: &[char],
    i: usize,
    base: Style,
    theme: &HexTheme,
) -> Option<(usize, usize, Style)> {
    let bold = base.fg(theme.text_bright).add_modifier(Modifier::BOLD);
    let candidates = [
        ('*', 2, bold),
        ('_', 2, bold),
        ('~', 2, base.add_modifier(Modifier::CROSSED_OUT)),
        ('*', 1, base.add_modifier(Modifier::ITALIC)),
        ('_', 1, base.add_modifier(Modifier::ITALIC)),
    ];
    for (c, len, style) in candidates {
        if run_len(chars, i, c) != len {
            continue;
        }
        if c == '_' {
            let before_ok = i == 0 || chars[i - 1].is_whitespace() || chars[i - 1] == '(';
            let after_ok = chars.get(i + len).map(|n| !n.is_whitespace()).unwrap_or(false);
            if !before_ok || !after_ok {
                continue;
            }
        }
        if let Some(end) = find_run(chars, i + len, c, len) {
            let inner: String = chars[i + len..end].iter().collect();
            if inner.trim().is_empty() {
                continue;
            }
            if len == 1 && (inner.starts_with(' ') || inner.ends_with(' ')) {
                continue;
            }
            return Some((len, end, style));
        }
    }
    None
}

fn is_escapable(c: char) -> bool {
    matches!(
        c,
        '\\' | '`' | '*' | '_' | '[' | ']' | '(' | ')' | '#' | '~' | '>' | '-' | '+' | '.'
    )
}

fn run_len(chars: &[char], from: usize, c: char) -> usize {
    chars[from..].iter().take_while(|x| **x == c).count()
}

/// Find the next run of exactly `count` copies of `c`, skipping escaped ones.
fn find_run(chars: &[char], from: usize, c: char, count: usize) -> Option<usize> {
    let mut i = from;
    while i < chars.len() {
        if chars[i] == '\\' && i + 1 < chars.len() {
            i += 2;
            continue;
        }
        if chars[i] == c && run_len(chars, i, c) >= count {
            return Some(i);
        }
        i += 1;
    }
    None
}

fn parse_link(chars: &[char], start: usize) -> Option<(String, String, usize)> {
    let close = find_run(chars, start + 1, ']', 1)?;
    if chars.get(close + 1) != Some(&'(') {
        return None;
    }
    let paren = find_run(chars, close + 2, ')', 1)?;
    let label: String = chars[start + 1..close].iter().collect();
    let url: String = chars[close + 2..paren].iter().collect();
    Some((label, url, paren + 1))
}

pub fn settled_boundary(text: &str) -> usize {
    let mut fence = None;
    let mut offset = 0;
    let mut settled = 0;
    for line in text.split_inclusive('\n') {
        offset += line.len();
        let trimmed = line.trim();
        if let Some(marker) = fence {
            if is_fence_close(trimmed, marker) { fence = None; }
        } else if let Some(marker) = fence_open(trimmed) {
            fence = Some(marker);
        } else if trimmed.is_empty() && line.ends_with('\n') {
            settled = offset;
        }
    }
    settled
}

pub fn wrap_line(line: Line<'static>, width: usize) -> Vec<Line<'static>> {
    let style = line.style;
    wrap_spans(line.spans, width.max(1), 0, 0)
        .into_iter()
        .map(|spans| Line::from(spans).style(style))
        .collect()
}

/// Greedy word wrap that keeps every character's style intact.
fn wrap_spans(
    spans: Vec<Span<'static>>,
    width: usize,
    first_indent: usize,
    cont_indent: usize,
) -> Vec<Vec<Span<'static>>> {
    let chars: Vec<(char, Style)> = spans
        .iter()
        .flat_map(|s| s.content.chars().map(move |c| (c, s.style)))
        .collect();
    wrap_chars(&chars, width, first_indent, cont_indent)
        .into_iter()
        .map(|row| group_chars(&row))
        .collect()
}

fn wrap_chars(
    chars: &[(char, Style)],
    width: usize,
    first_indent: usize,
    cont_indent: usize,
) -> Vec<Vec<(char, Style)>> {
    let mut rows: Vec<Vec<(char, Style)>> = Vec::new();
    let mut cur: Vec<(char, Style)> = Vec::new();
    let mut cur_width = 0usize;

    let limit = |rows: &Vec<Vec<(char, Style)>>| {
        let indent = if rows.is_empty() { first_indent } else { cont_indent };
        width.saturating_sub(indent).max(1)
    };

    let mut i = 0;
    while i < chars.len() {
        // A word is a run of non-space characters plus its trailing spaces.
        let start = i;
        while i < chars.len() && !chars[i].0.is_whitespace() {
            i += 1;
        }
        let core_end = i;
        while i < chars.len() && chars[i].0.is_whitespace() {
            i += 1;
        }
        let word = &chars[start..i];
        let core: usize = chars[start..core_end]
            .iter()
            .map(|(c, _)| c.width().unwrap_or(0))
            .sum();

        let max = limit(&rows);
        if cur_width + core > max && !cur.is_empty() {
            rows.push(std::mem::take(&mut cur));
            cur_width = 0;
        }

        let max = limit(&rows);
        if core > max {
            for (c, style) in word {
                let w = c.width().unwrap_or(0);
                if cur_width + w > max && !cur.is_empty() {
                    rows.push(std::mem::take(&mut cur));
                    cur_width = 0;
                }
                cur.push((*c, *style));
                cur_width += w;
            }
        } else {
            cur.extend_from_slice(word);
            cur_width += word.iter().map(|(c, _)| c.width().unwrap_or(0)).sum::<usize>();
        }
    }
    if !cur.is_empty() {
        rows.push(cur);
    }
    if rows.is_empty() {
        rows.push(Vec::new());
    }
    rows
}

fn group_chars(chars: &[(char, Style)]) -> Vec<Span<'static>> {
    let mut spans: Vec<Span<'static>> = Vec::new();
    for (c, style) in chars {
        match spans.last_mut() {
            Some(last) if last.style == *style => last.content.to_mut().push(*c),
            _ => spans.push(Span::styled(c.to_string(), *style)),
        }
    }
    spans
}

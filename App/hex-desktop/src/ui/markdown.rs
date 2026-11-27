#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MarkdownBlock {
    Paragraph(String),
    Heading {
        level: u8,
        text: String,
    },
    List {
        items: Vec<MarkdownListItem>,
    },
    BlockQuote(Vec<MarkdownBlock>),
    Code {
        language: String,
        code: String,
    },
    Table {
        headers: Vec<String>,
        rows: Vec<Vec<String>>,
    },
    Rule,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarkdownListItem {
    pub ordered: bool,
    pub number: u64,
    pub indent: usize,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InlineToken {
    pub text: String,
    pub bold: bool,
    pub italic: bool,
    pub code: bool,
    pub link: Option<String>,
}

pub fn parse_markdown(input: &str) -> Vec<MarkdownBlock> {
    let lines: Vec<&str> = input.lines().collect();
    let mut blocks = Vec::new();
    let mut index = 0;

    while index < lines.len() {
        let line = lines[index];
        if line.trim().is_empty() {
            index += 1;
            continue;
        }

        if let Some((fence, width, language)) = fence_open(line) {
            index += 1;
            let mut code_lines = Vec::new();
            while index < lines.len() {
                let candidate = lines[index];
                if fence_close(candidate, fence, width) {
                    index += 1;
                    break;
                }
                code_lines.push(candidate);
                index += 1;
            }
            blocks.push(MarkdownBlock::Code {
                language,
                code: code_lines.join("\n"),
            });
            continue;
        }

        if let Some((level, text)) = heading(line) {
            blocks.push(MarkdownBlock::Heading { level, text });
            index += 1;
            continue;
        }

        if is_rule(line) {
            blocks.push(MarkdownBlock::Rule);
            index += 1;
            continue;
        }

        if line.trim_start().starts_with('>') {
            let mut quoted = Vec::new();
            while index < lines.len() {
                let candidate = lines[index];
                let Some(rest) = candidate.trim_start().strip_prefix('>') else {
                    break;
                };
                quoted.push(rest.strip_prefix(' ').unwrap_or(rest));
                index += 1;
            }
            blocks.push(MarkdownBlock::BlockQuote(parse_markdown(
                &quoted.join("\n"),
            )));
            continue;
        }

        if list_marker(line).is_some() {
            let mut items = Vec::new();
            while index < lines.len() {
                if let Some((ordered, number, indent, text)) = list_marker(lines[index]) {
                    items.push(MarkdownListItem {
                        ordered,
                        number,
                        indent,
                        text,
                    });
                    index += 1;
                    continue;
                }
                if lines[index].trim().is_empty() {
                    index += 1;
                    break;
                }
                let Some(last) = items.last_mut() else {
                    break;
                };
                let leading = lines[index]
                    .chars()
                    .take_while(|ch| ch.is_whitespace())
                    .count();
                if leading <= last.indent {
                    break;
                }
                last.text.push('\n');
                last.text.push_str(lines[index].trim());
                index += 1;
            }
            blocks.push(MarkdownBlock::List { items });
            continue;
        }

        if index + 1 < lines.len()
            && line.contains('|')
            && table_delimiter(lines[index + 1]).is_some()
        {
            let headers = split_table_row(line);
            index += 2;
            let mut rows = Vec::new();
            while index < lines.len() {
                let candidate = lines[index];
                if candidate.trim().is_empty() || !candidate.contains('|') {
                    break;
                }
                rows.push(split_table_row(candidate));
                index += 1;
            }
            blocks.push(MarkdownBlock::Table { headers, rows });
            continue;
        }

        let mut paragraph = String::new();
        while index < lines.len() {
            let candidate = lines[index];
            if candidate.trim().is_empty() || starts_block(&lines, index) {
                break;
            }
            if !paragraph.is_empty() {
                paragraph.push('\n');
            }
            paragraph.push_str(candidate);
            index += 1;
        }
        if !paragraph.is_empty() {
            blocks.push(MarkdownBlock::Paragraph(paragraph));
        } else {
            index += 1;
        }
    }

    blocks
}

fn starts_block(lines: &[&str], index: usize) -> bool {
    let line = lines[index];
    fence_open(line).is_some()
        || heading(line).is_some()
        || is_rule(line)
        || line.trim_start().starts_with('>')
        || list_marker(line).is_some()
        || (index + 1 < lines.len()
            && line.contains('|')
            && table_delimiter(lines[index + 1]).is_some())
}

fn fence_open(line: &str) -> Option<(char, usize, String)> {
    let trimmed = line.trim_start();
    let fence = trimmed.chars().next()?;
    if fence != '`' && fence != '~' {
        return None;
    }
    let width = trimmed.chars().take_while(|ch| *ch == fence).count();
    if width < 3 {
        return None;
    }
    let language = trimmed[width..].trim().to_string();
    Some((fence, width, language))
}

fn fence_close(line: &str, fence: char, width: usize) -> bool {
    let trimmed = line.trim_start();
    let count = trimmed.chars().take_while(|ch| *ch == fence).count();
    count >= width && trimmed[count..].trim().is_empty()
}

fn heading(line: &str) -> Option<(u8, String)> {
    let trimmed = line.trim_start();
    if !trimmed.starts_with('#') {
        return None;
    }
    let level = trimmed.chars().take_while(|ch| *ch == '#').count();
    if level > 6 {
        return None;
    }
    let rest = &trimmed[level..];
    if !rest.is_empty() && !rest.chars().next().is_some_and(char::is_whitespace) {
        return None;
    }
    let text = rest.trim();
    let text = text.strip_suffix('#').map(str::trim_end).unwrap_or(text);
    Some((level as u8, text.to_string()))
}

fn is_rule(line: &str) -> bool {
    let trimmed = line.trim();
    if trimmed.len() < 3 {
        return false;
    }
    let first = trimmed.chars().next().unwrap();
    if first != '-' && first != '*' && first != '_' {
        return false;
    }
    trimmed.chars().all(|ch| ch == first)
}

fn list_marker(line: &str) -> Option<(bool, u64, usize, String)> {
    let indent = line.len() - line.trim_start().len();
    let rest = line[indent..]
        .strip_prefix("- ")
        .or_else(|| line[indent..].strip_prefix("* "))
        .or_else(|| line[indent..].strip_prefix("+ "));
    if let Some(text) = rest {
        return Some((false, 0, indent, text.to_string()));
    }
    let rest = &line[indent..];
    let digits = rest.chars().take_while(|ch| ch.is_ascii_digit()).count();
    if digits == 0 {
        return None;
    }
    let suffix = rest[digits..].chars().next()?;
    if suffix != '.' && suffix != ')' {
        return None;
    }
    let text = rest[digits + 1..].strip_prefix(' ')?;
    Some((
        true,
        rest[..digits].parse().unwrap_or(1),
        indent,
        text.to_string(),
    ))
}

fn table_delimiter(line: &str) -> Option<Vec<String>> {
    if !line.contains('-') {
        return None;
    }
    let cells = split_table_row(line);
    if cells.is_empty() {
        return None;
    }
    cells
        .iter()
        .map(|cell| {
            let cell = cell.trim();
            let cell = cell.strip_prefix(':').unwrap_or(cell);
            let cell = cell.strip_suffix(':').unwrap_or(cell);
            (cell.len() >= 3 && cell.chars().all(|ch| ch == '-')).then(|| cell.to_string())
        })
        .collect()
}

fn split_table_row(line: &str) -> Vec<String> {
    let mut line = line.trim();
    line = line.strip_prefix('|').unwrap_or(line);
    line = line.strip_suffix('|').unwrap_or(line);
    let mut cells = Vec::new();
    let mut cell = String::new();
    let mut code_delimiter = None;
    let mut tick_run = 0usize;
    let mut escaped = false;
    for ch in line.chars() {
        if escaped {
            if ch == '|' {
                cell.push('|');
            } else {
                cell.push('\\');
                cell.push(ch);
            }
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
            continue;
        }
        if ch == '`' {
            tick_run += 1;
            cell.push(ch);
            continue;
        }
        if tick_run > 0 {
            if code_delimiter == Some(tick_run) {
                code_delimiter = None;
            } else if code_delimiter.is_none() {
                code_delimiter = Some(tick_run);
            }
            tick_run = 0;
        }
        if ch == '|' && code_delimiter.is_none() {
            cells.push(std::mem::take(&mut cell).trim().to_string());
        } else {
            cell.push(ch);
        }
    }
    if escaped {
        cell.push('\\');
    }
    cells.push(cell.trim().to_string());
    cells
}

pub fn parse_inline(input: &str) -> Vec<InlineToken> {
    parse_inline_styled(input, false, false)
}

fn parse_inline_styled(input: &str, bold: bool, italic: bool) -> Vec<InlineToken> {
    let mut tokens = Vec::new();
    let mut plain = String::new();
    let mut index = 0;

    while index < input.len() {
        let rest = &input[index..];
        let mut flush_plain = || {
            if !plain.is_empty() {
                tokens.push(InlineToken {
                    text: std::mem::take(&mut plain),
                    bold,
                    italic,
                    code: false,
                    link: None,
                });
            }
        };

        if let Some(ch) = rest.chars().next() {
            if ch == '\\'
                && rest[1..]
                    .chars()
                    .next()
                    .is_some_and(|c| c.is_ascii_punctuation())
            {
                plain.push(rest[1..].chars().next().unwrap());
                index += 1 + ch.len_utf8();
                continue;
            }
        }

        if rest.starts_with('`') {
            let width = rest.chars().take_while(|ch| *ch == '`').count();
            let marker = "`".repeat(width);
            if let Some(close) = find_unescaped(&rest[width..], &marker) {
                flush_plain();
                let end = width + close;
                let mut code = rest[width..end].to_string();
                if code.starts_with(' ') && code.ends_with(' ') && code.len() > 1 {
                    code = code[1..code.len() - 1].to_string();
                }
                tokens.push(InlineToken {
                    text: code,
                    bold,
                    italic,
                    code: true,
                    link: None,
                });
                index += end + width;
                continue;
            }
        }

        if let Some((marker, close, next_bold, next_italic)) =
            emphasis_match(input, index, bold, italic)
        {
            flush_plain();
            let inner = &rest[marker.len()..marker.len() + close];
            tokens.extend(parse_inline_styled(inner, next_bold, next_italic));
            index += marker.len() + close + marker.len();
            continue;
        }

        if rest.starts_with('[') || rest.starts_with("![") {
            if let Some((end, label, url, image)) = link_at(rest) {
                flush_plain();
                if image {
                    tokens.extend(parse_inline_styled(label, bold, italic));
                } else if safe_link_url(&url).is_some() {
                    let mut linked = parse_inline_styled(label, bold, italic);
                    for token in &mut linked {
                        token.link = Some(url.clone());
                    }
                    tokens.extend(linked);
                } else {
                    plain.push_str(&rest[..end]);
                }
                index += end;
                continue;
            }
        }

        if rest.starts_with('<') {
            if let Some(close) = rest[1..].find('>') {
                let candidate = &rest[1..close + 1];
                if safe_link_url(candidate).is_some() {
                    flush_plain();
                    tokens.push(InlineToken {
                        text: candidate.to_string(),
                        bold,
                        italic,
                        code: false,
                        link: Some(candidate.to_string()),
                    });
                    index += close + 2;
                    continue;
                }
            }
        }

        let ch = rest.chars().next().unwrap();
        plain.push(ch);
        index += ch.len_utf8();
    }

    if !plain.is_empty() {
        tokens.push(InlineToken {
            text: plain,
            bold,
            italic,
            code: false,
            link: None,
        });
    }
    tokens
}

fn is_ident_char(ch: char) -> bool {
    ch.is_alphanumeric() || ch == '_'
}

fn emphasis_candidate(rest: &str) -> Option<(&'static str, bool, bool, bool)> {
    if rest.starts_with("***") {
        Some(("***", true, true, false))
    } else if rest.starts_with("___") {
        Some(("___", true, true, true))
    } else if rest.starts_with("**") {
        Some(("**", true, false, false))
    } else if rest.starts_with("__") {
        Some(("__", true, false, true))
    } else if rest.starts_with('*') {
        Some(("*", false, true, false))
    } else if rest.starts_with('_') {
        Some(("_", false, true, true))
    } else {
        None
    }
}

fn emphasis_match(
    input: &str,
    index: usize,
    bold: bool,
    italic: bool,
) -> Option<(&'static str, usize, bool, bool)> {
    let rest = &input[index..];
    let (marker, sets_bold, sets_italic, is_underscore) = emphasis_candidate(rest)?;
    if is_underscore
        && input[..index]
            .chars()
            .next_back()
            .is_some_and(is_ident_char)
    {
        return None;
    }
    let after = &rest[marker.len()..];
    let first_after = after.chars().next()?;
    if marker.len() == 1 && first_after.is_whitespace() {
        return None;
    }
    let close = find_emphasis_close(after, marker, is_underscore)?;
    Some((marker, close, bold || sets_bold, italic || sets_italic))
}

fn find_emphasis_close(after: &str, marker: &str, is_underscore: bool) -> Option<usize> {
    let delim = marker.chars().next()?;
    let m_len = marker.len();
    let mut pos = 0;
    while pos < after.len() {
        let slice = &after[pos..];
        let ch = slice.chars().next()?;
        if ch == '\\' {
            pos += ch.len_utf8();
            if let Some(escaped) = after[pos..].chars().next() {
                pos += escaped.len_utf8();
            }
            continue;
        }
        if ch == '`' {
            let ticks = slice.chars().take_while(|c| *c == '`').count();
            let tick_str = &slice[..ticks];
            if let Some(rel_end) = after[pos + ticks..].find(tick_str) {
                pos += ticks + rel_end + ticks;
                continue;
            }
        }
        if slice.starts_with(marker) {
            let run = slice.chars().take_while(|c| *c == delim).count();
            // Allow `**...***` to match the final `**` of a 3-char run so nested `*...*` keeps its closer.
            let candidate_pos = if run > m_len && run == m_len + 1 {
                pos + (run - m_len)
            } else if run != m_len {
                pos += run * delim.len_utf8();
                continue;
            } else {
                pos
            };
            if candidate_pos > 0 {
                let before_close = after[..candidate_pos].chars().next_back();
                let single_ws_ok = m_len > 1 || !before_close.is_some_and(char::is_whitespace);
                let after_idx = candidate_pos + marker.len();
                let after_close = after[after_idx..].chars().next();
                let boundary_ok = !is_underscore || !after_close.is_some_and(is_ident_char);
                if single_ws_ok && boundary_ok {
                    return Some(candidate_pos);
                }
            }
            pos += run * delim.len_utf8();
            continue;
        }
        pos += ch.len_utf8();
    }
    None
}

fn link_at(input: &str) -> Option<(usize, &str, String, bool)> {
    let image = input.starts_with("![");
    let open = if image { 2 } else { 1 };
    let mut depth = 1usize;
    let mut escaped = false;
    let mut close = None;
    for (offset, ch) in input[open..].char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
            continue;
        }
        if ch == '[' {
            depth += 1;
        } else if ch == ']' {
            depth -= 1;
            if depth == 0 {
                close = Some(open + offset);
                break;
            }
        }
    }
    let close = close?;
    let label = &input[open..close];
    let after = &input[close + 1..];
    let destination = after.strip_prefix('(')?;
    let mut depth = 1usize;
    escaped = false;
    for (offset, ch) in destination.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
            continue;
        }
        if ch == '(' {
            depth += 1;
        } else if ch == ')' {
            depth -= 1;
            if depth == 0 {
                let url = destination[..offset].trim().to_string();
                return Some((close + 2 + offset + 1, label, url, image));
            }
        }
    }
    None
}

fn find_unescaped(input: &str, marker: &str) -> Option<usize> {
    let mut offset = 0;
    while let Some(relative) = input[offset..].find(marker) {
        let found = offset + relative;
        let mut backslashes = 0;
        let mut cursor = found;
        while cursor > 0 && input.as_bytes()[cursor - 1] == b'\\' {
            backslashes += 1;
            cursor -= 1;
        }
        if backslashes % 2 == 0 {
            return Some(found);
        }
        offset = found + marker.len();
    }
    None
}

pub fn safe_link_url(url: &str) -> Option<String> {
    let url = url.trim();
    if url.is_empty() || url.chars().any(|ch| ch.is_control() || ch.is_whitespace()) {
        return None;
    }
    let lower = url.to_ascii_lowercase();
    let authority = if let Some(rest) = lower.strip_prefix("https://") {
        rest
    } else if let Some(rest) = lower.strip_prefix("http://") {
        rest
    } else {
        return None;
    };
    let authority = authority.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.is_empty() || authority.starts_with('.') || authority.ends_with('.') {
        return None;
    }
    Some(url.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_structured_blocks_and_streaming_code() {
        let blocks = parse_markdown(
            "# Title\n\nParagraph **bold**.\n\n- one\n- two\n\n1. first\n2. second\n\n> quoted\n\n| A | B |\n| --- | :---: |\n| x | y |\n\n```rust\nfn main() {}\n",
        );
        assert!(matches!(blocks[0], MarkdownBlock::Heading { level: 1, .. }));
        assert!(matches!(blocks[1], MarkdownBlock::Paragraph(_)));
        assert!(matches!(blocks[2], MarkdownBlock::List { .. }));
        assert!(matches!(blocks[3], MarkdownBlock::List { .. }));
        assert!(matches!(blocks[4], MarkdownBlock::BlockQuote(_)));
        assert!(matches!(blocks[5], MarkdownBlock::Table { .. }));
        assert!(matches!(
            blocks[6],
            MarkdownBlock::Code { ref language, ref code }
                if language == "rust" && code == "fn main() {}"
        ));
    }

    #[test]
    fn incomplete_markdown_stays_literal() {
        let tokens = parse_inline("**bold `code [link](https://example.com");
        let text: String = tokens.iter().map(|token| token.text.as_str()).collect();
        assert_eq!(text, "**bold `code [link](https://example.com");
        assert!(tokens
            .iter()
            .all(|token| !token.bold && !token.code && token.link.is_none()));
    }

    #[test]
    fn inline_styles_links_and_images_are_safe() {
        let tokens = parse_inline(
            "**bold `code`** [docs](https://example.com) ![alt](https://example.com/a.png)",
        );
        let rendered_text: String = tokens.iter().map(|t| t.text.as_str()).collect();
        assert_eq!(rendered_text, "bold code docs alt");
        assert!(tokens.iter().any(|token| token.bold && token.code));
        assert!(tokens
            .iter()
            .any(|token| token.link.as_deref() == Some("https://example.com")));
        assert!(tokens
            .iter()
            .any(|token| token.text == "alt" && token.link.is_none()));
        assert_eq!(safe_link_url("javascript:alert(1)"), None);
        assert_eq!(safe_link_url("file:///tmp/secret"), None);
        assert_eq!(safe_link_url("https://exa mple.com"), None);
        assert_eq!(
            safe_link_url("https://example.com/docs"),
            Some("https://example.com/docs".into())
        );
    }

    #[test]
    fn table_split_respects_escapes_and_code_pipes() {
        assert_eq!(
            split_table_row("| a\\|b | `x|y` |"),
            vec!["a|b".to_string(), "`x|y`".to_string()]
        );
        assert_eq!(
            split_table_row("| ``x|y`` | z |"),
            vec!["``x|y``".to_string(), "z".to_string()]
        );
    }

    #[test]
    fn empty_headings_and_leading_blank_code_lines_are_preserved() {
        let blocks = parse_markdown("#\n\n```text\n\nfirst\n```");
        assert!(
            matches!(blocks[0], MarkdownBlock::Heading { level: 1, ref text } if text.is_empty())
        );
        assert!(matches!(blocks[1], MarkdownBlock::Code { ref code, .. } if code == "\nfirst"));
    }

    #[test]
    fn snake_case_and_filenames_preserve_underscores() {
        for sample in ["read_file", "ANIMATIONS_PLAN.md", "foo_bar_baz", "a__b"] {
            let tokens = parse_inline(sample);
            let joined: String = tokens.iter().map(|t| t.text.as_str()).collect();
            assert_eq!(joined, sample);
            assert!(
                tokens.iter().all(|t| !t.bold && !t.italic),
                "unexpected formatting for {sample}: {tokens:?}"
            );
        }
    }

    #[test]
    fn emphasis_separates_bold_and_italic_and_inherits_nested_styles() {
        let italic = parse_inline("*italic* _under_ (_paren_) _keep_underscore_");
        assert!(italic
            .iter()
            .any(|t| t.text == "italic" && !t.bold && t.italic));
        assert!(italic
            .iter()
            .any(|t| t.text == "under" && !t.bold && t.italic));
        assert!(italic
            .iter()
            .any(|t| t.text == "paren" && !t.bold && t.italic));
        assert!(italic
            .iter()
            .any(|t| t.text == "keep_underscore" && !t.bold && t.italic));

        let nested = parse_inline("**bold _and_ italic** and ***both*** and __double__");
        assert!(nested
            .iter()
            .any(|t| t.text == "bold " && t.bold && !t.italic));
        assert!(nested
            .iter()
            .any(|t| t.text == "and" && t.bold && t.italic));
        assert!(nested
            .iter()
            .any(|t| t.text == "both" && t.bold && t.italic));
        assert!(nested
            .iter()
            .any(|t| t.text == "double" && t.bold && !t.italic));
    }
}

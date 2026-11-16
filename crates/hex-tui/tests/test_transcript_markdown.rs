//! Model output must arrive as formatted blocks, not raw markdown source.

use hex_tui::app::{App, ScreenState};
use hex_tui::ui::animation::AnimationState;
use hex_tui::ui::markdown::render_markdown;
use hex_tui::ui::screens::session;
use hex_tui::ui::style::HexTheme;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Modifier;

fn theme() -> HexTheme {
    HexTheme::truecolor()
}

fn rows(lines: &[ratatui::text::Line<'static>]) -> Vec<String> {
    lines.iter().map(|l| l.to_string()).collect()
}

fn screen(app: &App, width: u16, height: u16) -> Buffer {
    let area = Rect::new(0, 0, width, height);
    let mut buf = Buffer::empty(area);
    session::render(app, &theme(), &AnimationState::new(), area, &mut buf);
    buf
}

fn row_text(buf: &Buffer, y: u16) -> String {
    (0..buf.area.width)
        .map(|x| buf[(x, y)].symbol().to_string())
        .collect::<String>()
}

fn session_with(content: &str) -> App {
    let mut app = App::new("proj", "test-model", "apfs_clone");
    app.screen_state = ScreenState::Session;
    app.add_assistant_message(content);
    app.dialogue_scroll = 0;
    app
}

#[test]
fn test_emphasis_markers_never_reach_the_screen() {
    let md = "# Release notes\n\nSome **bold** words, *italic* words and `inline_code`.\n";
    let buf = screen(&session_with(md), 120, 40);
    let text: String = (0..buf.area.height).map(|y| row_text(&buf, y)).collect();

    assert!(!text.contains("**"), "bold markers must be consumed: {text}");
    assert!(!text.contains("`"), "code markers must be consumed");
    assert!(!text.contains("# Release"), "heading marker must be consumed");
    assert!(text.contains("Release notes"));
    assert!(text.contains("bold"));
    assert!(text.contains("inline_code"));
}

#[test]
fn test_bold_and_headings_are_actually_styled() {
    let lines = render_markdown("# Title\n\nA **bold** claim.", 80, &theme());
    let heading = lines.iter().find(|l| l.to_string().contains("Title")).unwrap();
    assert!(heading.to_string().starts_with("  Title"), "headings align with prose");
    let span = heading.spans.iter().find(|s| s.content.contains("Title")).unwrap();
    assert!(span.style.add_modifier.contains(Modifier::BOLD));
    assert_eq!(span.style.fg, Some(theme().text_bright));

    let body = lines.iter().find(|l| l.to_string().contains("claim")).unwrap();
    let bold = body.spans.iter().find(|s| s.content.contains("bold")).unwrap();
    assert!(bold.style.add_modifier.contains(Modifier::BOLD));
    let plain = body.spans.iter().find(|s| s.content.contains("claim")).unwrap();
    assert!(!plain.style.add_modifier.contains(Modifier::BOLD));
}

#[test]
fn test_inline_code_and_fences_are_tinted() {
    let t = theme();
    let lines = render_markdown("Run `cargo test` first.\n\n```rust\nfn main() {}\n```\n", 60, &t);
    let text = rows(&lines).join("\n");
    assert!(!text.contains("```"), "fence markers must not be printed");

    let inline = lines
        .iter()
        .flat_map(|l| l.spans.iter())
        .find(|s| s.content.contains("cargo test"))
        .unwrap();
    assert_eq!(inline.style.bg, Some(t.hover_bg));

    let code_row = lines.iter().find(|l| l.to_string().contains("fn main")).unwrap();
    assert_eq!(code_row.width(), 60, "a fence row is tinted edge to edge");
    for span in &code_row.spans {
        assert_eq!(span.style.bg, Some(t.hover_bg));
    }
}

#[test]
fn test_unterminated_fence_stays_tinted_while_streaming() {
    let t = theme();
    let lines = render_markdown("```python\nprint('partial')", 50, &t);
    let code = lines.iter().find(|l| l.to_string().contains("print")).unwrap();
    assert_eq!(code.width(), 50);
    assert!(code.spans.iter().all(|s| s.style.bg == Some(t.hover_bg)));
}

#[test]
fn test_bullets_hang_under_their_text() {
    let lines = render_markdown(
        "- a first bullet long enough that it must wrap onto a second row at this width\n- second",
        40,
        &theme(),
    );
    let text = rows(&lines);
    assert!(text.iter().any(|r| r.contains('•')), "bullets use a real marker");
    assert!(!text.iter().any(|r| r.trim_start().starts_with("- ")));

    let first = text.iter().position(|r| r.contains("a first bullet")).unwrap();
    let marker_col = text[first].chars().position(|c| c == '•').unwrap();
    let body_col = text[first]
        .chars()
        .position(|c| c == 'a')
        .unwrap();
    assert_eq!(marker_col, 2, "assistant prose indent");
    assert_eq!(body_col, 4);
    if text.len() > first + 1 && !text[first + 1].contains("second") {
        let cont_col = text[first + 1].chars().take_while(|c| *c == ' ').count();
        assert_eq!(cont_col, body_col, "continuation aligns under the text, not the marker");
    }
}

#[test]
fn test_numbered_lists_keep_their_numbers() {
    let lines = render_markdown("1. first\n2. second\n10. tenth", 60, &theme());
    let text = rows(&lines).join("\n");
    assert!(text.contains("1. first"));
    assert!(text.contains("10. tenth"));
}

#[test]
fn test_prose_wraps_inside_the_canvas_without_splitting_words() {
    let long = "The transcript renderer must wrap every paragraph at the canvas edge so that no word is ever clipped or lost off the right hand side of the terminal window.";
    let lines = render_markdown(long, 48, &theme());
    assert!(lines.len() >= 3, "a long paragraph needs several rows");
    for line in &lines {
        assert!(line.width() <= 48, "row overflowed: {}", line);
    }
    let joined = rows(&lines).join(" ");
    for word in long.split_whitespace() {
        assert!(joined.contains(word), "word was split or dropped: {word}");
    }
}

#[test]
fn test_wide_glyphs_are_measured_by_display_width() {
    let lines = render_markdown("日本語のテキストはとても長いので折り返される必要があります", 20, &theme());
    for line in &lines {
        assert!(line.width() <= 20, "double-width glyphs overflowed: {}", line);
    }
}

#[test]
fn test_identifiers_and_paths_are_not_mangled() {
    let lines = render_markdown(
        "Call parse_config_yaml in src/harness_core/config_loader.py.",
        80,
        &theme(),
    );
    let text = rows(&lines).join(" ");
    assert!(text.contains("parse_config_yaml"));
    assert!(text.contains("src/harness_core/config_loader.py"));
    assert!(
        lines.iter().flat_map(|l| l.spans.iter()).all(|s| !s.style.add_modifier.contains(Modifier::ITALIC)),
        "underscores inside identifiers must not become emphasis"
    );
}

#[test]
fn test_links_keep_label_and_target() {
    let lines = render_markdown("See [the docs](https://example.com/hex).", 80, &theme());
    let text = rows(&lines).join(" ");
    assert!(text.contains("the docs"));
    assert!(text.contains("https://example.com/hex"));
    assert!(!text.contains("]("), "link syntax must be consumed");
}

#[test]
fn test_blank_lines_separate_blocks_without_piling_up() {
    let lines = render_markdown("first\n\n\n\nsecond", 60, &theme());
    let text = rows(&lines);
    let blanks = text.iter().filter(|r| r.trim().is_empty()).count();
    assert_eq!(blanks, 1, "collapsed to a single separator: {text:?}");
}

#[test]
fn test_plain_text_is_unchanged() {
    let lines = render_markdown("just a plain sentence", 60, &theme());
    assert_eq!(rows(&lines), vec!["  just a plain sentence".to_string()]);
}

#[test]
fn test_transcript_shows_the_newest_formatted_message() {
    let mut app = App::new("proj", "test-model", "apfs_clone");
    app.screen_state = ScreenState::Session;
    for i in 0..12 {
        app.add_user_message(&format!("question number {i}"));
        app.add_assistant_message(&format!(
            "# Answer {i}\n\n- point one\n- point two\n\nSome **detail** about answer {i}."
        ));
    }
    let buf = screen(&app, 120, 40);
    let text: String = (0..buf.area.height).map(|y| row_text(&buf, y)).collect();
    assert!(text.contains("Answer 11"), "the newest message must be on screen");
    assert!(!text.contains("**"));
}
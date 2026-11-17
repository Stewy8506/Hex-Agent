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

    let code_row = lines
        .iter()
        .find(|l| l.to_string().contains("fn main"))
        .unwrap();
    assert_eq!(code_row.width(), 60, "a fence row is tinted edge to edge");
    for span in &code_row.spans {
        assert_eq!(span.style.bg, Some(t.hover_bg));
    }
}

#[test]
fn test_unterminated_fence_stays_tinted_while_streaming() {
    let t = theme();
    let lines = render_markdown("```python\nprint('partial')", 50, &t);
    let code = lines
        .iter()
        .find(|l| l.to_string().contains("print"))
        .unwrap();
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
    assert!(
        text.iter().any(|r| r.contains('•')),
        "bullets use a real marker"
    );
    assert!(!text.iter().any(|r| r.trim_start().starts_with("- ")));

    let first = text
        .iter()
        .position(|r| r.contains("a first bullet"))
        .unwrap();
    let marker_col = text[first].chars().position(|c| c == '•').unwrap();
    let body_col = text[first].chars().position(|c| c == 'a').unwrap();
    assert_eq!(marker_col, 2, "assistant prose indent");
    assert_eq!(body_col, 4);
    if text.len() > first + 1 && !text[first + 1].contains("second") {
        let cont_col = text[first + 1].chars().take_while(|c| *c == ' ').count();
        assert_eq!(
            cont_col, body_col,
            "continuation aligns under the text, not the marker"
        );
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
    let lines = render_markdown(
        "日本語のテキストはとても長いので折り返される必要があります",
        20,
        &theme(),
    );
    for line in &lines {
        assert!(
            line.width() <= 20,
            "double-width glyphs overflowed: {}",
            line
        );
    }
}

#[test]
fn test_identifiers_and_paths_are_not_mangled() {
    let lines = render_markdown(
        "Call parse_config_yaml in harness_core/config_loader.py.",
        80,
        &theme(),
    );
    let text = rows(&lines).join(" ");
    assert!(text.contains("parse_config_yaml"));
    assert!(text.contains("harness_core/config_loader.py"));
    assert!(
        lines
            .iter()
            .flat_map(|l| l.spans.iter())
            .all(|s| !s.style.add_modifier.contains(Modifier::ITALIC)),
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
fn test_structural_markers_use_the_cursor_accent_without_coloring_prose() {
    for t in [HexTheme::truecolor(), HexTheme::color_256()] {
        let lines = render_markdown("# Heading\n\n- bullet\n  - nested\n1. numbered\n\n> quoted", 60, &t);
        for marker in ["•", "1.", "│ "] {
            let matching: Vec<_> = lines.iter().flat_map(|line| &line.spans)
                .filter(|span| span.content == marker).collect();
            assert!(!matching.is_empty(), "missing marker {marker:?}");
            assert!(matching.iter().all(|span| span.style.fg == Some(t.accent_blue)));
        }
        for body in ["bullet", "nested", "numbered", "quoted"] {
            let span = lines.iter().flat_map(|line| &line.spans)
                .find(|span| span.content.contains(body)).unwrap();
            assert_eq!(span.style.fg, Some(t.text_muted));
        }
        let heading = lines.iter().flat_map(|line| &line.spans)
            .find(|span| span.content.contains("Heading")).unwrap();
        assert_eq!(heading.style.fg, Some(t.text_bright));
    }
}

#[test]
fn test_plain_text_is_unchanged() {
    let lines = render_markdown("just a plain sentence", 60, &theme());
    assert_eq!(rows(&lines), vec!["  just a plain sentence".to_string()]);
}

#[test]
fn test_system_notices_are_dimmer_flush_left_and_italic_on_every_wrapped_row() {
    for t in [HexTheme::truecolor(), HexTheme::color_256()] {
        for width in [60, 120] {
            let mut app = session_with("ordinary assistant response");
            app.dialogue_items.clear();
            app.add_user_message("operator input");
            app.add_tool_call("files", "list_files", "{}", None, None);
            app.complete_tool_call("files", "done", 100, false);
            app.add_assistant_message("ordinary assistant response");
            app.add_system_message(
            "Showing stored history. Previous messages remain visible, but the next prompt starts a fresh conversation without runtime memory.",
        );
            app.dialogue_entrance.set((app.dialogue_items.len(), None));
            app.dialogue_scroll = 0;
            let area = Rect::new(0, 0, width, 40);
            let mut buf = Buffer::empty(area);
            session::render(&app, &t, &AnimationState::new(), area, &mut buf);
            let first = (0..buf.area.height)
                .find(|y| row_text(&buf, *y).contains("Showing stored history."))
                .unwrap();
            let last = (first..buf.area.height)
                .find(|y| row_text(&buf, *y).contains("memory."))
                .unwrap();
            assert!(last > first, "the notice should wrap at width {width}");
            let notice_x = row_text(&buf, first)
                .split_once("Showing stored history.")
                .unwrap()
                .0
                .chars()
                .count() as u16;
            for y in first..=last {
                assert!(
                    !buf[(notice_x, y)].symbol().trim().is_empty(),
                    "wrapped notices must stay flush left"
                );
                for x in notice_x..buf.area.width - 1 {
                    let cell = &buf[(x, y)];
                    if cell.symbol() == "│" {
                        break;
                    }
                    if !cell.symbol().trim().is_empty() {
                        assert!(
                            cell.modifier.contains(Modifier::ITALIC),
                            "notice cell at {x},{y} must be italic"
                        );
                        assert_eq!(cell.fg, t.text_system, "system text must be subtly dimmer");
                    }
                }
            }
            for text in [
                "operator input",
                "list_files",
                "ordinary assistant response",
            ] {
                let y = (0..buf.area.height)
                    .find(|y| row_text(&buf, *y).contains(text))
                    .unwrap();
                let chars: Vec<_> = row_text(&buf, y).chars().collect();
                let needle: Vec<_> = text.chars().collect();
                let x = chars
                    .windows(needle.len())
                    .position(|part| part == needle)
                    .unwrap() as u16;
                for offset in 0..needle.len() as u16 {
                    assert!(
                        !buf[(x + offset, y)].modifier.contains(Modifier::ITALIC),
                        "{text} must not inherit system styling"
                    );
                }
                if text == "ordinary assistant response" {
                    assert_eq!(x, notice_x + 2, "only system notices lose the prose indent");
                    assert_eq!(buf[(x, y)].fg, t.text_muted);
                }
            }
        }
    }
}

#[test]
fn test_italic_system_markdown_preserves_existing_emphasis_and_accents() {
    for t in [HexTheme::truecolor(), HexTheme::color_256()] {
        let mut app = session_with("");
        app.dialogue_items.clear();
        app.add_system_message(
            "# System report\n\n**Approved** `safe_tool`\n\n- tool_allowed\n  - nested_allowed\n\n> retained_history\n\n```text\n    command_output\n```\n\n[details](https://example.com/hex)",
        );
        app.dialogue_entrance.set((app.dialogue_items.len(), None));
        app.dialogue_scroll = 0;
        let area = Rect::new(0, 0, 120, 40);
        let mut buf = Buffer::empty(area);
        session::render(&app, &t, &AnimationState::new(), area, &mut buf);
        let heading_y = (0..area.height)
            .find(|y| row_text(&buf, *y).contains("System report"))
            .unwrap();
        let notice_x = row_text(&buf, heading_y)
            .split_once("System report")
            .unwrap()
            .0
            .chars()
            .count() as u16;
        for (text, emphasis) in [
            ("System report", Modifier::BOLD),
            ("Approved", Modifier::BOLD),
            ("safe_tool", Modifier::empty()),
            ("•", Modifier::empty()),
            ("tool_allowed", Modifier::empty()),
            ("nested_allowed", Modifier::empty()),
            ("│ retained_history", Modifier::empty()),
            ("command_output", Modifier::empty()),
            ("details", Modifier::UNDERLINED),
        ] {
            let y = (0..area.height)
                .find(|y| row_text(&buf, *y).contains(text))
                .unwrap();
            let chars: Vec<_> = row_text(&buf, y).chars().collect();
            let needle: Vec<_> = text.chars().collect();
            let x = chars
                .windows(needle.len())
                .position(|part| part == needle)
                .unwrap() as u16;
            for offset in 0..needle.len() as u16 {
                let cell = &buf[(x + offset, y)];
                assert!(
                    cell.modifier.contains(Modifier::ITALIC | emphasis),
                    "{text} must preserve emphasis and be italic"
                );
                if matches!(text, "safe_tool" | "command_output") {
                    assert_eq!(cell.bg, t.hover_bg);
                }
            }
            if matches!(text, "•" | "│ retained_history" | "details") {
                assert_eq!(buf[(x, y)].fg, t.accent_blue);
            } else {
                assert_eq!(buf[(x, y)].fg, t.text_system);
            }
            if text == "command_output" {
                assert_eq!(x, notice_x + 4, "code indentation must remain intact");
            }
        }
    }
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
    assert!(
        text.contains("Answer 11"),
        "the newest message must be on screen"
    );
    assert!(!text.contains("**"));
}

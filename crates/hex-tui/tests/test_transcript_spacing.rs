//! Spacing acceptance: the transcript must breathe like the mockup, not touch
//! the window border, and blocks must be separated by a blank row.

use hex_tui::app::{App, ScreenState};
use hex_tui::ui::animation::AnimationState;
use hex_tui::ui::screens::session;
use hex_tui::ui::style::HexTheme;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;

// Geometry at 120x40: window border col 0, canvas pad 3 -> content col 4,
// assistant prose indent 2 -> col 6. Header row 1, divider row 2, canvas top
// row 3, +1 top pad -> first transcript row 4.
const CONTENT_X: u16 = 4;
const PROSE_X: u16 = 6;

fn render(app: &App, width: u16, height: u16) -> Buffer {
    let area = Rect::new(0, 0, width, height);
    let mut buf = Buffer::empty(area);
    session::render(app, &HexTheme::truecolor(), &AnimationState::new(), area, &mut buf);
    buf
}

fn row(buf: &Buffer, y: u16) -> String {
    (0..buf.area.width)
        .map(|x| buf[(x, y)].symbol().to_string())
        .collect()
}

/// Absolute column of the first content character, ignoring the window border.
fn first_col(text: &str) -> usize {
    1 + text.chars().skip(1).take_while(|c| *c == ' ').count()
}

/// Column of a substring, counted in characters (box glyphs are multi-byte).
fn col_of(text: &str, needle: &str) -> usize {
    let chars: Vec<char> = text.chars().collect();
    let n: Vec<char> = needle.chars().collect();
    chars.windows(n.len()).position(|w| w == n).unwrap_or_else(|| panic!("{needle:?} not in {text:?}"))
}

fn find_row(buf: &Buffer, needle: &str) -> u16 {
    (0..buf.area.height)
        .find(|y| row(buf, *y).contains(needle))
        .unwrap_or_else(|| panic!("missing {needle:?} in:\n{}", dump(buf)))
}

fn dump(buf: &Buffer) -> String {
    (0..buf.area.height)
        .map(|y| format!("{y:02}|{}", row(buf, y)))
        .collect::<Vec<_>>()
        .join("\n")
}

fn app_with(messages: &[&str]) -> App {
    let mut app = App::new("proj", "test-model", "apfs_clone");
    app.screen_state = ScreenState::Session;
    app.workspace_path = "/tmp/proj".to_string();
    for m in messages {
        app.add_assistant_message(m);
    }
    app.dialogue_scroll = 0;
    app
}

#[test]
fn test_canvas_has_top_and_side_padding() {
    let buf = render(&app_with(&["hello prose"]), 120, 40);
    let y = find_row(&buf, "hello prose");
    assert!(y >= 4, "transcript must start below the header plus a pad row");
    assert_eq!(first_col(&row(&buf, y)), PROSE_X as usize, "prose is inset from the border");
    assert_eq!(buf[(CONTENT_X - 1, y)].symbol(), " ", "padding column stays empty");
}

#[test]
fn test_blank_row_separates_message_blocks() {
    let buf = render(&app_with(&["first block", "second block"]), 120, 40);
    let a = find_row(&buf, "first block");
    let b = find_row(&buf, "second block");
    assert!(b > a + 1, "blocks must not be back to back");
    for y in (a + 1)..b {
        let canvas: String = (CONTENT_X..87).map(|x| buf[(x, y)].symbol().to_string()).collect();
        assert!(canvas.trim().is_empty(), "row {y} between blocks is not blank: {canvas:?}");
    }
}

#[test]
fn test_composer_is_five_rows_with_inner_padding() {
    let buf = render(&app_with(&["some output"]), 120, 40);
    let y = find_row(&buf, "Message hex");
    assert_eq!(col_of(&row(&buf, y), "> Message hex"), 7, "composer text has a 2-column inner pad");
    assert_eq!(buf[(CONTENT_X, y - 2)].symbol(), "╭", "top border two rows above");
    assert_eq!(buf[(CONTENT_X, y + 2)].symbol(), "╰", "bottom border two rows below");
    assert_eq!(buf[(CONTENT_X, y - 1)].symbol(), "│");
    assert_eq!(buf[(CONTENT_X, y + 1)].symbol(), "│");
    let box_right = (CONTENT_X..buf.area.width)
        .find(|x| buf[(*x, y - 2)].symbol() == "╮")
        .expect("composer top-right corner");
    let inner: String = (CONTENT_X + 1..box_right).map(|x| buf[(x, y - 1)].symbol().to_string()).collect();
    assert!(inner.trim().is_empty(), "row above the prompt is padding, not text");
}

#[test]
fn test_footer_is_inset_and_does_not_collide() {
    let buf = render(&app_with(&["some output"]), 120, 40);
    let y = find_row(&buf, "build");
    assert_eq!(first_col(&row(&buf, y)), CONTENT_X as usize);
    let text = row(&buf, y);
    assert!(text.contains("esc interrupt"), "hints still fit at 120 columns");
    let build_at = text.find("build").unwrap();
    let hints_at = text.find("esc interrupt").unwrap();
    assert!(hints_at > build_at + 10, "left telemetry and right hints must not overlap");
}

#[test]
fn test_narrow_terminal_drops_the_rail_and_truncates_the_header() {
    let mut app = app_with(&["narrow output"]);
    app.workspace_path = "/very/long/path/that/cannot/possibly/fit/in/sixty/columns".to_string();
    app.git_branch = "main".to_string();
    let buf = render(&app, 60, 30);

    let header = row(&buf, 1);
    assert!(header.contains("hex"), "header: {header}");
    assert!(header.contains("main"), "branch kept: {header}");
    assert!(header.contains('…'), "long path is truncated, not overlapped: {header}");
    assert_eq!(col_of(&header, "hex"), 4, "header is padded too");

    // No rail divider inside the canvas: the transcript owns the full width.
    let y = find_row(&buf, "narrow output");
    let text = row(&buf, y);
    let interior: String = text.chars().skip(1).take(text.chars().count() - 2).collect();
    assert!(!interior.contains('│'), "rail must be dropped at 60 columns: {text}");
    assert_eq!(first_col(&text), PROSE_X as usize);
}

#[test]
fn test_tool_rows_keep_their_indent_and_gain_a_gap() {
    let mut app = App::new("proj", "test-model", "apfs_clone");
    app.screen_state = ScreenState::Session;
    app.add_assistant_message("before the tool");
    app.dialogue_items.push(hex_tui::app::DialogueItem::ToolCall(
        hex_tui::app::ToolCallItem {
            id: "tc1".to_string(),
            tool: "bash".to_string(),
            args: "cargo test".to_string(),
            result: Some("line one\nline two".to_string()),
            duration_ms: Some(1200),
            exit_status: Some("exit 0".to_string()),
            collapsed: false,
            started_at: None,
        },
    ));
    app.add_assistant_message("after the tool");
    app.dialogue_scroll = 0;

    let buf = render(&app, 120, 40);
    let before = find_row(&buf, "before the tool");
    let tool = find_row(&buf, "cargo test");
    let result = find_row(&buf, "line two");
    let after = find_row(&buf, "after the tool");

    assert!(tool > before + 1, "gap before the tool row");
    assert_eq!(first_col(&row(&buf, find_row(&buf, "line one"))), 8, "tool output keeps its 4-column indent");
    assert!(after > result + 1, "gap after the tool result");
    let between: String = (CONTENT_X..87).map(|x| buf[(x, result + 1)].symbol().to_string()).collect();
    assert!(between.trim().is_empty(), "the gap row is really blank");
}
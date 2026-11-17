use hex_tui::app::App;
use hex_tui::ui::{animation::AnimationState, style::HexTheme, transcript};
use ratatui::{buffer::Buffer, layout::Rect};

fn draw(app: &App, width: u16, height: u16) -> Buffer {
    let area = Rect::new(0, 0, width, height);
    let mut buffer = Buffer::empty(area);
    app.dialogue_entrance.set((app.dialogue_items.len(), None));
    transcript::render(
        app,
        &HexTheme::truecolor(),
        &AnimationState::new(),
        area,
        &mut buffer,
    );
    buffer
}

fn text(buffer: &Buffer) -> String {
    buffer.content.iter().map(|cell| cell.symbol()).collect()
}

#[test]
fn scrolling_a_transcript_shorter_than_the_viewport_is_safe() {
    let mut app = App::new("p", "m", "cow");
    draw(&app, 60, 40);
    app.scroll_up();
    draw(&app, 60, 40);
    draw(&app, 120, 40);
    assert_eq!(app.dialogue_view_offset.get(), 0);
}

#[test]
fn tail_navigation_does_not_wrap_at_u16_max() {
    let mut app = App::new("p", "m", "cow");
    app.dialogue_items.clear();
    for i in 0..35_000 {
        app.add_assistant_message(&format!("row-{i:05}"));
    }
    let buffer = draw(&app, 60, 20);
    assert!(app.dialogue_metrics.get().0 > 65_535);
    assert!(text(&buffer).contains("row-34999"));
    app.scroll_up();
    let above = draw(&app, 60, 20);
    assert!(text(&above).contains("row-34998"));
    assert!(!text(&above).contains("row-34999"));
}

#[test]
fn user_wrapping_is_measured_and_reflow_keeps_the_message_anchor() {
    let mut app = App::new("p", "m", "cow");
    app.dialogue_items.clear();
    app.add_user_message(&"long user words 世界😀 ".repeat(100));
    app.add_assistant_message("last answer");
    let narrow = draw(&app, 32, 10);
    let narrow_rows = app.dialogue_metrics.get().0;
    assert!(narrow_rows > 40);
    assert!(text(&narrow).contains("last answer"));
    app.follow_dialogue = false;
    app.dialogue_scroll = 12;
    draw(&app, 32, 10);
    let resized = draw(&app, 80, 10);
    assert!(app.dialogue_metrics.get().0 < narrow_rows);
    assert!(text(&resized).contains("long user words"));
    let offset = app.dialogue_view_offset.get();
    app.scroll_up();
    assert_eq!(app.dialogue_scroll, offset - 1);
}

#[test]
fn incoming_messages_do_not_drag_a_scrolled_operator_to_the_bottom() {
    let mut app = App::new("p", "m", "cow");
    for i in 0..30 {
        app.add_assistant_message(&format!("answer {i}"));
    }
    draw(&app, 60, 10);
    app.scroll_up();
    draw(&app, 60, 10);
    let offset = app.dialogue_view_offset.get();
    app.append_assistant_delta(" more tokens");
    app.add_system_message("background notice");
    draw(&app, 60, 10);
    assert!(!app.follow_dialogue);
    assert_eq!(app.dialogue_view_offset.get(), offset);
}

#[test]
fn height_invalidation_tracks_tool_expansion_and_same_length_edits() {
    let mut app = App::new("p", "m", "cow");
    app.dialogue_items.clear();
    app.add_tool_call("call", "terminal", "echo test", None, None);
    app.complete_tool_call("call", "line one\nline two\nline three", 10, false);
    draw(&app, 40, 10);
    let collapsed = app.dialogue_metrics.get().0;
    app.toggle_all_tool_calls();
    draw(&app, 40, 10);
    assert_eq!(app.dialogue_metrics.get().0, collapsed + 3);
    app.add_assistant_message("a".repeat(80).as_str());
    draw(&app, 40, 10);
    if let Some(hex_tui::app::DialogueItem::Message(message)) = app.dialogue_items.last_mut() {
        message.content = "\n".repeat(80);
    }
    let buffer = draw(&app, 40, 10);
    assert!(!text(&buffer).contains("aaaa"));
}

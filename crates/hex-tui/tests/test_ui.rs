//! Integration test suite for HEX TUI cockpit layout, scrolling, and focus ergonomics.

use ratatui::backend::TestBackend;
use ratatui::Terminal;

use hex_tui::app::{App, Focus};
use hex_tui::ui;

#[test]
fn test_cockpit_full_layout_rendering() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.add_user_message("Hello from user");
    app.add_assistant_message("Hello from assistant");

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    // 1. Header text ("HEX", branch, model)
    assert!(content.contains("HEX"), "Buffer must contain HEX banner");
    assert!(
        content.contains(&app.git_branch),
        "Buffer must contain git branch"
    );
    assert!(
        content.contains("gemini-2.5-pro"),
        "Buffer must contain model name"
    );

    // 2. Status bar badges
    assert!(
        content.contains("ready") || content.contains("agent"),
        "Buffer must contain status bar telemetry"
    );

    // 3. Dialogue content
    assert!(
        content.contains("Hello from user"),
        "Buffer must contain user dialogue content"
    );
    assert!(
        content.contains("Hello from assistant"),
        "Buffer must contain assistant dialogue content"
    );

    // 4. Input prompt
    assert!(
        content.contains('❯'),
        "Buffer must contain input prompt prefix"
    );
}

#[test]
fn test_dialogue_scrolling_with_input_focus() {
    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    assert_eq!(app.focus, Focus::Input);
    assert!(app.follow_dialogue);

    // Set scroll position
    app.dialogue_scroll = 10;

    // Scrolling up with Focus::Input must decrement dialogue_scroll and disable follow_dialogue
    app.scroll_up();
    assert_eq!(app.dialogue_scroll, 9);
    assert!(!app.follow_dialogue);

    // Scrolling down with Focus::Input must increment dialogue_scroll
    app.scroll_down();
    assert_eq!(app.dialogue_scroll, 10);

    // Resetting follow_dialogue snaps back
    app.scroll_dialogue_to_bottom();
    assert!(app.follow_dialogue);
}

#[test]
fn test_tab_key_focus_cycling() {
    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    assert_eq!(app.focus, Focus::Input);

    // Tab without suggestions cycles to Dialogue
    app.handle_tab();
    assert_eq!(app.focus, Focus::Dialogue);
    assert_eq!(app.status_message, "Focus: Dialogue");

    // Tab from Dialogue without diff cycles back to Input
    app.handle_tab();
    assert_eq!(app.focus, Focus::Input);
    assert_eq!(app.status_message, "Focus: Input");

    // Now open diff viewer
    let sample_diff = "\
--- a/src/test.rs
+++ b/src/test.rs
@@ -1 +1 @@
-old
+new";
    app.load_diff(sample_diff);
    assert!(app.show_diff);

    // Focus cycles: Input -> Dialogue -> Diff -> Input
    app.handle_tab();
    assert_eq!(app.focus, Focus::Dialogue);

    app.handle_tab();
    assert_eq!(app.focus, Focus::Diff);
    assert_eq!(app.status_message, "Focus: Diff");

    app.handle_tab();
    assert_eq!(app.focus, Focus::Input);
    assert_eq!(app.status_message, "Focus: Input");

    // If slash suggestions are open, Tab accepts suggestion instead of switching focus
    app.insert_char('/');
    app.insert_char('d');
    assert!(!app.slash_suggestions.is_empty());
    app.handle_tab();
    assert_eq!(app.input_buffer, "/diff ");
    assert_eq!(app.focus, Focus::Input);
}

#[test]
fn test_page_up_page_down_steps() {
    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.focus = Focus::Dialogue;
    app.dialogue_scroll = 15;
    assert!(app.follow_dialogue);

    // PageUp triggers 5-row scroll delta and disables follow_dialogue
    app.handle_page_up();
    assert_eq!(app.dialogue_scroll, 10);
    assert!(!app.follow_dialogue);

    // PageDown triggers 5-row scroll delta
    app.handle_page_down();
    assert_eq!(app.dialogue_scroll, 15);

    // Also test with Focus::Diff
    app.focus = Focus::Diff;
    app.diff_scroll = 20;

    app.handle_page_up();
    assert_eq!(app.diff_scroll, 15);

    app.handle_page_down();
    assert_eq!(app.diff_scroll, 20);
}

#[test]
fn test_visual_wrapped_lines_scroll_calculation() {
    // In a narrow terminal (e.g. 50 columns wide), a long line wraps into multiple visual lines
    let width = 50;
    let long_text = "This is a very long line that definitely exceeds fifty characters and will be wrapped by Ratatui when rendered in a constrained terminal viewport.";
    let line_len = long_text.chars().count();
    assert!(line_len > width);

    let expected_visual_rows = (line_len + width - 1) / width;
    assert!(expected_visual_rows >= 3);

    // Create App and add this message
    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.dialogue_items.clear();
    app.add_assistant_message(long_text);

    let backend = TestBackend::new(width as u16, 20);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");
    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");

    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(content.contains("definitely exceeds"));
    assert!(content.contains("constrained terminal"));
}

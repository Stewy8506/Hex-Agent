//! Integration test suite for HEX TUI cockpit layout, scrolling, and focus ergonomics.

use ratatui::backend::TestBackend;
use ratatui::Terminal;

use hex_tui::app::{App, Focus};
use hex_tui::ui;

#[test]
fn recalled_unicode_history_keeps_character_cursor_indices() {
    let mut app = App::new("p", "m", "cow");
    app.command_history = vec!["first世".into(), "🙂e\u{301}界".into()];
    app.history_up();
    assert_eq!(app.cursor_pos, 4);
    app.delete_char();
    assert_eq!(app.input_buffer, "🙂e\u{301}");
    assert_eq!(app.cursor_pos, 3);
    app.move_cursor_start();
    app.delete_char_forward();
    app.insert_text("世界");
    assert_eq!(app.input_buffer, "世界e\u{301}");
    assert_eq!(app.cursor_pos, 2);
    app.move_cursor_end();
    assert_eq!(app.cursor_pos, 4);
    app.history_up();
    assert_eq!(app.input_buffer, "first世");
    assert_eq!(app.cursor_pos, 6);
    app.history_down();
    assert_eq!(app.input_buffer, "🙂e\u{301}界");
    assert_eq!(app.cursor_pos, 4);
}

#[test]
fn multiline_composer_renders_on_both_active_screens() {
    for screen in [hex_tui::app::ScreenState::Start, hex_tui::app::ScreenState::Session] {
        let mut app = App::new("p", "m", "cow");
        app.switch_screen(screen);
        app.insert_text("line-one\n世界line-two");
        let mut terminal = Terminal::new(TestBackend::new(100, 36)).unwrap();
        terminal.draw(|f| ui::draw(f, &app)).unwrap();
        let buffer = terminal.backend().buffer();
        let rows: Vec<String> = (0..buffer.area.height)
            .map(|y| (0..buffer.area.width).map(|x| buffer[(x, y)].symbol()).collect())
            .collect();
        let first = rows.iter().position(|row| row.contains("line-one")).unwrap();
        let second = rows.iter().position(|row| row.contains("line-two")).unwrap();
        assert_eq!(second, first + 1);
    }
}

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
    assert!(
        content.contains("hex") || content.contains("HEX"),
        "Buffer must contain HEX banner"
    );
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
        content.contains("build") || content.contains("ready") || content.contains("agent"),
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
        content.contains('>') || content.contains('❯'),
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
fn test_tab_key_mode_cycling() {
    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    assert_eq!(app.mode, "agent");

    // Tab without suggestions cycles mode: agent -> plan -> code -> agent
    app.handle_tab();
    assert_eq!(app.mode, "plan");
    assert_eq!(app.status_message, "Switched mode to: PLAN");

    app.handle_tab();
    assert_eq!(app.mode, "code");
    assert_eq!(app.status_message, "Switched mode to: CODE");

    app.handle_tab();
    assert_eq!(app.mode, "agent");
    assert_eq!(app.status_message, "Switched mode to: AGENT");
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

    // Every word survives the wrap; a phrase may straddle two visual rows.
    for word in long_text.split_whitespace() {
        assert!(content.contains(word), "word lost while wrapping: {word}");
    }
    assert!(content.contains("definitely"));
    assert!(content.contains("fifty characters"));
    assert!(content.contains("constrained"));
}

#[test]
fn test_palette_fuzzy_ranking_and_subsequence() {
    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.open_palette();

    assert_eq!(app.palette_results().len(), app.palette_items.len());
    let navigation: Vec<_> = app.palette_items.iter()
        .filter(|item| !item.command.starts_with('/'))
        .map(|item| item.command.as_str()).collect();
    assert_eq!(navigation, ["session:switch", "sidebar:settings"]);
    assert_eq!(
        app.palette_items.len(),
        hex_tui::app::available_slash_commands().len() + navigation.len(),
        "Palette must contain the real command catalog and supported navigation actions"
    );

    // 2. Subsequence match: 'ast' matches '/blast'
    app.palette_query = "ast".to_string();
    assert!(
        app.palette_results()
            .iter()
            .any(|m| app.palette_items[m.item_index].command == "/blast"),
        "'ast' must match '/blast' via subsequence"
    );

    // 3. Subsequence match: 'rb' matches '/rollback'
    app.palette_query = "rb".to_string();
    assert!(
        app.palette_results()
            .iter()
            .any(|m| app.palette_items[m.item_index].command == "/rollback"),
        "'rb' must match '/rollback' via subsequence"
    );

    // 4. Ranking: the typed command name outranks description matches
    app.palette_query = "mem".to_string();
    let results = app.palette_results();
    assert!(!results.is_empty(), "Results must not be empty for 'mem'");
    assert_eq!(
        app.palette_items[results[0].item_index].command,
        "/memory",
        "'/memory' must be ranked first before subsequence matches"
    );
    assert!(
        results
            .iter()
            .any(|m| app.palette_items[m.item_index].command == "/remember"),
        "Subsequence match '/remember' must also be included"
    );

    // 5. The panel renders the ranked results
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");
    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);
    assert!(
        content.contains("/memory"),
        "Buffer must render the top palette result"
    );
    assert!(
        content.contains("> mem"),
        "Buffer must render the palette query"
    );
}

#[test]
fn test_light_terminal_skin_selection_and_rendering() {
    use ratatui::style::Color;

    // 1. Verify LIGHT Skin constants and skin("light") registration
    let light_skin = ui::design::skin("light");
    assert_eq!(light_skin.border, Color::Rgb(75, 85, 99));
    assert_eq!(light_skin.heading, Color::Rgb(17, 24, 39));
    assert_eq!(light_skin.accent, Color::Rgb(217, 119, 6));
    assert_eq!(light_skin.category, Color::Rgb(79, 70, 229));
    assert_eq!(light_skin.text, Color::Rgb(17, 24, 39));
    assert_eq!(light_skin.muted, Color::Rgb(107, 114, 128));
    assert_eq!(light_skin.success, Color::Rgb(5, 150, 105));
    assert_eq!(light_skin.warn, Color::Rgb(217, 119, 6));
    assert_eq!(light_skin.danger, Color::Rgb(220, 38, 38));
    assert_eq!(light_skin.surface, Color::Rgb(249, 250, 251));
    assert_eq!(light_skin.prompt, "❯");
    assert_eq!(light_skin.gradient[0], Color::Rgb(217, 119, 6));
    assert_eq!(light_skin.gradient[5], Color::Rgb(75, 85, 99));

    // 2. Test COLORFGBG detection logic
    // Light terminal (e.g. "0;15" or "0;7")
    {
        let orig = std::env::var("COLORFGBG").ok();
        std::env::set_var("COLORFGBG", "0;15");
        assert!(hex_tui::app::is_light_terminal());
        assert_eq!(hex_tui::app::detect_default_skin(), "light");

        let app_light = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
        assert_eq!(app_light.skin_name, "light");

        // Dark terminal (e.g. "15;0")
        std::env::set_var("COLORFGBG", "15;0");
        assert!(!hex_tui::app::is_light_terminal());
        assert_eq!(hex_tui::app::detect_default_skin(), "sapphire");

        let app_dark = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
        assert_eq!(app_dark.skin_name, "sapphire");

        if let Some(val) = orig {
            std::env::set_var("COLORFGBG", val);
        } else {
            std::env::remove_var("COLORFGBG");
        }
    }

    // 3. Render frame with light skin
    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.skin_name = "light".to_string();
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");
    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(
        content.contains("███") || content.contains("█ █ █▀▀ ▀▄▀") || content.contains("hex") || content.contains("HEX"),
        "Buffer must contain HEX banner under light skin"
    );
    assert!(content.contains('>') || content.contains('❯'), "Buffer must contain light skin prompt symbol");
}

#[test]
fn test_side_by_side_diff_mode_toggle_and_rendering() {
    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    assert!(!app.side_by_side);

    let sample_diff = "\
--- a/src/core.rs
+++ b/src/core.rs
@@ -1,2 +1,2 @@
-let old_val = 10;
+let new_val = 20;";

    app.load_diff(sample_diff);
    assert!(app.show_diff);

    // Toggle to side-by-side mode
    app.toggle_side_by_side();
    assert!(app.side_by_side);

    let backend = TestBackend::new(160, 40);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");
    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    // Must render both Old and New columns in side-by-side mode
    assert!(
        content.contains("Diff Viewer (Old)"),
        "Must render Old column header in side-by-side mode"
    );
    assert!(
        content.contains("Diff Viewer (New)"),
        "Must render New column header in side-by-side mode"
    );
    assert!(
        content.contains("old_val"),
        "Must render deleted line in left column"
    );
    assert!(
        content.contains("new_val"),
        "Must render added line in right column"
    );

    // Toggle back to unified diff mode
    app.toggle_side_by_side();
    assert!(!app.side_by_side);

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer_unified = terminal.backend().buffer().clone();
    let content_unified = format!("{:?}", buffer_unified);

    assert!(
        content_unified.contains("src/core.rs") || content_unified.contains("Diff Viewer"),
        "Must render unified diff viewer"
    );
    assert!(
        !content_unified.contains("Diff Viewer (Old)"),
        "Must not render Old header in unified mode"
    );
}

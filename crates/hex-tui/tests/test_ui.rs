//! Integration test suite for HEX TUI cockpit layout, scrolling, and focus ergonomics.

use ratatui::backend::TestBackend;
use ratatui::Terminal;

use hex_tui::app::{App, ApprovalRequest, Focus};
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

fn approval_app() -> App {
    let mut app = App::new("p", "m", "cow");
    app.set_approval_request(ApprovalRequest {
        id: "b25".into(),
        risk_level: "R3".into(),
        tool_name: "bash".into(),
        parameters: "echo ok".into(),
        reason: "Run command".into(),
        blast_radius: "Read only".into(),
        cwd: "/work".into(),
    });
    app
}

fn paint_approval(app: &App, width: u16, height: u16) -> ratatui::buffer::Buffer {
    // Offset bounds catch renderers that accidentally assume a (0, 0) origin.
    let area = ratatui::layout::Rect::new(3, 4, width, height);
    let mut buffer = ratatui::buffer::Buffer::empty(area);
    ui::screens::permission::render(app, &ui::style::HexTheme::detect(), area, &mut buffer);
    buffer
}

fn approval_rows(buffer: &ratatui::buffer::Buffer) -> Vec<String> {
    (buffer.area.top()..buffer.area.bottom())
        .map(|y| {
            let mut row = String::new();
            let mut x = buffer.area.left();
            while x < buffer.area.right() {
                let symbol = buffer[(x, y)].symbol();
                row.push_str(symbol);
                // Wide graphemes own the following blank continuation cells.
                x += ratatui::text::Span::raw(symbol).width().max(1) as u16;
            }
            row
        })
        .collect()
}

#[test]
fn approval_compact_short_layout_keeps_all_context_and_controls() {
    let app = approval_app();
    for (width, height) in [(40, 10), (60, 10), (36, 11), (39, 11)] {
        let text = approval_rows(&paint_approval(&app, width, height)).join("\n");
        assert!(app.approval_can_allow.get(), "{width}x{height}: {text}");
        for required in [
            "Permission required (b25)",
            "Tool: bash  Risk: R3",
            "Cwd: /work",
            "Parameters: echo ok",
            "Reason: Run command",
            "Impact: Read only",
            "Allow once",
            "Allow matching calls in this task",
            "Deny",
            "Enter confirm; Esc deny",
        ] {
            assert!(
                text.contains(required),
                "{width}x{height} missing {required}: {text}"
            );
        }
        assert!(!text.contains("in this project"));
        let rows = approval_rows(&paint_approval(&app, width, height));
        for (label, key) in [
            ("Allow once", 'y'),
            ("Allow matching calls in this task", 'a'),
            ("Deny", 'n'),
        ] {
            let index = rows.iter().position(|row| row.contains(label)).unwrap();
            assert!(
                rows[index].trim_end().ends_with(key)
                    || rows
                        .get(index + 1)
                        .is_some_and(|row| row.trim() == key.to_string()),
                "{width}x{height} lost {label}'s hotkey: {}",
                rows.join("\n")
            );
        }
    }
}

#[test]
fn approval_tiny_bounds_always_reset_allow_and_advertise_deny() {
    let app = approval_app();
    for (width, height) in [
        (0, 0),
        (0, 5),
        (5, 0),
        (1, 1),
        (4, 1),
        (12, 2),
        (20, 2),
        (35, 3),
        (80, 6),
        (39, 9),
    ] {
        app.approval_can_allow.set(true);
        let text = approval_rows(&paint_approval(&app, width, height)).join("\n");
        assert!(!app.approval_can_allow.get(), "{width}x{height}: {text}");
        assert!(!text.contains("Allow once"));
        assert!(!text.contains("Allow matching"));
        if width > 0 && height > 0 {
            assert!(
                text.contains('n'),
                "deny hint missing at {width}x{height}: {text}"
            );
        }
        if width >= 14 && height >= 2 {
            assert!(
                text.to_lowercase().contains("resize"),
                "{width}x{height}: {text}"
            );
        }
    }
}

#[test]
fn approval_unicode_columns_and_multiline_parameters_are_not_silently_clipped() {
    let mut app = approval_app();
    // Twelve prefix columns plus fourteen double-width characters fit exactly.
    app.active_approval.as_mut().unwrap().parameters = "界".repeat(14);
    let text = approval_rows(&paint_approval(&app, 40, 10)).join("\n");
    assert!(app.approval_can_allow.get(), "{text}");
    assert!(text.contains(&format!("Parameters: {}", "界".repeat(14))));
    app.active_approval.as_mut().unwrap().parameters.push('界');
    let text = approval_rows(&paint_approval(&app, 40, 10)).join("\n");
    assert!(
        !app.approval_can_allow.get(),
        "a width-overflowing request must not allow: {text}"
    );
    assert!(text.contains("Resize"));

    app.active_approval.as_mut().unwrap().parameters = "e\u{301}👩‍💻界\nrm -rf /".into();
    paint_approval(&app, 40, 10);
    assert!(
        !app.approval_can_allow.get(),
        "hard newline needs another row"
    );
    let text = approval_rows(&paint_approval(&app, 40, 11)).join("\n");
    assert!(app.approval_can_allow.get(), "{text}");
    assert!(text.contains("e\u{301}👩‍💻界"));
    assert!(text.contains("rm -rf /"));

    app.active_approval.as_mut().unwrap().parameters = "first\n\nlast\n".into();
    let rows = approval_rows(&paint_approval(&app, 40, 13));
    assert!(app.approval_can_allow.get());
    let first = rows
        .iter()
        .position(|row| row.contains("Parameters: first"))
        .unwrap();
    assert!(rows[first + 1].trim().is_empty());
    assert_eq!(rows[first + 2].trim(), "last");
    assert!(rows[first + 3].trim().is_empty());
}

#[test]
fn approval_every_oversized_context_field_and_unrenderable_control_fails_closed() {
    for field in [
        "id",
        "tool",
        "risk",
        "cwd",
        "parameters",
        "reason",
        "impact",
    ] {
        let mut app = approval_app();
        let request = app.active_approval.as_mut().unwrap();
        let value = "界".repeat(2_000);
        match field {
            "id" => request.id = value,
            "tool" => request.tool_name = value,
            "risk" => request.risk_level = value,
            "cwd" => request.cwd = value,
            "parameters" => request.parameters = value,
            "reason" => request.reason = value,
            "impact" => request.blast_radius = value,
            _ => unreachable!(),
        }
        let text = approval_rows(&paint_approval(&app, 120, 36)).join("\n");
        assert!(
            !app.approval_can_allow.get(),
            "{field} was clipped but allow remained enabled"
        );
        assert!(text.contains("Resize"));
        assert!(text.contains("Deny"));
        assert!(!text.contains("Allow once"));
    }
    for parameters in [
        "echo safe\t; rm -rf /",
        "echo safe\rmalicious",
        "echo \u{1b}[2J",
        "\u{200b}hidden",
    ] {
        let mut app = approval_app();
        app.active_approval.as_mut().unwrap().parameters = parameters.into();
        paint_approval(&app, 120, 36);
        assert!(
            !app.approval_can_allow.get(),
            "invisible context must not allow: {parameters:?}"
        );
    }
}

#[test]
fn approval_pending_error_and_retry_states_are_visible_and_gate_allow() {
    let mut app = approval_app();
    paint_approval(&app, 100, 24);
    assert!(app.approval_can_allow.get());
    app.approval_pending = true;
    let text = approval_rows(&paint_approval(&app, 100, 24)).join("\n");
    assert!(!app.approval_can_allow.get());
    assert!(text.contains("Submitting decision; please wait"));
    assert!(text.contains("controls disabled while pending"));
    assert!(!text.contains("Allow once"));

    app.approval_pending = false;
    app.approval_error = Some("Backend offline".into());
    let text = approval_rows(&paint_approval(&app, 100, 24)).join("\n");
    assert!(app.approval_can_allow.get());
    assert!(text.contains("Error: Backend offline"));
    assert!(text.contains("Retry: choose a decision below"));
    assert!(text.contains("Allow matching calls in this task"));

    let text = approval_rows(&paint_approval(&app, 40, 4)).join("\n");
    assert!(!app.approval_can_allow.get());
    assert!(text.contains("Approval failed"));
    assert!(text.contains("Error: Backend offline"));
    assert!(text.contains("Resize to review and retry"));
    assert!(text.contains("Deny"));

    app.approval_error = Some("backend error ".repeat(1_000));
    let text = approval_rows(&paint_approval(&app, 100, 24)).join("\n");
    assert!(
        !app.approval_can_allow.get(),
        "a clipped error must not hide retry context"
    );
    assert!(text.contains("Approval failed"));
    assert!(!text.contains("Allow once"));
    app.approval_error = Some("Backend offline".into());

    app.approval_pending = true;
    let text = approval_rows(&paint_approval(&app, 100, 24)).join("\n");
    assert!(!app.approval_can_allow.get());
    assert!(text.contains("Error: Backend offline"));
    assert!(text.contains("Submitting decision"));
    assert!(!text.contains("Retry:"));
    let text = approval_rows(&paint_approval(&app, 40, 3)).join("\n");
    assert!(text.contains("Approval pending"));
    assert!(text.contains("Wait"));
    assert!(!text.contains("Deny"));
    assert!(!app.approval_can_allow.get());
}

#[test]
fn approval_is_topmost_over_diff_help_palette_and_settings() {
    for diff in [false, true] {
        let mut app = approval_app();
        app.switch_screen(hex_tui::app::ScreenState::Session);
        app.load_diff("--- a/a.rs\n+++ b/a.rs\n@@ -1 +1 @@\n-old\n+new");
        app.side_by_side = diff;
        app.show_help = true;
        app.show_palette = true;
        app.sidebar.show_settings = true;
        let mut terminal = Terminal::new(TestBackend::new(120, 36)).unwrap();
        terminal.draw(|frame| ui::draw(frame, &app)).unwrap();
        assert!(app.approval_can_allow.get());
        let actual = terminal.backend().buffer();
        let mut expected = ratatui::buffer::Buffer::empty(actual.area);
        ui::screens::permission::render(
            &app,
            &ui::style::HexTheme::detect(),
            actual.area,
            &mut expected,
        );
        // Every modal cell must survive lower-priority overlays.
        for y in 12..24 {
            for x in 20..100 {
                assert_eq!(
                    actual[(x, y)].symbol(),
                    expected[(x, y)].symbol(),
                    "diff={diff} at ({x}, {y})"
                );
            }
        }
        for (width, height, can_allow) in [(40, 10, true), (20, 5, false)] {
            let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
            terminal.draw(|frame| ui::draw(frame, &app)).unwrap();
            let text = approval_rows(terminal.backend().buffer()).join("\n");
            assert_eq!(
                app.approval_can_allow.get(),
                can_allow,
                "diff={diff}: {text}"
            );
            assert!(text.contains("Permission required"));
            assert!(text.contains("Deny"));
            assert_eq!(text.contains("Allow once"), can_allow);
        }
    }
}

#[test]
fn approval_allow_gate_is_recomputed_after_resize_and_without_an_approval() {
    let mut app = approval_app();
    let mut terminal = Terminal::new(TestBackend::new(120, 36)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &app)).unwrap();
    assert!(app.approval_can_allow.get());
    let mut tiny = Terminal::new(TestBackend::new(20, 5)).unwrap();
    tiny.draw(|frame| ui::draw(frame, &app)).unwrap();
    assert!(!app.approval_can_allow.get());
    let text = approval_rows(tiny.backend().buffer()).join("\n");
    assert!(text.contains("Deny"));
    terminal.draw(|frame| ui::draw(frame, &app)).unwrap();
    assert!(app.approval_can_allow.get());

    app.active_approval = None;
    // Exercise draw's reset independently of App's clear-approval method.
    app.approval_can_allow.set(true);
    app.side_by_side = true;
    app.show_diff = true;
    terminal.draw(|frame| ui::draw(frame, &app)).unwrap();
    assert!(!app.approval_can_allow.get());
}

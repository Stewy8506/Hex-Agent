use ratatui::backend::TestBackend;
use ratatui::Terminal;
use hex_tui::app::App;
use hex_tui::ui;

#[test]
fn test_render_241x68_start_screen() {
    let backend = TestBackend::new(241, 68);
    let mut terminal = Terminal::new(backend).unwrap();
    let app = App::with_workspace(
        "Harness",
        "gemini-2.5-pro",
        "apfs_clone",
        Some(std::path::Path::new("/Users/anv./Development/Harness")),
    );
    assert!(app.plan_steps.is_empty(), "plan_steps must initialize empty on session startup");

    terminal.draw(|f| ui::draw(f, &app)).unwrap();
    let buf = terminal.backend().buffer().clone();

    println!("=== RENDERED BUFFER SLICE (cols 65..175, rows 12..40) ===");
    for y in 12..40 {
        let mut line = String::new();
        for x in 65..176 {
            line.push(buf[(x, y)].symbol().chars().next().unwrap_or(' '));
        }
        println!("[{:02}] {}", y, line);
    }

    // 1. Verify vertical optical centering:
    // With area.height = 68 and content_height = 23, top_pad = (68 - 23) / 2 = 22.
    // The wordmark starts exactly at row 22.
    let mut row_22 = String::new();
    for x in 0..241 {
        row_22.push_str(buf[(x, 22)].symbol());
    }
    assert!(row_22.contains("██  ██  ██████  ██  ██"), "Row 22 must contain the first row of StartWordmark");

    // 2. Verify authentic 4-row geometric pixel wordmark with asymmetric low crossbar on H,
    // short middle arm on E, and center-pillar X:
    let mut row_24 = String::new();
    for x in 0..241 {
        row_24.push_str(buf[(x, 24)].symbol());
    }
    assert!(row_24.contains("██████  ████      ██"), "Row 24 must contain authentic H low crossbar, E middle arm, and center pillar X");

    // 3. Verify responsive proportion width (~52% of 241 = 125 cols, centered horizontally)
    let left_margin = 58;
    let right_margin = 58 + 125 - 1; // 182
    assert_eq!(buf[(left_margin, 32)].symbol(), "╭");
    assert_eq!(buf[(right_margin, 32)].symbol(), "╮");
    assert_eq!(right_margin - left_margin + 1, 125); // ~52% responsive width matching Start.dc.html
}

#[test]
fn test_render_241x68_session_screen() {
    let backend = TestBackend::new(241, 68);
    let mut terminal = Terminal::new(backend).unwrap();
    let mut app = App::with_workspace(
        "Harness",
        "gemini-2.5-pro",
        "apfs_clone",
        Some(std::path::Path::new("/Users/anv./Development/Harness")),
    );
    assert!(app.plan_steps.is_empty(), "plan_steps must initialize empty on session startup");

    app.screen_state = hex_tui::app::ScreenState::Session;
    app.add_user_message("refactor the graph builder to stream edges instead of buffering. keep the public API.");
    app.current_thought = Some("Reading the builder first, then replacing the edge buffer with an async iterator.".to_string());
    app.add_tool_call("tc_1", "read", "src/graph/builder.ts 212 lines", Some(120), Some("exit 0"));

    terminal.draw(|f| ui::draw(f, &app)).unwrap();
    let buf = terminal.backend().buffer().clone();

    println!("=== RENDERED SESSION SLICE (rows 0..25) ===");
    for y in 0..25 {
        let mut left_line = String::new();
        for x in 0..120 {
            left_line.push(buf[(x, y)].symbol().chars().next().unwrap_or(' '));
        }
        let mut right_line = String::new();
        for x in 186..241 {
            right_line.push(buf[(x, y)].symbol().chars().next().unwrap_or(' '));
        }
        println!("[{:02}] {} │ {}", y, left_line, right_line);
    }

    // 1. Verify right rail renders "no active plan" when plan_steps is empty
    let mut full_right = String::new();
    for y in 0..68 {
        for x in 180..241 {
            full_right.push_str(buf[(x, y)].symbol());
        }
    }
    assert!(full_right.contains("no active plan"), "Right rail must render 'no active plan' when plan_steps is empty");

    // 2. Verify dynamic plan step extraction via add_assistant_message
    app.add_assistant_message("Plan:\n- [x] Initial setup\n- [>] Refactor streaming builder\n- [ ] Regression verification");
    assert_eq!(app.plan_steps.len(), 3);
    assert_eq!(app.plan_steps[0].status, hex_tui::app::StepStatus::Completed);
    assert_eq!(app.plan_steps[1].status, hex_tui::app::StepStatus::Active);
    assert_eq!(app.plan_steps[2].status, hex_tui::app::StepStatus::Pending);

    // 3. Re-draw and confirm right rail updates dynamically with extracted plan steps
    terminal.draw(|f| ui::draw(f, &app)).unwrap();
    let buf2 = terminal.backend().buffer().clone();
    let mut full_right2 = String::new();
    for y in 0..68 {
        for x in 180..241 {
            full_right2.push_str(buf2[(x, y)].symbol());
        }
    }
    assert!(full_right2.contains("[x] Initial setup"), "Right rail must display completed plan step");
    assert!(full_right2.contains("Refactor streaming builder"), "Right rail must display active plan step");
    assert!(full_right2.contains("[ ] Regression verification"), "Right rail must display pending plan step");
}

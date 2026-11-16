use ratatui::backend::TestBackend;
use ratatui::Terminal;

use hex_tui::app::{App, ApprovalRequest, SubagentModalState};
use hex_tui::ui::screens::error_view::{render_diagnostic_card, DiagnosticFailure};
use hex_tui::ui::style::HexTheme;

#[test]
fn test_screen_2_permission_modal_rendering() {
    let mut app = App::new("harness-core", "sonnet", "sandbox");
    app.screen_state = hex_tui::app::ScreenState::Session;
    app.active_approval = Some(ApprovalRequest {
        id: "app-123".to_string(),
        tool_name: "cargo".to_string(),
        parameters: "cargo check --workspace".to_string(),
        reason: "Validates syntax and types across crates.".to_string(),
        blast_radius: String::new(),
        cwd: String::new(),
        risk_level: "low".to_string(),
    });
    app.selected_permission_index = 0;

    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).unwrap();

    terminal
        .draw(|f| {
            hex_tui::ui::draw(f, &app);
        })
        .unwrap();

    let buffer = terminal.backend().buffer();
    let content: String = (0..buffer.area.height)
        .map(|y| {
            let mut line = String::new();
            for x in 0..buffer.area.width {
                line.push_str(buffer[(x, y)].symbol());
            }
            line
        })
        .collect::<Vec<_>>()
        .join("\n");

    assert!(content.contains("Permission required"), "Content was:\n{}", content);
    assert!(content.contains("cargo"), "Content was:\n{}", content);
    assert!(content.contains("cargo check --workspace"), "Content was:\n{}", content);
    assert!(content.contains("Validates syntax and types across crates."), "Content was:\n{}", content);
    assert!(content.contains("Allow once"), "Content was:\n{}", content);
    assert!(content.contains("Allow cargo in this project"), "Content was:\n{}", content);
    assert!(content.contains("Deny"), "Content was:\n{}", content);
}

#[test]
fn test_screen_3_palette_modal_rendering() {
    let mut app = App::new("harness-core", "sonnet", "sandbox");
    app.screen_state = hex_tui::app::ScreenState::Session;
    app.show_palette = true;
    app.palette_query = "plan".to_string();
    app.selected_palette_index = 0;

    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).unwrap();

    terminal
        .draw(|f| {
            hex_tui::ui::draw(f, &app);
        })
        .unwrap();

    let buffer = terminal.backend().buffer();
    let content: String = (0..buffer.area.height)
        .map(|y| {
            let mut line = String::new();
            for x in 0..buffer.area.width {
                line.push_str(buffer[(x, y)].symbol());
            }
            line
        })
        .collect::<Vec<_>>()
        .join("\n");

    assert!(content.contains("> plan"), "Content was:\n{}", content);
    assert!(content.contains("/plan"), "Content was:\n{}", content);
    assert!(
        content.contains("Switch to read-only architecture planning mode"),
        "Content was:\n{}",
        content
    );
    assert!(content.contains("enter select · esc close"), "Content was:\n{}", content);
}

#[test]
fn test_screen_4_diagnostic_failure_card_rendering() {
    let theme = HexTheme::default();
    let diag = DiagnosticFailure {
        command: "cargo test --workspace".to_string(),
        exit_code: 101,
        duration_str: "2.5s".to_string(),
        test_file: "test_screens.rs".to_string(),
        test_name: "test_real_data_telemetry".to_string(),
        expected: "Pass(5)".to_string(),
        received: "Fail(1)".to_string(),
        stack_trace: vec!["at test_real_data_telemetry (tests/test_screens.rs:18:9)".to_string()],
        hidden_frames_count: 4,
        diagnosis: "Telemetry mismatch in test harness".to_string(),
        next_investigation: "Investigate tests/test_screens.rs".to_string(),
    };

    let backend = TestBackend::new(100, 20);
    let mut terminal = Terminal::new(backend).unwrap();

    terminal
        .draw(|f| {
            let area = f.area();
            render_diagnostic_card(&diag, &theme, area, f.buffer_mut());
        })
        .unwrap();

    let buffer = terminal.backend().buffer();
    let content: String = (0..buffer.area.height)
        .map(|y| {
            let mut line = String::new();
            for x in 0..buffer.area.width {
                line.push_str(buffer[(x, y)].symbol());
            }
            line
        })
        .collect::<Vec<_>>()
        .join("\n");

    assert!(content.contains("bash"), "Content was:\n{}", content);
    assert!(content.contains("cargo test --workspace"), "Content was:\n{}", content);
    assert!(content.contains("fail"), "Content was:\n{}", content);
    assert!(content.contains("test_screens.rs"), "Content was:\n{}", content);
    assert!(content.contains("test_real_data_telemetry"), "Content was:\n{}", content);
    assert!(content.contains("expected"), "Content was:\n{}", content);
    assert!(content.contains("received"), "Content was:\n{}", content);
    assert!(content.contains("4 frames hidden"), "Content was:\n{}", content);
    assert!(content.contains("Telemetry mismatch in test harness"), "Content was:\n{}", content);
}

#[test]
fn test_screen_5_subagent_modal_rendering() {
    let mut app = App::new("harness-core", "sonnet", "sandbox");
    app.screen_state = hex_tui::app::ScreenState::Session;
    app.active_subagent_modal = Some(SubagentModalState {
        name: "test-verifier".to_string(),
        prompt: "Verify continuous animations and dynamic telemetry.".to_string(),
        is_expanded: false,
        activity_lines: vec![
            "read crates/hex-tui/src/app.rs".to_string(),
            "check telemetry counts".to_string(),
        ],
        elapsed_seconds: 42,
    });

    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).unwrap();

    terminal
        .draw(|f| {
            hex_tui::ui::draw(f, &app);
        })
        .unwrap();

    let buffer = terminal.backend().buffer();
    let content: String = (0..buffer.area.height)
        .map(|y| {
            let mut line = String::new();
            for x in 0..buffer.area.width {
                line.push_str(buffer[(x, y)].symbol());
            }
            line
        })
        .collect::<Vec<_>>()
        .join("\n");

    assert!(content.contains("prompt"), "Content was:\n{}", content);
    assert!(content.contains("e expand"), "Content was:\n{}", content);
    assert!(content.contains("Verify continuous animations"), "Content was:\n{}", content);
    assert!(content.contains("read crates/hex-tui/src/app.rs"), "Content was:\n{}", content);
    assert!(content.contains("check telemetry counts"), "Content was:\n{}", content);
    assert!(content.contains("42s"), "Content was:\n{}", content);
    assert!(content.contains("up/down scroll · e expand prompt · esc close"), "Content was:\n{}", content);
}

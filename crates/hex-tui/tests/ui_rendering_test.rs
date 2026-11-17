//! Integration tests for Ratatui frame rendering, layout splits, and mockup screen parity.

use ratatui::backend::TestBackend;
use ratatui::Terminal;

use hex_tui::app::{App, ApprovalRequest, ScreenState, SubagentModalState};
use hex_tui::ui;
use hex_tui::ui::screens::error_view::{render_diagnostic_card, DiagnosticFailure};
use hex_tui::ui::style::HexTheme;

#[test]
fn test_screen_0_start_layout_rendering() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("harness-project", "gemini-2.5-pro", "sandbox");
    app.resume_sessions = vec![
        hex_tui::app::ResumeSession {
            title: "profile the render loop".to_string(),
            relative_time: "3d".to_string(),
            session_id: "ses_2e9d".to_string(),
        },
    ];
    assert_eq!(app.screen_state, ScreenState::Start);

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    // 1. Block ASCII Wordmark
    assert!(content.contains("██████") || content.contains("███") || content.contains("█ █ █▀▀ ▀▄▀"), "Buffer must contain ASCII banner");

    // 2. Project path and telemetry
    assert!(content.contains("harness-project"), "Buffer must contain project name");
    assert!(content.contains("files indexed"), "Buffer must contain indexed file count");

    // 3. Prompt container & shortcuts
    for hint in ["commands", "resume", "send", "help"] {
        assert!(content.contains(hint), "Buffer must contain {hint} hint");
    }
    for stale in ["ctrl+r", "@ files", "tab agent"] {
        assert!(!content.contains(stale), "Buffer must not advertise {stale}");
    }

    // 4. Resume sessions
    assert!(content.contains("resume"), "Buffer must contain resume section");
    assert!(content.contains("profile the render loop"), "Buffer must list recent session");
}

#[test]
fn test_screen_1_session_and_right_rail_rendering() {
    let backend = TestBackend::new(140, 40);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("harness-project", "claude-3-7-sonnet", "sandbox");
    app.screen_state = ScreenState::Session;
    app.add_user_message("refactor the graph router");
    app.plan_steps = vec![
        hex_tui::app::PlanStep {
            title: "audit memory graph".to_string(),
            status: hex_tui::app::StepStatus::Completed,
        },
    ];
    app.changed_files = vec![
        hex_tui::app::ChangedFile {
            path: "src/router.rs".to_string(),
            additions: 12,
            deletions: 4,
        },
    ];
    app.subagents = vec![
        hex_tui::app::SubagentTelemetry {
            name: "ast-walker".to_string(),
            status: "running".to_string(),
            detail: "indexing AST nodes".to_string(),
            is_running: true,
            has_failed: false,
        },
    ];
    app.tokens_used = 42_000;
    app.max_tokens = 200_000;

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    // 1. Top Bar
    assert!(content.contains("hex"), "Top bar must render 'hex'");
    assert!(content.contains(&app.git_branch), "Top bar must render branch");

    // 2. Session Transcript & Wordmark
    assert!(content.contains("0.4.2 · session"), "Transcript must render session version");
    assert!(content.contains("refactor the graph router"), "Must render user prompt");

    // 3. Right Rail Telemetry
    assert!(content.contains("context"), "Right rail must contain context header");
    assert!(content.contains("plan"), "Right rail must contain plan header");
    assert!(content.contains("changed"), "Right rail must contain changed header");
    assert!(content.contains("src/router.rs"), "Must render changed file");
    assert!(content.contains("agents"), "Right rail must contain agents header");
    assert!(content.contains("ast-walker"), "Must render subagent name");

    // 4. Composer & Status Footer
    assert!(content.contains("build"), "Footer must contain build mode");
    assert!(content.contains("claude-3-7-sonnet"), "Footer must contain model name");
    assert!(content.contains("tokens"), "Footer must contain token telemetry");
    assert!(content.contains("ctrl+p commands"), "Footer must contain commands hint");
}

#[test]
fn test_screen_2_permission_modal_overlay() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("harness-project", "claude-3-7-sonnet", "sandbox");
    app.screen_state = ScreenState::Session;
    app.active_approval = Some(ApprovalRequest {
        id: "app-123".to_string(),
        tool_name: "rm".to_string(),
        parameters: "rm -rf dist/build".to_string(),
        reason: "Deletes temporary build directory.".to_string(),
        blast_radius: String::new(),
        cwd: String::new(),
        risk_level: "high".to_string(),
    });
    app.selected_permission_index = 0;

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(content.contains("Permission required"), "Must render permission modal header");
    assert!(content.contains("rm"), "Must render tool name");
    assert!(content.contains("rm -rf dist/build"), "Must render command preview");
    assert!(content.contains("Deletes temporary build directory."), "Must render reason");
    assert!(content.contains("Allow once"), "Must render Allow once radio option");
    assert!(content.contains("Allow rm in this project"), "Must render dynamic Allow option");
    assert!(content.contains("Deny"), "Must render Deny radio option");
}

#[test]
fn test_screen_3_command_palette_overlay() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("harness-project", "claude-3-7-sonnet", "sandbox");
    app.screen_state = ScreenState::Session;
    app.show_palette = true;
    app.palette_query = "plan".to_string();

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(content.contains("> plan"), "Must render palette query");
    assert!(content.contains("/plan"), "Must render the matching command shortcut");
    assert!(
        !content.contains("Switch to read-only architecture planning mode"),
        "Must render command names without verbose help prose"
    );
    assert!(content.contains("enter select · esc close"), "Must render footer hint");
}

#[test]
fn test_screen_4_diagnostic_failure_card() {
    let backend = TestBackend::new(100, 20);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");
    let theme = HexTheme::default();
    let diag = DiagnosticFailure {
        command: "cargo check".to_string(),
        exit_code: 1,
        duration_str: "0.8s".to_string(),
        test_file: "test_harness.rs".to_string(),
        test_name: "test_pipeline".to_string(),
        expected: "Ready".to_string(),
        received: "Error".to_string(),
        stack_trace: vec!["at pipeline.run (src/pipeline.rs:12:4)".to_string()],
        hidden_frames_count: 2,
        diagnosis: "Pipeline state was error instead of ready".to_string(),
        next_investigation: "Check pipeline initialization".to_string(),
        detail_lines: Vec::new(),
    };

    terminal
        .draw(|f| {
            render_diagnostic_card(&diag, &theme, f.area(), f.buffer_mut());
        })
        .expect("Draw failed");

    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(content.contains("bash"), "Must render bash header");
    assert!(content.contains("cargo check"), "Must render command");
    assert!(content.contains("fail"), "Must render fail badge");
    assert!(content.contains("test_harness.rs"), "Must render failing file");
    assert!(content.contains("test_pipeline"), "Must render test name");
    assert!(content.contains("expected"), "Must render expected diff");
    assert!(content.contains("received"), "Must render received diff");
}

#[test]
fn test_screen_5_subagent_inspection_modal() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("harness-project", "claude-3-7-sonnet", "sandbox");
    app.screen_state = ScreenState::Session;
    app.active_subagent_modal = Some(SubagentModalState {
        name: "test-debugger".to_string(),
        prompt: "Diagnose why pipeline state was error.".to_string(),
        is_expanded: false,
        activity_lines: vec![
            "read src/pipeline.rs 50 lines".to_string(),
            "grep initialize src/".to_string(),
        ],
        elapsed_seconds: 15,
        status: "running".to_string(),
        is_running: true,
        activity_scroll: 0,
    });

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(content.contains("prompt"), "Must render prompt label");
    assert!(content.contains("e expand"), "Must render expand toggle hint");
    assert!(content.contains("Diagnose why pipeline state was error"), "Must render prompt body");
    assert!(content.contains("read src/pipeline.rs 50 lines"), "Must render tool activity");
    assert!(content.contains("15s"), "Must render stopwatch elapsed time");
    assert!(content.contains("up/down scroll · e expand prompt · esc close"), "Must render footer hints");
}

#[test]
fn test_tab_mode_cycling() {
    let mut app = App::new("test_proj", "gemini-3.8-flash-tiered", "apfs_clone");
    assert_eq!(app.mode, "agent");

    // Cycle to plan
    let m1 = app.cycle_mode();
    assert_eq!(m1, "plan");
    assert_eq!(app.mode, "plan");

    // Cycle to code
    let m2 = app.cycle_mode();
    assert_eq!(m2, "code");
    assert_eq!(app.mode, "code");

    // Cycle back to agent
    let m3 = app.cycle_mode();
    assert_eq!(m3, "agent");
    assert_eq!(app.mode, "agent");
}

#[test]
fn test_slash_opens_the_command_palette() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.open_palette();
    app.palette_push_char('r');
    assert!(app.input_buffer.is_empty(), "the query never touches the composer");
    assert!(!app.palette_results().is_empty());

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(
        content.contains("/rollback") || content.contains("/react"),
        "Must render matching commands in the palette"
    );
    assert!(content.contains("> r"), "Must render the palette query");
}

#[test]
fn test_help_modal_overlay_rendering() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.show_help = true;

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(
        content.contains("HELP & KEYBINDINGS"),
        "Must render help title"
    );
    assert!(content.contains("/status"), "Must document slash commands");
    assert!(content.contains("ctrl+p"), "Must document the palette binding");
    assert!(content.contains("Cockpit session"), "Must group bindings by surface");
    assert!(!content.contains("[Tab]"), "Must not document removed bracket-style bindings");
}

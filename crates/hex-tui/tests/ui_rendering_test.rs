//! Integration tests for Ratatui frame rendering, layout splits, and widget states.

use ratatui::backend::TestBackend;
use ratatui::Terminal;

use hex_tui::app::{App, ApprovalRequest, Focus};
use hex_tui::ui;

#[test]
fn test_full_tui_layout_rendering() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(content.contains("HEX"), "Buffer must contain ASCII banner");
    assert!(
        content.contains("test_proj"),
        "Buffer must contain project name"
    );
    assert!(
        content.contains("gemini-2.5-pro"),
        "Buffer must contain model name"
    );
    assert!(
        content.contains("did you know"),
        "Buffer must contain native HEX intro"
    );
    assert!(
        content.contains("❯"),
        "Buffer must contain input prompt prefix"
    );
    assert!(
        content.contains("agent"),
        "Buffer prompt footer must display active mode"
    );
}

#[test]
fn test_diff_pane_split_rendering() {
    let backend = TestBackend::new(140, 40);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    let sample_diff = "\
--- a/src/lib.rs
+++ b/src/lib.rs
@@ -1,3 +1,5 @@
 pub fn answer() -> u32 {
-    41
+    42
 }";
    app.load_diff(sample_diff);
    assert!(app.show_diff);

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(
        content.contains("Diff Viewer"),
        "Must render diff pane header"
    );
    assert!(
        content.contains("src/lib.rs"),
        "Must render modified filename"
    );
    assert!(content.contains("42"), "Must render added diff content");
}

#[test]
fn test_approval_modal_overlay_rendering() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.set_approval_request(ApprovalRequest {
        id: "appr_test_99".to_string(),
        risk_level: "R4 - DESTRUCTIVE".to_string(),
        tool_name: "rm -rf".to_string(),
        parameters: "path=/tmp/build".to_string(),
        reason: "Test safety gating".to_string(),
    });
    assert_eq!(app.focus, Focus::Approval);

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(
        content.contains("OPERATOR APPROVAL REQUIRED"),
        "Must render modal title"
    );
    assert!(
        content.contains("rm -rf"),
        "Must render privileged tool name"
    );
    assert!(
        content.contains("R4 - DESTRUCTIVE"),
        "Must render risk level"
    );
    assert!(
        content.contains("[y] Approve"),
        "Must render approve action button"
    );
    assert!(
        content.contains("[n] Reject"),
        "Must render reject action button"
    );
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
    assert!(content.contains("[Tab]"), "Must document keybindings");
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
fn test_slash_command_suggestion_rendering() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.insert_char('/');
    app.insert_char('r');
    assert!(!app.slash_suggestions.is_empty());

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(
        content.contains("/rollback") || content.contains("/react"),
        "Must render slash suggestion popup"
    );
}

#[test]
fn test_collapsible_tool_calls_rendering() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.dialogue_items.clear();
    app.dialogue_scroll = 0;
    app.add_tool_call(
        "tc_render_1",
        "run_tests",
        "suite=unit",
        Some(120),
        Some("OK"),
    );
    app.complete_tool_call("tc_render_1", "12 tests passed", 120, false);
    app.dialogue_scroll = 0;

    // Expanded state
    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buf1 = format!("{:?}", terminal.backend().buffer());
    assert!(buf1.contains("run_tests"), "Must render tool name");
    assert!(buf1.contains("12 tests passed"), "Must render tool result");

    // Collapsed state
    app.toggle_tool_call_collapse(0);
    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buf2 = format!("{:?}", terminal.backend().buffer());
    assert!(buf2.contains("collapsed"), "Must indicate collapsed state");
}

#[test]
fn test_transcript_design_language_and_badges() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("test_proj", "gemini-3.8-flash-tiered", "apfs_clone");
    app.dialogue_items.clear();
    app.dialogue_scroll = 0;

    // Add user message
    app.add_user_message("Refactor the parser loop");

    // Add assistant message with markdown code block
    app.add_assistant_message("Here is the updated implementation:\n```rust\nfn parse_token() -> bool {\n    true\n}\n```");

    // Add tool call
    app.add_tool_call("tc_1", "run_tests", "suite=all", Some(45), Some("OK"));

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    // 1. Badges
    assert!(
        content.contains("❯ You") || content.contains("❯ User"),
        "Must render ❯ You or ❯ User badge"
    );
    assert!(
        content.contains("◈ Assistant"),
        "Must render ◈ Assistant badge"
    );
    assert!(content.contains("run_tests"), "Must render tool call");

    // 2. Code gutter
    assert!(
        content.contains("│"),
        "Must render vertical code gutter line"
    );

    // 3. Prompt box with prefix and placeholder
    assert!(content.contains("❯"), "Must render ❯ input prefix");
    assert!(
        content.contains("Ask me anything") || content.contains("Type your objective"),
        "Must render placeholder text when input is empty"
    );

    // 4. Composer footer dynamic telemetry
    assert!(content.contains("main"), "Must render git branch");
    assert!(
        content.contains("gemini-3.8-flash-tiered"),
        "Must render dynamic model"
    );
    assert!(content.contains("agent"), "Must render active mode");
    assert!(content.contains("enter send"), "Must render hotkey hint");
}

#[test]
fn test_composer_is_locked_to_terminal_bottom() {
    let backend = TestBackend::new(100, 28);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");
    let app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer();

    // The final three rows are always the composer: its top rounded border,
    // input row, and bottom border. This is the invariant that stops streamed
    // content from moving the input away from the operator.
    assert_eq!(buffer[(0, 25)].symbol(), "╭");
    assert_eq!(buffer[(0, 27)].symbol(), "╰");
    let input_row = (0..100).map(|x| buffer[(x, 26)].symbol()).collect::<String>();
    assert!(input_row.contains('❯'));
}

#[test]
fn test_animation_changes_busy_telemetry_and_rotates_prompt() {
    let backend = TestBackend::new(120, 32);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");
    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.is_busy = true;
    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let first = format!("{:?}", terminal.backend().buffer());

    for _ in 0..8 {
        app.advance_animation();
    }
    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let second = format!("{:?}", terminal.backend().buffer());

    assert!(first.contains("Thinking") || first.contains("Analyzing"));
    assert_ne!(
        first, second,
        "shared animation clock must change a visible frame"
    );
}

#[test]
fn test_three_tier_dialogue_hierarchy_and_user_card() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("test_proj", "gemini-3.8-flash-tiered", "apfs_clone");
    app.dialogue_items.clear();
    app.dialogue_scroll = 0;

    // 1. User message
    app.add_user_message("hey what can you do?");

    // 2. Assistant tool execution
    app.add_tool_call(
        "tc_files",
        "list_files",
        "{\"path\": \".\"}",
        Some(120),
        Some("OK"),
    );

    // 3. Assistant response
    app.add_assistant_message("I am HEX, an autonomous engineering agent.");

    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    // User Card assertions
    assert!(content.contains("╭─ ❯ You"), "User card top border with '╭─ ❯ You'");
    assert!(content.contains("hey what can you do?"), "User card contains query content");
    assert!(content.contains("╰"), "User card contains bottom rounded corner '╰'");

    // Assistant Working assertions
    assert!(content.contains("◈ Assistant"), "Must render '◈ Assistant' header");
    assert!(content.contains("├─ ⚙ list_files (.)"), "Tool formatted as tree branch with args");
    assert!(content.contains("0.12s"), "Tool formatted with execution duration");

    // Assistant Response indicator
    assert!(content.contains("▼"), "Assistant response arrow indicator '▼'");
    assert!(content.contains("I am HEX"), "Assistant response content");
}

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
    assert!(content.contains("test_proj"), "Buffer must contain project name");
    assert!(content.contains("gemini-2.5-pro"), "Buffer must contain model name");
    assert!(content.contains("Welcome to HEX"), "Buffer must contain welcome message");
    assert!(content.contains("Command Prompt"), "Buffer must contain input prompt");
    assert!(content.contains("[AGENT]"), "Buffer status bar must display active mode");
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

    assert!(content.contains("Diff Viewer"), "Must render diff pane header");
    assert!(content.contains("src/lib.rs"), "Must render modified filename");
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

    assert!(content.contains("OPERATOR APPROVAL REQUIRED"), "Must render modal title");
    assert!(content.contains("rm -rf"), "Must render privileged tool name");
    assert!(content.contains("R4 - DESTRUCTIVE"), "Must render risk level");
    assert!(content.contains("[y] Approve"), "Must render approve action button");
    assert!(content.contains("[n] Reject"), "Must render reject action button");
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

    assert!(content.contains("HELP & KEYBINDINGS"), "Must render help title");
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

    assert!(content.contains("/rollback") || content.contains("/react"), "Must render slash suggestion popup");
}

#[test]
fn test_collapsible_tool_calls_rendering() {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend).expect("Failed to create TestBackend");

    let mut app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
    app.dialogue_items.clear();
    app.dialogue_scroll = 0;
    app.add_tool_call("tc_render_1", "run_tests", "suite=unit", Some(120), Some("OK"));
    app.complete_tool_call("tc_render_1", "12 tests passed", 120, false);
    app.dialogue_scroll = 0;

    // Expanded state
    terminal.draw(|f| ui::draw(f, &app)).expect("Draw failed");
    let buf1 = format!("{:?}", terminal.backend().buffer());
    assert!(buf1.contains("TOOL CALL"), "Must render tool call label");
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
    assert!(content.contains("❯ User"), "Must render ❯ User badge");
    assert!(content.contains("◈ Assistant"), "Must render ◈ Assistant badge");
    assert!(content.contains("TOOL CALL"), "Must render tool call badge");

    // 2. Code gutter
    assert!(content.contains("│"), "Must render vertical code gutter line");

    // 3. Prompt box with prefix and placeholder
    assert!(content.contains("❯"), "Must render ❯ input prefix");
    assert!(content.contains("Ask me anything") || content.contains("Type your objective"), "Must render placeholder text when input is empty");

    // 4. Composer footer dynamic telemetry
    assert!(content.contains("main"), "Must render git branch");
    assert!(content.contains("gemini-3.8-flash-tiered"), "Must render dynamic model");
    assert!(content.contains("agent"), "Must render active mode");
    assert!(content.contains("enter send"), "Must render hotkey hint");
}

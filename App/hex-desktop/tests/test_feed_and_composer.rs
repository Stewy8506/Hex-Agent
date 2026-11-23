//! Integration and component tests for Feed, Composer, Token Gauge & Reasoning Fold.

use core::prelude::v1::test;
use gpui::*;
use hex_desktop::theme::HermesTheme;
use hex_desktop::ui::{
    format_reasoning_elapsed, format_token_number, line_background, line_foreground, line_prefix,
    provider_dot_color, render_ascii_progress_bar, ApprovalCard, ApprovalCardState, AppShell,
    DiffView, GaugeThreshold, MessageFeed, MessageItem, PromptComposer, ThinkingDisclosure,
    TokenGaugeWidget, ToolCallCard, ToolStatus, UserMessageCard,
};
use hex_protocol::diff::DiffLineType;

#[test]
fn test_token_gauge_formatting_and_thresholds() {
    let theme = HermesTheme::default();

    // 1. Engineering number formatting
    assert_eq!(format_token_number(42), "42");
    assert_eq!(format_token_number(999), "999");
    assert_eq!(format_token_number(1_000), "1k");
    assert_eq!(format_token_number(24_500), "24.5k");
    assert_eq!(format_token_number(128_000), "128k");
    assert_eq!(format_token_number(1_000_000), "1M");
    assert_eq!(format_token_number(2_500_000), "2.5M");

    // 2. ASCII Progress Bar
    assert_eq!(render_ascii_progress_bar(0.0, 16), "▕░░░░░░░░░░░░░░░░▏");
    assert_eq!(render_ascii_progress_bar(0.5, 16), "▕████████░░░░░░░░▏");
    assert_eq!(render_ascii_progress_bar(1.0, 16), "▕████████████████▏");

    // 3. Threshold levels and colors:
    // 0% -> Normal (< 70%)
    let mut gauge = TokenGaugeWidget::new(0, 100_000).with_bar_width(16);
    assert_eq!(gauge.threshold(), GaugeThreshold::Normal);
    assert_eq!(gauge.threshold_color(&theme), theme.accent_blue());
    assert_eq!(gauge.ascii_bar(), "▕░░░░░░░░░░░░░░░░▏");

    // 50% -> Normal (< 70%)
    gauge.set_tokens(50_000, 100_000);
    assert_eq!(gauge.threshold(), GaugeThreshold::Normal);
    assert_eq!(gauge.threshold_color(&theme), theme.accent_blue());
    assert_eq!(gauge.format_display_text(), "50k / 100k (50%)");

    // 85% -> Warning (70%..=90%)
    gauge.set_tokens(85_000, 100_000);
    assert_eq!(gauge.threshold(), GaugeThreshold::Warning);
    assert_eq!(gauge.threshold_color(&theme), theme.warning_amber());
    assert_eq!(gauge.format_display_text(), "85k / 100k (85%)");

    // 95% -> Critical (> 90%)
    gauge.set_tokens(95_000, 100_000);
    assert_eq!(gauge.threshold(), GaugeThreshold::Critical);
    assert_eq!(gauge.threshold_color(&theme), theme.rose_fg());
    assert_eq!(gauge.format_display_text(), "95k / 100k (95%)");
}

#[test]
fn test_reasoning_fold_toggle_and_labels() {
    let mut reasoning = ThinkingDisclosure::new("Evaluating AST graph impact...");

    // Generating state
    reasoning.is_generating = true;
    assert_eq!(reasoning.header_label(), "Thinking...");

    // Elapsed formatting
    assert_eq!(format_reasoning_elapsed(0.2), "briefly");
    assert_eq!(format_reasoning_elapsed(5.8), "6s");
    assert_eq!(format_reasoning_elapsed(45.0), "45s");
    assert_eq!(format_reasoning_elapsed(85.0), "1m 25s");

    // Settled brief (< 1s)
    reasoning.finish(0.4);
    assert_eq!(reasoning.header_label(), "Thought briefly");

    // Settled timed (>= 1s)
    reasoning.finish(6.0);
    assert_eq!(reasoning.header_label(), "Thought for 6s");

    // Settled without timer
    reasoning.elapsed_seconds = None;
    assert_eq!(reasoning.header_label(), "Thought");

    // Toggle expansion
    assert!(!reasoning.is_expanded);
    reasoning.toggle();
    assert!(reasoning.is_expanded);
    reasoning.toggle();
    assert!(!reasoning.is_expanded);
}

#[test]
fn test_diff_line_classification_and_colors() {
    let theme = HermesTheme::default();

    let patch = r#"--- a/crates/hex-core/src/lib.rs
+++ b/crates/hex-core/src/lib.rs
@@ -10,3 +10,4 @@
 fn init() {
-    let old_val = 1;
+    let new_val = 2;
+    let bonus = 3;
 }
"#;

    let diff_view = DiffView::from_patch(patch);
    assert_eq!(diff_view.total_additions(), 2);
    assert_eq!(diff_view.total_deletions(), 1);

    // Verify addition colors (mockup diff sage: #c4e0c7 on rgba tint).
    assert_eq!(line_foreground(DiffLineType::Added, &theme), theme.mockup_diff_add_fg);
    assert_eq!(line_background(DiffLineType::Added, &theme), Some(theme.mockup_diff_add_bg));
    assert_eq!(line_prefix(DiffLineType::Added), "+");

    // Verify deletion colors (mockup diff rose: #d4a9a9, U+2212 minus).
    assert_eq!(line_foreground(DiffLineType::Deleted, &theme), theme.mockup_diff_del_fg);
    assert_eq!(line_background(DiffLineType::Deleted, &theme), Some(theme.mockup_diff_del_bg));
    assert_eq!(line_prefix(DiffLineType::Deleted), "\u{2212}");

    // Verify context colors (prose base on the plain card).
    assert_eq!(line_foreground(DiffLineType::Context, &theme), theme.mockup_text_base);
    assert_eq!(line_background(DiffLineType::Context, &theme), None);
    assert_eq!(line_prefix(DiffLineType::Context), " ");
}

#[test]
fn test_message_feed_and_tool_cards() {
    let mut feed = MessageFeed::new();

    // 1. Add user message
    let uid = feed.add_user_message("Run cargo check on workspace");
    assert_eq!(feed.len(), 1);

    // 2. Start assistant streaming
    let aid = feed.start_assistant_streaming();
    assert_eq!(feed.len(), 2);
    assert!(feed.is_streaming);

    feed.append_streaming_chunk("Executing command in sandbox...\n");
    feed.finish_streaming();
    assert!(!feed.is_streaming);

    // 3. Attach tool call card with diff
    let patch = r#"--- a/test.rs
+++ b/test.rs
@@ -1 +1 @@
-old
+new
"#;
    let diff = DiffView::from_patch(patch);
    let tool_card = ToolCallCard::new("tc_01", "run_command", r#"{"cmd":"cargo test"}"#)
        .with_output("17 passed; 0 failed")
        .with_duration(210)
        .with_diff(diff);

    assert_eq!(tool_card.status, ToolStatus::Success);
    assert_eq!(tool_card.duration_ms, Some(210));
    assert!(tool_card.diff.is_some());

    if let MessageItem::Assistant(ref mut asst) = feed.messages[1] {
        asst.tool_calls.push(tool_card);
        assert_eq!(asst.tool_calls.len(), 1);
        assert_eq!(asst.id, aid);
    } else {
        panic!("Expected assistant message");
    }

    if let MessageItem::User(ref user) = feed.messages[0] {
        assert_eq!(user.id, uid);
        assert_eq!(user.content, "Run cargo check on workspace");
    } else {
        panic!("Expected user message");
    }
}

#[test]
fn test_prompt_composer_state_and_model_pill() {
    let theme = HermesTheme::default();
    let mut composer = PromptComposer::new().with_tokens(10_000, 128_000);

    assert!(!composer.can_send());

    // Model Pill — neutral default until the runtime reports the real model
    assert_eq!(composer.model_pill.display_label(), "Select model");
    assert_eq!(
        provider_dot_color("Anthropic", &theme),
        rgb(0xd97706)
    );
    assert_eq!(
        provider_dot_color("OpenAI", &theme),
        rgb(0x10b981)
    );
    assert_eq!(
        provider_dot_color("Google", &theme),
        rgb(0x3b82f6)
    );

    // Text entry
    composer.set_text("Line 1\nLine 2\nLine 3\nLine 4");
    assert!(composer.can_send());

    // Clearing empties the buffer
    composer.clear();
    assert_eq!(composer.input_text, "");
    assert!(!composer.can_send());
}

#[gpui::test]
async fn test_full_app_shell_in_gpui_context(cx: &mut TestAppContext) {
    cx.update(|cx| {
        HermesTheme::init(cx);
    });

    let shell_entity = cx.new(|_cx| {
        let mut shell = AppShell::with_workspace("Harness", "experimental-features");
        let _session_id = shell.new_chat("First Exploration");
        shell.feed.add_user_message("Hello from GPUI!");
        shell.feed.add_assistant_message("Hello! I am HEX running inside GPUI.");
        shell
    });

    cx.update(|cx| {
        let shell = shell_entity.read(cx);
        assert!(!shell.sidebar_collapsed);
        assert_eq!(shell.feed.len(), 2);
        assert_eq!(shell.sidebar.workspace_name, "Harness");
        assert_eq!(shell.sidebar.git_branch, "experimental-features");
        assert!(shell.active_session_id.is_some());
    });

    // Test sidebar toggle
    shell_entity.update(cx, |shell, _cx| {
        shell.toggle_sidebar();
        assert!(shell.sidebar_collapsed);
        shell.toggle_sidebar();
        assert!(!shell.sidebar_collapsed);
    });
}

#[test]
fn test_approval_card_lifecycle_in_feed() {
    assert_eq!(ApprovalCardState::Pending.label(), "Needs you");
    assert_eq!(ApprovalCardState::Approved.label(), "Approved");
    assert_eq!(ApprovalCardState::Denied.label(), "Denied");

    let mut feed = MessageFeed::new();
    feed.add_approval_card(ApprovalCard::new("appr_1", "bash", "rm -rf build/", "R4"));
    let last = feed.messages.last().unwrap();
    assert!(
        matches!(last, MessageItem::Approval(card)
            if card.approval_id == "appr_1"
                && card.tool_name == "bash"
                && card.state == ApprovalCardState::Pending),
        "expected pending approval card, got {last:?}"
    );

    assert!(feed.update_approval_state("appr_1", ApprovalCardState::Approved));
    assert!(
        matches!(feed.messages.last(), Some(MessageItem::Approval(card))
            if card.state == ApprovalCardState::Approved)
    );
    assert!(!feed.update_approval_state("appr_missing", ApprovalCardState::Denied));
}

#[test]
fn test_update_tool_result_attaches_diff_view() {
    let mut feed = MessageFeed::new();
    feed.add_tool_call(ToolCallCard::new("call_1", "write_file", r#"{"path":"x.txt"}"#));
    feed.update_tool_result(
        "call_1",
        "wrote x.txt",
        false,
        Some("--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-old\n+new"),
    );
    let tool = match feed.messages.last() {
        Some(MessageItem::Assistant(view)) => &view.tool_calls[0],
        other => panic!("expected assistant with tool card, got {other:?}"),
    };
    assert_eq!(tool.status, ToolStatus::Success);
    assert_eq!(tool.output_text.as_deref(), Some("wrote x.txt"));
    assert!(tool.diff.is_some(), "diff should be attached as a DiffView");

    // Error outcome marks failure; a later call without diff leaves the
    // already-attached diff intact.
    feed.update_tool_result("call_1", "failed", true, None);
    let tool = match feed.messages.last() {
        Some(MessageItem::Assistant(view)) => &view.tool_calls[0],
        other => panic!("expected assistant with tool card, got {other:?}"),
    };
    assert_eq!(tool.status, ToolStatus::Failed);
    assert!(tool.diff.is_some());
    assert!(!feed.messages.is_empty());
}

#[test]
fn test_user_message_with_attachments_keeps_paths() {
    let mut feed = MessageFeed::new();
    feed.add_user_message_with_attachments(
        "review these",
        vec!["C:/repo/src/main.rs".to_string(), "C:/repo/README.md".to_string()],
    );
    let card = match feed.messages.last() {
        Some(MessageItem::User(card)) => card,
        other => panic!("expected user message, got {other:?}"),
    };
    assert_eq!(card.content, "review these");
    assert_eq!(card.attachments.len(), 2);
    assert_eq!(card.attachments[0], "C:/repo/src/main.rs");
}

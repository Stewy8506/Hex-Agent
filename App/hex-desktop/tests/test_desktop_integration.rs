//! Comprehensive Desktop Cockpit Integration Test Suite.
//!
//! Validates:
//! 1. Mock ACP stdio pipe handshake and session creation.
//! 2. Streaming notifications updating active session and message feed.
//! 3. Tool approval workflow: 3-way radio options and 15m lease auto-approval payload.
//! 4. Command Palette modal: fuzzy search across actions, navigation, and sessions.
//! 5. Model Picker modal: provider grouping, filtering, and selection.
//! 6. macOS shortcuts and native application menus in GPUI test context.

use core::prelude::v1::test;
use gpui::*;
use hex_desktop::acp_bridge::{AcpBridge, AcpEvent};
use hex_desktop::menu::{application_menus, init_menus};
use hex_desktop::shortcuts::{desktop_keybindings, init_shortcuts};
use hex_desktop::theme::HermesTheme;
use hex_desktop::ui::modals::{
    ActiveModal, CommandPaletteModal, ModelEntry, ModelPickerModal, PaletteAction, ToolApprovalChoice,
};
use hex_desktop::ui::session_list::SessionItem;
use hex_desktop::ui::shell::AppShell;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

#[tokio::test]
async fn test_mock_acp_handshake_and_stdio() {
    let (client_read, mut server_write) = tokio::io::duplex(4096);
    let (server_read, client_write) = tokio::io::duplex(4096);

    let bridge = AcpBridge::new();
    let mut events = bridge.subscribe();

    // Mock ACP backend server thread
    tokio::spawn(async move {
        let mut lines = BufReader::new(server_read).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let req: serde_json::Value = serde_json::from_str(&line).unwrap();
            let method = req.get("method").and_then(|v| v.as_str()).unwrap();
            let id = req.get("id").cloned();

            match method {
                "initialize" => {
                    let resp = serde_json::json!({
                        "jsonrpc": "2.0",
                        "id": id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {
                                "session": {
                                    "streaming": true,
                                    "cancel": true,
                                    "history": true
                                },
                                "tools": {
                                    "listChanged": false
                                }
                            },
                            "serverInfo": {
                                "name": "harness_core",
                                "version": "1.0.0"
                            }
                        }
                    });
                    server_write
                        .write_all((serde_json::to_string(&resp).unwrap() + "\n").as_bytes())
                        .await
                        .unwrap();
                }
                "session/new" => {
                    let resp = serde_json::json!({
                        "jsonrpc": "2.0",
                        "id": id,
                        "result": {
                            "sessionId": "ses_integration_42",
                            "cwd": "/workspace/project",
                            "model": "claude-3-7-sonnet"
                        }
                    });
                    server_write
                        .write_all((serde_json::to_string(&resp).unwrap() + "\n").as_bytes())
                        .await
                        .unwrap();
                }
                _ => {}
            }
        }
    });

    // Verify connect_streams establishes ACP handshake cleanly
    bridge
        .connect_streams(client_read, client_write)
        .await
        .expect("handshake failed");
    assert!(bridge.is_connected());

    // Verify Connected event emitted
    let evt = events.recv().await.unwrap();
    assert_eq!(
        evt,
        AcpEvent::Connected {
            server_name: "harness_core".to_string(),
            server_version: "1.0.0".to_string(),
        }
    );

    // Verify session/new roundtrip
    let session_id = bridge.new_session(None, None).await.unwrap();
    assert_eq!(session_id, "ses_integration_42");
}

#[test]
fn test_streaming_notifications_into_app_shell() {
    let mut shell = AppShell::new();
    let session_id = "ses_stream_test";

    // 1. Initial token chunks arrive from ACP
    shell.handle_acp_event(AcpEvent::TokenChunk {
        session_id: session_id.to_string(),
        text: "The quick brown fox ".to_string(),
    });
    assert!(shell.feed.is_streaming);
    assert_eq!(shell.feed.len(), 1);

    // 2. Subsequent chunks append seamlessly
    shell.handle_acp_event(AcpEvent::TokenChunk {
        session_id: session_id.to_string(),
        text: "jumps over the lazy dog.".to_string(),
    });
    assert!(shell.feed.is_streaming);

    // 3. Turn completes
    shell.handle_acp_event(AcpEvent::TurnComplete {
        session_id: session_id.to_string(),
    });
    assert!(!shell.feed.is_streaming);

    // 4. Verify Assistant message in feed contains complete concatenated prose
    match &shell.feed.messages[0] {
        hex_desktop::ui::MessageItem::Assistant(view) => {
            assert_eq!(view.content, "The quick brown fox jumps over the lazy dog.");
            assert!(!view.is_streaming);
        }
        _ => panic!("Expected Assistant message in feed"),
    }
}

#[tokio::test]
async fn test_tool_approval_workflow_with_15m_lease() {
    let (client_read, mut server_write) = tokio::io::duplex(4096);
    let (server_read, client_write) = tokio::io::duplex(4096);

    let bridge = AcpBridge::new();

    // Spawn server handling initialize and approval decision
    tokio::spawn(async move {
        let mut lines = BufReader::new(server_read).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let req: serde_json::Value = serde_json::from_str(&line).unwrap();
            let method = req.get("method").and_then(|v| v.as_str()).unwrap();
            let id = req.get("id").cloned();

            match method {
                "initialize" => {
                    let resp = serde_json::json!({
                        "jsonrpc": "2.0",
                        "id": id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {
                                "session": { "streaming": true, "cancel": true, "history": true },
                                "tools": { "listChanged": false }
                            },
                            "serverInfo": { "name": "harness_core", "version": "1.0.0" }
                        }
                    });
                    server_write
                        .write_all((serde_json::to_string(&resp).unwrap() + "\n").as_bytes())
                        .await
                        .unwrap();
                }
                "session/approvalDecision" => {
                    let params = req.get("params").unwrap();
                    let approval_id = params.get("approvalId").and_then(|v| v.as_str()).unwrap();
                    let decision = params.get("decision").and_then(|v| v.as_str()).unwrap();
                    let ttl = params.get("leaseTtlSeconds").and_then(|v| v.as_u64());

                    assert_eq!(approval_id, "appr_777");
                    assert_eq!(decision, "approve_scope");
                    assert_eq!(ttl, Some(900));
                    assert!(params.get("reason").is_none());

                    let resp = serde_json::json!({
                        "jsonrpc": "2.0",
                        "id": id,
                        "result": {
                            "approvalId": approval_id,
                            "status": "applied",
                            "decision": decision,
                            "applied": true,
                            "outcome": "APPROVE",
                            "leaseId": "lease_777",
                            "leaseTtlSeconds": ttl
                        }
                    });
                    server_write
                        .write_all((serde_json::to_string(&resp).unwrap() + "\n").as_bytes())
                        .await
                        .unwrap();
                }
                _ => {}
            }
        }
    });

    bridge
        .connect_streams(client_read, client_write)
        .await
        .unwrap();

    let mut shell = AppShell::new().with_acp_bridge(bridge.clone());

    // 1. Inbound approval request opens modal
    shell.handle_acp_event(AcpEvent::ApprovalRequest {
        session_id: "ses_1".to_string(),
        approval_id: "appr_777".to_string(),
        tool_name: "bash".to_string(),
        risk_level: "R4".to_string(),
        command: "cargo clippy --fix".to_string(),
        description: Some("Automated rust fix".to_string()),
    });

    match &shell.active_modal {
        ActiveModal::Approval(modal) => {
            assert_eq!(modal.request_id, "appr_777");
            assert_eq!(modal.tool_name, "bash");
            assert_eq!(modal.command, "cargo clippy --fix");
            assert_eq!(modal.risk_class, "R4");
            assert_eq!(modal.choice, ToolApprovalChoice::Once);
            assert!(!modal.lease_15m);
        }
        _ => panic!("Expected active approval modal"),
    }

    // 2. The 15m lease is real only for the scoped choice (B27/B30).
    if let ActiveModal::Approval(ref mut modal) = shell.active_modal {
        modal.toggle_lease();
        assert!(modal.lease_15m);

        let once = modal.confirm();
        assert_eq!(once.choice, ToolApprovalChoice::Once);
        assert!(!once.lease_15m);
        assert_eq!(once.lease_seconds(), None);

        modal.set_choice(ToolApprovalChoice::Session);
        let result = modal.confirm();
        assert_eq!(result.choice, ToolApprovalChoice::Session);
        assert!(result.lease_15m);
        assert_eq!(result.lease_seconds(), Some(900));

        // Send decision to backend bridge
        let resp = bridge
            .send_approval_decision(&result.request_id, result.choice, result.lease_seconds())
            .await
            .expect("decision dispatch failed");
        assert_eq!(resp.approval_id, "appr_777");
        assert_eq!(resp.decision, "approve_scope");
        assert_eq!(resp.status, "applied");
        assert_eq!(resp.applied, Some(true));
        assert_eq!(resp.outcome.as_deref(), Some("APPROVE"));
        assert_eq!(resp.lease_id.as_deref(), Some("lease_777"));
        assert_eq!(resp.lease_ttl_seconds, Some(900));
    }

    shell.dismiss_modal();
    assert_eq!(shell.active_modal, ActiveModal::None);
}

#[test]
fn test_command_palette_fuzzy_search_and_actions() {
    let mut palette = CommandPaletteModal::new();
    let sessions = vec![
        SessionItem::new("ses_1", "Feature: GPUI Metal Cockpit"),
        SessionItem::new("ses_2", "Bugfix: Timeline Rewind CAS token"),
        SessionItem::new("ses_3", "Refactor: Active Graphify AST indexing"),
    ];
    palette.populate_sessions(&sessions);

    // 1. Search for actions ("compact")
    palette.set_query("compact");
    let results = palette.filtered_items();
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].action, PaletteAction::CompactContext);

    // 2. Search for sessions ("timeline")
    palette.set_query("timeline");
    let session_results = palette.filtered_items();
    assert_eq!(session_results.len(), 1);
    assert_eq!(
        session_results[0].action,
        PaletteAction::SelectSession("ses_2".to_string())
    );

    // 3. Search for navigation ("settings")
    palette.set_query("settings");
    let nav_results = palette.filtered_items();
    assert_eq!(nav_results.len(), 1);
    assert_eq!(nav_results[0].action, PaletteAction::OpenSettings);

    // 4. Keyboard selection and execution
    palette.set_query("");
    palette.handle_key("down"); // Move to second item (Clear History)
    let action = palette.handle_key("enter").unwrap();
    assert_eq!(action, PaletteAction::ClearHistory);
}

#[test]
fn test_model_picker_workflow() {
    // B32: the picker is built from the live backend catalog, not a hardcoded list.
    let catalog = vec![
        ModelEntry::new("claude-3-7-sonnet", "Claude 3.7 Sonnet", "Anthropic", false),
        ModelEntry::new("gpt-4o", "GPT-4o", "OpenAI", false),
        ModelEntry::new("o3-mini", "o3-mini", "OpenAI", false),
        ModelEntry::new("qwen2.5-coder-32b", "Qwen (Ollama)", "Local", true),
    ];
    let mut picker = ModelPickerModal::new("claude-3-7-sonnet", catalog);
    assert_eq!(picker.selected_model_id, "claude-3-7-sonnet");

    // Filter by provider ("openai")
    picker.set_search("openai");
    let openai_models = picker.filtered_models();
    assert_eq!(openai_models.len(), 2);
    assert_eq!(openai_models[0].id, "gpt-4o");
    assert_eq!(openai_models[1].id, "o3-mini");

    // Select o3-mini via navigation
    picker.handle_key("down");
    let selected = picker.handle_key("enter").unwrap();
    assert_eq!(selected, "o3-mini");
    assert_eq!(picker.selected_model_id, "o3-mini");

    // Local origin tag comes from the catalog entry
    picker.set_search("qwen");
    let local = picker.filtered_models();
    assert_eq!(local.len(), 1);
    assert!(local[0].is_local);
}

#[gpui::test]
async fn test_desktop_cockpit_shortcuts_and_menus(cx: &mut TestAppContext) {
    // 1. Test theme and global registrations
    cx.update(|cx| {
        HermesTheme::init(cx);
        init_shortcuts(cx);
        init_menus(cx);

        let theme = HermesTheme::get(cx);
        assert_eq!((theme.mockup_canvas.r * 255.0).round() as u8, 0x14);
        assert_eq!((theme.mockup_surface.r * 255.0).round() as u8, 0x17);
    });

    // 2. Test keybindings structure
    let bindings = desktop_keybindings();
    assert!(bindings.len() >= 10);

    // 3. Test application menus
    let menus = application_menus();
    assert_eq!(menus.len(), 4);
    assert_eq!(menus[0].name.as_ref(), "HEX Desktop");
    assert_eq!(menus[1].name.as_ref(), "File");
    assert_eq!(menus[2].name.as_ref(), "Edit");
    assert_eq!(menus[3].name.as_ref(), "View");

    // 4. Test AppShell state (P6: the legacy DesktopWindow root view was
    // retired; ChatView owns layout and rendering).
    let shell_entity = cx.new(|_cx| AppShell::with_workspace("test-workspace", "main"));

    cx.update(|cx| {
        let shell = shell_entity.read(cx);
        assert_eq!(shell.sidebar.workspace_name, "test-workspace");
        assert!(!shell.sidebar_collapsed);
        assert_eq!(shell.active_modal, ActiveModal::None);
    });

    // 5. Test opening modal on AppShell inside GPUI entity
    shell_entity.update(cx, |shell, _cx| {
        shell.open_command_palette();
        assert!(matches!(shell.active_modal, ActiveModal::Palette(_)));
    });

    cx.update(|cx| {
        let shell = shell_entity.read(cx);
        assert!(shell.active_modal.is_open());
    });

    // 6. Test dismissing modal
    shell_entity.update(cx, |shell, _cx| {
        shell.dismiss_modal();
        assert_eq!(shell.active_modal, ActiveModal::None);
    });
}

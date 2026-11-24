//! F02 acceptance test: the real desktop chat vertical slice.
//!
//! Launches the real `ChatView` entry point in a GPUI test window, types through
//! the real keystroke/input-handler path, and talks to a scripted ACP model over
//! the canonical `session/update` wire shapes (tests/fixtures/acp_wire). Exercises:
//! submit → streamed completion with a tool card, copy, new chat, palette-driven
//! session switching with draft preservation, escape cancellation, and a failed
//! turn error state. Hermes reference: `components/assistant-ui/thread/` +
//! `app/chat/composer/` (Enter submits, Cmd+C copies the answer, Esc cancels).

#![recursion_limit = "512"]

// Import the built-in test attribute explicitly: `use gpui::*` also brings
// `gpui::test` into scope, which would otherwise re-expand the `#[test]`
// attribute emitted by the macro and recurse forever.
use core::prelude::v1::test;
use gpui::*;
use hex_desktop::acp_bridge::AcpBridge;
use hex_desktop::chat::{init_app, ChatView};
use hex_desktop::ui::composer::{GlowCardPaint, GlowCardProbe};
use hex_desktop::ui::feed::{FeedPaintProbe, MessageItem};
use hex_desktop::ui::modals::ActiveModal;
use hex_desktop::ui::sidebar::ProviderState;
use hex_desktop::ui::tool_card::ToolStatus;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, DuplexStream};
use tokio::sync::Mutex as AsyncMutex;

#[derive(Default)]
struct MockState {
    session_counter: AtomicUsize,
    cancel_flag: AtomicBool,
    cancel_requests: AtomicUsize,
    prompts: StdMutex<Vec<(String, String)>>,
}

async fn send_line(writer: &Arc<AsyncMutex<DuplexStream>>, value: Value) {
    let mut line = serde_json::to_string(&value).unwrap();
    line.push('\n');
    let mut w = writer.lock().await;
    w.write_all(line.as_bytes()).await.unwrap();
    w.flush().await.unwrap();
}

async fn send_update(writer: &Arc<AsyncMutex<DuplexStream>>, params: Value) {
    send_line(
        writer,
        json!({"jsonrpc": "2.0", "method": "session/update", "params": params}),
    )
    .await;
}

async fn handle_request(
    method: &str,
    id: Option<Value>,
    params: Value,
    writer: Arc<AsyncMutex<DuplexStream>>,
    state: Arc<MockState>,
) {
    match method {
        "initialize" => {
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "session": {"streaming": true, "cancel": true, "history": true},
                            "tools": {"listChanged": false}
                        },
                        "serverInfo": {"name": "harness_core_mock", "version": "1.0.0"}
                    }
                }),
            )
            .await;
        }
        "session/new" => {
            let n = state.session_counter.fetch_add(1, Ordering::SeqCst) + 1;
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {
                        "sessionId": format!("ses_test_{n}"),
                        "cwd": params.get("cwd").and_then(|v| v.as_str()).unwrap_or("/workspace"),
                        "model": "claude-3-7-sonnet",
                        "provider": "Anthropic"
                    }
                }),
            )
            .await;
        }
        "session/prompt" => {
            let sid = params["sessionId"].as_str().unwrap().to_string();
            let text = params["prompt"].as_str().unwrap().to_string();
            state.prompts.lock().unwrap().push((sid.clone(), text.clone()));

            if text.contains("fail") {
                send_update(
                    &writer,
                    json!({"sessionId": sid, "status": "Failed", "updateType": "stream_end"}),
                )
                .await;
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "result": {"sessionId": sid, "status": "failed", "content": [], "taskId": "tsk_fail"}
                    }),
                )
                .await;
            } else if text.contains("slow") {
                send_update(
                    &writer,
                    json!({"sessionId": sid, "delta": "Working...", "updateType": "agent_response"}),
                )
                .await;
                // Hold the turn open until the client cancels (30s ceiling so a
                // broken cancel path fails the test deadline instead of passing).
                for _ in 0..1500 {
                    if state.cancel_flag.load(Ordering::SeqCst) {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
                send_update(
                    &writer,
                    json!({"sessionId": sid, "status": "Cancelled", "updateType": "stream_end"}),
                )
                .await;
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "result": {"sessionId": sid, "status": "cancelled", "content": [], "taskId": "tsk_slow"}
                    }),
                )
                .await;
            } else {
                // Canonical streamed turn (tests/fixtures/acp_wire/turn_streamed.jsonl).
                send_update(
                    &writer,
                    json!({"sessionId": sid, "delta": "Hello", "updateType": "agent_response"}),
                )
                .await;
                send_update(
                    &writer,
                    json!({"sessionId": sid, "delta": " world", "updateType": "agent_response"}),
                )
                .await;
                send_update(
                    &writer,
                    json!({
                        "sessionId": sid, "updateType": "tool",
                        "tool": {"callId": "tc_list_1", "toolId": "list_files", "eventType": "start",
                                 "durationMs": 0, "payload": "{\"path\": \".\"}"}
                    }),
                )
                .await;
                send_update(
                    &writer,
                    json!({
                        "sessionId": sid, "updateType": "tool",
                        "tool": {"callId": "tc_list_1", "toolId": "list_files", "eventType": "complete",
                                 "durationMs": 12, "payload": "{\"files\": [\"a.py\"]}"}
                    }),
                )
                .await;
                send_update(
                    &writer,
                    json!({"sessionId": sid, "status": "Ready", "updateType": "stream_end"}),
                )
                .await;
                send_update(
                    &writer,
                    json!({"sessionId": sid, "status": "Ready", "updateType": "status"}),
                )
                .await;
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "result": {
                            "sessionId": sid, "status": "completed", "taskId": "tsk_ok",
                            "content": [{"type": "text", "text": "Hello world"}]
                        }
                    }),
                )
                .await;
            }
        }
        "session/cancel" => {
            state.cancel_requests.fetch_add(1, Ordering::SeqCst);
            state.cancel_flag.store(true, Ordering::SeqCst);
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {"sessionId": params["sessionId"], "cancelled": true}
                }),
            )
            .await;
        }
        _ => {}
    }
}

async fn run_mock_server(reader: DuplexStream, writer: DuplexStream, state: Arc<MockState>) {
    let writer = Arc::new(AsyncMutex::new(writer));
    let mut lines = BufReader::new(reader).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let req: Value = serde_json::from_str(&line).unwrap();
        let method = req["method"].as_str().unwrap().to_string();
        let id = req.get("id").cloned();
        let params = req.get("params").cloned().unwrap_or(json!({}));
        let writer = writer.clone();
        let state = state.clone();
        // Handle concurrently so session/cancel lands while a slow prompt is open.
        tokio::spawn(async move { handle_request(&method, id, params, writer, state).await });
    }
}

fn snap<R>(vc: &VisualTestContext, view: &Entity<ChatView>, f: impl FnOnce(&ChatView) -> R) -> R {
    vc.read(|app| f(view.read(app)))
}

fn pump_until(
    vc: &mut VisualTestContext,
    view: &Entity<ChatView>,
    what: &str,
    mut cond: impl FnMut(&ChatView) -> bool,
) {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        vc.run_until_parked();
        if snap(vc, view, &mut cond) {
            return;
        }
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// (composer focused, modal focused)
fn focus_state(vc: &mut VisualTestContext, view: &Entity<ChatView>) -> (bool, bool) {
    vc.run_until_parked();
    vc.update(|window, app| {
        let v = view.read(app);
        (v.is_composer_focused(window), v.is_modal_focused(window))
    })
}

fn last_notice(view: &ChatView) -> Option<String> {
    view.shell.feed.messages.iter().rev().find_map(|m| match m {
        MessageItem::SystemNotice(n) => Some(n.clone()),
        _ => None,
    })
}

fn last_assistant(view: &ChatView) -> Option<&hex_desktop::ui::message::AssistantMessageView> {
    view.shell.feed.messages.iter().rev().find_map(|m| match m {
        MessageItem::Assistant(a) => Some(a),
        _ => None,
    })
}

fn clear_glow_probe(vc: &mut VisualTestContext) {
    vc.update(|_window, app| {
        if let Some(probe) = app.try_global::<GlowCardProbe>() {
            probe.0.lock().unwrap().clear();
        }
    });
}

/// Forces a repaint and returns the glow-card paints recorded since the last
/// clear. The test executor paints exactly one frame per notify (the breathe
/// animation's next frame is 5s out, so `run_until_parked` stops after the
/// notified frame), so one entry per rendered glow card.
fn glow_paints(vc: &mut VisualTestContext, view: &Entity<ChatView>) -> Vec<GlowCardPaint> {
    vc.update(|_window, app| {
        view.update(app, |_, cx| cx.notify());
    });
    vc.run_until_parked();
    vc.read(|app| {
        app.try_global::<GlowCardProbe>()
            .map(|p| p.0.lock().unwrap().clone())
            .unwrap_or_default()
    })
}

fn assert_px_close(actual: Pixels, expected: Pixels) {
    assert!(
        (actual - expected).abs() < px(0.5),
        "expected ~{expected:?}, got {actual:?}"
    );
}

#[gpui::test]
async fn test_chat_vertical_slice(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();

    let (client_read, server_write) = tokio::io::duplex(64 * 1024);
    let (server_read, client_write) = tokio::io::duplex(64 * 1024);
    let state = Arc::new(MockState::default());
    rt.spawn(run_mock_server(server_read, server_write, state.clone()));

    let bridge = AcpBridge::new();
    rt.block_on(bridge.connect_streams(client_read, client_write))
        .expect("ACP handshake");
    assert!(bridge.is_connected());

    // Real entry point: same init + root view as main.rs.
    cx.update(|cx| {
        init_app(cx);
        cx.set_global(GlowCardProbe::default());
        cx.set_global(FeedPaintProbe::default());
    });
    let workspace = std::env::temp_dir();
    let (view, vc) = cx.add_window_view(|_window, cx| {
        ChatView::new(bridge.clone(), rt.handle().clone(), workspace.clone(), cx)
    });

    // 1. Backend session adopted with real model identity from session/new.
    pump_until(vc, &view, "initial session adoption", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_1")
    });
    snap(vc, &view, |v| {
        assert_eq!(v.shell.sidebar.provider_status.state, ProviderState::Connected);
        assert_eq!(v.shell.sidebar.provider_status.provider_name, "Anthropic");
        assert_eq!(v.shell.composer.model_pill.model_name, "claude-3-7-sonnet");
        assert_eq!(v.shell.sidebar.session_list.sessions.len(), 1);
        assert_eq!(v.shell.sidebar.session_list.sessions[0].title, "New Chat");
    });

    // 1b. Composer parity: the Home screen renders exactly one glow card at
    // the mockup's 640px Start width, with the halo overhanging it by the
    // mockup's 2px inset on every side.
    clear_glow_probe(vc);
    let paints = glow_paints(vc, &view);
    assert_eq!(paints.len(), 1, "Home must render exactly one glow card");
    assert_px_close(paints[0].card.size.width, px(640.0));
    assert_px_close(paints[0].halo.size.width, px(644.0));
    assert_px_close(paints[0].halo.size.height, paints[0].card.size.height + px(4.0));

    // 2. Type through the real input system and submit with Enter.
    vc.simulate_input("Hello HEX");
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "Hello HEX");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "streamed turn completion", |v| {
        !v.shell.composer.is_streaming
            && last_assistant(v)
                .map(|a| {
                    a.content == "Hello world"
                        && a.tool_calls.len() == 1
                        && a.tool_calls[0].status == ToolStatus::Success
                })
                .unwrap_or(false)
    });
    snap(vc, &view, |v| {
        assert!(matches!(&v.shell.feed.messages[0], MessageItem::User(u) if u.content == "Hello HEX"));
        let assistant = last_assistant(v).unwrap();
        assert!(!assistant.is_streaming);
        assert_eq!(assistant.model_name, "claude-3-7-sonnet");
        assert_eq!(assistant.tool_calls.len(), 1);
        assert_eq!(assistant.tool_calls[0].call_id, "tc_list_1");
        assert_eq!(assistant.tool_calls[0].status, ToolStatus::Success);
        assert!(assistant.tool_calls[0]
            .output_text
            .as_deref()
            .unwrap()
            .contains("a.py"));
        // Hermes-style auto title from the first prompt.
        assert_eq!(v.shell.sidebar.session_list.sessions[0].title, "Hello HEX");
        assert_eq!(v.composer_text(), "");
    });

    // 2b. Composer parity: the Session screen also renders exactly one glow
    // card, now at the mockup's 560px session width (fixes D4's missing
    // session glow).
    clear_glow_probe(vc);
    let paints = glow_paints(vc, &view);
    assert_eq!(paints.len(), 1, "Session must render exactly one glow card");
    assert_px_close(paints[0].card.size.width, px(560.0));

    // 2c. Feed parity: the session feed paints the mockup's 28px canvas fade
    // across its top edge, spanning the whole feed area (D7's missing fade).
    vc.update(|_window, app| {
        if let Some(probe) = app.try_global::<FeedPaintProbe>() {
            probe.0.lock().unwrap().clear();
        }
        view.update(app, |_, cx| cx.notify());
    });
    vc.run_until_parked();
    let fades = vc.read(|app| {
        app.try_global::<FeedPaintProbe>()
            .map(|p| p.0.lock().unwrap().clone())
            .unwrap_or_default()
    });
    assert_eq!(
        fades.len(),
        1,
        "session feed must paint exactly one fade overlay"
    );
    assert_px_close(fades[0].size.height, px(28.0));
    assert!(
        fades[0].size.width >= px(560.0),
        "fade must span the feed area, got {:?}",
        fades[0].size.width
    );

    // 3. Cmd+C copies the assistant answer (composer focused, no selection).
    vc.simulate_keystrokes("cmd-c");
    assert_eq!(
        vc.read_from_clipboard().unwrap().text().unwrap(),
        "Hello world"
    );

    // 4. Cmd+N creates a second real backend session.
    vc.simulate_keystrokes("cmd-n");
    pump_until(vc, &view, "second session adoption", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_2")
    });
    snap(vc, &view, |v| {
        assert!(v.shell.feed.is_empty());
        assert_eq!(v.shell.sidebar.session_list.sessions.len(), 2);
    });

    // 5. Draft typed in session 2 must survive switching away and back.
    vc.simulate_input("draft for later");
    assert_eq!(
        snap(vc, &view, |v| v.composer_text().to_string()),
        "draft for later"
    );

    // 6. Cmd+K palette: typing routes to the query, Enter switches session.
    vc.simulate_keystrokes("cmd-k");
    assert!(snap(vc, &view, |v| matches!(v.shell.active_modal, ActiveModal::Palette(_))));
    // Opening a modal moves focus off the composer onto the overlay.
    assert!(focus_state(vc, &view) == (false, true), "modal should own focus");
    vc.simulate_input("Hello");
    snap(vc, &view, |v| {
        let ActiveModal::Palette(p) = &v.shell.active_modal else {
            panic!("palette should be open");
        };
        assert_eq!(p.query, "Hello");
        // Keystrokes went to the palette, not the composer editor.
        assert_eq!(v.composer_text(), "draft for later");
    });
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "palette switch to first session", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_1")
    });
    // Closing the modal returns focus to the composer.
    assert!(focus_state(vc, &view) == (true, false), "composer should regain focus");
    snap(vc, &view, |v| {
        assert_eq!(v.shell.active_modal, ActiveModal::None);
        assert_eq!(v.shell.feed.len(), 2, "feed: {:?}", v.shell.feed.messages);
        assert_eq!(last_assistant(v).unwrap().content, "Hello world");
        assert_eq!(v.composer_text(), "");
    });

    // 7. Switch back via palette navigation; the draft is restored.
    vc.simulate_keystrokes("cmd-k");
    vc.simulate_input("New Chat");
    // Items: "New Chat" command first, then the "New Chat" session — move down.
    vc.simulate_keystrokes("down enter");
    pump_until(vc, &view, "palette switch back to second session", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_2")
    });
    snap(vc, &view, |v| {
        assert_eq!(v.composer_text(), "draft for later");
        assert!(v.shell.feed.is_empty());
    });

    // 7b. Escape dismisses the palette without touching the draft; up/down
    //     inside the palette never move the composer caret or edit text.
    vc.simulate_keystrokes("cmd-k down up backspace escape");
    snap(vc, &view, |v| {
        assert_eq!(v.shell.active_modal, ActiveModal::None);
        assert_eq!(v.composer_text(), "draft for later");
    });
    assert!(focus_state(vc, &view) == (true, false));

    // 8. Select-all + backspace clears the draft through real editor actions.
    vc.simulate_keystrokes("cmd-a backspace");
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "");

    // 9. Escape cancels a running turn; the runtime acknowledges and the feed
    //    shows the authoritative cancelled state.
    vc.simulate_input("slow task");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "slow turn streaming", |v| {
        v.shell.composer.is_streaming
            && last_assistant(v).map(|a| a.content.as_str()) == Some("Working...")
    });
    vc.simulate_keystrokes("escape");
    pump_until(vc, &view, "turn cancellation", |v| {
        !v.shell.composer.is_streaming && last_notice(v).as_deref() == Some("Turn cancelled.")
    });
    assert!(state.cancel_flag.load(Ordering::SeqCst));
    assert!(state.cancel_requests.load(Ordering::SeqCst) >= 1);

    // 10. Failed turn surfaces an honest error state and re-enables the composer.
    vc.simulate_input("fail please");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "failed turn error state", |v| {
        !v.shell.composer.is_streaming
            && last_notice(v).map(|n| n.contains("failed")).unwrap_or(false)
    });
    snap(vc, &view, |v| {
        assert_eq!(
            last_notice(v).unwrap(),
            "Turn ended with status \"failed\"."
        );
        assert_eq!(v.shell.sidebar.provider_status.state, ProviderState::Connected);
    });
    vc.simulate_input("ok");
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "ok");

    // The scripted model saw exactly the prompts the UI submitted, per session.
    let prompts = state.prompts.lock().unwrap().clone();
    assert_eq!(
        prompts,
        vec![
            ("ses_test_1".to_string(), "Hello HEX".to_string()),
            ("ses_test_2".to_string(), "slow task".to_string()),
            ("ses_test_2".to_string(), "fail please".to_string()),
        ]
    );
}

#[gpui::test]
async fn test_cmd_w_closes_window(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let (client_read, server_write) = tokio::io::duplex(64 * 1024);
    let (server_read, client_write) = tokio::io::duplex(64 * 1024);
    let state = Arc::new(MockState::default());
    rt.spawn(run_mock_server(server_read, server_write, state));
    let bridge = AcpBridge::new();
    rt.block_on(bridge.connect_streams(client_read, client_write))
        .expect("ACP handshake");

    cx.update(|cx| {
        init_app(cx);
        cx.set_global(GlowCardProbe::default());
        cx.set_global(FeedPaintProbe::default());
    });
    let (_view, vc) = cx.add_window_view(|_window, cx| {
        ChatView::new(bridge.clone(), rt.handle().clone(), std::env::temp_dir(), cx)
    });

    vc.simulate_keystrokes("cmd-w");
    vc.run_until_parked();
    assert!(cx.windows().is_empty(), "Cmd+W left the window open");
}

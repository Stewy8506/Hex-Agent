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
use hex_desktop::chat::{app_runtime, init_app, ChatView};
use hex_desktop::shortcuts::{About, ActualSize, Redo, Undo, ZoomIn, ZoomOut};
use hex_desktop::ui::composer::{GlowCardPaint, GlowCardProbe};
use hex_desktop::ui::feed::{FeedPaintProbe, MessageItem};
use hex_desktop::ui::message::AssistantMessageView;
use hex_desktop::ui::modals::approval::{ToolApprovalChoice, ToolApprovalModal};
use hex_desktop::ui::modals::model_picker::ModelPickerModal;
use hex_desktop::ui::modals::session_actions::{SessionActionMode, SessionActionModal};
use hex_desktop::ui::modals::settings::{SettingField, SettingsModal};
use hex_desktop::ui::modals::{ActiveModal, ModelEntry};
use hex_desktop::ui::motion::{MotionPolicy, MotionSystem};
use hex_desktop::ui::reasoning::ThinkingDisclosure;
use hex_desktop::ui::sidebar::ProviderState;
use hex_desktop::ui::tool_card::{ToolCallCard, ToolStatus};
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, DuplexStream};
use tokio::runtime::Runtime;
use tokio::sync::Mutex as AsyncMutex;

#[derive(Default)]
struct MockState {
    session_counter: AtomicUsize,
    list_requests: AtomicUsize,
    cancel_flag: AtomicBool,
    cancel_requests: AtomicUsize,
    prompts: StdMutex<Vec<(String, String)>>,
    sessions: StdMutex<Vec<Value>>,
    renames: StdMutex<Vec<(String, String)>>,
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
            let session_id = format!("ses_test_{n}");
            let cwd = params
                .get("cwd")
                .and_then(|v| v.as_str())
                .unwrap_or("/workspace")
                .to_string();
            state.sessions.lock().unwrap().push(json!({
                "sessionId": session_id,
                "cwd": cwd,
                "model": "claude-3-7-sonnet",
                "createdAt": 1_700_000_000.0 + n as f64,
                "turns": 0,
                "archived": false,
                "pinned": false
            }));
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {
                        "sessionId": session_id,
                        "cwd": params.get("cwd").and_then(|v| v.as_str()).unwrap_or("/workspace"),
                        "model": "claude-3-7-sonnet",
                        "provider": "Anthropic"
                    }
                }),
            )
            .await;
        }
        "session/list" => {
            state.list_requests.fetch_add(1, Ordering::SeqCst);
            let sessions = state.sessions.lock().unwrap().clone();
            send_line(
                &writer,
                json!({"jsonrpc": "2.0", "id": id, "result": {"sessions": sessions}}),
            )
            .await;
        }
        "session/rename" => {
            let session_id = params["sessionId"].as_str().unwrap().to_string();
            let title = params["title"].as_str().unwrap().to_string();
            state
                .renames
                .lock()
                .unwrap()
                .push((session_id.clone(), title.clone()));
            {
                let mut sessions = state.sessions.lock().unwrap();
                if let Some(session) = sessions
                    .iter_mut()
                    .find(|session| session["sessionId"] == session_id)
                {
                    session["title"] = json!(title);
                }
            }
            send_line(
                &writer,
                json!({"jsonrpc": "2.0", "id": id, "result": {"sessionId": session_id, "title": title}}),
            )
            .await;
        }
        "session/prompt" => {
            let sid = params["sessionId"].as_str().unwrap().to_string();
            let text = params["prompt"].as_str().unwrap().to_string();
            state.prompts.lock().unwrap().push((sid.clone(), text.clone()));
            {
                let mut sessions = state.sessions.lock().unwrap();
                if let Some(session) = sessions
                    .iter_mut()
                    .find(|session| session["sessionId"] == sid)
                {
                    let turns = session["turns"].as_u64().unwrap_or(0) + 1;
                    session["turns"] = json!(turns);
                }
            }

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

/// Forces a repaint and returns distinct glow-card geometries. Animated frames
/// can repaint the same card with subpixel drift; nearby bounds are one card.
fn glow_paints(vc: &mut VisualTestContext, view: &Entity<ChatView>) -> Vec<GlowCardPaint> {
    vc.update(|_window, app| {
        view.update(app, |_, cx| cx.notify());
    });
    vc.run_until_parked();
    let paints: Vec<GlowCardPaint> = vc.read(|app| {
        app.try_global::<GlowCardProbe>()
            .map(|p| p.0.lock().unwrap().clone())
            .unwrap_or_default()
    });
    let mut distinct: Vec<GlowCardPaint> = Vec::new();
    for paint in paints {
        if !distinct.iter().any(|existing| {
            bounds_close(existing.card, paint.card) && bounds_close(existing.halo, paint.halo)
        }) {
            distinct.push(paint);
        }
    }
    distinct
}

fn bounds_close(actual: Bounds<Pixels>, expected: Bounds<Pixels>) -> bool {
    (actual.origin.x - expected.origin.x).abs() <= px(1.0)
        && (actual.origin.y - expected.origin.y).abs() <= px(1.0)
        && (actual.size.width - expected.size.width).abs() <= px(1.0)
        && (actual.size.height - expected.size.height).abs() <= px(1.0)
}

fn assert_px_close(actual: Pixels, expected: Pixels) {
    assert!(
        (actual - expected).abs() < px(0.5),
        "expected ~{expected:?}, got {actual:?}"
    );
}

fn click_debug_element(vc: &mut VisualTestContext, selector: &'static str) {
    let bounds = vc
        .debug_bounds(selector)
        .unwrap_or_else(|| panic!("missing rendered element {selector}"));
    vc.simulate_click(bounds.center(), Modifiers::none());
    vc.run_until_parked();
}

fn finish_motion(vc: &mut VisualTestContext, view: &Entity<ChatView>) {
    vc.run_until_parked();
    vc.update(|window, app| {
        let window_id = window.window_handle().window_id();
        for _ in 0..30 {
            app.global_mut::<MotionSystem>()
                .tick(window_id, Duration::from_millis(16));
        }
        view.update(app, |_, cx| cx.notify());
        window.refresh();
    });
    vc.run_until_parked();
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

    // 1. Startup connects and lists durable sessions without creating a chat.
    pump_until(vc, &view, "session list refresh", |v| {
        v.shell.active_session_id.is_none()
            && v.shell.feed.is_empty()
            && state.list_requests.load(Ordering::SeqCst) >= 1
    });
    snap(vc, &view, |v| {
        assert_eq!(state.session_counter.load(Ordering::SeqCst), 0);
        assert!(state.sessions.lock().unwrap().is_empty());
        assert_eq!(v.shell.active_session_id, None);
        assert!(v.shell.feed.is_empty());
        assert_eq!(v.shell.sidebar.provider_status.state, ProviderState::Connected);
        assert_eq!(v.shell.composer.model_pill.provider_name, "HEX");
        assert_eq!(v.shell.composer.model_pill.model_name, "ready");
        assert!(v.shell.sidebar.session_list.sessions.is_empty());
    });

    // 1b. Composer parity: the Home screen renders exactly one glow card at
    // the mockup's 640px Start width, with the halo overhanging it by the
    // mockup's 2px inset on every side.
    clear_glow_probe(vc);
    let paints = glow_paints(vc, &view);
    assert_eq!(paints.len(), 1, "Home must render exactly one glow card: {paints:?}");
    assert_px_close(paints[0].card.size.width, px(640.0));
    assert_px_close(paints[0].halo.size.width, px(644.0));
    assert_px_close(paints[0].halo.size.height, paints[0].card.size.height + px(4.0));

    // 2. Type through the real input system and submit with Enter.
    vc.simulate_input("Hello HEX");
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "Hello HEX");
    vc.simulate_keystrokes("enter");
    // A duplicate submit while session/new is in flight must not create a chat.
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
    pump_until(vc, &view, "persisted first-prompt title", |_| {
        state
            .renames
            .lock()
            .unwrap()
            .iter()
            .any(|(session_id, title)| session_id == "ses_test_1" && title == "Hello HEX")
    });
    snap(vc, &view, |v| {
        assert_eq!(v.shell.active_session_id.as_deref(), Some("ses_test_1"));
        assert_eq!(state.session_counter.load(Ordering::SeqCst), 1);
        assert_eq!(state.prompts.lock().unwrap().len(), 1);
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

    // 2a. Only the live Chat view is exposed. Files, Browser, and Terminal
    // remain hidden until they have functional panes instead of dead links.
    vc.run_until_parked();
    assert!(
        vc.debug_bounds("segment_chat").is_some(),
        "the live Chat view indicator must remain visible"
    );
    for selector in ["segment_files", "segment_browser", "segment_terminal"] {
        assert!(
            vc.debug_bounds(selector).is_none(),
            "nonfunctional pane navigation must not render: {selector}"
        );
    }

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
    let fade_paints = vc.read(|app| {
        app.try_global::<FeedPaintProbe>()
            .map(|p| p.0.lock().unwrap().clone())
            .unwrap_or_default()
    });
    let mut fades = Vec::new();
    for fade in fade_paints {
        if !fades.contains(&fade) {
            fades.push(fade);
        }
    }
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

    // 4. Cmd+N opens a local Home draft without creating a backend session.
    vc.simulate_keystrokes("cmd-n");
    snap(vc, &view, |v| {
        assert!(v.shell.feed.is_empty());
        assert_eq!(v.shell.active_session_id, None);
        assert!(v.shell.feed.is_empty());
        assert_eq!(v.shell.sidebar.session_list.sessions.len(), 1);
        assert_eq!(state.session_counter.load(Ordering::SeqCst), 1);
    });

    // 5. The first prompt in that draft creates and adopts the second session.
    vc.simulate_input("slow task");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "second session creation and streaming", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_2")
            && v.shell.composer.is_streaming
            && last_assistant(v).map(|a| a.content.as_str()) == Some("Working...")
    });
    snap(vc, &view, |v| {
        assert_eq!(state.session_counter.load(Ordering::SeqCst), 2);
        assert_eq!(v.shell.sidebar.session_list.sessions.len(), 2);
    });

    // 6. Draft typed in session 2 must survive switching away and back.
    vc.simulate_input("draft for later");
    assert_eq!(
        snap(vc, &view, |v| v.composer_text().to_string()),
        "draft for later"
    );

    // 7. Cmd+K palette: typing routes to the query, Enter switches session.
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

    // 8. Switch back by its persisted auto-title; the draft is restored.
    vc.simulate_keystrokes("cmd-k");
    vc.simulate_input("slow task");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "palette switch back to second session", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_2")
    });
    snap(vc, &view, |v| {
        assert_eq!(v.composer_text(), "draft for later");
        assert_eq!(last_assistant(v).unwrap().content, "Working...");
    });

    // 8b. Escape dismisses the palette without touching the draft; up/down
    //     inside the palette never move the composer caret or edit text.
    vc.simulate_keystrokes("cmd-k down up backspace escape");
    snap(vc, &view, |v| {
        assert_eq!(v.shell.active_modal, ActiveModal::None);
        assert_eq!(v.composer_text(), "draft for later");
    });
    assert!(focus_state(vc, &view) == (true, false));

    // 9. Select-all + backspace clears the draft through real editor actions.
    vc.simulate_keystrokes("cmd-a backspace");
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "");

    // 10. Escape cancels the running turn; the runtime acknowledges and the feed
    //     shows the authoritative cancelled state.
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

fn launch_connected_chat<'a>(
    cx: &'a mut TestAppContext,
) -> (Runtime, Entity<ChatView>, &'a mut VisualTestContext) {
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
    let (view, vc) = cx.add_window_view(|_window, cx| {
        ChatView::new(bridge.clone(), rt.handle().clone(), std::env::temp_dir(), cx)
    });
    (rt, view, vc)
}

#[gpui::test]
async fn test_cmd_w_closes_window(cx: &mut TestAppContext) {
    let (_rt, _view, vc) = launch_connected_chat(cx);
    vc.simulate_keystrokes("cmd-w");
    vc.run_until_parked();
    assert!(cx.windows().is_empty(), "Cmd+W left the window open");
}

#[gpui::test]
async fn test_about_action_opens_and_dismisses(cx: &mut TestAppContext) {
    let (_rt, view, vc) = launch_connected_chat(cx);
    vc.dispatch_action(About);
    vc.run_until_parked();
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::About);
    });

    vc.simulate_keystrokes("escape");
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::None);
    });

    vc.simulate_input("open about in a session");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "session view", |view| {
        view.shell.active_session_id.as_deref() == Some("ses_test_1")
            && !view.shell.composer.is_streaming
    });
    vc.dispatch_action(About);
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::About);
    });
    vc.simulate_keystrokes("enter");
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::None);
    });
}

#[gpui::test]
fn test_modal_pointer_actions_activate_controls(cx: &mut TestAppContext) {
    cx.update(|cx| {
        init_app(cx);
        cx.set_global(MotionSystem::with_policy(MotionPolicy::new(true)));
    });
    let (view, vc) = cx.add_window_view(|_, cx| {
        ChatView::new(
            AcpBridge::new(),
            app_runtime().handle().clone(),
            std::env::temp_dir(),
            cx,
        )
    });

    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.open_command_palette();
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        assert!(matches!(view.shell.active_modal, ActiveModal::Palette(_)));
    });
    let sidebar_before = snap(vc, &view, |view| view.shell.sidebar_collapsed);
    click_debug_element(vc, "palette_nav_sidebar");
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::None);
        assert_ne!(view.shell.sidebar_collapsed, sidebar_before);
    });

    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::ModelPicker(ModelPickerModal::new(
                "gpt-4o",
                vec![
                    ModelEntry::new("gpt-4o", "GPT-4o", "OpenAI", false),
                    ModelEntry::new("o3-mini", "o3-mini", "OpenAI", false),
                ],
            ));
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    click_debug_element(vc, "model_1");
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::None);
        assert!(last_notice(view).unwrap().contains("cannot switch model"));
    });

    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::Approval(ToolApprovalModal::new(
                "req_pointer",
                "bash",
                "cargo test",
                "R3",
            ));
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    click_debug_element(vc, "approval_choice_session");
    click_debug_element(vc, "approval_lease");
    snap(vc, &view, |view| {
        let ActiveModal::Approval(modal) = &view.shell.active_modal else {
            panic!("approval should remain open");
        };
        assert_eq!(modal.choice, ToolApprovalChoice::Session);
        assert!(modal.lease_15m);
    });
    click_debug_element(vc, "approval_deny");
    pump_until(vc, &view, "pointer approval decision", |view| {
        last_notice(view).is_some_and(|notice| notice.starts_with("Approval decision failed"))
    });

    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::SessionAction(
                SessionActionModal::open_menu("ses_pointer", "Pointer session", false, false),
            );
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    click_debug_element(vc, "session_menu_0");
    snap(vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("session actions should remain open");
        };
        assert_eq!(modal.mode, SessionActionMode::Rename);
        assert_eq!(modal.draft, "Pointer session");
    });

    let mut settings = SettingsModal::default();
    settings.fields.push(SettingField::Text {
        key: "model.name",
        label: "Model",
        value: "new-model".into(),
        cursor: 0,
        required: false,
    });
    settings
        .originals
        .insert("model.name".into(), "old-model".into());
    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::Settings(settings);
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    click_debug_element(vc, "settings_save_row");
    pump_until(vc, &view, "pointer settings save", |view| {
        matches!(&view.shell.active_modal, ActiveModal::Settings(modal) if modal.error.as_deref().is_some_and(|error| error.starts_with("Save failed")))
    });
}

#[gpui::test]
async fn test_model_picker_virtualizes_catalog_and_scrolls_keyboard_selection(
    cx: &mut TestAppContext,
) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    cx.update(|cx| init_app(cx));
    let workspace = std::env::temp_dir();
    let (view, mut vc) = cx.add_window_view(|_window, cx| {
        ChatView::new(AcpBridge::new(), rt.handle().clone(), workspace.clone(), cx)
    });
    let models = (0..200)
        .map(|index| {
            ModelEntry::new(
                format!("model-{index}"),
                format!("Model {index}"),
                if index < 100 { "OpenAI" } else { "Anthropic" },
                false,
            )
        })
        .collect();
    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal =
                ActiveModal::ModelPicker(ModelPickerModal::new("model-0", models));
            cx.notify();
        });
    });
    finish_motion(&mut vc, &view);

    let list_bounds = vc
        .debug_bounds("model_picker_list")
        .expect("virtualized picker list should be mounted");
    assert!(vc.debug_bounds("model_0").is_some());
    assert!(
        vc.debug_bounds("model_199").is_none(),
        "offscreen catalog rows must not be mounted"
    );

    vc.simulate_keystrokes("end");
    finish_motion(&mut vc, &view);
    snap(&vc, &view, |view| {
        let ActiveModal::ModelPicker(picker) = &view.shell.active_modal else {
            panic!("picker should remain open");
        };
        assert_eq!(picker.highlighted_model().unwrap().id, "model-199");
    });
    let last_bounds = vc
        .debug_bounds("model_199")
        .expect("keyboard navigation should reveal the highlighted row");
    assert!(list_bounds.contains(&last_bounds.center()));
}

#[gpui::test]
async fn test_model_picker_disconnected_error_is_inline(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    cx.update(|cx| init_app(cx));
    let workspace = std::env::temp_dir();
    let (view, mut vc) = cx.add_window_view(|_window, cx| {
        ChatView::new(AcpBridge::new(), rt.handle().clone(), workspace.clone(), cx)
    });
    finish_motion(&mut vc, &view);

    click_debug_element(&mut vc, "model_pill_button");
    let failed_request_id = snap(&vc, &view, |view| {
        let ActiveModal::ModelPicker(picker) = &view.shell.active_modal else {
            panic!("picker should stay open with the connection error");
        };
        assert!(picker
            .error()
            .is_some_and(|error| error.contains("Not connected to the HEX runtime")));
        assert!(!last_notice(view)
            .is_some_and(|notice| notice.contains("Could not load the model catalog")));
        picker.request_id
    });
    click_debug_element(&mut vc, "model_picker_retry");
    snap(&vc, &view, |view| {
        let ActiveModal::ModelPicker(picker) = &view.shell.active_modal else {
            panic!("retry should keep the inline error open");
        };
        assert!(picker.request_id > failed_request_id);
        assert!(picker.error().is_some());
    });
}

#[gpui::test]
async fn test_view_zoom_actions_adjust_and_reset_scale(cx: &mut TestAppContext) {
    let (_rt, _view, vc) = launch_connected_chat(cx);
    vc.dispatch_action(ZoomIn);
    vc.update(|window, _| assert_eq!(window.rem_size(), px(18.0)));
    vc.dispatch_action(ZoomOut);
    vc.update(|window, _| assert_eq!(window.rem_size(), px(16.0)));
    vc.dispatch_action(ZoomOut);
    vc.update(|window, _| assert_eq!(window.rem_size(), px(14.0)));
    vc.dispatch_action(ActualSize);
    vc.update(|window, _| assert_eq!(window.rem_size(), px(16.0)));

    for _ in 0..10 {
        vc.dispatch_action(ZoomIn);
    }
    vc.update(|window, _| assert_eq!(window.rem_size(), px(24.0)));
    for _ in 0..10 {
        vc.dispatch_action(ZoomOut);
    }
    vc.update(|window, _| assert_eq!(window.rem_size(), px(12.0)));
}

#[gpui::test]
async fn test_undo_redo_actions_restore_composer_and_modal_text(cx: &mut TestAppContext) {
    let (_rt, view, vc) = launch_connected_chat(cx);

    vc.simulate_input("draft");
    vc.dispatch_action(Undo);
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "");
    vc.dispatch_action(Redo);
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draft");

    vc.simulate_keystrokes("backspace");
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draf");
    vc.simulate_keystrokes("cmd-z");
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draft");
    vc.simulate_keystrokes("cmd-shift-z");
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draf");

    vc.simulate_keystrokes("cmd-k");
    vc.simulate_input("settings");
    vc.dispatch_action(Undo);
    snap(vc, &view, |view| {
        let ActiveModal::Palette(palette) = &view.shell.active_modal else {
            panic!("palette should be open");
        };
        assert_eq!(palette.query, "");
    });
    vc.dispatch_action(Redo);
    snap(vc, &view, |view| {
        let ActiveModal::Palette(palette) = &view.shell.active_modal else {
            panic!("palette should be open");
        };
        assert_eq!(palette.query, "settings");
    });
    vc.simulate_keystrokes("escape");
    vc.dispatch_action(Undo);
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draft");

    let mut settings = SettingsModal::default();
    settings.fields.push(SettingField::Text {
        key: "model.cloudProvider",
        label: "Provider",
        value: "gemini".to_string(),
        cursor: "gemini".len(),
        required: true,
    });
    settings.selected = 0;
    settings.editing = true;
    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::Settings(settings);
            cx.notify();
        });
    });
    vc.run_until_parked();

    vc.simulate_keystrokes("x");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminix");
    });
    vc.dispatch_action(Undo);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "gemini");
    });
    vc.dispatch_action(Redo);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminix");
    });

    vc.simulate_keystrokes("left left x");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, cursor, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminxix");
        assert_eq!(*cursor, 6);
    });
    vc.dispatch_action(Undo);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, cursor, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminix");
        assert_eq!(*cursor, 5);
    });
    vc.dispatch_action(Redo);
    vc.simulate_keystrokes("home delete");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, cursor, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "eminxix");
        assert_eq!(*cursor, 0);
    });
    vc.dispatch_action(Undo);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminxix");
    });
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draft");
}

#[gpui::test]
async fn test_streaming_disclosures_toggle_by_click(cx: &mut TestAppContext) {
    let (rt, view, vc) = launch_connected_chat(cx);
    vc.simulate_input("show details");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "session view", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_1")
            && !v.shell.composer.is_streaming
            && last_assistant(v).is_some()
    });
    // Let the frame-coalesced notification pump drain before replacing the feed.
    rt.block_on(async {
        tokio::time::sleep(Duration::from_millis(50)).await;
    });
    vc.run_until_parked();
    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            view.shell.feed.messages.clear();
            view.shell.feed.messages.push(MessageItem::Assistant(
                AssistantMessageView::new("msg_disclosures", "Working on it")
                    .with_reasoning(
                        ThinkingDisclosure::new("Inspect the request and choose a tool")
                            .with_generating(true),
                    )
                    .with_tool_call(
                        ToolCallCard::new(
                            "tc_disclosures",
                            "read_file",
                            r#"{"path":"src/main.rs"}"#,
                        )
                        .with_output("fn main() {}")
                        .with_duration(12),
                    )
                    .with_streaming(true),
            ));
            cx.notify();
        });
    });
    vc.run_until_parked();
    finish_motion(vc, &view);

    click_debug_element(vc, "reasoning_msg_disclosures");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        let assistant = last_assistant(view).unwrap();
        assert!(assistant.reasoning.as_ref().unwrap().is_expanded);
    });

    click_debug_element(vc, "tool_input_tc_disclosures");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        assert!(last_assistant(view).unwrap().tool_calls[0].input_expanded);
    });

    click_debug_element(vc, "tool_output_tc_disclosures");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        assert!(!last_assistant(view).unwrap().tool_calls[0].output_expanded);
    });
}

#[gpui::test]
async fn test_modal_enter_is_single_step_and_isolated(cx: &mut TestAppContext) {
    let (_rt, view, vc) = launch_connected_chat(cx);
    vc.simulate_input("keep draft");
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "keep draft");
    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::SessionAction(SessionActionModal::open_menu(
                "ses_test_1",
                "Existing title",
                false,
                false,
            ));
            cx.notify();
        });
    });
    vc.run_until_parked();

    vc.simulate_keystrokes("enter");
    snap(&vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("session action modal should still be open");
        };
        assert_eq!(modal.mode, SessionActionMode::Rename);
        assert!(!modal.busy);
        assert_eq!(view.composer_text(), "keep draft");
    });
    vc.simulate_keystrokes("escape");

    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            let mut settings = SettingsModal::default();
            settings.fields.push(SettingField::Text {
                key: "model.cloudModelId",
                label: "Model ID",
                value: "gpt-4o".to_string(),
                cursor: "gpt-4o".len(),
                required: true,
            });
            view.shell.active_modal = ActiveModal::Settings(settings);
            cx.notify();
        });
    });
    vc.run_until_parked();

    vc.simulate_keystrokes("enter");
    snap(&vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings modal should still be open");
        };
        assert!(modal.editing, "one Enter must enter settings edit mode");
        assert_eq!(view.composer_text(), "keep draft");
    });
}

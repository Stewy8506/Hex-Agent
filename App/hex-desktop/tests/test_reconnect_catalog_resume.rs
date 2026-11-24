//! Phase 3.5 acceptance tests: supervised reconnect (B28), the live backend
//! model catalog (B32), and durable session persistence/resume.
//!
//! Each test launches the real `ChatView` entry point in a GPUI test window and
//! drives it through the real keystroke/action path against a scripted ACP
//! backend over duplex pipes. The mock speaks the canonical wire shapes
//! (`session/new`, `session/list`, `session/load` paged, `session/models`,
//! `session/command`) so the desktop is exercised end to end, not via stubs.

#![recursion_limit = "512"]

// `use gpui::*` also brings `gpui::test` into scope; import the built-in test
// attribute explicitly so the macro-emitted `#[test]` does not recurse.
use core::prelude::v1::test;
use gpui::*;
use hex_desktop::acp_bridge::AcpBridge;
use hex_desktop::chat::{init_app, ChatView};
use hex_desktop::ui::feed::MessageItem;
use hex_desktop::ui::modals::ActiveModal;
use hex_desktop::ui::sidebar::ProviderState;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, DuplexStream};
use tokio::sync::{watch, Mutex as AsyncMutex, Notify};

#[derive(Default)]
struct MockState {
    session_counter: AtomicUsize,
    list_requests: AtomicUsize,
    renames: StdMutex<Vec<(String, String)>>,
    catalog_models: StdMutex<Vec<Value>>,
    catalog_error: StdMutex<Option<String>>,
    catalog_fail: AtomicBool,
    switch_model: StdMutex<Option<String>>,
    switch_provider: StdMutex<Option<String>>,
    switch_error: AtomicBool,
    switch_unconfirmed: AtomicBool,
    list_sessions: StdMutex<Vec<Value>>,
    load_history: StdMutex<Vec<(String, String)>>,
    load_histories: StdMutex<std::collections::HashMap<String, Vec<(String, String)>>>,
    load_gates: StdMutex<std::collections::HashMap<String, Arc<Notify>>>,
    load_started: AtomicUsize,
    load_finished: AtomicUsize,
    load_model: StdMutex<String>,
}

impl MockState {
    fn new() -> Arc<Self> {
        let state = Self {
            load_model: StdMutex::new("claude-3-7-sonnet".to_string()),
            ..Default::default()
        };
        Arc::new(state)
    }
}

async fn send_line(writer: &Arc<AsyncMutex<DuplexStream>>, value: Value) {
    let mut line = serde_json::to_string(&value).unwrap();
    line.push('\n');
    let mut w = writer.lock().await;
    w.write_all(line.as_bytes()).await.unwrap();
    w.flush().await.unwrap();
}

fn catalog_model(id: &str, display: &str, provider: &str, is_local: bool) -> Value {
    json!({"id": id, "displayName": display, "provider": provider, "isLocal": is_local})
}

fn session_item(id: &str, model: &str) -> Value {
    json!({"sessionId": id, "cwd": "/workspace", "model": model, "turns": 1, "title": id})
}

async fn handle_request(
    method: &str,
    id: Option<Value>,
    params: Value,
    writer: &Arc<AsyncMutex<DuplexStream>>,
    state: &Arc<MockState>,
) {
    match method {
        "initialize" => {
            send_line(
                writer,
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
            state.list_sessions.lock().unwrap().push(json!({
                "sessionId": session_id,
                "cwd": "/workspace",
                "model": "claude-3-7-sonnet",
                "createdAt": 1_700_000_000.0 + n as f64,
                "turns": 0
            }));
            send_line(
                writer,
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
            let sessions = state.list_sessions.lock().unwrap().clone();
            send_line(
                writer,
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
                let mut sessions = state.list_sessions.lock().unwrap();
                if let Some(session) = sessions
                    .iter_mut()
                    .find(|session| session["sessionId"] == session_id)
                {
                    session["title"] = json!(title);
                }
            }
            send_line(
                writer,
                json!({"jsonrpc": "2.0", "id": id, "result": {"sessionId": session_id, "title": title}}),
            )
            .await;
        }
        "session/prompt" => {
            let session_id = params["sessionId"].as_str().unwrap().to_string();
            {
                let mut sessions = state.list_sessions.lock().unwrap();
                if let Some(session) = sessions
                    .iter_mut()
                    .find(|session| session["sessionId"] == session_id)
                {
                    let turns = session["turns"].as_u64().unwrap_or(0) + 1;
                    session["turns"] = json!(turns);
                }
            }
            send_line(
                writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {
                        "sessionId": session_id,
                        "status": "completed",
                        "taskId": "tsk_test",
                        "content": []
                    }
                }),
            )
            .await;
        }
        "session/load" => {
            let sid = params["sessionId"].as_str().unwrap_or("ses_unknown").to_string();
            state.load_started.fetch_add(1, Ordering::SeqCst);
            let gate = state.load_gates.lock().unwrap().get(&sid).cloned();
            if let Some(gate) = gate {
                gate.notified().await;
            }
            let model = state.load_model.lock().unwrap().clone();
            let history: Vec<Value> = state
                .load_histories
                .lock()
                .unwrap()
                .get(&sid)
                .cloned()
                .unwrap_or_else(|| state.load_history.lock().unwrap().clone())
                .iter()
                .enumerate()
                .map(|(i, (prompt, response))| {
                    json!({
                        "prompt": prompt,
                        "response": response,
                        "status": "completed",
                        "turnIndex": i,
                        "promptChars": prompt.chars().count(),
                        "responseChars": response.chars().count(),
                    })
                })
                .collect();
            let count = history.len();
            send_line(
                writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {
                        "sessionId": sid, "cwd": "/workspace", "model": model,
                        "history": history, "historyCount": count, "nextBefore": null
                    }
                }),
            )
            .await;
            state.load_finished.fetch_add(1, Ordering::SeqCst);
        }
        "session/models" => {
            let sid = params["sessionId"].as_str().unwrap_or("ses_unknown").to_string();
            if state.catalog_fail.load(Ordering::SeqCst) {
                send_line(
                    writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "error": {"code": -32000, "message": "No models discovered from any provider"}
                    }),
                )
                .await;
                return;
            }
            let models = state.catalog_models.lock().unwrap().clone();
            let error = state.catalog_error.lock().unwrap().clone();
            let mut result = json!({
                "sessionId": sid,
                "activeModel": "claude-3-7-sonnet",
                "provider": "Anthropic",
                "models": models,
            });
            if let Some(err) = error {
                result["error"] = json!(err);
            }
            send_line(writer, json!({"jsonrpc": "2.0", "id": id, "result": result})).await;
        }
        "session/command" => {
            let sid = params["sessionId"].as_str().unwrap_or("ses_unknown").to_string();
            let failed = state.switch_error.load(Ordering::SeqCst);
            let unconfirmed = state.switch_unconfirmed.load(Ordering::SeqCst);
            let model = state.switch_model.lock().unwrap().clone();
            let provider = state.switch_provider.lock().unwrap().clone();
            let output = if failed {
                "backend refused the switch".to_string()
            } else {
                format!("Switched to {}", model.clone().unwrap_or_default())
            };
            let mut result = json!({
                "sessionId": sid, "command": "model", "output": output,
            });
            if failed {
                result["isError"] = json!(true);
            }
            if !unconfirmed {
                if let Some(m) = model {
                    result["model"] = json!(m);
                }
                if let Some(p) = provider {
                    result["provider"] = json!(p);
                }
            }
            send_line(writer, json!({"jsonrpc": "2.0", "id": id, "result": result})).await;
        }
        "session/historyContent" => {
            // Previews in session/load are complete, so this is only a safety net.
            let total = params.get("limit").and_then(|v| v.as_u64()).unwrap_or(0);
            send_line(
                writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {"content": "", "nextOffset": null, "totalChars": total}
                }),
            )
            .await;
        }
        _ => {
            if let Some(id) = id {
                send_line(
                    writer,
                    json!({"jsonrpc": "2.0", "id": id, "result": {}}),
                )
                .await;
            }
        }
    }
}

/// Run a scripted backend until `shutdown` fires; dropping the writer half then
/// closes the client's read pipe, which the bridge surfaces as `Disconnected`.
async fn run_mock_server(
    reader: DuplexStream,
    writer: DuplexStream,
    state: Arc<MockState>,
    mut shutdown: watch::Receiver<bool>,
) {
    let writer = Arc::new(AsyncMutex::new(writer));
    let mut lines = BufReader::new(reader).lines();
    loop {
        tokio::select! {
            _ = shutdown.changed() => break,
            line = lines.next_line() => {
                match line {
                    Ok(Some(line)) => {
                        let req: Value = serde_json::from_str(&line).unwrap();
                        let method = req["method"].as_str().unwrap().to_string();
                        let id = req.get("id").cloned();
                        let params = req.get("params").cloned().unwrap_or(json!({}));
                        let writer = writer.clone();
                        let state = state.clone();
                        tokio::spawn(async move {
                            handle_request(&method, id, params, &writer, &state).await;
                        });
                    }
                    _ => break,
                }
            }
        }
    }
    drop(writer);
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

fn has_notice(view: &ChatView, needle: &str) -> bool {
    view.shell.feed.messages.iter().any(|m| match m {
        MessageItem::SystemNotice(n) => n.contains(needle),
        _ => false,
    })
}

fn last_assistant(view: &ChatView) -> Option<&hex_desktop::ui::message::AssistantMessageView> {
    view.shell.feed.messages.iter().rev().find_map(|m| match m {
        MessageItem::Assistant(a) => Some(a),
        _ => None,
    })
}

fn last_user(view: &ChatView) -> Option<String> {
    view.shell.feed.messages.iter().rev().find_map(|m| match m {
        MessageItem::User(u) => Some(u.content.clone()),
        _ => None,
    })
}

fn build_view(
    cx: &mut TestAppContext,
    rt: &tokio::runtime::Runtime,
    bridge: &AcpBridge,
) -> (Entity<ChatView>, VisualTestContext) {
    cx.update(init_app);
    let workspace = std::env::temp_dir();
    let (view, vc) = cx.add_window_view(|_window, cx| {
        ChatView::new(bridge.clone(), rt.handle().clone(), workspace.clone(), cx)
    });
    (view, vc.clone())
}

fn submit_initial_prompt(vc: &mut VisualTestContext, view: &Entity<ChatView>, text: &str) {
    vc.simulate_input(text);
    vc.simulate_keystrokes("enter");
    pump_until(vc, view, "first prompt creates a session", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_1")
            && !v.shell.composer.is_streaming
            && last_user(v).as_deref() == Some(text)
    });
}

/// B28: a transport loss arms one supervised reconnect loop (Reconnecting);
/// once the transport is back the previous session is restored from durable
/// history rather than silently dropped or duplicated.
#[gpui::test]
async fn test_supervised_reconnect_restores_session(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();

    // Backend A: adopts a session, then is torn down to simulate a loss.
    let state_a = MockState::new();
    let (client_read, server_write) = tokio::io::duplex(64 * 1024);
    let (server_read, client_write) = tokio::io::duplex(64 * 1024);
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    rt.spawn(run_mock_server(server_read, server_write, state_a.clone(), shutdown_rx));

    let bridge = AcpBridge::new();
    rt.block_on(bridge.connect_streams(client_read, client_write))
        .expect("ACP handshake");

    let (view, mut vc) = build_view(cx, &rt, &bridge);
    submit_initial_prompt(&mut vc, &view, "start a conversation");
    snap(&vc, &view, |v| {
        assert_eq!(v.shell.sidebar.provider_status.state, ProviderState::Connected);
    });

    // Backend B (a fresh process) holds the durable history for ses_test_1.
    let state_b = MockState::new();
    *state_b.load_history.lock().unwrap() =
        vec![("Recovered prompt".to_string(), "Recovered answer".to_string())];
    let (client_read_b, server_write_b) = tokio::io::duplex(64 * 1024);
    let (server_read_b, client_write_b) = tokio::io::duplex(64 * 1024);
    let (_shutdown_tx_b, shutdown_rx_b) = watch::channel(false);
    rt.spawn(run_mock_server(server_read_b, server_write_b, state_b.clone(), shutdown_rx_b));

    // Lose the transport. The bridge emits Disconnected and the view arms supervision.
    shutdown_tx.send(true).unwrap();
    pump_until(&mut vc, &view, "supervised reconnect armed", |v| {
        v.shell.sidebar.provider_status.state == ProviderState::Reconnecting
    });
    assert!(snap(&vc, &view, |v| has_notice(v, "reconnecting")));

    // Reconnect within the first backoff delay so the loop never clobbers the
    // freshly established transport (the post-sleep is_connected recheck).
    rt.block_on(bridge.connect_streams(client_read_b, client_write_b))
        .expect("reconnect handshake");

    pump_until(&mut vc, &view, "session restored after reconnect", |v| {
        v.shell.sidebar.provider_status.state == ProviderState::Connected
            && v.shell.active_session_id.as_deref() == Some("ses_test_1")
            && last_assistant(v).map(|a| a.content.as_str()) == Some("Recovered answer")
    });
    snap(&vc, &view, |v| {
        assert_eq!(last_user(v).as_deref(), Some("Recovered prompt"));
        // Exactly one supervised loop ran; the session was not duplicated.
        assert_eq!(
            v.shell.sidebar.session_list.sessions.iter().filter(|s| s.id == "ses_test_1").count(),
            1
        );
    });
}

/// B32: the picker is populated from the live backend catalog and a selection
/// only changes the displayed identity once the backend confirms the switch.
#[gpui::test]
async fn test_model_catalog_fetch_and_confirmed_switch(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();

    let state = MockState::new();
    *state.catalog_models.lock().unwrap() = vec![
        catalog_model("gpt-4o", "GPT-4o", "OpenAI", false),
        catalog_model("o3-mini", "o3-mini", "OpenAI", false),
        catalog_model("qwen2.5-coder-32b", "Qwen (Ollama)", "Local", true),
    ];
    *state.catalog_error.lock().unwrap() = Some("Local probe timed out".to_string());
    *state.switch_model.lock().unwrap() = Some("gpt-4o".to_string());
    *state.switch_provider.lock().unwrap() = Some("OpenAI".to_string());

    let (client_read, server_write) = tokio::io::duplex(64 * 1024);
    let (server_read, client_write) = tokio::io::duplex(64 * 1024);
    let (_shutdown_tx, shutdown_rx) = watch::channel(false);
    rt.spawn(run_mock_server(server_read, server_write, state.clone(), shutdown_rx));

    let bridge = AcpBridge::new();
    rt.block_on(bridge.connect_streams(client_read, client_write))
        .expect("ACP handshake");
    let (view, mut vc) = build_view(cx, &rt, &bridge);
    submit_initial_prompt(&mut vc, &view, "start a conversation");

    // Open the picker through the real palette action; it must fetch the catalog.
    vc.simulate_keystrokes("cmd-k");
    vc.simulate_input("switch model");
    vc.simulate_keystrokes("enter");
    pump_until(&mut vc, &view, "live catalog picker open", |v| {
        matches!(v.shell.active_modal, ActiveModal::ModelPicker(_))
    });
    snap(&vc, &view, |v| {
        // Partial discovery failure is surfaced honestly, not hidden.
        assert!(has_notice(v, "Some providers could not be queried"));
        let ActiveModal::ModelPicker(p) = &v.shell.active_modal else {
            panic!("picker should be open");
        };
        // Entries come from the backend catalog, including the local origin tag.
        assert_eq!(p.models.len(), 3);
        assert!(p.models.iter().any(|m| m.id == "qwen2.5-coder-32b" && m.is_local));
        assert!(!p.models.iter().any(|m| m.id == "claude-3-7-sonnet"));
    });

    // Typing belongs to the picker, never the composer hidden behind it.
    vc.simulate_input("o3");
    snap(&vc, &view, |v| {
        let ActiveModal::ModelPicker(p) = &v.shell.active_modal else {
            panic!("picker should be open");
        };
        assert_eq!(p.search_query, "o3");
        assert_eq!(p.filtered_models().len(), 1);
        assert_eq!(p.highlighted_model().unwrap().id, "o3-mini");
        assert_eq!(v.composer_text(), "");
    });
    vc.simulate_keystrokes("backspace backspace");
    snap(&vc, &view, |v| {
        let ActiveModal::ModelPicker(p) = &v.shell.active_modal else {
            panic!("picker should be open");
        };
        assert_eq!(p.search_query, "");
        assert_eq!(v.composer_text(), "");
    });

    // The active model is absent from the catalog, so the first entry (gpt-4o)
    // is highlighted; Enter selects it and dispatches the backend switch.
    vc.simulate_keystrokes("enter");
    pump_until(&mut vc, &view, "backend-confirmed model switch", |v| {
        v.shell.composer.model_pill.model_name == "gpt-4o"
    });
    snap(&vc, &view, |v| {
        assert_eq!(v.shell.active_modal, ActiveModal::None);
        assert_eq!(v.shell.composer.model_pill.provider_name, "OpenAI");
        assert_eq!(v.shell.sidebar.provider_status.model_name, "gpt-4o");
        let item = v.shell.sidebar.session_list.sessions.iter().find(|s| s.id == "ses_test_1").unwrap();
        assert_eq!(item.model_name.as_deref(), Some("gpt-4o"));
    });

    // A failed switch must keep the previously confirmed identity.
    state.switch_error.store(true, Ordering::SeqCst);
    *state.switch_model.lock().unwrap() = Some("o3-mini".to_string());
    vc.simulate_keystrokes("cmd-k");
    vc.simulate_input("switch model");
    vc.simulate_keystrokes("enter");
    pump_until(&mut vc, &view, "picker reopen for failed switch", |v| {
        matches!(v.shell.active_modal, ActiveModal::ModelPicker(_))
    });
    vc.simulate_keystrokes("down enter"); // highlight o3-mini, select
    pump_until(&mut vc, &view, "failed switch notice", |v| {
        has_notice(v, "Model switch failed")
    });
    snap(&vc, &view, |v| {
        assert_eq!(v.shell.composer.model_pill.model_name, "gpt-4o");
        assert_eq!(v.shell.sidebar.provider_status.model_name, "gpt-4o");
    });
}

/// B32: a total discovery failure surfaces an honest error and never opens a
/// picker over an invented/hardcoded catalog.
#[gpui::test]
async fn test_model_catalog_total_failure_is_honest(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();

    let state = MockState::new();
    state.catalog_fail.store(true, Ordering::SeqCst);

    let (client_read, server_write) = tokio::io::duplex(64 * 1024);
    let (server_read, client_write) = tokio::io::duplex(64 * 1024);
    let (_shutdown_tx, shutdown_rx) = watch::channel(false);
    rt.spawn(run_mock_server(server_read, server_write, state.clone(), shutdown_rx));

    let bridge = AcpBridge::new();
    rt.block_on(bridge.connect_streams(client_read, client_write))
        .expect("ACP handshake");
    let (view, mut vc) = build_view(cx, &rt, &bridge);
    submit_initial_prompt(&mut vc, &view, "start a conversation");

    vc.simulate_keystrokes("cmd-k");
    vc.simulate_input("switch model");
    vc.simulate_keystrokes("enter");
    pump_until(&mut vc, &view, "honest catalog failure notice", |v| {
        has_notice(v, "Could not load the model catalog")
    });
    snap(&vc, &view, |v| {
        assert_eq!(v.shell.active_modal, ActiveModal::None);
        assert_eq!(v.shell.composer.model_pill.model_name, "claude-3-7-sonnet");
    });
}

/// Resume: durable on-disk sessions appear in the sidebar via session/list, and
/// switching to one never hydrated in this process loads its transcript from the
/// backend instead of showing an empty feed.
#[gpui::test]
async fn test_resume_durable_session_from_backend(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();

    let state = MockState::new();
    *state.list_sessions.lock().unwrap() = vec![
        session_item("ses_test_1", "claude-3-7-sonnet"),
        session_item("ses_disk_99", "claude-3-7-sonnet"),
    ];
    *state.load_history.lock().unwrap() =
        vec![("Resumed prompt".to_string(), "Resumed answer".to_string())];

    let (client_read, server_write) = tokio::io::duplex(64 * 1024);
    let (server_read, client_write) = tokio::io::duplex(64 * 1024);
    let (_shutdown_tx, shutdown_rx) = watch::channel(false);
    rt.spawn(run_mock_server(server_read, server_write, state.clone(), shutdown_rx));

    let bridge = AcpBridge::new();
    rt.block_on(bridge.connect_streams(client_read, client_write))
        .expect("ACP handshake");
    let (view, mut vc) = build_view(cx, &rt, &bridge);

    // Startup lists durable sessions but stays on Home without creating a chat.
    pump_until(&mut vc, &view, "durable sessions listed", |v| {
        v.shell.active_session_id.is_none()
            && v.shell.feed.is_empty()
            && v.shell.sidebar.session_list.sessions.len() == 2
    });
    snap(&vc, &view, |v| {
        assert_eq!(state.session_counter.load(Ordering::SeqCst), 0);
        let item = v.shell.sidebar.session_list.sessions.iter().find(|s| s.id == "ses_disk_99").unwrap();
        assert_eq!(item.model_name.as_deref(), Some("claude-3-7-sonnet"));
        assert!(v.shell.feed.is_empty(), "listing must not load a transcript");
    });

    // Switch to the never-hydrated session through the palette; it resumes from
    // durable backend history.
    vc.simulate_keystrokes("cmd-k");
    vc.simulate_input("isk_99");
    vc.simulate_keystrokes("enter");
    pump_until(&mut vc, &view, "durable session resumed", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_disk_99")
            && last_assistant(v).map(|a| a.content.as_str()) == Some("Resumed answer")
    });
    snap(&vc, &view, |v| {
        assert_eq!(last_user(v).as_deref(), Some("Resumed prompt"));
        assert_eq!(v.shell.composer.model_pill.model_name, "claude-3-7-sonnet");
    });
}

#[gpui::test]
async fn test_latest_session_resume_wins_out_of_order(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();

    let state = MockState::new();
    *state.list_sessions.lock().unwrap() = vec![
        session_item("ses_test_1", "claude-3-7-sonnet"),
        session_item("ses_disk_a", "claude-3-7-sonnet"),
        session_item("ses_disk_b", "claude-3-7-sonnet"),
    ];
    let gate = Arc::new(Notify::new());
    state
        .load_gates
        .lock()
        .unwrap()
        .insert("ses_disk_a".to_string(), gate.clone());
    let mut histories = state.load_histories.lock().unwrap();
    histories.insert(
        "ses_disk_a".to_string(),
        vec![("Prompt A".to_string(), "Answer A".to_string())],
    );
    histories.insert(
        "ses_disk_b".to_string(),
        vec![("Prompt B".to_string(), "Answer B".to_string())],
    );
    drop(histories);

    let (client_read, server_write) = tokio::io::duplex(64 * 1024);
    let (server_read, client_write) = tokio::io::duplex(64 * 1024);
    let (_shutdown_tx, shutdown_rx) = watch::channel(false);
    rt.spawn(run_mock_server(server_read, server_write, state.clone(), shutdown_rx));

    let bridge = AcpBridge::new();
    rt.block_on(bridge.connect_streams(client_read, client_write))
        .expect("ACP handshake");
    let (view, mut vc) = build_view(cx, &rt, &bridge);
    pump_until(&mut vc, &view, "durable sessions listed", |v| {
        v.shell.active_session_id.is_none()
            && v.shell.feed.is_empty()
            && v.shell.sidebar.session_list.sessions.len() == 3
    });
    assert_eq!(state.session_counter.load(Ordering::SeqCst), 0);

    vc.simulate_keystrokes("cmd-k");
    vc.simulate_input("disk_a");
    vc.simulate_keystrokes("enter");
    pump_until(&mut vc, &view, "first resume request started", |_| {
        state.load_started.load(Ordering::SeqCst) == 1
    });

    vc.simulate_keystrokes("cmd-k");
    vc.simulate_input("disk_b");
    vc.simulate_keystrokes("enter");
    pump_until(&mut vc, &view, "newer session resumed", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_disk_b")
            && last_assistant(v).map(|a| a.content.as_str()) == Some("Answer B")
    });

    gate.notify_one();
    pump_until(&mut vc, &view, "stale resume response delivered", |_| {
        state.load_finished.load(Ordering::SeqCst) == 2
    });
    vc.run_until_parked();
    snap(&vc, &view, |v| {
        assert_eq!(v.shell.active_session_id.as_deref(), Some("ses_disk_b"));
        assert_eq!(last_assistant(v).unwrap().content, "Answer B");
    });
}

#[gpui::test]
async fn test_session_list_reconciles_stale_entries_after_reconnect(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();

    let state_a = MockState::new();
    *state_a.list_sessions.lock().unwrap() = vec![
        session_item("ses_stale", "claude-3-7-sonnet"),
        session_item("ses_keep", "claude-3-7-sonnet"),
    ];
    let (client_read, server_write) = tokio::io::duplex(64 * 1024);
    let (server_read, client_write) = tokio::io::duplex(64 * 1024);
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    rt.spawn(run_mock_server(server_read, server_write, state_a.clone(), shutdown_rx));

    let bridge = AcpBridge::new();
    rt.block_on(bridge.connect_streams(client_read, client_write))
        .expect("ACP handshake");
    let (view, mut vc) = build_view(cx, &rt, &bridge);
    pump_until(&mut vc, &view, "initial durable sessions", |v| {
        v.shell.active_session_id.is_none()
            && v.shell.sidebar.session_list.sessions.len() == 2
    });

    let state_b = MockState::new();
    *state_b.list_sessions.lock().unwrap() = vec![
        session_item("ses_keep", "claude-3-7-sonnet"),
        session_item("ses_new", "claude-3-7-sonnet"),
    ];
    let (client_read_b, server_write_b) = tokio::io::duplex(64 * 1024);
    let (server_read_b, client_write_b) = tokio::io::duplex(64 * 1024);
    let (_shutdown_tx_b, shutdown_rx_b) = watch::channel(false);
    rt.spawn(run_mock_server(server_read_b, server_write_b, state_b, shutdown_rx_b));

    shutdown_tx.send(true).unwrap();
    pump_until(&mut vc, &view, "reconnect armed on Home", |v| {
        v.shell.sidebar.provider_status.state == ProviderState::Reconnecting
    });
    rt.block_on(bridge.connect_streams(client_read_b, client_write_b))
        .expect("reconnect handshake");
    pump_until(&mut vc, &view, "reconciled durable sessions", |v| {
        v.shell.active_session_id.is_none()
            && v.shell.sidebar.session_list.sessions.iter().any(|s| s.id == "ses_new")
            && !v.shell.sidebar.session_list.sessions.iter().any(|s| s.id == "ses_stale")
    });
    snap(&vc, &view, |v| {
        assert_eq!(v.shell.sidebar.session_list.sessions.len(), 2);
        assert!(v.shell.sidebar.session_list.sessions.iter().any(|s| s.id == "ses_keep"));
    });
    assert_eq!(state_a.session_counter.load(Ordering::SeqCst), 0);
}

#[gpui::test]
async fn test_session_list_metadata_order_and_empty_filtering(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();

    let state = MockState::new();
    *state.list_sessions.lock().unwrap() = vec![
        json!({"sessionId": "ses_empty", "cwd": "/workspace", "model": "claude-3-7-sonnet", "createdAt": 50.0, "turns": 0}),
        json!({"sessionId": "ses_older", "cwd": "/workspace", "model": "claude-3-7-sonnet", "createdAt": 100.0, "turns": 3, "title": "Older"}),
        json!({"sessionId": "ses_newer", "cwd": "/workspace", "model": "claude-3-7-sonnet", "createdAt": 200.0, "turns": 1, "title": "  Newer   chat  "}),
        json!({"sessionId": "ses_pinned", "cwd": "/workspace", "model": "claude-3-7-sonnet", "createdAt": 10.0, "turns": 1, "title": "Pinned", "pinned": true}),
    ];

    let (client_read, server_write) = tokio::io::duplex(64 * 1024);
    let (server_read, client_write) = tokio::io::duplex(64 * 1024);
    let (_shutdown_tx, shutdown_rx) = watch::channel(false);
    rt.spawn(run_mock_server(server_read, server_write, state.clone(), shutdown_rx));

    let bridge = AcpBridge::new();
    rt.block_on(bridge.connect_streams(client_read, client_write))
        .expect("ACP handshake");
    let (view, mut vc) = build_view(cx, &rt, &bridge);
    pump_until(&mut vc, &view, "ordered durable sessions", |v| {
        v.shell.active_session_id.is_none()
            && v.shell.feed.is_empty()
            && v.shell.sidebar.session_list.sessions.len() == 3
    });
    snap(&vc, &view, |v| {
        let sessions = &v.shell.sidebar.session_list.sessions;
        assert_eq!(
            sessions.iter().map(|session| session.id.as_str()).collect::<Vec<_>>(),
            vec!["ses_pinned", "ses_newer", "ses_older"]
        );
        assert_eq!(sessions[0].created_at.timestamp(), 10);
        assert_eq!(sessions[1].created_at.timestamp(), 200);
        assert_eq!(sessions[2].created_at.timestamp(), 100);
        assert_eq!(sessions[1].title, "Newer chat");
        assert_eq!(sessions[1].message_count, 1);
        assert_eq!(sessions[2].message_count, 3);
        assert!(sessions[0].is_pinned);
        assert!(!sessions.iter().any(|session| session.id == "ses_empty"));
        assert!(v.shell.feed.is_empty());
    });
    assert_eq!(state.session_counter.load(Ordering::SeqCst), 0);
    assert!(state.list_requests.load(Ordering::SeqCst) >= 1);
}

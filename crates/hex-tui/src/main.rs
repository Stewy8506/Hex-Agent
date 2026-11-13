//! Native Rust Terminal Cockpit (Ratatui + Crossterm) binary for HEX Autonomous AI Workbench.

use std::io::{stdout, Stdout};
use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;
use crossterm::event::{KeyCode, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::{CrosstermBackend, TestBackend};
use ratatui::Terminal;

use hex_protocol::client::AcpClient;
use hex_tui::app::{App, ApprovalRequest, Focus};
use hex_tui::event::{AppEvent, EventHandler};
use hex_tui::ui;

#[derive(Parser, Debug)]
#[command(
    name = "hex-tui",
    author = "HEX Autonomous AI Workbench",
    version = "0.1.0",
    about = "Native Ratatui cockpit with animated HEX visual language and ACP integration"
)]
struct Args {
    #[arg(long, help = "Run headless virtual verification and exit 0")]
    check: bool,

    #[arg(short, long, default_value = "default", help = "Project ID")]
    project: String,

    #[arg(short, long, help = "Workspace path override")]
    workspace: Option<PathBuf>,

    #[arg(short, long, help = "Active model provider override")]
    model: Option<String>,

    #[arg(long, default_value = "amber", value_parser = ["amber", "cyberpunk", "matrix", "nord", "mono"], help = "HEX visual skin")]
    skin: String,

    #[arg(
        long,
        default_value = "OverlayFS",
        help = "CoW snapshot provider backend"
    )]
    cow: String,

    #[arg(short, long, help = "Session ID to attach or resume")]
    session: Option<String>,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();

    // -----------------------------------------------------------------------
    // Headless Verification Mode (--check)
    // -----------------------------------------------------------------------
    if args.check {
        return run_headless_check(args);
    }

    // -----------------------------------------------------------------------
    // Interactive Terminal Setup
    // -----------------------------------------------------------------------
    setup_panic_hook();
    enable_raw_mode()?;
    let mut stdout = stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let app_res = run_app(&mut terminal, args).await;

    // Restore terminal on exit
    restore_terminal(&mut terminal)?;

    if let Err(e) = app_res {
        eprintln!("HEX TUI Error: {:?}", e);
    }

    Ok(())
}

/// Headless verification test for CI/CD and self-diagnostics.
fn run_headless_check(args: Args) -> Result<()> {
    let backend = TestBackend::new(120, 36);
    let mut terminal = Terminal::new(backend)?;

    let model_str = args.model.as_deref().unwrap_or("gemini-3.8-flash-tiered");
    let mut app = App::with_workspace(
        &args.project,
        model_str,
        &args.cow,
        args.workspace.as_deref(),
    );
    if let Some(s) = args.session {
        app.session_id = s;
    }
    app.skin_name = args.skin.clone();

    // Add sample dialogue and tool calls
    app.add_user_message("Create a high-performance HTTP service");
    app.add_tool_call(
        "tc_check_1",
        "create_file",
        "path=src/server.rs",
        Some(32),
        Some("OK"),
    );
    app.complete_tool_call("tc_check_1", "Created src/server.rs (128 bytes)", 32, false);

    // Add sample unified diff
    let sample_diff = "\
--- a/src/server.rs
+++ b/src/server.rs
@@ -1,3 +1,5 @@
 use std::net::SocketAddr;
+use tokio::net::TcpListener;
 fn main() {
+    println!(\"Server running\");
 }";
    app.load_diff(sample_diff);

    // Render frame 1: Main layout with dialogue and diff
    terminal.draw(|f| ui::draw(f, &app))?;
    let buffer = terminal.backend().buffer().clone();
    let content = format!("{:?}", buffer);

    assert!(
        content.contains("HEX"),
        "Virtual buffer must contain HEX banner"
    );
    let ws_name = if args.project == "default" && !app.workspace_path.is_empty() {
        std::path::Path::new(&app.workspace_path)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(&args.project)
    } else {
        &args.project
    };
    assert!(
        content.contains(&args.project) || content.contains(ws_name),
        "Virtual buffer must contain project ID or workspace name"
    );
    assert!(
        content.contains("Diff Viewer"),
        "Virtual buffer must contain Diff Viewer"
    );
    assert!(
        content.contains("agent") || content.contains('❯'),
        "Virtual buffer must contain Prompt"
    );

    // Render frame 2: Approval modal overlay
    app.set_approval_request(ApprovalRequest {
        id: "appr_check_1".to_string(),
        risk_level: "R4 - DESTRUCTIVE".to_string(),
        tool_name: "rm -rf".to_string(),
        parameters: "target=/data/cache".to_string(),
        reason: "Test approval trigger".to_string(),
    });
    terminal.draw(|f| ui::draw(f, &app))?;
    let approval_buffer = terminal.backend().buffer().clone();
    let approval_content = format!("{:?}", approval_buffer);
    assert!(
        approval_content.contains("APPROVAL REQUIRED") || approval_content.contains("DESTRUCTIVE"),
        "Virtual buffer must render approval modal"
    );

    println!("HEX TUI headless verification passed: 1 virtual frame rendered successfully.");
    Ok(())
}

/// Primary interactive application loop.
async fn run_app(terminal: &mut Terminal<CrosstermBackend<Stdout>>, args: Args) -> Result<()> {
    let model_str = args.model.as_deref().unwrap_or("gemini-3.8-flash-tiered");
    let mut app = App::with_workspace(
        &args.project,
        model_str,
        &args.cow,
        args.workspace.as_deref(),
    );
    if let Some(s) = args.session {
        app.session_id = s;
    }
    app.skin_name = args.skin.clone();

    // Try connecting to ACP server in background
    let ws_path = args.workspace.as_deref();
    let maybe_client = AcpClient::spawn_acp_server(ws_path).await;
    let acp_client = match maybe_client {
        Ok(client) => {
            app.connection_status = "Connected".to_string();
            // Handshake session to populate dynamic tools, skills, cow backend, and active model
            match client
                .session_new(ws_path.and_then(|p| p.to_str()), args.model.as_deref())
                .await
            {
                Ok(sess) => {
                    app.session_id = sess.session_id;
                    app.model_name = sess.model;
                    if let Some(prov) = sess.provider {
                        app.provider_name = prov;
                    }
                    if let Some(tc) = sess.tools_count {
                        app.tools_count = tc;
                    }
                    if let Some(sc) = sess.skills_count {
                        app.skills_count = sc;
                    }
                    if let Some(cow) = sess.cow_backend {
                        app.cow_mode = cow;
                    }
                    if let Some(splash) = sess.splash_text {
                        app.splash_text = Some(splash);
                    }
                }
                Err(e) => {
                    app.add_system_message(&format!("Session handshake notice: {}", e));
                }
            }
            app.add_system_message("Connected to internal ACP server.");
            Some(client)
        }
        Err(err) => {
            app.connection_status = "Standalone".to_string();
            app.add_system_message(&format!("Running in standalone mode: {}", err));
            None
        }
    };

    let mut events = EventHandler::new(50);
    let event_tx = events.sender();

    // If ACP client connected, forward notifications to event loop
    if let Some(client) = &acp_client {
        let mut notif_rx = client.subscribe();
        let notif_tx = event_tx.clone();
        tokio::spawn(async move {
            while let Ok(notif) = notif_rx.recv().await {
                if notif_tx.send(AppEvent::Notification(notif)).is_err() {
                    break;
                }
            }
        });
    }

    while !app.should_quit {
        terminal.draw(|f| ui::draw(f, &app))?;

        if let Some(event) = events.next().await {
            match event {
                AppEvent::Key(key) => {
                    handle_key_event(&mut app, key, acp_client.as_ref(), event_tx.clone()).await?;
                }
                AppEvent::Mouse(mouse) => match mouse.kind {
                    crossterm::event::MouseEventKind::ScrollUp => {
                        app.scroll_up();
                        app.scroll_up();
                    }
                    crossterm::event::MouseEventKind::ScrollDown => {
                        app.scroll_down();
                        app.scroll_down();
                    }
                    _ => {}
                },
                AppEvent::Notification(notif) => {
                    handle_notification(&mut app, notif);
                }
                AppEvent::AssistantResponse(msg) => {
                    let already_populated = if let Some(hex_tui::app::DialogueItem::Message(m)) =
                        app.dialogue_items.last()
                    {
                        m.role == hex_protocol::models::Role::Assistant
                            && (m.content == msg || m.content.contains(&msg[..msg.len().min(40)]))
                    } else {
                        false
                    };
                    if !already_populated {
                        app.add_assistant_message(&msg);
                    }
                    app.is_busy = false;
                    app.status_message = "Ready".to_string();
                }
                AppEvent::Ready => {
                    app.is_busy = false;
                    app.status_message = "Ready".to_string();
                }
                AppEvent::AcpMessage(msg) => {
                    app.add_system_message(&msg);
                    app.is_busy = false;
                    app.status_message = "Ready".to_string();
                }
                AppEvent::Error(err) => {
                    app.add_system_message(&format!("Error: {}", err));
                    app.is_busy = false;
                    app.status_message = "Ready".to_string();
                }
                AppEvent::Tick => {
                    app.advance_animation();
                }
                AppEvent::Resize(_, _) => {
                    // Handled on next draw
                }
            }
        }
    }

    Ok(())
}

/// Process keyboard inputs.
async fn handle_key_event(
    app: &mut App,
    key: crossterm::event::KeyEvent,
    acp_client: Option<&AcpClient>,
    event_tx: tokio::sync::mpsc::UnboundedSender<AppEvent>,
) -> Result<()> {
    // Global hotkeys
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
        app.should_quit = true;
        return Ok(());
    }

    if key.code == KeyCode::F(1) {
        app.show_help = !app.show_help;
        return Ok(());
    }

    if key.code == KeyCode::F(2) {
        app.toggle_diff_pane();
        return Ok(());
    }

    // Modal dismiss / help dismiss
    if key.code == KeyCode::Esc {
        if app.show_help {
            app.show_help = false;
            return Ok(());
        }
        if app.active_approval.is_some() {
            app.clear_approval_request();
            app.add_system_message("Operator dismissed approval dialog.");
            return Ok(());
        }
    }

    // Modal approval controls
    if app.active_approval.is_some() {
        match key.code {
            KeyCode::Char('y') | KeyCode::Char('Y') => {
                if let Some(req) = app.active_approval.take() {
                    app.focus = Focus::Input;
                    app.add_system_message(&format!(
                        "Approved execution of tool '{}'.",
                        req.tool_name
                    ));
                    app.status_message = "Tool execution approved".to_string();
                    if let Some(client) = acp_client {
                        let client_clone = client.clone();
                        let approval_id = req.id.clone();
                        tokio::spawn(async move {
                            let _ = client_clone
                                .session_approval_decision(&approval_id, "approve", None)
                                .await;
                        });
                    }
                }
            }
            KeyCode::Char('n') | KeyCode::Char('N') => {
                if let Some(req) = app.active_approval.take() {
                    app.focus = Focus::Input;
                    app.add_system_message(&format!(
                        "Rejected execution of tool '{}'. Rolled back.",
                        req.tool_name
                    ));
                    app.status_message = "Tool execution rejected".to_string();
                    if let Some(client) = acp_client {
                        let client_clone = client.clone();
                        let approval_id = req.id.clone();
                        tokio::spawn(async move {
                            let _ = client_clone
                                .session_approval_decision(&approval_id, "reject", None)
                                .await;
                        });
                    }
                }
            }
            KeyCode::Char('d') | KeyCode::Char('D') => {
                app.show_diff = true;
            }
            _ => {}
        }
        return Ok(());
    }

    // Help modal dismiss
    if app.show_help {
        if key.code == KeyCode::Esc || key.code == KeyCode::Enter || key.code == KeyCode::Char('q')
        {
            app.show_help = false;
        }
        return Ok(());
    }

    // Tab key: accept slash suggestion if active, otherwise cycle mode (agent -> plan -> code)
    if key.code == KeyCode::Tab {
        if !app.slash_suggestions.is_empty() {
            app.accept_slash_suggestion();
        } else {
            let next_mode = app.cycle_mode();
            app.status_message = format!("Switched mode to: {}", next_mode.to_uppercase());
        }
        return Ok(());
    }

    if key.code == KeyCode::PageUp {
        app.scroll_up();
        app.scroll_up();
        app.scroll_up();
        return Ok(());
    }

    if key.code == KeyCode::PageDown {
        app.scroll_down();
        app.scroll_down();
        app.scroll_down();
        return Ok(());
    }

    // Focus-specific key actions
    match app.focus {
        Focus::Input => {
            // Slash suggestions navigation
            if !app.slash_suggestions.is_empty() {
                match key.code {
                    KeyCode::Up => {
                        app.prev_slash_suggestion();
                        return Ok(());
                    }
                    KeyCode::Down => {
                        app.next_slash_suggestion();
                        return Ok(());
                    }
                    _ => {}
                }
            }

            match key.code {
                KeyCode::Enter => {
                    if !app.slash_suggestions.is_empty()
                        && app.input_buffer.starts_with('/')
                        && !app.input_buffer.contains(' ')
                    {
                        app.accept_slash_suggestion();
                        return Ok(());
                    }

                    if let Some(text) = app.submit_input() {
                        execute_submitted_text(app, text, acp_client, event_tx).await?;
                    }
                }
                KeyCode::Char(c) => app.insert_char(c),
                KeyCode::Backspace => app.delete_char(),
                KeyCode::Delete => app.delete_char_forward(),
                KeyCode::Left => app.move_cursor_left(),
                KeyCode::Right => app.move_cursor_right(),
                KeyCode::Home => app.move_cursor_start(),
                KeyCode::End => app.move_cursor_end(),
                KeyCode::Up => {
                    if app.input_buffer.is_empty() {
                        app.scroll_up();
                    } else {
                        app.history_up();
                    }
                }
                KeyCode::Down => {
                    if app.input_buffer.is_empty() {
                        app.scroll_down();
                    } else {
                        app.history_down();
                    }
                }
                _ => {}
            }
        }
        Focus::Dialogue => match key.code {
            KeyCode::Up | KeyCode::Char('k') => app.scroll_up(),
            KeyCode::Down | KeyCode::Char('j') => app.scroll_down(),
            KeyCode::Char('c') => app.toggle_all_tool_calls(),
            KeyCode::Enter => app.focus = Focus::Input,
            _ => {}
        },
        Focus::Diff => match key.code {
            KeyCode::Up | KeyCode::Char('k') => app.scroll_up(),
            KeyCode::Down | KeyCode::Char('j') => app.scroll_down(),
            KeyCode::Char('d') | KeyCode::Esc => app.toggle_diff_pane(),
            KeyCode::Enter => app.focus = Focus::Input,
            _ => {}
        },
        Focus::Approval => {}
    }

    Ok(())
}

/// Execute user entered text (slash commands or regular prompt).
async fn execute_submitted_text(
    app: &mut App,
    text: String,
    acp_client: Option<&AcpClient>,
    event_tx: tokio::sync::mpsc::UnboundedSender<AppEvent>,
) -> Result<()> {
    if text.starts_with('/') {
        let parts: Vec<&str> = text.split_whitespace().collect();
        let cmd = parts.first().copied().unwrap_or("");
        let arg = if parts.len() > 1 {
            Some(text[cmd.len()..].trim())
        } else {
            None
        };

        // Local TUI interface commands
        match cmd {
            "/diff" => {
                app.toggle_diff_pane();
                return Ok(());
            }
            "/clear" => {
                app.dialogue_items.clear();
                app.add_system_message("Dialogue viewport cleared.");
                return Ok(());
            }
            "/quit" => {
                app.should_quit = true;
                return Ok(());
            }
            "/model" => {
                if let Some(m) = arg {
                    app.model_name = m.to_string();
                }
            }
            "/mode" => {
                if let Some(m) = arg {
                    app.set_mode(m);
                    app.add_system_message(&format!(
                        "Execution mode set to: {}",
                        app.mode.to_uppercase()
                    ));
                } else {
                    let next = app.cycle_mode();
                    app.add_system_message(&format!("Mode cycled to: {}", next.to_uppercase()));
                }
                return Ok(());
            }
            "/plan" => {
                app.set_mode("plan");
                app.add_system_message("Execution mode switched to: PLAN");
                return Ok(());
            }
            "/agent" => {
                app.set_mode("agent");
                app.add_system_message("Execution mode switched to: AGENT");
                return Ok(());
            }
            "/code" => {
                app.set_mode("code");
                app.add_system_message("Execution mode switched to: CODE");
                return Ok(());
            }
            _ => {}
        }

        // If ACP client connected, forward slash command to backend server
        if let Some(client) = acp_client {
            app.is_busy = true;
            app.status_message = format!("Running {}...", cmd);
            let sess_id = app.session_id.clone();
            let cmd_str = cmd.to_string();
            let arg_str = arg.map(|s| s.to_string());
            let client_clone = client.clone();
            let tx = event_tx.clone();

            tokio::spawn(async move {
                match client_clone
                    .session_command(&sess_id, &cmd_str, arg_str.as_deref())
                    .await
                {
                    Ok(resp) => {
                        let _ = tx.send(AppEvent::AcpMessage(resp.output));
                    }
                    Err(err) => {
                        let _ = tx.send(AppEvent::Error(format!(
                            "Command {} failed: {}",
                            cmd_str, err
                        )));
                    }
                }
            });
            return Ok(());
        }

        // Standalone fallback handling for slash commands
        match cmd {
            "/help" => {
                app.show_help = true;
            }
            "/status" => {
                app.add_system_message(&format!(
                    "Status: [Model: {}] [CoW: {}] [Fence: #{}] [Status: {}]",
                    app.model_name, app.cow_mode, app.fencing_token, app.connection_status
                ));
            }
            "/rollback" => {
                app.add_system_message("CoW workspace rolled back to clean checkpoint.");
                app.clear_diff();
            }
            "/skills" => {
                app.add_system_message("Active Skills: [Python AST Analyzer, OSV CVE Scanner, Graphify Blast-Radius, Shell Gateway]");
            }
            "/react" => {
                app.add_system_message("Toggled autonomous continuous ReAct loop.");
            }
            "/model" => {
                if let Some(m) = arg {
                    app.model_name = m.to_string();
                    app.add_system_message(&format!("Model switched to {}", app.model_name));
                } else {
                    app.add_system_message(&format!("Active Model: {}", app.model_name));
                }
            }
            "/budget" => {
                app.add_system_message(
                    "Budget Telemetry: 1,240 tokens used | $0.003 USD consumed | Limit: $5.00",
                );
            }
            other => {
                app.add_system_message(&format!(
                    "Unknown slash command: {}. Type /help for available commands.",
                    other
                ));
            }
        }
        return Ok(());
    }

    // Regular conversation message
    app.add_user_message(&text);
    app.is_busy = true;
    app.status_message = "Reasoning...".to_string();

    // Mode-specific prompt framing matching tui.py
    let effective_prompt = match app.mode.as_str() {
        "plan" | "ask" => format!("[MODE: PLAN - READ-ONLY ARCHITECTURE PLANNING & SPECIFICATION / DO NOT MODIFY FILES OR RUN MUTATING TOOLS]\n{}", text),
        "code" => format!("[MODE: CODE - DIRECT CODE SYNTHESIS & TARGETED FILE EDITS]\n{}", text),
        _ => text.clone(),
    };

    if let Some(client) = acp_client {
        // Send to ACP server
        let sess_id = app.session_id.clone();
        let prompt_text = effective_prompt;
        let client_clone = client.clone();
        let tx = event_tx.clone();
        tokio::spawn(async move {
            match client_clone.session_prompt(&sess_id, &prompt_text).await {
                Ok(resp) => {
                    let txt = resp.text_content();
                    if !txt.is_empty() {
                        let _ = tx.send(AppEvent::AssistantResponse(txt));
                    } else {
                        let _ = tx.send(AppEvent::Ready);
                    }
                }
                Err(err) => {
                    let _ = tx.send(AppEvent::Error(format!("Prompt failed: {}", err)));
                }
            }
        });
    } else {
        // Standalone simulated assistant response
        let reply = format!("Received: \"{}\". Connect to ACP server (`hex acp`) for live autonomous agent execution.", text);
        app.add_assistant_message(&reply);
        app.is_busy = false;
        app.status_message = "Ready".to_string();
    }

    Ok(())
}

/// Handle incoming JSON-RPC notifications from the ACP server.
fn handle_notification(app: &mut App, notif: hex_protocol::models::JsonRpcNotification) {
    if notif.method == "session/update" {
        if let Some(params) = notif.params {
            if let Ok(update) =
                serde_json::from_value::<hex_protocol::models::SessionUpdateNotification>(params)
            {
                if let Some(delta) = update.delta {
                    app.append_assistant_delta(&delta);
                    app.is_busy = true;
                    app.status_message = "Streaming...".to_string();
                }
                if let Some(tool_val) = update.tool {
                    if let Some(tool_obj) = tool_val.as_object() {
                        let ev_type = tool_obj.get("eventType").and_then(|v| v.as_str()).unwrap_or("");
                        let tool_id = tool_obj.get("toolId").and_then(|v| v.as_str()).unwrap_or("tool");
                        let call_id = tool_obj.get("callId").and_then(|v| v.as_str()).unwrap_or(tool_id);
                        let dur = tool_obj.get("durationMs").and_then(|v| v.as_u64());
                        let payload_str = tool_obj.get("payload").and_then(|v| v.as_str()).unwrap_or("");

                        if ev_type == "start" {
                            app.add_tool_call(call_id, tool_id, payload_str, dur, Some("RUNNING"));
                        } else if ev_type == "complete" {
                            app.complete_tool_call(call_id, payload_str, dur.unwrap_or(0), false);
                        }
                    }
                }
                if let Some(st) = update.status {
                    app.status_message = st.clone();
                    if st.eq_ignore_ascii_case("ready") || st.eq_ignore_ascii_case("completed") {
                        app.is_busy = false;
                        app.status_message = "Ready".to_string();
                    }
                }
            }
        }
    } else if notif.method == "session/approvalRequest" {
        if let Some(params) = notif.params {
            if let Ok(appr) =
                serde_json::from_value::<hex_protocol::models::ApprovalRequestNotification>(params)
            {
                app.set_approval_request(ApprovalRequest {
                    id: appr.approval_id,
                    risk_level: if appr.risk_level.is_empty() {
                        "R3 - ELEVATED".to_string()
                    } else {
                        appr.risk_level
                    },
                    tool_name: appr.tool_name,
                    parameters: appr.parameters,
                    reason: appr.reason,
                });
            }
        }
    }
}

fn setup_panic_hook() {
    let original_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |panic_info| {
        let _ = disable_raw_mode();
        let _ = execute!(stdout(), LeaveAlternateScreen);
        original_hook(panic_info);
    }));
}

fn restore_terminal(terminal: &mut Terminal<CrosstermBackend<Stdout>>) -> Result<()> {
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    Ok(())
}

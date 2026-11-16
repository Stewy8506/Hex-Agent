//! Native Rust Terminal Cockpit (Ratatui + Crossterm) binary for HEX Agent.

use std::io::{stdout, Stdout};
use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;
use crossterm::event::{DisableMouseCapture, EnableMouseCapture, KeyCode, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::{CrosstermBackend, TestBackend};
use ratatui::Terminal;

use hex_protocol::client::AcpClient;
use hex_tui::app::{available_slash_commands, App, ApprovalRequest, Focus, ScreenState};
use hex_tui::event::{AppEvent, EventHandler};
use hex_tui::ui;
use hex_tui::ui::design::{format_available_skills, format_available_tools};

#[derive(Parser, Debug)]
#[command(
    name = "hex-tui",
    author = "HEX Agent",
    version = env!("CARGO_PKG_VERSION"),
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

    #[arg(
        long,
        value_parser = ["sapphire", "amber", "cyberpunk", "matrix", "nord", "mono", "light"],
        help = "HEX visual skin"
    )]
    skin: Option<String>,

    #[arg(
        long,
        default_value = "OverlayFS",
        help = "CoW snapshot provider backend"
    )]
    cow: String,

    #[arg(short, long, help = "Session ID to attach or resume")]
    session: Option<String>,

    #[arg(long, help = "Assistant display name (default: HEX)")]
    name: Option<String>,

    #[arg(long, visible_alias = "context-window", help = "Maximum context window tokens")]
    max_tokens: Option<usize>,
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
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
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

    let model_str = args.model.as_deref().unwrap_or("");
    let mut app = App::with_workspace(
        &args.project,
        model_str,
        &args.cow,
        args.workspace.as_deref(),
    );
    if let Some(s) = &args.session {
        app.session_id = s.clone();
    }
    if let Some(s) = args.skin {
        app.skin_name = s;
    }
    if let Some(n) = args.name {
        app.agent_name = n;
    }
    if let Some(tok) = args.max_tokens {
        app.max_tokens = tok;
    }

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

    // Render frame 1: Start Screen (HEX wordmark, workspace, prompt card, shortcut hints)
    terminal.draw(|f| ui::draw(f, &app))?;
    let buffer1 = terminal.backend().buffer().clone();
    let content1 = format!("{:?}", buffer1);
    assert!(
        content1.contains("HEX") || content1.contains("███"),
        "Virtual buffer must contain HEX block wordmark"
    );
    assert!(
        content1.contains("commands") || content1.contains("build"),
        "Virtual buffer must contain prompt hints or action bar"
    );

    // Render frame 2: Active Session Screen (Dialogue stream, diff, right rail context bar)
    app.screen_state = ScreenState::Session;
    terminal.draw(|f| ui::draw(f, &app))?;
    let buffer2 = terminal.backend().buffer().clone();
    let content2 = format!("{:?}", buffer2);
    assert!(
        content2.contains("Create a high-performance HTTP service") || content2.contains("server.rs"),
        "Virtual buffer must contain session dialogue and tool activity"
    );
    assert!(
        content2.contains("context") || content2.contains("plan"),
        "Virtual buffer must contain right rail telemetry"
    );

    // Render frame 3: Approval modal overlay
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
        approval_content.contains("APPROVAL REQUIRED") || approval_content.contains("DESTRUCTIVE") || approval_content.contains("rm -rf"),
        "Virtual buffer must render approval modal"
    );

    println!("HEX TUI headless verification passed: 3 virtual frames (Start, Session, Approval) verified successfully.");
    Ok(())
}

/// Primary interactive application loop.
async fn run_app(terminal: &mut Terminal<CrosstermBackend<Stdout>>, args: Args) -> Result<()> {
    let model_str = args.model.as_deref().unwrap_or("");
    let mut app = App::with_workspace(
        &args.project,
        model_str,
        &args.cow,
        args.workspace.as_deref(),
    );
    if let Some(s) = &args.session {
        app.session_id = s.clone();
    }
    if let Some(s) = args.skin {
        app.skin_name = s;
    }
    if let Some(n) = args.name {
        app.agent_name = n;
    }
    if let Some(tok) = args.max_tokens {
        app.max_tokens = tok;
    }

    // Try connecting to ACP server in background
    let ws_path = args.workspace.as_deref();
    let maybe_client = AcpClient::spawn_acp_server(ws_path).await;
    let acp_client = match maybe_client {
        Ok(client) => {
            app.connection_status = "Connected".to_string();
            // Resume a requested session first; only fall back to a fresh
            // session when the server cannot load the supplied identifier.
            let mut resumed = false;
            if let Some(requested) = args.session.as_deref() {
                match client.session_load(requested).await {
                    Ok(loaded) => {
                        app.session_id = loaded.session_id.clone();
                        app.model_name = loaded.model.clone();
                        app.replay_session_history(&loaded.history);
                        app.add_system_message(&format!(
                            "Resumed session {} ({} prior turn{}).",
                            loaded.session_id,
                            loaded.history_count,
                            if loaded.history_count == 1 { "" } else { "s" }
                        ));
                        resumed = true;
                    }
                    Err(e) => {
                        app.add_system_message(&format!(
                            "Could not resume session {}: {}. Starting a new session.",
                            requested, e
                        ));
                    }
                }
            }
            if !resumed {
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
            }
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
                        if app.screen_state == ScreenState::Start && !app.resume_sessions.is_empty() {
                            app.scroll_resume(-3);
                        } else {
                            app.scroll_up();
                            app.scroll_up();
                        }
                    }
                    crossterm::event::MouseEventKind::ScrollDown => {
                        if app.screen_state == ScreenState::Start && !app.resume_sessions.is_empty() {
                            app.scroll_resume(3);
                        } else {
                            app.scroll_down();
                            app.scroll_down();
                        }
                    }
                    crossterm::event::MouseEventKind::Moved
                    | crossterm::event::MouseEventKind::Drag(_) => {
                        let term_rect = terminal
                            .size()
                            .map(|s| ratatui::layout::Rect::new(0, 0, s.width, s.height))
                            .unwrap_or(ratatui::layout::Rect::new(0, 0, 120, 36));
                        app.handle_mouse_move(mouse.column, mouse.row, term_rect);
                    }
                    crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left)
                        if app.screen_state == ScreenState::Start =>
                    {
                        let term_rect = terminal
                            .size()
                            .map(|s| ratatui::layout::Rect::new(0, 0, s.width, s.height))
                            .unwrap_or(ratatui::layout::Rect::new(0, 0, 120, 36));
                        let resume_rect = app.get_start_resume_area(term_rect);
                        handle_start_mouse_click(&mut app, mouse.column, mouse.row, resume_rect);
                    }
                    _ => {}
                },
                AppEvent::Notification(notif) => {
                    handle_notification(&mut app, notif);
                }
                AppEvent::AssistantResponse(msg) => {
                    let detected_plan = hex_tui::app::extract_plan_steps_from_text(&msg);
                    if !detected_plan.is_empty() {
                        app.plan_steps = detected_plan;
                    }
                    let already_populated = app.active_turn_has_streamed
                        || app.dialogue_items.iter().rev().any(|item| {
                            if let hex_tui::app::DialogueItem::Message(m) = item {
                                if m.role == hex_protocol::models::Role::Assistant {
                                    let check_len = msg.len().min(40);
                                    return check_len > 0
                                        && (m.content == msg
                                            || m.content.contains(&msg[..check_len]));
                                }
                            }
                            false
                        });
                    if !already_populated {
                        app.add_assistant_message(&msg);
                    }
                    app.active_turn_has_streamed = false;
                    app.cancel_requested = false;
                    app.close_running_tool_calls();
                    app.is_busy = false;
                    app.busy_started_at = None;
                    app.status_message = "Ready".to_string();
                }
                AppEvent::Ready => {
                    app.active_turn_has_streamed = false;
                    app.cancel_requested = false;
                    app.close_running_tool_calls();
                    app.is_busy = false;
                    app.busy_started_at = None;
                    app.status_message = "Ready".to_string();
                }
                AppEvent::AcpMessage(msg) => {
                    app.close_running_tool_calls();
                    app.add_system_message(&msg);
                    app.is_busy = false;
                    app.busy_started_at = None;
                    app.status_message = "Ready".to_string();
                }
                AppEvent::Error(err) => {
                    app.close_running_tool_calls();
                    app.add_system_message(&format!("Error: {}", err));
                    app.cancel_requested = false;
                    app.is_busy = false;
                    app.busy_started_at = None;
                    app.status_message = "Ready".to_string();
                }
                AppEvent::Tick => {
                    app.advance_animation();
                    // Turn completion is owned by authoritative server events
                    // (stream_end / terminal status / prompt response), never
                    // by a delta-gap heuristic.
                }
                AppEvent::Resize(_, _) => {
                    // Handled on next draw
                }
            }
        }
    }

    Ok(())
}

/// Ask the server to stop the active turn. With a live connection the busy
/// state is sealed only by the server's authoritative terminal event/response;
/// standalone mode has no server, so it settles locally.
fn request_turn_cancel(app: &mut App, acp_client: Option<&AcpClient>) {
    if let Some(client) = acp_client {
        let client_clone = client.clone();
        let sess_id = app.session_id.clone();
        tokio::spawn(async move {
            let _ = client_clone.session_cancel(&sess_id).await;
        });
        app.status_message = "Cancelling...".to_string();
        app.add_system_message("Cancellation requested; waiting for the server to stop the turn.");
    } else {
        app.active_turn_has_streamed = false;
        app.cancel_requested = false;
        app.close_running_tool_calls();
        app.is_busy = false;
        app.busy_started_at = None;
        app.status_message = "Ready".to_string();
        app.add_system_message("Execution cancelled by operator.");
    }
}

/// Process keyboard inputs.
async fn handle_key_event(
    app: &mut App,
    key: crossterm::event::KeyEvent,
    acp_client: Option<&AcpClient>,
    event_tx: tokio::sync::mpsc::UnboundedSender<AppEvent>,
) -> Result<()> {
    // Global hotkeys: Ctrl+C cancels an in-flight turn first; a second
    // Ctrl+C while cancelling (or Ctrl+C when idle) quits the application.
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
        if app.is_busy && !app.cancel_requested {
            app.cancel_requested = true;
            request_turn_cancel(app, acp_client);
        } else {
            app.should_quit = true;
        }
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

    if key.code == KeyCode::F(3) {
        let next_mode = app.cycle_mode();
        app.status_message = format!("Switched mode to: {}", next_mode.to_uppercase());
        return Ok(());
    }

    // Modal dismiss / help dismiss / interrupt busy execution
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
        if app.cancel_pending_delete() {
            return Ok(());
        }
        if app.is_busy {
            app.cancel_requested = true;
            request_turn_cancel(app, acp_client);
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
        app.handle_tab();
        return Ok(());
    }

    if key.code == KeyCode::PageUp || key.code == KeyCode::PageDown {
        let on_resume_list = app.screen_state == ScreenState::Start
            && app.input_buffer.is_empty()
            && !app.resume_sessions.is_empty();
        if on_resume_list {
            let page = app.resume_visible_count().max(1) as i32;
            app.scroll_resume(if key.code == KeyCode::PageUp { -page } else { page });
        } else if key.code == KeyCode::PageUp {
            app.handle_page_up();
        } else {
            app.handle_page_down();
        }
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

                    if app.is_busy {
                        app.status_message = "Agent busy - please wait... (Esc to cancel)".to_string();
                        return Ok(());
                    }

                    if app.screen_state == ScreenState::Start && app.input_buffer.is_empty() && !app.resume_sessions.is_empty() {
                        if app.selected_resume_index < app.resume_sessions.len() {
                            let session = app.resume_sessions[app.selected_resume_index].clone();
                            app.resume_session(&session.session_id);
                            return Ok(());
                        } else if Some(app.selected_resume_index) == app.resume_toggle_index() {
                            app.toggle_show_all_resume();
                            return Ok(());
                        }
                    }

                    if let Some(text) = app.submit_input() {
                        execute_submitted_text(app, text, acp_client, event_tx).await?;
                    }
                }
                KeyCode::Char('d') => {
                    if app.screen_state == ScreenState::Start && app.input_buffer.is_empty() && !app.resume_sessions.is_empty() {
                        if app.selected_resume_index < app.resume_sessions.len() {
                            app.request_delete_resume_session(app.selected_resume_index);
                            return Ok(());
                        }
                    }
                    app.insert_char('d');
                }
                KeyCode::Char(c) => app.insert_char(c),
                KeyCode::Backspace => app.delete_char(),
                KeyCode::Delete => {
                    if app.screen_state == ScreenState::Start && app.input_buffer.is_empty() && !app.resume_sessions.is_empty() {
                        if app.selected_resume_index < app.resume_sessions.len() {
                            app.request_delete_resume_session(app.selected_resume_index);
                            return Ok(());
                        }
                    }
                    app.delete_char_forward();
                }
                KeyCode::Left => app.move_cursor_left(),
                KeyCode::Right => app.move_cursor_right(),
                KeyCode::Home => app.move_cursor_start(),
                KeyCode::End => app.move_cursor_end(),
                KeyCode::Up => {
                    if app.screen_state == ScreenState::Start && app.input_buffer.is_empty() && !app.resume_sessions.is_empty() {
                        app.move_resume_selection(-1);
                    } else if app.input_buffer.is_empty() {
                        app.scroll_up();
                    } else {
                        app.history_up();
                    }
                }
                KeyCode::Down => {
                    if app.screen_state == ScreenState::Start && app.input_buffer.is_empty() && !app.resume_sessions.is_empty() {
                        app.move_resume_selection(1);
                    } else if app.input_buffer.is_empty() {
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
            "/sbs" => {
                app.toggle_side_by_side();
                if app.side_by_side && (app.active_diff.is_some() || app.raw_diff.is_some()) {
                    app.show_diff = true;
                }
                let mode_str = if app.side_by_side { "side-by-side" } else { "unified" };
                app.add_system_message(&format!("Diff viewer mode: {}", mode_str));
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
            app.busy_started_at = Some(std::time::Instant::now());
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
                app.add_system_message(
                    "/rollback requires an ACP connection (`hex acp`); no workspace changes were made.",
                );
            }
            "/sbs" => {
                app.toggle_side_by_side();
                if app.side_by_side && (app.active_diff.is_some() || app.raw_diff.is_some()) {
                    app.show_diff = true;
                }
                let mode_str = if app.side_by_side { "side-by-side" } else { "unified" };
                app.add_system_message(&format!("Diff viewer mode: {}", mode_str));
            }
            "/skills" => {
                app.add_system_message(&format_available_skills(4));
            }
            "/tools" => {
                app.add_system_message(&format_available_tools(4));
            }
            "/react" => {
                app.add_system_message(
                    "/react requires an ACP connection (`hex acp`); the ReAct loop was not toggled.",
                );
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
                    "/budget requires an ACP connection (`hex acp`); no telemetry is available standalone.",
                );
            }
            other => {
                let known = available_slash_commands().iter().any(|c| c.name == other);
                if known {
                    app.add_system_message(&format!(
                        "{} requires an ACP connection (`hex acp`).",
                        other
                    ));
                } else {
                    app.add_system_message(&format!(
                        "Unknown slash command: {}. Type /help for available commands.",
                        other
                    ));
                }
            }
        }
        return Ok(());
    }

    // Regular conversation message
    app.active_turn_has_streamed = false;
    app.cancel_requested = false;
    app.add_user_message(&text);
    app.is_busy = true;
    app.busy_started_at = Some(std::time::Instant::now());
    app.status_message = "Thinking...".to_string();

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
        app.busy_started_at = None;
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
                let terminal = update.is_turn_terminal();
                let status_val = update.status.clone();
                if let Some(delta) = update.delta {
                    app.append_assistant_delta(&delta);
                    app.is_busy = true;
                    if app.busy_started_at.is_none() {
                        app.busy_started_at = Some(std::time::Instant::now());
                    }
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
                        } else if ev_type == "error" {
                            app.complete_tool_call(call_id, payload_str, dur.unwrap_or(0), true);
                        }
                    }
                }
                // Authoritative terminal events own turn completion.
                if terminal {
                    app.active_turn_has_streamed = false;
                    app.cancel_requested = false;
                    app.close_running_tool_calls();
                    app.is_busy = false;
                    app.busy_started_at = None;
                    let st = status_val.as_deref().unwrap_or("Ready");
                    app.status_message =
                        if st.eq_ignore_ascii_case("ready") || st.eq_ignore_ascii_case("completed") {
                            "Ready".to_string()
                        } else {
                            st.to_string()
                        };
                } else if let Some(st) = status_val {
                    app.status_message = st;
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
        let _ = execute!(stdout(), LeaveAlternateScreen, DisableMouseCapture);
        original_hook(panic_info);
    }));
}

fn restore_terminal(terminal: &mut Terminal<CrosstermBackend<Stdout>>) -> Result<()> {
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen, DisableMouseCapture)?;
    terminal.show_cursor()?;
    Ok(())
}

fn handle_start_mouse_click(app: &mut App, col: u16, row: u16, rect: ratatui::layout::Rect) {
    app.handle_start_mouse_click(col, row, rect);
}

#[cfg(test)]
mod wire_contract_tests {
    //! F04: prove the TUI consumes the same real-server fixtures as the desktop
    //! bridge (tests/fixtures/acp_wire, captured by tests/test_acp_wire_contract.py).
    use super::*;
    use hex_protocol::models::Role;
    use hex_tui::app::DialogueItem;

    fn fixture(name: &str) -> Vec<serde_json::Value> {
        let path = format!(
            "{}/../../tests/fixtures/acp_wire/{}",
            env!("CARGO_MANIFEST_DIR"),
            name
        );
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("missing wire fixture {path}: {e}"));
        text.lines()
            .filter(|ln| !ln.trim().is_empty())
            .map(|ln| serde_json::from_str(ln).unwrap())
            .collect()
    }

    fn replay(app: &mut App, name: &str) {
        for line in fixture(name) {
            if line.get("method").is_some() {
                let notif: hex_protocol::models::JsonRpcNotification =
                    serde_json::from_value(line).unwrap();
                handle_notification(app, notif);
            }
        }
    }

    #[test]
    fn streamed_turn_fixture_drives_app_state() {
        let mut app = App::new("p", "m", "cow");
        app.is_busy = true;
        replay(&mut app, "turn_streamed.jsonl");

        assert!(!app.is_busy, "turn must end unbusy");
        assert_eq!(app.status_message, "Ready");
        assert!(!app.active_turn_has_streamed);

        let assistant_text: String = app
            .dialogue_items
            .iter()
            .filter_map(|d| match d {
                DialogueItem::Message(m) if m.role == Role::Assistant => Some(m.content.as_str()),
                _ => None,
            })
            .collect();
        assert!(assistant_text.contains("Hello world"), "got: {assistant_text}");

        let tool = app
            .dialogue_items
            .iter()
            .find_map(|d| match d {
                DialogueItem::ToolCall(tc) => Some(tc),
                _ => None,
            })
            .expect("tool card must exist");
        assert_eq!(tool.tool, "list_files");
        assert_eq!(tool.exit_status.as_deref(), Some("OK"));
        assert_eq!(tool.duration_ms, Some(12));
        assert!(tool.result.as_deref().unwrap_or("").contains("a.py"));
    }

    #[test]
    fn nonstreamed_turn_fixture_drives_app_state() {
        let mut app = App::new("p", "m", "cow");
        app.is_busy = true;
        replay(&mut app, "turn_nonstreamed.jsonl");

        assert!(!app.is_busy);
        assert_eq!(app.status_message, "Ready");
        let assistant_text: String = app
            .dialogue_items
            .iter()
            .filter_map(|d| match d {
                DialogueItem::Message(m) if m.role == Role::Assistant => Some(m.content.as_str()),
                _ => None,
            })
            .collect();
        assert!(assistant_text.contains("Fixture answer."), "got: {assistant_text}");
    }
}

#[cfg(test)]
mod session_lifecycle_tests {
    //! F05: authoritative completion, cancellation journey, and session resume.
    use super::*;
    use hex_protocol::models::{Role, SessionHistoryTurn};
    use hex_tui::app::DialogueItem;

    fn status_notification(status: &str) -> hex_protocol::models::JsonRpcNotification {
        serde_json::from_value(serde_json::json!({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "acp_sess_lifecycle",
                "updateType": "status",
                "status": status,
            }
        }))
        .unwrap()
    }

    fn ctrl_c() -> crossterm::event::KeyEvent {
        crossterm::event::KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)
    }

    #[test]
    fn cancelled_status_seals_turn_with_truthful_state() {
        let mut app = App::new("p", "m", "cow");
        app.is_busy = true;
        app.cancel_requested = true;
        handle_notification(&mut app, status_notification("Cancelled"));
        assert!(!app.is_busy);
        assert!(!app.cancel_requested);
        assert_eq!(app.status_message, "Cancelled");
    }

    #[test]
    fn failed_status_seals_turn_with_truthful_state() {
        let mut app = App::new("p", "m", "cow");
        app.is_busy = true;
        handle_notification(&mut app, status_notification("Failed"));
        assert!(!app.is_busy);
        assert_eq!(app.status_message, "Failed");
    }

    #[tokio::test]
    async fn ctrl_c_cancels_busy_turn_before_quitting() {
        let mut app = App::new("p", "m", "cow");
        app.is_busy = true;
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();

        // First Ctrl+C while busy: cancel journey, not quit (standalone settles locally).
        handle_key_event(&mut app, ctrl_c(), None, tx.clone()).await.unwrap();
        assert!(!app.should_quit, "first Ctrl+C must cancel, not quit");
        assert!(!app.is_busy, "standalone cancel settles locally");
        assert!(app
            .dialogue_items
            .iter()
            .any(|d| matches!(d, DialogueItem::Message(m) if m.role == Role::System && m.content.contains("cancelled"))));

        // Second Ctrl+C: quit.
        handle_key_event(&mut app, ctrl_c(), None, tx).await.unwrap();
        assert!(app.should_quit);
    }

    #[tokio::test]
    async fn ctrl_c_quits_when_idle() {
        let mut app = App::new("p", "m", "cow");
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        handle_key_event(&mut app, ctrl_c(), None, tx).await.unwrap();
        assert!(app.should_quit);
    }

    #[test]
    fn replay_session_history_restores_dialogue() {
        let mut app = App::new("p", "m", "cow");
        let history = vec![
            SessionHistoryTurn {
                task_id: Some("tsk_1".into()),
                prompt: "first question".into(),
                response: "first answer".into(),
                status: "completed".into(),
            },
            SessionHistoryTurn {
                task_id: Some("tsk_2".into()),
                prompt: "follow-up".into(),
                response: "second answer".into(),
                status: "completed".into(),
            },
        ];
        app.replay_session_history(&history);
        let messages: Vec<(Role, String)> = app
            .dialogue_items
            .iter()
            .filter_map(|d| match d {
                DialogueItem::Message(m) => Some((m.role.clone(), m.content.clone())),
                _ => None,
            })
            .collect();
        // App::new seeds welcome dialogue; the replayed transcript is the tail.
        let tail = &messages[messages.len() - 4..];
        assert_eq!(tail[0], (Role::User, "first question".to_string()));
        assert_eq!(tail[1], (Role::Assistant, "first answer".to_string()));
        assert_eq!(tail[2], (Role::User, "follow-up".to_string()));
        assert_eq!(tail[3], (Role::Assistant, "second answer".to_string()));
    }
}

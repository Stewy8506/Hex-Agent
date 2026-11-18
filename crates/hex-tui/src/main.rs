//! Native Rust Terminal Cockpit (Ratatui + Crossterm) binary for HEX Agent.

use std::io::{stdout, Stdout};
use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;
use crossterm::event::{
    DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
    KeyCode, KeyEventKind, KeyModifiers,
};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::{CrosstermBackend, TestBackend};
use ratatui::Terminal;

use hex_protocol::client::AcpClient;
use hex_tui::app::{available_slash_commands, App, ApprovalRequest, Focus, ScreenState};
use hex_tui::event::{AppEvent, EventHandler, FramePacer};
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

    #[arg(
        long,
        visible_alias = "context-window",
        help = "Maximum context window tokens"
    )]
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
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture, EnableBracketedPaste)?;
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
        content2.contains("Create a high-performance HTTP service")
            || content2.contains("server.rs"),
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
        blast_radius: String::new(),
        cwd: String::new(),
    });
    terminal.draw(|f| ui::draw(f, &app))?;
    let approval_buffer = terminal.backend().buffer().clone();
    let approval_content = format!("{:?}", approval_buffer);
    assert!(
        approval_content.contains("APPROVAL REQUIRED")
            || approval_content.contains("DESTRUCTIVE")
            || approval_content.contains("rm -rf"),
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

    if let Err(error) = app.sidebar.load(std::path::Path::new(&app.workspace_path)) {
        app.add_system_message(&format!("Sidebar preferences unavailable: {error}"));
    }

    // Try connecting to ACP server in background
    let backend_workspace = PathBuf::from(&app.workspace_path);
    let ws_path = Some(backend_workspace.as_path());
    let maybe_client = AcpClient::spawn_acp_server(ws_path).await;
    let acp_client =
        match maybe_client {
            Ok(client) => {
                app.connection_status = "Connected".to_string();
                // Resume a requested session first; only fall back to a fresh
                // session when the server cannot load the supplied identifier.
                let mut resumed = false;
                if let Some(requested) = args.session.as_deref() {
                    match client.session_load_paged(requested).await {
                        Ok(loaded) => {
                            app.session_id = loaded.session_id.clone();
                            app.model_name = loaded.model.clone();
                            for entry in &loaded.page.history {
                                let turn = &entry.turn;
                                if !turn.prompt.is_empty() {
                                    app.add_user_message(&turn.prompt);
                                }
                                if entry.prompt_chars > turn.prompt.chars().count() {
                                    app.add_system_message(&format!(
                                        "Full prompt: /history-turn {} prompt",
                                        entry.turn_index
                                    ));
                                }
                                if !turn.response.is_empty() {
                                    app.add_assistant_message(&turn.response);
                                }
                                if entry.response_chars > turn.response.chars().count() {
                                    app.add_system_message(&format!(
                                        "Full response: /history-turn {} response",
                                        entry.turn_index
                                    ));
                                }
                            }
                            app.add_system_message(&format!(
                            "Resumed session {} ({} prior turn{}). Older turns: /session-history",
                            loaded.session_id,
                            loaded.page.history_count,
                            if loaded.page.history_count == 1 { "" } else { "s" }
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
                            app.tools_count = sess.tools_count;
                            app.skills_count = sess.skills_count;
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

    if let Err(error) = app.enable_transcript_storage() {
        app.add_system_message(&format!(
            "Transcript archive unavailable; history remains in memory: {error}"
        ));
    }
    app.enforce_transcript_budget();
    let mut events = EventHandler::new(50)?;
    let event_tx = events.sender();

    let notification_task = if let Some(client) = &acp_client {
        let mut notif_rx = client.subscribe_lossless().await;
        let notif_tx = event_tx.clone();
        Some(tokio::spawn(async move {
            while let Some(notif) = notif_rx.recv().await {
                if notif_tx.send(AppEvent::Notification(notif)).await.is_err() {
                    return;
                }
            }
            let _ = notif_tx
                .send(AppEvent::Error("ACP connection closed.".into()))
                .await;
        }))
    } else {
        None
    };

    let mut pacer = FramePacer::new(std::time::Duration::from_millis(50));
    let mut last_change = std::time::Instant::now();
    let anim = ui::animation::AnimationState::with_start(app.session_started);
    let mut cursor = anim.cursor_visible();
    let mut placeholder = anim.placeholder_prompt();
    let hover_state = |app: &App| {
        use std::hash::{Hash, Hasher};
        let mut hash = std::collections::hash_map::DefaultHasher::new();
        for level in app
            .row_hover_levels
            .iter()
            .chain(&app.delete_hover_levels)
            .chain(&app.shortcut_hover_levels)
            .chain([&app.input_hover_level, &app.toggle_hover_level])
        {
            level.to_bits().hash(&mut hash);
        }
        hash.finish()
    };
    let result = async {
        while !app.should_quit {
            if pacer.draw_due(std::time::Instant::now()) {
                terminal.draw(|f| ui::draw(f, &app))?;
            }

            if let Some(event) = events.next().await {
                if !matches!(event, AppEvent::Tick) {
                    pacer.mark_dirty();
                    last_change = std::time::Instant::now();
                }
                let storage_was_failed = app.transcript_error.is_some();
                match event {
                    AppEvent::Key(key) => {
                        handle_key_event(&mut app, key, acp_client.as_ref(), event_tx.clone())
                            .await?;
                    }
                    AppEvent::Paste(text) => handle_paste_event(&mut app, &text),
                    AppEvent::Mouse(mouse) => {
                        let term_rect = terminal.size()
                            .map(|s| ratatui::layout::Rect::new(0, 0, s.width, s.height))?;
                        handle_mouse_event(&mut app, mouse, term_rect);
                    },
                    AppEvent::Notification(packet) => {
                        handle_notification(&mut app, packet.notification);
                    }
                    AppEvent::Notice(msg) => app.add_system_message(&msg),
                    AppEvent::AssistantResponse(msg) => {
                        let mut plan_end = msg.len().min(64 * 1024);
                        while !msg.is_char_boundary(plan_end) { plan_end -= 1; }
                        let detected_plan = hex_tui::app::extract_plan_steps_from_text(&msg[..plan_end]);
                        if !detected_plan.is_empty() {
                            app.plan_steps = detected_plan;
                        }
                        let already_populated = app.active_turn_has_streamed
                            || app.dialogue_items.iter().rev().any(|item| {
                                if let hex_tui::app::DialogueItem::Message(m) = item {
                                    if m.role == hex_protocol::models::Role::Assistant {
                                        let check_len = msg
                                            .char_indices()
                                            .nth(40)
                                            .map_or(msg.len(), |(byte, _)| byte);
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
                        app.pending_cancel = None;
                        app.close_running_tool_calls();
                        app.clear_approval_request();
                        app.is_busy = false;
                        app.busy_started_at = None;
                        app.status_message = "Ready".to_string();
                    }
                    AppEvent::Subagents(Ok(agents)) => {
                        app.set_subagent_telemetry(&agents);
                        app.open_subagent_modal(&agents);
                        app.status_message = if agents.is_empty() {
                            "No subagents in this session".to_string()
                        } else {
                            format!("{} subagent(s)", agents.len())
                        };
                    }
                    AppEvent::Subagents(Err(err)) => {
                        app.status_message = "Ready".to_string();
                        app.add_system_message(&err);
                    }
                    AppEvent::Ready => {
                        app.active_turn_has_streamed = false;
                        app.cancel_requested = false;
                        app.pending_cancel = None;
                        app.close_running_tool_calls();
                        app.clear_approval_request();
                        app.is_busy = false;
                        app.busy_started_at = None;
                        app.status_message = "Ready".to_string();
                    }
                    event @ (AppEvent::ApprovalDecision { .. }
                        | AppEvent::CancelResult { .. }
                        | AppEvent::CommandResult { .. }) => handle_control_result(&mut app, event),
                    AppEvent::TerminalError(err) => return Err(anyhow::anyhow!(err)),
                    AppEvent::Error(err) => handle_backend_error(&mut app, &err),
                    AppEvent::Tick => {
                        let before_hover = hover_state(&app);
                        app.advance_animation();
                        let continuous = !anim.is_reduced()
                            && (app.is_busy
                                || app
                                    .plan_steps
                                    .iter()
                                    .any(|step| step.status == hex_tui::app::StepStatus::Active)
                                || last_change.elapsed() < std::time::Duration::from_millis(1200));
                        let next_cursor = anim.cursor_visible();
                        let next_placeholder = anim.placeholder_prompt();
                        if continuous
                            || before_hover != hover_state(&app)
                            || cursor != next_cursor
                            || (app.screen_state == ScreenState::Start
                                && placeholder != next_placeholder)
                        {
                            pacer.mark_dirty();
                        }
                        cursor = next_cursor;
                        placeholder = next_placeholder;
                    }
                    AppEvent::Resize(_, _) => app.approval_can_allow.set(false),
                }
                app.enforce_transcript_budget();
                if !storage_was_failed {
                    if let Some(error) = app.transcript_error.clone() {
                        app.add_system_message(&format!("Transcript persistence failed; eviction stopped and source retained: {error}"));
                        pacer.mark_dirty();
                    }
                }
            }
        }
        Ok::<(), anyhow::Error>(())
    }
    .await;
    if let Some(task) = notification_task {
        task.abort();
    }
    let archived = app.flush_transcript().map_err(anyhow::Error::msg);
    if let Some(client) = &acp_client {
        let _ = client.close().await;
    }
    result.and(archived)
}

fn request_turn_cancel(
    app: &mut App,
    acp_client: Option<&AcpClient>,
    event_tx: tokio::sync::mpsc::Sender<AppEvent>,
) {
    if app.pending_cancel.is_some() {
        return;
    }
    if let Some(client) = acp_client {
        let client = client.clone();
        let session_id = app.session_id.clone();
        app.control_request_id += 1;
        let request_id = app.control_request_id;
        app.pending_cancel = Some(request_id);
        tokio::spawn(async move {
            let result = client.session_cancel(&session_id).await.map_err(|e| e.to_string());
            let _ = event_tx.send(AppEvent::CancelResult { session_id, request_id, result }).await;
        });
        app.status_message = "Cancelling...".to_string();
        app.add_system_message("Cancellation requested; waiting for the server to stop the turn.");
    } else {
        app.active_turn_has_streamed = false;
        app.cancel_requested = false;
        app.close_running_tool_calls();
        app.clear_approval_request();
        app.is_busy = false;
        app.busy_started_at = None;
        app.status_message = "Ready".to_string();
        app.add_system_message("Execution cancelled by operator.");
    }
}

fn submit_approval_decision(
    app: &mut App,
    acp_client: Option<&AcpClient>,
    option: usize,
    event_tx: tokio::sync::mpsc::Sender<AppEvent>,
) {
    if app.approval_pending || app.active_approval.is_none() {
        return;
    }
    if option < 2 && !app.approval_can_allow.get() {
        app.approval_error = Some("Resize to review the full request before allowing; n/Esc denies.".into());
        return;
    }
    let Some(client) = acp_client else {
        app.approval_error = Some("Approval was not applied: no ACP connection. Retry after reconnecting.".into());
        return;
    };
    let client = client.clone();
    let approval_id = app.active_approval.as_ref().unwrap().id.clone();
    let session_id = app.session_id.clone();
    app.approval_generation += 1;
    let generation = app.approval_generation;
    let decision = match option { 0 => "approve", 1 => "approve_scope", _ => "reject" }.to_string();
    app.approval_pending = true;
    app.approval_error = None;
    app.status_message = "Sending approval decision...".into();
    tokio::spawn(async move {
        let result = client.session_approval_decision(&approval_id, &decision, None)
            .await.map_err(|e| e.to_string());
        let _ = event_tx.send(AppEvent::ApprovalDecision {
            session_id, approval_id, generation, decision, result,
        }).await;
    });
}

fn handle_backend_error(app: &mut App, error: &str) {
    app.add_system_message(&format!("Error: {error}"));
    app.pending_cancel = None;
    app.cancel_requested = false;
    if app.active_approval.is_some() {
        if error == "ACP connection closed." {
            app.approval_pending = false;
        }
        app.approval_error = Some(format!("Backend error: {error}. No decision confirmed; retry or cancel."));
        app.focus = Focus::Approval;
        app.status_message = "Approval unresolved; retry or cancel".into();
    } else {
        app.close_running_tool_calls();
        app.pending_command = None;
        app.is_busy = false;
        app.busy_started_at = None;
        app.status_message = "Ready".into();
    }
}

fn handle_control_result(app: &mut App, event: AppEvent) {
    match event {
        AppEvent::ApprovalDecision { session_id, approval_id, generation, decision, result } => {
            if session_id != app.session_id || generation != app.approval_generation
                || !app.approval_pending
                || !app.active_approval.as_ref().is_some_and(|req| req.id == approval_id)
            {
                return;
            }
            app.approval_pending = false;
            match result {
                Ok(response) if response.approval_id == approval_id
                    && response.status == "applied" && response.decision == decision => {
                    let tool = app.active_approval.as_ref().unwrap().tool_name.clone();
                    app.clear_approval_request();
                    let action = match decision.as_str() {
                        "approve" => "Approved once",
                        "approve_scope" => "Approved matching calls in this task",
                        _ => "Denied",
                    };
                    app.add_system_message(&format!("{action}: '{tool}' (server confirmed)."));
                    app.status_message = "Waiting for tool outcome...".into();
                }
                result => {
                    let error = match result {
                        Ok(response) => format!("Server did not apply this decision ({}; approval {}).", response.status, response.approval_id),
                        Err(error) => error,
                    };
                    app.approval_error = Some(format!("Decision not confirmed: {error} Retry or cancel the turn."));
                    app.focus = Focus::Approval;
                    app.status_message = "Approval failed; retry or cancel".into();
                }
            }
        }
        AppEvent::CancelResult { session_id, request_id, result } => {
            if session_id != app.session_id || app.pending_cancel != Some(request_id) {
                return;
            }
            app.pending_cancel = None;
            if !app.is_busy || !app.cancel_requested {
                return;
            }
            match result {
                Ok(response) if response.session_id == session_id && response.cancelled => {
                    app.status_message = "Cancellation accepted; waiting for turn to stop...".into();
                }
                result => {
                    let error = match result {
                        Ok(_) => "server did not confirm cancellation".to_string(),
                        Err(error) => error,
                    };
                    app.cancel_requested = false;
                    app.status_message = "Cancellation failed; retry".into();
                    let retry_key = if app.active_approval.is_some() { "Ctrl+C" } else { "Ctrl+C or Esc" };
                    app.add_system_message(&format!("Cancellation failed: {error}. Press {retry_key} to retry."));
                }
            }
        }
        AppEvent::CommandResult { session_id, request_id, command, result } => {
            if session_id != app.session_id || app.pending_command != Some(request_id) {
                return;
            }
            app.pending_command = None;
            app.pending_cancel = None;
            app.cancel_requested = false;
            app.is_busy = false;
            app.busy_started_at = None;
            app.status_message = "Ready".into();
            match result {
                Ok(response) if response.session_id == session_id && response.command == command => {
                    if response.is_error == Some(true) {
                        app.add_system_message(&format!("Command {command} failed: {}", response.output));
                    } else {
                        if command == "/model" {
                            if let Some(model) = response.model.filter(|model| !model.is_empty()) {
                                app.model_name = model;
                                app.provider_name = response.provider.unwrap_or_default();
                            } else {
                                app.add_system_message("Model change was not confirmed by the backend; label unchanged.");
                            }
                        }
                        app.add_system_message(&response.output);
                    }
                }
                Ok(_) => app.add_system_message(&format!("Command {command} returned a mismatched response; state unchanged.")),
                Err(error) => app.add_system_message(&format!("Command {command} failed: {error}")),
            }
        }
        _ => unreachable!(),
    }
}

fn save_sidebar_preferences(app: &mut App) {
    if let Err(error) = app.sidebar.save() {
        app.add_system_message(&format!("Could not save sidebar preferences: {error}"));
    }
}

fn handle_mouse_event(app: &mut App, mouse: crossterm::event::MouseEvent, area: ratatui::layout::Rect) {
    use crossterm::event::{MouseButton, MouseEventKind};
    if app.show_palette || app.show_help || app.active_approval.is_some()
        || app.active_subagent_modal.is_some() || app.sidebar.show_settings
        || (app.side_by_side && app.show_diff)
    {
        app.sidebar.resizing = false;
        return;
    }
    let rail = app.sidebar.area.get();
    let in_rail = app.screen_state == ScreenState::Session
        && rail.contains((mouse.column, mouse.row).into());
    if app.sidebar.resizing {
        match mouse.kind {
            MouseEventKind::Drag(MouseButton::Left) => {
                let width = area.right().saturating_sub(mouse.column.saturating_add(2))
                    .min(area.width.saturating_sub(43));
                app.sidebar.resize_to(width);
            }
            MouseEventKind::Up(MouseButton::Left) => {
                app.sidebar.resizing = false;
                save_sidebar_preferences(app);
            }
            _ => {}
        }
        return;
    }
    match mouse.kind {
        MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
            let delta = if mouse.kind == MouseEventKind::ScrollUp { -3 } else { 3 };
            if in_rail {
                app.sidebar.scroll_by(delta);
            } else if app.screen_state == ScreenState::Start && !app.resume_sessions.is_empty() {
                app.scroll_resume(delta);
            } else if delta < 0 {
                app.scroll_up();
                app.scroll_up();
            } else {
                app.scroll_down();
                app.scroll_down();
            }
        }
        MouseEventKind::Down(MouseButton::Left) => {
            if app.screen_state == ScreenState::Session && rail.width > 0
                && mouse.column + 1 == rail.x && mouse.row >= rail.y && mouse.row < rail.bottom()
            {
                app.sidebar.resizing = true;
            } else if in_rail {
                if app.sidebar.click(mouse.column, mouse.row) {
                    save_sidebar_preferences(app);
                }
            } else if app.screen_state == ScreenState::Start {
                handle_start_mouse_click(app, mouse.column, mouse.row, area);
            }
        }
        MouseEventKind::Moved | MouseEventKind::Drag(_) => {
            app.handle_mouse_move(mouse.column, mouse.row, area);
        }
        _ => {}
    }
}

fn handle_paste_event(app: &mut App, text: &str) {
    if app.active_approval.is_some() || app.active_subagent_modal.is_some()
        || app.show_help || app.sidebar.show_settings
    {
        return;
    }
    let text: String = text.replace("\r\n", "\n").replace('\r', "\n")
        .chars().filter(|c| !c.is_control() || matches!(*c, '\n' | '\t')).collect();
    if app.show_palette {
        for c in text.chars() {
            app.palette_push_char(if c == '\n' || c == '\t' { ' ' } else { c });
        }
    } else if app.focus == Focus::Input {
        app.insert_text(&text);
    }
}

/// Process keyboard inputs.
async fn handle_key_event(
    app: &mut App,
    key: crossterm::event::KeyEvent,
    acp_client: Option<&AcpClient>,
    event_tx: tokio::sync::mpsc::Sender<AppEvent>,
) -> Result<()> {
    if key.kind != KeyEventKind::Press {
        return Ok(());
    }

    // Global hotkeys: Ctrl+C cancels an in-flight turn first; a second
    // Ctrl+C while cancelling (or Ctrl+C when idle) quits the application.
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
        if app.is_busy && !app.cancel_requested {
            app.cancel_requested = true;
            request_turn_cancel(app, acp_client, event_tx.clone());
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

    if app.active_approval.is_some() {
        match key.code {
            KeyCode::Up => app.move_permission_selection(-1),
            KeyCode::Down => app.move_permission_selection(1),
            KeyCode::Enter => {
                submit_approval_decision(app, acp_client, app.selected_permission_index, event_tx.clone())
            }
            KeyCode::Char('y') | KeyCode::Char('Y') => submit_approval_decision(app, acp_client, 0, event_tx.clone()),
            KeyCode::Char('a') | KeyCode::Char('A') => submit_approval_decision(app, acp_client, 1, event_tx.clone()),
            KeyCode::Esc | KeyCode::Char('n') | KeyCode::Char('N') => submit_approval_decision(app, acp_client, 2, event_tx.clone()),
            KeyCode::Char('d') | KeyCode::Char('D') => app.show_diff = true,
            _ => {}
        }
        return Ok(());
    }

    // Agents drill-down: arrows scroll the activity tail, e expands the prompt,
    // Esc or Tab closes it. Checked before Esc so closing never interrupts a turn.
    if app.active_subagent_modal.is_some() {
        match key.code {
            KeyCode::Esc | KeyCode::Tab => app.close_subagent_modal(),
            KeyCode::Up => app.scroll_subagent_activity(1),
            KeyCode::Down => app.scroll_subagent_activity(-1),
            KeyCode::Char('e') | KeyCode::Char('E') => app.toggle_subagent_prompt(),
            _ => {}
        }
        return Ok(());
    }

    // Modal dismiss / help dismiss / interrupt busy execution
    if key.code == KeyCode::Esc {
        if app.show_palette {
            app.close_palette();
            return Ok(());
        }
        if app.show_help {
            app.show_help = false;
            return Ok(());
        }
        if app.sidebar.show_settings {
            app.sidebar.show_settings = false;
            return Ok(());
        }
        if app.cancel_pending_delete() {
            return Ok(());
        }
        if app.is_busy {
            app.cancel_requested = true;
            request_turn_cancel(app, acp_client, event_tx.clone());
            return Ok(());
        }
    }

    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('p') {
        if app.show_palette {
            app.close_palette();
        } else {
            app.open_palette();
        }
        return Ok(());
    }

    // Command palette: typing edits the query, not the composer.
    if app.show_palette {
        match key.code {
            KeyCode::Enter => {
                let query = app.palette_query.trim().trim_start_matches('/');
                if matches!(
                    query.split_whitespace().next(),
                    Some(
                        "tool-result"
                            | "history"
                            | "transcript"
                            | "session-history"
                            | "history-turn"
                    )
                ) {
                    let command = format!("/{query}");
                    app.close_palette();
                    execute_submitted_text(app, command, acp_client, event_tx).await?;
                } else if let Some(item) = app.palette_selected() {
                    app.close_palette();
                    execute_palette_command(app, &item.command, acp_client, event_tx).await?;
                }
            }
            KeyCode::Up => app.palette_move(-1),
            KeyCode::Down | KeyCode::Tab => app.palette_move(1),
            KeyCode::Backspace => app.palette_backspace(),
            KeyCode::Char(c) => app.palette_push_char(c),
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

    if app.sidebar.show_settings {
        if key.code == KeyCode::Tab {
            app.handle_tab();
        } else if app.sidebar.settings_key(key.code) {
            save_sidebar_preferences(app);
        }
        return Ok(());
    }
    if key.code == KeyCode::F(4) {
        app.sidebar.show_settings = true;
        return Ok(());
    }
    if app.screen_state == ScreenState::Session && key.modifiers.contains(KeyModifiers::CONTROL) {
        match key.code {
            KeyCode::Up => { app.sidebar.scroll_by(-3); return Ok(()); }
            KeyCode::Down => { app.sidebar.scroll_by(3); return Ok(()); }
            _ => {}
        }
    }

    if key.code == KeyCode::Tab {
        app.handle_tab();
        return Ok(());
    }

    if key.code == KeyCode::PageUp || key.code == KeyCode::PageDown {
        if app.screen_state == ScreenState::Start {
            if app.focus == Focus::Resume {
                let page = app.resume_visible_count().max(1) as i32;
                app.scroll_resume(if key.code == KeyCode::PageUp { -page } else { page });
            }
        } else if key.code == KeyCode::PageUp {
            app.handle_page_up();
        } else {
            app.handle_page_down();
        }
        return Ok(());
    }

    // Focus-specific key actions
    match app.focus {
        Focus::Input => match key.code {
            KeyCode::Enter if key.modifiers.contains(KeyModifiers::SHIFT) => app.insert_char('\n'),
            KeyCode::Enter => {
                if app.is_busy {
                    app.status_message = "Agent busy - please wait... (Esc to cancel)".to_string();
                    return Ok(());
                }

                if let Some(text) = app.submit_input() {
                    execute_submitted_text(app, text, acp_client, event_tx).await?;
                }
            }
            KeyCode::Char('/') if app.input_buffer.is_empty() => app.open_palette(),
            KeyCode::Char(c) => app.insert_char(c),
            KeyCode::Backspace => app.delete_char(),
            KeyCode::Delete => app.delete_char_forward(),
            KeyCode::Left => app.move_cursor_left(),
            KeyCode::Right => app.move_cursor_right(),
            KeyCode::Home => app.move_cursor_start(),
            KeyCode::End => app.move_cursor_end(),
            KeyCode::Up => {
                if app.screen_state == ScreenState::Start {
                    app.move_resume_selection(-1);
                } else if app.input_buffer.is_empty() {
                    app.scroll_up();
                } else {
                    app.history_up();
                }
            }
            KeyCode::Down => {
                if app.screen_state == ScreenState::Start {
                    app.move_resume_selection(1);
                } else if app.input_buffer.is_empty() {
                    app.scroll_down();
                } else {
                    app.history_down();
                }
            }
            _ => {}
        },
        Focus::Resume => match key.code {
            KeyCode::Up => app.move_resume_selection(-1),
            KeyCode::Down => app.move_resume_selection(1),
            KeyCode::Enter => {
                if let Some(session) = app.resume_sessions.get(app.selected_resume_index) {
                    let session_id = session.session_id.clone();
                    app.resume_session(&session_id);
                } else if Some(app.selected_resume_index) == app.resume_toggle_index() {
                    app.toggle_show_all_resume();
                }
            }
            KeyCode::Char('d') | KeyCode::Delete => {
                app.request_delete_resume_session(app.selected_resume_index);
            }
            KeyCode::Char('/') => app.open_palette(),
            KeyCode::Char(c) => {
                app.set_start_focus(Focus::Input);
                app.insert_char(c);
            }
            KeyCode::Esc => app.set_start_focus(Focus::Input),
            _ => {}
        },
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

/// Open the agents drill-down. With a live connection the records come from the
/// server's SubagentManager; standalone has no subagents, so the modal says so.
fn open_agents_modal(
    app: &mut App,
    acp_client: Option<&AcpClient>,
    event_tx: tokio::sync::mpsc::Sender<AppEvent>,
) {
    match acp_client {
        Some(client) => {
            let client_clone = client.clone();
            let sess_id = app.session_id.clone();
            app.status_message = "Loading agents...".to_string();
            tokio::spawn(async move {
                let result = client_clone
                    .session_subagents(&sess_id)
                    .await
                    .map_err(|err| format!("Could not load subagents: {}", err));
                let _ = event_tx.send(AppEvent::Subagents(result)).await;
            });
        }
        None => app.open_subagent_modal(&[]),
    }
}

/// Run a palette entry through the same dispatcher as typed slash commands.
async fn execute_palette_command(
    app: &mut App,
    command: &str,
    acp_client: Option<&AcpClient>,
    event_tx: tokio::sync::mpsc::Sender<AppEvent>,
) -> Result<()> {
    if command == "sidebar:settings" {
        app.sidebar.show_settings = true;
        return Ok(());
    }
    if command == "session:switch" {
        app.switch_screen(ScreenState::Start);
        let focus = if app.resume_sessions.is_empty() { Focus::Input } else { Focus::Resume };
        app.set_start_focus(focus);
        app.status_message = "Pick a session to resume".to_string();
        return Ok(());
    }
    execute_submitted_text(app, command.to_string(), acp_client, event_tx).await
}

fn format_history_page(page: &hex_protocol::models::HistoryPage) -> String {
    let mut text = format!(
        "Stored session history ({} total turns):\n",
        page.history_count
    );
    for entry in &page.history {
        let turn = &entry.turn;
        text.push_str(&format!(
            "\nTurn {} [{}]\n> {}\n{}\n",
            entry.turn_index, turn.status, turn.prompt, turn.response
        ));
        for (field, total, shown) in [
            ("prompt", entry.prompt_chars, turn.prompt.chars().count()),
            (
                "response",
                entry.response_chars,
                turn.response.chars().count(),
            ),
        ] {
            if total > shown {
                text.push_str(&format!(
                    "Full {field}: /history-turn {} {field}\n",
                    entry.turn_index
                ));
            }
        }
    }
    if let Some(before) = page.next_before {
        text.push_str(&format!("\nOlder turns: /session-history {before}"));
    }
    text
}

/// Execute user entered text (slash commands or regular prompt).
async fn execute_submitted_text(
    app: &mut App,
    text: String,
    acp_client: Option<&AcpClient>,
    event_tx: tokio::sync::mpsc::Sender<AppEvent>,
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
            "/subagents" => {
                open_agents_modal(app, acp_client, event_tx);
                return Ok(());
            }
            "/diff" => {
                app.toggle_diff_pane();
                return Ok(());
            }
            "/sbs" => {
                app.toggle_side_by_side();
                if app.side_by_side && (app.active_diff.is_some() || app.raw_diff.is_some()) {
                    app.show_diff = true;
                }
                let mode_str = if app.side_by_side {
                    "side-by-side"
                } else {
                    "unified"
                };
                app.add_system_message(&format!("Diff viewer mode: {}", mode_str));
                return Ok(());
            }
            "/history" => {
                let before = parts.get(1).map(|value| value.parse::<usize>()).transpose();
                if parts.len() > 2 || before.is_err() {
                    app.add_system_message("Usage: /history [before]");
                    return Ok(());
                }
                let notice = match app.transcript_store.as_ref() {
                    Some(store) => match store.summary(before.unwrap()) {
                        Ok(summary) => {
                            let mut text = format!(
                                "Native archive ({} stored records):\n",
                                summary.total_count
                            );
                            for line in summary.content.lines() {
                                text.push_str(line);
                                if let Some(id) = line.split_whitespace().next() {
                                    text.push_str(&format!("\n  Full text: /transcript {id}\n"));
                                }
                            }
                            if let Some(before) = summary.next_before {
                                text.push_str(&format!("\nOlder records: /history {before}"));
                            }
                            text
                        }
                        Err(error) => format!("Could not read native history: {error}"),
                    },
                    None => "Native archive is unavailable; the transcript remains in memory."
                        .to_string(),
                };
                app.add_system_message(&notice);
                return Ok(());
            }
            "/transcript" => {
                let id = parts.get(1).and_then(|value| value.parse::<usize>().ok());
                let offset = parts
                    .get(2)
                    .map(|value| value.parse::<usize>())
                    .unwrap_or(Ok(0));
                if parts.len() > 3 || id.is_none() || offset.is_err() {
                    app.add_system_message("Usage: /transcript <record_id> [offset]");
                    return Ok(());
                }
                let id = id.unwrap();
                let offset = offset.unwrap();
                let notice = match app.transcript_store.as_ref() {
                    Some(store) => match store.page(id, offset) {
                        Ok(page) => {
                            let next = page
                                .next_offset
                                .map(|next| format!("\nNext page: /transcript {id} {next}"))
                                .unwrap_or_default();
                            format!(
                                "Stored transcript {id} (offset {offset}, {} bytes):\n{}{next}",
                                page.total_bytes, page.content
                            )
                        }
                        Err(error) => format!("Could not read stored transcript: {error}"),
                    },
                    None => "Native archive is unavailable; the transcript remains in memory."
                        .to_string(),
                };
                app.add_system_message(&notice);
                return Ok(());
            }
            "/session-history" => {
                let before = parts.get(1).map(|value| value.parse::<usize>()).transpose();
                if parts.len() > 2 || before.is_err() {
                    app.add_system_message("Usage: /session-history [before]");
                    return Ok(());
                }
                let Some(client) = acp_client.cloned() else {
                    app.add_system_message("Reconnect to read durable conversation turns.");
                    return Ok(());
                };
                let session = app.session_id.clone();
                let before = before.unwrap();
                tokio::spawn(async move {
                    let notice = match client.session_history(&session, before).await {
                        Ok(page) => format_history_page(&page),
                        Err(error) => format!("Could not read session history: {error}"),
                    };
                    let _ = event_tx.send(AppEvent::Notice(notice)).await;
                });
                return Ok(());
            }
            "/history-turn" => {
                let index = parts.get(1).and_then(|value| value.parse::<usize>().ok());
                let field = parts.get(2).copied();
                let offset = parts
                    .get(3)
                    .map(|value| value.parse::<usize>())
                    .unwrap_or(Ok(0));
                if parts.len() > 4
                    || index.is_none()
                    || !matches!(field, Some("prompt" | "response"))
                    || offset.is_err()
                {
                    app.add_system_message(
                        "Usage: /history-turn <turn_index> <prompt|response> [offset]",
                    );
                    return Ok(());
                }
                let Some(client) = acp_client.cloned() else {
                    app.add_system_message("Reconnect to read durable conversation turns.");
                    return Ok(());
                };
                let session = app.session_id.clone();
                let index = index.unwrap();
                let field = field.unwrap().to_owned();
                let offset = offset.unwrap();
                tokio::spawn(async move {
                    let notice = match client
                        .session_history_content(&session, index, &field, offset)
                        .await
                    {
                        Ok(page) => {
                            let next = page
                                .next_offset
                                .map(|next| {
                                    format!("\nNext page: /history-turn {index} {field} {next}")
                                })
                                .unwrap_or_default();
                            format!("Stored turn {index} {field} (offset {offset}, {} characters):\n{}{next}", page.total_chars, page.content)
                        }
                        Err(error) => format!("Could not read stored turn: {error}"),
                    };
                    let _ = event_tx.send(AppEvent::Notice(notice)).await;
                });
                return Ok(());
            }
            "/tool-result" => {
                let selected = parts.get(1).map(|id| (*id).to_string()).or_else(|| {
                    app.dialogue_items.iter().rev().find_map(|item| {
                        if let hex_tui::app::DialogueItem::ToolCall(tool) = item {
                            app.tool_result_refs
                                .contains_key(&tool.id)
                                .then(|| tool.id.clone())
                        } else {
                            None
                        }
                    })
                });
                let offset = parts
                    .get(2)
                    .map(|value| value.parse::<usize>())
                    .unwrap_or(Ok(0));
                if parts.len() > 3 || offset.is_err() {
                    app.add_system_message("Usage: /tool-result [call_id] [offset]");
                    return Ok(());
                }
                let Some((id, reference)) = selected.and_then(|id| {
                    app.tool_result_refs
                        .get(&id)
                        .cloned()
                        .or_else(|| {
                            app.transcript_store
                                .as_ref()
                                .and_then(|store| store.find_tool_reference(&id))
                        })
                        .map(|reference| (id, reference))
                }) else {
                    app.add_system_message("No stored tool result reference is available.");
                    return Ok(());
                };
                let offset = offset.unwrap();
                let workspace = PathBuf::from(&app.workspace_path);
                let session = app.session_id.clone();
                let client = acp_client.cloned();
                if !reference.local && client.is_none() {
                    app.add_system_message("Reconnect to read this tool's stored result.");
                    return Ok(());
                }
                tokio::spawn(async move {
                    let result = if reference.local {
                        tokio::task::spawn_blocking(move || {
                            hex_tui::app::load_tool_result_page(
                                &workspace,
                                &reference.task_id,
                                &reference.call_id,
                                offset,
                            )
                        })
                        .await
                        .map_err(|e| e.to_string())
                        .and_then(|result| result)
                    } else {
                        client
                            .unwrap()
                            .session_tool_result(
                                &session,
                                &reference.task_id,
                                &reference.call_id,
                                offset,
                            )
                            .await
                            .map_err(|e| e.to_string())
                    };
                    let notice = match result {
                        Ok(page) => {
                            let next = page
                                .next_offset
                                .map(|next| format!("\nNext page: /tool-result {id} {next}"))
                                .unwrap_or_default();
                            format!("Stored tool result {id} (offset {offset}, {} characters total):\n{}{next}", page.total_chars, page.content)
                        }
                        Err(error) => format!("Could not read stored tool result: {error}"),
                    };
                    let _ = event_tx.send(AppEvent::Notice(notice)).await;
                });
                return Ok(());
            }
            "/clear" => {
                match app.clear_transcript() {
                    Ok(()) => app.add_system_message(
                        "Dialogue viewport cleared; stored history remains available.",
                    ),
                    Err(error) => app.add_system_message(&format!(
                        "Could not persist transcript; viewport retained: {error}"
                    )),
                }
                return Ok(());
            }
            "/quit" => {
                app.should_quit = true;
                return Ok(());
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
            let cmd_str = cmd.to_ascii_lowercase();
            let arg_str = arg.map(|s| s.to_string());
            let client_clone = client.clone();
            let tx = event_tx.clone();

            app.control_request_id += 1;
            let request_id = app.control_request_id;
            app.pending_command = Some(request_id);
            tokio::spawn(async move {
                let result = client_clone.session_command(&sess_id, &cmd_str, arg_str.as_deref())
                    .await.map_err(|e| e.to_string());
                let _ = tx.send(AppEvent::CommandResult {
                    session_id: sess_id, request_id, command: cmd_str, result,
                }).await;
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
                let mode_str = if app.side_by_side {
                    "side-by-side"
                } else {
                    "unified"
                };
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
                app.add_system_message("/model requires an ACP connection; no model switch was made.");
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
            match client_clone
                .session_prompt_paged(&sess_id, &prompt_text)
                .await
            {
                Ok(resp) => {
                    let txt = resp.text_content();
                    if !txt.is_empty() {
                        let _ = tx.send(AppEvent::AssistantResponse(txt)).await;
                    } else {
                        let _ = tx.send(AppEvent::Ready).await;
                    }
                }
                Err(err) => {
                    let _ = tx
                        .send(AppEvent::Error(format!("Prompt failed: {}", err)))
                        .await;
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
                        let ev_type = tool_obj
                            .get("eventType")
                            .and_then(|v| v.as_str())
                            .unwrap_or("");
                        let tool_id = tool_obj
                            .get("toolId")
                            .and_then(|v| v.as_str())
                            .unwrap_or("tool");
                        let call_id = tool_obj
                            .get("callId")
                            .and_then(|v| v.as_str())
                            .unwrap_or(tool_id);
                        let dur = tool_obj.get("durationMs").and_then(|v| v.as_u64());
                        let payload_str = tool_obj
                            .get("payload")
                            .and_then(|v| v.as_str())
                            .unwrap_or("");
                        if let (Some(task), Some(ledger_call)) = (
                            tool_obj.get("taskId").and_then(|v| v.as_str()),
                            tool_obj.get("ledgerCallId").and_then(|v| v.as_str()),
                        ) {
                            app.tool_result_refs.insert(
                                call_id.to_string(),
                                hex_tui::app::ToolResultRef {
                                    task_id: task.to_string(),
                                    call_id: ledger_call.to_string(),
                                    local: false,
                                },
                            );
                        }

                        if ev_type == "start" {
                            app.add_tool_call(call_id, tool_id, payload_str, dur, Some("RUNNING"));
                        } else if ev_type == "complete" {
                            app.complete_tool_call(call_id, payload_str, dur.unwrap_or(0), false);
                        } else if ev_type == "error" {
                            app.complete_tool_call(call_id, payload_str, dur.unwrap_or(0), true);
                            app.record_tool_failure(call_id, payload_str, dur.unwrap_or(0));
                        }
                    }
                }
                // Retain the streaming marker until the prompt response, even if the display prefix was archived.
                if terminal {
                    app.pending_cancel = None;
                    app.cancel_requested = false;
                    app.close_running_tool_calls();
                    app.clear_approval_request();
                    app.is_busy = false;
                    app.busy_started_at = None;
                    let st = status_val.as_deref().unwrap_or("Ready");
                    app.status_message = if st.eq_ignore_ascii_case("ready")
                        || st.eq_ignore_ascii_case("completed")
                    {
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
                if appr.session_id != app.session_id {
                    return;
                }
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
                    blast_radius: appr.blast_radius,
                    cwd: appr.cwd,
                });
            }
        }
    }
}

fn setup_panic_hook() {
    let original_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |panic_info| {
        let _ = disable_raw_mode();
        let _ = execute!(stdout(), LeaveAlternateScreen, DisableMouseCapture, DisableBracketedPaste);
        original_hook(panic_info);
    }));
}

fn restore_terminal(terminal: &mut Terminal<CrosstermBackend<Stdout>>) -> Result<()> {
    disable_raw_mode()?;
    execute!(
        terminal.backend_mut(),
        LeaveAlternateScreen,
        DisableMouseCapture,
        DisableBracketedPaste
    )?;
    terminal.show_cursor()?;
    Ok(())
}

fn handle_start_mouse_click(app: &mut App, col: u16, row: u16, area: ratatui::layout::Rect) {
    let prompt = app.get_start_prompt_area(area);
    if prompt.contains(ratatui::layout::Position::new(col, row)) {
        app.set_start_focus(Focus::Input);
        return;
    }
    let resume = app.get_start_resume_area(area);
    app.handle_start_mouse_click(col, row, resume);
}

#[cfg(test)]
mod wire_contract_tests {
    //! F04: prove the TUI consumes the same real-server fixtures as the desktop
    //! bridge (tests/fixtures/acp_wire, captured by tests/client/test_acp_wire_contract.py).
    use super::*;
    use hex_protocol::models::Role;
    use hex_tui::app::DialogueItem;

    #[tokio::test]
    async fn release_and_repeat_keys_never_edit_submit_or_trigger_hotkeys() {
        for kind in [KeyEventKind::Release, KeyEventKind::Repeat] {
            for (code, modifiers) in [
                (KeyCode::Char('x'), KeyModifiers::NONE),
                (KeyCode::Enter, KeyModifiers::NONE),
                (KeyCode::Char('c'), KeyModifiers::CONTROL),
                (KeyCode::Char('p'), KeyModifiers::CONTROL),
                (KeyCode::F(3), KeyModifiers::NONE),
            ] {
                let mut app = App::new("p", "m", "cow");
                app.insert_text("世界");
                let (tx, _) = tokio::sync::mpsc::channel(8);
                handle_key_event(&mut app, crossterm::event::KeyEvent::new_with_kind(code, modifiers, kind), None, tx)
                    .await.unwrap();
                assert_eq!(app.input_buffer, "世界");
                assert_eq!(app.cursor_pos, 2);
                assert!(app.command_history.is_empty());
                assert!(!app.should_quit && !app.show_palette && !app.cancel_requested);
                assert_eq!(app.mode, "agent");
            }
        }
    }

    #[tokio::test]
    async fn paste_and_shift_enter_remain_editable_until_plain_enter() {
        let mut app = App::new("p", "m", "cow");
        let (tx, _) = tokio::sync::mpsc::channel(8);
        handle_paste_event(&mut app, "first\r\n世界");
        handle_key_event(&mut app, crossterm::event::KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT), None, tx.clone())
            .await.unwrap();
        handle_paste_event(&mut app, "third");
        assert_eq!(app.input_buffer, "first\n世界\nthird");
        assert_eq!(app.cursor_pos, app.input_buffer.chars().count());
        assert!(app.command_history.is_empty());
        assert!(!app.is_busy);
        handle_key_event(&mut app, crossterm::event::KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE), None, tx)
            .await.unwrap();
        assert_eq!(app.command_history, vec!["first\n世界\nthird"]);
        assert!(app.input_buffer.is_empty());
    }

    #[test]
    fn paste_respects_cursor_focus_and_modal_boundaries() {
        let mut app = App::new("p", "m", "cow");
        app.insert_text("left-right");
        app.cursor_pos = 5;
        handle_paste_event(&mut app, "世\r\n界\t\0\u{1b}");
        assert_eq!(app.input_buffer, "left-世\n界\tright");
        assert_eq!(app.cursor_pos, 9);
        app.show_help = true;
        handle_paste_event(&mut app, "ignored");
        app.show_help = false;
        app.focus = Focus::Resume;
        handle_paste_event(&mut app, "ignored");
        assert_eq!(app.input_buffer, "left-世\n界\tright");
        app.open_palette();
        handle_paste_event(&mut app, "/diff\n/help");
        assert_eq!(app.palette_query, "/diff /help");
        assert!(app.show_palette);
        assert!(!app.show_diff);
        assert!(app.command_history.is_empty());
    }

    #[test]
    fn history_summary_preserves_unicode_lengths_and_navigation() {
        let page = serde_json::from_value(serde_json::json!({
            "history": [{"turnIndex": 7, "prompt": "世界", "response": "complete",
                "promptChars": 2, "responseChars": 9000, "status": "completed"}],
            "historyCount": 80, "nextBefore": 7
        }))
        .unwrap();
        let text = format_history_page(&page);
        assert!(text.contains("80 total turns"));
        assert!(text.contains("Turn 7 [completed]"));
        assert!(text.contains("Full response: /history-turn 7 response"));
        assert!(!text.contains("Full prompt:"));
        assert!(text.contains("Older turns: /session-history 7"));
    }

    #[tokio::test]
    async fn history_commands_validate_before_dispatch() {
        let mut app = App::new("p", "m", "cow");
        let (tx, mut rx) = tokio::sync::mpsc::channel(8);
        for command in [
            "/session-history -1",
            "/session-history 1 extra",
            "/history-turn nope prompt",
            "/history-turn 0 invalid",
            "/history-turn 0 response -1",
        ] {
            execute_submitted_text(&mut app, command.to_owned(), None, tx.clone())
                .await
                .unwrap();
            let Some(DialogueItem::Message(message)) = app.dialogue_items.last() else {
                panic!("missing usage notice")
            };
            assert!(message.content.starts_with("Usage:"));
            assert!(!app.is_busy);
        }
        assert!(rx.try_recv().is_err());
    }

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
        assert!(app.active_turn_has_streamed);

        let assistant_text: String = app
            .dialogue_items
            .iter()
            .filter_map(|d| match d {
                DialogueItem::Message(m) if m.role == Role::Assistant => Some(m.content.as_str()),
                _ => None,
            })
            .collect();
        assert!(
            assistant_text.contains("Hello world"),
            "got: {assistant_text}"
        );

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

    #[tokio::test]
    async fn palette_accepts_tool_result_arguments() {
        for query in ["tool-result unknown 4096", "/tool-result unknown 4096"] {
            let mut app = App::new("p", "m", "cow");
            app.open_palette();
            app.palette_query = query.to_string();
            assert_eq!(app.palette_selected().unwrap().command, "/tool-result");
            let (tx, _rx) = tokio::sync::mpsc::channel(128);
            handle_key_event(
                &mut app,
                crossterm::event::KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
                None,
                tx,
            )
            .await
            .unwrap();
            assert!(!app.show_palette);
            assert!(
                matches!(app.dialogue_items.last(), Some(DialogueItem::Message(m)) if m.content.contains("No stored tool result reference"))
            );
        }
    }

    #[test]
    fn tool_results_keep_durable_ids_separate_from_ui_correlation() {
        let mut app = App::new("p", "m", "cow");
        for event in ["start", "complete"] {
            let notification = serde_json::from_value(serde_json::json!({
                "jsonrpc": "2.0", "method": "session/update",
                "params": {"sessionId": "session_1", "updateType": "tool", "tool": {
                    "eventType": event, "callId": "ui_call", "toolId": "terminal",
                    "taskId": "task_1", "ledgerCallId": "durable_call",
                    "payload": "short preview", "durationMs": 12
                }}
            }))
            .unwrap();
            handle_notification(&mut app, notification);
        }
        let reference = &app.tool_result_refs["ui_call"];
        assert_eq!(reference.task_id, "task_1");
        assert_eq!(reference.call_id, "durable_call");
        assert!(!reference.local);
        assert!(
            matches!(app.dialogue_items.last(), Some(DialogueItem::ToolCall(t)) if t.id == "ui_call")
        );
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
        assert!(
            assistant_text.contains("Fixture answer."),
            "got: {assistant_text}"
        );
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
        let (tx, _rx) = tokio::sync::mpsc::channel(128);

        // First Ctrl+C while busy: cancel journey, not quit (standalone settles locally).
        handle_key_event(&mut app, ctrl_c(), None, tx.clone())
            .await
            .unwrap();
        assert!(!app.should_quit, "first Ctrl+C must cancel, not quit");
        assert!(!app.is_busy, "standalone cancel settles locally");
        assert!(app
            .dialogue_items
            .iter()
            .any(|d| matches!(d, DialogueItem::Message(m) if m.role == Role::System && m.content.contains("cancelled"))));

        // Second Ctrl+C: quit.
        handle_key_event(&mut app, ctrl_c(), None, tx)
            .await
            .unwrap();
        assert!(app.should_quit);
    }

    #[tokio::test]
    async fn ctrl_c_quits_when_idle() {
        let mut app = App::new("p", "m", "cow");
        let (tx, _rx) = tokio::sync::mpsc::channel(128);
        handle_key_event(&mut app, ctrl_c(), None, tx)
            .await
            .unwrap();
        assert!(app.should_quit);
    }

    fn key(code: KeyCode) -> crossterm::event::KeyEvent {
        crossterm::event::KeyEvent::new(code, KeyModifiers::NONE)
    }

    #[tokio::test]
    async fn slash_on_an_empty_composer_opens_the_palette_on_both_screens() {
        for screen in [ScreenState::Start, ScreenState::Session] {
            let mut app = App::new("p", "m", "cow");
            app.screen_state = screen;
            let (tx, _rx) = tokio::sync::mpsc::channel(128);
            handle_key_event(&mut app, key(KeyCode::Char('/')), None, tx)
                .await
                .unwrap();
            assert!(app.show_palette, "palette must open on {screen:?}");
            assert!(
                app.input_buffer.is_empty(),
                "the slash is not typed into the composer"
            );
            assert!(app.palette_query.is_empty());
        }
    }

    #[tokio::test]
    async fn slash_inside_existing_text_stays_literal() {
        let mut app = App::new("p", "m", "cow");
        let (tx, _rx) = tokio::sync::mpsc::channel(128);
        for c in "path".chars() {
            handle_key_event(&mut app, key(KeyCode::Char(c)), None, tx.clone())
                .await
                .unwrap();
        }
        handle_key_event(&mut app, key(KeyCode::Char('/')), None, tx)
            .await
            .unwrap();
        assert!(!app.show_palette);
        assert_eq!(app.input_buffer, "path/");
    }

    #[tokio::test]
    async fn palette_query_filters_and_enter_runs_the_real_command() {
        let mut app = App::new("p", "m", "cow");
        app.add_assistant_message("some earlier output");
        let (tx, _rx) = tokio::sync::mpsc::channel(128);
        handle_key_event(&mut app, key(KeyCode::Char('/')), None, tx.clone())
            .await
            .unwrap();
        for c in "clr".chars() {
            handle_key_event(&mut app, key(KeyCode::Char(c)), None, tx.clone())
                .await
                .unwrap();
        }
        assert_eq!(app.palette_query, "clr");
        assert!(app.input_buffer.is_empty(), "typing belongs to the palette");
        assert_eq!(
            app.palette_selected().map(|i| i.command),
            Some("/clear".to_string())
        );

        handle_key_event(&mut app, key(KeyCode::Enter), None, tx)
            .await
            .unwrap();
        assert!(!app.show_palette);
        assert!(app
            .dialogue_items
            .iter()
            .any(|d| matches!(d, DialogueItem::Message(m)
            if m.role == Role::System && m.content.contains("cleared"))));
    }

    #[tokio::test]
    async fn esc_closes_the_palette_and_restores_the_composer() {
        let mut app = App::new("p", "m", "cow");
        app.input_buffer = "draft goal".to_string();
        app.cursor_pos = app.input_buffer.len();
        let (tx, _rx) = tokio::sync::mpsc::channel(128);
        handle_key_event(&mut app, ctrl_p(), None, tx.clone())
            .await
            .unwrap();
        assert!(app.show_palette);
        handle_key_event(&mut app, key(KeyCode::Char('x')), None, tx.clone())
            .await
            .unwrap();
        handle_key_event(&mut app, key(KeyCode::Esc), None, tx)
            .await
            .unwrap();
        assert!(!app.show_palette);
        assert_eq!(app.input_buffer, "draft goal", "the composer is untouched");
        assert!(app.palette_query.is_empty());
    }

    #[tokio::test]
    async fn backspace_on_an_empty_query_closes_the_palette() {
        let mut app = App::new("p", "m", "cow");
        let (tx, _rx) = tokio::sync::mpsc::channel(128);
        handle_key_event(&mut app, ctrl_p(), None, tx.clone())
            .await
            .unwrap();
        handle_key_event(&mut app, key(KeyCode::Char('a')), None, tx.clone())
            .await
            .unwrap();
        handle_key_event(&mut app, key(KeyCode::Backspace), None, tx.clone())
            .await
            .unwrap();
        assert!(app.show_palette, "first backspace only clears the query");
        handle_key_event(&mut app, key(KeyCode::Backspace), None, tx)
            .await
            .unwrap();
        assert!(!app.show_palette);
    }

    #[tokio::test]
    async fn ctrl_p_toggles_and_arrows_move_the_selection() {
        let mut app = App::new("p", "m", "cow");
        let (tx, _rx) = tokio::sync::mpsc::channel(128);
        handle_key_event(&mut app, ctrl_p(), None, tx.clone())
            .await
            .unwrap();
        assert!(app.show_palette);
        handle_key_event(&mut app, key(KeyCode::Down), None, tx.clone())
            .await
            .unwrap();
        assert_eq!(app.selected_palette_index, 1);
        handle_key_event(&mut app, key(KeyCode::Up), None, tx.clone())
            .await
            .unwrap();
        assert_eq!(app.selected_palette_index, 0);
        handle_key_event(&mut app, key(KeyCode::Up), None, tx.clone())
            .await
            .unwrap();
        assert_eq!(
            app.selected_palette_index,
            app.palette_results().len() - 1,
            "wraps around"
        );
        handle_key_event(&mut app, ctrl_p(), None, tx)
            .await
            .unwrap();
        assert!(!app.show_palette);
    }

    fn ctrl_p() -> crossterm::event::KeyEvent {
        crossterm::event::KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL)
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

#[cfg(test)]
mod control_recovery_tests {
    use super::*;
    use hex_protocol::models::{ApprovalDecisionResponse, SessionCancelResponse, SessionCommandResponse};
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

    fn approval_app() -> App {
        let mut app = App::new("p", "original", "cow");
        app.is_busy = true;
        app.set_approval_request(ApprovalRequest {
            id: "approval-1".into(), risk_level: "high".into(), tool_name: "terminal".into(),
            parameters: "git status".into(), reason: "inspect workspace".into(),
            blast_radius: String::new(), cwd: String::new(),
        });
        app.approval_can_allow.set(true);
        app
    }

    fn approval_result(app: &App, result: Result<ApprovalDecisionResponse, String>) -> AppEvent {
        AppEvent::ApprovalDecision {
            session_id: app.session_id.clone(), approval_id: "approval-1".into(),
            generation: app.approval_generation, decision: "approve".into(), result,
        }
    }

    #[tokio::test]
    async fn unavailable_approval_keeps_the_request_and_never_announces_success() {
        let mut app = approval_app();
        let (tx, _rx) = tokio::sync::mpsc::channel(8);
        submit_approval_decision(&mut app, None, 0, tx);
        assert!(app.active_approval.is_some());
        assert_eq!(app.focus, Focus::Approval);
        assert!(app.approval_error.as_deref().unwrap().contains("no ACP connection"));
        assert!(!app.dialogue_items.iter().any(|item| matches!(item,
            hex_tui::app::DialogueItem::Message(m) if m.content.contains("Approved"))));
    }

    #[tokio::test]
    async fn approval_rpc_failure_can_retry_and_only_applied_ack_closes_prompt() {
        let (client_io, server_io) = tokio::io::duplex(4096);
        let (reader, writer) = tokio::io::split(client_io);
        let client = AcpClient::from_streams(reader, writer);
        let server = tokio::spawn(async move {
            let (reader, mut writer) = tokio::io::split(server_io);
            let mut reader = BufReader::new(reader);
            for attempt in 0..2 {
                let mut line = String::new();
                reader.read_line(&mut line).await.unwrap();
                let request: serde_json::Value = serde_json::from_str(&line).unwrap();
                assert_eq!(request["method"], "session/approvalDecision");
                let mut response = serde_json::json!({"jsonrpc":"2.0", "id":request["id"]});
                if attempt == 0 {
                    response["error"] = serde_json::json!({"code":-32603,"message":"temporary RPC failure"});
                } else {
                    response["result"] = serde_json::json!({"approvalId":"approval-1","status":"applied","decision":"approve"});
                }
                writer.write_all(format!("{response}\n").as_bytes()).await.unwrap();
            }
        });
        let mut app = approval_app();
        let (tx, mut rx) = tokio::sync::mpsc::channel(8);
        submit_approval_decision(&mut app, Some(&client), 0, tx.clone());
        assert!(app.active_approval.is_some() && app.approval_pending);
        let generation = app.approval_generation;
        submit_approval_decision(&mut app, Some(&client), 2, tx.clone());
        assert_eq!(app.approval_generation, generation, "in-flight decision was sent twice");
        let event = tokio::time::timeout(std::time::Duration::from_secs(2), rx.recv()).await.unwrap().unwrap();
        handle_control_result(&mut app, event);
        assert!(app.active_approval.is_some() && !app.approval_pending);
        assert!(app.approval_error.as_deref().unwrap().contains("temporary RPC failure"));
        submit_approval_decision(&mut app, Some(&client), 0, tx);
        let event = tokio::time::timeout(std::time::Duration::from_secs(2), rx.recv()).await.unwrap().unwrap();
        handle_control_result(&mut app, event);
        assert!(app.active_approval.is_none());
        assert!(app.is_busy, "acknowledgement is not a completed turn");
        server.await.unwrap();
    }

    #[test]
    fn negative_and_mismatched_acknowledgements_leave_approval_retryable() {
        for (id, status, decision) in [
            ("approval-1", "acknowledged", "approve"),
            ("other", "applied", "approve"),
            ("approval-1", "applied", "reject"),
        ] {
            let mut app = approval_app();
            app.approval_pending = true;
            let event = approval_result(&app, Ok(ApprovalDecisionResponse {
                approval_id: id.into(), status: status.into(), decision: decision.into(),
            }));
            handle_control_result(&mut app, event);
            assert!(app.active_approval.is_some() && !app.approval_pending);
            assert!(app.approval_error.is_some());
        }
    }

    #[test]
    fn stale_decision_cannot_clear_a_replacement_request() {
        let mut app = approval_app();
        app.approval_pending = true;
        let event = approval_result(&app, Ok(ApprovalDecisionResponse {
            approval_id: "approval-1".into(), status: "applied".into(), decision: "approve".into(),
        }));
        let req = app.active_approval.clone().unwrap();
        app.set_approval_request(req);
        handle_control_result(&mut app, event);
        assert!(app.active_approval.is_some());
        assert!(app.approval_error.is_none());
    }

    #[test]
    fn prompt_timeout_or_disconnect_does_not_discard_an_unresolved_approval() {
        for error in ["Prompt failed: request timed out", "ACP connection closed."] {
            let mut app = approval_app();
            handle_backend_error(&mut app, error);
            assert!(app.active_approval.is_some() && app.is_busy);
            assert_eq!(app.focus, Focus::Approval);
            assert!(app.approval_error.as_deref().unwrap().contains(error));
        }
    }

    #[test]
    fn cancellation_failure_restores_retry_without_sealing_or_losing_approval() {
        for has_approval in [true, false] {
            for result in [Err("RPC failed".into()), Ok(SessionCancelResponse {
                session_id: "ses_default".into(), cancelled: false,
            })] {
                let mut app = approval_app();
                if !has_approval {
                    app.clear_approval_request();
                }
                app.cancel_requested = true;
                app.pending_cancel = Some(3);
                let session_id = app.session_id.clone();
                handle_control_result(&mut app, AppEvent::CancelResult { session_id, request_id: 3, result });
                assert!(app.is_busy);
                assert_eq!(app.active_approval.is_some(), has_approval);
                assert!(!app.cancel_requested && app.pending_cancel.is_none());
                assert!(app.status_message.contains("failed"));
                let notice = app.dialogue_items.iter().find_map(|item| match item {
                    hex_tui::app::DialogueItem::Message(message) if message.content.contains("Cancellation failed:") => {
                        Some(message.content.as_str())
                    }
                    _ => None,
                }).unwrap();
                let hint = if has_approval { "Press Ctrl+C to retry." } else { "Press Ctrl+C or Esc to retry." };
                assert!(notice.contains(hint), "{notice}");
            }
        }
    }

    #[test]
    fn stale_cancel_cannot_change_a_newer_turn() {
        let mut app = approval_app();
        app.cancel_requested = true;
        app.pending_cancel = Some(4);
        let session_id = app.session_id.clone();
        handle_control_result(&mut app, AppEvent::CancelResult {
            session_id, request_id: 3, result: Err("late failure".into()),
        });
        assert!(app.cancel_requested && app.pending_cancel == Some(4));
    }

    #[tokio::test]
    async fn standalone_model_does_not_fabricate_a_switch() {
        let mut app = App::new("p", "original", "cow");
        let (tx, _rx) = tokio::sync::mpsc::channel(8);
        execute_submitted_text(&mut app, "/model imaginary".into(), None, tx).await.unwrap();
        assert_eq!(app.model_name, "original");
    }

    #[tokio::test]
    async fn connected_model_label_stays_unchanged_until_canonical_ack() {
        for command in ["/model alias", "/MODEL alias"] {
            let (client_io, server_io) = tokio::io::duplex(4096);
            let (reader, writer) = tokio::io::split(client_io);
            let client = AcpClient::from_streams(reader, writer);
            let mut app = App::new("p", "original", "cow");
            let (tx, mut rx) = tokio::sync::mpsc::channel(8);
            execute_submitted_text(&mut app, command.into(), Some(&client), tx).await.unwrap();
            assert_eq!(app.model_name, "original");
            assert!(app.is_busy);
            let (reader, mut writer) = tokio::io::split(server_io);
            let mut reader = BufReader::new(reader);
            let mut line = String::new();
            reader.read_line(&mut line).await.unwrap();
            let request: serde_json::Value = serde_json::from_str(&line).unwrap();
            assert_eq!(request["params"]["command"], "/model");
            assert_eq!(request["params"]["args"], "alias");
            let response = serde_json::json!({"jsonrpc":"2.0","id":request["id"],"result":{
                "sessionId":"ses_default","command":"/model","output":"opaque output",
                "isError":false,"model":"canonical","provider":"canonical-provider"
            }});
            writer.write_all(format!("{response}\n").as_bytes()).await.unwrap();
            let event = tokio::time::timeout(std::time::Duration::from_secs(2), rx.recv()).await.unwrap().unwrap();
            handle_control_result(&mut app, event);
            assert_eq!(app.model_name, "canonical");
            assert_eq!(app.provider_name, "canonical-provider");
            assert!(!app.is_busy);
        }
    }

    #[tokio::test]
    async fn invisible_allow_keys_cannot_submit_even_after_a_resize_event() {
        for code in [KeyCode::Char('y'), KeyCode::Char('a'), KeyCode::Enter] {
            let mut app = approval_app();
            app.approval_can_allow.set(false);
            let (tx, _rx) = tokio::sync::mpsc::channel(8);
            handle_key_event(&mut app, crossterm::event::KeyEvent::new(code, KeyModifiers::NONE), None, tx).await.unwrap();
            assert!(app.active_approval.is_some());
            assert!(app.approval_error.as_deref().unwrap().contains("Resize"));
        }
    }

    #[tokio::test]
    async fn topmost_approval_keys_are_not_intercepted_by_hidden_modals() {
        for code in [KeyCode::Esc, KeyCode::Enter, KeyCode::Down] {
            let mut app = approval_app();
            app.show_help = true;
            app.open_palette();
            app.active_subagent_modal = Some(hex_tui::app::SubagentModalState {
                name: "agent".into(), prompt: String::new(), is_expanded: false,
                activity_lines: Vec::new(), elapsed_seconds: 0, status: "running".into(),
                is_running: true, activity_scroll: 0,
            });
            let (tx, _rx) = tokio::sync::mpsc::channel(8);
            handle_key_event(&mut app, crossterm::event::KeyEvent::new(code, KeyModifiers::NONE), None, tx).await.unwrap();
            assert!(app.active_subagent_modal.is_some());
            assert!(app.show_help && app.show_palette);
            if code == KeyCode::Down {
                assert_eq!(app.selected_permission_index, 1);
            } else {
                assert!(app.approval_error.is_some());
            }
        }
    }

    #[test]
    fn confirmed_model_with_unknown_provider_does_not_keep_a_stale_provider_label() {
        let mut app = App::new("p", "original", "cow");
        app.provider_name = "old-provider".into();
        app.pending_command = Some(2);
        let session_id = app.session_id.clone();
        handle_control_result(&mut app, AppEvent::CommandResult {
            session_id: session_id.clone(), request_id: 2, command: "/model".into(),
            result: Ok(SessionCommandResponse {
                session_id, command: "/model".into(), output: "opaque".into(),
                is_error: Some(false), model: Some("canonical".into()), provider: None,
            }),
        });
        assert_eq!(app.model_name, "canonical");
        assert_eq!(app.provider_label(), "unknown provider");
    }

    #[test]
    fn only_successful_current_model_response_updates_labels() {
        for (failed, model, session, request, expected) in [
            (false, Some("canonical"), "ses_default", 2, "canonical"),
            (true, Some("canonical"), "ses_default", 2, "original"),
            (false, None, "ses_default", 2, "original"),
            (false, Some("canonical"), "other", 2, "original"),
            (false, Some("canonical"), "ses_default", 1, "original"),
        ] {
            let mut app = App::new("p", "original", "cow");
            app.pending_command = Some(2);
            handle_control_result(&mut app, AppEvent::CommandResult {
                session_id: session.into(), request_id: request, command: "/model".into(),
                result: Ok(SessionCommandResponse {
                    session_id: "ses_default".into(), command: "/model".into(), output: "opaque output".into(),
                    is_error: Some(failed), model: model.map(str::to_string), provider: Some("canonical-provider".into()),
                }),
            });
            assert_eq!(app.model_name, expected);
            if expected == "canonical" { assert_eq!(app.provider_name, "canonical-provider"); }
        }
    }

    #[test]
    fn capability_counts_display_unknown_differently_from_zero() {
        let mut app = App::new("p", "original", "cow");
        let mut terminal = Terminal::new(TestBackend::new(120, 8)).unwrap();
        terminal.draw(|frame| ui::header::render(frame, &app, frame.area())).unwrap();
        let rendered = |terminal: &Terminal<TestBackend>| terminal.backend().buffer().content()
            .iter().map(|cell| cell.symbol()).collect::<String>();
        assert!(rendered(&terminal).contains("Tools: [?]"));
        assert!(rendered(&terminal).contains("Skills: [?]"));
        app.tools_count = Some(0);
        app.skills_count = Some(0);
        terminal.draw(|frame| ui::header::render(frame, &app, frame.area())).unwrap();
        assert!(rendered(&terminal).contains("Tools: [0]"));
        assert!(rendered(&terminal).contains("Skills: [0]"));
    }

    #[test]
    fn capability_counts_remain_unknown_until_discovered() {
        let app = App::new("p", "original", "cow");
        assert_eq!(app.tools_count, None);
        assert_eq!(app.skills_count, None);
    }
}

#[cfg(test)]
mod keymap_truth_tests {
    //! G06: every hint the UI prints must survive a real key press.
    use super::*;
    use hex_protocol::models::Role;
    use hex_tui::app::{DialogueItem, ResumeSession};
    use hex_tui::ui::keymap::{self, Binding, Scope, BINDINGS};
    use crossterm::event::KeyEvent;

    const DIFF: &str = "--- a/f.rs\n+++ b/f.rs\n@@ -1,2 +1,2 @@\n-old\n+new\n";

    fn fixture(b: &Binding) -> App {
        let mut app = App::new("p", "m", "cow");
        match b.scope {
            Scope::Session => {
                app.screen_state = ScreenState::Session;
                match b.label {
                    "esc" => app.is_busy = true,
                    "enter" => {
                        app.input_buffer = "hello".to_string();
                        app.cursor_pos = 5;
                    }
                    "ctrl+up" | "ctrl+down" => {
                        app.sidebar.metrics.set((100, 20));
                        app.sidebar.scroll.set(if b.label == "ctrl+up" { 10 } else { 0 });
                    }
                    "pgdn" | "down" => {
                        // Already scrolled up: paging down moves toward the bottom.
                        app.follow_dialogue = false;
                        app.dialogue_scroll = 0;
                        app.dialogue_metrics.set((40, 10));
                    }
                    _ => {}
                }
            }
            Scope::Start => {
                app.resume_sessions = vec![ResumeSession {
                    title: "earlier task".to_string(),
                    relative_time: "1h".to_string(),
                    session_id: "tsk_earlier".to_string(),
                }];
                if matches!(b.label, "enter" | "d") {
                    app.set_start_focus(Focus::Resume);
                }
            }
            Scope::Palette => {
                app.open_palette();
                if b.label == "backspace" {
                    app.palette_push_char('x');
                }
            }
            Scope::Approval => {
                app.active_approval = Some(ApprovalRequest {
                    id: "ap1".to_string(),
                    risk_level: "high".to_string(),
                    tool_name: "bash".to_string(),
                    parameters: "rm -rf dist".to_string(),
                    reason: "cleanup".to_string(),
                    blast_radius: String::new(),
                    cwd: String::new(),
                });
            }
            Scope::Agents => {
                app.screen_state = ScreenState::Session;
                app.active_subagent_modal = Some(hex_tui::app::SubagentModalState {
                    name: "researcher".to_string(),
                    prompt: "map every caller of GraphBuilder.edges".to_string(),
                    is_expanded: false,
                    activity_lines: vec![
                        "read src/graph/index.ts".to_string(),
                        "grep GraphBuilder".to_string(),
                        "status   running".to_string(),
                    ],
                    elapsed_seconds: 42,
                    status: "running".to_string(),
                    is_running: true,
                    activity_scroll: if b.label == "down" { 2 } else { 0 },
                });
            }
            Scope::Global => {}
        }
        if b.label == "f2" {
            app.load_diff(DIFF);
            app.show_diff = false; // a diff exists, the pane is hidden
        }
        app
    }

    fn assert_effect(app: &App, b: &Binding) {
        match (b.scope, b.label) {
            (Scope::Global, "ctrl+c") => assert!(app.should_quit),
            (Scope::Global, "f1") | (Scope::Start, "f1") => assert!(app.show_help),
            (_, "f2") => assert!(app.show_diff),
            (Scope::Global, "f3") => assert_ne!(app.mode, "agent"),
            (Scope::Global, "f4") => assert!(app.sidebar.show_settings),
            (Scope::Session, "ctrl+up") => assert_eq!(app.sidebar.scroll.get(), 7),
            (Scope::Session, "ctrl+down") => assert_eq!(app.sidebar.scroll.get(), 3),
            (Scope::Session | Scope::Start, "tab") => {
                assert_eq!(app.mode, "plan");
                assert!(app.active_subagent_modal.is_none());
                assert_eq!(app.focus, Focus::Input);
            }
            (Scope::Start, "up/down" | "down") => {
                assert_eq!(app.focus, Focus::Resume);
                assert_eq!(app.selected_resume_index, 0);
            }
            (Scope::Session, "ctrl+p") => assert!(app.show_palette),
            (Scope::Session, "esc") => assert!(!app.is_busy),
            (Scope::Session, "enter") => assert!(app.dialogue_items.iter().any(|d| matches!(d,
                DialogueItem::Message(m) if m.role == Role::User && m.content == "hello"))),
            (Scope::Session, "pgup" | "up") => {
                assert!(
                    !app.follow_dialogue,
                    "{} did not leave the pinned bottom",
                    b.label
                )
            }
            (Scope::Session, "pgdn" | "down") => {
                assert!(app.dialogue_scroll > 0, "{} did not scroll down", b.label)
            }
            (Scope::Start, "/") => assert!(app.show_palette),
            (Scope::Start, "enter") => assert_eq!(app.screen_state, ScreenState::Session),
            (Scope::Start, "d") => assert_eq!(app.pending_delete_index, Some(0)),
            (Scope::Palette, "enter") | (Scope::Palette, "esc") => assert!(!app.show_palette),
            (Scope::Palette, "up") => {
                assert_eq!(app.selected_palette_index, app.palette_results().len() - 1)
            }
            (Scope::Palette, "down") | (Scope::Palette, "tab") => {
                assert_eq!(app.selected_palette_index, 1)
            }
            (Scope::Palette, "backspace") => assert!(app.palette_query.is_empty()),
            (Scope::Approval, "up/down") => assert_eq!(app.selected_permission_index, 2),
            (Scope::Approval, "down") => assert_eq!(app.selected_permission_index, 1),
            (Scope::Approval, "enter")
            | (Scope::Approval, "y")
            | (Scope::Approval, "a")
            | (Scope::Approval, "n")
            | (Scope::Approval, "esc") => {
                assert!(app.active_approval.is_some(), "unconfirmed decision lost the prompt");
                assert!(app.approval_error.is_some(), "unavailable approval needs actionable feedback");
            }
            (Scope::Approval, "d") => assert!(app.show_diff),
            (Scope::Agents, "up/down") => {
                assert_eq!(
                    app.active_subagent_modal.as_ref().unwrap().activity_scroll,
                    1
                )
            }
            (Scope::Agents, "down") => {
                assert_eq!(
                    app.active_subagent_modal.as_ref().unwrap().activity_scroll,
                    1
                )
            }
            (Scope::Agents, "e") => {
                assert!(app.active_subagent_modal.as_ref().unwrap().is_expanded)
            }
            (Scope::Agents, "esc") | (Scope::Agents, "tab") => {
                assert!(
                    app.active_subagent_modal.is_none(),
                    "{} left the modal open",
                    b.label
                )
            }
            other => panic!("advertised binding {other:?} has no verified effect"),
        }
    }

    #[tokio::test]
    async fn every_advertised_binding_really_works() {
        for b in BINDINGS {
            let mut app = fixture(b);
            let (tx, _rx) = tokio::sync::mpsc::channel(128);
            handle_key_event(&mut app, b.event(), None, tx)
                .await
                .unwrap();
            assert_effect(&app, b);
        }
    }

    fn sidebar_fixture() -> App {
        let mut app = App::new("p", "m", "cow");
        app.screen_state = ScreenState::Session;
        app.changed_files = (0..12).map(|i| hex_tui::app::ChangedFile {
            path: format!("src/file_{i}.rs"), additions: 12, deletions: 3,
        }).collect();
        app.plan_steps = (0..14).map(|i| hex_tui::app::PlanStep {
            title: format!("Plan step {i}"), status: hex_tui::app::StepStatus::Pending,
        }).collect();
        app
    }

    fn paint_sidebar(app: &App) {
        let area = ratatui::layout::Rect::new(0, 0, 120, 40);
        let mut buffer = ratatui::buffer::Buffer::empty(area);
        hex_tui::ui::screens::session::render(app, &hex_tui::ui::style::HexTheme::truecolor(),
            &hex_tui::ui::animation::AnimationState::new(), area, &mut buffer);
    }

    fn mouse(app: &mut App, kind: crossterm::event::MouseEventKind, x: u16, y: u16) {
        handle_mouse_event(app, crossterm::event::MouseEvent {
            kind, column: x, row: y, modifiers: KeyModifiers::NONE,
        }, ratatui::layout::Rect::new(0, 0, 120, 40));
    }

    #[test]
    fn sidebar_wheel_and_clicks_do_not_move_the_transcript() {
        use crossterm::event::{MouseButton, MouseEventKind};
        use hex_tui::sidebar::SidebarAction;
        let mut app = sidebar_fixture();
        app.subagents = (0..8).map(|i| hex_tui::app::SubagentTelemetry {
            name: format!("worker {i}"), status: "pass".to_string(), detail: String::new(),
            is_running: false, has_failed: false,
        }).collect();
        paint_sidebar(&app);
        let rail = app.sidebar.area.get();
        let transcript = (app.dialogue_scroll, app.follow_dialogue);
        mouse(&mut app, MouseEventKind::ScrollDown, rail.x + 2, rail.y + 5);
        assert!(app.sidebar.scroll.get() > 0);
        assert_eq!((app.dialogue_scroll, app.follow_dialogue), transcript);
        app.sidebar.scroll.set(0);
        paint_sidebar(&app);
        let target = app.sidebar.hit_targets.borrow().iter()
            .find(|(_, action)| *action == SidebarAction::ToggleChanges)
            .map(|(rect, _)| *rect).expect("overflow link visible");
        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), target.x, target.y);
        assert!(app.sidebar.show_all_changed);
        assert_eq!(app.focus, Focus::Input);
        assert_eq!((app.dialogue_scroll, app.follow_dialogue), transcript);
        mouse(&mut app, MouseEventKind::ScrollUp, 4, 10);
        assert!(!app.follow_dialogue, "wheel outside sidebar still scrolls transcript");
    }

    #[test]
    fn divider_drag_resizes_without_stealing_input() {
        use crossterm::event::{MouseButton, MouseEventKind};
        let mut app = sidebar_fixture();
        paint_sidebar(&app);
        let rail = app.sidebar.area.get();
        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), rail.x - 1, rail.y + 3);
        assert!(app.sidebar.resizing);
        mouse(&mut app, MouseEventKind::Drag(MouseButton::Left), rail.x - 9, rail.y + 3);
        assert_eq!(app.sidebar.preferences.width, Some(rail.width + 8));
        mouse(&mut app, MouseEventKind::Up(MouseButton::Left), rail.x - 9, rail.y + 3);
        assert!(!app.sidebar.resizing);
        assert_eq!(app.focus, Focus::Input);
        paint_sidebar(&app);
        assert!(app.sidebar.area.get().width > rail.width);
    }

    #[tokio::test]
    async fn sidebar_settings_preserve_drafts_and_tab_mode_cycling() {
        let mut app = sidebar_fixture();
        app.input_buffer = "世 draft".to_string();
        app.cursor_pos = 2;
        paint_sidebar(&app);
        press(&mut app, KeyCode::F(4)).await;
        assert!(app.sidebar.show_settings);
        press(&mut app, KeyCode::Down).await;
        press(&mut app, KeyCode::Char('c')).await;
        assert!(app.sidebar.preferences.sections[1].collapsed);
        press(&mut app, KeyCode::Tab).await;
        assert_eq!(app.mode, "plan");
        assert!(app.sidebar.show_settings);
        press(&mut app, KeyCode::Esc).await;
        assert!(!app.sidebar.show_settings);
        assert_eq!(app.input_buffer, "世 draft");
        assert_eq!(app.cursor_pos, 2);
        let (tx, _rx) = tokio::sync::mpsc::channel(128);
        execute_palette_command(&mut app, "sidebar:settings", None, tx).await.unwrap();
        assert!(app.sidebar.show_settings);
        assert_eq!(app.input_buffer, "世 draft");
    }

    #[tokio::test]
    async fn modals_keep_mouse_and_sidebar_keys_from_changing_background_state() {
        use crossterm::event::{MouseButton, MouseEventKind};
        for modal in ["palette", "help", "approval", "agents", "sidebar"] {
            let mut app = sidebar_fixture();
            paint_sidebar(&app);
            let rail = app.sidebar.area.get();
            match modal {
                "palette" => app.open_palette(),
                "help" => app.show_help = true,
                "approval" => app.active_approval = fixture(
                    BINDINGS.iter().find(|b| b.scope == Scope::Approval).unwrap()
                ).active_approval,
                "agents" => app.open_subagent_modal(&[]),
                _ => app.sidebar.show_settings = true,
            }
            let preferences = app.sidebar.preferences.clone();
            mouse(&mut app, MouseEventKind::ScrollDown, rail.x + 2, rail.y + 5);
            mouse(&mut app, MouseEventKind::Down(MouseButton::Left), rail.x - 1, rail.y + 3);
            assert_eq!(app.sidebar.scroll.get(), 0, "{modal}");
            assert!(!app.sidebar.resizing, "{modal}");
            assert_eq!(app.sidebar.preferences, preferences, "{modal}");
            if modal != "sidebar" {
                press(&mut app, KeyCode::F(4)).await;
                assert!(!app.sidebar.show_settings, "{modal} owns its keys");
            }
        }
    }

    async fn press(app: &mut App, code: KeyCode) {
        let (tx, _rx) = tokio::sync::mpsc::channel(128);
        handle_key_event(app, KeyEvent::new(code, KeyModifiers::NONE), None, tx)
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn start_arrows_transfer_focus_and_tab_only_cycles_mode() {
        for entry in [KeyCode::Up, KeyCode::Down] {
            let mut app = App::new("p", "m", "cow");
            app.resume_sessions = (0..3).map(|i| ResumeSession {
                title: format!("task {i}"),
                relative_time: "1m".to_string(),
                session_id: format!("tsk_{i}"),
            }).collect();
            app.input_buffer = "世 draft".to_string();
            app.cursor_pos = 2;
            press(&mut app, entry).await;
            assert_eq!(app.focus, Focus::Resume);
            assert_eq!(app.selected_resume_index, 0);
            press(&mut app, KeyCode::Tab).await;
            assert_eq!(app.mode, "plan");
            assert_eq!(app.focus, Focus::Resume);
            assert!(app.active_subagent_modal.is_none());
            press(&mut app, KeyCode::Down).await;
            assert_eq!(app.selected_resume_index, 1);
            press(&mut app, KeyCode::Up).await;
            assert_eq!(app.selected_resume_index, 0);
            press(&mut app, KeyCode::Up).await;
            assert_eq!(app.focus, Focus::Input);
            assert_eq!(app.input_buffer, "世 draft");
            assert_eq!(app.cursor_pos, 2);
            assert!(app.row_hover_levels.iter().all(|level| *level == 0.0));
            press(&mut app, KeyCode::Tab).await;
            assert_eq!(app.mode, "code");
            assert_eq!(app.focus, Focus::Input);
        }
        let mut empty = App::new("p", "m", "cow");
        for key in [KeyCode::Up, KeyCode::Down] {
            press(&mut empty, key).await;
            assert_eq!(empty.focus, Focus::Input);
        }
    }

    #[tokio::test]
    async fn start_input_cannot_resume_or_delete_a_selected_session() {
        let binding = BINDINGS.iter().find(|b| b.scope == Scope::Start && b.key == KeyCode::Down).unwrap();
        let mut app = fixture(binding);
        press(&mut app, KeyCode::Enter).await;
        assert_eq!(app.screen_state, ScreenState::Start);
        press(&mut app, KeyCode::Char('d')).await;
        assert_eq!(app.input_buffer, "d");
        assert_eq!(app.pending_delete_index, None);
        app.cursor_pos = 0;
        press(&mut app, KeyCode::Delete).await;
        assert!(app.input_buffer.is_empty());
        assert_eq!(app.pending_delete_index, None);
        press(&mut app, KeyCode::Down).await;
        press(&mut app, KeyCode::Char('d')).await;
        assert_eq!(app.pending_delete_index, Some(0));
        press(&mut app, KeyCode::Esc).await;
        assert_eq!(app.pending_delete_index, None);
        assert_eq!(app.focus, Focus::Resume);
        press(&mut app, KeyCode::Up).await;
        assert_eq!(app.focus, Focus::Input);
        press(&mut app, KeyCode::Down).await;
        press(&mut app, KeyCode::Enter).await;
        assert_eq!(app.screen_state, ScreenState::Session);
        assert_eq!(app.focus, Focus::Input);
        press(&mut app, KeyCode::Char('x')).await;
        assert_eq!(app.input_buffer, "x");
    }

    #[tokio::test]
    async fn start_palette_preserves_focus_and_prompt_click_returns_to_input() {
        for focus in [Focus::Input, Focus::Resume] {
            let mut app = App::new("p", "m", "cow");
            app.set_start_focus(focus);
            app.input_buffer = "draft".to_string();
            app.cursor_pos = 5;
            let (tx, _rx) = tokio::sync::mpsc::channel(128);
            handle_key_event(&mut app, KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL), None, tx).await.unwrap();
            assert!(app.show_palette);
            press(&mut app, KeyCode::Esc).await;
            assert_eq!(app.focus, focus);
            assert_eq!(app.input_buffer, "draft");
            let area = ratatui::layout::Rect::new(0, 0, 120, 40);
            let prompt = app.get_start_prompt_area(area);
            handle_start_mouse_click(&mut app, prompt.x + 5, prompt.y + 1, area);
            assert_eq!(app.focus, Focus::Input);
            assert_eq!(app.input_buffer, "draft");
        }
    }

    #[tokio::test]
    async fn subagents_command_keeps_the_agents_drill_down_reachable() {
        let mut app = App::new("p", "m", "cow");
        let (tx, _rx) = tokio::sync::mpsc::channel(128);
        execute_submitted_text(&mut app, "/subagents".to_string(), None, tx).await.unwrap();
        assert!(app.active_subagent_modal.is_some());
        press(&mut app, KeyCode::Esc).await;
        assert!(app.active_subagent_modal.is_none());
    }

    #[test]
    fn no_surface_advertises_a_binding_that_was_removed() {
        for scope in [
            Scope::Session,
            Scope::Start,
            Scope::Palette,
            Scope::Approval,
            Scope::Agents,
        ] {
            let hints = keymap::footer_hints(scope);
            assert!(!hints.is_empty());
            for stale in ["tab agents", "ctrl+r", "@ files", "esc dismiss"] {
                assert!(
                    !hints.contains(stale),
                    "{scope:?} still advertises {stale:?}"
                );
            }
        }
        assert!(keymap::footer_hints(Scope::Session).contains("tab mode"));
        let listed: usize = keymap::help_sections()
            .iter()
            .map(|(_, rows)| rows.len())
            .sum();
        assert_eq!(listed, BINDINGS.len(), "help must list the whole table");
    }
}

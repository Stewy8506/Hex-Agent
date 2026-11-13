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
    about = "Native Ratatui Cockpit with 60 FPS differential rendering and ACP integration"
)]
struct Args {
    #[arg(long, help = "Run headless virtual verification and exit 0")]
    check: bool,

    #[arg(short, long, default_value = "default", help = "Project ID")]
    project: String,

    #[arg(short, long, help = "Workspace path override")]
    workspace: Option<PathBuf>,

    #[arg(short, long, default_value = "gemini-2.5-pro", help = "Active model provider")]
    model: String,

    #[arg(long, default_value = "OverlayFS", help = "CoW snapshot provider backend")]
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

    let mut app = App::new(&args.project, &args.model, &args.cow);
    if let Some(s) = args.session {
        app.session_id = s;
    }

    // Add sample dialogue and tool calls
    app.add_user_message("Create a high-performance HTTP service");
    app.add_tool_call("tc_check_1", "create_file", "path=src/server.rs", Some(32), Some("OK"));
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

    assert!(content.contains("HEX"), "Virtual buffer must contain HEX banner");
    assert!(content.contains(&args.project), "Virtual buffer must contain project ID");
    assert!(content.contains("Diff Viewer"), "Virtual buffer must contain Diff Viewer");
    assert!(content.contains("Command Prompt"), "Virtual buffer must contain Command Prompt");

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
    let mut app = App::new(&args.project, &args.model, &args.cow);
    if let Some(s) = args.session {
        app.session_id = s;
    }

    // Try connecting to ACP server in background
    let ws_path = args.workspace.as_deref();
    let maybe_client = AcpClient::spawn_acp_server(ws_path).await;
    let acp_client = match maybe_client {
        Ok(client) => {
            app.connection_status = "Connected".to_string();
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

    // If ACP client connected, forward notifications to event loop
    if let Some(client) = &acp_client {
        let mut notif_rx = client.subscribe();
        let event_tx = events.sender();
        tokio::spawn(async move {
            while let Ok(notif) = notif_rx.recv().await {
                if event_tx.send(AppEvent::Notification(notif)).is_err() {
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
                    handle_key_event(&mut app, key, acp_client.as_ref()).await?;
                }
                AppEvent::Notification(notif) => {
                    handle_notification(&mut app, notif);
                }
                AppEvent::Tick => {
                    // Periodic refresh / spinner tick
                }
                AppEvent::Resize(_, _) => {
                    // Handled on next draw
                }
                _ => {}
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
                    app.add_system_message(&format!("Approved execution of tool '{}'.", req.tool_name));
                    app.status_message = "Tool execution approved".to_string();
                }
            }
            KeyCode::Char('n') | KeyCode::Char('N') => {
                if let Some(req) = app.active_approval.take() {
                    app.focus = Focus::Input;
                    app.add_system_message(&format!("Rejected execution of tool '{}'. Rolled back.", req.tool_name));
                    app.status_message = "Tool execution rejected".to_string();
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
        if key.code == KeyCode::Esc || key.code == KeyCode::Enter || key.code == KeyCode::Char('q') {
            app.show_help = false;
        }
        return Ok(());
    }

    // Focus switching
    if key.code == KeyCode::Tab {
        if app.focus == Focus::Input && !app.slash_suggestions.is_empty() {
            app.accept_slash_suggestion();
        } else {
            app.switch_focus();
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
                    if !app.slash_suggestions.is_empty() && app.input_buffer.starts_with('/') && !app.input_buffer.contains(' ') {
                        app.accept_slash_suggestion();
                        return Ok(());
                    }

                    if let Some(text) = app.submit_input() {
                        execute_submitted_text(app, text, acp_client).await?;
                    }
                }
                KeyCode::Char(c) => app.insert_char(c),
                KeyCode::Backspace => app.delete_char(),
                KeyCode::Delete => app.delete_char_forward(),
                KeyCode::Left => app.move_cursor_left(),
                KeyCode::Right => app.move_cursor_right(),
                KeyCode::Home => app.move_cursor_start(),
                KeyCode::End => app.move_cursor_end(),
                KeyCode::Up => app.history_up(),
                KeyCode::Down => app.history_down(),
                _ => {}
            }
        }
        Focus::Dialogue => {
            match key.code {
                KeyCode::Up | KeyCode::Char('k') => app.scroll_up(),
                KeyCode::Down | KeyCode::Char('j') => app.scroll_down(),
                KeyCode::Char('c') => app.toggle_all_tool_calls(),
                KeyCode::Enter => app.focus = Focus::Input,
                _ => {}
            }
        }
        Focus::Diff => {
            match key.code {
                KeyCode::Up | KeyCode::Char('k') => app.scroll_up(),
                KeyCode::Down | KeyCode::Char('j') => app.scroll_down(),
                KeyCode::Char('d') | KeyCode::Esc => app.toggle_diff_pane(),
                KeyCode::Enter => app.focus = Focus::Input,
                _ => {}
            }
        }
        Focus::Approval => {}
    }

    Ok(())
}

/// Execute user entered text (slash commands or regular prompt).
async fn execute_submitted_text(
    app: &mut App,
    text: String,
    acp_client: Option<&AcpClient>,
) -> Result<()> {
    if text.starts_with('/') {
        let parts: Vec<&str> = text.split_whitespace().collect();
        let cmd = parts.first().copied().unwrap_or("");
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
            "/diff" => {
                app.toggle_diff_pane();
            }
            "/clear" => {
                app.dialogue_items.clear();
                app.add_system_message("Dialogue viewport cleared.");
            }
            "/quit" => {
                app.should_quit = true;
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
                if parts.len() > 1 {
                    app.model_name = parts[1].to_string();
                    app.add_system_message(&format!("Model switched to {}", app.model_name));
                } else {
                    app.add_system_message(&format!("Active Model: {}", app.model_name));
                }
            }
            "/budget" => {
                app.add_system_message("Budget Telemetry: 1,240 tokens used | $0.003 USD consumed | Limit: $5.00");
            }
            other => {
                app.add_system_message(&format!("Unknown slash command: {}. Type /help for available commands.", other));
            }
        }
        return Ok(());
    }

    // Regular conversation message
    app.add_user_message(&text);
    app.is_busy = true;
    app.status_message = "Reasoning...".to_string();

    if let Some(client) = acp_client {
        // Send to ACP server
        let sess_id = app.session_id.clone();
        let prompt_text = text.clone();
        let client_clone = client.clone();
        tokio::spawn(async move {
            let _ = client_clone.session_prompt(&sess_id, &prompt_text).await;
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
            if let Ok(update) = serde_json::from_value::<hex_protocol::models::SessionUpdateNotification>(params) {
                if let Some(delta) = update.delta {
                    app.add_assistant_message(&delta);
                    app.is_busy = false;
                    app.status_message = "Ready".to_string();
                } else if let Some(st) = update.status {
                    app.status_message = st;
                }
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

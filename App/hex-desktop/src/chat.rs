//! Interactive chat root view for HEX Desktop (F02 vertical slice).
//!
//! `ChatView` is the real application root opened by `main`. Unlike the static
//! `AppShell` component previews, it owns:
//! - a focusable multiline composer driven by the platform input system
//!   (`EntityInputHandler` + `window.handle_input`, mirroring gpui's canonical
//!   text input pattern),
//! - the live `AcpBridge` connection (connect on startup, real `session/new`,
//!   `session/prompt`, `session/cancel`, `session/approvalDecision`),
//! - per-session feeds and composer drafts so switching sessions preserves
//!   conversation state (Hermes `use-composer-submit.ts` draft semantics),
//! - authoritative turn completion from the `session/prompt` response, with
//!   honest system notices for cancelled/failed/error states.

use std::collections::{HashMap, HashSet};
use std::ops::Range;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::OnceLock;

use gpui::*;
use tokio::runtime::{Handle, Runtime};
use unicode_segmentation::UnicodeSegmentation;

use hex_protocol::error::AcpError;
use hex_protocol::models::{ApprovalDecisionResponse, SessionNewResponse, SessionPromptResponse};

use crate::acp_bridge::{AcpBridge, AcpEvent};
use crate::menu::init_menus;
use crate::shortcuts::{self, init_shortcuts};
use crate::theme::HermesTheme;
use crate::ui::feed::{MessageFeed, MessageItem};
use crate::ui::message::AssistantMessageView;
use crate::ui::modals::approval::{ToolApprovalChoice, ToolApprovalResult};
use crate::ui::modals::{render_modal_container, ActiveModal, PaletteAction, ToolApprovalModal};
use crate::ui::model_pill::SendButton;
use crate::ui::session_list::SessionItem;
use crate::ui::shell::AppShell;
use crate::ui::sidebar::ProviderState;
use crate::ui::tool_card::ToolCallCard;
use crate::window::WindowConfig;

gpui::actions!(
    hex_chat,
    [
        Backspace,
        Delete,
        Left,
        Right,
        SelectLeft,
        SelectRight,
        Home,
        End,
        Up,
        Down,
        InsertNewline,
    ]
);

/// Composer-scoped keybindings layered on top of the global desktop shortcuts.
pub fn composer_keybindings() -> Vec<KeyBinding> {
    vec![
        KeyBinding::new("enter", shortcuts::SubmitPrompt, Some("Composer")),
        KeyBinding::new("shift-enter", InsertNewline, Some("Composer")),
        KeyBinding::new("backspace", Backspace, Some("Composer")),
        KeyBinding::new("delete", Delete, Some("Composer")),
        KeyBinding::new("left", Left, Some("Composer")),
        KeyBinding::new("right", Right, Some("Composer")),
        KeyBinding::new("shift-left", SelectLeft, Some("Composer")),
        KeyBinding::new("shift-right", SelectRight, Some("Composer")),
        KeyBinding::new("home", Home, Some("Composer")),
        KeyBinding::new("end", End, Some("Composer")),
        KeyBinding::new("up", Up, Some("Composer")),
        KeyBinding::new("down", Down, Some("Composer")),
        // Modal overlay owns focus while open; the same keys drive modal
        // navigation/confirmation instead of the editor underneath.
        KeyBinding::new("enter", shortcuts::SubmitPrompt, Some("Modal")),
        KeyBinding::new("backspace", Backspace, Some("Modal")),
        KeyBinding::new("up", Up, Some("Modal")),
        KeyBinding::new("down", Down, Some("Modal")),
    ]
}

/// Register theme, menus, global shortcuts, and composer bindings.
/// Shared by the real `main` entry point and the acceptance tests.
pub fn init_app(cx: &mut App) {
    HermesTheme::init(cx);
    init_shortcuts(cx);
    init_menus(cx);
    cx.bind_keys(composer_keybindings());
}

/// Process-wide tokio runtime backing the ACP bridge (which spawns tokio tasks).
pub fn app_runtime() -> &'static Runtime {
    static RUNTIME: OnceLock<Runtime> = OnceLock::new();
    RUNTIME.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("failed to build tokio runtime for HEX Desktop")
    })
}

/// Open the real chat window. Used by `main`; tests drive `ChatView::new`
/// through the gpui test platform with the same constructor.
pub fn open_chat_window(
    cx: &mut App,
    bridge: AcpBridge,
    rt: Handle,
    workspace: PathBuf,
) -> anyhow::Result<WindowHandle<ChatView>> {
    let options = WindowConfig::default().window_options(None);
    cx.open_window(options, move |_window, cx| {
        cx.new(|cx| ChatView::new(bridge.clone(), rt.clone(), workspace.clone(), cx))
    })
}

/// Saved state for an inactive session.
#[derive(Debug, Clone, Default)]
struct SessionState {
    feed: MessageFeed,
    draft: String,
}

fn find_assistant_mut<'a>(feed: &'a mut MessageFeed, id: &str) -> Option<&'a mut AssistantMessageView> {
    feed.messages.iter_mut().rev().find_map(|m| match m {
        MessageItem::Assistant(a) if a.id == id => Some(a),
        _ => None,
    })
}

/// Replace the turn's streamed preview with the authoritative final text (B31).
///
/// The final answer wins even when the preview is non-empty: lost or truncated
/// deltas must never be presented as the completed answer.
fn apply_authoritative_final(
    feed: &mut MessageFeed,
    assistant_id: Option<&str>,
    text: &str,
    model_name: &str,
) {
    if text.is_empty() {
        return;
    }
    match assistant_id.and_then(|id| find_assistant_mut(feed, id)) {
        Some(assistant) => {
            assistant.content = text.to_string();
            assistant.is_streaming = false;
            assistant.model_name = model_name.to_string();
        }
        None => {
            feed.add_assistant_message(text.to_string());
            if let Some(MessageItem::Assistant(a)) = feed.messages.last_mut() {
                a.is_streaming = false;
                a.model_name = model_name.to_string();
            }
        }
    }
}

/// The interactive desktop root: sidebar + feed + composer wired to ACP.
pub struct ChatView {
    pub shell: AppShell,
    focus_handle: FocusHandle,
    modal_focus_handle: FocusHandle,
    last_modal_open: bool,

    // Composer editor state (utf8 byte offsets, mirroring gpui examples/input.rs).
    content: String,
    selected_range: Range<usize>,
    selection_reversed: bool,
    marked_range: Option<Range<usize>>,
    last_layout: Option<Vec<ShapedLine>>,
    last_bounds: Option<Bounds<Pixels>>,
    last_line_height: Pixels,
    is_selecting: bool,

    bridge: AcpBridge,
    rt: Handle,
    workspace: PathBuf,

    sessions: HashMap<String, SessionState>,
    running_turns: HashSet<String>,
    /// Assistant message id created for each session's current/most recent turn.
    /// The prompt response and the streamed `session/update` notifications race
    /// on separate paths, so all turn output reconciles into this one message.
    turn_assistants: HashMap<String, String>,
    current_model: String,
    current_provider: String,
    model_id: Option<String>,
}

impl Focusable for ChatView {
    fn focus_handle(&self, _: &App) -> FocusHandle {
        self.focus_handle.clone()
    }
}

impl ChatView {
    pub fn new(bridge: AcpBridge, rt: Handle, workspace: PathBuf, cx: &mut Context<Self>) -> Self {
        let workspace_name = workspace
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| "Workspace".to_string());
        let git_branch = std::process::Command::new("git")
            .args(["rev-parse", "--abbrev-ref", "HEAD"])
            .current_dir(&workspace)
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .unwrap_or_else(|| "no git".to_string());

        let mut shell = AppShell::new().with_acp_bridge(bridge.clone());
        shell.sidebar.set_workspace(workspace_name, git_branch);
        shell.sidebar.provider_status.state = if bridge.is_connected() {
            ProviderState::Connected
        } else {
            ProviderState::Offline
        };
        // Honest identity until session/new reports the real provider/model.
        shell.sidebar.provider_status.provider_name = "HEX".to_string();
        shell.sidebar.provider_status.model_name = String::new();
        shell.composer.model_pill.set_model("HEX", "connecting…");

        let mut view = Self {
            shell,
            focus_handle: cx.focus_handle(),
            modal_focus_handle: cx.focus_handle(),
            last_modal_open: false,
            content: String::new(),
            selected_range: 0..0,
            selection_reversed: false,
            marked_range: None,
            last_layout: None,
            last_bounds: None,
            last_line_height: px(20.0),
            is_selecting: false,
            bridge,
            rt,
            workspace,
            sessions: HashMap::new(),
            running_turns: HashSet::new(),
            turn_assistants: HashMap::new(),
            current_model: String::new(),
            current_provider: "HEX".to_string(),
            model_id: None,
        };
        view.start_event_pump(cx);
        view.start_backend(cx);
        view
    }

    // ------------------------------------------------------------------
    // Backend lifecycle
    // ------------------------------------------------------------------

    fn start_backend(&mut self, cx: &mut Context<Self>) {
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let cwd = self.workspace.to_string_lossy().to_string();
        cx.spawn(async move |this, cx| {
            if !bridge.is_connected() {
                let b = bridge.clone();
                let res = rt.spawn(async move { b.connect_auto().await }).await;
                if !matches!(res, Ok(Ok(()))) {
                    this.update(cx, |view, cx| {
                        view.shell.sidebar.provider_status.state = ProviderState::Offline;
                        view.shell
                            .composer
                            .model_pill
                            .set_model("HEX", "not connected");
                        view.shell.feed.add_system_notice(
                            "Could not connect to the HEX runtime. Install it (install.sh) or start `hex acp`, then relaunch.",
                        );
                        cx.notify();
                    })
                    .ok();
                    return;
                }
            }
            let b = bridge.clone();
            let res = rt
                .spawn(async move { b.new_session_full(Some(&cwd), None).await })
                .await;
            this.update(cx, |view, cx| match res {
                Ok(Ok(resp)) => view.adopt_session(resp, cx),
                Ok(Err(err)) => {
                    view.shell.composer.model_pill.set_model("HEX", "no session");
                    view.shell
                        .feed
                        .add_system_notice(format!("Could not create a session: {err}"));
                    cx.notify();
                }
                Err(err) => {
                    view.shell.composer.model_pill.set_model("HEX", "no session");
                    view.shell
                        .feed
                        .add_system_notice(format!("Session task aborted: {err}"));
                    cx.notify();
                }
            })
            .ok();
        })
        .detach();
    }

    fn start_event_pump(&mut self, cx: &mut Context<Self>) {
        let mut rx = self.bridge.subscribe();
        cx.spawn(async move |this, cx| loop {
            match rx.recv().await {
                Ok(event) => {
                    if this
                        .update(cx, |view, cx| view.route_event(event, cx))
                        .is_err()
                    {
                        break;
                    }
                }
                Err(tokio::sync::broadcast::error::RecvError::Lagged(dropped)) => {
                    if this
                        .update(cx, |view, cx| view.note_stream_lag(dropped, cx))
                        .is_err()
                    {
                        break;
                    }
                }
                Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
            }
        })
        .detach();
    }

    fn route_event(&mut self, event: AcpEvent, cx: &mut Context<Self>) {
        let session_id = match &event {
            AcpEvent::TokenChunk { session_id, .. }
            | AcpEvent::TurnComplete { session_id }
            | AcpEvent::ToolCall { session_id, .. }
            | AcpEvent::ToolResult { session_id, .. }
            | AcpEvent::ApprovalRequest { session_id, .. } => Some(session_id.clone()),
            _ => None,
        };

        let Some(sid) = session_id else {
            if matches!(event, AcpEvent::Disconnected { .. }) {
                self.shell
                    .feed
                    .add_system_notice("Connection to the HEX runtime was lost.");
            }
            self.shell.handle_acp_event(event);
            self.sync_transport_state();
            cx.notify();
            return;
        };

        // Approvals surface as a modal regardless of which session is active.
        if let AcpEvent::ApprovalRequest {
            approval_id,
            tool_name,
            risk_level,
            command,
            description,
            ..
        } = event
        {
            let mut modal = ToolApprovalModal::new(approval_id, tool_name, command, risk_level);
            if let Some(desc) = description {
                modal = modal.with_description(desc);
            }
            self.shell.open_approval(modal);
            self.sync_transport_state();
            cx.notify();
            return;
        }

        let is_active = self.shell.active_session_id.as_deref() == Some(sid.as_str());
        let running = self.running_turns.contains(&sid);
        let turn_assistant = self.turn_assistants.get(&sid).cloned();

        {
            let feed: &mut MessageFeed = if is_active {
                &mut self.shell.feed
            } else if let Some(state) = self.sessions.get_mut(&sid) {
                &mut state.feed
            } else {
                self.sync_transport_state();
                cx.notify();
                return;
            };

            match event {
                AcpEvent::TokenChunk { text, .. } => {
                    if let Some(assistant) = turn_assistant
                        .as_deref()
                        .and_then(|id| find_assistant_mut(feed, id))
                    {
                        // While the turn is running always append; after the
                        // authoritative response only fill a still-empty message
                        // (late deltas must not duplicate the final text).
                        if running || assistant.content.is_empty() {
                            assistant.content.push_str(&text);
                        }
                    } else if running {
                        if !feed.is_streaming {
                            feed.start_assistant_streaming();
                        }
                        feed.append_streaming_chunk(&text);
                    }
                }
                AcpEvent::TurnComplete { .. } => {
                    feed.finish_streaming();
                }
                AcpEvent::ToolCall {
                    call_id,
                    tool_name,
                    arguments_json,
                    ..
                } => {
                    let card = ToolCallCard::new(call_id, tool_name, arguments_json);
                    match turn_assistant
                        .as_deref()
                        .and_then(|id| find_assistant_mut(feed, id))
                    {
                        Some(assistant) => assistant.tool_calls.push(card),
                        None => feed.add_tool_call(card),
                    }
                }
                AcpEvent::ToolResult {
                    call_id,
                    output_text,
                    is_error,
                    ..
                } => {
                    feed.update_tool_result(&call_id, &output_text, is_error);
                }
                _ => {}
            }

            if let Some(assistant) = turn_assistant
                .as_deref()
                .and_then(|id| find_assistant_mut(feed, id))
            {
                assistant.model_name = self.current_model.clone();
            }
        }

        self.sync_transport_state();
        cx.notify();
    }

    fn sync_transport_state(&mut self) {
        let running = self.active_turn_running();
        self.shell.composer.set_streaming(running);
        self.shell.sidebar.provider_status.state = if !self.bridge.is_connected() {
            ProviderState::Offline
        } else if running {
            ProviderState::Generating
        } else {
            ProviderState::Connected
        };
    }

    fn active_turn_running(&self) -> bool {
        self.shell
            .active_session_id
            .as_deref()
            .map(|id| self.running_turns.contains(id))
            .unwrap_or(false)
    }

    // ------------------------------------------------------------------
    // Sessions
    // ------------------------------------------------------------------

    fn save_active_session(&mut self) {
        if let Some(current) = self.shell.active_session_id.clone() {
            let feed = std::mem::replace(&mut self.shell.feed, MessageFeed::new());
            let draft = self.content.clone();
            self.sessions.insert(current, SessionState { feed, draft });
        }
    }

    fn adopt_session(&mut self, resp: SessionNewResponse, cx: &mut Context<Self>) {
        self.save_active_session();
        let id = resp.session_id.clone();
        self.shell.feed = MessageFeed::new();
        self.reset_editor(cx);
        self.shell
            .sidebar
            .add_session(SessionItem::new(id.clone(), "New Chat").with_model(resp.model.clone()));
        self.shell.select_session(id);
        self.current_model = resp.model.clone();
        self.model_id = Some(resp.model.clone());
        self.current_provider = resp.provider.clone().unwrap_or_else(|| "HEX".to_string());
        self.shell
            .composer
            .model_pill
            .set_model(self.current_provider.clone(), resp.model.clone());
        self.shell.sidebar.provider_status.provider_name = self.current_provider.clone();
        self.shell.sidebar.provider_status.model_name = resp.model;
        self.sync_transport_state();
        cx.notify();
    }

    fn switch_session(&mut self, id: String, cx: &mut Context<Self>) {
        if self.shell.active_session_id.as_deref() == Some(id.as_str()) {
            return;
        }
        if !self
            .shell
            .sidebar
            .session_list
            .sessions
            .iter()
            .any(|s| s.id == id)
        {
            return;
        }
        self.save_active_session();
        let state = self.sessions.remove(&id).unwrap_or_default();
        self.shell.feed = state.feed;
        self.set_editor_content(state.draft, cx);
        self.shell.select_session(id);
        self.sync_transport_state();
        cx.notify();
    }

    fn request_new_chat(&mut self, cx: &mut Context<Self>) {
        if !self.bridge.is_connected() {
            self.shell.feed.add_system_notice(
                "Not connected to the HEX runtime — cannot start a new chat.",
            );
            cx.notify();
            return;
        }
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let cwd = self.workspace.to_string_lossy().to_string();
        let model = self.model_id.clone();
        cx.spawn(async move |this, cx| {
            let res = rt
                .spawn(async move { bridge.new_session_full(Some(&cwd), model.as_deref()).await })
                .await;
            this.update(cx, |view, cx| match res {
                Ok(Ok(resp)) => view.adopt_session(resp, cx),
                Ok(Err(err)) => {
                    view.shell
                        .feed
                        .add_system_notice(format!("Could not create session: {err}"));
                    cx.notify();
                }
                Err(err) => {
                    view.shell
                        .feed
                        .add_system_notice(format!("Session task aborted: {err}"));
                    cx.notify();
                }
            })
            .ok();
        })
        .detach();
    }

    // ------------------------------------------------------------------
    // Turns
    // ------------------------------------------------------------------

    fn submit_prompt(&mut self, cx: &mut Context<Self>) {
        let text = self.content.trim().to_string();
        if text.is_empty() {
            return;
        }
        self.submit_text(text, cx);
    }

    fn submit_text(&mut self, text: String, cx: &mut Context<Self>) {
        if self.active_turn_running() {
            return;
        }
        let Some(session_id) = self.shell.active_session_id.clone() else {
            self.shell
                .feed
                .add_system_notice("No active session yet — waiting for the HEX runtime.");
            cx.notify();
            return;
        };

        self.reset_editor(cx);
        self.shell.feed.add_user_message(text.clone());
        // Create the turn's assistant message up front so streamed deltas, tool
        // cards, and the authoritative response all reconcile into one message
        // regardless of which path wins the race.
        let assistant_id = self.shell.feed.start_assistant_streaming();
        self.turn_assistants
            .insert(session_id.clone(), assistant_id);

        // Hermes-style auto title from the first prompt.
        if self
            .shell
            .sidebar
            .session_list
            .selected_session()
            .map(|s| s.title == "New Chat")
            .unwrap_or(false)
        {
            let title: String = text.chars().take(48).collect();
            self.shell
                .sidebar
                .session_list
                .rename_session(&session_id, title);
        }

        self.running_turns.insert(session_id.clone());
        self.sync_transport_state();
        cx.notify();

        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let sid = session_id.clone();
        cx.spawn(async move |this, cx| {
            let result = rt
                .spawn(async move { bridge.send_prompt(&sid, &text).await })
                .await;
            this.update(cx, |view, cx| view.finish_turn(&session_id, result, cx))
                .ok();
        })
        .detach();
    }

    fn finish_turn(
        &mut self,
        session_id: &str,
        result: Result<Result<SessionPromptResponse, AcpError>, tokio::task::JoinError>,
        cx: &mut Context<Self>,
    ) {
        self.running_turns.remove(session_id);
        let is_active = self.shell.active_session_id.as_deref() == Some(session_id);
        let feed: &mut MessageFeed = if is_active {
            &mut self.shell.feed
        } else {
            match self.sessions.get_mut(session_id) {
                Some(state) => &mut state.feed,
                None => {
                    self.sync_transport_state();
                    cx.notify();
                    return;
                }
            }
        };

        feed.finish_streaming();
        let turn_assistant = self.turn_assistants.get(session_id).cloned();
        match result {
            Ok(Ok(resp)) => {
                if resp.is_completed() {
                    let text = resp.text_content();
                    apply_authoritative_final(feed, turn_assistant.as_deref(), &text, &self.current_model);
                    if resp.turn_index.is_some() && resp.response_chars.is_some() {
                        let bridge = self.bridge.clone();
                        let rt = self.rt.clone();
                        let sid = session_id.to_string();
                        let assistant_id = turn_assistant.clone();
                        cx.spawn(async move |this, cx| {
                            let sid2 = sid.clone();
                            let res = rt
                                .spawn(async move { bridge.fetch_canonical_final(&sid2, &resp).await })
                                .await;
                            this.update(cx, |view, cx| {
                                view.reconcile_canonical_final(&sid, assistant_id.as_deref(), res, cx);
                            })
                            .ok();
                        })
                        .detach();
                    }
                } else if resp.status.eq_ignore_ascii_case("cancelled") {
                    feed.add_system_notice("Turn cancelled.");
                } else {
                    feed.add_system_notice(format!(
                        "Turn ended with status \"{}\".",
                        resp.status
                    ));
                }
            }
            Ok(Err(err)) => {
                feed.add_system_notice(format!("Prompt failed: {err}"));
            }
            Err(err) => {
                feed.add_system_notice(format!("Prompt task aborted: {err}"));
            }
        }
        // Drop the placeholder when the turn produced no content at all.
        if let Some(id) = turn_assistant.as_deref() {
            if let Some(pos) = feed.messages.iter().position(|m| {
                matches!(m, MessageItem::Assistant(a) if a.id == id && a.content.is_empty() && a.tool_calls.is_empty())
            }) {
                feed.messages.remove(pos);
            }
        }
        self.sync_transport_state();
        cx.notify();
    }

    /// Replace the turn's preview with the authoritative paged final (B31).
    fn reconcile_canonical_final(
        &mut self,
        session_id: &str,
        assistant_id: Option<&str>,
        res: Result<Result<String, AcpError>, tokio::task::JoinError>,
        cx: &mut Context<Self>,
    ) {
        let text = match res {
            Ok(Ok(text)) => text,
            Ok(Err(err)) => {
                self.shell.feed.add_system_notice(format!(
                    "Could not fetch the full final answer: {err}. The preview may be incomplete."
                ));
                cx.notify();
                return;
            }
            Err(err) => {
                self.shell.feed.add_system_notice(format!(
                    "Final-answer task aborted: {err}. The preview may be incomplete."
                ));
                cx.notify();
                return;
            }
        };
        if text.is_empty() {
            return;
        }
        let is_active = self.shell.active_session_id.as_deref() == Some(session_id);
        let feed: &mut MessageFeed = if is_active {
            &mut self.shell.feed
        } else {
            match self.sessions.get_mut(session_id) {
                Some(state) => &mut state.feed,
                None => {
                    cx.notify();
                    return;
                }
            }
        };
        apply_authoritative_final(feed, assistant_id, &text, &self.current_model);
        cx.notify();
    }

    /// A lagged broadcast means deltas were dropped; say so instead of silently
    /// presenting a truncated preview as the answer (B31).
    fn note_stream_lag(&mut self, dropped: u64, cx: &mut Context<Self>) {
        self.shell.feed.add_system_notice(format!(
            "Live stream lagged ({dropped} events dropped); the authoritative final answer will replace the preview."
        ));
        cx.notify();
    }

    fn cancel_turn(&mut self, cx: &mut Context<Self>) {
        if !self.active_turn_running() {
            return;
        }
        let Some(session_id) = self.shell.active_session_id.clone() else {
            return;
        };
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        cx.spawn(async move |this, cx| {
            let res = rt
                .spawn(async move { bridge.cancel_session(&session_id).await })
                .await;
            this.update(cx, |view, cx| {
                if !matches!(res, Ok(Ok(()))) {
                    view.shell.feed.add_system_notice(
                        "Cancel request failed — the runtime did not acknowledge it.",
                    );
                }
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    fn regenerate(&mut self, assistant_id: &str, cx: &mut Context<Self>) {
        let messages = &self.shell.feed.messages;
        let Some(pos) = messages.iter().position(
            |m| matches!(m, MessageItem::Assistant(a) if a.id == assistant_id),
        ) else {
            return;
        };
        let prompt = messages[..pos].iter().rev().find_map(|m| match m {
            MessageItem::User(u) => Some(u.content.clone()),
            _ => None,
        });
        match prompt {
            Some(text) => self.submit_text(text, cx),
            None => {
                self.shell
                    .feed
                    .add_system_notice("Nothing to regenerate — no preceding prompt.");
                cx.notify();
            }
        }
    }

    fn last_assistant_content(&self) -> Option<String> {
        self.shell.feed.messages.iter().rev().find_map(|m| match m {
            MessageItem::Assistant(a) if !a.content.is_empty() => Some(a.content.clone()),
            _ => None,
        })
    }

    /// Current composer draft text.
    pub fn composer_text(&self) -> &str {
        &self.content
    }

    /// Whether the composer editor currently owns keyboard focus.
    pub fn is_composer_focused(&self, window: &Window) -> bool {
        self.focus_handle.is_focused(window)
    }

    /// Whether the modal overlay currently owns keyboard focus.
    pub fn is_modal_focused(&self, window: &Window) -> bool {
        self.modal_focus_handle.is_focused(window)
    }

    // ------------------------------------------------------------------
    // Approvals & palette
    // ------------------------------------------------------------------

    fn send_approval_result(&mut self, result: ToolApprovalResult, cx: &mut Context<Self>) {
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let approval_id = result.request_id.clone();
        let choice = result.choice;
        let ttl = result.lease_seconds();
        cx.spawn(async move |this, cx| {
            let id = approval_id.clone();
            let res = rt
                .spawn(async move {
                    bridge
                        .send_approval_decision(&approval_id, choice, ttl)
                        .await
                })
                .await;
            this.update(cx, |view, cx| {
                view.apply_approval_outcome(&id, choice, res, cx);
            })
            .ok();
        })
        .detach();
    }

    /// Apply the server's typed approval outcome (B27).
    ///
    /// Nothing is announced before the runtime confirms it. An unapplied or
    /// failed decision leaves the modal open and retryable instead of claiming
    /// an approval that never happened.
    fn apply_approval_outcome(
        &mut self,
        approval_id: &str,
        choice: ToolApprovalChoice,
        res: Result<Result<ApprovalDecisionResponse, AcpError>, tokio::task::JoinError>,
        cx: &mut Context<Self>,
    ) {
        if let ActiveModal::Approval(modal) = &mut self.shell.active_modal {
            if modal.request_id == approval_id {
                modal.is_submitting = false;
            }
        }
        match res {
            Ok(Ok(resp)) if resp.status == "applied" && resp.applied != Some(false) => {
                self.shell.dismiss_modal();
                let outcome = resp.outcome.clone().unwrap_or_else(|| match choice {
                    ToolApprovalChoice::Deny => "DENY".to_string(),
                    _ => "APPROVE".to_string(),
                });
                let notice = if outcome == "DENY" {
                    "Approval denied; the runtime blocked the action.".to_string()
                } else if let (Some(lease_id), Some(ttl)) = (&resp.lease_id, resp.lease_ttl_seconds) {
                    format!("Approved for this session; lease {lease_id} expires in {ttl}s.")
                } else if choice == ToolApprovalChoice::Session {
                    "Approved for this session with the default scoped lease.".to_string()
                } else {
                    "Approved once; the runtime executed the action.".to_string()
                };
                self.shell.feed.add_system_notice(notice);
            }
            Ok(Ok(resp)) => {
                self.shell.feed.add_system_notice(format!(
                    "The runtime did not apply this decision ({}; approval {}); no pending request matched. Retry or deny.",
                    resp.status, resp.approval_id
                ));
            }
            Ok(Err(err)) => {
                self.shell.feed.add_system_notice(format!(
                    "Approval decision failed: {err}. No decision confirmed; retry or deny."
                ));
            }
            Err(err) => {
                self.shell.feed.add_system_notice(format!(
                    "Approval task aborted: {err}. No decision confirmed; retry or deny."
                ));
            }
        }
        cx.notify();
    }

    fn execute_palette_action(&mut self, action: PaletteAction, cx: &mut Context<Self>) {
        match action {
            PaletteAction::NewChat => self.request_new_chat(cx),
            PaletteAction::SelectSession(id) => self.switch_session(id, cx),
            PaletteAction::ToggleSidebar => {
                self.shell.toggle_sidebar();
                cx.notify();
            }
            PaletteAction::ClearHistory => {
                self.shell.feed.clear();
                cx.notify();
            }
            other => {
                self.shell.feed.add_system_notice(format!(
                    "{other:?} is not available in the desktop yet."
                ));
                cx.notify();
            }
        }
    }

    fn apply_model_choice(&mut self, model_id: String, cx: &mut Context<Self>) {
        self.model_id = Some(model_id.clone());
        self.current_model = model_id.clone();
        self.shell
            .composer
            .model_pill
            .set_model(self.current_provider.clone(), model_id.clone());
        self.shell.sidebar.provider_status.model_name = model_id;
        cx.notify();
    }

    /// Route a key to the open modal. Returns true when the modal consumed it.
    fn handle_modal_key(&mut self, key: &str, cx: &mut Context<Self>) -> bool {
        match &mut self.shell.active_modal {
            ActiveModal::Approval(modal) => {
                if modal.is_submitting {
                    return true;
                }
                let result = modal.handle_key(key);
                if let Some(result) = result {
                    modal.is_submitting = true;
                    self.send_approval_result(result, cx);
                    true
                } else {
                    matches!(key, "y" | "a" | "n" | "l" | "1" | "2" | "3")
                }
            }
            ActiveModal::Palette(palette) => {
                if key == "escape" {
                    self.shell.dismiss_modal();
                    cx.notify();
                    return true;
                }
                if let Some(action) = palette.handle_key(key) {
                    self.shell.dismiss_modal();
                    self.execute_palette_action(action, cx);
                    return true;
                }
                match key {
                    // handle_key already moved the highlight; consume the key so
                    // it never reaches the editor underneath the modal.
                    "up" | "down" => {
                        cx.notify();
                        true
                    }
                    "backspace" => {
                        let mut query = palette.query.clone();
                        query.pop();
                        palette.set_query(query);
                        cx.notify();
                        true
                    }
                    _ => false,
                }
            }
            ActiveModal::ModelPicker(picker) => {
                if key == "escape" {
                    self.shell.dismiss_modal();
                    cx.notify();
                    return true;
                }
                if let Some(model_id) = picker.handle_key(key) {
                    self.shell.dismiss_modal();
                    self.apply_model_choice(model_id, cx);
                    return true;
                }
                matches!(key, "up" | "down")
            }
            ActiveModal::None => false,
        }
    }

    // ------------------------------------------------------------------
    // Action handlers
    // ------------------------------------------------------------------

    fn on_submit(&mut self, _: &shortcuts::SubmitPrompt, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("enter", cx) {
            return;
        }
        self.submit_prompt(cx);
    }

    fn on_new_chat(&mut self, _: &shortcuts::NewChat, _: &mut Window, cx: &mut Context<Self>) {
        self.request_new_chat(cx);
    }

    fn on_toggle_palette(
        &mut self,
        _: &shortcuts::ToggleCommandPalette,
        _: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if matches!(self.shell.active_modal, ActiveModal::Palette(_)) {
            self.shell.dismiss_modal();
        } else {
            self.shell.open_command_palette();
        }
        cx.notify();
    }

    fn on_toggle_sidebar(
        &mut self,
        _: &shortcuts::ToggleSidebar,
        _: &mut Window,
        cx: &mut Context<Self>,
    ) {
        self.shell.toggle_sidebar();
        cx.notify();
    }

    fn on_open_settings(
        &mut self,
        _: &shortcuts::OpenSettings,
        _: &mut Window,
        cx: &mut Context<Self>,
    ) {
        self.shell
            .feed
            .add_system_notice("Settings are not available in the desktop yet.");
        cx.notify();
    }

    fn on_cancel_or_dismiss(
        &mut self,
        _: &shortcuts::CancelOrDismiss,
        _: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if self.handle_modal_key("escape", cx) {
            return;
        }
        self.cancel_turn(cx);
    }

    fn on_copy(&mut self, _: &shortcuts::Copy, window: &mut Window, cx: &mut Context<Self>) {
        if self.shell.active_modal.is_open() {
            return;
        }
        if self.focus_handle.is_focused(window) && !self.selected_range.is_empty() {
            cx.write_to_clipboard(ClipboardItem::new_string(
                self.content[self.selected_range.clone()].to_string(),
            ));
        } else if let Some(text) = self.last_assistant_content() {
            cx.write_to_clipboard(ClipboardItem::new_string(text));
        }
    }

    fn on_cut(&mut self, _: &shortcuts::Cut, window: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("cmd-x", cx) {
            return;
        }
        if !self.selected_range.is_empty() {
            cx.write_to_clipboard(ClipboardItem::new_string(
                self.content[self.selected_range.clone()].to_string(),
            ));
            self.replace_text_in_range(None, "", window, cx);
        }
    }

    fn on_paste(&mut self, _: &shortcuts::Paste, window: &mut Window, cx: &mut Context<Self>) {
        if self.shell.active_modal.is_open() {
            return;
        }
        if let Some(text) = cx.read_from_clipboard().and_then(|item| item.text()) {
            self.replace_text_in_range(None, &text, window, cx);
        }
    }

    fn on_select_all(
        &mut self,
        _: &shortcuts::SelectAll,
        _: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if self.shell.active_modal.is_open() {
            return;
        }
        self.move_to(0, cx);
        self.select_to(self.content.len(), cx);
    }

    fn on_insert_newline(
        &mut self,
        _: &InsertNewline,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if self.handle_modal_key("shift-enter", cx) {
            return;
        }
        self.replace_text_in_range(None, "\n", window, cx);
    }

    fn on_backspace(&mut self, _: &Backspace, window: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("backspace", cx) {
            return;
        }
        if self.selected_range.is_empty() {
            self.select_to(self.previous_boundary(self.cursor_offset()), cx);
        }
        self.replace_text_in_range(None, "", window, cx);
    }

    fn on_delete(&mut self, _: &Delete, window: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("delete", cx) {
            return;
        }
        if self.selected_range.is_empty() {
            self.select_to(self.next_boundary(self.cursor_offset()), cx);
        }
        self.replace_text_in_range(None, "", window, cx);
    }

    fn on_left(&mut self, _: &Left, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("left", cx) {
            return;
        }
        if self.selected_range.is_empty() {
            self.move_to(self.previous_boundary(self.cursor_offset()), cx);
        } else {
            self.move_to(self.selected_range.start, cx);
        }
    }

    fn on_right(&mut self, _: &Right, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("right", cx) {
            return;
        }
        if self.selected_range.is_empty() {
            self.move_to(self.next_boundary(self.selected_range.end), cx);
        } else {
            self.move_to(self.selected_range.end, cx);
        }
    }

    fn on_select_left(&mut self, _: &SelectLeft, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("shift-left", cx) {
            return;
        }
        self.select_to(self.previous_boundary(self.cursor_offset()), cx);
    }

    fn on_select_right(&mut self, _: &SelectRight, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("shift-right", cx) {
            return;
        }
        self.select_to(self.next_boundary(self.cursor_offset()), cx);
    }

    fn on_home(&mut self, _: &Home, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("home", cx) {
            return;
        }
        let (line, _) = self.line_col(self.cursor_offset());
        self.move_to(self.line_starts()[line], cx);
    }

    fn on_end(&mut self, _: &End, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("end", cx) {
            return;
        }
        let (line, _) = self.line_col(self.cursor_offset());
        let starts = self.line_starts();
        self.move_to(starts[line] + self.line_len(line), cx);
    }

    fn on_up(&mut self, _: &Up, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("up", cx) {
            return;
        }
        self.move_vertically(true, cx);
    }

    fn on_down(&mut self, _: &Down, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("down", cx) {
            return;
        }
        self.move_vertically(false, cx);
    }

    fn on_key_down(&mut self, event: &KeyDownEvent, _: &mut Window, cx: &mut Context<Self>) {
        let keystroke = &event.keystroke;
        match &self.shell.active_modal {
            ActiveModal::Approval(_) => {
                if self.handle_modal_key(&keystroke.key, cx) {
                    cx.stop_propagation();
                }
            }
            ActiveModal::Palette(_) => {
                if keystroke.modifiers.platform || keystroke.modifiers.control {
                    return;
                }
                let typed = keystroke.key_char.clone().or_else(|| {
                    (keystroke.key.chars().count() == 1).then(|| keystroke.key.clone())
                });
                if let Some(ch) = typed {
                    let ActiveModal::Palette(palette) = &mut self.shell.active_modal else {
                        return;
                    };
                    let query = format!("{}{}", palette.query, ch);
                    palette.set_query(query);
                    cx.notify();
                    cx.stop_propagation();
                }
            }
            _ => {}
        }
    }

    fn on_mouse_down(
        &mut self,
        event: &MouseDownEvent,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if self.shell.active_modal.is_open() {
            return;
        }
        self.focus_handle.focus(window);
        self.is_selecting = true;
        let index = self.index_for_mouse_position(event.position);
        if event.modifiers.shift {
            self.select_to(index, cx);
        } else {
            self.move_to(index, cx);
        }
    }

    fn on_mouse_up(&mut self, _: &MouseUpEvent, _: &mut Window, cx: &mut Context<Self>) {
        self.is_selecting = false;
        cx.notify();
    }

    fn on_mouse_move(
        &mut self,
        event: &MouseMoveEvent,
        _: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if self.is_selecting {
            self.select_to(self.index_for_mouse_position(event.position), cx);
        }
    }

    // ------------------------------------------------------------------
    // Editor state helpers (gpui examples/input.rs pattern)
    // ------------------------------------------------------------------

    fn set_editor_content(&mut self, text: String, cx: &mut Context<Self>) {
        self.content = text;
        let len = self.content.len();
        self.selected_range = len..len;
        self.selection_reversed = false;
        self.marked_range = None;
        cx.notify();
    }

    fn reset_editor(&mut self, cx: &mut Context<Self>) {
        self.set_editor_content(String::new(), cx);
    }

    fn move_to(&mut self, offset: usize, cx: &mut Context<Self>) {
        self.selected_range = offset..offset;
        cx.notify();
    }

    fn cursor_offset(&self) -> usize {
        if self.selection_reversed {
            self.selected_range.start
        } else {
            self.selected_range.end
        }
    }

    fn select_to(&mut self, offset: usize, cx: &mut Context<Self>) {
        if self.selection_reversed {
            self.selected_range.start = offset;
        } else {
            self.selected_range.end = offset;
        }
        if self.selected_range.end < self.selected_range.start {
            self.selection_reversed = !self.selection_reversed;
            self.selected_range = self.selected_range.end..self.selected_range.start;
        }
        cx.notify();
    }

    fn offset_from_utf16(&self, offset: usize) -> usize {
        let mut utf8_offset = 0;
        let mut utf16_count = 0;
        for ch in self.content.chars() {
            if utf16_count >= offset {
                break;
            }
            utf16_count += ch.len_utf16();
            utf8_offset += ch.len_utf8();
        }
        utf8_offset
    }

    fn offset_to_utf16(&self, offset: usize) -> usize {
        let mut utf16_offset = 0;
        let mut utf8_count = 0;
        for ch in self.content.chars() {
            if utf8_count >= offset {
                break;
            }
            utf8_count += ch.len_utf8();
            utf16_offset += ch.len_utf16();
        }
        utf16_offset
    }

    fn range_to_utf16(&self, range: &Range<usize>) -> Range<usize> {
        self.offset_to_utf16(range.start)..self.offset_to_utf16(range.end)
    }

    fn range_from_utf16(&self, range_utf16: &Range<usize>) -> Range<usize> {
        self.offset_from_utf16(range_utf16.start)..self.offset_from_utf16(range_utf16.end)
    }

    fn previous_boundary(&self, offset: usize) -> usize {
        self.content
            .grapheme_indices(true)
            .rev()
            .find_map(|(idx, _)| (idx < offset).then_some(idx))
            .unwrap_or(0)
    }

    fn next_boundary(&self, offset: usize) -> usize {
        self.content
            .grapheme_indices(true)
            .find_map(|(idx, _)| (idx > offset).then_some(idx))
            .unwrap_or(self.content.len())
    }

    fn line_starts(&self) -> Vec<usize> {
        let mut starts = vec![0];
        for (i, byte) in self.content.bytes().enumerate() {
            if byte == b'\n' {
                starts.push(i + 1);
            }
        }
        starts
    }

    fn line_len(&self, line: usize) -> usize {
        let starts = self.line_starts();
        let start = starts[line];
        let end = starts
            .get(line + 1)
            .map(|s| s - 1)
            .unwrap_or(self.content.len());
        end - start
    }

    fn line_col(&self, offset: usize) -> (usize, usize) {
        let starts = self.line_starts();
        let line = starts.partition_point(|&s| s <= offset).saturating_sub(1);
        (line, offset - starts[line])
    }

    fn move_vertically(&mut self, up: bool, cx: &mut Context<Self>) {
        let cursor = self.cursor_offset();
        let (line, col) = self.line_col(cursor);
        let starts = self.line_starts();
        let target = if up {
            line.saturating_sub(1)
        } else {
            (line + 1).min(starts.len() - 1)
        };
        if target == line {
            return;
        }
        let mut offset = starts[target] + col.min(self.line_len(target));
        while offset > 0 && !self.content.is_char_boundary(offset) {
            offset -= 1;
        }
        self.move_to(offset, cx);
    }

    fn index_for_mouse_position(&self, position: Point<Pixels>) -> usize {
        let (Some(bounds), Some(lines)) = (self.last_bounds.as_ref(), self.last_layout.as_ref())
        else {
            return self.content.len();
        };
        let Some(local) = bounds.localize(&position) else {
            return self.content.len();
        };
        if local.y < px(0.0) {
            return 0;
        }
        let line = ((local.y / self.last_line_height).floor() as usize).min(lines.len() - 1);
        let col = lines[line].closest_index_for_x(local.x);
        (self.line_starts()[line] + col).min(self.content.len())
    }

    // ------------------------------------------------------------------
    // Rendering
    // ------------------------------------------------------------------

    fn message_action_bar(
        &self,
        theme: &HermesTheme,
        content: String,
        assistant_id: String,
        cx: &mut Context<Self>,
    ) -> Div {
        div()
            .flex()
            .flex_row()
            .items_center()
            .gap_3()
            .pt_1()
            .text_xs()
            .child(
                div()
                    .id(SharedString::from(format!("msg_copy_{assistant_id}")))
                    .cursor_pointer()
                    .text_color(theme.text_dim())
                    .hover(|s| s.text_color(theme.text_primary()))
                    .on_click(move |_, _, cx| {
                        cx.write_to_clipboard(ClipboardItem::new_string(content.clone()));
                    })
                    .child("📋 Copy"),
            )
            .child(
                div()
                    .id(SharedString::from(format!("msg_regen_{assistant_id}")))
                    .cursor_pointer()
                    .text_color(theme.text_dim())
                    .hover(|s| s.text_color(theme.text_primary()))
                    .on_click(cx.listener(move |this, _, _, cx| {
                        this.regenerate(&assistant_id, cx);
                    }))
                    .child("🔄 Regenerate"),
            )
    }

    fn render_composer(&mut self, theme: &HermesTheme, cx: &mut Context<Self>) -> Div {
        let running = self.active_turn_running();
        let can_send = !self.content.trim().is_empty() && !running;
        let send_btn = SendButton::new(running, can_send);
        let has_text = !self.content.is_empty();

        div()
            .w_full()
            .p_4()
            .border_t_1()
            .border_color(theme.border_subtle())
            .child(
                div()
                    .w_full()
                    .flex()
                    .flex_col()
                    .p_3()
                    .rounded_xl()
                    .bg(theme.card_bg())
                    .border_1()
                    .border_color(theme.border_subtle())
                    .key_context("Composer")
                    .track_focus(&self.focus_handle)
                    .cursor(CursorStyle::IBeam)
                    .line_height(px(20.0))
                    .text_size(px(14.0))
                    .on_action(cx.listener(Self::on_insert_newline))
                    .on_action(cx.listener(Self::on_backspace))
                    .on_action(cx.listener(Self::on_delete))
                    .on_action(cx.listener(Self::on_left))
                    .on_action(cx.listener(Self::on_right))
                    .on_action(cx.listener(Self::on_select_left))
                    .on_action(cx.listener(Self::on_select_right))
                    .on_action(cx.listener(Self::on_home))
                    .on_action(cx.listener(Self::on_end))
                    .on_action(cx.listener(Self::on_up))
                    .on_action(cx.listener(Self::on_down))
                    .on_mouse_down(MouseButton::Left, cx.listener(Self::on_mouse_down))
                    .on_mouse_up(MouseButton::Left, cx.listener(Self::on_mouse_up))
                    .on_mouse_up_out(MouseButton::Left, cx.listener(Self::on_mouse_up))
                    .on_mouse_move(cx.listener(Self::on_mouse_move))
                    .child(
                        div()
                            .w_full()
                            .min_h(px(36.0))
                            .max_h(px(160.0))
                            .child(ComposerElement {
                                view: cx.entity(),
                            }),
                    )
                    .child(
                        div()
                            .w_full()
                            .flex()
                            .flex_row()
                            .items_center()
                            .justify_between()
                            .pt_2()
                            .border_t_1()
                            .border_color(theme.border_subtle())
                            .child(
                                div()
                                    .id("model_pill_button")
                                    .on_click(cx.listener(|this, _, _, cx| {
                                        this.shell.open_model_picker();
                                        cx.notify();
                                    }))
                                    .child(self.shell.composer.model_pill.render(theme)),
                            )
                            .child(
                                div()
                                    .flex()
                                    .flex_row()
                                    .items_center()
                                    .gap_3()
                                    .child(if has_text {
                                        div()
                                            .id("composer_clear")
                                            .cursor_pointer()
                                            .px_1()
                                            .text_xs()
                                            .text_color(theme.text_dim())
                                            .hover(|s| s.text_color(theme.text_primary()))
                                            .on_click(cx.listener(|this, _, _, cx| {
                                                this.reset_editor(cx);
                                            }))
                                            .child("×")
                                            .into_any()
                                    } else {
                                        div().into_any()
                                    })
                                    .child(self.shell.composer.token_gauge.render(theme))
                                    .child(
                                        div()
                                            .id("composer_send")
                                            .on_click(cx.listener(|this, _, _, cx| {
                                                if this.active_turn_running() {
                                                    this.cancel_turn(cx);
                                                } else {
                                                    this.submit_prompt(cx);
                                                }
                                            }))
                                            .child(send_btn.render(theme)),
                                    ),
                            ),
                    ),
            )
    }

    fn render_main_pane(&mut self, theme: &HermesTheme, cx: &mut Context<Self>) -> Div {
        let active_title = self
            .shell
            .sidebar
            .session_list
            .selected_session()
            .map(|s| s.title.clone())
            .unwrap_or_else(|| "New Session".to_string());

        let feed = self.shell.feed.render_with_bars(theme, |assistant| {
            if assistant.is_streaming {
                return None;
            }
            Some(self.message_action_bar(
                theme,
                assistant.content.clone(),
                assistant.id.clone(),
                cx,
            ))
        });

        div()
            .flex_1()
            .h_full()
            .flex()
            .flex_col()
            .bg(theme.canvas_bg())
            .child(
                div()
                    .w_full()
                    .h(px(40.0))
                    .flex()
                    .flex_row()
                    .items_center()
                    .justify_between()
                    .px_4()
                    .border_b_1()
                    .border_color(theme.border_subtle())
                    .child(
                        div()
                            .flex()
                            .flex_row()
                            .items_center()
                            .gap_3()
                            .child(
                                div()
                                    .id("sidebar_toggle")
                                    .cursor_pointer()
                                    .text_sm()
                                    .text_color(theme.text_muted())
                                    .hover(|s| s.text_color(theme.text_primary()))
                                    .on_click(cx.listener(|this, _, _, cx| {
                                        this.shell.toggle_sidebar();
                                        cx.notify();
                                    }))
                                    .child(if self.shell.sidebar_collapsed {
                                        "▶"
                                    } else {
                                        "◀"
                                    }),
                            )
                            .child(
                                div()
                                    .text_sm()
                                    .font_weight(FontWeight::SEMIBOLD)
                                    .text_color(theme.text_primary())
                                    .child(active_title),
                            ),
                    )
                    .child(
                        div()
                            .text_xs()
                            .text_color(theme.text_dim())
                            .child(self.shell.sidebar.provider_status.display_label()),
                    ),
            )
            .child(feed)
            .child(self.render_composer(theme, cx))
    }
}

impl Render for ChatView {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let theme = HermesTheme::get(cx).clone();

        // Predictable focus: opening a modal moves focus to it, closing returns
        // focus to the composer. Background activity never steals focus.
        let modal_open = self.shell.active_modal.is_open();
        if modal_open {
            if !self.last_modal_open {
                self.modal_focus_handle.focus(window);
            }
        } else if self.last_modal_open || !self.focus_handle.is_focused(window) {
            self.focus_handle.focus(window);
        }
        self.last_modal_open = modal_open;

        let on_new_chat: Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)> =
            Rc::new(cx.listener(|this, _, _, cx| this.request_new_chat(cx)));
        let weak = cx.entity().downgrade();
        let on_select_session: Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)> =
            Rc::new(move |id, _event, _window, cx| {
                weak.update(cx, |this, cx| this.switch_session(id, cx)).ok();
            });

        let mut root = div()
            .size_full()
            .flex()
            .flex_row()
            .overflow_hidden()
            .bg(theme.canvas_bg())
            .key_context("ChatView")
            .on_action(cx.listener(Self::on_submit))
            .on_action(cx.listener(Self::on_new_chat))
            .on_action(cx.listener(Self::on_toggle_palette))
            .on_action(cx.listener(Self::on_toggle_sidebar))
            .on_action(cx.listener(Self::on_open_settings))
            .on_action(cx.listener(Self::on_cancel_or_dismiss))
            .on_action(cx.listener(Self::on_copy))
            .on_action(cx.listener(Self::on_cut))
            .on_action(cx.listener(Self::on_paste))
            .on_action(cx.listener(Self::on_select_all))
            .on_key_down(cx.listener(Self::on_key_down));

        if !self.shell.sidebar_collapsed {
            root = root
                .child(self.shell.sidebar.render_with_actions(
                    &theme,
                    Some(on_new_chat),
                    Some(on_select_session),
                ))
                .child(div().w(px(1.0)).h_full().bg(theme.border_subtle()));
        }

        root = root.child(self.render_main_pane(&theme, cx));

        let modal = match &self.shell.active_modal {
            ActiveModal::None => None,
            ActiveModal::Approval(modal) => {
                Some(render_modal_container(modal.render(&theme), &theme))
            }
            ActiveModal::Palette(palette) => {
                Some(render_modal_container(palette.render(&theme), &theme))
            }
            ActiveModal::ModelPicker(picker) => {
                Some(render_modal_container(picker.render(&theme), &theme))
            }
        };
        if let Some(modal) = modal {
            root = root.child(
                div()
                    .absolute()
                    .inset_0()
                    .key_context("Modal")
                    .track_focus(&self.modal_focus_handle)
                    .on_action(cx.listener(Self::on_backspace))
                    .on_action(cx.listener(Self::on_up))
                    .on_action(cx.listener(Self::on_down))
                    .child(modal),
            );
        }

        root
    }
}

impl EntityInputHandler for ChatView {
    fn text_for_range(
        &mut self,
        range_utf16: Range<usize>,
        actual_range: &mut Option<Range<usize>>,
        _window: &mut Window,
        _cx: &mut Context<Self>,
    ) -> Option<String> {
        let range = self.range_from_utf16(&range_utf16);
        actual_range.replace(self.range_to_utf16(&range));
        Some(self.content[range].to_string())
    }

    fn selected_text_range(
        &mut self,
        _ignore_disabled_input: bool,
        _window: &mut Window,
        _cx: &mut Context<Self>,
    ) -> Option<UTF16Selection> {
        Some(UTF16Selection {
            range: self.range_to_utf16(&self.selected_range),
            reversed: self.selection_reversed,
        })
    }

    fn marked_text_range(
        &self,
        _window: &mut Window,
        _cx: &mut Context<Self>,
    ) -> Option<Range<usize>> {
        self.marked_range
            .as_ref()
            .map(|range| self.range_to_utf16(range))
    }

    fn unmark_text(&mut self, _window: &mut Window, _cx: &mut Context<Self>) {
        self.marked_range = None;
    }

    fn replace_text_in_range(
        &mut self,
        range_utf16: Option<Range<usize>>,
        new_text: &str,
        _window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        let range = range_utf16
            .as_ref()
            .map(|range_utf16| self.range_from_utf16(range_utf16))
            .or(self.marked_range.clone())
            .unwrap_or(self.selected_range.clone());

        self.content =
            self.content[0..range.start].to_owned() + new_text + &self.content[range.end..];
        self.selected_range = range.start + new_text.len()..range.start + new_text.len();
        self.marked_range.take();
        cx.notify();
    }

    fn replace_and_mark_text_in_range(
        &mut self,
        range_utf16: Option<Range<usize>>,
        new_text: &str,
        new_selected_range_utf16: Option<Range<usize>>,
        _window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        let range = range_utf16
            .as_ref()
            .map(|range_utf16| self.range_from_utf16(range_utf16))
            .or(self.marked_range.clone())
            .unwrap_or(self.selected_range.clone());

        self.content =
            self.content[0..range.start].to_owned() + new_text + &self.content[range.end..];
        if !new_text.is_empty() {
            self.marked_range = Some(range.start..range.start + new_text.len());
        } else {
            self.marked_range = None;
        }
        self.selected_range = new_selected_range_utf16
            .as_ref()
            .map(|range_utf16| self.range_from_utf16(range_utf16))
            .map(|new_range| new_range.start + range.start..new_range.end + range.end)
            .unwrap_or_else(|| range.start + new_text.len()..range.start + new_text.len());
        cx.notify();
    }

    fn bounds_for_range(
        &mut self,
        range_utf16: Range<usize>,
        bounds: Bounds<Pixels>,
        _window: &mut Window,
        _cx: &mut Context<Self>,
    ) -> Option<Bounds<Pixels>> {
        let lines = self.last_layout.as_ref()?;
        let range = self.range_from_utf16(&range_utf16);
        let (line, col) = self.line_col(range.start);
        let shaped = lines.get(line)?;
        let x = shaped.x_for_index(col.min(shaped.text.len()));
        Some(Bounds::new(
            point(
                bounds.left() + x,
                bounds.top() + self.last_line_height * line as f32,
            ),
            size(px(1.0), self.last_line_height),
        ))
    }

    fn character_index_for_point(
        &mut self,
        point: Point<Pixels>,
        _window: &mut Window,
        _cx: &mut Context<Self>,
    ) -> Option<usize> {
        let bounds = self.last_bounds?;
        let lines = self.last_layout.as_ref()?;
        let local = bounds.localize(&point)?;
        let line = ((local.y / self.last_line_height).floor() as usize).min(lines.len() - 1);
        let col = lines[line].index_for_x(local.x)?;
        let utf8_index = (self.line_starts()[line] + col).min(self.content.len());
        Some(self.offset_to_utf16(utf8_index))
    }
}

// ----------------------------------------------------------------------
// Multiline composer element (registers the platform input handler on paint)
// ----------------------------------------------------------------------

struct ComposerElement {
    view: Entity<ChatView>,
}

struct ComposerPrepaint {
    lines: Vec<ShapedLine>,
    cursor: Option<PaintQuad>,
    selections: Vec<PaintQuad>,
    line_height: Pixels,
}

impl IntoElement for ComposerElement {
    type Element = Self;

    fn into_element(self) -> Self::Element {
        self
    }
}

fn line_runs(
    line_len: usize,
    line_start: usize,
    marked: Option<&Range<usize>>,
    base: &TextRun,
) -> Vec<TextRun> {
    let Some(marked) = marked else {
        return vec![TextRun {
            len: line_len,
            ..base.clone()
        }];
    };
    let line_end = line_start + line_len;
    let start = marked.start.max(line_start);
    let end = marked.end.min(line_end);
    if start >= end {
        return vec![TextRun {
            len: line_len,
            ..base.clone()
        }];
    }
    vec![
        TextRun {
            len: start - line_start,
            ..base.clone()
        },
        TextRun {
            len: end - start,
            underline: Some(UnderlineStyle {
                color: Some(base.color),
                thickness: px(1.0),
                wavy: false,
            }),
            ..base.clone()
        },
        TextRun {
            len: line_end - end,
            ..base.clone()
        },
    ]
    .into_iter()
    .filter(|run| run.len > 0)
    .collect()
}

impl Element for ComposerElement {
    type RequestLayoutState = ();
    type PrepaintState = ComposerPrepaint;

    fn id(&self) -> Option<ElementId> {
        None
    }

    fn source_location(&self) -> Option<&'static core::panic::Location<'static>> {
        None
    }

    fn request_layout(
        &mut self,
        _id: Option<&GlobalElementId>,
        _inspector_id: Option<&gpui::InspectorElementId>,
        window: &mut Window,
        cx: &mut App,
    ) -> (LayoutId, Self::RequestLayoutState) {
        let line_count = self.view.read(cx).content.split('\n').count().clamp(1, 8);
        let mut style = Style::default();
        style.size.width = relative(1.).into();
        style.size.height = (window.line_height() * line_count as f32).into();
        (window.request_layout(style, [], cx), ())
    }

    fn prepaint(
        &mut self,
        _id: Option<&GlobalElementId>,
        _inspector_id: Option<&gpui::InspectorElementId>,
        bounds: Bounds<Pixels>,
        _request_layout: &mut Self::RequestLayoutState,
        window: &mut Window,
        cx: &mut App,
    ) -> Self::PrepaintState {
        let view = self.view.read(cx);
        let content = view.content.clone();
        let selected_range = view.selected_range.clone();
        let cursor = view.cursor_offset();
        let marked_range = view.marked_range.clone();
        let placeholder = view.shell.composer.placeholder.clone();
        let theme = HermesTheme::get(cx);
        let style = window.text_style();
        let font_size = style.font_size.to_pixels(window.rem_size());
        let line_height = window.line_height();

        let (display_text, text_color) = if content.is_empty() {
            (placeholder, theme.text_extra_dim().into())
        } else {
            (content.clone(), style.color)
        };

        let mut lines = Vec::new();
        let mut line_start = 0usize;
        for line_text in display_text.split('\n') {
            let base = TextRun {
                len: line_text.len(),
                font: style.font(),
                color: text_color,
                background_color: None,
                underline: None,
                strikethrough: None,
            };
            let runs = line_runs(line_text.len(), line_start, marked_range.as_ref(), &base);
            lines.push(window.text_system().shape_line(
                line_text.to_string().into(),
                font_size,
                &runs,
                None,
            ));
            line_start += line_text.len() + 1;
        }

        let mut selections = Vec::new();
        let mut cursor_quad = None;
        let mut start = 0usize;
        for (i, shaped) in lines.iter().enumerate() {
            let line_end = start + shaped.text.len();
            let y = bounds.top() + line_height * i as f32;
            if selected_range.is_empty() {
                if cursor >= start && cursor <= line_end {
                    let x = shaped.x_for_index(cursor - start);
                    cursor_quad = Some(fill(
                        Bounds::new(point(bounds.left() + x, y), size(px(2.0), line_height)),
                        theme.accent_blue(),
                    ));
                }
            } else {
                let sel_start = selected_range.start.max(start);
                let sel_end = selected_range.end.min(line_end);
                if sel_start < sel_end {
                    let x0 = shaped.x_for_index(sel_start - start);
                    let x1 = shaped.x_for_index(sel_end - start);
                    selections.push(fill(
                        Bounds::from_corners(
                            point(bounds.left() + x0, y),
                            point(bounds.left() + x1, y + line_height),
                        ),
                        rgba(0x7aa7ff40),
                    ));
                }
            }
            start = line_end + 1;
        }

        ComposerPrepaint {
            lines,
            cursor: cursor_quad,
            selections,
            line_height,
        }
    }

    fn paint(
        &mut self,
        _id: Option<&GlobalElementId>,
        _inspector_id: Option<&gpui::InspectorElementId>,
        bounds: Bounds<Pixels>,
        _request_layout: &mut Self::RequestLayoutState,
        prepaint: &mut Self::PrepaintState,
        window: &mut Window,
        cx: &mut App,
    ) {
        let focus_handle = self.view.read(cx).focus_handle.clone();
        window.handle_input(
            &focus_handle,
            ElementInputHandler::new(bounds, self.view.clone()),
            cx,
        );

        for selection in prepaint.selections.drain(..) {
            window.paint_quad(selection);
        }
        for (i, line) in prepaint.lines.iter().enumerate() {
            line.paint(
                point(bounds.left(), bounds.top() + prepaint.line_height * i as f32),
                prepaint.line_height,
                window,
                cx,
            )
            .unwrap();
        }
        if focus_handle.is_focused(window) {
            if let Some(cursor) = prepaint.cursor.take() {
                window.paint_quad(cursor);
            }
        }

        let lines = std::mem::take(&mut prepaint.lines);
        let line_height = prepaint.line_height;
        self.view.update(cx, |view, _cx| {
            view.last_layout = Some(lines);
            view.last_bounds = Some(bounds);
            view.last_line_height = line_height;
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn authoritative_final_replaces_nonempty_truncated_preview() {
        let mut feed = MessageFeed::new();
        let id = feed.start_assistant_streaming();
        feed.append_streaming_chunk("The quick brown f");
        feed.finish_streaming();

        apply_authoritative_final(
            &mut feed,
            Some(&id),
            "The quick brown fox jumps over the lazy dog.",
            "claude-3-7-sonnet",
        );

        assert_eq!(feed.len(), 1);
        match &feed.messages[0] {
            MessageItem::Assistant(a) => {
                assert_eq!(a.content, "The quick brown fox jumps over the lazy dog.");
                assert!(!a.is_streaming);
                assert_eq!(a.model_name, "claude-3-7-sonnet");
            }
            _ => panic!("expected assistant row"),
        }
    }

    #[test]
    fn authoritative_final_targets_only_its_turn_row() {
        let mut feed = MessageFeed::new();
        let older_id = feed.add_assistant_message("Earlier completed answer.");
        let current_id = feed.start_assistant_streaming();
        feed.append_streaming_chunk("partial");
        feed.finish_streaming();

        apply_authoritative_final(&mut feed, Some(&current_id), "Full current answer.", "m1");

        assert_eq!(feed.len(), 2);
        match &feed.messages[0] {
            MessageItem::Assistant(a) => {
                assert_eq!(a.id, older_id);
                assert_eq!(a.content, "Earlier completed answer.");
            }
            _ => panic!("expected assistant row"),
        }
        match &feed.messages[1] {
            MessageItem::Assistant(a) => {
                assert_eq!(a.id, current_id);
                assert_eq!(a.content, "Full current answer.");
                assert!(!a.is_streaming);
            }
            _ => panic!("expected assistant row"),
        }
    }

    #[test]
    fn authoritative_final_adds_row_when_preview_missing() {
        let mut feed = MessageFeed::new();
        feed.add_user_message("Question?");

        apply_authoritative_final(&mut feed, Some("missing_id"), "Recovered final.", "m1");

        assert_eq!(feed.len(), 2);
        match &feed.messages[1] {
            MessageItem::Assistant(a) => {
                assert_eq!(a.content, "Recovered final.");
                assert!(!a.is_streaming);
                assert_eq!(a.model_name, "m1");
            }
            _ => panic!("expected assistant row"),
        }
    }

    #[test]
    fn authoritative_final_without_id_appends_new_row() {
        let mut feed = MessageFeed::new();
        let orphan_id = feed.start_assistant_streaming();
        feed.append_streaming_chunk("orphan preview");
        feed.finish_streaming();

        apply_authoritative_final(&mut feed, None, "Final without correlation.", "m1");

        assert_eq!(feed.len(), 2);
        match &feed.messages[0] {
            MessageItem::Assistant(a) => {
                assert_eq!(a.id, orphan_id);
                assert_eq!(a.content, "orphan preview");
            }
            _ => panic!("expected assistant row"),
        }
        match &feed.messages[1] {
            MessageItem::Assistant(a) => {
                assert_eq!(a.content, "Final without correlation.");
                assert!(!a.is_streaming);
            }
            _ => panic!("expected assistant row"),
        }
    }

    #[test]
    fn empty_authoritative_final_is_noop() {
        let mut feed = MessageFeed::new();
        let id = feed.start_assistant_streaming();
        feed.append_streaming_chunk("preview");
        feed.finish_streaming();

        apply_authoritative_final(&mut feed, Some(&id), "", "m1");

        assert_eq!(feed.len(), 1);
        match &feed.messages[0] {
            MessageItem::Assistant(a) => assert_eq!(a.content, "preview"),
            _ => panic!("expected assistant row"),
        }
    }
}
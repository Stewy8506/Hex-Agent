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
use std::time::Duration;

use crate::ui::home::{ContinueRow, COMING_SOON_DOCK};
use crate::ui::home;

use gpui::prelude::FluentBuilder;
use gpui::*;
use tokio::runtime::{Handle, Runtime};
use unicode_segmentation::UnicodeSegmentation;

use hex_protocol::error::AcpError;
use hex_protocol::models::{
    ApprovalDecisionResponse, CloudAuthStatus, CloudUpdatePlan, PagedSessionLoad, SessionNewResponse,
    SessionPromptResponse,
};

use crate::acp_bridge::{AcpBridge, AcpEvent};
use crate::fonts;
use crate::menu::init_menus;
use crate::shortcuts::{self, init_shortcuts};
use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::composer::{context_bar, glow_card, COMPOSER_COLUMN_GAP, SESSION_PLACEHOLDER};
use crate::ui::feed::{MessageFeed, MessageItem};
use crate::ui::message::{ActionBarHandlers, AssistantMessageView};
use crate::ui::modals::account::AccountAction;
use crate::ui::modals::approval::{ToolApprovalChoice, ToolApprovalResult};
use crate::ui::modals::session_actions::{
    SessionActionModal, SessionActionOutcome, SessionSearchModal,
};
use crate::ui::modals::{render_modal_container, ActiveModal, AccountModal, ModelEntry, PaletteAction, ToolApprovalModal};
use crate::ui::model_pill::SendButton;
use crate::ui::motion;
use crate::ui::pill::{mockup_icon_button, mockup_pill, mockup_pill_chevron};
use crate::ui::session_list::SessionItem;
use crate::ui::shell::{AppShell, ShellView};
use crate::ui::sidebar::{ProviderState, SidebarHandlers};
use crate::ui::token_gauge::{format_token_number, DEFAULT_CONTEXT_LIMIT};
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

/// Register embedded brand fonts, theme, menus, global shortcuts, and
/// composer bindings. Shared by the real `main` entry point and the
/// acceptance tests.
pub fn init_app(cx: &mut App) {
    if let Err(err) = fonts::load_embedded_fonts(cx) {
        log::warn!("failed to load embedded brand fonts: {err}");
    }
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

/// Rebuild a feed from durable history, fetching canonical pages for any
/// turn whose inline preview was truncated (B28 recovery / resume).
async fn build_restored_feed(bridge: &AcpBridge, rt: &Handle, load: &PagedSessionLoad) -> MessageFeed {
    let mut feed = MessageFeed::new();
    let mut incomplete = false;
    for paged in &load.page.history {
        let prompt = match full_history_field(
            bridge, rt, &load.session_id, paged.turn_index, "prompt",
            paged.prompt_chars, &paged.turn.prompt,
        ).await {
            Some(text) => text,
            None => { incomplete = true; paged.turn.prompt.clone() }
        };
        if !prompt.is_empty() {
            feed.add_user_message(prompt);
        }
        let response = match full_history_field(
            bridge, rt, &load.session_id, paged.turn_index, "response",
            paged.response_chars, &paged.turn.response,
        ).await {
            Some(text) => text,
            None => { incomplete = true; paged.turn.response.clone() }
        };
        if !response.is_empty() {
            feed.add_assistant_message(response);
        }
    }
    if incomplete {
        feed.add_system_notice(
            "Some restored turns may be incomplete: canonical history pages could not be validated.",
        );
    }
    feed
}

/// Fetch and validate every page of one turn field; None on any inconsistency.
async fn full_history_field(
    bridge: &AcpBridge,
    rt: &Handle,
    session_id: &str,
    turn_index: usize,
    field: &str,
    total: usize,
    preview: &str,
) -> Option<String> {
    if preview.chars().count() >= total {
        return Some(preview.to_string());
    }
    let mut text = String::new();
    let mut offset = 0usize;
    loop {
        let b = bridge.clone();
        let sid = session_id.to_string();
        let fld = field.to_string();
        let page = rt
            .spawn(async move { b.history_content_page(&sid, turn_index, &fld, offset).await })
            .await
            .ok()?
            .ok()?;
        let end = offset.checked_add(page.content.chars().count())?;
        if page.total_chars != total
            || end > total
            || (end < total && end == offset)
            || page.next_offset != (end < total).then_some(end)
        {
            return None;
        }
        text.push_str(&page.content);
        if end == total {
            return Some(text);
        }
        offset = end;
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
    /// B28: one supervised reconnect loop at a time; re-armed on every loss.
    supervisor_active: bool,
    /// Phase 2 routing: the app opens on the Home start screen.
    show_home: bool,
    /// Prompt typed on Home before the first session exists; submitted once
    /// `adopt_session` creates the session.
    pending_home_prompt: Option<String>,
    /// Dock feature the user clicked that is not built yet (Coming soon).
    home_coming_soon: Option<&'static str>,
    settings_open: bool,
    home_connection_error: Option<String>,
    /// Cached `cloud/auth/status` for the sidebar footer and header device chip.
    cloud_status: Option<CloudAuthStatus>,
    /// Latest verified update plan from `cloud/update/check` (signed in only).
    cloud_update: Option<CloudUpdatePlan>,
    /// Scroll state of the bottom-anchored message list; owned here because
    /// `AppShell` derives Debug/Clone/PartialEq and `ListState` cannot.
    feed_list: ListState,
    /// Feed item count currently spliced into `feed_list`.
    feed_synced_count: usize,
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

        let mut shell = AppShell::new().with_acp_bridge(bridge.clone());
        // B35: the git branch is resolved off the UI thread; show a placeholder
        // until the background lookup completes instead of blocking startup.
        shell.sidebar.set_workspace(workspace_name, "…");
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
            supervisor_active: false,
            show_home: true,
            pending_home_prompt: None,
            home_coming_soon: None,
            settings_open: false,
            home_connection_error: None,
            cloud_status: None,
            cloud_update: None,
            feed_list: ListState::new(0, ListAlignment::Bottom, px(1000.)),
            feed_synced_count: 0,
        };
        view.start_event_pump(cx);
        view.start_backend(cx);
        view.start_git_branch_lookup(cx);
        view.refresh_cloud_status(cx);
        view
    }

    /// Fetch the local cloud account/device status for the footer + device chip.
    fn refresh_cloud_status(&mut self, cx: &mut Context<Self>) {
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        cx.spawn(async move |this, cx| {
            let res = rt.spawn(async move { bridge.cloud_auth_status().await }).await;
            this.update(cx, |view, cx| {
                if let Ok(Ok(status)) = res {
                    view.cloud_status = Some(status.clone());
                    if status.logged_in {
                        view.shell.sidebar.set_account_status(
                            status.email.clone().unwrap_or_else(|| "Account".into()),
                            "Cloud connected",
                        );
                        view.refresh_cloud_update(cx);
                    } else {
                        view.shell.sidebar.set_account_status("Account", "Local mode");
                    }
                    cx.notify();
                }
            })
            .ok();
        })
        .detach();
    }

    /// Signed-in update check: the banner only appears for verified plans;
    /// signed-out or backend-down errors simply clear it.
    fn refresh_cloud_update(&mut self, cx: &mut Context<Self>) {
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        cx.spawn(async move |this, cx| {
            let res = rt.spawn(async move { bridge.cloud_update_check().await }).await;
            this.update(cx, |view, cx| {
                view.cloud_update = match res {
                    Ok(Ok(plan)) => Some(plan),
                    _ => None,
                };
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    /// Pending approval modals (active + queued), mirroring the render badge.
    fn pending_approval_count(&self) -> usize {
        matches!(self.shell.active_modal, ActiveModal::Approval(_)) as usize
            + self
                .shell
                .modal_queue
                .iter()
                .filter(|m| matches!(m, ActiveModal::Approval(_)))
                .count()
    }

    /// Aggregate notification count driving the header bell badge.
    fn notification_count(&self) -> usize {
        self.pending_approval_count()
            + if self.update_banner_line().is_some() { 1 } else { 0 }
    }

    /// One-line banner summary for the verified update plan, if any:
    /// `(text, mandatory)`.
    fn update_banner_line(&self) -> Option<(String, bool)> {
        let plan = self.cloud_update.as_ref()?;
        if plan.apply_allowed == Some(false) {
            return Some((
                "Incompatible update set — HEX components are out of sync".to_string(),
                true,
            ));
        }
        let updates = plan.updates.as_ref()?;
        if updates.is_empty() {
            return None;
        }
        let mandatory = plan.mandatory_upgrade_required == Some(true)
            || updates.iter().any(|u| u.mandatory == Some(true));
        let names: Vec<String> = updates
            .iter()
            .map(|u| {
                format!(
                    "{} {} → {}",
                    u.component,
                    u.installed_version.as_deref().unwrap_or("?"),
                    u.target_version.as_deref().unwrap_or("?")
                )
            })
            .collect();
        Some((names.join(", "), mandatory))
    }

    /// Resolve the git branch on a blocking pool instead of the UI thread (B35).
    fn start_git_branch_lookup(&mut self, cx: &mut Context<Self>) {
        let rt = self.rt.clone();
        let ws = self.workspace.clone();
        let name = self.shell.sidebar.workspace_name.clone();
        cx.spawn(async move |this, cx| {
            let branch = rt
                .spawn_blocking(move || {
                    std::process::Command::new("git")
                        .args(["rev-parse", "--abbrev-ref", "HEAD"])
                        .current_dir(&ws)
                        .output()
                        .ok()
                        .filter(|o| o.status.success())
                        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
                        .unwrap_or_else(|| "no git".to_string())
                })
                .await
                .unwrap_or_else(|_| "no git".to_string());
            this.update(cx, |view, cx| {
                view.shell.sidebar.set_workspace(name, branch);
                cx.notify();
            })
            .ok();
        })
        .detach();
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
                        view.shell
                            .composer
                            .model_pill
                            .set_model("HEX", "not connected");
                        view.shell.feed.add_system_notice(
                            "Could not connect to the HEX runtime. Install it (install.sh) or start `hex acp`; retrying automatically.",
                        );
                        view.begin_supervision(cx);
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
                Ok(Ok(resp)) => {
                    view.adopt_session(resp, false, cx);
                    view.refresh_session_list(cx);
                }
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

    /// B28: supervise the transport after any loss. One loop at a time;
    /// exponential backoff until connected, then explicit session recovery.
    fn begin_supervision(&mut self, cx: &mut Context<Self>) {
        if self.supervisor_active {
            return;
        }
        self.supervisor_active = true;
        self.shell.sidebar.provider_status.state = ProviderState::Reconnecting;
        cx.notify();

        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        cx.spawn(async move |this, cx| {
            let b = bridge.clone();
            let reconnected = rt
                .spawn(async move {
                    b.reconnect_with_backoff(Duration::from_secs(30)).await;
                })
                .await
                .is_ok();
            if !reconnected {
                return;
            }
            this.update(cx, |view, cx| {
                view.supervisor_active = false;
                view.shell
                    .feed
                    .add_system_notice("Reconnected to the HEX runtime.");
                view.recover_session(cx);
            })
            .ok();
        })
        .detach();
    }

    /// After a reconnect the backend may be a fresh process: restore the
    /// previous session from durable history, or start a new one honestly.
    fn recover_session(&mut self, cx: &mut Context<Self>) {
        let previous = self.shell.active_session_id.clone();
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let cwd = self.workspace.to_string_lossy().to_string();
        let model = self.model_id.clone();
        cx.spawn(async move |this, cx| {
            let mut restored = None;
            if let Some(sid) = previous {
                let b = bridge.clone();
                if let Ok(Ok(load)) = rt
                    .spawn(async move { b.load_session_paged(&sid).await })
                    .await
                {
                    let feed = build_restored_feed(&bridge, &rt, &load).await;
                    restored = Some((load, feed));
                }
            }
            let fresh = if restored.is_none() {
                let b = bridge.clone();
                rt.spawn(async move { b.new_session_full(Some(&cwd), model.as_deref()).await })
                    .await
                    .ok()
                    .and_then(|r| r.ok())
            } else {
                None
            };
            this.update(cx, |view, cx| {
                match restored {
                    Some((load, feed)) => view.adopt_loaded_session(load, feed, cx),
                    None => match fresh {
                        Some(resp) => {
                            view.adopt_session(resp, true, cx);
                            view.shell.feed.add_system_notice(
                                "Started a new session; the previous conversation was not recoverable.",
                            );
                        }
                        None => {
                            view.shell.sidebar.provider_status.state = ProviderState::Offline;
                            view.shell
                                .feed
                                .add_system_notice("Reconnected, but no session could be established.");
                        }
                    },
                }
                view.refresh_session_list(cx);
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    /// Populate the sidebar with durable backend sessions (including inactive
    /// on-disk ones) so they can be resumed. Never adopts or loads transcripts.
    fn refresh_session_list(&mut self, cx: &mut Context<Self>) {
        if !self.bridge.is_connected() {
            return;
        }
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        cx.spawn(async move |this, cx| {
            let res = rt.spawn(async move { bridge.list_sessions().await }).await;
            if let Ok(Ok(items)) = res {
                this.update(cx, |view, cx| {
                    for item in items {
                        let tail: String = item
                            .session_id
                            .chars()
                            .skip(item.session_id.chars().count().saturating_sub(6))
                            .collect();
                        let fallback_title = format!("Chat …{tail}");
                        let session_title = match item.title.as_deref() {
                            Some(t) if !t.trim().is_empty() => t.to_string(),
                            _ => fallback_title,
                        };
                        if let Some(existing) = view
                            .shell
                            .sidebar
                            .session_list
                            .sessions
                            .iter_mut()
                            .find(|s| s.id == item.session_id)
                        {
                            existing.title = session_title;
                            existing.is_archived = item.archived;
                            existing.is_pinned = item.pinned;
                            existing.model_name = Some(item.model.clone());
                            continue;
                        }
                        view.shell.sidebar.add_session(
                            SessionItem::new(item.session_id.clone(), session_title)
                                .with_pinned(item.pinned)
                                .with_model(item.model.clone()),
                        );
                        let added = view
                            .shell
                            .sidebar
                            .session_list
                            .sessions
                            .iter_mut()
                            .find(|s| s.id == item.session_id);
                        if let Some(added) = added {
                            added.is_archived = item.archived;
                        }
                    }
                    cx.notify();
                })
                .ok();
            }
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
        // Cloud account lifecycle pushes never belong to a session.
        if let AcpEvent::CloudAccount(cloud_event) = &event {
            let notice = match cloud_event.kind.as_str() {
                "account_logged_in" => format!(
                    "Signed in to HEX cloud{}",
                    cloud_event
                        .email
                        .as_deref()
                        .map(|e| format!(" as {e}"))
                        .unwrap_or_default(),
                ),
                "account_logged_out" => "Signed out of HEX cloud on this device.".to_string(),
                "account_registered" => "HEX cloud account created.".to_string(),
                "device_revoked" => format!(
                    "Device revoked: {}",
                    cloud_event.device_record_id.as_deref().unwrap_or("unknown")
                ),
                other => format!("Cloud event: {other}"),
            };
            self.shell.feed.add_system_notice(notice);
            self.refresh_cloud_status(cx);
            cx.notify();
            return;
        }

        // Turn usage feeds the context gauge on the active session only; other
        // sessions keep their own state and are not rendered right now.
        if let AcpEvent::TurnUsage {
            session_id,
            input_tokens,
            context_limit,
            ..
        } = &event
        {
            if self.shell.active_session_id.as_deref() == Some(session_id.as_str()) {
                let limit = context_limit.unwrap_or(DEFAULT_CONTEXT_LIMIT as u64);
                self.shell
                    .composer
                    .token_gauge
                    .set_tokens(*input_tokens as usize, limit as usize);
            }
            cx.notify();
            return;
        }

        let session_id = match &event {
            AcpEvent::TokenChunk { session_id, .. }
            | AcpEvent::TurnComplete { session_id }
            | AcpEvent::ToolCall { session_id, .. }
            | AcpEvent::ToolResult { session_id, .. }
            | AcpEvent::ApprovalRequest { session_id, .. } => Some(session_id.clone()),
            _ => None,
        };

        let Some(sid) = session_id else {
            if let AcpEvent::Disconnected { reason } = &event {
                self.shell.feed.add_system_notice(format!(
                    "Connection to the HEX runtime was lost ({reason}); reconnecting…"
                ));
                self.shell.handle_acp_event(event);
                self.begin_supervision(cx);
                cx.notify();
                return;
            }
            self.shell.handle_acp_event(event);
            self.sync_transport_state();
            cx.notify();
            return;
        };

        // Approvals surface as a modal regardless of which session is active.
        if let AcpEvent::ApprovalRequest {
            approval_id,
            session_id,
            tool_name,
            risk_level,
            command,
            description,
            ..
        } = event
        {
            let mut modal = ToolApprovalModal::new(approval_id, tool_name, command, risk_level);
            modal.session_id = Some(session_id.clone());
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
        self.shell.sidebar.provider_status.state = if self.supervisor_active {
            ProviderState::Reconnecting
        } else if !self.bridge.is_connected() {
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

    fn adopt_session(
        &mut self,
        resp: SessionNewResponse,
        opens_view: bool,
        cx: &mut Context<Self>,
    ) {
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
        // Phase 2: a prompt submitted on Home opens the session it created, and
        // an explicit new chat opens the blank session view. The startup
        // pre-warm keeps the user on Home until they submit.
        self.home_connection_error = None;
        let home_prompt = self.pending_home_prompt.take();
        if let Some(text) = home_prompt {
            self.show_home = false;
            self.submit_text(text, cx);
        } else if opens_view {
            self.show_home = false;
        }
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

    /// Adopt a session restored from durable backend history (B28 / resume).
    fn adopt_loaded_session(
        &mut self,
        load: PagedSessionLoad,
        feed: MessageFeed,
        cx: &mut Context<Self>,
    ) {
        self.save_active_session();
        let id = load.session_id.clone();
        self.running_turns.remove(&id);
        self.turn_assistants.remove(&id);
        self.shell.feed = feed;
        self.reset_editor(cx);
        let title = self
            .shell
            .feed
            .messages
            .iter()
            .find_map(|m| match m {
                MessageItem::User(u) => Some(u.content.clone()),
                _ => None,
            })
            .unwrap_or_else(|| "Resumed Chat".to_string());
        let title: String = title.chars().take(48).collect();
        self.shell
            .sidebar
            .add_session(SessionItem::new(id.clone(), title).with_model(load.model.clone()));
        self.shell.select_session(id);
        self.current_model = load.model.clone();
        self.model_id = Some(load.model.clone());
        self.shell
            .composer
            .model_pill
            .set_model(self.current_provider.clone(), load.model.clone());
        self.shell.sidebar.provider_status.model_name = load.model;
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
        if let Some(state) = self.sessions.remove(&id) {
            self.save_active_session();
            self.shell.feed = state.feed;
            self.set_editor_content(state.draft, cx);
            self.shell.select_session(id);
            self.sync_transport_state();
            cx.notify();
            return;
        }
        // Never hydrated in this process: resume from durable backend history.
        if !self.bridge.is_connected() {
            self.shell.feed.add_system_notice(
                "Not connected to the HEX runtime — cannot resume that session.",
            );
            cx.notify();
            return;
        }
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let sid = id.clone();
        cx.spawn(async move |this, cx| {
            let b = bridge.clone();
            let loaded = rt.spawn(async move { b.load_session_paged(&sid).await }).await;
            let restored = match loaded {
                Ok(Ok(load)) => {
                    let feed = build_restored_feed(&bridge, &rt, &load).await;
                    Some((load, feed))
                }
                _ => None,
            };
            this.update(cx, |view, cx| match restored {
                Some((load, feed)) => view.adopt_loaded_session(load, feed, cx),
                None => {
                    view.shell.feed.add_system_notice(format!(
                        "Could not resume session {id}: durable history was unavailable."
                    ));
                    cx.notify();
                }
            })
            .ok();
        })
        .detach();
    }

    /// Phase 2: full Home / start screen — h60 top bar (panel toggle, search,
    /// bell, settings), centered column with the rising ASCII logo and glowing
    /// prompt card, the Continue list of recent sessions (running dots, Review
    /// chips, compact times) and the bottom feature dock. Submitting a prompt
    /// requests a new session (adopted on creation) and opens it; a dock item
    /// that is not built yet surfaces its Coming soon notice.
    fn render_home(&mut self, theme: &HermesTheme, cx: &mut Context<Self>) -> Div {
        // Continue list: the five most recent real sessions, with honest
        // running (live turn) and Review (pending approval) state, synced
        // by sync_sidebar_live_state each frame.
        let now = chrono::Utc::now();
        let workspace = self.shell.sidebar.workspace_name.clone();
        let pending_sessions = self.shell.sidebar.review_sessions.clone();
        let rows: Vec<ContinueRow> = self
            .shell
            .sidebar
            .session_list
            .sessions
            .iter()
            .filter(|s| !s.is_archived)
            .take(5)
            .map(|s| ContinueRow {
                id: s.id.clone(),
                title: s.title.clone(),
                relative_time: crate::ui::session_list::format_compact_relative_time(
                    s.updated_at, now,
                ),
                location_chip: "This Mac",
                project: workspace.clone(),
                running: self.running_turns.contains(&s.id),
                pending_review: pending_sessions.contains(&s.id),
            })
            .collect();

        let home_coming_soon = self.home_coming_soon;

        // Top bar: h60, padding 0 20px; panel toggle left; right gap 4 =
        // search, bell (+ pulsing accent dot when needs-you > 0), settings.
        let sidebar_toggle = div()
            .id("home_sidebar_toggle")
            .on_click(cx.listener(|this, _, _, cx| {
                this.shell.toggle_sidebar();
                cx.notify();
            }))
            .child(mockup_icon_button(
                theme,
                "sidebar",
                home::TOP_BAR_ICON_SIZE,
                theme.mockup_text_muted,
                None,
            ));
        let search_button = div()
            .id("home_search_button")
            .on_click(cx.listener(|this, _, window, cx| {
                this.on_toggle_palette(&shortcuts::ToggleCommandPalette, window, cx);
            }))
            .child(mockup_icon_button(
                theme,
                "search",
                home::TOP_BAR_ICON_SIZE,
                theme.mockup_text_muted,
                None,
            ));
        let bell_button = div()
            .id("home_bell_button")
            .on_click(cx.listener(|this, _, _, cx| {
                this.open_first_pending_approval(cx);
            }))
            .child(mockup_icon_button(
                theme,
                "bell",
                home::TOP_BAR_ICON_SIZE,
                theme.mockup_text_muted,
                None,
            ));
        let settings_button = div()
            .id("home_settings_button")
            .on_click(cx.listener(|this, _, window, cx| {
                this.on_open_settings(&shortcuts::OpenSettings, window, cx);
            }))
            .child(mockup_icon_button(
                theme,
                "settings",
                home::TOP_BAR_ICON_SIZE,
                theme.mockup_text_muted,
                None,
            ));
        let bell_dot = if self.shell.sidebar.pending_approvals > 0 {
            let dot = div()
                .absolute()
                .top(px(6.0))
                .right(px(7.0))
                .size(px(home::CONTINUE_DOT_SIZE))
                .rounded_full()
                .bg(theme.accent_blue);
            if motion::reduced_motion() {
                dot.into_any_element()
            } else {
                dot.with_animation(
                    "home_bell_pl",
                    Animation::new(motion::PULSE_PERIOD).repeat(),
                    |el, phase| {
                        let elapsed = std::time::Duration::from_secs_f32(
                            phase * motion::PULSE_PERIOD.as_secs_f32(),
                        );
                        el.opacity(motion::pulse_opacity(elapsed))
                    },
                )
                .into_any_element()
            }
        } else {
            div().into_any_element()
        };

        // Provider chip: live transport state from the ACP bridge, not a
        // mockup prop. Dot color + "HEX · Connected" style label.
        let provider_status = &self.shell.sidebar.provider_status;
        let provider_chip = div()
            .flex()
            .flex_row()
            .items_center()
            .gap(px(6.0))
            .mr(px(8.0))
            .child(
                div()
                    .size(px(home::CONTINUE_DOT_SIZE))
                    .rounded_full()
                    .bg(provider_status.state.dot_color(theme)),
            )
            .child(
                div()
                    .text_size(px(12.0))
                    .text_color(theme.mockup_text_muted)
                    .whitespace_nowrap()
                    .child(format!(
                        "{} · {}",
                        provider_status.provider_name,
                        provider_status.state.status_label()
                    )),
            );

        let top_bar = div()
            .w_full()
            .h(px(home::TOP_BAR_HEIGHT))
            .flex()
            .flex_row()
            .items_center()
            .justify_between()
            .px(px(home::TOP_BAR_PADDING_X))
            .child(sidebar_toggle)
            .child(
                div()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap(px(4.0))
                    .child(provider_chip)
                    .child(search_button)
                    .child(
                        div()
                            .relative()
                            .child(bell_button)
                            .child(bell_dot),
                    )
                    .child(settings_button),
            );

        // Center column: flex-1, centered, padding-bottom 96px; the composer
        // column stays w640 while the Continue block is w600.
        let center = div()
            .flex_1()
            .w_full()
            .flex()
            .flex_col()
            .items_center()
            .justify_center()
            .pb(px(home::CENTER_PADDING_BOTTOM))
            .child(
                div()
                    .w(px(640.0))
                    .max_w_full()
                    .flex()
                    .flex_col()
                    .items_center()
                    .child(
                        // ASCII logo with rise entrance
                        crate::ui::home::render_logo(theme),
                    )
                    .child(
                        // Pulsating glowing prompt card wrapping the composer
                        // body (mode/model pills + attach/send) re-rendered here
                        // without the session-only context bar.
                        self.render_home_composer(theme, cx),
                    )
                    .when_some(
                        self.home_connection_error.clone(),
                        |c, msg| {
                            c.child(
                                div()
                                    .mt(px(12.0))
                                    .px(px(14.0))
                                    .py(px(8.0))
                                    .rounded(px(10.0))
                                    .bg(theme.mockup_surface)
                                    .border_1()
                                    .border_color(theme.mockup_border)
                                    .text_sm()
                                    .text_color(theme.mockup_text_dim)
                                    .child(SharedString::from(msg)),
                            )
                        },
                    ),
            )
            .child(
                // Continue list: w600, margin-top 48, `.sl` header padding
                // 0 14px 4px 33px.
                div()
                    .w(px(home::CONTINUE_WIDTH))
                    .max_w_full()
                    .mt(px(home::CONTINUE_MARGIN_TOP))
                    .flex()
                    .flex_col()
                    .gap(px(2.0))
                    .when(!rows.is_empty(), |c| {
                        c.child(
                            div()
                                .text_size(px(crate::theme::MockupTypeScale::CONTINUE_HEADER_SIZE))
                                .text_color(theme.mockup_text_dim)
                                .pl(px(home::CONTINUE_HEADER_PAD_LEFT))
                                .pr(px(14.0))
                                .pb(px(home::CONTINUE_HEADER_PAD_BOTTOM))
                                .child("Continue"),
                        )
                    })
                    .children(rows.iter().map(|row| {
                        let id = row.id.clone();
                        crate::ui::home::render_continue_row(theme, row, {
                            let this = cx.entity().downgrade();
                            move |_, _, cx| {
                                let _ = this.update(cx, |view, cx| {
                                    view.show_home = false;
                                    view.switch_session(id.clone(), cx);
                                    cx.notify();
                                });
                            }
                        })
                    })),
            );

        // Bottom feature dock: absolute, bottom 28, centered; container
        // bg #191919, border #232323, radius 16, padding 5, gap 2.
        let dock = div()
            .absolute()
            .left_0()
            .right_0()
            .bottom(px(home::DOCK_BOTTOM))
            .flex()
            .justify_center()
            .child(
                div()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap(px(home::DOCK_GAP))
                    .bg(theme.mockup_dock)
                    .border_1()
                    .border_color(theme.mockup_dock_border)
                    .rounded(px(home::DOCK_RADIUS))
                    .p(px(home::DOCK_PADDING))
                    .children(COMING_SOON_DOCK.iter().map(|(label, glyph)| {
                        let this = cx.entity().downgrade();
                        let name: &'static str = label;
                        crate::ui::home::render_dock_item(theme, label, label, glyph)
                            .on_click(move |_, _, cx| {
                                let _ = this.update(cx, |view, cx| {
                                    view.home_coming_soon = Some(name);
                                    cx.notify();
                                });
                            })
                    })),
            );

        div()
            .size_full()
            .flex()
            .flex_col()
            .font_family(theme.font_sans()[0].clone())
            .bg(theme.mockup_canvas)
            .child(top_bar)
            .child(center)
            .child(dock)
            .when_some(home_coming_soon, |c, label| {
                c.child(
                    div()
                        .absolute()
                        .bottom(px(home::NOTICE_BOTTOM))
                        .left_0()
                        .right_0()
                        .flex()
                        .justify_center()
                        .child(
                            div()
                                .bg(theme.mockup_surface)
                                .border_1()
                                .border_color(theme.mockup_border)
                                .rounded(px(10.0))
                                .px(px(14.0))
                                .py(px(8.0))
                                .text_sm()
                                .text_color(theme.mockup_text_dim)
                                .child(SharedString::from(format!(
                                    "{} — coming soon",
                                    label
                                ))),
                        ),
                )
            })
    }

    /// Home prompt card: the pulsating glow composer with pills and buttons.
    /// Delegates to the shared composer in its Home variant so Home and
    /// Session stay visually identical; submit routes into the session (see
    /// adopt_session).
    fn render_home_composer(&mut self, theme: &HermesTheme, cx: &mut Context<Self>) -> Div {
        self.render_composer(theme, cx, ComposerVariant::Home)
    }

    fn request_new_chat(&mut self, cx: &mut Context<Self>) {
        // Phase 2: New chat returns to the Home start screen. A prompt already
        // queued from the Home composer (set in submit_text) must survive this
        // reset so adopt_session can submit it once the session exists.
        self.show_home = true;
        self.home_connection_error = None;
        if !self.bridge.is_connected() {
            let msg = "Not connected to the HEX runtime — cannot start a new chat.".to_string();
            // Surface on Home itself, where the user actually is.
            self.home_connection_error = Some(msg.clone());
            self.shell.feed.add_system_notice(msg);
            cx.notify();
            return;
        }
        // start_backend pre-warms a session at launch; while it is still
        // untouched, the first Home prompt must adopt it rather than orphan
        // it in the sidebar as an empty "New Chat" entry.
        if self.pending_home_prompt.is_some()
            && self.shell.feed.is_empty()
            && !self.active_turn_running()
            && self
                .shell
                .sidebar
                .session_list
                .selected_session()
                .map(|s| s.title == "New Chat")
                .unwrap_or(false)
        {
            let text = self.pending_home_prompt.take().unwrap();
            self.show_home = false;
            self.submit_text(text, cx);
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
                Ok(Ok(resp)) => view.adopt_session(resp, true, cx),
                Ok(Err(err)) => {
                    view.home_connection_error = Some(format!("Could not create session: {err}"));
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
        if self.show_home {
            self.pending_home_prompt = Some(text);
            self.request_new_chat(cx);
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
                // B35: a late decision against an already expired/canceled/resolved
                // approval is ignored, not applied — say so precisely instead of
                // prompting a futile retry.
                self.shell
                    .feed
                    .add_system_notice(late_approval_notice(&resp.status, &resp.approval_id));
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
            PaletteAction::SwitchModel => self.request_model_picker(cx),
            PaletteAction::ToggleSidebar => {
                self.shell.toggle_sidebar();
                cx.notify();
            }
            PaletteAction::ClearHistory => {
                self.shell.feed.clear();
                cx.notify();
            }
            PaletteAction::OpenSettings => {
                self.settings_open = !self.settings_open;
                cx.notify();
            }
            PaletteAction::ExportTranscript => self.export_transcript(cx),
            PaletteAction::Custom(label) => {
                self.shell.feed.add_system_notice(format!(
                    "{label} is not available in the desktop yet."
                ));
                cx.notify();
            }
        }
    }

    /// Writes the active conversation to `<workspace>/.hex/exports/` as
    /// Markdown, mirroring the on-screen feed (user bubbles, assistant prose
    /// with tool-call summaries, system notices).
    fn export_transcript(&mut self, cx: &mut Context<Self>) {
        if self.shell.feed.is_empty() {
            self.shell
                .feed
                .add_system_notice("Nothing to export yet — the transcript is empty.");
            cx.notify();
            return;
        }
        let mut md = String::from("# HEX session transcript\n\n");
        for item in &self.shell.feed.messages {
            match item {
                MessageItem::User(card) => {
                    md.push_str("## You\n\n");
                    md.push_str(card.content.trim());
                    md.push_str("\n\n");
                }
                MessageItem::Assistant(view) => {
                    md.push_str("## HEX");
                    if !view.model_name.is_empty() {
                        md.push_str(&format!(" ({})", view.model_name));
                    }
                    md.push_str("\n\n");
                    md.push_str(view.content.trim());
                    md.push_str("\n\n");
                    for call in &view.tool_calls {
                        let outcome = match call.status {
                            crate::ui::tool_card::ToolStatus::Running => "running",
                            crate::ui::tool_card::ToolStatus::Success => "ok",
                            crate::ui::tool_card::ToolStatus::Failed => "failed",
                        };
                        md.push_str(&format!("- Tool `{}` — {}\n", call.tool_name, outcome));
                    }
                    if !view.tool_calls.is_empty() {
                        md.push('\n');
                    }
                }
                MessageItem::SystemNotice(notice) => {
                    md.push_str(&format!("> {notice}\n\n"));
                }
            }
        }
        let exports_dir = self.workspace.join(".hex").join("exports");
        let stamp = chrono::Utc::now().format("%Y%m%d-%H%M%S");
        let session = self
            .shell
            .active_session_id
            .clone()
            .unwrap_or_else(|| "session".to_string());
        let path = exports_dir.join(format!("{session}-{stamp}.md"));
        match std::fs::create_dir_all(&exports_dir).and_then(|_| std::fs::write(&path, md)) {
            Ok(()) => {
                self.shell
                    .feed
                    .add_system_notice(format!("Transcript exported to {}", path.display()));
            }
            Err(e) => {
                self.shell
                    .feed
                    .add_system_notice(format!("Export failed: {e}"));
            }
        }
        cx.notify();
    }

    /// B32: open the picker over the live backend catalog, never a hardcoded list.
    fn request_model_picker(&mut self, cx: &mut Context<Self>) {
        if !self.bridge.is_connected() {
            self.shell.feed.add_system_notice(
                "Not connected to the HEX runtime — cannot load the model catalog.",
            );
            cx.notify();
            return;
        }
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let sid = self
            .shell
            .active_session_id
            .clone()
            .unwrap_or_else(|| "default".to_string());
        cx.spawn(async move |this, cx| {
            let res = rt.spawn(async move { bridge.list_models(&sid).await }).await;
            this.update(cx, |view, cx| {
                match res {
                    Ok(Ok(catalog)) => {
                        if let Some(err) = &catalog.error {
                            view.shell.feed.add_system_notice(format!(
                                "Some providers could not be queried: {err}"
                            ));
                        }
                        if catalog.models.is_empty() {
                            view.shell
                                .feed
                                .add_system_notice("The backend reported no available models.");
                        } else {
                            let entries =
                                catalog.models.iter().map(ModelEntry::from_catalog).collect();
                            view.shell.open_model_picker(entries);
                        }
                    }
                    Ok(Err(err)) => view
                        .shell
                        .feed
                        .add_system_notice(format!("Could not load the model catalog: {err}")),
                    Err(err) => view
                        .shell
                        .feed
                        .add_system_notice(format!("Model catalog task aborted: {err}")),
                }
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    // ------------------------------------------------------------------
    // Cloud account (cloud/* ACP surface)
    // ------------------------------------------------------------------

    /// Open the account modal over cached status, then refresh live status
    /// and, when signed in, the device roster.
    fn open_account_modal(&mut self, cx: &mut Context<Self>) {
        self.shell.active_modal = ActiveModal::Account(AccountModal::new(self.cloud_status.clone()));
        cx.notify();
        self.refresh_account_modal_data(cx);
    }

    /// Refresh the open account modal: live status plus devices when signed in.
    fn refresh_account_modal_data(&mut self, cx: &mut Context<Self>) {
        if let ActiveModal::Account(modal) = &mut self.shell.active_modal {
            modal.busy = true;
            modal.error = None;
        } else {
            return;
        }
        let rt = self.rt.clone();
        let bridge_status = self.bridge.clone();
        let bridge_devices = self.bridge.clone();
        cx.spawn(async move |this, cx| {
            let status_res = rt
                .spawn(async move { bridge_status.cloud_auth_status().await })
                .await;
            this.update(cx, |view, cx| {
                if let ActiveModal::Account(modal) = &mut view.shell.active_modal {
                    match status_res {
                        Ok(Ok(status)) => {
                            modal.status = Some(status.clone());
                            view.cloud_status = Some(status);
                        }
                        Ok(Err(err)) => modal.error = Some(format!("{err}")),
                        Err(err) => modal.error = Some(format!("Account status task aborted: {err}")),
                    }
                } else {
                    return;
                }
                cx.notify();
            })
            .ok();

            // Devices are only fetched when the fresh status says signed in.
            let signed_in = this
                .read_with(cx, |view, _| {
                    view.cloud_status.as_ref().map(|s| s.logged_in).unwrap_or(false)
                })
                .ok()
                .unwrap_or(false);
            if !signed_in {
                this.update(cx, |view, cx| {
                    if let ActiveModal::Account(modal) = &mut view.shell.active_modal {
                        modal.busy = false;
                    }
                    cx.notify();
                })
                .ok();
                return;
            }
            let res = rt
                .spawn(async move { bridge_devices.cloud_devices_list(false).await })
                .await;
            this.update(cx, |view, cx| {
                if let ActiveModal::Account(modal) = &mut view.shell.active_modal {
                    match res {
                        Ok(Ok(devices)) => modal.set_devices(devices),
                        Ok(Err(err)) => modal.error = Some(format!("{err}")),
                        Err(err) => modal.error = Some(format!("Device list task aborted: {err}")),
                    }
                    modal.busy = false;
                }
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    /// Dispatch a modal-requested account action to the ACP backend.
    fn execute_account_action(&mut self, action: AccountAction, cx: &mut Context<Self>) {
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        match action {
            AccountAction::SubmitLogin { email, password } => {
                if let ActiveModal::Account(modal) = &mut self.shell.active_modal {
                    modal.busy = true;
                    modal.error = None;
                }
                cx.spawn(async move |this, cx| {
                    let res = rt
                        .spawn(async move { bridge.cloud_auth_login(&email, &password).await })
                        .await;
                    this.update(cx, |view, cx| {
                        let mut refresh_modal = false;
                        if let ActiveModal::Account(modal) = &mut view.shell.active_modal {
                            match res {
                                Ok(Ok(status)) => {
                                    view.cloud_status = Some(status);
                                    modal.mode = crate::ui::modals::account::AccountMode::Devices;
                                    modal.busy = false;
                                    modal.error = None;
                                    modal.notice = Some("Signed in.".to_string());
                                    refresh_modal = true;
                                }
                                Ok(Err(err)) => {
                                    modal.busy = false;
                                    modal.error = Some(format!("{err}"));
                                }
                                Err(err) => {
                                    modal.busy = false;
                                    modal.error = Some(format!("Sign-in task aborted: {err}"));
                                }
                            }
                        }
                        view.refresh_cloud_status(cx);
                        if refresh_modal {
                            view.refresh_account_modal_data(cx);
                        }
                        cx.notify();
                    })
                    .ok();
                })
                .detach();
            }
            AccountAction::SubmitRegister { email, password } => {
                if let ActiveModal::Account(modal) = &mut self.shell.active_modal {
                    modal.busy = true;
                    modal.error = None;
                }
                cx.spawn(async move |this, cx| {
                    let res = rt
                        .spawn(async move { bridge.cloud_auth_register(&email, &password).await })
                        .await;
                    this.update(cx, |view, cx| {
                        if let ActiveModal::Account(modal) = &mut view.shell.active_modal {
                            modal.busy = false;
                            match res {
                                Ok(Ok(_)) => {
                                    modal.mode = crate::ui::modals::account::AccountMode::Login;
                                    modal.notice =
                                        Some("Account created — sign in to continue.".to_string());
                                }
                                Ok(Err(err)) => modal.error = Some(format!("{err}")),
                                Err(err) => {
                                    modal.error = Some(format!("Registration task aborted: {err}"))
                                }
                            }
                        }
                        cx.notify();
                    })
                    .ok();
                })
                .detach();
            }
            AccountAction::Logout => {
                cx.spawn(async move |this, cx| {
                    let res = rt.spawn(async move { bridge.cloud_auth_logout().await }).await;
                    this.update(cx, |view, cx| {
                        if let ActiveModal::Account(modal) = &mut view.shell.active_modal {
                            match res {
                                Ok(Ok(status)) => {
                                    view.cloud_status = Some(status);
                                    modal.mode = crate::ui::modals::account::AccountMode::Login;
                                    modal.set_devices(Vec::new());
                                    modal.notice = Some("Signed out.".to_string());
                                }
                                Ok(Err(err)) => modal.error = Some(format!("{err}")),
                                Err(err) => {
                                    modal.error = Some(format!("Sign-out task aborted: {err}"))
                                }
                            }
                        }
                        view.refresh_cloud_status(cx);
                        cx.notify();
                    })
                    .ok();
                })
                .detach();
            }
            AccountAction::RevokeDevice(device_record_id) => {
                if let ActiveModal::Account(modal) = &mut self.shell.active_modal {
                    modal.busy = true;
                    modal.error = None;
                }
                cx.spawn(async move |this, cx| {
                    let res = rt
                        .spawn(async move { bridge.cloud_devices_revoke(&device_record_id).await })
                        .await;
                    this.update(cx, |view, cx| {
                        let mut refresh_modal = false;
                        if let ActiveModal::Account(modal) = &mut view.shell.active_modal {
                            modal.busy = false;
                            match res {
                                Ok(Ok(result)) => {
                                    modal.notice =
                                        Some(format!("Revoked {}.", result.device_record_id));
                                    refresh_modal = true;
                                }
                                Ok(Err(err)) => modal.error = Some(format!("{err}")),
                                Err(err) => {
                                    modal.error = Some(format!("Revoke task aborted: {err}"))
                                }
                            }
                        }
                        view.refresh_cloud_status(cx);
                        if refresh_modal {
                            view.refresh_account_modal_data(cx);
                        }
                        cx.notify();
                    })
                    .ok();
                })
                .detach();
            }
        }
    }

    /// B32: apply a model choice only once the backend confirms the switch.
    fn apply_model_choice(&mut self, model_id: String, cx: &mut Context<Self>) {
        let Some(sid) = self.shell.active_session_id.clone() else {
            self.shell
                .feed
                .add_system_notice("No active session — cannot switch model.");
            cx.notify();
            return;
        };
        if !self.bridge.is_connected() {
            self.shell.feed.add_system_notice(
                "Not connected to the HEX runtime — cannot switch model.",
            );
            cx.notify();
            return;
        }
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let spawn_sid = sid.clone();
        cx.spawn(async move |this, cx| {
            let res = rt
                .spawn(async move { bridge.set_session_model(&spawn_sid, &model_id).await })
                .await;
            this.update(cx, |view, cx| {
                match res {
                    Ok(Ok(resp)) if resp.is_error != Some(true) => {
                        if let Some(confirmed) = resp.model {
                            view.model_id = Some(confirmed.clone());
                            view.current_model = confirmed.clone();
                            if let Some(prov) = resp.provider {
                                view.current_provider = prov;
                            }
                            view.shell.composer.model_pill.set_model(
                                view.current_provider.clone(),
                                confirmed.clone(),
                            );
                            view.shell.sidebar.provider_status.provider_name =
                                view.current_provider.clone();
                            view.shell.sidebar.provider_status.model_name = confirmed.clone();
                            if let Some(item) = view
                                .shell
                                .sidebar
                                .session_list
                                .sessions
                                .iter_mut()
                                .find(|s| s.id == sid)
                            {
                                item.model_name = Some(confirmed);
                            }
                        } else {
                            view.shell.feed.add_system_notice(format!(
                                "Model switch was not confirmed: {}",
                                resp.output
                            ));
                        }
                    }
                    Ok(Ok(resp)) => view
                        .shell
                        .feed
                        .add_system_notice(format!("Model switch failed: {}", resp.output)),
                    Ok(Err(err)) => view
                        .shell
                        .feed
                        .add_system_notice(format!("Model switch failed: {err}")),
                    Err(err) => view
                        .shell
                        .feed
                        .add_system_notice(format!("Model switch task aborted: {err}")),
                }
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    /// Route a key to the open modal. Returns true when the modal consumed it.
    fn handle_modal_key(&mut self, key: &str, cx: &mut Context<Self>) -> bool {
        // The settings card is static: consume every key while it is open so
        // nothing reaches the composer underneath, and close on Esc.
        if self.settings_open {
            if key == "escape" {
                self.close_settings(cx);
            }
            return true;
        }
        // Live filtering needs `&mut self` (SessionListView::filter_sessions),
        // so resolve the results before the match takes a borrow on the modal.
        let search_results = match &self.shell.active_modal {
            ActiveModal::SessionSearch(modal) => {
                let state = modal.clone();
                Some(self.search_results(&state))
            }
            _ => None,
        };
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
            ActiveModal::Account(modal) => {
                if key == "escape" {
                    self.shell.dismiss_modal();
                    cx.notify();
                    return true;
                }
                if modal.busy {
                    return true;
                }
                if key == "backspace" {
                    modal.backspace();
                    cx.notify();
                    return true;
                }
                if let Some(action) = modal.handle_key(key) {
                    self.execute_account_action(action, cx);
                    return true;
                }
                // Consume navigation keys so they never reach the composer.
                matches!(key, "tab" | "shift-tab" | "up" | "arrowup" | "down" | "arrowdown")
            }
            ActiveModal::SessionAction(modal) => {
                if key == "escape" {
                    self.shell.dismiss_modal();
                    cx.notify();
                    return true;
                }
                if modal.busy {
                    return true;
                }
                if key == "backspace" {
                    modal.backspace();
                    cx.notify();
                    return true;
                }
                if let Some(outcome) = modal.handle_key(key) {
                    self.execute_session_action(outcome, cx);
                    return true;
                }
                matches!(key, "up" | "arrowup" | "down" | "arrowdown")
            }
            ActiveModal::SessionSearch(modal) => {
                if key == "escape" {
                    self.shell.dismiss_modal();
                    cx.notify();
                    return true;
                }
                let results = search_results
                    .as_ref()
                    .expect("search results precomputed for SessionSearch");
                let count = results.len();
                match key {
                    "up" | "arrowup" => {
                        modal.move_selection(-1, count);
                        cx.notify();
                        true
                    }
                    "down" | "arrowdown" => {
                        modal.move_selection(1, count);
                        cx.notify();
                        true
                    }
                    "enter" => {
                        if let Some(session) =
                            results.get(modal.selected.min(count.saturating_sub(1)))
                        {
                            let id = session.id.clone();
                            self.shell.dismiss_modal();
                            self.switch_session(id, cx);
                        }
                        true
                    }
                    _ => false,
                }
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
        self.settings_open = !self.settings_open;
        cx.notify();
    }

    /// Shared settings-dismissal path for the Esc shortcut and the modal
    /// backdrop, so the panel can always be closed.
    fn close_settings(&mut self, cx: &mut Context<Self>) {
        if self.settings_open {
            self.settings_open = false;
            cx.notify();
        }
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
            ActiveModal::Account(_) => {
                let key = if keystroke.key == "tab" && keystroke.modifiers.shift {
                    "shift-tab"
                } else {
                    keystroke.key.as_str()
                };
                if !keystroke.modifiers.platform && !keystroke.modifiers.control {
                    let typed = keystroke.key_char.clone().or_else(|| {
                        (keystroke.key.chars().count() == 1).then(|| keystroke.key.clone())
                    });
                    if let Some(ch) = typed {
                        if let ActiveModal::Account(modal) = &mut self.shell.active_modal {
                            modal.push_char(&ch);
                            cx.notify();
                            cx.stop_propagation();
                        }
                        return;
                    }
                }
                if self.handle_modal_key(key, cx) {
                    cx.stop_propagation();
                }
            }
            ActiveModal::SessionAction(_) => {
                if !keystroke.modifiers.platform && !keystroke.modifiers.control {
                    let typed = keystroke.key_char.clone().or_else(|| {
                        (keystroke.key.chars().count() == 1).then(|| keystroke.key.clone())
                    });
                    if let Some(ch) = typed {
                        if let ActiveModal::SessionAction(modal) = &mut self.shell.active_modal {
                            modal.push_char(&ch);
                            cx.notify();
                            cx.stop_propagation();
                        }
                        return;
                    }
                }
                let key = if keystroke.key == "tab" && keystroke.modifiers.shift {
                    "shift-tab"
                } else {
                    keystroke.key.as_str()
                };
                if self.handle_modal_key(key, cx) {
                    cx.stop_propagation();
                }
            }
            ActiveModal::SessionSearch(_) => {
                if !keystroke.modifiers.platform && !keystroke.modifiers.control {
                    let typed = keystroke.key_char.clone().or_else(|| {
                        (keystroke.key.chars().count() == 1).then(|| keystroke.key.clone())
                    });
                    if let Some(ch) = typed {
                        if let ActiveModal::SessionSearch(modal) = &mut self.shell.active_modal {
                            modal.push_char(&ch);
                            cx.notify();
                            cx.stop_propagation();
                        }
                        return;
                    }
                    if keystroke.key == "backspace" {
                        if let ActiveModal::SessionSearch(modal) = &mut self.shell.active_modal {
                            modal.backspace();
                            cx.notify();
                            cx.stop_propagation();
                        }
                        return;
                    }
                }
                if self.handle_modal_key(keystroke.key.as_str(), cx) {
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

    /// Expands or collapses the Changes card on an assistant message (P4).
    fn toggle_changes(&mut self, assistant_id: &str, cx: &mut Context<Self>) {
        if let Some(assistant) = find_assistant_mut(&mut self.shell.feed, assistant_id) {
            assistant.changes_expanded = !assistant.changes_expanded;
            cx.notify();
        }
    }

    /// The gauge total for the action bar ("24.5k tokens"), or `None` while
    /// the gauge has not reported anything (the count is then hidden).
    fn action_token_count(&self) -> Option<String> {
        let gauge = &self.shell.composer.token_gauge;
        if gauge.current_tokens == 0 {
            None
        } else {
            Some(format!("{} tokens", format_token_number(gauge.current_tokens)))
        }
    }

    /// Per-assistant action-bar handlers for the session feed: Copy, Retry
    /// and the Changes toggle. Built per render because they capture the
    /// message content and the weak entity handle.
    fn action_bar_handlers(&self, cx: &mut Context<Self>) -> HashMap<String, ActionBarHandlers> {
        let mut bars: HashMap<String, ActionBarHandlers> = HashMap::new();
        for item in &self.shell.feed.messages {
            if let MessageItem::Assistant(view) = item {
                if view.is_streaming {
                    continue;
                }
                let weak_retry = cx.entity().downgrade();
                let weak_changes = cx.entity().downgrade();
                let retry_id = view.id.clone();
                let changes_id = view.id.clone();
                let content = view.content.clone();
                bars.insert(
                    view.id.clone(),
                    ActionBarHandlers {
                        copy: Some(Rc::new(move |_, _, cx| {
                            cx.write_to_clipboard(ClipboardItem::new_string(content.clone()));
                        })),
                        retry: Some(Rc::new(move |_, _, cx| {
                            weak_retry
                                .update(cx, |this, cx| this.regenerate(&retry_id, cx))
                                .ok();
                        })),
                        changes: Some(Rc::new(move |_, _, cx| {
                            weak_changes
                                .update(cx, |this, cx| this.toggle_changes(&changes_id, cx))
                                .ok();
                        })),
                    },
                );
            }
        }
        bars
    }

    fn render_composer(
        &mut self,
        theme: &HermesTheme,
        cx: &mut Context<Self>,
        variant: ComposerVariant,
    ) -> Div {
        let running = self.active_turn_running();
        let can_send = !self.content.trim().is_empty() && !running;
        let send_btn = SendButton::new(running, can_send);
        let session = variant == ComposerVariant::Session;
        let placeholder = if session {
            SESSION_PLACEHOLDER.to_string()
        } else {
            self.shell.composer.placeholder.clone()
        };

        // Input row: 16/20 text, the focus target and editor action surface.
        let input_row = div()
            .w_full()
            .text_size(px(16.0))
            .line_height(px(20.0))
            .text_color(theme.mockup_text)
            .key_context("Composer")
            .track_focus(&self.focus_handle)
            .cursor(CursorStyle::IBeam)
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
            .child(ComposerElement {
                view: cx.entity(),
                placeholder,
            });

        // Toolbar (mockup gap 8): model pill, spacer, send. The mockup's
        // attach / Auto / mic controls stay out until they do something.
        let toolbar = div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .gap(px(8.0))
            .child(
                div()
                    .id("model_pill_button")
                    .on_click(cx.listener(|this, _, _, cx| {
                        this.request_model_picker(cx);
                    }))
                    .child(
                        mockup_pill(theme)
                            .child(self.shell.composer.model_pill.model_name.clone())
                            .child(mockup_pill_chevron()),
                    ),
            )
            .child(div().flex_1())
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
            );

        let column = div()
            .w_full()
            .flex()
            .flex_col()
            .gap(px(COMPOSER_COLUMN_GAP))
            .child(input_row)
            .child(toolbar);

        let glow = glow_card(column, theme);

        // The context bar only appears once the runtime has reported real
        // usage for this session; before the first turn it would show a
        // fabricated "0% full".
        if session && self.shell.composer.token_gauge.current_tokens > 0 {
            let fraction = self.shell.composer.token_gauge.fraction();
            div()
                .flex_none()
                .flex()
                .flex_col()
                .items_center()
                .pt(px(20.0))
                .pb(px(26.0))
                .gap(px(10.0))
                .child(context_bar(fraction, theme))
                .child(div().w(px(560.0)).max_w_full().child(glow))
        } else {
            glow
        }
    }

    fn render_main_pane(&mut self, theme: &HermesTheme, cx: &mut Context<Self>) -> Div {
        // Non-Chat segments show a Coming soon empty state instead of the feed.
        let coming_soon_pane = self.shell.active_view != ShellView::Chat;
        // Re-splice the list whenever the item count changes or a turn is
        // streaming (streamed items grow taller per chunk).
        let item_count = self.shell.feed.len();
        if item_count != self.feed_synced_count || self.shell.feed.is_streaming {
            self.feed_list.reset(item_count);
            self.feed_synced_count = item_count;
        }
        let bars = if coming_soon_pane {
            HashMap::new()
        } else {
            self.action_bar_handlers(cx)
        };
        let feed = self
            .shell
            .feed
            .render_scrolled(theme, &self.feed_list, bars, self.action_token_count());

        div()
            .flex()
            .flex_col()
            .bg(theme.mockup_canvas)
            // Header h60, padding 0 20px: hamburger ib left; the icon
            // segmented control is absolute-centered per the mockup (it
            // paints below the side row, whose corners stay clickable);
            // right gap 4: Device / Team / More ibs.
            .child(
                div()
                    .relative()
                    .w_full()
                    .h(px(crate::ui::shell::HEADER_HEIGHT))
                    .flex_none()
                    .child(
                        div()
                            .absolute()
                            .left_0()
                            .right_0()
                            .top_0()
                            .bottom_0()
                            .flex()
                            .items_center()
                            .justify_center()
                            .child(self.render_segmented_header(theme, cx)),
                    )
                    .child(
                        div()
                            .w_full()
                            .h_full()
                            .flex()
                            .flex_row()
                            .items_center()
                            .justify_between()
                            .px(px(crate::ui::shell::HEADER_PADDING_X))
                            .child(
                                div()
                                    .id("header_toggle")
                                    .cursor_pointer()
                                    .on_click(cx.listener(|this, _, _, cx| {
                                        this.shell.toggle_sidebar();
                                        cx.notify();
                                    }))
                                    .child(mockup_icon_button(
                                        theme,
                                        "hamburger",
                                        crate::ui::shell::HEADER_ICON,
                                        theme.mockup_text_muted,
                                        None,
                                    )),
                            )
                            .child(
                                div()
                                    .flex()
                                    .flex_row()
                                    .items_center()
                                    .gap(px(4.0))
                                    .child({
                                        let bell_count = self.notification_count();
                                        div()
                                            .id("header_bell")
                                            .relative()
                                            .cursor_pointer()
                                            .on_click(cx.listener(|this, _, _, cx| {
                                                if this.pending_approval_count() > 0 {
                                                    this.open_first_pending_approval(cx);
                                                } else {
                                                    this.open_account_modal(cx);
                                                }
                                            }))
                                            .child(mockup_icon_button(
                                                theme,
                                                "bell",
                                                crate::ui::shell::HEADER_ICON,
                                                theme.mockup_text_muted,
                                                None,
                                            ))
                                            .when(bell_count > 0, |c| {
                                                c.child(
                                                    div()
                                                        .absolute()
                                                        .top(px(-3.0))
                                                        .right(px(-3.0))
                                                        .min_w(px(14.0))
                                                        .h(px(14.0))
                                                        .px(px(3.0))
                                                        .rounded_full()
                                                        .bg(theme.accent_blue)
                                                        .text_color(theme.mockup_body)
                                                        .text_size(px(9.0))
                                                        .font_weight(FontWeight::SEMIBOLD)
                                                        .flex()
                                                        .items_center()
                                                        .justify_center()
                                                        .child(SharedString::from(
                                                            bell_count.to_string(),
                                                        )),
                                                )
                                            })
                                    })
                                    .child(
                                        div()
                                            .id("header_device")
                                            .cursor_pointer()
                                            .on_click(cx.listener(|this, _, _, cx| {
                                                this.open_account_modal(cx);
                                            }))
                                            .child(mockup_icon_button(
                                                theme,
                                                "device",
                                                crate::ui::shell::HEADER_ICON,
                                                theme.mockup_text_muted,
                                                None,
                                            )),
                                    )
                                    .child(
                                        div()
                                            .id("header_team")
                                            .cursor_pointer()
                                            .on_click(cx.listener(|this, _, _, cx| {
                                                this.shell
                                                    .feed
                                                    .add_system_notice("Team — coming soon");
                                                cx.notify();
                                            }))
                                            .child(mockup_icon_button(
                                                theme,
                                                "team",
                                                crate::ui::shell::HEADER_ICON,
                                                theme.mockup_text_muted,
                                                None,
                                            )),
                                    )
                                    .child(
                                        div()
                                            .id("header_more")
                                            .cursor_pointer()
                                            .on_click(cx.listener(|this, _, window, cx| {
                                                this.on_open_settings(
                                                    &shortcuts::OpenSettings,
                                                    window,
                                                    cx,
                                                );
                                            }))
                                            .child(mockup_icon_button(
                                                theme,
                                                "more",
                                                crate::ui::shell::HEADER_ICON,
                                                theme.mockup_text_muted,
                                                None,
                                            )),
                                    ),
                            ),
                    ),
            )
            .children(self.render_update_banner(theme))
            .child(if coming_soon_pane {
                div()
                    .flex_1()
                    .flex()
                    .items_center()
                    .justify_center()
                    .flex_col()
                    .gap_2()
                    .child(
                        div()
                            .text_lg()
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(theme.mockup_text)
                            .child(SharedString::from(format!(
                                "{} — coming soon",
                                self.shell.active_view.pane_title()
                            ))),
                    )
                    .child(
                        div()
                            .text_sm()
                            .text_color(theme.mockup_text_dim)
                            .child("This pane isn't built yet."),
                    )
            } else {
                feed
            })
            .when(!coming_soon_pane, |c| {
                c.child(self.render_composer(theme, cx, ComposerVariant::Session))
            })
    }

    /// Segmented Chat/Files/Browser/Terminal header control: bg `#191919`,
    /// border `#232323`, radius 12, padding 3, gap 2; four 32×32 icon buttons
    /// (Chat bubble / Files / Browser globe / Terminal); active button bg
    /// `#262626`, icon tint unchanged. Only Chat is live; the other panes
    /// render a coming-soon empty state when picked.
    fn render_segmented_header(&self, theme: &HermesTheme, cx: &mut Context<Self>) -> Div {
        let segments: [(&str, ShellView); 4] = [
            ("chat", ShellView::Chat),
            ("files", ShellView::Files),
            ("browser", ShellView::Browser),
            ("terminal", ShellView::Terminal),
        ];

        div()
            .flex()
            .flex_row()
            .items_center()
            .gap(px(crate::ui::shell::SEGMENTED_GAP))
            .p(px(crate::ui::shell::SEGMENTED_PADDING))
            .rounded(px(crate::ui::shell::SEGMENTED_RADIUS))
            .bg(theme.mockup_dock)
            .border_1()
            .border_color(theme.mockup_dock_border)
            .children(segments.map(|(icon, view)| {
                let active = self.shell.active_view == view;
                div()
                    .id(SharedString::from(format!("segment_{icon}")))
                    .cursor_pointer()
                    .size(px(crate::ui::shell::SEGMENT_BUTTON))
                    .flex()
                    .items_center()
                    .justify_center()
                    .rounded(px(crate::ui::shell::SEGMENT_BUTTON_RADIUS))
                    .text_color(theme.mockup_text_muted)
                    .when(active, |s| s.bg(theme.mockup_seg_active))
                    .when(!active, |s| s.hover(|s| s.bg(theme.mockup_hover)))
                    .on_click(cx.listener(move |this, _, _, cx| {
                        this.shell.active_view = view;
                        cx.notify();
                    }))
                    .child(crate::ui::icon_svg(icon, crate::ui::shell::SEGMENT_ICON))
            }))
    }

    // ------------------------------------------------------------------
    // Session management (DX2): rename / pin / archive / delete + search.
    // ------------------------------------------------------------------

    fn open_session_actions(&mut self, session_id: String, cx: &mut Context<Self>) {
        let Some(item) = self
            .shell
            .sidebar
            .session_list
            .sessions
            .iter()
            .find(|s| s.id == session_id)
        else {
            return;
        };
        let modal = SessionActionModal::open_menu(
            session_id,
            item.title.clone(),
            item.is_pinned,
            item.is_archived,
        );
        self.shell.active_modal = ActiveModal::SessionAction(modal);
        cx.notify();
    }

    fn open_session_search(&mut self, cx: &mut Context<Self>) {
        let query = self.shell.sidebar.session_list.search_query.clone();
        self.shell.active_modal = ActiveModal::SessionSearch(SessionSearchModal::new(query));
        cx.notify();
    }

    /// Search results matching the modal query, from the same
    /// `filter_sessions` the sidebar renders. The sidebar's own query stays
    /// empty — the modal owns the search text while it is open.
    fn search_results(
        &mut self,
        modal: &SessionSearchModal,
    ) -> Vec<crate::ui::session_list::SessionItem> {
        self.shell.sidebar.session_list.set_search_query(modal.query.clone());
        let results: Vec<crate::ui::session_list::SessionItem> = self
            .shell
            .sidebar
            .session_list
            .filter_sessions()
            .into_iter()
            .cloned()
            .collect();
        self.shell.sidebar.session_list.set_search_query("");
        results
    }

    fn execute_session_action(&mut self, outcome: SessionActionOutcome, cx: &mut Context<Self>) {
        let ActiveModal::SessionAction(modal) = &self.shell.active_modal else {
            return;
        };
        let session_id = modal.session_id.clone();
        self.shell.dismiss_modal();
        match outcome {
            SessionActionOutcome::Rename(title) => {
                self.apply_session_rename(&session_id, &title, cx)
            }
            SessionActionOutcome::Delete => self.apply_session_delete(&session_id, cx),
            SessionActionOutcome::TogglePin(pinned) => {
                self.apply_session_pin(&session_id, pinned, cx)
            }
            SessionActionOutcome::ToggleArchive(archived) => {
                self.apply_session_archive(&session_id, archived, cx)
            }
        }
    }

    fn apply_session_rename(&mut self, session_id: &str, title: &str, cx: &mut Context<Self>) {
        let old_title = self
            .shell
            .sidebar
            .session_list
            .sessions
            .iter()
            .find(|s| s.id == session_id)
            .map(|s| s.title.clone());
        self.shell
            .sidebar
            .session_list
            .rename_session(session_id, title);
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let sid = session_id.to_string();
        let title_owned = title.to_string();
        let revert = old_title;
        let sid_revert = sid.clone();
        cx.spawn(async move |this, cx| {
            let res = rt
                .spawn(async move { bridge.rename_session(&sid, &title_owned).await })
                .await;
            this.update(cx, |view, cx| {
                if let Err(err) = res {
                    view.shell
                        .feed
                        .add_system_notice(format!("Rename task aborted: {err}"));
                } else if let Ok(Err(err)) = res {
                    view.shell
                        .feed
                        .add_system_notice(format!("Rename failed: {err}"));
                    if let Some(old) = revert {
                        view.shell
                            .sidebar
                            .session_list
                            .rename_session(&sid_revert, old);
                    }
                }
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    fn apply_session_delete(&mut self, session_id: &str, cx: &mut Context<Self>) {
        let was_active = self.shell.active_session_id.as_deref() == Some(session_id);
        self.running_turns.remove(session_id);
        self.turn_assistants.remove(session_id);
        self.sessions.remove(session_id);
        self.shell.sidebar.session_list.delete_session(session_id);
        if was_active {
            self.shell.feed = MessageFeed::new();
            self.shell.active_session_id = None;
            self.feed_synced_count = 0;
            self.sync_transport_state();
        }
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let sid = session_id.to_string();
        cx.spawn(async move |this, cx| {
            let res = rt
                .spawn(async move { bridge.delete_session(&sid).await })
                .await;
            this.update(cx, |view, cx| {
                match res {
                    Ok(Ok(_)) => {}
                    Ok(Err(err)) => {
                        view.shell
                            .feed
                            .add_system_notice(format!("Delete failed: {err}"));
                        view.refresh_session_list(cx);
                    }
                    Err(err) => {
                        view.shell
                            .feed
                            .add_system_notice(format!("Delete task aborted: {err}"));
                        view.refresh_session_list(cx);
                    }
                }
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    fn apply_session_pin(&mut self, session_id: &str, pinned: bool, cx: &mut Context<Self>) {
        if let Some(item) = self
            .shell
            .sidebar
            .session_list
            .sessions
            .iter_mut()
            .find(|s| s.id == session_id)
        {
            item.is_pinned = pinned;
        }
        self.spawn_session_flag(session_id, pinned, cx, "Pin", "pin_session");
    }

    fn apply_session_archive(&mut self, session_id: &str, archived: bool, cx: &mut Context<Self>) {
        if let Some(item) = self
            .shell
            .sidebar
            .session_list
            .sessions
            .iter_mut()
            .find(|s| s.id == session_id)
        {
            item.is_archived = archived;
        }
        if archived && self.shell.active_session_id.as_deref() == Some(session_id) {
            // Archiving the visible chat returns to Home; the session stays
            // resumable from the archived section.
            self.shell.feed = MessageFeed::new();
            self.shell.active_session_id = None;
            self.feed_synced_count = 0;
            self.sync_transport_state();
        }
        self.spawn_session_flag(session_id, archived, cx, "Archive", "archive_session");
    }

    /// Bridge call for a pin/archive flag flip; reverts the optimistic local
    /// flag on failure.
    fn spawn_session_flag(
        &mut self,
        session_id: &str,
        value: bool,
        cx: &mut Context<Self>,
        label: &'static str,
        kind: &'static str,
    ) {
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let sid = session_id.to_string();
        let sid_revert = sid.clone();
        cx.spawn(async move |this, cx| {
            let res = if kind == "pin_session" {
                rt.spawn(async move { bridge.pin_session(&sid, value).await }).await
            } else {
                rt.spawn(async move { bridge.archive_session(&sid, value).await })
                    .await
            };
            this.update(cx, |view, cx| {
                let failed = match &res {
                    Ok(Ok(_)) => false,
                    Ok(Err(err)) => {
                        view.shell
                            .feed
                            .add_system_notice(format!("{label} failed: {err}"));
                        true
                    }
                    Err(err) => {
                        view.shell
                            .feed
                            .add_system_notice(format!("{label} task aborted: {err}"));
                        true
                    }
                };
                if failed {
                    if let Some(item) = view
                        .shell
                        .sidebar
                        .session_list
                        .sessions
                        .iter_mut()
                        .find(|s| s.id == sid_revert)
                    {
                        if kind == "pin_session" {
                            item.is_pinned = !value;
                        } else {
                            item.is_archived = !value;
                        }
                    }
                }
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    /// Phase 3: opens the first pending approval from the modal queue, if any.
    /// Used by the sidebar "Needs you" row and the header bell.
    fn open_first_pending_approval(&mut self, cx: &mut Context<Self>) {
        if matches!(self.shell.active_modal, ActiveModal::Approval(_)) {
            return;
        }
        if let Some(ActiveModal::Approval(modal)) = self.shell.modal_queue.pop_front() {
            self.shell.active_modal = ActiveModal::Approval(modal);
            cx.notify();
        }
    }

    /// Signed-update pill between the header and the feed (absent when up to
    /// date or signed out). Mandatory/incompatible plans use the deletion
    /// tint; eligible updates use the addition tint.
    fn render_update_banner(&self, theme: &HermesTheme) -> Option<AnyElement> {
        let (line, mandatory) = self.update_banner_line()?;
        let (bg, fg) = if mandatory {
            (theme.mockup_diff_del_bg, theme.rose_fg())
        } else {
            (theme.mockup_diff_add_bg, theme.mockup_diff_add_fg)
        };
        Some(
            div()
                .mx(px(crate::ui::shell::HEADER_PADDING_X))
                .mt(px(6.0))
                .child(
                    div()
                        .rounded(px(8.0))
                        .bg(bg)
                        .px_3()
                        .py(px(6.0))
                        .text_size(px(MockupTypeScale::HINT_SIZE))
                        .text_color(fg)
                        .child(SharedString::from(if mandatory {
                            format!("Update required — {line}. Run `hex update`, then restart.")
                        } else {
                            format!("Update available — {line}. Run `hex update` to apply.")
                        })),
                )
                .into_any_element(),
        )
    }

    /// Syncs sidebar live state every frame: live-turn session ids drive the
    /// slot spinners and the running project dot; pending-approval session
    /// ids drive the "Review" chips (sidebar rows and the Home Continue list).
    fn sync_sidebar_live_state(&mut self) {
        self.shell
            .sidebar
            .set_running_sessions(self.running_turns.clone());
        let mut review: HashSet<String> = HashSet::new();
        if let ActiveModal::Approval(modal) = &self.shell.active_modal {
            if let Some(sid) = &modal.session_id {
                review.insert(sid.clone());
            }
        }
        for modal in &self.shell.modal_queue {
            if let ActiveModal::Approval(modal) = modal {
                if let Some(sid) = &modal.session_id {
                    review.insert(sid.clone());
                }
            }
        }
        self.shell.sidebar.set_review_sessions(review);
    }
}

impl Render for ChatView {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        // Phase 3: badge + bell reflect the live approval queue every frame
        // (active modal + queued approval modals).
        {
            let pending = self.pending_approval_count();
            self.shell.sidebar.set_pending_approvals(pending);
        }
        self.sync_sidebar_live_state();
        let theme = HermesTheme::get(cx).clone();

        // Phase 2: the app opens on the Home / start screen; any submit or
        // session selection routes to the Session view (show_home=false).
        // Home still renders inside the shared root (key context, global
        // actions and the modal layer) so modals raised on Home stay visible
        // and dismissible.
        if self.show_home {
            // The start screen's composer is the primary target (mockup shows
            // an active caret); focus it so typing works without a click.
            // Modal focus follows the same rules as the session path.
            let modal_open = self.shell.active_modal.is_open() || self.settings_open;
            if modal_open {
                if !self.last_modal_open {
                    self.modal_focus_handle.focus(window);
                }
            } else if self.last_modal_open || !self.focus_handle.is_focused(window) {
                self.focus_handle.focus(window);
            }
            self.last_modal_open = modal_open;

            let home = self
                .render_home(&theme, cx)
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

            let modal = self.render_modal_layer(&theme, cx);
            return if let Some(modal) = modal {
                home.child(modal).into_any_element()
            } else {
                home.into_any_element()
            };
        }

        // Predictable focus: opening a modal moves focus to it, closing returns
        // focus to the composer. Background activity never steals focus.
        let modal_open = self.shell.active_modal.is_open() || self.settings_open;
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
        let on_needs_you: Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)> =
            Rc::new(cx.listener(|this, _, _, cx| this.open_first_pending_approval(cx)));
        let on_select_session: Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)> = {
            let weak = cx.entity().downgrade();
            Rc::new(move |id, _event, _window, cx| {
                weak.update(cx, |this, cx| this.switch_session(id, cx)).ok();
            })
        };
        let on_search: Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)> =
            Rc::new(cx.listener(|this, _, _, cx| this.open_session_search(cx)));
        let on_settings: Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)> =
            Rc::new(cx.listener(|this, _, window, cx| {
                this.on_open_settings(&shortcuts::OpenSettings, window, cx);
            }));
        let on_feature: Rc<dyn Fn(&'static str, &ClickEvent, &mut Window, &mut App)> = {
            let weak = cx.entity().downgrade();
            Rc::new(move |label, _event, _window, cx| {
                weak.update(cx, |view, cx| {
                    view.shell
                        .feed
                        .add_system_notice(format!("{label} — coming soon"));
                    cx.notify();
                })
                .ok();
            })
        };
        let on_toggle_project: Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)> =
            Rc::new(cx.listener(|this, _, _, cx| {
                this.shell.sidebar.projects_collapsed = !this.shell.sidebar.projects_collapsed;
                cx.notify();
            }));
        let on_session_actions: Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)> = {
            let weak = cx.entity().downgrade();
            Rc::new(move |id, _event, _window, cx| {
                weak.update(cx, |this, cx| this.open_session_actions(id, cx))
                    .ok();
            })
        };
        let sidebar_handlers = SidebarHandlers {
            on_new_chat: Some(on_new_chat),
            on_select_session: Some(on_select_session),
            on_needs_you: Some(on_needs_you),
            on_search: Some(on_search),
            on_settings: Some(on_settings),
            on_feature: Some(on_feature),
            on_toggle_project: Some(on_toggle_project),
            on_account: Some(Rc::new(cx.listener(|this, _, _, cx| {
                this.open_account_modal(cx);
            }))),
            on_session_actions: Some(on_session_actions),
        };

        let mut root = div()
            .size_full()
            .font_family(theme.font_sans()[0].clone())
            .flex()
            .flex_row()
            .overflow_hidden()
            .bg(theme.mockup_canvas)
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
                .child(self.shell.sidebar.render_with_actions(&theme, &sidebar_handlers));
        }

        root = root.child(self.render_main_pane(&theme, cx));

        if let Some(modal) = self.render_modal_layer(&theme, cx) {
            root = root.child(modal);
        }

        root.into_any_element()
    }
}

impl ChatView {
    /// The overlay modal layer (approval, palette, model picker), or `None`
    /// when no modal is open. Shared by the Session and Home render paths.
    fn render_modal_layer(
        &mut self,
        theme: &HermesTheme,
        cx: &mut Context<Self>,
    ) -> Option<Div> {
        // SessionSearch resolves live results through `&mut self` (the modal
        // drives SessionListView::filter_sessions), so it cannot borrow the
        // active modal while doing so; clone the tiny state and return early.
        if let ActiveModal::SessionSearch(state) = self.shell.active_modal.clone() {
            let now = chrono::Utc::now();
            let results = self.search_results(&state);
            let refs: Vec<&crate::ui::session_list::SessionItem> = results.iter().collect();
            let view = cx.entity().downgrade();
            let on_open: std::rc::Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)> =
                std::rc::Rc::new(
                    move |id: String, _event: &ClickEvent, _window: &mut Window, cx: &mut App| {
                        view.update(cx, |this, cx| this.switch_session(id, cx)).ok();
                    },
                );
            let modal = render_modal_container(state.render(theme, &refs, now, on_open), theme);
            return Some(self.modal_overlay(modal, cx));
        }

        let modal = match &self.shell.active_modal {
            ActiveModal::None => {
                if self.settings_open {
                    Some(render_modal_container(
                        self.render_settings_card(theme),
                        theme,
                    ))
                } else {
                    None
                }
            }
            ActiveModal::Approval(modal) => {
                Some(render_modal_container(modal.render(theme), theme))
            }
            ActiveModal::Palette(palette) => {
                Some(render_modal_container(palette.render(theme), theme))
            }
            ActiveModal::ModelPicker(picker) => {
                Some(render_modal_container(picker.render(theme), theme))
            }
            ActiveModal::Account(modal) => {
                Some(render_modal_container(modal.render(theme), theme))
            }
            ActiveModal::SessionAction(modal) => {
                Some(render_modal_container(modal.render(theme), theme))
            }
            ActiveModal::SessionSearch(_) => unreachable!("handled above"),
        }?;
        Some(self.modal_overlay(modal, cx))
    }

    /// Focus-tracked overlay wrapper shared by every modal surface.
    fn modal_overlay(&mut self, modal: Div, cx: &mut Context<Self>) -> Div {
        div()
            .absolute()
            .inset_0()
            .key_context("Modal")
            .track_focus(&self.modal_focus_handle)
            .on_action(cx.listener(Self::on_backspace))
            .on_action(cx.listener(Self::on_up))
            .on_action(cx.listener(Self::on_down))
            .child(modal)
    }

    /// Settings surface (P5, derived): the mockup has no settings screen, so
    /// the card is built from the shared token language — surface card,
    /// muted labels, mono values, and an Esc hint like the palette's.
    fn render_settings_card(&self, theme: &HermesTheme) -> Div {
        let status = &self.shell.sidebar.provider_status;
        let model = if status.model_name.is_empty() {
            "Not selected".to_string()
        } else {
            status.model_name.clone()
        };
        let workspace = format!(
            "{} · {}",
            self.shell.sidebar.workspace_name, self.shell.sidebar.git_branch
        );
        let rows: Vec<(&str, String)> = vec![
            ("Provider", status.provider_name.clone()),
            ("Model", model),
            ("Workspace", workspace),
            (
                "Sessions",
                self.shell.sidebar.session_list.sessions.len().to_string(),
            ),
        ];

        let mut list = div().flex().flex_col();
        for (label, value) in rows {
            list = list.child(
                div()
                    .flex()
                    .items_center()
                    .justify_between()
                    .gap(px(16.0))
                    .px(px(12.0))
                    .py(px(8.0))
                    .rounded(px(8.0))
                    .bg(theme.mockup_sidebar)
                    .child(
                        div()
                            .text_size(px(MockupTypeScale::INLINE_CODE_SIZE))
                            .text_color(theme.mockup_text_muted)
                            .child(label.to_string()),
                    )
                    .child(
                        div()
                            .font_family(theme.font_mono()[0].clone())
                            .text_size(px(MockupTypeScale::INLINE_CODE_SIZE))
                            .text_color(theme.mockup_text_bright)
                            .child(value),
                    ),
            );
        }

        div()
            .w(px(460.0))
            .flex()
            .flex_col()
            // Header
            .child(
                div()
                    .flex()
                    .items_center()
                    .justify_between()
                    .px(px(20.0))
                    .py(px(14.0))
                    .border_b_1()
                    .border_color(theme.mockup_border)
                    .child(
                        div()
                            .text_size(px(MockupTypeScale::BASE_SIZE))
                            .line_height(px(MockupTypeScale::BASE_LINE_HEIGHT))
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(theme.mockup_text_bright)
                            .child("Settings"),
                    )
                    .child(
                        div()
                            .px(px(6.0))
                            .py(px(2.0))
                            .rounded(px(3.0))
                            .bg(theme.mockup_hover)
                            .border_1()
                            .border_color(theme.mockup_border)
                            .font_family(theme.font_mono()[0].clone())
                            .text_size(px(MockupTypeScale::HINT_SIZE))
                            .text_color(theme.mockup_text_dim)
                            .child("Esc"),
                    ),
            )
            // Honest rows
            .child(
                div()
                    .flex()
                    .flex_col()
                    .gap(px(4.0))
                    .px(px(20.0))
                    .py(px(16.0))
                    .child(list),
            )
            // Footer hint
            .child(
                div()
                    .flex()
                    .justify_center()
                    .px(px(20.0))
                    .py(px(12.0))
                    .border_t_1()
                    .border_color(theme.mockup_border)
                    .font_family(theme.font_mono()[0].clone())
                    .text_size(px(MockupTypeScale::HINT_SIZE))
                    .text_color(theme.mockup_text_dim)
                    .child("Press Esc to close"),
            )
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

/// Composer variants: the Start screen glow card (640px, no context bar) and
/// the Session footer (560px card over the context bar).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ComposerVariant {
    Home,
    Session,
}

/// Milliseconds since first use, driving the caret blink phase.
fn caret_epoch_ms() -> u64 {
    static EPOCH: OnceLock<std::time::Instant> = OnceLock::new();
    EPOCH.get_or_init(std::time::Instant::now).elapsed().as_millis() as u64
}

struct ComposerElement {
    view: Entity<ChatView>,
    /// Placeholder shown while the buffer is empty (Start vs Session wording).
    placeholder: String,
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
        let placeholder = self.placeholder.clone();
        let theme = HermesTheme::get(cx);
        let style = window.text_style();
        let font_size = style.font_size.to_pixels(window.rem_size());
        let line_height = window.line_height();

        let (display_text, text_color) = if content.is_empty() {
            // Mockup placeholder tone (#858585).
            (placeholder, theme.mockup_text_dim.into())
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
        // Mockup caret: 2×20 accent, 1.1s steps(1) blink; reduced motion
        // keeps it solid. The glow's breathe animation supplies the repaints.
        let caret_on = motion::reduced_motion()
            || motion::blink_visible(std::time::Duration::from_millis(caret_epoch_ms()));
        let mut start = 0usize;
        for (i, shaped) in lines.iter().enumerate() {
            let line_end = start + shaped.text.len();
            let y = bounds.top() + line_height * i as f32;
            if selected_range.is_empty() {
                if caret_on && cursor >= start && cursor <= line_end {
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

/// Notice for a decision the runtime did not apply (B35). A late decision against
/// an already closed approval is reported as ignored; an unknown id stays retryable.
fn late_approval_notice(status: &str, approval_id: &str) -> String {
    match status {
        "expired" | "canceled" | "resolved" => {
            format!("This approval was already {status}; the late decision was ignored.")
        }
        _ => format!(
            "The runtime did not apply this decision ({status}; approval {approval_id}); no pending request matched. Retry or deny."
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn late_approval_notice_distinguishes_closed_from_unknown() {
        for status in ["expired", "canceled", "resolved"] {
            let notice = late_approval_notice(status, "appr_1");
            assert!(notice.contains(&format!("already {status}")), "{notice}");
            assert!(notice.contains("ignored"), "{notice}");
            assert!(!notice.contains("Retry"), "{notice}");
        }
        let unknown = late_approval_notice("acknowledged", "appr_1");
        assert!(unknown.contains("Retry or deny"), "{unknown}");
        assert!(!unknown.contains("ignored"), "{unknown}");
    }

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
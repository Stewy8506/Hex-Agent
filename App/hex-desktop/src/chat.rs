//! Interactive chat root view for HEX Desktop (F02 vertical slice).
//!
//! `ChatView` is the real application root opened by `main`. Unlike the static
//! `AppShell` component previews, it owns:
//! - a focusable multiline composer driven by the platform input system
//!   (`EntityInputHandler` + `window.handle_input`, mirroring gpui's canonical
//!   text input pattern),
//! - the live `AcpBridge` connection (connect on startup, create a session on
//!   the first prompt, then `session/prompt`, `session/cancel`, `session/approvalDecision`),
//! - per-session feeds and composer drafts so switching sessions preserves
//!   conversation state (Hermes `use-composer-submit.ts` draft semantics),
//! - authoritative turn completion from the `session/prompt` response, with
//!   honest system notices for cancelled/failed/error states.

use std::collections::{HashMap, HashSet};
use std::ops::Range;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use crate::ui::home;
use crate::ui::home::{ContinueRow, COMING_SOON_DOCK};

use gpui::prelude::FluentBuilder;
use gpui::*;
use tokio::runtime::{Handle, Runtime};
use unicode_segmentation::UnicodeSegmentation;

use hex_protocol::error::AcpError;
use hex_protocol::models::{
    ApprovalDecisionResponse, CloudAuthStatus, CloudUpdatePlan, PagedSessionLoad,
    SessionNewResponse, SessionPromptResponse,
};

use crate::acp_bridge::{AcpBridge, AcpEvent};
use crate::fonts;
use crate::menu::init_menus;
use crate::shortcuts::{
    self, init_shortcuts, About, ActualSize, CloseWindow, Hide, Quit, Redo, Undo, ZoomIn, ZoomOut,
};
use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::composer::{context_bar, glow_card, COMPOSER_COLUMN_GAP, SESSION_PLACEHOLDER};
use crate::ui::feed::{ApprovalCard, ApprovalCardState, ApprovalVisual, MessageFeed, MessageItem};
use crate::ui::message::{ActionBarHandlers, AssistantMessageView};
use crate::ui::modals::account::{AccountAction, AccountMode};
use crate::ui::modals::approval::{
    ApprovalHandler, ApprovalInteraction, ToolApprovalChoice, ToolApprovalResult,
};
use crate::ui::modals::model_picker::{ModelPickerModal, ModelRetryHandler, ModelSelectHandler};
use crate::ui::modals::palette::PaletteActionHandler;
use crate::ui::modals::session_actions::{
    SessionActionHandler, SessionActionModal, SessionActionMode, SessionActionOutcome,
    SessionSearchModal,
};
use crate::ui::modals::settings::{SettingsAction, SettingsModal, SettingsSaveHandler};
use crate::ui::modals::{
    render_about, render_modal_container, AccountModal, ActiveModal, ModalMotion, ModelEntry,
    PaletteAction, ToolApprovalModal,
};
use crate::ui::model_pill::SendButton;
use crate::ui::motion;
use crate::ui::pill::{
    mockup_icon_button, mockup_icon_button_motion, mockup_pill_base, mockup_pill_chevron,
};
use crate::ui::reasoning::ThinkingDisclosure;
use crate::ui::session_list::SessionItem;
use crate::ui::shell::{AppShell, ShellView};
use crate::ui::sidebar::{ProviderState, SidebarHandlers, SIDEBAR_WIDTH};
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

const DEFAULT_REM_SIZE: f32 = 16.0;
const ZOOM_STEP: f32 = 2.0;
const MIN_REM_SIZE: f32 = 12.0;
const MAX_REM_SIZE: f32 = 24.0;
const MAX_UNDO_HISTORY: usize = 100;

/// Register embedded brand fonts, theme, menus, global shortcuts, and
/// composer bindings. Shared by the real `main` entry point and the
/// acceptance tests.
pub fn init_app(cx: &mut App) {
    if let Err(err) = fonts::load_embedded_fonts(cx) {
        log::warn!("failed to load embedded brand fonts: {err}");
    }
    HermesTheme::init(cx);
    cx.set_global(motion::MotionSystem::new());
    init_shortcuts(cx);
    init_menus(cx);
    cx.bind_keys(composer_keybindings());
    cx.on_action(|_: &Quit, cx| cx.quit());
    cx.on_action(|_: &Hide, cx| cx.hide());
    cx.on_window_closed(|cx| {
        if cx.windows().is_empty() {
            cx.quit();
        }
    })
    .detach();
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
    let config = WindowConfig::default();
    let bounds = cx
        .primary_display()
        .map(|display| config.centered_on_display(display.bounds()));
    let options = config.window_options(bounds);
    cx.open_window(options, move |_window, cx| {
        cx.new(|cx| ChatView::new(bridge.clone(), rt.clone(), workspace.clone(), cx))
    })
}

/// Saved state for an inactive session.
#[derive(Debug, Clone, Default)]
struct SessionState {
    feed: MessageFeed,
    draft: String,
    attachments: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct UpdateBannerVisual {
    line: String,
    mandatory: bool,
}

#[derive(Debug, Clone)]
struct SessionTransition {
    outgoing: Option<MessageFeed>,
}

#[derive(Debug, Clone)]
struct ComposerSnapshot {
    content: String,
    selected_range: Range<usize>,
    selection_reversed: bool,
    marked_range: Option<Range<usize>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TextEditKind {
    Insert,
    Delete,
    Replace,
}

fn find_assistant_mut<'a>(
    feed: &'a mut MessageFeed,
    id: &str,
) -> Option<&'a mut AssistantMessageView> {
    feed.messages.iter_mut().rev().find_map(|m| match m {
        MessageItem::Assistant(a) if a.id == id => Some(a),
        _ => None,
    })
}

fn session_title_from_prompt(text: &str) -> String {
    let normalized = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let title: String = normalized.chars().take(48).collect();
    if title.trim().is_empty() {
        "Untitled chat".to_string()
    } else {
        title
    }
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
            feed.mark_changed();
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
async fn build_restored_feed(
    bridge: &AcpBridge,
    rt: &Handle,
    load: &PagedSessionLoad,
) -> MessageFeed {
    let mut feed = MessageFeed::new();
    let mut incomplete = false;
    for paged in &load.page.history {
        let prompt = match full_history_field(
            bridge,
            rt,
            &load.session_id,
            paged.turn_index,
            "prompt",
            paged.prompt_chars,
            &paged.turn.prompt,
        )
        .await
        {
            Some(text) => text,
            None => {
                incomplete = true;
                paged.turn.prompt.clone()
            }
        };
        if !prompt.is_empty() {
            feed.add_user_message(prompt);
        }
        let response = match full_history_field(
            bridge,
            rt,
            &load.session_id,
            paged.turn_index,
            "response",
            paged.response_chars,
            &paged.turn.response,
        )
        .await
        {
            Some(text) => text,
            None => {
                incomplete = true;
                paged.turn.response.clone()
            }
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
    MessageFeed::with_messages(feed.messages)
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
    rendered_modal: Option<(u64, ActiveModal)>,
    outgoing_modals: Vec<(u64, ActiveModal)>,
    next_modal_instance: u64,

    // Composer editor state (utf8 byte offsets, mirroring gpui examples/input.rs).
    content: String,
    selected_range: Range<usize>,
    selection_reversed: bool,
    marked_range: Option<Range<usize>>,
    last_layout: Option<Vec<ShapedLine>>,
    last_bounds: Option<Bounds<Pixels>>,
    last_line_height: Pixels,
    is_selecting: bool,
    composer_undo: Vec<ComposerSnapshot>,
    composer_redo: Vec<ComposerSnapshot>,
    composer_edit_group: Option<(TextEditKind, Instant)>,
    composer_undo_break: bool,
    modal_undo: Vec<ActiveModal>,
    modal_redo: Vec<ActiveModal>,
    modal_edit_group: Option<(TextEditKind, Instant)>,
    modal_undo_break: bool,

    bridge: AcpBridge,
    rt: Handle,
    workspace: PathBuf,

    sessions: HashMap<String, SessionState>,
    session_load_generation: u64,
    session_list_generation: u64,
    model_picker_generation: u64,
    creating_session: bool,
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
    /// Current or exiting toast content kept mounted until its springs settle.
    home_toast_visual: Option<&'static str>,
    home_connection_error: Option<String>,
    /// Cached `cloud/auth/status` for the sidebar footer and header device chip.
    cloud_status: Option<CloudAuthStatus>,
    /// Latest verified update plan from `cloud/update/check` (signed in only).
    cloud_update: Option<CloudUpdatePlan>,
    /// Current or exiting banner content kept mounted until its springs settle.
    update_banner_visual: Option<UpdateBannerVisual>,
    /// Scroll state of the bottom-anchored message list; owned here because
    /// `AppShell` derives Debug/Clone/PartialEq and `ListState` cannot.
    feed_list: ListState,
    /// Scroll state for the outgoing session crossfade layer.
    outgoing_feed_list: ListState,
    /// Feed item count currently spliced into `feed_list`.
    feed_synced_count: usize,
    /// Feed content revision whose row measurements are reflected in `feed_list`.
    feed_synced_revision: u64,
    /// Outgoing feed captured by the latest completed session swap.
    pending_session_transition: Option<SessionTransition>,
    /// Outgoing feed captured while an uncached session is still loading.
    deferred_session_transition: Option<SessionTransition>,
    /// Previous session kept mounted while its crossfade exits.
    outgoing_session_feed: Option<MessageFeed>,
    /// Reused layer springs preserve momentum across rapid session switches.
    session_incoming_motion: Option<motion::RevealMotion>,
    session_outgoing_motion: Option<motion::RevealMotion>,
    /// Operator-attached local file paths pending inclusion in the next prompt.
    attachments: Vec<String>,
    /// Removed chips stay mounted until their exit springs settle.
    outgoing_attachments: Vec<String>,
    approval_visuals: HashMap<String, ApprovalVisual>,
    /// When the first reasoning chunk arrived per session, for the
    /// "Thought for Ns" header on turn completion.
    thought_starts: HashMap<String, Instant>,
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
            rendered_modal: None,
            outgoing_modals: Vec::new(),
            next_modal_instance: 0,
            content: String::new(),
            selected_range: 0..0,
            selection_reversed: false,
            marked_range: None,
            last_layout: None,
            last_bounds: None,
            last_line_height: px(20.0),
            is_selecting: false,
            composer_undo: Vec::new(),
            composer_redo: Vec::new(),
            composer_edit_group: None,
            composer_undo_break: false,
            modal_undo: Vec::new(),
            modal_redo: Vec::new(),
            modal_edit_group: None,
            modal_undo_break: false,
            bridge,
            rt,
            workspace,
            sessions: HashMap::new(),
            session_load_generation: 0,
            session_list_generation: 0,
            model_picker_generation: 0,
            creating_session: false,
            running_turns: HashSet::new(),
            turn_assistants: HashMap::new(),
            current_model: String::new(),
            current_provider: "HEX".to_string(),
            model_id: None,
            supervisor_active: false,
            show_home: true,
            pending_home_prompt: None,
            home_coming_soon: None,
            home_toast_visual: None,
            home_connection_error: None,
            cloud_status: None,
            cloud_update: None,
            update_banner_visual: None,
            feed_list: ListState::new(0, ListAlignment::Bottom, px(1000.)),
            outgoing_feed_list: ListState::new(0, ListAlignment::Bottom, px(1000.)),
            feed_synced_count: 0,
            feed_synced_revision: 0,
            pending_session_transition: None,
            deferred_session_transition: None,
            outgoing_session_feed: None,
            session_incoming_motion: None,
            session_outgoing_motion: None,
            attachments: Vec::new(),
            outgoing_attachments: Vec::new(),
            approval_visuals: HashMap::new(),
            thought_starts: HashMap::new(),
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
            let res = rt
                .spawn(async move { bridge.cloud_auth_status().await })
                .await;
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
                        view.shell
                            .sidebar
                            .set_account_status("Account", "Local mode");
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
            let res = rt
                .spawn(async move { bridge.cloud_update_check().await })
                .await;
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
            + if self.update_banner_line().is_some() {
                1
            } else {
                0
            }
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
                        let message = "Could not connect to the HEX runtime. Install it (install.sh) or start `hex acp`; retrying automatically.".to_string();
                        view.home_connection_error = Some(message.clone());
                        view.shell.feed.add_system_notice(message);
                        view.begin_supervision(cx);
                    })
                    .ok();
                    return;
                }
            }
            this.update(cx, |view, cx| {
                view.shell.composer.model_pill.set_model("HEX", "ready");
                view.shell.sidebar.provider_status.model_name = "Ready".to_string();
                view.sync_transport_state();
                view.refresh_session_list(cx);
                cx.notify();
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
    /// previous session from durable history, or return to a local Home draft.
    fn recover_session(&mut self, cx: &mut Context<Self>) {
        let generation = self.begin_session_navigation();
        let Some(previous) = self.shell.active_session_id.clone() else {
            self.shell.sidebar.provider_status.state = ProviderState::Connected;
            self.refresh_session_list(cx);
            cx.notify();
            return;
        };
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        cx.spawn(async move |this, cx| {
            let b = bridge.clone();
            let restored = match rt
                .spawn(async move { b.load_session_paged(&previous).await })
                .await
            {
                Ok(Ok(load)) => {
                    let feed = build_restored_feed(&bridge, &rt, &load).await;
                    Some((load, feed))
                }
                _ => None,
            };
            this.update(cx, |view, cx| {
                if view.session_load_generation != generation {
                    return;
                }
                if let Some((load, feed)) = restored {
                    view.adopt_loaded_session(load, feed, cx);
                } else {
                    view.clear_session_transition(cx);
                    view.show_home = true;
                    view.shell.active_session_id = None;
                    view.shell.sidebar.session_list.selected_session_id = None;
                    view.shell.feed = MessageFeed::new();
                    view.force_feed_resync();
                    view.home_connection_error = Some(
                        "Reconnected, but the previous conversation could not be restored. Your draft is still here.".to_string(),
                    );
                    view.sync_transport_state();
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
        self.session_list_generation = self.session_list_generation.wrapping_add(1);
        let generation = self.session_list_generation;
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        cx.spawn(async move |this, cx| {
            let res = rt.spawn(async move { bridge.list_sessions().await }).await;
            let Ok(Ok(items)) = res else {
                return;
            };
            this.update(cx, |view, cx| {
                if view.session_list_generation != generation {
                    return;
                }
                let active_id = view.shell.active_session_id.clone();
                let now = chrono::Utc::now();
                let mut listed_ids = HashSet::new();
                let mut sessions = Vec::new();
                for item in items {
                    let backend_title = item
                        .title
                        .as_deref()
                        .map(str::trim)
                        .filter(|title| !title.is_empty())
                        .map(|title| title.split_whitespace().collect::<Vec<_>>().join(" "));
                    if item.turns == 0
                        && backend_title.is_none()
                        && active_id.as_deref() != Some(item.session_id.as_str())
                    {
                        continue;
                    }
                    let existing = view
                        .shell
                        .sidebar
                        .session_list
                        .sessions
                        .iter()
                        .find(|session| session.id == item.session_id)
                        .cloned();
                    let created_at = item
                        .created_at
                        .and_then(|seconds| chrono::DateTime::from_timestamp(seconds as i64, 0))
                        .unwrap_or_else(|| {
                            existing
                                .as_ref()
                                .map(|session| session.created_at)
                                .unwrap_or(now)
                        });
                    let title = backend_title
                        .or_else(|| {
                            existing
                                .as_ref()
                                .map(|session| session.title.clone())
                                .filter(|title| !title.trim().is_empty() && title != "Untitled chat")
                        })
                        .unwrap_or_else(|| "Untitled chat".to_string());
                    let mut session = SessionItem::with_timestamps(
                        item.session_id.clone(),
                        title,
                        created_at,
                        existing
                            .as_ref()
                            .map(|session| session.updated_at)
                            .unwrap_or(created_at),
                    )
                    .with_pinned(item.pinned)
                    .with_model(item.model.clone());
                    session.is_archived = item.archived;
                    session.message_count = item.turns;
                    listed_ids.insert(item.session_id.clone());
                    sessions.push(session);
                }
                if let Some(active_id) = active_id {
                    if !listed_ids.contains(&active_id) {
                        if let Some(active) = view
                            .shell
                            .sidebar
                            .session_list
                            .sessions
                            .iter()
                            .find(|session| session.id == active_id)
                            .cloned()
                        {
                            listed_ids.insert(active_id);
                            sessions.push(active);
                        }
                    }
                }
                sessions.sort_by(|a, b| {
                    b.is_pinned
                        .cmp(&a.is_pinned)
                        .then_with(|| b.updated_at.cmp(&a.updated_at))
                        .then_with(|| a.id.cmp(&b.id))
                });
                view.shell.sidebar.session_list.sessions = sessions;
                view.shell.sidebar.session_list.selected_session_id = view
                    .shell
                    .active_session_id
                    .clone()
                    .filter(|id| listed_ids.contains(id));
                view.sessions.retain(|id, _| listed_ids.contains(id));
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    fn start_event_pump(&mut self, cx: &mut Context<Self>) {
        let mut rx = self.bridge.subscribe();
        let rt = self.rt.clone();
        cx.spawn(async move |this, cx| loop {
            let first = match rx.recv().await {
                Some(event) => event,
                None => break,
            };
            let mut batch = vec![first];
            rt.spawn(async { tokio::time::sleep(Duration::from_millis(16)).await })
                .await
                .ok();
            loop {
                match rx.try_recv() {
                    Ok(event) => {
                        let coalesced = batch
                            .last_mut()
                            .is_some_and(|last| last.coalesce(&event));
                        if !coalesced {
                            batch.push(event);
                        }
                    }
                    Err(tokio::sync::mpsc::error::TryRecvError::Empty) => break,
                    Err(tokio::sync::mpsc::error::TryRecvError::Disconnected) => break,
                }
            }
            if this
                .update(cx, |view, cx| view.route_events(batch, cx))
                .is_err()
            {
                break;
            }
            if rx.is_closed() {
                break;
            }
        })
        .detach();
    }

    fn route_events(&mut self, events: Vec<AcpEvent>, cx: &mut Context<Self>) {
        let before = (
            self.shell.feed.revision(),
            self.shell.composer.is_streaming,
            self.shell.composer.token_gauge.current_tokens,
            self.shell.composer.token_gauge.max_tokens,
            self.shell.sidebar.provider_status.state,
            std::mem::discriminant(&self.shell.active_modal),
            self.shell.modal_queue.len(),
            self.supervisor_active,
        );
        let provider_updated = events.iter().any(|event| {
            matches!(
                event,
                AcpEvent::Connected { .. } | AcpEvent::StatusUpdate { .. }
            )
        });
        for event in events {
            self.route_event(event, cx);
        }
        self.sync_transport_state();
        let after = (
            self.shell.feed.revision(),
            self.shell.composer.is_streaming,
            self.shell.composer.token_gauge.current_tokens,
            self.shell.composer.token_gauge.max_tokens,
            self.shell.sidebar.provider_status.state,
            std::mem::discriminant(&self.shell.active_modal),
            self.shell.modal_queue.len(),
            self.supervisor_active,
        );
        if provider_updated || before != after {
            cx.notify();
        }
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
            return;
        }

        let session_id = match &event {
            AcpEvent::TokenChunk { session_id, .. }
            | AcpEvent::ThoughtChunk { session_id, .. }
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
                return;
            }
            self.shell.handle_acp_event(event);
            self.sync_transport_state();
            return;
        };

        // Approvals surface as a modal regardless of which session is active,
        // and mirror into that session's feed so the stream shows what was
        // asked and how it resolved.
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
            let mut modal = ToolApprovalModal::new(
                approval_id.clone(),
                tool_name.clone(),
                command.clone(),
                risk_level.clone(),
            );
            modal.session_id = Some(session_id.clone());
            if let Some(desc) = description.clone() {
                modal = modal.with_description(desc);
            }
            self.shell.open_approval(modal);

            let summary_source = if command.trim().is_empty() {
                description.as_deref().unwrap_or_default()
            } else {
                command.as_str()
            };
            let summary: String = summary_source.chars().take(200).collect();
            let summary = if summary.trim().is_empty() {
                tool_name.clone()
            } else {
                summary
            };
            let feed: &mut MessageFeed =
                if self.shell.active_session_id.as_deref() == Some(session_id.as_str()) {
                    &mut self.shell.feed
                } else {
                    match self.sessions.get_mut(&session_id) {
                        Some(state) => &mut state.feed,
                        None => {
                            // The modal still carries the request even when no
                            // feed exists yet for this session.
                            self.sync_transport_state();
                            return;
                        }
                    }
                };
            feed.add_approval_card(ApprovalCard::new(
                approval_id,
                tool_name,
                summary,
                risk_level,
            ));
            self.sync_transport_state();
            return;
        }

        let is_active = self.shell.active_session_id.as_deref() == Some(sid.as_str());
        let running = self.running_turns.contains(&sid);
        let turn_assistant = self.turn_assistants.get(&sid).cloned();
        // Elapsed reasoning time only when this notification ends the turn.
        let thought_elapsed = if matches!(event, AcpEvent::TurnComplete { .. }) {
            self.thought_starts
                .remove(&sid)
                .map(|t| t.elapsed().as_secs_f64())
        } else {
            None
        };
        // Stamp the reasoning clock before the feed borrow (same field can't
        // be touched while `feed` aliases into `self`).
        if matches!(event, AcpEvent::ThoughtChunk { .. }) {
            self.thought_starts
                .entry(sid.clone())
                .or_insert_with(Instant::now);
        }

        {
            let feed: &mut MessageFeed = if is_active {
                &mut self.shell.feed
            } else if let Some(state) = self.sessions.get_mut(&sid) {
                &mut state.feed
            } else {
                self.sync_transport_state();
                return;
            };

            match event {
                AcpEvent::TokenChunk { text, .. } => {
                    let mut changed = false;
                    if let Some(assistant) = turn_assistant
                        .as_deref()
                        .and_then(|id| find_assistant_mut(feed, id))
                    {
                        // While the turn is running always append; after the
                        // authoritative response only fill a still-empty message
                        // (late deltas must not duplicate the final text).
                        if running || assistant.content.is_empty() {
                            assistant.content.push_str(&text);
                            changed = true;
                        }
                    } else if running {
                        if !feed.is_streaming {
                            feed.start_assistant_streaming();
                        }
                        feed.append_streaming_chunk(&text);
                    }
                    if changed {
                        feed.mark_changed();
                    }
                }
                AcpEvent::ThoughtChunk { text, .. } => {
                    if let Some(assistant) = turn_assistant
                        .as_deref()
                        .and_then(|id| find_assistant_mut(feed, id))
                    {
                        let disclosure = assistant.reasoning.get_or_insert_with(|| {
                            ThinkingDisclosure::new("").with_generating(true)
                        });
                        disclosure.append_chunk(&text);
                        feed.mark_changed();
                    }
                }
                AcpEvent::TurnComplete { .. } => {
                    feed.finish_streaming();
                    if let Some(elapsed) = thought_elapsed {
                        if let Some(assistant) = turn_assistant
                            .as_deref()
                            .and_then(|id| find_assistant_mut(feed, id))
                        {
                            if let Some(disclosure) = &mut assistant.reasoning {
                                disclosure.finish(elapsed);
                            }
                        }
                    }
                }
                AcpEvent::ToolCall {
                    call_id,
                    tool_name,
                    arguments_json,
                    ..
                } => {
                    let card = ToolCallCard::new(call_id, tool_name, arguments_json);
                    let mut changed = false;
                    match turn_assistant
                        .as_deref()
                        .and_then(|id| find_assistant_mut(feed, id))
                    {
                        Some(assistant) => {
                            assistant.tool_calls.push(card);
                            changed = true;
                        }
                        None => feed.add_tool_call(card),
                    }
                    if changed {
                        feed.mark_changed();
                    }
                }
                AcpEvent::ToolResult {
                    call_id,
                    output_text,
                    is_error,
                    diff,
                    ..
                } => {
                    feed.update_tool_result(&call_id, &output_text, is_error, diff.as_deref());
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

    fn begin_session_navigation(&mut self) -> u64 {
        self.session_load_generation = self.session_load_generation.wrapping_add(1);
        self.model_picker_generation = self.model_picker_generation.wrapping_add(1);
        self.shell
            .modal_queue
            .retain(|modal| !matches!(modal, ActiveModal::ModelPicker(_)));
        if matches!(self.shell.active_modal, ActiveModal::ModelPicker(_)) {
            self.shell.dismiss_modal();
        }
        self.session_load_generation
    }

    fn capture_session_transition(&self, visible: bool) -> SessionTransition {
        let has_conversation = self.shell.feed.messages.iter().any(|item| {
            matches!(
                item,
                MessageItem::User(_) | MessageItem::Assistant(_) | MessageItem::Approval(_)
            )
        });
        SessionTransition {
            outgoing: (visible && has_conversation)
                .then(|| self.shell.feed.snapshot_without_entrances()),
        }
    }

    fn session_motion_layers(
        &mut self,
        cx: &Context<Self>,
    ) -> (motion::RevealMotion, motion::RevealMotion) {
        let owner = cx.entity().entity_id();
        if self.session_incoming_motion.is_none() {
            self.session_incoming_motion = Some(motion::RevealMotion::for_identity(
                owner,
                "session_transition",
                "incoming",
            ));
        }
        if self.session_outgoing_motion.is_none() {
            self.session_outgoing_motion = Some(motion::RevealMotion::for_identity(
                owner,
                "session_transition",
                "outgoing",
            ));
        }
        (
            self.session_incoming_motion
                .expect("incoming layer initialized"),
            self.session_outgoing_motion
                .expect("outgoing layer initialized"),
        )
    }

    fn clear_session_transition(&mut self, cx: &mut Context<Self>) {
        if let Some(motion) = self.session_incoming_motion.take() {
            motion.remove(cx);
        }
        if let Some(motion) = self.session_outgoing_motion.take() {
            motion.remove(cx);
        }
        self.pending_session_transition = None;
        self.deferred_session_transition = None;
        self.outgoing_session_feed = None;
        self.outgoing_feed_list.reset(0);
    }

    fn sync_session_transition(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        if let Some(transition) = self.pending_session_transition.take() {
            let (mut incoming, mut outgoing) = self.session_motion_layers(cx);
            let has_outgoing =
                transition.outgoing.is_some() || self.outgoing_session_feed.is_some();
            if transition.outgoing.is_some() && self.outgoing_session_feed.is_some() {
                std::mem::swap(
                    &mut self.session_incoming_motion,
                    &mut self.session_outgoing_motion,
                );
                (incoming, outgoing) = self.session_motion_layers(cx);
            }
            if let Some(feed) = transition.outgoing {
                self.outgoing_feed_list.reset(feed.len());
                self.outgoing_session_feed = Some(feed);
                outgoing.exit(window, cx);
            } else if let Some(motion) = self.session_outgoing_motion {
                motion.exit(window, cx);
            }
            if has_outgoing {
                incoming.enter(window, cx);
            } else {
                incoming.ensure(window, cx, 1.0);
            }
        }

        if let Some(motion) = self.session_outgoing_motion {
            let settled = motion::MotionSystem::is_settled(&*cx, motion.spatial)
                && motion::MotionSystem::is_settled(&*cx, motion.opacity);
            if settled && !motion.values(&*cx).is_visible() {
                motion.remove(cx);
                self.session_outgoing_motion = None;
                self.outgoing_session_feed = None;
                self.outgoing_feed_list.reset(0);
            }
        }
        if self.outgoing_session_feed.is_none() {
            if let Some(motion) = self.session_incoming_motion {
                if motion::MotionSystem::is_settled(&*cx, motion.spatial)
                    && motion::MotionSystem::is_settled(&*cx, motion.opacity)
                {
                    motion.remove(cx);
                    self.session_incoming_motion = None;
                }
            }
        }
    }

    fn force_feed_resync(&mut self) {
        self.feed_synced_count = usize::MAX;
    }

    fn save_active_session(&mut self) {
        if let Some(current) = self.shell.active_session_id.clone() {
            let feed = std::mem::replace(&mut self.shell.feed, MessageFeed::new());
            let draft = self.content.clone();
            let attachments = self.attachments.clone();
            self.sessions.insert(
                current,
                SessionState {
                    feed,
                    draft,
                    attachments,
                },
            );
        }
    }

    fn adopt_session(
        &mut self,
        resp: SessionNewResponse,
        opens_view: bool,
        cx: &mut Context<Self>,
    ) {
        self.begin_session_navigation();
        self.deferred_session_transition = None;
        self.pending_session_transition = Some(self.capture_session_transition(!self.show_home));
        self.save_active_session();
        let id = resp.session_id.clone();
        self.shell.feed = MessageFeed::new();
        self.force_feed_resync();
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
        self.shell.sidebar.provider_status.model_name = resp.model.clone();
        self.home_connection_error = None;
        let home_prompt = self.pending_home_prompt.take();
        if let Some(text) = home_prompt {
            self.show_home = false;
            self.submit_text(text, cx);
        } else if opens_view {
            self.show_home = false;
        }
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
        self.begin_session_navigation();
        let transition = self
            .deferred_session_transition
            .take()
            .unwrap_or_else(|| self.capture_session_transition(!self.show_home));
        self.pending_session_transition = Some(transition);
        self.save_active_session();
        let id = load.session_id.clone();
        self.running_turns.remove(&id);
        self.turn_assistants.remove(&id);
        self.shell.feed = feed;
        self.force_feed_resync();
        self.reset_editor(cx);
        let existing = self
            .shell
            .sidebar
            .session_list
            .sessions
            .iter()
            .find(|session| session.id == id)
            .cloned();
        let prompted_title = self
            .shell
            .feed
            .messages
            .iter()
            .find_map(|m| match m {
                MessageItem::User(u) => Some(u.content.clone()),
                _ => None,
            })
            .map(|text| session_title_from_prompt(&text))
            .filter(|title| title != "Untitled chat");
        let title = existing
            .as_ref()
            .map(|session| session.title.clone())
            .filter(|title| {
                !title.trim().is_empty()
                    && !matches!(title.as_str(), "New Chat" | "Untitled chat" | "Resumed Chat")
            })
            .or(prompted_title)
            .unwrap_or_else(|| "Untitled chat".to_string());
        let mut item = existing.unwrap_or_else(|| {
            let now = chrono::Utc::now();
            SessionItem::with_timestamps(id.clone(), title.clone(), now, now)
        });
        item.title = title;
        item.model_name = Some(load.model.clone());
        item.message_count = item.message_count.max(load.page.history_count);
        self.shell.sidebar.add_session(item);
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
        if self.creating_session || self.shell.active_session_id.as_deref() == Some(id.as_str()) {
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
        let was_home = self.show_home;
        let transition = self.capture_session_transition(!was_home);
        self.show_home = false;
        let generation = self.begin_session_navigation();
        if let Some(state) = self.sessions.remove(&id) {
            self.pending_session_transition = Some(transition);
            self.save_active_session();
            self.clear_attachment_motion(cx);
            self.shell.feed = state.feed;
            self.force_feed_resync();
            self.set_editor_content(state.draft, cx);
            self.attachments = state.attachments;
            self.shell.select_session(id);
            self.sync_transport_state();
            cx.notify();
            return;
        }
        // Never hydrated in this process: resume from durable backend history.
        if !self.bridge.is_connected() {
            self.show_home = was_home;
            let message =
                "Not connected to the HEX runtime — cannot resume that session.".to_string();
            if was_home {
                self.home_connection_error = Some(message.clone());
            }
            self.shell.feed.add_system_notice(message);
            cx.notify();
            return;
        }
        self.deferred_session_transition = Some(transition);
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let sid = id.clone();
        cx.spawn(async move |this, cx| {
            let b = bridge.clone();
            let loaded = rt
                .spawn(async move { b.load_session_paged(&sid).await })
                .await;
            let restored = match loaded {
                Ok(Ok(load)) => {
                    let feed = build_restored_feed(&bridge, &rt, &load).await;
                    Some((load, feed))
                }
                _ => None,
            };
            this.update(cx, |view, cx| {
                if view.session_load_generation != generation {
                    return;
                }
                match restored {
                    Some((load, feed)) => view.adopt_loaded_session(load, feed, cx),
                    None => {
                        view.deferred_session_transition = None;
                        view.shell.feed.add_system_notice(format!(
                            "Could not resume session {id}: durable history was unavailable."
                        ));
                        cx.notify();
                    }
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
    fn render_home(
        &mut self,
        theme: &HermesTheme,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Div {
        // Continue list: the five most recent real sessions, with honest
        // running (live turn) and Review (pending approval) state, synced
        // by sync_sidebar_live_state each frame.
        let now = chrono::Utc::now();
        let workspace = self.shell.sidebar.workspace_name.clone();
        let pending_sessions = self.shell.sidebar.review_sessions.clone();
        let rows: Vec<ContinueRow> = {
            let mut recent: Vec<_> = self
                .shell
                .sidebar
                .session_list
                .sessions
                .iter()
                .filter(|s| !s.is_archived)
                .collect();
            recent.sort_by(|a, b| {
                b.is_pinned
                    .cmp(&a.is_pinned)
                    .then_with(|| b.updated_at.cmp(&a.updated_at))
                    .then_with(|| a.id.cmp(&b.id))
            });
            recent
                .into_iter()
                .take(5)
                .map(|s| ContinueRow {
                    id: s.id.clone(),
                    title: s.title.clone(),
                    relative_time: crate::ui::session_list::format_compact_relative_time(
                        s.updated_at,
                        now,
                    ),
                    location_chip: "This Mac",
                    project: workspace.clone(),
                    running: self.running_turns.contains(&s.id),
                    pending_review: pending_sessions.contains(&s.id),
                })
                .collect()
        };

        let (toast_spatial, toast_opacity) = self.home_toast_keys(cx);
        let home_coming_soon = self.home_toast_visual.map(|label| {
            (
                label,
                motion::MotionSystem::value(cx, toast_spatial),
                motion::MotionSystem::value(cx, toast_opacity).clamp(0.0, 1.0),
            )
        });

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
                    .child(div().relative().child(bell_button).child(bell_dot))
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
                        self.render_home_composer(theme, window, cx),
                    )
                    .when_some(self.home_connection_error.clone(), |c, msg| {
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
                    }),
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
                                    view.switch_session(id.clone(), cx);
                                    cx.notify();
                                });
                            }
                        })
                    })),
            );

        // Bottom feature dock: absolute, bottom 28, centered; container
        // bg #191919, border #232323, radius 16, padding 5, gap 2.
        let dock_owner = cx.entity().entity_id();
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
                        let control =
                            motion::ControlMotion::for_identity(dock_owner, "dock_control", name);
                        crate::ui::home::render_dock_item(theme, label, label, glyph, control, &*cx)
                            .on_click(move |_, _, cx| {
                                let _ = this.update(cx, |view, cx| {
                                    view.home_toast_visual = None;
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
            .when_some(home_coming_soon, |c, (label, spatial, opacity)| {
                let rise = motion::REVEAL_RISE * (1.0 - spatial);
                c.child(
                    div()
                        .absolute()
                        .bottom(px(home::NOTICE_BOTTOM + rise))
                        .left_0()
                        .right_0()
                        .flex()
                        .justify_center()
                        .opacity(opacity)
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
                                .child(SharedString::from(format!("{} — coming soon", label))),
                        ),
                )
            })
    }

    /// Home prompt card: the pulsating glow composer with pills and buttons.
    /// Delegates to the shared composer in its Home variant so Home and
    /// Session stay visually identical; submit routes into the session (see
    /// adopt_session).
    fn render_home_composer(
        &mut self,
        theme: &HermesTheme,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Div {
        self.render_composer(theme, window, cx, ComposerVariant::Home)
    }

    fn request_new_chat(&mut self, cx: &mut Context<Self>) {
        if self.creating_session {
            return;
        }
        self.begin_session_navigation();
        self.clear_session_transition(cx);
        self.save_active_session();
        self.shell.active_session_id = None;
        self.shell.sidebar.session_list.selected_session_id = None;
        self.shell.feed = MessageFeed::new();
        self.force_feed_resync();
        self.reset_editor(cx);
        self.attachments.clear();
        self.outgoing_attachments.clear();
        self.pending_home_prompt = None;
        self.show_home = true;
        self.home_connection_error = None;
        self.sync_transport_state();
        cx.notify();
    }

    fn create_session_for_prompt(&mut self, text: String, cx: &mut Context<Self>) {
        if self.creating_session {
            return;
        }
        if !self.bridge.is_connected() {
            self.home_connection_error = Some(
                "Not connected to the HEX runtime — cannot start a new chat.".to_string(),
            );
            cx.notify();
            return;
        }
        self.pending_home_prompt = Some(text);
        self.creating_session = true;
        self.home_connection_error = None;
        let generation = self.begin_session_navigation();
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let cwd = self.workspace.to_string_lossy().to_string();
        let model = self.model_id.clone();
        cx.spawn(async move |this, cx| {
            let res = rt
                .spawn(async move { bridge.new_session_full(Some(&cwd), model.as_deref()).await })
                .await;
            this.update(cx, |view, cx| {
                view.creating_session = false;
                if view.session_load_generation != generation {
                    view.pending_home_prompt = None;
                    return;
                }
                match res {
                    Ok(Ok(resp)) => view.adopt_session(resp, true, cx),
                    Ok(Err(err)) => {
                        view.pending_home_prompt = None;
                        view.home_connection_error = Some(format!("Could not create session: {err}"));
                        cx.notify();
                    }
                    Err(err) => {
                        view.pending_home_prompt = None;
                        view.home_connection_error = Some(format!("Session task aborted: {err}"));
                        cx.notify();
                    }
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
        if text.is_empty() && self.attachments.is_empty() {
            return;
        }
        self.submit_text(text, cx);
    }

    fn submit_text(&mut self, text: String, cx: &mut Context<Self>) {
        if self.active_turn_running() {
            return;
        }
        if self.show_home {
            self.create_session_for_prompt(text, cx);
            return;
        }
        let Some(session_id) = self.shell.active_session_id.clone() else {
            self.shell
                .feed
                .add_system_notice("No active session yet — waiting for the HEX runtime.");
            cx.notify();
            return;
        };

        let attachments = std::mem::take(&mut self.attachments);
        for path in &attachments {
            self.attachment_reveal(cx, path).remove(cx);
        }
        self.outgoing_attachments.clear();
        self.reset_editor(cx);
        self.shell
            .feed
            .add_user_message_with_attachments(text.clone(), attachments.clone());
        // Create the turn's assistant message up front so streamed deltas, tool
        // cards, and the authoritative response all reconcile into one message
        // regardless of which path wins the race.
        let assistant_id = self.shell.feed.start_assistant_streaming();
        self.turn_assistants
            .insert(session_id.clone(), assistant_id);

        if self
            .shell
            .sidebar
            .session_list
            .selected_session()
            .map(|session| {
                matches!(session.title.as_str(), "New Chat" | "Untitled chat")
            })
            .unwrap_or(false)
        {
            let title = session_title_from_prompt(&text);
            self.apply_session_rename(&session_id, &title, cx);
        }
        if let Some(session) = self
            .shell
            .sidebar
            .session_list
            .sessions
            .iter_mut()
            .find(|session| session.id == session_id)
        {
            session.message_count += 1;
        }

        self.running_turns.insert(session_id.clone());
        self.sync_transport_state();
        cx.notify();

        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        let sid = session_id.clone();
        cx.spawn(async move |this, cx| {
            let result = rt
                .spawn(async move {
                    if attachments.is_empty() {
                        bridge.send_prompt(&sid, &text).await
                    } else {
                        bridge
                            .send_prompt_with_attachments(&sid, &text, &attachments)
                            .await
                    }
                })
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
                    apply_authoritative_final(
                        feed,
                        turn_assistant.as_deref(),
                        &text,
                        &self.current_model,
                    );
                    if resp.turn_index.is_some() && resp.response_chars.is_some() {
                        let bridge = self.bridge.clone();
                        let rt = self.rt.clone();
                        let sid = session_id.to_string();
                        let assistant_id = turn_assistant.clone();
                        cx.spawn(async move |this, cx| {
                            let sid2 = sid.clone();
                            let res = rt
                                .spawn(
                                    async move { bridge.fetch_canonical_final(&sid2, &resp).await },
                                )
                                .await;
                            this.update(cx, |view, cx| {
                                view.reconcile_canonical_final(
                                    &sid,
                                    assistant_id.as_deref(),
                                    res,
                                    cx,
                                );
                            })
                            .ok();
                        })
                        .detach();
                    }
                } else if resp.status.eq_ignore_ascii_case("cancelled") {
                    feed.add_system_notice("Turn cancelled.");
                } else {
                    feed.add_system_notice(format!("Turn ended with status \"{}\".", resp.status));
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
                feed.remove_message_at(pos);
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
        let Some(pos) = messages
            .iter()
            .position(|m| matches!(m, MessageItem::Assistant(a) if a.id == assistant_id))
        else {
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
        let approval_session = if let ActiveModal::Approval(modal) = &self.shell.active_modal {
            if modal.request_id == approval_id {
                modal.session_id.clone()
            } else {
                None
            }
        } else {
            None
        };
        match res {
            Ok(Ok(resp)) if resp.status == "applied" && resp.applied != Some(false) => {
                let outcome = resp.outcome.clone().unwrap_or_else(|| match choice {
                    ToolApprovalChoice::Deny => "DENY".to_string(),
                    _ => "APPROVE".to_string(),
                });
                let card_state = if outcome == "DENY" {
                    ApprovalCardState::Denied
                } else {
                    ApprovalCardState::Approved
                };
                self.mark_approval_card(&approval_id, approval_session.as_deref(), card_state);
                self.shell.dismiss_modal();
                let notice = if outcome == "DENY" {
                    "Approval denied; the runtime blocked the action.".to_string()
                } else if let (Some(lease_id), Some(ttl)) = (&resp.lease_id, resp.lease_ttl_seconds)
                {
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

    /// Marks the in-feed approval card(s) with the runtime-confirmed outcome.
    fn mark_approval_card(
        &mut self,
        approval_id: &str,
        session_id: Option<&str>,
        state: ApprovalCardState,
    ) {
        let mut touched = false;
        if let Some(sid) = session_id {
            if self.shell.active_session_id.as_deref() == Some(sid) {
                touched |= self.shell.feed.update_approval_state(approval_id, state);
            } else if let Some(session) = self.sessions.get_mut(sid) {
                touched |= session.feed.update_approval_state(approval_id, state);
            }
        }
        if !touched {
            self.shell.feed.update_approval_state(approval_id, state);
        }
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
            PaletteAction::OpenSettings => self.open_settings_modal(cx),
            PaletteAction::ExportTranscript => self.export_transcript(cx),
            PaletteAction::Custom(label) => {
                self.shell
                    .feed
                    .add_system_notice(format!("{label} is not available in the desktop yet."));
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
                MessageItem::Approval(card) => {
                    md.push_str(&format!(
                        "> Approval `{}` — {}\n\n",
                        card.tool_name,
                        card.state.label()
                    ));
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

    /// B32: open the picker immediately, then populate it from the live backend catalog.
    fn request_model_picker(&mut self, cx: &mut Context<Self>) {
        let search_query = match &self.shell.active_modal {
            ActiveModal::ModelPicker(picker) => picker.search_query.clone(),
            _ => String::new(),
        };
        let session_id = self.shell.active_session_id.clone();
        let sid = session_id.clone().unwrap_or_else(|| "default".to_string());
        self.model_picker_generation = self.model_picker_generation.wrapping_add(1);
        let request_id = self.model_picker_generation;
        let mut picker = ModelPickerModal::loading(
            self.shell.composer.model_pill.model_name.clone(),
            request_id,
            session_id,
            );
        if !search_query.is_empty() {
            picker.set_search(search_query);
        }
        self.shell.show_model_picker(picker);

        if !self.bridge.is_connected() {
            if let Some(picker) = self.model_picker_mut(request_id) {
                picker
                    .set_error("Not connected to the HEX runtime — cannot load the model catalog.");
            }
            cx.notify();
            return;
        }

        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        cx.spawn(async move |this, cx| {
            let res = rt
                .spawn(async move { bridge.list_models(&sid).await })
                .await;
            this.update(cx, |view, cx| {
                if view.model_picker_generation != request_id {
                    return;
                }
                let Some(picker) = view.model_picker_mut(request_id) else {
                    return;
                };
                match res {
                    Ok(Ok(catalog)) => {
                            let entries = catalog
                                .models
                                .iter()
                                .map(ModelEntry::from_catalog)
                                .collect();
                        picker.set_catalog(entries, catalog.error.clone());
                        }
                    Ok(Err(err)) => {
                        picker.set_error(format!("Could not load the model catalog: {err}"))
                    }
                    Err(err) => picker.set_error(format!("Model catalog task aborted: {err}")),
                }
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    fn model_picker_mut(&mut self, request_id: u64) -> Option<&mut ModelPickerModal> {
        if let ActiveModal::ModelPicker(picker) = &mut self.shell.active_modal {
            if picker.request_id == request_id {
                return Some(picker);
            }
        }
        self.shell
            .modal_queue
            .iter_mut()
            .find_map(|modal| match modal {
                ActiveModal::ModelPicker(picker) if picker.request_id == request_id => Some(picker),
                _ => None,
            })
    }

    // ------------------------------------------------------------------
    // Cloud account (cloud/* ACP surface)
    // ------------------------------------------------------------------

    /// Open the account modal over cached status, then refresh live status
    /// and, when signed in, the device roster.
    fn open_account_modal(&mut self, cx: &mut Context<Self>) {
        self.shell.active_modal =
            ActiveModal::Account(AccountModal::new(self.cloud_status.clone()));
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
                        Err(err) => {
                            modal.error = Some(format!("Account status task aborted: {err}"))
                        }
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
                    view.cloud_status
                        .as_ref()
                        .map(|s| s.logged_in)
                        .unwrap_or(false)
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
                    let res = rt
                        .spawn(async move { bridge.cloud_auth_logout().await })
                        .await;
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
            self.shell
                .feed
                .add_system_notice("Not connected to the HEX runtime — cannot switch model.");
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
                            view.shell
                                .composer
                                .model_pill
                                .set_model(view.current_provider.clone(), confirmed.clone());
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
        if self.shell.active_modal.is_open() && matches!(key, "cmd-z" | "cmd-shift-z") {
            let busy = match &self.shell.active_modal {
                ActiveModal::Account(modal) => modal.busy,
                ActiveModal::SessionAction(modal) => modal.busy,
                ActiveModal::Settings(modal) => modal.busy,
                _ => false,
            };
            if busy {
                return true;
            }
            let restored = if key == "cmd-z" {
                self.undo_modal()
            } else {
                self.redo_modal()
            };
            if restored {
                cx.notify();
            }
            return true;
        }
        if matches!(
            key,
            "up" | "arrowup"
                | "down"
                | "arrowdown"
                | "left"
                | "arrowleft"
                | "right"
                | "arrowright"
                | "home"
                | "end"
                | "enter"
                | "tab"
                | "shift-tab"
        ) {
            self.modal_undo_break = true;
        }
        if key == "backspace"
            && self.modal_accepts_text_edit()
            && !matches!(
                self.shell.active_modal,
                ActiveModal::Settings(_) | ActiveModal::SessionSearch(_)
            )
        {
            self.record_modal_edit(TextEditKind::Delete);
        }

        if key == "enter"
            && matches!(
                &self.shell.active_modal,
                ActiveModal::ModelPicker(picker) if picker.error().is_some()
            )
        {
            self.request_model_picker(cx);
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
                } else if matches!(key, "y" | "a" | "n" | "l" | "1" | "2" | "3") {
                    cx.notify();
                    true
                } else {
                    false
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
                    "up" | "down" | "enter" => {
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
                if key == "backspace" {
                    let mut query = picker.search_query.clone();
                    query.pop();
                    picker.set_search(query);
                    cx.notify();
                    return true;
                }
                if let Some(model_id) = picker.handle_key(key) {
                    self.shell.dismiss_modal();
                    self.apply_model_choice(model_id, cx);
                    return true;
                }
                if matches!(
                    key,
                    "enter" | "up" | "arrowup" | "down" | "arrowdown" | "home" | "end"
                ) {
                    cx.notify();
                    true
                } else {
                    false
                }
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
                if matches!(
                    key,
                    "tab" | "shift-tab" | "up" | "arrowup" | "down" | "arrowdown" | "enter"
                ) {
                    cx.notify();
                    true
                } else {
                    false
                }
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
                if matches!(key, "up" | "arrowup" | "down" | "arrowdown" | "enter") {
                    cx.notify();
                    true
                } else {
                    false
                }
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
            ActiveModal::About => {
                if matches!(key, "escape" | "enter") {
                    self.shell.dismiss_modal();
                    cx.notify();
                    true
                } else {
                    false
                }
            }
            ActiveModal::Settings(modal) => {
                if modal.busy {
                    return true;
                }
                if key == "escape" {
                    if modal.editing {
                        modal.cancel_edit();
                    } else {
                        self.shell.dismiss_modal();
                    }
                    cx.notify();
                    return true;
                }
                if let Some(SettingsAction::Save) = modal.handle_key(key) {
                    self.execute_settings_save(cx);
                    return true;
                }
                // Consume navigation keys so they never reach the composer.
                if matches!(
                    key,
                    "up" | "arrowup"
                        | "down"
                        | "arrowdown"
                        | "left"
                        | "arrowleft"
                        | "right"
                        | "arrowright"
                        | "enter"
                        | "backspace"
                        | "home"
                        | "end"
                        | "delete"
                ) {
                    cx.notify();
                    true
                } else {
                    false
                }
            }
            ActiveModal::None => false,
        }
    }

    // ------------------------------------------------------------------
    // Action handlers
    // ------------------------------------------------------------------

    fn on_close_window(&mut self, _: &CloseWindow, window: &mut Window, _: &mut Context<Self>) {
        window.remove_window();
    }

    fn on_about(&mut self, _: &About, _: &mut Window, cx: &mut Context<Self>) {
        self.shell.open_about();
        cx.notify();
    }

    fn on_actual_size(&mut self, _: &ActualSize, window: &mut Window, cx: &mut Context<Self>) {
        window.set_rem_size(px(DEFAULT_REM_SIZE));
        cx.notify();
    }

    fn on_zoom_in(&mut self, _: &ZoomIn, window: &mut Window, cx: &mut Context<Self>) {
        let next = (window.rem_size() + px(ZOOM_STEP)).min(px(MAX_REM_SIZE));
        window.set_rem_size(next);
        cx.notify();
    }

    fn on_zoom_out(&mut self, _: &ZoomOut, window: &mut Window, cx: &mut Context<Self>) {
        let next = (window.rem_size() - px(ZOOM_STEP)).max(px(MIN_REM_SIZE));
        window.set_rem_size(next);
        cx.notify();
    }

    fn on_submit(&mut self, _: &shortcuts::SubmitPrompt, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("enter", cx) || self.shell.active_modal.is_open() {
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
        self.open_settings_modal(cx);
    }

    fn on_cancel_or_dismiss(
        &mut self,
        _: &shortcuts::CancelOrDismiss,
        _: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if self.handle_modal_key("escape", cx) || self.shell.active_modal.is_open() {
            return;
        }
        self.cancel_turn(cx);
    }

    fn on_undo(&mut self, _: &Undo, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("cmd-z", cx) || self.shell.active_modal.is_open() {
            return;
        }
        self.undo_composer(cx);
    }

    fn on_redo(&mut self, _: &Redo, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("cmd-shift-z", cx) || self.shell.active_modal.is_open() {
            return;
        }
        self.redo_composer(cx);
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
        if self.handle_modal_key("cmd-x", cx) || self.shell.active_modal.is_open() {
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

    fn on_select_all(&mut self, _: &shortcuts::SelectAll, _: &mut Window, cx: &mut Context<Self>) {
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
        if self.handle_modal_key("shift-enter", cx) || self.shell.active_modal.is_open() {
            return;
        }
        self.replace_text_in_range(None, "\n", window, cx);
    }

    fn on_backspace(&mut self, _: &Backspace, window: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("backspace", cx) || self.shell.active_modal.is_open() {
            return;
        }
        if self.selected_range.is_empty() {
            self.select_to(self.previous_boundary(self.cursor_offset()), cx);
        }
        self.replace_text_in_range(None, "", window, cx);
    }

    fn on_delete(&mut self, _: &Delete, window: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("delete", cx) || self.shell.active_modal.is_open() {
            return;
        }
        if self.selected_range.is_empty() {
            self.select_to(self.next_boundary(self.cursor_offset()), cx);
        }
        self.replace_text_in_range(None, "", window, cx);
    }

    fn on_left(&mut self, _: &Left, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("left", cx) || self.shell.active_modal.is_open() {
            return;
        }
        if self.selected_range.is_empty() {
            self.move_to(self.previous_boundary(self.cursor_offset()), cx);
        } else {
            self.move_to(self.selected_range.start, cx);
        }
    }

    fn on_right(&mut self, _: &Right, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("right", cx) || self.shell.active_modal.is_open() {
            return;
        }
        if self.selected_range.is_empty() {
            self.move_to(self.next_boundary(self.selected_range.end), cx);
        } else {
            self.move_to(self.selected_range.end, cx);
        }
    }

    fn on_select_left(&mut self, _: &SelectLeft, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("shift-left", cx) || self.shell.active_modal.is_open() {
            return;
        }
        self.select_to(self.previous_boundary(self.cursor_offset()), cx);
    }

    fn on_select_right(&mut self, _: &SelectRight, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("shift-right", cx) || self.shell.active_modal.is_open() {
            return;
        }
        self.select_to(self.next_boundary(self.cursor_offset()), cx);
    }

    fn on_home(&mut self, _: &Home, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("home", cx) || self.shell.active_modal.is_open() {
            return;
        }
        let (line, _) = self.line_col(self.cursor_offset());
        self.move_to(self.line_starts()[line], cx);
    }

    fn on_end(&mut self, _: &End, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("end", cx) || self.shell.active_modal.is_open() {
            return;
        }
        let (line, _) = self.line_col(self.cursor_offset());
        let starts = self.line_starts();
        self.move_to(starts[line] + self.line_len(line), cx);
    }

    fn on_up(&mut self, _: &Up, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("up", cx) || self.shell.active_modal.is_open() {
            return;
        }
        self.move_vertically(true, cx);
    }

    fn on_down(&mut self, _: &Down, _: &mut Window, cx: &mut Context<Self>) {
        if self.handle_modal_key("down", cx) || self.shell.active_modal.is_open() {
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
                    if self.modal_accepts_text_edit() {
                        self.record_modal_edit(TextEditKind::Insert);
                    }
                    let ActiveModal::Palette(palette) = &mut self.shell.active_modal else {
                        return;
                    };
                    let query = format!("{}{}", palette.query, ch);
                    palette.set_query(query);
                    cx.notify();
                    cx.stop_propagation();
                }
            }
            ActiveModal::ModelPicker(_) => {
                if !keystroke.modifiers.platform && !keystroke.modifiers.control {
                    let typed = keystroke.key_char.clone().or_else(|| {
                        (keystroke.key.chars().count() == 1).then(|| keystroke.key.clone())
                    });
                    if let Some(ch) = typed {
                        if self.modal_accepts_text_edit() {
                            self.record_modal_edit(TextEditKind::Insert);
                        }
                        if let ActiveModal::ModelPicker(modal) = &mut self.shell.active_modal {
                            let query = format!("{}{}", modal.search_query, ch);
                            modal.set_search(query);
                            cx.notify();
                            cx.stop_propagation();
                        }
                        return;
                    }
                    if keystroke.key == "backspace" {
                        if self.modal_accepts_text_edit() {
                            self.record_modal_edit(TextEditKind::Delete);
                        }
                        if let ActiveModal::ModelPicker(modal) = &mut self.shell.active_modal {
                            let mut query = modal.search_query.clone();
                            query.pop();
                            modal.set_search(query);
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
                        if self.modal_accepts_text_edit() {
                            self.record_modal_edit(TextEditKind::Insert);
                        }
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
                        if self.modal_accepts_text_edit() {
                            self.record_modal_edit(TextEditKind::Insert);
                        }
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
                        if self.modal_accepts_text_edit() {
                            self.record_modal_edit(TextEditKind::Insert);
                        }
                        if let ActiveModal::SessionSearch(modal) = &mut self.shell.active_modal {
                            modal.push_char(&ch);
                            cx.notify();
                            cx.stop_propagation();
                        }
                        return;
                    }
                    if keystroke.key == "backspace" {
                        if self.modal_accepts_text_edit() {
                            self.record_modal_edit(TextEditKind::Delete);
                        }
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
            ActiveModal::Settings(_) => {
                if !keystroke.modifiers.platform && !keystroke.modifiers.control {
                    let typed = keystroke.key_char.clone().or_else(|| {
                        (keystroke.key.chars().count() == 1).then(|| keystroke.key.clone())
                    });
                    if let Some(ch) = typed {
                        let editing = matches!(
                            &self.shell.active_modal,
                            ActiveModal::Settings(modal) if modal.editing && !modal.busy
                        );
                        if editing {
                            self.record_modal_edit(TextEditKind::Insert);
                        }
                        if let ActiveModal::Settings(modal) = &mut self.shell.active_modal {
                            if modal.editing {
                                modal.push_char(&ch);
                                cx.notify();
                                cx.stop_propagation();
                            }
                        }
                        return;
                    }
                    if matches!(keystroke.key.as_str(), "backspace" | "delete") {
                        let editing = matches!(
                            &self.shell.active_modal,
                            ActiveModal::Settings(modal) if modal.editing && !modal.busy
                        );
                        if editing {
                            self.record_modal_edit(TextEditKind::Delete);
                        }
                        if let ActiveModal::Settings(modal) = &mut self.shell.active_modal {
                            if modal.editing {
                                if keystroke.key == "backspace" {
                                    modal.backspace();
                                } else {
                                    modal.delete();
                                }
                                cx.notify();
                                cx.stop_propagation();
                            }
                            return;
                        }
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

    fn on_mouse_move(&mut self, event: &MouseMoveEvent, _: &mut Window, cx: &mut Context<Self>) {
        if self.is_selecting {
            self.select_to(self.index_for_mouse_position(event.position), cx);
        }
    }

    // ------------------------------------------------------------------
    // Editor state helpers (gpui examples/input.rs pattern)
    // ------------------------------------------------------------------

    fn set_editor_content(&mut self, text: String, cx: &mut Context<Self>) {
        self.composer_undo.clear();
        self.composer_redo.clear();
        self.composer_edit_group = None;
        self.composer_undo_break = false;
        self.content = text;
        let len = self.content.len();
        self.selected_range = len..len;
        self.selection_reversed = false;
        self.marked_range = None;
        cx.notify();
    }

    fn reset_editor(&mut self, cx: &mut Context<Self>) {
        self.set_editor_content(String::new(), cx);
        self.clear_attachment_motion(cx);
    }

    fn attachment_reveal(&self, cx: &Context<Self>, path: &str) -> motion::RevealMotion {
        motion::RevealMotion::for_identity(cx.entity().entity_id(), "attachment_reveal", path)
    }

    fn approval_spring(&self, cx: &Context<Self>, approval_id: &str) -> motion::SpringKey {
        motion::SpringKey::for_slot(
            cx.entity().entity_id(),
            "approval_state",
            motion::slot(approval_id),
        )
    }

    fn sync_approval_motion(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let mut current = HashSet::new();
        for item in &self.shell.feed.messages {
            let MessageItem::Approval(card) = item else {
                continue;
            };
            current.insert(card.approval_id.clone());
            let key = self.approval_spring(cx, &card.approval_id);
            match self.approval_visuals.get(&card.approval_id).copied() {
                None => {
                    self.approval_visuals.insert(
                        card.approval_id.clone(),
                        ApprovalVisual {
                            from_state: card.state,
                            state: card.state,
                            from_color: card.state.color(&HermesTheme::get(cx)),
                        },
                    );
                    motion::MotionSystem::snap(window, cx, key, 1.0, motion::SNAPPY);
                }
                Some(visual) if visual.state != card.state => {
                    let progress = motion::MotionSystem::value(cx, key);
                    let theme = HermesTheme::get(cx);
                    let from_color = visual.color(theme, progress);
                    self.approval_visuals.insert(
                        card.approval_id.clone(),
                        ApprovalVisual {
                            from_state: visual.state,
                            state: card.state,
                            from_color,
                        },
                    );
                    motion::MotionSystem::snap(window, cx, key, 0.0, motion::SNAPPY);
                    motion::MotionSystem::set_opacity_target(window, cx, key, 1.0, motion::SNAPPY);
                }
                Some(_) => {}
            }
        }

        let stale: Vec<String> = self
            .approval_visuals
            .keys()
            .filter(|approval_id| !current.contains(*approval_id))
            .cloned()
            .collect();
        for approval_id in stale {
            let key = self.approval_spring(cx, &approval_id);
            motion::MotionSystem::remove(cx, key);
            self.approval_visuals.remove(&approval_id);
        }
    }

    fn prune_outgoing_attachments(&mut self, cx: &mut Context<Self>) {
        let settled: Vec<String> = self
            .outgoing_attachments
            .iter()
            .filter(|path| {
                let reveal = self.attachment_reveal(cx, path);
                motion::MotionSystem::is_settled(cx, reveal.spatial)
                    && motion::MotionSystem::is_settled(cx, reveal.opacity)
            })
            .cloned()
            .collect();
        for path in &settled {
            self.attachment_reveal(cx, path).remove(cx);
        }
        self.outgoing_attachments
            .retain(|path| !settled.contains(path));
    }

    fn clear_attachment_motion(&mut self, cx: &mut Context<Self>) {
        for path in self
            .attachments
            .iter()
            .chain(self.outgoing_attachments.iter())
        {
            self.attachment_reveal(cx, path).remove(cx);
        }
        self.attachments.clear();
        self.outgoing_attachments.clear();
    }

    fn composer_snapshot(&self) -> ComposerSnapshot {
        ComposerSnapshot {
            content: self.content.clone(),
            selected_range: self.selected_range.clone(),
            selection_reversed: self.selection_reversed,
            marked_range: self.marked_range.clone(),
        }
    }

    fn record_composer_edit(&mut self, range: &Range<usize>, new_text: &str) {
        let kind = if new_text.is_empty() {
            TextEditKind::Delete
        } else if range.is_empty() {
            TextEditKind::Insert
        } else {
            TextEditKind::Replace
        };
        let continues = !self.composer_undo_break
            && new_text.chars().count() <= 1
            && self.composer_edit_group.is_some_and(|(previous, at)| {
                previous == kind && at.elapsed() < Duration::from_millis(750)
            });
        if !continues {
            self.composer_undo.push(self.composer_snapshot());
            if self.composer_undo.len() > MAX_UNDO_HISTORY {
                self.composer_undo.remove(0);
            }
        }
        self.composer_redo.clear();
        self.composer_edit_group = Some((kind, Instant::now()));
        self.composer_undo_break =
            !continues && (kind == TextEditKind::Replace || new_text.chars().count() > 1);
    }

    fn restore_composer_snapshot(&mut self, snapshot: ComposerSnapshot, cx: &mut Context<Self>) {
        self.content = snapshot.content;
        self.selected_range = snapshot.selected_range;
        self.selection_reversed = snapshot.selection_reversed;
        self.marked_range = snapshot.marked_range;
        cx.notify();
    }

    fn undo_composer(&mut self, cx: &mut Context<Self>) {
        if let Some(previous) = self.composer_undo.pop() {
            self.composer_redo.push(self.composer_snapshot());
            self.composer_edit_group = None;
            self.composer_undo_break = true;
            self.restore_composer_snapshot(previous, cx);
        }
    }

    fn redo_composer(&mut self, cx: &mut Context<Self>) {
        if let Some(next) = self.composer_redo.pop() {
            self.composer_undo.push(self.composer_snapshot());
            if self.composer_undo.len() > MAX_UNDO_HISTORY {
                self.composer_undo.remove(0);
            }
            self.composer_edit_group = None;
            self.composer_undo_break = true;
            self.restore_composer_snapshot(next, cx);
        }
    }

    fn modal_accepts_text_edit(&self) -> bool {
        match &self.shell.active_modal {
            ActiveModal::Palette(_)
            | ActiveModal::ModelPicker(_)
            | ActiveModal::SessionSearch(_) => true,
            ActiveModal::Account(modal) => {
                !modal.busy && !matches!(modal.mode, AccountMode::Devices)
            }
            ActiveModal::SessionAction(modal) => {
                !modal.busy && matches!(modal.mode, SessionActionMode::Rename)
            }
            ActiveModal::Settings(modal) => !modal.busy && modal.editing,
            _ => false,
        }
    }

    fn clear_modal_history(&mut self) {
        self.modal_undo.clear();
        self.modal_redo.clear();
        self.modal_edit_group = None;
        self.modal_undo_break = true;
    }

    fn record_modal_edit(&mut self, kind: TextEditKind) {
        if self.modal_undo.last().is_some_and(|previous| {
            std::mem::discriminant(previous) != std::mem::discriminant(&self.shell.active_modal)
        }) {
            self.clear_modal_history();
        }
        let continues = !self.modal_undo_break
            && self.modal_edit_group.is_some_and(|(previous, at)| {
                previous == kind && at.elapsed() < Duration::from_millis(750)
            });
        if !continues {
            self.modal_undo.push(self.shell.active_modal.clone());
            if self.modal_undo.len() > MAX_UNDO_HISTORY {
                self.modal_undo.remove(0);
            }
        }
        self.modal_redo.clear();
        self.modal_edit_group = Some((kind, Instant::now()));
        self.modal_undo_break = false;
    }

    fn undo_modal(&mut self) -> bool {
        if self.modal_undo.last().is_some_and(|previous| {
            std::mem::discriminant(previous) != std::mem::discriminant(&self.shell.active_modal)
        }) {
            self.clear_modal_history();
            return false;
        }
        let Some(previous) = self.modal_undo.pop() else {
            return false;
        };
        self.modal_redo.push(self.shell.active_modal.clone());
        self.modal_edit_group = None;
        self.modal_undo_break = true;
        self.shell.active_modal = previous;
        true
    }

    fn redo_modal(&mut self) -> bool {
        if self.modal_redo.last().is_some_and(|next| {
            std::mem::discriminant(next) != std::mem::discriminant(&self.shell.active_modal)
        }) {
            self.clear_modal_history();
            return false;
        }
        let Some(next) = self.modal_redo.pop() else {
            return false;
        };
        self.modal_undo.push(self.shell.active_modal.clone());
        if self.modal_undo.len() > MAX_UNDO_HISTORY {
            self.modal_undo.remove(0);
        }
        self.modal_edit_group = None;
        self.modal_undo_break = true;
        self.shell.active_modal = next;
        true
    }

    /// Opens the native file picker and stages the picked files as prompt
    /// attachments (deduped; the runtime enforces its own count/size budget).
    fn open_file_picker(&mut self, _: &ClickEvent, window: &mut Window, cx: &mut Context<Self>) {
        let picked = rfd::FileDialog::new()
            .set_title("Attach files")
            .pick_files();
        if let Some(paths) = picked {
            for path in paths {
                self.stage_attachment(path.to_string_lossy().to_string(), window, cx);
            }
            cx.notify();
        }
    }

    fn stage_attachment(&mut self, path: String, window: &mut Window, cx: &mut Context<Self>) {
        if self.attachments.contains(&path) {
            return;
        }
        self.outgoing_attachments
            .retain(|outgoing| outgoing != &path);
        self.attachments.push(path.clone());
        self.attachment_reveal(cx, &path).enter(window, cx);
        cx.notify();
    }

    fn remove_attachment(&mut self, path: &str, window: &mut Window, cx: &mut Context<Self>) {
        let Some(index) = self
            .attachments
            .iter()
            .position(|attached| attached == path)
        else {
            return;
        };
        self.attachments.remove(index);
        if !self
            .outgoing_attachments
            .iter()
            .any(|outgoing| outgoing == path)
        {
            self.outgoing_attachments.push(path.to_string());
        }
        self.attachment_reveal(cx, path).exit(window, cx);
        cx.notify();
    }

    fn move_to(&mut self, offset: usize, cx: &mut Context<Self>) {
        self.composer_undo_break = true;
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
        self.composer_undo_break = true;
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

    fn toggle_reasoning(
        &mut self,
        assistant_id: &str,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if let Some(assistant) = find_assistant_mut(&mut self.shell.feed, assistant_id) {
            if let Some(reasoning) = &mut assistant.reasoning {
                reasoning.toggle();
                motion::RevealMotion::for_identity(
                    cx.entity().entity_id(),
                    "reasoning_reveal",
                    &format!("reasoning_{assistant_id}"),
                )
                .set(window, cx, reasoning.is_expanded);
                self.shell.feed.mark_changed();
                cx.notify();
            }
        }
    }

    fn toggle_tool_input(
        &mut self,
        assistant_id: &str,
        call_id: &str,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if let Some(tool) =
            find_assistant_mut(&mut self.shell.feed, assistant_id).and_then(|assistant| {
                assistant
                    .tool_calls
                    .iter_mut()
                    .find(|tool| tool.call_id == call_id)
            })
        {
            tool.toggle_input();
            motion::RevealMotion::for_identity(
                cx.entity().entity_id(),
                "tool_reveal",
                &format!("{call_id}:input"),
            )
            .set(window, cx, tool.input_expanded);
            self.shell.feed.mark_changed();
            cx.notify();
        }
    }

    fn toggle_tool_output(
        &mut self,
        assistant_id: &str,
        call_id: &str,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        if let Some(tool) =
            find_assistant_mut(&mut self.shell.feed, assistant_id).and_then(|assistant| {
                assistant
                    .tool_calls
                    .iter_mut()
                    .find(|tool| tool.call_id == call_id)
            })
        {
            tool.toggle_output();
            motion::RevealMotion::for_identity(
                cx.entity().entity_id(),
                "tool_reveal",
                &format!("{call_id}:output"),
            )
            .set(window, cx, tool.output_expanded);
            self.shell.feed.mark_changed();
            cx.notify();
        }
    }

    /// Expands or collapses the Changes card on an assistant message (P4).
    fn toggle_changes(&mut self, assistant_id: &str, window: &mut Window, cx: &mut Context<Self>) {
        if let Some(assistant) = find_assistant_mut(&mut self.shell.feed, assistant_id) {
            assistant.changes_expanded = !assistant.changes_expanded;
            motion::RevealMotion::for_identity(
                cx.entity().entity_id(),
                "changes_reveal",
                &format!("changes_{assistant_id}"),
            )
            .set(window, cx, assistant.changes_expanded);
            self.shell.feed.mark_changed();
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
            Some(format!(
                "{} tokens",
                format_token_number(gauge.current_tokens)
            ))
        }
    }

    /// Per-assistant handlers for the session feed: Copy, Retry, Changes,
    /// and the reasoning/tool disclosures. Built per render because they
    /// capture message state and the weak entity handle.
    fn action_bar_handlers(&self, cx: &mut Context<Self>) -> HashMap<String, ActionBarHandlers> {
        let mut bars: HashMap<String, ActionBarHandlers> = HashMap::new();
        for item in &self.shell.feed.messages {
            if let MessageItem::Assistant(view) = item {
                let weak = cx.entity().downgrade();
                let weak_retry = weak.clone();
                let weak_changes = weak.clone();
                let weak_reasoning = weak.clone();
                let weak_tool_input = weak.clone();
                let weak_tool_output = weak;
                let retry_id = view.id.clone();
                let changes_id = view.id.clone();
                let reasoning_id = view.id.clone();
                let tool_input_id = view.id.clone();
                let tool_output_id = view.id.clone();
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
                        changes: Some(Rc::new(move |_, window, cx| {
                            weak_changes
                                .update(cx, |this, cx| this.toggle_changes(&changes_id, window, cx))
                                .ok();
                        })),
                        reasoning: Some(Rc::new(move |_, window, cx| {
                            weak_reasoning
                                .update(cx, |this, cx| {
                                    this.toggle_reasoning(&reasoning_id, window, cx)
                                })
                                .ok();
                        })),
                        tool_input: Some(Rc::new(move |call_id, _, window, cx| {
                            weak_tool_input
                                .update(cx, |this, cx| {
                                    this.toggle_tool_input(&tool_input_id, call_id, window, cx)
                                })
                                .ok();
                        })),
                        tool_output: Some(Rc::new(move |call_id, _, window, cx| {
                            weak_tool_output
                                .update(cx, |this, cx| {
                                    this.toggle_tool_output(&tool_output_id, call_id, window, cx)
                                })
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
        window: &mut Window,
        cx: &mut Context<Self>,
        variant: ComposerVariant,
    ) -> Div {
        self.prune_outgoing_attachments(cx);

        let running = self.active_turn_running();
        let can_send =
            (!self.content.trim().is_empty() || !self.attachments.is_empty()) && !running;
        let send_btn = SendButton::new(running, can_send);
        let session = variant == ComposerVariant::Session;
        let placeholder = if session {
            SESSION_PLACEHOLDER.to_string()
        } else {
            self.shell.composer.placeholder.clone()
        };

        let mut chips_row = div().w_full().flex().flex_wrap().gap(px(6.0)).pb(px(8.0));
        for path in self
            .attachments
            .iter()
            .chain(self.outgoing_attachments.iter())
        {
            let path = path.clone();
            let outgoing = self.outgoing_attachments.contains(&path);
            let reveal = self.attachment_reveal(cx, &path);
            if !outgoing {
                reveal.ensure(window, cx, 1.0);
            }
            let selector = SharedString::from(format!(
                "attach_chip_{}",
                motion::slot(&format!("attachment_reveal:{path}"))
            ));
            let name: String = path
                .rsplit(['/', '\\'])
                .next()
                .filter(|s| !s.is_empty())
                .unwrap_or(&path)
                .to_string();
            let mut chip = div()
                .id(selector.clone())
                .debug_selector(move || selector.to_string())
                .flex()
                .items_center()
                .gap(px(4.0))
                .px(px(8.0))
                .py(px(3.0))
                .rounded(px(6.0))
                .bg(theme.mockup_chip)
                .border_1()
                .border_color(theme.mockup_border);
            if !outgoing {
                chip = chip
                    .cursor_pointer()
                    .on_click(cx.listener(move |this, _, window, cx| {
                        this.remove_attachment(&path, window, cx);
                    }));
            }
            chips_row = chips_row.child(
                reveal
                    .values(cx)
                    .apply(chip)
                    .child(
                        div()
                            .font_family(theme.font_mono()[0].clone())
                            .text_size(px(MockupTypeScale::HINT_SIZE))
                            .text_color(theme.mockup_text_muted)
                            .child(name),
                    )
                    .child(
                        div()
                            .text_size(px(MockupTypeScale::HINT_SIZE))
                            .text_color(theme.mockup_text_dim)
                            .child("✕"),
                    ),
            );
        }
        let attachment_chips = (!self.attachments.is_empty()
            || !self.outgoing_attachments.is_empty())
        .then_some(chips_row);

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

        let owner = cx.entity().entity_id();
        let attach = motion::ControlMotion::for_identity(owner, "composer_control", "attach");
        let model = motion::ControlMotion::for_identity(owner, "composer_control", "model");
        let send = motion::ControlMotion::for_identity(owner, "composer_control", "send");
        let attach_values = attach.values(cx);
        let model_values = model.values(cx);
        let send_values = send.values(cx);
        let send_background: Hsla = if running {
            theme.rose_fg().into()
        } else {
            rgb(0x2a2a2a).into()
        };

        // Toolbar (mockup gap 8): attach, model pill, spacer, send. Attach
        // returned in DX4 with a real file picker; Auto/mic stay out — they
        // still do nothing honest.
        let toolbar = div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .gap(px(8.0))
            .child(
                motion::hover_feedback(
                    motion::press_feedback(
                        div()
                            .id("composer_attach")
                            .debug_selector(|| "composer_attach".into()),
                        attach,
                        cx,
                    ),
                    attach,
                )
                .on_click(cx.listener(Self::open_file_picker))
                .child(mockup_icon_button_motion(
                    theme,
                    "attach",
                    16.0,
                    theme.mockup_text_muted,
                    None,
                    attach_values,
                )),
            )
            .child(
                motion::hover_feedback(
                    motion::press_feedback(
                        div()
                            .id("model_pill_button")
                            .debug_selector(|| "model_pill_button".into()),
                        model,
                        cx,
                    ),
                    model,
                )
                .on_click(cx.listener(|this, _, _, cx| {
                    this.request_model_picker(cx);
                }))
                .child(
                    mockup_pill_base(theme)
                        .bg(model_values
                            .color(theme.mockup_pill.into(), theme.mockup_pill_hover.into()))
                        .child(self.shell.composer.model_pill.model_name.clone())
                        .child(mockup_pill_chevron()),
                ),
            )
            .child(div().flex_1())
            .child(
                motion::hover_feedback(
                    motion::press_feedback(
                        div()
                            .id("composer_send")
                            .debug_selector(|| "composer_send".into()),
                        send,
                        cx,
                    ),
                    send,
                )
                .on_click(cx.listener(|this, _, _, cx| {
                    if this.active_turn_running() {
                        this.cancel_turn(cx);
                    } else {
                        this.submit_prompt(cx);
                    }
                }))
                .child(send_btn.render_with_background(
                    send_values.color(send_background, theme.mockup_pill_hover.into()),
                )),
            );

        let mut input_area = div().w_full().flex().flex_col();
        if let Some(chips) = attachment_chips {
            input_area = input_area.child(chips);
        }
        input_area = input_area.child(input_row);

        let column = div()
            .w_full()
            .flex()
            .flex_col()
            .gap(px(COMPOSER_COLUMN_GAP))
            .child(input_area)
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

    fn render_main_pane(
        &mut self,
        theme: &HermesTheme,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Div {
        // Non-Chat segments show a Coming soon empty state instead of the feed.
        let coming_soon_pane = self.shell.active_view != ShellView::Chat;
        // Structural changes remeasure the list. While a turn streams, only the
        // active assistant row grows, so keep every other row's measurement.
        let item_count = self.shell.feed.len();
        let feed_revision = self.shell.feed.revision();
        if item_count != self.feed_synced_count {
            self.feed_list.reset(item_count);
            self.feed_synced_count = item_count;
            self.feed_synced_revision = feed_revision;
        } else if feed_revision != self.feed_synced_revision {
            if self.shell.feed.is_streaming {
                if let Some(index) = self.shell.feed.streaming_row_index() {
                    self.feed_list.splice(index..index + 1, 1);
                } else {
                    self.feed_list.reset(item_count);
                }
            } else {
                self.feed_list.reset(item_count);
            }
            self.feed_synced_revision = feed_revision;
        }
        let bars = if coming_soon_pane {
            HashMap::new()
        } else {
            self.action_bar_handlers(cx)
        };
        let motion_owner = cx.entity().entity_id();
        let feed = self.shell.feed.render_scrolled(
            theme,
            &self.feed_list,
            bars,
            self.action_token_count(),
            cx,
            motion_owner,
            &self.approval_visuals,
        );
        let feed = if coming_soon_pane {
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
            let incoming_values = self
                .session_incoming_motion
                .map(|motion| motion.values(&*cx))
                .unwrap_or(motion::RevealValues {
                    spatial: 1.0,
                    opacity: 1.0,
                });
            let mut layer = div()
                .relative()
                .flex_1()
                .min_h_0()
                .w_full()
                .flex()
                .flex_col()
                .overflow_hidden();
            layer = layer.child(
                incoming_values.apply(feed.debug_selector(|| "session_feed_incoming".to_string())),
            );
            if let Some(outgoing) = self.outgoing_session_feed.as_mut() {
                let outgoing_values = self
                    .session_outgoing_motion
                    .expect("outgoing feed has a motion layer")
                    .values(&*cx);
                let outgoing_feed = outgoing.render_scrolled(
                    theme,
                    &self.outgoing_feed_list,
                    HashMap::new(),
                    None,
                    cx,
                    motion_owner,
                    &HashMap::new(),
                );
                let rise = motion::REVEAL_RISE * (1.0 - outgoing_values.spatial);
                layer = layer.child(
                    div()
                        .absolute()
                        .left_0()
                        .right_0()
                        .top(px(rise))
                        .bottom_0()
                        .opacity(outgoing_values.opacity.clamp(0.0, 1.0))
                        .child(
                            outgoing_feed.debug_selector(|| "session_feed_outgoing".to_string()),
                        ),
                );
            }
            layer
        };

        div()
            .flex()
            .flex_col()
            .bg(theme.mockup_canvas)
            // Header h60, padding 0 20px: hamburger button left; the live
            // Chat view indicator is absolute-centered per the mockup; right
            // gap 4: Device / Team / More buttons.
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
                            .child(self.render_chat_header_indicator(theme)),
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
            .children(self.render_update_banner(theme, cx))
            .child(feed)
            .when(!coming_soon_pane, |c| {
                c.child(self.render_composer(theme, window, cx, ComposerVariant::Session))
            })
    }

    /// Static Chat view indicator: bg `#191919`, border `#232323`, radius 12,
    /// padding 3; active 32×32 icon button with radius 8 and bg `#262626`.
    /// Files, Browser, and Terminal stay hidden until their panes are functional.
    fn render_chat_header_indicator(&self, theme: &HermesTheme) -> Div {
        div()
            .flex()
            .flex_row()
            .items_center()
            .p(px(crate::ui::shell::SEGMENTED_PADDING))
            .rounded(px(crate::ui::shell::SEGMENTED_RADIUS))
            .bg(theme.mockup_dock)
            .border_1()
            .border_color(theme.mockup_dock_border)
            .child(
                div()
                    .id("segment_chat")
                    .debug_selector(|| "segment_chat".to_string())
                    .size(px(crate::ui::shell::SEGMENT_BUTTON))
                    .flex()
                    .items_center()
                    .justify_center()
                    .rounded(px(crate::ui::shell::SEGMENT_BUTTON_RADIUS))
                    .text_color(theme.mockup_text_muted)
                    .bg(theme.mockup_seg_active)
                    .child(crate::ui::icon_svg(
                        "chat",
                        crate::ui::shell::SEGMENT_ICON,
                    )),
            )
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

    /// DX3: opens the editable settings modal and hydrates it from the
    /// runtime via `config/get`. Offline: the modal states that honestly
    /// instead of inventing values.
    fn open_settings_modal(&mut self, cx: &mut Context<Self>) {
        if matches!(self.shell.active_modal, ActiveModal::Settings(_)) {
            self.shell.dismiss_modal();
            cx.notify();
            return;
        }
        let mut modal = SettingsModal::loading();
        if !self.bridge.is_connected() {
            modal.mark_load_failed("Runtime offline — settings unavailable until HEX connects.");
            self.shell.active_modal = ActiveModal::Settings(modal);
            cx.notify();
            return;
        }
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        cx.spawn(async move |this, cx| {
            let res = rt.spawn(async move { bridge.get_config().await }).await;
            this.update(cx, |view, cx| {
                let load_error = match &res {
                    Ok(Err(err)) => Some(format!("config/get failed: {err}")),
                    Err(err) => Some(format!("config task aborted: {err}")),
                    Ok(Ok(_)) => None,
                };
                if let Some(error) = &load_error {
                    view.shell.feed.add_system_notice(error.clone());
                }
                if let ActiveModal::Settings(modal) = &mut view.shell.active_modal {
                    match res {
                        Ok(Ok(cfg)) => modal.apply_view(&cfg),
                        _ => modal.mark_load_failed(load_error.unwrap_or_else(|| {
                            "Could not read settings from the runtime.".to_string()
                        })),
                    }
                }
                cx.notify();
            })
            .ok();
        })
        .detach();
        self.shell.active_modal = ActiveModal::Settings(modal);
        cx.notify();
    }

    /// DX3: writes the modal's dirty diff via `config/set`; the fresh view in
    /// the response re-bases the modal, failures keep the operator's edits.
    fn execute_settings_save(&mut self, cx: &mut Context<Self>) {
        let ActiveModal::Settings(modal) = &self.shell.active_modal else {
            return;
        };
        let updates = modal.dirty_updates();
        if !updates.as_object().is_some_and(|m| !m.is_empty()) {
            return;
        }
        let bridge = self.bridge.clone();
        let rt = self.rt.clone();
        cx.spawn(async move |this, cx| {
            let res = rt
                .spawn(async move { bridge.set_config(updates).await })
                .await;
            this.update(cx, |view, cx| {
                if let ActiveModal::Settings(modal) = &mut view.shell.active_modal {
                    match res {
                        Ok(Ok(saved)) => {
                            modal.apply_saved(&saved);
                            view.clear_modal_history();
                        }
                        Ok(Err(err)) => {
                            modal.busy = false;
                            modal.error = Some(format!("Save failed: {err}"));
                        }
                        Err(err) => {
                            modal.busy = false;
                            modal.error = Some(format!("Save task aborted: {err}"));
                        }
                    }
                }
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    /// Search results matching the modal query, from the same
    /// `filter_sessions` the sidebar renders. The sidebar's own query stays
    /// empty — the modal owns the search text while it is open.
    fn search_results(
        &mut self,
        modal: &SessionSearchModal,
    ) -> Vec<crate::ui::session_list::SessionItem> {
        self.shell
            .sidebar
            .session_list
            .set_search_query(modal.query.clone());
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
            self.begin_session_navigation();
            self.shell.feed = MessageFeed::new();
            self.shell.active_session_id = None;
            self.feed_synced_count = 0;
            self.feed_synced_revision = 0;
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
            self.begin_session_navigation();
            // Archiving the visible chat returns to Home; the session stays
            // resumable from the archived section.
            self.shell.feed = MessageFeed::new();
            self.shell.active_session_id = None;
            self.feed_synced_count = 0;
            self.feed_synced_revision = 0;
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
                rt.spawn(async move { bridge.pin_session(&sid, value).await })
                    .await
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

    fn update_banner_keys(&self, cx: &Context<Self>) -> (motion::SpringKey, motion::SpringKey) {
        let owner = cx.entity().entity_id();
        (
            motion::SpringKey::new(owner, "update_banner_spatial"),
            motion::SpringKey::new(owner, "update_banner_opacity"),
        )
    }

    fn sync_update_banner_motion(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let (spatial, opacity) = self.update_banner_keys(cx);
        let current = self
            .update_banner_line()
            .map(|(line, mandatory)| UpdateBannerVisual { line, mandatory });
        if let Some(visual) = current {
            if self.update_banner_visual.as_ref() != Some(&visual) {
                self.update_banner_visual = Some(visual);
                if !motion::MotionSystem::has(&*cx, spatial)
                    || (motion::MotionSystem::is_settled(&*cx, spatial)
                        && motion::MotionSystem::is_settled(&*cx, opacity))
                {
                    motion::MotionSystem::snap(window, cx, spatial, 0.0, motion::BOUNCY);
                    motion::MotionSystem::snap(window, cx, opacity, 0.0, motion::BOUNCY);
                }
            }
            motion::MotionSystem::set_target(window, cx, spatial, 1.0, motion::BOUNCY);
            motion::MotionSystem::set_opacity_target(window, cx, opacity, 1.0, motion::BOUNCY);
        } else if self.update_banner_visual.is_some() {
            motion::MotionSystem::set_target(window, cx, spatial, 0.0, motion::BOUNCY);
            motion::MotionSystem::set_opacity_target(window, cx, opacity, 0.0, motion::BOUNCY);
            if motion::MotionSystem::is_settled(&*cx, spatial)
                && motion::MotionSystem::is_settled(&*cx, opacity)
            {
                motion::MotionSystem::remove(cx, spatial);
                motion::MotionSystem::remove(cx, opacity);
                self.update_banner_visual = None;
            }
        }
    }

    /// Signed-update pill between the header and the feed (absent when up to
    /// date or signed out). Mandatory/incompatible plans use the deletion
    /// tint; eligible updates use the addition tint.
    fn render_update_banner(&self, theme: &HermesTheme, cx: &Context<Self>) -> Option<AnyElement> {
        let visual = self.update_banner_visual.as_ref()?;
        let (spatial, opacity) = self.update_banner_keys(cx);
        let rise = 10.0 * (1.0 - motion::MotionSystem::value(cx, spatial));
        let alpha = motion::MotionSystem::value(cx, opacity).clamp(0.0, 1.0);
        let (bg, fg) = if visual.mandatory {
            (theme.mockup_diff_del_bg, theme.rose_fg())
        } else {
            (theme.mockup_diff_add_bg, theme.mockup_diff_add_fg)
        };
        Some(
            div()
                .mx(px(crate::ui::shell::HEADER_PADDING_X))
                .mt(px(6.0 + rise))
                .opacity(alpha)
                .child(
                    div()
                        .rounded(px(8.0))
                        .bg(bg)
                        .px_3()
                        .py(px(6.0))
                        .text_size(px(MockupTypeScale::HINT_SIZE))
                        .text_color(fg)
                        .child(SharedString::from(if visual.mandatory {
                            format!(
                                "Update required — {}. Run `hex update`, then restart.",
                                visual.line
                            )
                        } else {
                            format!(
                                "Update available — {}. Run `hex update` to apply.",
                                visual.line
                            )
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

    fn sidebar_motion_keys(&self, cx: &Context<Self>) -> (motion::SpringKey, motion::SpringKey) {
        let owner = cx.entity().entity_id();
        (
            motion::SpringKey::new(owner, "sidebar_width"),
            motion::SpringKey::new(owner, "sidebar_opacity"),
        )
    }

    fn sync_sidebar_motion(&self, window: &mut Window, cx: &mut Context<Self>) {
        let (width, opacity) = self.sidebar_motion_keys(cx);
        let target = f32::from(!self.shell.sidebar_collapsed);
        if !motion::MotionSystem::has(&*cx, width) {
            motion::MotionSystem::snap(window, cx, width, target, motion::SMOOTH);
            motion::MotionSystem::snap(window, cx, opacity, target, motion::SMOOTH);
        } else {
            motion::MotionSystem::set_target(window, cx, width, target, motion::SMOOTH);
            motion::MotionSystem::set_opacity_target(window, cx, opacity, target, motion::SMOOTH);
        }
    }

    fn home_toast_keys(&self, cx: &Context<Self>) -> (motion::SpringKey, motion::SpringKey) {
        let owner = cx.entity().entity_id();
        (
            motion::SpringKey::new(owner, "home_toast_spatial"),
            motion::SpringKey::new(owner, "home_toast_opacity"),
        )
    }

    fn sync_home_toast_motion(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let (spatial, opacity) = self.home_toast_keys(cx);
        if let Some(label) = self.home_coming_soon {
            if self.home_toast_visual != Some(label) {
                self.home_toast_visual = Some(label);
                if !motion::MotionSystem::has(&*cx, spatial)
                    || (motion::MotionSystem::is_settled(&*cx, spatial)
                        && motion::MotionSystem::is_settled(&*cx, opacity))
                {
                    motion::MotionSystem::snap(window, cx, spatial, 0.0, motion::BOUNCY);
                    motion::MotionSystem::snap(window, cx, opacity, 0.0, motion::BOUNCY);
                }
            }
            motion::MotionSystem::set_target(window, cx, spatial, 1.0, motion::BOUNCY);
            motion::MotionSystem::set_opacity_target(window, cx, opacity, 1.0, motion::BOUNCY);
        } else if self.home_toast_visual.is_some() {
            motion::MotionSystem::set_target(window, cx, spatial, 0.0, motion::BOUNCY);
            motion::MotionSystem::set_opacity_target(window, cx, opacity, 0.0, motion::BOUNCY);
            if motion::MotionSystem::is_settled(&*cx, spatial)
                && motion::MotionSystem::is_settled(&*cx, opacity)
            {
                motion::MotionSystem::remove(cx, spatial);
                motion::MotionSystem::remove(cx, opacity);
                self.home_toast_visual = None;
            }
        }
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
        self.sync_sidebar_motion(window, cx);
        self.sync_home_toast_motion(window, cx);
        self.sync_update_banner_motion(window, cx);
        if self.show_home {
            self.pending_session_transition = None;
        } else {
            self.sync_session_transition(window, cx);
        }
        self.sync_approval_motion(window, cx);
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
            let modal_open = self.shell.active_modal.is_open();
            if modal_open != self.last_modal_open {
                self.clear_modal_history();
            }
            if modal_open {
                if !self.last_modal_open {
                    self.modal_focus_handle.focus(window);
                }
            } else if self.last_modal_open || !self.focus_handle.is_focused(window) {
                self.focus_handle.focus(window);
            }
            self.last_modal_open = modal_open;

            let home = self
                .render_home(&theme, window, cx)
                .key_context("ChatView")
                .on_action(cx.listener(Self::on_submit))
                .on_action(cx.listener(Self::on_new_chat))
                .on_action(cx.listener(Self::on_close_window))
                .on_action(cx.listener(Self::on_about))
                .on_action(cx.listener(Self::on_undo))
                .on_action(cx.listener(Self::on_redo))
                .on_action(cx.listener(Self::on_actual_size))
                .on_action(cx.listener(Self::on_zoom_in))
                .on_action(cx.listener(Self::on_zoom_out))
                .on_action(cx.listener(Self::on_toggle_palette))
                .on_action(cx.listener(Self::on_toggle_sidebar))
                .on_action(cx.listener(Self::on_open_settings))
                .on_action(cx.listener(Self::on_cancel_or_dismiss))
                .on_action(cx.listener(Self::on_copy))
                .on_action(cx.listener(Self::on_cut))
                .on_action(cx.listener(Self::on_paste))
                .on_action(cx.listener(Self::on_select_all))
                .on_key_down(cx.listener(Self::on_key_down));

            let modal = self.render_modal_layer(&theme, window, cx);
            return if let Some(modal) = modal {
                home.child(modal).into_any_element()
            } else {
                home.into_any_element()
            };
        }

        // Predictable focus: opening a modal moves focus to it, closing returns
        // focus to the composer. Background activity never steals focus.
        let modal_open = self.shell.active_modal.is_open();
        if modal_open != self.last_modal_open {
            self.clear_modal_history();
        }
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
            .on_action(cx.listener(Self::on_close_window))
            .on_action(cx.listener(Self::on_about))
            .on_action(cx.listener(Self::on_undo))
            .on_action(cx.listener(Self::on_redo))
            .on_action(cx.listener(Self::on_actual_size))
            .on_action(cx.listener(Self::on_zoom_in))
            .on_action(cx.listener(Self::on_zoom_out))
            .on_action(cx.listener(Self::on_toggle_palette))
            .on_action(cx.listener(Self::on_toggle_sidebar))
            .on_action(cx.listener(Self::on_open_settings))
            .on_action(cx.listener(Self::on_cancel_or_dismiss))
            .on_action(cx.listener(Self::on_copy))
            .on_action(cx.listener(Self::on_cut))
            .on_action(cx.listener(Self::on_paste))
            .on_action(cx.listener(Self::on_select_all))
            .on_key_down(cx.listener(Self::on_key_down));

        let (sidebar_width, sidebar_opacity) = self.sidebar_motion_keys(cx);
        let sidebar_progress = motion::MotionSystem::value(cx, sidebar_width).clamp(0.0, 1.0);
        let sidebar_alpha = motion::MotionSystem::value(cx, sidebar_opacity).clamp(0.0, 1.0);
        if !self.shell.sidebar_collapsed || sidebar_progress > 0.0 || sidebar_alpha > 0.0 {
            root = root.child(
                div()
                    .debug_selector(|| "animated_sidebar".to_string())
                    .w(px(SIDEBAR_WIDTH * sidebar_progress))
                    .h_full()
                    .flex_none()
                    .overflow_hidden()
                    .opacity(sidebar_alpha)
                    .child(
                        self.shell
                            .sidebar
                            .render_with_actions(&theme, &sidebar_handlers),
                    ),
            );
        }

        root = root.child(self.render_main_pane(&theme, window, cx));

        if let Some(modal) = self.render_modal_layer(&theme, window, cx) {
            root = root.child(modal);
        }

        root.into_any_element()
    }
}

impl ChatView {
    fn modal_spring_keys(
        cx: &Context<Self>,
        instance: u64,
    ) -> (motion::SpringKey, motion::SpringKey) {
        let owner = cx.entity().entity_id();
        (
            motion::SpringKey::for_slot(owner, "modal_card", instance),
            motion::SpringKey::for_slot(owner, "modal_scrim", instance),
        )
    }

    fn set_modal_target(
        &mut self,
        window: &mut Window,
        cx: &mut Context<Self>,
        instance: u64,
        target: f32,
    ) {
        let (card, scrim) = Self::modal_spring_keys(cx, instance);
        motion::MotionSystem::set_target(window, cx, card, target, motion::SMOOTH);
        motion::MotionSystem::set_opacity_target(window, cx, scrim, target, motion::SMOOTH);
    }

    fn sync_modal_motion(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let active = self.shell.active_modal.clone();

        if let Some((instance, current)) = self.rendered_modal.as_ref() {
            if matches!(&active, ActiveModal::None) {
                let outgoing = self.rendered_modal.take().expect("rendered modal present");
                self.set_modal_target(window, cx, outgoing.0, 0.0);
                self.outgoing_modals.push(outgoing);
            } else if !current.same_instance(&active) {
                let outgoing = self.rendered_modal.take().expect("rendered modal present");
                self.set_modal_target(window, cx, outgoing.0, 0.0);
                self.outgoing_modals.push(outgoing);
                self.next_modal_instance += 1;
                let instance = self.next_modal_instance;
                self.rendered_modal = Some((instance, active));
                self.set_modal_target(window, cx, instance, 1.0);
            } else {
                self.rendered_modal = Some((*instance, active));
            }
        } else if !matches!(active, ActiveModal::None) {
            if let Some(index) = self
                .outgoing_modals
                .iter()
                .rposition(|(_, outgoing)| outgoing.same_instance(&active))
            {
                let (instance, _) = self.outgoing_modals.remove(index);
                self.rendered_modal = Some((instance, active));
                self.set_modal_target(window, cx, instance, 1.0);
            } else {
                self.next_modal_instance += 1;
                let instance = self.next_modal_instance;
                self.rendered_modal = Some((instance, active));
                self.set_modal_target(window, cx, instance, 1.0);
            }
        }

        self.outgoing_modals.retain(|(instance, _)| {
            let (card, scrim) = Self::modal_spring_keys(cx, *instance);
            let settled = motion::MotionSystem::is_settled(cx, card)
                && motion::MotionSystem::is_settled(cx, scrim);
            if settled {
                motion::MotionSystem::remove(cx, card);
                motion::MotionSystem::remove(cx, scrim);
            }
            !settled
        });
    }

    fn render_modal_content(
        &mut self,
        active: &ActiveModal,
        theme: &HermesTheme,
        cx: &mut Context<Self>,
    ) -> Div {
        let motion = ModalMotion {
            owner: cx.entity().entity_id(),
            cx: &**cx,
        };

        if let ActiveModal::SessionSearch(state) = active {
            let now = chrono::Utc::now();
            let results = self.search_results(state);
            let refs: Vec<&crate::ui::session_list::SessionItem> = results.iter().collect();
            let view = cx.entity().downgrade();
            let on_open: std::rc::Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)> =
                std::rc::Rc::new(
                    move |id: String, _event: &ClickEvent, _window: &mut Window, cx: &mut App| {
                        view.update(cx, |this, cx| this.switch_session(id, cx)).ok();
                    },
                );
            return state.render(theme, &refs, now, on_open, motion);
        }

        match active {
            ActiveModal::None => div(),
            ActiveModal::Approval(modal) => {
                let request_id = modal.request_id.clone();
                let view = cx.entity().downgrade();
                let on_action: ApprovalHandler =
                    std::rc::Rc::new(move |interaction, _event, _window, cx| {
                        view.update(cx, |this, cx| {
                            let mut submit = None;
                            if let ActiveModal::Approval(current) = &mut this.shell.active_modal {
                                if current.request_id != request_id || current.is_submitting {
                                    return;
                                }
                                match interaction {
                                    ApprovalInteraction::Choose(choice) => {
                                        current.set_choice(choice);
                                    }
                                    ApprovalInteraction::ToggleLease => {
                                        current.toggle_lease();
                                    }
                                    ApprovalInteraction::Submit(result) => {
                                        if result.request_id != current.request_id {
                                            return;
                                        }
                                        current.is_submitting = true;
                                        submit = Some(result);
                                    }
                                }
                            }
                            if let Some(result) = submit {
                                this.send_approval_result(result, cx);
                            }
                            cx.notify();
                        })
                        .ok();
                    });
                modal.render(theme, motion, on_action)
            }
            ActiveModal::Palette(palette) => {
                let view = cx.entity().downgrade();
                let on_action: PaletteActionHandler =
                    std::rc::Rc::new(move |action, _event, _window, cx| {
                        view.update(cx, |this, cx| {
                            if !matches!(this.shell.active_modal, ActiveModal::Palette(_)) {
                                return;
                            }
                            this.shell.dismiss_modal();
                            this.execute_palette_action(action, cx);
                        })
                        .ok();
                    });
                palette.render(theme, motion, on_action)
            }
            ActiveModal::ModelPicker(picker) => {
                let request_id = picker.request_id;
                let session_id = picker.session_id.clone();
                let view = cx.entity().downgrade();
                let retry_view = view.clone();
                let retry_session_id = session_id.clone();
                let on_select: ModelSelectHandler =
                    std::rc::Rc::new(move |model_id, _event, _window, cx| {
                        view.update(cx, |this, cx| {
                            let current = match &this.shell.active_modal {
                                ActiveModal::ModelPicker(picker) => picker,
                                _ => return,
                            };
                            if current.request_id != request_id
                                || current.session_id != session_id
                                || !current.is_ready()
                            {
                                return;
                            }
                            this.shell.dismiss_modal();
                            this.apply_model_choice(model_id, cx);
                        })
                        .ok();
                    });
                let on_retry: ModelRetryHandler = std::rc::Rc::new(move |_event, _window, cx| {
                    retry_view
                        .update(cx, |this, cx| {
                            let current = match &this.shell.active_modal {
                                ActiveModal::ModelPicker(picker) => picker,
                                _ => return,
                            };
                            if current.request_id != request_id
                                || current.session_id != retry_session_id
                                || current.error().is_none()
                            {
                                return;
                            }
                            this.request_model_picker(cx);
                        })
                        .ok();
                });
                picker.render(theme, motion, on_select, on_retry)
            }
            ActiveModal::Account(modal) => modal.render(theme),
            ActiveModal::SessionAction(modal) => {
                let session_id = modal.session_id.clone();
                let view = cx.entity().downgrade();
                let on_action: SessionActionHandler =
                    std::rc::Rc::new(move |action, _event, _window, cx| {
                        view.update(cx, |this, cx| {
                            let outcome = if let ActiveModal::SessionAction(current) =
                                &mut this.shell.active_modal
                            {
                                if current.session_id != session_id || current.busy {
                                    return;
                                }
                                current.activate_menu(action)
                            } else {
                                return;
                            };
                            if let Some(outcome) = outcome {
                                this.execute_session_action(outcome, cx);
                            }
                            cx.notify();
                        })
                        .ok();
                    });
                modal.render(theme, motion, on_action)
            }
            ActiveModal::Settings(modal) => {
                let view = cx.entity().downgrade();
                let on_save: SettingsSaveHandler = std::rc::Rc::new(move |_event, _window, cx| {
                    view.update(cx, |this, cx| {
                        let save =
                            if let ActiveModal::Settings(current) = &mut this.shell.active_modal {
                                if current.busy {
                                    return;
                                }
                                current.selected = current.fields.len();
                                current.handle_key("enter") == Some(SettingsAction::Save)
                            } else {
                                return;
                            };
                        if save {
                            this.execute_settings_save(cx);
                        }
                        cx.notify();
                    })
                    .ok();
                });
                modal.render(theme, motion, on_save)
            }
            ActiveModal::About => render_about(theme),
            ActiveModal::SessionSearch(_) => unreachable!("handled above"),
        }
    }

    /// The shared animated modal layer, including outgoing content during exit.
    fn render_modal_layer(
        &mut self,
        theme: &HermesTheme,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Option<Div> {
        self.sync_modal_motion(window, cx);

        let mut modals = self.outgoing_modals.clone();
        if let Some(active) = self.rendered_modal.clone() {
            modals.push(active);
        }
        if modals.is_empty() {
            return None;
        }

        let mut stack = div().absolute().inset_0();
        for (instance, active) in modals {
            let content = self.render_modal_content(&active, theme, cx);
            let (card, scrim) = Self::modal_spring_keys(cx, instance);
            let card_progress = motion::MotionSystem::value_or(cx, card, 0.0);
            let scrim_progress = motion::MotionSystem::value_or(cx, scrim, 0.0);
            stack = stack.child(render_modal_container(
                content,
                theme,
                card_progress,
                scrim_progress,
            ));
        }
        Some(self.modal_overlay(stack, cx))
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
        if self.shell.active_modal.is_open() {
            return;
        }
        let range = range_utf16
            .as_ref()
            .map(|range_utf16| self.range_from_utf16(range_utf16))
            .or(self.marked_range.clone())
            .unwrap_or(self.selected_range.clone());

        if self.content.get(range.clone()) != Some(new_text) {
            self.record_composer_edit(&range, new_text);
        }
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
        if self.shell.active_modal.is_open() {
            return;
        }
        let range = range_utf16
            .as_ref()
            .map(|range_utf16| self.range_from_utf16(range_utf16))
            .or(self.marked_range.clone())
            .unwrap_or(self.selected_range.clone());

        if self.content.get(range.clone()) != Some(new_text) {
            self.record_composer_edit(&range, new_text);
        }
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
        // Keep the caret solid: blinking would require a perpetual frame loop.
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
                point(
                    bounds.left(),
                    bounds.top() + prepaint.line_height * i as f32,
                ),
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

    fn modal_test_view<'a>(
        cx: &'a mut TestAppContext,
        reduced_motion: bool,
    ) -> (Entity<ChatView>, &'a mut VisualTestContext) {
        cx.update(init_app);
        cx.set_global(motion::MotionSystem::with_policy(
            motion::MotionPolicy::new(reduced_motion),
        ));
        cx.add_window_view(|_, cx| {
            ChatView::new(
                AcpBridge::new(),
                app_runtime().handle().clone(),
                std::env::temp_dir(),
                cx,
            )
        })
    }

    fn advance_motion(app: &mut App, window_id: WindowId, frames: usize) {
        for _ in 0..frames {
            app.global_mut::<motion::MotionSystem>()
                .tick(window_id, Duration::from_millis(16));
        }
    }

    fn advance_visual_motion(vc: &mut VisualTestContext, view: &Entity<ChatView>, frames: usize) {
        vc.update(|window, app| {
            advance_motion(app, window.window_handle().window_id(), frames);
            view.update(app, |_, cx| cx.notify());
            window.refresh();
        });
        vc.run_until_parked();
    }

    #[gpui::test]
    fn modal_motion_reverses_and_reuses_outgoing_instance(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, false);
        vc.update(|window, app| {
            let window_id = window.window_handle().window_id();
            let (instance, card, scrim) = view.update(app, |view, cx| {
                view.shell.active_modal = ActiveModal::About;
                view.sync_modal_motion(window, cx);
                let (instance, _) = view.rendered_modal.clone().expect("modal rendered");
                let (card, scrim) = ChatView::modal_spring_keys(cx, instance);
                (instance, card, scrim)
            });

            advance_motion(app, window_id, 4);
            let before_close = motion::MotionSystem::value(app, card);
            assert!(before_close > 0.0 && before_close < 1.0);

            view.update(app, |view, cx| {
                view.shell.active_modal = ActiveModal::None;
                view.sync_modal_motion(window, cx);
                assert_eq!(view.rendered_modal, None);
                assert_eq!(view.outgoing_modals, vec![(instance, ActiveModal::About)]);
                assert_eq!(motion::MotionSystem::value(cx, card), before_close);
                assert!(!motion::MotionSystem::is_settled(cx, card));
                assert!(!motion::MotionSystem::is_settled(cx, scrim));
            });

            view.update(app, |view, cx| {
                view.shell.active_modal = ActiveModal::About;
                view.sync_modal_motion(window, cx);
                assert_eq!(view.rendered_modal, Some((instance, ActiveModal::About)));
                assert!(view.outgoing_modals.is_empty());
                assert_eq!(motion::MotionSystem::value(cx, card), before_close);
            });

            view.update(app, |view, cx| {
                view.shell.active_modal = ActiveModal::None;
                view.sync_modal_motion(window, cx);
            });
            advance_motion(app, window_id, 600);
            view.update(app, |view, cx| {
                view.sync_modal_motion(window, cx);
                assert!(view.outgoing_modals.is_empty());
                assert_eq!(motion::MotionSystem::value_or(cx, card, -1.0), -1.0);
                assert_eq!(motion::MotionSystem::value_or(cx, scrim, -1.0), -1.0);
            });
        });
    }

    #[gpui::test]
    fn modal_state_updates_keep_the_entrance_instance(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, false);
        vc.update(|window, app| {
            let window_id = window.window_handle().window_id();
            let (instance, card) = view.update(app, |view, cx| {
                view.shell.open_command_palette();
                view.sync_modal_motion(window, cx);
                let (instance, _) = view.rendered_modal.clone().expect("palette rendered");
                let (card, _) = ChatView::modal_spring_keys(cx, instance);
                (instance, card)
            });
            advance_motion(app, window_id, 4);
            let before_edit = motion::MotionSystem::value(app, card);

            view.update(app, |view, cx| {
                if let ActiveModal::Palette(palette) = &mut view.shell.active_modal {
                    palette.set_query("settings");
                }
                view.sync_modal_motion(window, cx);
                assert_eq!(
                    view.rendered_modal.as_ref().map(|(instance, _)| *instance),
                    Some(instance)
                );
                assert!(view.outgoing_modals.is_empty());
                assert_eq!(motion::MotionSystem::value(cx, card), before_edit);
            });
        });
    }

    #[gpui::test]
    fn queued_modal_promotion_gets_a_distinct_entrance(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, false);
        vc.update(|window, app| {
            let window_id = window.window_handle().window_id();
            let (first, first_card, first_modal) = view.update(app, |view, cx| {
                let first_modal = ToolApprovalModal::new("req_1", "bash", "cargo build", "R3");
                let second_modal = ToolApprovalModal::new("req_2", "bash", "cargo test", "R3");
                view.shell.open_approval(first_modal.clone());
                view.shell.open_approval(second_modal.clone());
                view.sync_modal_motion(window, cx);
                let (instance, _) = view
                    .rendered_modal
                    .clone()
                    .expect("first approval rendered");
                let (card, _) = ChatView::modal_spring_keys(cx, instance);
                (instance, card, first_modal)
            });
            advance_motion(app, window_id, 4);

            let (second, second_card) = view.update(app, |view, cx| {
                view.shell.dismiss_modal();
                view.sync_modal_motion(window, cx);
                let (instance, active) = view
                    .rendered_modal
                    .clone()
                    .expect("second approval promoted");
                match &active {
                    ActiveModal::Approval(modal) => assert_eq!(modal.request_id, "req_2"),
                    _ => panic!("expected the queued approval"),
                }
                let (card, _) = ChatView::modal_spring_keys(cx, instance);
                assert_eq!(
                    view.outgoing_modals,
                    vec![(first, ActiveModal::Approval(first_modal))]
                );
                assert_ne!(instance, first);
                (instance, card)
            });

            assert_ne!(second, first);
            assert_eq!(motion::MotionSystem::value_or(app, second_card, -1.0), 0.0);
            assert!(motion::MotionSystem::value(app, first_card) > 0.0);
            advance_motion(app, window_id, 1);
            assert!(motion::MotionSystem::value(app, second_card) > 0.0);
        });
    }

    #[gpui::test]
    fn reduced_motion_snaps_card_and_fades_scrim(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, true);
        vc.update(|window, app| {
            let window_id = window.window_handle().window_id();
            let (instance, card, scrim) = view.update(app, |view, cx| {
                view.shell.active_modal = ActiveModal::About;
                view.sync_modal_motion(window, cx);
                let (instance, _) = view.rendered_modal.clone().expect("modal rendered");
                let (card, scrim) = ChatView::modal_spring_keys(cx, instance);
                assert_eq!(motion::MotionSystem::value(cx, card), 1.0);
                assert_eq!(motion::MotionSystem::value(cx, scrim), 0.0);
                (instance, card, scrim)
            });
            assert!(app
                .global::<motion::MotionSystem>()
                .driver_active(window_id));

            advance_motion(app, window_id, 600);
            view.update(app, |view, cx| {
                view.shell.active_modal = ActiveModal::None;
                view.sync_modal_motion(window, cx);
                assert_eq!(motion::MotionSystem::value(cx, card), 0.0);
                assert!(motion::MotionSystem::value(cx, scrim) > 0.0);
                assert!(!motion::MotionSystem::is_settled(cx, scrim));
                assert_eq!(view.outgoing_modals, vec![(instance, ActiveModal::About)]);
            });
            advance_motion(app, window_id, 600);
            view.update(app, |view, cx| {
                view.sync_modal_motion(window, cx);
                assert!(view.outgoing_modals.is_empty());
            });
        });
    }

    #[gpui::test]
    fn attachment_exit_reverses_without_resetting_the_chip(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, false);
        vc.update(|window, app| {
            let window_id = window.window_handle().window_id();
            let reveal = view.update(app, |view, cx| {
                view.stage_attachment("/tmp/an3.txt".to_string(), window, cx);
                view.attachment_reveal(cx, "/tmp/an3.txt")
            });
            advance_motion(app, window_id, 4);
            let entering = reveal.values(app);
            assert!(entering.spatial > 0.0 && entering.spatial < 1.0);

            view.update(app, |view, cx| {
                view.remove_attachment("/tmp/an3.txt", window, cx);
                assert!(view.attachments.is_empty());
                assert_eq!(view.outgoing_attachments, vec!["/tmp/an3.txt"]);
                assert_eq!(reveal.values(cx), entering);
            });
            advance_motion(app, window_id, 16);
            let exiting = reveal.values(app);
            assert!(exiting.opacity < entering.opacity);

            view.update(app, |view, cx| {
                view.stage_attachment("/tmp/an3.txt".to_string(), window, cx);
                assert_eq!(view.attachments, vec!["/tmp/an3.txt"]);
                assert!(view.outgoing_attachments.is_empty());
                assert_eq!(reveal.values(cx), exiting);
            });
            advance_motion(app, window_id, 600);
            assert_eq!(reveal.values(app).spatial, 1.0);
            assert_eq!(reveal.values(app).opacity, 1.0);

            view.update(app, |view, cx| {
                view.remove_attachment("/tmp/an3.txt", window, cx);
            });
            advance_motion(app, window_id, 600);
            view.update(app, |view, cx| {
                view.prune_outgoing_attachments(cx);
                assert!(view.outgoing_attachments.is_empty());
                assert!(!motion::MotionSystem::has(cx, reveal.spatial));
                assert!(!motion::MotionSystem::has(cx, reveal.opacity));
            });
        });
    }

    #[gpui::test]
    fn approval_retarget_samples_the_current_color(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, false);
        vc.update(|window, app| {
            let window_id = window.window_handle().window_id();
            let key = view.update(app, |view, cx| {
                view.shell.feed.add_approval_card(ApprovalCard::new(
                    "approval_an3",
                    "bash",
                    "cargo test",
                    "R2",
                ));
                view.sync_approval_motion(window, cx);
                assert_eq!(
                    motion::MotionSystem::value(cx, view.approval_spring(cx, "approval_an3")),
                    1.0
                );
                view.approval_spring(cx, "approval_an3")
            });
            advance_motion(app, window_id, 4);

            view.update(app, |view, cx| {
                view.shell
                    .feed
                    .update_approval_state("approval_an3", ApprovalCardState::Approved);
                view.sync_approval_motion(window, cx);
                assert_eq!(motion::MotionSystem::value(cx, key), 0.0);
            });
            advance_motion(app, window_id, 4);
            let (progress, color) = view.update(app, |view, cx| {
                let progress = motion::MotionSystem::value(cx, key);
                assert!(progress > 0.0 && progress < 1.0);
                let visual = view.approval_visuals["approval_an3"];
                let color = visual.color(HermesTheme::get(cx), progress);
                view.shell
                    .feed
                    .update_approval_state("approval_an3", ApprovalCardState::Denied);
                view.sync_approval_motion(window, cx);
                let next = view.approval_visuals["approval_an3"];
                assert_eq!(next.from_state, ApprovalCardState::Approved);
                assert_eq!(next.state, ApprovalCardState::Denied);
                assert_eq!(next.from_color, color);
                assert_eq!(motion::MotionSystem::value(cx, key), 0.0);
                (progress, color)
            });
            assert!(progress > 0.0 && progress < 1.0);
            assert_ne!(color, HermesTheme::get(app).warning_amber().into());

            advance_motion(app, window_id, 600);
            view.update(app, |view, cx| {
                assert_eq!(motion::MotionSystem::value(cx, key), 1.0);
                view.shell.feed.clear();
                view.sync_approval_motion(window, cx);
                assert!(view.approval_visuals.is_empty());
                assert!(!motion::MotionSystem::has(cx, key));
            });
        });
    }

    #[gpui::test]
    fn feed_disclosures_retarget_during_rapid_toggles(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, false);
        vc.update(|window, app| {
            let window_id = window.window_handle().window_id();
            let (reasoning, input, output) = view.update(app, |view, cx| {
                view.shell.feed.messages.push(MessageItem::Assistant(
                    AssistantMessageView::new("assistant_an3", "Answer")
                        .with_reasoning(ThinkingDisclosure::new("Thinking"))
                        .with_tool_call(
                            ToolCallCard::new("call_an3", "bash", "{}").with_output("ok"),
                        ),
                ));
                let theme = HermesTheme::get(cx).clone();
                let owner = cx.entity().entity_id();
                if let MessageItem::Assistant(assistant) = &view.shell.feed.messages[0] {
                    let _ = assistant
                        .render_with_action_bar_motion(&theme, None, None, window, cx, owner);
                }
                (
                    motion::RevealMotion::for_identity(
                        owner,
                        "reasoning_reveal",
                        "reasoning_assistant_an3",
                    ),
                    motion::RevealMotion::for_identity(owner, "tool_reveal", "call_an3:input"),
                    motion::RevealMotion::for_identity(owner, "tool_reveal", "call_an3:output"),
                )
            });
            assert_eq!(output.values(app).spatial, 1.0);

            view.update(app, |view, cx| {
                view.toggle_reasoning("assistant_an3", window, cx);
                view.toggle_tool_input("assistant_an3", "call_an3", window, cx);
                view.toggle_tool_output("assistant_an3", "call_an3", window, cx);
            });
            advance_motion(app, window_id, 4);
            let entering = reasoning.values(app);
            assert!(entering.spatial > 0.0 && entering.spatial < 1.0);
            assert!(output.values(app).spatial > 0.0 && output.values(app).spatial < 1.0);

            view.update(app, |view, cx| {
                view.toggle_reasoning("assistant_an3", window, cx);
                view.toggle_tool_input("assistant_an3", "call_an3", window, cx);
                view.toggle_tool_output("assistant_an3", "call_an3", window, cx);
                assert_eq!(reasoning.values(cx), entering);
                assert_eq!(input.values(cx).spatial, entering.spatial);
                assert!(output.values(cx).spatial < 1.0);
                view.toggle_reasoning("assistant_an3", window, cx);
                view.toggle_tool_input("assistant_an3", "call_an3", window, cx);
                assert_eq!(reasoning.values(cx), entering);
                assert_eq!(input.values(cx).spatial, entering.spatial);
            });
            advance_motion(app, window_id, 600);
            assert_eq!(reasoning.values(app).spatial, 1.0);
            assert_eq!(input.values(app).spatial, 1.0);
            assert_eq!(output.values(app).spatial, 1.0);
        });
    }

    #[gpui::test]
    fn composer_controls_have_independent_hover_and_press_springs(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, false);
        vc.run_until_parked();
        let bounds = vc
            .debug_bounds("composer_send")
            .expect("rendered send control");
        let owner = view.entity_id();
        let send = motion::ControlMotion::for_identity(owner, "composer_control", "send");
        let attach = motion::ControlMotion::for_identity(owner, "composer_control", "attach");

        vc.simulate_mouse_move(bounds.center(), None, Modifiers::none());
        advance_visual_motion(vc, &view, 4);
        assert!(vc.read(|app| send.values(app).hover) > 0.0);
        assert_eq!(vc.read(|app| attach.values(app).hover), 0.0);

        vc.simulate_mouse_down(bounds.center(), MouseButton::Left, Modifiers::none());
        advance_visual_motion(vc, &view, 2);
        assert!(vc.read(|app| send.values(app).press) > 0.0);
        assert_eq!(vc.read(|app| attach.values(app).press), 0.0);

        vc.simulate_mouse_up(bounds.center(), MouseButton::Left, Modifiers::none());
        advance_visual_motion(vc, &view, 600);
        assert_eq!(vc.read(|app| send.values(app).press), 0.0);
        let window_id = vc.update(|window, _| window.window_handle().window_id());
        assert!(!vc.read(|app| app
            .global::<motion::MotionSystem>()
            .driver_active(window_id)));
    }

    #[gpui::test]
    fn sidebar_width_retargets_without_a_layout_jump(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, false);
        vc.update(|_, app| {
            view.update(app, |view, cx| {
                view.show_home = false;
                cx.notify();
            });
        });
        vc.run_until_parked();
        let initial = vc
            .debug_bounds("animated_sidebar")
            .expect("expanded sidebar rendered");
        assert_eq!(initial.size.width, px(SIDEBAR_WIDTH));

        let (width, opacity) =
            vc.update(|_, app| view.update(app, |view, cx| view.sidebar_motion_keys(cx)));
        vc.update(|_, app| {
            view.update(app, |view, cx| {
                view.shell.toggle_sidebar();
                cx.notify();
            });
        });
        advance_visual_motion(vc, &view, 4);
        let collapsing = vc
            .debug_bounds("animated_sidebar")
            .expect("collapsing sidebar stays mounted");
        assert!(collapsing.size.width > px(0.0));
        assert!(collapsing.size.width < initial.size.width);
        let before_retarget = vc.read(|app| motion::MotionSystem::value(app, width));

        vc.update(|_, app| {
            view.update(app, |view, cx| {
                view.shell.toggle_sidebar();
                assert_eq!(motion::MotionSystem::value(cx, width), before_retarget);
                cx.notify();
            });
        });
        advance_visual_motion(vc, &view, 600);
        let expanded = vc
            .debug_bounds("animated_sidebar")
            .expect("reopened sidebar rendered");
        assert_eq!(expanded.size.width, initial.size.width);
        assert_eq!(vc.read(|app| motion::MotionSystem::value(app, width)), 1.0);
        assert_eq!(
            vc.read(|app| motion::MotionSystem::value(app, opacity)),
            1.0
        );
        let window_id = vc.update(|window, _| window.window_handle().window_id());
        assert!(!vc.read(|app| app
            .global::<motion::MotionSystem>()
            .driver_active(window_id)));
    }

    #[gpui::test]
    fn rapid_session_swaps_reuse_the_two_crossfade_layers(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, false);
        vc.update(|window, app| {
            view.update(app, |view, cx| {
                view.show_home = false;
                view.shell.feed.add_user_message("From A");
                for id in ["a", "b", "c"] {
                    view.shell
                        .sidebar
                        .add_session(SessionItem::new(id, format!("Chat {id}")));
                }
                view.shell.select_session("a".to_string());
                let mut feed_b = MessageFeed::new();
                feed_b.add_user_message("From B");
                let mut feed_c = MessageFeed::new();
                feed_c.add_user_message("From C");
                view.sessions.insert(
                    "b".to_string(),
                    SessionState {
                        feed: feed_b,
                        draft: String::new(),
                        attachments: Vec::new(),
                    },
                );
                view.sessions.insert(
                    "c".to_string(),
                    SessionState {
                        feed: feed_c,
                        draft: String::new(),
                        attachments: Vec::new(),
                    },
                );
                view.sync_session_transition(window, cx);
                cx.notify();
            });
        });
        vc.run_until_parked();

        vc.update(|_, app| {
            view.update(app, |view, cx| view.switch_session("b".to_string(), cx));
        });
        advance_visual_motion(vc, &view, 4);
        let (first_incoming, first_outgoing, incoming_value, outgoing_value) = vc.read(|app| {
            let view = view.read(app);
            (
                view.session_incoming_motion.expect("incoming layer"),
                view.session_outgoing_motion.expect("outgoing layer"),
                view.session_incoming_motion
                    .expect("incoming layer")
                    .values(app)
                    .spatial,
                view.session_outgoing_motion
                    .expect("outgoing layer")
                    .values(app)
                    .spatial,
            )
        });
        assert!(incoming_value > 0.0 && incoming_value < 1.0);
        assert!(outgoing_value > 0.0 && outgoing_value < 1.0);

        vc.update(|window, app| {
            view.update(app, |view, cx| {
                view.switch_session("c".to_string(), cx);
                view.sync_session_transition(window, cx);
                assert_eq!(
                    view.session_outgoing_motion
                        .expect("outgoing layer")
                        .spatial,
                    first_incoming.spatial
                );
                assert_eq!(
                    view.session_incoming_motion
                        .expect("incoming layer")
                        .spatial,
                    first_outgoing.spatial
                );
                assert_eq!(
                    motion::MotionSystem::value(cx, first_incoming.spatial),
                    incoming_value
                );
                assert_eq!(
                    motion::MotionSystem::value(cx, first_outgoing.spatial),
                    outgoing_value
                );
                let outgoing = view.outgoing_session_feed.as_ref().expect("outgoing feed");
                assert!(matches!(
                    outgoing.messages.first(),
                    Some(MessageItem::User(card)) if card.content == "From B"
                ));
            });
        });
        advance_visual_motion(vc, &view, 600);
        assert!(vc.read(|app| view.read(app).outgoing_session_feed.is_none()));
        let window_id = vc.update(|window, _| window.window_handle().window_id());
        assert!(!vc.read(|app| app
            .global::<motion::MotionSystem>()
            .driver_active(window_id)));
    }

    #[gpui::test]
    fn dock_controls_have_independent_hover_and_press_springs(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, false);
        vc.run_until_parked();
        let bounds = vc
            .debug_bounds("dock_Notebooks")
            .expect("rendered dock item");
        let notebooks =
            motion::ControlMotion::for_identity(view.entity_id(), "dock_control", "Notebooks");
        let design =
            motion::ControlMotion::for_identity(view.entity_id(), "dock_control", "Design");

        vc.simulate_mouse_move(bounds.center(), None, Modifiers::none());
        advance_visual_motion(vc, &view, 4);
        assert!(vc.read(|app| notebooks.values(app).hover) > 0.0);
        assert_eq!(vc.read(|app| design.values(app).hover), 0.0);

        vc.simulate_mouse_down(bounds.center(), MouseButton::Left, Modifiers::none());
        advance_visual_motion(vc, &view, 2);
        assert!(vc.read(|app| notebooks.values(app).press) > 0.0);
        assert_eq!(vc.read(|app| design.values(app).press), 0.0);

        vc.update(|window, app| {
            view.update(app, |_, cx| {
                notebooks.set_hovered(window, cx, false);
                notebooks.set_pressed(window, cx, false);
            });
        });
        advance_visual_motion(vc, &view, 600);
        assert_eq!(vc.read(|app| notebooks.values(app).hover), 0.0);
        assert_eq!(vc.read(|app| notebooks.values(app).press), 0.0);
        let window_id = vc.update(|window, _| window.window_handle().window_id());
        assert!(!vc.read(|app| app
            .global::<motion::MotionSystem>()
            .driver_active(window_id)));
    }

    fn update_plan() -> CloudUpdatePlan {
        CloudUpdatePlan {
            apply_allowed: Some(true),
            reason: None,
            mandatory_upgrade_required: Some(false),
            channel: None,
            updates: Some(vec![hex_protocol::models::CloudUpdateItem {
                component: "hex-desktop".to_string(),
                installed_version: Some("1.0.0".to_string()),
                target_version: Some("1.1.0".to_string()),
                mandatory: Some(false),
                release_notes: None,
                rollback_to: None,
            }]),
            rollbacks: None,
            compatibility_issues: None,
        }
    }

    #[gpui::test]
    fn toast_and_banner_bounce_then_exit_from_their_live_state(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, false);
        let window_id = vc.update(|window, _| window.window_handle().window_id());
        let (toast_spatial, toast_opacity, banner_spatial, banner_opacity) =
            vc.update(|window, app| {
                view.update(app, |view, cx| {
                    view.home_coming_soon = Some("Notebooks");
                    view.sync_home_toast_motion(window, cx);
                    let (toast_spatial, toast_opacity) = view.home_toast_keys(cx);
                    assert_eq!(motion::MotionSystem::value(cx, toast_spatial), 0.0);
                    assert_eq!(motion::MotionSystem::value(cx, toast_opacity), 0.0);

                    view.cloud_update = Some(update_plan());
                    view.sync_update_banner_motion(window, cx);
                    let (banner_spatial, banner_opacity) = view.update_banner_keys(cx);
                    assert_eq!(motion::MotionSystem::value(cx, banner_spatial), 0.0);
                    assert_eq!(motion::MotionSystem::value(cx, banner_opacity), 0.0);
                    (toast_spatial, toast_opacity, banner_spatial, banner_opacity)
                })
            });

        let mut toast_peak: f32 = 0.0;
        let mut banner_peak: f32 = 0.0;
        vc.update(|_, app| {
            for _ in 0..600 {
                app.global_mut::<motion::MotionSystem>()
                    .tick(window_id, Duration::from_millis(16));
                toast_peak = toast_peak.max(motion::MotionSystem::value(app, toast_spatial));
                banner_peak = banner_peak.max(motion::MotionSystem::value(app, banner_spatial));
            }
        });
        assert!(toast_peak > 1.0 && toast_peak < 1.5);
        assert!(banner_peak > 1.0 && banner_peak < 1.5);

        let (toast_before_exit, banner_before_exit) = vc.update(|window, app| {
            view.update(app, |view, cx| {
                view.home_toast_visual = None;
                view.sync_home_toast_motion(window, cx);
                assert_eq!(motion::MotionSystem::value(cx, toast_spatial), 0.0);
                for _ in 0..4 {
                    cx.global_mut::<motion::MotionSystem>()
                        .tick(window_id, Duration::from_millis(16));
                }
                let toast_before_exit = motion::MotionSystem::value(cx, toast_spatial);

                view.update_banner_visual = None;
                view.sync_update_banner_motion(window, cx);
                assert_eq!(motion::MotionSystem::value(cx, banner_spatial), 0.0);
                for _ in 0..4 {
                    cx.global_mut::<motion::MotionSystem>()
                        .tick(window_id, Duration::from_millis(16));
                }
                let banner_before_exit = motion::MotionSystem::value(cx, banner_spatial);

                view.home_coming_soon = None;
                view.sync_home_toast_motion(window, cx);
                let toast_during_exit = motion::MotionSystem::value(cx, toast_spatial);
                assert!(toast_during_exit > 0.0 && toast_during_exit < 1.5);
                view.cloud_update = None;
                view.sync_update_banner_motion(window, cx);
                let banner_during_exit = motion::MotionSystem::value(cx, banner_spatial);
                assert!(banner_during_exit > 0.0 && banner_during_exit < 1.5);
                assert!(view.home_toast_visual.is_some());
                assert!(view.update_banner_visual.is_some());
                (toast_before_exit, banner_before_exit)
            })
        });
        assert!(toast_before_exit > 0.0 && toast_before_exit < 1.0);
        assert!(banner_before_exit > 0.0 && banner_before_exit < 1.5);

        vc.update(|window, app| {
            advance_motion(app, window_id, 600);
            view.update(app, |view, cx| {
                view.sync_home_toast_motion(window, cx);
                view.sync_update_banner_motion(window, cx);
                assert_eq!(view.home_toast_visual, None);
                assert_eq!(view.update_banner_visual, None);
                assert!(!motion::MotionSystem::has(cx, toast_spatial));
                assert!(!motion::MotionSystem::has(cx, toast_opacity));
                assert!(!motion::MotionSystem::has(cx, banner_spatial));
                assert!(!motion::MotionSystem::has(cx, banner_opacity));
            });
        });
        assert!(!vc.read(|app| app
            .global::<motion::MotionSystem>()
            .driver_active(window_id)));
    }

    #[gpui::test]
    fn reduced_shell_motion_snaps_space_and_fades_opacity(cx: &mut TestAppContext) {
        let (view, vc) = modal_test_view(cx, true);
        let window_id = vc.update(|window, _| window.window_handle().window_id());
        let (
            sidebar_width,
            sidebar_opacity,
            toast_spatial,
            toast_opacity,
            banner_spatial,
            banner_opacity,
        ) = vc.update(|window, app| {
            view.update(app, |view, cx| {
                view.sync_sidebar_motion(window, cx);
                let (sidebar_width, sidebar_opacity) = view.sidebar_motion_keys(cx);
                view.shell.toggle_sidebar();
                view.sync_sidebar_motion(window, cx);
                assert_eq!(motion::MotionSystem::value(cx, sidebar_width), 0.0);
                assert!(motion::MotionSystem::value(cx, sidebar_opacity) > 0.0);

                view.home_coming_soon = Some("Design");
                view.sync_home_toast_motion(window, cx);
                let (toast_spatial, toast_opacity) = view.home_toast_keys(cx);
                assert_eq!(motion::MotionSystem::value(cx, toast_spatial), 1.0);
                assert_eq!(motion::MotionSystem::value(cx, toast_opacity), 0.0);

                view.cloud_update = Some(update_plan());
                view.sync_update_banner_motion(window, cx);
                let (banner_spatial, banner_opacity) = view.update_banner_keys(cx);
                assert_eq!(motion::MotionSystem::value(cx, banner_spatial), 1.0);
                assert_eq!(motion::MotionSystem::value(cx, banner_opacity), 0.0);
                (
                    sidebar_width,
                    sidebar_opacity,
                    toast_spatial,
                    toast_opacity,
                    banner_spatial,
                    banner_opacity,
                )
            })
        });
        assert!(vc.read(|app| app
            .global::<motion::MotionSystem>()
            .driver_active(window_id)));

        vc.update(|_, app| advance_motion(app, window_id, 600));
        for key in [toast_opacity, banner_opacity] {
            assert_eq!(vc.read(|app| motion::MotionSystem::value(app, key)), 1.0);
        }
        assert_eq!(
            vc.read(|app| motion::MotionSystem::value(app, sidebar_opacity)),
            0.0
        );
        assert_eq!(
            vc.read(|app| motion::MotionSystem::value(app, sidebar_width)),
            0.0
        );
        assert_eq!(
            vc.read(|app| motion::MotionSystem::value(app, toast_spatial)),
            1.0
        );
        assert_eq!(
            vc.read(|app| motion::MotionSystem::value(app, banner_spatial)),
            1.0
        );
        assert!(!vc.read(|app| app
            .global::<motion::MotionSystem>()
            .driver_active(window_id)));
    }

    #[gpui::test]
    fn cached_session_switch_preserves_attachments(cx: &mut TestAppContext) {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        cx.update(init_app);
        let (view, vc) = cx.add_window_view(|_, cx| {
            ChatView::new(
                AcpBridge::new(),
                rt.handle().clone(),
                std::env::temp_dir(),
                cx,
            )
        });
        vc.update(|_, app| {
            view.update(app, |view, cx| {
                view.shell
                    .sidebar
                    .add_session(SessionItem::new("a", "Chat A"));
                view.shell
                    .sidebar
                    .add_session(SessionItem::new("b", "Chat B"));
                view.shell.select_session("a".to_string());
                view.content = "Draft A".to_string();
                view.attachments = vec!["a.txt".to_string()];
                view.sessions.insert(
                    "b".to_string(),
                    SessionState {
                        feed: MessageFeed::new(),
                        draft: "Draft B".to_string(),
                        attachments: vec!["b.txt".to_string()],
                    },
                );

                view.switch_session("b".to_string(), cx);
                assert_eq!(view.content, "Draft B");
                assert_eq!(view.attachments, vec!["b.txt".to_string()]);

                view.switch_session("a".to_string(), cx);
                assert_eq!(view.content, "Draft A");
                assert_eq!(view.attachments, vec!["a.txt".to_string()]);
            });
        });
    }

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

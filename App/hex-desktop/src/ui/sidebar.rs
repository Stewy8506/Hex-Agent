//! Mockup sidebar for HEX Desktop (Phase 3): w296 `#101010` project navigator.
//!
//! Mirrors the mockup's `DesktopSession.dc.html` sidebar: a New chat `.row`,
//! the `#1a1a1a` search box with the platform-aware shortcut hint, the
//! Projects header plus one project group per workspace (expand/collapse,
//! folder tint states, session count vs pulsing running dot, chevrons),
//! session rows with the 16px braille-spinner slot and location/Review chips,
//! the features block (Needs you accent chip, Team, Notebooks, Design,
//! Extensions, Automations — no badges) and the Account / Local-mode footer.
//!
//! The legacy Hermes workspace header, provider status chip and "Coming
//! soon" badges are gone (D11); provider state is still tracked for events.

use std::collections::HashSet;
use std::rc::Rc;
use std::sync::OnceLock;
use std::time::Duration;

use gpui::prelude::FluentBuilder;
use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::motion;
use crate::ui::session_list::{sort_project_sessions, SessionItem, SessionListView};

/// Connectivity and operational state for the AI provider.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProviderState {
    /// Connected and ready to receive prompts (Green dot #8fbf93).
    Connected,
    /// Active generation or autonomous tool loop running (Amber dot #f59e0b).
    Generating,
    /// Connection severed, rate limited, or error state (Red dot #c98b8b).
    Offline,
    /// Transport lost; supervised reconnect with backoff is in progress.
    Reconnecting,
}

impl ProviderState {
    pub fn dot_color(&self, theme: &HermesTheme) -> Rgba {
        match self {
            ProviderState::Connected => theme.sage_fg(),
            ProviderState::Generating => theme.warning_amber(),
            ProviderState::Offline => theme.rose_fg(),
            ProviderState::Reconnecting => theme.warning_amber(),
        }
    }

    pub fn status_label(&self) -> &'static str {
        match self {
            ProviderState::Connected => "Connected",
            ProviderState::Generating => "Generating",
            ProviderState::Offline => "Offline",
            ProviderState::Reconnecting => "Reconnecting",
        }
    }
}

/// Active provider and model status payload. No longer rendered (mockup has
/// no provider chip) but still updated from ACP status events.
#[derive(Debug, Clone, PartialEq)]
pub struct ProviderStatus {
    pub state: ProviderState,
    pub provider_name: String,
    pub model_name: String,
}

impl Default for ProviderStatus {
    fn default() -> Self {
        Self {
            state: ProviderState::Connected,
            provider_name: "Anthropic".to_string(),
            model_name: "Claude 3.7 Sonnet".to_string(),
        }
    }
}

impl ProviderStatus {
    pub fn new(
        state: ProviderState,
        provider_name: impl Into<String>,
        model_name: impl Into<String>,
    ) -> Self {
        Self {
            state,
            provider_name: provider_name.into(),
            model_name: model_name.into(),
        }
    }

    pub fn display_label(&self) -> String {
        if self.model_name.is_empty() {
            self.provider_name.clone()
        } else {
            format!("{} {}", self.provider_name, self.model_name)
        }
    }
}

// --- Mockup sidebar metrics (docs/design mockups-desktop) ---

/// Sidebar width in px.
pub const SIDEBAR_WIDTH: f32 = 296.0;
/// Sidebar padding: 18px top, 12px x, 14px bottom.
pub const SIDEBAR_PADDING_TOP: f32 = 18.0;
pub const SIDEBAR_PADDING_X: f32 = 12.0;
pub const SIDEBAR_PADDING_BOTTOM: f32 = 14.0;
/// Standard `.row` metrics: gap 10, padding 6px 10px, radius 10.
pub const ROW_GAP: f32 = 10.0;
pub const ROW_PADDING_X: f32 = 10.0;
pub const ROW_PADDING_Y: f32 = 6.0;
pub const ROW_RADIUS: f32 = 10.0;
/// New chat row bottom margin.
pub const NEW_CHAT_MARGIN_BOTTOM: f32 = 8.0;
/// New chat row icons (plus and folder) in px.
pub const NEW_CHAT_ICON: f32 = 16.0;
/// Search box: padding 7px 12px, gap 10, margin-bottom 22, icons 16.
pub const SEARCH_PADDING_X: f32 = 12.0;
pub const SEARCH_PADDING_Y: f32 = 7.0;
pub const SEARCH_GAP: f32 = 10.0;
pub const SEARCH_MARGIN_BOTTOM: f32 = 22.0;
pub const SEARCH_ICON: f32 = 16.0;
/// Projects header: padding 0 10px 4px; plus icon 14.
pub const PROJECTS_HEADER_ICON: f32 = 14.0;
/// Project/session row icons: folder, chevrons, feature icons.
pub const PROJECT_ICON: f32 = 16.0;
pub const CHEVRON_ICON: f32 = 14.0;
pub const FEATURE_ICON: f32 = 16.0;
/// Session rows indent: padding-left 18 with a 16px leading slot.
pub const SESSION_ROW_INDENT: f32 = 18.0;
pub const SESSION_SLOT: f32 = 16.0;
/// Running project dot (`.pl`) diameter.
pub const RUNNING_DOT: f32 = 7.0;
/// Account footer: padding 12px 8px 0, gap 10, avatar 30 circle.
pub const ACCOUNT_PADDING_TOP: f32 = 12.0;
pub const ACCOUNT_PADDING_X: f32 = 8.0;
pub const ACCOUNT_GAP: f32 = 10.0;
pub const AVATAR: f32 = 30.0;

/// Mockup `ctrl K` (Windows/Linux) / `cmd K` (macOS) sidebar hint.
pub fn shortcut_hint_for(is_mac: bool) -> &'static str {
    if is_mac {
        "cmd K"
    } else {
        "ctrl K"
    }
}

/// The platform-aware sidebar shortcut hint.
pub fn shortcut_hint() -> &'static str {
    shortcut_hint_for(cfg!(target_os = "macos"))
}

/// Milliseconds since first paint (deterministic animation epoch).
fn spinner_epoch_ms() -> u64 {
    static EPOCH: OnceLock<std::time::Instant> = OnceLock::new();
    EPOCH.get_or_init(std::time::Instant::now).elapsed().as_millis() as u64
}

/// Location/Review chip for a session row: pending approval renders the
/// mockup's accent "Review" chip; everything else stays the honest "This Mac"
/// (local-only execution).
pub fn session_chip(pending_review: bool) -> (&'static str, bool) {
    if pending_review {
        ("Review", true)
    } else {
        ("This Mac", false)
    }
}

/// The sidebar session slot glyph: a braille spinner frame while running,
/// nothing otherwise.
pub fn session_slot_glyph(running: bool) -> Option<char> {
    if running {
        Some(motion::spinner_frame(Duration::from_millis(
            spinner_epoch_ms(),
        )))
    } else {
        None
    }
}

/// Click handlers wired by `ChatView` to real backend operations.
#[derive(Default)]
pub struct SidebarHandlers {
    pub on_new_chat: Option<Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>>,
    pub on_select_session: Option<Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)>>,
    pub on_needs_you: Option<Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>>,
    pub on_search: Option<Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>>,
    pub on_settings: Option<Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>>,
    pub on_feature: Option<Rc<dyn Fn(&'static str, &ClickEvent, &mut Window, &mut App)>>,
    pub on_toggle_project: Option<Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>>,
}

/// State and rendering container for the mockup project navigator.
#[derive(Debug, Clone, PartialEq)]
pub struct SidebarView {
    pub workspace_name: String,
    pub git_branch: String,
    pub session_list: SessionListView,
    pub provider_status: ProviderStatus,
    /// Number of unresolved approvals waiting for the user.
    pub pending_approvals: usize,
    /// Session ids with a live turn (drives slot spinners and the running dot).
    pub running_sessions: HashSet<String>,
    /// Session ids with a pending approval (drives "Review" chips).
    pub review_sessions: HashSet<String>,
    /// Whether the project group body is collapsed.
    pub projects_collapsed: bool,
}

impl Default for SidebarView {
    fn default() -> Self {
        Self::new()
    }
}

impl SidebarView {
    pub fn new() -> Self {
        Self {
            workspace_name: "Harness".to_string(),
            git_branch: "experimental-features".to_string(),
            session_list: SessionListView::new(),
            provider_status: ProviderStatus::default(),
            pending_approvals: 0,
            running_sessions: HashSet::new(),
            review_sessions: HashSet::new(),
            projects_collapsed: false,
        }
    }

    pub fn with_workspace(
        workspace_name: impl Into<String>,
        git_branch: impl Into<String>,
    ) -> Self {
        Self {
            workspace_name: workspace_name.into(),
            git_branch: git_branch.into(),
            session_list: SessionListView::new(),
            provider_status: ProviderStatus::default(),
            pending_approvals: 0,
            running_sessions: HashSet::new(),
            review_sessions: HashSet::new(),
            projects_collapsed: false,
        }
    }

    /// Update the "Needs you" badge from the live approval queue.
    pub fn set_pending_approvals(&mut self, count: usize) {
        self.pending_approvals = count;
    }

    /// Sync live-turn session ids (drives spinners and the running dot).
    pub fn set_running_sessions(&mut self, ids: HashSet<String>) {
        self.running_sessions = ids;
    }

    /// Sync pending-approval session ids (drives "Review" chips).
    pub fn set_review_sessions(&mut self, ids: HashSet<String>) {
        self.review_sessions = ids;
    }

    pub fn set_provider_status(&mut self, status: ProviderStatus) {
        self.provider_status = status;
    }

    pub fn set_workspace(&mut self, name: impl Into<String>, branch: impl Into<String>) {
        self.workspace_name = name.into();
        self.git_branch = branch.into();
    }

    pub fn add_session(&mut self, session: SessionItem) {
        self.session_list.add_session(session);
    }

    pub fn select_session(&mut self, id: impl Into<String>) {
        self.session_list.select_session(id);
    }

    /// The mockup `.row` base: gap 10, padding 6px 10px, radius 10, hover
    /// `#1a1a1a` (.2s).
    fn mockup_row(theme: &HermesTheme) -> Div {
        div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .gap(px(ROW_GAP))
            .px(px(ROW_PADDING_X))
            .py(px(ROW_PADDING_Y))
            .rounded(px(ROW_RADIUS))
            .hover(|s| s.bg(theme.mockup_row_hover))
    }

    /// Tints an SVG icon through a colored wrapper (SVGs inherit the
    /// container text color).
    fn tinted_icon(icon: &str, size: f32, tint: Rgba) -> Div {
        div().flex_none().text_color(tint).child(
            crate::ui::icon_svg(icon, size),
        )
    }

    /// New chat `.row`, margin-bottom 8: plus 16 `#e6e6e6` · label flex-1
    /// 14.5 `#e6e6e6` · folder 16 `#858585`.
    fn render_new_chat_row(&self, theme: &HermesTheme, handlers: &SidebarHandlers) -> Stateful<Div> {
        let mut row = Self::mockup_row(theme)
            .id("sidebar_new_chat")
            .mb(px(NEW_CHAT_MARGIN_BOTTOM))
            .cursor_pointer()
            .child(Self::tinted_icon("plus", NEW_CHAT_ICON, theme.mockup_text))
            .child(
                div()
                    .flex_1()
                    .min_w_0()
                    .text_size(px(MockupTypeScale::FEATURE_LABEL_SIZE))
                    .text_color(theme.mockup_text)
                    .child("New chat"),
            )
            .child(Self::tinted_icon(
                "folder",
                NEW_CHAT_ICON,
                theme.mockup_text_dim,
            ));
        if let Some(on_new_chat) = &handlers.on_new_chat {
            let on_new_chat = on_new_chat.clone();
            row = row.on_click(move |event, window, cx| on_new_chat(event, window, cx));
        }
        row
    }

    /// Search box: bg `#1a1a1a`, radius 10, padding 7px 12px, gap 10,
    /// margin-bottom 22: search 16 · "Search chats" 14.5 flex-1 · shortcut
    /// hint mono 11.5 (all `#858585`). Clicking opens the command palette.
    fn render_search_row(&self, theme: &HermesTheme, handlers: &SidebarHandlers) -> Stateful<Div> {
        let mut row = div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .gap(px(SEARCH_GAP))
            .px(px(SEARCH_PADDING_X))
            .py(px(SEARCH_PADDING_Y))
            .mb(px(SEARCH_MARGIN_BOTTOM))
            .rounded(px(ROW_RADIUS))
            .bg(theme.mockup_row_hover)
            .cursor_pointer()
            .id("sidebar_search")
            .child(Self::tinted_icon("search", SEARCH_ICON, theme.mockup_text_dim))
            .child(
                div()
                    .flex_1()
                    .min_w_0()
                    .text_size(px(MockupTypeScale::SEARCH_SIZE))
                    .text_color(theme.mockup_text_dim)
                    .child(if self.session_list.search_query.is_empty() {
                        "Search chats".to_string()
                    } else {
                        self.session_list.search_query.clone()
                    }),
            )
            .child(
                div()
                    .font_family(theme.font_mono()[0].clone())
                    .text_size(px(MockupTypeScale::HINT_SIZE))
                    .text_color(theme.mockup_text_dim)
                    .child(shortcut_hint()),
            );
        if let Some(on_search) = &handlers.on_search {
            let on_search = on_search.clone();
            row = row.on_click(move |event, window, cx| on_search(event, window, cx));
        }
        row
    }

    /// Projects header: padding 0 10px 4px, 13.5 `#858585`, plus 14 right.
    fn render_projects_header(&self, theme: &HermesTheme) -> Div {
        div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .px(px(ROW_PADDING_X))
            .pb(px(4.0))
            .child(
                div()
                    .flex_1()
                    .text_size(px(MockupTypeScale::PROJECTS_HEADER_SIZE))
                    .text_color(theme.mockup_text_dim)
                    .child("Projects"),
            )
            .child(Self::tinted_icon(
                "plus",
                PROJECTS_HEADER_ICON,
                theme.mockup_text_dim,
            ))
    }

    /// Project `.row`: folder 16 (`#e6e6e6` expanded / `#9a9a9a` collapsed) ·
    /// name flex-1 · count 13 `#858585` or pulsing 7px accent dot when a
    /// session is running · chevron 14. Clicking toggles the group.
    fn render_project_row(
        &self,
        theme: &HermesTheme,
        handlers: &SidebarHandlers,
        session_count: usize,
        any_running: bool,
    ) -> Stateful<Div> {
        let running_dot = if any_running {
            let dot = div()
                .size(px(RUNNING_DOT))
                .rounded_full()
                .bg(theme.accent_blue);
            if motion::reduced_motion() {
                dot.into_any_element()
            } else {
                dot.with_animation(
                    "sidebar_project_pl",
                    Animation::new(motion::PULSE_PERIOD).repeat(),
                    |el, phase| {
                        let elapsed = Duration::from_secs_f32(
                            phase * motion::PULSE_PERIOD.as_secs_f32(),
                        );
                        el.opacity(motion::pulse_opacity(elapsed))
                    },
                )
                .into_any_element()
            }
        } else {
            div()
                .text_size(px(MockupTypeScale::PROJECT_COUNT_SIZE))
                .text_color(theme.mockup_text_dim)
                .child(session_count.to_string())
                .into_any_element()
        };

        let mut row = Self::mockup_row(theme)
            .id("sidebar_project_row")
            .cursor_pointer()
            .child(Self::tinted_icon(
                "folder",
                PROJECT_ICON,
                if self.projects_collapsed {
                    theme.mockup_text_muted
                } else {
                    theme.mockup_text
                },
            ))
            .child(
                div()
                    .flex_1()
                    .min_w_0()
                    .truncate()
                    .text_size(px(MockupTypeScale::SESSION_TITLE_SIZE))
                    .text_color(theme.mockup_text)
                    .child(self.workspace_name.clone()),
            )
            .child(running_dot)
            .child(Self::tinted_icon(
                if self.projects_collapsed {
                    "chevron-right"
                } else {
                    "chevron-down"
                },
                CHEVRON_ICON,
                theme.mockup_text_dim,
            ));
        if let Some(on_toggle_project) = &handlers.on_toggle_project {
            let on_toggle_project = on_toggle_project.clone();
            row = row.on_click(move |event, window, cx| on_toggle_project(event, window, cx));
        }
        row
    }

    /// Session `.row`, padding-left 18: 16px leading slot (empty, or braille
    /// spinner mono 13 `#7aa7ff` centered when running) · title 14.5 ellipsis
    /// (`#e6e6e6` on the `.act` row, else `#bdbdbd`) · chip right.
    fn render_session_row(
        &self,
        theme: &HermesTheme,
        session: &SessionItem,
        handlers: &SidebarHandlers,
    ) -> Stateful<Div> {
        let is_active = self.session_list.selected_session_id.as_deref() == Some(&session.id);
        let running = self.running_sessions.contains(&session.id);
        let (chip_label, chip_accent) = session_chip(self.review_sessions.contains(&session.id));

        let slot = if running {
            let glyph = div()
                .flex()
                .items_center()
                .justify_center()
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(MockupTypeScale::SPINNER_SIZE))
                .text_color(theme.accent_blue);
            if motion::reduced_motion() {
                glyph.child(motion::spinner_frame(Duration::ZERO).to_string()).into_any_element()
            } else {
                glyph
                    .with_animation(
                        ElementId::Name(SharedString::from(format!("sidebar_sp_{}", session.id))),
                        Animation::new(motion::SPIN_PERIOD).repeat(),
                        |el, phase| {
                            let elapsed = Duration::from_secs_f32(
                                phase * motion::SPIN_PERIOD.as_secs_f32(),
                            );
                            el.child(motion::spinner_frame(elapsed).to_string())
                        },
                    )
                    .into_any_element()
            }
        } else {
            div().into_any_element()
        };

        let mut row = Self::mockup_row(theme)
            .id(SharedString::from(format!("sidebar_session_{}", session.id)))
            .cursor_pointer()
            .pl(px(SESSION_ROW_INDENT))
            .when(is_active, |s| s.bg(theme.mockup_hover))
            .child(
                div()
                    .w(px(SESSION_SLOT))
                    .h(px(SESSION_SLOT))
                    .flex()
                    .items_center()
                    .justify_center()
                    .flex_none()
                    .child(slot),
            )
            .child(
                div()
                    .flex_1()
                    .min_w_0()
                    .truncate()
                    .text_size(px(MockupTypeScale::SESSION_TITLE_SIZE))
                    .when(is_active, |s| s.text_color(theme.mockup_text))
                    .when(!is_active, |s| s.text_color(theme.mockup_session_inactive))
                    .child(session.title.clone()),
            )
            .child(
                div()
                    .flex_none()
                    .text_size(px(MockupTypeScale::CHIP_SIZE))
                    .line_height(px(MockupTypeScale::CHIP_LINE_HEIGHT))
                    .rounded(px(6.0))
                    .px_2()
                    .when(chip_accent, |c| {
                        c.text_color(theme.accent_blue)
                            .bg(theme.mockup_accent_chip_bg)
                    })
                    .when(!chip_accent, |c| {
                        c.text_color(theme.mockup_text_muted).bg(theme.mockup_chip)
                    })
                    .child(chip_label),
            );
        if let Some(on_select_session) = &handlers.on_select_session {
            let on_select_session = on_select_session.clone();
            let id = session.id.clone();
            row = row.on_click(move |event, window, cx| on_select_session(id.clone(), event, window, cx));
        }
        row
    }

    /// Features block: border-top `#222`, padding-top 10, full-bleed
    /// (margin 0 −12px, padding-x 12), gap 1; rows in the exact mockup
    /// order — Needs you (accent chip count), Team, Notebooks, Design,
    /// Extensions, Automations — with no badges.
    fn render_features_block(&self, theme: &HermesTheme, handlers: &SidebarHandlers) -> Div {
        let needs_you = self.feature_row(
            theme,
            "needs_you_row",
            "Needs you",
            "bell",
            handlers.on_needs_you.clone(),
        )
        .child(
            div()
                .flex_none()
                .text_size(px(MockupTypeScale::CHIP_SIZE))
                .line_height(px(MockupTypeScale::CHIP_LINE_HEIGHT))
                .rounded(px(6.0))
                .px_2()
                .when(self.pending_approvals > 0, |c| {
                    c.text_color(theme.accent_blue)
                        .bg(theme.mockup_accent_chip_bg)
                })
                .when(self.pending_approvals == 0, |c| {
                    c.text_color(theme.mockup_text_muted).bg(theme.mockup_chip)
                })
                .child(self.pending_approvals.to_string()),
        );

        let mut block = div()
            .w_full()
            .flex()
            .flex_col()
            .gap(px(1.0))
            .mt(px(10.0))
            .ml(px(-12.0))
            .mr(px(-12.0))
            .px(px(SIDEBAR_PADDING_X))
            .pt(px(10.0))
            .border_t_1()
            .border_color(theme.mockup_border)
            .child(needs_you);
        for (label, icon) in [
            ("Team", "team"),
            ("Notebooks", "notebooks"),
            ("Design", "design"),
            ("Extensions", "extensions"),
            ("Automations", "bolt"),
        ] {
            let click = handlers.on_feature.clone().map(|f| {
                let f = f.clone();
                let label: &'static str = label;
                Rc::new(move |event: &ClickEvent, window: &mut Window, app: &mut App| {
                    f(label, event, window, app)
                }) as Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>
            });
            block = block.child(self.feature_row(theme, label, label, icon, click));
        }
        block
    }

    /// One standard feature `.row`: icon 16 `#9a9a9a` · label 14.5 `#cfcfcf`
    /// flex-1. `id` keys the stateful element and doubles as the feature name.
    fn feature_row(
        &self,
        theme: &HermesTheme,
        id: &str,
        label: &str,
        icon: &str,
        on_click: Option<Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>>,
    ) -> Stateful<Div> {
        let mut row = Self::mockup_row(theme)
            .id(SharedString::from(format!("feature_{}", id)))
            .cursor_pointer()
            .child(Self::tinted_icon(icon, FEATURE_ICON, theme.mockup_text_muted))
            .child(
                div()
                    .flex_1()
                    .min_w_0()
                    .text_size(px(MockupTypeScale::FEATURE_LABEL_SIZE))
                    .text_color(theme.mockup_text_muted)
                    .child(label.to_string()),
            );
        if let Some(on_click) = on_click {
            row = row.on_click(move |event, window, cx| on_click(event, window, cx));
        }
        row
    }

    /// Account footer: padding 12px 8px 0, gap 10: avatar 30 circle `#262626`
    /// letter 13 · column lh18 (name 14 `#e6e6e6`, sub 12 `#858585`) · gear 16.
    fn render_account_footer(&self, theme: &HermesTheme, handlers: &SidebarHandlers) -> Stateful<Div> {
        let mut row = div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .gap(px(ACCOUNT_GAP))
            .px(px(ACCOUNT_PADDING_X))
            .pt(px(ACCOUNT_PADDING_TOP))
            .id("sidebar_account_gear")
            .cursor_pointer()
            .child(
                div()
                    .size(px(AVATAR))
                    .rounded_full()
                    .bg(theme.mockup_seg_active)
                    .flex()
                    .items_center()
                    .justify_center()
                    .text_size(px(MockupTypeScale::AVATAR_SIZE))
                    .text_color(theme.mockup_text_base)
                    .child("A"),
            )
            .child(
                div()
                    .flex_1()
                    .min_w_0()
                    .line_height(px(MockupTypeScale::ACCOUNT_LINE_HEIGHT))
                    .child(
                        div()
                            .text_size(px(MockupTypeScale::ACCOUNT_NAME_SIZE))
                            .text_color(theme.mockup_text)
                            .child("Account"),
                    )
                    .child(
                        div()
                            .text_size(px(MockupTypeScale::ACCOUNT_SUB_SIZE))
                            .text_color(theme.mockup_text_dim)
                            .child("Local mode"),
                    ),
            )
            .child(Self::tinted_icon("settings", FEATURE_ICON, theme.mockup_text_dim));
        if let Some(on_settings) = &handlers.on_settings {
            let on_settings = on_settings.clone();
            row = row.on_click(move |event, window, cx| on_settings(event, window, cx));
        }
        row
    }

    /// Renders the mockup 296px sidebar.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        self.render_with_actions(theme, &SidebarHandlers::default())
    }

    /// Renders the sidebar with the interactive handlers `ChatView` wires to
    /// real backend operations.
    pub fn render_with_actions(&self, theme: &HermesTheme, handlers: &SidebarHandlers) -> Div {
        let sessions = sort_project_sessions(&self.session_list.sessions);
        let any_running = sessions
            .iter()
            .any(|s| self.running_sessions.contains(&s.id));

        let mut project_list = div()
            .id("sidebar_project_list")
            .w_full()
            .flex_1()
            .min_h_0()
            .flex()
            .flex_col()
            .gap(px(2.0))
            .overflow_y_scroll()
            .child(self.render_project_row(theme, handlers, sessions.len(), any_running));
        if !self.projects_collapsed {
            for session in &sessions {
                project_list =
                    project_list.child(self.render_session_row(theme, session, handlers));
            }
        }

        div()
            .w(px(SIDEBAR_WIDTH))
            .h_full()
            .flex()
            .flex_col()
            .flex_none()
            .bg(theme.mockup_sidebar)
            .border_r_1()
            .border_color(theme.mockup_border)
            .pt(px(SIDEBAR_PADDING_TOP))
            .px(px(SIDEBAR_PADDING_X))
            .pb(px(SIDEBAR_PADDING_BOTTOM))
            .font_family(theme.font_sans()[0].clone())
            .child(self.render_new_chat_row(theme, handlers))
            .child(self.render_search_row(theme, handlers))
            .child(self.render_projects_header(theme))
            .child(project_list)
            .child(self.render_features_block(theme, handlers))
            .child(self.render_account_footer(theme, handlers))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_provider_state_and_colors() {
        let theme = HermesTheme::default();

        let conn = ProviderState::Connected;
        assert_eq!(conn.status_label(), "Connected");
        assert_eq!(conn.dot_color(&theme), theme.sage_fg());

        let gen = ProviderState::Generating;
        assert_eq!(gen.status_label(), "Generating");
        assert_eq!(gen.dot_color(&theme), theme.warning_amber());

        let off = ProviderState::Offline;
        assert_eq!(off.status_label(), "Offline");
        assert_eq!(off.dot_color(&theme), theme.rose_fg());
    }

    #[test]
    fn test_sidebar_initialization_and_workspace() {
        let mut sidebar = SidebarView::with_workspace("Harness", "experimental-features");
        assert_eq!(sidebar.workspace_name, "Harness");
        assert_eq!(sidebar.git_branch, "experimental-features");

        sidebar.set_workspace("hex-agent", "main");
        assert_eq!(sidebar.workspace_name, "hex-agent");
        assert_eq!(sidebar.git_branch, "main");

        let status = ProviderStatus::new(ProviderState::Generating, "OpenAI", "o3-mini");
        assert_eq!(status.display_label(), "OpenAI o3-mini");
        sidebar.set_provider_status(status);
        assert_eq!(sidebar.provider_status.state, ProviderState::Generating);
    }

    #[test]
    fn test_sidebar_metric_tokens_match_mockup() {
        assert_eq!(SIDEBAR_WIDTH, 296.0);
        assert_eq!(SIDEBAR_PADDING_TOP, 18.0);
        assert_eq!(SIDEBAR_PADDING_X, 12.0);
        assert_eq!(SIDEBAR_PADDING_BOTTOM, 14.0);
        assert_eq!(ROW_GAP, 10.0);
        assert_eq!(ROW_PADDING_X, 10.0);
        assert_eq!(ROW_PADDING_Y, 6.0);
        assert_eq!(ROW_RADIUS, 10.0);
        assert_eq!(NEW_CHAT_MARGIN_BOTTOM, 8.0);
        assert_eq!(NEW_CHAT_ICON, 16.0);
        assert_eq!(SEARCH_PADDING_X, 12.0);
        assert_eq!(SEARCH_PADDING_Y, 7.0);
        assert_eq!(SEARCH_GAP, 10.0);
        assert_eq!(SEARCH_MARGIN_BOTTOM, 22.0);
        assert_eq!(SEARCH_ICON, 16.0);
        assert_eq!(PROJECTS_HEADER_ICON, 14.0);
        assert_eq!(PROJECT_ICON, 16.0);
        assert_eq!(CHEVRON_ICON, 14.0);
        assert_eq!(FEATURE_ICON, 16.0);
        assert_eq!(SESSION_ROW_INDENT, 18.0);
        assert_eq!(SESSION_SLOT, 16.0);
        assert_eq!(RUNNING_DOT, 7.0);
        assert_eq!(ACCOUNT_PADDING_TOP, 12.0);
        assert_eq!(ACCOUNT_PADDING_X, 8.0);
        assert_eq!(ACCOUNT_GAP, 10.0);
        assert_eq!(AVATAR, 30.0);
    }

    #[test]
    fn test_sidebar_tones_match_mockup() {
        let theme = HermesTheme::default();
        // Sidebar bg #101010, border #222, search/hover #1a1a1a, inactive
        // titles #bdbdbd, account avatar #262626, letter #d9d9d9.
        assert_eq!(theme.mockup_sidebar, rgb(0x101010));
        assert_eq!(theme.mockup_border, rgb(0x222222));
        assert_eq!(theme.mockup_row_hover, rgb(0x1a1a1a));
        assert_eq!(theme.mockup_session_inactive, rgb(0xbdbdbd));
        assert_eq!(theme.mockup_seg_active, rgb(0x262626));
        assert_eq!(theme.mockup_text_base, rgb(0xd9d9d9));
        assert_eq!(theme.mockup_accent_chip_bg, rgba(0x7aa7ff24));
    }

    #[test]
    fn test_session_chip_mapping() {
        assert_eq!(session_chip(false), ("This Mac", false));
        assert_eq!(session_chip(true), ("Review", true));
    }

    #[test]
    fn test_session_slot_glyph_integration() {
        // Not running: empty slot. Running: a braille spinner frame from the
        // real spinner cycle (first frame is U+280B '⠋').
        assert_eq!(session_slot_glyph(false), None);
        let glyph = session_slot_glyph(true).expect("running session must show a spinner");
        assert_eq!(glyph, motion::spinner_frame(Duration::ZERO));
        assert!(('\u{2800}'..='\u{28FF}').contains(&glyph));
    }

    #[test]
    fn test_spinner_frame_cycles_ten_steps() {
        let mut seen = Vec::new();
        for step in 0..10 {
            seen.push(motion::spinner_frame(Duration::from_millis(step * 90)));
        }
        let unique: HashSet<char> = seen.iter().copied().collect();
        assert_eq!(unique.len(), 10, "spinner must cycle 10 distinct frames");
    }

    #[test]
    fn test_shortcut_hint_platform_aware() {
        assert_eq!(shortcut_hint_for(true), "cmd K");
        assert_eq!(shortcut_hint_for(false), "ctrl K");
        let expected = if cfg!(target_os = "macos") {
            "cmd K"
        } else {
            "ctrl K"
        };
        assert_eq!(shortcut_hint(), expected);
    }

    #[test]
    fn test_feature_rows_are_in_mockup_order() {
        // The block is built from this exact sequence; keep it pinned.
        let order = [
            "Team",
            "Notebooks",
            "Design",
            "Extensions",
            "Automations",
        ];
        assert_eq!(order.len(), 5);
        assert_eq!(order.first(), Some(&"Team"));
        assert_eq!(order.last(), Some(&"Automations"));
    }
}

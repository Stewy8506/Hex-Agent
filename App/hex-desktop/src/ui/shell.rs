//! Application-shell state container for HEX Desktop.
//!
//! Owns the sidebar view, message feed, composer, active pane, and the modal
//! stack. Layout and rendering live in `ChatView` (`src/chat.rs`); this type
//! only carries state shared across sessions.

use crate::acp_bridge::{AcpBridge, AcpEvent};
use crate::ui::modals::{
    ActiveModal, CommandPaletteModal, ModelPickerModal, ToolApprovalModal,
};
use crate::ui::sidebar::{ProviderState, SidebarView};
use crate::ui::tool_card::ToolCallCard;

/// Active view within the main pane of the application shell.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ShellView {
    /// Conversation message feed and prompt composer.
    #[default]
    Chat,
    /// Coming soon: file browser pane.
    Files,
    /// Coming soon: embedded browser pane.
    Browser,
    /// Coming soon: embedded terminal pane.
    Terminal,
    /// Settings and configuration editor.
    Settings,
}

impl ShellView {
    /// Human label for the view (header segment or empty-state title).
    pub fn pane_title(self) -> &'static str {
        match self {
            Self::Chat => "Chat",
            Self::Files => "Files",
            Self::Browser => "Browser",
            Self::Terminal => "Terminal",
            Self::Settings => "Settings",
        }
    }
}

// --- Mockup session-header metrics (docs/design mockups-desktop) ---

/// Header height in px.
pub const HEADER_HEIGHT: f32 = 60.0;
/// Header horizontal padding.
pub const HEADER_PADDING_X: f32 = 20.0;
/// Header icon-button SVG size.
pub const HEADER_ICON: f32 = 16.0;
/// Segmented control: container radius 12, padding 3, gap 2; buttons 32×32
/// radius 8 with 16px icons.
pub const SEGMENTED_RADIUS: f32 = 12.0;
pub const SEGMENTED_PADDING: f32 = 3.0;
pub const SEGMENTED_GAP: f32 = 2.0;
pub const SEGMENT_BUTTON: f32 = 32.0;
pub const SEGMENT_BUTTON_RADIUS: f32 = 8.0;
pub const SEGMENT_ICON: f32 = 16.0;

/// Top-level application shell GPUI component.
#[derive(Debug, Clone, PartialEq)]
pub struct AppShell {
    /// Left sidebar navigation view.
    pub sidebar: SidebarView,
    /// Message feed container.
    pub feed: crate::ui::feed::MessageFeed,
    /// Bottom multiline prompt composer.
    pub composer: crate::ui::composer::PromptComposer,
    /// Whether the left sidebar is currently collapsed (hidden).
    pub sidebar_collapsed: bool,
    /// Currently selected session ID, if any.
    pub active_session_id: Option<String>,
    /// Currently active main pane view.
    pub active_view: ShellView,
    /// Currently displayed modal overlay (Command Palette, Tool Approval, Model Picker).
    pub active_modal: ActiveModal,
    /// Modals waiting behind the active one; unresolved approvals are never dropped.
    pub modal_queue: std::collections::VecDeque<ActiveModal>,
    /// ACP communication bridge connecting to harness_core runtime.
    pub acp_bridge: Option<AcpBridge>,
}

impl Default for AppShell {
    fn default() -> Self {
        Self::new()
    }
}

impl AppShell {
    /// Construct a new default application shell.
    pub fn new() -> Self {
        Self {
            sidebar: SidebarView::new(),
            feed: crate::ui::feed::MessageFeed::new(),
            composer: crate::ui::composer::PromptComposer::new(),
            sidebar_collapsed: false,
            active_session_id: None,
            active_view: ShellView::Chat,
            active_modal: ActiveModal::None,
            modal_queue: std::collections::VecDeque::new(),
            acp_bridge: None,
        }
    }

    /// Construct a shell with a specific workspace name and git branch.
    pub fn with_workspace(
        workspace_name: impl Into<String>,
        git_branch: impl Into<String>,
    ) -> Self {
        Self {
            sidebar: SidebarView::with_workspace(workspace_name, git_branch),
            feed: crate::ui::feed::MessageFeed::new(),
            composer: crate::ui::composer::PromptComposer::new(),
            sidebar_collapsed: false,
            active_session_id: None,
            active_view: ShellView::Chat,
            active_modal: ActiveModal::None,
            modal_queue: std::collections::VecDeque::new(),
            acp_bridge: None,
        }
    }

    /// Attach ACP bridge to application shell.
    pub fn with_acp_bridge(mut self, bridge: AcpBridge) -> Self {
        self.acp_bridge = Some(bridge);
        self
    }

    /// Open Command Palette (Cmd+K).
    pub fn open_command_palette(&mut self) {
        let mut palette = CommandPaletteModal::new();
        palette.populate_sessions(&self.sidebar.session_list.sessions);
        self.push_modal(ActiveModal::Palette(palette));
    }

    /// Open Model Picker over the live backend catalog (B32).
    pub fn open_model_picker(&mut self, models: Vec<crate::ui::modals::ModelEntry>) {
        let current_model = self.composer.model_pill.model_name.clone();
        self.show_model_picker(ModelPickerModal::new(current_model, models));
    }

    pub fn show_model_picker(&mut self, picker: ModelPickerModal) {
        self.modal_queue
            .retain(|modal| !matches!(modal, ActiveModal::ModelPicker(_)));
        if matches!(self.active_modal, ActiveModal::ModelPicker(_)) {
            self.active_modal = ActiveModal::ModelPicker(picker);
        } else {
            self.push_modal(ActiveModal::ModelPicker(picker));
        }
    }

    /// Open the application identity panel.
    pub fn open_about(&mut self) {
        self.push_modal(ActiveModal::About);
    }

    /// Open Tool Approval modal.
    ///
    /// B29: a second approval (or any modal opened over an unresolved one) is
    /// queued, never substituted; duplicate deliveries of the same request are
    /// ignored so the operator cannot decide the same request twice.
    pub fn open_approval(&mut self, modal: ToolApprovalModal) {
        let already_present = match &self.active_modal {
            ActiveModal::Approval(active) => active.request_id == modal.request_id,
            _ => false,
        } || self.modal_queue.iter().any(|queued| match queued {
            ActiveModal::Approval(active) => active.request_id == modal.request_id,
            _ => false,
        });
        if already_present {
            return;
        }
        self.push_modal(ActiveModal::Approval(modal));
    }

    /// Show a modal now, or queue it behind the active one.
    fn push_modal(&mut self, modal: ActiveModal) {
        if matches!(self.active_modal, ActiveModal::None) {
            self.active_modal = modal;
        } else {
            self.modal_queue.push_back(modal);
        }
    }

    /// Dismiss the active modal and promote the next queued one, if any.
    pub fn dismiss_modal(&mut self) {
        self.active_modal.close();
        self.active_modal = self.modal_queue.pop_front().unwrap_or(ActiveModal::None);
    }

    /// Handle high-level events incoming from ACP bridge.
    pub fn handle_acp_event(&mut self, event: AcpEvent) {
        match event {
            AcpEvent::Connected { server_name, .. } => {
                self.sidebar.provider_status.state = ProviderState::Connected;
                self.sidebar.provider_status.provider_name = server_name;
            }
            AcpEvent::Disconnected { .. } => {
                self.sidebar.provider_status.state = ProviderState::Offline;
            }
            AcpEvent::TokenChunk { text, .. } => {
                if !self.feed.is_streaming {
                    self.feed.start_assistant_streaming();
                }
                self.feed.append_streaming_chunk(&text);
            }
            AcpEvent::TurnComplete { .. } => {
                self.feed.finish_streaming();
            }
            AcpEvent::TurnUsage { .. } => {
                // Gauge update is owned by ChatView's route_event; shell keeps
                // no gauge state of its own.
            }
            AcpEvent::ThoughtChunk { .. } => {
                // Reasoning state is owned by ChatView; the static shell path
                // renders no thinking disclosure.
            }
            AcpEvent::ToolCall { call_id, tool_name, arguments_json, .. } => {
                let card = ToolCallCard::new(call_id, tool_name, arguments_json);
                self.feed.add_tool_call(card);
            }
            AcpEvent::ToolResult { call_id, output_text, is_error, diff, .. } => {
                self.feed.update_tool_result(&call_id, &output_text, is_error, diff.as_deref());
            }
            AcpEvent::ApprovalRequest { approval_id, session_id, tool_name, risk_level, command, description, .. } => {
                let mut modal = ToolApprovalModal::new(approval_id, tool_name, command, risk_level);
                modal.session_id = Some(session_id.clone());
                if let Some(desc) = description {
                    modal = modal.with_description(desc);
                }
                self.open_approval(modal);
            }
            AcpEvent::StatusUpdate { provider_name, is_healthy, .. } => {
                self.sidebar.provider_status.provider_name = provider_name;
                self.sidebar.provider_status.state = if is_healthy {
                    ProviderState::Connected
                } else {
                    ProviderState::Offline
                };
            }
            AcpEvent::CloudAccount(_) => {
                // Routed by ChatView (footer + notices); shell keeps no state.
            }
        }
    }

    /// Toggle the collapsed state of the left sidebar (e.g. Cmd+B shortcut).
    pub fn toggle_sidebar(&mut self) {
        self.sidebar_collapsed = !self.sidebar_collapsed;
    }

    /// Explicitly set the collapsed state of the left sidebar.
    pub fn set_sidebar_collapsed(&mut self, collapsed: bool) {
        self.sidebar_collapsed = collapsed;
    }

    /// Select an active session by ID.
    pub fn select_session(&mut self, session_id: impl Into<String>) {
        let id = session_id.into();
        self.sidebar.select_session(id.clone());
        self.active_session_id = Some(id);
    }

    /// Create and switch to a brand new conversation session.
    pub fn new_chat(&mut self, title: impl Into<String>) -> String {
        let new_id = format!("ses_{}", chrono::Utc::now().timestamp_millis());
        let item = crate::ui::session_list::SessionItem::new(new_id.clone(), title);
        self.sidebar.add_session(item);
        self.select_session(new_id.clone());
        new_id
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::HermesTheme;
    use core::prelude::v1::test;
    use gpui::*;

    #[test]
    fn test_header_tokens_match_mockup() {
        let theme = HermesTheme::default();
        assert_eq!(HEADER_HEIGHT, 60.0);
        assert_eq!(HEADER_PADDING_X, 20.0);
        assert_eq!(HEADER_ICON, 16.0);
        assert_eq!(SEGMENTED_RADIUS, 12.0);
        assert_eq!(SEGMENTED_PADDING, 3.0);
        assert_eq!(SEGMENTED_GAP, 2.0);
        assert_eq!(SEGMENT_BUTTON, 32.0);
        assert_eq!(SEGMENT_BUTTON_RADIUS, 8.0);
        assert_eq!(SEGMENT_ICON, 16.0);
        // Segmented container #191919 / border #232323; active button #262626.
        assert_eq!(theme.mockup_dock, rgb(0x191919));
        assert_eq!(theme.mockup_dock_border, rgb(0x232323));
        assert_eq!(theme.mockup_seg_active, rgb(0x262626));
    }

    #[test]
    fn test_app_shell_defaults_and_toggles() {
        let mut shell = AppShell::new();
        assert!(!shell.sidebar_collapsed);
        assert_eq!(shell.active_view, ShellView::Chat);
        assert!(shell.active_session_id.is_none());

        // Toggle sidebar (Cmd+B)
        shell.toggle_sidebar();
        assert!(shell.sidebar_collapsed);
        shell.toggle_sidebar();
        assert!(!shell.sidebar_collapsed);

        shell.set_sidebar_collapsed(true);
        assert!(shell.sidebar_collapsed);

        // New chat creation
        let session_id = shell.new_chat("First Exploration");
        assert_eq!(shell.active_session_id.as_deref(), Some(session_id.as_str()));
        assert_eq!(
            shell.sidebar.session_list.selected_session().unwrap().title,
            "First Exploration"
        );
    }

    #[test]
    fn test_app_shell_modal_management() {
        let mut shell = AppShell::new();
        assert_eq!(shell.active_modal, ActiveModal::None);

        // Open Command Palette
        shell.open_command_palette();
        assert!(matches!(shell.active_modal, ActiveModal::Palette(_)));

        // Dismiss Modal
        shell.dismiss_modal();
        assert_eq!(shell.active_modal, ActiveModal::None);

        // Open Model Picker
        shell.open_model_picker(vec![crate::ui::modals::ModelEntry::new("gpt-4o", "GPT-4o", "OpenAI", false)]);
        assert!(matches!(shell.active_modal, ActiveModal::ModelPicker(_)));

        shell.dismiss_modal();
        assert_eq!(shell.active_modal, ActiveModal::None);

        // Open Approval
        let approval = ToolApprovalModal::new("req_1", "bash", "cargo build", "R3");
        shell.open_approval(approval);
        assert!(matches!(shell.active_modal, ActiveModal::Approval(_)));
    }

    #[test]
    fn test_app_shell_handle_acp_events() {
        let mut shell = AppShell::new();

        // 1. Connection status
        shell.handle_acp_event(AcpEvent::Connected {
            server_name: "harness_core_acp".to_string(),
            server_version: "2.0".to_string(),
        });
        assert_eq!(shell.sidebar.provider_status.state, ProviderState::Connected);
        assert_eq!(shell.sidebar.provider_status.provider_name, "harness_core_acp");

        // 2. Token chunks
        shell.handle_acp_event(AcpEvent::TokenChunk {
            session_id: "ses_1".to_string(),
            text: "Hello from ACP".to_string(),
        });
        assert_eq!(shell.feed.len(), 1);
        assert!(shell.feed.is_streaming);

        // 3. Turn complete
        shell.handle_acp_event(AcpEvent::TurnComplete {
            session_id: "ses_1".to_string(),
        });
        assert!(!shell.feed.is_streaming);

        // 4. Tool call
        shell.handle_acp_event(AcpEvent::ToolCall {
            session_id: "ses_1".to_string(),
            call_id: "tc_1".to_string(),
            tool_name: "bash".to_string(),
            arguments_json: r#"{"command": "ls -la"}"#.to_string(),
        });
        // 5. Tool result
        shell.handle_acp_event(AcpEvent::ToolResult {
            session_id: "ses_1".to_string(),
            call_id: "tc_1".to_string(),
            output_text: "file1.rs\nfile2.rs".to_string(),
            is_error: false,
            diff: None,
        });

        // 6. Approval request
        shell.handle_acp_event(AcpEvent::ApprovalRequest {
            session_id: "ses_1".to_string(),
            approval_id: "appr_1".to_string(),
            tool_name: "rm".to_string(),
            risk_level: "R4".to_string(),
            command: "rm -rf /".to_string(),
            description: Some("Dangerous root deletion".to_string()),
        });
        assert!(matches!(shell.active_modal, ActiveModal::Approval(_)));
    }

    #[test]
    fn test_second_approval_is_queued_never_substituted() {
        let mut shell = AppShell::new();
        shell.open_approval(ToolApprovalModal::new("appr_1", "bash", "cargo build", "R3"));
        shell.open_approval(ToolApprovalModal::new("appr_2", "rm", "rm file", "R4"));

        match &shell.active_modal {
            ActiveModal::Approval(active) => assert_eq!(active.request_id, "appr_1"),
            _ => panic!("first approval must stay active"),
        }
        assert_eq!(shell.modal_queue.len(), 1);

        // Dismissing the first promotes the queued second approval.
        shell.dismiss_modal();
        match &shell.active_modal {
            ActiveModal::Approval(active) => assert_eq!(active.request_id, "appr_2"),
            _ => panic!("queued approval must be promoted"),
        }
        assert!(shell.modal_queue.is_empty());

        shell.dismiss_modal();
        assert_eq!(shell.active_modal, ActiveModal::None);
    }

    #[test]
    fn test_duplicate_approval_deliveries_are_ignored() {
        let mut shell = AppShell::new();
        shell.open_approval(ToolApprovalModal::new("appr_1", "bash", "cargo build", "R3"));
        shell.open_approval(ToolApprovalModal::new("appr_2", "rm", "rm file", "R4"));

        // Redelivery of the active request and of the queued request.
        shell.open_approval(ToolApprovalModal::new("appr_1", "bash", "cargo build", "R3"));
        shell.open_approval(ToolApprovalModal::new("appr_2", "rm", "rm file", "R4"));

        assert_eq!(shell.modal_queue.len(), 1);
        shell.dismiss_modal();
        match &shell.active_modal {
            ActiveModal::Approval(active) => assert_eq!(active.request_id, "appr_2"),
            _ => panic!("expected queued approval"),
        }
        shell.dismiss_modal();
        assert_eq!(shell.active_modal, ActiveModal::None);
    }

    #[test]
    fn test_palette_and_picker_queue_behind_unresolved_approval() {
        let mut shell = AppShell::new();
        shell.open_approval(ToolApprovalModal::new("appr_1", "bash", "cargo build", "R3"));

        shell.open_command_palette();
        shell.open_model_picker(vec![crate::ui::modals::ModelEntry::new("gpt-4o", "GPT-4o", "OpenAI", false)]);
        assert!(matches!(shell.active_modal, ActiveModal::Approval(_)));
        assert_eq!(shell.modal_queue.len(), 2);

        shell.dismiss_modal();
        assert!(matches!(shell.active_modal, ActiveModal::Palette(_)));
        shell.dismiss_modal();
        assert!(matches!(shell.active_modal, ActiveModal::ModelPicker(_)));
        shell.dismiss_modal();
        assert_eq!(shell.active_modal, ActiveModal::None);
    }
}

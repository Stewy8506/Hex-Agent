#![recursion_limit = "1024"]
//! HEX Desktop: Native macOS Metal-accelerated AI Workbench Cockpit.

pub mod acp_bridge;
pub mod chat;
pub mod fonts;
pub mod menu;
pub mod platform;
pub mod preferences;
pub mod shortcuts;
pub mod theme;
pub mod ui;
pub mod window;

pub use acp_bridge::{AcpBridge, AcpEvent};
pub use chat::{init_app, open_chat_window, ChatView};
pub use menu::{application_menus, init_menus};
pub use shortcuts::{desktop_keybindings, init_shortcuts};

pub use theme::{
    contrast_ratio, relative_luminance, HermesTheme, MockupTypeScale, TypographyTokens,
};
pub use ui::{
    classify_session_date, format_reasoning_elapsed, format_relative_time, format_token_number,
    line_background, line_foreground, line_prefix, provider_dot_color, render_ascii_progress_bar,
    render_modal_container, ActiveModal, AppShell, AssistantMessageView, CommandPaletteModal,
    DiffView, GaugeThreshold, MessageFeed, MessageItem, ModelEntry, ModelPickerModal,
    ModelSelectorPill, PaletteAction, PaletteItem, PromptComposer, ProviderState, ProviderStatus,
    SendButton, SessionGroupCategory, SessionItem, SessionListView, SidebarView,
    ThinkingDisclosure, TokenGaugeWidget, ToolApprovalChoice, ToolApprovalModal, ToolApprovalResult,
    ToolCallCard, ToolStatus, UserMessageCard,
};
pub use ui::feed::{ApprovalCard, ApprovalCardState};
pub use window::WindowConfig;

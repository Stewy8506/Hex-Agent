//! GPUI user interface components, views, and navigation shell for HEX Desktop.

pub mod composer;
pub mod diff_view;
pub mod feed;
pub mod message;
pub mod modals;
pub mod model_pill;
pub mod reasoning;
pub mod session_list;
pub mod shell;
pub mod sidebar;
pub mod token_gauge;
pub mod tool_card;

pub use composer::PromptComposer;
pub use diff_view::{line_background, line_foreground, line_prefix, DiffView};
pub use feed::{MessageFeed, MessageItem};
pub use message::{format_message_time, AssistantMessageView, UserMessageCard};
pub use modals::{
    render_modal_container, ActiveModal, CommandPaletteModal, ModelEntry, ModelPickerModal,
    PaletteAction, PaletteItem, ToolApprovalChoice, ToolApprovalModal, ToolApprovalResult,
};
pub use model_pill::{provider_dot_color, ModelSelectorPill, SendButton};
pub use reasoning::{format_reasoning_elapsed, ThinkingDisclosure};
pub use session_list::{
    classify_session_date, format_relative_time, SessionGroupCategory, SessionItem,
    SessionListView,
};
pub use shell::{AppShell, ShellView};
pub use sidebar::{ProviderState, ProviderStatus, SidebarView};
pub use token_gauge::{
    format_token_number, render_ascii_progress_bar, GaugeThreshold, TokenGaugeWidget,
};
pub use tool_card::{tool_icon, ToolCallCard, ToolStatus};

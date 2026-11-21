pub mod home;
pub use home::home_tooltip;
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

/// Renders a bundled stroke SVG icon at the given pixel size, tinted via
/// `text_color` on the parent (SVGs use `stroke="currentColor"`).
pub fn icon_svg(name: &str, size: f32) -> gpui::Svg {
    use gpui::{px, svg, Styled};
    svg()
        .path(format!("icons/{name}.svg"))
        .w(px(size))
        .h(px(size))
        .flex_none()
}

//! Modal dialogs and overlay views for HEX Desktop.
//!
//! Provides elevated overlay cards with backdrop blur:
//! - Tool Approval Modal (`approval.rs`): 3-way radio selection, 15m lease auto-approval toggle.
//! - Command Palette Modal (`palette.rs`): Fuzzy search across actions, navigation, and sessions.
//! - Model Picker Modal (`model_picker.rs`): Provider-grouped model browser with context badges.

pub mod approval;
pub mod model_picker;
pub mod palette;

pub use approval::{ToolApprovalChoice, ToolApprovalModal, ToolApprovalResult};
pub use model_picker::{ModelEntry, ModelPickerModal};
pub use palette::{CommandPaletteModal, PaletteAction, PaletteItem};

use gpui::*;
use crate::theme::HermesTheme;

/// Active modal state within the application cockpit.
#[derive(Debug, Clone, PartialEq, Default)]
pub enum ActiveModal {
    /// No modal currently displayed.
    #[default]
    None,
    /// Privileged tool approval request dialog.
    Approval(ToolApprovalModal),
    /// Global command palette (Cmd+K).
    Palette(CommandPaletteModal),
    /// Model selector dialog.
    ModelPicker(ModelPickerModal),
}

impl ActiveModal {
    /// Returns true if any modal is currently active/open.
    pub fn is_open(&self) -> bool {
        !matches!(self, ActiveModal::None)
    }

    /// Dismiss/close the active modal.
    pub fn close(&mut self) {
        *self = ActiveModal::None;
    }
}

/// Helper function to wrap modal content with backdrop wash and centered card.
pub fn render_modal_container(content: Div, theme: &HermesTheme) -> Div {
    div()
        .absolute()
        .inset_0()
        .bg(rgba(0x09090bb3)) // rgba(0x09090b, 0.70)
        .flex()
        .items_center()
        .justify_center()
        .child(
            div()
                .bg(theme.card_bg())
                .border_1()
                .border_color(theme.border_structural())
                .rounded(px(12.0))
                .shadow_lg()
                .overflow_hidden()
                .child(content),
        )
}

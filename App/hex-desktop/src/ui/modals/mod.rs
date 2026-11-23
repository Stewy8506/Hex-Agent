//! Modal dialogs and overlay views for HEX Desktop.
//!
//! Provides elevated overlay cards with backdrop blur:
//! - Tool Approval Modal (`approval.rs`): 3-way radio selection, 15m lease auto-approval toggle.
//! - Command Palette Modal (`palette.rs`): Fuzzy search across actions, navigation, and sessions.
//! - Model Picker Modal (`model_picker.rs`): Provider-grouped model browser with context badges.
//! - Account Modal (`account.rs`): Cloud sign in / register / device management.

pub mod account;
pub mod approval;
pub mod model_picker;
pub mod palette;

pub use account::{AccountAction, AccountModal, AccountMode};
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
    /// Cloud account sign in / device management.
    Account(AccountModal),
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

/// Modal card corner radius (derived token language, plan P5: radius 14–24).
pub const MODAL_CARD_RADIUS: f32 = 16.0;

/// Width for form modals (account sign-in / device list).
pub const MODAL_CARD_WIDTH_WIDE: f32 = 440.0;

/// Backdrop scrim: 70% black over the `#141414` canvas (derived — modals are
/// not in the mockup).
pub const MODAL_SCRIM: Rgba = Rgba {
    r: 0.0,
    g: 0.0,
    b: 0.0,
    a: 179.0 / 255.0, // 0xb3
};

/// Helper function to wrap modal content with the backdrop scrim and centered
/// card: `#171717` card with a `#262626` border (derived — modals are not in
/// the mockup).
pub fn render_modal_container(content: Div, theme: &HermesTheme) -> Div {
    div()
        .absolute()
        .inset_0()
        .bg(MODAL_SCRIM)
        .flex()
        .items_center()
        .justify_center()
        .child(
            div()
                .bg(theme.mockup_surface)
                .border_1()
                .border_color(theme.mockup_border_strong)
                .rounded(px(MODAL_CARD_RADIUS))
                .shadow_lg()
                .overflow_hidden()
                .child(content),
        )
}

#[cfg(test)]
mod tests {
    // gpui re-exports a `test` macro that shadows the built-in attribute.
    use core::prelude::v1::test;
    use super::*;

    #[test]
    fn test_modal_container_tokens() {
        // Plan P5: modal card radius is derived within the 14–24 band.
        assert_eq!(MODAL_CARD_RADIUS, 16.0);
        assert!((14.0..=24.0).contains(&MODAL_CARD_RADIUS));

        // Scrim: 70% black over the #141414 canvas.
        assert_eq!(MODAL_SCRIM, rgba(0x000000b3));
        let scrim = MODAL_SCRIM;
        assert_eq!(scrim.r, 0.0);
        assert_eq!(scrim.g, 0.0);
        assert_eq!(scrim.b, 0.0);
        let alpha_pct = scrim.a * 100.0;
        assert!((69.0..=72.0).contains(&alpha_pct), "got {alpha_pct}%");

        // Card surface uses the shared mockup tokens, not legacy zinc colors.
        let theme = HermesTheme::default();
        assert_eq!(theme.mockup_surface, rgb(0x171717));
        assert_eq!(theme.mockup_border_strong, rgb(0x262626));
    }
}

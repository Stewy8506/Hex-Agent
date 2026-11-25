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
pub mod session_actions;
pub mod settings;
pub mod text_input;

pub use account::{AccountAction, AccountHandler, AccountInteraction, AccountModal, AccountMode};
pub use approval::{ToolApprovalChoice, ToolApprovalModal, ToolApprovalResult};
pub use model_picker::{ModelEntry, ModelPickerModal};
pub use palette::{CommandPaletteModal, PaletteAction, PaletteItem};
pub use session_actions::{
    SessionActionHandler, SessionActionInteraction, SessionActionModal, SessionActionOutcome,
    SessionSearchModal,
};
pub use settings::{SettingsAction, SettingsHandler, SettingsInteraction, SettingsModal};

use gpui::*;
use crate::theme::HermesTheme;
use crate::ui::motion::{MotionSystem, SpringKey, SNAPPY};

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
    /// Per-session actions (rename / pin / archive / delete).
    SessionAction(SessionActionModal),
    /// Live chat search over the session list.
    SessionSearch(SessionSearchModal),
    /// Editable settings backed by ACP config/get + config/set (DX3).
    Settings(SettingsModal),
    /// Application identity and version information.
    About,
}

impl ActiveModal {
    /// Returns true if any modal is currently active/open.
    pub fn is_open(&self) -> bool {
        !matches!(self, ActiveModal::None)
    }

    pub fn same_instance(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Approval(current), Self::Approval(other)) => {
                current.request_id == other.request_id
            }
            (Self::SessionAction(current), Self::SessionAction(other)) => {
                current.session_id == other.session_id
            }
            _ => std::mem::discriminant(self) == std::mem::discriminant(other),
        }
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

/// Vertical card travel during modal entrance and exit.
pub const MODAL_CARD_RISE: f32 = 12.0;

/// Minimum space retained between a modal card and the viewport edges.
pub const MODAL_VIEWPORT_GUTTER: f32 = 16.0;

#[derive(Clone, Copy)]
pub struct ModalMotion<'a> {
    pub owner: EntityId,
    pub cx: &'a App,
}

pub fn modal_press<T: InteractiveElement + Styled>(
    button: T,
    name: &'static str,
    motion: ModalMotion<'_>,
) -> T {
    let key = SpringKey::new(motion.owner, name);
    let press = MotionSystem::value_or(motion.cx, key, 0.0).clamp(0.0, 1.0);
    button
        .mt(px(press))
        .opacity(1.0 - 0.18 * press)
        .on_mouse_down(MouseButton::Left, move |_, window, cx| {
            MotionSystem::set_target(window, cx, key, 1.0, SNAPPY);
        })
        .on_mouse_up(MouseButton::Left, move |_, window, cx| {
            MotionSystem::set_target(window, cx, key, 0.0, SNAPPY);
        })
        .on_mouse_up_out(MouseButton::Left, move |_, window, cx| {
            MotionSystem::set_target(window, cx, key, 0.0, SNAPPY);
        })
}

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
pub fn render_modal_container(
    content: Div,
    theme: &HermesTheme,
    card_progress: f32,
    scrim_progress: f32,
    viewport_height: Pixels,
    instance: u64,
) -> Div {
    let card_progress = card_progress.clamp(0.0, 1.0);
    let scrim_progress = scrim_progress.clamp(0.0, 1.0);
    let mut scrim = MODAL_SCRIM;
    scrim.a *= scrim_progress;
    let gutter = px(MODAL_VIEWPORT_GUTTER);
    let max_height = (viewport_height - gutter * 2.0).max(px(1.0));

    div()
        .absolute()
        .inset_0()
        .child(div().absolute().inset_0().bg(scrim))
        .child(
            div()
                .absolute()
                .inset_0()
                .p(gutter)
                .flex()
                .items_center()
                .justify_center()
                .child(
                    div()
                        .mt(px(MODAL_CARD_RISE * (1.0 - card_progress)))
                        .opacity(scrim_progress)
                        .max_h(max_height)
                        .max_w_full()
                        .child(
                            div()
                                .id(SharedString::from(format!(
                                    "modal_card_scroll_{instance}"
                                )))
                                .debug_selector(|| "modal_card_scroll".to_string())
                                .max_h(max_height)
                                .max_w_full()
                                .overflow_y_scroll()
                                .bg(theme.mockup_surface)
                                .border_1()
                                .border_color(theme.mockup_border_strong)
                                .rounded(px(MODAL_CARD_RADIUS))
                                .shadow_lg()
                                .child(content),
                        ),
                ),
        )
}

pub fn render_about(theme: &HermesTheme) -> Div {
    div()
        .w(px(420.0))
        .max_w_full()
        .flex()
        .flex_col()
        .gap(px(12.0))
        .px(px(24.0))
        .py(px(22.0))
        .child(
            div()
                .text_size(px(22.0))
                .font_weight(FontWeight::SEMIBOLD)
                .text_color(theme.mockup_text)
                .child("HEX Desktop"),
        )
        .child(
            div()
                .text_size(px(14.0))
                .text_color(theme.mockup_text_muted)
                .child("Autonomous AI workbench"),
        )
        .child(
            div()
                .mt(px(4.0))
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(12.0))
                .text_color(theme.mockup_text_dim)
                .child(format!("Version {}", env!("CARGO_PKG_VERSION"))),
        )
        .child(
            div()
                .mt(px(8.0))
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(12.0))
                .text_color(theme.mockup_text_dim)
                .child("Enter or Esc to close"),
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

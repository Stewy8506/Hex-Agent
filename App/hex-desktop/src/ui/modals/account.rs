//! Cloud Account modal: sign in / register / device management.

use gpui::prelude::FluentBuilder;
use gpui::*;

use hex_protocol::models::{CloudAuthStatus, CloudDevice};

use crate::theme::HermesTheme;
use crate::ui::icon_svg;
use crate::ui::modals::text_input::{button, select_all, text_field, ButtonTone, ModalClickHandler};
use crate::ui::modals::MODAL_CARD_WIDTH_WIDE;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AccountMode {
    Login,
    Register,
    Devices,
}

#[derive(Debug, Clone, PartialEq)]
pub enum AccountAction {
    SubmitLogin { email: String, password: String },
    SubmitRegister { email: String, password: String },
    Logout,
    RevokeDevice(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AccountConfirmation {
    RevokeDevice(usize),
    Logout,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AccountInteraction {
    FocusField(usize),
    Submit,
    ToggleMode,
    ActivateRow(usize),
    Confirm,
    Cancel,
}

pub type AccountHandler = std::rc::Rc<
    dyn Fn(AccountInteraction, &ClickEvent, &mut Window, &mut App),
>;

#[derive(Debug, Clone)]
pub struct AccountModal {
    pub mode: AccountMode,
    pub email: String,
    pub password: String,
    pub email_cursor: usize,
    pub email_anchor: usize,
    pub password_cursor: usize,
    pub password_anchor: usize,
    pub focus_index: usize,
    pub status: Option<CloudAuthStatus>,
    pub devices: Vec<CloudDevice>,
    pub selected_row: usize,
    pub busy: bool,
    pub error: Option<String>,
    pub notice: Option<String>,
    confirmation: Option<AccountConfirmation>,
    confirm_focused: bool,
    scroll_handle: UniformListScrollHandle,
}

impl PartialEq for AccountModal {
    fn eq(&self, other: &Self) -> bool {
        self.mode == other.mode
            && self.email == other.email
            && self.password == other.password
            && self.email_cursor == other.email_cursor
            && self.email_anchor == other.email_anchor
            && self.password_cursor == other.password_cursor
            && self.password_anchor == other.password_anchor
            && self.focus_index == other.focus_index
            && self.status == other.status
            && self.devices == other.devices
            && self.selected_row == other.selected_row
            && self.busy == other.busy
            && self.error == other.error
            && self.notice == other.notice
            && self.confirmation == other.confirmation
            && self.confirm_focused == other.confirm_focused
    }
}

impl Default for AccountModal {
    fn default() -> Self {
        Self::new(None)
    }
}

impl AccountModal {
    pub fn new(status: Option<CloudAuthStatus>) -> Self {
        let logged_in = status.as_ref().map(|status| status.logged_in).unwrap_or(false);
        let email = status
            .as_ref()
            .and_then(|status| status.email.clone())
            .unwrap_or_default();
        let email_cursor = email.len();
        Self {
            mode: if logged_in {
                AccountMode::Devices
            } else {
                AccountMode::Login
            },
            email,
            password: String::new(),
            email_cursor,
            email_anchor: email_cursor,
            password_cursor: 0,
            password_anchor: 0,
            focus_index: 0,
            status,
            devices: Vec::new(),
            selected_row: 0,
            busy: false,
            error: None,
            notice: None,
            confirmation: None,
            confirm_focused: true,
            scroll_handle: UniformListScrollHandle::new(),
        }
    }

    pub fn set_devices(&mut self, devices: Vec<CloudDevice>) {
        self.devices = devices;
        self.selected_row = self.selected_row.min(self.row_count().saturating_sub(1));
        self.scroll_selected_into_view();
    }

    pub fn set_focus_field(&mut self, index: usize) {
        if !matches!(self.mode, AccountMode::Login | AccountMode::Register) || self.busy {
            return;
        }
        self.focus_index = index.min(3);
    }

    pub fn focus_password(&self) -> bool {
        self.focus_index == 1
    }

    pub fn accepts_text_edit(&self) -> bool {
        !self.busy
            && self.confirmation.is_none()
            && matches!(self.mode, AccountMode::Login | AccountMode::Register)
            && self.focus_index < 2
    }

    fn focused_text(&mut self) -> Option<(&mut String, &mut usize, &mut usize)> {
        if self.busy || self.confirmation.is_some() || self.mode == AccountMode::Devices {
            return None;
        }
        match self.focus_index {
            0 => Some((&mut self.email, &mut self.email_cursor, &mut self.email_anchor)),
            1 => Some((&mut self.password, &mut self.password_cursor, &mut self.password_anchor)),
            _ => None,
        }
    }

    pub fn push_char(&mut self, text: &str) {
        let Some((value, cursor, anchor)) = self.focused_text() else {
            return;
        };
        crate::ui::modals::text_input::insert_text(value, cursor, anchor, text);
    }

    pub fn backspace(&mut self) {
        let Some((value, cursor, anchor)) = self.focused_text() else {
            return;
        };
        crate::ui::modals::text_input::backspace(value, cursor, anchor);
    }

    pub fn delete(&mut self) {
        let Some((value, cursor, anchor)) = self.focused_text() else {
            return;
        };
        crate::ui::modals::text_input::delete(value, cursor, anchor);
    }

    pub fn move_cursor(&mut self, delta: isize, selecting: bool) {
        let Some((value, cursor, anchor)) = self.focused_text() else {
            return;
        };
        crate::ui::modals::text_input::move_horizontal(value, cursor, anchor, delta, selecting);
    }

    pub fn move_cursor_to(&mut self, offset: usize, selecting: bool) {
        let Some((value, cursor, anchor)) = self.focused_text() else {
            return;
        };
        crate::ui::modals::text_input::move_to(value, cursor, anchor, offset, selecting);
    }

    pub fn move_cursor_to_edge(&mut self, end: bool, selecting: bool) {
        let Some((value, cursor, anchor)) = self.focused_text() else {
            return;
        };
        crate::ui::modals::text_input::move_to_edge(value, cursor, anchor, end, selecting);
    }

    pub fn select_all_text(&mut self) {
        let Some((value, cursor, anchor)) = self.focused_text() else {
            return;
        };
        select_all(value, cursor, anchor);
    }

    pub fn selected_text(&self) -> Option<String> {
        if self.busy || self.confirmation.is_some() || self.mode == AccountMode::Devices {
            return None;
        }
        let (value, cursor, anchor) = if self.focus_password() {
            (&self.password, self.password_cursor, self.password_anchor)
        } else {
            (&self.email, self.email_cursor, self.email_anchor)
        };
        Some(crate::ui::modals::text_input::selected_text(value, cursor, anchor))
    }

    fn row_count(&self) -> usize {
        self.devices.len() + 1
    }

    pub fn select_row(&mut self, row: usize) {
        if self.mode != AccountMode::Devices || self.busy {
            return;
        }
        self.selected_row = row.min(self.row_count().saturating_sub(1));
        self.scroll_selected_into_view();
    }

    fn move_selection(&mut self, delta: isize) {
        let count = self.row_count() as isize;
        let next = (self.selected_row as isize + delta).rem_euclid(count);
        self.select_row(next as usize);
    }

    fn scroll_selected_into_view(&self) {
        self.scroll_handle
            .scroll_to_item(self.selected_row, ScrollStrategy::Center);
    }

    pub fn toggle_mode(&mut self) {
        if self.busy || self.mode == AccountMode::Devices {
            return;
        }
        self.mode = match self.mode {
            AccountMode::Login => AccountMode::Register,
            AccountMode::Register => AccountMode::Login,
            AccountMode::Devices => AccountMode::Devices,
        };
        self.focus_index = 0;
    }

    fn submit_credentials(&self) -> Option<AccountAction> {
        if self.busy || self.mode == AccountMode::Devices {
            return None;
        }
        let email = self.email.trim().to_string();
        let password = self.password.clone();
        if email.is_empty() || password.is_empty() {
            return None;
        }
        match self.mode {
            AccountMode::Login => Some(AccountAction::SubmitLogin { email, password }),
            AccountMode::Register => Some(AccountAction::SubmitRegister { email, password }),
            AccountMode::Devices => None,
        }
    }

    fn arm_selected_action(&mut self) {
        if self.busy || self.mode != AccountMode::Devices {
            return;
        }
        self.confirmation = if self.selected_row < self.devices.len() {
            Some(AccountConfirmation::RevokeDevice(self.selected_row))
        } else {
            Some(AccountConfirmation::Logout)
        };
        self.confirm_focused = true;
    }

    pub fn cancel_confirmation(&mut self) -> bool {
        self.confirmation.take().is_some()
    }

    fn confirm_action(&mut self) -> Option<AccountAction> {
        if !self.confirm_focused {
            self.confirmation = None;
            return None;
        }
        match self.confirmation.take()? {
            AccountConfirmation::Logout => Some(AccountAction::Logout),
            AccountConfirmation::RevokeDevice(index) => self
                .devices
                .get(index)
                .map(|device| AccountAction::RevokeDevice(device.device_record_id.clone())),
        }
    }

    pub fn handle_interaction(
        &mut self,
        interaction: AccountInteraction,
    ) -> Option<AccountAction> {
        if self.busy {
            return None;
        }
        match interaction {
            AccountInteraction::FocusField(index) => {
                self.set_focus_field(index);
                None
            }
            AccountInteraction::Submit => self.submit_credentials(),
            AccountInteraction::ToggleMode => {
                self.toggle_mode();
                None
            }
            AccountInteraction::ActivateRow(index) => {
                self.select_row(index);
                self.arm_selected_action();
                None
            }
            AccountInteraction::Confirm => self.confirm_action(),
            AccountInteraction::Cancel => {
                self.cancel_confirmation();
                None
            }
        }
    }

    pub fn handle_key(&mut self, key: &str) -> Option<AccountAction> {
        if self.busy {
            return None;
        }
        if self.confirmation.is_some() {
            return match key {
                "escape" => {
                    self.confirmation = None;
                    None
                }
                "tab" | "shift-tab" | "left" | "arrowleft" | "right" | "arrowright" => {
                    self.confirm_focused = !self.confirm_focused;
                    None
                }
                "enter" => self.confirm_action(),
                _ => None,
            };
        }
        match key {
            "escape" => None,
            "tab" | "shift-tab" => {
                if self.mode == AccountMode::Devices {
                    self.move_selection(if key == "tab" { 1 } else { -1 });
                } else {
                    let delta = if key == "tab" { 1 } else { -1 };
                    self.focus_index = (self.focus_index as isize + delta).rem_euclid(4) as usize;
                }
                None
            }
            "up" | "arrowup" => {
                if self.mode == AccountMode::Devices {
                    self.move_selection(-1);
                }
                None
            }
            "down" | "arrowdown" => {
                if self.mode == AccountMode::Devices {
                    self.move_selection(1);
                }
                None
            }
            "enter" => match self.mode {
                AccountMode::Devices => {
                    self.arm_selected_action();
                    None
                }
                _ if self.focus_index == 3 => {
                    self.toggle_mode();
                    None
                }
                _ => self.submit_credentials(),
            },
            _ => None,
        }
    }

    fn title(&self) -> &'static str {
        match self.mode {
            AccountMode::Login => "Sign in to HEX Cloud",
            AccountMode::Register => "Create a HEX Cloud account",
            AccountMode::Devices => "HEX Account",
        }
    }

    fn status_line(&self) -> String {
        match (&self.mode, &self.status) {
            (AccountMode::Devices, Some(status)) => {
                let device = status
                    .device_record_id
                    .clone()
                    .unwrap_or_else(|| "not enrolled".to_string());
                let email = status
                    .email
                    .clone()
                    .unwrap_or_else(|| "signed in".to_string());
                format!("{email} · device {device}")
            }
            (AccountMode::Devices, None) => "Loading account…".to_string(),
            _ => format!(
                "Backend: {}",
                self.status
                    .as_ref()
                    .map(|status| status.base_url.as_str())
                    .unwrap_or("not connected")
            ),
        }
    }

    pub fn render(&self, theme: &HermesTheme, on_action: AccountHandler) -> Div {
        let mut card = div()
            .w(px(MODAL_CARD_WIDTH_WIDE))
            .max_h(px(520.0))
            .flex()
            .flex_col()
            .child(
                div()
                    .flex()
                    .items_center()
                    .justify_between()
                    .px_4()
                    .py_3()
                    .border_b_1()
                    .border_color(theme.mockup_border)
                    .child(
                        div()
                            .flex()
                            .flex_col()
                            .child(
                                div()
                                    .text_size(px(14.0))
                                    .font_weight(FontWeight::SEMIBOLD)
                                    .text_color(theme.mockup_text_bright)
                                    .child(self.title()),
                            )
                            .child(
                                div()
                                    .text_size(px(10.5))
                                    .text_color(theme.mockup_text_dim)
                                    .child(self.status_line()),
                            ),
                    )
                    .child(
                        div()
                            .px_1p5()
                            .py_0p5()
                            .rounded(px(3.0))
                            .bg(theme.mockup_hover)
                            .border_1()
                            .border_color(theme.mockup_border)
                            .text_size(px(10.0))
                            .font_family(theme.font_mono()[0].clone())
                            .text_color(theme.mockup_text_dim)
                            .child("Esc"),
                    ),
            );

        card = card.child(if let Some(confirmation) = self.confirmation {
            self.render_confirmation(theme, confirmation, on_action.clone())
        } else {
            match self.mode {
                AccountMode::Devices => self.render_devices(theme, on_action.clone()),
                _ => self.render_credentials(theme, on_action.clone()),
            }
        });

        let hint = if self.confirmation.is_some() {
            "Tab — choose · Enter — confirm · Esc — cancel"
        } else {
            match self.mode {
                AccountMode::Login | AccountMode::Register => {
                    "Tab — focus · Enter — sign in / create · Esc — close"
                }
                AccountMode::Devices => "↑↓ / Tab — select · Enter — revoke / sign out",
            }
        };
        card.child(
            div()
                .flex()
                .items_center()
                .justify_between()
                .gap_3()
                .px_4()
                .py_2p5()
                .border_t_1()
                .border_color(theme.mockup_border)
                .child(
                    div()
                        .text_size(px(10.5))
                        .text_color(theme.mockup_text_dim)
                        .child(hint),
                )
                .child(self.render_status_message(theme)),
        )
    }

    fn render_status_message(&self, theme: &HermesTheme) -> Div {
        let (text, foreground) = if let Some(error) = &self.error {
            (error.clone(), theme.rose_fg)
        } else if let Some(notice) = &self.notice {
            (notice.clone(), theme.sage_fg)
        } else if self.busy {
            ("Working…".to_string(), theme.mockup_text_dim)
        } else {
            (String::new(), theme.mockup_text_dim)
        };
        div()
            .max_w(px(280.0))
            .text_size(px(10.5))
            .text_color(foreground)
            .text_ellipsis()
            .child(text)
    }

    fn click_handler(
        on_action: &AccountHandler,
        interaction: AccountInteraction,
    ) -> ModalClickHandler {
        let on_action = on_action.clone();
        std::rc::Rc::new(move |event, window, cx| on_action(interaction, event, window, cx))
    }

    fn render_credentials(&self, theme: &HermesTheme, on_action: AccountHandler) -> Div {
        let can_submit = !self.busy && !self.email.trim().is_empty() && !self.password.is_empty();
        let mode_label = match self.mode {
            AccountMode::Register => "Back to sign in",
            _ => "Create account",
        };
        div()
            .p_4()
            .flex()
            .flex_col()
            .gap_3()
            .child(text_field(
                "Email",
                &self.email,
                self.email_cursor,
                self.email_anchor,
                "you@example.com",
                false,
                self.focus_index == 0,
                self.busy,
                theme,
                Some(Self::click_handler(&on_action, AccountInteraction::FocusField(0))),
            ))
            .child(text_field(
                "Password",
                &self.password,
                self.password_cursor,
                self.password_anchor,
                "••••••••",
                true,
                self.focus_index == 1,
                self.busy,
                theme,
                Some(Self::click_handler(&on_action, AccountInteraction::FocusField(1))),
            ))
            .child(
                div()
                    .text_size(px(10.5))
                    .text_color(theme.mockup_text_dim)
                    .child(match self.mode {
                        AccountMode::Register => {
                            "Keys stay in the OS keychain; devices enroll with Ed25519 challenge signatures."
                        }
                        _ => "Credentials are verified against your hex-backend; tokens live in the OS keychain.",
                    }),
            )
            .child(
                div()
                    .flex()
                    .items_center()
                    .justify_between()
                    .gap_2()
                    .child(button(
                        mode_label,
                        ButtonTone::Neutral,
                        !self.busy,
                        self.focus_index == 3,
                        theme,
                        Self::click_handler(&on_action, AccountInteraction::ToggleMode),
                    ))
                    .child(button(
                        match self.mode {
                            AccountMode::Register => "Create account",
                            _ => "Sign in",
                        },
                        ButtonTone::Accent,
                        can_submit,
                        self.focus_index == 2,
                        theme,
                        Self::click_handler(&on_action, AccountInteraction::Submit),
                    )),
            )
    }

    fn render_confirmation(
        &self,
        theme: &HermesTheme,
        confirmation: AccountConfirmation,
        on_action: AccountHandler,
    ) -> Div {
        let (title, detail, action) = match confirmation {
            AccountConfirmation::Logout => (
                "Sign out of HEX Cloud?",
                "This device will stop using your cloud account. You can sign in again at any time."
                    .to_string(),
                "Sign out",
            ),
            AccountConfirmation::RevokeDevice(index) => {
                let id = self
                    .devices
                    .get(index)
                    .map(|device| device.device_record_id.as_str())
                    .unwrap_or("this device");
                (
                    "Revoke this device?",
                    format!("{id} will lose access immediately. This cannot be undone."),
                    "Revoke",
                )
            }
        };
        div()
            .p_5()
            .flex()
            .flex_col()
            .gap_3()
            .child(
                div()
                    .text_size(px(15.0))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.mockup_text)
                    .child(title),
            )
            .child(
                div()
                    .text_size(px(12.5))
                    .text_color(theme.mockup_text_dim)
                    .child(detail),
            )
            .child(
                div()
                    .flex()
                    .justify_end()
                    .gap_2()
                    .child(button(
                        "Cancel",
                        ButtonTone::Neutral,
                        true,
                        !self.confirm_focused,
                        theme,
                        Self::click_handler(&on_action, AccountInteraction::Cancel),
                    ))
                    .child(button(
                        action,
                        ButtonTone::Destructive,
                        true,
                        self.confirm_focused,
                        theme,
                        Self::click_handler(&on_action, AccountInteraction::Confirm),
                    )),
            )
    }

    fn render_devices(&self, theme: &HermesTheme, on_action: AccountHandler) -> Div {
        if self.devices.is_empty() && !self.busy {
            return div()
                .h(px(300.0))
                .p_4()
                .flex()
                .flex_col()
                .items_center()
                .justify_center()
                .gap_3()
                .child(
                    div()
                        .text_size(px(12.0))
                        .text_color(theme.mockup_text_dim)
                        .child("No enrolled devices"),
                )
                .child(button(
                    "Sign out on this device",
                    ButtonTone::Neutral,
                    true,
                    true,
                    theme,
                    Self::click_handler(&on_action, AccountInteraction::ActivateRow(0)),
                ));
        }

        let devices = self.devices.clone();
        let selected_row = self.selected_row;
        let busy = self.busy;
        let scroll_handle = self.scroll_handle.clone();
        let theme = theme.clone();
        let list = uniform_list(
            "account_device_rows",
            self.row_count(),
            move |range, _window, _cx| {
                let handler = on_action.clone();
                range
                    .map(|index| {
                        if index < devices.len() {
                            render_device_row(
                                &devices[index],
                                index,
                                index == selected_row,
                                busy,
                                &theme,
                                handler.clone(),
                            )
                        } else {
                            render_sign_out_row(
                                index == selected_row,
                                busy,
                                &theme,
                                handler.clone(),
                                devices.len(),
                            )
                        }
                        .into_any_element()
                    })
                    .collect::<Vec<_>>()
            },
        )
        .track_scroll(scroll_handle)
        .w_full()
        .h_full();
        div()
            .debug_selector(|| "account_device_list".to_string())
            .h(px(330.0))
            .w_full()
            .overflow_hidden()
            .child(list)
    }
}

fn render_device_row(
    device: &CloudDevice,
    index: usize,
    selected: bool,
    busy: bool,
    theme: &HermesTheme,
    on_action: AccountHandler,
) -> Stateful<Div> {
    let revoked = device.trust_state.as_deref() == Some("revoked");
    let host_tag = if device.host_eligible {
        "host-eligible"
    } else {
        "member"
    };
    let handler = AccountModal::click_handler(
        &on_action,
        AccountInteraction::ActivateRow(index),
    );
    div()
        .id(SharedString::from(format!("account_device_{}", device.device_record_id)))
        .h(px(56.0))
        .w_full()
        .flex()
        .items_center()
        .justify_between()
        .px_3()
        .rounded(px(8.0))
        .border_1()
        .border_color(if selected {
            theme.mockup_accent_border
        } else {
            gpui::rgba(0x00000000)
        })
        .bg(if selected {
            theme.mockup_accent_chip_bg
        } else {
            gpui::rgba(0x00000000)
        })
        .when(!busy, |this| {
            this.cursor_pointer()
                .hover(|this| this.bg(theme.mockup_hover))
                .on_click(move |event, window, cx| handler(event, window, cx))
        })
        .child(
            div()
                .flex()
                .items_center()
                .gap_2p5()
                .min_w_0()
                .child(
                    icon_svg(if revoked { "x" } else { "device" }, 14.0).text_color(if revoked {
                        theme.rose_fg
                    } else {
                        theme.mockup_text_muted
                    }),
                )
                .child(
                    div()
                        .flex()
                        .flex_col()
                        .min_w_0()
                        .child(
                            div()
                                .text_size(px(12.0))
                                .font_family(theme.font_mono()[0].clone())
                                .text_color(if selected {
                                    theme.mockup_text_bright
                                } else {
                                    theme.mockup_text_muted
                                })
                                .truncate()
                                .child(device.device_record_id.clone()),
                        )
                        .child(
                            div()
                                .text_size(px(10.0))
                                .text_color(theme.mockup_text_dim)
                                .child(format!(
                                    "{} · {host_tag}",
                                    device
                                        .platform
                                        .clone()
                                        .unwrap_or_else(|| "unknown".into()),
                                )),
                        ),
                ),
        )
        .child(
            div()
                .px_1p5()
                .py_0p5()
                .rounded(px(3.0))
                .bg(if revoked {
                    theme.rose_bg
                } else {
                    theme.sage_bg
                })
                .text_size(px(10.0))
                .text_color(if revoked {
                    theme.rose_fg
                } else {
                    theme.sage_fg
                })
                .child(if revoked { "revoked" } else { "trusted" }),
        )
}

fn render_sign_out_row(
    selected: bool,
    busy: bool,
    theme: &HermesTheme,
    on_action: AccountHandler,
    index: usize,
) -> Stateful<Div> {
    let handler = AccountModal::click_handler(
        &on_action,
        AccountInteraction::ActivateRow(index),
    );
    div()
        .id("account_sign_out")
        .h(px(56.0))
        .w_full()
        .flex()
        .items_center()
        .gap_2p5()
        .px_3()
        .rounded(px(8.0))
        .border_1()
        .border_color(if selected {
            theme.mockup_accent_border
        } else {
            gpui::rgba(0x00000000)
        })
        .bg(if selected {
            theme.mockup_accent_chip_bg
        } else {
            gpui::rgba(0x00000000)
        })
        .when(!busy, |this| {
            this.cursor_pointer()
                .hover(|this| this.bg(theme.mockup_hover))
                .on_click(move |event, window, cx| handler(event, window, cx))
        })
        .child(icon_svg("settings", 14.0).text_color(theme.mockup_text_muted))
        .child(
            div()
                .text_size(px(12.0))
                .text_color(if selected {
                    theme.mockup_text_bright
                } else {
                    theme.mockup_text_muted
                })
                .child("Sign out on this device"),
        )
}

#[cfg(test)]
mod tests {
    use core::prelude::v1::test;
    use super::*;

    fn logged_in_status() -> CloudAuthStatus {
        CloudAuthStatus {
            base_url: "http://127.0.0.1:8787".into(),
            logged_in: true,
            email: Some("dev@example.com".into()),
            user_id: Some("usr_1".into()),
            device_record_id: Some("dev_1".into()),
            device_enrolled: true,
            platform: Some("windows".into()),
        }
    }

    #[test]
    fn test_account_modal_modes() {
        let logged_out = AccountModal::new(None);
        assert_eq!(logged_out.mode, AccountMode::Login);

        let logged_in = AccountModal::new(Some(logged_in_status()));
        assert_eq!(logged_in.mode, AccountMode::Devices);
        assert_eq!(logged_in.email, "dev@example.com");
    }

    #[test]
    fn test_account_modal_typing_and_submit() {
        let mut modal = AccountModal::new(None);
        modal.push_char("d");
        modal.push_char("e");
        modal.push_char("v");
        assert_eq!(modal.email, "dev");
        assert_eq!(modal.password, "");
        assert!(!modal.focus_password());

        modal.handle_key("tab");
        assert!(modal.focus_password());
        modal.push_char("s");
        modal.backspace();
        modal.push_char("e");
        modal.push_char("c");
        assert_eq!(modal.password, "ec");

        modal.handle_key("tab");
        let action = modal.handle_key("enter");
        assert_eq!(modal.focus_index, 2);
        assert_eq!(
            action,
            Some(AccountAction::SubmitLogin {
                email: "dev".into(),
                password: "ec".into()
            })
        );

        let mut empty = AccountModal::new(None);
        assert_eq!(empty.handle_key("enter"), None);
    }

    #[test]
    fn test_account_modal_text_selection_and_mode_button() {
        let mut modal = AccountModal::new(None);
        modal.push_char("dev@example.com");
        modal.select_all_text();
        modal.push_char("new@example.com");
        assert_eq!(modal.email, "new@example.com");
        modal.handle_key("tab");
        modal.handle_key("tab");
        modal.handle_key("tab");
        assert_eq!(modal.focus_index, 3);
        modal.handle_key("enter");
        assert_eq!(modal.mode, AccountMode::Register);
    }

    #[test]
    fn test_account_modal_device_selection_and_confirmation() {
        let mut modal = AccountModal::new(Some(logged_in_status()));
        modal.set_devices(vec![
            CloudDevice {
                device_record_id: "dev_a".into(),
                user_id: None,
                encrypted_device_id: None,
                device_pubkey: None,
                platform: Some("windows".into()),
                trust_state: Some("trusted".into()),
                host_eligible: true,
                created_at: None,
                last_seen_at: None,
                revoked_at: None,
            },
            CloudDevice {
                device_record_id: "dev_b".into(),
                user_id: None,
                encrypted_device_id: None,
                device_pubkey: None,
                platform: Some("macos".into()),
                trust_state: Some("trusted".into()),
                host_eligible: false,
                created_at: None,
                last_seen_at: None,
                revoked_at: None,
            },
        ]);

        modal.handle_key("down");
        assert_eq!(modal.handle_key("enter"), None);
        assert!(modal.confirmation.is_some());
        assert!(modal.cancel_confirmation());
        modal.handle_key("down");
        modal.handle_key("enter");
        assert_eq!(modal.handle_key("enter"), Some(AccountAction::Logout));
    }
}

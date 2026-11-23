//! Cloud Account modal: sign in / register / device management.
//!
//! Two-field hand-rolled text entry (no GPUI editor element in modals yet):
//! typed characters arrive through `ChatView::on_key_down` and route into the
//! focused field. Keys: `tab` moves between fields, `shift+tab` toggles the
//! login/register mode, `enter` submits; on the device list `up`/`down` move
//! the selection and `enter` executes the highlighted row (revoke or sign out).

use gpui::*;

use hex_protocol::models::{CloudAuthStatus, CloudDevice};

use crate::theme::HermesTheme;
use crate::ui::icon_svg;
use crate::ui::modals::MODAL_CARD_WIDTH_WIDE;

/// Which surface the account modal currently shows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AccountMode {
    Login,
    Register,
    Devices,
}

/// Action requested by the modal; executed async by `ChatView`.
#[derive(Debug, Clone, PartialEq)]
pub enum AccountAction {
    SubmitLogin { email: String, password: String },
    SubmitRegister { email: String, password: String },
    Logout,
    RevokeDevice(String),
}

#[derive(Debug, Clone, PartialEq)]
pub struct AccountModal {
    pub mode: AccountMode,
    pub email: String,
    pub password: String,
    pub focus_password: bool,
    pub status: Option<CloudAuthStatus>,
    pub devices: Vec<CloudDevice>,
    pub selected_row: usize,
    pub busy: bool,
    pub error: Option<String>,
    pub notice: Option<String>,
}

impl Default for AccountModal {
    fn default() -> Self {
        Self::new(None)
    }
}

impl AccountModal {
    pub fn new(status: Option<CloudAuthStatus>) -> Self {
        let logged_in = status.as_ref().map(|s| s.logged_in).unwrap_or(false);
        Self {
            mode: if logged_in {
                AccountMode::Devices
            } else {
                AccountMode::Login
            },
            email: status
                .as_ref()
                .and_then(|s| s.email.clone())
                .unwrap_or_default(),
            password: String::new(),
            focus_password: false,
            status,
            devices: Vec::new(),
            selected_row: 0,
            busy: false,
            error: None,
            notice: None,
        }
    }

    pub fn set_devices(&mut self, devices: Vec<CloudDevice>) {
        self.devices = devices;
        self.selected_row = self.selected_row.min(self.row_count().saturating_sub(1));
    }

    /// Rows in the devices view: one per device + the trailing sign-out row.
    fn row_count(&self) -> usize {
        self.devices.len() + 1
    }

    pub fn push_char(&mut self, ch: &str) {
        if self.busy {
            return;
        }
        if self.focus_password {
            self.password.push_str(ch);
        } else {
            self.email.push_str(ch);
        }
    }

    pub fn backspace(&mut self) {
        if self.busy {
            return;
        }
        if self.focus_password {
            self.password.pop();
        } else {
            self.email.pop();
        }
    }

    fn toggle_focus(&mut self) {
        self.focus_password = !self.focus_password;
    }

    fn move_selection(&mut self, delta: isize) {
        if self.row_count() == 0 {
            return;
        }
        let count = self.row_count() as isize;
        let next = (self.selected_row as isize + delta).rem_euclid(count);
        self.selected_row = next as usize;
    }

    /// Handle non-printable keys. Printable characters and backspace route
    /// through `push_char`/`backspace` from the key-down listener.
    pub fn handle_key(&mut self, key: &str) -> Option<AccountAction> {
        match key {
            "escape" => None, // handled by ChatView (closes the modal)
            "tab" => match self.mode {
                AccountMode::Devices => None,
                _ => {
                    self.toggle_focus();
                    None
                }
            },
            "shift-tab" => match self.mode {
                AccountMode::Devices => None,
                AccountMode::Login => {
                    self.mode = AccountMode::Register;
                    None
                }
                AccountMode::Register => {
                    self.mode = AccountMode::Login;
                    None
                }
            },
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
            "enter" => {
                if self.busy {
                    return None;
                }
                let email = self.email.trim().to_string();
                let password = self.password.clone();
                match self.mode {
                    AccountMode::Login => {
                        if email.is_empty() || password.is_empty() {
                            return None;
                        }
                        Some(AccountAction::SubmitLogin { email, password })
                    }
                    AccountMode::Register => {
                        if email.is_empty() || password.is_empty() {
                            return None;
                        }
                        Some(AccountAction::SubmitRegister { email, password })
                    }
                    AccountMode::Devices => {
                        if self.selected_row < self.devices.len() {
                            Some(AccountAction::RevokeDevice(
                                self.devices[self.selected_row]
                                    .device_record_id
                                    .clone(),
                            ))
                        } else {
                            Some(AccountAction::Logout)
                        }
                    }
                }
            }
            _ => None,
        }
    }

    /// Titles the header by mode.
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
            _ => format!("Backend: {}", self.status.as_ref().map(|s| s.base_url.as_str()).unwrap_or("not connected")),
        }
    }

    /// Render the account modal card.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        let mut card = div()
            .w(px(MODAL_CARD_WIDTH_WIDE))
            .max_h(px(480.0))
            .flex()
            .flex_col();

        // Header: title, status line, Esc chip.
        card = card
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
                            .font_family(theme.typography.font_mono[0].clone())
                            .text_color(theme.mockup_text_dim)
                            .child("Esc"),
                    ),
            );

        // Body per mode.
        card = card.child(match self.mode {
            AccountMode::Devices => self.render_devices(theme),
            _ => self.render_credentials(theme),
        });

        // Footer hints + error/notice line.
        let hint = match self.mode {
            AccountMode::Login => {
                "Enter — sign in · Shift+Tab — create account · Tab — switch field"
            }
            AccountMode::Register => {
                "Enter — create account · Shift+Tab — back to sign in · Tab — switch field"
            }
            AccountMode::Devices => "↑↓ — select · Enter — revoke / sign out",
        };
        card = card.child(
            div()
                .flex()
                .items_center()
                .justify_between()
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
        );
        card
    }

    fn render_status_message(&self, theme: &HermesTheme) -> Div {
        let (text, fg) = if let Some(err) = &self.error {
            (err.clone(), theme.rose_fg)
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
            .text_color(fg)
            .text_ellipsis()
            .child(text)
    }

    /// One editable field row: label + value (password dots-masked) with an
    /// accent border while focused.
    fn render_field(&self, theme: &HermesTheme, label: &str, value: &str, focused: bool) -> Div {
        let masked = label == "Password";
        let display = if masked {
            "•".repeat(value.chars().count())
        } else {
            value.to_string()
        };
        div()
            .w_full()
            .rounded(px(8.0))
            .border_1()
            .border_color(if focused {
                theme.mockup_accent_border
            } else {
                theme.mockup_border
            })
            .bg(if focused {
                theme.mockup_accent_chip_bg
            } else {
                theme.mockup_hover
            })
            .px_3()
            .py_2()
            .flex()
            .flex_col()
            .gap_1()
            .child(
                div()
                    .text_size(px(10.0))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.mockup_text_dim)
                    .child(label.to_string()),
            )
            .child(
                div()
                    .text_size(px(12.5))
                    .font_family(theme.typography.font_mono[0].clone())
                    .text_color(if value.is_empty() {
                        theme.mockup_text_dim
                    } else {
                        theme.mockup_text_bright
                    })
                    .child(if value.is_empty() && !focused {
                        placeholder_for(label).to_string()
                    } else if focused {
                        format!("{display}▏")
                    } else {
                        display
                    }),
            )
    }

    fn render_credentials(&self, theme: &HermesTheme) -> Div {
        div()
            .p_4()
            .flex()
            .flex_col()
            .gap_3()
            .child(self.render_field(theme, "Email", &self.email, !self.focus_password))
            .child(self.render_field(
                theme,
                "Password",
                &self.password,
                self.focus_password,
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
    }

    fn render_devices(&self, theme: &HermesTheme) -> Div {
        let mut list = div().p_2().flex().flex_col().gap_1().overflow_y_hidden();

        if self.devices.is_empty() {
            list = list.child(
                div()
                    .py_6()
                    .flex()
                    .flex_col()
                    .items_center()
                    .gap_1()
                    .child(
                        div()
                            .text_size(px(12.0))
                            .text_color(theme.mockup_text_dim)
                            .child(if self.busy {
                                "Loading devices…"
                            } else {
                                "No enrolled devices"
                            }),
                    ),
            );
        } else {
            for (idx, device) in self.devices.iter().enumerate() {
                let is_selected = idx == self.selected_row;
                let revoked = device.trust_state.as_deref() == Some("revoked");
                let host_tag = if device.host_eligible {
                    "host-eligible"
                } else {
                    "member"
                };
                list = list.child(
                    div()
                        .flex()
                        .items_center()
                        .justify_between()
                        .px_3()
                        .py(px(7.0))
                        .rounded(px(8.0))
                        .border_1()
                        .border_color(if is_selected {
                            theme.mockup_accent_border
                        } else {
                            gpui::rgba(0x00000000)
                        })
                        .bg(if is_selected {
                            theme.mockup_accent_chip_bg
                        } else {
                            gpui::rgba(0x00000000)
                        })
                        .child(
                            div()
                                .flex()
                                .items_center()
                                .gap_2p5()
                                .child(
                                    icon_svg(if revoked { "x" } else { "device" }, 14.0)
                                        .text_color(if revoked {
                                            theme.rose_fg
                                        } else {
                                            theme.mockup_text_muted
                                        }),
                                )
                                .child(
                                    div()
                                        .flex()
                                        .flex_col()
                                        .child(
                                            div()
                                                .text_size(px(12.0))
                                                .font_family(
                                                    theme.typography.font_mono[0].clone(),
                                                )
                                                .text_color(if is_selected {
                                                    theme.mockup_text_bright
                                                } else {
                                                    theme.mockup_text_muted
                                                })
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
                        ),
                );
            }
        }

        // Trailing sign-out row (selected when selected_row == devices.len()).
        let sign_out_selected = self.selected_row >= self.devices.len();
        list = list.child(
            div()
                .flex()
                .items_center()
                .gap_2p5()
                .px_3()
                .py(px(7.0))
                .rounded(px(8.0))
                .border_1()
                .border_color(if sign_out_selected {
                    theme.mockup_accent_border
                } else {
                    gpui::rgba(0x00000000)
                })
                .bg(if sign_out_selected {
                    theme.mockup_accent_chip_bg
                } else {
                    gpui::rgba(0x00000000)
                })
                .child(
                    icon_svg("settings", 14.0).text_color(theme.mockup_text_muted),
                )
                .child(
                    div()
                        .text_size(px(12.0))
                        .text_color(if sign_out_selected {
                            theme.mockup_text_bright
                        } else {
                            theme.mockup_text_muted
                        })
                        .child("Sign out on this device"),
                ),
        );
        list
    }
}

fn placeholder_for(label: &str) -> &'static str {
    match label {
        "Email" => "you@example.com",
        "Password" => "••••••••",
        _ => "",
    }
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
        assert!(!modal.focus_password);

        modal.handle_key("tab");
        assert!(modal.focus_password);
        modal.push_char("s");
        modal.backspace(); // pops the "s"
        modal.push_char("e");
        modal.push_char("c");
        assert_eq!(modal.password, "ec");

        // Enter on login submits; empty email in register mode does not.
        modal.handle_key("shift-tab");
        modal.handle_key("shift-tab"); // back to login
        let action = modal.handle_key("enter");
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
    fn test_account_modal_device_selection() {
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
        assert_eq!(modal.handle_key("enter"), Some(AccountAction::RevokeDevice("dev_b".into())));
        modal.handle_key("down"); // row 2 = sign-out row
        assert_eq!(modal.handle_key("enter"), Some(AccountAction::Logout));
    }
}

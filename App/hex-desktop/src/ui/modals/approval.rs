//! Tool approval modal overlay with 3-way radio options and 15m lease auto-approval toggle.
//!
//! Conforms to Hermes Desktop approval cards and Phase 37 temporal lease architecture:
//! - Warning/shield header with tool name badge and risk classification tag (R3/R4)
//! - Command / script preview box
//! - 3-Way Radio: [1] Allow once (y), [2] Allow again for this session (a), [3] Deny (n)
//! - 15-minute lease toggle, valid only for the scoped choice: the grant binds
//!   to this exact action (B45) and carries a real server-validated TTL (B30)
//! - Primary Approve and Danger Deny actions

use gpui::*;
use serde::{Deserialize, Serialize};

use crate::theme::HermesTheme;
use crate::ui::icon_svg;
use crate::ui::modals::{modal_press, ModalMotion};
use crate::ui::payload::ScrollablePayload;

/// Operator decision choice for privileged tool approval.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ToolApprovalChoice {
    /// Allow this single execution only (hotkey `y` / `1`).
    Once,
    /// Allow this exact action again for the remainder of the active session's lease (hotkey `a` / `2`).
    Session,
    /// Deny tool invocation and halt execution (hotkey `n` / `3`).
    Deny,
}

impl ToolApprovalChoice {
    pub fn label(&self) -> &'static str {
        match self {
            ToolApprovalChoice::Once => "Allow once",
            ToolApprovalChoice::Session => "Allow again for this session",
            ToolApprovalChoice::Deny => "Deny",
        }
    }

    pub fn hotkey(&self) -> &'static str {
        match self {
            ToolApprovalChoice::Once => "y",
            ToolApprovalChoice::Session => "a",
            ToolApprovalChoice::Deny => "n",
        }
    }

    pub fn key_number(&self) -> &'static str {
        match self {
            ToolApprovalChoice::Once => "1",
            ToolApprovalChoice::Session => "2",
            ToolApprovalChoice::Deny => "3",
        }
    }
}

/// Result returned when an operator submits a tool approval decision.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolApprovalResult {
    pub request_id: String,
    pub choice: ToolApprovalChoice,
    pub lease_15m: bool,
    pub lease_duration_seconds: Option<u64>,
}

impl ToolApprovalResult {
    /// Returns the lease duration in seconds if a scoped lease was requested.
    pub fn lease_seconds(&self) -> Option<u64> {
        if self.lease_15m && self.choice == ToolApprovalChoice::Session {
            Some(self.lease_duration_seconds.unwrap_or(900))
        } else {
            None
        }
    }
}

#[derive(Debug, Clone)]
pub enum ApprovalInteraction {
    Choose(ToolApprovalChoice),
    ToggleLease,
    Submit(ToolApprovalResult),
}

pub type ApprovalHandler =
    std::rc::Rc<dyn Fn(ApprovalInteraction, &ClickEvent, &mut Window, &mut App)>;

/// Tool Approval modal state and rendering.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolApprovalModal {
    pub request_id: String,
    /// ACP session that raised the request, when known; drives the Home
    /// Continue list's "Review" chip.
    pub session_id: Option<String>,
    pub tool_name: String,
    pub risk_class: String,
    pub command: String,
    pub description: Option<String>,
    pub choice: ToolApprovalChoice,
    pub lease_15m: bool,
    pub is_submitting: bool,
}

impl ToolApprovalModal {
    /// Construct a new tool approval modal.
    pub fn new(
        request_id: impl Into<String>,
        tool_name: impl Into<String>,
        command: impl Into<String>,
        risk_class: impl Into<String>,
    ) -> Self {
        Self {
            request_id: request_id.into(),
            session_id: None,
            tool_name: tool_name.into(),
            risk_class: risk_class.into(),
            command: command.into(),
            description: None,
            choice: ToolApprovalChoice::Once,
            lease_15m: false,
            is_submitting: false,
        }
    }

    /// Attach an optional human-readable description of the tool operation.
    pub fn with_description(mut self, desc: impl Into<String>) -> Self {
        self.description = Some(desc.into());
        self
    }

    /// Select a decision radio choice.
    pub fn set_choice(&mut self, choice: ToolApprovalChoice) {
        self.choice = choice;
    }

    /// Toggle the 15-minute auto-approval lease checkbox.
    pub fn toggle_lease(&mut self) {
        self.lease_15m = !self.lease_15m;
    }

    /// Explicitly set the 15-minute lease toggle.
    pub fn set_lease(&mut self, enabled: bool) {
        self.lease_15m = enabled;
    }

    /// Handle keyboard shortcut interaction.
    pub fn handle_key(&mut self, key: &str) -> Option<ToolApprovalResult> {
        match key.to_lowercase().as_str() {
            "y" | "1" => {
                self.choice = ToolApprovalChoice::Once;
                None
            }
            "a" | "2" => {
                self.choice = ToolApprovalChoice::Session;
                None
            }
            "n" | "3" => {
                self.choice = ToolApprovalChoice::Deny;
                None
            }
            "l" => {
                self.toggle_lease();
                None
            }
            "enter" => Some(self.confirm()),
            "escape" => Some(ToolApprovalResult {
                request_id: self.request_id.clone(),
                choice: ToolApprovalChoice::Deny,
                lease_15m: false,
                lease_duration_seconds: None,
            }),
            _ => None,
        }
    }

    /// Confirm and produce the final approval decision result.
    pub fn confirm(&self) -> ToolApprovalResult {
        self.confirm_choice(self.choice)
    }

    fn confirm_choice(&self, choice: ToolApprovalChoice) -> ToolApprovalResult {
        let scoped_lease = self.lease_15m && choice == ToolApprovalChoice::Session;
        ToolApprovalResult {
            request_id: self.request_id.clone(),
            choice,
            lease_15m: scoped_lease,
            lease_duration_seconds: if scoped_lease { Some(900) } else { None },
        }
    }

    fn render_header(&self, theme: &HermesTheme) -> Div {
        let is_high_risk =
            self.risk_class.contains("R4") || self.risk_class.to_uppercase().contains("CRITICAL");
        let badge_bg = if is_high_risk {
            theme.rose_bg()
        } else {
            rgba(0xf59e0b26)
        };
        let badge_fg = if is_high_risk {
            theme.rose_fg()
        } else {
            theme.warning_amber()
        };

        div()
            .flex()
            .items_center()
            .justify_between()
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap_2()
                    .child(div().text_size(px(16.0)).child(icon_svg("design", 16.0)))
                    .child(
                        div()
                            .text_size(px(14.0))
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(theme.mockup_text_bright)
                            .child("Privileged Tool Approval"),
                    ),
            )
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap_2()
                    .child(
                        div()
                            .px_2()
                            .py_0p5()
                            .rounded(px(4.0))
                            .bg(theme.mockup_hover)
                            .border_1()
                            .border_color(theme.mockup_border)
                            .text_size(px(11.0))
                            .font_family(theme.typography.font_mono[0].clone())
                            .text_color(theme.accent_blue())
                            .child(self.tool_name.clone()),
                    )
                    .child(
                        div()
                            .px_2()
                            .py_0p5()
                            .rounded(px(4.0))
                            .bg(badge_bg)
                            .text_size(px(11.0))
                            .font_weight(FontWeight::MEDIUM)
                            .text_color(badge_fg)
                            .child(self.risk_class.clone()),
                    ),
            )
    }

    fn render_command_box(&self, theme: &HermesTheme) -> Div {
        div()
            .flex()
            .flex_col()
            .gap_1()
            .child(
                div()
                    .text_size(px(11.0))
                    .text_color(theme.mockup_text_dim)
                    .child("COMMAND / ARGUMENTS:"),
            )
            .child(
                div()
                    .w_full()
                    .min_w_0()
                    .max_h(px(200.0))
                    .scroll_both(&format!("approval_{}_command_scroll", self.request_id))
                    .p_3()
                    .rounded(px(6.0))
                    .bg(theme.mockup_body)
                    .border_1()
                    .border_color(theme.mockup_border)
                    .font_family(theme.typography.font_mono[0].clone())
                    .text_size(px(11.5))
                    .text_color(theme.mockup_text_bright)
                    .whitespace_nowrap()
                    .child(self.command.clone()),
            )
    }

    fn render_decision_box(&self, theme: &HermesTheme, on_action: &ApprovalHandler) -> Div {
        div()
            .flex()
            .flex_col()
            .gap_2()
            .p_3()
            .rounded(px(6.0))
            .bg(theme.mockup_hover)
            .border_1()
            .border_color(theme.mockup_border)
            .child(
                div()
                    .text_size(px(11.0))
                    .font_weight(FontWeight::MEDIUM)
                    .text_color(theme.mockup_text_dim)
                    .child("DECISION:"),
            )
            .child(self.render_radio_option(ToolApprovalChoice::Once, theme, on_action))
            .child(self.render_radio_option(ToolApprovalChoice::Session, theme, on_action))
            .child(self.render_radio_option(ToolApprovalChoice::Deny, theme, on_action))
    }

    fn render_lease_box(&self, theme: &HermesTheme, on_action: &ApprovalHandler) -> Stateful<Div> {
        let lease_active = self.lease_15m && self.choice == ToolApprovalChoice::Session;
        div()
            .flex()
            .items_center()
            .justify_between()
            .px_3()
            .py_2()
            .rounded(px(6.0))
            .bg(if lease_active {
                rgba(0x7aa7ff1a)
            } else {
                theme.mockup_hover
            })
            .border_1()
            .border_color(if lease_active {
                rgba(0x7aa7ff59)
            } else {
                theme.mockup_border
            })
            .id("approval_lease")
            .debug_selector(|| "approval_lease".into())
            .cursor_pointer()
            .on_click({
                let on_action = on_action.clone();
                move |event, window, cx| {
                    on_action(ApprovalInteraction::ToggleLease, event, window, cx)
                }
            })
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap_2()
                    .child(
                        div()
                            .w(px(16.0))
                            .h(px(16.0))
                            .rounded(px(4.0))
                            .border_1()
                            .border_color(if lease_active {
                                theme.accent_blue()
                            } else {
                                theme.mockup_border_strong
                            })
                            .bg(if lease_active {
                                theme.accent_blue()
                            } else {
                                theme.mockup_body
                            })
                            .flex()
                            .items_center()
                            .justify_center()
                            .text_size(px(10.0))
                            .text_color(if lease_active {
                                theme.mockup_body
                            } else {
                                theme.mockup_text_dim
                            })
                            .child(if lease_active { "✓" } else { "" }),
                    )
                    .child(
                        div()
                            .flex()
                            .flex_col()
                            .child(
                                div()
                                    .text_size(px(12.0))
                                    .font_weight(FontWeight::MEDIUM)
                                    .text_color(theme.mockup_text_bright)
                                    .child("Grant a 15-minute lease for this exact action"),
                            )
                            .child(
                                div()
                                    .text_size(px(10.5))
                                    .text_color(theme.mockup_text_dim)
                                    .child("Applies only to \"Allow again for this session\"; the identical command skips approval until the lease expires (hotkey: l)"),
                            ),
                    ),
            )
            .child(
                div()
                    .px_1p5()
                    .py_0p5()
                    .rounded(px(3.0))
                    .bg(theme.mockup_border)
                    .text_size(px(10.0))
                    .font_family(theme.typography.font_mono[0].clone())
                    .text_color(theme.mockup_text_dim)
                    .child("15m"),
            )
    }

    fn render_action_buttons(
        &self,
        theme: &HermesTheme,
        motion: ModalMotion<'_>,
        on_action: &ApprovalHandler,
    ) -> Div {
        div()
            .flex()
            .items_center()
            .justify_end()
            .gap_2()
            .pt_2()
            .child(modal_press(
                div()
                    .id("approval_deny")
                    .debug_selector(|| "approval_deny".into())
                    .px_3()
                    .py_1p5()
                    .rounded(px(6.0))
                    .border_1()
                    .border_color(rgba(0xc98b8b66))
                    .bg(theme.rose_bg())
                    .cursor_pointer()
                    .on_click({
                        let on_action = on_action.clone();
                        let result = self.confirm_choice(ToolApprovalChoice::Deny);
                        move |event, window, cx| {
                            on_action(
                                ApprovalInteraction::Submit(result.clone()),
                                event,
                                window,
                                cx,
                            )
                        }
                    })
                    .flex()
                    .items_center()
                    .gap_1()
                    .child(
                        div()
                            .text_size(px(12.0))
                            .text_color(theme.rose_fg())
                            .child("Deny"),
                    )
                    .child(
                        div()
                            .text_size(px(10.0))
                            .text_color(theme.rose_fg())
                            .child("(Esc)"),
                    ),
                "approval_deny_press",
                motion,
            ))
            .child(modal_press(
                div()
                    .id("approval_approve")
                    .debug_selector(|| "approval_approve".into())
                    .px_4()
                    .py_1p5()
                    .rounded(px(6.0))
                    .bg(theme.accent_blue())
                    .cursor_pointer()
                    .on_click({
                        let on_action = on_action.clone();
                        let result = self.confirm();
                        move |event, window, cx| {
                            on_action(
                                ApprovalInteraction::Submit(result.clone()),
                                event,
                                window,
                                cx,
                            )
                        }
                    })
                    .flex()
                    .items_center()
                    .gap_1()
                    .child(
                        div()
                            .text_size(px(12.0))
                            .font_weight(FontWeight::MEDIUM)
                            .text_color(theme.mockup_body)
                            .child(if self.choice == ToolApprovalChoice::Deny {
                                "Confirm Deny"
                            } else {
                                "Approve"
                            }),
                    )
                    .child(
                        div()
                            .text_size(px(10.0))
                            .text_color(theme.mockup_body)
                            .child("↵"),
                    ),
                "approval_approve_press",
                motion,
            ))
    }

    /// Render the tool approval modal card.
    pub fn render(
        &self,
        theme: &HermesTheme,
        motion: ModalMotion<'_>,
        on_action: ApprovalHandler,
    ) -> Div {
        let mut root = div()
            .w(px(520.0))
            .p_5()
            .flex()
            .flex_col()
            .gap_4()
            .child(self.render_header(theme));

        if let Some(ref desc) = self.description {
            root = root.child(
                div()
                    .text_size(px(12.0))
                    .text_color(theme.mockup_text_muted)
                    .child(desc.clone()),
            );
        }

        root.child(self.render_command_box(theme))
            .child(self.render_decision_box(theme, &on_action))
            .child(self.render_lease_box(theme, &on_action))
            .child(self.render_action_buttons(theme, motion, &on_action))
    }

    fn render_radio_option(
        &self,
        option: ToolApprovalChoice,
        theme: &HermesTheme,
        on_action: &ApprovalHandler,
    ) -> Stateful<Div> {
        let is_selected = self.choice == option;
        let active_fg = if option == ToolApprovalChoice::Deny {
            theme.rose_fg()
        } else {
            theme.accent_blue()
        };

        div()
            .flex()
            .items_center()
            .justify_between()
            .px_2()
            .py_1()
            .rounded(px(4.0))
            .bg(if is_selected {
                theme.mockup_body
            } else {
                gpui::rgba(0x00000000)
            })
            .cursor_pointer()
            .id(match option {
                ToolApprovalChoice::Once => "approval_choice_once",
                ToolApprovalChoice::Session => "approval_choice_session",
                ToolApprovalChoice::Deny => "approval_choice_deny",
            })
            .debug_selector(move || {
                match option {
                    ToolApprovalChoice::Once => "approval_choice_once",
                    ToolApprovalChoice::Session => "approval_choice_session",
                    ToolApprovalChoice::Deny => "approval_choice_deny",
                }
                .into()
            })
            .on_click({
                let on_action = on_action.clone();
                move |event, window, cx| {
                    on_action(ApprovalInteraction::Choose(option), event, window, cx)
                }
            })
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap_2()
                    .child(
                        div()
                            .w(px(14.0))
                            .h(px(14.0))
                            .rounded_full()
                            .border_1()
                            .border_color(if is_selected {
                                active_fg
                            } else {
                                theme.mockup_border_strong
                            })
                            .flex()
                            .items_center()
                            .justify_center()
                            .child(
                                div()
                                    .w(px(6.0))
                                    .h(px(6.0))
                                    .rounded_full()
                                    .bg(if is_selected {
                                        active_fg
                                    } else {
                                        gpui::rgba(0x00000000)
                                    }),
                            ),
                    )
                    .child(
                        div()
                            .text_size(px(12.0))
                            .text_color(if is_selected {
                                theme.mockup_text_bright
                            } else {
                                theme.mockup_text_muted
                            })
                            .child(format!("[{}] {}", option.key_number(), option.label())),
                    ),
            )
            .child(
                div()
                    .px_1p5()
                    .rounded(px(3.0))
                    .bg(theme.mockup_border)
                    .font_family(theme.typography.font_mono[0].clone())
                    .text_size(px(10.0))
                    .text_color(theme.mockup_text_dim)
                    .child(option.hotkey()),
            )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_tool_approval_modal_initialization() {
        let modal = ToolApprovalModal::new(
            "req_123",
            "execute_code",
            "rm -rf /tmp/test",
            "R4 - Critical",
        );
        assert_eq!(modal.request_id, "req_123");
        assert_eq!(modal.tool_name, "execute_code");
        assert_eq!(modal.choice, ToolApprovalChoice::Once);
        assert!(!modal.lease_15m);

        let res = modal.confirm();
        assert_eq!(res.choice, ToolApprovalChoice::Once);
        assert!(!res.lease_15m);
        assert_eq!(res.lease_seconds(), None);
    }

    #[test]
    fn test_tool_approval_modal_lease_toggle_and_shortcuts() {
        let mut modal = ToolApprovalModal::new("req_456", "bash", "cargo test --workspace", "R3");

        // Press 'a' to select Session
        assert!(modal.handle_key("a").is_none());
        assert_eq!(modal.choice, ToolApprovalChoice::Session);

        // Press 'l' to toggle lease
        assert!(modal.handle_key("l").is_none());
        assert!(modal.lease_15m);

        // Press Enter to confirm
        let res = modal.handle_key("enter").expect("expected confirmation");
        assert_eq!(res.choice, ToolApprovalChoice::Session);
        assert!(res.lease_15m);
        assert_eq!(res.lease_seconds(), Some(900));
    }

    #[test]
    fn test_tool_approval_deny_clears_lease() {
        let mut modal =
            ToolApprovalModal::new("req_789", "write_file", "writing sensitive file", "R3");
        modal.set_lease(true);
        modal.set_choice(ToolApprovalChoice::Deny);

        let res = modal.confirm();
        assert_eq!(res.choice, ToolApprovalChoice::Deny);
        // Deny should not grant temporal lease
        assert!(!res.lease_15m);
        assert_eq!(res.lease_seconds(), None);
    }

    #[test]
    fn test_once_choice_never_grants_lease_even_when_toggled() {
        let mut modal = ToolApprovalModal::new("req_once", "bash", "cargo build", "R3");
        modal.toggle_lease();
        assert!(modal.lease_15m);

        let res = modal.confirm();
        assert_eq!(res.choice, ToolApprovalChoice::Once);
        assert!(!res.lease_15m);
        assert_eq!(res.lease_seconds(), None);

        // Switching to the scoped choice makes the toggled lease real.
        modal.set_choice(ToolApprovalChoice::Session);
        let res = modal.confirm();
        assert!(res.lease_15m);
        assert_eq!(res.lease_seconds(), Some(900));
    }
}

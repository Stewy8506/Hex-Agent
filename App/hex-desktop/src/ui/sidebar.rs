//! Left sidebar navigation, workspace header, and provider status chip for HEX Desktop.
//!
//! Mirrors Hermes Desktop sidebar (`apps/desktop/src/app/chat/sidebar/`):
//! - Workspace selector & Git branch badge
//! - "New Chat" action trigger with `⌘N` keyboard badge
//! - Search filter for session history
//! - Pinned sessions & grouped chronological history
//! - Provider status dot & model display
//! - Bottom Settings & Profile footer

use std::rc::Rc;

use chrono::Utc;
use gpui::*;

use crate::theme::HermesTheme;
use crate::ui::session_list::{SessionItem, SessionListView};

/// Connectivity and operational state for the AI provider.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProviderState {
    /// Connected and ready to receive prompts (Green dot #8fbf93).
    Connected,
    /// Active generation or autonomous tool loop running (Amber dot #f59e0b).
    Generating,
    /// Connection severed, rate limited, or error state (Red dot #c98b8b).
    Offline,
}

impl ProviderState {
    pub fn dot_color(&self, theme: &HermesTheme) -> Rgba {
        match self {
            ProviderState::Connected => theme.sage_fg(),
            ProviderState::Generating => theme.warning_amber(),
            ProviderState::Offline => theme.rose_fg(),
        }
    }

    pub fn status_label(&self) -> &'static str {
        match self {
            ProviderState::Connected => "Connected",
            ProviderState::Generating => "Generating",
            ProviderState::Offline => "Offline",
        }
    }
}

/// Active provider and model status payload.
#[derive(Debug, Clone, PartialEq)]
pub struct ProviderStatus {
    pub state: ProviderState,
    pub provider_name: String,
    pub model_name: String,
}

impl Default for ProviderStatus {
    fn default() -> Self {
        Self {
            state: ProviderState::Connected,
            provider_name: "Anthropic".to_string(),
            model_name: "Claude 3.7 Sonnet".to_string(),
        }
    }
}

impl ProviderStatus {
    pub fn new(
        state: ProviderState,
        provider_name: impl Into<String>,
        model_name: impl Into<String>,
    ) -> Self {
        Self {
            state,
            provider_name: provider_name.into(),
            model_name: model_name.into(),
        }
    }

    pub fn display_label(&self) -> String {
        if self.model_name.is_empty() {
            self.provider_name.clone()
        } else {
            format!("{} {}", self.provider_name, self.model_name)
        }
    }
}

/// State and rendering container for the left navigation sidebar.
#[derive(Debug, Clone, PartialEq)]
pub struct SidebarView {
    pub workspace_name: String,
    pub git_branch: String,
    pub session_list: SessionListView,
    pub provider_status: ProviderStatus,
}

impl Default for SidebarView {
    fn default() -> Self {
        Self::new()
    }
}

impl SidebarView {
    pub fn new() -> Self {
        Self {
            workspace_name: "Harness".to_string(),
            git_branch: "experimental-features".to_string(),
            session_list: SessionListView::new(),
            provider_status: ProviderStatus::default(),
        }
    }

    pub fn with_workspace(
        workspace_name: impl Into<String>,
        git_branch: impl Into<String>,
    ) -> Self {
        Self {
            workspace_name: workspace_name.into(),
            git_branch: git_branch.into(),
            session_list: SessionListView::new(),
            provider_status: ProviderStatus::default(),
        }
    }

    pub fn set_provider_status(&mut self, status: ProviderStatus) {
        self.provider_status = status;
    }

    pub fn set_workspace(&mut self, name: impl Into<String>, branch: impl Into<String>) {
        self.workspace_name = name.into();
        self.git_branch = branch.into();
    }

    pub fn add_session(&mut self, session: SessionItem) {
        self.session_list.add_session(session);
    }

    pub fn select_session(&mut self, id: impl Into<String>) {
        self.session_list.select_session(id);
    }

    /// Renders the top Workspace Header with directory name and git branch pill.
    fn render_workspace_header(&self, theme: &HermesTheme) -> Div {
        div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .justify_between()
            .px_3()
            .py_2()
            .border_b_1()
            .border_color(theme.border_subtle())
            .child(
                div()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap_2()
                    .child(
                        div()
                            .text_sm()
                            .child("📁"),
                    )
                    .child(
                        div()
                            .text_sm()
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(theme.text_primary())
                            .child(self.workspace_name.clone()),
                    ),
            )
            .child(
                div()
                    .flex()
                    .items_center()
                    .px_2()
                    .py(px(2.0))
                    .rounded(px(4.0))
                    .bg(theme.card_bg())
                    .border_1()
                    .border_color(theme.border_structural())
                    .child(
                        div()
                            .text_xs()
                            .text_color(theme.text_muted())
                            .child(self.git_branch.clone()),
                    ),
            )
    }

    /// Renders the prominent "New Chat" Trigger Button with `⌘N` shortcut badge.
    fn render_new_chat_button(
        &self,
        theme: &HermesTheme,
        on_new_chat: Option<&Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>>,
    ) -> Div {
        let mut button = div()
            .id("new_chat_button")
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .justify_between()
            .px_3()
            .py_2()
            .rounded_md()
            .bg(theme.card_hover()) // #27272a
            .border_1()
            .border_color(theme.border_subtle())
            .cursor_pointer()
            .hover(|s| s.bg(theme.border_structural()));

        if let Some(on_new_chat) = on_new_chat {
            let on_new_chat = on_new_chat.clone();
            button = button.on_click(move |event, window, cx| {
                on_new_chat(event, window, cx);
            });
        }

        div().w_full().px_3().py_2().child(
            button
                .child(
                    div()
                        .flex()
                        .flex_row()
                        .items_center()
                        .gap_2()
                        .child(
                            div()
                                .text_sm()
                                .child("✏️"),
                        )
                        .child(
                            div()
                                .text_xs()
                                .font_weight(FontWeight::SEMIBOLD)
                                .text_color(theme.text_primary())
                                .child("New Chat"),
                        ),
                )
                .child(
                    div()
                        .flex()
                        .items_center()
                        .px(px(6.0))
                        .py(px(1.0))
                        .rounded(px(3.0))
                        .bg(theme.card_bg())
                        .border_1()
                        .border_color(theme.border_subtle())
                        .child(
                            div()
                                .text_xs()
                                .text_color(theme.text_dim())
                                .child("⌘N"),
                        ),
                ),
        )
    }

    /// Renders the Session Search filter input row.
    fn render_search_field(&self, theme: &HermesTheme) -> Div {
        div()
            .w_full()
            .px_3()
            .py_1()
            .child(
                div()
                    .w_full()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap_2()
                    .px_2()
                    .py_1()
                    .rounded_md()
                    .bg(theme.card_bg())
                    .border_1()
                    .border_color(theme.border_subtle())
                    .child(
                        div()
                            .text_xs()
                            .text_color(theme.text_dim())
                            .child("🔍"),
                    )
                    .child(
                        div()
                            .text_xs()
                            .text_color(if self.session_list.search_query.is_empty() {
                                theme.text_extra_dim()
                            } else {
                                theme.text_primary()
                            })
                            .child(if self.session_list.search_query.is_empty() {
                                "Search sessions...".to_string()
                            } else {
                                self.session_list.search_query.clone()
                            }),
                    ),
            )
    }

    /// Renders the Provider Status Chip with connection state dot and model label.
    fn render_provider_status(&self, theme: &HermesTheme) -> Div {
        let dot_color = self.provider_status.state.dot_color(theme);

        div()
            .w_full()
            .px_3()
            .py_2()
            .border_t_1()
            .border_color(theme.border_subtle())
            .child(
                div()
                    .w_full()
                    .flex()
                    .flex_row()
                    .items_center()
                    .justify_between()
                    .px_2()
                    .py(px(6.0))
                    .rounded_md()
                    .bg(theme.card_bg())
                    .border_1()
                    .border_color(theme.border_subtle())
                    .child(
                        div()
                            .flex()
                            .flex_row()
                            .items_center()
                            .gap_2()
                            .overflow_hidden()
                            .child(
                                // Status Dot
                                div()
                                    .w(px(8.0))
                                    .h(px(8.0))
                                    .rounded_full()
                                    .bg(dot_color),
                            )
                            .child(
                                div()
                                    .text_xs()
                                    .font_weight(FontWeight::NORMAL)
                                    .text_color(theme.text_muted())
                                    .child(self.provider_status.display_label()),
                            ),
                    )
                    .child(
                        div()
                            .text_xs()
                            .text_color(theme.text_dim())
                            .child("▾"),
                    ),
            )
    }

    /// Renders the bottom Settings and Profile footer.
    fn render_footer(&self, theme: &HermesTheme) -> Div {
        div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .justify_between()
            .px_3()
            .py_2()
            .border_t_1()
            .border_color(theme.border_subtle())
            .child(
                div()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap_2()
                    .cursor_pointer()
                    .hover(|s| s.text_color(theme.text_primary()))
                    .child(
                        div()
                            .text_xs()
                            .child("👤"),
                    )
                    .child(
                        div()
                            .text_xs()
                            .text_color(theme.text_muted())
                            .child("Operator"),
                    ),
            )
            .child(
                div()
                    .flex()
                    .items_center()
                    .cursor_pointer()
                    .hover(|s| s.text_color(theme.text_primary()))
                    .child(
                        div()
                            .text_xs()
                            .text_color(theme.text_muted())
                            .child("⚙️ Settings"),
                    ),
            )
    }

    /// Renders the full 260px sidebar.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        self.render_with_actions(theme, None, None)
    }

    /// Renders the sidebar with optional interactive handlers:
    /// `on_new_chat` for the New Chat button and `on_select_session(session_id)`
    /// for history rows. `ChatView` wires these to real backend operations.
    pub fn render_with_actions(
        &self,
        theme: &HermesTheme,
        on_new_chat: Option<Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>>,
        on_select_session: Option<Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)>>,
    ) -> Div {
        let now = Utc::now();

        div()
            .w(px(260.0))
            .h_full()
            .flex()
            .flex_col()
            .bg(theme.canvas_bg())
            .border_r_1()
            .border_color(theme.border_subtle())
            // Top Section: Workspace Header + New Chat + Search
            .child(self.render_workspace_header(theme))
            .child(self.render_new_chat_button(theme, on_new_chat.as_ref()))
            .child(self.render_search_field(theme))
            // Middle Scrollable Section: Sessions (Pinned & Grouped)
            .child(
                div()
                    .flex_1()
                    .overflow_y_hidden()
                    .px_1()
                    .child(self.session_list.render_with_select(theme, now, on_select_session)),
            )
            // Bottom Fixed Section: Provider Status + Footer
            .child(self.render_provider_status(theme))
            .child(self.render_footer(theme))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_provider_state_and_colors() {
        let theme = HermesTheme::default();

        let conn = ProviderState::Connected;
        assert_eq!(conn.status_label(), "Connected");
        assert_eq!(conn.dot_color(&theme), theme.sage_fg());

        let gen = ProviderState::Generating;
        assert_eq!(gen.status_label(), "Generating");
        assert_eq!(gen.dot_color(&theme), theme.warning_amber());

        let off = ProviderState::Offline;
        assert_eq!(off.status_label(), "Offline");
        assert_eq!(off.dot_color(&theme), theme.rose_fg());
    }

    #[test]
    fn test_sidebar_initialization_and_workspace() {
        let mut sidebar = SidebarView::with_workspace("Harness", "experimental-features");
        assert_eq!(sidebar.workspace_name, "Harness");
        assert_eq!(sidebar.git_branch, "experimental-features");

        sidebar.set_workspace("hex-agent", "main");
        assert_eq!(sidebar.workspace_name, "hex-agent");
        assert_eq!(sidebar.git_branch, "main");

        let status = ProviderStatus::new(ProviderState::Generating, "OpenAI", "o3-mini");
        assert_eq!(status.display_label(), "OpenAI o3-mini");
        sidebar.set_provider_status(status);
        assert_eq!(sidebar.provider_status.state, ProviderState::Generating);
    }
}

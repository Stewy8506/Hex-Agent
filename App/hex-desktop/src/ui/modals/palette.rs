//! Global Command Palette modal with fuzzy search and grouped action navigation.
//!
//! Provides fast Cmd+K command palette matching Hermes Desktop and IDE conventions:
//! - Search input with real-time fuzzy filtering
//! - Grouped entries: Actions, Navigation, Sessions
//! - Keyboard navigation (Up/Down arrows, Enter to execute, Esc to dismiss)
//! - Action execution callback dispatch

use gpui::prelude::FluentBuilder;
use gpui::*;
use serde::{Deserialize, Serialize};

use crate::theme::HermesTheme;
use crate::ui::icon_svg;
use crate::ui::modals::{modal_press, ModalMotion};
use crate::ui::session_list::SessionItem;

/// Category grouping for command palette entries.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum PaletteCategory {
    Actions,
    Navigation,
    Sessions,
}

impl PaletteCategory {
    pub fn title(&self) -> &'static str {
        match self {
            PaletteCategory::Actions => "ACTIONS",
            PaletteCategory::Navigation => "NAVIGATION",
            PaletteCategory::Sessions => "SESSIONS",
        }
    }
}

/// Action to execute when a command palette item is selected.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum PaletteAction {
    NewChat,
    ClearHistory,
    ExportTranscript,
    SwitchModel,
    OpenSettings,
    ToggleSidebar,
    SelectSession(String),
    Custom(String),
}

/// Single executable item inside the command palette.
#[derive(Debug, Clone, PartialEq)]
pub struct PaletteItem {
    pub id: String,
    pub title: String,
    pub subtitle: Option<String>,
    pub category: PaletteCategory,
    pub shortcut: Option<String>,
    pub icon: String,
    pub action: PaletteAction,
}

impl PaletteItem {
    pub fn new(
        id: impl Into<String>,
        title: impl Into<String>,
        category: PaletteCategory,
        icon: impl Into<String>,
        action: PaletteAction,
    ) -> Self {
        Self {
            id: id.into(),
            title: title.into(),
            subtitle: None,
            category,
            shortcut: None,
            icon: icon.into(),
            action,
        }
    }

    pub fn with_subtitle(mut self, sub: impl Into<String>) -> Self {
        self.subtitle = Some(desc_to_string(sub));
        self
    }

    pub fn with_shortcut(mut self, sc: impl Into<String>) -> Self {
        self.shortcut = Some(sc.into());
        self
    }

    /// Fuzzy match helper: returns true if query matches title, subtitle, or category.
    pub fn matches(&self, query: &str) -> bool {
        let q = query.trim().to_lowercase();
        if q.is_empty() {
            return true;
        }

        let title_lower = self.title.to_lowercase();
        if title_lower.contains(&q) {
            return true;
        }

        if let Some(ref sub) = self.subtitle {
            if sub.to_lowercase().contains(&q) {
                return true;
            }
        }

        // Sequential character match
        let mut q_chars = q.chars().peekable();
        for c in title_lower.chars() {
            if let Some(&target) = q_chars.peek() {
                if c == target {
                    q_chars.next();
                }
            }
        }
        q_chars.peek().is_none()
    }
}

fn desc_to_string(s: impl Into<String>) -> String {
    s.into()
}

/// Command palette modal state and rendering.
#[derive(Debug, Clone, PartialEq)]
pub struct CommandPaletteModal {
    pub query: String,
    pub selected_index: usize,
    pub items: Vec<PaletteItem>,
}

impl Default for CommandPaletteModal {
    fn default() -> Self {
        Self::new()
    }
}

impl CommandPaletteModal {
    /// Create a new command palette with standard built-in actions and navigation items.
    pub fn new() -> Self {
        let mut items = Vec::new();

        // Standard Actions
        items.push(
            PaletteItem::new("act_new_chat", "New Chat", PaletteCategory::Actions, "chat", PaletteAction::NewChat)
                .with_subtitle("Start a fresh agent conversation")
                .with_shortcut("\u{2318}N"),
        );
        items.push(
            PaletteItem::new("act_clear", "Clear History", PaletteCategory::Actions, "trash", PaletteAction::ClearHistory)
                .with_subtitle("Reset the active conversation stream"),
        );
        items.push(
            PaletteItem::new("act_export", "Export Transcript", PaletteCategory::Actions, "notebooks", PaletteAction::ExportTranscript)
                .with_subtitle("Save session dialogue to Markdown file"),
        );
        items.push(
            PaletteItem::new("act_model", "Switch Model", PaletteCategory::Actions, "search", PaletteAction::SwitchModel)
                .with_subtitle("Pick the active model from the live backend catalog"),
        );

        // Navigation
        items.push(
            PaletteItem::new("nav_settings", "Open Settings", PaletteCategory::Navigation, "settings", PaletteAction::OpenSettings)
                .with_subtitle("Configure model providers, API keys, and gateways")
                .with_shortcut("\u{2318},"),
        );
        items.push(
            PaletteItem::new("nav_sidebar", "Toggle Sidebar", PaletteCategory::Navigation, "sidebar", PaletteAction::ToggleSidebar)
                .with_subtitle("Show or hide the navigation sidebar")
                .with_shortcut("\u{2318}B"),
        );

        Self {
            query: String::new(),
            selected_index: 0,
            items,
        }
    }

    /// Populate recent sessions into the command palette.
    pub fn populate_sessions(&mut self, sessions: &[SessionItem]) {
        // Retain non-session items
        self.items.retain(|it| it.category != PaletteCategory::Sessions);

        for session in sessions {
            self.items.push(
                PaletteItem::new(
                    format!("ses_{}", session.id),
                    session.title.clone(),
                    PaletteCategory::Sessions,
                    "notebooks",
                    PaletteAction::SelectSession(session.id.clone()),
                )
                .with_subtitle(format!(
                    "Session • {}",
                    session.model_name.as_deref().unwrap_or("HEX Agent")
                )),
            );
        }
    }

    /// Set search input query.
    pub fn set_query(&mut self, query: impl Into<String>) {
        self.query = query.into();
        self.selected_index = 0;
    }

    /// Get list of items matching the current query.
    pub fn filtered_items(&self) -> Vec<&PaletteItem> {
        self.items
            .iter()
            .filter(|it| it.matches(&self.query))
            .collect()
    }

    /// Move selection highlight up.
    pub fn move_up(&mut self) {
        let total = self.filtered_items().len();
        if total > 0 {
            if self.selected_index == 0 {
                self.selected_index = total - 1;
            } else {
                self.selected_index -= 1;
            }
        }
    }

    /// Move selection highlight down.
    pub fn move_down(&mut self) {
        let total = self.filtered_items().len();
        if total > 0 {
            self.selected_index = (self.selected_index + 1) % total;
        }
    }

    /// Retrieve the currently selected item, if any.
    pub fn selected_item(&self) -> Option<&PaletteItem> {
        let filtered = self.filtered_items();
        filtered.get(self.selected_index).copied()
    }

    /// Execute the currently highlighted item.
    pub fn execute_selected(&self) -> Option<PaletteAction> {
        self.selected_item().map(|it| it.action.clone())
    }

    /// Handle key press events (Up, Down, Enter).
    pub fn handle_key(&mut self, key: &str) -> Option<PaletteAction> {
        match key.to_lowercase().as_str() {
            "up" | "arrowup" => {
                self.move_up();
                None
            }
            "down" | "arrowdown" => {
                self.move_down();
                None
            }
            "enter" => self.execute_selected(),
            _ => None,
        }
    }

    /// Render the command palette modal card.
    pub fn render(&self, theme: &HermesTheme, motion: ModalMotion<'_>) -> Div {
        let filtered = self.filtered_items();

        div()
            .w(px(560.0))
            .max_h(px(480.0))
            .flex()
            .flex_col()
            .bg(theme.mockup_surface)
            // Top search bar
            .child(
                div()
                    .flex()
                    .items_center()
                    .px_4()
                    .py_3()
                    .border_b_1()
                    .border_color(theme.mockup_border)
                    .gap_3()
                    .child(icon_svg("search", 14.0).text_color(theme.mockup_text_dim))
                    .child(
                        div()
                            .flex_1()
                            .text_size(px(13.5))
                            .text_color(if self.query.is_empty() {
                                theme.mockup_text_dim
                            } else {
                                theme.mockup_text_bright
                            })
                            .child(if self.query.is_empty() {
                                "Type a command or search sessions...".to_string()
                            } else {
                                self.query.clone()
                            }),
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
            )
            // Results list or Empty State
            .child(
                div()
                    .flex_1()
                    .overflow_y_hidden()
                    .p_2()
                    .flex()
                    .flex_col()
                    .gap_1()
                    .when(filtered.is_empty(), |this| {
                        this.child(
                            div()
                                .py_8()
                                .flex()
                                .flex_col()
                                .items_center()
                                .justify_center()
                                .gap_1()
                                .child(
                                    div()
                                        .text_size(px(13.0))
                                        .text_color(theme.mockup_text_dim)
                                        .child("No matching commands found"),
                                )
                                .child(
                                    div()
                                        .text_size(px(11.0))
                                        .text_color(theme.mockup_text_dim)
                                        .child(format!("No results for \"{}\"", self.query)),
                                ),
                        )
                    })
                    .when(!filtered.is_empty(), |this| {
                        let mut container = this;
                        let mut current_cat = None;

                        for (idx, item) in filtered.iter().enumerate() {
                            let is_selected = idx == self.selected_index;

                            // Insert category header if new
                            if current_cat != Some(item.category) {
                                current_cat = Some(item.category);
                                container = container.child(
                                    div()
                                        .px_3()
                                        .pt_2()
                                        .pb_1()
                                        .text_size(px(10.0))
                                        .font_weight(FontWeight::SEMIBOLD)
                                        .text_color(theme.mockup_text_dim)
                                        .child(item.category.title()),
                                );
                            }

                            // Render item row (mockup `.row` language: radius 8,
                            // hover `#1a1a1a`; the selection carries the accent
                            // chip wash + accent border as the focus indicator).
                            let mut row = div()
                                .flex()
                                    .items_center()
                                    .justify_between()
                                    .px(px(12.0))
                                    .py(px(8.0))
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
                                    .hover(|s| s.bg(theme.mockup_row_hover))
                                    .cursor_pointer()
                                    .child(
                                        div()
                                            .flex()
                                            .items_center()
                                            .gap_2p5()
                                            .child(
                                                div()
                                                    .text_color(theme.mockup_text_muted)
                                                    .child(crate::ui::icon_svg(
                                                        item.icon.as_str(),
                                                        14.0,
                                                    )),
                                            )
                                            .child(
                                                div()
                                                    .flex()
                                                    .flex_col()
                                                    .child(
                                                        div()
                                                            .text_size(px(12.5))
                                                            .font_weight(if is_selected {
                                                                FontWeight::MEDIUM
                                                            } else {
                                                                FontWeight::NORMAL
                                                            })
                                                            .text_color(if is_selected {
                                                                theme.mockup_text_bright
                                                            } else {
                                                                theme.mockup_text_muted
                                                            })
                                                            .child(item.title.clone()),
                                                    )
                                                    .when_some(item.subtitle.as_ref(), |p, sub| {
                                                        p.child(
                                                            div()
                                                                .text_size(px(10.5))
                                                                .text_color(theme.mockup_text_dim)
                                                                .child(sub.clone()),
                                                        )
                                                    }),
                                            ),
                                    )
                                    .when_some(item.shortcut.as_ref(), |p, sc| {
                                        p.child(
                                            div()
                                                .px_1p5()
                                                .py_0p5()
                                                .rounded(px(3.0))
                                                .bg(theme.mockup_border)
                                                .font_family(theme.typography.font_mono[0].clone())
                                                .text_size(px(10.5))
                                                .text_color(if is_selected {
                                                    theme.mockup_text_bright
                                                } else {
                                                    theme.mockup_text_dim
                                                })
                                                .child(sc.clone()),
                                        )
                                    });
                            if is_selected {
                                row = modal_press(row, "palette_selected_press", motion);
                            }
                            container = container.child(row);
                        }
                        container
                    }),
            )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_command_palette_filtering() {
        let mut palette = CommandPaletteModal::new();
        assert!(palette.items.len() >= 6);

        // Filter for "settings"
        palette.set_query("settings");
        let filtered = palette.filtered_items();
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].title, "Open Settings");

        // CompactContext was removed — no honest backend mapping exists.
        palette.set_query("compact");
        assert!(palette.filtered_items().is_empty());
    }

    #[test]
    fn test_command_palette_icons_resolve_to_assets() {
        // Every palette icon (chat, trash, …) must name a bundled SVG — the
        // old "contains(' ')" fallback is gone (plan P5).
        let manifest = env!("CARGO_MANIFEST_DIR");
        let palette = CommandPaletteModal::new();
        for item in &palette.items {
            let path = format!("{manifest}/assets/icons/{}.svg", item.icon);
            assert!(
                std::path::Path::new(&path).exists(),
                "palette icon {:?} (item {:?}) has no bundled asset at {path}",
                item.icon,
                item.title
            );
        }
    }

    #[test]
    fn test_command_palette_navigation_and_execution() {
        let mut palette = CommandPaletteModal::new();
        palette.set_query("");

        assert_eq!(palette.selected_index, 0);

        // Down
        palette.handle_key("down");
        assert_eq!(palette.selected_index, 1);

        // Up
        palette.handle_key("up");
        assert_eq!(palette.selected_index, 0);

        // Enter on first action (New Chat)
        let action = palette.handle_key("enter");
        assert_eq!(action, Some(PaletteAction::NewChat));
    }

    #[test]
    fn test_command_palette_with_sessions() {
        let mut palette = CommandPaletteModal::new();
        let sessions = vec![
            SessionItem::new("ses_1", "Refactor authentication"),
            SessionItem::new("ses_2", "Fix memory leak in parser"),
        ];

        palette.populate_sessions(&sessions);
        palette.set_query("leak");
        let filtered = palette.filtered_items();
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].title, "Fix memory leak in parser");
        assert_eq!(filtered[0].action, PaletteAction::SelectSession("ses_2".to_string()));
    }
}

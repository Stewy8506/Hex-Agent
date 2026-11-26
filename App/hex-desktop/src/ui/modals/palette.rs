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
use crate::ui::modals::settings::SETTING_DESCRIPTORS;
use crate::ui::modals::text_input::{
    backspace as text_backspace, delete as text_delete, input_content, insert_text, move_horizontal,
    move_to as text_move_to, move_to_edge, select_all, selected_text,
};
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
    OpenSetting(String),
    ToggleSidebar,
    SelectSession(String),
    Custom(String),
}

pub type PaletteActionHandler = std::rc::Rc<
    dyn Fn(PaletteAction, &ClickEvent, &mut Window, &mut App),
>;

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

        if let PaletteAction::OpenSetting(key) = &self.action {
            if key.to_lowercase().contains(&q) {
                return true;
            }
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PaletteRow {
    Header(PaletteCategory),
    Item(usize),
}

/// Command palette modal state and rendering.
#[derive(Debug, Clone)]
pub struct CommandPaletteModal {
    pub query: String,
    pub cursor: usize,
    pub anchor: usize,
    pub selected_index: usize,
    pub items: Vec<PaletteItem>,
    scroll_handle: UniformListScrollHandle,
}

impl PartialEq for CommandPaletteModal {
    fn eq(&self, other: &Self) -> bool {
        self.query == other.query
            && self.cursor == other.cursor
            && self.anchor == other.anchor
            && self.selected_index == other.selected_index
            && self.items == other.items
    }
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
        for descriptor in SETTING_DESCRIPTORS {
            items.push(
                PaletteItem::new(
                    format!("setting_{}", descriptor.key),
                    descriptor.label,
                    PaletteCategory::Navigation,
                    "settings",
                    PaletteAction::OpenSetting(descriptor.key.to_string()),
                )
                .with_subtitle(descriptor.palette_subtitle()),
            );
        }

        Self {
            query: String::new(),
            cursor: 0,
            anchor: 0,
            selected_index: 0,
            items,
            scroll_handle: UniformListScrollHandle::new(),
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
        self.cursor = self.query.len();
        self.anchor = self.cursor;
        self.selected_index = 0;
        self.scroll_to_top();
    }

    pub fn push_char(&mut self, text: &str) {
        insert_text(&mut self.query, &mut self.cursor, &mut self.anchor, text);
        self.selected_index = 0;
        self.scroll_to_top();
    }

    pub fn backspace(&mut self) {
        if text_backspace(&mut self.query, &mut self.cursor, &mut self.anchor) {
            self.selected_index = 0;
            self.scroll_to_top();
        }
    }

    pub fn delete(&mut self) {
        if text_delete(&mut self.query, &mut self.cursor, &mut self.anchor) {
            self.selected_index = 0;
            self.scroll_to_top();
        }
    }

    pub fn move_cursor(&mut self, delta: isize, selecting: bool) {
        move_horizontal(
            &self.query,
            &mut self.cursor,
            &mut self.anchor,
            delta,
            selecting,
        );
    }

    pub fn move_cursor_to(&mut self, offset: usize, selecting: bool) {
        text_move_to(
            &self.query,
            &mut self.cursor,
            &mut self.anchor,
            offset,
            selecting,
        );
    }

    pub fn move_cursor_to_edge(&mut self, end: bool, selecting: bool) {
        move_to_edge(
            &self.query,
            &mut self.cursor,
            &mut self.anchor,
            end,
            selecting,
        );
    }

    pub fn select_all_text(&mut self) {
        select_all(&self.query, &mut self.cursor, &mut self.anchor);
    }

    pub fn selected_text(&self) -> String {
        selected_text(&self.query, self.cursor, self.anchor)
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
            self.scroll_selected_into_view();
        }
    }

    /// Move selection highlight down.
    pub fn move_down(&mut self) {
        let total = self.filtered_items().len();
        if total > 0 {
            self.selected_index = (self.selected_index + 1) % total;
            self.scroll_selected_into_view();
        }
    }

    fn rows(&self, filtered: &[&PaletteItem]) -> Vec<PaletteRow> {
        let mut rows = Vec::with_capacity(filtered.len() + 3);
        let mut category = None;
        for (index, item) in filtered.iter().enumerate() {
            if category != Some(item.category) {
                category = Some(item.category);
                rows.push(PaletteRow::Header(item.category));
            }
            rows.push(PaletteRow::Item(index));
        }
        rows
    }

    fn scroll_selected_into_view(&self) {
        let filtered = self.filtered_items();
        let rows = self.rows(&filtered);
        if let Some(index) = rows.iter().position(|row| {
            matches!(row, PaletteRow::Item(item) if *item == self.selected_index)
        }) {
            self.scroll_handle
                .scroll_to_item(index, ScrollStrategy::Center);
        }
    }

    fn scroll_to_top(&self) {
        self.scroll_handle
            .scroll_to_item_strict(0, ScrollStrategy::Top);
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
    pub fn render(
        &self,
        theme: &HermesTheme,
        motion: ModalMotion<'_>,
        on_action: PaletteActionHandler,
    ) -> Div {
        let filtered = self.filtered_items();
        let search = div()
            .flex()
            .items_center()
            .px_4()
            .py_3()
            .border_b_1()
            .border_color(theme.mockup_border)
            .gap_3()
            .child(icon_svg("search", 14.0).text_color(theme.mockup_text_dim))
            .child(
                input_content(
                    &self.query,
                    self.cursor,
                    self.anchor,
                    "Type a command or search sessions...",
                    false,
                    true,
                    theme,
                )
                .flex_1()
                .min_w_0()
                .text_size(px(13.5)),
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
            );
        let results = if filtered.is_empty() {
            div()
                .h(px(280.0))
                .p_4()
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
                )
        } else {
            let items: Vec<PaletteItem> = filtered.iter().map(|item| (*item).clone()).collect();
            let rows = self.rows(&filtered);
            let selected_index = self.selected_index;
            let scroll_handle = self.scroll_handle.clone();
            let theme = theme.clone();
            let list = uniform_list(
                "palette_rows",
                rows.len(),
                move |range, _window, cx| {
                    let motion = ModalMotion {
                        owner: motion.owner,
                        cx,
                    };
                    range
                        .map(|row_index| match rows[row_index] {
                            PaletteRow::Header(category) => {
                                render_palette_header(category, &theme).into_any_element()
                            }
                            PaletteRow::Item(index) => render_palette_row(
                                &items[index],
                                index == selected_index,
                                &theme,
                                motion,
                                on_action.clone(),
                            )
                            .into_any_element(),
                        })
                        .collect::<Vec<_>>()
                },
            )
            .track_scroll(scroll_handle)
            .w_full()
            .h_full();
            div()
                .debug_selector(|| "palette_list".to_string())
                .h(px(360.0))
                .w_full()
                .p_1()
                .overflow_hidden()
                .child(list)
        };
        div()
            .w(px(560.0))
            .max_h(px(480.0))
            .flex()
            .flex_col()
            .bg(theme.mockup_surface)
            .child(search)
            .child(results)
    }
}

fn render_palette_header(category: PaletteCategory, theme: &HermesTheme) -> Div {
    div()
        .h(px(52.0))
        .w_full()
        .flex()
        .items_center()
        .px_3()
        .text_size(px(10.0))
        .font_weight(FontWeight::SEMIBOLD)
        .text_color(theme.mockup_text_dim)
        .child(category.title())
}

fn render_palette_row(
    item: &PaletteItem,
    selected: bool,
    theme: &HermesTheme,
    motion: ModalMotion<'_>,
    on_action: PaletteActionHandler,
) -> Stateful<Div> {
    let debug_id = format!("palette_{}", item.id);
    let action = item.action.clone();
    let mut row = div()
        .id(SharedString::from(format!("palette_{}", item.id)))
        .debug_selector(move || debug_id.clone())
        .h(px(52.0))
        .w_full()
        .flex()
        .items_center()
        .justify_between()
        .px(px(12.0))
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
        .hover(|this| this.bg(theme.mockup_row_hover))
        .cursor_pointer()
        .on_click(move |event, window, cx| on_action(action.clone(), event, window, cx))
        .child(
            div()
                .flex()
                .items_center()
                .gap_2p5()
                .min_w_0()
                .child(
                    div()
                        .text_color(theme.mockup_text_muted)
                        .child(icon_svg(item.icon.as_str(), 14.0)),
                )
                .child(
                    div()
                        .flex()
                        .flex_col()
                        .min_w_0()
                        .child(
                            div()
                                .text_size(px(12.5))
                                .font_weight(if selected {
                                    FontWeight::MEDIUM
                                } else {
                                    FontWeight::NORMAL
                                })
                                .text_color(if selected {
                                    theme.mockup_text_bright
                                } else {
                                    theme.mockup_text_muted
                                })
                                .truncate()
                                .child(item.title.clone()),
                        )
                        .when_some(item.subtitle.as_ref(), |this, subtitle| {
                            this.child(
                                div()
                                    .text_size(px(10.5))
                                    .text_color(theme.mockup_text_dim)
                                    .truncate()
                                    .child(subtitle.clone()),
                            )
                        }),
                ),
        )
        .when_some(item.shortcut.as_ref(), |this, shortcut| {
            this.child(
                div()
                    .px_1p5()
                    .py_0p5()
                    .rounded(px(3.0))
                    .bg(theme.mockup_border)
                    .font_family(theme.font_mono()[0].clone())
                    .text_size(px(10.5))
                    .text_color(if selected {
                        theme.mockup_text_bright
                    } else {
                        theme.mockup_text_dim
                    })
                    .child(shortcut.clone()),
            )
        });
    if selected {
        row = modal_press(row, "palette_selected_press", motion);
    }
    row
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
        assert_eq!(filtered.len(), 2);
        assert_eq!(filtered[0].title, "Open Settings");
        assert_eq!(
            filtered[1].action,
            PaletteAction::OpenSetting("desktop.keybind.open_settings".to_string())
        );

        // CompactContext was removed — no honest backend mapping exists.
        palette.set_query("compactcontext");
        assert!(palette.filtered_items().is_empty());
    }

    #[test]
    fn test_command_palette_setting_deep_links() {
        let mut palette = CommandPaletteModal::new();
        palette.set_query("max turns");
        let filtered = palette.filtered_items();
        assert_eq!(filtered.len(), 1);
        assert_eq!(
            filtered[0].action,
            PaletteAction::OpenSetting("runtime.maxTurns".to_string())
        );

        palette.set_query("runtime.maxTurns");
        let filtered = palette.filtered_items();
        assert_eq!(filtered.len(), 1);
        assert_eq!(
            filtered[0].action,
            PaletteAction::OpenSetting("runtime.maxTurns".to_string())
        );

        palette.set_query("Manage sessions");
        let filtered = palette.filtered_items();
        assert_eq!(filtered.len(), 1);
        assert_eq!(
            filtered[0].action,
            PaletteAction::OpenSetting("desktop.action.sessions".to_string())
        );
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

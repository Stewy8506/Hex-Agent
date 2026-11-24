//! Model picker modal overlay backed by the live backend catalog (B32).
//!
//! Entries come from `session/models` — the models the configured providers
//! actually serve. No hardcoded catalog, no invented context limits or tiers:
//! - Searchable list grouped by provider
//! - Honest Local / Cloud origin tag per entry
//! - Selection checkmark and keyboard navigation (Up, Down, Enter, Esc)

use gpui::prelude::FluentBuilder;
use gpui::*;
use serde::{Deserialize, Serialize};

use crate::theme::HermesTheme;
use crate::ui::icon_svg;
use crate::ui::modals::{modal_press, ModalMotion};
use crate::ui::model_pill::provider_dot_color;

/// Model entry representation in the picker dialog.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelEntry {
    pub id: String,
    pub display_name: String,
    pub provider: String,
    pub is_local: bool,
}

impl ModelEntry {
    pub fn new(
        id: impl Into<String>,
        display_name: impl Into<String>,
        provider: impl Into<String>,
        is_local: bool,
    ) -> Self {
        Self {
            id: id.into(),
            display_name: display_name.into(),
            provider: provider.into(),
            is_local,
        }
    }

    pub fn from_catalog(m: &hex_protocol::models::CatalogModel) -> Self {
        Self::new(m.id.clone(), m.display_name.clone(), m.provider.clone(), m.is_local)
    }

    /// Check if model matches a search query.
    pub fn matches(&self, query: &str) -> bool {
        let q = query.trim().to_lowercase();
        if q.is_empty() {
            return true;
        }

        self.id.to_lowercase().contains(&q)
            || self.display_name.to_lowercase().contains(&q)
            || self.provider.to_lowercase().contains(&q)
    }
}

pub type ModelSelectHandler =
    std::rc::Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)>;

/// Model Picker modal state and rendering.
#[derive(Debug, Clone, PartialEq)]
pub struct ModelPickerModal {
    pub models: Vec<ModelEntry>,
    pub selected_model_id: String,
    pub highlighted_index: usize,
    pub search_query: String,
}

impl ModelPickerModal {
    /// Create a picker over the live backend catalog, highlighting the active model.
    pub fn new(current_model: impl Into<String>, models: Vec<ModelEntry>) -> Self {
        let selected_model_id = current_model.into();
        let highlighted_index = models
            .iter()
            .position(|m| m.id == selected_model_id)
            .unwrap_or(0);
        Self {
            models,
            selected_model_id,
            highlighted_index,
            search_query: String::new(),
        }
    }

    /// Set search filter query.
    pub fn set_search(&mut self, query: impl Into<String>) {
        self.search_query = query.into();
        self.highlighted_index = 0;
    }

    /// Return filtered models matching current query.
    pub fn filtered_models(&self) -> Vec<&ModelEntry> {
        self.models
            .iter()
            .filter(|m| m.matches(&self.search_query))
            .collect()
    }

    /// Move selection highlight up.
    pub fn move_up(&mut self) {
        let total = self.filtered_models().len();
        if total > 0 {
            if self.highlighted_index == 0 {
                self.highlighted_index = total - 1;
            } else {
                self.highlighted_index -= 1;
            }
        }
    }

    /// Move selection highlight down.
    pub fn move_down(&mut self) {
        let total = self.filtered_models().len();
        if total > 0 {
            self.highlighted_index = (self.highlighted_index + 1) % total;
        }
    }

    /// Retrieve the currently highlighted model entry.
    pub fn highlighted_model(&self) -> Option<&ModelEntry> {
        let filtered = self.filtered_models();
        filtered.get(self.highlighted_index).copied()
    }

    /// Select the currently highlighted model and return its ID.
    pub fn select_highlighted(&mut self) -> Option<String> {
        if let Some(m) = self.highlighted_model() {
            let id = m.id.clone();
            self.selected_model_id = id.clone();
            Some(id)
        } else {
            None
        }
    }

    /// Handle keyboard input (Up, Down, Enter).
    pub fn handle_key(&mut self, key: &str) -> Option<String> {
        match key.to_lowercase().as_str() {
            "up" | "arrowup" => {
                self.move_up();
                None
            }
            "down" | "arrowdown" => {
                self.move_down();
                None
            }
            "enter" => self.select_highlighted(),
            _ => None,
        }
    }

    /// Render the model picker modal card.
    pub fn render(
        &self,
        theme: &HermesTheme,
        motion: ModalMotion<'_>,
        on_select: ModelSelectHandler,
    ) -> Div {
        let filtered = self.filtered_models();

        div()
            .w(px(540.0))
            .max_h(px(500.0))
            .flex()
            .flex_col()
            .bg(theme.mockup_surface)
            // Header with search input
            .child(
                div()
                    .flex()
                    .items_center()
                    .px_4()
                    .py_3()
                    .border_b_1()
                    .border_color(theme.mockup_border)
                    .gap_3()
                    .child(
                        div()
                            .text_size(px(14.0))
                            .text_color(theme.mockup_text_dim)
                            .child(icon_svg("bolt", 14.0)),
                    )
                    .child(
                        div()
                            .flex_1()
                            .text_size(px(13.5))
                            .text_color(if self.search_query.is_empty() {
                                theme.mockup_text_dim
                            } else {
                                theme.mockup_text_bright
                            })
                            .child(if self.search_query.is_empty() {
                                "Search models across providers...".to_string()
                            } else {
                                self.search_query.clone()
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
            // Model list grouped by provider
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
                                        .child("No models found"),
                                )
                                .child(
                                    div()
                                        .text_size(px(11.0))
                                        .text_color(theme.mockup_text_dim)
                                        .child(format!("No matches for \"{}\"", self.search_query)),
                                ),
                        )
                    })
                    .when(!filtered.is_empty(), |this| {
                        let mut container = this;
                        let mut current_provider = None;

                        for (idx, model) in filtered.iter().enumerate() {
                            let is_highlighted = idx == self.highlighted_index;
                            let is_selected = model.id == self.selected_model_id;

                            // Insert Provider Header if changed
                            if current_provider.as_deref() != Some(model.provider.as_str()) {
                                current_provider = Some(model.provider.clone());
                                container = container.child(
                                    div()
                                        .px_3()
                                        .pt_2()
                                        .pb_1()
                                        .text_size(px(10.0))
                                        .font_weight(FontWeight::SEMIBOLD)
                                        .text_color(theme.mockup_text_dim)
                                        .child(model.provider.to_uppercase()),
                                );
                            }

                            let dot_color = provider_dot_color(&model.provider, theme);

                            // Model Row
                            let mut row = div()
                                    .flex()
                                    .items_center()
                                    .justify_between()
                                    .px_3()
                                    .py_2()
                                    .rounded(px(6.0))
                                    .bg(if is_highlighted {
                                        theme.mockup_hover
                                    } else {
                                        gpui::rgba(0x00000000)
                                    })
                                    .cursor_pointer()
                                    .id(SharedString::from(format!("model_{idx}")))
                                    .debug_selector(move || format!("model_{idx}"))
                                    .on_click({
                                        let on_select = on_select.clone();
                                        let model_id = model.id.clone();
                                        move |event, window, cx| {
                                            on_select(model_id.clone(), event, window, cx)
                                        }
                                    })
                                    .child(
                                        div()
                                            .flex()
                                            .items_center()
                                            .gap_2p5()
                                            .child(
                                                // Provider status dot
                                                div()
                                                    .w(px(7.0))
                                                    .h(px(7.0))
                                                    .rounded_full()
                                                    .bg(dot_color),
                                            )
                                            .child(
                                                // Display name
                                                div()
                                                    .text_size(px(12.5))
                                                    .font_weight(if is_selected {
                                                        FontWeight::SEMIBOLD
                                                    } else {
                                                        FontWeight::NORMAL
                                                    })
                                                    .text_color(if is_selected {
                                                        theme.accent_blue()
                                                    } else if is_highlighted {
                                                        theme.mockup_text_bright
                                                    } else {
                                                        theme.mockup_text_muted
                                                    })
                                                    .child(model.display_name.clone()),
                                            ),
                                    )
                                    .child(
                                        div()
                                            .flex()
                                            .items_center()
                                            .gap_2()
                                            .child(
                                                // Honest origin tag: where the model actually runs
                                                div()
                                                    .px_1p5()
                                                    .py_0p5()
                                                    .rounded(px(3.0))
                                                    .bg(if model.is_local {
                                                        theme.sage_bg()
                                                    } else {
                                                        theme.mockup_hover
                                                    })
                                                    .text_size(px(10.0))
                                                    .text_color(if model.is_local {
                                                        theme.sage_fg()
                                                    } else {
                                                        theme.mockup_text_dim
                                                    })
                                                    .child(if model.is_local { "Local" } else { "Cloud" }),
                                            )
                                            .child(
                                                // Selection checkmark
                                                div()
                                                    .w(px(16.0))
                                                    .text_size(px(12.0))
                                                    .text_color(if is_selected {
                                                        theme.accent_blue()
                                                    } else {
                                                        gpui::rgba(0x00000000)
                                                    })
                                                    .child(if is_selected { "✓" } else { "" }),
                                            ),
                                    );
                            if is_highlighted {
                                row = modal_press(row, "model_picker_selected_press", motion);
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

    fn catalog() -> Vec<ModelEntry> {
        vec![
            ModelEntry::new("gpt-4o", "GPT-4o", "OpenAI", false),
            ModelEntry::new("o3-mini", "o3-mini", "OpenAI", false),
            ModelEntry::new("qwen2.5-coder-32b", "Qwen (Ollama)", "Local", true),
        ]
    }

    #[test]
    fn test_model_picker_uses_backend_catalog_and_filtering() {
        let mut picker = ModelPickerModal::new("gpt-4o", catalog());
        assert_eq!(picker.selected_model_id, "gpt-4o");
        assert_eq!(picker.highlighted_index, 0);
        assert_eq!(picker.models.len(), 3);

        // Filter for local
        picker.set_search("local");
        let filtered = picker.filtered_models();
        assert_eq!(filtered.len(), 1);
        assert!(filtered.iter().all(|m| m.is_local));

        // Filter for gpt
        picker.set_search("gpt");
        let filtered = picker.filtered_models();
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].id, "gpt-4o");
    }

    #[test]
    fn test_model_picker_selection_and_shortcuts() {
        let mut picker = ModelPickerModal::new("gpt-4o", catalog());
        picker.set_search("");

        // Move down
        picker.handle_key("down");
        assert_eq!(picker.highlighted_index, 1);

        // Enter to select
        let selected = picker.handle_key("enter");
        assert_eq!(selected, Some("o3-mini".to_string()));
        assert_eq!(picker.selected_model_id, "o3-mini");
    }

    #[test]
    fn test_model_picker_highlights_active_model_wherever_it_sits() {
        let picker = ModelPickerModal::new("qwen2.5-coder-32b", catalog());
        assert_eq!(picker.highlighted_index, 2);
        assert_eq!(picker.highlighted_model().unwrap().id, "qwen2.5-coder-32b");
    }

    #[test]
    fn test_model_picker_empty_catalog_selects_nothing() {
        let mut picker = ModelPickerModal::new("gpt-4o", Vec::new());
        assert_eq!(picker.handle_key("enter"), None);
        assert_eq!(picker.selected_model_id, "gpt-4o");
    }
}
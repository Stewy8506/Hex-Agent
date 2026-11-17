//! Model picker modal overlay with provider grouping, context length badges, and instant search.
//!
//! Conforms to Hermes Desktop model selection paradigms:
//! - Searchable catalog of models grouped by provider: Anthropic, OpenAI, Local MLX/Ollama, OpenRouter
//! - Context limit pill badges (128k, 200k, 32k)
//! - Latency and capability tier tags (Reasoning, Fast, Flagship, Local)
//! - Selection checkmark and keyboard navigation (Up, Down, Enter, Esc)

use gpui::prelude::FluentBuilder;
use gpui::*;
use serde::{Deserialize, Serialize};

use crate::theme::HermesTheme;
use crate::ui::model_pill::provider_dot_color;

/// Model entry representation in the picker dialog.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelEntry {
    pub id: String,
    pub display_name: String,
    pub provider: String,
    pub context_limit: String,
    pub tier_badge: String,
    pub is_local: bool,
}

impl ModelEntry {
    pub fn new(
        id: impl Into<String>,
        display_name: impl Into<String>,
        provider: impl Into<String>,
        context_limit: impl Into<String>,
        tier_badge: impl Into<String>,
        is_local: bool,
    ) -> Self {
        Self {
            id: id.into(),
            display_name: display_name.into(),
            provider: provider.into(),
            context_limit: context_limit.into(),
            tier_badge: tier_badge.into(),
            is_local,
        }
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
            || self.tier_badge.to_lowercase().contains(&q)
    }
}

/// Model Picker modal state and rendering.
#[derive(Debug, Clone, PartialEq)]
pub struct ModelPickerModal {
    pub models: Vec<ModelEntry>,
    pub selected_model_id: String,
    pub highlighted_index: usize,
    pub search_query: String,
}

impl Default for ModelPickerModal {
    fn default() -> Self {
        Self::new("claude-3-7-sonnet")
    }
}

impl ModelPickerModal {
    /// Create a new model picker with standard curated models catalog.
    pub fn new(current_model: impl Into<String>) -> Self {
        let selected_model_id = current_model.into();
        let mut models = Vec::new();

        // Anthropic
        models.push(ModelEntry::new(
            "claude-3-7-sonnet",
            "Claude 3.7 Sonnet",
            "Anthropic",
            "200k",
            "Hybrid Reasoning",
            false,
        ));
        models.push(ModelEntry::new(
            "claude-3-5-haiku",
            "Claude 3.5 Haiku",
            "Anthropic",
            "200k",
            "Ultra-Fast",
            false,
        ));

        // OpenAI
        models.push(ModelEntry::new(
            "gpt-4o",
            "GPT-4o",
            "OpenAI",
            "128k",
            "Flagship Multimodal",
            false,
        ));
        models.push(ModelEntry::new(
            "o3-mini",
            "o3-mini",
            "OpenAI",
            "200k",
            "Reasoning High",
            false,
        ));

        // Local MLX / Ollama
        models.push(ModelEntry::new(
            "qwen2.5-coder-32b",
            "Qwen 2.5 Coder 32B",
            "Local MLX / Ollama",
            "32k",
            "Local 0ms Metal",
            true,
        ));
        models.push(ModelEntry::new(
            "llama-3.3-70b",
            "Llama 3.3 70B",
            "Local MLX / Ollama",
            "128k",
            "Local High Capacity",
            true,
        ));

        // OpenRouter
        models.push(ModelEntry::new(
            "deepseek-r1",
            "DeepSeek R1",
            "OpenRouter",
            "64k",
            "Open Reasoning",
            false,
        ));

        Self {
            models,
            selected_model_id,
            highlighted_index: 0,
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
    pub fn render(&self, theme: &HermesTheme) -> Div {
        let filtered = self.filtered_models();

        div()
            .w(px(540.0))
            .max_h(px(500.0))
            .flex()
            .flex_col()
            .bg(theme.card_bg())
            // Header with search input
            .child(
                div()
                    .flex()
                    .items_center()
                    .px_4()
                    .py_3()
                    .border_b_1()
                    .border_color(theme.border_subtle())
                    .gap_3()
                    .child(
                        div()
                            .text_size(px(14.0))
                            .text_color(theme.text_dim())
                            .child("✨"),
                    )
                    .child(
                        div()
                            .flex_1()
                            .text_size(px(13.5))
                            .text_color(if self.search_query.is_empty() {
                                theme.text_dim()
                            } else {
                                theme.text_primary()
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
                            .bg(theme.card_hover())
                            .border_1()
                            .border_color(theme.border_subtle())
                            .text_size(px(10.0))
                            .font_family(theme.typography.font_mono[0].clone())
                            .text_color(theme.text_dim())
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
                                        .text_color(theme.text_dim())
                                        .child("No models found"),
                                )
                                .child(
                                    div()
                                        .text_size(px(11.0))
                                        .text_color(theme.text_extra_dim())
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
                                        .text_color(theme.text_dim())
                                        .child(model.provider.to_uppercase()),
                                );
                            }

                            let dot_color = provider_dot_color(&model.provider, theme);

                            // Model Row
                            container = container.child(
                                div()
                                    .flex()
                                    .items_center()
                                    .justify_between()
                                    .px_3()
                                    .py_2()
                                    .rounded(px(6.0))
                                    .bg(if is_highlighted {
                                        theme.card_hover()
                                    } else {
                                        gpui::rgba(0x00000000)
                                    })
                                    .cursor_pointer()
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
                                                        theme.text_primary()
                                                    } else {
                                                        theme.text_muted()
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
                                                // Tier badge
                                                div()
                                                    .px_1p5()
                                                    .py_0p5()
                                                    .rounded(px(3.0))
                                                    .bg(if model.is_local {
                                                        theme.sage_bg()
                                                    } else {
                                                        theme.card_hover()
                                                    })
                                                    .text_size(px(10.0))
                                                    .text_color(if model.is_local {
                                                        theme.sage_fg()
                                                    } else {
                                                        theme.text_dim()
                                                    })
                                                    .child(model.tier_badge.clone()),
                                            )
                                            .child(
                                                // Context limit badge
                                                div()
                                                    .px_1p5()
                                                    .py_0p5()
                                                    .rounded(px(3.0))
                                                    .bg(theme.border_subtle())
                                                    .font_family(theme.typography.font_mono[0].clone())
                                                    .text_size(px(10.0))
                                                    .text_color(theme.text_dim())
                                                    .child(model.context_limit.clone()),
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
                                    ),
                            );
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
    fn test_model_picker_initialization_and_filtering() {
        let mut picker = ModelPickerModal::new("claude-3-7-sonnet");
        assert_eq!(picker.selected_model_id, "claude-3-7-sonnet");
        assert!(picker.models.len() >= 5);

        // Filter for local
        picker.set_search("local");
        let filtered = picker.filtered_models();
        assert_eq!(filtered.len(), 2);
        assert!(filtered.iter().all(|m| m.is_local));

        // Filter for gpt
        picker.set_search("gpt");
        let filtered = picker.filtered_models();
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].id, "gpt-4o");
    }

    #[test]
    fn test_model_picker_selection_and_shortcuts() {
        let mut picker = ModelPickerModal::new("claude-3-7-sonnet");
        picker.set_search("");

        // Move down
        picker.handle_key("down");
        assert_eq!(picker.highlighted_index, 1);

        // Enter to select
        let selected = picker.handle_key("enter");
        assert_eq!(selected, Some("claude-3-5-haiku".to_string()));
        assert_eq!(picker.selected_model_id, "claude-3-5-haiku");
    }
}

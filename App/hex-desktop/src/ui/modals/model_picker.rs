//! Model picker modal overlay backed by the live backend catalog (B32).
//!
//! Entries come from `session/models` — the models the configured providers
//! actually serve. No hardcoded catalog, no invented context limits or tiers:
//! - Searchable list grouped by provider
//! - Honest Local / Cloud origin tag per entry
//! - Selection checkmark and keyboard navigation (Up, Down, Enter, Esc)

use std::rc::Rc;

use gpui::*;
use serde::{Deserialize, Serialize};

use crate::theme::HermesTheme;
use crate::ui::icon_svg;
use crate::ui::modals::text_input::{
    backspace as text_backspace, delete as text_delete, input_content, insert_text, move_horizontal,
    move_to as text_move_to, move_to_edge, select_all, selected_text,
};
use crate::ui::modals::{modal_press, ModalMotion};
use crate::ui::model_pill::provider_dot_color;

pub const MODEL_PICKER_ROW_HEIGHT: f32 = 40.0;
pub const MODEL_PICKER_LIST_HEIGHT: f32 = 360.0;

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
        Self::new(
            m.id.clone(),
            m.display_name.clone(),
            m.provider.clone(),
            m.is_local,
        )
    }

    pub fn display_label(&self) -> String {
        model_display_name(&self.id, &self.provider, std::slice::from_ref(self))
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

pub fn model_display_name(model_id: &str, provider: &str, models: &[ModelEntry]) -> String {
    let model_id = model_id.trim();
    if model_id.is_empty() {
        return String::new();
    }

    models
        .iter()
        .find(|model| {
            model.id == model_id
                && (provider.trim().is_empty() || model.provider.trim() == provider.trim())
        })
        .map(|model| {
            let label = model.display_name.trim();
            if label.is_empty() {
                model.id.clone()
            } else {
                label.to_string()
            }
        })
        .unwrap_or_else(|| model_id.to_string())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModelPickerStatus {
    Loading,
    Ready,
    Error(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum ModelPickerRow {
    Provider(String),
    Model(usize),
}

pub type ModelSelectHandler =
    Rc<dyn Fn(String, String, &ClickEvent, &mut Window, &mut App)>;
pub type ModelRetryHandler = Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>;

/// Model Picker modal state and rendering.
#[derive(Debug, Clone)]
pub struct ModelPickerModal {
    pub models: Vec<ModelEntry>,
    pub selected_model_id: String,
    pub selected_provider: String,
    pub highlighted_index: usize,
    pub search_query: String,
    pub search_cursor: usize,
    pub search_anchor: usize,
    pub status: ModelPickerStatus,
    pub request_id: u64,
    pub session_id: Option<String>,
    pub warning: Option<String>,
    scroll_handle: UniformListScrollHandle,
}

impl PartialEq for ModelPickerModal {
    fn eq(&self, other: &Self) -> bool {
        self.models == other.models
            && self.selected_model_id == other.selected_model_id
            && self.selected_provider == other.selected_provider
            && self.highlighted_index == other.highlighted_index
            && self.search_query == other.search_query
            && self.search_cursor == other.search_cursor
            && self.search_anchor == other.search_anchor
            && self.status == other.status
            && self.request_id == other.request_id
            && self.session_id == other.session_id
            && self.warning == other.warning
    }
}

impl ModelPickerModal {
    /// Create a picker over the live backend catalog, highlighting the active model.
    pub fn new(
        current_model: impl Into<String>,
        current_provider: impl Into<String>,
        models: Vec<ModelEntry>,
    ) -> Self {
        let selected_model_id = current_model.into();
        let selected_provider = current_provider.into();
        let highlighted_index = models
            .iter()
            .position(|m| {
                m.id == selected_model_id
                    && (selected_provider.trim().is_empty()
                        || m.provider.trim() == selected_provider.trim())
            })
            .unwrap_or(0);
        let picker = Self {
            models,
            selected_model_id,
            selected_provider,
            highlighted_index,
            search_query: String::new(),
            search_cursor: 0,
            search_anchor: 0,
            status: ModelPickerStatus::Ready,
            request_id: 0,
            session_id: None,
            warning: None,
            scroll_handle: UniformListScrollHandle::new(),
        };
        picker.scroll_highlighted_into_view();
        picker
    }

    pub fn loading(
        current_model: impl Into<String>,
        current_provider: impl Into<String>,
        request_id: u64,
        session_id: Option<String>,
    ) -> Self {
        Self {
            models: Vec::new(),
            selected_model_id: current_model.into(),
            selected_provider: current_provider.into(),
            highlighted_index: 0,
            search_query: String::new(),
            search_cursor: 0,
            search_anchor: 0,
            status: ModelPickerStatus::Loading,
            request_id,
            session_id,
            warning: None,
            scroll_handle: UniformListScrollHandle::new(),
        }
    }

    pub fn is_loading(&self) -> bool {
        matches!(self.status, ModelPickerStatus::Loading)
    }

    pub fn is_ready(&self) -> bool {
        matches!(self.status, ModelPickerStatus::Ready)
    }

    pub fn error(&self) -> Option<&str> {
        match &self.status {
            ModelPickerStatus::Error(message) => Some(message),
            _ => None,
        }
    }

    pub fn set_catalog(
        &mut self,
        models: Vec<ModelEntry>,
        active_model: impl Into<String>,
        active_provider: impl Into<String>,
        warning: Option<String>,
    ) {
        self.models = models;
        let active_model = active_model.into();
        let active_provider = active_provider.into();
        if !active_model.trim().is_empty() {
            self.selected_model_id = active_model;
            self.selected_provider = active_provider;
        }
        self.warning = warning;
        self.status = ModelPickerStatus::Ready;
        self.highlighted_index = self
            .filtered_models()
            .iter()
            .position(|model| {
                model.id == self.selected_model_id
                    && (self.selected_provider.trim().is_empty()
                        || model.provider.trim() == self.selected_provider.trim())
            })
            .unwrap_or(0);
        self.scroll_highlighted_into_view();
    }

    pub fn set_error(&mut self, message: impl Into<String>) {
        self.models.clear();
        self.warning = None;
        self.status = ModelPickerStatus::Error(message.into());
        self.highlighted_index = 0;
        self.scroll_to_top();
    }

    /// Set search filter query.
    pub fn set_search(&mut self, query: impl Into<String>) {
        self.search_query = query.into();
        self.search_cursor = self.search_query.len();
        self.search_anchor = self.search_cursor;
        self.highlighted_index = 0;
        self.scroll_to_top();
    }

    pub fn push_search_char(&mut self, text: &str) {
        insert_text(
            &mut self.search_query,
            &mut self.search_cursor,
            &mut self.search_anchor,
            text,
        );
        self.highlighted_index = 0;
        self.scroll_to_top();
    }

    pub fn backspace_search(&mut self) {
        if text_backspace(
            &mut self.search_query,
            &mut self.search_cursor,
            &mut self.search_anchor,
        ) {
            self.highlighted_index = 0;
            self.scroll_to_top();
        }
    }

    pub fn delete_search(&mut self) {
        if text_delete(
            &mut self.search_query,
            &mut self.search_cursor,
            &mut self.search_anchor,
        ) {
            self.highlighted_index = 0;
            self.scroll_to_top();
        }
    }

    pub fn move_search_cursor(&mut self, delta: isize, selecting: bool) {
        move_horizontal(
            &self.search_query,
            &mut self.search_cursor,
            &mut self.search_anchor,
            delta,
            selecting,
        );
    }

    pub fn move_search_cursor_to(&mut self, offset: usize, selecting: bool) {
        text_move_to(
            &self.search_query,
            &mut self.search_cursor,
            &mut self.search_anchor,
            offset,
            selecting,
        );
    }

    pub fn move_search_cursor_to_edge(&mut self, end: bool, selecting: bool) {
        move_to_edge(
            &self.search_query,
            &mut self.search_cursor,
            &mut self.search_anchor,
            end,
            selecting,
        );
    }

    pub fn select_all_search(&mut self) {
        select_all(
            &self.search_query,
            &mut self.search_cursor,
            &mut self.search_anchor,
        );
    }

    pub fn selected_search_text(&self) -> String {
        selected_text(&self.search_query, self.search_cursor, self.search_anchor)
    }

    /// Return filtered models matching current query.
    pub fn filtered_models(&self) -> Vec<&ModelEntry> {
        self.models
            .iter()
            .filter(|m| m.matches(&self.search_query))
            .collect()
    }

    fn rows(&self) -> Vec<ModelPickerRow> {
        Self::rows_for(
            &self
                .filtered_models()
                .into_iter()
                .cloned()
                .collect::<Vec<_>>(),
        )
    }

    fn rows_for(models: &[ModelEntry]) -> Vec<ModelPickerRow> {
        let mut rows = Vec::with_capacity(models.len() + 1);
        let mut current_provider = None;
        for (index, model) in models.iter().enumerate() {
            if current_provider.as_deref() != Some(model.provider.as_str()) {
                current_provider = Some(model.provider.clone());
                rows.push(ModelPickerRow::Provider(model.provider.clone()));
            }
            rows.push(ModelPickerRow::Model(index));
        }
        rows
    }

    fn highlighted_row_index(&self) -> Option<usize> {
        self.rows().iter().position(
            |row| matches!(row, ModelPickerRow::Model(index) if *index == self.highlighted_index),
        )
    }

    fn scroll_highlighted_into_view(&self) {
        if let Some(index) = self.highlighted_row_index() {
            self.scroll_handle
                .scroll_to_item(index, ScrollStrategy::Center);
        }
    }

    fn scroll_to_top(&self) {
        self.scroll_handle
            .scroll_to_item_strict(0, ScrollStrategy::Top);
    }

    /// Move selection highlight up.
    pub fn move_up(&mut self) {
        let total = self.filtered_models().len();
        if self.is_ready() && total > 0 {
            self.highlighted_index = if self.highlighted_index == 0 {
                total - 1
            } else {
                self.highlighted_index - 1
            };
            self.scroll_highlighted_into_view();
        }
    }

    /// Move selection highlight down.
    pub fn move_down(&mut self) {
        let total = self.filtered_models().len();
        if self.is_ready() && total > 0 {
            self.highlighted_index = (self.highlighted_index + 1) % total;
            self.scroll_highlighted_into_view();
        }
    }

    fn move_to(&mut self, index: usize) {
        let total = self.filtered_models().len();
        if self.is_ready() && total > 0 {
            self.highlighted_index = index.min(total - 1);
            self.scroll_highlighted_into_view();
        }
    }

    /// Retrieve the currently highlighted model entry.
    pub fn highlighted_model(&self) -> Option<&ModelEntry> {
        let filtered = self.filtered_models();
        filtered.get(self.highlighted_index).copied()
    }

    /// Select the currently highlighted model and return its ID.
    pub fn select_highlighted(&mut self) -> Option<String> {
        if !self.is_ready() {
            return None;
        }
        if let Some((id, provider)) = self
            .highlighted_model()
            .map(|model| (model.id.clone(), model.provider.clone()))
        {
            self.selected_model_id = id.clone();
            self.selected_provider = provider;
            Some(id)
        } else {
            None
        }
    }

    /// Handle keyboard input (Up, Down, Home, End, Enter).
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
            "home" => {
                self.move_to(0);
                None
            }
            "end" => {
                let total = self.filtered_models().len();
                self.move_to(total.saturating_sub(1));
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
        on_retry: ModelRetryHandler,
    ) -> Div {
        let filtered = self
            .filtered_models()
            .into_iter()
            .cloned()
            .collect::<Vec<_>>();

        let mut card = div()
            .w(px(540.0))
            .max_w_full()
            .max_h(px(500.0))
            .flex()
            .flex_col()
            .bg(theme.mockup_surface)
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
                        input_content(
                            &self.search_query,
                            self.search_cursor,
                            self.search_anchor,
                            "Search models across providers...",
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
                    ),
            );

        if let Some(warning) = &self.warning {
            card = card.child(
                div()
                    .px_4()
                    .py_2()
                    .border_b_1()
                    .border_color(theme.mockup_border)
                    .text_size(px(11.0))
                    .text_color(theme.mockup_text_dim)
                    .child(format!("Some providers could not be queried: {warning}")),
            );
        }

        let body = match &self.status {
            ModelPickerStatus::Loading => render_picker_message(
                "Loading model catalog…",
                "Querying the providers configured for this session.".to_string(),
                None,
                theme,
            ),
            ModelPickerStatus::Error(message) => {
                let retry = div()
                    .id("model_picker_retry")
                    .debug_selector(|| "model_picker_retry".to_string())
                    .cursor_pointer()
                    .px_3()
                    .py_1p5()
                    .rounded(px(6.0))
                    .bg(theme.mockup_hover)
                    .border_1()
                    .border_color(theme.mockup_border_strong)
                    .text_size(px(12.0))
                    .text_color(theme.mockup_text_bright)
                    .on_click(move |event, window, cx| on_retry(event, window, cx))
                    .child("Try again");
                render_picker_message("Could not load models", message.clone(), Some(retry), theme)
            }
            ModelPickerStatus::Ready if filtered.is_empty() => render_picker_message(
                "No models found",
                if self.search_query.is_empty() {
                    "The backend reported no available models.".to_string()
                } else {
                    format!("No matches for \"{}\"", self.search_query)
                },
                None,
                theme,
            ),
            ModelPickerStatus::Ready => self.render_list(filtered, theme, motion, on_select),
        };

        card.child(body)
    }

    fn render_list(
        &self,
        models: Vec<ModelEntry>,
        theme: &HermesTheme,
        motion: ModalMotion<'_>,
        on_select: ModelSelectHandler,
    ) -> Div {
        let theme = theme.clone();
        let rows = Self::rows_for(&models);
        let highlighted_index = self.highlighted_index;
        let selected_model_id = self.selected_model_id.clone();
        let selected_provider = self.selected_provider.clone();
        let scroll_handle = self.scroll_handle.clone();
        let motion_owner = motion.owner;
        let list = uniform_list(
            "model_picker_rows",
            rows.len(),
            move |range, _window, cx| {
                let motion = ModalMotion {
                    owner: motion_owner,
                    cx,
                };
                range
                    .map(|row_index| match &rows[row_index] {
                        ModelPickerRow::Provider(provider) => {
                            render_provider_row(provider, theme.clone()).into_any_element()
                        }
                        ModelPickerRow::Model(index) => render_model_row(
                            &models[*index],
                            *index,
                            &selected_model_id,
                            &selected_provider,
                            *index == highlighted_index,
                            &theme,
                            motion,
                            on_select.clone(),
                        )
                        .into_any_element(),
                    })
                    .collect::<Vec<_>>()
            },
        )
        .track_scroll(scroll_handle)
        .w_full()
        .h_full();

        // Headers and rows share fixed-height slots so GPUI can virtualize them.
        div()
            .debug_selector(|| "model_picker_list".to_string())
            .w_full()
            .h(px(MODEL_PICKER_LIST_HEIGHT))
            .overflow_hidden()
            .child(list)
    }
}

fn model_row_id(model: &ModelEntry) -> SharedString {
    SharedString::from(format!("model_row_{}_{}", model.id, model.provider))
}

fn render_provider_row(provider: &str, theme: HermesTheme) -> Div {
    div()
        .h(px(MODEL_PICKER_ROW_HEIGHT))
        .w_full()
        .px_3()
        .flex()
        .items_center()
        .text_size(px(10.0))
        .font_weight(FontWeight::SEMIBOLD)
        .text_color(theme.mockup_text_dim)
        .child(provider.to_uppercase())
}

fn render_model_row(
    model: &ModelEntry,
    index: usize,
    selected_model_id: &str,
    selected_provider: &str,
    is_highlighted: bool,
    theme: &HermesTheme,
    motion: ModalMotion<'_>,
    on_select: ModelSelectHandler,
) -> Stateful<Div> {
    let is_selected = model.id == selected_model_id
        && (selected_provider.trim().is_empty()
            || model.provider.trim() == selected_provider.trim());
    let dot_color = provider_dot_color(&model.provider, theme);
    let model_id = model.id.clone();
    let model_provider = model.provider.clone();
    let mut row = div()
        .h_full()
        .w_full()
        .flex()
        .items_center()
        .justify_between()
        .px_3()
        .rounded(px(6.0))
        .bg(if is_highlighted {
            theme.mockup_hover
        } else {
            gpui::rgba(0x00000000)
        })
        .child(
            div()
                .flex()
                .items_center()
                .gap_2p5()
                .min_w_0()
                .flex_1()
                .child(
                    div()
                        .w(px(7.0))
                        .h(px(7.0))
                        .flex_none()
                        .rounded_full()
                        .bg(dot_color),
                )
                .child(
                    div()
                        .min_w_0()
                        .flex_1()
                        .truncate()
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
                        .child(model.display_label()),
                ),
        )
        .child(
            div()
                .flex()
                .items_center()
                .gap_2()
                .flex_none()
                .child(
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

    div()
        .id(model_row_id(model))
        .debug_selector(move || format!("model_{index}"))
        .h(px(MODEL_PICKER_ROW_HEIGHT))
        .w_full()
        .cursor_pointer()
        .on_click(move |event, window, cx| {
            on_select(model_id.clone(), model_provider.clone(), event, window, cx)
        })
        .child(row)
}

fn render_picker_message(
    title: impl Into<String>,
    detail: String,
    action: Option<Stateful<Div>>,
    theme: &HermesTheme,
) -> Div {
    let title = title.into();
    let mut message = div()
        .h(px(MODEL_PICKER_LIST_HEIGHT))
        .w_full()
        .flex()
        .flex_col()
        .items_center()
        .justify_center()
        .gap_2()
        .px_6()
        .text_center()
        .child(
            div()
                .text_size(px(13.0))
                .font_weight(FontWeight::MEDIUM)
                .text_color(theme.mockup_text_bright)
                .child(title),
        )
        .child(
            div()
                .max_w_full()
                .text_size(px(11.5))
                .text_color(theme.mockup_text_dim)
                .child(detail),
        );
    if let Some(action) = action {
        message = message.child(action.mt_2());
    }
    message
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
        let mut picker = ModelPickerModal::new("gpt-4o", "OpenAI", catalog());
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
        let mut picker = ModelPickerModal::new("gpt-4o", "OpenAI", catalog());
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
        let picker = ModelPickerModal::new("qwen2.5-coder-32b", "Local", catalog());
        assert_eq!(picker.highlighted_index, 2);
        assert_eq!(picker.highlighted_model().unwrap().id, "qwen2.5-coder-32b");
    }

    #[test]
    fn test_model_picker_authoritative_catalog_updates_selection() {
        let mut picker = ModelPickerModal::loading("stale-model", "Old provider", 1, None);
        picker.set_catalog(catalog(), "qwen2.5-coder-32b", "Local", None);
        assert_eq!(picker.selected_model_id, "qwen2.5-coder-32b");
        assert_eq!(picker.selected_provider, "Local");
        assert_eq!(picker.highlighted_index, 2);
        assert_eq!(picker.highlighted_model().unwrap().display_label(), "Qwen (Ollama)");
    }

    #[test]
    fn test_model_picker_keeps_provider_pair_for_duplicate_ids() {
        let models = vec![
            ModelEntry::new("shared-id", "Cloud Shared", "OpenAI", false),
            ModelEntry::new("shared-id", "Local Shared", "Local", true),
        ];
        let picker = ModelPickerModal::new("shared-id", "Local", models.clone());
        assert_eq!(picker.highlighted_index, 1);
        assert_eq!(picker.highlighted_model().unwrap().display_label(), "Local Shared");
        assert_ne!(model_row_id(&models[0]), model_row_id(&models[1]));
    }

    #[test]
    fn test_model_picker_empty_catalog_selects_nothing() {
        let mut picker = ModelPickerModal::new("gpt-4o", "OpenAI", Vec::new());
        assert_eq!(picker.handle_key("enter"), None);
        assert_eq!(picker.selected_model_id, "gpt-4o");
    }

    #[test]
    fn loading_and_error_states_cannot_select_stale_models() {
        let mut picker = ModelPickerModal::loading("gpt-4o", "OpenAI", 7, Some("ses_1".into()));
        assert!(picker.is_loading());
        assert_eq!(picker.handle_key("enter"), None);

        picker.set_search("o3");
        picker.set_catalog(
            catalog(),
            "o3-mini",
            "OpenAI",
            Some("Local probe timed out".into()),
        );
        assert!(picker.is_ready());
        assert_eq!(picker.highlighted_model().unwrap().id, "o3-mini");
        assert_eq!(picker.warning.as_deref(), Some("Local probe timed out"));

        picker.set_error("Provider unavailable");
        assert_eq!(picker.error(), Some("Provider unavailable"));
        assert_eq!(picker.handle_key("enter"), None);
        assert!(picker.models.is_empty());
    }

    #[test]
    fn keyboard_navigation_targets_virtual_rows_and_stable_ids() {
        let mut picker = ModelPickerModal::new("gpt-4o", "OpenAI", catalog());
        assert_eq!(
            picker.rows(),
            vec![
                ModelPickerRow::Provider("OpenAI".into()),
                ModelPickerRow::Model(0),
                ModelPickerRow::Model(1),
                ModelPickerRow::Provider("Local".into()),
                ModelPickerRow::Model(2),
            ]
        );

        picker.handle_key("end");
        assert_eq!(picker.highlighted_index, 2);
        assert_eq!(picker.scroll_handle.logical_scroll_top_index(), 4);
        picker.handle_key("home");
        assert_eq!(picker.highlighted_index, 0);
        assert_eq!(picker.scroll_handle.logical_scroll_top_index(), 1);

        let stable_id = model_row_id(&catalog()[0]);
        picker.set_search("o3");
        assert_eq!(
            model_row_id(picker.highlighted_model().unwrap()),
            model_row_id(&catalog()[1])
        );
        assert_ne!(stable_id, model_row_id(&catalog()[1]));
    }
}

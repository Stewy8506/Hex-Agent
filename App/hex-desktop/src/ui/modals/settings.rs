//! DX3 settings modal: an editable settings surface driven by the ACP
//! `config/get` / `config/set` methods. The backend owns the whitelist and
//! validation; this modal mirrors the returned view, lets the operator edit
//! text fields and cycle choice fields, and sends only changed keys.

use std::collections::HashMap;

use gpui::prelude::FluentBuilder;
use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::modals::text_input::{
    backspace as text_backspace, delete as text_delete, insert_text, move_horizontal,
    move_to as text_move_to, move_to_edge, select_all, selected_text,
};
use crate::ui::modals::{modal_press, ModalMotion};
use hex_protocol::models::ConfigGetResponse;

/// One editable row in the settings modal.
#[derive(Debug, Clone, PartialEq)]
pub enum SettingField {
    /// Free-text value; Enter starts/commits editing, Esc cancels.
    Text {
        key: &'static str,
        label: &'static str,
        value: String,
        cursor: usize,
        required: bool,
    },
    /// Single-choice value cycled with Enter / ← / →; None = not set.
    Choice {
        key: &'static str,
        label: &'static str,
        options: Vec<String>,
        value: Option<String>,
    },
}

impl SettingField {
    pub fn key(&self) -> &'static str {
        match self {
            Self::Text { key, .. } | Self::Choice { key, .. } => key,
        }
    }

    pub fn label(&self) -> &'static str {
        match self {
            Self::Text { label, .. } | Self::Choice { label, .. } => label,
        }
    }

    fn display(&self) -> String {
        match self {
            Self::Text { value, .. } => value.clone(),
            Self::Choice { value, .. } => value.clone().unwrap_or_default(),
        }
    }
}

/// What the operator confirmed; executed by `ChatView` against the runtime.
#[derive(Debug, Clone, PartialEq)]
pub enum SettingsAction {
    Save,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettingsInteraction {
    SelectRow(usize),
    Save,
}

pub type SettingsHandler =
    std::rc::Rc<dyn Fn(SettingsInteraction, &ClickEvent, &mut Window, &mut App)>;

/// Editable settings modal (DX3). Fields are rebuilt from each `config/get`
/// response so the view never drifts from the file; `originals` snapshots the
/// loaded values to compute the dirty diff for Save.
#[derive(Debug, Clone)]
pub struct SettingsModal {
    pub path: String,
    pub workspace: String,
    pub data_dir: String,
    pub fields: Vec<SettingField>,
    pub options: serde_json::Value,
    pub originals: HashMap<String, String>,
    pub selected: usize, // index into fields; fields.len() == Save row
    pub editing: bool,
    selection_anchor: usize,
    edit_snapshot: Option<(String, usize, usize)>,
    pub busy: bool,
    pub error: Option<String>,
    pub status: Option<String>,
    pub load_failed: bool,
    scroll_handle: UniformListScrollHandle,
}

impl PartialEq for SettingsModal {
    fn eq(&self, other: &Self) -> bool {
        self.path == other.path
            && self.workspace == other.workspace
            && self.data_dir == other.data_dir
            && self.fields == other.fields
            && self.options == other.options
            && self.originals == other.originals
            && self.selected == other.selected
            && self.editing == other.editing
            && self.selection_anchor == other.selection_anchor
            && self.edit_snapshot == other.edit_snapshot
            && self.busy == other.busy
            && self.error == other.error
            && self.status == other.status
            && self.load_failed == other.load_failed
    }
}

impl Default for SettingsModal {
    fn default() -> Self {
        Self {
            path: String::new(),
            workspace: String::new(),
            data_dir: String::new(),
            fields: Vec::new(),
            options: serde_json::Value::Null,
            originals: HashMap::new(),
            selected: 0,
            editing: false,
            selection_anchor: 0,
            edit_snapshot: None,
            busy: false,
            error: None,
            status: None,
            load_failed: false,
            scroll_handle: UniformListScrollHandle::new(),
        }
    }
}

impl SettingsModal {
    /// Empty shell shown while `config/get` is in flight (or after a failure).
    pub fn loading() -> Self {
        Self::default()
    }

    fn option_list(options: &serde_json::Value, key: &str, fallback: &[&str]) -> Vec<String> {
        options
            .get(key)
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
            .filter(|v: &Vec<String>| !v.is_empty())
            .unwrap_or_else(|| fallback.iter().map(|s| s.to_string()).collect())
    }

    fn text_field(
        key: &'static str,
        label: &'static str,
        required: bool,
        config: Option<&str>,
    ) -> SettingField {
        let value = config.unwrap_or_default().to_string();
        SettingField::Text {
            key,
            label,
            cursor: value.len(),
            value,
            required,
        }
    }

    fn choice_field(
        key: &'static str,
        label: &'static str,
        options: &serde_json::Value,
        fallback: &[&str],
        config: Option<&str>,
    ) -> SettingField {
        SettingField::Choice {
            key,
            label,
            options: Self::option_list(options, key, fallback),
            value: config.map(str::to_string),
        }
    }

    /// Rebuild the field list from a backend view; keeps `selected` in range.
    pub fn apply_view(&mut self, view: &ConfigGetResponse) {
        let model = view.config.model.clone().unwrap_or_default();
        let runtime = view.config.runtime.clone().unwrap_or_default();
        let web = view.config.web_search.clone().unwrap_or_default();
        let cloud = view.config.cloud.clone().unwrap_or_default();
        let learning = view.config.learning.clone().unwrap_or_default();

        let fields = vec![
            Self::text_field("model.cloudProvider", "Provider", true, model.cloud_provider.as_deref()),
            Self::text_field("model.cloudModelId", "Model ID", true, model.cloud_model_id.as_deref()),
            Self::choice_field(
                "model.reasoningEffort",
                "Reasoning effort",
                &view.options,
                &["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                model.reasoning_effort.as_deref(),
            ),
            Self::choice_field(
                "model.profile",
                "Execution profile",
                &view.options,
                &["cloud", "local", "hybrid", "antigravity_hybrid"],
                model.profile.as_deref(),
            ),
            Self::choice_field(
                "model.compressorMode",
                "Log compression",
                &view.options,
                &["deterministic", "cloud", "local", "off"],
                model.compressor_mode.as_deref(),
            ),
            Self::text_field("model.cloudBaseUrl", "Model endpoint", false, model.cloud_base_url.as_deref()),
            Self::text_field("runtime.maxTurns", "Max turns", true, runtime.max_turns.map(|v| v.to_string()).as_deref()),
            Self::choice_field(
                "webSearch.provider",
                "Web search",
                &view.options,
                &["auto", "duckduckgo", "tavily", "brave"],
                web.provider.as_deref(),
            ),
            Self::text_field("cloud.baseUrl", "HEX cloud URL", false, cloud.base_url.as_deref()),
            Self::choice_field(
                "learning.autoReflect",
                "Auto-reflect",
                &view.options,
                &["cloud_only", "idle", "off"],
                learning.auto_reflect.as_deref(),
            ),
        ];

        let mut originals = HashMap::new();
        for field in &fields {
            originals.insert(field.key().to_string(), field.display());
        }

        self.path = view.path.clone();
        self.workspace = view.workspace.clone();
        self.data_dir = view.data_dir.clone();
        self.options = view.options.clone();
        self.fields = fields;
        self.originals = originals;
        self.selected = self.selected.min(self.fields.len());
        self.editing = false;
        self.selection_anchor = 0;
        self.edit_snapshot = None;
        self.load_failed = false;
        self.error = None;
        self.scroll_handle
            .scroll_to_item_strict(0, ScrollStrategy::Top);
    }

    pub fn mark_load_failed(&mut self, message: impl Into<String>) {
        self.load_failed = true;
        self.error = Some(message.into());
    }

    fn move_selection(&mut self, delta: isize) {
        self.commit_edit();
        let count = self.fields.len() as isize + 1;
        let next = (self.selected as isize + delta).rem_euclid(count);
        self.selected = next as usize;
        self.selection_anchor = 0;
        self.scroll_selected_into_view();
    }

    fn scroll_selected_into_view(&self) {
        let index = self.selected.min(self.fields.len().saturating_sub(1));
        self.scroll_handle
            .scroll_to_item(index, ScrollStrategy::Center);
    }

    pub fn select_row(&mut self, index: usize) {
        if self.busy {
            return;
        }
        self.commit_edit();
        self.selected = index.min(self.fields.len());
        self.selection_anchor = 0;
        if self.selected == self.fields.len() {
            self.scroll_selected_into_view();
            return;
        }
        match &mut self.fields[self.selected] {
            SettingField::Text { value, cursor, .. } => {
                self.editing = true;
                *cursor = value.len();
                self.selection_anchor = *cursor;
                self.edit_snapshot = Some((value.clone(), *cursor, *cursor));
            }
            SettingField::Choice { .. } => {
                self.cycle_choice(1);
            }
        }
        self.scroll_selected_into_view();
    }

    fn cycle_choice(&mut self, delta: isize) -> bool {
        let Some(SettingField::Choice { options, value, .. }) =
            self.fields.get_mut(self.selected)
        else {
            return false;
        };
        if options.is_empty() {
            return false;
        }
        let current = value
            .as_deref()
            .and_then(|v| options.iter().position(|o| o == v))
            .map(|i| i as isize)
            .unwrap_or_else(|| if delta > 0 { -1 } else { 0 });
        let next = (current + delta).rem_euclid(options.len() as isize);
        *value = Some(options[next as usize].clone());
        true
    }

    fn commit_edit(&mut self) {
        self.editing = false;
        self.edit_snapshot = None;
    }

    fn move_text_cursor(&mut self, delta: isize, selecting: bool) -> bool {
        let Some(SettingField::Text { value, cursor, .. }) = self.fields.get_mut(self.selected)
        else {
            return false;
        };
        move_horizontal(
            value,
            cursor,
            &mut self.selection_anchor,
            delta,
            selecting,
        );
        true
    }

    /// Non-printable keys; printable characters route through `push_char`.
    pub fn handle_key(&mut self, key: &str) -> Option<SettingsAction> {
        if self.busy {
            return None;
        }
        match key {
            "up" | "arrowup" => {
                self.commit_edit();
                self.move_selection(-1);
                None
            }
            "down" | "arrowdown" => {
                self.commit_edit();
                self.move_selection(1);
                None
            }
            "tab" | "shift-tab" => {
                self.move_selection(if key == "tab" { 1 } else { -1 });
                None
            }
            "left" | "arrowleft" => {
                if !(self.editing && self.move_text_cursor(-1, false)) && self.cycle_choice(-1) {
                    self.commit_edit();
                }
                None
            }
            "right" | "arrowright" => {
                if !(self.editing && self.move_text_cursor(1, false)) && self.cycle_choice(1) {
                    self.commit_edit();
                }
                None
            }
            "shift-left" | "shift-arrowleft" => {
                self.move_text_cursor(-1, true);
                None
            }
            "shift-right" | "shift-arrowright" => {
                self.move_text_cursor(1, true);
                None
            }
            "home" | "end" => {
                if self.editing {
                    if let Some(SettingField::Text { value, cursor, .. }) =
                        self.fields.get_mut(self.selected)
                    {
                        move_to_edge(
                            value,
                            cursor,
                            &mut self.selection_anchor,
                            key == "end",
                            false,
                        );
                    }
                }
                None
            }
            "shift-home" | "shift-end" => {
                if self.editing {
                    if let Some(SettingField::Text { value, cursor, .. }) =
                        self.fields.get_mut(self.selected)
                    {
                        move_to_edge(
                            value,
                            cursor,
                            &mut self.selection_anchor,
                            key == "shift-end",
                            true,
                        );
                    }
                }
                None
            }
            "enter" => {
                if self.selected == self.fields.len() {
                    if self.dirty_count() == 0 {
                        return None;
                    }
                    self.busy = true;
                    return Some(SettingsAction::Save);
                }
                match self.fields.get_mut(self.selected) {
                    Some(SettingField::Text { value, cursor, .. }) => {
                        if self.editing {
                            self.commit_edit();
                        } else {
                            self.editing = true;
                            *cursor = value.len();
                            self.selection_anchor = *cursor;
                            self.edit_snapshot = Some((value.clone(), *cursor, *cursor));
                        }
                        None
                    }
                    Some(SettingField::Choice { .. }) => {
                        self.commit_edit();
                        self.cycle_choice(1);
                        None
                    }
                    None => None,
                }
            }
            _ => None,
        }
    }

    /// Leave edit mode without committing; restores the pre-edit value.
    pub fn cancel_edit(&mut self) {
        if !self.editing {
            return;
        }
        self.editing = false;
        if let Some((snapshot, cursor, anchor)) = self.edit_snapshot.take() {
            if let Some(SettingField::Text { value, cursor: current, .. }) =
                self.fields.get_mut(self.selected)
            {
                *value = snapshot;
                *current = cursor;
                self.selection_anchor = anchor;
            }
        }
    }

    pub fn text_selection(&self) -> Option<(String, usize, usize)> {
        let SettingField::Text { value, cursor, .. } = self.fields.get(self.selected)? else {
            return None;
        };
        Some((value.clone(), *cursor, self.selection_anchor))
    }

    pub fn replace_text_range(&mut self, range: std::ops::Range<usize>, text: &str) -> bool {
        if self.busy || !self.editing {
            return false;
        }
        self.move_cursor_to(range.start, false);
        self.move_cursor_to(range.end, true);
        self.push_char(text);
        true
    }

    pub fn select_text_range(&mut self, range: std::ops::Range<usize>) {
        self.move_cursor_to(range.start, false);
        self.move_cursor_to(range.end, true);
    }

    pub fn push_char(&mut self, text: &str) {
        if self.busy || !self.editing {
            return;
        }
        if let Some(SettingField::Text { value, cursor, .. }) = self.fields.get_mut(self.selected) {
            insert_text(value, cursor, &mut self.selection_anchor, text);
        }
    }

    pub fn backspace(&mut self) {
        if self.busy || !self.editing {
            return;
        }
        if let Some(SettingField::Text { value, cursor, .. }) = self.fields.get_mut(self.selected) {
            text_backspace(value, cursor, &mut self.selection_anchor);
        }
    }

    pub fn delete(&mut self) {
        if self.busy || !self.editing {
            return;
        }
        if let Some(SettingField::Text { value, cursor, .. }) = self.fields.get_mut(self.selected) {
            text_delete(value, cursor, &mut self.selection_anchor);
        }
    }

    pub fn move_cursor_to(&mut self, offset: usize, selecting: bool) {
        if !self.editing {
            return;
        }
        if let Some(SettingField::Text { value, cursor, .. }) = self.fields.get_mut(self.selected) {
            text_move_to(
                value,
                cursor,
                &mut self.selection_anchor,
                offset,
                selecting,
            );
        }
    }

    pub fn select_all_text(&mut self) {
        if !self.editing {
            return;
        }
        if let Some(SettingField::Text { value, cursor, .. }) = self.fields.get_mut(self.selected) {
            select_all(value, cursor, &mut self.selection_anchor);
        }
    }

    pub fn selected_text(&self) -> Option<String> {
        self.editing.then(|| {
            let SettingField::Text { value, cursor, .. } = &self.fields[self.selected] else {
                return String::new();
            };
            selected_text(value, *cursor, self.selection_anchor)
        })
    }

    /// Dotted keys whose committed value differs from the loaded snapshot.
    pub fn dirty_updates(&self) -> serde_json::Value {
        let mut groups: HashMap<String, serde_json::Map<String, serde_json::Value>> =
            HashMap::new();
        for field in &self.fields {
            if field.display() == self.originals.get(field.key()).cloned().unwrap_or_default()
            {
                continue;
            }
            let (section, key) = field.key().split_once('.').unwrap_or((field.key(), ""));
            groups
                .entry(section.to_string())
                .or_default()
                .insert(key.to_string(), serde_json::Value::String(field.display()));
        }
        serde_json::Value::Object(
            groups
                .into_iter()
                .map(|(k, v)| (k, serde_json::Value::Object(v)))
                .collect(),
        )
    }

    pub fn dirty_count(&self) -> usize {
        self.fields
            .iter()
            .filter(|f| f.display() != self.originals.get(f.key()).cloned().unwrap_or_default())
            .count()
    }

    /// Adopt a successful save response; returns the changed-key summary.
    pub fn apply_saved(&mut self, view: &hex_protocol::models::ConfigSetResponse) -> usize {
        let changed = view.changed.len();
        let getter = ConfigGetResponse {
            path: view.path.clone(),
            workspace: view.workspace.clone(),
            data_dir: view.data_dir.clone(),
            config: view.config.clone(),
            options: view.options.clone(),
        };
        self.apply_view(&getter);
        self.busy = false;
        self.editing = false;
        self.status = Some(format!(
            "Saved {} key{} to {}",
            changed,
            if changed == 1 { "" } else { "s" },
            self.path
        ));
        changed
    }

    fn section_label(key: &str) -> &'static str {
        match key.split('.').next().unwrap_or(key) {
            "model" => "Model",
            "runtime" => "Runtime",
            "webSearch" => "Web search",
            "cloud" => "Cloud",
            "learning" => "Learning",
            _ => "Other",
        }
    }

    /// Renders the modal card content.
    pub fn render(
        &self,
        theme: &HermesTheme,
        motion: ModalMotion<'_>,
        on_action: SettingsHandler,
    ) -> Div {
        let mut card = div()
            .debug_selector(|| "settings_content".to_string())
            .w(px(560.0))
            .max_w_full()
            .flex()
            .flex_col()
            .gap(px(10.0))
            .px(px(20.0))
            .py(px(18.0));

        card = card.child(
            div()
                .flex()
                .items_center()
                .justify_between()
                .child(
                    div()
                        .text_size(px(MockupTypeScale::FEATURE_LABEL_SIZE))
                        .font_weight(FontWeight::SEMIBOLD)
                        .text_color(theme.mockup_text)
                        .child("Settings"),
                )
                .child(
                    div()
                        .px(px(6.0))
                        .py(px(2.0))
                        .rounded(px(3.0))
                        .bg(theme.mockup_hover)
                        .border_1()
                        .border_color(theme.mockup_border)
                        .font_family(theme.font_mono()[0].clone())
                        .text_size(px(MockupTypeScale::HINT_SIZE))
                        .text_color(theme.mockup_text_dim)
                        .child("Esc"),
                ),
        );

        if self.load_failed {
            card = card.child(
                div()
                    .text_size(px(MockupTypeScale::BASE_SIZE))
                    .text_color(theme.mockup_text)
                    .child("Settings could not be loaded."),
            );
            if let Some(error) = &self.error {
                card = card.child(
                    div()
                        .text_size(px(MockupTypeScale::HINT_SIZE))
                        .text_color(theme.rose_fg())
                        .child(error.clone()),
                );
            }
            return card.child(
                div()
                    .font_family(theme.font_mono()[0].clone())
                    .text_size(px(MockupTypeScale::HINT_SIZE))
                    .text_color(theme.mockup_text_dim)
                    .child("Esc — close"),
            );
        }

        card = card.child(
            div()
                .flex()
                .flex_col()
                .gap(px(2.0))
                .text_size(px(MockupTypeScale::HINT_SIZE))
                .text_color(theme.mockup_text_dim)
                .child(format!("File · {}", self.path))
                .child(format!("Workspace · {}", self.workspace))
                .child(format!("Data · {}", self.data_dir)),
        );

        if self.fields.is_empty() {
            card = card.child(
                div()
                    .text_size(px(MockupTypeScale::BASE_SIZE))
                    .text_color(theme.mockup_text_dim)
                    .child("Loading settings…"),
            );
        }

        if !self.fields.is_empty() {
            let fields = self.fields.clone();
            let originals = self.originals.clone();
            let selected = self.selected;
            let editing = self.editing;
            let selection_anchor = self.selection_anchor;
            let busy = self.busy;
            let scroll_handle = self.scroll_handle.clone();
            let theme = theme.clone();
            let on_action = on_action.clone();
            let list = uniform_list(
                "settings_rows",
                fields.len(),
                move |range, _window, cx| {
                    let motion = ModalMotion {
                        owner: motion.owner,
                        cx,
                    };
                    range
                        .map(|index| {
                            let show_section = index == 0
                                || fields[index].key().split('.').next()
                                    != fields[index - 1].key().split('.').next();
                            render_setting_row(
                                &fields[index],
                                index,
                                show_section,
                                index == selected,
                                editing,
                                selection_anchor,
                                busy,
                                &originals,
                                &theme,
                                motion,
                                on_action.clone(),
                            )
                            .into_any_element()
                        })
                        .collect::<Vec<_>>()
                },
            )
            .track_scroll(scroll_handle)
            .w_full()
            .h_full();
            card = card.child(
                div()
                    .debug_selector(|| "settings_list".to_string())
                    .h(px(320.0))
                    .w_full()
                    .overflow_hidden()
                    .child(list),
            );
        }

        let dirty = self.dirty_count();
        let save_selected = self.selected == self.fields.len();
        card = card.child(modal_press(
            div()
                .id("settings_save_row")
                .debug_selector(|| "settings_save_row".into())
                .mt(px(12.0))
                .w_full()
                .flex()
                .items_center()
                .justify_between()
                .px(px(12.0))
                .py(px(8.0))
                .rounded(px(8.0))
                .border_1()
                .border_color(if save_selected {
                    theme.accent_blue
                } else {
                    theme.mockup_border
                })
                .when(save_selected, |c| c.bg(theme.mockup_hover))
                .when(!save_selected, |c| c.hover(|s| s.bg(theme.mockup_hover)))
                .cursor_pointer()
                .on_click(move |event, window, cx| {
                    on_action(SettingsInteraction::Save, event, window, cx)
                })
                .child(
                    div()
                        .text_size(px(MockupTypeScale::BASE_SIZE))
                        .font_weight(FontWeight::MEDIUM)
                        .text_color(if dirty > 0 {
                            theme.mockup_text
                        } else {
                            theme.mockup_text_dim
                        })
                        .child(if dirty > 0 {
                            format!("Save changes ({dirty})")
                        } else {
                            "No changes".to_string()
                        }),
                )
                .child(
                    div()
                        .font_family(theme.font_mono()[0].clone())
                        .text_size(px(MockupTypeScale::HINT_SIZE))
                        .text_color(theme.mockup_text_dim)
                        .child("Enter"),
                ),
            "settings_save_press",
            motion,
        ));

        if let Some(status) = &self.status {
            card = card.child(
                div()
                    .text_size(px(MockupTypeScale::HINT_SIZE))
                    .text_color(theme.accent_blue)
                    .child(status.clone()),
            );
        }
        if let Some(error) = &self.error {
            card = card.child(
                div()
                    .text_size(px(MockupTypeScale::HINT_SIZE))
                    .text_color(theme.rose_fg())
                    .child(error.clone()),
            );
        }

        card.child(
            div()
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(MockupTypeScale::HINT_SIZE))
                .text_color(theme.mockup_text_dim)
                .child("↑↓ — select · Enter — edit / cycle · ←→ — move / cycle · Home/End — jump · Del — forward · Esc — cancel / close"),
        )
    }
}

fn settings_click_handler(
    on_action: &SettingsHandler,
    interaction: SettingsInteraction,
) -> crate::ui::modals::text_input::ModalClickHandler {
    let on_action = on_action.clone();
    std::rc::Rc::new(move |event, window, cx| on_action(interaction, event, window, cx))
}

fn render_setting_row(
    field: &SettingField,
    index: usize,
    show_section: bool,
    selected: bool,
    editing: bool,
    selection_anchor: usize,
    busy: bool,
    originals: &HashMap<String, String>,
    theme: &HermesTheme,
    motion: ModalMotion<'_>,
    on_action: SettingsHandler,
) -> Stateful<Div> {
    let dirty = field.display() != originals.get(field.key()).cloned().unwrap_or_default();
    let mut value = match field {
        SettingField::Text {
            value,
            cursor,
            required,
            ..
        } => {
            if selected && editing {
                crate::ui::modals::text_input::input_content(
                    value,
                    *cursor,
                    selection_anchor,
                    if *required { "required" } else { "not set" },
                    false,
                    !busy,
                    theme,
                )
            } else if value.is_empty() {
                div()
                    .text_color(theme.mockup_text_dim)
                    .child(if *required { "required" } else { "not set" })
            } else {
                div()
                    .text_color(theme.mockup_text_bright)
                    .child(value.clone())
            }
        }
        SettingField::Choice { value, .. } => {
            let current = value
                .as_deref()
                .map(str::to_string)
                .unwrap_or_else(|| "not set".to_string());
            div()
                .text_color(if value.is_none() {
                    theme.mockup_text_dim
                } else {
                    theme.mockup_text_bright
                })
                .child(if selected {
                    format!("‹ {current} ›")
                } else {
                    current
                })
        }
    };
    value = value
        .font_family(theme.font_mono()[0].clone())
        .text_size(px(MockupTypeScale::INLINE_CODE_SIZE))
        .when(dirty, |this| this.text_color(theme.accent_blue))
        .flex_1()
        .min_w_0()
        .justify_end()
        .overflow_hidden()
        .whitespace_nowrap();

    let handler = settings_click_handler(
        &on_action,
        SettingsInteraction::SelectRow(index),
    );
    let mut row = div()
        .id(SharedString::from(format!("settings_row_{index}")))
        .debug_selector(move || format!("settings_row_{index}"))
        .h(px(64.0))
        .w_full()
        .flex()
        .flex_col()
        .justify_center()
        .gap(px(3.0))
        .px(px(12.0))
        .rounded(px(8.0))
        .border_1()
        .border_color(if selected {
            theme.accent_blue
        } else {
            gpui::rgba(0x00000000)
        })
        .when(selected, |this| this.bg(theme.mockup_hover))
        .when(!selected && !busy, |this| {
            this.hover(|this| this.bg(theme.mockup_hover))
        })
        .when(!busy, |this| {
            this.cursor_pointer()
                .on_click(move |event, window, cx| handler(event, window, cx))
        })
        .child(
            div()
                .w_full()
                .flex()
                .items_center()
                .justify_between()
                .gap(px(12.0))
                .child(
                    div()
                        .text_size(px(MockupTypeScale::BASE_SIZE))
                        .text_color(if selected {
                            theme.mockup_text
                        } else {
                            theme.mockup_session_inactive
                        })
                        .child(field.label()),
                )
                .child(value),
        );
    if show_section {
        row = row.child(
            div()
                .text_size(px(9.5))
                .font_weight(FontWeight::MEDIUM)
                .text_color(theme.mockup_text_muted)
                .child(SettingsModal::section_label(field.key())),
        );
    }
    if selected {
        row = modal_press(row, "settings_selected_press", motion);
    }
    row
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    fn sample_view() -> ConfigGetResponse {
        serde_json::from_value(serde_json::json!({
            "path": "~/.hex/config.yaml",
            "workspace": "/ws",
            "dataDir": "/ws/.hex/data",
            "config": {
                "model": {
                    "cloudProvider": "gemini",
                    "cloudModelId": "gemini-2.5-pro",
                    "reasoningEffort": "low",
                    "profile": "cloud"
                },
                "runtime": { "maxTurns": 90 },
                "webSearch": { "provider": "brave" }
            },
            "options": {
                "model.reasoningEffort": ["none", "low", "medium", "high"],
                "model.profile": ["cloud", "local", "hybrid", "antigravity_hybrid"]
            }
        }))
        .expect("valid view")
    }

    #[test]
    fn test_load_failure_retains_detail() {
        let mut modal = SettingsModal::loading();
        modal.mark_load_failed("config/get failed: permission denied");
        assert!(modal.load_failed);
        assert_eq!(modal.error.as_deref(), Some("config/get failed: permission denied"));
    }

    #[test]
    fn test_apply_view_builds_fields_and_dirty_tracking() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());
        assert_eq!(modal.fields.len(), 10);
        assert_eq!(modal.dirty_count(), 0);

        // Choice cycling from the set value; dirty count follows.
        let effort_idx = modal
            .fields
            .iter()
            .position(|f| f.key() == "model.reasoningEffort")
            .unwrap();
        modal.selected = effort_idx;
        assert!(modal.cycle_choice(1));
        assert_eq!(modal.dirty_count(), 1);
        let updates = modal.dirty_updates();
        assert_eq!(
            updates,
            serde_json::json!({ "model": { "reasoningEffort": "medium" } })
        );

        // Cycle wraps within the backend-supplied vocabulary.
        let field = &modal.fields[effort_idx];
        let SettingField::Choice { options, value, .. } = field else {
            panic!("choice field");
        };
        assert_eq!(options.join(","), "none,low,medium,high");
        assert_eq!(value.as_deref(), Some("medium"));
    }

    #[test]
    fn test_text_editing_commit_and_cancel() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());
        let provider_idx = modal
            .fields
            .iter()
            .position(|f| f.key() == "model.cloudProvider")
            .unwrap();
        modal.selected = provider_idx;

        modal.handle_key("enter"); // start editing (field prefilled)
        assert!(modal.editing);
        // Editing appends to the prefilled value; clear it first.
        for _ in 0..6 {
            modal.backspace();
        }
        modal.push_char("a");
        modal.push_char("n");
        modal.push_char("t");
        assert_eq!(modal.dirty_count(), 1);
        modal.handle_key("enter"); // commit
        assert!(!modal.editing);
        let updates = modal.dirty_updates();
        assert_eq!(updates["model"]["cloudProvider"], "ant");

        // Esc cancels the in-flight edit back to the pre-edit value.
        modal.handle_key("enter"); // start editing again
        modal.push_char("x");
        modal.cancel_edit();
        assert_eq!(modal.dirty_count(), 1); // only the committed edit remains
        assert_eq!(modal.fields[provider_idx].display(), "ant");
    }

    #[test]
    fn test_text_cursor_editing_and_grapheme_deletion() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());
        let provider_idx = modal
            .fields
            .iter()
            .position(|f| f.key() == "model.cloudProvider")
            .unwrap();
        modal.selected = provider_idx;
        modal.handle_key("enter");

        modal.handle_key("left");
        modal.handle_key("left");
        modal.push_char("x");
        assert_eq!(modal.fields[provider_idx].display(), "gemixni");
        modal.backspace();
        assert_eq!(modal.fields[provider_idx].display(), "gemini");
        modal.delete();
        assert_eq!(modal.fields[provider_idx].display(), "gemii");

        modal.handle_key("home");
        modal.push_char("a");
        modal.handle_key("end");
        modal.push_char("z");
        assert_eq!(modal.fields[provider_idx].display(), "agemiiz");
        modal.cancel_edit();
        assert_eq!(modal.fields[provider_idx].display(), "gemini");
        let SettingField::Text { cursor, .. } = &modal.fields[provider_idx] else {
            panic!("text field");
        };
        assert_eq!(*cursor, "gemini".len());

        let value = "e\u{301}👩\u{200d}💻x".to_string();
        modal.fields[provider_idx] = SettingField::Text {
            key: "model.cloudProvider",
            label: "Provider",
            cursor: value.len(),
            value,
            required: true,
        };
        modal.handle_key("enter");
        modal.handle_key("left");
        modal.push_char("!");
        assert_eq!(modal.fields[provider_idx].display(), "e\u{301}👩\u{200d}💻!x");
        modal.backspace();
        modal.handle_key("left");
        modal.delete();
        assert_eq!(modal.fields[provider_idx].display(), "e\u{301}x");
    }

    #[test]
    fn test_row_navigation_commits_text_and_preserves_choice_cycling() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());
        modal.handle_key("enter");
        modal.push_char("x");
        modal.handle_key("down");
        assert!(!modal.editing);
        assert_eq!(modal.selected, 1);
        assert_eq!(modal.fields[0].display(), "geminix");
        assert_eq!(modal.fields[1].display(), "gemini-2.5-pro");

        let effort_idx = modal
            .fields
            .iter()
            .position(|f| f.key() == "model.reasoningEffort")
            .unwrap();
        modal.selected = effort_idx;
        modal.handle_key("left");
        assert_eq!(modal.fields[effort_idx].display(), "none");
        modal.handle_key("right");
        assert_eq!(modal.fields[effort_idx].display(), "low");
    }

    #[test]
    fn test_save_row_requires_changes_and_is_last() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());
        assert_eq!(modal.selected, 0);
        // Navigate to the Save row (last index) with nothing changed.
        modal.selected = modal.fields.len();
        assert_eq!(modal.handle_key("enter"), None);

        // One committed change makes Save fire.
        let effort_idx = modal
            .fields
            .iter()
            .position(|f| f.key() == "model.reasoningEffort")
            .unwrap();
        modal.selected = effort_idx;
        assert!(modal.cycle_choice(1));
        modal.selected = modal.fields.len();
        assert_eq!(modal.handle_key("enter"), Some(SettingsAction::Save));
    }
}

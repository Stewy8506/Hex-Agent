//! DX3 settings modal: an editable settings surface driven by the ACP
//! `config/get` / `config/set` methods. The backend owns the whitelist and
//! validation; this modal mirrors the returned view, lets the operator edit
//! text fields and cycle choice fields, and sends only changed keys.

use std::collections::HashMap;

use gpui::prelude::FluentBuilder;
use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use hex_protocol::models::ConfigGetResponse;

/// One editable row in the settings modal.
#[derive(Debug, Clone, PartialEq)]
pub enum SettingField {
    /// Free-text value; Enter starts/commits editing, Esc cancels.
    Text {
        key: &'static str,
        label: &'static str,
        value: String,
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

/// Editable settings modal (DX3). Fields are rebuilt from each `config/get`
/// response so the view never drifts from the file; `originals` snapshots the
/// loaded values to compute the dirty diff for Save.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct SettingsModal {
    pub path: String,
    pub workspace: String,
    pub data_dir: String,
    pub fields: Vec<SettingField>,
    pub options: serde_json::Value,
    pub originals: HashMap<String, String>,
    pub selected: usize, // index into fields; fields.len() == Save row
    pub editing: bool,
    edit_snapshot: Option<String>,
    pub busy: bool,
    pub error: Option<String>,
    pub status: Option<String>,
    pub load_failed: bool,
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
        SettingField::Text {
            key,
            label,
            value: config.unwrap_or_default().to_string(),
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
        self.edit_snapshot = None;
        self.load_failed = false;
        self.error = None;
    }

    pub fn mark_load_failed(&mut self, message: impl Into<String>) {
        self.load_failed = true;
        self.error = Some(message.into());
    }

    fn move_selection(&mut self, delta: isize) {
        let count = self.fields.len() as isize + 1; // + the Save row
        let next = (self.selected as isize + delta).rem_euclid(count);
        self.selected = next as usize;
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

    /// Non-printable keys; printable characters route through `push_char`.
    pub fn handle_key(&mut self, key: &str) -> Option<SettingsAction> {
        if self.busy {
            return None;
        }
        match key {
            "up" | "arrowup" => {
                self.move_selection(-1);
                None
            }
            "down" | "arrowdown" => {
                self.move_selection(1);
                None
            }
            "left" | "arrowleft" => {
                if self.cycle_choice(-1) {
                    self.editing = false;
                }
                None
            }
            "right" | "arrowright" => {
                if self.cycle_choice(1) {
                    self.editing = false;
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
                match self.fields.get(self.selected) {
                    Some(SettingField::Text { value, .. }) => {
                        if self.editing {
                            self.editing = false;
                            self.edit_snapshot = None;
                        } else {
                            self.editing = true;
                            self.edit_snapshot = Some(value.clone());
                        }
                        None
                    }
                    Some(SettingField::Choice { .. }) => {
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
        if let Some(snapshot) = self.edit_snapshot.take() {
            if let Some(SettingField::Text { value, .. }) = self.fields.get_mut(self.selected) {
                *value = snapshot;
            }
        }
    }

    pub fn push_char(&mut self, ch: &str) {
        if self.busy || !self.editing {
            return;
        }
        if let Some(SettingField::Text { value, .. }) = self.fields.get_mut(self.selected) {
            value.push_str(ch);
        }
    }

    pub fn backspace(&mut self) {
        if self.busy || !self.editing {
            return;
        }
        if let Some(SettingField::Text { value, .. }) = self.fields.get_mut(self.selected) {
            value.pop();
        }
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
    pub fn render(&self, theme: &HermesTheme) -> Div {
        let mut card = div()
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
                    .text_color(theme.mockup_text_dim)
                    .child("Runtime offline — settings unavailable until HEX connects."),
            );
            return card;
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

        let mut last_section: Option<&str> = None;
        for (index, field) in self.fields.iter().enumerate() {
            let section = field.key().split('.').next().unwrap_or(field.key());
            if last_section != Some(section) {
                last_section = Some(section);
                card = card.child(
                    div()
                        .mt(px(if index == 0 { 2.0 } else { 10.0 }))
                        .text_size(px(MockupTypeScale::HINT_SIZE))
                        .font_weight(FontWeight::MEDIUM)
                        .text_color(theme.mockup_text_muted)
                        .child(Self::section_label(field.key())),
                );
            }
            let is_selected = self.selected == index;
            let is_editing = is_selected && self.editing;
            let dirty = field.display()
                != self.originals.get(field.key()).cloned().unwrap_or_default();
            let value_text = match field {
                SettingField::Text { value, required, .. } => {
                    if is_editing {
                        format!("{value}▌")
                    } else if value.is_empty() {
                        if *required {
                            "required".to_string()
                        } else {
                            "not set".to_string()
                        }
                    } else {
                        value.clone()
                    }
                }
                SettingField::Choice { value, .. } => {
                    let current = value
                        .as_deref()
                        .map(str::to_string)
                        .unwrap_or_else(|| "not set".to_string());
                    if is_selected {
                        format!("{current}  ‹ ›")
                    } else {
                        current
                    }
                }
            };
            let value_color = if dirty {
                theme.accent_blue
            } else if value_text == "not set" || value_text == "required" {
                theme.mockup_text_dim
            } else {
                theme.mockup_text_bright
            };
            card = card.child(
                div()
                    .id(SharedString::from(format!("settings_row_{}", index)))
                    .w_full()
                    .flex()
                    .flex_row()
                    .items_center()
                    .justify_between()
                    .gap(px(12.0))
                    .px(px(12.0))
                    .py(px(6.0))
                    .rounded(px(8.0))
                    .when(is_selected, |c| c.bg(theme.mockup_hover))
                    .when(!is_selected, |c| c.hover(|s| s.bg(theme.mockup_hover)))
                    .child(
                        div()
                            .text_size(px(MockupTypeScale::BASE_SIZE))
                            .text_color(if is_selected {
                                theme.mockup_text
                            } else {
                                theme.mockup_session_inactive
                            })
                            .child(field.label()),
                    )
                    .child(
                        div()
                            .font_family(theme.font_mono()[0].clone())
                            .text_size(px(MockupTypeScale::INLINE_CODE_SIZE))
                            .text_color(value_color)
                            .overflow_hidden()
                            .whitespace_nowrap()
                            .child(value_text),
                    ),
            );
        }

        let dirty = self.dirty_count();
        let save_selected = self.selected == self.fields.len();
        card = card.child(
            div()
                .id("settings_save_row")
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
        );

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
                .child("↑↓ — select · Enter — edit / cycle · ←→ — cycle choice · Esc — cancel / close"),
        )
    }
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

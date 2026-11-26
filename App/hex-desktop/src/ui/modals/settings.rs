//! Editable settings surface driven by the ACP `config/get` / `config/set`
//! methods. The backend owns the whitelist and validation; desktop descriptors
//! organize those values into sections and send only changed keys.

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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettingsSection {
    Model,
    Runtime,
    WebSearch,
    Cloud,
    Learning,
}

impl SettingsSection {
    pub const ALL: [Self; 5] = [
        Self::Model,
        Self::Runtime,
        Self::WebSearch,
        Self::Cloud,
        Self::Learning,
    ];

    pub fn title(self) -> &'static str {
        match self {
            Self::Model => "Model",
            Self::Runtime => "Runtime",
            Self::WebSearch => "Web search",
            Self::Cloud => "Cloud",
            Self::Learning => "Learning",
        }
    }

    pub fn description(self) -> &'static str {
        match self {
            Self::Model => "Choose the provider, model, and execution behavior.",
            Self::Runtime => "Control how long an agent turn may run.",
            Self::WebSearch => "Configure the provider used for web lookups.",
            Self::Cloud => "Set the HEX cloud service endpoint.",
            Self::Learning => "Choose when HEX reflects on completed work.",
        }
    }

    pub fn id(self) -> &'static str {
        match self {
            Self::Model => "model",
            Self::Runtime => "runtime",
            Self::WebSearch => "web_search",
            Self::Cloud => "cloud",
            Self::Learning => "learning",
        }
    }

    fn adjacent(self, delta: isize) -> Self {
        let current = Self::ALL
            .iter()
            .position(|section| *section == self)
            .unwrap_or(0) as isize;
        Self::ALL[(current + delta).rem_euclid(Self::ALL.len() as isize) as usize]
    }
}

#[derive(Debug, Clone, Copy)]
enum SettingKind {
    Text { required: bool },
    Number { min: u64, max: u64 },
    Choice { fallback: &'static [&'static str] },
}

#[derive(Debug, Clone, Copy)]
struct SettingDescriptor {
    section: SettingsSection,
    key: &'static str,
    label: &'static str,
    description: &'static str,
    kind: SettingKind,
}

const SETTING_DESCRIPTORS: [SettingDescriptor; 10] = [
    SettingDescriptor {
        section: SettingsSection::Model,
        key: "model.cloudProvider",
        label: "Provider",
        description: "Cloud provider used for new agent sessions.",
        kind: SettingKind::Text { required: true },
    },
    SettingDescriptor {
        section: SettingsSection::Model,
        key: "model.cloudModelId",
        label: "Model ID",
        description: "Default model identifier for cloud sessions.",
        kind: SettingKind::Text { required: true },
    },
    SettingDescriptor {
        section: SettingsSection::Model,
        key: "model.reasoningEffort",
        label: "Reasoning effort",
        description: "How much reasoning the model should use.",
        kind: SettingKind::Choice {
            fallback: &["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        },
    },
    SettingDescriptor {
        section: SettingsSection::Model,
        key: "model.profile",
        label: "Execution profile",
        description: "Where HEX runs tools and model requests.",
        kind: SettingKind::Choice {
            fallback: &["cloud", "local", "hybrid", "antigravity_hybrid"],
        },
    },
    SettingDescriptor {
        section: SettingsSection::Model,
        key: "model.compressorMode",
        label: "Log compression",
        description: "How large tool output is compressed.",
        kind: SettingKind::Choice {
            fallback: &["deterministic", "cloud", "local", "off"],
        },
    },
    SettingDescriptor {
        section: SettingsSection::Model,
        key: "model.cloudBaseUrl",
        label: "Model endpoint",
        description: "Optional custom endpoint for the cloud provider.",
        kind: SettingKind::Text { required: false },
    },
    SettingDescriptor {
        section: SettingsSection::Runtime,
        key: "runtime.maxTurns",
        label: "Max turns",
        description: "Maximum model turns allowed in one run.",
        kind: SettingKind::Number { min: 1, max: 1000 },
    },
    SettingDescriptor {
        section: SettingsSection::WebSearch,
        key: "webSearch.provider",
        label: "Web search",
        description: "Search backend used when a tool needs the web.",
        kind: SettingKind::Choice {
            fallback: &["auto", "duckduckgo", "tavily", "brave"],
        },
    },
    SettingDescriptor {
        section: SettingsSection::Cloud,
        key: "cloud.baseUrl",
        label: "HEX cloud URL",
        description: "Base URL for account and cloud services.",
        kind: SettingKind::Text { required: false },
    },
    SettingDescriptor {
        section: SettingsSection::Learning,
        key: "learning.autoReflect",
        label: "Auto-reflect",
        description: "When HEX should learn from completed sessions.",
        kind: SettingKind::Choice {
            fallback: &["cloud_only", "idle", "off"],
        },
    },
];

fn setting_descriptor(key: &str) -> &'static SettingDescriptor {
    SETTING_DESCRIPTORS
        .iter()
        .find(|descriptor| descriptor.key == key)
        .expect("every setting field has a descriptor")
}

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
    /// Integer value with backend-aligned range validation.
    Number {
        key: &'static str,
        label: &'static str,
        value: String,
        cursor: usize,
        min: u64,
        max: u64,
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
            Self::Text { key, .. } | Self::Number { key, .. } | Self::Choice { key, .. } => key,
        }
    }

    pub fn label(&self) -> &'static str {
        match self {
            Self::Text { label, .. } | Self::Number { label, .. } | Self::Choice { label, .. } => {
                label
            }
        }
    }

    fn display(&self) -> String {
        match self {
            Self::Text { value, .. } | Self::Number { value, .. } => value.clone(),
            Self::Choice { value, .. } => value.clone().unwrap_or_default(),
        }
    }

    fn text_state_mut(&mut self) -> Option<(&mut String, &mut usize)> {
        match self {
            Self::Text { value, cursor, .. } | Self::Number { value, cursor, .. } => {
                Some((value, cursor))
            }
            Self::Choice { .. } => None,
        }
    }

    fn wire_value(&self) -> Result<serde_json::Value, String> {
        match self {
            Self::Text { value, .. } => Ok(serde_json::Value::String(value.clone())),
            Self::Choice { value, .. } => Ok(serde_json::Value::String(
                value.as_deref().unwrap_or_default().to_string(),
            )),
            Self::Number {
                value,
                min,
                max,
                key,
                ..
            } => value
                .parse::<u64>()
                .map_err(|_| format!("{key} must be an integer between {min} and {max}"))
                .and_then(|parsed| {
                    if parsed < *min || parsed > *max {
                        Err(format!("{key} must be an integer between {min} and {max}"))
                    } else {
                        Ok(serde_json::Value::from(parsed))
                    }
                }),
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
    SelectSection(SettingsSection),
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
    pub active_section: SettingsSection,
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
            && self.active_section == other.active_section
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
            active_section: SettingsSection::Model,
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

    fn field_from_descriptor(
        descriptor: &SettingDescriptor,
        raw: Option<&serde_json::Value>,
        options: &serde_json::Value,
    ) -> SettingField {
        match descriptor.kind {
            SettingKind::Text { required } => {
                let value = raw
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                SettingField::Text {
                    key: descriptor.key,
                    label: descriptor.label,
                    cursor: value.len(),
                    value,
                    required,
                }
            }
            SettingKind::Number { min, max } => {
                let value = raw
                    .and_then(serde_json::Value::as_u64)
                    .map(|value| value.to_string())
                    .unwrap_or_default();
                SettingField::Number {
                    key: descriptor.key,
                    label: descriptor.label,
                    cursor: value.len(),
                    value,
                    min,
                    max,
                }
            }
            SettingKind::Choice { fallback } => SettingField::Choice {
                key: descriptor.key,
                label: descriptor.label,
                options: Self::option_list(options, descriptor.key, fallback),
                value: raw.and_then(serde_json::Value::as_str).map(str::to_string),
            },
        }
    }

    /// Rebuild the field list from a backend view; keeps `selected` in range.
    pub fn apply_view(&mut self, view: &ConfigGetResponse) {
        let config = serde_json::to_value(&view.config).expect("settings config is serializable");
        let fields = SETTING_DESCRIPTORS
            .iter()
            .map(|descriptor| {
                let mut raw = Some(&config);
                for segment in descriptor.key.split('.') {
                    raw = raw.and_then(|value| value.get(segment));
                }
                Self::field_from_descriptor(descriptor, raw, &view.options)
            })
            .collect::<Vec<_>>();

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
        self.selected = self
            .fields
            .iter()
            .position(|field| setting_descriptor(field.key()).section == self.active_section)
            .unwrap_or(0);
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

    fn section_indices(&self) -> Vec<usize> {
        self.fields
            .iter()
            .enumerate()
            .filter_map(|(index, field)| {
                (setting_descriptor(field.key()).section == self.active_section).then_some(index)
            })
            .collect()
    }

    fn move_selection(&mut self, delta: isize) {
        self.commit_edit();
        let mut rows = self.section_indices();
        rows.push(self.fields.len());
        let current = rows
            .iter()
            .position(|index| *index == self.selected)
            .unwrap_or(0) as isize;
        let next = (current + delta).rem_euclid(rows.len() as isize);
        self.selected = rows[next as usize];
        self.selection_anchor = 0;
        self.scroll_selected_into_view();
    }

    fn move_section(&mut self, delta: isize) {
        self.commit_edit();
        self.active_section = self.active_section.adjacent(delta);
        self.selected = self
            .fields
            .iter()
            .position(|field| setting_descriptor(field.key()).section == self.active_section)
            .unwrap_or(self.fields.len());
        self.selection_anchor = 0;
        self.scroll_selected_into_view();
    }

    fn scroll_selected_into_view(&self) {
        let index = self
            .section_indices()
            .iter()
            .position(|field_index| *field_index == self.selected)
            .unwrap_or_else(|| self.section_indices().len().saturating_sub(1));
        self.scroll_handle
            .scroll_to_item(index, ScrollStrategy::Center);
    }

    pub fn select_section(&mut self, section: SettingsSection) {
        if self.busy {
            return;
        }
        self.commit_edit();
        self.active_section = section;
        self.selected = self
            .fields
            .iter()
            .position(|field| setting_descriptor(field.key()).section == section)
            .unwrap_or(self.fields.len());
        self.selection_anchor = 0;
        self.scroll_selected_into_view();
    }

    pub fn select_row(&mut self, index: usize) {
        if self.busy {
            return;
        }
        self.commit_edit();
        self.selected = index.min(self.fields.len());
        if let Some(field) = self.fields.get(self.selected) {
            self.active_section = setting_descriptor(field.key()).section;
        }
        self.selection_anchor = 0;
        if self.selected == self.fields.len() {
            self.scroll_selected_into_view();
            return;
        }
        let Some(field) = self.fields.get_mut(self.selected) else {
            return;
        };
        if let Some((value, cursor)) = field.text_state_mut() {
            self.editing = true;
            *cursor = value.len();
            self.selection_anchor = *cursor;
            self.edit_snapshot = Some((value.clone(), *cursor, *cursor));
        } else if matches!(field, SettingField::Choice { .. }) {
            self.cycle_choice(1);
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
        let Some(field) = self.fields.get_mut(self.selected) else {
            return false;
        };
        let Some((value, cursor)) = field.text_state_mut() else {
            return false;
        };
        move_horizontal(value, cursor, &mut self.selection_anchor, delta, selecting);
        true
    }

    /// Non-printable keys; printable characters route through `push_char`.
    pub fn handle_key(&mut self, key: &str) -> Option<SettingsAction> {
        if self.busy {
            return None;
        }
        match key {
            "pageup" => {
                self.move_section(-1);
                None
            }
            "pagedown" => {
                self.move_section(1);
                None
            }
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
                    if let Some(field) = self.fields.get_mut(self.selected) {
                        if let Some((value, cursor)) = field.text_state_mut() {
                            move_to_edge(
                                value,
                                cursor,
                                &mut self.selection_anchor,
                                key == "end",
                                false,
                            );
                        }
                    }
                }
                None
            }
            "shift-home" | "shift-end" => {
                if self.editing {
                    if let Some(field) = self.fields.get_mut(self.selected) {
                        if let Some((value, cursor)) = field.text_state_mut() {
                            move_to_edge(
                                value,
                                cursor,
                                &mut self.selection_anchor,
                                key == "shift-end",
                                true,
                            );
                        }
                    }
                }
                None
            }
            "enter" => {
                if self.selected == self.fields.len() {
                    if self.dirty_count() == 0 {
                        return None;
                    }
                    if let Some(error) = self.validation_error() {
                        self.error = Some(error);
                        return None;
                    }
                    self.busy = true;
                    return Some(SettingsAction::Save);
                }
                let Some(field) = self.fields.get_mut(self.selected) else {
                    return None;
                };
                if let Some((value, cursor)) = field.text_state_mut() {
                    if self.editing {
                        self.commit_edit();
                    } else {
                        self.editing = true;
                        *cursor = value.len();
                        self.selection_anchor = *cursor;
                        self.edit_snapshot = Some((value.clone(), *cursor, *cursor));
                    }
                    None
                } else if matches!(field, SettingField::Choice { .. }) {
                    self.commit_edit();
                    self.cycle_choice(1);
                    None
                } else {
                    None
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
            if let Some(field) = self.fields.get_mut(self.selected) {
                if let Some((value, current)) = field.text_state_mut() {
                    *value = snapshot;
                    *current = cursor;
                    self.selection_anchor = anchor;
                }
            }
        }
    }

    pub fn text_selection(&self) -> Option<(String, usize, usize)> {
        let field = self.fields.get(self.selected)?;
        let (value, cursor) = match field {
            SettingField::Text { value, cursor, .. }
            | SettingField::Number { value, cursor, .. } => (value, cursor),
            SettingField::Choice { .. } => return None,
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
        if let Some(field) = self.fields.get_mut(self.selected) {
            if let Some((value, cursor)) = field.text_state_mut() {
                insert_text(value, cursor, &mut self.selection_anchor, text);
            }
        }
    }

    pub fn backspace(&mut self) {
        if self.busy || !self.editing {
            return;
        }
        if let Some(field) = self.fields.get_mut(self.selected) {
            if let Some((value, cursor)) = field.text_state_mut() {
                text_backspace(value, cursor, &mut self.selection_anchor);
            }
        }
    }

    pub fn delete(&mut self) {
        if self.busy || !self.editing {
            return;
        }
        if let Some(field) = self.fields.get_mut(self.selected) {
            if let Some((value, cursor)) = field.text_state_mut() {
                text_delete(value, cursor, &mut self.selection_anchor);
            }
        }
    }

    pub fn move_cursor_to(&mut self, offset: usize, selecting: bool) {
        if !self.editing {
            return;
        }
        if let Some(field) = self.fields.get_mut(self.selected) {
            if let Some((value, cursor)) = field.text_state_mut() {
                text_move_to(value, cursor, &mut self.selection_anchor, offset, selecting);
            }
        }
    }

    pub fn select_all_text(&mut self) {
        if !self.editing {
            return;
        }
        if let Some(field) = self.fields.get_mut(self.selected) {
            if let Some((value, cursor)) = field.text_state_mut() {
                select_all(value, cursor, &mut self.selection_anchor);
            }
        }
    }

    pub fn selected_text(&self) -> Option<String> {
        self.editing.then(|| {
            let field = &self.fields[self.selected];
            let (value, cursor) = match field {
                SettingField::Text { value, cursor, .. }
                | SettingField::Number { value, cursor, .. } => (value, cursor),
                SettingField::Choice { .. } => return String::new(),
            };
            selected_text(value, *cursor, self.selection_anchor)
        })
    }

    fn is_dirty(&self, field: &SettingField) -> bool {
        field.display() != self.originals.get(field.key()).cloned().unwrap_or_default()
    }

    pub fn validation_error(&self) -> Option<String> {
        for field in &self.fields {
            if !self.is_dirty(field) {
                continue;
            }
            match field {
                SettingField::Text {
                    key,
                    value,
                    required,
                    ..
                } if *required && value.trim().is_empty() => {
                    return Some(format!("{key} must be a non-empty string"));
                }
                SettingField::Number { .. } => {
                    if let Err(error) = field.wire_value() {
                        return Some(error);
                    }
                }
                _ => {}
            }
        }
        None
    }

    /// Dotted keys whose committed value differs from the loaded snapshot.
    pub fn dirty_updates(&self) -> serde_json::Value {
        let mut groups: HashMap<String, serde_json::Map<String, serde_json::Value>> =
            HashMap::new();
        for field in &self.fields {
            if !self.is_dirty(field) {
                continue;
            }
            let Ok(value) = field.wire_value() else {
                continue;
            };
            let (section, key) = field.key().split_once('.').unwrap_or((field.key(), ""));
            groups
                .entry(section.to_string())
                .or_default()
                .insert(key.to_string(), value);
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
            .filter(|field| self.is_dirty(field))
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

    fn render_navigation(
        &self,
        theme: &HermesTheme,
        motion: ModalMotion<'_>,
        on_action: SettingsHandler,
    ) -> Div {
        let mut nav = div()
            .debug_selector(|| "settings_navigation".to_string())
            .w(px(180.0))
            .min_w(px(150.0))
            .flex()
            .flex_col()
            .gap(px(4.0))
            .p(px(12.0))
            .bg(theme.mockup_sidebar)
            .child(
                div()
                    .px(px(8.0))
                    .py(px(6.0))
                    .text_size(px(MockupTypeScale::FEATURE_LABEL_SIZE))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.mockup_text)
                    .child("Settings"),
            );

        for section in SettingsSection::ALL {
            let selected = self.active_section == section;
            let count = self
                .fields
                .iter()
                .filter(|field| setting_descriptor(field.key()).section == section)
                .count();
            let handler =
                settings_click_handler(&on_action, SettingsInteraction::SelectSection(section));
            let mut row = div()
                .id(SharedString::from(format!(
                    "settings_section_{}",
                    section.id()
                )))
                .debug_selector(move || format!("settings_section_{}", section.id()))
                .h(px(40.0))
                .w_full()
                .flex()
                .items_center()
                .justify_between()
                .px(px(10.0))
                .rounded(px(7.0))
                .border_1()
                .border_color(if selected {
                    theme.accent_blue
                } else {
                    gpui::rgba(0x00000000)
                })
                .when(selected, |this| this.bg(theme.mockup_hover))
                .when(!selected && !self.busy, |this| {
                    this.hover(|this| this.bg(theme.mockup_hover))
                })
                .when(!self.busy, |this| {
                    this.cursor_pointer()
                        .on_click(move |event, window, cx| handler(event, window, cx))
                })
                .child(
                    div()
                        .text_size(px(MockupTypeScale::BASE_SIZE))
                        .text_color(if selected {
                            theme.mockup_text
                        } else {
                            theme.mockup_session_inactive
                        })
                        .child(section.title()),
                )
                .child(
                    div()
                        .font_family(theme.font_mono()[0].clone())
                        .text_size(px(MockupTypeScale::HINT_SIZE))
                        .text_color(theme.mockup_text_dim)
                        .child(count.to_string()),
                );
            if selected {
                row = modal_press(row, section.id(), motion);
            }
            nav = nav.child(row);
        }

        nav.child(
            div()
                .mt(px(12.0))
                .px(px(8.0))
                .flex()
                .flex_col()
                .gap(px(3.0))
                .text_size(px(MockupTypeScale::HINT_SIZE))
                .text_color(theme.mockup_text_dim)
                .child("CONFIG FILE")
                .child(
                    div()
                        .font_family(theme.font_mono()[0].clone())
                        .overflow_hidden()
                        .whitespace_nowrap()
                        .child(self.path.clone()),
                ),
        )
    }

    /// Renders the split settings shell.
    pub fn render(
        &self,
        theme: &HermesTheme,
        motion: ModalMotion<'_>,
        on_action: SettingsHandler,
    ) -> Div {
        let card = div()
            .debug_selector(|| "settings_content".to_string())
            .w(px(880.0))
            .max_w_full()
            .h(px(560.0))
            .flex()
            .flex_row()
            .child(self.render_navigation(theme, motion, on_action.clone()));
        let mut content = div()
            .flex_1()
            .min_w_0()
            .flex()
            .flex_col()
            .gap(px(10.0))
            .px(px(20.0))
            .py(px(18.0));

        content = content.child(
            div()
                .flex()
                .items_center()
                .justify_between()
                .child(
                    div()
                        .flex()
                        .flex_col()
                        .gap(px(3.0))
                        .child(
                            div()
                                .text_size(px(18.0))
                                .font_weight(FontWeight::SEMIBOLD)
                                .text_color(theme.mockup_text)
                                .child(self.active_section.title()),
                        )
                        .child(
                            div()
                                .text_size(px(MockupTypeScale::HINT_SIZE))
                                .text_color(theme.mockup_text_dim)
                                .child(self.active_section.description()),
                        ),
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
            content = content.child(
                div()
                    .text_size(px(MockupTypeScale::BASE_SIZE))
                    .text_color(theme.mockup_text)
                    .child("Settings could not be loaded."),
            );
            if let Some(error) = &self.error {
                content = content.child(
                    div()
                        .text_size(px(MockupTypeScale::HINT_SIZE))
                        .text_color(theme.rose_fg())
                        .child(error.clone()),
                );
            }
            return card.child(content);
        }

        if self.fields.is_empty() {
            content = content.child(
                div()
                    .flex_1()
                    .flex()
                    .items_center()
                    .justify_center()
                    .text_size(px(MockupTypeScale::BASE_SIZE))
                    .text_color(theme.mockup_text_dim)
                    .child("Loading settings…"),
            );
        }

        if !self.fields.is_empty() {
            let fields = self.fields.clone();
            let section_indices = self.section_indices();
            let originals = self.originals.clone();
            let selected = self.selected;
            let editing = self.editing;
            let selection_anchor = self.selection_anchor;
            let busy = self.busy;
            let scroll_handle = self.scroll_handle.clone();
            let theme = theme.clone();
            let on_action = on_action.clone();
            let list_id = format!("settings_rows_{}", self.active_section.id());
            let list = uniform_list(
                SharedString::from(list_id),
                section_indices.len(),
                move |range, _window, cx| {
                    let motion = ModalMotion {
                        owner: motion.owner,
                        cx,
                    };
                    range
                        .map(|row_index| {
                            let index = section_indices[row_index];
                            render_setting_row(
                                &fields[index],
                                index,
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
            content = content.child(
                div()
                    .debug_selector(|| "settings_list".to_string())
                    .flex_1()
                    .min_h_0()
                    .w_full()
                    .overflow_hidden()
                    .child(list),
            );
        }

        let dirty = self.dirty_count();
        let validation_error = self.validation_error();
        let save_selected = self.selected == self.fields.len();
        content = content.child(modal_press(
            div()
                .id("settings_save_row")
                .debug_selector(|| "settings_save_row".into())
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
                        .child(if validation_error.is_some() {
                            "Fix invalid values".to_string()
                        } else if dirty > 0 {
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
            content = content.child(
                div()
                    .text_size(px(MockupTypeScale::HINT_SIZE))
                    .text_color(theme.accent_blue)
                    .child(status.clone()),
            );
        }
        if let Some(error) = &self.error {
            content = content.child(
                div()
                    .text_size(px(MockupTypeScale::HINT_SIZE))
                    .text_color(theme.rose_fg())
                    .child(error.clone()),
            );
        }

        content = content.child(
            div()
                .flex()
                .flex_col()
                .gap(px(2.0))
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(MockupTypeScale::HINT_SIZE))
                .text_color(theme.mockup_text_dim)
                .child("↑↓ rows · PgUp/PgDn sections · Enter edit · ←→ adjust · Esc cancel")
                .child(format!("Workspace · {}", self.workspace))
                .child(format!("Data · {}", self.data_dir)),
        );

        card.child(content)
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
                div().text_color(theme.mockup_text_dim).child(if *required {
                    "required"
                } else {
                    "not set"
                })
            } else {
                div()
                    .text_color(theme.mockup_text_bright)
                    .child(value.clone())
            }
        }
        SettingField::Number {
            value,
            cursor,
            min,
            max,
            ..
        } => {
            if selected && editing {
                crate::ui::modals::text_input::input_content(
                    value,
                    *cursor,
                    selection_anchor,
                    &format!("{min}–{max}"),
                    false,
                    !busy,
                    theme,
                )
            } else if value.is_empty() {
                div()
                    .text_color(theme.mockup_text_dim)
                    .child(format!("{min}–{max}"))
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
        .h(px(76.0))
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
    row = row.child(
        div()
            .w_full()
            .text_size(px(MockupTypeScale::HINT_SIZE))
            .text_color(theme.mockup_text_dim)
            .child(setting_descriptor(field.key()).description),
    );
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
    fn test_split_navigation_scopes_rows_to_the_active_section() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());

        assert_eq!(modal.active_section, SettingsSection::Model);
        assert_eq!(modal.section_indices(), vec![0, 1, 2, 3, 4, 5]);
        modal.handle_key("down");
        assert_eq!(modal.selected, 1);

        modal.handle_key("pageup");
        assert_eq!(modal.active_section, SettingsSection::Learning);
        assert_eq!(modal.selected, 9);
        modal.handle_key("down");
        assert_eq!(modal.selected, modal.fields.len());
        modal.handle_key("down");
        assert_eq!(modal.selected, 9);

        modal.select_section(SettingsSection::Runtime);
        assert_eq!(modal.active_section, SettingsSection::Runtime);
        assert_eq!(modal.section_indices(), vec![6]);
        assert_eq!(modal.selected, 6);

        modal.select_row(0);
        assert_eq!(modal.active_section, SettingsSection::Model);
        assert_eq!(modal.selected, 0);
    }

    #[test]
    fn test_max_turns_serializes_as_an_integer() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());
        let max_turns_idx = modal
            .fields
            .iter()
            .position(|field| field.key() == "runtime.maxTurns")
            .unwrap();

        modal.select_row(max_turns_idx);
        modal.select_all_text();
        modal.push_char("120");
        modal.handle_key("enter");

        assert_eq!(modal.validation_error(), None);
        assert_eq!(
            modal.dirty_updates(),
            serde_json::json!({ "runtime": { "maxTurns": 120 } })
        );
        assert!(modal.dirty_updates()["runtime"]["maxTurns"].is_number());
    }

    #[test]
    fn test_invalid_max_turns_blocks_save() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());
        let max_turns_idx = modal
            .fields
            .iter()
            .position(|field| field.key() == "runtime.maxTurns")
            .unwrap();

        modal.select_row(max_turns_idx);
        modal.select_all_text();
        modal.push_char("0");
        modal.handle_key("enter");
        modal.selected = modal.fields.len();

        assert!(modal.validation_error().unwrap().contains("1 and 1000"));
        assert_eq!(modal.handle_key("enter"), None);
        assert_eq!(modal.dirty_updates(), serde_json::json!({}));
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

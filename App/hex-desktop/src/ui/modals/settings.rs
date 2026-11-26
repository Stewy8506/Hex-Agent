//! Editable settings surface driven by the ACP `config/get` / `config/set`
//! methods. The backend owns the whitelist and validation; desktop descriptors
//! organize those values into sections and send only changed keys.

use std::collections::{BTreeMap, HashMap};

use gpui::prelude::FluentBuilder;
use gpui::*;

use crate::preferences::DesktopPreferences;
use crate::shortcuts::{self, ShortcutId};
use crate::theme::{HermesTheme, InterfaceDensity, MockupTypeScale, ThemePreset};
use crate::ui::modals::text_input::{
    backspace as text_backspace, delete as text_delete, insert_text, move_horizontal,
    move_to as text_move_to, move_to_edge, select_all, selected_text,
};
use crate::ui::modals::{modal_press, ModalMotion};
use crate::ui::motion::MotionPreference;
use hex_protocol::models::ConfigGetResponse;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettingsSection {
    Model,
    Sessions,
    Cloud,
    Tools,
    Skills,
    Runtime,
    WebSearch,
    Learning,
    Appearance,
    MotionDisplay,
    Keyboard,
    Notifications,
}

impl SettingsSection {
    pub const ALL: [Self; 12] = [
        Self::Model,
        Self::Sessions,
        Self::Cloud,
        Self::Tools,
        Self::Skills,
        Self::Runtime,
        Self::WebSearch,
        Self::Learning,
        Self::Appearance,
        Self::MotionDisplay,
        Self::Keyboard,
        Self::Notifications,
    ];

    pub fn title(self) -> &'static str {
        match self {
            Self::Model => "Models",
            Self::Sessions => "Sessions",
            Self::Cloud => "Cloud",
            Self::Tools => "Tools",
            Self::Skills => "Skills",
            Self::Runtime => "Runtime",
            Self::WebSearch => "Web search",
            Self::Learning => "Learning",
            Self::Appearance => "Appearance",
            Self::MotionDisplay => "Motion & display",
            Self::Keyboard => "Keyboard",
            Self::Notifications => "Notifications",
        }
    }

    pub fn description(self) -> &'static str {
        match self {
            Self::Model => "Choose the provider, model, and execution behavior.",
            Self::Sessions => "Find, resume, rename, pin, archive, or delete sessions.",
            Self::Cloud => "Manage the HEX cloud account and enrolled devices.",
            Self::Tools => "Inspect the live tool registry exposed by the runtime.",
            Self::Skills => "Inspect skills registered with the active runtime session.",
            Self::Runtime => "Control how long an agent turn may run.",
            Self::WebSearch => "Configure the provider used for web lookups.",
            Self::Learning => "Choose when HEX reflects on completed work.",
            Self::Appearance => "Personalize the desktop theme and interface density.",
            Self::MotionDisplay => "Control animation policy and sidebar visibility.",
            Self::Keyboard => {
                "Rebind global shortcuts; native text editing stays protected. Esc cancels, Delete clears, and Cmd/Ctrl+R resets."
            }
            Self::Notifications => "Control in-app badges and update notices. No sound is played.",
        }
    }

    pub fn id(self) -> &'static str {
        match self {
            Self::Model => "models",
            Self::Sessions => "sessions",
            Self::Cloud => "cloud",
            Self::Tools => "tools",
            Self::Skills => "skills",
            Self::Runtime => "runtime",
            Self::WebSearch => "web_search",
            Self::Learning => "learning",
            Self::Appearance => "appearance",
            Self::MotionDisplay => "motion_display",
            Self::Keyboard => "keyboard",
            Self::Notifications => "notifications",
        }
    }

    pub fn is_local(self) -> bool {
        matches!(
            self,
            Self::Appearance | Self::MotionDisplay | Self::Keyboard | Self::Notifications
        )
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
    Action,
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct SettingDescriptor {
    section: SettingsSection,
    pub(crate) key: &'static str,
    pub(crate) label: &'static str,
    description: &'static str,
    kind: SettingKind,
}

const fn keybind_setting(id: ShortcutId) -> SettingDescriptor {
    SettingDescriptor {
        section: SettingsSection::Keyboard,
        key: id.setting_key(),
        label: id.label(),
        description: id.description(),
        kind: SettingKind::Choice { fallback: &[] },
    }
}

const fn notification_setting(
    key: &'static str,
    label: &'static str,
    description: &'static str,
) -> SettingDescriptor {
    SettingDescriptor {
        section: SettingsSection::Notifications,
        key,
        label,
        description,
        kind: SettingKind::Choice {
            fallback: &["enabled", "disabled"],
        },
    }
}

pub(crate) const SETTING_DESCRIPTORS: [SettingDescriptor; 28] = [
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
    SettingDescriptor {
        section: SettingsSection::Appearance,
        key: "desktop.theme",
        label: "Theme",
        description: "Color palette applied to this desktop app.",
        kind: SettingKind::Choice {
            fallback: &["hermes", "midnight", "mono"],
        },
    },
    SettingDescriptor {
        section: SettingsSection::Appearance,
        key: "desktop.density",
        label: "Interface density",
        description: "Spacing used by navigation, messages, and controls.",
        kind: SettingKind::Choice {
            fallback: &["comfortable", "compact"],
        },
    },
    SettingDescriptor {
        section: SettingsSection::MotionDisplay,
        key: "desktop.motion",
        label: "Motion",
        description: "Animation policy for spatial transitions and ambient effects.",
        kind: SettingKind::Choice {
            fallback: &["system", "reduced", "full"],
        },
    },
    SettingDescriptor {
        section: SettingsSection::MotionDisplay,
        key: "desktop.sidebar",
        label: "Sidebar",
        description: "Whether the navigation sidebar is expanded.",
        kind: SettingKind::Choice {
            fallback: &["expanded", "collapsed"],
        },
    },
    keybind_setting(ShortcutId::CommandPalette),
    keybind_setting(ShortcutId::NewChat),
    keybind_setting(ShortcutId::SubmitPrompt),
    keybind_setting(ShortcutId::ToggleSidebar),
    keybind_setting(ShortcutId::OpenSettings),
    keybind_setting(ShortcutId::CloseWindow),
    keybind_setting(ShortcutId::Quit),
    keybind_setting(ShortcutId::Hide),
    notification_setting(
        "desktop.notifications.approval_badges",
        "Approval badges",
        "Show pending approval counts on the header bell and in the sidebar.",
    ),
    notification_setting(
        "desktop.notifications.update_badges",
        "Update badges",
        "Include available updates in the header notification badge.",
    ),
    notification_setting(
        "desktop.notifications.update_banners",
        "Update banners",
        "Show optional update banners above the conversation. Required updates stay visible.",
    ),
    SettingDescriptor {
        section: SettingsSection::Model,
        key: "desktop.action.models",
        label: "Choose a model",
        description: "Open the live model catalog for the current session.",
        kind: SettingKind::Action,
    },
    SettingDescriptor {
        section: SettingsSection::Sessions,
        key: "desktop.action.sessions",
        label: "Manage sessions",
        description: "Search, resume, rename, pin, archive, or delete existing sessions.",
        kind: SettingKind::Action,
    },
    SettingDescriptor {
        section: SettingsSection::Cloud,
        key: "desktop.action.cloud",
        label: "Account & devices",
        description: "Open HEX cloud sign-in, account status, and enrolled devices.",
        kind: SettingKind::Action,
    },
];

impl SettingDescriptor {
    pub(crate) fn palette_subtitle(self) -> String {
        format!(
            "Open {} setting · {}",
            self.section.title(),
            self.description
        )
    }
}

fn setting_descriptor(key: &str) -> &'static SettingDescriptor {
    SETTING_DESCRIPTORS
        .iter()
        .find(|descriptor| descriptor.key == key)
        .expect("every setting field has a descriptor")
}

fn is_desktop_setting(key: &str) -> bool {
    key.starts_with("desktop.")
}

fn local_setting_fields(preferences: &DesktopPreferences) -> Vec<SettingField> {
    let theme_options: Vec<String> = ThemePreset::ALL
        .iter()
        .map(|preset| preset.id().to_string())
        .collect();
    let density_options: Vec<String> = InterfaceDensity::ALL
        .iter()
        .map(|density| density.id().to_string())
        .collect();
    let motion_options: Vec<String> = MotionPreference::ALL
        .iter()
        .map(|motion| motion.id().to_string())
        .collect();
    let sidebar_options = vec!["expanded".to_string(), "collapsed".to_string()];
    let notification_options = vec!["enabled".to_string(), "disabled".to_string()];
    let mut fields = Vec::new();
    for descriptor in SETTING_DESCRIPTORS
        .iter()
        .filter(|descriptor| is_desktop_setting(descriptor.key))
    {
        if matches!(descriptor.kind, SettingKind::Action) {
            fields.push(SettingField::Action {
                key: descriptor.key,
                label: descriptor.label,
                value: "Open".to_string(),
            });
            continue;
        }
        if let Some(id) = ShortcutId::from_setting_key(descriptor.key) {
            if id.available() {
                let value = shortcuts::effective_keystroke(preferences, id);
                fields.push(SettingField::Keybind {
                    key: descriptor.key,
                    label: descriptor.label,
                    value,
                });
            }
            continue;
        }
        fields.push(SettingField::Choice {
            key: descriptor.key,
            label: descriptor.label,
            options: match descriptor.key {
                "desktop.theme" => theme_options.clone(),
                "desktop.density" => density_options.clone(),
                "desktop.motion" => motion_options.clone(),
                "desktop.sidebar" => sidebar_options.clone(),
                "desktop.notifications.approval_badges"
                | "desktop.notifications.update_badges"
                | "desktop.notifications.update_banners" => notification_options.clone(),
                _ => unreachable!("unknown desktop setting"),
            },
            value: Some(match descriptor.key {
                "desktop.theme" => preferences.theme.id().to_string(),
                "desktop.density" => preferences.density.id().to_string(),
                "desktop.motion" => preferences.motion.id().to_string(),
                "desktop.sidebar" => {
                    if preferences.sidebar_collapsed {
                        "collapsed".to_string()
                    } else {
                        "expanded".to_string()
                    }
                }
                "desktop.notifications.approval_badges" => {
                    if preferences.approval_badges {
                        "enabled".to_string()
                    } else {
                        "disabled".to_string()
                    }
                }
                "desktop.notifications.update_badges" => {
                    if preferences.update_badges {
                        "enabled".to_string()
                    } else {
                        "disabled".to_string()
                    }
                }
                "desktop.notifications.update_banners" => {
                    if preferences.update_banners {
                        "enabled".to_string()
                    } else {
                        "disabled".to_string()
                    }
                }
                _ => unreachable!("unknown desktop setting"),
            }),
        });
    }
    fields
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
    /// Global shortcut; None means explicitly unbound.
    Keybind {
        key: &'static str,
        label: &'static str,
        value: Option<String>,
    },
    /// Opens an existing desktop workflow without changing a setting.
    Action {
        key: &'static str,
        label: &'static str,
        value: String,
    },
}

impl SettingField {
    pub fn key(&self) -> &'static str {
        match self {
            Self::Text { key, .. }
            | Self::Number { key, .. }
            | Self::Choice { key, .. }
            | Self::Keybind { key, .. }
            | Self::Action { key, .. } => key,
        }
    }

    pub fn label(&self) -> &'static str {
        match self {
            Self::Text { label, .. }
            | Self::Number { label, .. }
            | Self::Choice { label, .. }
            | Self::Keybind { label, .. }
            | Self::Action { label, .. } => label,
        }
    }

    fn is_action(&self) -> bool {
        matches!(self, Self::Action { .. })
    }

    fn display(&self) -> String {
        match self {
            Self::Text { value, .. } | Self::Number { value, .. } => value.clone(),
            Self::Choice { value, .. } => value.clone().unwrap_or_default(),
            Self::Keybind { value, .. } => value
                .as_deref()
                .map(shortcuts::display_keystroke)
                .unwrap_or_else(|| "Unbound".to_string()),
            Self::Action { value, .. } => value.clone(),
        }
    }

    fn text_state_mut(&mut self) -> Option<(&mut String, &mut usize)> {
        match self {
            Self::Text { value, cursor, .. } | Self::Number { value, cursor, .. } => {
                Some((value, cursor))
            }
            Self::Choice { .. } | Self::Keybind { .. } | Self::Action { .. } => None,
        }
    }

    fn wire_value(&self) -> Result<serde_json::Value, String> {
        match self {
            Self::Text { value, .. } => Ok(serde_json::Value::String(value.clone())),
            Self::Choice { value, .. } | Self::Keybind { value, .. } => Ok(
                serde_json::Value::String(value.as_deref().unwrap_or_default().to_string()),
            ),
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
            Self::Action { .. } => Err("actions cannot be saved as settings".to_string()),
        }
    }
}

/// What the operator confirmed; executed by `ChatView` against the runtime.
#[derive(Debug, Clone, PartialEq)]
pub enum SettingsAction {
    Save,
    Activate(&'static str),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettingsInteraction {
    SelectRow(usize),
    SelectSection(SettingsSection),
    FocusSearch,
    RevealSetting(&'static str),
    Save,
    Activate(&'static str),
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
    pub desktop_preferences: DesktopPreferences,
    desktop_originals: DesktopPreferences,
    pub active_section: SettingsSection,
    pub search_query: String,
    search_cursor: usize,
    search_anchor: usize,
    pub searching: bool,
    search_snapshot: Option<(String, usize, usize)>,
    pending_setting: Option<String>,
    pub selected: usize, // index into fields; fields.len() == Save row
    pub editing: bool,
    capturing_keybind: Option<&'static str>,
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
            && self.desktop_preferences == other.desktop_preferences
            && self.desktop_originals == other.desktop_originals
            && self.active_section == other.active_section
            && self.search_query == other.search_query
            && self.search_cursor == other.search_cursor
            && self.search_anchor == other.search_anchor
            && self.searching == other.searching
            && self.search_snapshot == other.search_snapshot
            && self.pending_setting == other.pending_setting
            && self.selected == other.selected
            && self.editing == other.editing
            && self.capturing_keybind == other.capturing_keybind
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
        let desktop_preferences = DesktopPreferences::default();
        let fields = local_setting_fields(&desktop_preferences);
        let selected = fields.len();
        let originals = fields
            .iter()
            .map(|field| (field.key().to_string(), field.display()))
            .collect();
        Self {
            path: String::new(),
            workspace: String::new(),
            data_dir: String::new(),
            fields,
            options: serde_json::Value::Null,
            originals,
            desktop_preferences: desktop_preferences.clone(),
            desktop_originals: desktop_preferences,
            active_section: SettingsSection::Model,
            search_query: String::new(),
            search_cursor: 0,
            search_anchor: 0,
            searching: false,
            search_snapshot: None,
            pending_setting: None,
            selected,
            editing: false,
            capturing_keybind: None,
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

    pub fn loading_with_preferences(preferences: DesktopPreferences) -> Self {
        let mut modal = Self::default();
        modal.fields = local_setting_fields(&preferences);
        modal.originals = modal
            .fields
            .iter()
            .map(|field| (field.key().to_string(), field.display()))
            .collect();
        modal.desktop_preferences = preferences.clone();
        modal.desktop_originals = preferences;
        modal.selected = modal.fields.len();
        modal
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
            SettingKind::Action => SettingField::Action {
                key: descriptor.key,
                label: descriptor.label,
                value: "Open".to_string(),
            },
        }
    }

    /// Rebuild the field list from a backend view; keeps `selected` in range.
    pub fn apply_view(&mut self, view: &ConfigGetResponse) {
        let config = serde_json::to_value(&view.config).expect("settings config is serializable");
        let mut fields = SETTING_DESCRIPTORS
            .iter()
            .filter(|descriptor| !is_desktop_setting(descriptor.key))
            .map(|descriptor| {
                let mut raw = Some(&config);
                for segment in descriptor.key.split('.') {
                    raw = raw.and_then(|value| value.get(segment));
                }
                Self::field_from_descriptor(descriptor, raw, &view.options)
            })
            .collect::<Vec<_>>();
        fields.extend(
            self.fields
                .iter()
                .filter(|field| is_desktop_setting(field.key()))
                .cloned(),
        );

        let mut originals = HashMap::new();
        for field in &fields {
            let original = if is_desktop_setting(field.key()) {
                self.originals
                    .get(field.key())
                    .cloned()
                    .unwrap_or_else(|| field.display())
            } else {
                field.display()
            };
            originals.insert(field.key().to_string(), original);
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
        self.capturing_keybind = None;
        self.selection_anchor = 0;
        self.edit_snapshot = None;
        self.load_failed = false;
        self.error = None;
        self.scroll_handle
            .scroll_to_item_strict(0, ScrollStrategy::Top);
        if let Some(key) = self.pending_setting.take() {
            self.reveal_setting(&key);
        } else if self.searching {
            self.sync_search_selection();
        }
    }

    pub fn mark_load_failed(&mut self, message: impl Into<String>) {
        self.load_failed = true;
        self.error = Some(message.into());
    }

    pub fn accepts_text_edit(&self) -> bool {
        !self.busy && (self.searching || self.editing)
    }

    pub fn capturing_keybind(&self) -> Option<&'static str> {
        self.capturing_keybind
    }

    pub fn begin_keybind_capture(&mut self, key: &'static str) {
        if self.busy
            || !self
                .fields
                .iter()
                .any(|field| field.key() == key && matches!(field, SettingField::Keybind { .. }))
        {
            return;
        }
        self.commit_edit();
        self.clear_search();
        self.active_section = SettingsSection::Keyboard;
        self.selected = self
            .fields
            .iter()
            .position(|field| field.key() == key)
            .unwrap_or(self.fields.len());
        self.selection_anchor = 0;
        self.capturing_keybind = Some(key);
        self.error = None;
        self.scroll_selected_into_view();
    }

    pub fn capture_keystroke(&mut self, source: &str) {
        if self.busy || shortcuts::is_modifier_only(source) {
            return;
        }
        let Some(key) = self.capturing_keybind.take() else {
            return;
        };
        if source == "escape" {
            return;
        }
        let value = if matches!(source, "backspace" | "delete") {
            None
        } else if shortcuts::canonical_keystroke(source).as_deref()
            == shortcuts::canonical_keystroke("secondary-r").as_deref()
        {
            ShortcutId::from_setting_key(key).map(|id| id.default_keystroke().to_string())
        } else {
            Some(shortcuts::canonical_keystroke(source).unwrap_or_else(|| source.to_string()))
        };
        if let Some(SettingField::Keybind { value: current, .. }) =
            self.fields.iter_mut().find(|field| field.key() == key)
        {
            *current = value;
        }
        self.error = None;
    }

    fn keybinding_values(&self) -> BTreeMap<String, String> {
        self.fields
            .iter()
            .filter_map(|field| {
                let SettingField::Keybind { key, value, .. } = field else {
                    return None;
                };
                ShortcutId::from_setting_key(key)
                    .map(|id| (id.id().to_string(), value.clone().unwrap_or_default()))
            })
            .collect()
    }

    fn search_results(&self) -> Vec<usize> {
        let query = self.search_query.trim().to_lowercase();
        if query.is_empty() {
            return Vec::new();
        }
        let mut scored = self
            .fields
            .iter()
            .enumerate()
            .filter_map(|(index, field)| {
                let descriptor = setting_descriptor(field.key());
                let key = descriptor.key.to_lowercase();
                let label = descriptor.label.to_lowercase();
                let score = if key == query || label == query {
                    Some(0)
                } else if key.starts_with(&query) || label.starts_with(&query) {
                    Some(1)
                } else if key.contains(&query)
                    || label.contains(&query)
                    || field.display().to_lowercase().contains(&query)
                    || descriptor.description.to_lowercase().contains(&query)
                    || descriptor.section.title().to_lowercase().contains(&query)
                {
                    Some(2)
                } else {
                    None
                };
                score.map(|score| (score, index))
            })
            .collect::<Vec<_>>();
        scored.sort_by_key(|(score, index)| (*score, *index));
        scored.into_iter().map(|(_, index)| index).collect()
    }

    fn visible_indices(&self) -> Vec<usize> {
        if self.search_query.trim().is_empty() {
            self.section_indices()
        } else {
            self.search_results()
        }
    }

    fn sync_search_selection(&mut self) {
        if self.search_query.trim().is_empty() {
            return;
        }
        let visible = self.search_results();
        if !visible.contains(&self.selected) {
            self.selected = visible.first().copied().unwrap_or(self.fields.len());
        }
        self.scroll_selected_into_view();
    }

    pub fn focus_search(&mut self) {
        if self.busy {
            return;
        }
        self.commit_edit();
        if !self.searching {
            self.search_snapshot = Some((
                self.search_query.clone(),
                self.search_cursor,
                self.search_anchor,
            ));
        }
        self.searching = true;
        self.search_cursor = self.search_query.len();
        self.search_anchor = self.search_cursor;
    }

    pub fn cancel_search(&mut self) {
        if let Some((query, cursor, anchor)) = self.search_snapshot.take() {
            self.search_query = query;
            self.search_cursor = cursor;
            self.search_anchor = anchor;
        }
        self.searching = false;
        self.sync_search_selection();
    }

    fn clear_search(&mut self) {
        self.search_query.clear();
        self.search_cursor = 0;
        self.search_anchor = 0;
        self.searching = false;
        self.search_snapshot = None;
    }

    pub fn request_setting(&mut self, key: &str) -> bool {
        if !SETTING_DESCRIPTORS
            .iter()
            .any(|descriptor| descriptor.key == key)
        {
            return false;
        }
        self.pending_setting = Some(key.to_string());
        if self.fields.iter().any(|field| field.key() == key) {
            self.reveal_setting(key)
        } else {
            true
        }
    }

    pub fn reveal_setting(&mut self, key: &str) -> bool {
        let Some(index) = self.fields.iter().position(|field| field.key() == key) else {
            return false;
        };
        self.commit_edit();
        self.clear_search();
        self.pending_setting = None;
        self.active_section = setting_descriptor(key).section;
        self.selected = index;
        self.selection_anchor = 0;
        let field_key = self.fields[index].key();
        if matches!(self.fields.get(index), Some(SettingField::Keybind { .. })) {
            self.begin_keybind_capture(field_key);
            return true;
        }
        let Some(field) = self.fields.get_mut(index) else {
            return false;
        };
        if let Some((value, cursor)) = field.text_state_mut() {
            self.editing = true;
            *cursor = value.len();
            self.selection_anchor = *cursor;
            self.edit_snapshot = Some((value.clone(), *cursor, *cursor));
        }
        self.scroll_selected_into_view();
        true
    }

    fn active_text_state_mut(&mut self) -> Option<(&mut String, &mut usize, &mut usize)> {
        if self.searching {
            Some((
                &mut self.search_query,
                &mut self.search_cursor,
                &mut self.search_anchor,
            ))
        } else {
            let field = self.fields.get_mut(self.selected)?;
            field
                .text_state_mut()
                .map(|(value, cursor)| (value, cursor, &mut self.selection_anchor))
        }
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
        let mut rows = self.visible_indices();
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
        self.clear_search();
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
        let visible = self.visible_indices();
        let index = visible
            .iter()
            .position(|field_index| *field_index == self.selected)
            .unwrap_or_else(|| visible.len().saturating_sub(1));
        self.scroll_handle
            .scroll_to_item(index, ScrollStrategy::Center);
    }

    pub fn select_section(&mut self, section: SettingsSection) {
        if self.busy {
            return;
        }
        self.commit_edit();
        self.clear_search();
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
        self.clear_search();
        self.selected = index.min(self.fields.len());
        if let Some(field) = self.fields.get(self.selected) {
            self.active_section = setting_descriptor(field.key()).section;
        }
        self.selection_anchor = 0;
        if self.selected == self.fields.len() {
            self.scroll_selected_into_view();
            return;
        }
        let keybind_key = self
            .fields
            .get(self.selected)
            .and_then(|field| match field {
                SettingField::Keybind { key, .. } => Some(*key),
                _ => None,
            });
        if let Some(key) = keybind_key {
            self.begin_keybind_capture(key);
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

    pub fn selected_action(&self) -> Option<&'static str> {
        match self.fields.get(self.selected) {
            Some(SettingField::Action { key, .. }) => Some(*key),
            _ => None,
        }
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
        self.capturing_keybind = None;
        self.edit_snapshot = None;
    }

    fn move_text_cursor(&mut self, delta: isize, selecting: bool) -> bool {
        let Some((value, cursor, anchor)) = self.active_text_state_mut() else {
            return false;
        };
        move_horizontal(value, cursor, anchor, delta, selecting);
        true
    }

    /// Non-printable keys; printable characters route through `push_char`.
    pub fn handle_key(&mut self, key: &str) -> Option<SettingsAction> {
        if self.busy {
            return None;
        }
        if self.capturing_keybind.is_some() {
            self.capture_keystroke(key);
            return None;
        }
        match key {
            "/" if !self.searching && !self.editing => {
                self.focus_search();
                None
            }
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
                if !((self.searching || self.editing) && self.move_text_cursor(-1, false))
                    && self.cycle_choice(-1)
                {
                    self.commit_edit();
                }
                None
            }
            "right" | "arrowright" => {
                if !((self.searching || self.editing) && self.move_text_cursor(1, false))
                    && self.cycle_choice(1)
                {
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
                if self.searching || self.editing {
                    if let Some((value, cursor, anchor)) = self.active_text_state_mut() {
                        move_to_edge(value, cursor, anchor, key == "end", false);
                    }
                }
                None
            }
            "shift-home" | "shift-end" => {
                if self.searching || self.editing {
                    if let Some((value, cursor, anchor)) = self.active_text_state_mut() {
                        move_to_edge(value, cursor, anchor, key == "shift-end", true);
                    }
                }
                None
            }
            "enter" => {
                if self.searching {
                    if !self.search_query.trim().is_empty() {
                        if let Some(field) = self.fields.get(self.selected) {
                            let key = field.key();
                            if matches!(field, SettingField::Action { .. }) {
                                return Some(SettingsAction::Activate(key));
                            }
                            self.reveal_setting(key);
                        }
                        return None;
                    }
                    self.cancel_search();
                }
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
                if let Some(SettingField::Action { key, .. }) = self.fields.get(self.selected) {
                    return Some(SettingsAction::Activate(*key));
                }
                let keybind_key = self
                    .fields
                    .get(self.selected)
                    .and_then(|field| match field {
                        SettingField::Keybind { key, .. } => Some(*key),
                        _ => None,
                    });
                if let Some(key) = keybind_key {
                    self.begin_keybind_capture(key);
                    return None;
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
        if self.searching {
            return Some((
                self.search_query.clone(),
                self.search_cursor,
                self.search_anchor,
            ));
        }
        if !self.editing {
            return None;
        }
        let field = self.fields.get(self.selected)?;
        let (value, cursor) = match field {
            SettingField::Text { value, cursor, .. }
            | SettingField::Number { value, cursor, .. } => (value, cursor),
            SettingField::Choice { .. } | SettingField::Keybind { .. } | SettingField::Action { .. } => {
                return None;
            }
        };
        Some((value.clone(), *cursor, self.selection_anchor))
    }

    pub fn replace_text_range(&mut self, range: std::ops::Range<usize>, text: &str) -> bool {
        if !self.accepts_text_edit() {
            return false;
        }
        self.move_cursor_to(range.start, false);
        self.move_cursor_to(range.end, true);
        self.push_char(text);
        true
    }

    pub fn select_text_range(&mut self, range: std::ops::Range<usize>) {
        if !self.accepts_text_edit() {
            return;
        }
        self.move_cursor_to(range.start, false);
        self.move_cursor_to(range.end, true);
    }

    pub fn push_char(&mut self, text: &str) {
        if !self.accepts_text_edit() {
            return;
        }
        if let Some((value, cursor, anchor)) = self.active_text_state_mut() {
            insert_text(value, cursor, anchor, text);
        }
        self.sync_search_selection();
    }

    pub fn backspace(&mut self) {
        if !self.accepts_text_edit() {
            return;
        }
        if let Some((value, cursor, anchor)) = self.active_text_state_mut() {
            text_backspace(value, cursor, anchor);
        }
        self.sync_search_selection();
    }

    pub fn delete(&mut self) {
        if !self.accepts_text_edit() {
            return;
        }
        if let Some((value, cursor, anchor)) = self.active_text_state_mut() {
            text_delete(value, cursor, anchor);
        }
        self.sync_search_selection();
    }

    pub fn move_cursor_to(&mut self, offset: usize, selecting: bool) {
        if !self.accepts_text_edit() {
            return;
        }
        if let Some((value, cursor, anchor)) = self.active_text_state_mut() {
            text_move_to(value, cursor, anchor, offset, selecting);
        }
    }

    pub fn select_all_text(&mut self) {
        if !self.accepts_text_edit() {
            return;
        }
        if let Some((value, cursor, anchor)) = self.active_text_state_mut() {
            select_all(value, cursor, anchor);
        }
    }

    pub fn selected_text(&self) -> Option<String> {
        let (value, cursor, anchor) = self.text_selection()?;
        Some(selected_text(&value, cursor, anchor))
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
                SettingField::Keybind {
                    key,
                    value: Some(source),
                    ..
                } => {
                    if let Some(id) = ShortcutId::from_setting_key(key) {
                        if let Err(error) =
                            shortcuts::validate_keybinding(id, source, &self.keybinding_values())
                        {
                            return Some(error);
                        }
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
            if is_desktop_setting(field.key()) || !self.is_dirty(field) {
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

    pub fn desktop_dirty_count(&self) -> usize {
        self.fields
            .iter()
            .filter(|field| is_desktop_setting(field.key()) && self.is_dirty(field))
            .count()
    }

    pub fn changed_desktop_preferences(&self) -> Option<DesktopPreferences> {
        if self.desktop_dirty_count() == 0 {
            return None;
        }
        let mut preferences = self.desktop_preferences.clone();
        for field in &self.fields {
            match field {
                SettingField::Choice { key, value, .. } => match (*key, value.as_deref()) {
                    ("desktop.theme", Some("hermes")) => preferences.theme = ThemePreset::Hermes,
                    ("desktop.theme", Some("midnight")) => {
                        preferences.theme = ThemePreset::Midnight
                    }
                    ("desktop.theme", Some("mono")) => preferences.theme = ThemePreset::Mono,
                    ("desktop.density", Some("comfortable")) => {
                        preferences.density = InterfaceDensity::Comfortable
                    }
                    ("desktop.density", Some("compact")) => {
                        preferences.density = InterfaceDensity::Compact
                    }
                    ("desktop.motion", Some("system")) => {
                        preferences.motion = MotionPreference::System
                    }
                    ("desktop.motion", Some("reduced")) => {
                        preferences.motion = MotionPreference::Reduced
                    }
                    ("desktop.motion", Some("full")) => preferences.motion = MotionPreference::Full,
                    ("desktop.sidebar", Some("expanded")) => preferences.sidebar_collapsed = false,
                    ("desktop.sidebar", Some("collapsed")) => preferences.sidebar_collapsed = true,
                    ("desktop.notifications.approval_badges", Some("enabled")) => {
                        preferences.approval_badges = true
                    }
                    ("desktop.notifications.approval_badges", Some("disabled")) => {
                        preferences.approval_badges = false
                    }
                    ("desktop.notifications.update_badges", Some("enabled")) => {
                        preferences.update_badges = true
                    }
                    ("desktop.notifications.update_badges", Some("disabled")) => {
                        preferences.update_badges = false
                    }
                    ("desktop.notifications.update_banners", Some("enabled")) => {
                        preferences.update_banners = true
                    }
                    ("desktop.notifications.update_banners", Some("disabled")) => {
                        preferences.update_banners = false
                    }
                    _ => {}
                },
                SettingField::Keybind {
                    key,
                    value: Some(value),
                    ..
                } => {
                    if let Some(id) = ShortcutId::from_setting_key(key) {
                        if shortcuts::canonical_keystroke(value).as_deref()
                            == shortcuts::canonical_keystroke(id.default_keystroke()).as_deref()
                        {
                            preferences.keybindings.remove(id.id());
                        } else {
                            preferences
                                .keybindings
                                .insert(id.id().to_string(), value.clone());
                        }
                    }
                }
                SettingField::Keybind {
                    key, value: None, ..
                } => {
                    if let Some(id) = ShortcutId::from_setting_key(key) {
                        preferences
                            .keybindings
                            .insert(id.id().to_string(), String::new());
                    }
                }
                _ => {}
            }
        }
        Some(preferences)
    }

    pub fn apply_desktop_saved(&mut self) {
        self.desktop_originals = self.desktop_preferences.clone();
        self.capturing_keybind = None;
        for field in self
            .fields
            .iter()
            .filter(|field| is_desktop_setting(field.key()))
        {
            self.originals
                .insert(field.key().to_string(), field.display());
        }
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
        self.capturing_keybind = None;
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
    ) -> Stateful<Div> {
        let mut nav = div()
            .id("settings_navigation")
            .debug_selector(|| "settings_navigation".to_string())
            .w(px(180.0))
            .min_w(px(150.0))
            .min_h_0()
            .overflow_y_scroll()
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
                .h(theme.space(40.0))
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

        let search_handler = settings_click_handler(&on_action, SettingsInteraction::FocusSearch);
        let search = div()
            .id("settings_search")
            .debug_selector(|| "settings_search".to_string())
            .w_full()
            .min_h(px(38.0))
            .flex()
            .items_center()
            .gap(px(10.0))
            .px(px(11.0))
            .rounded(px(8.0))
            .bg(theme.mockup_sidebar)
            .border_1()
            .border_color(if self.searching {
                theme.accent_blue
            } else {
                theme.mockup_border
            })
            .when(!self.busy, |this| {
                this.cursor_pointer()
                    .on_click(move |event, window, cx| search_handler(event, window, cx))
            })
            .child(
                crate::ui::modals::text_input::input_content(
                    &self.search_query,
                    self.search_cursor,
                    self.search_anchor,
                    "Search settings…",
                    false,
                    self.searching && !self.busy,
                    theme,
                )
                .flex_1()
                .min_w_0()
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(MockupTypeScale::BASE_SIZE)),
            )
            .child(
                div()
                    .px(px(5.0))
                    .py(px(1.0))
                    .rounded(px(3.0))
                    .bg(theme.mockup_hover)
                    .font_family(theme.font_mono()[0].clone())
                    .text_size(px(MockupTypeScale::HINT_SIZE))
                    .text_color(theme.mockup_text_dim)
                    .child("/"),
            );
        content = content.child(search);

        if self.load_failed {
            content = content.child(
                div()
                    .flex()
                    .flex_col()
                    .gap(px(4.0))
                    .child(
                        div()
                            .text_size(px(MockupTypeScale::BASE_SIZE))
                            .text_color(theme.mockup_text)
                            .child("Runtime settings are unavailable."),
                    )
                    .when(self.error.is_some(), |this| {
                        this.child(
                            div()
                                .text_size(px(MockupTypeScale::HINT_SIZE))
                                .text_color(theme.rose_fg())
                                .child(self.error.clone().unwrap_or_default()),
                        )
                    }),
            );
        }

        if !self.active_section.is_local()
            && self.fields.iter().all(|field| is_desktop_setting(field.key()))
            && !self.load_failed
        {
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

        let has_visible_action = self
            .visible_indices()
            .iter()
            .any(|index| self.fields[*index].is_action());
        if !self.fields.is_empty()
            && (!self.load_failed
                || self.active_section.is_local()
                || !self.search_query.trim().is_empty()
                || has_visible_action)
        {
            let fields = self.fields.clone();
            let visible_indices = self.visible_indices();
            let filtering = !self.search_query.trim().is_empty();
            let originals = self.originals.clone();
            let selected = self.selected;
            let editing = self.editing && !self.searching;
            let capturing_keybind = self.capturing_keybind;
            let selection_anchor = self.selection_anchor;
            let busy = self.busy;
            let scroll_handle = self.scroll_handle.clone();
            let theme = theme.clone();
            let on_action = on_action.clone();
            let list_id = format!("settings_rows_{}", self.active_section.id());
            if visible_indices.is_empty() {
                let message = if filtering {
                    format!("No settings match “{}”", self.search_query.trim())
                } else {
                    "No items are available in this section.".to_string()
                };
                content = content.child(
                    div()
                        .debug_selector(|| "settings_no_results".to_string())
                        .flex_1()
                        .flex()
                        .items_center()
                        .justify_center()
                        .text_size(px(MockupTypeScale::BASE_SIZE))
                        .text_color(theme.mockup_text_dim)
                        .child(message),
                );
            } else {
                let list = uniform_list(
                    SharedString::from(list_id),
                    visible_indices.len(),
                    move |range, _window, cx| {
                        let motion = ModalMotion {
                            owner: motion.owner,
                            cx,
                        };
                        range
                            .map(|row_index| {
                                let index = visible_indices[row_index];
                                render_setting_row(
                                    &fields[index],
                                    index,
                                    index == selected,
                                    editing,
                                    capturing_keybind,
                                    selection_anchor,
                                    busy,
                                    filtering,
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
                .child(
                    "↑↓ rows · PgUp/PgDn sections · / search · Enter open/edit · ←→ adjust · Esc cancel",
                )
                .child("Appearance, motion, display, keyboard & notifications · saved locally on this device")
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
    capturing_keybind: Option<&'static str>,
    selection_anchor: usize,
    busy: bool,
    filtering: bool,
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
        SettingField::Keybind { value, .. } => {
            let capturing = capturing_keybind == Some(field.key());
            let current = value
                .as_deref()
                .map(shortcuts::display_keystroke)
                .unwrap_or_else(|| "Unbound".to_string());
            div()
                .text_color(if capturing || value.is_none() {
                    theme.accent_blue
                } else {
                    theme.mockup_text_bright
                })
                .child(if capturing {
                    "Press a shortcut…".to_string()
                } else if selected {
                    format!("‹ {current} ›")
                } else {
                    current
                })
        }
        SettingField::Action { value, .. } => div()
            .text_color(theme.accent_blue)
            .child(format!("{value} →")),
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

    let interaction = match field {
        SettingField::Action { key, .. } => SettingsInteraction::Activate(key),
        _ if filtering => SettingsInteraction::RevealSetting(field.key()),
        _ => SettingsInteraction::SelectRow(index),
    };
    let handler = settings_click_handler(&on_action, interaction);
    let row_selector = match field {
        SettingField::Action { key, .. } => format!(
            "settings_action_{}",
            key.strip_prefix("desktop.action.").unwrap_or(key)
        ),
        _ => format!("settings_row_{index}"),
    };
    let mut row = div()
        .id(SharedString::from(format!("settings_row_{index}")))
        .debug_selector(move || row_selector.clone())
        .h(theme.space(76.0))
        .w_full()
        .flex()
        .flex_col()
        .justify_center()
        .gap(theme.space(3.0))
        .px(theme.space(12.0))
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
    let descriptor = setting_descriptor(field.key());
    row = row.child(
        div()
            .w_full()
            .text_size(px(MockupTypeScale::HINT_SIZE))
            .text_color(theme.mockup_text_dim)
            .child(if filtering {
                format!(
                    "{} · {}",
                    descriptor.section.title(),
                    descriptor.description
                )
            } else {
                descriptor.description.to_string()
            }),
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
                "webSearch": { "provider": "brave" },
                "cloud": { "baseUrl": "" },
                "learning": { "autoReflect": "off" }
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
        let keyboard_count = ShortcutId::ALL
            .into_iter()
            .filter(|id| id.available())
            .count();
        assert_eq!(modal.fields.len(), 20 + keyboard_count);
        assert_eq!(SettingsSection::ALL.len(), 12);
        assert_eq!(modal.fields[12].key(), "desktop.motion");
        assert_eq!(modal.fields[13].key(), "desktop.sidebar");
        assert_eq!(modal.fields[14].key(), "desktop.keybind.command_palette");
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
    fn test_settings_actions_activate_without_dirtying_settings() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());

        for (section, key) in [
            (SettingsSection::Model, "desktop.action.models"),
            (SettingsSection::Sessions, "desktop.action.sessions"),
            (SettingsSection::Cloud, "desktop.action.cloud"),
        ] {
            let index = modal
                .fields
                .iter()
                .position(|field| field.key() == key)
                .unwrap();
            modal.select_row(index);
            assert_eq!(modal.active_section, section);
            assert_eq!(modal.selected_action(), Some(key));
            assert_eq!(
                modal.handle_key("enter"),
                Some(SettingsAction::Activate(key))
            );
            assert_eq!(modal.dirty_count(), 0);
            assert_eq!(modal.desktop_dirty_count(), 0);
            assert_eq!(modal.dirty_updates(), serde_json::json!({}));
            assert!(modal.changed_desktop_preferences().is_none());
        }
    }

    #[test]
    fn test_settings_actions_are_searchable_and_deep_linkable() {
        let mut modal = SettingsModal::loading();
        assert!(modal.request_setting("desktop.action.sessions"));
        assert_eq!(modal.active_section, SettingsSection::Sessions);
        assert_eq!(modal.selected_action(), Some("desktop.action.sessions"));
        assert_eq!(modal.dirty_count(), 0);

        modal.focus_search();
        modal.select_all_text();
        modal.push_char("Manage sessions");
        let index = modal
            .fields
            .iter()
            .position(|field| field.key() == "desktop.action.sessions")
            .unwrap();
        assert_eq!(modal.search_results(), vec![index]);
        assert_eq!(
            modal.handle_key("enter"),
            Some(SettingsAction::Activate("desktop.action.sessions"))
        );
        assert_eq!(modal.dirty_count(), 0);
    }

    #[test]
    fn test_visual_notification_preferences_save_locally() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());
        for key in [
            "desktop.notifications.approval_badges",
            "desktop.notifications.update_badges",
            "desktop.notifications.update_banners",
        ] {
            let index = modal
                .fields
                .iter()
                .position(|field| field.key() == key)
                .unwrap();
            assert_eq!(modal.fields[index].display(), "enabled");
            modal.select_row(index);
            assert_eq!(modal.fields[index].display(), "disabled");
        }

        assert_eq!(modal.desktop_dirty_count(), 3);
        assert_eq!(modal.dirty_count(), 3);
        assert!(modal.dirty_updates().as_object().unwrap().is_empty());
        let preferences = modal.changed_desktop_preferences().unwrap();
        assert!(!preferences.approval_badges);
        assert!(!preferences.update_badges);
        assert!(!preferences.update_banners);
    }

    #[test]
    fn test_keybind_capture_clear_reset_and_conflict_validation() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());
        let index = modal
            .fields
            .iter()
            .position(|field| field.key() == ShortcutId::CommandPalette.setting_key())
            .unwrap();
        let original = modal.fields[index].display();

        modal.select_row(index);
        assert_eq!(
            modal.capturing_keybind(),
            Some(ShortcutId::CommandPalette.setting_key())
        );
        modal.capture_keystroke("cmd");
        assert!(modal.capturing_keybind().is_some());
        modal.capture_keystroke("escape");
        assert!(modal.capturing_keybind().is_none());
        assert_eq!(modal.fields[index].display(), original);

        modal.begin_keybind_capture(ShortcutId::CommandPalette.setting_key());
        modal.capture_keystroke("cmd-alt-p");
        assert!(modal.capturing_keybind().is_none());
        assert_eq!(modal.dirty_count(), 1);
        assert_eq!(modal.validation_error(), None);
        let preferences = modal.changed_desktop_preferences().unwrap();
        assert_eq!(
            preferences.keybindings[ShortcutId::CommandPalette.id()],
            shortcuts::canonical_keystroke("cmd-alt-p").unwrap()
        );
        assert!(modal.dirty_updates().as_object().unwrap().is_empty());

        modal.begin_keybind_capture(ShortcutId::CommandPalette.setting_key());
        modal.capture_keystroke("secondary-c");
        assert!(modal.validation_error().unwrap().contains("Copy"));

        modal.begin_keybind_capture(ShortcutId::CommandPalette.setting_key());
        modal.capture_keystroke("backspace");
        let preferences = modal.changed_desktop_preferences().unwrap();
        assert_eq!(preferences.keybindings[ShortcutId::CommandPalette.id()], "");
        assert_eq!(modal.validation_error(), None);

        modal.begin_keybind_capture(ShortcutId::CommandPalette.setting_key());
        modal.capture_keystroke("secondary-r");
        assert!(modal.changed_desktop_preferences().is_none());
        assert_eq!(modal.fields[index].display(), original);
        assert_eq!(modal.dirty_count(), 0);
    }

    #[test]
    fn test_keybind_reset_removes_a_persisted_override() {
        let preferences = DesktopPreferences {
            keybindings: BTreeMap::from([(
                ShortcutId::CommandPalette.id().to_string(),
                "cmd-alt-p".to_string(),
            )]),
            ..DesktopPreferences::default()
        };
        let mut modal = SettingsModal::loading_with_preferences(preferences);
        modal.begin_keybind_capture(ShortcutId::CommandPalette.setting_key());
        modal.capture_keystroke("secondary-r");

        let preferences = modal.changed_desktop_preferences().unwrap();
        assert!(!preferences
            .keybindings
            .contains_key(ShortcutId::CommandPalette.id()));
        assert_eq!(modal.dirty_count(), 1);
    }

    #[test]
    fn test_explicitly_unbound_shortcut_stays_unbound_when_reloaded() {
        let preferences = DesktopPreferences {
            keybindings: BTreeMap::from([(
                ShortcutId::CommandPalette.id().to_string(),
                String::new(),
            )]),
            ..DesktopPreferences::default()
        };
        let mut modal = SettingsModal::loading_with_preferences(preferences);
        let palette_index = modal
            .fields
            .iter()
            .position(|field| field.key() == ShortcutId::CommandPalette.setting_key())
            .unwrap();
        assert!(matches!(
            &modal.fields[palette_index],
            SettingField::Keybind { value: None, .. }
        ));
        assert_eq!(modal.fields[palette_index].display(), "Unbound");
        assert_eq!(modal.desktop_dirty_count(), 0);

        modal.begin_keybind_capture(ShortcutId::NewChat.setting_key());
        modal.capture_keystroke("cmd-alt-p");
        let preferences = modal.changed_desktop_preferences().unwrap();
        assert_eq!(preferences.keybindings[ShortcutId::CommandPalette.id()], "");

        let modal = SettingsModal::loading_with_preferences(preferences);
        assert!(matches!(
            &modal.fields[palette_index],
            SettingField::Keybind { value: None, .. }
        ));
        assert_eq!(modal.desktop_dirty_count(), 0);
    }

    #[test]
    fn test_split_navigation_scopes_rows_to_the_active_section() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());

        assert_eq!(modal.active_section, SettingsSection::Model);
        assert_eq!(modal.section_indices(), vec![0, 1, 2, 3, 4, 5, 25]);
        modal.handle_key("down");
        assert_eq!(modal.selected, 1);

        modal.handle_key("pageup");
        assert_eq!(modal.active_section, SettingsSection::Notifications);
        assert_eq!(
            modal.section_indices().first(),
            Some(&(modal.fields.len() - 6))
        );
        assert_eq!(
            modal.section_indices().last(),
            Some(&(modal.fields.len() - 4))
        );
        assert_eq!(modal.selected, modal.fields.len() - 6);

        modal.handle_key("pageup");
        assert_eq!(modal.active_section, SettingsSection::Keyboard);
        assert_eq!(modal.section_indices().first(), Some(&14));
        assert_eq!(
            modal.section_indices().last(),
            Some(&(modal.fields.len() - 7))
        );
        assert_eq!(modal.selected, 14);

        modal.handle_key("pageup");
        assert_eq!(modal.active_section, SettingsSection::MotionDisplay);
        assert_eq!(modal.section_indices(), vec![12, 13]);
        assert_eq!(modal.selected, 12);
        modal.handle_key("down");
        assert_eq!(modal.selected, 13);
        modal.handle_key("down");
        assert_eq!(modal.selected, modal.fields.len());
        modal.handle_key("down");
        assert_eq!(modal.selected, 12);

        modal.handle_key("pageup");
        assert_eq!(modal.active_section, SettingsSection::Appearance);
        assert_eq!(modal.section_indices(), vec![10, 11]);
        assert_eq!(modal.selected, 10);

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
    fn test_search_ranks_matches_and_reveals_across_sections() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());

        modal.focus_search();
        modal.push_char("model.cloud");
        assert_eq!(modal.search_results(), vec![0, 1, 5]);
        assert_eq!(modal.selected, 0);

        modal.select_all_text();
        modal.push_char("Model ID");
        assert_eq!(modal.search_results(), vec![1]);
        modal.handle_key("enter");
        assert!(!modal.searching);
        assert!(modal.search_query.is_empty());
        assert_eq!(modal.active_section, SettingsSection::Model);
        assert_eq!(modal.selected, 1);
        assert!(modal.editing);

        modal.focus_search();
        modal.push_char("learn");
        assert_eq!(modal.search_results(), vec![9]);
        assert_eq!(modal.selected, 9);
        modal.handle_key("enter");
        assert_eq!(modal.active_section, SettingsSection::Learning);
        assert_eq!(modal.selected, 9);
        assert!(!modal.editing);
        assert_eq!(modal.fields[9].display(), "off");
    }

    #[test]
    fn test_search_editing_and_empty_results_do_not_change_settings() {
        let mut modal = SettingsModal::loading();
        modal.apply_view(&sample_view());

        modal.handle_key("/");
        assert!(modal.searching);
        modal.push_char("maxx");
        assert_eq!(modal.search_results(), Vec::<usize>::new());
        assert_eq!(modal.selected, modal.fields.len());
        modal.backspace();
        assert_eq!(modal.search_query, "max");
        assert_eq!(modal.search_results(), vec![6]);
        modal.handle_key("home");
        modal.handle_key("shift-right");
        assert_eq!(modal.selected_text().as_deref(), Some("m"));
        modal.push_char("M");
        assert_eq!(modal.search_query, "Max");
        assert_eq!(modal.search_results(), vec![6]);
        assert_eq!(modal.fields[6].display(), "90");

        modal.cancel_search();
        assert!(!modal.searching);
        assert!(modal.search_query.is_empty());
        assert_eq!(modal.dirty_count(), 0);
    }

    #[test]
    fn test_pending_setting_deep_link_is_applied_after_load() {
        let mut modal = SettingsModal::loading();
        assert!(modal.request_setting("runtime.maxTurns"));
        assert_eq!(modal.pending_setting.as_deref(), Some("runtime.maxTurns"));
        assert!(!modal.request_setting("runtime.unknown"));

        modal.apply_view(&sample_view());
        assert!(modal.pending_setting.is_none());
        assert_eq!(modal.active_section, SettingsSection::Runtime);
        assert_eq!(modal.selected, 6);
        assert!(modal.editing);
        assert_eq!(modal.fields[6].display(), "90");
        assert_eq!(modal.dirty_count(), 0);
    }

    #[test]
    fn test_desktop_settings_stay_out_of_acp_updates() {
        let preferences = DesktopPreferences {
            sidebar_collapsed: true,
            motion: MotionPreference::Reduced,
            theme: ThemePreset::Midnight,
            density: InterfaceDensity::Compact,
            ..DesktopPreferences::default()
        };
        let mut modal = SettingsModal::loading_with_preferences(preferences);
        assert_eq!(modal.selected, modal.fields.len());
        assert_eq!(modal.handle_key("enter"), None);
        assert_eq!(modal.dirty_count(), 0);
        for key in [
            "desktop.theme",
            "desktop.density",
            "desktop.motion",
            "desktop.sidebar",
        ] {
            let index = modal
                .fields
                .iter()
                .position(|field| field.key() == key)
                .unwrap();
            modal.select_row(index);
        }
        let changed = modal
            .changed_desktop_preferences()
            .expect("desktop changes");
        assert_eq!(changed.theme, ThemePreset::Mono);
        assert_eq!(changed.density, InterfaceDensity::Comfortable);
        assert_eq!(changed.motion, MotionPreference::Full);
        assert!(!changed.sidebar_collapsed);
        assert_eq!(modal.dirty_updates(), serde_json::json!({}));
        assert_eq!(modal.desktop_dirty_count(), 4);

        modal.apply_view(&sample_view());
        assert_eq!(modal.fields[10].display(), "mono");
        assert_eq!(modal.fields[11].display(), "comfortable");
        assert_eq!(modal.fields[12].display(), "full");
        assert_eq!(modal.fields[13].display(), "expanded");
        assert_eq!(modal.desktop_dirty_count(), 4);

        modal.desktop_preferences = changed;
        modal.apply_desktop_saved();
        assert_eq!(modal.desktop_dirty_count(), 0);
        assert_eq!(modal.dirty_count(), 0);
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

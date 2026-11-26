//! Global keyboard shortcuts and GPUI actions for HEX Desktop.
//!
//! `secondary` is the platform primary modifier: Command on macOS, Ctrl on
//! Windows and Linux. Bindings that only make sense on one platform are added
//! per target.

use std::collections::BTreeMap;

use gpui::{App, KeyBinding, Keystroke};

use crate::preferences::DesktopPreferences;

macro_rules! declare_actions {
    (
        $($(#[$doc:meta])* $name:ident),* $(,)?
    ) => {
        $(
            $(#[$doc])*
            #[derive(Clone, PartialEq, Eq, Default, Debug)]
            pub struct $name;

            impl gpui::Action for $name {
                fn boxed_clone(&self) -> Box<dyn gpui::Action> {
                    Box::new(self.clone())
                }

                fn partial_eq(&self, action: &dyn gpui::Action) -> bool {
                    action.as_any().downcast_ref::<Self>().map_or(false, |a| self == a)
                }

                fn name(&self) -> &'static str {
                    concat!("hex_desktop::", stringify!($name))
                }

                fn name_for_type() -> &'static str
                where
                    Self: Sized,
                {
                    concat!("hex_desktop::", stringify!($name))
                }

                fn build(_value: serde_json::Value) -> anyhow::Result<Box<dyn gpui::Action>>
                where
                    Self: Sized,
                {
                    Ok(Box::new(Self))
                }
            }
        )*
    };
}

declare_actions!(
    /// Toggle the Command Palette modal overlay (Cmd+K).
    ToggleCommandPalette,
    /// Start a fresh conversation session (Cmd+N).
    NewChat,
    /// Submit current composer prompt to the agent (Cmd+Enter).
    SubmitPrompt,
    /// Toggle left sidebar collapse (Cmd+B).
    ToggleSidebar,
    /// Open Settings and configuration dialog (Cmd+,).
    OpenSettings,
    /// Dismiss active modal or cancel active generation (Esc).
    CancelOrDismiss,
    /// Standard macOS Edit actions
    Undo,
    Redo,
    Cut,
    Copy,
    Paste,
    SelectAll,
    /// Window & App actions
    CloseWindow,
    Quit,
    Hide,
    About,
);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShortcutId {
    CommandPalette,
    NewChat,
    SubmitPrompt,
    ToggleSidebar,
    OpenSettings,
    CloseWindow,
    Quit,
    Hide,
}

impl ShortcutId {
    pub const ALL: [Self; 8] = [
        Self::CommandPalette,
        Self::NewChat,
        Self::SubmitPrompt,
        Self::ToggleSidebar,
        Self::OpenSettings,
        Self::CloseWindow,
        Self::Quit,
        Self::Hide,
    ];

    pub fn id(self) -> &'static str {
        match self {
            Self::CommandPalette => "command_palette",
            Self::NewChat => "new_chat",
            Self::SubmitPrompt => "submit_prompt",
            Self::ToggleSidebar => "toggle_sidebar",
            Self::OpenSettings => "open_settings",
            Self::CloseWindow => "close_window",
            Self::Quit => "quit",
            Self::Hide => "hide",
        }
    }

    pub const fn setting_key(self) -> &'static str {
        match self {
            Self::CommandPalette => "desktop.keybind.command_palette",
            Self::NewChat => "desktop.keybind.new_chat",
            Self::SubmitPrompt => "desktop.keybind.submit_prompt",
            Self::ToggleSidebar => "desktop.keybind.toggle_sidebar",
            Self::OpenSettings => "desktop.keybind.open_settings",
            Self::CloseWindow => "desktop.keybind.close_window",
            Self::Quit => "desktop.keybind.quit",
            Self::Hide => "desktop.keybind.hide",
        }
    }

    pub fn from_setting_key(key: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|id| id.setting_key() == key)
    }

    pub const fn label(self) -> &'static str {
        match self {
            Self::CommandPalette => "Command palette",
            Self::NewChat => "New chat",
            Self::SubmitPrompt => "Submit prompt",
            Self::ToggleSidebar => "Toggle sidebar",
            Self::OpenSettings => "Open settings",
            Self::CloseWindow => "Close window",
            Self::Quit => "Quit HEX",
            Self::Hide => "Hide HEX",
        }
    }

    pub const fn description(self) -> &'static str {
        match self {
            Self::CommandPalette => "Open the command palette.",
            Self::NewChat => "Start a new chat.",
            Self::SubmitPrompt => "Submit the composer outside the chat editor.",
            Self::ToggleSidebar => "Show or hide the navigation sidebar.",
            Self::OpenSettings => "Open desktop and runtime settings.",
            Self::CloseWindow => "Close the current window.",
            Self::Quit => "Quit the application.",
            Self::Hide => "Hide the application.",
        }
    }

    pub fn default_keystroke(self) -> &'static str {
        match self {
            Self::CommandPalette => "secondary-k",
            Self::NewChat => "secondary-n",
            Self::SubmitPrompt => "secondary-enter",
            Self::ToggleSidebar => "secondary-b",
            Self::OpenSettings => "secondary-,",
            Self::CloseWindow => "secondary-w",
            Self::Quit => "secondary-q",
            Self::Hide => "secondary-h",
        }
    }

    pub fn available(self) -> bool {
        match self {
            Self::Quit | Self::Hide => cfg!(target_os = "macos"),
            _ => true,
        }
    }

    fn binding(self, source: &str) -> KeyBinding {
        match self {
            Self::CommandPalette => KeyBinding::new(source, ToggleCommandPalette, None),
            Self::NewChat => KeyBinding::new(source, NewChat, None),
            Self::SubmitPrompt => KeyBinding::new(source, SubmitPrompt, None),
            Self::ToggleSidebar => KeyBinding::new(source, ToggleSidebar, None),
            Self::OpenSettings => KeyBinding::new(source, OpenSettings, None),
            Self::CloseWindow => KeyBinding::new(source, CloseWindow, None),
            Self::Quit => KeyBinding::new(source, Quit, None),
            Self::Hide => KeyBinding::new(source, Hide, None),
        }
    }
}

pub fn canonical_keystroke(source: &str) -> Option<String> {
    Keystroke::parse(source)
        .ok()
        .map(|keystroke| keystroke.unparse())
}

pub fn display_keystroke(source: &str) -> String {
    canonical_keystroke(source)
        .and_then(|source| Keystroke::parse(&source).ok())
        .map(|keystroke| keystroke.to_string())
        .unwrap_or_else(|| source.to_string())
}

pub fn is_modifier_only(source: &str) -> bool {
    Keystroke::parse(source).is_ok_and(|keystroke| {
        matches!(
            keystroke.key.as_str(),
            "shift" | "control" | "alt" | "platform" | "function"
        )
    })
}

pub fn validate_keystroke(source: &str) -> Result<(), String> {
    if source.trim().is_empty() {
        return Err("Choose a shortcut or press Backspace to unbind it.".to_string());
    }
    if source.chars().any(char::is_whitespace) || source.contains("->") {
        return Err("Shortcuts must be a single key combination.".to_string());
    }
    let keystroke =
        Keystroke::parse(source).map_err(|error| format!("Invalid shortcut: {error}"))?;
    if matches!(
        keystroke.key.as_str(),
        "shift" | "control" | "alt" | "platform" | "function"
    ) {
        return Err("Modifier keys cannot be shortcuts on their own.".to_string());
    }
    let has_command_modifier = keystroke.modifiers.control
        || keystroke.modifiers.platform
        || keystroke.modifiers.alt
        || keystroke.modifiers.function;
    if keystroke.key.chars().count() == 1 && !has_command_modifier {
        return Err(
            "Letter and punctuation shortcuts need Command or Ctrl, Option or Alt, or Fn."
                .to_string(),
        );
    }
    if !has_command_modifier
        && matches!(
            keystroke.key.as_str(),
            "enter"
                | "tab"
                | "escape"
                | "space"
                | "backspace"
                | "delete"
                | "insert"
                | "left"
                | "right"
                | "up"
                | "down"
                | "home"
                | "end"
                | "pageup"
                | "pagedown"
                | "back"
                | "forward"
        )
    {
        return Err("That key is reserved for text editing and settings navigation.".to_string());
    }
    Ok(())
}

const FIXED_KEYBINDINGS: &[(&str, &str)] = &[
    ("secondary-enter", "Submit prompt"),
    ("enter", "Submit prompt"),
    ("shift-enter", "Insert newline"),
    ("tab", "Focus navigation"),
    ("secondary-a", "Select all"),
    ("secondary-c", "Copy"),
    ("secondary-x", "Cut"),
    ("secondary-v", "Paste"),
    ("secondary-z", "Undo"),
    ("secondary-shift-z", "Redo"),
];

pub fn keybind_conflict(
    id: ShortcutId,
    source: &str,
    overrides: &BTreeMap<String, String>,
) -> Option<String> {
    let candidate = canonical_keystroke(source)?;
    for (fixed, label) in FIXED_KEYBINDINGS {
        if id == ShortcutId::SubmitPrompt && *fixed == "secondary-enter" {
            continue;
        }
        if canonical_keystroke(fixed).as_deref() == Some(candidate.as_str()) {
            return Some((*label).to_string());
        }
    }
    #[cfg(target_os = "macos")]
    for (fixed, label) in [
        ("secondary-left", "Text navigation"),
        ("secondary-right", "Text navigation"),
        ("secondary-up", "Text navigation"),
        ("secondary-down", "Text navigation"),
        ("secondary-shift-left", "Text selection"),
        ("secondary-shift-right", "Text selection"),
        ("secondary-shift-up", "Text selection"),
        ("secondary-shift-down", "Text selection"),
        ("secondary-backspace", "Delete line"),
        ("alt-left", "Word navigation"),
        ("alt-right", "Word navigation"),
        ("alt-shift-left", "Word selection"),
        ("alt-shift-right", "Word selection"),
        ("alt-backspace", "Delete word"),
        ("alt-delete", "Delete word"),
    ] {
        if canonical_keystroke(fixed).as_deref() == Some(candidate.as_str()) {
            return Some(label.to_string());
        }
    }
    #[cfg(not(target_os = "macos"))]
    for (fixed, label) in [
        ("secondary-left", "Text navigation"),
        ("secondary-right", "Text navigation"),
        ("secondary-home", "Text navigation"),
        ("secondary-end", "Text navigation"),
        ("secondary-shift-left", "Text selection"),
        ("secondary-shift-right", "Text selection"),
        ("secondary-shift-home", "Text selection"),
        ("secondary-shift-end", "Text selection"),
        ("secondary-backspace", "Delete word"),
        ("secondary-delete", "Delete word"),
        ("secondary-y", "Redo"),
        ("alt-f4", "Close window"),
    ] {
        if canonical_keystroke(fixed).as_deref() == Some(candidate.as_str()) {
            return Some(label.to_string());
        }
    }
    for other in ShortcutId::ALL {
        if other == id || !other.available() {
            continue;
        }
        let current = overrides
            .get(other.id())
            .filter(|source| !source.is_empty())
            .cloned()
            .unwrap_or_else(|| other.default_keystroke().to_string());
        if canonical_keystroke(&current).as_deref() == Some(candidate.as_str()) {
            return Some(other.label().to_string());
        }
    }
    None
}

pub fn validate_keybinding(
    id: ShortcutId,
    source: &str,
    overrides: &BTreeMap<String, String>,
) -> Result<(), String> {
    validate_keystroke(source)?;
    if let Some(conflict) = keybind_conflict(id, source, overrides) {
        return Err(format!("{source} is already used by {conflict}."));
    }
    Ok(())
}

pub fn effective_keystroke(preferences: &DesktopPreferences, id: ShortcutId) -> Option<String> {
    if !id.available() {
        return None;
    }
    let source = preferences
        .keybindings
        .get(id.id())
        .map(String::as_str)
        .unwrap_or(id.default_keystroke());
    if source.is_empty() {
        return None;
    }
    if validate_keystroke(source).is_err() {
        return Some(id.default_keystroke().to_string());
    }
    canonical_keystroke(source)
}

pub fn resolved_keybindings(preferences: &DesktopPreferences) -> BTreeMap<String, String> {
    ShortcutId::ALL
        .into_iter()
        .filter(|id| id.available())
        .filter_map(|id| {
            effective_keystroke(preferences, id).map(|source| (id.id().to_string(), source))
        })
        .collect()
}

/// Returns the standard bindings plus validated, editable desktop actions.
pub fn desktop_keybindings(preferences: &DesktopPreferences) -> Vec<KeyBinding> {
    let resolved = resolved_keybindings(preferences);
    let mut bindings = vec![
        KeyBinding::new("escape", CancelOrDismiss, None),
        KeyBinding::new("secondary-z", Undo, None),
        KeyBinding::new("secondary-shift-z", Redo, None),
        KeyBinding::new("secondary-x", Cut, None),
        KeyBinding::new("secondary-c", Copy, None),
        KeyBinding::new("secondary-v", Paste, None),
        KeyBinding::new("secondary-a", SelectAll, None),
    ];
    #[cfg(target_os = "windows")]
    {
        bindings.push(KeyBinding::new("secondary-y", Redo, None));
        bindings.push(KeyBinding::new("alt-f4", CloseWindow, None));
    }
    bindings.extend(
        ShortcutId::ALL
            .into_iter()
            .filter(|id| id.available())
            .filter_map(|id| {
                let custom = preferences.keybindings.get(id.id()).cloned();
                if custom.as_deref() == Some("") {
                    return None;
                }
                let source = custom
                    .as_deref()
                    .filter(|source| !source.is_empty())
                    .filter(|source| validate_keybinding(id, source, &resolved).is_ok())
                    .map(str::to_string)
                    .or_else(|| {
                        let default = id.default_keystroke().to_string();
                        validate_keybinding(id, &default, &resolved)
                            .ok()
                            .map(|()| default)
                    })?;
                Some(id.binding(&source))
            }),
    );
    bindings
}

/// Register desktop keyboard bindings on the GPUI application context.
pub fn init_shortcuts(cx: &mut App) {
    cx.bind_keys(desktop_keybindings(&DesktopPreferences::default()));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[core::prelude::v1::test]
    fn test_desktop_keybindings_registration() {
        let bindings = desktop_keybindings(&DesktopPreferences::default());
        assert!(bindings.len() >= 6);

        // Verify key combinations exist
        let has_cmd_k = bindings
            .iter()
            .any(|b| format!("{:?}", b.action()).contains("ToggleCommandPalette"));
        assert!(has_cmd_k, "expected cmd-k binding");

        let has_cmd_n = bindings
            .iter()
            .any(|b| format!("{:?}", b.action()).contains("NewChat"));
        assert!(has_cmd_n, "expected cmd-n binding");

        let has_cmd_b = bindings
            .iter()
            .any(|b| format!("{:?}", b.action()).contains("ToggleSidebar"));
        assert!(has_cmd_b, "expected cmd-b binding");

        for unsupported in ["cmd-0", "cmd-=", "cmd-+", "cmd--"] {
            assert!(
                bindings.iter().all(|binding| {
                    binding
                        .keystrokes()
                        .iter()
                        .all(|keystroke| keystroke.to_string() != unsupported)
                }),
                "unsupported zoom binding must not be advertised: {unsupported}"
            );
        }

        let uses_platform_modifier = bindings.iter().any(|binding| {
            binding
                .keystrokes()
                .iter()
                .any(|keystroke| keystroke.modifiers().platform || keystroke.modifiers().control)
        });
        assert!(
            uses_platform_modifier,
            "global shortcuts must use the platform primary modifier"
        );
    }

    fn binds(bindings: &[KeyBinding], action: &str, shortcut: &str) -> bool {
        let expected = Keystroke::parse(shortcut)
            .expect("valid shortcut")
            .to_string();
        bindings.iter().any(|binding| {
            format!("{:?}", binding.action()).contains(action)
                && binding
                    .keystrokes()
                    .iter()
                    .any(|keystroke| keystroke.to_string() == expected)
        })
    }

    #[test]
    fn custom_bindings_replace_defaults_and_empty_values_unbind_actions() {
        let preferences = DesktopPreferences {
            keybindings: BTreeMap::from([
                (
                    ShortcutId::CommandPalette.id().to_string(),
                    "cmd-alt-p".to_string(),
                ),
                (ShortcutId::NewChat.id().to_string(), String::new()),
            ]),
            ..DesktopPreferences::default()
        };
        let bindings = desktop_keybindings(&preferences);

        assert!(binds(&bindings, "ToggleCommandPalette", "cmd-alt-p"));
        assert!(!binds(&bindings, "ToggleCommandPalette", "secondary-k"));
        assert!(!bindings
            .iter()
            .any(|binding| format!("{:?}", binding.action()).contains("NewChat")));
    }

    #[test]
    fn invalid_and_conflicting_overrides_fall_back_without_panicking() {
        for value in ["not a shortcut", "a", "secondary-c", "cmd-alt-p"] {
            let preferences = DesktopPreferences {
                keybindings: BTreeMap::from([
                    (
                        ShortcutId::CommandPalette.id().to_string(),
                        value.to_string(),
                    ),
                    (
                        ShortcutId::NewChat.id().to_string(),
                        "cmd-alt-p".to_string(),
                    ),
                ]),
                ..DesktopPreferences::default()
            };
            let bindings = desktop_keybindings(&preferences);
            assert!(binds(&bindings, "ToggleCommandPalette", "secondary-k"));
        }
    }

    #[test]
    fn shortcut_validation_protects_text_editing_and_duplicate_actions() {
        let overrides = BTreeMap::new();
        assert!(validate_keystroke("f12").is_ok());
        assert!(validate_keystroke("shift-a").is_err());
        assert!(validate_keystroke("space").is_err());
        assert!(validate_keystroke("secondary-c").is_ok());
        assert_eq!(
            keybind_conflict(ShortcutId::CommandPalette, "secondary-c", &overrides).as_deref(),
            Some("Copy")
        );
        assert_eq!(
            keybind_conflict(ShortcutId::CommandPalette, "secondary-n", &overrides).as_deref(),
            Some("New chat")
        );
    }
}

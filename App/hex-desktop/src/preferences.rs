use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Deserializer, Serialize};

use crate::theme::{InterfaceDensity, ThemePreset};
use crate::ui::motion::MotionPreference;

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct DesktopPreferences {
    pub sidebar_collapsed: bool,
    #[serde(
        default,
        alias = "reduce_motion",
        deserialize_with = "deserialize_motion_preference"
    )]
    pub motion: MotionPreference,
    pub theme: ThemePreset,
    pub density: InterfaceDensity,
    pub keybindings: BTreeMap<String, String>,
}

fn deserialize_motion_preference<'de, D>(deserializer: D) -> Result<MotionPreference, D::Error>
where
    D: Deserializer<'de>,
{
    let value = serde_json::Value::deserialize(deserializer)?;
    match value {
        serde_json::Value::Null => Ok(MotionPreference::System),
        serde_json::Value::Bool(true) => Ok(MotionPreference::Reduced),
        serde_json::Value::Bool(false) => Ok(MotionPreference::Full),
        value @ serde_json::Value::String(_) => {
            serde_json::from_value(value).map_err(serde::de::Error::custom)
        }
        _ => Err(serde::de::Error::custom(
            "motion must be system, reduced, full, or a legacy boolean",
        )),
    }
}

impl DesktopPreferences {
    pub fn load() -> Result<Self, String> {
        let Some(path) = default_path() else {
            return Ok(Self::default());
        };
        Self::read_from(&path)
    }

    pub fn save(&self) -> Result<(), String> {
        let path =
            default_path().ok_or_else(|| "desktop preferences path is unavailable".to_string())?;
        self.write_to(&path)
    }

    pub fn read_from(path: &Path) -> Result<Self, String> {
        match fs::read(path) {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .map_err(|error| format!("could not parse {}: {error}", path.display())),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(error) => Err(format!("could not read {}: {error}", path.display())),
        }
    }

    pub fn write_to(&self, path: &Path) -> Result<(), String> {
        let parent = path
            .parent()
            .ok_or_else(|| format!("preferences path has no parent: {}", path.display()))?;
        fs::create_dir_all(parent)
            .map_err(|error| format!("could not create {}: {error}", parent.display()))?;
        let bytes = serde_json::to_vec_pretty(self)
            .map_err(|error| format!("could not serialize desktop preferences: {error}"))?;
        let temporary = path.with_extension("json.tmp");
        fs::write(&temporary, bytes)
            .map_err(|error| format!("could not write {}: {error}", temporary.display()))?;
        if let Err(error) = fs::rename(&temporary, path) {
            #[cfg(windows)]
            if path.exists() {
                fs::remove_file(path)
                    .and_then(|_| fs::rename(&temporary, path))
                    .map_err(|replace| {
                        format!(
                            "could not replace {} after {error}: {replace}",
                            path.display()
                        )
                    })?;
                return Ok(());
            }
            let _ = fs::remove_file(&temporary);
            return Err(format!("could not replace {}: {error}", path.display()));
        }
        Ok(())
    }
}

fn default_path() -> Option<PathBuf> {
    #[cfg(target_os = "macos")]
    {
        env::var_os("HOME")
            .map(PathBuf::from)
            .map(|home| home.join("Library/Application Support/hex-desktop/preferences.json"))
    }
    #[cfg(windows)]
    {
        env::var_os("APPDATA")
            .map(PathBuf::from)
            .map(|root| root.join("hex-desktop/preferences.json"))
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))
            .map(|root| root.join("hex-desktop/preferences.json"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn preferences_round_trip_and_create_parent_directories() {
        let directory = tempfile::tempdir().expect("temp directory");
        let path = directory.path().join("nested/preferences.json");
        let preferences = DesktopPreferences {
            sidebar_collapsed: true,
            motion: MotionPreference::Reduced,
            theme: ThemePreset::Midnight,
            density: InterfaceDensity::Compact,
            ..DesktopPreferences::default()
        };

        preferences.write_to(&path).expect("write preferences");
        assert_eq!(
            DesktopPreferences::read_from(&path).expect("read preferences"),
            preferences
        );
        assert!(!path.with_extension("json.tmp").exists());
    }

    #[test]
    fn missing_preferences_use_defaults_and_invalid_json_is_reported() {
        let directory = tempfile::tempdir().expect("temp directory");
        let path = directory.path().join("preferences.json");
        assert_eq!(
            DesktopPreferences::read_from(&path).expect("missing file"),
            DesktopPreferences::default()
        );

        fs::write(&path, b"not json").expect("write invalid preferences");
        let error = DesktopPreferences::read_from(&path).expect_err("invalid json");
        assert!(error.contains("could not parse"));
    }

    #[test]
    fn legacy_reduced_motion_values_migrate_to_motion_preferences() {
        let directory = tempfile::tempdir().expect("temp directory");
        let path = directory.path().join("preferences.json");
        for (legacy, expected) in [
            ("true", MotionPreference::Reduced),
            ("false", MotionPreference::Full),
            ("null", MotionPreference::System),
        ] {
            fs::write(&path, format!(r#"{{"reduce_motion":{legacy}}}"#).as_bytes())
                .expect("write legacy preferences");
            let preferences = DesktopPreferences::read_from(&path).expect("read preferences");
            assert_eq!(preferences.motion, expected);
        }
    }

    #[test]
    fn older_preferences_keep_new_fields_at_their_defaults() {
        let directory = tempfile::tempdir().expect("temp directory");
        let path = directory.path().join("preferences.json");
        fs::write(&path, br#"{"sidebar_collapsed":true}"#).expect("write older preferences");

        let preferences = DesktopPreferences::read_from(&path).expect("read preferences");
        assert!(preferences.sidebar_collapsed);
        assert_eq!(preferences.motion, MotionPreference::System);
        assert_eq!(preferences.theme, ThemePreset::Hermes);
        assert_eq!(preferences.density, InterfaceDensity::Comfortable);
        assert!(preferences.keybindings.is_empty());
    }

    #[test]
    fn keybinding_overrides_round_trip_including_explicitly_unbound_actions() {
        let directory = tempfile::tempdir().expect("temp directory");
        let path = directory.path().join("preferences.json");
        let preferences = DesktopPreferences {
            keybindings: BTreeMap::from([
                ("command_palette".to_string(), "cmd-alt-p".to_string()),
                ("toggle_sidebar".to_string(), String::new()),
            ]),
            ..DesktopPreferences::default()
        };

        preferences.write_to(&path).expect("write preferences");
        assert_eq!(
            DesktopPreferences::read_from(&path).expect("read preferences"),
            preferences
        );
    }
}

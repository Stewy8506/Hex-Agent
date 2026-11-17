use std::cell::{Cell, RefCell};
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

use crossterm::event::KeyCode;
use ratatui::layout::Rect;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Section {
    Context,
    Plan,
    Changed,
    Agents,
}

impl Section {
    pub fn label(self) -> &'static str {
        match self {
            Self::Context => "context",
            Self::Plan => "plan",
            Self::Changed => "changed",
            Self::Agents => "agents",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SectionPreference {
    pub section: Section,
    pub visible: bool,
    pub collapsed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SidebarPreferences {
    pub width: Option<u16>,
    pub sections: Vec<SectionPreference>,
}

impl Default for SidebarPreferences {
    fn default() -> Self {
        Self {
            width: None,
            sections: [
                Section::Context,
                Section::Plan,
                Section::Changed,
                Section::Agents,
            ]
            .into_iter()
            .map(|section| SectionPreference {
                section,
                visible: true,
                collapsed: false,
            })
            .collect(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SidebarAction {
    Settings,
    ToggleSection(Section),
    ToggleChanges,
}

#[derive(Debug, Default)]
pub struct SidebarState {
    pub preferences: SidebarPreferences,
    pub scroll: Cell<usize>,
    pub metrics: Cell<(usize, usize)>,
    pub area: Cell<Rect>,
    pub hit_targets: RefCell<Vec<(Rect, SidebarAction)>>,
    pub show_all_changed: bool,
    pub show_settings: bool,
    pub selected_section: usize,
    pub resizing: bool,
    storage_path: Option<PathBuf>,
}

impl SidebarState {
    pub fn width_for(&self, viewport_width: u16) -> u16 {
        if viewport_width < 90 {
            return 0;
        }
        let preferred = self
            .preferences
            .width
            .unwrap_or_else(|| ((viewport_width as f32 * 0.23).round() as u16).clamp(28, 44));
        preferred
            .clamp(28, 96)
            .min(viewport_width.saturating_sub(41))
    }

    pub fn clear_geometry(&self) {
        self.area.set(Rect::default());
        self.hit_targets.borrow_mut().clear();
        self.metrics.set((0, 0));
    }

    pub fn scroll_by(&self, delta: i32) {
        let (total, visible) = self.metrics.get();
        let max = total.saturating_sub(visible);
        self.scroll.set(
            self.scroll
                .get()
                .saturating_add_signed(delta as isize)
                .min(max),
        );
    }

    pub fn click(&mut self, x: u16, y: u16) -> bool {
        let action = self
            .hit_targets
            .borrow()
            .iter()
            .find(|(rect, _)| rect.contains((x, y).into()))
            .map(|(_, action)| *action);
        match action {
            Some(SidebarAction::Settings) => self.show_settings = true,
            Some(SidebarAction::ToggleChanges) => self.show_all_changed = !self.show_all_changed,
            Some(SidebarAction::ToggleSection(section)) => {
                if let Some(item) = self
                    .preferences
                    .sections
                    .iter_mut()
                    .find(|p| p.section == section)
                {
                    item.collapsed = !item.collapsed;
                }
                return true;
            }
            None => {}
        }
        false
    }

    pub fn settings_key(&mut self, key: KeyCode) -> bool {
        let previous = self.preferences.clone();
        match key {
            KeyCode::Esc | KeyCode::F(4) => self.show_settings = false,
            KeyCode::Up => self.selected_section = self.selected_section.saturating_sub(1),
            KeyCode::Down => self.selected_section = (self.selected_section + 1).min(3),
            KeyCode::Char(' ') => {
                let item = &mut self.preferences.sections[self.selected_section];
                item.visible = !item.visible;
            }
            KeyCode::Char('c') => {
                let item = &mut self.preferences.sections[self.selected_section];
                item.collapsed = !item.collapsed;
            }
            KeyCode::Char(',') if self.selected_section > 0 => {
                self.preferences
                    .sections
                    .swap(self.selected_section, self.selected_section - 1);
                self.selected_section -= 1;
            }
            KeyCode::Char('.') if self.selected_section < 3 => {
                self.preferences
                    .sections
                    .swap(self.selected_section, self.selected_section + 1);
                self.selected_section += 1;
            }
            KeyCode::Char('+') | KeyCode::Char('=') | KeyCode::Char('-') => {
                let width = self
                    .preferences
                    .width
                    .unwrap_or_else(|| self.area.get().width.max(28));
                self.preferences.width = Some(if key == KeyCode::Char('-') {
                    width.saturating_sub(2).max(28)
                } else {
                    width.saturating_add(2).min(96)
                });
            }
            KeyCode::Char('a') => self.preferences.width = None,
            KeyCode::Char('r') => {
                self.preferences = SidebarPreferences::default();
                self.selected_section = 0;
                self.show_all_changed = false;
                self.scroll.set(0);
            }
            _ => {}
        }
        previous != self.preferences
    }

    pub fn resize_to(&mut self, width: u16) {
        self.preferences.width = Some(width.clamp(28, 96));
    }

    pub fn load(&mut self, workspace: &Path) -> io::Result<()> {
        self.storage_path = None;
        let path = workspace.join(".hex/data/tui-sidebar.json");
        check_preferences_path(workspace)?;
        let bytes = match fs::File::open(&path) {
            Ok(file) => {
                let mut bytes = Vec::new();
                file.take(64 * 1024 + 1).read_to_end(&mut bytes)?;
                if bytes.len() > 64 * 1024 {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "sidebar preferences exceed 64 KiB",
                    ));
                }
                Some(bytes)
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        if let Some(bytes) = bytes {
            let preferences: SidebarPreferences = serde_json::from_slice(&bytes)?;
            let defaults = SidebarPreferences::default();
            if preferences
                .width
                .is_some_and(|width| !(28..=96).contains(&width))
                || preferences.sections.len() != 4
                || defaults.sections.iter().any(|expected| {
                    preferences
                        .sections
                        .iter()
                        .filter(|p| p.section == expected.section)
                        .count()
                        != 1
                })
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "invalid sidebar preferences",
                ));
            }
            self.preferences = preferences;
        } else {
            self.preferences = SidebarPreferences::default();
        }
        self.storage_path = Some(path);
        Ok(())
    }

    pub fn save(&self) -> io::Result<()> {
        let Some(path) = &self.storage_path else {
            return Ok(());
        };
        let workspace = path.parent().unwrap().parent().unwrap().parent().unwrap();
        check_preferences_path(workspace)?;
        let parent = path.parent().unwrap();
        fs::create_dir_all(parent)?;
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let temporary = parent.join(format!(".tui-sidebar-{}-{nonce}.tmp", std::process::id()));
        let result = (|| {
            let bytes = serde_json::to_vec_pretty(&self.preferences)?;
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temporary)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&temporary, path)
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }
}

fn check_preferences_path(workspace: &Path) -> io::Result<()> {
    for path in [
        workspace.join(".hex"),
        workspace.join(".hex/data"),
        workspace.join(".hex/data/tui-sidebar.json"),
    ] {
        match fs::symlink_metadata(path) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "sidebar preferences must not use symlinks",
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

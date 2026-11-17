use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

use crossterm::event::KeyCode;
use hex_tui::sidebar::{Section, SidebarAction, SidebarPreferences, SidebarState};
use ratatui::layout::Rect;

struct Workspace(PathBuf);
impl Workspace {
    fn new() -> Self {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "hex_sidebar_{}_{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Workspace {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}

#[test]
fn presentation_preferences_round_trip_without_persisting_live_state() {
    let workspace = Workspace::new();
    let mut state = SidebarState::default();
    state.load(&workspace.0).unwrap();
    assert!(
        !workspace.0.join(".hex").exists(),
        "loading defaults is read-only"
    );
    state.area.set(Rect::new(80, 3, 38, 30));
    state.selected_section = 2;
    assert!(state.settings_key(KeyCode::Char(',')));
    assert_eq!(state.preferences.sections[1].section, Section::Changed);
    assert!(state.settings_key(KeyCode::Char('c')));
    state.selected_section = 3;
    assert!(state.settings_key(KeyCode::Char(' ')));
    assert!(state.settings_key(KeyCode::Char('+')));
    state.scroll.set(12);
    state.show_all_changed = true;
    state.show_settings = true;
    state.resizing = true;
    state.save().unwrap();

    let mut restored = SidebarState::default();
    restored.load(&workspace.0).unwrap();
    assert_eq!(restored.preferences, state.preferences);
    assert_eq!(restored.preferences.width, Some(40));
    assert_eq!(restored.scroll.get(), 0);
    assert!(!restored.show_all_changed && !restored.show_settings && !restored.resizing);
    let files = std::fs::read_dir(workspace.0.join(".hex/data"))
        .unwrap()
        .count();
    assert_eq!(files, 1, "atomic save leaves no scratch file");
}

#[test]
fn workspaces_keep_independent_sidebar_preferences() {
    let first = Workspace::new();
    let second = Workspace::new();
    let mut state = SidebarState::default();
    state.load(&first.0).unwrap();
    state.resize_to(60);
    state.preferences.sections[0].visible = false;
    state.save().unwrap();
    let mut other = SidebarState::default();
    other.load(&second.0).unwrap();
    assert_eq!(other.preferences, SidebarPreferences::default());
    other.resize_to(32);
    other.save().unwrap();
    state.load(&first.0).unwrap();
    assert_eq!(state.preferences.width, Some(60));
    assert!(!state.preferences.sections[0].visible);
}

#[test]
fn malformed_preferences_are_not_overwritten() {
    let workspace = Workspace::new();
    std::fs::create_dir_all(workspace.0.join(".hex/data")).unwrap();
    let path = workspace.0.join(".hex/data/tui-sidebar.json");
    for text in [
        "not json".to_string(),
        " ".repeat(64 * 1024 + 1),
        r#"{"width":4,"sections":[]}"#.to_string(),
        serde_json::to_string(&SidebarPreferences {
            width: None,
            sections: vec![SidebarPreferences::default().sections[0].clone(); 4],
        })
        .unwrap(),
    ] {
        std::fs::write(&path, &text).unwrap();
        let mut state = SidebarState::default();
        assert!(state.load(&workspace.0).is_err());
        state.resize_to(44);
        state.save().unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), text);
    }
}

#[test]
fn save_failure_surfaces_without_destroying_existing_state() {
    let workspace = Workspace::new();
    let mut state = SidebarState::default();
    state.load(&workspace.0).unwrap();
    state.save().unwrap();
    let path = workspace.0.join(".hex/data/tui-sidebar.json");
    std::fs::remove_file(&path).unwrap();
    std::fs::create_dir(&path).unwrap();
    state.resize_to(44);
    assert!(state.save().is_err());
    assert!(path.is_dir());
    assert_eq!(
        std::fs::read_dir(workspace.0.join(".hex/data"))
            .unwrap()
            .count(),
        1
    );
}

#[test]
fn sidebar_scrolling_and_width_remain_bounded() {
    let mut state = SidebarState::default();
    state.metrics.set((100, 20));
    state.scroll_by(200);
    assert_eq!(state.scroll.get(), 80);
    state.scroll_by(-200);
    assert_eq!(state.scroll.get(), 0);
    state.metrics.set((2, 20));
    state.scroll.set(9);
    state.scroll_by(0);
    assert_eq!(state.scroll.get(), 0);
    for viewport in [58, 88, 90, 118, 239] {
        for width in [1, 28, 44, 96, u16::MAX] {
            state.resize_to(width);
            let actual = state.width_for(viewport);
            if viewport < 90 {
                assert_eq!(actual, 0);
            } else {
                assert!((28..=96).contains(&actual));
                assert!(viewport - actual - 1 >= 40);
            }
        }
    }
}

#[test]
fn keyboard_settings_keep_selected_section_through_reordering() {
    let mut state = SidebarState::default();
    state.show_settings = true;
    assert!(!state.settings_key(KeyCode::Char(',')));
    assert!(state.settings_key(KeyCode::Char('.')));
    assert_eq!(state.selected_section, 1);
    assert_eq!(state.preferences.sections[1].section, Section::Context);
    assert!(state.settings_key(KeyCode::Char(' ')));
    assert!(!state.preferences.sections[1].visible);
    assert!(state.settings_key(KeyCode::Char('c')));
    assert!(state.preferences.sections[1].collapsed);
    assert!(state.settings_key(KeyCode::Char('r')));
    assert_eq!(state.preferences, SidebarPreferences::default());
    assert_eq!(state.selected_section, 0);
    state.settings_key(KeyCode::Esc);
    assert!(!state.show_settings);
}

#[test]
fn clicks_only_activate_the_painted_target() {
    let mut state = SidebarState::default();
    state
        .hit_targets
        .borrow_mut()
        .push((Rect::new(80, 14, 20, 1), SidebarAction::ToggleChanges));
    assert!(!state.click(79, 14));
    assert!(!state.show_all_changed);
    state.click(84, 14);
    assert!(state.show_all_changed);
    state.hit_targets.borrow_mut().push((
        Rect::new(80, 3, 20, 1),
        SidebarAction::ToggleSection(Section::Plan),
    ));
    assert!(state.click(90, 3));
    assert!(state.preferences.sections[1].collapsed);
    state.clear_geometry();
    state.click(84, 14);
    assert!(state.show_all_changed);
}

#[cfg(unix)]
#[test]
fn symlinked_preferences_cannot_read_or_write_another_workspace() {
    let workspace = Workspace::new();
    let foreign = Workspace::new();
    let target = foreign.0.join("private.json");
    std::fs::write(&target, "private").unwrap();
    std::fs::create_dir_all(workspace.0.join(".hex/data")).unwrap();
    std::os::unix::fs::symlink(&target, workspace.0.join(".hex/data/tui-sidebar.json")).unwrap();
    let mut state = SidebarState::default();
    assert!(state.load(&workspace.0).is_err());
    state.save().unwrap();
    assert_eq!(std::fs::read_to_string(target).unwrap(), "private");
}

//! Native macOS application menu bar configuration for HEX Desktop.
//!
//! Provides the standard macOS menu structure matching Apple HIG and Hermes Desktop:
//! - App Menu: About, Preferences (⌘,), Hide (⌘H), Quit (⌘Q)
//! - File Menu: New Chat (⌘N), Close Window (⌘W)
//! - Edit Menu: Undo (⌘Z), Redo (⇧⌘Z), Cut (⌘X), Copy (⌘C), Paste (⌘V), Select All (⌘A)
//! - View Menu: Toggle Sidebar (⌘B), Command Palette (⌘K), Actual Size (⌘0), Zoom In (⌘+), Zoom Out (⌘-)

use gpui::*;

use crate::shortcuts::{
    About, ActualSize, CloseWindow, Copy, Cut, Hide, NewChat, OpenSettings, Paste, Quit, Redo,
    SelectAll, ToggleCommandPalette, ToggleSidebar, Undo, ZoomIn, ZoomOut,
};

/// Constructs the full macOS application menu hierarchy.
pub fn application_menus() -> Vec<Menu> {
    vec![
        Menu {
            name: "HEX Desktop".into(),
            items: vec![
                MenuItem::action("About HEX Desktop", About),
                MenuItem::separator(),
                MenuItem::action("Preferences...", OpenSettings),
                MenuItem::separator(),
                MenuItem::action("Hide HEX", Hide),
                MenuItem::action("Quit HEX", Quit),
            ],
        },
        Menu {
            name: "File".into(),
            items: vec![
                MenuItem::action("New Chat", NewChat),
                MenuItem::separator(),
                MenuItem::action("Close Window", CloseWindow),
            ],
        },
        Menu {
            name: "Edit".into(),
            items: vec![
                MenuItem::action("Undo", Undo),
                MenuItem::action("Redo", Redo),
                MenuItem::separator(),
                MenuItem::action("Cut", Cut),
                MenuItem::action("Copy", Copy),
                MenuItem::action("Paste", Paste),
                MenuItem::action("Select All", SelectAll),
            ],
        },
        Menu {
            name: "View".into(),
            items: vec![
                MenuItem::action("Toggle Sidebar", ToggleSidebar),
                MenuItem::action("Command Palette...", ToggleCommandPalette),
                MenuItem::separator(),
                MenuItem::action("Actual Size", ActualSize),
                MenuItem::action("Zoom In", ZoomIn),
                MenuItem::action("Zoom Out", ZoomOut),
            ],
        },
    ]
}

/// Configure and attach application menus on the GPUI platform.
pub fn init_menus(cx: &App) {
    cx.set_menus(application_menus());
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_application_menus_structure() {
        let menus = application_menus();
        assert_eq!(menus.len(), 4);

        assert_eq!(menus[0].name.as_ref(), "HEX Desktop");
        assert_eq!(menus[1].name.as_ref(), "File");
        assert_eq!(menus[2].name.as_ref(), "Edit");
        assert_eq!(menus[3].name.as_ref(), "View");

        // View menu contains Toggle Sidebar and Command Palette
        let view_items = &menus[3].items;
        assert!(view_items.len() >= 6);
    }
}

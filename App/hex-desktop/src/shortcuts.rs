//! Global keyboard shortcuts and GPUI actions for HEX Desktop.
//!
//! `secondary` is the platform primary modifier: Command on macOS, Ctrl on
//! Windows and Linux. Bindings that only make sense on one platform are added
//! per target.

use gpui::*;

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

/// Returns the standard set of keyboard bindings for the HEX Desktop Cockpit.
pub fn desktop_keybindings() -> Vec<KeyBinding> {
    let mut bindings = vec![
        KeyBinding::new("secondary-k", ToggleCommandPalette, None),
        KeyBinding::new("secondary-n", NewChat, None),
        KeyBinding::new("secondary-enter", SubmitPrompt, None),
        KeyBinding::new("secondary-b", ToggleSidebar, None),
        KeyBinding::new("secondary-,", OpenSettings, None),
        KeyBinding::new("escape", CancelOrDismiss, None),
        KeyBinding::new("secondary-z", Undo, None),
        KeyBinding::new("secondary-shift-z", Redo, None),
        KeyBinding::new("secondary-x", Cut, None),
        KeyBinding::new("secondary-c", Copy, None),
        KeyBinding::new("secondary-v", Paste, None),
        KeyBinding::new("secondary-a", SelectAll, None),
        KeyBinding::new("secondary-w", CloseWindow, None),
    ];
    #[cfg(target_os = "macos")]
    {
        bindings.push(KeyBinding::new("secondary-q", Quit, None));
        bindings.push(KeyBinding::new("secondary-h", Hide, None));
    }
    #[cfg(target_os = "windows")]
    {
        bindings.push(KeyBinding::new("secondary-y", Redo, None));
        bindings.push(KeyBinding::new("alt-f4", CloseWindow, None));
    }
    bindings
}

/// Register desktop keyboard bindings on the GPUI application context.
pub fn init_shortcuts(cx: &mut App) {
    cx.bind_keys(desktop_keybindings());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[core::prelude::v1::test]
    fn test_desktop_keybindings_registration() {
        let bindings = desktop_keybindings();
        assert!(bindings.len() >= 6);

        // Verify key combinations exist
        let has_cmd_k = bindings.iter().any(|b| {
            format!("{:?}", b.action()).contains("ToggleCommandPalette")
        });
        assert!(has_cmd_k, "expected cmd-k binding");

        let has_cmd_n = bindings.iter().any(|b| {
            format!("{:?}", b.action()).contains("NewChat")
        });
        assert!(has_cmd_n, "expected cmd-n binding");

        let has_cmd_b = bindings.iter().any(|b| {
            format!("{:?}", b.action()).contains("ToggleSidebar")
        });
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
}

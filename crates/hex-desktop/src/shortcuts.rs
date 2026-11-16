//! Global macOS keyboard shortcuts and GPUI actions for HEX Desktop.
//!
//! Maps key combinations to actions matching macOS human interface guidelines and Hermes Desktop:
//! - `cmd-k`: Toggles Command Palette modal
//! - `cmd-n`: Starts a new chat conversation
//! - `cmd-enter`: Submits prompt from message composer
//! - `cmd-b`: Toggles left sidebar collapse
//! - `cmd-,`: Opens settings and preferences
//! - `escape`: Closes open modal / cancels active prompt generation

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
    ActualSize,
    ZoomIn,
    ZoomOut,
);

/// Returns the standard set of keyboard bindings for the HEX Desktop Cockpit.
pub fn desktop_keybindings() -> Vec<KeyBinding> {
    vec![
        KeyBinding::new("cmd-k", ToggleCommandPalette, None),
        KeyBinding::new("cmd-n", NewChat, None),
        KeyBinding::new("cmd-enter", SubmitPrompt, None),
        KeyBinding::new("cmd-b", ToggleSidebar, None),
        KeyBinding::new("cmd-,", OpenSettings, None),
        KeyBinding::new("escape", CancelOrDismiss, None),
        KeyBinding::new("cmd-z", Undo, None),
        KeyBinding::new("cmd-shift-z", Redo, None),
        KeyBinding::new("cmd-x", Cut, None),
        KeyBinding::new("cmd-c", Copy, None),
        KeyBinding::new("cmd-v", Paste, None),
        KeyBinding::new("cmd-a", SelectAll, None),
        KeyBinding::new("cmd-w", CloseWindow, None),
        KeyBinding::new("cmd-q", Quit, None),
        KeyBinding::new("cmd-h", Hide, None),
        KeyBinding::new("cmd-0", ActualSize, None),
        KeyBinding::new("cmd-=", ZoomIn, None),
        KeyBinding::new("cmd-+", ZoomIn, None),
        KeyBinding::new("cmd--", ZoomOut, None),
    ]
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
    }
}

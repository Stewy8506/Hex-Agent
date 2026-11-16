//! The bindings this client actually handles, in one table.
//!
//! Footers, start-screen badges and the help modal all render from here, and
//! `main.rs` tests drive every entry through the real key handler, so a hint
//! cannot outlive its binding.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    Global,
    Session,
    Start,
    Palette,
    Approval,
}

impl Scope {
    pub fn heading(self) -> &'static str {
        match self {
            Scope::Global => "Anywhere",
            Scope::Session => "Cockpit session",
            Scope::Start => "Start screen",
            Scope::Palette => "Command palette",
            Scope::Approval => "Operator approvals",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct Binding {
    pub key: KeyCode,
    pub ctrl: bool,
    pub label: &'static str,
    pub action: &'static str,
    pub scope: Scope,
    /// Shown in the one-line footer / badge row for this scope.
    pub footer: bool,
}

impl Binding {
    pub fn event(self) -> KeyEvent {
        let modifiers = if self.ctrl { KeyModifiers::CONTROL } else { KeyModifiers::NONE };
        KeyEvent::new(self.key, modifiers)
    }

    pub fn hint(self) -> String {
        format!("{} {}", self.label, self.action)
    }
}

pub const BINDINGS: &[Binding] = &[
    Binding { key: KeyCode::Char('c'), ctrl: true, label: "ctrl+c", action: "cancel turn, then quit", scope: Scope::Global, footer: false },
    Binding { key: KeyCode::F(1), ctrl: false, label: "f1", action: "help", scope: Scope::Global, footer: false },
    Binding { key: KeyCode::F(2), ctrl: false, label: "f2", action: "diff pane", scope: Scope::Global, footer: false },
    Binding { key: KeyCode::F(3), ctrl: false, label: "f3", action: "cycle mode", scope: Scope::Global, footer: false },
    Binding { key: KeyCode::Char('p'), ctrl: true, label: "ctrl+p", action: "commands", scope: Scope::Session, footer: true },
    Binding { key: KeyCode::Tab, ctrl: false, label: "tab", action: "mode", scope: Scope::Session, footer: true },
    Binding { key: KeyCode::Esc, ctrl: false, label: "esc", action: "interrupt", scope: Scope::Session, footer: true },
    Binding { key: KeyCode::Enter, ctrl: false, label: "enter", action: "send", scope: Scope::Session, footer: false },
    Binding { key: KeyCode::PageUp, ctrl: false, label: "pgup", action: "scroll up", scope: Scope::Session, footer: false },
    Binding { key: KeyCode::PageDown, ctrl: false, label: "pgdn", action: "scroll down", scope: Scope::Session, footer: false },
    Binding { key: KeyCode::Up, ctrl: false, label: "up", action: "history / scroll", scope: Scope::Session, footer: false },
    Binding { key: KeyCode::Down, ctrl: false, label: "down", action: "history / scroll", scope: Scope::Session, footer: false },
    Binding { key: KeyCode::Char('/'), ctrl: false, label: "/", action: "commands", scope: Scope::Start, footer: true },
    Binding { key: KeyCode::Enter, ctrl: false, label: "enter", action: "resume", scope: Scope::Start, footer: true },
    Binding { key: KeyCode::Char('d'), ctrl: false, label: "d", action: "delete", scope: Scope::Start, footer: true },
    Binding { key: KeyCode::F(1), ctrl: false, label: "f1", action: "help", scope: Scope::Start, footer: true },
    Binding { key: KeyCode::Enter, ctrl: false, label: "enter", action: "select", scope: Scope::Palette, footer: true },
    Binding { key: KeyCode::Esc, ctrl: false, label: "esc", action: "close", scope: Scope::Palette, footer: true },
    Binding { key: KeyCode::Up, ctrl: false, label: "up", action: "previous", scope: Scope::Palette, footer: false },
    Binding { key: KeyCode::Down, ctrl: false, label: "down", action: "next", scope: Scope::Palette, footer: false },
    Binding { key: KeyCode::Tab, ctrl: false, label: "tab", action: "next", scope: Scope::Palette, footer: false },
    Binding { key: KeyCode::Backspace, ctrl: false, label: "backspace", action: "edit query", scope: Scope::Palette, footer: false },
    Binding { key: KeyCode::Up, ctrl: false, label: "up/down", action: "select", scope: Scope::Approval, footer: true },
    Binding { key: KeyCode::Down, ctrl: false, label: "down", action: "select", scope: Scope::Approval, footer: false },
    Binding { key: KeyCode::Enter, ctrl: false, label: "enter", action: "confirm", scope: Scope::Approval, footer: true },
    Binding { key: KeyCode::Char('y'), ctrl: false, label: "y", action: "approve once", scope: Scope::Approval, footer: false },
    Binding { key: KeyCode::Char('a'), ctrl: false, label: "a", action: "allow in project", scope: Scope::Approval, footer: false },
    Binding { key: KeyCode::Char('n'), ctrl: false, label: "n", action: "deny", scope: Scope::Approval, footer: false },
    Binding { key: KeyCode::Char('d'), ctrl: false, label: "d", action: "diff", scope: Scope::Approval, footer: false },
    Binding { key: KeyCode::Esc, ctrl: false, label: "esc", action: "deny", scope: Scope::Approval, footer: true },
];

/// One-line hint string for a footer or badge row.
pub fn footer_hints(scope: Scope) -> String {
    BINDINGS
        .iter()
        .filter(|b| b.scope == scope && b.footer)
        .map(|b| b.hint())
        .collect::<Vec<_>>()
        .join(" · ")
}

/// Badges for the start screen, in hover-index order.
pub fn start_badges() -> Vec<&'static Binding> {
    BINDINGS.iter().filter(|b| b.scope == Scope::Start && b.footer).collect()
}

pub fn help_sections() -> Vec<(Scope, Vec<&'static Binding>)> {
    [Scope::Global, Scope::Session, Scope::Start, Scope::Palette, Scope::Approval]
        .iter()
        .map(|scope| (*scope, BINDINGS.iter().filter(|b| b.scope == *scope).collect()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn labels_describe_the_key_they_send() {
        for b in BINDINGS {
            let event = b.event();
            assert_eq!(event.modifiers.contains(KeyModifiers::CONTROL), b.ctrl);
            assert_eq!(event.code, b.key);
            if b.ctrl {
                assert!(b.label.starts_with("ctrl+"), "{}", b.label);
            }
            assert!(!b.action.is_empty());
        }
    }

    #[test]
    fn every_scope_has_a_footer_and_no_duplicate_keys() {
        for scope in [Scope::Session, Scope::Start, Scope::Palette, Scope::Approval] {
            let hints = footer_hints(scope);
            assert!(!hints.is_empty(), "{scope:?} has no footer hints");
            let mut keys: Vec<(KeyCode, bool)> = BINDINGS
                .iter()
                .filter(|b| b.scope == scope)
                .map(|b| (b.key, b.ctrl))
                .collect();
            let before = keys.len();
            keys.sort_by_key(|(k, c)| (format!("{k:?}"), *c));
            keys.dedup();
            assert_eq!(keys.len(), before, "{scope:?} advertises a key twice");
        }
    }

    #[test]
    fn start_badges_match_the_hover_slots() {
        assert_eq!(start_badges().len(), 4, "app.shortcut_hover_levels has four slots");
    }

    #[test]
    fn help_covers_every_binding() {
        let listed: usize = help_sections().iter().map(|(_, rows)| rows.len()).sum();
        assert_eq!(listed, BINDINGS.len());
    }
}
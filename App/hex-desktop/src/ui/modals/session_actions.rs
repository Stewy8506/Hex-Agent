//! Session management modals for HEX Desktop (DX2):
//! - `SessionActionModal`: per-session actions (rename / pin / archive /
//!   delete) opened from a sidebar row's "more" affordance; rename uses the
//!   same hand-rolled text entry as the account modal.
//! - `SessionSearchModal`: live-filtered chat search driving
//!   `SessionListView::search_query` / `filter_sessions`.

use chrono::{DateTime, Utc};
use gpui::prelude::FluentBuilder;
use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::modals::text_input::{
    backspace as text_backspace, button, delete as text_delete, insert_text, move_horizontal,
    move_to as text_move_to, move_to_edge, select_all, selected_text, ButtonTone,
    ModalClickHandler,
};
use crate::ui::modals::{modal_press, ModalMotion};
use crate::ui::session_list::SessionItem;

/// One entry of the session action menu.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionMenuAction {
    Rename,
    TogglePin,
    ToggleArchive,
    Delete,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionActionInteraction {
    Choose(SessionMenuAction),
    FocusRename,
    SubmitRename,
    CancelRename,
    ConfirmDelete,
    CancelDelete,
}

pub type SessionActionHandler = std::rc::Rc<
    dyn Fn(SessionActionInteraction, &ClickEvent, &mut Window, &mut App),
>;

const MENU: [SessionMenuAction; 4] = [
    SessionMenuAction::Rename,
    SessionMenuAction::TogglePin,
    SessionMenuAction::ToggleArchive,
    SessionMenuAction::Delete,
];

const MENU_PRESS_KEYS: [&str; 4] = [
    "session_action_rename_press",
    "session_action_pin_press",
    "session_action_archive_press",
    "session_action_delete_press",
];

/// What the operator confirmed; executed by `ChatView` against the runtime.
#[derive(Debug, Clone, PartialEq)]
pub enum SessionActionOutcome {
    Rename(String),
    Delete,
    TogglePin(bool),
    ToggleArchive(bool),
}

#[derive(Debug, Clone, PartialEq)]
pub enum SessionActionMode {
    Menu { selected: usize },
    Rename,
    DeleteConfirm,
}

/// Modal state for one session's action flow.
#[derive(Debug, Clone, PartialEq)]
pub struct SessionActionModal {
    pub session_id: String,
    pub session_title: String,
    pub is_pinned: bool,
    pub is_archived: bool,
    pub mode: SessionActionMode,
    pub draft: String,
    pub draft_cursor: usize,
    pub draft_anchor: usize,
    pub confirm_focused: bool,
    pub busy: bool,
    pub error: Option<String>,
}

impl SessionActionModal {
    pub fn open_menu(
        session_id: impl Into<String>,
        session_title: impl Into<String>,
        is_pinned: bool,
        is_archived: bool,
    ) -> Self {
        Self {
            session_id: session_id.into(),
            session_title: session_title.into(),
            is_pinned,
            is_archived,
            mode: SessionActionMode::Menu { selected: 0 },
            draft: String::new(),
            draft_cursor: 0,
            draft_anchor: 0,
            confirm_focused: true,
            busy: false,
            error: None,
        }
    }

    pub fn push_char(&mut self, text: &str) {
        if self.busy || !matches!(self.mode, SessionActionMode::Rename) {
            return;
        }
        insert_text(
            &mut self.draft,
            &mut self.draft_cursor,
            &mut self.draft_anchor,
            text,
        );
    }

    pub fn backspace(&mut self) {
        if self.busy || !matches!(self.mode, SessionActionMode::Rename) {
            return;
        }
        text_backspace(&mut self.draft, &mut self.draft_cursor, &mut self.draft_anchor);
    }

    pub fn delete(&mut self) {
        if self.busy || !matches!(self.mode, SessionActionMode::Rename) {
            return;
        }
        text_delete(&mut self.draft, &mut self.draft_cursor, &mut self.draft_anchor);
    }

    pub fn move_cursor(&mut self, delta: isize, selecting: bool) {
        if !matches!(self.mode, SessionActionMode::Rename) {
            return;
        }
        move_horizontal(
            &self.draft,
            &mut self.draft_cursor,
            &mut self.draft_anchor,
            delta,
            selecting,
        );
    }

    pub fn move_cursor_to(&mut self, offset: usize, selecting: bool) {
        if !matches!(self.mode, SessionActionMode::Rename) {
            return;
        }
        text_move_to(
            &self.draft,
            &mut self.draft_cursor,
            &mut self.draft_anchor,
            offset,
            selecting,
        );
    }

    pub fn move_cursor_to_edge(&mut self, end: bool, selecting: bool) {
        if !matches!(self.mode, SessionActionMode::Rename) {
            return;
        }
        move_to_edge(
            &self.draft,
            &mut self.draft_cursor,
            &mut self.draft_anchor,
            end,
            selecting,
        );
    }

    pub fn select_all_text(&mut self) {
        if !matches!(self.mode, SessionActionMode::Rename) {
            return;
        }
        select_all(&self.draft, &mut self.draft_cursor, &mut self.draft_anchor);
    }

    pub fn selected_text(&self) -> Option<String> {
        matches!(self.mode, SessionActionMode::Rename)
            .then(|| selected_text(&self.draft, self.draft_cursor, self.draft_anchor))
    }

    pub fn accepts_text_edit(&self) -> bool {
        !self.busy && matches!(self.mode, SessionActionMode::Rename)
    }

    fn move_selection(&mut self, delta: isize) {
        let SessionActionMode::Menu { selected } = &mut self.mode else {
            return;
        };
        let count = MENU.len() as isize;
        let next = (*selected as isize + delta).rem_euclid(count);
        *selected = next as usize;
    }

    /// Non-printable keys; printable characters and backspace route through
    /// `push_char`/`backspace` from the key-down listener.
    pub fn handle_key(&mut self, key: &str) -> Option<SessionActionOutcome> {
        if self.busy {
            return None;
        }
        match key {
            "escape" => None,
            "tab" | "shift-tab" | "left" | "arrowleft" | "right" | "arrowright"
                if matches!(self.mode, SessionActionMode::DeleteConfirm) =>
            {
                self.confirm_focused = !self.confirm_focused;
                None
            }
            "up" | "arrowup" => {
                self.move_selection(-1);
                None
            }
            "down" | "arrowdown" => {
                self.move_selection(1);
                None
            }
            "enter" => match &self.mode {
                SessionActionMode::Menu { selected } => self.activate_menu(MENU[*selected]),
                SessionActionMode::Rename => {
                    let title = self.draft.trim().to_string();
                    if title.is_empty() {
                        return None;
                    }
                    self.busy = true;
                    Some(SessionActionOutcome::Rename(title))
                }
                SessionActionMode::DeleteConfirm => {
                    if !self.confirm_focused {
                        self.mode = SessionActionMode::Menu { selected: 3 };
                        return None;
                    }
                    self.busy = true;
                    Some(SessionActionOutcome::Delete)
                }
            },
            _ => None,
        }
    }

    pub fn handle_interaction(
        &mut self,
        interaction: SessionActionInteraction,
    ) -> Option<SessionActionOutcome> {
        if self.busy {
            return None;
        }
        match interaction {
            SessionActionInteraction::Choose(action) => self.activate_menu(action),
            SessionActionInteraction::FocusRename
                if matches!(self.mode, SessionActionMode::Rename) =>
            {
                None
            }
            SessionActionInteraction::SubmitRename
                if matches!(self.mode, SessionActionMode::Rename) =>
            {
                let title = self.draft.trim().to_string();
                if title.is_empty() {
                    return None;
                }
                self.busy = true;
                Some(SessionActionOutcome::Rename(title))
            }
            SessionActionInteraction::CancelRename
                if matches!(self.mode, SessionActionMode::Rename) =>
            {
                self.mode = SessionActionMode::Menu { selected: 0 };
                None
            }
            SessionActionInteraction::ConfirmDelete
                if matches!(self.mode, SessionActionMode::DeleteConfirm) =>
            {
                self.busy = true;
                Some(SessionActionOutcome::Delete)
            }
            SessionActionInteraction::CancelDelete
                if matches!(self.mode, SessionActionMode::DeleteConfirm) =>
            {
                self.mode = SessionActionMode::Menu { selected: 3 };
                None
            }
            _ => None,
        }
    }

    pub fn activate_menu(&mut self, action: SessionMenuAction) -> Option<SessionActionOutcome> {
        if self.busy || !matches!(self.mode, SessionActionMode::Menu { .. }) {
            return None;
        }
        match action {
            SessionMenuAction::Rename => {
                self.mode = SessionActionMode::Rename;
                self.draft = self.session_title.clone();
                self.draft_cursor = self.draft.len();
                self.draft_anchor = self.draft_cursor;
                None
            }
            SessionMenuAction::TogglePin => {
                let target = !self.is_pinned;
                self.busy = true;
                Some(SessionActionOutcome::TogglePin(target))
            }
            SessionMenuAction::ToggleArchive => {
                let target = !self.is_archived;
                self.busy = true;
                Some(SessionActionOutcome::ToggleArchive(target))
            }
            SessionMenuAction::Delete => {
                self.mode = SessionActionMode::DeleteConfirm;
                self.confirm_focused = true;
                None
            }
        }
    }

    fn title(&self) -> &'static str {
        match self.mode {
            SessionActionMode::Menu { .. } => "Chat actions",
            SessionActionMode::Rename => "Rename chat",
            SessionActionMode::DeleteConfirm => "Delete chat?",
        }
    }

    fn menu_label(&self, action: SessionMenuAction) -> String {
        match action {
            SessionMenuAction::Rename => "Rename chat".to_string(),
            SessionMenuAction::TogglePin => {
                if self.is_pinned {
                    "Unpin chat".to_string()
                } else {
                    "Pin chat".to_string()
                }
            }
            SessionMenuAction::ToggleArchive => {
                if self.is_archived {
                    "Unarchive chat".to_string()
                } else {
                    "Archive chat".to_string()
                }
            }
            SessionMenuAction::Delete => "Delete chat…".to_string(),
        }
    }

    fn click_handler(
        on_action: &SessionActionHandler,
        interaction: SessionActionInteraction,
    ) -> ModalClickHandler {
        let on_action = on_action.clone();
        std::rc::Rc::new(move |event, window, cx| on_action(interaction, event, window, cx))
    }

    /// Renders the modal card content for the current mode.
    pub fn render(
        &self,
        theme: &HermesTheme,
        motion: ModalMotion<'_>,
        on_action: SessionActionHandler,
    ) -> Div {
        let mut card = div()
            .w(px(crate::ui::modals::MODAL_CARD_WIDTH_WIDE))
            .flex()
            .flex_col()
            .gap(px(12.0))
            .px(px(20.0))
            .py(px(18.0))
            .child(
                div()
                    .text_size(px(MockupTypeScale::FEATURE_LABEL_SIZE))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.mockup_text)
                    .child(self.title()),
            )
            .child(
                div()
                    .text_size(px(MockupTypeScale::BASE_SIZE))
                    .text_color(theme.mockup_text_dim)
                    .overflow_hidden()
                    .whitespace_nowrap()
                    .child(self.session_title.clone()),
            );

        match &self.mode {
            SessionActionMode::Menu { selected } => {
                for (index, action) in MENU.iter().enumerate() {
                    let is_selected = *selected == index;
                    let label = self.menu_label(*action);
                    let mut row = div()
                            .id(SharedString::from(format!("session_menu_{}", index)))
                            .debug_selector(move || format!("session_menu_{index}"))
                            .w_full()
                            .flex()
                            .flex_row()
                            .items_center()
                            .justify_between()
                            .px(px(12.0))
                            .py(px(8.0))
                            .rounded(px(8.0))
                            .cursor_pointer()
                            .on_click({
                                let on_action = on_action.clone();
                                move |event, window, cx| {
                                    on_action(
                                        SessionActionInteraction::Choose(*action),
                                        event,
                                        window,
                                        cx,
                                    )
                                }
                            })
                            .when(is_selected, |c| c.bg(theme.mockup_hover))
                            .when(!is_selected, |c| c.hover(|s| s.bg(theme.mockup_hover)))
                            .text_size(px(MockupTypeScale::BASE_SIZE))
                            .when(is_selected, |c| c.text_color(theme.mockup_text))
                            .when(!is_selected, |c| {
                                c.text_color(theme.mockup_session_inactive)
                            })
                            .when(
                                *action == SessionMenuAction::Delete,
                                |c| c.text_color(theme.rose_fg()),
                            )
                            .child(label);
                    if is_selected {
                        row = modal_press(row, MENU_PRESS_KEYS[index], motion);
                    }
                    card = card.child(row);
                }
            }
            SessionActionMode::Rename => {
                card = card
                    .child(crate::ui::modals::text_input::text_field(
                        "Chat title",
                        &self.draft,
                        self.draft_cursor,
                        self.draft_anchor,
                        "New title",
                        false,
                        true,
                        self.busy,
                        theme,
                        Some(Self::click_handler(
                            &on_action,
                            SessionActionInteraction::FocusRename,
                        )),
                    ))
                    .child(
                        div()
                            .flex()
                            .justify_end()
                            .gap_2()
                            .child(button(
                                "Cancel",
                                ButtonTone::Neutral,
                                !self.busy,
                                false,
                                theme,
                                Self::click_handler(
                                    &on_action,
                                    SessionActionInteraction::CancelRename,
                                ),
                            ))
                            .child(button(
                                "Rename",
                                ButtonTone::Accent,
                                !self.busy && !self.draft.trim().is_empty(),
                                true,
                                theme,
                                Self::click_handler(
                                    &on_action,
                                    SessionActionInteraction::SubmitRename,
                                ),
                            )),
                    );
            }
            SessionActionMode::DeleteConfirm => {
                card = card
                    .child(
                        div()
                            .text_size(px(MockupTypeScale::BASE_SIZE))
                            .text_color(theme.mockup_text_dim)
                            .child(
                                "This permanently removes the transcript from this workspace. \
                                 This cannot be undone.",
                            ),
                    )
                    .child(
                        div()
                            .flex()
                            .justify_end()
                            .gap_2()
                            .child(button(
                                "Cancel",
                                ButtonTone::Neutral,
                                true,
                                !self.confirm_focused,
                                theme,
                                Self::click_handler(
                                    &on_action,
                                    SessionActionInteraction::CancelDelete,
                                ),
                            ))
                            .child(button(
                                "Delete forever",
                                ButtonTone::Destructive,
                                true,
                                self.confirm_focused,
                                theme,
                                Self::click_handler(
                                    &on_action,
                                    SessionActionInteraction::ConfirmDelete,
                                ),
                            )),
                    );
            }
        }

        if let Some(error) = &self.error {
            card = card.child(
                div()
                    .text_size(px(MockupTypeScale::HINT_SIZE))
                    .text_color(theme.rose_fg())
                    .child(error.clone()),
            );
        }

        let hint = match self.mode {
            SessionActionMode::Menu { .. } => "↑↓ — choose · Enter — apply · Esc — close",
            SessionActionMode::Rename => "Enter — rename · Esc — close",
            SessionActionMode::DeleteConfirm => "Tab — choose · Enter — confirm · Esc — cancel",
        };
        card = card.child(
            div()
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(MockupTypeScale::HINT_SIZE))
                .text_color(theme.mockup_text_dim)
                .child(hint),
        );
        card
    }
}

/// Live chat-search modal. Results are provided by `ChatView` from
/// `SessionListView::filter_sessions` at render/key time (the modal owns only
/// query text and the highlighted row).
#[derive(Debug, Clone)]
pub struct SessionSearchModal {
    pub query: String,
    pub cursor: usize,
    pub anchor: usize,
    pub selected: usize,
    scroll_handle: UniformListScrollHandle,
}

impl PartialEq for SessionSearchModal {
    fn eq(&self, other: &Self) -> bool {
        self.query == other.query
            && self.cursor == other.cursor
            && self.anchor == other.anchor
            && self.selected == other.selected
    }
}

impl Default for SessionSearchModal {
    fn default() -> Self {
        Self::new("")
    }
}

impl SessionSearchModal {
    pub fn new(initial: impl Into<String>) -> Self {
        let query = initial.into();
        let cursor = query.len();
        Self {
            query,
            cursor,
            anchor: cursor,
            selected: 0,
            scroll_handle: UniformListScrollHandle::new(),
        }
    }

    pub fn set_query(&mut self, query: impl Into<String>) {
        self.query = query.into();
        self.cursor = self.query.len();
        self.anchor = self.cursor;
        self.selected = 0;
        self.scroll_handle
            .scroll_to_item_strict(0, ScrollStrategy::Top);
    }

    pub fn push_char(&mut self, text: &str) {
        insert_text(&mut self.query, &mut self.cursor, &mut self.anchor, text);
        self.selected = 0;
        self.scroll_handle
            .scroll_to_item_strict(0, ScrollStrategy::Top);
    }

    pub fn backspace(&mut self) {
        if text_backspace(&mut self.query, &mut self.cursor, &mut self.anchor) {
            self.selected = 0;
            self.scroll_handle
                .scroll_to_item_strict(0, ScrollStrategy::Top);
        }
    }

    pub fn delete(&mut self) {
        if text_delete(&mut self.query, &mut self.cursor, &mut self.anchor) {
            self.selected = 0;
            self.scroll_handle
                .scroll_to_item_strict(0, ScrollStrategy::Top);
        }
    }

    pub fn move_cursor(&mut self, delta: isize, selecting: bool) {
        move_horizontal(
            &self.query,
            &mut self.cursor,
            &mut self.anchor,
            delta,
            selecting,
        );
    }

    pub fn move_cursor_to(&mut self, offset: usize, selecting: bool) {
        text_move_to(
            &self.query,
            &mut self.cursor,
            &mut self.anchor,
            offset,
            selecting,
        );
    }

    pub fn move_cursor_to_edge(&mut self, end: bool, selecting: bool) {
        move_to_edge(
            &self.query,
            &mut self.cursor,
            &mut self.anchor,
            end,
            selecting,
        );
    }

    pub fn select_all_text(&mut self) {
        select_all(&self.query, &mut self.cursor, &mut self.anchor);
    }

    pub fn selected_text(&self) -> String {
        selected_text(&self.query, self.cursor, self.anchor)
    }

    pub fn move_selection(&mut self, delta: isize, result_count: usize) {
        if result_count == 0 {
            return;
        }
        let count = result_count as isize;
        let next = (self.selected as isize + delta).rem_euclid(count);
        self.selected = next as usize;
        self.scroll_handle
            .scroll_to_item(self.selected, ScrollStrategy::Center);
    }

    pub fn render(
        &self,
        theme: &HermesTheme,
        results: &[&SessionItem],
        now: DateTime<Utc>,
        on_open: std::rc::Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)>,
        motion: ModalMotion<'_>,
    ) -> Div {
        let mut card = div()
            .w(px(520.0))
            .max_w_full()
            .flex()
            .flex_col()
            .gap(px(10.0))
            .px(px(20.0))
            .py(px(18.0))
            .child(
                div()
                    .text_size(px(MockupTypeScale::FEATURE_LABEL_SIZE))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.mockup_text)
                    .child("Search chats"),
            )
            .child(crate::ui::modals::text_input::text_field(
                "Search",
                &self.query,
                self.cursor,
                self.anchor,
                "Type to filter chats…",
                false,
                true,
                false,
                theme,
                None,
            ));

        if results.is_empty() {
            card = card.child(
                div()
                    .h(px(120.0))
                    .flex()
                    .items_center()
                    .justify_center()
                    .text_size(px(MockupTypeScale::BASE_SIZE))
                    .text_color(theme.mockup_text_dim)
                    .child("No chats match this search."),
            );
        } else {
            let sessions: Vec<SessionItem> = results.iter().map(|session| (*session).clone()).collect();
            let selected = self.selected;
            let scroll_handle = self.scroll_handle.clone();
            let theme = theme.clone();
            let list = uniform_list(
                "session_search_rows",
                sessions.len(),
                move |range, _window, cx| {
                    let motion = ModalMotion {
                        owner: motion.owner,
                        cx,
                    };
                    range
                        .map(|index| {
                            render_search_row(
                                &sessions[index],
                                index == selected,
                                now,
                                &theme,
                                motion,
                                on_open.clone(),
                            )
                            .into_any_element()
                        })
                        .collect::<Vec<_>>()
                },
            )
            .track_scroll(scroll_handle)
            .w_full()
            .h_full();
            card = card.child(
                div()
                    .debug_selector(|| "session_search_list".to_string())
                    .h(px(320.0))
                    .w_full()
                    .overflow_hidden()
                    .child(list),
            );
        }

        card.child(
            div()
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(MockupTypeScale::HINT_SIZE))
                .text_color(theme.mockup_text_dim)
                .child("↑↓ — choose · Enter — open · Esc — close"),
        )
    }
}

fn render_search_row(
    session: &SessionItem,
    selected: bool,
    now: DateTime<Utc>,
    theme: &HermesTheme,
    motion: ModalMotion<'_>,
    on_open: std::rc::Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)>,
) -> Stateful<Div> {
    let id = session.id.clone();
    let on_open_id = id.clone();
    let mut row = div()
        .id(SharedString::from(format!("session_search_{id}")))
        .h(px(44.0))
        .w_full()
        .flex()
        .flex_row()
        .items_center()
        .justify_between()
        .gap(px(12.0))
        .px(px(12.0))
        .rounded(px(8.0))
        .cursor_pointer()
        .text_size(px(MockupTypeScale::BASE_SIZE))
        .when(selected, |this| this.bg(theme.mockup_hover))
        .when(!selected, |this| this.hover(|this| this.bg(theme.mockup_hover)))
        .when(selected, |this| this.text_color(theme.mockup_text))
        .when(!selected, |this| this.text_color(theme.mockup_session_inactive))
        .on_click(move |event, window, cx| on_open(on_open_id.clone(), event, window, cx))
        .child(
            div()
                .flex_1()
                .min_w_0()
                .truncate()
                .child(session.title.clone()),
        )
        .child(
            div()
                .flex_none()
                .text_size(px(MockupTypeScale::HINT_SIZE))
                .text_color(theme.mockup_text_dim)
                .child(session.relative_time(now)),
        );
    if selected {
        row = modal_press(row, "session_search_selected_press", motion);
    }
    row
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    fn menu_modal() -> SessionActionModal {
        SessionActionModal::open_menu("ses_1", "My chat", false, false)
    }

    #[test]
    fn test_menu_navigation_and_outcomes() {
        let mut modal = menu_modal();
        assert!(matches!(modal.mode, SessionActionMode::Menu { selected: 0 }));

        // Rename entry switches to the draft mode pre-filled with the title.
        modal.handle_key("enter");
        assert!(matches!(modal.mode, SessionActionMode::Rename));
        assert_eq!(modal.draft, "My chat");

        // Toggle pin from a fresh menu (menu order: Rename, Pin, Archive, Delete).
        let mut modal = menu_modal();
        modal.handle_key("down");
        assert_eq!(
            modal.handle_key("enter"),
            Some(SessionActionOutcome::TogglePin(true))
        );

        // Archive from a fresh menu.
        let mut modal = menu_modal();
        modal.handle_key("down");
        modal.handle_key("down");
        assert_eq!(
            modal.handle_key("enter"),
            Some(SessionActionOutcome::ToggleArchive(true))
        );

        // Delete entry arms the confirm stage, Enter confirms.
        let mut modal = menu_modal();
        for _ in 0..3 {
            modal.handle_key("down");
        }
        modal.handle_key("enter");
        assert!(matches!(modal.mode, SessionActionMode::DeleteConfirm));
        assert_eq!(modal.handle_key("enter"), Some(SessionActionOutcome::Delete));
    }

    #[test]
    fn test_rename_requires_non_empty_title() {
        let mut modal = menu_modal();
        modal.handle_key("enter"); // -> Rename
        modal.draft.clear();
        assert_eq!(modal.handle_key("enter"), None);
        modal.push_char("N");
        modal.push_char("e");
        modal.push_char("w");
        modal.backspace();
        assert_eq!(modal.draft, "Ne");
        assert_eq!(
            modal.handle_key("enter"),
            Some(SessionActionOutcome::Rename("Ne".to_string()))
        );
    }

    #[test]
    fn test_search_selection_wraps() {
        let mut modal = SessionSearchModal::new("bug");
        modal.push_char("f");
        assert_eq!(modal.query, "bugf");
        modal.move_selection(1, 3);
        assert_eq!(modal.selected, 1);
        modal.move_selection(1, 3);
        assert_eq!(modal.selected, 2);
        modal.move_selection(1, 3);
        assert_eq!(modal.selected, 0);
        modal.move_selection(-1, 3);
        assert_eq!(modal.selected, 2);
        // Zero results never moves.
        modal.move_selection(1, 0);
        assert_eq!(modal.selected, 2);
        modal.backspace();
        assert_eq!(modal.query, "bug");
    }
}

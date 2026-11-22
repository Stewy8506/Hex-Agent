//! Session list navigation and relative timestamp formatting for HEX Desktop.
//!
//! Mirrors Hermes Desktop session list with grouping by:
//! - "Today"
//! - "Yesterday"
//! - "Previous 7 Days"
//! - "Older"
//!
//! Includes relative time formatting ("just now", "12m ago", "3h ago", "2d ago"),
//! pinned sessions, selection indicators, and context actions.

use std::rc::Rc;

use chrono::{DateTime, Duration, Utc};
use gpui::*;

use crate::theme::HermesTheme;

/// Formats a timestamp into a human-readable relative time string.
///
/// Matches Hermes Desktop:
/// - `< 60s`: "just now"
/// - `< 60m`: "{m}m ago"
/// - `< 24h`: "{h}h ago"
/// - `< 7d`: "Yesterday" (for 1d) or "{d}d ago"
/// - `>= 7d`: "MMM d" (e.g., "Nov 12")
pub fn format_relative_time(target: DateTime<Utc>, now: DateTime<Utc>) -> String {
    let diff = now.signed_duration_since(target);
    let seconds = diff.num_seconds();

    if seconds < 60 {
        "just now".to_string()
    } else if seconds < 3600 {
        let mins = seconds / 60;
        format!("{}m ago", mins)
    } else if seconds < 86400 {
        let hours = seconds / 3600;
        format!("{}h ago", hours)
    } else if seconds < 86400 * 7 {
        let days = seconds / 86400;
        if days == 1 {
            "Yesterday".to_string()
        } else {
            format!("{}d ago", days)
        }
    } else {
        target.format("%b %-d").to_string()
    }
}

/// Compact Continue-list format (mockup, no "ago"):
/// - `< 60s`: "now"
/// - `< 60m`: "{m}m"
/// - `< 24h`: "{h}h"
/// - `< 7d`: "{d}d"
/// - `>= 7d`: "MMM d" (e.g., "Nov 6")
pub fn format_compact_relative_time(target: DateTime<Utc>, now: DateTime<Utc>) -> String {
    let seconds = now.signed_duration_since(target).num_seconds();

    if seconds < 60 {
        "now".to_string()
    } else if seconds < 3600 {
        format!("{}m", seconds / 60)
    } else if seconds < 86400 {
        format!("{}h", seconds / 3600)
    } else if seconds < 86400 * 7 {
        format!("{}d", seconds / 86400)
    } else {
        target.format("%b %-d").to_string()
    }
}

/// Orders one project's sessions for the sidebar: pinned sessions first,
/// then most recently updated. Keeps pin logic while the mockup groups by
/// project instead of date.
pub fn sort_project_sessions<'a>(sessions: &'a [SessionItem]) -> Vec<&'a SessionItem> {
    let mut ordered: Vec<&SessionItem> = sessions.iter().collect();
    ordered.sort_by(|a, b| {
        b.is_pinned
            .cmp(&a.is_pinned)
            .then_with(|| b.updated_at.cmp(&a.updated_at))
    });
    ordered
}

/// Date grouping category for recent sessions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum SessionGroupCategory {
    Today,
    Yesterday,
    Previous7Days,
    Older,
}

impl SessionGroupCategory {
    pub fn label(&self) -> &'static str {
        match self {
            SessionGroupCategory::Today => "Today",
            SessionGroupCategory::Yesterday => "Yesterday",
            SessionGroupCategory::Previous7Days => "Previous 7 Days",
            SessionGroupCategory::Older => "Older",
        }
    }
}

/// Individual conversation session item.
#[derive(Debug, Clone, PartialEq)]
pub struct SessionItem {
    pub id: String,
    pub title: String,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub is_pinned: bool,
    pub is_archived: bool,
    pub message_count: usize,
    pub model_name: Option<String>,
}

impl SessionItem {
    pub fn new(id: impl Into<String>, title: impl Into<String>) -> Self {
        let now = Utc::now();
        Self {
            id: id.into(),
            title: title.into(),
            created_at: now,
            updated_at: now,
            is_pinned: false,
            is_archived: false,
            message_count: 0,
            model_name: None,
        }
    }

    pub fn with_timestamps(
        id: impl Into<String>,
        title: impl Into<String>,
        created_at: DateTime<Utc>,
        updated_at: DateTime<Utc>,
    ) -> Self {
        Self {
            id: id.into(),
            title: title.into(),
            created_at,
            updated_at,
            is_pinned: false,
            is_archived: false,
            message_count: 0,
            model_name: None,
        }
    }

    pub fn with_pinned(mut self, pinned: bool) -> Self {
        self.is_pinned = pinned;
        self
    }

    pub fn with_model(mut self, model: impl Into<String>) -> Self {
        self.model_name = Some(model.into());
        self
    }
}

/// Classifies a target timestamp into a date group relative to `now`.
pub fn classify_session_date(target: DateTime<Utc>, now: DateTime<Utc>) -> SessionGroupCategory {
    let target_date = target.date_naive();
    let now_date = now.date_naive();

    if target_date == now_date {
        SessionGroupCategory::Today
    } else if target_date == now_date - Duration::days(1) {
        SessionGroupCategory::Yesterday
    } else if now.signed_duration_since(target).num_days() < 7 {
        SessionGroupCategory::Previous7Days
    } else {
        SessionGroupCategory::Older
    }
}

/// State and logic for session history list navigation.
#[derive(Debug, Clone, PartialEq)]
pub struct SessionListView {
    pub sessions: Vec<SessionItem>,
    pub selected_session_id: Option<String>,
    pub search_query: String,
}

impl Default for SessionListView {
    fn default() -> Self {
        Self::new()
    }
}

impl SessionListView {
    pub fn new() -> Self {
        Self {
            sessions: Vec::new(),
            selected_session_id: None,
            search_query: String::new(),
        }
    }

    pub fn with_sessions(sessions: Vec<SessionItem>) -> Self {
        let selected = sessions.first().map(|s| s.id.clone());
        Self {
            sessions,
            selected_session_id: selected,
            search_query: String::new(),
        }
    }

    pub fn add_session(&mut self, session: SessionItem) {
        if let Some(pos) = self.sessions.iter().position(|s| s.id == session.id) {
            self.sessions[pos] = session;
        } else {
            self.sessions.insert(0, session);
        }
    }

    pub fn select_session(&mut self, id: impl Into<String>) {
        let id_str = id.into();
        if self.sessions.iter().any(|s| s.id == id_str) {
            self.selected_session_id = Some(id_str);
        }
    }

    pub fn selected_session(&self) -> Option<&SessionItem> {
        let id = self.selected_session_id.as_ref()?;
        self.sessions.iter().find(|s| &s.id == id)
    }

    pub fn set_search_query(&mut self, query: impl Into<String>) {
        self.search_query = query.into();
    }

    pub fn filter_sessions(&self) -> Vec<&SessionItem> {
        let q = self.search_query.trim().to_lowercase();
        self.sessions
            .iter()
            .filter(|s| !s.is_archived)
            .filter(|s| {
                if q.is_empty() {
                    true
                } else {
                    s.title.to_lowercase().contains(&q)
                }
            })
            .collect()
    }

    pub fn pinned_sessions(&self) -> Vec<&SessionItem> {
        self.filter_sessions()
            .into_iter()
            .filter(|s| s.is_pinned)
            .collect()
    }

    pub fn unpinned_sessions(&self) -> Vec<&SessionItem> {
        self.filter_sessions()
            .into_iter()
            .filter(|s| !s.is_pinned)
            .collect()
    }

    /// Groups unpinned active sessions into chronological date categories.
    pub fn grouped_recent_sessions(
        &self,
        now: DateTime<Utc>,
    ) -> Vec<(SessionGroupCategory, Vec<&SessionItem>)> {
        let unpinned = self.unpinned_sessions();
        let mut today = Vec::new();
        let mut yesterday = Vec::new();
        let mut prev_7_days = Vec::new();
        let mut older = Vec::new();

        for session in unpinned {
            match classify_session_date(session.updated_at, now) {
                SessionGroupCategory::Today => today.push(session),
                SessionGroupCategory::Yesterday => yesterday.push(session),
                SessionGroupCategory::Previous7Days => prev_7_days.push(session),
                SessionGroupCategory::Older => older.push(session),
            }
        }

        let mut groups = Vec::new();
        if !today.is_empty() {
            groups.push((SessionGroupCategory::Today, today));
        }
        if !yesterday.is_empty() {
            groups.push((SessionGroupCategory::Yesterday, yesterday));
        }
        if !prev_7_days.is_empty() {
            groups.push((SessionGroupCategory::Previous7Days, prev_7_days));
        }
        if !older.is_empty() {
            groups.push((SessionGroupCategory::Older, older));
        }
        groups
    }

    pub fn toggle_pin(&mut self, id: &str) -> bool {
        if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
            session.is_pinned = !session.is_pinned;
            true
        } else {
            false
        }
    }

    pub fn rename_session(&mut self, id: &str, new_title: impl Into<String>) -> bool {
        if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
            session.title = new_title.into();
            session.updated_at = Utc::now();
            true
        } else {
            false
        }
    }

    pub fn archive_session(&mut self, id: &str) -> bool {
        if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
            session.is_archived = true;
            true
        } else {
            false
        }
    }

    pub fn delete_session(&mut self, id: &str) -> bool {
        let initial_len = self.sessions.len();
        self.sessions.retain(|s| s.id != id);
        if self.selected_session_id.as_deref() == Some(id) {
            self.selected_session_id = self.sessions.first().map(|s| s.id.clone());
        }
        self.sessions.len() < initial_len
    }

    /// Renders a single session row item.
    fn render_session_row(
        &self,
        session: &SessionItem,
        is_selected: bool,
        theme: &HermesTheme,
        now: DateTime<Utc>,
        on_select: Option<&Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)>>,
    ) -> Stateful<Div> {
        let rel_time = format_relative_time(session.updated_at, now);
        let bg_color = if is_selected {
            theme.mockup_row_hover
        } else {
            rgb(0x00000000) // transparent
        };

        let mut row = div()
            .id(SharedString::from(session.id.clone()))
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .justify_between()
            .px_2()
            .py_1()
            .rounded_md()
            .bg(bg_color)
            .cursor_pointer()
            .hover(|s| s.bg(theme.mockup_row_hover));

        // Active indicator on the left
        if is_selected {
            row = row.border_l_2().border_color(theme.accent_blue());
        }

        if let Some(on_select) = on_select {
            let id = session.id.clone();
            let on_select = on_select.clone();
            row = row.on_click(move |event, window, cx| {
                on_select(id.clone(), event, window, cx);
            });
        }

        row.child(
            div()
                .flex()
                .flex_col()
                .gap(px(2.0))
                .overflow_hidden()
                .child(
                    div()
                        .text_xs()
                        .font_weight(if is_selected {
                            FontWeight::SEMIBOLD
                        } else {
                            FontWeight::NORMAL
                        })
                        .text_color(if is_selected {
                            theme.mockup_text_bright
                        } else {
                            theme.mockup_session_inactive
                        })
                        .child(session.title.clone()),
                )
                .child(
                    div()
                        .text_xs()
                        .text_color(theme.mockup_text_dim)
                        .child(rel_time),
                ),
        )
    }

    /// Renders the complete session list view into a GPUI Div element.
    pub fn render(&self, theme: &HermesTheme, now: DateTime<Utc>) -> Div {
        self.render_with_select(theme, now, None)
    }

    /// Renders the session list with an optional row-click handler receiving
    /// the session id. `ChatView` uses this for real session switching.
    pub fn render_with_select(
        &self,
        theme: &HermesTheme,
        now: DateTime<Utc>,
        on_select: Option<Rc<dyn Fn(String, &ClickEvent, &mut Window, &mut App)>>,
    ) -> Div {
        let mut list_container = div()
            .w_full()
            .flex()
            .flex_col()
            .gap_2()
            .overflow_y_hidden();

        // 1. Pinned Sessions (if any)
        let pinned = self.pinned_sessions();
        if !pinned.is_empty() {
            let mut pin_section = div().w_full().flex().flex_col().gap_1();
            pin_section = pin_section.child(
                div()
                    .px_2()
                    .py(px(2.0))
                    .text_xs()
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.mockup_text_dim)
                    .child("PINNED"),
            );

            for session in pinned {
                let is_selected = self.selected_session_id.as_deref() == Some(&session.id);
                pin_section = pin_section.child(self.render_session_row(
                    session,
                    is_selected,
                    theme,
                    now,
                    on_select.as_ref(),
                ));
            }
            list_container = list_container.child(pin_section);
        }

        // 2. Grouped Recent Sessions
        let groups = self.grouped_recent_sessions(now);
        for (category, sessions) in groups {
            let mut group_section = div().w_full().flex().flex_col().gap_1();
            group_section = group_section.child(
                div()
                    .px_2()
                    .py(px(2.0))
                    .text_xs()
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.mockup_text_dim)
                    .child(category.label()),
            );

            for session in sessions {
                let is_selected = self.selected_session_id.as_deref() == Some(&session.id);
                group_section = group_section.child(self.render_session_row(
                    session,
                    is_selected,
                    theme,
                    now,
                    on_select.as_ref(),
                ));
            }
            list_container = list_container.child(group_section);
        }

        list_container
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;
    use core::prelude::v1::test;

    #[test]
    fn test_relative_time_formatting() {
        let now = Utc.with_ymd_and_hms(2026, 11, 16, 12, 0, 0).unwrap();

        // 30 seconds ago -> "just now"
        let t_30s = now - Duration::seconds(30);
        assert_eq!(format_relative_time(t_30s, now), "just now");

        // 12 minutes ago -> "12m ago"
        let t_12m = now - Duration::minutes(12);
        assert_eq!(format_relative_time(t_12m, now), "12m ago");

        // 3 hours ago -> "3h ago"
        let t_3h = now - Duration::hours(3);
        assert_eq!(format_relative_time(t_3h, now), "3h ago");

        // 1 day ago -> "Yesterday"
        let t_1d = now - Duration::days(1);
        assert_eq!(format_relative_time(t_1d, now), "Yesterday");

        // 3 days ago -> "3d ago"
        let t_3d = now - Duration::days(3);
        assert_eq!(format_relative_time(t_3d, now), "3d ago");

        // 10 days ago -> "Nov 6"
        let t_10d = now - Duration::days(10);
        assert_eq!(format_relative_time(t_10d, now), "Nov 6");
    }

    #[test]
    fn test_compact_relative_time_formatting() {
        let now = Utc.with_ymd_and_hms(2026, 11, 16, 12, 0, 0).unwrap();

        // 30 seconds ago -> "now"
        let t_30s = now - Duration::seconds(30);
        assert_eq!(format_compact_relative_time(t_30s, now), "now");

        // 5 minutes ago -> "5m"
        let t_5m = now - Duration::minutes(5);
        assert_eq!(format_compact_relative_time(t_5m, now), "5m");

        // 2 hours ago -> "2h"
        let t_2h = now - Duration::hours(2);
        assert_eq!(format_compact_relative_time(t_2h, now), "2h");

        // 3 days ago -> "3d"
        let t_3d = now - Duration::days(3);
        assert_eq!(format_compact_relative_time(t_3d, now), "3d");

        // 10 days ago -> "Nov 6"
        let t_10d = now - Duration::days(10);
        assert_eq!(format_compact_relative_time(t_10d, now), "Nov 6");
    }

    #[test]
    fn test_sort_project_sessions_pinned_first_then_recency() {
        let now = Utc.with_ymd_and_hms(2026, 11, 16, 12, 0, 0).unwrap();
        let old = SessionItem::with_timestamps("old", "Old", now - Duration::days(2), now - Duration::days(2));
        let recent = SessionItem::with_timestamps("recent", "Recent", now - Duration::hours(1), now - Duration::hours(1));
        let pinned = SessionItem::with_timestamps("pinned", "Pinned", now - Duration::days(1), now - Duration::days(1))
            .with_pinned(true);

        let sessions = [old, recent, pinned];
        let ordered = sort_project_sessions(&sessions);
        let ids: Vec<&str> = ordered.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, ["pinned", "recent", "old"]);
    }

    #[test]
    fn test_session_date_classification() {
        let now = Utc.with_ymd_and_hms(2026, 11, 16, 12, 0, 0).unwrap();

        let today = now - Duration::hours(2);
        assert_eq!(
            classify_session_date(today, now),
            SessionGroupCategory::Today
        );

        let yesterday = now - Duration::days(1);
        assert_eq!(
            classify_session_date(yesterday, now),
            SessionGroupCategory::Yesterday
        );

        let four_days = now - Duration::days(4);
        assert_eq!(
            classify_session_date(four_days, now),
            SessionGroupCategory::Previous7Days
        );

        let ten_days = now - Duration::days(10);
        assert_eq!(
            classify_session_date(ten_days, now),
            SessionGroupCategory::Older
        );
    }

    #[test]
    fn test_session_list_operations_and_filtering() {
        let now = Utc.with_ymd_and_hms(2026, 11, 16, 12, 0, 0).unwrap();

        let s1 = SessionItem::with_timestamps("s1", "Refactor Compiler", now - Duration::minutes(5), now - Duration::minutes(5)).with_pinned(true);
        let s2 = SessionItem::with_timestamps("s2", "Bugfix parser", now - Duration::hours(2), now - Duration::hours(2));
        let s3 = SessionItem::with_timestamps("s3", "Setup database", now - Duration::days(1), now - Duration::days(1));

        let mut list = SessionListView::with_sessions(vec![s1, s2, s3]);
        assert_eq!(list.selected_session_id.as_deref(), Some("s1"));
        assert_eq!(list.pinned_sessions().len(), 1);
        assert_eq!(list.unpinned_sessions().len(), 2);

        // Grouping
        let groups = list.grouped_recent_sessions(now);
        assert_eq!(groups.len(), 2); // Today and Yesterday
        assert_eq!(groups[0].0, SessionGroupCategory::Today);
        assert_eq!(groups[0].1.len(), 1); // s2
        assert_eq!(groups[1].0, SessionGroupCategory::Yesterday);
        assert_eq!(groups[1].1.len(), 1); // s3

        // Search filtering
        list.set_search_query("parser");
        let filtered = list.filter_sessions();
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].id, "s2");
        list.set_search_query("");

        // Pin toggle
        assert!(list.toggle_pin("s2"));
        assert_eq!(list.pinned_sessions().len(), 2);

        // Rename
        assert!(list.rename_session("s2", "Parser AST overhaul"));
        assert_eq!(list.sessions[1].title, "Parser AST overhaul");

        // Delete
        assert!(list.delete_session("s1"));
        assert_eq!(list.sessions.len(), 2);
    }
}

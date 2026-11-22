//! Home / start screen building blocks for HEX Desktop (Phase 2).
//!
//! Mirrors the mockup's `DesktopStart.dc.html`: an ASCII logo, the pulsating
//! glowing prompt composer card (reusing `glow_card`), a "Continue" list of
//! recent sessions, and a bottom feature dock whose Automations, Team,
//! Notebooks, Design and Extensions entries are labelled Coming soon.

use gpui::*;

use crate::theme::HermesTheme;

/// The mockup's ASCII wordmark (56px mono block art).
pub const ASCII_LOGO: &str = "█ ▀ ██▀▀ ▀▄▀\n█▀█ ██▄ ▄▀▄";

/// Coming-soon dock entries, in the mockup's left-to-right order.
pub const COMING_SOON_DOCK: [(&str, &str); 5] = [
    ("Notebooks", "notebooks"),
    ("Design", "design"),
    ("Extensions", "extensions"),
    ("Automations", "bolt"),
    ("Team", "team"),
];

/// Renders the ASCII logo block, centered.
pub fn render_logo(theme: &HermesTheme) -> Div {
    div()
        .flex()
        .flex_col()
        .items_center()
        .mb(px(46.0))
        .child(
            div()
                .font_family("Geist Mono")
                .text_size(px(56.0))
                .line_height(px(56.0))
                .font_weight(FontWeight::MEDIUM)
                .text_color(theme.mockup_text_bright)
                .whitespace_nowrap()
                .child(ASCII_LOGO),
        )
}

/// A minimal one-line tooltip view returned by `home_tooltip`.
pub struct LabelTooltip {
    label: SharedString,
}

impl Render for LabelTooltip {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        let theme = HermesTheme::default();
        div()
            .bg(theme.mockup_surface)
            .border_1()
            .border_color(theme.mockup_border)
            .rounded(px(8.0))
            .px(px(10.0))
            .py(px(5.0))
            .text_size(px(12.0))
            .text_color(theme.mockup_text)
            .child(self.label.clone())
    }
}

/// Builds the dock item tooltip view (gpui 0.2 tooltip closure target).
pub fn home_tooltip(label: &'static str, _window: &mut Window, cx: &mut App) -> AnyView {
    cx.new(|_| LabelTooltip {
        label: SharedString::from(label),
    })
    .into()
}

/// One dock icon button with a tooltip label; clicking surfaces a Coming soon
/// notice via the provided `on_click` (the features are not implemented).
pub fn render_dock_item(
    theme: &HermesTheme,
    id: &'static str,
    label: &'static str,
    glyph: &'static str,
) -> Stateful<Div> {
    div()
        .id(SharedString::from(id))
        .cursor_pointer()
        .size(px(42.0))
        .flex()
        .items_center()
        .justify_center()
        .rounded(px(12.0))
        .text_sm()
        .text_color(theme.mockup_text_dim)
        .hover(|s| {
            s.bg(theme.mockup_hover)
                .text_color(theme.mockup_text)
        })
        .tooltip(move |window, cx| crate::ui::home_tooltip(label, window, cx))
        .child(glyph)
}

/// A "Continue" session row: title, relative time and a location chip.
pub struct ContinueRow {
    pub id: String,
    pub title: String,
    pub relative_time: String,
    pub location_chip: &'static str,
}

/// Renders one Continue row; clicking opens the session via `on_open`.
pub fn render_continue_row(
    theme: &HermesTheme,
    row: &ContinueRow,
    on_open: impl Fn(&ClickEvent, &mut Window, &mut App) + 'static,
) -> Stateful<Div> {
    div()
        .id(SharedString::from(format!("continue_{}", row.id)))
        .cursor_pointer()
        .w_full()
        .flex()
        .flex_row()
        .items_center()
        .gap(px(12.0))
        .px(px(14.0))
        .py(px(7.0))
        .rounded(px(10.0))
        .hover(|s| s.bg(theme.mockup_hover))
        .on_click(move |event, window, cx| on_open(event, window, cx))
        .child(
            div()
                .flex_1()
                .min_w_0()
                .truncate()
                .text_color(theme.mockup_text)
                .child(row.title.clone()),
        )
        .child(
            div()
                .text_size(px(11.5))
                .text_color(theme.mockup_text_dim)
                .bg(theme.mockup_chip)
                .rounded(px(6.0))
                .px_2()
                .child(row.location_chip),
        )
        .child(
            div()
                .text_size(px(12.0))
                .text_color(theme.mockup_text_dim)
                .min_w(px(34.0))
                .text_right()
                .child(row.relative_time.clone()),
        )
}

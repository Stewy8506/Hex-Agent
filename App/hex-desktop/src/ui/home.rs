//! Home / start screen building blocks for HEX Desktop (Phase 2).
//!
//! Mirrors the mockup's `DesktopStart.dc.html`: the h60 top bar (panel toggle,
//! search / bell / settings icon buttons), the ASCII logo with its `rise`
//! entrance, the pulsating glowing prompt composer card (reusing `glow_card`),
//! the "Continue" list of recent sessions with running/pending-review state,
//! and the bottom feature dock with exact `.dk` tooltips (Notebooks, Design,
//! Extensions, Automations and Team are not built yet).

use gpui::prelude::FluentBuilder;
use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::motion::{self, ControlMotion};

/// The mockup's ASCII wordmark (56px mono block art), byte-exact.
pub const ASCII_LOGO: &str = "█ █ █▀▀ ▀▄▀\n█▀█ ██▄ ▄▀▄";

/// `rise` translateY start offset in px (settles to 0).
pub const RISE_OFFSET: f32 = 6.0;

/// Top bar: h60, padding 0 20px.
pub const TOP_BAR_HEIGHT: f32 = 60.0;
pub const TOP_BAR_PADDING_X: f32 = 20.0;
/// Header icon-button SVG size (mockup `.ib` svgs are 16px).
pub const TOP_BAR_ICON_SIZE: f32 = 16.0;

/// Center column bottom padding (flex-1 centered content sits 96px up).
pub const CENTER_PADDING_BOTTOM: f32 = 96.0;

/// Continue block: w600, margin-top 48; `.sl` header padding 0 14px 4px 33px.
pub const CONTINUE_WIDTH: f32 = 600.0;
pub const CONTINUE_MARGIN_TOP: f32 = 48.0;
pub const CONTINUE_HEADER_PAD_LEFT: f32 = 33.0;
pub const CONTINUE_HEADER_PAD_BOTTOM: f32 = 4.0;

/// Continue `.bt.row` inline overrides: padding 7px 14px, gap 12, radius 10.
pub const CONTINUE_ROW_PADDING_X: f32 = 14.0;
pub const CONTINUE_ROW_PADDING_Y: f32 = 7.0;
pub const CONTINUE_ROW_GAP: f32 = 12.0;
/// 7px leading dot slot (empty, or pulsing accent dot when running).
pub const CONTINUE_DOT_SIZE: f32 = 7.0;
/// Project column: Geist Mono 12.
pub const CONTINUE_PROJECT_SIZE: f32 = 12.0;
/// Time column: 13px, w34 right-aligned.
pub const CONTINUE_TIME_SIZE: f32 = 13.0;
pub const CONTINUE_TIME_WIDTH: f32 = 34.0;

/// Dock: absolute bottom 28, container radius 16, padding 5, gap 2; items
/// 42×42 radius 12 with 18px icons.
pub const DOCK_BOTTOM: f32 = 28.0;
pub const DOCK_RADIUS: f32 = 16.0;
pub const DOCK_PADDING: f32 = 5.0;
pub const DOCK_GAP: f32 = 2.0;
pub const DOCK_ITEM_SIZE: f32 = 42.0;
pub const DOCK_ITEM_RADIUS: f32 = 12.0;
pub const DOCK_ICON_SIZE: f32 = 18.0;
/// Coming-soon notice bottom offset (clears the 52px dock plus spacing).
pub const NOTICE_BOTTOM: f32 = 92.0;

/// Coming-soon dock entries, in the mockup's left-to-right order. The second
/// element is the icon asset stem rendered via `dock_icon_path`.
pub const COMING_SOON_DOCK: [(&str, &str); 5] = [
    ("Notebooks", "notebooks"),
    ("Design", "design"),
    ("Extensions", "extensions"),
    ("Automations", "bolt"),
    ("Team", "team"),
];

/// SVG asset path for a dock item's icon stem.
pub fn dock_icon_path(glyph: &str) -> String {
    format!("icons/{glyph}.svg")
}

/// Renders the ASCII logo block with the mockup's `rise` entrance (0.9s
/// cubic-bezier(.2,.7,.2,1), opacity 0→1 and a 6px→0 upward settle simulated
/// via margin-top), centered. Skipped entirely under reduced motion.
pub fn render_logo(theme: &HermesTheme) -> AnyElement {
    let logo = div().flex().flex_col().items_center().mb(px(46.0)).child(
        div()
            .font_family(theme.font_mono()[0].clone())
            .text_size(px(MockupTypeScale::LOGO_SIZE))
            .line_height(px(MockupTypeScale::LOGO_LINE_HEIGHT))
            .font_weight(MockupTypeScale::LOGO_WEIGHT)
            .text_color(theme.mockup_text_bright)
            .whitespace_nowrap()
            .child(ASCII_LOGO),
    );
    if motion::reduced_motion() {
        logo.into_any_element()
    } else {
        logo.with_animation(
            "home_logo_rise",
            Animation::new(std::time::Duration::from_secs_f32(motion::SMOOTH.duration)),
            |el, phase| {
                let progress = motion::spring_easing(motion::SMOOTH, phase);
                el.opacity(progress).mt(px(RISE_OFFSET * (1.0 - progress)))
            },
        )
        .into_any_element()
    }
}

/// A minimal one-line tooltip view returned by `home_tooltip`, styled exactly
/// like the mockup's `.dk` tooltips: bg `#262626`, 12.5/20 `#e6e6e6`,
/// padding 2px 10px, radius 6, nowrap.
pub struct LabelTooltip {
    label: SharedString,
}

impl Render for LabelTooltip {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        let theme = HermesTheme::default();
        div()
            .bg(theme.mockup_seg_active)
            .rounded(px(6.0))
            .px(px(10.0))
            .py(px(2.0))
            .text_size(px(MockupTypeScale::TOOLTIP_SIZE))
            .line_height(px(MockupTypeScale::TOOLTIP_LINE_HEIGHT))
            .text_color(theme.mockup_text)
            .whitespace_nowrap()
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

/// One dock icon button (42×42, radius 12) rendering its 18px SVG asset
/// (tinted via the container text color), with the exact `.dk` tooltip; the
/// features are not implemented, so clicking surfaces a Coming soon notice
/// via the provided `on_click`.
pub fn render_dock_item(
    theme: &HermesTheme,
    id: &'static str,
    label: &'static str,
    glyph: &'static str,
    control: ControlMotion,
    cx: &App,
) -> Stateful<Div> {
    let values = control.values(cx);
    let background = values.color(hsla(0.0, 0.0, 0.0, 0.0), theme.mockup_hover.into());
    let tint = values.color(theme.mockup_text_muted.into(), theme.mockup_text.into());
    let button = div()
        .id(SharedString::from(id))
        .debug_selector(move || format!("dock_{id}"))
        .cursor_pointer()
        .size(px(DOCK_ITEM_SIZE))
        .flex()
        .items_center()
        .justify_center()
        .rounded(px(DOCK_ITEM_RADIUS))
        .bg(background)
        .tooltip(move |window, cx| crate::ui::home_tooltip(label, window, cx))
        .child(
            svg()
                .path(dock_icon_path(glyph))
                .w(px(DOCK_ICON_SIZE))
                .h(px(DOCK_ICON_SIZE))
                .flex_none()
                .text_color(tint),
        );
    motion::hover_feedback(motion::press_feedback(button, control, cx), control)
}

/// A "Continue" session row: title, project column, location chip, compact
/// relative time and the 7px leading dot slot state.
pub struct ContinueRow {
    pub id: String,
    pub title: String,
    pub relative_time: String,
    pub location_chip: &'static str,
    pub project: String,
    pub running: bool,
    pub pending_review: bool,
}

/// Chip label + accent variant for a Continue row: sessions with a pending
/// approval get the mockup's accent "Review" chip, everything else keeps the
/// honest location chip ("This Mac" — local-only execution).
pub fn continue_chip(row: &ContinueRow) -> (&'static str, bool) {
    if row.pending_review {
        ("Review", true)
    } else {
        (row.location_chip, false)
    }
}

/// Renders one Continue row; clicking opens the session via `on_open`.
pub fn render_continue_row(
    theme: &HermesTheme,
    row: &ContinueRow,
    on_open: impl Fn(&ClickEvent, &mut Window, &mut App) + 'static,
) -> Stateful<Div> {
    let (chip_label, chip_accent) = continue_chip(row);
    let dot_slot = if row.running {
        let dot = div().size_full().rounded_full().bg(theme.accent_blue);
        let animated = if motion::reduced_motion() {
            dot.into_any_element()
        } else {
            dot.with_animation(
                ElementId::Name(SharedString::from(format!("continue_pl_{}", row.id))),
                Animation::new(motion::PULSE_PERIOD).repeat(),
                |el, phase| {
                    let elapsed = std::time::Duration::from_secs_f32(
                        phase * motion::PULSE_PERIOD.as_secs_f32(),
                    );
                    el.opacity(motion::pulse_opacity(elapsed))
                },
            )
            .into_any_element()
        };
        div()
            .w(px(CONTINUE_DOT_SIZE))
            .h(px(CONTINUE_DOT_SIZE))
            .flex_none()
            .child(animated)
            .into_any_element()
    } else {
        div()
            .w(px(CONTINUE_DOT_SIZE))
            .h(px(CONTINUE_DOT_SIZE))
            .flex_none()
            .into_any_element()
    };

    div()
        .id(SharedString::from(format!("continue_{}", row.id)))
        .cursor_pointer()
        .w_full()
        .flex()
        .flex_row()
        .items_center()
        .gap(px(CONTINUE_ROW_GAP))
        .px(px(CONTINUE_ROW_PADDING_X))
        .py(px(CONTINUE_ROW_PADDING_Y))
        .rounded(px(10.0))
        .hover(|s| s.bg(theme.mockup_row_hover))
        .on_click(move |event, window, cx| on_open(event, window, cx))
        .child(dot_slot)
        .child(
            div()
                .flex_1()
                .min_w_0()
                .truncate()
                .text_size(px(MockupTypeScale::BASE_SIZE))
                .line_height(px(MockupTypeScale::BASE_LINE_HEIGHT))
                .text_color(theme.mockup_text)
                .child(row.title.clone()),
        )
        .child(
            div()
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(CONTINUE_PROJECT_SIZE))
                .text_color(theme.mockup_text_dim)
                .flex_none()
                .child(row.project.clone()),
        )
        .child(
            div()
                .flex_none()
                .text_size(px(MockupTypeScale::CHIP_SIZE))
                .line_height(px(MockupTypeScale::CHIP_LINE_HEIGHT))
                .rounded(px(6.0))
                .px_2()
                .when(chip_accent, |c| {
                    c.text_color(theme.accent_blue)
                        .bg(theme.mockup_accent_chip_bg)
                })
                .when(!chip_accent, |c| {
                    c.text_color(theme.mockup_text_muted).bg(theme.mockup_chip)
                })
                .child(chip_label),
        )
        .child(
            div()
                .text_size(px(CONTINUE_TIME_SIZE))
                .text_color(theme.mockup_text_dim)
                .w(px(CONTINUE_TIME_WIDTH))
                .flex_none()
                .text_right()
                .child(row.relative_time.clone()),
        )
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_ascii_logo_matches_mockup_exactly() {
        assert_eq!(ASCII_LOGO, "█ █ █▀▀ ▀▄▀\n█▀█ ██▄ ▄▀▄");
    }

    #[test]
    fn test_top_bar_and_center_tokens_match_mockup() {
        assert_eq!(TOP_BAR_HEIGHT, 60.0);
        assert_eq!(TOP_BAR_PADDING_X, 20.0);
        assert_eq!(TOP_BAR_ICON_SIZE, 16.0);
        assert_eq!(CENTER_PADDING_BOTTOM, 96.0);
        assert_eq!(RISE_OFFSET, 6.0);
    }

    #[test]
    fn test_continue_block_tokens_match_mockup() {
        assert_eq!(CONTINUE_WIDTH, 600.0);
        assert_eq!(CONTINUE_MARGIN_TOP, 48.0);
        assert_eq!(CONTINUE_HEADER_PAD_LEFT, 33.0);
        assert_eq!(CONTINUE_HEADER_PAD_BOTTOM, 4.0);
        assert_eq!(CONTINUE_ROW_PADDING_X, 14.0);
        assert_eq!(CONTINUE_ROW_PADDING_Y, 7.0);
        assert_eq!(CONTINUE_ROW_GAP, 12.0);
        assert_eq!(CONTINUE_DOT_SIZE, 7.0);
        assert_eq!(CONTINUE_PROJECT_SIZE, 12.0);
        assert_eq!(CONTINUE_TIME_SIZE, 13.0);
        assert_eq!(CONTINUE_TIME_WIDTH, 34.0);
    }

    #[test]
    fn test_dock_tokens_match_mockup() {
        assert_eq!(DOCK_BOTTOM, 28.0);
        assert_eq!(DOCK_RADIUS, 16.0);
        assert_eq!(DOCK_PADDING, 5.0);
        assert_eq!(DOCK_GAP, 2.0);
        assert_eq!(DOCK_ITEM_SIZE, 42.0);
        assert_eq!(DOCK_ITEM_RADIUS, 12.0);
        assert_eq!(DOCK_ICON_SIZE, 18.0);
        assert_eq!(NOTICE_BOTTOM, 92.0);
    }

    #[test]
    fn test_dock_icons_use_svg_assets_not_name_text() {
        let icons_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("assets/icons");
        for (label, glyph) in COMING_SOON_DOCK {
            assert_eq!(dock_icon_path(glyph), format!("icons/{glyph}.svg"));
            let asset = icons_dir.join(format!("{glyph}.svg"));
            assert!(
                asset.is_file(),
                "dock entry {label} must map to an SVG asset, missing {asset:?}"
            );
        }
    }

    #[test]
    fn test_continue_chip_variants() {
        let mut row = ContinueRow {
            id: "s1".into(),
            title: "Fix flaky test".into(),
            relative_time: "5m".into(),
            location_chip: "This Mac",
            project: "hex-agent".into(),
            running: false,
            pending_review: false,
        };
        assert_eq!(continue_chip(&row), ("This Mac", false));

        row.pending_review = true;
        assert_eq!(continue_chip(&row), ("Review", true));
    }

    #[test]
    fn test_tooltip_tokens_match_mockup() {
        let theme = HermesTheme::default();
        assert_eq!(MockupTypeScale::TOOLTIP_SIZE, 12.5);
        assert_eq!(MockupTypeScale::TOOLTIP_LINE_HEIGHT, 20.0);
        assert_eq!(theme.mockup_seg_active, rgb(0x262626));
        assert_eq!(theme.mockup_text, rgb(0xe6e6e6));
    }

    #[test]
    fn test_continue_row_tones_match_mockup() {
        let theme = HermesTheme::default();
        // Row hover `#1a1a1a`; default chip `#9a9a9a` on `#1d1d1d`; Review
        // accent chip `#7aa7ff` on rgba(122,167,255,.14).
        assert_eq!(theme.mockup_row_hover, rgb(0x1a1a1a));
        assert_eq!(theme.mockup_text_muted, rgb(0x9a9a9a));
        assert_eq!(theme.mockup_chip, rgb(0x1d1d1d));
        assert_eq!(theme.accent_blue, rgb(0x7aa7ff));
        assert_eq!(theme.mockup_accent_chip_bg, rgba(0x7aa7ff24));
    }
}

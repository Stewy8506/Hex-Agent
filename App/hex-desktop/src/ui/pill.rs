//! Mockup `.pill` and `.ib` control primitives for HEX Desktop.
//!
//! Mirrors the mockup's shared control classes:
//! - `.pill`: inline-flex, gap 6, `#1f1f1f`, radius 99, padding 5px 14px,
//!   14/20 `#cfcfcf`, hover `#272727`
//! - `.ib`: 32×32 radius-8 icon button, tint `#9a9a9a`; hover bg `#1e1e1e`,
//!   tint `#e6e6e6`

use gpui::*;

use crate::theme::HermesTheme;

/// Horizontal pill padding in px (`.pill` padding 5px 14px).
pub const PILL_PADDING_X: f32 = 14.0;
/// Vertical pill padding in px.
pub const PILL_PADDING_Y: f32 = 5.0;
/// Pill label size / line height (14/20).
pub const PILL_TEXT_SIZE: f32 = 14.0;
pub const PILL_LINE_HEIGHT: f32 = 20.0;
/// Gap between pill label and chevron.
pub const PILL_GAP: f32 = 6.0;
/// Chevron-down size inside pills.
pub const PILL_CHEVRON_SIZE: f32 = 12.0;
/// Icon-button (`.ib`) square size.
pub const ICON_BUTTON_SIZE: f32 = 32.0;
/// Icon-button corner radius.
pub const ICON_BUTTON_RADIUS: f32 = 8.0;

/// Mockup pill text tone (`#cfcfcf`).
pub const PILL_TEXT_HEX: u32 = 0xcfcfcf;
/// Chevron tone inside pills (`#858585`).
pub const PILL_CHEVRON_HEX: u32 = 0x858585;

/// Mockup `.pill` base: bg `#1f1f1f`, radius 99, padding 5px 14px, 14/20
/// `#cfcfcf`, hover `#272727`.
pub fn mockup_pill(theme: &HermesTheme) -> Div {
    div()
        .flex()
        .flex_row()
        .items_center()
        .gap(px(PILL_GAP))
        .rounded_full()
        .bg(theme.mockup_pill)
        .px(px(PILL_PADDING_X))
        .py(px(PILL_PADDING_Y))
        .text_size(px(PILL_TEXT_SIZE))
        .line_height(px(PILL_LINE_HEIGHT))
        .text_color(rgb(PILL_TEXT_HEX))
        .cursor_pointer()
        .hover(|s| s.bg(theme.mockup_pill_hover))
}

/// Pill chevron: bundled chevron-down SVG sized 12px in `#858585`.
pub fn mockup_pill_chevron() -> Svg {
    svg()
        .path("icons/chevron-down.svg")
        .w(px(PILL_CHEVRON_SIZE))
        .h(px(PILL_CHEVRON_SIZE))
        .flex_none()
        .text_color(rgb(0x858585))
}

/// Mockup `.ib` icon button: 32×32, radius 8, icon tinted `tint`; hover bg
/// `#1e1e1e`. `hover_tint` mirrors the mockup's inline-color buttons (e.g.
/// Attach `#cfcfcf`), whose tint survives hover because the inline color beats
/// `.ib:hover`; default-tinted buttons (`.ib` `#9a9a9a`) brighten to
/// `#e6e6e6`. The SVG inherits the container's text color so the hover tint
/// swap reaches the stroke.
pub fn mockup_icon_button(
    theme: &HermesTheme,
    icon: &str,
    icon_size: f32,
    tint: Rgba,
    hover_tint: Option<Rgba>,
) -> Div {
    let hover_bg = theme.mockup_hover;
    let hover_stroke = hover_tint.unwrap_or(theme.mockup_text);
    div()
        .group("icon_button")
        .w(px(ICON_BUTTON_SIZE))
        .h(px(ICON_BUTTON_SIZE))
        .rounded(px(ICON_BUTTON_RADIUS))
        .flex()
        .items_center()
        .justify_center()
        .cursor_pointer()
        .hover(move |s| s.bg(hover_bg))
        .child(
            // gpui does not flow container text color into svg children; the
            // icon carries its own tint and swaps it via group_hover.
            svg()
                .path(format!("icons/{icon}.svg"))
                .w(px(icon_size))
                .h(px(icon_size))
                .flex_none()
                .text_color(tint)
                .group_hover("icon_button", move |s| s.text_color(hover_stroke)),
        )
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_pill_geometry_constants_match_mockup() {
        assert_eq!(PILL_PADDING_X, 14.0);
        assert_eq!(PILL_PADDING_Y, 5.0);
        assert_eq!(PILL_TEXT_SIZE, 14.0);
        assert_eq!(PILL_LINE_HEIGHT, 20.0);
        assert_eq!(PILL_GAP, 6.0);
        assert_eq!(PILL_CHEVRON_SIZE, 12.0);
    }

    #[test]
    fn test_icon_button_geometry_constants_match_mockup() {
        assert_eq!(ICON_BUTTON_SIZE, 32.0);
        assert_eq!(ICON_BUTTON_RADIUS, 8.0);
    }

    #[test]
    fn test_pill_tones_match_mockup() {
        assert_eq!(PILL_TEXT_HEX, 0xcfcfcf);
        assert_eq!(PILL_CHEVRON_HEX, 0x858585);

        let theme = HermesTheme::default();
        // The pill backgrounds come from the shared mockup tokens.
        assert_eq!(theme.mockup_pill, rgb(0x1f1f1f));
        assert_eq!(theme.mockup_pill_hover, rgb(0x272727));
        assert_eq!(theme.mockup_hover, rgb(0x1e1e1e));
        assert_eq!(theme.mockup_text, rgb(0xe6e6e6));
    }
}

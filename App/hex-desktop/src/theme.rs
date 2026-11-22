//! Hermes Design System Tokens, Zinc Palette & Typography for HEX Desktop.
//!
//! Provides the complete Hermes visual language matching `benchmarks/hermes-agent/apps/desktop/`
//! and the HEX truecolor design tokens:
//! - Canvas Background: `#09090b` (Dark Zinc canvas)
//! - Card / Surface Background: `#18181b` (Zinc-900)
//! - Card Elevated / Hover: `#27272a` (Zinc-800)
//! - Border Low / Subtle: `#27272a`
//! - Border Mid / Structural: `#3f3f46` (Zinc-700)
//! - Primary Text: `#fafafa` (Zinc-50)
//! - Secondary / Muted Text: `#a1a1aa` (Zinc-400)
//! - Dim / Metadata Text: `#71717a` (Zinc-500)
//! - Extra Dim Text: `#52525b` (Zinc-600)
//! - Electric Blue Accent: `#7aa7ff`
//! - Sage Green / Addition FG: `#8fbf93`
//! - Sage Tint / Addition BG: `rgba(0x8fbf93, 0.12)` (#171f18)
//! - Rose Red / Deletion FG: `#c98b8b`
//! - Rose Tint / Deletion BG: `rgba(0xc98b8b, 0.10)` (#1c1616)
//! - Warning Amber: `#f59e0b`
//! - Translucent Frosted Glass: `rgba(0x18181b, 0.88)`

use gpui::*;

/// Typography scale and font family hierarchy for the Hermes Design System.
#[derive(Debug, Clone, PartialEq)]
pub struct TypographyTokens {
    /// Monospace font family stack.
    pub font_mono: Vec<SharedString>,
    /// Sans-serif system font family stack.
    pub font_sans: Vec<SharedString>,
    /// Title size in pixels (default 15px).
    pub title_size: Pixels,
    /// Title font weight (default 600 / Semibold).
    pub title_weight: FontWeight,
    /// Body size in pixels (default 13px).
    pub body_size: Pixels,
    /// Body font weight (default 400 / Normal).
    pub body_weight: FontWeight,
    /// Code size in pixels (default 12px).
    pub code_size: Pixels,
    /// Code font weight (default 400 / Normal).
    pub code_weight: FontWeight,
    /// Caption size in pixels (default 11px).
    pub caption_size: Pixels,
    /// Caption font weight (default 400 / Normal).
    pub caption_weight: FontWeight,
}

impl Default for TypographyTokens {
    fn default() -> Self {
        Self {
            font_mono: vec![
                // Mockup (DesktopStart.dc.html) typography: Geist Mono first.
                "Geist Mono".into(),
                "JetBrains Mono".into(),
                "SF Mono".into(),
                "Menlo".into(),
                "Courier New".into(),
                "monospace".into(),
            ],
            font_sans: vec![
                // Mockup (DesktopStart.dc.html) typography: Apfel Grotezk first.
                "Apfel Grotezk".into(),
                "Helvetica Neue".into(),
                "Arial".into(),
                "-apple-system".into(),
                "BlinkMacSystemFont".into(),
                "SF Pro Text".into(),
                "system-ui".into(),
                "sans-serif".into(),
            ],
            title_size: px(15.0),
            title_weight: FontWeight::SEMIBOLD,
            body_size: px(13.0),
            body_weight: FontWeight::NORMAL,
            code_size: px(12.0),
            code_weight: FontWeight::NORMAL,
            caption_size: px(11.0),
            caption_weight: FontWeight::NORMAL,
        }
    }
}

/// Hermes Design System Theme containing Dark Zinc tokens, diff tints, and typography.
#[derive(Debug, Clone, PartialEq)]
pub struct HermesTheme {
    /// Canvas background (#09090b).
    pub canvas_bg: Rgba,
    /// Surface card background (#18181b / zinc-900).
    pub card_bg: Rgba,
    /// Elevated card / hover state (#27272a / zinc-800).
    pub card_hover: Rgba,
    /// Subtle low-contrast border (#27272a).
    pub border_subtle: Rgba,
    /// Structural mid-contrast border (#3f3f46 / zinc-700).
    pub border_structural: Rgba,
    /// Primary high-contrast text (#fafafa / zinc-50).
    pub text_primary: Rgba,
    /// Secondary muted text (#a1a1aa / zinc-400).
    pub text_muted: Rgba,
    /// Dim metadata text (#71717a / zinc-500).
    pub text_dim: Rgba,
    /// Extra dim separator text (#52525b / zinc-600).
    pub text_extra_dim: Rgba,
    /// Electric Blue accent for focus and active indicators (#7aa7ff).
    pub accent_blue: Rgba,
    /// Sage green for diff additions and test passes (#8fbf93).
    pub sage_fg: Rgba,
    /// Sage background tint for addition lines (12% alpha / rgba(0x8fbf931f)).
    pub sage_bg: Rgba,
    /// Rose red for diff deletions and test failures (#c98b8b).
    pub rose_fg: Rgba,
    /// Rose background tint for deletion lines (10% alpha / rgba(0xc98b8b1a)).
    pub rose_bg: Rgba,
    /// Warning amber for alerts (#f59e0b).
    pub warning_amber: Rgba,
    /// Frosted glass translucent panel background (rgba(0x18181be0)).
    pub panel_frosted_bg: Rgba,
    /// Typography tokens.
    pub typography: TypographyTokens,
    // --- Mockup palette (docs/design/mockups-desktop) ---
    /// Window canvas (#141414).
    pub mockup_canvas: Rgba,
    /// Deep body background (#0a0a0a).
    pub mockup_body: Rgba,
    /// Sidebar background (#101010).
    pub mockup_sidebar: Rgba,
    /// Composer / card surface (#171717).
    pub mockup_surface: Rgba,
    /// Hover surface (#1e1e1e).
    pub mockup_hover: Rgba,
    /// Chip background (#1d1d1d).
    pub mockup_chip: Rgba,
    /// Subtle mockup border (#222222).
    pub mockup_border: Rgba,
    /// Structural mockup border (#262626).
    pub mockup_border_strong: Rgba,
    /// Primary mockup text (#e6e6e6).
    pub mockup_text: Rgba,
    /// Bright heading text (#f2f2f2).
    pub mockup_text_bright: Rgba,
    /// Muted mockup text (#9a9a9a).
    pub mockup_text_muted: Rgba,
    /// Dim mockup text (#858585).
    pub mockup_text_dim: Rgba,
    /// Row hover / search box background (#1a1a1a).
    pub mockup_row_hover: Rgba,
    /// Pill background (#1f1f1f).
    pub mockup_pill: Rgba,
    /// Pill hover background (#272727).
    pub mockup_pill_hover: Rgba,
    /// Dock / segmented container background (#191919).
    pub mockup_dock: Rgba,
    /// Dock / segmented container border (#232323).
    pub mockup_dock_border: Rgba,
    /// Active segmented button background (#262626).
    pub mockup_seg_active: Rgba,
    /// Send circle / context track background (#2a2a2a).
    pub mockup_track: Rgba,
    /// Non-active session row titles (#bdbdbd).
    pub mockup_session_inactive: Rgba,
    /// Assistant prose base text (#d9d9d9).
    pub mockup_text_base: Rgba,
    /// Composer card border (rgba(122,167,255,.3)).
    pub mockup_accent_border: Rgba,
    /// Review / Needs-you chip background (rgba(122,167,255,.14)).
    pub mockup_accent_chip_bg: Rgba,
    /// Added diff line text (#c4e0c7).
    pub mockup_diff_add_fg: Rgba,
    /// Added diff line tint (rgba(143,191,147,.1)).
    pub mockup_diff_add_bg: Rgba,
    /// Deleted diff line text (#d4a9a9).
    pub mockup_diff_del_fg: Rgba,
    /// Deleted diff line tint (rgba(201,139,139,.09)).
    pub mockup_diff_del_bg: Rgba,
}

impl Default for HermesTheme {
    fn default() -> Self {
        Self {
            canvas_bg: rgb(0x09090b),
            card_bg: rgb(0x18181b),
            card_hover: rgb(0x27272a),
            border_subtle: rgb(0x27272a),
            border_structural: rgb(0x3f3f46),
            text_primary: rgb(0xfafafa),
            text_muted: rgb(0xa1a1aa),
            text_dim: rgb(0x71717a),
            text_extra_dim: rgb(0x52525b),
            accent_blue: rgb(0x7aa7ff),
            sage_fg: rgb(0x8fbf93),
            sage_bg: rgba(0x8fbf931f),
            rose_fg: rgb(0xc98b8b),
            rose_bg: rgba(0xc98b8b1a),
            warning_amber: rgb(0xf59e0b),
            panel_frosted_bg: rgba(0x18181be0),
            mockup_canvas: rgb(0x141414),
            mockup_body: rgb(0x0a0a0a),
            mockup_sidebar: rgb(0x101010),
            mockup_surface: rgb(0x171717),
            mockup_hover: rgb(0x1e1e1e),
            mockup_chip: rgb(0x1d1d1d),
            mockup_border: rgb(0x222222),
            mockup_border_strong: rgb(0x262626),
            mockup_text: rgb(0xe6e6e6),
            mockup_text_bright: rgb(0xf2f2f2),
            mockup_text_muted: rgb(0x9a9a9a),
            mockup_text_dim: rgb(0x858585),
            mockup_row_hover: rgb(0x1a1a1a),
            mockup_pill: rgb(0x1f1f1f),
            mockup_pill_hover: rgb(0x272727),
            mockup_dock: rgb(0x191919),
            mockup_dock_border: rgb(0x232323),
            mockup_seg_active: rgb(0x262626),
            mockup_track: rgb(0x2a2a2a),
            mockup_session_inactive: rgb(0xbdbdbd),
            mockup_text_base: rgb(0xd9d9d9),
            mockup_accent_border: rgba(0x7aa7ff4d),
            mockup_accent_chip_bg: rgba(0x7aa7ff24),
            mockup_diff_add_fg: rgb(0xc4e0c7),
            mockup_diff_add_bg: rgba(0x8fbf931a),
            mockup_diff_del_fg: rgb(0xd4a9a9),
            mockup_diff_del_bg: rgba(0xc98b8b17),
            typography: TypographyTokens::default(),
        }
    }
}

impl Global for HermesTheme {}

impl HermesTheme {
    /// Retrieve the current theme from the GPUI application context.
    pub fn get(cx: &App) -> &Self {
        cx.global::<Self>()
    }

    /// Initialize the global theme on the GPUI application context.
    pub fn init(cx: &mut App) {
        cx.set_global(Self::default());
    }

    // --- Color Getters ---

    #[inline]
    pub fn canvas_bg(&self) -> Rgba {
        self.canvas_bg
    }

    #[inline]
    pub fn card_bg(&self) -> Rgba {
        self.card_bg
    }

    #[inline]
    pub fn card_hover(&self) -> Rgba {
        self.card_hover
    }

    #[inline]
    pub fn border_subtle(&self) -> Rgba {
        self.border_subtle
    }

    #[inline]
    pub fn border_structural(&self) -> Rgba {
        self.border_structural
    }

    #[inline]
    pub fn text_primary(&self) -> Rgba {
        self.text_primary
    }

    #[inline]
    pub fn text_muted(&self) -> Rgba {
        self.text_muted
    }

    #[inline]
    pub fn text_dim(&self) -> Rgba {
        self.text_dim
    }

    #[inline]
    pub fn text_extra_dim(&self) -> Rgba {
        self.text_extra_dim
    }

    #[inline]
    pub fn accent_blue(&self) -> Rgba {
        self.accent_blue
    }

    #[inline]
    pub fn sage_fg(&self) -> Rgba {
        self.sage_fg
    }

    #[inline]
    pub fn sage_bg(&self) -> Rgba {
        self.sage_bg
    }

    #[inline]
    pub fn rose_fg(&self) -> Rgba {
        self.rose_fg
    }

    #[inline]
    pub fn rose_bg(&self) -> Rgba {
        self.rose_bg
    }

    #[inline]
    pub fn warning_amber(&self) -> Rgba {
        self.warning_amber
    }

    #[inline]
    pub fn panel_frosted_bg(&self) -> Rgba {
        self.panel_frosted_bg
    }

    // --- Typography Getters ---

    #[inline]
    pub fn font_mono(&self) -> &[SharedString] {
        &self.typography.font_mono
    }

    #[inline]
    pub fn font_sans(&self) -> &[SharedString] {
        &self.typography.font_sans
    }

    #[inline]
    pub fn title_size(&self) -> Pixels {
        self.typography.title_size
    }

    #[inline]
    pub fn title_weight(&self) -> FontWeight {
        self.typography.title_weight
    }

    #[inline]
    pub fn body_size(&self) -> Pixels {
        self.typography.body_size
    }

    #[inline]
    pub fn body_weight(&self) -> FontWeight {
        self.typography.body_weight
    }

    #[inline]
    pub fn code_size(&self) -> Pixels {
        self.typography.code_size
    }

    #[inline]
    pub fn code_weight(&self) -> FontWeight {
        self.typography.code_weight
    }

    #[inline]
    pub fn caption_size(&self) -> Pixels {
        self.typography.caption_size
    }

    #[inline]
    pub fn caption_weight(&self) -> FontWeight {
        self.typography.caption_weight
    }
}

/// Mockup type scale (docs/design mockups-desktop §2.2). Sizes in px, paired
/// with the matching line-height where the mockup defines one. The legacy
/// `TypographyTokens` stays untouched until the P6 retirement.
pub struct MockupTypeScale;

impl MockupTypeScale {
    /// Base prose / bold lead-ins / user bubble / Continue + project titles.
    pub const BASE_SIZE: f32 = 15.0;
    pub const BASE_LINE_HEIGHT: f32 = 26.0;
    /// ASCII logo (Geist Mono w500).
    pub const LOGO_SIZE: f32 = 56.0;
    pub const LOGO_LINE_HEIGHT: f32 = 56.0;
    pub const LOGO_WEIGHT: FontWeight = FontWeight::MEDIUM;
    /// Bold lead-ins inside assistant prose.
    pub const BOLD_WEIGHT: FontWeight = FontWeight::BOLD;
    /// Composer placeholder.
    pub const PLACEHOLDER_SIZE: f32 = 16.0;
    /// `.pill` rows.
    pub const PILL_SIZE: f32 = 14.0;
    pub const PILL_LINE_HEIGHT: f32 = 20.0;
    /// `.chip` rows.
    pub const CHIP_SIZE: f32 = 11.5;
    pub const CHIP_LINE_HEIGHT: f32 = 20.0;
    /// Inline code `.cd` and diff header/lines (lines use DIFF_LINE_HEIGHT).
    pub const INLINE_CODE_SIZE: f32 = 12.5;
    pub const DIFF_SIZE: f32 = 12.5;
    pub const DIFF_LINE_HEIGHT: f32 = 21.0;
    /// Sidebar search placeholder / session titles / feature labels.
    pub const SEARCH_SIZE: f32 = 14.5;
    pub const SESSION_TITLE_SIZE: f32 = 14.5;
    pub const FEATURE_LABEL_SIZE: f32 = 14.5;
    /// `cmd K` hint.
    pub const HINT_SIZE: f32 = 11.5;
    /// Projects header.
    pub const PROJECTS_HEADER_SIZE: f32 = 13.5;
    /// Project count / running spinner.
    pub const PROJECT_COUNT_SIZE: f32 = 13.0;
    pub const SPINNER_SIZE: f32 = 13.0;
    /// Account footer.
    pub const ACCOUNT_NAME_SIZE: f32 = 14.0;
    pub const ACCOUNT_SUB_SIZE: f32 = 12.0;
    pub const ACCOUNT_LINE_HEIGHT: f32 = 18.0;
    /// Avatar letter.
    pub const AVATAR_SIZE: f32 = 13.0;
    /// Continue list.
    pub const CONTINUE_HEADER_SIZE: f32 = 13.0;
    pub const CONTINUE_PROJECT_SIZE: f32 = 12.0;
    pub const CONTINUE_TIME_SIZE: f32 = 13.0;
    /// Action bar / token count / context label.
    pub const ACTION_BAR_SIZE: f32 = 13.5;
    pub const TOKEN_COUNT_SIZE: f32 = 13.0;
    pub const CONTEXT_LABEL_SIZE: f32 = 13.0;
    /// Changes button.
    pub const CHANGES_SIZE: f32 = 14.0;
    /// Tooltip.
    pub const TOOLTIP_SIZE: f32 = 12.5;
    pub const TOOLTIP_LINE_HEIGHT: f32 = 20.0;
}

// --- WCAG Contrast & Luminance Calculations ---
/// Computes the linear sRGB channel value according to WCAG 2.1 specifications.
fn srgb_to_linear(channel: f32) -> f32 {
    let c = channel.clamp(0.0, 1.0);
    if c <= 0.04045 {
        c / 12.92
    } else {
        ((c + 0.055) / 1.055).powf(2.4)
    }
}

/// Computes the WCAG 2.1 relative luminance for an RGBA color.
pub fn relative_luminance(color: Rgba) -> f32 {
    let r = srgb_to_linear(color.r);
    let g = srgb_to_linear(color.g);
    let b = srgb_to_linear(color.b);
    0.2126 * r + 0.7152 * g + 0.0722 * b
}

/// Computes the WCAG 2.1 contrast ratio between two colors (ranging from 1.0 to 21.0).
pub fn contrast_ratio(c1: Rgba, c2: Rgba) -> f32 {
    let l1 = relative_luminance(c1);
    let l2 = relative_luminance(c2);
    let lighter = l1.max(l2);
    let darker = l1.min(l2);
    (lighter + 0.05) / (darker + 0.05)
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_theme_color_tokens() {
        let theme = HermesTheme::default();

        // Canvas #09090b
        assert_eq!((theme.canvas_bg.r * 255.0).round() as u8, 0x09);
        assert_eq!((theme.canvas_bg.g * 255.0).round() as u8, 0x09);
        assert_eq!((theme.canvas_bg.b * 255.0).round() as u8, 0x0b);

        // Card #18181b
        assert_eq!((theme.card_bg.r * 255.0).round() as u8, 0x18);
        assert_eq!((theme.card_bg.g * 255.0).round() as u8, 0x18);
        assert_eq!((theme.card_bg.b * 255.0).round() as u8, 0x1b);

        // Text primary #fafafa
        assert_eq!((theme.text_primary.r * 255.0).round() as u8, 0xfa);
        assert_eq!((theme.text_primary.g * 255.0).round() as u8, 0xfa);
        assert_eq!((theme.text_primary.b * 255.0).round() as u8, 0xfa);

        // Sage green #8fbf93
        assert_eq!((theme.sage_fg.r * 255.0).round() as u8, 0x8f);
        assert_eq!((theme.sage_fg.g * 255.0).round() as u8, 0xbf);
        assert_eq!((theme.sage_fg.b * 255.0).round() as u8, 0x93);

        // Rose red #c98b8b
        assert_eq!((theme.rose_fg.r * 255.0).round() as u8, 0xc9);
        assert_eq!((theme.rose_fg.g * 255.0).round() as u8, 0x8b);
        assert_eq!((theme.rose_fg.b * 255.0).round() as u8, 0x8b);

        // Accent blue #7aa7ff
        assert_eq!((theme.accent_blue.r * 255.0).round() as u8, 0x7a);
        assert_eq!((theme.accent_blue.g * 255.0).round() as u8, 0xa7);
        assert_eq!((theme.accent_blue.b * 255.0).round() as u8, 0xff);
    }

    #[test]
    fn test_wcag_contrast_ratios() {
        let theme = HermesTheme::default();

        let cr_canvas = contrast_ratio(theme.text_primary, theme.canvas_bg);
        assert!(
            cr_canvas > 12.0,
            "Contrast ratio on canvas must exceed 12:1, got {:.2}",
            cr_canvas
        );

        let cr_card = contrast_ratio(theme.text_primary, theme.card_bg);
        assert!(
            cr_card > 9.0,
            "Contrast ratio on card must exceed 9:1, got {:.2}",
            cr_card
        );
    }

    #[test]
    fn test_typography_hierarchy() {
        let theme = HermesTheme::default();
        assert_eq!(theme.title_size(), px(15.0));
        assert_eq!(theme.title_weight(), FontWeight::SEMIBOLD);
        assert_eq!(theme.body_size(), px(13.0));
        assert_eq!(theme.body_weight(), FontWeight::NORMAL);
        assert_eq!(theme.code_size(), px(12.0));
        assert_eq!(theme.caption_size(), px(11.0));

        assert!(theme.font_mono().iter().any(|f| f.as_ref() == "JetBrains Mono"));
        assert!(theme.font_sans().iter().any(|f| f.as_ref() == "-apple-system"));
    }
}

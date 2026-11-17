//! Precise visual tokens matching the HEX Agent HTML Mockup.
//!
//! Handles Truecolor (`COLORTERM=truecolor` / `24bit`) detection with automatic
//! fallback to high-fidelity 256-color terminal approximations.

use ratatui::style::Color;

/// Theme tokens representing the HEX Agent Cockpit visual design system.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct HexTheme {
    /// Canvas/outermost terminal background (#000000 or #0a0a0a)
    pub canvas_bg: Color,
    /// Main cockpit/window background (#0e0e0e)
    pub window_bg: Color,
    /// Inner card, prompt container, modal background (#0c0c0c / #0b0b0b)
    pub card_bg: Color,
    /// Hover row / selection background (#141414 / #151515)
    pub hover_bg: Color,
    /// Active selection background (#1a1a1a)
    pub selection_bg: Color,
    /// Low contrast border (#1c1c1c / #1f1f1f)
    pub border_low: Color,
    /// Mid contrast border (#232323 / #2a2a2a)
    pub border_mid: Color,
    /// High contrast modal border (#333333)
    pub border_modal: Color,
    /// Bright white for titles, user prompt, emphasis (#f2f2f2)
    pub text_bright: Color,
    /// Primary body text (#d6d6d6)
    pub text_primary: Color,
    /// Muted secondary text / thought explanation (#bdbdbd)
    pub text_muted: Color,
    /// Dim metadata, timestamps, token counts (#8c8c8c)
    pub text_dim: Color,
    /// Extra dim separators, hidden frames, bullets (#7d7d7d / #4a4a4a)
    pub text_extra_dim: Color,
    /// Electric Blue accent for cursor, spinner, active marker (#7aa7ff)
    pub accent_blue: Color,
    /// Sage green for diff additions, test pass (#8fbf93)
    pub sage_fg: Color,
    /// Sage tint background for added lines (rgba(143,191,147,.09) -> #171f18)
    pub sage_bg: Color,
    /// Sage highlighted line text (#c4e0c7)
    pub sage_text: Color,
    /// Rose red for diff removals, test fail, exit errors (#c98b8b)
    pub rose_fg: Color,
    /// Rose tint background for removed lines (rgba(201,139,139,.08) -> #1c1616)
    pub rose_bg: Color,
    /// Rose highlighted line text (#c9a0a0)
    pub rose_text: Color,
    /// Whether truecolor is active
    pub is_truecolor: bool,
}

impl Default for HexTheme {
    fn default() -> Self {
        Self::detect()
    }
}

impl HexTheme {
    /// Auto-detect terminal color capabilities via `COLORTERM` env var.
    pub fn detect() -> Self {
        let is_truecolor = match std::env::var("COLORTERM") {
            Ok(val) => {
                let v = val.to_lowercase();
                v == "truecolor" || v == "24bit"
            }
            Err(_) => false,
        };

        if is_truecolor {
            Self::truecolor()
        } else {
            Self::color_256()
        }
    }

    /// Full 24-bit Truecolor palette derived directly from the HTML mockup.
    pub const fn truecolor() -> Self {
        Self {
            canvas_bg: Color::Rgb(10, 10, 10),
            window_bg: Color::Rgb(14, 14, 14),
            card_bg: Color::Rgb(12, 12, 12),
            hover_bg: Color::Rgb(21, 21, 21),
            selection_bg: Color::Rgb(26, 26, 26),
            border_low: Color::Rgb(28, 28, 28),
            border_mid: Color::Rgb(42, 42, 42),
            border_modal: Color::Rgb(51, 51, 51),
            text_bright: Color::Rgb(242, 242, 242),
            text_primary: Color::Rgb(214, 214, 214),
            text_muted: Color::Rgb(189, 189, 189),
            text_dim: Color::Rgb(140, 140, 140),
            text_extra_dim: Color::Rgb(74, 74, 74),
            accent_blue: Color::Rgb(122, 167, 255),
            sage_fg: Color::Rgb(143, 191, 147),
            sage_bg: Color::Rgb(23, 31, 24),
            sage_text: Color::Rgb(196, 224, 199),
            rose_fg: Color::Rgb(201, 139, 139),
            rose_bg: Color::Rgb(28, 22, 22),
            rose_text: Color::Rgb(201, 160, 160),
            is_truecolor: true,
        }
    }

    /// High-fidelity 256-color fallback palette for legacy terminals.
    pub const fn color_256() -> Self {
        Self {
            canvas_bg: Color::Indexed(232),      // #080808
            window_bg: Color::Indexed(233),      // #121212
            card_bg: Color::Indexed(233),        // #121212
            hover_bg: Color::Indexed(234),       // #1c1c1c
            selection_bg: Color::Indexed(235),   // #262626
            border_low: Color::Indexed(235),     // #262626
            border_mid: Color::Indexed(237),     // #3a3a3a
            border_modal: Color::Indexed(238),   // #444444
            text_bright: Color::Indexed(255),    // #eeeeee
            text_primary: Color::Indexed(252),   // #d0d0d0
            text_muted: Color::Indexed(249),     // #b2b2b2
            text_dim: Color::Indexed(244),       // #808080
            text_extra_dim: Color::Indexed(240), // #585858
            accent_blue: Color::Indexed(111),    // #87afff
            sage_fg: Color::Indexed(108),        // #87af87
            sage_bg: Color::Indexed(234),        // #1c1c1c
            sage_text: Color::Indexed(151),      // #afd7af
            rose_fg: Color::Indexed(174),        // #d78787
            rose_bg: Color::Indexed(234),        // #1c1c1c
            rose_text: Color::Indexed(181),      // #d7afaf
            is_truecolor: false,
        }
    }

    /// Apfel Grotezk section header style (`.d .hd` in HTML mockup: #8c8c8c).
    pub fn apfel_header_style(&self) -> ratatui::style::Style {
        ratatui::style::Style::default().fg(self.text_dim)
    }

    /// Apfel Grotezk modal / project title style (`.f .hd` in HTML mockup: #f2f2f2 bold).
    pub fn apfel_title_style(&self) -> ratatui::style::Style {
        ratatui::style::Style::default()
            .fg(self.text_bright)
            .add_modifier(ratatui::style::Modifier::BOLD)
    }
}

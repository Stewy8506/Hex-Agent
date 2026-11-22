//! Embedded brand fonts for the mockup design language.
//!
//! The Windows text system (DirectWrite) accepts TTF/OTF only, so the mockup
//! woff2 sources are pre-converted to static TTFs and embedded here. Both
//! weights of each family share one family name; DirectWrite picks the nearest
//! weight within the family (`GetMatchingFonts`).

use std::borrow::Cow;

use gpui::{App, Result};

const APFEL_GROTEZK_REGULAR: &[u8] = include_bytes!("../assets/fonts/ApfelGrotezk-Regular.ttf");
const APFEL_GROTEZK_BOLD: &[u8] = include_bytes!("../assets/fonts/ApfelGrotezk-Bold.ttf");
const GEIST_MONO_REGULAR: &[u8] = include_bytes!("../assets/fonts/GeistMono-Regular.ttf");
const GEIST_MONO_MEDIUM: &[u8] = include_bytes!("../assets/fonts/GeistMono-Medium.ttf");

/// Registers the embedded brand fonts with the text system.
pub fn load_embedded_fonts(cx: &App) -> Result<()> {
    cx.text_system().add_fonts(vec![
        Cow::Borrowed(APFEL_GROTEZK_REGULAR),
        Cow::Borrowed(APFEL_GROTEZK_BOLD),
        Cow::Borrowed(GEIST_MONO_REGULAR),
        Cow::Borrowed(GEIST_MONO_MEDIUM),
    ])
}

/// Returns the embedded bytes for one of the bundled font files, by name.
pub fn embedded_font(name: &str) -> Option<&'static [u8]> {
    match name {
        "ApfelGrotezk-Regular.ttf" => Some(APFEL_GROTEZK_REGULAR),
        "ApfelGrotezk-Bold.ttf" => Some(APFEL_GROTEZK_BOLD),
        "GeistMono-Regular.ttf" => Some(GEIST_MONO_REGULAR),
        "GeistMono-Medium.ttf" => Some(GEIST_MONO_MEDIUM),
        _ => None,
    }
}

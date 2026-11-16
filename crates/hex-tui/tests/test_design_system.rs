//! Verification tests for HEX Agent TUI Design System, Truecolor Tokens,
//! Animation Engine, and ASCII Wordmarks.

use hex_tui::ui::animation::{AnimationState, BRAILLE_FRAMES};
use hex_tui::ui::design::skin;
use hex_tui::ui::style::HexTheme;
use hex_tui::ui::widgets::ascii_logo::{SessionWordmark, StartWordmark};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Color;
use ratatui::widgets::Widget;

#[test]
fn test_hex_theme_truecolor_tokens() {
    let theme = HexTheme::truecolor();
    assert!(theme.is_truecolor);
    assert_eq!(theme.canvas_bg, Color::Rgb(10, 10, 10));
    assert_eq!(theme.window_bg, Color::Rgb(14, 14, 14));
    assert_eq!(theme.card_bg, Color::Rgb(12, 12, 12));
    assert_eq!(theme.hover_bg, Color::Rgb(21, 21, 21));
    assert_eq!(theme.border_low, Color::Rgb(28, 28, 28));
    assert_eq!(theme.border_mid, Color::Rgb(42, 42, 42));
    assert_eq!(theme.border_modal, Color::Rgb(51, 51, 51));
    assert_eq!(theme.text_bright, Color::Rgb(242, 242, 242));
    assert_eq!(theme.text_primary, Color::Rgb(214, 214, 214));
    assert_eq!(theme.text_muted, Color::Rgb(189, 189, 189));
    assert_eq!(theme.text_dim, Color::Rgb(140, 140, 140));
    assert_eq!(theme.text_extra_dim, Color::Rgb(74, 74, 74));
    assert_eq!(theme.accent_blue, Color::Rgb(122, 167, 255));
    assert_eq!(theme.sage_fg, Color::Rgb(143, 191, 147));
    assert_eq!(theme.sage_bg, Color::Rgb(23, 31, 24));
    assert_eq!(theme.rose_fg, Color::Rgb(201, 139, 139));
    assert_eq!(theme.rose_bg, Color::Rgb(28, 22, 22));
}

#[test]
fn test_hex_theme_256_fallback() {
    let theme = HexTheme::color_256();
    assert!(!theme.is_truecolor);
    match theme.window_bg {
        Color::Indexed(idx) => assert!(idx >= 232, "Expected 256-color grey ramp index"),
        other => panic!("Expected indexed color for 256 fallback, got {:?}", other),
    }
}

#[test]
fn test_hex_dark_skin_default() {
    let default_skin = skin("default");
    assert_eq!(default_skin.accent, Color::Rgb(122, 167, 255));
    assert_eq!(default_skin.surface, Color::Rgb(14, 14, 14));
    assert_eq!(default_skin.heading, Color::Rgb(242, 242, 242));
    assert_eq!(default_skin.prompt, ">");
}

#[test]
fn test_braille_spinner_sequence() {
    assert_eq!(BRAILLE_FRAMES.len(), 10);
    assert_eq!(BRAILLE_FRAMES[0], "⠋");
    assert_eq!(BRAILLE_FRAMES[9], "⠿");

    let now = std::time::Instant::now();
    // Simulate tick 0ms
    let anim0 = AnimationState::with_start(now);
    assert_eq!(anim0.spinner_frame(), "⠋");

    // Simulate tick at 95ms -> frame 1 ("⠙")
    let start_95ms_ago = now - std::time::Duration::from_millis(95);
    let anim1 = AnimationState::with_start(start_95ms_ago);
    assert_eq!(anim1.spinner_frame(), "⠙");

    // Simulate tick at 280ms -> frame 3 ("⠸")
    let start_280ms_ago = now - std::time::Duration::from_millis(280);
    let anim3 = AnimationState::with_start(start_280ms_ago);
    assert_eq!(anim3.spinner_frame(), "⠸");
}

#[test]
fn test_pulse_sine_cycle_bounds() {
    let anim = AnimationState::new();
    let factor = anim.pulse_factor();
    assert!((0.35..=1.0).contains(&factor));

    let dim = Color::Rgb(74, 74, 74);
    let accent = Color::Rgb(122, 167, 255);
    let color = anim.pulse_color(dim, accent);
    match color {
        Color::Rgb(r, g, b) => {
            assert!(r >= 74 && r <= 122);
            assert!(g >= 74 && g <= 167);
            assert!(b >= 74);
        }
        _ => panic!("Expected RGB color from pulse interpolation"),
    }
}

#[test]
fn test_cursor_blink_duty_cycle() {
    let anim = AnimationState::new();
    // Initially within the first 550ms, cursor is visible
    let visible = anim.cursor_visible();
    assert!(visible, "Cursor must initially be visible on tick 0");
}

#[test]
fn test_thinking_shimmer_generation() {
    let anim = AnimationState::new();
    let text = "Verifying public API surface";
    let spans = anim.render_shimmer(text, Color::Rgb(106, 106, 106), Color::Rgb(240, 240, 240));
    assert_eq!(spans.len(), text.len());
}

#[test]
fn test_ascii_wordmarks_render_dimensions() {
    let start_logo = StartWordmark::new(ratatui::style::Style::default());
    let area = Rect::new(0, 0, 35, 6);
    let mut buf = Buffer::empty(area);
    start_logo.render(area, &mut buf);

    // Verify row 0 contains bold block letters matching the 4-row geometric logo
    let mut row0 = String::new();
    for x in 0..area.width {
        row0.push_str(buf[(x, 0)].symbol());
    }
    assert!(row0.contains("██████") || row0.contains("██"));

    let session_logo = SessionWordmark::new(ratatui::style::Style::default());
    let s_area = Rect::new(0, 0, 20, 3);
    let mut s_buf = Buffer::empty(s_area);
    session_logo.render(s_area, &mut s_buf);

    let mut s_row0 = String::new();
    for x in 0..s_area.width {
        s_row0.push_str(s_buf[(x, 0)].symbol());
    }
    assert!(s_row0.contains("█ █ █▀▀ ▀▄▀"));
}

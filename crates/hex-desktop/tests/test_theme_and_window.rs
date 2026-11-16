use core::prelude::v1::test;
use gpui::*;
use hex_desktop::theme::*;
use hex_desktop::window::*;

#[test]
fn test_theme_color_tokens_and_hex_accuracy() {
    let theme = HermesTheme::default();

    // Canvas Background #09090b (Dark Zinc canvas)
    assert_eq!((theme.canvas_bg.r * 255.0).round() as u8, 0x09);
    assert_eq!((theme.canvas_bg.g * 255.0).round() as u8, 0x09);
    assert_eq!((theme.canvas_bg.b * 255.0).round() as u8, 0x0b);
    assert_eq!((theme.canvas_bg.a * 255.0).round() as u8, 0xff);

    // Card Background #18181b (zinc-900)
    assert_eq!((theme.card_bg.r * 255.0).round() as u8, 0x18);
    assert_eq!((theme.card_bg.g * 255.0).round() as u8, 0x18);
    assert_eq!((theme.card_bg.b * 255.0).round() as u8, 0x1b);

    // Card Hover / Elevated #27272a (zinc-800)
    assert_eq!((theme.card_hover.r * 255.0).round() as u8, 0x27);
    assert_eq!((theme.card_hover.g * 255.0).round() as u8, 0x27);
    assert_eq!((theme.card_hover.b * 255.0).round() as u8, 0x2a);

    // Border Low Subtle #27272a
    assert_eq!((theme.border_subtle.r * 255.0).round() as u8, 0x27);
    assert_eq!((theme.border_subtle.g * 255.0).round() as u8, 0x27);
    assert_eq!((theme.border_subtle.b * 255.0).round() as u8, 0x2a);

    // Border Structural #3f3f46 (zinc-700)
    assert_eq!((theme.border_structural.r * 255.0).round() as u8, 0x3f);
    assert_eq!((theme.border_structural.g * 255.0).round() as u8, 0x3f);
    assert_eq!((theme.border_structural.b * 255.0).round() as u8, 0x46);

    // Text Primary #fafafa (zinc-50)
    assert_eq!((theme.text_primary.r * 255.0).round() as u8, 0xfa);
    assert_eq!((theme.text_primary.g * 255.0).round() as u8, 0xfa);
    assert_eq!((theme.text_primary.b * 255.0).round() as u8, 0xfa);

    // Text Muted #a1a1aa (zinc-400)
    assert_eq!((theme.text_muted.r * 255.0).round() as u8, 0xa1);
    assert_eq!((theme.text_muted.g * 255.0).round() as u8, 0xa1);
    assert_eq!((theme.text_muted.b * 255.0).round() as u8, 0xaa);

    // Text Dim #71717a (zinc-500)
    assert_eq!((theme.text_dim.r * 255.0).round() as u8, 0x71);
    assert_eq!((theme.text_dim.g * 255.0).round() as u8, 0x71);
    assert_eq!((theme.text_dim.b * 255.0).round() as u8, 0x7a);

    // Text Extra Dim #52525b (zinc-600)
    assert_eq!((theme.text_extra_dim.r * 255.0).round() as u8, 0x52);
    assert_eq!((theme.text_extra_dim.g * 255.0).round() as u8, 0x52);
    assert_eq!((theme.text_extra_dim.b * 255.0).round() as u8, 0x5b);

    // Accent Electric Blue #7aa7ff
    assert_eq!((theme.accent_blue.r * 255.0).round() as u8, 0x7a);
    assert_eq!((theme.accent_blue.g * 255.0).round() as u8, 0xa7);
    assert_eq!((theme.accent_blue.b * 255.0).round() as u8, 0xff);

    // Sage Green FG #8fbf93
    assert_eq!((theme.sage_fg.r * 255.0).round() as u8, 0x8f);
    assert_eq!((theme.sage_fg.g * 255.0).round() as u8, 0xbf);
    assert_eq!((theme.sage_fg.b * 255.0).round() as u8, 0x93);

    // Sage BG Tint 12% alpha (0x1f)
    assert_eq!((theme.sage_bg.r * 255.0).round() as u8, 0x8f);
    assert_eq!((theme.sage_bg.g * 255.0).round() as u8, 0xbf);
    assert_eq!((theme.sage_bg.b * 255.0).round() as u8, 0x93);
    assert_eq!((theme.sage_bg.a * 255.0).round() as u8, 0x1f);

    // Rose Red FG #c98b8b
    assert_eq!((theme.rose_fg.r * 255.0).round() as u8, 0xc9);
    assert_eq!((theme.rose_fg.g * 255.0).round() as u8, 0x8b);
    assert_eq!((theme.rose_fg.b * 255.0).round() as u8, 0x8b);

    // Rose BG Tint 10% alpha (0x1a)
    assert_eq!((theme.rose_bg.r * 255.0).round() as u8, 0xc9);
    assert_eq!((theme.rose_bg.g * 255.0).round() as u8, 0x8b);
    assert_eq!((theme.rose_bg.b * 255.0).round() as u8, 0x8b);
    assert_eq!((theme.rose_bg.a * 255.0).round() as u8, 0x1a);

    // Warning Amber #f59e0b
    assert_eq!((theme.warning_amber.r * 255.0).round() as u8, 0xf5);
    assert_eq!((theme.warning_amber.g * 255.0).round() as u8, 0x9e);
    assert_eq!((theme.warning_amber.b * 255.0).round() as u8, 0x0b);

    // Frosted Glass Panel Fill 88% alpha (0xe0)
    assert_eq!((theme.panel_frosted_bg.r * 255.0).round() as u8, 0x18);
    assert_eq!((theme.panel_frosted_bg.g * 255.0).round() as u8, 0x18);
    assert_eq!((theme.panel_frosted_bg.b * 255.0).round() as u8, 0x1b);
    assert_eq!((theme.panel_frosted_bg.a * 255.0).round() as u8, 0xe0);
}

#[test]
fn test_theme_wcag_contrast_ratios() {
    let theme = HermesTheme::default();

    // Contrast on canvas background (#fafafa on #09090b)
    let cr_canvas = contrast_ratio(theme.text_primary, theme.canvas_bg);
    assert!(
        cr_canvas > 12.0,
        "Luminance contrast ratio on canvas must exceed 12:1, got {:.2}:1",
        cr_canvas
    );

    // Contrast on card surface (#fafafa on #18181b)
    let cr_card = contrast_ratio(theme.text_primary, theme.card_bg);
    assert!(
        cr_card > 9.0,
        "Luminance contrast ratio on card must exceed 9:1, got {:.2}:1",
        cr_card
    );

    // Contrast for secondary text on canvas (#a1a1aa on #09090b)
    let cr_muted = contrast_ratio(theme.text_muted, theme.canvas_bg);
    assert!(
        cr_muted > 7.0,
        "Muted text contrast ratio on canvas must exceed 7:1 (WCAG AAA), got {:.2}:1",
        cr_muted
    );
}

#[test]
fn test_window_config_defaults_and_metal_options() {
    let config = WindowConfig::default();
    assert_eq!(config.width, px(1200.0));
    assert_eq!(config.height, px(800.0));
    assert_eq!(config.min_width, px(900.0));
    assert_eq!(config.min_height, px(600.0));
    assert_eq!(config.target_fps, 120);

    // Verify default bounds calculations
    let bounds = config.default_bounds();
    assert_eq!(bounds.size.width, px(1200.0));
    assert_eq!(bounds.size.height, px(800.0));
    assert_eq!(bounds.origin.x, px(360.0));
    assert_eq!(bounds.origin.y, px(140.0));

    // Centered on 2560x1440 display
    let display_bounds = Bounds {
        origin: point(px(0.0), px(0.0)),
        size: size(px(2560.0), px(1440.0)),
    };
    let centered = config.centered_on_display(display_bounds);
    assert_eq!(centered.origin.x, px(680.0));
    assert_eq!(centered.origin.y, px(320.0));

    // Verify Titlebar options: transparent and custom traffic light placement
    let titlebar = config.titlebar_options();
    assert!(titlebar.appears_transparent);
    assert_eq!(
        titlebar.traffic_light_position,
        Some(point(px(14.0), px(16.0)))
    );

    // Verify WindowOptions configuration
    let win_opts = config.window_options(None);
    assert!(win_opts.window_bounds.is_some());
    assert_eq!(win_opts.window_min_size, Some(size(px(900.0), px(600.0))));
    assert_eq!(
        win_opts.window_background,
        WindowBackgroundAppearance::Blurred
    );
    assert!(win_opts.focus);
    assert!(win_opts.show);
}

#[gpui::test]
async fn test_desktop_window_in_test_context(cx: &mut TestAppContext) {
    // Test theme initialization on GPUI context
    cx.update(|cx| {
        HermesTheme::init(cx);
        let theme = HermesTheme::get(cx);
        assert_eq!((theme.canvas_bg.r * 255.0).round() as u8, 0x09);
        assert_eq!((theme.card_bg.r * 255.0).round() as u8, 0x18);
        assert_eq!(theme.title_size(), px(15.0));
    });

    // Test DesktopWindow view instantiation
    let window_entity = cx.new(|_cx| DesktopWindow::new());
    cx.update(|cx| {
        let win = window_entity.read(cx);
        assert_eq!(win.status_message, "Ready");
        assert_eq!(win.active_view, DesktopView::Start);
        assert!(win.workspace_path.is_none());
    });

    // Test view state transition
    window_entity.update(cx, |win, _cx| {
        win.set_view(DesktopView::Session);
        assert_eq!(win.active_view, DesktopView::Session);
    });

    // Test initialization with custom workspace path
    let loaded_window = cx.new(|_cx| DesktopWindow::with_workspace("/tmp/test-hex-workspace"));
    cx.update(|cx| {
        let win = loaded_window.read(cx);
        assert_eq!(
            win.workspace_path.as_deref(),
            Some("/tmp/test-hex-workspace")
        );
        assert_eq!(win.status_message, "Workspace loaded");
        assert_eq!(win.active_view, DesktopView::Start);
    });
}

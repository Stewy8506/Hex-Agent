use core::prelude::v1::test;
use gpui::*;
use hex_desktop::theme::*;
use hex_desktop::window::*;

#[test]
fn test_mockup_token_values_byte_exact() {
    let theme = HermesTheme::default();

    let px8 = |c: Rgba| -> [u8; 4] {
        [
            (c.r * 255.0).round() as u8,
            (c.g * 255.0).round() as u8,
            (c.b * 255.0).round() as u8,
            (c.a * 255.0).round() as u8,
        ]
    };

    // Row hover / search box #1a1a1a.
    assert_eq!(px8(theme.mockup_row_hover), [0x1a, 0x1a, 0x1a, 0xff]);

    // Pill #1f1f1f, hover #272727.
    assert_eq!(px8(theme.mockup_pill), [0x1f, 0x1f, 0x1f, 0xff]);
    assert_eq!(px8(theme.mockup_pill_hover), [0x27, 0x27, 0x27, 0xff]);

    // Dock / segmented container #191919, border #232323.
    assert_eq!(px8(theme.mockup_dock), [0x19, 0x19, 0x19, 0xff]);
    assert_eq!(px8(theme.mockup_dock_border), [0x23, 0x23, 0x23, 0xff]);

    // Segmented active bg #262626; send / context track #2a2a2a.
    assert_eq!(px8(theme.mockup_seg_active), [0x26, 0x26, 0x26, 0xff]);
    assert_eq!(px8(theme.mockup_track), [0x2a, 0x2a, 0x2a, 0xff]);

    // Inactive session titles #bdbdbd; prose base #d9d9d9.
    assert_eq!(px8(theme.mockup_session_inactive), [0xbd, 0xbd, 0xbd, 0xff]);
    assert_eq!(px8(theme.mockup_text_base), [0xd9, 0xd9, 0xd9, 0xff]);

    // Accent border rgba(122,167,255,.3) -> alpha 0x4d.
    assert_eq!(px8(theme.mockup_accent_border), [0x7a, 0xa7, 0xff, 0x4d]);

    // Accent chip bg rgba(122,167,255,.14) -> alpha 0x24.
    assert_eq!(px8(theme.mockup_accent_chip_bg), [0x7a, 0xa7, 0xff, 0x24]);

    // Diff add: fg #c4e0c7, tint rgba(143,191,147,.1) -> alpha 0x1a.
    assert_eq!(px8(theme.mockup_diff_add_fg), [0xc4, 0xe0, 0xc7, 0xff]);
    assert_eq!(px8(theme.mockup_diff_add_bg), [0x8f, 0xbf, 0x93, 0x1a]);

    // Diff del: fg #d4a9a9, tint rgba(201,139,139,.09) -> alpha 0x17.
    assert_eq!(px8(theme.mockup_diff_del_fg), [0xd4, 0xa9, 0xa9, 0xff]);
    assert_eq!(px8(theme.mockup_diff_del_bg), [0xc9, 0x8b, 0x8b, 0x17]);
}

#[test]
fn test_mockup_type_scale_constants() {
    use hex_desktop::theme::MockupTypeScale as T;

    assert_eq!(T::BASE_SIZE, 15.0);
    assert_eq!(T::BASE_LINE_HEIGHT, 26.0);
    assert_eq!(T::LOGO_SIZE, 56.0);
    assert_eq!(T::LOGO_LINE_HEIGHT, 56.0);
    assert_eq!(T::LOGO_WEIGHT, FontWeight::MEDIUM);
    assert_eq!(T::BOLD_WEIGHT, FontWeight::BOLD);
    assert_eq!(T::PLACEHOLDER_SIZE, 16.0);
    assert_eq!(T::PILL_SIZE, 14.0);
    assert_eq!(T::PILL_LINE_HEIGHT, 20.0);
    assert_eq!(T::CHIP_SIZE, 11.5);
    assert_eq!(T::CHIP_LINE_HEIGHT, 20.0);
    assert_eq!(T::INLINE_CODE_SIZE, 12.5);
    assert_eq!(T::DIFF_SIZE, 12.5);
    assert_eq!(T::DIFF_LINE_HEIGHT, 21.0);
    assert_eq!(T::SEARCH_SIZE, 14.5);
    assert_eq!(T::SESSION_TITLE_SIZE, 14.5);
    assert_eq!(T::FEATURE_LABEL_SIZE, 14.5);
    assert_eq!(T::HINT_SIZE, 11.5);
    assert_eq!(T::PROJECTS_HEADER_SIZE, 13.5);
    assert_eq!(T::PROJECT_COUNT_SIZE, 13.0);
    assert_eq!(T::SPINNER_SIZE, 13.0);
    assert_eq!(T::ACCOUNT_NAME_SIZE, 14.0);
    assert_eq!(T::ACCOUNT_SUB_SIZE, 12.0);
    assert_eq!(T::ACCOUNT_LINE_HEIGHT, 18.0);
    assert_eq!(T::AVATAR_SIZE, 13.0);
    assert_eq!(T::CONTINUE_HEADER_SIZE, 13.0);
    assert_eq!(T::CONTINUE_PROJECT_SIZE, 12.0);
    assert_eq!(T::CONTINUE_TIME_SIZE, 13.0);
    assert_eq!(T::ACTION_BAR_SIZE, 13.5);
    assert_eq!(T::TOKEN_COUNT_SIZE, 13.0);
    assert_eq!(T::CONTEXT_LABEL_SIZE, 13.0);
    assert_eq!(T::CHANGES_SIZE, 14.0);
    assert_eq!(T::TOOLTIP_SIZE, 12.5);
    assert_eq!(T::TOOLTIP_LINE_HEIGHT, 20.0);
}

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
    assert_eq!(config.width, px(1440.0));
    assert_eq!(config.height, px(900.0));
    assert_eq!(config.min_width, px(900.0));
    assert_eq!(config.min_height, px(600.0));
    assert_eq!(config.target_fps, 120);

    // Verify default bounds calculations
    let bounds = config.default_bounds();
    assert_eq!(bounds.size.width, px(1440.0));
    assert_eq!(bounds.size.height, px(900.0));
    assert_eq!(bounds.origin.x, px(240.0));
    assert_eq!(bounds.origin.y, px(90.0));

    // Centered on 2560x1440 display
    let display_bounds = Bounds {
        origin: point(px(0.0), px(0.0)),
        size: size(px(2560.0), px(1440.0)),
    };
    let centered = config.centered_on_display(display_bounds);
    assert_eq!(centered.origin.x, px(560.0));
    assert_eq!(centered.origin.y, px(270.0));

    // Verify Titlebar options: transparent and custom traffic light placement
    let titlebar = config.titlebar_options();
    assert!(titlebar.appears_transparent);
    assert_eq!(
        titlebar.traffic_light_position,
        Some(point(px(14.0), px(16.0)))
    );

    // Verify WindowOptions configuration: opaque mockup canvas, no vibrancy.
    let win_opts = config.window_options(None);
    assert!(win_opts.window_bounds.is_some());
    assert_eq!(win_opts.window_min_size, Some(size(px(900.0), px(600.0))));
    assert_eq!(win_opts.window_background, WindowBackgroundAppearance::Opaque);
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

use core::prelude::v1::test;
use gpui::*;
use hex_desktop::theme::*;
use hex_desktop::ui::shell::{AppShell, ShellView};
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

    // P6 deliberate retirement: the legacy zinc tokens (canvas #09090b, card
    // #18181b, zinc borders/text, frosted glass) were removed; the roles they
    // played are now asserted against the canonical mockup palette.
    // Canvas #141414
    assert_eq!((theme.mockup_canvas.r * 255.0).round() as u8, 0x14);
    assert_eq!((theme.mockup_canvas.g * 255.0).round() as u8, 0x14);
    assert_eq!((theme.mockup_canvas.b * 255.0).round() as u8, 0x14);
    assert_eq!((theme.mockup_canvas.a * 255.0).round() as u8, 0xff);

    // Surface card #171717
    assert_eq!((theme.mockup_surface.r * 255.0).round() as u8, 0x17);
    assert_eq!((theme.mockup_surface.g * 255.0).round() as u8, 0x17);
    assert_eq!((theme.mockup_surface.b * 255.0).round() as u8, 0x17);

    // Hover surface #1e1e1e
    assert_eq!((theme.mockup_hover.r * 255.0).round() as u8, 0x1e);
    assert_eq!((theme.mockup_hover.g * 255.0).round() as u8, 0x1e);
    assert_eq!((theme.mockup_hover.b * 255.0).round() as u8, 0x1e);

    // Border subtle #222222
    assert_eq!((theme.mockup_border.r * 255.0).round() as u8, 0x22);
    assert_eq!((theme.mockup_border.g * 255.0).round() as u8, 0x22);
    assert_eq!((theme.mockup_border.b * 255.0).round() as u8, 0x22);

    // Border structural #262626
    assert_eq!((theme.mockup_border_strong.r * 255.0).round() as u8, 0x26);
    assert_eq!((theme.mockup_border_strong.g * 255.0).round() as u8, 0x26);
    assert_eq!((theme.mockup_border_strong.b * 255.0).round() as u8, 0x26);

    // Bright text #f2f2f2
    assert_eq!((theme.mockup_text_bright.r * 255.0).round() as u8, 0xf2);
    assert_eq!((theme.mockup_text_bright.g * 255.0).round() as u8, 0xf2);
    assert_eq!((theme.mockup_text_bright.b * 255.0).round() as u8, 0xf2);

    // Muted text #9a9a9a
    assert_eq!((theme.mockup_text_muted.r * 255.0).round() as u8, 0x9a);
    assert_eq!((theme.mockup_text_muted.g * 255.0).round() as u8, 0x9a);
    assert_eq!((theme.mockup_text_muted.b * 255.0).round() as u8, 0x9a);

    // Dim text #858585
    assert_eq!((theme.mockup_text_dim.r * 255.0).round() as u8, 0x85);
    assert_eq!((theme.mockup_text_dim.g * 255.0).round() as u8, 0x85);
    assert_eq!((theme.mockup_text_dim.b * 255.0).round() as u8, 0x85);

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
}

#[test]
fn test_theme_wcag_contrast_ratios() {
    let theme = HermesTheme::default();

    // Contrast on canvas background (#f2f2f2 on #141414)
    let cr_canvas = contrast_ratio(theme.mockup_text_bright, theme.mockup_canvas);
    assert!(
        cr_canvas > 12.0,
        "Luminance contrast ratio on canvas must exceed 12:1, got {:.2}:1",
        cr_canvas
    );

    // Contrast on card surface (#f2f2f2 on #171717)
    let cr_card = contrast_ratio(theme.mockup_text_bright, theme.mockup_surface);
    assert!(
        cr_card > 9.0,
        "Luminance contrast ratio on card must exceed 9:1, got {:.2}:1",
        cr_card
    );

    // Contrast for secondary text on canvas (#9a9a9a on #141414) — the mockup
    // muted token lands at ~6.5:1: WCAG AA for body text, not the zinc AAA 7:1.
    let cr_muted = contrast_ratio(theme.mockup_text_muted, theme.mockup_canvas);
    assert!(
        cr_muted > 6.0,
        "Muted text contrast ratio on canvas must exceed 6:1 (WCAG AA), got {:.2}:1",
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
async fn test_app_shell_state_in_test_context(cx: &mut TestAppContext) {
    // Test theme initialization on GPUI context
    cx.update(|cx| {
        HermesTheme::init(cx);
        let theme = HermesTheme::get(cx);
        assert_eq!((theme.mockup_canvas.r * 255.0).round() as u8, 0x14);
        assert_eq!((theme.mockup_surface.r * 255.0).round() as u8, 0x17);
        assert_eq!(theme.title_size(), px(15.0));
    });

    // P6: the legacy DesktopWindow root view was retired; ChatView owns
    // layout and rendering, so only AppShell state is asserted here.
    let shell_entity = cx.new(|_cx| AppShell::new());
    cx.update(|cx| {
        let shell = shell_entity.read(cx);
        assert_eq!(shell.active_view, ShellView::Chat);
        assert!(shell.active_session_id.is_none());
    });

    // Test workspace-scoped shell construction
    let workspace_shell =
        cx.new(|_cx| AppShell::with_workspace("test-hex-workspace", "main"));
    cx.update(|cx| {
        let shell = workspace_shell.read(cx);
        assert_eq!(shell.sidebar.workspace_name, "test-hex-workspace");
        assert_eq!(shell.sidebar.git_branch, "main");
        assert_eq!(shell.active_view, ShellView::Chat);
    });
}

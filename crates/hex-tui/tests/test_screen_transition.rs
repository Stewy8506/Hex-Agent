//! Start → Session reveal contracts. Environment-sensitive renders run in children.

use std::process::Command;
use std::time::{Duration, Instant};

use hex_protocol::models::Role;
use hex_tui::app::{
    App, ApprovalRequest, ChangedFile, DialogueItem, MessageItem, PlanStep, ScreenState,
    StepStatus, SubagentTelemetry,
};
use hex_tui::ui::animation::{
    apply_screen_reveal, interpolate_color, screen_reveal_factor, AnimationState, SCREEN_REVEAL_MS,
};
use hex_tui::ui::screens::session;
use hex_tui::ui::style::HexTheme;
use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::Terminal;

const DIFF: &str = "--- a/reveal.rs\n+++ b/reveal.rs\n@@ -1 +1 @@\n-old\n+new\n";

fn app() -> App {
    // No ledger, preferences, or scratch directories are created by these fixtures.
    let workspace =
        std::env::temp_dir().join(format!("hex-reveal-no-ledger-{}", std::process::id()));
    let mut app = App::with_workspace("reveal", "model", "sandbox", Some(&workspace));
    app.workspace_path = "/fixture/reveal".into();
    app.git_branch = "main".into();
    app.changed_files.clear();
    app.resume_sessions.clear();
    app
}

fn future() -> Instant {
    Instant::now() + Duration::from_secs(60)
}

fn message(role: Role, text: &str) -> DialogueItem {
    DialogueItem::Message(MessageItem {
        role,
        content: text.into(),
        timestamp: String::new(),
    })
}

fn rendered_app(revealing: bool, with_diff: bool) -> App {
    let mut app = app();
    app.dialogue_items = vec![
        message(Role::User, "Reveal this goal 世 e\u{301}"),
        message(Role::Assistant, "A **small** answer."),
    ];
    if with_diff {
        app.load_diff(DIFF);
    }
    app.switch_screen(ScreenState::Session);
    app.screen_transition_started = revealing.then(future);
    app.dialogue_entrance.set((app.dialogue_items.len(), None));
    app.input_buffer = "compose now".into();
    app.cursor_pos = app.input_buffer.len();
    app.plan_steps = vec![PlanStep {
        title: "verify reveal".into(),
        status: StepStatus::Completed,
    }];
    app.changed_files = vec![ChangedFile {
        path: "reveal.rs".into(),
        additions: 1,
        deletions: 1,
    }];
    app.subagents = vec![SubagentTelemetry {
        name: "verifier".into(),
        status: "pass".into(),
        detail: "done".into(),
        is_running: false,
        has_failed: false,
    }];
    app.tokens_used = 40_000;
    app.max_tokens = 200_000;
    app.session_started = future();
    app
}

fn render(app: &App, theme: &HexTheme, anim: &AnimationState, area: Rect) -> Buffer {
    let mut buf = Buffer::empty(area);
    session::render(app, theme, anim, area, &mut buf);
    buf
}

fn draw(app: &App, area: Rect) -> Buffer {
    let mut terminal = Terminal::new(TestBackend::new(area.width, area.height)).unwrap();
    terminal
        .draw(|frame| hex_tui::ui::draw(frame, app))
        .unwrap();
    terminal.backend().buffer().clone()
}

fn position(buf: &Buffer, needle: &str) -> (u16, u16) {
    for y in buf.area.top()..buf.area.bottom() {
        let row: String = (buf.area.left()..buf.area.right())
            .map(|x| buf[(x, y)].symbol())
            .collect();
        if let Some(index) = row.find(needle) {
            return (buf.area.x + row[..index].chars().count() as u16, y);
        }
    }
    panic!("missing {needle:?}");
}

/// Reinvoke just this test with a fixed environment; never mutate this process's env.
fn child_environment(test: &str, reduced: bool) -> bool {
    if std::env::var("HEX_SCREEN_REVEAL_TEST_CHILD").as_deref() == Ok(test) {
        return true;
    }
    let output = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", test, "--nocapture"])
        .env("HEX_SCREEN_REVEAL_TEST_CHILD", test)
        .env("HEX_REDUCED_MOTION", if reduced { "1" } else { "0" })
        .env("COLORTERM", "truecolor")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "child {test} failed:\n{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    false
}

#[test]
fn easing_has_exact_endpoints_and_is_monotonic_bounded_without_overshoot() {
    assert_eq!(SCREEN_REVEAL_MS, 240);
    assert_eq!(screen_reveal_factor(Duration::ZERO), 0.6);
    assert_eq!(screen_reveal_factor(Duration::from_millis(240)), 1.0);
    assert_eq!(screen_reveal_factor(Duration::from_millis(241)), 1.0);
    assert_eq!(screen_reveal_factor(Duration::MAX), 1.0);
    let mut previous = 0.6;
    for micros in (0..=240_000).step_by(100) {
        let age = Duration::from_micros(micros);
        let factor = screen_reveal_factor(age);
        assert!(
            factor.is_finite() && (0.6..=1.0).contains(&factor),
            "{age:?}: {factor}"
        );
        assert!(factor >= previous, "{age:?}: {previous} → {factor}");
        assert_eq!(
            factor,
            screen_reveal_factor(age),
            "the easing must be deterministic"
        );
        previous = factor;
    }
    assert!(screen_reveal_factor(Duration::from_millis(120)) > 0.6);
    assert!(screen_reveal_factor(Duration::from_millis(120)) < 1.0);
}

fn submit(app: &mut App) {
    app.input_buffer = "  reveal goal  ".into();
    assert_eq!(app.submit_input().as_deref(), Some("reveal goal"));
}

fn resume(app: &mut App) {
    assert!(app.resume_session("reveal-session"));
    assert_eq!(app.session_id, "reveal-session");
}

fn tool(app: &mut App) {
    app.add_tool_call("reveal-tool", "terminal", "{}", None, None);
}

#[test]
fn every_session_entry_path_starts_once_and_same_session_updates_do_not_restart() {
    type Entry = (&'static str, fn(&mut App));
    let entries: [Entry; 7] = [
        ("switch", |app| app.switch_screen(ScreenState::Session)),
        ("submit", submit),
        ("resume", resume),
        ("user", |app| app.add_user_message("goal")),
        ("assistant", |app| app.add_assistant_message("answer")),
        ("tool", tool),
        ("diff", |app| app.load_diff(DIFF)),
    ];
    for (name, enter) in entries {
        let mut app = app();
        assert_eq!(app.screen_state, ScreenState::Start);
        assert_eq!(app.screen_transition_started, None);
        let before = Instant::now();
        enter(&mut app);
        let started = app.screen_transition_started.expect(name);
        assert_eq!(app.screen_state, ScreenState::Session, "{name}");
        assert!(started >= before && started <= Instant::now(), "{name}");

        // A distinct pinned timestamp detects accidental restarts without sleeps.
        let pinned = future();
        app.screen_transition_started = Some(pinned);
        for (update_name, update) in entries {
            update(&mut app);
            assert_eq!(
                app.screen_transition_started,
                Some(pinned),
                "{name}/{update_name}"
            );
        }
        app.append_assistant_delta(" streamed");
        app.complete_tool_call("reveal-tool", "ok", 1, false);
        assert_eq!(
            app.screen_transition_started,
            Some(pinned),
            "stream/tool completion"
        );

        app.screen_transition_started = Some(Instant::now() - Duration::from_millis(240));
        app.advance_animation();
        assert_eq!(app.screen_transition_started, None, "{name}: expiry");
        for (update_name, update) in entries {
            update(&mut app);
            assert_eq!(
                app.screen_transition_started, None,
                "{name}/{update_name}: settled session"
            );
        }
    }
}

#[test]
fn a_first_assistant_delta_enters_session_and_later_deltas_keep_the_clock() {
    let mut app = app();
    app.append_assistant_delta("first");
    assert_eq!(app.screen_state, ScreenState::Session);
    assert!(app.screen_transition_started.is_some());
    let pinned = future();
    app.screen_transition_started = Some(pinned);
    app.append_assistant_delta(" second");
    assert_eq!(app.screen_transition_started, Some(pinned));
}

#[test]
fn start_noops_do_not_reveal_and_leaving_and_expiry_clear_the_clock() {
    let mut app = app();
    app.switch_screen(ScreenState::Start);
    app.input_buffer = " \t\n ".into();
    assert_eq!(app.submit_input(), None);
    app.add_system_message("notice");
    assert_eq!(app.screen_state, ScreenState::Start);
    assert_eq!(app.screen_transition_started, None);

    app.switch_screen(ScreenState::Session);
    let pinned = future();
    app.screen_transition_started = Some(pinned);
    app.advance_animation();
    assert_eq!(app.screen_transition_started, Some(pinned), "not expired");
    app.switch_screen(ScreenState::Start);
    assert_eq!(app.screen_transition_started, None);
    app.advance_animation();
    assert_eq!(app.screen_transition_started, None);
    app.switch_screen(ScreenState::Session);
    assert!(
        app.screen_transition_started.is_some(),
        "a new real entry must reveal again"
    );
    app.screen_transition_started = Some(Instant::now() - Duration::from_millis(SCREEN_REVEAL_MS));
    app.advance_animation();
    assert_eq!(
        app.screen_transition_started, None,
        "the endpoint expires without an extra tick"
    );
    app.switch_screen(ScreenState::Session);
    assert_eq!(
        app.screen_transition_started, None,
        "same-session entry does not reopen expiry"
    );
}

fn assert_only_foreground_changed(before: &Buffer, after: &Buffer) {
    assert_eq!(before.area, after.area);
    assert_eq!(before.content.len(), after.content.len());
    for (index, (original, revealed)) in before.content.iter().zip(&after.content).enumerate() {
        let mut restored = revealed.clone();
        restored.fg = original.fg;
        assert_eq!(
            &restored, original,
            "cell {index}: only foreground may change"
        );
    }
}

#[test]
fn reveal_is_in_place_preserves_unicode_styles_backgrounds_and_space_cells() {
    let area = Rect::new(8, 5, 12, 6);
    let mut buf = Buffer::empty(area);
    let fg = Color::Rgb(200, 100, 40);
    let bg = Color::Rgb(20, 30, 60);
    for cell in &mut buf.content {
        cell.set_fg(fg).set_bg(bg);
        cell.modifier = Modifier::BOLD | Modifier::ITALIC | Modifier::UNDERLINED;
    }
    buf.set_string(9, 6, "世e\u{301}界", Style::default().fg(fg).bg(bg));
    buf[(9, 7)].set_symbol("x").set_bg(Color::Rgb(80, 10, 20));
    buf[(10, 7)].set_symbol("x");
    let before = buf.clone();
    let pointer = buf.content.as_ptr();
    let capacity = buf.content.capacity();
    apply_screen_reveal(area, 0.6, &mut buf);
    assert_eq!(buf.content.as_ptr(), pointer);
    assert_eq!(buf.content.capacity(), capacity);
    assert_only_foreground_changed(&before, &buf);
    assert_eq!(buf[(9, 6)].symbol(), "世");
    assert_eq!(
        buf[(10, 6)].symbol(),
        " ",
        "wide glyph continuation remains empty"
    );
    assert_eq!(buf[(11, 6)].symbol(), "e\u{301}");
    for (original, revealed) in before.content.iter().zip(&buf.content) {
        if original.symbol() == " " {
            assert_eq!(revealed, original);
        } else {
            assert_eq!(revealed.fg, interpolate_color(original.bg, fg, 0.6));
            assert!(matches!(revealed.fg, Color::Rgb(..)));
        }
    }
    assert_ne!(
        buf[(9, 7)].fg,
        buf[(10, 7)].fg,
        "each cell uses its own background"
    );
}

#[test]
fn reveal_clips_nonzero_rectangles_and_outside_or_empty_areas_are_safe() {
    let area = Rect::new(8, 5, 6, 4);
    let mut original = Buffer::empty(area);
    for cell in &mut original.content {
        cell.set_symbol("x")
            .set_fg(Color::Rgb(240, 180, 100))
            .set_bg(Color::Rgb(10, 20, 30));
    }
    for requested in [
        Rect::new(6, 3, 5, 4),
        Rect::new(11, 7, 50, 50),
        Rect::new(0, 0, 2, 2),
        Rect::new(100, 100, 3, 3),
        Rect::new(9, 6, 0, 2),
        Rect::new(9, 6, 2, 0),
        Rect::new(u16::MAX - 1, u16::MAX - 1, 1, 1),
    ] {
        let mut buf = original.clone();
        apply_screen_reveal(requested, 0.6, &mut buf);
        let intersection = area.intersection(requested);
        for y in area.top()..area.bottom() {
            for x in area.left()..area.right() {
                if intersection.contains((x, y).into()) {
                    assert_ne!(
                        buf[(x, y)].fg,
                        original[(x, y)].fg,
                        "{requested:?}: {x},{y}"
                    );
                } else {
                    assert_eq!(buf[(x, y)], original[(x, y)], "{requested:?}: {x},{y}");
                }
            }
        }
        assert_only_foreground_changed(&original, &buf);
    }
    for factor in [1.0, 1.5, f32::INFINITY] {
        let mut buf = original.clone();
        apply_screen_reveal(Rect::new(0, 0, u16::MAX, u16::MAX), factor, &mut buf);
        assert_eq!(buf, original, "{factor} is a no-op");
    }
}

fn indexed_rgb(index: u8) -> [i32; 3] {
    if index >= 232 {
        let value = 8 + i32::from(index - 232) * 10;
        [value; 3]
    } else {
        assert!(index >= 16);
        let cube = index - 16;
        let levels = [0, 95, 135, 175, 215, 255];
        [
            levels[(cube / 36) as usize],
            levels[((cube / 6) % 6) as usize],
            levels[(cube % 6) as usize],
        ]
    }
}

#[test]
fn indexed_reveal_keeps_stepped_hue_and_grayscale_families_without_rgb_promotion() {
    for foreground in [196, 46, 21, 255] {
        let area = Rect::new(0, 0, 1, 1);
        let mut previous_distance = 0;
        let mut colors = Vec::new();
        for factor in [0.6, 0.7, 0.8, 0.9, 1.0] {
            let mut buf = Buffer::empty(area);
            buf[(0, 0)]
                .set_symbol("x")
                .set_fg(Color::Indexed(foreground))
                .set_bg(Color::Indexed(232));
            apply_screen_reveal(area, factor, &mut buf);
            let Color::Indexed(index) = buf[(0, 0)].fg else {
                panic!("indexed foreground promoted: {:?}", buf[(0, 0)].fg);
            };
            assert!(index >= 16);
            let rgb = indexed_rgb(index);
            let distance: i32 = rgb.iter().map(|channel| (channel - 8).pow(2)).sum();
            assert!(
                distance >= previous_distance,
                "quantized brightness must be monotonic"
            );
            previous_distance = distance;
            if foreground == 255 {
                assert!(index >= 232, "grayscale stays on its ramp");
                assert_eq!(rgb[0], rgb[1]);
                assert_eq!(rgb[1], rgb[2]);
            } else {
                let dominant = match foreground {
                    196 => 0,
                    46 => 1,
                    _ => 2,
                };
                assert!(rgb[dominant] > rgb[(dominant + 1) % 3]);
                assert_eq!(rgb[(dominant + 1) % 3], rgb[(dominant + 2) % 3]);
            }
            colors.push(index);
        }
        assert_ne!(
            colors[0], foreground,
            "the first frame must dim indexed text too"
        );
        assert_eq!(
            colors[4], foreground,
            "endpoint restores the exact source index"
        );
        assert!(
            colors.windows(2).any(|pair| pair[0] != pair[1]),
            "the stepped reveal must advance"
        );
    }
}

#[test]
fn unsupported_named_reset_and_ansi_palette_cells_are_untouched() {
    let unsupported = [
        Color::Reset,
        Color::Black,
        Color::Red,
        Color::Green,
        Color::Yellow,
        Color::Blue,
        Color::Magenta,
        Color::Cyan,
        Color::Gray,
        Color::DarkGray,
        Color::LightRed,
        Color::LightGreen,
        Color::LightYellow,
        Color::LightBlue,
        Color::LightMagenta,
        Color::LightCyan,
        Color::White,
    ];
    for unsupported in unsupported.into_iter().chain((0..16).map(Color::Indexed)) {
        for (fg, bg) in [
            (unsupported, Color::Rgb(10, 10, 10)),
            (Color::Rgb(240, 240, 240), unsupported),
            (unsupported, Color::Indexed(232)),
            (Color::Indexed(255), unsupported),
        ] {
            let area = Rect::new(0, 0, 1, 1);
            let mut buf = Buffer::empty(area);
            buf[(0, 0)].set_symbol("x").set_fg(fg).set_bg(bg);
            let before = buf.clone();
            apply_screen_reveal(area, 0.6, &mut buf);
            assert_eq!(buf, before, "unsupported pair {fg:?}/{bg:?}");
        }
    }
}

#[test]
fn initial_items_settle_under_reveal_and_later_items_get_an_ordinary_entrance() {
    if !child_environment(
        "initial_items_settle_under_reveal_and_later_items_get_an_ordinary_entrance",
        false,
    ) {
        return;
    }
    let mut app = app();
    app.add_user_message("initial goal");
    app.screen_transition_started = Some(future());
    // Even a previously started line entrance must be retired during screen reveal.
    app.dialogue_entrance.set((0, Some(future())));
    let len = app.dialogue_items.len();
    assert_eq!(app.observe_dialogue_entrance(len).0, len);
    assert_eq!(app.dialogue_entrance.get(), (len, None));
    app.add_assistant_message("initial answer");
    let len = app.dialogue_items.len();
    assert_eq!(app.observe_dialogue_entrance(len).0, len);
    assert_eq!(app.dialogue_entrance.get(), (len, None));

    let theme = HexTheme::truecolor();
    let anim = AnimationState::with_start(future());
    let area = Rect::new(0, 0, 120, 40);
    let first = render(&app, &theme, &anim, area);
    let pos = position(&first, "initial goal");
    assert_eq!(
        first[pos].fg,
        interpolate_color(theme.window_bg, theme.text_bright, 0.6)
    );
    app.screen_transition_started = Some(Instant::now() - Duration::from_millis(240));
    app.advance_animation();
    assert_eq!(app.screen_transition_started, None);
    let landed = render(&app, &theme, &anim, area);
    assert_eq!(app.observe_dialogue_entrance(len).0, len);
    assert_eq!(
        app.dialogue_entrance.get(),
        (len, None),
        "no second entrance after reveal"
    );
    assert_eq!(
        landed[position(&landed, "initial goal")].fg,
        theme.text_bright
    );

    app.add_user_message("later goal");
    assert_eq!(app.screen_transition_started, None);
    assert_eq!(app.observe_dialogue_entrance(len + 1).0, len);
    assert!(app.dialogue_entrance.get().1.is_some());
    app.dialogue_entrance.set((len, Some(future())));
    let later = render(&app, &theme, &anim, area);
    assert_eq!(later[position(&later, "later goal")].fg, theme.window_bg);
    assert_eq!(
        later[position(&later, "initial goal")].fg,
        theme.text_bright
    );
}

#[test]
fn session_renderer_reveals_only_stream_and_rail_and_preserves_click_geometry() {
    if !child_environment(
        "session_renderer_reveals_only_stream_and_rail_and_preserves_click_geometry",
        false,
    ) {
        return;
    }
    let anim = AnimationState::with_start(future());
    assert!(!anim.is_reduced());
    for theme in [HexTheme::truecolor(), HexTheme::color_256()] {
        for (width, height) in [(60, 30), (120, 40), (241, 68)] {
            for with_diff in [false, true] {
                let area = Rect::new(3, 2, width, height);
                let mut baseline_app = rendered_app(false, with_diff);
                let mut reveal_app = rendered_app(true, with_diff);
                let baseline = render(&baseline_app, &theme, &anim, area);
                let revealed = render(&reveal_app, &theme, &anim, area);
                let rail_width = baseline_app.sidebar.width_for(width - 2);
                let canvas_width = width - 2 - if rail_width > 0 { rail_width + 1 } else { 0 };
                let stream = Rect::new(area.x + 4, area.y + 4, canvas_width - 6, height - 11);
                let rail = if rail_width > 0 {
                    Rect::new(
                        area.right() - 1 - rail_width,
                        area.y + 3,
                        rail_width,
                        height - 4,
                    )
                } else {
                    Rect::default()
                };
                let mut expected = baseline.clone();
                apply_screen_reveal(stream, 0.6, &mut expected);
                apply_screen_reveal(rail, 0.6, &mut expected);
                assert_eq!(revealed, expected, "{width}x{height}/diff={with_diff}");
                assert_only_foreground_changed(&baseline, &revealed);
                let mut stream_changes = 0;
                let mut rail_changes = 0;
                for y in area.top()..area.bottom() {
                    for x in area.left()..area.right() {
                        let in_stream = stream.contains((x, y).into());
                        let in_rail = rail.contains((x, y).into());
                        if !in_stream && !in_rail {
                            assert_eq!(
                                revealed[(x, y)],
                                baseline[(x, y)],
                                "protected cell {x},{y}"
                            );
                        } else if revealed[(x, y)].fg != baseline[(x, y)].fg {
                            stream_changes += usize::from(in_stream);
                            rail_changes += usize::from(in_rail);
                            match baseline[(x, y)].fg {
                                Color::Rgb(..) => {
                                    assert!(matches!(revealed[(x, y)].fg, Color::Rgb(..)))
                                }
                                Color::Indexed(_) => {
                                    assert!(matches!(revealed[(x, y)].fg, Color::Indexed(_)))
                                }
                                other => panic!("unsupported foreground changed: {other:?}"),
                            }
                        }
                    }
                }
                assert!(stream_changes > 0, "transcript reveal must be visible");
                assert_eq!(rail_changes > 0, rail_width > 0);
                assert_eq!(baseline_app.sidebar.area.get(), rail);
                assert_eq!(reveal_app.sidebar.area.get(), rail);
                assert_eq!(
                    baseline_app.sidebar.metrics.get(),
                    reveal_app.sidebar.metrics.get()
                );
                assert_eq!(
                    baseline_app.sidebar.scroll.get(),
                    reveal_app.sidebar.scroll.get()
                );
                assert_eq!(
                    baseline_app.get_session_composer_area(area),
                    reveal_app.get_session_composer_area(area)
                );
                let targets = baseline_app.sidebar.hit_targets.borrow().clone();
                assert_eq!(*reveal_app.sidebar.hit_targets.borrow(), targets);
                assert_eq!(targets.is_empty(), rail_width == 0);

                // The landed frame has identical layout and original palette, not a replayed entrance.
                reveal_app.screen_transition_started = None;
                assert_eq!(render(&reveal_app, &theme, &anim, area), baseline);
                assert_eq!(*reveal_app.sidebar.hit_targets.borrow(), targets);
                for (target, _) in targets {
                    let x = target.x;
                    let y = target.y;
                    assert_eq!(
                        baseline_app.sidebar.click(x, y),
                        reveal_app.sidebar.click(x, y)
                    );
                    assert_eq!(
                        baseline_app.sidebar.preferences,
                        reveal_app.sidebar.preferences
                    );
                    assert_eq!(
                        baseline_app.sidebar.show_settings,
                        reveal_app.sidebar.show_settings
                    );
                    assert_eq!(
                        baseline_app.sidebar.show_all_changed,
                        reveal_app.sidebar.show_all_changed
                    );
                }
            }
        }
    }
}

#[test]
fn reduced_motion_bypasses_session_reveal_in_an_isolated_process() {
    if !child_environment(
        "reduced_motion_bypasses_session_reveal_in_an_isolated_process",
        true,
    ) {
        return;
    }
    let anim = AnimationState::with_start(future());
    assert!(anim.is_reduced());
    for theme in [HexTheme::truecolor(), HexTheme::color_256()] {
        for (width, height) in [(60, 30), (120, 40), (241, 68)] {
            let baseline = rendered_app(false, true);
            let revealed = rendered_app(true, true);
            let area = Rect::new(0, 0, width, height);
            assert_eq!(
                render(&revealed, &theme, &anim, area),
                render(&baseline, &theme, &anim, area)
            );
            assert_eq!(draw(&revealed, area), draw(&baseline, area));
            assert!(
                revealed.screen_transition_started.is_some(),
                "bypass must not depend on expiry"
            );
        }
    }
}

#[test]
fn modal_overlays_remain_full_opacity_during_reveal() {
    if !child_environment("modal_overlays_remain_full_opacity_during_reveal", false) {
        return;
    }
    for overlay in ["palette", "permission", "sidebar", "help"] {
        let mut baseline = rendered_app(false, false);
        let mut revealed = rendered_app(true, false);
        for app in [&mut baseline, &mut revealed] {
            match overlay {
                "palette" => app.open_palette(),
                "permission" => {
                    app.active_approval = Some(ApprovalRequest {
                        id: "reveal-approval".into(),
                        risk_level: "low".into(),
                        tool_name: "terminal".into(),
                        parameters: "ls".into(),
                        reason: "inspect workspace".into(),
                        blast_radius: "read only".into(),
                        cwd: "/fixture".into(),
                    })
                }
                "sidebar" => app.sidebar.show_settings = true,
                _ => app.show_help = true,
            }
        }
        let area = Rect::new(0, 0, 120, 40);
        let base = draw(&baseline, area);
        let reveal = draw(&revealed, area);
        // Find the actual modal border instead of probing potentially empty center rows.
        let help_title_y = (overlay == "help").then(|| position(&base, "HEX AGENT HELP").1);
        let (left, top) = (area.top()..area.bottom())
            .flat_map(|y| (area.left()..area.right()).map(move |x| (x, y)))
            .find(|&(x, y)| {
                let cell = &base[(x, y)];
                matches!(cell.symbol(), "╭" | "┌")
                    && match help_title_y {
                        Some(title_y) => y == title_y,
                        None => cell.fg == HexTheme::truecolor().border_modal,
                    }
            })
            .expect("modal must have a top-left corner");
        let border_fg = base[(left, top)].fg;
        let right = (left + 1..area.right())
            .find(|&x| {
                matches!(base[(x, top)].symbol(), "╮" | "┐") && base[(x, top)].fg == border_fg
            })
            .expect("modal must have a top-right corner");
        let bottom = (top + 1..area.bottom())
            .find(|&y| {
                matches!(base[(right, y)].symbol(), "╯" | "┘") && base[(right, y)].fg == border_fg
            })
            .expect("modal must have a bottom-right corner");
        let mut glyphs = 0;
        for y in top..=bottom {
            for x in left..=right {
                assert_eq!(
                    reveal[(x, y)],
                    base[(x, y)],
                    "{overlay}: modal cell {x},{y}"
                );
                glyphs += usize::from(base[(x, y)].symbol() != " ");
            }
        }
        assert!(glyphs > 0, "{overlay}: probe must contain modal content");
    }
}

#[test]
#[ignore = "release-only microbenchmark; run with --release --ignored --nocapture"]
fn screen_reveal_dense_release_microbenchmark() {
    if cfg!(debug_assertions) {
        panic!("run this benchmark with --release");
    }
    let area = Rect::new(0, 0, 241, 68);
    for (name, theme) in [
        ("truecolor", HexTheme::truecolor()),
        ("indexed", HexTheme::color_256()),
    ] {
        let foregrounds = [
            theme.text_bright,
            theme.accent_blue,
            theme.sage_fg,
            theme.rose_fg,
        ];
        let mut buf = Buffer::empty(area);
        for (index, cell) in buf.content.iter_mut().enumerate() {
            cell.set_symbol("x")
                .set_fg(foregrounds[index % foregrounds.len()])
                .set_bg(theme.window_bg);
        }
        let original = buf.clone();
        let mut samples = Vec::with_capacity(500);
        for iteration in 0..550 {
            for (cell, source) in buf.content.iter_mut().zip(&original.content) {
                cell.fg = source.fg;
            }
            let start = Instant::now();
            apply_screen_reveal(
                std::hint::black_box(area),
                std::hint::black_box(0.6),
                std::hint::black_box(&mut buf),
            );
            let elapsed = start.elapsed();
            std::hint::black_box(&buf);
            if iteration >= 50 {
                samples.push(elapsed);
            }
        }
        let changed = buf
            .content
            .iter()
            .zip(&original.content)
            .filter(|(cell, source)| cell.fg != source.fg)
            .count();
        samples.sort_unstable();
        let p95 = samples[(samples.len() * 95).div_ceil(100) - 1];
        println!(
            "apply_screen_reveal {name} 241x68: p95={:.2}us changedcells={changed}/{}",
            p95.as_secs_f64() * 1_000_000.0,
            buf.content.len()
        );
        assert!(changed > 0);
    }
}

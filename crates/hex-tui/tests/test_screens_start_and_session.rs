//! Tests for Screen 0 (Start Screen) and Screen 1 (Active Cockpit + Right Rail + Tinted Diff).

use hex_protocol::diff::DiffLineType;
use hex_tui::app::{App, ScreenState};
use hex_tui::ui::animation::AnimationState;
use hex_tui::ui::diff::{render_mockup_inline_diff, MockupDiffLine};
use hex_tui::ui::screens::{session, start};
use hex_tui::ui::style::HexTheme;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;

#[test]
fn test_screen_0_start_rendering() {
    let mut app = App::new("my-project", "sonnet", "sandbox");
    app.workspace_path = "/Users/test/projects/my-project".to_string();
    app.resume_sessions = vec![
        hex_tui::app::ResumeSession {
            title: "resume previous work on parser".to_string(),
            relative_time: "5m".to_string(),
            session_id: "ses_001".to_string(),
        },
    ];
    let theme = HexTheme::truecolor();
    let anim = AnimationState::new();

    let area = Rect::new(0, 0, 100, 30);
    let mut buf = Buffer::empty(area);

    start::render(&app, &theme, &anim, area, &mut buf);

    // Collect all text from buffer
    let mut full_text = String::new();
    for y in 0..area.height {
        for x in 0..area.width {
            full_text.push_str(buf[(x, y)].symbol());
        }
        full_text.push('\n');
    }

    // 1. Verify ASCII wordmark
    assert!(full_text.contains("██  ██  ██████  ██  ██") || full_text.contains("███   ███   █████████") || full_text.contains("█ █ █▀▀ ▀▄▀"), "Must contain block wordmark");

    // 2. Verify workspace path & git telemetry
    assert!(full_text.contains("my-project"), "Must show project name");
    assert!(full_text.contains("modified"), "Must show modified file count");
    assert!(full_text.contains("files indexed"), "Must show indexed count");

    // 3. Verify keyboard shortcut badges advertise real bindings only
    for hint in ["commands", "resume", "send", "help"] {
        assert!(full_text.contains(hint), "Must show {hint} shortcut");
    }
    for stale in ["ctrl+r", "@ files", "tab agent"] {
        assert!(!full_text.contains(stale), "Must not advertise {stale}");
    }

    // 4. Verify resume previous sessions section
    assert!(full_text.contains("resume"), "Must show resume header");
    assert!(full_text.contains("resume previous work on parser"), "Must list recent session dynamically");
    assert!(full_text.contains("5m"), "Must list relative time");
}

#[test]
fn test_screen_1_session_and_right_rail_rendering() {
    let mut app = App::new("my-project", "sonnet", "sandbox");
    app.screen_state = ScreenState::Session;
    app.add_user_message("stream real tokens dynamically");
    app.plan_steps = vec![
        hex_tui::app::PlanStep {
            title: "verify step".to_string(),
            status: hex_tui::app::StepStatus::Completed,
        },
    ];
    app.changed_files = vec![
        hex_tui::app::ChangedFile {
            path: "src/dynamic.rs".to_string(),
            additions: 15,
            deletions: 3,
        },
    ];
    app.subagents = vec![
        hex_tui::app::SubagentTelemetry {
            name: "verifier-bot".to_string(),
            status: "pass".to_string(),
            detail: "5 of 5".to_string(),
            is_running: false,
            has_failed: false,
        },
    ];
    app.tokens_used = 80_000;
    app.max_tokens = 200_000;

    let theme = HexTheme::truecolor();
    let anim = AnimationState::new();

    let area = Rect::new(0, 0, 120, 35);
    let mut buf = Buffer::empty(area);

    session::render(&app, &theme, &anim, area, &mut buf);

    let mut full_text = String::new();
    for y in 0..area.height {
        for x in 0..area.width {
            full_text.push_str(buf[(x, y)].symbol());
        }
        full_text.push('\n');
    }

    // 1. Top bar
    assert!(full_text.contains("hex"), "Must show hex logo/title in top bar");

    // 2. Compact wordmark & user query
    assert!(full_text.contains("█ █ █▀▀ ▀▄▀"), "Must show compact wordmark in transcript");
    assert!(full_text.contains("stream real tokens dynamically"), "Must show dynamic prompt query");

    // 3. Right rail sections
    assert!(full_text.contains("context"), "Right rail must have context section");
    assert!(full_text.contains("plan"), "Right rail must have plan section");
    assert!(full_text.contains("[x] verify step"), "Must render completed plan step");
    assert!(full_text.contains("changed"), "Right rail must have changed files section");
    assert!(full_text.contains("src/dynamic.rs"), "Must render changed file name");
    assert!(full_text.contains("agents"), "Right rail must have agents section");
    assert!(full_text.contains("verifier-bot"), "Must list verifier subagent");

    // 4. Message composer & footer
    assert!(full_text.contains("Message hex"), "Must show input composer prompt");
    assert!(full_text.contains("esc interrupt"), "Must show footer navigation hint");
    assert!(full_text.contains("ctrl+p commands"), "Must show command palette hint");
}

#[test]
fn test_sage_rose_tinted_inline_diff_rendering() {
    let theme = HexTheme::truecolor();
    let area = Rect::new(0, 0, 60, 8);
    let mut buf = Buffer::empty(area);

    let diff_lines = vec![
        MockupDiffLine {
            line_no: Some(42),
            line_type: DiffLineType::Deleted,
            content: "private edges: Edge[] = [];".to_string(),
        },
        MockupDiffLine {
            line_no: Some(42),
            line_type: DiffLineType::Added,
            content: "async *edges(): AsyncIterable<Edge> {".to_string(),
        },
    ];

    let rendered_h = render_mockup_inline_diff(
        "src/graph/builder.ts",
        1,
        1,
        &diff_lines,
        &theme,
        area,
        &mut buf,
    );

    assert!(rendered_h >= 4);

    // Verify line 2 is deleted with rose background
    let del_cell = &buf[(10, area.y + 2)];
    assert_eq!(del_cell.bg, theme.rose_bg, "Deleted line must have rose tint background");

    // Verify line 3 is added with sage background
    let add_cell = &buf[(10, area.y + 3)];
    assert_eq!(add_cell.bg, theme.sage_bg, "Added line must have sage tint background");
}

#[test]
fn test_start_to_session_transition_on_input_submit() {
    let mut app = App::new("test-project", "sonnet", "sandbox");
    assert_eq!(app.screen_state, ScreenState::Start);

    app.input_buffer = "Build graph streaming".to_string();
    let submitted = app.submit_input();
    assert_eq!(submitted, Some("Build graph streaming".to_string()));
    assert_eq!(app.screen_state, ScreenState::Session, "Must transition to Session screen");
}

#[test]
fn test_start_screen_hover_and_pulse_animation_styles() {
    use ratatui::style::{Color, Modifier};

    let mut app = App::new("test-project", "sonnet", "sandbox");
    app.resume_sessions = vec![
        hex_tui::app::ResumeSession {
            title: "Task Alpha".to_string(),
            relative_time: "2m".to_string(),
            session_id: "s_alpha".to_string(),
        },
        hex_tui::app::ResumeSession {
            title: "Task Beta".to_string(),
            relative_time: "5m".to_string(),
            session_id: "s_beta".to_string(),
        },
        hex_tui::app::ResumeSession {
            title: "Task Gamma".to_string(),
            relative_time: "10m".to_string(),
            session_id: "s_gamma".to_string(),
        },
        hex_tui::app::ResumeSession {
            title: "Task Delta".to_string(),
            relative_time: "15m".to_string(),
            session_id: "s_delta".to_string(),
        },
    ];

    let theme = HexTheme::truecolor();
    let anim = AnimationState::new();
    let area = Rect::new(0, 0, 120, 36);

    // --- State 1: Inactive (all levels 0.0) ---
    app.input_hover_level = 0.0;
    app.row_hover_levels = vec![0.0; 5];
    app.delete_hover_levels = vec![0.0; 4];
    app.shortcut_hover_levels = [0.0; 4];
    app.toggle_hover_level = 0.0;
    app.selected_resume_index = 999; // no keyboard selection

    let mut buf_idle = Buffer::empty(area);
    start::render(&app, &theme, &anim, area, &mut buf_idle);

    let prompt_rect = app.get_start_prompt_area(area);
    let prompt_top_left = &buf_idle[(prompt_rect.x, prompt_rect.y)];
    assert_eq!(prompt_top_left.fg, Color::Rgb(42, 42, 42), "Prompt border should be #2a2a2a when idle");

    let resume_rect = app.get_start_resume_area(area);
    let row0_y = resume_rect.y + 1;
    let row0_cell = &buf_idle[(resume_rect.x + 2, row0_y)];
    assert_eq!(row0_cell.bg, theme.canvas_bg, "Row 0 bg should be canvas_bg when idle");
    assert_eq!(row0_cell.fg, Color::Rgb(189, 189, 189), "Row 0 title should be #bdbdbd when idle");

    let del_cell_idle = &buf_idle[(resume_rect.x + resume_rect.width - 3, row0_y)];
    assert_eq!(del_cell_idle.fg, Color::Rgb(74, 74, 74), "Delete icon should be dim #4a4a4a when idle");

    // --- State 2: Fully Hovered (all levels 1.0) ---
    app.input_hover_level = 1.0;
    app.row_hover_levels[0] = 1.0;
    app.delete_hover_levels[0] = 1.0;
    app.shortcut_hover_levels[0] = 1.0; // '/' shortcut hovered
    app.toggle_hover_level = 1.0;

    let mut buf_hover = Buffer::empty(area);
    start::render(&app, &theme, &anim, area, &mut buf_hover);

    // 1. Prompt input box border color lerps to #666666 (102, 102, 102)
    let prompt_hover_cell = &buf_hover[(prompt_rect.x, prompt_rect.y)];
    assert_eq!(prompt_hover_cell.fg, Color::Rgb(102, 102, 102), "Prompt border should be #666666 when hovered");

    // 2. Shortcut badge '/' is highlighted with blue accent and underlined
    let hints_rect = app.get_start_hints_area(area);
    // Find '/' badge character in hints area
    let mut found_underlined_slash = false;
    for x in hints_rect.x..hints_rect.x + hints_rect.width {
        let cell = &buf_hover[(x, hints_rect.y)];
        if cell.symbol() == "/" {
            assert_eq!(cell.fg, theme.accent_blue, "Shortcut badge key should have accent_blue fg");
            assert!(cell.modifier.contains(Modifier::UNDERLINED), "Shortcut badge key should be underlined on hover");
            found_underlined_slash = true;
            break;
        }
    }
    assert!(found_underlined_slash, "Should find underlined '/' shortcut badge");

    // 3. Resume session row smooth background pill lerps to #151515 and title to #f2f2f2
    let row0_hover_cell = &buf_hover[(resume_rect.x + 2, row0_y)];
    assert_eq!(row0_hover_cell.bg, Color::Rgb(21, 21, 21), "Row 0 bg should be #151515 when hovered");
    assert_eq!(row0_hover_cell.fg, Color::Rgb(242, 242, 242), "Row 0 title should be #f2f2f2 when hovered");

    // 4. Delete button '✕' glowing micro-pulse animation on direct hover
    let del_cell_hover = &buf_hover[(resume_rect.x + resume_rect.width - 3, row0_y)];
    assert_eq!(del_cell_hover.symbol(), "✕");
    assert!(del_cell_hover.modifier.contains(Modifier::BOLD), "Delete button should be BOLD on direct hover");
    let pulse_target = anim.pulse_color(Color::Rgb(255, 123, 114), Color::Rgb(255, 180, 180));
    assert_eq!(del_cell_hover.fg, pulse_target, "Delete button should render pulsing rose glow on direct hover");

    // 5. Toggle '+ show all' row animated background and accent blue highlight
    let toggle_y = resume_rect.y + 1 + 3; // 3 visible sessions + 1
    let toggle_cell = &buf_hover[(resume_rect.x + 2, toggle_y)];
    assert_eq!(toggle_cell.bg, Color::Rgb(21, 21, 21), "Toggle bg should be #151515 when active");
    assert_eq!(toggle_cell.fg, theme.accent_blue, "Toggle fg should be accent_blue when active");
    assert!(toggle_cell.modifier.contains(Modifier::BOLD), "Toggle text should be BOLD when active");
}

#[test]
fn mode_labels_follow_tab_and_use_mode_colours_on_both_screens() {
    use hex_tui::ui::screens::error_view::DiagnosticFailure;

    for theme in [HexTheme::truecolor(), HexTheme::color_256()] {
        for width in [60, 120, 241] {
            for state in [ScreenState::Start, ScreenState::Session] {
                for failing in [false, true] {
                    if state == ScreenState::Start && failing {
                        continue;
                    }
                    let mut app = App::new("mode-test", "mode-fixture", "sandbox");
                    app.screen_state = state;
                    if failing {
                        app.active_diagnostic = Some(DiagnosticFailure::from_tool_error(
                            "terminal", "{}", r#"{"exit_code":1,"error":"test failed"}"#, 10,
                        ));
                    }
                    for (mode, colour) in [
                        ("agent", theme.accent_blue),
                        ("plan", theme.rose_fg),
                        ("code", theme.text_bright),
                        ("agent", theme.accent_blue),
                    ] {
                        assert_eq!(app.mode, mode);
                        let area = Rect::new(3, 2, width, 40);
                        let mut buf = Buffer::empty(area);
                        let anim = AnimationState::new();
                        if state == ScreenState::Start {
                            start::render(&app, &theme, &anim, area, &mut buf);
                        } else {
                            session::render(&app, &theme, &anim, area, &mut buf);
                        }
                        let needle = format!("{mode} · mode-fixture");
                        let (y, text) = (area.y..area.bottom()).map(|y| {
                            let text = (area.x..area.right()).map(|x| buf[(x, y)].symbol()).collect::<String>();
                            (y, text)
                        }).find(|(_, text)| text.contains(&needle)).expect("active mode must be visible");
                        let prefix = text.find(&needle).unwrap();
                        let x = area.x + text[..prefix].chars().count() as u16;
                        for offset in 0..mode.len() as u16 {
                            assert_eq!(buf[(x + offset, y)].fg, colour, "{state:?}/{mode}/{width}");
                        }
                        if failing {
                            assert!(text.contains("1 failing"), "failure badge must not replace the mode");
                        }
                        app.handle_tab();
                    }
                }
            }
        }
    }
}

#[test]
fn empty_composer_caret_stays_at_the_hint_start_when_typing_and_clearing() {
    use std::time::{Duration, Instant};
    use ratatui::style::Color;

    for width in [60, 120] {
        for theme in [HexTheme::truecolor(), HexTheme::color_256()] {
            for state in [ScreenState::Start, ScreenState::Session] {
                for visible in [true, false] {
                    let mut app = App::new("caret-test", "model", "sandbox");
                    app.screen_state = state;
                    let anim = AnimationState::with_start(if visible {
                        Instant::now() + Duration::from_secs(60)
                    } else {
                        Instant::now() - Duration::from_millis(700)
                    });
                    let area = Rect::new(3, 2, width, 40);
                    let hint = if state == ScreenState::Start {
                        anim.placeholder_prompt()
                    } else {
                        "Message hex"
                    };
                    let background = if state == ScreenState::Start {
                        Color::Rgb(12, 12, 12)
                    } else {
                        theme.card_bg
                    };
                    let render = |app: &App| {
                        let mut buf = Buffer::empty(area);
                        if state == ScreenState::Start {
                            start::render(app, &theme, &anim, area, &mut buf);
                        } else {
                            session::render(app, &theme, &anim, area, &mut buf);
                        }
                        buf
                    };
                    let row = |buf: &Buffer, y| {
                        (area.x..area.right()).map(|x| buf[(x, y)].symbol()).collect::<String>()
                    };
                    let empty = render(&app);
                    let needle = format!("> {hint}");
                    let y = (area.y..area.bottom()).find(|y| row(&empty, *y).contains(&needle)).unwrap();
                    let text = row(&empty, y);
                    let prefix = text.find(&needle).unwrap();
                    let x = area.x + text[..prefix].chars().count() as u16 + 2;
                    assert_eq!(empty[(x, y)].symbol(), &hint[..1]);
                    assert_eq!(empty[(x, y)].bg, if anim.cursor_visible() { theme.accent_blue } else { background });
                    assert_eq!(empty[(x + 1, y)].bg, background, "the hint must not shift right");
                    assert!(!text.contains('█'), "no caret may remain after the placeholder");

                    app.insert_char('世');
                    let typed = render(&app);
                    assert_eq!(typed[(x, y)].symbol(), "世", "typing must start at the hint's first column");
                    assert_eq!(typed[(x, y)].bg, background);
                    assert!(!row(&typed, y).contains(hint), "typing must remove the entire hint");
                    if anim.cursor_visible() {
                        assert_eq!(typed[(x + 2, y)].symbol(), "█", "typed caret follows display width");
                        assert_eq!(typed[(x + 2, y)].fg, theme.accent_blue);
                    }

                    app.delete_char();
                    let cleared = render(&app);
                    assert!(row(&cleared, y).contains(&needle));
                    assert_eq!(cleared[(x, y)].bg, if anim.cursor_visible() { theme.accent_blue } else { background });
                    assert_eq!(app.cursor_pos, 0);
                }
            }
        }
    }
}


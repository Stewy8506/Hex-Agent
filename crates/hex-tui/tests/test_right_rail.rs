//! Renderer acceptance contracts, exercised through terminal cells rather than source text.

use hex_tui::app::{App, ApprovalRequest, ChangedFile, PlanStep, StepStatus, SubagentTelemetry};
use hex_tui::sidebar::{Section, SectionPreference, SidebarAction, SidebarPreferences};
use hex_tui::ui::animation::AnimationState;
use hex_tui::ui::right_rail::{render, render_settings};
use hex_tui::ui::style::HexTheme;
use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Style};
use ratatui::Terminal;
use unicode_width::UnicodeWidthStr;

fn app() -> App {
    let mut app = App::with_workspace(
        "rail-test",
        "test-model",
        "sandbox",
        Some(std::path::Path::new("/__hex_right_rail_acceptance__")),
    );
    app.plan_steps.clear();
    app.changed_files.clear();
    app.subagents.clear();
    app.sidebar.preferences = SidebarPreferences::default();
    app.sidebar.show_all_changed = false;
    app.sidebar.scroll.set(0);
    app.active_approval = None;
    app.tokens_used = 0;
    app.max_tokens = 200_000;
    app
}

fn preference(section: Section, visible: bool, collapsed: bool) -> SectionPreference {
    SectionPreference {
        section,
        visible,
        collapsed,
    }
}

fn only(app: &mut App, section: Section) {
    app.sidebar.preferences.sections = vec![preference(section, true, false)];
}

fn paint(app: &App, theme: &HexTheme, area: Rect) -> Buffer {
    let mut buf = Buffer::empty(area);
    render(app, theme, &AnimationState::new(), area, &mut buf);
    buf
}

// Skip the empty continuation cells of wide glyphs when collecting rendered text.
fn row(buf: &Buffer, area: Rect, y: u16) -> String {
    let mut text = String::new();
    let mut x = area.x;
    while x < area.right() {
        let symbol = buf[(x, y)].symbol();
        text.push_str(symbol);
        x += UnicodeWidthStr::width(symbol).max(1) as u16;
    }
    text.trim_end().to_string()
}

fn rows(buf: &Buffer, area: Rect) -> Vec<String> {
    (area.y..area.bottom()).map(|y| row(buf, area, y)).collect()
}

fn find_row(buf: &Buffer, area: Rect, needle: &str) -> u16 {
    (area.y..area.bottom())
        .find(|y| row(buf, area, *y).contains(needle))
        .unwrap_or_else(|| panic!("missing {needle:?}:\n{}", rows(buf, area).join("\n")))
}

fn symbol_x(buf: &Buffer, area: Rect, y: u16, symbol: &str) -> u16 {
    (area.x..area.right())
        .find(|x| buf[(*x, y)].symbol() == symbol)
        .unwrap()
}

fn changes(count: usize) -> Vec<ChangedFile> {
    (0..count)
        .map(|index| ChangedFile {
            path: format!("src/file{index}.rs"),
            additions: 12,
            deletions: 3,
        })
        .collect()
}

fn approval() -> ApprovalRequest {
    ApprovalRequest {
        id: "approval-test".into(),
        risk_level: "high".into(),
        tool_name: "terminal".into(),
        parameters: String::new(),
        reason: String::new(),
        blast_radius: String::new(),
        cwd: String::new(),
    }
}

#[test]
fn six_changes_and_clickable_expansion_in_both_palettes() {
    for theme in [HexTheme::truecolor(), HexTheme::color_256()] {
        for width in [28, 44, 60] {
            let mut app = app();
            only(&mut app, Section::Changed);
            app.changed_files = changes(10);
            let area = Rect::new(0, 0, width, 30);
            let collapsed = paint(&app, &theme, area);
            let text = rows(&collapsed, area).join("\n");
            for index in 0..6 {
                assert!(text.contains(&format!("file{index}.rs")));
            }
            for index in 6..10 {
                assert!(!text.contains(&format!("file{index}.rs")));
            }
            assert_eq!(
                rows(&collapsed, area)
                    .iter()
                    .filter(|r| r.contains("+12 −3"))
                    .count(),
                6
            );
            assert!(text.contains("changed 10"));
            assert!(text.contains("+ 4 more"));
            assert_eq!(app.sidebar.metrics.get().0, 8);
            let (target, _) = app
                .sidebar
                .hit_targets
                .borrow()
                .iter()
                .find(|(_, action)| *action == SidebarAction::ToggleChanges)
                .copied()
                .unwrap();
            assert_eq!(row(&collapsed, target, target.y), "+ 4 more");
            assert_eq!(target.width, 8);
            assert_eq!(collapsed[(target.x, target.y)].fg, theme.accent_blue);

            app.sidebar.show_all_changed = true;
            let expanded = paint(&app, &theme, area);
            let text = rows(&expanded, area).join("\n");
            for index in 0..10 {
                assert!(text.contains(&format!("file{index}.rs")));
            }
            assert_eq!(
                rows(&expanded, area)
                    .iter()
                    .filter(|r| r.contains("+12 −3"))
                    .count(),
                10
            );
            assert!(text.contains("− show less"));
            assert!(!text.contains("+ 4 more"));
            assert_eq!(app.sidebar.metrics.get().0, 12);
            let target = app
                .sidebar
                .hit_targets
                .borrow()
                .iter()
                .find(|(_, action)| *action == SidebarAction::ToggleChanges)
                .unwrap()
                .0;
            assert_eq!(row(&expanded, target, target.y), "− show less");
            assert_eq!(target.width, 11);
        }
    }
}

#[test]
fn six_or_fewer_changes_have_no_overflow_action() {
    let mut app = app();
    only(&mut app, Section::Changed);
    for count in [1, 6] {
        app.changed_files = changes(count);
        let area = Rect::new(0, 0, 28, 20);
        let buf = paint(&app, &HexTheme::truecolor(), area);
        assert!(!rows(&buf, area).join("\n").contains("more"));
        assert!(!app
            .sidebar
            .hit_targets
            .borrow()
            .iter()
            .any(|(_, action)| *action == SidebarAction::ToggleChanges));
        assert_eq!(app.sidebar.metrics.get().0, count + 1);
    }
}

#[test]
fn unicode_paths_preserve_basename_and_never_enter_diff_columns() {
    for theme in [HexTheme::truecolor(), HexTheme::color_256()] {
        for width in [28, 44, 60] {
            let mut app = app();
            only(&mut app, Section::Changed);
            app.changed_files = [
                "a/very/long/directory/that/cannot/possibly/fit/a.rs",
                "目录/特别长的目录/还有另外一个目录/界.rs",
                "cafe\u{301}/a/very/long/combining/directory/e\u{301}.rs",
                "plain.rs",
            ]
            .into_iter()
            .map(|path| ChangedFile {
                path: path.into(),
                additions: 12,
                deletions: 3,
            })
            .collect();
            let area = Rect::new(5, 3, width, 20);
            let buf = paint(&app, &theme, area);
            for (index, basename) in ["a.rs", "界.rs", "e\u{301}.rs", "plain.rs"]
                .into_iter()
                .enumerate()
            {
                let y = area.y + 2 + index as u16;
                let text = row(&buf, area, y);
                assert!(text.contains(basename), "{text:?}");
                assert!(text.ends_with("+12 −3"), "{text:?}");
                assert_eq!(symbol_x(&buf, area, y, "+"), area.right() - 8);
                assert_eq!(symbol_x(&buf, area, y, "−"), area.right() - 4);
                assert_eq!(buf[(area.right() - 9, y)].symbol(), " ");
                assert_eq!(buf[(area.right() - 8, y)].fg, theme.sage_fg);
                assert_eq!(buf[(area.right() - 4, y)].fg, theme.rose_fg);
                assert!(UnicodeWidthStr::width(text.as_str()) <= width as usize - 2);
                let source_width = UnicodeWidthStr::width(app.changed_files[index].path.as_str());
                if source_width > width as usize - 11 {
                    assert!(text.contains('…'));
                } else {
                    assert!(text.contains(&app.changed_files[index].path));
                }
                for x in [area.x, area.x + 1, area.right() - 2, area.right() - 1] {
                    assert_eq!(buf[(x, y)].symbol(), " ");
                }
            }
        }
    }
}

#[test]
fn mixed_size_change_counters_share_reserved_columns() {
    for theme in [HexTheme::truecolor(), HexTheme::color_256()] {
        for width in [28, 44, 60] {
            let mut app = app();
            only(&mut app, Section::Changed);
            app.changed_files = [(1, 999), (1234, 2), (12, 34)]
                .into_iter()
                .map(|(additions, deletions)| ChangedFile {
                    path: "src/long/directory/file.rs".into(),
                    additions,
                    deletions,
                })
                .collect();
            let area = Rect::new(0, 0, width, 20);
            let buf = paint(&app, &theme, area);
            let positions: Vec<_> = (2..5)
                .map(|y| (symbol_x(&buf, area, y, "+"), symbol_x(&buf, area, y, "−")))
                .collect();
            assert!(positions.iter().all(|position| *position == positions[0]));
            assert!(row(&buf, area, 2).ends_with("+   1 −999"));
            assert!(row(&buf, area, 3).ends_with("+1234 −  2"));
            assert!(row(&buf, area, 4).ends_with("+  12 − 34"));
        }
    }
}

#[test]
fn long_basenames_and_giant_counters_stay_bounded() {
    for width in [7, 8, 12, 28, 44, 60] {
        let mut app = app();
        only(&mut app, Section::Changed);
        app.changed_files = vec![
            ChangedFile {
                path: "a_really_long_basename_that_requires_middle_truncation.rs".into(),
                additions: 1,
                deletions: 2,
            },
            ChangedFile {
                path: "deep/目录/cafe\u{301}/huge.rs".into(),
                additions: usize::MAX,
                deletions: usize::MAX,
            },
            ChangedFile {
                path: "unchanged.rs".into(),
                additions: 0,
                deletions: 0,
            },
        ];
        let area = Rect::new(4, 2, width, 12);
        let buf = paint(&app, &HexTheme::truecolor(), area);
        for y in area.y..area.bottom() {
            let text = row(&buf, area, y);
            assert!(UnicodeWidthStr::width(text.as_str()) <= width as usize);
            for x in [area.x, area.x + 1, area.right() - 2, area.right() - 1] {
                assert_eq!(buf[(x, y)].symbol(), " ");
            }
        }
        if width >= 28 {
            let long = row(&buf, area, area.y + 2);
            assert!(long.contains('…') && long.contains(".rs"));
            let huge = row(&buf, area, area.y + 3);
            assert!(huge.contains('+') && huge.contains('−') && huge.contains("huge.rs"));
            let counter_width = usize::MAX.to_string().len() * 2 + 3;
            if counter_width > width as usize - 6 {
                assert!(!huge.contains(&usize::MAX.to_string()));
            }
            assert_ne!(buf[(area.right() - 3, area.y + 3)].symbol(), " ");
            assert_eq!(
                buf[(area.right() - 3, area.y + 3)].fg,
                HexTheme::truecolor().rose_fg
            );
            assert_eq!(symbol_x(&buf, area, area.y + 4, "—"), area.right() - 3);
        }
    }
}

#[test]
fn complete_visual_content_is_reachable_by_independent_scroll() {
    for theme in [HexTheme::truecolor(), HexTheme::color_256()] {
        for width in [28, 44, 60] {
            let mut app = app();
            app.dialogue_scroll = 37;
            app.plan_steps = (0..12).map(|index| PlanStep {
                title: format!("step{index}: wrap this complete plan title across multiple visual lines\nfinish{index}"),
                status: if index == 0 { StepStatus::Completed } else if index == 1 { StepStatus::Active } else { StepStatus::Pending },
            }).collect();
            app.changed_files = changes(9);
            app.sidebar.show_all_changed = true;
            app.subagents = (0..9)
                .map(|index| SubagentTelemetry {
                    name: format!("agent{index} with an intentionally lengthy name"),
                    status: "pass".into(),
                    detail: format!(
                        "detail{index}: inspect every reachable source item\nagent-tail{index}"
                    ),
                    is_running: false,
                    has_failed: false,
                })
                .collect();
            let tall_area = Rect::new(3, 2, width, 400);
            let tall = paint(&app, &theme, tall_area);
            let total = app.sidebar.metrics.get().0;
            assert!(total < 399);
            let expected: Vec<String> = (0..total)
                .map(|offset| row(&tall, tall_area, tall_area.y + 1 + offset as u16))
                .collect();
            let full = expected.join("\n");
            for index in 0..12 {
                assert!(full.contains(&format!("step{index}:")));
                assert!(full.contains(&format!("finish{index}")));
            }
            for index in 0..9 {
                assert!(full.contains(&format!("agent{index}")));
                assert!(full.contains(&format!("agent-tail{index}")));
                assert!(full.contains(&format!("file{index}.rs")));
            }
            assert!(full.contains("plan 1/12"));
            assert!(full.contains("agents 9"));
            let short_area = Rect::new(3, 2, width, 7);
            for requested in 0..=total {
                app.sidebar.scroll.set(requested);
                let short = paint(&app, &theme, short_area);
                let (short_total, viewport) = app.sidebar.metrics.get();
                assert_eq!(short_total, total);
                assert_eq!(viewport, 5);
                let scroll = requested.min(total - viewport);
                assert_eq!(app.sidebar.scroll.get(), scroll);
                let actual: Vec<String> = (0..viewport)
                    .map(|offset| row(&short, short_area, short_area.y + 1 + offset as u16))
                    .collect();
                assert_eq!(actual, expected[scroll..scroll + viewport]);
                assert!(row(&short, short_area, short_area.y).contains("sidebar"));
                assert!(row(&short, short_area, short_area.bottom() - 1).contains('↑'));
                assert!(row(&short, short_area, short_area.bottom() - 1).contains('↓'));
                for (target, action) in app.sidebar.hit_targets.borrow().iter() {
                    assert!(short_area.contains((target.x, target.y).into()));
                    assert!(target.right() <= short_area.right() - 2);
                    if *action != SidebarAction::Settings {
                        assert!(target.y > short_area.y && target.y < short_area.bottom() - 1);
                    }
                }
                assert_eq!(app.dialogue_scroll, 37);
            }
        }
    }
}

#[test]
fn plan_hanging_wrap_and_agent_status_columns_preserve_multiline_content() {
    for width in [28, 44, 60] {
        let mut app = app();
        app.sidebar.preferences.sections = vec![
            preference(Section::Plan, true, false),
            preference(Section::Agents, true, false),
        ];
        let title = "first words and more words with 界 and cafe\u{301}\nlast line";
        app.plan_steps = vec![PlanStep {
            title: title.into(),
            status: StepStatus::Active,
        }];
        app.subagents = vec![SubagentTelemetry {
            name: "named worker with a long name\nname-tail".into(),
            status: "pass".into(),
            detail: "detail has a long explanation with 界 and cafe\u{301}\ndetail-tail".into(),
            is_running: false,
            has_failed: false,
        }];
        let area = Rect::new(0, 0, width, 80);
        let buf = paint(&app, &HexTheme::truecolor(), area);
        let agent_header = find_row(&buf, area, "agents 1");
        let plan_start = find_row(&buf, area, "[>]");
        let mut rendered_title = String::new();
        for y in plan_start..agent_header - 1 {
            let line = row(&buf, area, y);
            assert!(line.starts_with(if y == plan_start { "  [>] " } else { "      " }));
            rendered_title.push_str(line.get(6..).unwrap_or(""));
        }
        let compact = |text: &str| {
            text.chars()
                .filter(|c| !c.is_whitespace())
                .collect::<String>()
        };
        assert_eq!(compact(&rendered_title), compact(title));
        let name_y = find_row(&buf, area, "named worker");
        assert_eq!(symbol_x(&buf, area, name_y, "p"), area.right() - 6);
        assert!(rows(&buf, area).join("\n").contains("name-tail"));
        assert!(rows(&buf, area).join("\n").contains("detail-tail"));
        assert!(rows(&buf, area).join("\n").contains("cafe\u{301}"));
    }
}

#[test]
fn empty_sections_are_compact_and_have_no_unneeded_bottom_margin() {
    let app = app();
    let area = Rect::new(0, 0, 28, 13);
    let buf = paint(&app, &HexTheme::truecolor(), area);
    assert_eq!(app.sidebar.metrics.get(), (12, 12));
    assert_eq!(find_row(&buf, area, "context"), 1);
    assert_eq!(find_row(&buf, area, "plan 0/0"), 5);
    assert_eq!(find_row(&buf, area, "changed 0"), 8);
    assert_eq!(find_row(&buf, area, "agents 0"), 11);
    assert!(row(&buf, area, 12).contains("none active"));
    let text = rows(&buf, area).join("\n");
    assert!(text.contains("no active plan") && text.contains("working tree clean"));
    assert!(!text.contains('↑') && !text.contains('↓') && !text.contains('─'));
}

#[test]
fn blue_markers_counts_toolbar_and_clamped_context_in_both_palettes() {
    for theme in [HexTheme::truecolor(), HexTheme::color_256()] {
        for width in [28, 44, 60] {
            let mut app = app();
            app.plan_steps = vec![
                PlanStep {
                    title: "completed".into(),
                    status: StepStatus::Completed,
                },
                PlanStep {
                    title: "active".into(),
                    status: StepStatus::Active,
                },
            ];
            app.subagents = vec![SubagentTelemetry {
                name: "running".into(),
                status: "working".into(),
                detail: String::new(),
                is_running: true,
                has_failed: false,
            }];
            let area = Rect::new(0, 0, width, 30);
            for (tokens, maximum, percentage, filled) in [
                (0, 200_000, "0%", false),
                (usize::MAX, 0, "0%", false),
                (usize::MAX, 1, "100%", true),
            ] {
                app.tokens_used = tokens;
                app.max_tokens = maximum;
                let buf = paint(&app, &theme, area);
                let context_y = find_row(&buf, area, "context");
                for x in 2..width - 2 {
                    assert_eq!(
                        buf[(x, context_y + 1)].symbol(),
                        if filled { "█" } else { "░" }
                    );
                    assert_eq!(
                        buf[(x, context_y + 1)].fg,
                        if filled {
                            theme.accent_blue
                        } else {
                            theme.text_extra_dim
                        }
                    );
                }
                assert!(row(&buf, area, context_y + 2).contains(percentage));
                let plan_y = find_row(&buf, area, "plan 1/2");
                assert_eq!(buf[(2, plan_y)].fg, theme.accent_blue);
                assert_eq!(buf[(4, plan_y)].fg, theme.text_dim);
                assert_eq!(buf[(9, plan_y)].fg, theme.accent_blue);
                let completed_y = find_row(&buf, area, "[x] completed");
                assert_eq!(buf[(2, completed_y)].fg, theme.text_extra_dim);
                let active_y = find_row(&buf, area, "[>] active");
                for x in 2..5 {
                    assert_eq!(buf[(x, active_y)].fg, theme.accent_blue);
                }
                let running_y = find_row(&buf, area, "running");
                assert_eq!(buf[(width - 3, running_y)].fg, theme.accent_blue);
                let target = app
                    .sidebar
                    .hit_targets
                    .borrow()
                    .iter()
                    .find(|(_, action)| *action == SidebarAction::Settings)
                    .unwrap()
                    .0;
                assert_eq!(target, Rect::new(width - 6, 0, 4, 1));
                assert_eq!(row(&buf, target, target.y), "[f4]");
                assert!(!target.contains((2, 0).into()));
            }
        }
    }
}

#[test]
fn preferences_reorder_hide_collapse_and_clamp_stale_scroll() {
    let mut app = app();
    app.plan_steps = vec![PlanStep {
        title: "hidden plan body".into(),
        status: StepStatus::Pending,
    }];
    app.subagents = vec![SubagentTelemetry {
        name: "first visible worker".into(),
        status: "pass".into(),
        detail: String::new(),
        is_running: false,
        has_failed: false,
    }];
    app.changed_files = changes(8);
    app.sidebar.preferences.sections = vec![
        preference(Section::Agents, true, false),
        preference(Section::Changed, true, true),
        preference(Section::Plan, false, false),
        preference(Section::Context, true, true),
    ];
    app.sidebar.scroll.set(usize::MAX);
    let area = Rect::new(5, 4, 44, 25);
    let mut buf = paint(&app, &HexTheme::truecolor(), area);
    assert_eq!(app.sidebar.scroll.get(), 0);
    assert_eq!(app.sidebar.metrics.get().0, 6);
    assert!(find_row(&buf, area, "agents 1") < find_row(&buf, area, "changed 8"));
    assert!(find_row(&buf, area, "changed 8") < find_row(&buf, area, "context"));
    let text = rows(&buf, area).join("\n");
    assert!(!text.contains("plan") && !text.contains("file0.rs") && !text.contains('░'));
    assert_eq!(buf[(7, find_row(&buf, area, "changed 8"))].symbol(), "▸");
    let actions: Vec<_> = app
        .sidebar
        .hit_targets
        .borrow()
        .iter()
        .map(|(_, action)| *action)
        .collect();
    assert!(!actions.contains(&SidebarAction::ToggleSection(Section::Plan)));
    assert!(!actions.contains(&SidebarAction::ToggleChanges));

    for preference in &mut app.sidebar.preferences.sections {
        preference.visible = false;
    }
    render(
        &app,
        &HexTheme::truecolor(),
        &AnimationState::new(),
        area,
        &mut buf,
    );
    assert_eq!(app.sidebar.metrics.get().0, 0);
    assert_eq!(app.sidebar.hit_targets.borrow().len(), 1);
    assert!(
        !rows(&buf, area).join("\n").contains("worker"),
        "stale content must be cleared"
    );
}

#[test]
fn scrolled_click_targets_follow_painted_rows_and_drop_offscreen_actions() {
    let mut app = app();
    only(&mut app, Section::Changed);
    app.changed_files = changes(10);
    let area = Rect::new(8, 6, 28, 5);
    let first = paint(&app, &HexTheme::truecolor(), area);
    let targets = app.sidebar.hit_targets.borrow().clone();
    assert!(targets
        .iter()
        .any(|(_, a)| *a == SidebarAction::ToggleSection(Section::Changed)));
    assert!(!targets
        .iter()
        .any(|(_, a)| *a == SidebarAction::ToggleChanges));
    assert!(row(&first, area, area.bottom() - 1).contains("1–3/8"));
    app.sidebar.scroll.set(usize::MAX);
    let last = paint(&app, &HexTheme::truecolor(), area);
    let targets = app.sidebar.hit_targets.borrow().clone();
    assert_eq!(app.sidebar.scroll.get(), 5);
    assert!(!targets
        .iter()
        .any(|(_, a)| *a == SidebarAction::ToggleSection(Section::Changed)));
    let target = targets
        .iter()
        .find(|(_, a)| *a == SidebarAction::ToggleChanges)
        .unwrap()
        .0;
    assert_eq!(target, Rect::new(area.x + 2, area.y + 3, 8, 1));
    assert_eq!(row(&last, target, target.y), "+ 4 more");
    assert!(row(&last, area, area.bottom() - 1).contains("6–8/8"));
}

#[test]
fn approval_waiting_text_is_preserved_and_wrapped_when_needed() {
    for width in [28, 44, 60] {
        let mut app = app();
        only(&mut app, Section::Agents);
        app.active_approval = Some(approval());
        let area = Rect::new(0, 0, width, 20);
        let theme = HexTheme::truecolor();
        let buf = paint(&app, &theme, area);
        let y = find_row(&buf, area, "blocked / waiting on you");
        assert_eq!(buf[(2, y)].fg, theme.accent_blue);
    }
    let mut app = app();
    only(&mut app, Section::Agents);
    app.active_approval = Some(approval());
    let area = Rect::new(0, 0, 12, 30);
    let buf = paint(&app, &HexTheme::truecolor(), area);
    let text = rows(&buf, area).join("\n");
    for word in ["blocked", "waiting", "on you"] {
        assert!(text.contains(word));
    }
}

#[test]
fn customization_panel_fits_sixty_by_twenty_and_shows_real_controls() {
    for theme in [HexTheme::truecolor(), HexTheme::color_256()] {
        let mut app = app();
        app.sidebar.show_settings = true;
        app.sidebar.preferences.width = Some(44);
        app.sidebar.preferences.sections = vec![
            preference(Section::Agents, true, true),
            preference(Section::Changed, false, false),
            preference(Section::Context, true, false),
            preference(Section::Plan, true, false),
        ];
        app.sidebar.selected_section = 1;
        let area = Rect::new(5, 3, 60, 20);
        let mut buf = Buffer::empty(area);
        render_settings(&app, &theme, area, &mut buf);
        let text = rows(&buf, area).join("\n");
        for instruction in [
            "Customize sidebar",
            "width: 44 cols",
            "Up/Down select",
            "space show/hide",
            "c collapse",
            ",/. reorder",
            "+/- width",
            "a auto-width",
            "r reset",
            "Esc/F4 close",
            "Tab cycles operating mode",
        ] {
            assert!(
                text.contains(instruction),
                "missing {instruction:?}:\n{text}"
            );
        }
        assert!(find_row(&buf, area, "agents") < find_row(&buf, area, "changed"));
        assert!(find_row(&buf, area, "changed") < find_row(&buf, area, "context"));
        assert!(find_row(&buf, area, "context") < find_row(&buf, area, "plan"));
        assert!(text.contains("[x] agents  ▸ collapsed"));
        assert!(text.contains("[ ] changed  ▾ expanded"));
        let y = find_row(&buf, area, "changed");
        let x = symbol_x(&buf, area, y, "›");
        assert_eq!(buf[(x, y)].fg, theme.accent_blue);
        assert_eq!(buf[(x, y)].bg, theme.selection_bg);
        app.sidebar.preferences.width = None;
        render_settings(&app, &theme, area, &mut buf);
        assert!(rows(&buf, area).join("\n").contains("width: auto"));
    }
}

#[test]
fn bounds_nonzero_origins_and_tiny_areas_clear_stale_geometry() {
    let mut app = app();
    app.plan_steps = vec![PlanStep {
        title: "界界\ncombining e\u{301} words".into(),
        status: StepStatus::Active,
    }];
    app.changed_files = changes(8);
    app.subagents = vec![SubagentTelemetry {
        name: "界界 long worker".into(),
        status: "unusually long status".into(),
        detail: "multiline\n界 detail".into(),
        is_running: false,
        has_failed: false,
    }];
    let theme = HexTheme::truecolor();
    let buffer_area = Rect::new(2, 3, 70, 35);
    for width in [0, 1, 2, 3, 4, 5, 6, 7, 12, 28, 44, 60] {
        for height in [0, 1, 2, 3, 6, 20] {
            let area = Rect::new(6, 7, width, height);
            let mut buf = Buffer::filled(
                buffer_area,
                ratatui::buffer::Cell::default()
                    .set_symbol("·")
                    .set_style(Style::default().fg(Color::Magenta))
                    .clone(),
            );
            let before = buf.clone();
            app.sidebar.scroll.set(usize::MAX);
            app.sidebar
                .hit_targets
                .borrow_mut()
                .push((Rect::new(0, 0, 1, 1), SidebarAction::ToggleChanges));
            render(&app, &theme, &AnimationState::new(), area, &mut buf);
            assert_eq!(app.sidebar.area.get(), area);
            for y in buffer_area.y..buffer_area.bottom() {
                for x in buffer_area.x..buffer_area.right() {
                    if !area.contains((x, y).into()) {
                        assert_eq!(
                            buf[(x, y)],
                            before[(x, y)],
                            "outside {area:?} at ({x}, {y})"
                        );
                    }
                }
            }
            for (target, _) in app.sidebar.hit_targets.borrow().iter() {
                assert!(target.width > 0 && target.height == 1);
                assert!(area.contains((target.x, target.y).into()));
                assert!(target.right() <= area.right());
            }
            if width <= 4 || height == 0 {
                assert!(app.sidebar.hit_targets.borrow().is_empty());
            }
            let (total, viewport) = app.sidebar.metrics.get();
            assert!(app.sidebar.scroll.get() <= total.saturating_sub(viewport));
            render_settings(&app, &theme, area, &mut buf);
            for y in buffer_area.y..buffer_area.bottom() {
                for x in buffer_area.x..buffer_area.right() {
                    if !area.contains((x, y).into()) {
                        assert_eq!(buf[(x, y)], before[(x, y)]);
                    }
                }
            }
        }
    }
}

#[test]
fn direct_test_backend_render_matches_buffer_render() {
    for theme in [HexTheme::truecolor(), HexTheme::color_256()] {
        for width in [28, 44, 60] {
            let mut app = app();
            app.changed_files = changes(10);
            let area = Rect::new(0, 0, width, 20);
            let expected = paint(&app, &theme, area);
            let mut terminal = Terminal::new(TestBackend::new(width, 20)).unwrap();
            terminal
                .draw(|frame| {
                    render(
                        &app,
                        &theme,
                        &AnimationState::new(),
                        area,
                        frame.buffer_mut(),
                    )
                })
                .unwrap();
            assert_eq!(
                rows(terminal.backend().buffer(), area),
                rows(&expected, area)
            );
        }
    }
}

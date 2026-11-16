//! The resume list must be navigable: arrows, wheel and page keys move the
//! highlight and scroll the window, even when the mouse is parked over a row.

use hex_tui::app::{App, ResumeSession};
use hex_tui::ui::animation::AnimationState;
use hex_tui::ui::screens::start;
use hex_tui::ui::style::HexTheme;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;

fn app_with(count: usize) -> App {
    let mut app = App::new("proj", "", "apfs_clone");
    app.resume_sessions = (0..count)
        .map(|i| ResumeSession {
            session_id: format!("tsk_{i}"),
            title: format!("task {i}"),
            relative_time: "1m".to_string(),
        })
        .collect();
    app
}

fn settle(app: &mut App) {
    for _ in 0..40 {
        app.advance_animation();
    }
}

fn resume_rows(app: &App, area: Rect) -> Vec<String> {
    let mut buf = Buffer::empty(area);
    start::render(app, &HexTheme::truecolor(), &AnimationState::new(), area, &mut buf);
    let ra = app.get_start_resume_area(area);
    let mut out = Vec::new();
    for y in ra.y..ra.bottom() {
        let mut line = String::new();
        for x in ra.x..ra.right() {
            line.push_str(buf[(x, y)].symbol());
        }
        out.push(line.trim_end().to_string());
    }
    out
}

fn highlighted_row(app: &App) -> Option<usize> {
    app.row_hover_levels
        .iter()
        .position(|&level| level > 0.9)
}

#[test]
fn test_arrows_move_highlight_while_mouse_is_parked_on_a_row() {
    let mut app = app_with(234);
    app.show_all_resume = true;
    let area = Rect::new(0, 0, 100, 34);
    let ra = app.get_start_resume_area(area);

    // Park the pointer on the last visible row, as a real user's mouse would be.
    app.handle_mouse_move(ra.x + 10, ra.y + 10, area);
    settle(&mut app);
    assert_eq!(highlighted_row(&app), Some(9), "mouse hover highlights row 9");

    for _ in 0..5 {
        app.move_resume_selection(1);
        settle(&mut app);
    }
    assert_eq!(app.selected_resume_index, 5);
    assert_eq!(
        highlighted_row(&app),
        Some(5),
        "keyboard must take over from a stale mouse hover"
    );
}

#[test]
fn test_arrows_scroll_beyond_the_first_window() {
    let mut app = app_with(234);
    app.show_all_resume = true;
    let area = Rect::new(0, 0, 100, 34);

    for _ in 0..15 {
        app.move_resume_selection(1);
    }
    settle(&mut app);

    let (offset, count) = app.resume_window();
    assert_eq!((offset, count), (6, 10), "window follows the selection");
    let rows = resume_rows(&app, area);
    assert!(rows[1].starts_with("  task 6"), "first drawn row is task 6: {:?}", rows[1]);
    assert_eq!(highlighted_row(&app), Some(9), "selection stays inside the window");

    // Scrolling back up returns to the top of the history.
    for _ in 0..15 {
        app.move_resume_selection(-1);
    }
    settle(&mut app);
    assert_eq!(app.resume_window().0, 0);
    assert!(resume_rows(&app, area)[1].starts_with("  task 0"));
}

#[test]
fn test_wheel_scroll_expands_the_preview_and_reaches_the_end() {
    let mut app = app_with(234);
    let area = Rect::new(0, 0, 100, 34);
    assert!(!app.show_all_resume);

    app.scroll_resume(3);
    assert!(app.show_all_resume, "scrolling reveals the rest of the history");
    settle(&mut app);

    for _ in 0..100 {
        app.scroll_resume(3);
    }
    settle(&mut app);
    let (offset, count) = app.resume_window();
    assert_eq!(offset + count, 234, "the last session is reachable by wheel");
    let rows = resume_rows(&app, area);
    assert!(rows.iter().any(|r| r.starts_with("  task 233")), "last row drawn: {rows:?}");

    // Scrolling up clamps at the top instead of wrapping to the bottom.
    for _ in 0..100 {
        app.scroll_resume(-3);
    }
    assert_eq!(app.selected_resume_index, 0);
    assert_eq!(app.resume_window().0, 0);
}

#[test]
fn test_page_keys_jump_one_window() {
    let mut app = app_with(234);
    app.show_all_resume = true;
    let page = app.resume_visible_count() as i32;

    app.scroll_resume(page);
    assert_eq!(app.selected_resume_index, 10);
    assert_eq!(app.resume_window().0, 1);

    app.scroll_resume(-page);
    assert_eq!(app.selected_resume_index, 0);
    assert_eq!(app.resume_window().0, 0);
}

#[test]
fn test_toggle_row_is_reachable_by_keyboard_in_both_directions() {
    let mut app = app_with(234);
    app.show_all_resume = true;
    let toggle = app.resume_toggle_index().expect("toggle exists");
    assert_eq!(toggle, 234, "toggle sits one past the last session");

    // Up from the first session wraps onto the toggle row.
    app.move_resume_selection(-1);
    assert_eq!(app.selected_resume_index, toggle);
    settle(&mut app);
    assert!(app.toggle_hover_level > 0.9, "toggle row shows the keyboard selection");
    assert_eq!(app.resume_window().0 + app.resume_visible_count(), 234);

    // Down from the toggle wraps back to the first session.
    app.move_resume_selection(1);
    assert_eq!(app.selected_resume_index, 0);
    settle(&mut app);
    assert_eq!(highlighted_row(&app), Some(0));

    // Collapsing keeps the selection inside the shortened list.
    app.selected_resume_index = toggle;
    app.toggle_show_all_resume();
    assert!(!app.show_all_resume);
    assert!(app.selected_resume_index <= app.resume_toggle_index().unwrap());
}

#[test]
fn test_mouse_hover_still_wins_after_the_mouse_moves_again() {
    let mut app = app_with(234);
    app.show_all_resume = true;
    let area = Rect::new(0, 0, 100, 34);
    let ra = app.get_start_resume_area(area);

    app.move_resume_selection(1);
    settle(&mut app);
    assert_eq!(highlighted_row(&app), Some(1));

    app.handle_mouse_move(ra.x + 10, ra.y + 3, area);
    settle(&mut app);
    assert_eq!(highlighted_row(&app), Some(2), "moving the mouse takes over again");

    // Hovering the toggle row highlights it, not a session.
    app.handle_mouse_move(ra.x + 10, ra.y + 11, area);
    settle(&mut app);
    assert!(app.toggle_hover_level > 0.9);
    assert_eq!(highlighted_row(&app), None);
}
//! G04: the permission modal — command chip, blast radius, radio selection,
//! hotkey alignment, and the waiting states around it.

use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::Terminal;

use hex_tui::app::{App, ApprovalRequest, ScreenState};
use hex_tui::ui;
use hex_tui::ui::animation::AnimationState;
use hex_tui::ui::style::HexTheme;

fn theme() -> HexTheme {
    HexTheme::detect()
}

fn render(app: &App, width: u16, height: u16) -> Buffer {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).expect("backend");
    terminal.draw(|f| ui::draw(f, app)).expect("draw");
    terminal.backend().buffer().clone()
}

fn rows(buf: &Buffer) -> Vec<String> {
    (0..buf.area.height)
        .map(|y| (0..buf.area.width).map(|x| buf[(x, y)].symbol()).collect())
        .collect()
}

fn cell(buf: &Buffer, x: usize, y: usize) -> &ratatui::buffer::Cell {
    &buf[(x as u16, y as u16)]
}

/// Column of a needle, counted in characters (box glyphs are multi-byte).
fn col(row: &str, needle: &str) -> usize {
    let hay: Vec<char> = row.chars().collect();
    let pat: Vec<char> = needle.chars().collect();
    hay.windows(pat.len())
        .position(|w| w == pat.as_slice())
        .unwrap_or_else(|| panic!("row does not contain {needle:?}: {row}"))
}

fn row_of(screen: &[String], needle: &str) -> usize {
    screen
        .iter()
        .position(|r| r.contains(needle))
        .unwrap_or_else(|| panic!("no row contains {needle:?}\n{}", screen.join("\n")))
}

fn approval_app() -> App {
    let mut app = App::new("harness", "gemini-2.5-pro", "OverlayFS");
    app.screen_state = ScreenState::Session;
    app.set_approval_request(ApprovalRequest {
        id: "appr_9".to_string(),
        risk_level: "R3".to_string(),
        tool_name: "terminal".to_string(),
        parameters: "rm -rf ./dist".to_string(),
        reason: "generic reason".to_string(),
        blast_radius: "Deletes 214 build artifacts under ./dist".to_string(),
        cwd: "/tmp/project".to_string(),
    });
    app
}

#[test]
fn chip_carries_the_command_and_the_working_directory() {
    let app = approval_app();
    let buf = render(&app, 120, 36);
    let screen = rows(&buf);
    let chip_row = row_of(&screen, "$ rm -rf ./dist");
    assert!(screen[chip_row].contains("/tmp/project"), "cwd must ride along\n{}", screen[chip_row]);

    let cmd_col = col(&screen[chip_row], "rm");
    assert_eq!(cell(&buf, cmd_col, chip_row).bg, theme().hover_bg, "chip sits on #151515");
    assert_eq!(cell(&buf, cmd_col, chip_row).fg, theme().text_bright);
}

#[test]
fn impact_line_prefers_the_blast_radius_over_the_generic_reason() {
    let app = approval_app();
    let screen = rows(&render(&app, 120, 36));
    assert!(screen.iter().any(|r| r.contains("Deletes 214 build artifacts")));
    assert!(!screen.iter().any(|r| r.contains("generic reason")));
}

#[test]
fn a_long_impact_sentence_is_clipped_inside_the_modal() {
    let mut app = approval_app();
    if let Some(req) = app.active_approval.as_mut() {
        req.blast_radius = "x".repeat(300);
    }
    let screen = rows(&render(&app, 120, 36));
    let impact_row = row_of(&screen, "xxxx");
    let border_row = row_of(&screen, "Permission required") - 1;
    let left = col(&screen[border_row], "╭");
    let right = col(&screen[border_row], "╮");
    let chars: Vec<char> = screen[impact_row].chars().collect();
    assert_eq!(chars[right], '│', "the modal border must survive the impact line");
    assert_eq!(chars[right - 1], '…', "the cut must be marked just inside the border");
    assert!(chars[left + 1..right - 1].iter().all(|c| *c == 'x'));
}

#[test]
fn exactly_one_option_is_tinted_and_marked_in_blue() {
    let mut app = approval_app();
    app.selected_permission_index = 1;
    let buf = render(&app, 120, 36);
    let screen = rows(&buf);
    let t = theme();

    for (label, selected) in [
        ("Allow once", false),
        ("Allow rm in this project", true),
        ("Deny", false),
    ] {
        let y = row_of(&screen, label);
        let x = col(&screen[y], label);
        let expected_bg = if selected { t.selection_bg } else { t.card_bg };
        assert_eq!(cell(&buf, x, y).bg, expected_bg, "{label} row background");
        let marker = cell(&buf, x - 2, y);
        if selected {
            assert_eq!(marker.symbol(), ">");
            assert_eq!(marker.fg, t.accent_blue);
            assert_eq!(cell(&buf, x, y).fg, t.text_bright);
        } else {
            assert_ne!(marker.fg, t.accent_blue, "{label} must not look selected");
        }
    }
}

#[test]
fn hotkeys_are_right_aligned_on_the_option_rows() {
    let app = approval_app();
    let buf = render(&app, 120, 36);
    let screen = rows(&buf);
    let border_row = row_of(&screen, "Permission required") - 1;
    let right = col(&screen[border_row], "╮") - 1;
    for (label, key) in [("Allow once", "y"), ("Allow rm in this project", "a"), ("Deny", "n")] {
        let y = row_of(&screen, label);
        assert_eq!(cell(&buf, right, y).symbol(), key, "{label} hotkey must sit at the right edge");
        assert_eq!(cell(&buf, right, y).fg, theme().text_dim);
    }
}

#[test]
fn arrows_wrap_the_radio_and_a_new_request_starts_at_the_top() {
    let mut app = approval_app();
    assert_eq!(app.selected_permission_index, 0);
    app.move_permission_selection(-1);
    assert_eq!(app.selected_permission_index, 2, "up from the first option wraps to Deny");
    app.move_permission_selection(1);
    assert_eq!(app.selected_permission_index, 0);
    app.move_permission_selection(1);
    app.move_permission_selection(1);
    assert_eq!(app.selected_permission_index, 2);

    app.selected_permission_index = 2;
    app.set_approval_request(ApprovalRequest {
        id: "appr_10".to_string(),
        risk_level: "R3".to_string(),
        tool_name: "terminal".to_string(),
        parameters: "ls".to_string(),
        reason: String::new(),
        blast_radius: String::new(),
        cwd: String::new(),
    });
    assert_eq!(app.selected_permission_index, 0, "a fresh request must not inherit the old choice");
}

#[test]
fn the_pending_tool_row_says_why_it_stalls() {
    let mut app = approval_app();
    app.is_busy = true;
    app.add_tool_call("terminal_1", "terminal", "rm -rf ./dist", None, Some("RUNNING"));

    // Render the session screen alone: the modal would cover the transcript.
    let area = Rect::new(0, 0, 120, 36);
    let mut buf = Buffer::empty(area);
    hex_tui::ui::screens::session::render(&app, &theme(), &AnimationState::new(), area, &mut buf);
    let screen = rows(&buf);
    let tool_y = row_of(&screen, "rm -rf ./dist");
    assert!(screen[tool_y].contains("waiting for permission"), "{}", screen[tool_y]);
    assert_eq!(
        cell(&buf, col(&screen[tool_y], "terminal"), tool_y).fg,
        theme().accent_blue,
        "the blocked verb is highlighted"
    );
    assert!(screen.iter().any(|r| r.contains("Waiting for permission")));
}

#[test]
fn rail_and_footer_name_the_blocker() {
    let mut app = approval_app();
    app.is_busy = true;
    let screen = rows(&render(&app, 120, 36));
    let joined = screen.join("\n");
    assert!(joined.contains("blocked / waiting on you"), "rail must name the blocker");
    assert!(joined.contains("Waiting for permission"), "composer must wait, not prompt");

    let footer = screen.iter().find(|r| r.contains("enter confirm")).expect("approval footer");
    assert!(footer.contains("up/down select"));
    assert!(footer.contains("esc deny"));
    assert!(!footer.contains("tab mode"), "session hints must not advertise keys the modal eats");
}

#[test]
fn the_modal_still_renders_on_a_narrow_terminal() {
    let app = approval_app();
    let joined = rows(&render(&app, 60, 20)).join("\n");
    assert!(joined.contains("Permission required"));
    assert!(joined.contains("Allow once"));
    assert!(joined.contains("Deny"));
}
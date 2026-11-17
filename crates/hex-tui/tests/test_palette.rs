//! G03: the command & navigation palette — geometry, fuzzy highlighting,
//! selection state, and the footer contract from the HEX Agent mockup.

use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::style::Modifier;
use ratatui::Terminal;

use hex_tui::app::{App, PaletteItem, ScreenState};
use hex_tui::ui;
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

fn row_of(rows: &[String], needle: &str) -> usize {
    rows.iter()
        .position(|r| r.contains(needle))
        .unwrap_or_else(|| panic!("no row contains {needle:?}\n{}", rows.join("\n")))
}

fn col_of(row: &str, needle: &str) -> usize {
    let hay: Vec<char> = row.chars().collect();
    let pat: Vec<char> = needle.chars().collect();
    hay.windows(pat.len())
        .position(|w| w == pat.as_slice())
        .unwrap_or_else(|| panic!("row does not contain {needle:?}: {row}"))
}

fn palette_app(query: &str) -> App {
    let mut app = App::new("harness", "gemini-2.5-pro", "OverlayFS");
    app.screen_state = ScreenState::Session;
    app.open_palette();
    for c in query.chars() {
        app.palette_push_char(c);
    }
    app
}

/// Only the palette interior: the rail behind it prints real workspace paths,
/// which can contain a command name by coincidence.
fn panel_rows(screen: &[String]) -> Vec<String> {
    let top = screen
        .iter()
        .enumerate()
        .skip(1)
        .find(|(_, r)| r.contains('╭'))
        .map(|(i, _)| i)
        .expect("palette panel");
    let left = col_of(&screen[top], "╭") + 1;
    let right = col_of(&screen[top], "╮");
    screen
        .iter()
        .map(|r| r.chars().skip(left).take(right - left).collect())
        .collect()
}

#[test]
fn panel_is_top_anchored_wider_and_responsive() {
    for (width, expected) in [(45, 41), (60, 56), (120, 80), (160, 96), (241, 120), (400, 120)] {
        for state in [ScreenState::Start, ScreenState::Session] {
            let mut app = palette_app("");
            app.screen_state = state;
            let rows = rows(&render(&app, width, 68));
            let top = rows.iter().enumerate().skip(1)
                .find(|(_, r)| r.contains('╭') && r.contains('╮'))
                .map(|(y, _)| y).expect("rounded panel border");
            assert_eq!(top, 4, "panel hangs below the header");
            let left = col_of(&rows[top], "╭");
            let right = col_of(&rows[top], "╮");
            assert_eq!(right - left + 1, expected, "terminal width {width}");
            assert_eq!(left, (width as usize - expected) / 2, "horizontally centred");
        }
    }
}

#[test]
fn query_row_owns_the_keyboard_and_shows_the_prompt() {
    let app = palette_app("sw");
    let buf = render(&app, 120, 36);
    let rows = rows(&buf);
    let search = row_of(&rows, "> sw");
    assert_eq!(search, 5, "search sits on the first inner row");
    assert!(app.input_buffer.is_empty());
}

#[test]
fn only_the_matched_characters_are_bold() {
    let app = palette_app("sw");
    let buf = render(&app, 120, 36);
    let rows = rows(&buf);

    let y = row_of(&rows, "Switch session");
    let x = col_of(&rows[y], "Switch session");
    let bold = |dx: usize| cell(&buf, x + dx, y).modifier.contains(Modifier::BOLD);

    // 's' and 'w' matched; the rest of "Switch" did not.
    assert!(bold(0), "matched 'S' must be bold");
    assert!(bold(1), "matched 'w' must be bold");
    assert!(!bold(2), "unmatched 'i' must stay regular");
    assert!(!bold(3), "unmatched 't' must stay regular");
    assert!(!bold(4), "unmatched 'c' must stay regular");
}

#[test]
fn command_name_is_highlighted_at_its_rendered_positions() {
    let app = palette_app("plan");
    let buf = render(&app, 120, 36);
    let rows = rows(&buf);

    let y = row_of(&rows, "/plan");
    let x = col_of(&rows[y], "/plan");
    assert!(
        !cell(&buf, x, y).modifier.contains(Modifier::BOLD),
        "the leading slash is not part of the match"
    );
    for dx in 1..5 {
        assert!(
            cell(&buf, x + dx, y).modifier.contains(Modifier::BOLD),
            "matched hint char at +{dx} must be bold"
        );
    }
}

#[test]
fn selected_row_is_tinted_and_the_others_are_not() {
    let app = palette_app("mem");
    let buf = render(&app, 120, 36);
    let rows = rows(&buf);
    let t = theme();

    let selected = row_of(&rows, "/memory");
    let other = row_of(&rows, "/remember");
    assert_ne!(selected, other);

    let left = col_of(&rows[4], "╭") + 1;
    let right = col_of(&rows[4], "╮");
    for x in left..right {
        assert_eq!(cell(&buf, x, selected).bg, t.selection_bg, "selected row at col {x}");
        assert_eq!(cell(&buf, x, other).bg, t.card_bg, "unselected row at col {x}");
    }
}

#[test]
fn browsing_without_a_query_groups_entries_under_categories() {
    let app = palette_app("");
    let buf = render(&app, 120, 50);
    let rows = rows(&buf);
    // Only the panel interior counts: the right rail shares these rows.
    let left = col_of(&rows[4], "╭") + 1;
    let right = col_of(&rows[4], "╮");
    let panel: Vec<String> = rows
        .iter()
        .map(|r| r.chars().skip(left).take(right - left).collect::<String>().trim().to_string())
        .collect();
    for category in ["modes", "agents", "session", "workspace", "knowledge"] {
        assert!(
            panel.iter().any(|r| r == category),
            "missing category header {category:?}\n{}",
            panel.join("\n")
        );
    }
}

#[test]
fn footer_reports_the_live_result_count() {
    let app = palette_app("mem");
    let expected = app.palette_results().len();
    let rows = rows(&render(&app, 120, 36));
    let footer = row_of(&rows, "enter select · esc close");
    assert!(
        rows[footer].contains(&format!("{expected} result")),
        "footer must count the filtered results: {}",
        rows[footer]
    );
}

#[test]
fn a_query_with_no_match_says_so() {
    let app = palette_app("zzqqxx");
    assert!(app.palette_results().is_empty());
    let rows = rows(&render(&app, 120, 36));
    assert!(rows.iter().any(|r| r.contains("no matching command")));
    assert!(rows.iter().any(|r| r.contains("0 results")));
}

#[test]
fn long_labels_truncate_without_crossing_the_panel_border() {
    for label in ["command".repeat(30), "世界".repeat(30)] {
        let prefix = format!("> {}", label.chars().next().unwrap());
        let mut app = palette_app("");
        app.palette_items = vec![PaletteItem {
            category: "session".to_string(),
            title: label,
            command: "session:switch".to_string(),
        }];
        let buf = render(&app, 60, 36);
        let rows = rows(&buf);
        let y = row_of(&rows, &prefix);
        assert!(rows[y].contains('…'));
        let right = col_of(&rows[4], "╮");
        assert_eq!(cell(&buf, right, y).symbol(), "│", "label must not overwrite the border");
        assert!(right < 60);
    }
}

#[test]
fn browsing_lists_command_names_once_without_help_prose() {
    let app = palette_app("");
    let panel = panel_rows(&rows(&render(&app, 241, 68)));
    for item in &app.palette_items {
        let label = item.title.as_str();
        let y = row_of(&panel, label);
        let row = panel[y].trim().trim_start_matches("> ");
        assert_eq!(row, label, "rows must contain only the action label");
        assert_eq!(panel.iter().filter(|row| row.trim().trim_start_matches("> ") == label).count(), 1);
    }
}

#[test]
fn fuzzy_search_uses_command_names_not_help_descriptions() {
    for query in ["autonomous", "SQLite", "governed", "read-only"] {
        let app = palette_app(query);
        assert!(app.palette_results().is_empty(), "description matched {query:?}");
    }
    for query in ["pln", "/pln", "PLN"] {
        let app = palette_app(query);
        assert_eq!(app.palette_selected().unwrap().command, "/plan");
    }
}

#[test]
fn the_composer_underneath_keeps_its_draft() {
    let mut app = palette_app("sw");
    app.input_buffer = "draft goal".to_string();
    app.cursor_pos = app.input_buffer.len();
    let rows = rows(&render(&app, 120, 36));
    assert!(
        rows.iter().any(|r| r.contains("draft goal")),
        "the palette must not erase the composer draft\n{}",
        rows.join("\n")
    );
    assert_eq!(app.input_buffer, "draft goal");
}

#[test]
fn sw_matches_session_navigation_without_incidental_description_matches() {
    let app = palette_app("sw");
    let results = app.palette_results();
    assert_eq!(results.len(), 1);
    assert_eq!(app.palette_selected().unwrap().command, "session:switch");
    assert_eq!(results[0].title_matched, vec![0, 1]);
}

#[test]
fn selection_scrolls_into_view_when_the_list_outgrows_the_panel() {
    let mut app = palette_app("");
    let results = app.palette_results();
    let first = app.palette_items[results[0].item_index].title.clone();
    let last = app.palette_items[results[results.len() - 1].item_index].title.clone();

    app.selected_palette_index = results.len() - 1;
    let scrolled = panel_rows(&rows(&render(&app, 120, 24)));
    assert!(
        scrolled.iter().any(|r| r.contains(&last)),
        "the selected entry must be scrolled into view\n{}",
        scrolled.join("\n")
    );
    assert!(
        !scrolled.iter().any(|r| r.contains(&first)),
        "entries above the fold must scroll away\n{}",
        scrolled.join("\n")
    );

    app.selected_palette_index = 0;
    let back_at_top = panel_rows(&rows(&render(&app, 120, 24)));
    assert!(back_at_top.iter().any(|r| r.contains(&first)), "wrapping back scrolls to the top");
}

#[test]
fn palette_also_opens_on_the_start_screen() {
    let mut app = App::new("harness", "gemini-2.5-pro", "OverlayFS");
    assert_eq!(app.screen_state, ScreenState::Start);
    app.open_palette();
    app.palette_push_char('s');
    let rows = rows(&render(&app, 120, 36));
    assert!(rows.iter().any(|r| r.contains("> s")));
    assert!(rows.iter().any(|r| r.contains("enter select · esc close")));
}
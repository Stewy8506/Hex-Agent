//! G07: motion polish — the staggered line entrance, the one-row top fade mask,
//! tool-row hover, and the `HEX_REDUCED_MOTION` opt-out.

use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier};
use ratatui::text::{Line, Span};
use ratatui::Terminal;

use hex_protocol::models::Role;
use hex_tui::app::{App, DialogueItem, MessageItem, ScreenState, ToolCallItem};
use hex_tui::ui::animation::{
    apply_entrance_fade, apply_top_fade, interpolate_color, rise_factor, AnimationState,
    BRAILLE_FRAMES, RISE_MS, RISE_STAGGER_MS, TOOL_HOVER_BG,
};
use hex_tui::ui::style::HexTheme;

/// `ui::draw` reads COLORTERM and HEX_REDUCED_MOTION while building its animation
/// state, and `std::env` is process-global, so these tests take turns.
static ENV_LOCK: Mutex<()> = Mutex::new(());

struct Env {
    _guard: MutexGuard<'static, ()>,
    colorterm: Option<String>,
}

impl Env {
    /// Exact RGB assertions need the truecolor palette, not the 256-color ramp.
    fn truecolor() -> Self {
        let guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let colorterm = std::env::var("COLORTERM").ok();
        std::env::set_var("COLORTERM", "truecolor");
        std::env::remove_var("HEX_REDUCED_MOTION");
        Self { _guard: guard, colorterm }
    }

    fn reduced(&self) {
        std::env::set_var("HEX_REDUCED_MOTION", "1");
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        std::env::remove_var("HEX_REDUCED_MOTION");
        match self.colorterm.take() {
            Some(v) => std::env::set_var("COLORTERM", v),
            None => std::env::remove_var("COLORTERM"),
        }
    }
}

/// An 80x30 cockpit drops the right rail, leaving the transcript stream at
/// x 4..76, y 4..23 (window border 1 + canvas pad 3, header 2 + pad top 1).
const SCREEN_W: u16 = 80;
const SCREEN_H: u16 = 30;
const STREAM_X: u16 = 4;
const STREAM_RIGHT: u16 = 76;
const STREAM_Y: u16 = 4;

fn session_app() -> App {
    let mut app = App::new("harness-core", "sonnet", "sandbox");
    app.screen_state = ScreenState::Session;
    app.dialogue_items.clear();
    app
}

fn user(text: &str) -> DialogueItem {
    DialogueItem::Message(MessageItem {
        role: Role::User,
        content: text.to_string(),
        timestamp: String::new(),
    })
}

fn tool(id: &str, command: &str) -> DialogueItem {
    DialogueItem::ToolCall(ToolCallItem {
        id: id.to_string(),
        tool: "terminal".to_string(),
        args: format!(r#"{{"command":"{command}"}}"#),
        result: Some("ok".to_string()),
        duration_ms: Some(120),
        exit_status: Some("exit 0".to_string()),
        collapsed: false,
        started_at: None,
    })
}

fn draw(app: &App) -> Buffer {
    let backend = TestBackend::new(SCREEN_W, SCREEN_H);
    let mut terminal = Terminal::new(backend).unwrap();
    terminal.draw(|f| hex_tui::ui::draw(f, app)).unwrap();
    terminal.backend().buffer().clone()
}

fn row_text(buf: &Buffer, y: u16) -> String {
    (0..buf.area.width).map(|x| buf[(x, y)].symbol()).collect()
}

fn pos_of(buf: &Buffer, needle: &str) -> (u16, u16) {
    for y in 0..buf.area.height {
        let row = row_text(buf, y);
        if let Some(byte_idx) = row.find(needle) {
            return (row[..byte_idx].chars().count() as u16, y);
        }
    }
    panic!("no row containing {needle:?} in:\n{}", (0..buf.area.height).map(|y| row_text(buf, y)).collect::<Vec<_>>().join("\n"));
}

fn fg_of(buf: &Buffer, needle: &str) -> Color {
    let (x, y) = pos_of(buf, needle);
    buf[(x, y)].fg
}

fn red(c: Color) -> u8 {
    match c {
        Color::Rgb(r, _, _) => r,
        other => panic!("expected an RGB color, got {other:?}"),
    }
}

/// Let the entrance finish so steady-state colors can be asserted.
fn settle(app: &App) {
    app.dialogue_entrance.set((app.dialogue_items.len(), None));
}

/// Pin the entrance to its first frame: a start instant ahead of the clock reads
/// as zero elapsed, so the new blocks are exactly at the bottom of their rise
/// however long the render takes.
fn mark_arrival(app: &App, already_seen: usize) {
    app.dialogue_entrance.set((already_seen, Some(Instant::now() + Duration::from_secs(1))));
}

#[test]
fn reduced_motion_stills_the_spinner_cursor_pulse_and_shimmer() {
    let env = Env::truecolor();
    env.reduced();
    let anim = AnimationState::with_start(Instant::now() - Duration::from_millis(700));
    assert!(anim.is_reduced());
    assert_eq!(anim.spinner_frame(), BRAILLE_FRAMES[0]);
    assert!(anim.cursor_visible(), "a still cursor must stay visible");
    assert_eq!(anim.pulse_factor(), 1.0);

    let base = Color::Rgb(140, 140, 140);
    let spans = anim.render_shimmer("Verifying public API surface", base, Color::Rgb(242, 242, 242));
    assert_eq!(spans.len(), "Verifying public API surface".chars().count());
    for span in &spans {
        assert_eq!(span.style.fg, Some(base), "reduced shimmer is flat, no sweep");
        assert!(!span.style.add_modifier.contains(Modifier::BOLD));
    }
}

#[test]
fn the_flag_accepts_the_usual_truthy_spellings() {
    let env = Env::truecolor();
    for value in ["1", "true", "YES", " on "] {
        std::env::set_var("HEX_REDUCED_MOTION", value);
        assert!(AnimationState::new().is_reduced(), "{value:?} should still the motion");
    }
    for value in ["0", "false", "off", ""] {
        std::env::set_var("HEX_REDUCED_MOTION", value);
        assert!(!AnimationState::new().is_reduced(), "{value:?} should leave motion on");
    }
    std::env::remove_var("HEX_REDUCED_MOTION");
    assert!(!AnimationState::new().is_reduced());
    drop(env);
}

#[test]
fn motion_runs_when_the_flag_is_absent() {
    let _env = Env::truecolor();
    let anim = AnimationState::with_start(Instant::now() - Duration::from_millis(700));
    assert!(!anim.is_reduced());
    assert_eq!(anim.spinner_frame(), BRAILLE_FRAMES[7]);
    assert!(!anim.cursor_visible(), "700ms into the 1.1s cycle the cursor is off");
    assert!((0.35..=1.0).contains(&anim.pulse_factor()));

    let base = Color::Rgb(140, 140, 140);
    let spans = anim.render_shimmer("Verifying public API surface", base, Color::Rgb(242, 242, 242));
    assert!(spans.iter().any(|s| s.style.fg != Some(base)), "the sweep must highlight some cells");
    assert!(spans.iter().any(|s| s.style.add_modifier.contains(Modifier::BOLD)));
}

#[test]
fn rise_factor_eases_out_and_honours_the_stagger_delay() {
    let zero = Duration::ZERO;
    assert_eq!(rise_factor(zero, zero), 0.0);
    assert_eq!(rise_factor(Duration::from_millis(RISE_MS), zero), 1.0);
    assert_eq!(rise_factor(Duration::from_millis(RISE_MS * 4), zero), 1.0);
    // ease-out cubic at the halfway point: 1 - (1 - 0.5)^3
    assert!((rise_factor(Duration::from_millis(RISE_MS / 2), zero) - 0.875).abs() < 1e-6);

    let a = rise_factor(Duration::from_millis(100), zero);
    let b = rise_factor(Duration::from_millis(300), zero);
    let c = rise_factor(Duration::from_millis(500), zero);
    assert!(a < b && b < c, "the rise must be monotonic: {a} {b} {c}");

    let delay = Duration::from_millis(RISE_STAGGER_MS);
    assert_eq!(rise_factor(zero, delay), 0.0);
    assert_eq!(rise_factor(delay, delay), 0.0, "a delayed line has not started yet");
    assert!(rise_factor(delay + Duration::from_millis(300), delay) > 0.8);
}

#[test]
fn entrance_fade_only_touches_spans_with_a_foreground() {
    let bg = Color::Rgb(14, 14, 14);
    let fg = Color::Rgb(242, 242, 242);
    let mut line = Line::from(vec![Span::styled("text", ratatui::style::Style::default().fg(fg)), Span::raw("plain")]);

    apply_entrance_fade(&mut line, 1.0, bg);
    assert_eq!(line.spans[0].style.fg, Some(fg), "a settled line is untouched");

    apply_entrance_fade(&mut line, 0.0, bg);
    assert_eq!(line.spans[0].style.fg, Some(bg));
    assert_eq!(line.spans[1].style.fg, None, "spans without a foreground keep inheriting theirs");

    let mut mid = Line::from(vec![Span::styled("text", ratatui::style::Style::default().fg(fg))]);
    apply_entrance_fade(&mut mid, 0.5, bg);
    assert_eq!(mid.spans[0].style.fg, Some(interpolate_color(bg, fg, 0.5)));
}

#[test]
fn only_new_blocks_rise_and_then_settle() {
    let _env = Env::truecolor();
    let theme = HexTheme::detect();
    let mut app = session_app();
    app.dialogue_items.push(user("first goal"));
    app.dialogue_items.push(user("second goal"));
    app.dialogue_items.push(user("third goal"));

    mark_arrival(&app, 1);
    let buf = draw(&app);
    assert_eq!(fg_of(&buf, "first goal"), theme.text_bright, "an already-shown block does not rise again");
    assert_eq!(fg_of(&buf, "second goal"), theme.window_bg);
    assert_eq!(fg_of(&buf, "third goal"), theme.window_bg);

    app.dialogue_entrance.set((3, None));
    let settled = draw(&app);
    assert_eq!(fg_of(&settled, "second goal"), theme.text_bright);
    assert_eq!(fg_of(&settled, "third goal"), theme.text_bright);
}

#[test]
fn the_stagger_settles_blocks_in_reading_order() {
    let _env = Env::truecolor();
    let theme = HexTheme::detect();
    let mut app = session_app();
    app.dialogue_items.push(user("first goal"));
    app.dialogue_items.push(user("second goal"));
    app.dialogue_items.push(user("third goal"));

    // The arrival frame starts the clock; date it 150ms back so the second block
    // is 150ms through its rise while the third, delayed 120ms, is only 30ms in.
    let _ = draw(&app);
    app.dialogue_entrance.set((1, Some(Instant::now() - Duration::from_millis(150))));
    let buf = draw(&app);
    let second = red(fg_of(&buf, "second goal"));
    let third = red(fg_of(&buf, "third goal"));
    assert!(third < second, "the later block must still be dimmer: {third} vs {second}");
    assert!(second < red(theme.text_bright));
}

#[test]
fn the_rise_advances_across_frames_and_lands_for_good() {
    let _env = Env::truecolor();
    let theme = HexTheme::detect();
    let mut app = session_app();
    app.dialogue_items.push(user("first goal"));
    app.dialogue_items.push(user("second goal"));

    mark_arrival(&app, 1);
    assert_eq!(fg_of(&draw(&app), "second goal"), theme.window_bg);

    // A later frame is part-way up, not snapped back to the first frame.
    app.dialogue_entrance.set((1, Some(Instant::now() - Duration::from_millis(300))));
    let mid = draw(&app);
    let mid_red = red(fg_of(&mid, "second goal"));
    assert!(mid_red > red(theme.window_bg) && mid_red < red(theme.text_bright), "mid-rise red was {mid_red}");
    assert_eq!(fg_of(&mid, "first goal"), theme.text_bright);

    // Once the span elapses the block lands, and stays landed on later frames.
    app.dialogue_entrance.set((1, Some(Instant::now() - Duration::from_secs(2))));
    assert_eq!(fg_of(&draw(&app), "second goal"), theme.text_bright);
    for _ in 0..3 {
        assert_eq!(fg_of(&draw(&app), "second goal"), theme.text_bright, "a landed block must not rise again");
    }

    // The next arrival gets its own rise without disturbing the landed one.
    app.dialogue_items.push(user("third goal"));
    let next = draw(&app);
    assert_eq!(fg_of(&next, "second goal"), theme.text_bright);
    assert!(red(fg_of(&next, "third goal")) < red(theme.text_bright) / 2, "the new block starts at the bottom of its rise");
}

#[test]
fn a_long_transcript_rises_only_in_its_newest_blocks() {
    let _env = Env::truecolor();
    let theme = HexTheme::detect();
    let mut app = session_app();
    for i in 0..30 {
        app.dialogue_items.push(user(&format!("goal {i}")));
    }
    app.follow_dialogue = true;
    mark_arrival(&app, 0);
    let buf = draw(&app);
    assert_eq!(fg_of(&buf, "goal 21"), theme.text_bright, "a resumed transcript settles at once");
    assert_eq!(fg_of(&buf, "goal 27"), theme.window_bg, "the newest blocks still rise");
}

#[test]
fn reduced_motion_skips_the_entrance() {
    let env = Env::truecolor();
    env.reduced();
    let theme = HexTheme::detect();
    let mut app = session_app();
    app.dialogue_items.push(user("first goal"));
    app.dialogue_items.push(user("second goal"));
    mark_arrival(&app, 0);
    let buf = draw(&app);
    assert_eq!(fg_of(&buf, "first goal"), theme.text_bright);
    assert_eq!(fg_of(&buf, "second goal"), theme.text_bright);
}

#[test]
fn hovering_a_tool_row_paints_the_mockup_background() {
    let _env = Env::truecolor();
    let mut app = session_app();
    app.dialogue_items.push(user("list the workspace"));
    app.dialogue_items.push(tool("t1", "ls -la"));
    app.dialogue_items.push(user("thanks"));
    settle(&app);

    let plain = draw(&app);
    let (tx, ty) = pos_of(&plain, "terminal");
    assert_ne!(plain[(tx, ty)].bg, TOOL_HOVER_BG);

    app.mouse_pos = Some((tx, ty));
    let hovered = draw(&app);
    for x in STREAM_X..STREAM_RIGHT {
        assert_eq!(hovered[(x, ty)].bg, TOOL_HOVER_BG, "the whole stream row takes #141414 at x={x}");
    }
    assert_ne!(hovered[(0, ty)].bg, TOOL_HOVER_BG, "the window border is not part of the row");
    assert_ne!(hovered[(SCREEN_W - 1, ty)].bg, TOOL_HOVER_BG);

    let (_, uy) = pos_of(&hovered, "list the workspace");
    assert_ne!(hovered[(STREAM_X, uy)].bg, TOOL_HOVER_BG, "only the row under the pointer is lit");
}

#[test]
fn hovering_a_message_row_or_the_composer_paints_nothing() {
    let _env = Env::truecolor();
    let mut app = session_app();
    app.dialogue_items.push(user("list the workspace"));
    app.dialogue_items.push(tool("t1", "ls -la"));
    settle(&app);

    let (mx, my) = pos_of(&draw(&app), "list the workspace");
    app.mouse_pos = Some((mx, my));
    let buf = draw(&app);
    for x in STREAM_X..STREAM_RIGHT {
        assert_ne!(buf[(x, my)].bg, TOOL_HOVER_BG, "a message row is not a tool row");
    }

    // The composer sits below the stream; a pointer there lights no transcript row.
    app.mouse_pos = Some((mx, SCREEN_H - 3));
    let buf = draw(&app);
    for y in 0..SCREEN_H {
        for x in 0..SCREEN_W {
            assert_ne!(buf[(x, y)].bg, TOOL_HOVER_BG, "hover outside the stream must not paint at {x},{y}");
        }
    }
}

#[test]
fn the_top_fade_masks_one_row_only_when_scrolled() {
    let _env = Env::truecolor();
    let theme = HexTheme::detect();
    let mut app = session_app();
    for _ in 0..25 {
        app.dialogue_items.push(user("alpha beta gamma"));
    }
    settle(&app);
    app.follow_dialogue = false;
    // Line 24 is the tenth message row, so the first visible row carries text.
    app.dialogue_scroll = 24;

    let scrolled = draw(&app);
    let (x, y) = pos_of(&scrolled, "alpha beta gamma");
    assert_eq!(y, STREAM_Y);
    assert_eq!(scrolled[(x, y)].fg, interpolate_color(theme.window_bg, theme.text_bright, 0.5));
    assert_eq!(scrolled[(x, y + 2)].fg, theme.text_bright, "the mask is one row deep");

    app.dialogue_scroll = 0;
    let top = draw(&app);
    let (wx, wy) = pos_of(&top, "█ █ █▀▀ ▀▄▀");
    assert_eq!(top[(wx, wy)].fg, theme.text_bright, "an unscrolled transcript is not masked");
}

#[test]
fn top_fade_ramps_toward_the_background_and_clamps_to_the_area() {
    let _env = Env::truecolor();
    let area = Rect::new(0, 0, 10, 4);
    let mut buf = Buffer::empty(area);
    for y in 0..4 {
        for x in 0..10 {
            buf[(x, y)].set_symbol("x");
            buf[(x, y)].set_fg(Color::Rgb(200, 200, 200));
        }
    }
    let bg = Color::Rgb(10, 10, 10);

    apply_top_fade(area, 0, bg, &mut buf);
    assert_eq!(buf[(0, 0)].fg, Color::Rgb(200, 200, 200), "no fade rows means no mask");

    apply_top_fade(area, 2, bg, &mut buf);
    assert_eq!(buf[(0, 0)].fg, Color::Rgb(58, 58, 58));
    assert_eq!(buf[(0, 1)].fg, Color::Rgb(153, 153, 153));
    assert_eq!(buf[(0, 2)].fg, Color::Rgb(200, 200, 200));
    assert_eq!(buf[(9, 1)].fg, Color::Rgb(153, 153, 153), "the mask spans the full width");

    apply_top_fade(area, 40, bg, &mut buf);
    assert_eq!(buf[(0, 3)].fg, Color::Rgb(27, 27, 27), "a mask deeper than the area is clamped");
}
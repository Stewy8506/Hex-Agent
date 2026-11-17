//! Inline tool-failure cards and the agents drill-down fed by real records.

use ratatui::backend::TestBackend;
use ratatui::Terminal;

use hex_protocol::models::{Role, SubagentInfo, SubagentResultInfo};
use hex_tui::app::{App, DialogueItem, MessageItem, ScreenState, ToolCallItem};
use hex_tui::ui::keymap;
use hex_tui::ui::screens::error_view::{diagnostic_lines, DiagnosticFailure};
use hex_tui::ui::style::HexTheme;

/// A sealed `terminal` failure as the Python gateway reports it.
const PYTEST_FAILURE: &str = r#"{"ok":false,"exit_code":1,"stdout":"running 1 test\ntests/test_ordering.py::test_sorts_paths FAILED\nE   assert ['b.ts'] == ['a.ts']\nFile \"tests/test_ordering.py\", line 18, in test_sorts_paths\n","stderr":"","error":"assertion failed: ordering differs","timed_out":false}"#;

fn session_app() -> App {
    let mut app = App::new("harness-core", "sonnet", "sandbox");
    app.screen_state = ScreenState::Session;
    app
}

fn tool_row(id: &str, tool: &str, args: &str) -> DialogueItem {
    DialogueItem::ToolCall(ToolCallItem {
        id: id.to_string(),
        tool: tool.to_string(),
        args: args.to_string(),
        result: None,
        duration_ms: None,
        exit_status: None,
        collapsed: false,
        started_at: None,
    })
}

fn message(role: Role, content: &str) -> DialogueItem {
    DialogueItem::Message(MessageItem {
        role,
        content: content.to_string(),
        timestamp: String::new(),
    })
}

fn render_rows(app: &App, width: u16, height: u16) -> Vec<String> {
    let backend = TestBackend::new(width, height);
    let mut terminal = Terminal::new(backend).unwrap();
    terminal.draw(|f| hex_tui::ui::draw(f, app)).unwrap();
    let buffer = terminal.backend().buffer();
    (0..buffer.area.height)
        .map(|y| {
            let mut line = String::new();
            for x in 0..buffer.area.width {
                line.push_str(buffer[(x, y)].symbol());
            }
            line.trim_end().to_string()
        })
        .collect()
}

fn row_of(rows: &[String], needle: &str) -> usize {
    rows.iter()
        .position(|r| r.contains(needle))
        .unwrap_or_else(|| panic!("no row containing {needle:?} in:\n{}", rows.join("\n")))
}

fn epoch_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

fn researcher() -> SubagentInfo {
    SubagentInfo {
        subagent_id: "sub_1".to_string(),
        parent_task_id: "tsk_1".to_string(),
        role: "researcher".to_string(),
        goal: "Map the approval flow across the Python gateway and the Rust clients.".to_string(),
        depth: 1,
        status: "running".to_string(),
        started_at: epoch_now() - 42.0,
        ended_at: None,
        interrupted: false,
        tool_calls_count: 7,
        result: None,
    }
}

fn verifier() -> SubagentInfo {
    SubagentInfo {
        subagent_id: "sub_2".to_string(),
        parent_task_id: "tsk_1".to_string(),
        role: "test-verifier".to_string(),
        goal: "Verify the ordering assertions.".to_string(),
        depth: 1,
        status: "completed".to_string(),
        started_at: epoch_now() - 84.0,
        ended_at: Some(epoch_now() - 42.0),
        interrupted: false,
        tool_calls_count: 3,
        result: Some(SubagentResultInfo {
            status: "completed".to_string(),
            summary: "All ordering assertions hold.".to_string(),
            findings: vec!["paths sort case-sensitively".to_string()],
            diff_summary: "1 file changed".to_string(),
            artifacts: vec!["report.md".to_string()],
            duration_seconds: 42.0,
            error: None,
        }),
    }
}

#[test]
fn test_error_payload_parses_into_a_diagnostic() {
    let diag = DiagnosticFailure::from_tool_error(
        "terminal",
        r#"{"command":"pytest tests/test_ordering.py -q"}"#,
        PYTEST_FAILURE,
        2500,
    );

    assert_eq!(diag.command, "pytest tests/test_ordering.py -q");
    assert_eq!(diag.exit_code, 1);
    assert_eq!(diag.duration_str, "2.5s");
    assert_eq!(diag.test_file, "tests/test_ordering.py");
    assert_eq!(diag.test_name, "test_sorts_paths");
    // pytest prints `assert actual == expected`.
    assert_eq!(diag.expected, "['a.ts']");
    assert_eq!(diag.received, "['b.ts']");
    assert_eq!(diag.stack_trace.len(), 1);
    assert_eq!(diag.hidden_frames_count, 0);
    assert_eq!(diag.diagnosis, "assertion failed: ordering differs");
}

#[test]
fn test_exception_payload_without_exit_code_still_reports() {
    let diag = DiagnosticFailure::from_tool_error(
        "terminal",
        r#"{"command":"cargo check"}"#,
        r#"{"ok":false,"error":"PolicyBlockedError: writes outside the workspace"}"#,
        120,
    );

    assert_eq!(diag.exit_code, 1);
    assert_eq!(diag.command, "cargo check");
    assert_eq!(diag.diagnosis, "PolicyBlockedError: writes outside the workspace");
    assert!(diag.expected.is_empty() && diag.received.is_empty());
    assert!(!diag.detail_lines.is_empty());
}

#[test]
fn test_truncated_payload_does_not_panic() {
    // The server caps error payloads, so the JSON can arrive cut mid-string.
    let cut = &PYTEST_FAILURE[..120];
    let diag = DiagnosticFailure::from_tool_error("terminal", "", cut, 0);
    assert_eq!(diag.command, "terminal");
    assert_eq!(diag.exit_code, 1);
    assert!(!diag.detail_lines.is_empty());
}

#[test]
fn test_failure_card_renders_inline_with_its_tool_row() {
    let mut app = session_app();
    app.dialogue_items.push(message(Role::User, "Run the ordering tests"));
    app.dialogue_items.push(tool_row(
        "call-1",
        "terminal",
        r#"{"command":"pytest tests/test_ordering.py -q"}"#,
    ));
    app.dialogue_items
        .push(message(Role::Assistant, "The ordering assertion is reversed."));

    app.record_tool_failure("call-1", PYTEST_FAILURE, 2500);

    let diag = app.active_diagnostic.as_ref().expect("diagnostic recorded");
    assert_eq!(diag.exit_code, 1);
    let failed_row = app
        .dialogue_items
        .iter()
        .find_map(|item| match item {
            DialogueItem::ToolCall(t) if t.id == "call-1" => Some(t),
            _ => None,
        })
        .expect("tool row present");
    assert_eq!(failed_row.exit_status.as_deref(), Some("exit 1"));

    let rows = render_rows(&app, 120, 36);
    let tool = row_of(&rows, "pytest tests/test_ordering.py -q");
    // The window itself is a rounded box, so look for the card below the call.
    let top = rows[tool + 1..]
        .iter()
        .position(|r| r.contains('╭'))
        .expect("card top border")
        + tool
        + 1;
    let bottom = rows[top..]
        .iter()
        .position(|r| r.contains('╰'))
        .expect("card bottom border")
        + top;
    let reply = row_of(&rows, "The ordering assertion is reversed.");

    // The card sits directly under the call that produced it, and the reply
    // that came afterwards is still on screen below it.
    assert_eq!(top, tool + 1, "rows:\n{}", rows.join("\n"));
    assert!(bottom > top);
    assert!(reply > bottom, "rows:\n{}", rows.join("\n"));
    assert!(rows[tool].contains("exit 1"));
    assert!(rows[top + 1].contains("tests/test_ordering.py"));
    assert!(rows.iter().any(|r| r.contains("['a.ts']")));
    assert!(rows.iter().any(|r| r.contains("['b.ts']")));
    assert!(rows.iter().any(|r| r.contains("assertion failed: ordering differs")));
}

#[test]
fn test_failure_card_survives_its_tool_row_scrolling_away() {
    let mut app = session_app();
    app.dialogue_items.push(tool_row("call-1", "terminal", r#"{"command":"pytest -q"}"#));
    app.record_tool_failure("call-1", PYTEST_FAILURE, 2500);
    // A later turn replaces the transcript; the failure still gets its card.
    app.dialogue_items.clear();
    app.dialogue_items.push(message(Role::User, "What failed?"));

    let rows = render_rows(&app, 120, 36);
    assert!(rows.iter().any(|r| r.contains("╭")), "rows:\n{}", rows.join("\n"));
    assert!(rows.iter().any(|r| r.contains("tests/test_ordering.py")));
}

#[test]
fn test_new_prompt_retires_the_failure_card() {
    let mut app = session_app();
    app.dialogue_items.push(tool_row("call-1", "terminal", r#"{"command":"pytest -q"}"#));
    app.record_tool_failure("call-1", PYTEST_FAILURE, 2500);
    assert!(app.active_diagnostic.is_some());

    app.input_buffer = "Try again".to_string();
    app.submit_input();
    assert!(app.active_diagnostic.is_none());
}

#[test]
fn test_card_lines_are_exactly_the_stream_width() {
    let theme = HexTheme::detect();
    let diag = DiagnosticFailure::from_tool_error(
        "terminal",
        r#"{"command":"pytest tests/test_ordering.py -q --very-long-flag-name"}"#,
        PYTEST_FAILURE,
        2500,
    );

    // Paragraph wrapping reflows any over-long line, which would tear the box.
    for width in [40usize, 62, 90, 120] {
        let lines = diagnostic_lines(&diag, &theme, width);
        assert!(!lines.is_empty(), "width {width} produced no card");
        for line in &lines {
            let chars: usize = line.spans.iter().map(|s| s.content.chars().count()).sum();
            assert_eq!(chars, width, "width {width}: {line:?}");
        }
    }
    assert!(diagnostic_lines(&diag, &theme, 10).is_empty());
}

#[test]
fn test_card_box_is_not_reflowed_by_wrapping() {
    use ratatui::widgets::{Paragraph, Wrap};

    let theme = HexTheme::detect();
    let diag = DiagnosticFailure::from_tool_error(
        "terminal",
        r#"{"command":"pytest tests/test_ordering.py -q"}"#,
        PYTEST_FAILURE,
        2500,
    );

    for width in [40u16, 60, 80, 120] {
        let lines = diagnostic_lines(&diag, &theme, width as usize);
        let backend = TestBackend::new(width, 40);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|f| {
                let area = f.area();
                f.render_widget(
                    Paragraph::new(lines.clone()).wrap(Wrap { trim: false }),
                    area,
                );
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        let rows: Vec<String> = (0..buffer.area.height)
            .map(|y| {
                (0..buffer.area.width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
            })
            .collect();

        let painted = rows.iter().filter(|r| !r.trim().is_empty()).count();
        assert_eq!(painted, lines.len(), "width {width} reflowed:\n{}", rows.join("\n"));
        assert_eq!(rows.iter().filter(|r| r.contains('╭')).count(), 1);
        assert_eq!(rows.iter().filter(|r| r.contains('╰')).count(), 1);
    }
}

#[test]
fn test_agents_modal_opens_on_the_running_subagent() {
    let mut app = session_app();
    let agents = vec![verifier(), researcher()];
    app.set_subagent_telemetry(&agents);
    app.open_subagent_modal(&agents);

    let modal = app.active_subagent_modal.as_ref().expect("modal open");
    assert_eq!(modal.name, "researcher");
    assert!(modal.is_running);
    assert_eq!(modal.status, "running");
    assert!(modal.prompt.starts_with("Map the approval flow"));
    assert!(modal.activity_lines.iter().any(|l| l.contains("7 calls")));
    assert!(!modal.is_expanded);

    // The rail carries the same records, with the completed run marked pass.
    assert_eq!(app.subagents.len(), 2);
    assert_eq!(app.subagents[0].status, "pass");
    assert!(app.subagents[1].is_running);

    let rows = render_rows(&app, 120, 36);
    let body = rows.join("\n");
    assert!(body.contains("researcher"), "{body}");
    assert!(body.contains("Map the approval flow"), "{body}");
    assert!(body.contains("up/down scroll · e expand prompt · esc close"), "{body}");
}

#[test]
fn test_agents_modal_empty_state_is_honest() {
    let mut app = session_app();
    app.set_subagent_telemetry(&[]);
    app.open_subagent_modal(&[]);

    let modal = app.active_subagent_modal.as_ref().expect("modal open");
    assert!(modal.name.is_empty());
    assert!(modal.activity_lines.is_empty());
    assert!(!modal.is_running);

    let rows = render_rows(&app, 120, 36);
    let body = rows.join("\n");
    assert!(body.contains("no subagents in this session"), "{body}");
    assert!(body.contains("delegated work appears here while a turn runs"), "{body}");
    assert!(!body.contains("spawned"), "{body}");
}

#[test]
fn test_agents_prompt_clamps_to_two_rows_until_expanded() {
    let mut app = session_app();
    let mut agent = researcher();
    agent.goal = "word ".repeat(120).trim().to_string();
    app.open_subagent_modal(&[agent]);

    let prompt_rows = |app: &App| -> usize {
        let rows = render_rows(app, 120, 36);
        let start = row_of(&rows, "prompt");
        let divider = rows[start + 1..]
            .iter()
            .position(|r| r.contains('─') && !r.contains("word"))
            .expect("prompt divider")
            + start
            + 1;
        divider - start - 1
    };

    assert_eq!(prompt_rows(&app), 2);
    let collapsed = render_rows(&app, 120, 36).join("\n");
    assert!(collapsed.contains('…'), "{collapsed}");

    app.toggle_subagent_prompt();
    assert!(app.active_subagent_modal.as_ref().unwrap().is_expanded);
    assert_eq!(prompt_rows(&app), 6);
    let expanded = render_rows(&app, 120, 36).join("\n");
    assert!(expanded.contains("e collapse"), "{expanded}");
}

#[test]
fn test_agents_activity_scrolls_and_shows_newer_marker() {
    let mut app = session_app();
    let mut agent = verifier();
    agent.result.as_mut().unwrap().findings = (0..30).map(|i| format!("finding number {i}")).collect();
    app.open_subagent_modal(&[agent]);

    let total = app.active_subagent_modal.as_ref().unwrap().activity_lines.len();
    assert!(total > 20);

    let tail = render_rows(&app, 120, 36).join("\n");
    assert!(tail.contains("finding number 29"), "{tail}");
    assert!(!tail.contains("finding number 0"), "{tail}");

    app.scroll_subagent_activity(5);
    assert_eq!(app.active_subagent_modal.as_ref().unwrap().activity_scroll, 5);
    let scrolled = render_rows(&app, 120, 36).join("\n");
    assert!(scrolled.contains("newer"), "{scrolled}");
    assert!(scrolled.contains("finding number 24"), "{scrolled}");

    // Scrolling past the oldest row clamps instead of blanking the stream.
    app.scroll_subagent_activity(1000);
    let clamped = app.active_subagent_modal.as_ref().unwrap().activity_scroll;
    assert!(clamped < total);
    let oldest = render_rows(&app, 120, 36).join("\n");
    assert!(oldest.contains("spawned"), "{oldest}");

    app.scroll_subagent_activity(-1000);
    assert_eq!(app.active_subagent_modal.as_ref().unwrap().activity_scroll, 0);
}

#[test]
fn test_agents_modal_reports_a_failed_run() {
    let mut app = session_app();
    let mut agent = verifier();
    agent.status = "failed".to_string();
    agent.result.as_mut().unwrap().error = Some("tool budget exhausted".to_string());
    app.set_subagent_telemetry(&[agent.clone()]);
    app.open_subagent_modal(&[agent]);

    assert!(app.subagents[0].has_failed);
    assert_eq!(app.subagents[0].status, "fail");

    let modal = app.active_subagent_modal.as_ref().unwrap();
    assert!(!modal.is_running);
    assert_eq!(modal.elapsed_seconds, 42);

    let body = render_rows(&app, 120, 36).join("\n");
    assert!(body.contains("tool budget exhausted"), "{body}");
    assert!(body.contains("42s"), "{body}");
}

#[test]
fn test_agents_footer_hints_come_from_the_keymap() {
    let hints = keymap::footer_hints(keymap::Scope::Agents);
    assert_eq!(hints, "up/down scroll · e expand prompt · esc close");
    assert!(keymap::footer_hints(keymap::Scope::Session).contains("tab mode"));
    assert!(!keymap::footer_hints(keymap::Scope::Session).contains("tab agents"));
}
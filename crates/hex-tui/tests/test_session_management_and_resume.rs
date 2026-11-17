//! Empirical validation for Session click-to-resume, top-3 toggle, cascading delete, and title truncation.

use hex_tui::app::{App, Focus, ResumeSession, ScreenState};
use hex_tui::ui::animation::AnimationState;
use hex_tui::ui::screens::start;
use hex_tui::ui::style::HexTheme;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use std::fs;
use std::process::Command;

#[test]
fn test_top_3_sessions_default_and_toggle_expand_collapse() {
    let mut app = App::new("test-project", "sonnet", "sandbox");
    app.resume_sessions = (1..=6)
        .map(|i| ResumeSession {
            session_id: format!("sess_{}", i),
            title: format!("Task description number {}", i),
            relative_time: format!("{}m", i * 5),
        })
        .collect();

    let area = Rect::new(0, 0, 120, 36);
    let theme = HexTheme::truecolor();
    let anim = AnimationState::new();

    // 1. Collapsed by default: shows top 3 and "+ show all (6)"
    assert!(!app.show_all_resume);
    let mut buf = Buffer::empty(area);
    start::render(&app, &theme, &anim, area, &mut buf);

    let mut rendered_text = String::new();
    for y in 0..area.height {
        for x in 0..area.width {
            rendered_text.push_str(buf[(x, y)].symbol());
        }
        rendered_text.push('\n');
    }

    assert!(rendered_text.contains("Task description number 1"));
    assert!(rendered_text.contains("Task description number 2"));
    assert!(rendered_text.contains("Task description number 3"));
    assert!(!rendered_text.contains("Task description number 4"), "Task 4 must be hidden when collapsed");
    assert!(!rendered_text.contains("Task description number 5"), "Task 5 must be hidden when collapsed");
    assert!(rendered_text.contains("+ show all (6)"), "Must render expand toggle");

    // 2. Click or toggle expand
    let resume_area = app.get_start_resume_area(area);
    let toggle_y = resume_area.y + 1 + 3; // 1 header + 3 items
    let handled = app.handle_start_mouse_click(resume_area.x + 5, toggle_y, resume_area);
    assert!(handled, "Clicking toggle row must be handled");
    assert!(app.show_all_resume, "app.show_all_resume must now be true");
    assert_eq!(app.focus, Focus::Resume);

    // 3. Expanded view: shows all 6 sessions and "− show less"
    let mut buf2 = Buffer::empty(area);
    start::render(&app, &theme, &anim, area, &mut buf2);
    let mut rendered_text2 = String::new();
    for y in 0..area.height {
        for x in 0..area.width {
            rendered_text2.push_str(buf2[(x, y)].symbol());
        }
        rendered_text2.push('\n');
    }

    for i in 1..=6 {
        assert!(rendered_text2.contains(&format!("Task description number {}", i)));
    }
    assert!(rendered_text2.contains("− show less"), "Must render collapse toggle");

    // 4. Click "− show less" collapses back
    let expanded_resume_area = app.get_start_resume_area(area);
    let collapse_y = expanded_resume_area.y + 1 + 6; // 1 header + 6 items
    let handled_collapse = app.handle_start_mouse_click(expanded_resume_area.x + 5, collapse_y, expanded_resume_area);
    assert!(handled_collapse, "Clicking collapse row must be handled");
    assert!(!app.show_all_resume, "app.show_all_resume must be false after collapse");
}

#[test]
fn test_session_click_to_resume_and_token_recalculation() {
    let mut app = App::new("test-project", "sonnet", "sandbox");
    app.resume_sessions = vec![
        ResumeSession {
            session_id: "tsk_demo_resume".to_string(),
            title: "Implement graph streaming pipeline".to_string(),
            relative_time: "10m".to_string(),
        },
    ];

    assert_eq!(app.screen_state, ScreenState::Start);
    assert_eq!(app.tokens_used, 0);

    let area = Rect::new(0, 0, 120, 36);
    let resume_area = app.get_start_resume_area(area);

    // Click on the first session row (excluding the delete button on the right)
    let click_x = resume_area.x + 10;
    let click_y = resume_area.y + 1; // First session row
    let handled = app.handle_start_mouse_click(click_x, click_y, resume_area);
    assert!(handled, "Clicking session row must be handled");

    // Screen transitions to Session
    assert_eq!(app.screen_state, ScreenState::Session);
    assert_eq!(app.focus, Focus::Input);
    assert_eq!(app.session_id, "tsk_demo_resume");
    assert!(!app.dialogue_items.is_empty(), "Dialogue history must be populated");
    assert!(app.status_message.contains("Resumed session tsk_demo_resume"));

    // Tokens used is dynamically recalculated from loaded dialogue content
    assert!(app.tokens_used > 0, "Tokens used must be greater than 0 after resume recalculation");
}

#[test]
fn test_long_title_clean_truncation_without_overflow() {
    let mut app = App::new("test-project", "sonnet", "sandbox");
    let long_title = "This is an extremely long goal description designed to exceed the maximum available horizontal column width of the resume area in the Start screen";
    app.resume_sessions = vec![
        ResumeSession {
            session_id: "sess_long".to_string(),
            title: long_title.to_string(),
            relative_time: "2m".to_string(),
        },
    ];

    let area = Rect::new(0, 0, 100, 30);
    let theme = HexTheme::truecolor();
    let anim = AnimationState::new();

    let mut buf = Buffer::empty(area);
    start::render(&app, &theme, &anim, area, &mut buf);

    let resume_area = app.get_start_resume_area(area);
    let row_y = resume_area.y + 1;

    let mut line_chars = String::new();
    for x in resume_area.x..resume_area.x + resume_area.width {
        line_chars.push_str(buf[(x, row_y)].symbol());
    }

    assert!(line_chars.contains('…'), "Truncated title must contain ellipsis '…'");
    assert!(line_chars.contains("2m"), "Relative timestamp must remain visible");
    assert!(line_chars.contains('✕'), "Delete button '✕' must remain intact");

    // Ensure the delete button is rendered exactly at the right side inside the resume area
    let right_part = &line_chars[line_chars.len().saturating_sub(10)..];
    assert!(right_part.contains('✕'), "Delete button '✕' must be at right edge of resume area");
}

#[test]
fn test_delete_session_ui_and_sqlite_cascading_deletion() {
    // 1. Setup a fresh isolated temporary SQLite database
    let rand_id = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let temp_dir = std::env::temp_dir().join(format!("hex_test_del_{}", rand_id));
    let hex_dir = temp_dir.join(".hex").join("data");
    fs::create_dir_all(&hex_dir).expect("create .hex/data");
    let db_path = hex_dir.join("ledger.db");

    let init_sql = "
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY, goal TEXT, created_at TEXT);
        CREATE TABLE tool_calls (call_id TEXT PRIMARY KEY, task_id TEXT, tool_id TEXT, arguments_redacted_json TEXT, result_json TEXT, status TEXT, started_at TEXT);
        CREATE TABLE task_events (event_id TEXT PRIMARY KEY, task_id TEXT, event_type TEXT, payload_json TEXT, sequence_no INTEGER, occurred_at TEXT);
        CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, task_id TEXT);
        CREATE TABLE plans (plan_id TEXT PRIMARY KEY, task_id TEXT, revision INTEGER, dag_json TEXT);
        CREATE TABLE checkpoints (checkpoint_id TEXT PRIMARY KEY, task_id TEXT);
        CREATE TABLE approvals (approval_id TEXT PRIMARY KEY, task_id TEXT);
        CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY, task_id TEXT);
        CREATE TABLE proof_bundles (bundle_id TEXT PRIMARY KEY, task_id TEXT);
        CREATE TABLE leases (lease_id TEXT PRIMARY KEY, task_id TEXT);
        CREATE TABLE approval_leases (lease_id TEXT PRIMARY KEY, task_id TEXT);
        CREATE TABLE dialogue_turns (turn_id TEXT PRIMARY KEY, task_id TEXT, turn_index INTEGER, role TEXT, content TEXT);
        CREATE TABLE task_search_fts (task_id TEXT, goal TEXT);
    ";

    let status = Command::new("sqlite3")
        .args([db_path.to_str().unwrap(), init_sql])
        .status()
        .expect("init db");
    assert!(status.success(), "SQLite schema creation failed");

    // Insert records for task 'test_target_task' across all 11 tables
    let target_id = "test_target_task";
    let insert_sql = format!("
        INSERT INTO tasks VALUES ('{id}', 'Target task goal', '2026-11-16T10:00:00Z');
        INSERT INTO tool_calls VALUES ('c1', '{id}', 'read', '{{}}', 'ok', 'COMPLETED', '2026-11-16T10:01:00Z');
        INSERT INTO task_events VALUES ('e1', '{id}', 'STATE_SUCCEEDED', '{{\"result\":\"done\"}}', 1, '2026-11-16T10:02:00Z');
        INSERT INTO attempts VALUES ('att1', '{id}');
        INSERT INTO plans VALUES ('p1', '{id}', 1, '[]');
        INSERT INTO checkpoints VALUES ('chk1', '{id}');
        INSERT INTO approvals VALUES ('app1', '{id}');
        INSERT INTO artifacts VALUES ('art1', '{id}');
        INSERT INTO proof_bundles VALUES ('pb1', '{id}');
        INSERT INTO leases VALUES ('l1', '{id}');
        INSERT INTO approval_leases VALUES ('al1', '{id}');
        INSERT INTO dialogue_turns VALUES ('t1', '{id}', 0, 'user', 'Target task goal');
        INSERT INTO dialogue_turns VALUES ('t2', '{id}', 1, 'assistant', 'Done.');
        INSERT INTO task_search_fts VALUES ('{id}', 'Target task goal');
    ", id = target_id);

    let status = Command::new("sqlite3")
        .args([db_path.to_str().unwrap(), &insert_sql])
        .status()
        .expect("insert records");
    assert!(status.success(), "Seed insertion failed");

    // 2. Test App deletion UI logic
    let mut app = App::new("test-project", "sonnet", "sandbox");
    app.workspace_path = temp_dir.to_str().unwrap().to_string();
    app.resume_sessions = vec![
        ResumeSession {
            session_id: target_id.to_string(),
            title: "Target task goal".to_string(),
            relative_time: "5m".to_string(),
        },
        ResumeSession {
            session_id: "other_task".to_string(),
            title: "Other task".to_string(),
            relative_time: "1h".to_string(),
        },
    ];

    let area = Rect::new(0, 0, 100, 30);
    let resume_area = app.get_start_resume_area(area);

    // First click arms deletion; nothing is removed until it is confirmed.
    let del_click_x = resume_area.x + resume_area.width - 2;
    let del_click_y = resume_area.y + 1;
    let handled = app.handle_start_mouse_click(del_click_x, del_click_y, resume_area);
    assert!(handled, "Clicking delete button must be handled");
    assert_eq!(app.resume_sessions.len(), 2, "first click must not delete");
    assert!(app.status_message.contains("Press d or Delete again to confirm"));

    let handled = app.handle_start_mouse_click(del_click_x, del_click_y, resume_area);
    assert!(handled, "Confirming click must be handled");

    // UI list removed the target session
    assert_eq!(app.resume_sessions.len(), 1);
    assert_eq!(app.resume_sessions[0].session_id, "other_task");
    assert!(app.status_message.contains("Deleted session: Target task goal"));

    // 3. Verify SQLite DB: target task is completely wiped, conversation included
    let tables = [
        "tasks",
        "tool_calls",
        "task_events",
        "attempts",
        "plans",
        "checkpoints",
        "approvals",
        "artifacts",
        "proof_bundles",
        "leases",
        "approval_leases",
        "dialogue_turns",
        "task_search_fts",
    ];

    for tbl in tables {
        let count_out = Command::new("sqlite3")
            .args([
                db_path.to_str().unwrap(),
                &format!("SELECT count(*) FROM {} WHERE task_id = '{}';", tbl, target_id),
            ])
            .output()
            .expect("query count");
        let count_str = String::from_utf8(count_out.stdout).unwrap().trim().to_string();
        assert_eq!(
            count_str, "0",
            "Table '{}' must have 0 rows for deleted task '{}'",
            tbl, target_id
        );
    }

    app.resume_sessions = vec![ResumeSession {
        session_id: "last_task".to_string(),
        title: "Last task".to_string(),
        relative_time: "1m".to_string(),
    }];
    let status = Command::new("sqlite3")
        .args([db_path.to_str().unwrap(), "INSERT INTO tasks VALUES ('last_task', 'Last task', '2026-11-16T10:00:00Z');"])
        .status().unwrap();
    assert!(status.success());
    app.input_buffer = "preserved draft".to_string();
    app.cursor_pos = 3;
    app.request_delete_resume_session(0);
    assert!(app.request_delete_resume_session(0));
    assert!(app.resume_sessions.is_empty());
    assert_eq!(app.focus, Focus::Input);
    assert_eq!(app.input_buffer, "preserved draft");
    assert_eq!(app.cursor_pos, 3);

    // Clean up temp dir
    let _ = fs::remove_dir_all(&temp_dir);
}

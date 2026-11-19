//! The resume list must reflect the real ledger: every session is listed, the
//! count is the true total, and a confirmed delete removes it for good.

use hex_protocol::models::Role;
use hex_tui::app::{
    delete_session_from_db, load_real_resume_sessions, load_session_dialogue_from_db,
    load_session_plan_from_db, load_tool_result_page, App, DialogueItem, PlanStep,
    ScreenState, StepStatus,
};
use std::fs;
use std::path::{Path, PathBuf};
use rusqlite::Connection;

fn temp_workspace(tag: &str) -> PathBuf {
    let unique = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!("hex_history_{tag}_{unique}"));
    fs::create_dir_all(dir.join(".hex").join("data")).expect("create workspace");
    dir
}

fn sqlite(db: &Path, sql: &str) -> String {
    let conn = Connection::open(db).expect("open fixture");
    if sql.trim_start().starts_with("SELECT") {
        conn.query_row(sql, [], |row| row.get::<_, i64>(0))
            .expect("query fixture")
            .to_string()
    } else {
        conn.execute_batch(sql).expect("seed fixture");
        String::new()
    }
}

fn seed_ledger(dir: &Path, count: usize) -> PathBuf {
    let db = dir.join(".hex").join("data").join("ledger.db");
    // Deliberately partial: a ledger written before the newer task tables existed.
    sqlite(
        &db,
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, goal TEXT, created_at TEXT);
         CREATE TABLE dialogue_turns (turn_id TEXT PRIMARY KEY, task_id TEXT, content TEXT);
         CREATE TABLE tool_calls (call_id TEXT PRIMARY KEY, task_id TEXT);",
    );
    for i in 0..count {
        sqlite(
            &db,
            &format!(
                "INSERT INTO tasks VALUES ('tsk_{i}', 'Task number {i}', '2026-11-01T{:02}:{:02}:00Z');
                 INSERT INTO dialogue_turns VALUES ('turn_{i}', 'tsk_{i}', 'hello');
                 INSERT INTO tool_calls VALUES ('call_{i}', 'tsk_{i}');",
                i / 60,
                i % 60
            ),
        );
    }
    db
}

#[test]
fn test_history_is_not_truncated_at_fifty() {
    let dir = temp_workspace("full");
    seed_ledger(&dir, 120);

    let sessions = load_real_resume_sessions(&dir).unwrap();
    assert_eq!(sessions.len(), 120, "every stored session must be listed");
    assert_eq!(sessions[0].session_id, "tsk_119", "newest first");

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn test_deleting_shrinks_the_real_history() {
    let dir = temp_workspace("shrink");
    let db = seed_ledger(&dir, 60);

    let mut app = App::new("proj", "", "apfs_clone");
    app.workspace_path = dir.to_string_lossy().to_string();
    app.resume_sessions = load_real_resume_sessions(&dir).unwrap();
    assert_eq!(app.resume_sessions.len(), 60);

    // Deleting the three visible rows must not be refilled from a hidden tail.
    for _ in 0..3 {
        assert!(app.delete_resume_session(0), "delete must succeed");
    }
    assert_eq!(app.resume_sessions.len(), 57);

    let reloaded = load_real_resume_sessions(&dir).unwrap();
    assert_eq!(reloaded.len(), 57, "the ledger really lost three sessions");
    assert_eq!(sqlite(&db, "SELECT COUNT(*) FROM tasks;"), "57");

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn test_delete_removes_conversation_turns_and_tool_calls() {
    let dir = temp_workspace("cascade");
    let db = seed_ledger(&dir, 2);

    assert!(delete_session_from_db(&dir, "tsk_0"));
    assert_eq!(sqlite(&db, "SELECT COUNT(*) FROM tasks WHERE task_id='tsk_0';"), "0");
    assert_eq!(sqlite(&db, "SELECT COUNT(*) FROM dialogue_turns WHERE task_id='tsk_0';"), "0");
    assert_eq!(sqlite(&db, "SELECT COUNT(*) FROM tool_calls WHERE task_id='tsk_0';"), "0");
    // Untouched neighbours keep their rows.
    assert_eq!(sqlite(&db, "SELECT COUNT(*) FROM dialogue_turns WHERE task_id='tsk_1';"), "1");

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn test_armed_delete_is_visible_and_cancellable() {
    use hex_tui::ui::animation::AnimationState;
    use hex_tui::ui::screens::start;
    use hex_tui::ui::style::HexTheme;
    use ratatui::buffer::Buffer;
    use ratatui::layout::Rect;

    let dir = temp_workspace("armed");
    seed_ledger(&dir, 1);

    let mut app = App::new("proj", "", "apfs_clone");
    app.workspace_path = dir.to_string_lossy().to_string();
    app.resume_sessions = load_real_resume_sessions(&dir).unwrap();
    app.resume_sessions[0].title =
        "An extremely long task title that cannot possibly fit in the hint line".to_string();

    assert!(!app.request_delete_resume_session(0), "first press only arms");

    // The prompt must fit the narrow hint line and keep the instruction intact.
    for width in [80u16, 100, 140] {
        let area = Rect::new(0, 0, width, 34);
        let mut buf = Buffer::empty(area);
        start::render(&app, &HexTheme::truecolor(), &AnimationState::new(), area, &mut buf);
        let mut text = String::new();
        for y in 0..area.height {
            for x in 0..area.width {
                text.push_str(buf[(x, y)].symbol());
            }
            text.push('\n');
        }
        assert!(text.contains("esc cancel"), "instruction survives at width {width}");
        assert!(text.contains("✕?"), "armed row is marked at width {width}");
    }

    // Typing abandons the armed delete.
    app.insert_char('h');
    assert!(app.pending_delete_index.is_none());

    // Esc abandons it too, and the ledger is untouched.
    app.input_buffer.clear();
    app.request_delete_resume_session(0);
    assert!(app.cancel_pending_delete());
    assert!(!app.cancel_pending_delete(), "nothing left to cancel");
    assert_eq!(app.resume_sessions.len(), 1);
    assert_eq!(load_real_resume_sessions(&dir).unwrap().len(), 1);

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn test_delete_reports_failure_for_unknown_session() {
    let dir = temp_workspace("missing");
    seed_ledger(&dir, 1);

    // Nothing to delete: the row was never there, so success must not be claimed.
    assert!(!delete_session_from_db(&dir, "tsk_does_not_exist"));

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn test_delete_failure_keeps_the_row_visible() {
    let dir = temp_workspace("locked");
    seed_ledger(&dir, 1);

    let mut app = App::new("proj", "", "apfs_clone");
    // Point at a workspace with no ledger: the delete cannot happen.
    app.workspace_path = dir.join("elsewhere").to_string_lossy().to_string();
    app.resume_sessions = load_real_resume_sessions(&dir).unwrap();
    assert_eq!(app.resume_sessions.len(), 1);

    assert!(!app.delete_resume_session(0));
    assert_eq!(app.resume_sessions.len(), 1, "row stays when the delete failed");
    assert!(app.status_message.contains("Could not delete"));

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn test_multiline_and_pipe_goals_are_not_dropped() {
    let dir = temp_workspace("awkward");
    let db = dir.join(".hex").join("data").join("ledger.db");
    sqlite(
        &db,
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, goal TEXT, created_at TEXT);",
    );
    sqlite(
        &db,
        "INSERT INTO tasks VALUES ('tsk_plan', '[MODE: PLAN]' || char(10) || 'read only' || char(13) || 'no writes', '2026-11-03T10:00:00Z');
         INSERT INTO tasks VALUES ('tsk_pipe', 'a | b | c', '2026-11-02T10:00:00Z');
         INSERT INTO tasks VALUES ('tsk_plain', 'plain goal', '2026-11-01T10:00:00Z');",
    );

    let sessions = load_real_resume_sessions(&dir).unwrap();
    assert_eq!(sessions.len(), 3, "a goal spanning several lines must still be listed");
    assert_eq!(sessions[0].session_id, "tsk_plan");
    assert_eq!(sessions[0].title, "[MODE: PLAN] read only no writes");
    assert_eq!(sessions[1].session_id, "tsk_pipe");
    assert_eq!(sessions[1].title, "a | b | c", "a pipe in the goal must not shift the timestamp field");
    assert_eq!(sessions[2].session_id, "tsk_plain");

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn embedded_ledger_works_without_sqlite_on_path() {
    if std::env::var_os("HEX_B24_NO_PATH_CHILD").is_none() {
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "embedded_ledger_works_without_sqlite_on_path", "--nocapture"])
            .env("PATH", "")
            .env("HEX_B24_NO_PATH_CHILD", "1")
            .output().unwrap();
        assert!(output.status.success(), "{}\n{}",
            String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr));
        return;
    }
    assert_eq!(std::env::var("PATH").unwrap(), "");
    let dir = temp_workspace("no_path");
    let conn = Connection::open(dir.join(".hex/data/ledger.db")).unwrap();
    conn.execute_batch(
        r#"CREATE TABLE tasks(task_id TEXT PRIMARY KEY, goal TEXT, created_at TEXT);
         CREATE TABLE tool_calls(call_id TEXT, task_id TEXT, tool_id TEXT, arguments_redacted_json TEXT, result_json TEXT, status TEXT, started_at TEXT);
         CREATE TABLE task_events(task_id TEXT, event_type TEXT, payload_json TEXT, sequence_no INTEGER, occurred_at TEXT);
         CREATE TABLE plans(task_id TEXT, revision INTEGER, dag_json TEXT);
         CREATE VIRTUAL TABLE task_search_fts USING fts5(task_id UNINDEXED, goal);
         INSERT INTO tasks VALUES('tsk_path', 'No external executable', '2026-11-01T10:00:00Z');
         INSERT INTO tool_calls VALUES('cal_path', 'tsk_path', 'read', '{}', 'stored result', 'SEALED', 'now');
         INSERT INTO task_events VALUES('tsk_path', 'STATE_SUCCEEDED', '{"result":"done"}', 1, 'now');
         INSERT INTO plans VALUES('tsk_path', 1, '[{"title":"Read","status":"COMPLETED"}]');
         INSERT INTO task_search_fts VALUES('tsk_path', 'No external executable');"#
    ).unwrap();
    assert_eq!(load_real_resume_sessions(&dir).unwrap()[0].session_id, "tsk_path");
    assert_eq!(load_session_dialogue_from_db(&dir, "tsk_path").unwrap().len(), 3);
    assert_eq!(load_session_plan_from_db(&dir, "tsk_path").unwrap()[0].title, "Read");
    assert_eq!(load_tool_result_page(&dir, "tsk_path", "cal_path", 0).unwrap().content, "stored result");
    let mut app = App::with_workspace("test", "", "none", Some(&dir));
    assert!(app.resume_session("tsk_path"));
    assert_eq!(app.screen_state, ScreenState::Session);
    assert!(delete_session_from_db(&dir, "tsk_path"));
    assert!(load_real_resume_sessions(&dir).unwrap().is_empty());
    for table in ["tasks", "tool_calls", "task_events", "plans", "task_search_fts"] {
        assert_eq!(conn.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get::<_, i64>(0)).unwrap(), 0);
    }
    drop(conn);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn missing_ledger_is_empty_and_is_never_created() {
    let dir = temp_workspace("absent_db");
    let db = dir.join(".hex/data/ledger.db");
    assert!(load_real_resume_sessions(&dir).unwrap().is_empty());
    assert!(load_session_dialogue_from_db(&dir, "tsk_1").unwrap().is_empty());
    assert!(load_session_plan_from_db(&dir, "tsk_1").unwrap().is_empty());
    assert!(load_tool_result_page(&dir, "tsk_1", "cal_1", 0).is_err());
    assert!(!delete_session_from_db(&dir, "tsk_1"));
    assert!(!db.exists());
    let app = App::with_workspace("test", "", "none", Some(&dir));
    assert!(app.resume_sessions.is_empty());
    assert_eq!(app.status_message, "Ready");
    assert!(!db.exists());
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn corrupt_ledger_errors_are_visible_without_switching_sessions() {
    let dir = temp_workspace("corrupt");
    let db = dir.join(".hex/data/ledger.db");
    fs::write(&db, b"not a SQLite database").unwrap();
    assert!(load_real_resume_sessions(&dir).unwrap_err().contains("not a database"));
    assert!(load_session_dialogue_from_db(&dir, "tsk_1").unwrap_err().contains("not a database"));
    assert!(load_session_plan_from_db(&dir, "tsk_1").unwrap_err().contains("not a database"));
    assert!(load_tool_result_page(&dir, "tsk_1", "cal_1", 0).unwrap_err().contains("not a database"));
    let mut app = App::with_workspace("test", "", "none", Some(&dir));
    assert!(app.status_message.contains("Could not load resume history"));
    app.session_id = "ses_existing".to_string();
    app.add_user_message("existing transcript");
    app.input_buffer = "unsent draft".to_string();
    app.cursor_pos = 3;
    app.command_history = vec!["earlier command".to_string()];
    app.plan_steps = vec![PlanStep { title: "Existing plan".to_string(), status: StepStatus::Active }];
    let before = app.dialogue_items.clone();
    assert!(!app.resume_session("tsk_1"));
    assert_eq!(app.session_id, "ses_existing");
    assert_eq!(app.dialogue_items, before);
    assert_eq!(app.input_buffer, "unsent draft");
    assert_eq!(app.cursor_pos, 3);
    assert_eq!(app.command_history, ["earlier command"]);
    assert_eq!(app.plan_steps[0].title, "Existing plan");
    assert!(app.status_message.contains("Could not load session dialogue"));
    assert!(!delete_session_from_db(&dir, "tsk_1"));
    assert_eq!(fs::read(db).unwrap(), b"not a SQLite database");
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn legacy_optional_tables_are_empty_but_real_schema_errors_are_not() {
    let dir = temp_workspace("optional");
    let conn = Connection::open(dir.join(".hex/data/ledger.db")).unwrap();
    conn.execute_batch(
        "CREATE TABLE tasks(task_id TEXT PRIMARY KEY, goal TEXT, created_at TEXT);
         INSERT INTO tasks VALUES('tsk_old', 'Legacy goal', 'now');"
    ).unwrap();
    let items = load_session_dialogue_from_db(&dir, "tsk_old").unwrap();
    assert_eq!(items.len(), 1);
    assert!(matches!(&items[0], DialogueItem::Message(m) if m.role == Role::User && m.content == "Legacy goal"));
    assert!(load_session_plan_from_db(&dir, "tsk_old").unwrap().is_empty());
    conn.execute_batch("CREATE TABLE plans(task_id TEXT, revision INTEGER, dag_json TEXT);
        INSERT INTO plans VALUES('tsk_old', 1, 'not json');").unwrap();
    let mut app = App::with_workspace("test", "", "none", Some(&dir));
    let original_id = app.session_id.clone();
    let before = app.dialogue_items.clone();
    assert!(!app.resume_session("tsk_old"));
    assert_eq!(app.screen_state, ScreenState::Start);
    assert_eq!(app.session_id, original_id);
    assert_eq!(app.dialogue_items, before);
    assert!(app.status_message.contains("Could not load session plan"));
    conn.execute_batch("CREATE TABLE tool_calls(task_id TEXT);").unwrap();
    assert!(load_session_dialogue_from_db(&dir, "tsk_old").is_err());
    drop(conn);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn task_parameters_and_control_separators_cannot_shift_rows() {
    let dir = temp_workspace("parameters");
    let conn = Connection::open(dir.join(".hex/data/ledger.db")).unwrap();
    conn.execute_batch("CREATE TABLE tasks(task_id TEXT PRIMARY KEY, goal TEXT, created_at TEXT);
        CREATE TABLE plans(task_id TEXT, revision INTEGER, dag_json TEXT);").unwrap();
    let task = "tsk_' OR 1=1 --";
    let goal = "世界😀 | first\nsecond\rthird\u{1f}last\u{1e}end";
    conn.execute("INSERT INTO tasks VALUES(?1, ?2, '2026-11-03T10:00:00Z')", rusqlite::params![task, goal]).unwrap();
    conn.execute("INSERT INTO tasks VALUES('tsk_neighbor', 'neighbor', '2026-11-02T10:00:00Z')", []).unwrap();
    conn.execute("INSERT INTO plans VALUES(?1, 1, ?2)", rusqlite::params![task, "[{\"title\":\"世界😀 | one\\nsecond\\u001fend\",\"status\":\"RUNNING\"}]"]).unwrap();
    let sessions = load_real_resume_sessions(&dir).unwrap();
    assert_eq!(sessions.len(), 2);
    assert_eq!(sessions[0].session_id, task);
    assert_eq!(sessions[0].title, "世界😀 | first second third last\u{1e}end");
    let dialogue = load_session_dialogue_from_db(&dir, task).unwrap();
    assert_eq!(dialogue.len(), 1);
    assert!(matches!(&dialogue[0], DialogueItem::Message(m) if m.content == goal));
    let plan = load_session_plan_from_db(&dir, task).unwrap();
    assert_eq!(plan[0].title, "世界😀 | one\nsecond\u{1f}end");
    assert_eq!(plan[0].status, StepStatus::Active);
    assert!(load_session_dialogue_from_db(&dir, "' OR 1=1 --").unwrap().is_empty());
    assert!(load_session_plan_from_db(&dir, "' OR 1=1 --").unwrap().is_empty());
    assert!(!delete_session_from_db(&dir, "' OR 1=1 --"));
    assert!(delete_session_from_db(&dir, task));
    assert_eq!(load_real_resume_sessions(&dir).unwrap()[0].session_id, "tsk_neighbor");
    drop(conn);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn failed_or_ignored_delete_rolls_back_every_scoped_row_including_fts5() {
    let tables = ["tasks", "dialogue_turns", "tool_calls", "task_events", "attempts", "plans",
        "checkpoints", "approvals", "artifacts", "proof_bundles", "leases", "approval_leases", "task_search_fts"];
    for (failure, blocked_table) in [("ABORT, 'blocked'", "tasks"), ("FAIL, 'blocked'", "tasks"),
        ("IGNORE", "tasks"), ("IGNORE", "tool_calls")] {
        let dir = temp_workspace("rollback");
        let conn = Connection::open(dir.join(".hex/data/ledger.db")).unwrap();
        for table in tables {
            if table == "task_search_fts" {
                conn.execute_batch("CREATE VIRTUAL TABLE task_search_fts USING fts5(task_id UNINDEXED, value);").unwrap();
            } else {
                conn.execute_batch(&format!("CREATE TABLE {table}(task_id TEXT, value TEXT);")).unwrap();
            }
            for task in ["tsk_target", "tsk_neighbor"] {
                conn.execute(&format!("INSERT INTO {table} VALUES(?1, ?2)"), rusqlite::params![task, format!("{table}: {task} 世界😀\n\u{1f}")]).unwrap();
            }
        }
        conn.execute_batch("CREATE TABLE unrelated(task_id TEXT, value TEXT);
            INSERT INTO unrelated VALUES('tsk_target', 'must stay');").unwrap();
        let snapshot = || -> Vec<Vec<(String, String)>> {
            tables.iter().map(|table| {
                conn.prepare(&format!("SELECT task_id, value FROM {table} ORDER BY task_id, value")).unwrap()
                    .query_map([], |row| Ok((row.get(0)?, row.get(1)?))).unwrap()
                    .collect::<rusqlite::Result<Vec<_>>>().unwrap()
            }).collect()
        };
        let before = snapshot();
        assert!(!delete_session_from_db(&dir, "tsk_unknown"));
        assert_eq!(snapshot(), before);
        conn.execute_batch(&format!("CREATE TRIGGER block_delete BEFORE DELETE ON {blocked_table}
            WHEN OLD.task_id = 'tsk_target' BEGIN SELECT RAISE({failure}); END;")).unwrap();
        assert!(!delete_session_from_db(&dir, "tsk_target"));
        assert_eq!(snapshot(), before, "every row must survive {failure}");
        conn.execute_batch("DROP TRIGGER block_delete;").unwrap();
        assert!(delete_session_from_db(&dir, "tsk_target"));
        for rows in snapshot() {
            assert_eq!(rows.len(), 1);
            assert_eq!(rows[0].0, "tsk_neighbor");
        }
        assert_eq!(conn.query_row("SELECT value FROM unrelated", [], |row| row.get::<_, String>(0)).unwrap(), "must stay");
        drop(conn);
        fs::remove_dir_all(dir).unwrap();
    }
}
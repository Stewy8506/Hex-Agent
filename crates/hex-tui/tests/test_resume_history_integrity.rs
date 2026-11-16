//! The resume list must reflect the real ledger: every session is listed, the
//! count is the true total, and a confirmed delete removes it for good.

use hex_tui::app::{delete_session_from_db, load_real_resume_sessions, App};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

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
    let out = Command::new("sqlite3")
        .args([db.to_str().unwrap(), sql])
        .output()
        .expect("run sqlite3");
    assert!(out.status.success(), "sqlite failed: {}", String::from_utf8_lossy(&out.stderr));
    String::from_utf8(out.stdout).unwrap().trim().to_string()
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

    let sessions = load_real_resume_sessions(&dir);
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
    app.resume_sessions = load_real_resume_sessions(&dir);
    assert_eq!(app.resume_sessions.len(), 60);

    // Deleting the three visible rows must not be refilled from a hidden tail.
    for _ in 0..3 {
        assert!(app.delete_resume_session(0), "delete must succeed");
    }
    assert_eq!(app.resume_sessions.len(), 57);

    let reloaded = load_real_resume_sessions(&dir);
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
    app.resume_sessions = load_real_resume_sessions(&dir);
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
    assert_eq!(load_real_resume_sessions(&dir).len(), 1);

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
    app.resume_sessions = load_real_resume_sessions(&dir);
    assert_eq!(app.resume_sessions.len(), 1);

    assert!(!app.delete_resume_session(0));
    assert_eq!(app.resume_sessions.len(), 1, "row stays when the delete failed");
    assert!(app.status_message.contains("Could not delete"));

    let _ = fs::remove_dir_all(&dir);
}
use hex_tui::app::{load_session_dialogue_from_db, load_tool_result_page, tool_preview, App, DialogueItem};
use std::process::Command;

#[test]
fn direct_tool_results_and_arguments_are_bounded_without_splitting_unicode() {
    let mut app = App::new("memory", "offline", "none");
    let huge = "世界😀".repeat(400_000);
    app.add_tool_call("cal_1", "read_file", &huge, None, None);
    app.complete_tool_call("cal_1", &huge, 1, false);
    let DialogueItem::ToolCall(tool) = app.dialogue_items.last().unwrap() else { panic!() };
    assert!(tool.args.len() < 4100);
    assert!(tool.result.as_ref().unwrap().len() < 1300);
    assert!(tool.result.as_ref().unwrap().contains("/tool-result"));
    assert_eq!(tool_preview("small", 300), "small");
}

#[test]
fn resume_reads_previews_and_full_result_pages_are_exact_and_task_scoped() {
    let workspace = std::env::temp_dir().join(format!("hex_tool_pages_{}", std::process::id()));
    std::fs::create_dir_all(workspace.join(".hex/data")).unwrap();
    let db = workspace.join(".hex/data/ledger.db");
    let sql = "CREATE TABLE tasks(task_id TEXT, goal TEXT, created_at TEXT);
        CREATE TABLE tool_calls(call_id TEXT, task_id TEXT, tool_id TEXT, arguments_redacted_json TEXT, result_json TEXT, status TEXT, started_at TEXT);
        INSERT INTO tasks VALUES('tsk_1', 'memory test', 'now');
        INSERT INTO tool_calls VALUES('cal_1','tsk_1','read_file','{}',replace(hex(zeroblob(3000000)), '00', '世界😀'), 'SEALED', 'now');";
    assert!(Command::new("sqlite3").args([db.to_str().unwrap(), sql]).status().unwrap().success());
    let items = load_session_dialogue_from_db(&workspace, "tsk_1");
    let tool = items.iter().find_map(|item| if let DialogueItem::ToolCall(tool) = item { Some(tool) } else { None }).unwrap();
    assert!(tool.result.as_ref().unwrap().len() < 1500);
    let page = load_tool_result_page(&workspace, "tsk_1", "cal_1", 0).unwrap();
    assert_eq!(page.content.chars().count(), 4096);
    assert_eq!(page.total_chars, 9_000_000);
    assert_eq!(page.next_offset, Some(4096));
    let end = load_tool_result_page(&workspace, "tsk_1", "cal_1", page.total_chars - 6).unwrap();
    assert_eq!(end.content, "世界😀世界😀");
    assert_eq!(end.next_offset, None);
    assert!(load_tool_result_page(&workspace, "tsk_other", "cal_1", 0).is_err());
    assert!(load_tool_result_page(&workspace, "tsk_1';--", "cal_1", 0).is_err());
    assert!(load_tool_result_page(&workspace, "tsk_1", "cal_1", usize::MAX).is_err());
    std::fs::remove_dir_all(workspace).unwrap();
}

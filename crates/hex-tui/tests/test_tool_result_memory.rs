use hex_tui::app::{load_session_dialogue_from_db, load_tool_result_page, tool_preview, App, DialogueItem};
use rusqlite::Connection;

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
    Connection::open(&db).unwrap().execute_batch(sql).unwrap();
    let items = load_session_dialogue_from_db(&workspace, "tsk_1").unwrap();
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

#[test]
fn unicode_multiline_control_separator_pages_are_exact_and_previews_are_bounded() {
    let unique = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let workspace = std::env::temp_dir().join(format!("hex_unicode_pages_{unique}"));
    std::fs::create_dir_all(workspace.join(".hex/data")).unwrap();
    let conn = Connection::open(workspace.join(".hex/data/ledger.db")).unwrap();
    conn.execute_batch("CREATE TABLE tasks(task_id TEXT, goal TEXT, created_at TEXT);
        CREATE TABLE tool_calls(call_id TEXT, task_id TEXT, tool_id TEXT, arguments_redacted_json TEXT, result_json TEXT, status TEXT, started_at TEXT);
        INSERT INTO tasks VALUES('tsk_unicode', 'unicode pages', 'now');").unwrap();
    let content = "世界😀\n\r\t\u{1f}\u{1e}|".repeat(1500);
    let arguments = "引数😀\n\u{1f}".repeat(500);
    for (call, status, time) in [("cal_complete", "SEALED", "1"), ("cal_failed", "FAILED", "2")] {
        conn.execute("INSERT INTO tool_calls VALUES(?1, 'tsk_unicode', 'read', ?2, ?3, ?4, ?5)",
            rusqlite::params![call, arguments, content, status, time]).unwrap();
    }
    conn.execute_batch("INSERT INTO tool_calls VALUES('cal_complete', 'tsk_neighbor', 'read', '{}', 'wrong task', 'SEALED', '0');
        INSERT INTO tool_calls VALUES('cal_empty', 'tsk_unicode', 'read', '{}', '', 'SEALED', '3');
        INSERT INTO tool_calls VALUES('cal_null', 'tsk_unicode', 'read', '{}', NULL, 'SEALED', '4');").unwrap();
    let items = load_session_dialogue_from_db(&workspace, "tsk_unicode").unwrap();
    let tools: Vec<_> = items.iter().filter_map(|item| if let DialogueItem::ToolCall(tool) = item { Some(tool) } else { None }).collect();
    assert_eq!(tools.len(), 4);
    for (tool, limit, status) in [(tools[0], 300, "exit 0"), (tools[1], 4000, "exit 1")] {
        assert_eq!(tool.args, arguments.chars().take(1000).collect::<String>());
        assert_eq!(tool.result.as_ref().unwrap(), &format!("{}… [full result: /tool-result {}]", content.chars().take(limit).collect::<String>(), tool.id));
        assert_eq!(tool.exit_status.as_deref(), Some(status));
    }
    assert!(tools[2].result.is_none());
    assert!(tools[3].result.is_none());
    let total = content.chars().count();
    let mut reconstructed = String::new();
    let mut offset = 0;
    loop {
        let page = load_tool_result_page(&workspace, "tsk_unicode", "cal_complete", offset).unwrap();
        assert_eq!(page.total_chars, total);
        assert_eq!(page.content, content.chars().skip(offset).take(4096).collect::<String>());
        assert!(page.content.chars().count() <= 4096);
        reconstructed.push_str(&page.content);
        if let Some(next) = page.next_offset {
            assert_eq!(next, offset + 4096);
            offset = next;
        } else { break; }
    }
    assert_eq!(reconstructed, content);
    let middle = load_tool_result_page(&workspace, "tsk_unicode", "cal_complete", 4097).unwrap();
    assert_eq!(middle.content, content.chars().skip(4097).take(4096).collect::<String>());
    assert_eq!(middle.next_offset, Some(8193));
    for offset in [total, total + 10] {
        let page = load_tool_result_page(&workspace, "tsk_unicode", "cal_complete", offset).unwrap();
        assert_eq!(page.content, "");
        assert_eq!(page.total_chars, total);
        assert_eq!(page.next_offset, None);
    }
    let empty = load_tool_result_page(&workspace, "tsk_unicode", "cal_empty", 0).unwrap();
    assert_eq!(empty.content, "");
    assert_eq!(empty.total_chars, 0);
    assert_eq!(empty.next_offset, None);
    assert!(load_tool_result_page(&workspace, "tsk_unicode", "cal_null", 0).is_err());
    assert_eq!(load_tool_result_page(&workspace, "tsk_neighbor", "cal_complete", 0).unwrap().content, "wrong task");
    for (task, call) in [("", "cal_complete"), ("tsk_unicode", ""), ("tsk_unicode", "cal_complete' OR 1=1 --"), ("tsk_unicode", "世界")] {
        assert!(load_tool_result_page(&workspace, task, call, 0).unwrap_err().contains("Invalid tool result identifier"));
    }
    drop(conn);
    std::fs::remove_dir_all(workspace).unwrap();
}

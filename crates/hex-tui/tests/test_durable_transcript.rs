use hex_protocol::models::Role;
use hex_tui::app::{App, DialogueItem, MessageItem, ToolResultRef};
use hex_tui::history::{COMMAND_HISTORY_BUDGET, MESSAGE_DISPLAY_BYTES, TRANSCRIPT_BUDGET};
use hex_tui::TranscriptStore;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

struct Workspace(PathBuf);
impl Workspace {
    fn new() -> Self {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "hex_transcript_{}_{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn app(&self, session: &str) -> App {
        let mut app = App::with_workspace("durable", "offline", "none", Some(&self.0));
        app.dialogue_items.clear();
        app.session_id = session.to_string();
        app.enable_transcript_storage().unwrap();
        app
    }
}
impl Drop for Workspace {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}

fn message(text: &str) -> DialogueItem {
    DialogueItem::Message(MessageItem {
        role: Role::Assistant,
        content: text.to_string(),
        timestamp: "test-time".to_string(),
    })
}

fn reconstruct(store: &TranscriptStore, id: usize) -> String {
    let mut source = String::new();
    let mut offset = 0;
    loop {
        let page = store.page(id, offset).unwrap();
        assert!(page.content.len() <= 4096);
        source.push_str(&page.content);
        if let Some(next) = page.next_offset {
            assert!(next > offset);
            offset = next;
        } else {
            assert_eq!(source.len(), page.total_bytes);
            return source;
        }
    }
}

fn displayed(app: &App) -> &str {
    match app.dialogue_items.last().unwrap() {
        DialogueItem::Message(msg) => &msg.content,
        _ => panic!("expected message"),
    }
}

#[test]
fn ten_thousand_items_plateau_including_allocated_capacity() {
    let workspace = Workspace::new();
    let mut app = workspace.app("plateau");
    let text = "世界𐐀 row\n".repeat(260);
    for i in 0..10_000 {
        app.add_assistant_message(&format!("{i}: {text}"));
        assert!(app.transcript_error.is_none(), "{:?}", app.transcript_error);
        assert!(app.resident_transcript_bytes() <= TRANSCRIPT_BUDGET);
    }
    assert!(app.dialogue_items.len() < 600);
    assert!(app.resident_transcript_bytes() > TRANSCRIPT_BUDGET / 2);
    let store = app.transcript_store.as_ref().unwrap();
    assert!(store.total_count().unwrap() > 9000);
    assert_eq!(reconstruct(store, 0), format!("0: {text}"));
    app.follow_dialogue = false;
    app.dialogue_scroll = 0;
    app.scroll_up();
    assert!(app.status_message.contains("/history"));
}

#[test]
fn giant_unicode_and_streaming_reconstruct_exactly_without_display_markers() {
    let workspace = Workspace::new();
    let mut app = workspace.app("unicode");
    let mut full = "界𐐷é\n".repeat(150_000);
    app.add_assistant_message(&full);
    assert!(displayed(&app).len() <= MESSAGE_DISPLAY_BYTES);
    assert!(displayed(&app).starts_with("[Earlier text stored: /transcript 0]"));
    for delta in ["a", "é", "𐐷", "\n", &"新𐐷".repeat(50_000), "finished"] {
        app.append_assistant_delta(delta);
        full.push_str(delta);
        assert!(displayed(&app).len() <= MESSAGE_DISPLAY_BYTES);
        assert!(app.transcript_error.is_none());
    }
    let store = app.transcript_store.as_ref().unwrap();
    assert_eq!(store.total_count().unwrap(), 1);
    assert_eq!(reconstruct(store, 0), full);
    assert!(!reconstruct(store, 0).contains("Earlier text stored"));
    let inside = store.page(0, 1).unwrap();
    assert!(inside.content.starts_with('𐐷'));
    assert!(store.page(0, usize::MAX).is_err());
    // The previously spilled completed item is not duplicated when later evicted.
    for _ in 0..600 {
        app.add_system_message(&"x".repeat(4096));
    }
    assert_eq!(reconstruct(app.transcript_store.as_ref().unwrap(), 0), full);
    app.clear_transcript().unwrap();
    let count = app
        .transcript_store
        .as_ref()
        .unwrap()
        .total_count()
        .unwrap();
    assert_eq!(count, 601);
}

#[test]
fn flush_preserves_recent_items_and_does_not_duplicate_settled_tools() {
    let workspace = Workspace::new();
    let mut app = workspace.app("flush");
    app.add_user_message("exact recent prompt");
    app.add_tool_call("ui", "read_file", "exact args", None, None);
    app.flush_transcript().unwrap();
    app.complete_tool_call("ui", "exact recent result", 12, false);
    app.add_assistant_message("exact recent response");
    app.flush_transcript().unwrap();
    let count = app
        .transcript_store
        .as_ref()
        .unwrap()
        .total_count()
        .unwrap();
    app.flush_transcript().unwrap();
    let store = app.transcript_store.as_ref().unwrap();
    assert_eq!(store.total_count().unwrap(), count);
    assert_eq!(reconstruct(store, 0), "exact recent prompt");
    assert_eq!(reconstruct(store, 2), "exact recent result");
    assert_eq!(reconstruct(store, 3), "exact recent response");
}

#[test]
fn crossing_display_limit_in_small_deltas_keeps_the_original_prefix() {
    let workspace = Workspace::new();
    let mut app = workspace.app("crossing");
    let initial = "é".repeat(30_000);
    app.add_assistant_message(&initial);
    let mut full = initial;
    for _ in 0..120 {
        let delta = "界𐐀\n".repeat(40);
        full.push_str(&delta);
        app.append_assistant_delta(&delta);
        assert!(displayed(&app).len() <= MESSAGE_DISPLAY_BYTES);
    }
    assert_eq!(
        app.transcript_store
            .as_ref()
            .unwrap()
            .total_count()
            .unwrap(),
        1
    );
    assert_eq!(reconstruct(app.transcript_store.as_ref().unwrap(), 0), full);
}

#[test]
fn storage_failure_retains_oversized_source_and_stops_eviction() {
    let workspace = Workspace::new();
    let mut app = workspace.app("failure");
    let path = app.transcript_store.as_ref().unwrap().path().join("0.txt");
    std::fs::create_dir(&path).unwrap();
    let original = "世界𐐷".repeat(500_000);
    app.add_assistant_message(&original);
    assert_eq!(displayed(&app), original);
    assert!(app.transcript_error.is_some());
    assert!(app.resident_transcript_bytes() > TRANSCRIPT_BUDGET);
    app.append_assistant_delta("new exact delta");
    assert_eq!(displayed(&app), format!("{original}new exact delta"));
    let len = app.dialogue_items.len();
    app.enforce_transcript_budget();
    assert_eq!(app.dialogue_items.len(), len);
    assert!(app.clear_transcript().is_err());
    assert_eq!(app.dialogue_items.len(), len);
}

#[test]
fn metadata_failure_does_not_shorten_and_stream_failure_keeps_new_delta() {
    let workspace = Workspace::new();
    let mut app = workspace.app("metadata_failure");
    std::fs::create_dir(app.transcript_store.as_ref().unwrap().path().join("0.json")).unwrap();
    let original = "原𐐷".repeat(30_000);
    app.add_assistant_message(&original);
    assert_eq!(displayed(&app), original);
    assert!(app.transcript_error.is_some());

    let mut app = workspace.app("delta_failure");
    app.add_assistant_message(&original);
    let root = app.transcript_store.as_ref().unwrap().path().to_path_buf();
    let moved = workspace.0.join("saved_archive");
    std::fs::rename(&root, &moved).unwrap();
    let delta = "not lost 𐐷".repeat(20_000);
    app.append_assistant_delta(&delta);
    assert!(app.transcript_error.is_some());
    assert!(displayed(&app).ends_with(&delta));
    assert!(displayed(&app).len() > MESSAGE_DISPLAY_BYTES);
    assert_eq!(
        std::fs::read_to_string(moved.join("0.txt")).unwrap(),
        original
    );
}

#[test]
fn completed_tool_metadata_and_references_survive_eviction() {
    let workspace = Workspace::new();
    let mut app = workspace.app("tools");
    let reference = ToolResultRef {
        task_id: "task_original".to_string(),
        call_id: "durable_call".to_string(),
        local: true,
    };
    app.add_tool_call(
        "ui-call/unsafe-as-path",
        "terminal",
        "{\"command\":\"test\"}",
        None,
        None,
    );
    app.tool_result_refs
        .insert("ui-call/unsafe-as-path".to_string(), reference.clone());
    app.complete_tool_call("ui-call/unsafe-as-path", "exact result", 123, true);
    for _ in 0..650 {
        app.add_assistant_message(&"m".repeat(4096));
    }
    assert!(!app.tool_result_refs.contains_key("ui-call/unsafe-as-path"));
    let store = app.transcript_store.as_ref().unwrap();
    assert_eq!(
        store.find_tool_reference("ui-call/unsafe-as-path"),
        Some(reference.clone())
    );
    assert_eq!(store.find_tool_reference("absent"), None);
    let meta: serde_json::Value =
        serde_json::from_slice(&std::fs::read(store.path().join("0.json")).unwrap()).unwrap();
    assert_eq!(meta["item"]["tool"], "terminal");
    assert_eq!(meta["item"]["exit_status"], "ERROR");
    assert_eq!(meta["item"]["duration_ms"], 123);
    assert_eq!(meta["item"]["collapsed"], true);
    assert!(meta["item"].get("started_at").is_none());
    assert_eq!(
        serde_json::from_value::<ToolResultRef>(meta["reference"].clone()).unwrap(),
        reference
    );
    assert!(app.resident_transcript_bytes() <= TRANSCRIPT_BUDGET);
}

#[test]
fn oversized_tool_sources_are_exact_and_failure_keeps_unbounded_source() {
    let workspace = Workspace::new();
    let mut app = workspace.app("tool_sources");
    let args = "引数𐐷".repeat(50_000);
    let result = "結果é".repeat(50_000);
    app.add_tool_call("ui", "read_file", &args, None, None);
    app.complete_tool_call("ui", &result, 1, false);
    let store = app.transcript_store.as_ref().unwrap();
    assert_eq!(reconstruct(store, 0), args);
    assert_eq!(reconstruct(store, 1), result);
    let DialogueItem::ToolCall(tool) = app.dialogue_items.last().unwrap() else {
        panic!()
    };
    assert!(tool.args.contains("/transcript 0"));
    assert!(tool.result.as_ref().unwrap().contains("/transcript 1"));

    let mut app = workspace.app("tool_failure");
    std::fs::create_dir(app.transcript_store.as_ref().unwrap().path().join("0.txt")).unwrap();
    app.add_tool_call("ui", "read_file", &args, None, None);
    app.complete_tool_call("ui", &result, 1, false);
    let DialogueItem::ToolCall(tool) = app.dialogue_items.last().unwrap() else {
        panic!()
    };
    assert_eq!(tool.args, args);
    assert_eq!(tool.result.as_deref(), Some(result.as_str()));
    assert!(app.transcript_error.is_some());
}

#[test]
fn summary_pages_backward_in_chronological_bounded_groups() {
    let workspace = Workspace::new();
    let mut store = TranscriptStore::new(&workspace.0, "summary").unwrap();
    for id in 0..70 {
        assert_eq!(
            store
                .append(&message(&format!("entry {id} 𐐷")), None)
                .unwrap(),
            id
        );
    }
    let mut before = None;
    let mut all = Vec::new();
    loop {
        let summary = store.summary(before).unwrap();
        assert_eq!(summary.total_count, 70);
        assert!(summary.content.len() <= 64 * 1024);
        let ids: Vec<usize> = summary
            .content
            .lines()
            .map(|line| line.split_whitespace().next().unwrap().parse().unwrap())
            .collect();
        assert!(ids.len() <= 32);
        assert!(ids.windows(2).all(|w| w[0] + 1 == w[1]));
        if all.is_empty() {
            assert_eq!(ids[0], 38);
        }
        all.extend(ids);
        before = summary.next_before;
        if before.is_none() {
            break;
        }
    }
    all.sort_unstable();
    assert_eq!(all, (0..70).collect::<Vec<_>>());
    assert!(store.summary(Some(0)).unwrap().content.is_empty());
    drop(store);
    let store = TranscriptStore::new(&workspace.0, "summary").unwrap();
    assert_eq!(store.total_count().unwrap(), 70);
    assert_eq!(reconstruct(&store, 69), "entry 69 𐐷");
}

#[test]
fn resume_rehomes_record_ids_without_cross_session_delta_writes() {
    let workspace = Workspace::new();
    let mut app = workspace.app("session_a");
    let a = "A世界𐐷".repeat(30_000);
    app.add_assistant_message(&a);
    app.input_buffer = "old command exact 𐐷".to_string();
    app.submit_input();
    assert!(app.resume_session("session_b"));
    assert!(app.command_history.is_empty());
    assert!(app
        .transcript_store
        .as_ref()
        .unwrap()
        .path()
        .ends_with("session_b"));
    let b = "B世界𐐷".repeat(30_000);
    app.add_assistant_message(&b);
    app.append_assistant_delta("new session delta");
    assert_eq!(
        reconstruct(app.transcript_store.as_ref().unwrap(), 0),
        format!("{b}new session delta")
    );
    let old = TranscriptStore::new(&workspace.0, "session_a").unwrap();
    assert_eq!(reconstruct(&old, 0), a);
    assert_eq!(reconstruct(&old, 1), "old command exact 𐐷");
    assert!(!app.resume_session("../escape"));
    assert_eq!(app.session_id, "session_b");
}

#[test]
fn command_history_is_capacity_bounded_and_only_exact_commands_are_repeatable() {
    let workspace = Workspace::new();
    let mut app = workspace.app("commands");
    let mut first = String::new();
    for i in 0..350 {
        let command = format!("command-{i} {}", "é𐐷".repeat(200));
        if i == 0 {
            first = command.clone();
        }
        app.input_buffer = command.clone();
        assert_eq!(app.submit_input(), Some(command));
        let bytes = app.command_history.capacity() * std::mem::size_of::<String>()
            + app
                .command_history
                .iter()
                .map(String::capacity)
                .sum::<usize>();
        assert!(bytes <= COMMAND_HISTORY_BUDGET);
    }
    assert_eq!(
        reconstruct(app.transcript_store.as_ref().unwrap(), 0),
        first
    );
    assert!(app
        .command_history
        .iter()
        .all(|command| !command.contains("/transcript")));
    app.history_up();
    assert!(app.input_buffer.starts_with("command-349 "));
    let giant = "repeat exactly 𐐷".repeat(30_000);
    app.input_buffer = giant.clone();
    assert_eq!(app.submit_input(), Some(giant.clone()));
    assert!(app.command_history.is_empty());
    let store = app.transcript_store.as_ref().unwrap();
    assert_eq!(reconstruct(store, store.total_count().unwrap() - 1), giant);
    assert!(app.resident_transcript_bytes() <= TRANSCRIPT_BUDGET);
}

#[test]
fn command_history_archival_failure_retains_exact_repeatable_commands() {
    let workspace = Workspace::new();
    let mut app = workspace.app("command_failure");
    std::fs::create_dir(app.transcript_store.as_ref().unwrap().path().join("0.txt")).unwrap();
    let command = "exact command 𐐷".repeat(30_000);
    app.input_buffer = command.clone();
    app.submit_input();
    assert_eq!(app.command_history, vec![command.clone()]);
    assert!(app.transcript_error.is_some());
    app.history_up();
    assert_eq!(app.input_buffer, command);
}

#[test]
fn diff_preview_is_explicitly_partial_and_full_source_remains_exact() {
    let workspace = Workspace::new();
    let mut app = workspace.app("diff");
    let raw = format!(
        "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1,50000 @@\n-old\n{}",
        "+新𐐷\n".repeat(50_000)
    );
    app.load_diff(&raw);
    let id = app.diff_transcript_id.unwrap();
    assert_eq!(reconstruct(app.transcript_store.as_ref().unwrap(), id), raw);
    assert!(app.raw_diff.as_ref().unwrap().len() <= MESSAGE_DISPLAY_BYTES);
    assert!(app
        .raw_diff
        .as_ref()
        .unwrap()
        .contains("PARTIAL DIFF PREVIEW"));
    assert!(app.active_diff.as_ref().unwrap().files[0]
        .path()
        .contains("/transcript"));
    assert!(
        app.active_diff
            .as_ref()
            .unwrap()
            .files
            .iter()
            .flat_map(|f| &f.hunks)
            .map(|h| h.lines.len())
            .sum::<usize>()
            <= 512
    );
    assert!(app.resident_transcript_bytes() <= TRANSCRIPT_BUDGET);
    app.clear_diff();
    assert!(app.diff_transcript_id.is_none());

    let mut app = workspace.app("diff_failure");
    std::fs::create_dir(app.transcript_store.as_ref().unwrap().path().join("0.txt")).unwrap();
    app.load_diff(&raw);
    assert_eq!(app.raw_diff.as_deref(), Some(raw.as_str()));
    assert!(app.transcript_error.is_some());
    assert!(app.active_diff.is_none());
}

#[test]
fn active_turn_and_running_tools_survive_prefix_pressure() {
    let workspace = Workspace::new();
    let mut app = workspace.app("active");
    for _ in 0..100 {
        app.add_system_message(&"old".repeat(1500));
    }
    app.add_user_message("active prompt");
    app.is_busy = true;
    app.add_tool_call("running", "terminal", "args", None, None);
    for _ in 0..600 {
        app.add_assistant_message(&"active".repeat(700));
        assert!(app.resident_transcript_bytes() <= TRANSCRIPT_BUDGET);
    }
    assert!(app
        .dialogue_items
        .iter()
        .any(|item| matches!(item, DialogueItem::Message(msg) if msg.content == "active prompt")));
    assert!(app.dialogue_items.iter().any(|item| matches!(item, DialogueItem::ToolCall(tool) if tool.id == "running" && tool.exit_status.as_deref() == Some("RUNNING"))));
    app.is_busy = false;
    app.close_running_tool_calls();
    app.enforce_transcript_budget();
    assert!(app.resident_transcript_bytes() <= TRANSCRIPT_BUDGET);
}

#[test]
fn measured_prefix_rows_rebase_manual_visual_scroll_and_clear_indices() {
    let workspace = Workspace::new();
    let mut app = workspace.app("scroll");
    app.follow_dialogue = false;
    app.dialogue_scroll = 1000;
    for _ in 0..480 {
        app.add_assistant_message(&"x".repeat(4096));
    }
    let area = ratatui::layout::Rect::new(0, 0, 80, 40);
    let mut buffer = ratatui::buffer::Buffer::empty(area);
    hex_tui::ui::transcript::render(
        &app,
        &hex_tui::ui::style::HexTheme::truecolor(),
        &hex_tui::ui::animation::AnimationState::new(),
        area,
        &mut buffer,
    );
    let before = app.dialogue_items.len();
    let prefix_rows: Vec<_> = (0..=before)
        .map(|count| app.transcript_layout.borrow().discarded_rows(count))
        .collect();
    let offset = app.dialogue_view_offset.get();
    let huge = "large𐐷".repeat(10_000);
    app.add_assistant_message(&huge);
    assert!(app.dialogue_items.len() < before + 1);
    let drained = before + 1 - app.dialogue_items.len();
    assert_eq!(
        app.dialogue_scroll,
        offset.saturating_sub(prefix_rows[drained])
    );
    app.clear_transcript().unwrap();
    app.add_assistant_message(&huge);
    app.append_assistant_delta("fresh delta");
    let store = app.transcript_store.as_ref().unwrap();
    assert_eq!(
        reconstruct(store, store.total_count().unwrap() - 1),
        format!("{huge}fresh delta")
    );
}

#[test]
fn storage_is_opt_in_and_rejects_traversal_or_symlinked_sessions() {
    let workspace = Workspace::new();
    let mut app = App::with_workspace("offline", "offline", "none", Some(&workspace.0));
    let text = "é".repeat(100_000);
    app.add_assistant_message(&text);
    app.enforce_transcript_budget();
    assert_eq!(displayed(&app), text);
    assert!(app.transcript_store.is_none());
    assert!(!workspace.0.join(".hex/data/tui_transcripts").exists());
    for id in ["", "../bad", "bad/name", "世界", ".", ".."] {
        assert!(TranscriptStore::new(&workspace.0, id).is_err());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{symlink, PermissionsExt};
        let store = TranscriptStore::new(&workspace.0, "private").unwrap();
        assert_eq!(
            std::fs::metadata(store.path())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        symlink(store.path(), store.path().parent().unwrap().join("linked")).unwrap();
        assert!(TranscriptStore::new(&workspace.0, "linked").is_err());
        drop(store);
        let mut store = TranscriptStore::new(&workspace.0, "private").unwrap();
        let id = store.append(&message("private text"), None).unwrap();
        assert_eq!(
            std::fs::metadata(store.path().join(format!("{id}.txt")))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}

#[test]
fn a_published_record_with_a_lagging_count_is_recovered_without_overwrite() {
    let workspace = Workspace::new();
    let mut store = TranscriptStore::new(&workspace.0, "recover").unwrap();
    assert_eq!(
        store
            .append(&message("original exact source 𐐷"), None)
            .unwrap(),
        0
    );
    let count = store.path().join("count");
    drop(store);
    // Simulate the crash window between durable metadata and the count update.
    std::fs::write(count, 0u64.to_le_bytes()).unwrap();
    let mut store = TranscriptStore::new(&workspace.0, "recover").unwrap();
    assert_eq!(store.append(&message("next source"), None).unwrap(), 1);
    assert_eq!(store.total_count().unwrap(), 2);
    assert_eq!(reconstruct(&store, 0), "original exact source 𐐷");
    assert_eq!(reconstruct(&store, 1), "next source");
}

#[test]
#[ignore]
fn transcript_process_writer() {
    let directory = std::env::var_os("HEX_TEST_TRANSCRIPT_DIRECTORY").unwrap();
    let mut store = TranscriptStore::new(Path::new(&directory), "concurrent").unwrap();
    for number in 0..100 {
        store
            .append(
                &message(&format!("process={} row={number} 𐐷", std::process::id())),
                None,
            )
            .unwrap();
    }
}

#[test]
fn native_processes_lock_count_without_overwriting_records() {
    let workspace = Workspace::new();
    let exe = std::env::current_exe().unwrap();
    let launch = || {
        std::process::Command::new(&exe)
            .args(["--exact", "transcript_process_writer", "--ignored"])
            .env("HEX_TEST_TRANSCRIPT_DIRECTORY", &workspace.0)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .unwrap()
    };
    let first = launch();
    let second = launch();
    for child in [first, second] {
        let output = child.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    let store = TranscriptStore::new(&workspace.0, "concurrent").unwrap();
    assert_eq!(store.total_count().unwrap(), 200);
    let mut sources = std::collections::HashSet::new();
    for id in 0..200 {
        assert!(sources.insert(reconstruct(&store, id)));
    }
    assert_eq!(sources.len(), 200);
}

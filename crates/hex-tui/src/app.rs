//! Application state and business logic for HEX native terminal cockpit.

use hex_protocol::diff::UnifiedDiff;
use hex_protocol::models::Role;
use std::time::Instant;
pub use crate::history::TranscriptStore;

/// Focused viewport pane in the TUI.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Focus {
    Input,
    Resume,
    Dialogue,
    Diff,
    Approval,
}

/// Top-level screen view state in the HEX Cockpit matching mockup screens.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScreenState {
    Start,
    Session,
}

/// Execution status of a GSD plan step.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StepStatus {
    Completed,
    Active,
    Pending,
}

/// A plan step rendered in the right rail.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanStep {
    pub title: String,
    pub status: StepStatus,
}

/// A modified/changed file tracking git delta.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChangedFile {
    pub path: String,
    pub additions: usize,
    pub deletions: usize,
}

/// Telemetry for a subagent in the right rail.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubagentTelemetry {
    pub name: String,
    pub status: String,
    pub detail: String,
    pub is_running: bool,
    pub has_failed: bool,
}

/// Historical or resumable session entry on the Start screen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResumeSession {
    pub title: String,
    pub relative_time: String,
    pub session_id: String,
}

/// Active drill-down inspection state for a subagent modal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubagentModalState {
    pub name: String,
    pub prompt: String,
    pub is_expanded: bool,
    pub activity_lines: Vec<String>,
    pub elapsed_seconds: u64,
    /// Lifecycle word from the server (`running`, `completed`, `failed`, …).
    pub status: String,
    pub is_running: bool,
    /// Rows scrolled back from the newest activity line.
    pub activity_scroll: usize,
}

/// Diagnostic failure detail for failed commands and tests.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiagnosticFailure {
    pub command: String,
    pub exit_code: i32,
    pub duration_str: String,
    pub test_file: String,
    pub test_name: String,
    pub expected: String,
    pub received: String,
    pub stack_trace: Vec<String>,
    pub hidden_frames_count: usize,
    pub diagnosis: String,
    pub next_investigation: String,
    /// Raw failure output shown when no assertion structure could be parsed.
    pub detail_lines: Vec<String>,
}

impl Default for DiagnosticFailure {
    fn default() -> Self {
        Self {
            command: String::new(),
            exit_code: 1,
            duration_str: "0.0s".to_string(),
            test_file: String::new(),
            test_name: String::new(),
            expected: String::new(),
            received: String::new(),
            stack_trace: Vec::new(),
            hidden_frames_count: 0,
            diagnosis: String::new(),
            next_investigation: String::new(),
            detail_lines: Vec::new(),
        }
    }
}

impl DiagnosticFailure {
    /// Build the card from a tool's error payload. Every field comes from the
    /// command, its arguments or its output — nothing is invented.
    pub fn from_tool_error(tool: &str, args: &str, payload: &str, duration_ms: u64) -> Self {
        let value: Option<serde_json::Value> = serde_json::from_str(payload).ok();
        let field = |key: &str| -> String {
            value
                .as_ref()
                .and_then(|v| v.get(key))
                .map(|v| match v {
                    serde_json::Value::String(s) => s.clone(),
                    serde_json::Value::Null => String::new(),
                    other => other.to_string(),
                })
                .unwrap_or_default()
        };

        let exit_code = value
            .as_ref()
            .and_then(|v| v.get("exit_code"))
            .and_then(|v| v.as_i64())
            .unwrap_or(1) as i32;

        let command = command_text(args).unwrap_or_else(|| {
            if args.trim().is_empty() { tool.to_string() } else { args.trim().to_string() }
        });

        // A terminal failure puts the output in stdout/stderr while `error` may
        // hold only a short summary; read all of them so the detail is not lost.
        let combined = ["stdout", "stderr", "error"]
            .iter()
            .map(|k| field(k))
            .filter(|s| !s.trim().is_empty())
            .collect::<Vec<_>>()
            .join("\n");
        let error_text = if combined.trim().is_empty() {
            payload.trim().to_string()
        } else {
            combined
        };

        let mut diag = Self {
            command,
            exit_code,
            duration_str: format!("{:.1}s", duration_ms as f64 / 1000.0),
            ..Self::default()
        };

        let mut detail: Vec<String> = Vec::new();
        for raw in error_text.lines() {
            let line = raw.trim_end();
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let lower = trimmed.to_lowercase();
            if let Some(rest) = strip_prefix_ci(trimmed, "expected:") {
                diag.expected = rest.trim().to_string();
                continue;
            }
            if let Some(rest) = strip_prefix_ci(trimmed, "received:") {
                diag.received = rest.trim().to_string();
                continue;
            }
            // pytest: `E   assert ['b.ts'] == ['a.ts']`. Tests are written
            // `assert actual == expected`, so the left operand is what arrived.
            if let Some(rest) = trimmed.strip_prefix("E ").map(str::trim) {
                if let Some(assertion) = rest.strip_prefix("assert ") {
                    if let Some((lhs, rhs)) = assertion.split_once(" == ") {
                        diag.expected = rhs.trim().to_string();
                        diag.received = lhs.trim().to_string();
                        continue;
                    }
                }
            }
            if trimmed.starts_with("at ") || trimmed.starts_with("File \"") {
                diag.stack_trace.push(trimmed.to_string());
                continue;
            }
            // pytest writes `FAILED path::name` in its summary and
            // `path::name FAILED` per test under -q.
            let locus = trimmed
                .strip_prefix("FAILED ")
                .map(|rest| rest.split(" - ").next().unwrap_or(rest).trim())
                .or_else(|| trimmed.strip_suffix(" FAILED").map(str::trim));
            if let Some(locus) = locus {
                if let Some((file, name)) = locus.split_once("::") {
                    diag.test_file = file.trim().to_string();
                    diag.test_name = name.trim().to_string();
                } else {
                    diag.test_file = locus.to_string();
                }
                continue;
            }
            if lower.starts_with("error:") || lower.starts_with("failed:") {
                if diag.diagnosis.is_empty() {
                    diag.diagnosis = trimmed.to_string();
                }
            }
            if detail.len() < 4 {
                detail.push(trimmed.to_string());
            }
        }

        // A stack frame names the file the failure came from.
        if diag.test_file.is_empty() {
            if let Some(frame) = diag.stack_trace.first() {
                if let Some(path) = frame_path(frame) {
                    diag.test_file = path;
                }
            }
        }
        diag.hidden_frames_count = diag.stack_trace.len().saturating_sub(1);
        if diag.diagnosis.is_empty() {
            diag.diagnosis = field("error");
        }
        if diag.diagnosis.is_empty() {
            diag.diagnosis = detail.first().cloned().unwrap_or_default();
        }
        diag.detail_lines = detail;
        diag
    }
}

/// First non-empty line of a summary, clamped for the rail's detail row.
fn first_line(text: &str, max: usize) -> String {
    let line = text.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or("");
    clamp_chars(line, max)
}

fn clamp_chars(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_string();
    }
    let tail: String = text.chars().take(max.saturating_sub(1)).collect();
    format!("{tail}…")
}

/// The drill-down transcript: only fields the SubagentManager actually recorded.
fn subagent_activity(a: &hex_protocol::models::SubagentInfo) -> Vec<String> {
    let mut lines = vec![format!("spawned  role={} depth={}", a.role, a.depth)];
    if a.tool_calls_count > 0 {
        lines.push(format!("tools    {} calls", a.tool_calls_count));
    }
    if let Some(r) = &a.result {
        for finding in &r.findings {
            if !finding.trim().is_empty() {
                lines.push(finding.trim().to_string());
            }
        }
        if !r.diff_summary.trim().is_empty() {
            lines.push(format!("diff     {}", r.diff_summary.trim()));
        }
        for artifact in &r.artifacts {
            lines.push(format!("artifact {artifact}"));
        }
        if !r.summary.trim().is_empty() {
            lines.push(r.summary.trim().to_string());
        }
        if let Some(err) = r.error.as_ref().filter(|e| !e.trim().is_empty()) {
            lines.push(format!("error    {}", err.trim()));
        }
    }
    lines.push(format!("status   {}", a.status));
    lines
}

/// What the transcript prints for a tool's arguments: the command or path the
/// call names, rather than the raw JSON envelope the server sends.
pub fn tool_args_label(args: &str) -> String {
    if let Some(cmd) = command_text(args) {
        return cmd;
    }
    if let Ok(value) = serde_json::from_str::<serde_json::Value>(args) {
        for key in ["path", "file_path", "pattern", "query", "symbol", "url"] {
            if let Some(text) = value.get(key).and_then(|v| v.as_str()) {
                if !text.is_empty() {
                    return text.to_string();
                }
            }
        }
    }
    args.to_string()
}

/// The command a tool invocation was asked to run, if its arguments name one.
fn command_text(args: &str) -> Option<String> {
    let value: serde_json::Value = serde_json::from_str(args).ok()?;
    for key in ["command", "cmd", "command_line", "script"] {
        if let Some(cmd) = value.get(key) {
            let text = match cmd {
                serde_json::Value::String(s) => s.clone(),
                serde_json::Value::Array(items) => items
                    .iter()
                    .map(|i| i.as_str().unwrap_or_default())
                    .collect::<Vec<_>>()
                    .join(" "),
                _ => continue,
            };
            if !text.trim().is_empty() {
                return Some(text);
            }
        }
    }
    None
}

fn strip_prefix_ci<'a>(text: &'a str, prefix: &str) -> Option<&'a str> {
    if text.len() >= prefix.len() && text[..prefix.len()].eq_ignore_ascii_case(prefix) {
        Some(&text[prefix.len()..])
    } else {
        None
    }
}

/// Pull `path:line:col` out of a JS `at fn (path:1:2)` or Python `File "path", line N` frame.
fn frame_path(frame: &str) -> Option<String> {
    if let Some(start) = frame.find("File \"") {
        let rest = &frame[start + 6..];
        let end = rest.find('"')?;
        return Some(rest[..end].to_string());
    }
    let open = frame.rfind('(')?;
    let close = frame.rfind(')')?;
    if close <= open {
        return None;
    }
    let locus = &frame[open + 1..close];
    let path = locus.split(':').next()?.trim();
    if path.is_empty() { None } else { Some(path.to_string()) }
}

/// Item in the command & navigation palette overlay.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaletteItem {
    pub category: String,
    pub title: String,
    pub command: String,
}

/// A palette entry plus the character positions the query matched.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaletteMatch {
    pub item_index: usize,
    pub title_matched: Vec<usize>,
}

/// A regular conversational message item in dialogue.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct MessageItem {
    pub role: Role,
    pub content: String,
    pub timestamp: String,
}

/// An expandable/collapsible tool invocation block.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ToolCallItem {
    pub id: String,
    pub tool: String,
    pub args: String,
    pub result: Option<String>,
    pub duration_ms: Option<u64>,
    pub exit_status: Option<String>,
    pub collapsed: bool,
    #[serde(skip)]
    pub started_at: Option<std::time::Instant>,
}

/// Dialogue viewport item (either a chat message or a tool call).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum DialogueItem {
    Message(MessageItem),
    ToolCall(ToolCallItem),
}

/// Approval request requiring operator intervention.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApprovalRequest {
    pub id: String,
    pub risk_level: String,
    pub tool_name: String,
    pub parameters: String,
    pub reason: String,
    pub blast_radius: String,
    pub cwd: String,
}

/// Slash command metadata for autocompletion popup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlashCommand {
    pub name: String,
    pub description: String,
    pub usage: String,
}

/// TUI slash commands come from the shared command catalog (F06): the same
/// JSON that drives Python console help/completion and ACP dispatch. Only
/// entries marked supported on the `tui` surface are offered here.
pub fn available_slash_commands() -> &'static [SlashCommand] {
    static COMMANDS: std::sync::OnceLock<Vec<SlashCommand>> = std::sync::OnceLock::new();
    COMMANDS.get_or_init(|| {
        #[derive(serde::Deserialize)]
        struct CatalogEntry {
            name: String,
            usage: String,
            description: String,
            #[serde(default)]
            surfaces: std::collections::HashMap<String, String>,
        }
        #[derive(serde::Deserialize)]
        struct Catalog {
            commands: Vec<CatalogEntry>,
        }
        let catalog: Catalog = serde_json::from_str(include_str!(
            "../../../hex_cli/command_catalog.json"
        ))
        .expect("embedded command catalog must parse");
        catalog
            .commands
            .into_iter()
            .filter(|e| e.surfaces.get("tui").map(|s| s == "supported").unwrap_or(false))
            .map(|e| SlashCommand {
                name: e.name,
                description: e.description,
                usage: e.usage,
            })
            .collect()
    })
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ToolResultRef {
    pub task_id: String,
    pub call_id: String,
    pub local: bool,
}

pub fn tool_preview(text: &str, limit: usize) -> String {
    let end = text.char_indices().nth(limit).map(|(i, _)| i);
    match end {
        Some(end) => format!("{}… [full result: /tool-result]", &text[..end]),
        None => text.to_string(),
    }
}

pub fn load_tool_result_page(
    workspace: &std::path::Path,
    task_id: &str,
    call_id: &str,
    offset: usize,
) -> Result<hex_protocol::models::ToolResultPage, String> {
    if [task_id, call_id].iter().any(|id| {
        id.is_empty() || !id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
    }) {
        return Err("Invalid tool result identifier".to_string());
    }
    if offset >= i64::MAX as usize {
        return Err("Invalid tool result offset".to_string());
    }
    let db = workspace.join(".hex/data/ledger.db");
    if !db.is_file() {
        return Err("No stored ledger in this workspace".to_string());
    }
    let sql = format!(
        "SELECT substr(result_json, {}, 4096) AS content, length(result_json) AS totalChars FROM tool_calls WHERE task_id = '{task_id}' AND call_id = '{call_id}' AND result_json IS NOT NULL;",
        offset.saturating_add(1)
    );
    let output = get_sqlite_command()
        .args([db.to_str().ok_or("Invalid workspace path")?, "-json", &sql])
        .stdin(std::process::Stdio::null())
        .output()
        .map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Err("Could not read stored tool result".to_string());
    }
    let rows: Vec<serde_json::Value> = serde_json::from_slice(&output.stdout).map_err(|e| e.to_string())?;
    let row = rows.first().ok_or("Tool result not found in this task")?;
    let content = row["content"].as_str().unwrap_or("").to_string();
    let total_chars = row["totalChars"].as_u64().unwrap_or(0) as usize;
    let next = offset.saturating_add(content.chars().count());
    Ok(hex_protocol::models::ToolResultPage {
        content,
        total_chars,
        next_offset: (next < total_chars).then_some(next),
    })
}

/// Top-level application state.
pub struct App {
    // Session metadata
    pub session_id: String,
    pub project_id: String,
    pub model_name: String,
    pub agent_name: String,
    pub provider_name: String,
    pub cow_mode: String,
    pub git_branch: String,
    pub git_sha: String,
    pub workspace_path: String,
    pub tools_count: usize,
    pub skills_count: usize,
    pub splash_text: Option<String>,
    pub fencing_token: u64,
    pub connection_status: String,
    pub skin_name: String,

    // Navigation and layout
    pub screen_state: ScreenState,
    pub mode: String,
    pub focus: Focus,
    pub show_help: bool,
    pub show_diff: bool,
    pub side_by_side: bool,
    pub should_quit: bool,
    pub is_busy: bool,
    pub busy_started_at: Option<std::time::Instant>,
    pub status_message: String,
    pub active_turn_has_streamed: bool,
    pub cancel_requested: bool,

    // Dialogue items and scrolling
    pub dialogue_items: Vec<DialogueItem>,
    pub tool_result_refs: std::collections::HashMap<String, ToolResultRef>,
    pub transcript_store: Option<TranscriptStore>,
    pub transcript_error: Option<String>,
    pub(crate) transcript_ids: Vec<(usize, usize)>,
    pub(crate) transcript_history_stored: bool,
    /// Full source command for a deliberately partial visible diff.
    pub diff_transcript_id: Option<usize>,
    pub dialogue_scroll: usize,
    pub dialogue_view_offset: std::cell::Cell<usize>,
    pub transcript_layout: std::cell::RefCell<crate::ui::transcript::TranscriptLayout>,
    pub follow_dialogue: bool,
    /// (total transcript rows, viewport rows) as last measured by the renderer.
    /// Wrapping depends on the terminal width, so only the renderer knows the
    /// real bottom offset.
    pub dialogue_metrics: std::cell::Cell<(usize, usize)>,
    /// (blocks that have finished rising, when the current batch arrived).
    /// `None` once the batch lands, so the next arrival starts a fresh rise.
    pub dialogue_entrance: std::cell::Cell<(usize, Option<std::time::Instant>)>,

    // Diff inspection
    pub active_diff: Option<UnifiedDiff>,
    pub raw_diff: Option<String>,
    pub diff_scroll: usize,

    // Right Rail & Telemetry
    pub sidebar: crate::sidebar::SidebarState,
    pub plan_steps: Vec<PlanStep>,
    pub changed_files: Vec<ChangedFile>,
    pub subagents: Vec<SubagentTelemetry>,
    pub resume_sessions: Vec<ResumeSession>,
    pub selected_resume_index: usize,
    pub show_all_resume: bool,
    pub resume_window_offset: usize,
    pub pending_delete_index: Option<usize>,
    pub tokens_used: usize,
    pub max_tokens: usize,
    pub modified_files_count: usize,
    pub indexed_files_count: usize,
    pub current_thought: Option<String>,
    pub thinking_status: Option<String>,

    // Overlay modals (Palette, Subagent, Permission)
    pub show_palette: bool,
    pub palette_query: String,
    pub selected_palette_index: usize,
    pub palette_items: Vec<PaletteItem>,
    pub active_subagent_modal: Option<SubagentModalState>,
    pub selected_permission_index: usize,

    // Command input & history
    pub input_buffer: String,
    pub cursor_pos: usize,
    pub command_history: Vec<String>,
    pub history_index: Option<usize>,

    // Suggestion rotation (advances per submitted prompt)
    pub suggestion_index: usize,

    // Approval interrupt modal
    pub active_approval: Option<ApprovalRequest>,

    // Inline diagnostic failure card
    pub active_diagnostic: Option<DiagnosticFailure>,

    // One shared clock powers every animated surface.  Widgets never create
    // timers of their own, which keeps redraw cost predictable.
    pub animation_frame: u64,
    pub session_started: Instant,
    pub screen_transition_started: Option<Instant>,

    // Mouse hover tracking and animated transitions
    pub mouse_pos: Option<(u16, u16)>,
    pub hovered_resume_row: Option<usize>,
    pub hovered_delete_row: Option<usize>,
    pub hovered_input_box: bool,
    pub hovered_shortcut_hint: Option<usize>,
    pub row_hover_levels: Vec<f32>,
    pub delete_hover_levels: Vec<f32>,
    pub input_hover_level: f32,
    pub toggle_hover_level: f32,
    pub shortcut_hover_levels: [f32; 4],
}

pub fn detect_git_info(dir: &std::path::Path) -> (String, String) {
    let branch = std::process::Command::new("git")
        .args(["rev-parse", "--abbrev-ref", "HEAD"])
        .current_dir(dir)
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "main".to_string());

    let sha = std::process::Command::new("git")
        .args(["rev-parse", "--short=7", "HEAD"])
        .current_dir(dir)
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "clean".to_string());

    (branch, sha)
}

/// Detect whether the current terminal has a light background via `COLORFGBG`.
/// If the background color index >= 7 (e.g. "0;15"), it is considered a light terminal.
pub fn is_light_terminal() -> bool {
    if let Ok(val) = std::env::var("COLORFGBG") {
        let parts: Vec<&str> = val.split(';').collect();
        let bg_part = if parts.len() >= 2 {
            parts.last()
        } else {
            None
        };
        if let Some(bg_str) = bg_part {
            if let Ok(bg_num) = bg_str.trim().parse::<u32>() {
                return bg_num >= 7;
            }
        }
    }
    false
}

/// Default skin name based on terminal background detection.
pub fn detect_default_skin() -> String {
    if is_light_terminal() {
        "light".to_string()
    } else {
        "sapphire".to_string()
    }
}

pub fn detect_git_status_counts(dir: &std::path::Path) -> (usize, usize) {
    let modified = std::process::Command::new("git")
        .args(["status", "--porcelain"])
        .current_dir(dir)
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.lines().filter(|l| !l.trim().is_empty()).count())
        .unwrap_or(0);

    let indexed = std::process::Command::new("git")
        .args(["ls-files"])
        .current_dir(dir)
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.lines().filter(|l| !l.trim().is_empty()).count())
        .unwrap_or(0);

    (modified, indexed)
}

pub fn detect_changed_files_from_git(dir: &std::path::Path) -> Vec<ChangedFile> {
    let output = std::process::Command::new("git")
        .args(["diff", "--numstat", "HEAD"])
        .current_dir(dir)
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .or_else(|| {
            std::process::Command::new("git")
                .args(["diff", "--numstat"])
                .current_dir(dir)
                .output()
                .ok()
                .and_then(|o| String::from_utf8(o.stdout).ok())
        })
        .unwrap_or_default();

    let mut files = Vec::new();
    for line in output.lines() {
        let parts: Vec<&str> = line.split('\t').collect();
        if parts.len() == 3 {
            let additions = parts[0].parse::<usize>().unwrap_or(0);
            let deletions = parts[1].parse::<usize>().unwrap_or(0);
            let path = parts[2].to_string();
            files.push(ChangedFile {
                path,
                additions,
                deletions,
            });
        }
    }
    files
}

pub fn extract_plan_steps_from_text(text: &str) -> Vec<PlanStep> {
    let mut steps = Vec::new();
    for line in text.lines() {
        let trimmed = line.trim();
        if let Some(rest) = trimmed.strip_prefix("- [x] ")
            .or_else(|| trimmed.strip_prefix("* [x] "))
            .or_else(|| trimmed.strip_prefix("[x] "))
        {
            steps.push(PlanStep {
                title: rest.trim().to_string(),
                status: StepStatus::Completed,
            });
        } else if let Some(rest) = trimmed.strip_prefix("- [>] ")
            .or_else(|| trimmed.strip_prefix("* [>] "))
            .or_else(|| trimmed.strip_prefix("[>] "))
        {
            steps.push(PlanStep {
                title: rest.trim().to_string(),
                status: StepStatus::Active,
            });
        } else if let Some(rest) = trimmed.strip_prefix("- [ ] ")
            .or_else(|| trimmed.strip_prefix("* [ ] "))
            .or_else(|| trimmed.strip_prefix("[ ] "))
        {
            steps.push(PlanStep {
                title: rest.trim().to_string(),
                status: StepStatus::Pending,
            });
        }
    }
    steps
}

pub fn detect_gsd_plan_steps(dir: &std::path::Path) -> Vec<PlanStep> {
    let phases_dir = dir.join(".gsd").join("phases");
    if !phases_dir.is_dir() {
        return Vec::new();
    }

    let mut entries: Vec<_> = match std::fs::read_dir(&phases_dir) {
        Ok(read_dir) => read_dir.filter_map(|e| e.ok()).collect(),
        Err(_) => return Vec::new(),
    };
    entries.sort_by_key(|e| e.file_name());

    for entry in entries.into_iter().rev() {
        if entry.path().is_dir() {
            if let Ok(plan_dir) = std::fs::read_dir(entry.path()) {
                let mut plan_files: Vec<_> = plan_dir
                    .filter_map(|e| e.ok())
                    .filter(|e| {
                        let name = e.file_name().to_string_lossy().to_string();
                        name.ends_with("-PLAN.md") || name == "PLAN.md"
                    })
                    .collect();
                plan_files.sort_by_key(|e| e.file_name());

                if let Some(latest_plan) = plan_files.last() {
                    if let Ok(content) = std::fs::read_to_string(latest_plan.path()) {
                        let mut steps = Vec::new();
                        for line in content.lines() {
                            let trimmed = line.trim();
                            if trimmed.starts_with("<task") && trimmed.contains("title=\"") {
                                if let Some(start) = trimmed.find("title=\"") {
                                    let rest = &trimmed[start + 7..];
                                    if let Some(end) = rest.find('"') {
                                        let title = rest[..end].to_string();
                                        steps.push(PlanStep {
                                            title,
                                            status: StepStatus::Pending,
                                        });
                                    }
                                }
                            }
                        }
                        if !steps.is_empty() {
                            if let Some(first) = steps.first_mut() {
                                first.status = StepStatus::Active;
                            }
                            return steps;
                        }
                    }
                }
            }
        }
    }

    Vec::new()
}

pub fn fmt_relative_time(created_at_str: &str) -> String {
    if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(created_at_str) {
        let now = chrono::Utc::now();
        let diff = now.signed_duration_since(dt.with_timezone(&chrono::Utc));
        if diff.num_minutes() < 1 {
            "just now".to_string()
        } else if diff.num_minutes() < 60 {
            format!("{}m", diff.num_minutes())
        } else if diff.num_hours() < 24 {
            format!("{}h", diff.num_hours())
        } else if diff.num_days() == 1 {
            "yesterday".to_string()
        } else {
            format!("{}d", diff.num_days())
        }
    } else {
        "recently".to_string()
    }
}

pub fn get_sqlite_command() -> std::process::Command {
    // Prefer native system binaries with complete FTS5 extension support over stripped SDK binaries
    for candidate in &["/usr/bin/sqlite3", "/opt/homebrew/bin/sqlite3", "/usr/local/bin/sqlite3"] {
        if std::path::Path::new(candidate).exists() {
            return std::process::Command::new(candidate);
        }
    }
    std::process::Command::new("sqlite3")
}

pub fn load_real_resume_sessions(dir: &std::path::Path) -> Vec<ResumeSession> {
    let db_path = dir.join(".hex").join("data").join("ledger.db");
    if !db_path.exists() {
        return Vec::new();
    }

    let mut cmd = get_sqlite_command();
    let output = cmd
        .args([
            db_path.to_str().unwrap_or(".hex/data/ledger.db"),
            "-separator",
            "\u{1f}",
            "SELECT task_id, replace(replace(replace(goal, char(10), ' '), char(13), ' '), char(31), ' '), created_at FROM tasks ORDER BY created_at DESC;",
        ])
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .unwrap_or_default();

    let mut sessions = Vec::new();
    for line in output.lines() {
        let parts: Vec<&str> = line.split('\u{1f}').collect();
        if parts.len() == 3 {
            let session_id = parts[0].trim().to_string();
            let title = parts[1].trim().to_string();
            let created_at = parts[2].trim();
            let relative_time = fmt_relative_time(created_at);
            if !title.is_empty() {
                sessions.push(ResumeSession {
                    title,
                    relative_time,
                    session_id,
                });
            }
        }
    }
    sessions
}

pub(crate) fn ellipsize(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_string();
    }
    let kept: String = text.chars().take(max.saturating_sub(1)).collect();
    format!("{kept}…")
}

pub fn delete_session_from_db(dir: &std::path::Path, task_id: &str) -> bool {
    let db_path = dir.join(".hex").join("data").join("ledger.db");
    if !db_path.exists() {
        return false;
    }

    let clean_id = task_id.replace('\'', "''");
    let path_arg = db_path.to_str().unwrap_or(".hex/data/ledger.db");
    let run_sql = |sql: &str| -> String {
        let mut cmd = get_sqlite_command();
        cmd.args([path_arg, sql])
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .output()
            .ok()
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .unwrap_or_default()
            .trim()
            .to_string()
    };

    let count_sql = format!("SELECT COUNT(*) FROM tasks WHERE task_id = '{clean_id}';");
    // Nothing to delete is not a successful deletion.
    if run_sql(&count_sql) != "1" {
        return false;
    }

    // A statement naming an absent table aborts the whole sqlite3 batch, so only
    // reference tables this ledger actually has (older ledgers lack newer ones).
    let present = run_sql("SELECT name FROM sqlite_master WHERE type IN ('table', 'view');");
    let has = |name: &str| present.lines().any(|l| l.trim() == name);

    let scoped = [
        "dialogue_turns",
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
        "task_search_fts",
        "tasks",
    ];

    let mut batch = String::from("BEGIN IMMEDIATE; ");
    for table in scoped.iter().filter(|t| has(t)) {
        batch.push_str(&format!(
            "DELETE FROM {table} WHERE task_id = '{clean_id}'; "
        ));
    }
    batch.push_str("COMMIT;");
    run_sql(&batch);

    // Only claim success when the task row is actually gone.
    run_sql(&count_sql) == "0"
}

pub fn load_session_dialogue_from_db(dir: &std::path::Path, task_id: &str) -> Vec<DialogueItem> {
    let db_path = dir.join(".hex").join("data").join("ledger.db");
    if !db_path.exists() {
        return Vec::new();
    }

    let mut items = Vec::new();
    let clean_id = task_id.replace('\'', "''");

    // 1. Fetch user goal from tasks table
    let task_sql = format!(
        "SELECT goal, created_at FROM tasks WHERE task_id = '{}';",
        clean_id
    );
    let mut cmd_task = get_sqlite_command();
    let task_out = cmd_task
        .args([
            db_path.to_str().unwrap_or(".hex/data/ledger.db"),
            "-json",
            &task_sql,
        ])
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .unwrap_or_default();

    let mut task_goal = String::new();
    let mut task_time = String::new();
    if let Ok(records) = serde_json::from_str::<Vec<serde_json::Value>>(&task_out) {
        if let Some(first) = records.first() {
            task_goal = first.get("goal").and_then(|v| v.as_str()).unwrap_or("").to_string();
            task_time = first.get("created_at").and_then(|v| v.as_str()).unwrap_or("").to_string();
        }
    }

    if !task_goal.is_empty() {
        items.push(DialogueItem::Message(MessageItem {
            role: Role::User,
            content: task_goal,
            timestamp: fmt_relative_time(&task_time),
        }));
    }

    // 2. Fetch tool calls
    let tc_sql = format!(
        "SELECT call_id, tool_id, substr(arguments_redacted_json, 1, 1000) AS arguments_redacted_json, substr(result_json, 1, CASE WHEN status = 'FAILED' THEN 4000 ELSE 300 END) AS result_json, length(result_json) > CASE WHEN status = 'FAILED' THEN 4000 ELSE 300 END AS result_truncated, status FROM tool_calls WHERE task_id = '{}' ORDER BY started_at;",
        clean_id
    );
    let mut cmd_tc = get_sqlite_command();
    let tc_out = cmd_tc
        .args([
            db_path.to_str().unwrap_or(".hex/data/ledger.db"),
            "-json",
            &tc_sql,
        ])
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .unwrap_or_default();

    if let Ok(tc_records) = serde_json::from_str::<Vec<serde_json::Value>>(&tc_out) {
        for r in tc_records {
            let id = r.get("call_id").and_then(|v| v.as_str()).unwrap_or("tc").to_string();
            let tool = r.get("tool_id").and_then(|v| v.as_str()).unwrap_or("tool").to_string();
            let args_raw = r.get("arguments_redacted_json").and_then(|v| v.as_str()).unwrap_or("{}");
            let res_raw = r.get("result_json").and_then(|v| v.as_str()).unwrap_or("");
            let status = r.get("status").and_then(|v| v.as_str()).unwrap_or("COMPLETED");

            let args_display = if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(args_raw) {
                if let Some(p) = parsed.get("path").and_then(|v| v.as_str()) {
                    p.to_string()
                } else if let Some(c) = parsed.get("command").and_then(|v| v.as_str()) {
                    c.to_string()
                } else {
                    args_raw.to_string()
                }
            } else {
                args_raw.to_string()
            };

            items.push(DialogueItem::ToolCall(ToolCallItem {
                id: id.clone(),
                tool,
                args: args_display,
                result: if res_raw.is_empty() { None } else if r["result_truncated"].as_i64() == Some(1) {
                    Some(format!("{res_raw}… [full result: /tool-result {id}]"))
                } else { Some(res_raw.to_string()) },
                duration_ms: Some(120),
                exit_status: Some(format!("exit {}", if status == "FAILED" { "1" } else { "0" })),
                collapsed: true,
                started_at: None,
            }));
        }
    }

    // 3. Fetch assistant output from task_events
    let ev_sql = format!(
        "SELECT event_type, payload_json, occurred_at FROM task_events WHERE task_id = '{}' AND event_type IN ('STATE_SUCCEEDED', 'STATE_FAILED') ORDER BY sequence_no DESC LIMIT 1;",
        clean_id
    );
    let mut cmd_ev = get_sqlite_command();
    let ev_out = cmd_ev
        .args([
            db_path.to_str().unwrap_or(".hex/data/ledger.db"),
            "-json",
            &ev_sql,
        ])
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .unwrap_or_default();

    if let Ok(ev_records) = serde_json::from_str::<Vec<serde_json::Value>>(&ev_out) {
        if let Some(ev) = ev_records.first() {
            let ev_type = ev.get("event_type").and_then(|v| v.as_str()).unwrap_or("");
            let payload_str = ev.get("payload_json").and_then(|v| v.as_str()).unwrap_or("{}");
            let occurred_at = ev.get("occurred_at").and_then(|v| v.as_str()).unwrap_or("");

            let mut assistant_text = String::new();
            if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(payload_str) {
                if let Some(r) = parsed.get("result").and_then(|v| v.as_str()) {
                    assistant_text = r.to_string();
                } else if let Some(e) = parsed.get("error").and_then(|v| v.as_str()) {
                    assistant_text = format!("Error: {}", e);
                }
            }

            if assistant_text.is_empty() {
                assistant_text = if ev_type == "STATE_SUCCEEDED" {
                    "Task execution completed successfully.".to_string()
                } else {
                    "Task execution ended with an error.".to_string()
                };
            }

            items.push(DialogueItem::Message(MessageItem {
                role: Role::Assistant,
                content: assistant_text,
                timestamp: fmt_relative_time(occurred_at),
            }));
        }
    }

    items
}

pub fn load_session_plan_from_db(dir: &std::path::Path, task_id: &str) -> Vec<PlanStep> {
    let db_path = dir.join(".hex").join("data").join("ledger.db");
    if !db_path.exists() {
        return Vec::new();
    }

    let clean_id = task_id.replace('\'', "''");
    let sql = format!(
        "SELECT dag_json FROM plans WHERE task_id = '{}' ORDER BY revision DESC LIMIT 1;",
        clean_id
    );
    let mut cmd_plan = get_sqlite_command();
    let out = cmd_plan
        .args([
            db_path.to_str().unwrap_or(".hex/data/ledger.db"),
            "-json",
            &sql,
        ])
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .unwrap_or_default();

    if let Ok(records) = serde_json::from_str::<Vec<serde_json::Value>>(&out) {
        if let Some(first) = records.first() {
            if let Some(dag_raw) = first.get("dag_json").and_then(|v| v.as_str()) {
                if let Ok(steps) = serde_json::from_str::<Vec<serde_json::Value>>(dag_raw) {
                    return steps
                        .into_iter()
                        .filter_map(|s| {
                            let title = s.get("title").and_then(|v| v.as_str())?.to_string();
                            let st = match s
                                .get("status")
                                .and_then(|v| v.as_str())
                                .unwrap_or("PENDING")
                            {
                                "COMPLETED" | "SUCCEEDED" => StepStatus::Completed,
                                "RUNNING" | "ACTIVE" => StepStatus::Active,
                                _ => StepStatus::Pending,
                            };
                            Some(PlanStep { title, status: st })
                        })
                        .collect();
                }
            }
        }
    }

    Vec::new()
}

pub fn format_workspace_display(workspace_path: &str, project_id: &str) -> (String, String) {
    let home = std::env::var("HOME").unwrap_or_default();
    let norm_path = if !home.is_empty() && workspace_path.starts_with(&home) {
        format!("~{}", &workspace_path[home.len()..])
    } else {
        workspace_path.to_string()
    };

    let p = std::path::Path::new(&norm_path);
    let parent = p.parent()
        .and_then(|par| par.to_str())
        .map(|s| if s.ends_with('/') { s.to_string() } else { format!("{}/", s) })
        .unwrap_or_else(|| "~/".to_string());

    let leaf = if !project_id.is_empty() && project_id != "default" {
        project_id.to_string()
    } else {
        p.file_name()
            .and_then(|f| f.to_str())
            .unwrap_or("project")
            .to_string()
    };

    (parent, leaf)
}

/// Palette entries: every slash command this surface really supports, plus
/// navigation actions. Nothing here may offer an action the runtime lacks.
pub fn build_palette_items() -> Vec<PaletteItem> {
    let mut items: Vec<PaletteItem> = available_slash_commands()
        .iter()
        .map(|cmd| PaletteItem {
            category: palette_category(&cmd.name).to_string(),
            title: cmd.name.clone(),
            command: cmd.name.clone(),
        })
        .collect();
    items.push(PaletteItem {
        category: "session".to_string(),
        title: "Switch session".to_string(),
        command: "session:switch".to_string(),
    });
    items.push(PaletteItem {
        category: "workspace".to_string(),
        title: "Customize sidebar".to_string(),
        command: "sidebar:settings".to_string(),
    });
    items
}

fn palette_category(name: &str) -> &'static str {
    match name {
        "/mode" | "/plan" | "/agent" | "/code" => "modes",
        "/subagents" | "/react" => "agents",
        "/skills" | "/tools" | "/memory" | "/remember" | "/reflect" | "/recipe" => "knowledge",
        "/diff" | "/sbs" | "/rollback" | "/blast" | "/proof" | "/scan" | "/search" | "/lsp"
        | "/doctor" => "workspace",
        _ => "session",
    }
}

fn palette_category_rank(category: &str) -> u8 {
    match category {
        "modes" => 0,
        "agents" => 1,
        "session" => 2,
        "workspace" => 3,
        "knowledge" => 4,
        _ => 5,
    }
}

pub fn resolve_fallback_model_context(model: &str) -> usize {
    let m = model.to_lowercase().replace('.', "-");
    if m.contains("gemma-4") || m.contains("gemma4") {
        256_000
    } else if m.contains("gemma-3") {
        131_072
    } else if m.contains("gemma") {
        8_192
    } else if m.contains("gemini") {
        1_048_576
    } else if m.contains("claude-opus-4") || m.contains("claude-sonnet-4") || m.contains("claude-opus-5") || m.contains("claude-sonnet-5") {
        1_000_000
    } else if m.contains("claude") {
        200_000
    } else if m.contains("gpt-6") || m.contains("gpt-5-4") || m.contains("gpt-5-5") || m.contains("gpt-5-6") {
        1_050_000
    } else if m.contains("gpt-5-4-mini") || m.contains("gpt-5-4-nano") || m.contains("gpt-5") {
        400_000
    } else if m.contains("gpt-4-1") {
        1_047_576
    } else if m.contains("gpt-4") || m.contains("o1-mini") || m.contains("o1-preview") {
        128_000
    } else if m.contains("o3") || m.contains("o1") {
        200_000
    } else if m.contains("deepseek-v4") || m.contains("deepseek-chat") || m.contains("deepseek-reasoner") || m.contains("deepseek-flash") {
        1_000_000
    } else if m.contains("deepseek") {
        128_000
    } else if m.contains("grok-4-fast") || m.contains("grok-4-20") {
        2_000_000
    } else if m.contains("grok-4-6") || m.contains("grok-4-5") || m.contains("grok-build-latest") {
        500_000
    } else if m.contains("grok-4-3") {
        1_000_000
    } else if m.contains("grok-4") || m.contains("grok-build") || m.contains("grok-code-fast") {
        256_000
    } else if m.contains("grok-composer") {
        200_000
    } else if m.contains("grok") {
        131_072
    } else if m.contains("kimi-k3") || m.contains("muse-spark") || m.contains("inkling") {
        1_048_576
    } else if m.contains("kimi") || m.contains("qwen3-coder") || m.contains("qwen3-max") {
        262_144
    } else if m.contains("minimax-m3") {
        1_000_000
    } else if m.contains("minimax") {
        204_800
    } else if m.contains("solar-mini") {
        32_768
    } else if m.contains("solar-pro2") {
        65_536
    } else if m.contains("solar-pro3") {
        131_072
    } else if m.contains("solar") {
        524_288
    } else if m.contains("llama") || m.contains("nemotron") || m.contains("qwen2-5") || m.contains("qwen") {
        131_072
    } else if m.contains("qwen3") {
        1_000_000
    } else {
        256_000
    }
}

impl App {
    pub fn new(
        project_id: impl Into<String>,
        model_name: impl Into<String>,
        cow_mode: impl Into<String>,
    ) -> Self {
        Self::with_workspace(project_id, model_name, cow_mode, None)
    }

    pub fn with_workspace(
        project_id: impl Into<String>,
        model_name: impl Into<String>,
        cow_mode: impl Into<String>,
        workspace: Option<&std::path::Path>,
    ) -> Self {
        let ws = workspace.map(|p| p.to_path_buf()).unwrap_or_else(|| {
            std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."))
        });
        let (branch, sha) = detect_git_info(&ws);
        let (modified_files_count, indexed_files_count) = detect_git_status_counts(&ws);
        let changed_files = detect_changed_files_from_git(&ws);
        let plan_steps = Vec::new(); // Starts empty; populated dynamically during session
        let resume_sessions = load_real_resume_sessions(&ws);

        let model_str = model_name.into();
        // Unknown until the runtime reports it; never invent a model.
        let resolved_model = if model_str.is_empty() || model_str == "default" {
            String::new()
        } else {
            model_str
        };
        let palette_items = build_palette_items();
        let default_max_tokens = resolve_fallback_model_context(&resolved_model);

        let mut app = Self {
            session_id: "ses_default".to_string(),
            project_id: project_id.into(),
            model_name: resolved_model,
            agent_name: std::env::var("HEX_AGENT_NAME").unwrap_or_else(|_| "HEX".to_string()),
            provider_name: String::new(),
            cow_mode: cow_mode.into(),
            git_branch: branch,
            git_sha: sha,
            workspace_path: ws.to_string_lossy().to_string(),
            tools_count: 26,
            skills_count: 83,
            splash_text: None,
            fencing_token: 1,
            connection_status: "Connecting".to_string(),
            skin_name: detect_default_skin(),

            screen_state: ScreenState::Start,
            mode: "agent".to_string(),
            focus: Focus::Input,
            show_help: false,
            show_diff: false,
            side_by_side: false,
            should_quit: false,
            is_busy: false,
            busy_started_at: None,
            status_message: "Ready".to_string(),
            active_turn_has_streamed: false,
            cancel_requested: false,

            dialogue_items: Vec::new(),
            tool_result_refs: std::collections::HashMap::new(),
            transcript_store: None,
            transcript_error: None,
            transcript_ids: Vec::new(),
            transcript_history_stored: false,
            diff_transcript_id: None,
            dialogue_scroll: 0,
            dialogue_view_offset: std::cell::Cell::new(0),
            transcript_layout: std::cell::RefCell::new(Default::default()),
            follow_dialogue: true,
            dialogue_metrics: std::cell::Cell::new((0, 0)),
            dialogue_entrance: std::cell::Cell::new((0, None)),

            active_diff: None,
            raw_diff: None,
            diff_scroll: 0,

            sidebar: Default::default(),
            plan_steps,
            changed_files,
            subagents: Vec::new(),
            resume_sessions,
            selected_resume_index: 0,
            show_all_resume: false,
            resume_window_offset: 0,
            pending_delete_index: None,
            tokens_used: 0,
            max_tokens: default_max_tokens,
            modified_files_count,
            indexed_files_count,
            current_thought: None,
            thinking_status: None,

            show_palette: false,
            palette_query: String::new(),
            selected_palette_index: 0,
            palette_items,
            active_subagent_modal: None,
            selected_permission_index: 0,

            input_buffer: String::new(),
            cursor_pos: 0,
            command_history: Vec::new(),
            history_index: None,

            suggestion_index: 0,

            active_approval: None,
            active_diagnostic: None,
            animation_frame: 0,
            session_started: Instant::now(),
            screen_transition_started: None,

            mouse_pos: None,
            hovered_resume_row: None,
            hovered_delete_row: None,
            hovered_input_box: false,
            hovered_shortcut_hint: None,
            row_hover_levels: vec![0.0; 12],
            delete_hover_levels: vec![0.0; 12],
            input_hover_level: 0.0,
            toggle_hover_level: 0.0,
            shortcut_hover_levels: [0.0; 4],
        };

        // Seed with welcome messages
        app.add_system_message("◈ Welcome to HEX Agent");
        app.add_system_message("Type your goal or /help for interactive commands.");
        app
    }

    /// Set or override the agent's display name.
    pub fn set_agent_name(&mut self, name: impl Into<String>) {
        self.agent_name = name.into();
    }

    /// Add a user message to dialogue history.
    pub fn add_user_message(&mut self, content: &str) {
        self.switch_screen(ScreenState::Session);
        self.dialogue_items.push(DialogueItem::Message(MessageItem {
            role: Role::User,
            content: content.to_string(),
            timestamp: chrono_now(),
        }));
        self.enforce_transcript_budget();
        self.scroll_dialogue_to_bottom();
    }

    /// Add an assistant response message to dialogue history.
    pub fn add_assistant_message(&mut self, content: &str) {
        self.switch_screen(ScreenState::Session);
        let plan_source = if self.transcript_store.is_some() {
            let mut end = content.len().min(crate::history::MESSAGE_DISPLAY_BYTES);
            while !content.is_char_boundary(end) { end -= 1; }
            &content[..end]
        } else { content };
        let detected = extract_plan_steps_from_text(plan_source);
        if !detected.is_empty() {
            self.plan_steps = detected;
        }
        self.dialogue_items.push(DialogueItem::Message(MessageItem {
            role: Role::Assistant,
            content: content.to_string(),
            timestamp: chrono_now(),
        }));
        self.enforce_transcript_budget();
    }

    /// Replay a loaded session transcript so resumed conversations keep their dialogue.
    pub fn replay_session_history(&mut self, history: &[hex_protocol::models::SessionHistoryTurn]) {
        for turn in history {
            if !turn.prompt.is_empty() {
                self.add_user_message(&turn.prompt);
            }
            if !turn.response.is_empty() {
                self.add_assistant_message(&turn.response);
            }
        }
    }

    /// Append a streaming token delta to the active Assistant message, or create one if none exists.
    pub fn append_assistant_delta(&mut self, delta: &str) {
        self.active_turn_has_streamed = true;
        let index = self.dialogue_items.len().saturating_sub(1);
        if matches!(self.dialogue_items.last(), Some(DialogueItem::Message(msg)) if msg.role == Role::Assistant) {
            let stored = self.stored_message_id(index);
            let durable = if self.transcript_error.is_none() {
                if let (Some(id), Some(store)) = (stored, self.transcript_store.as_mut()) {
                    match store.append_delta(id, delta) {
                        Ok(()) => true,
                        Err(e) => { self.transcript_error = Some(e); false }
                    }
                } else { false }
            } else { false };
            if let DialogueItem::Message(msg) = &mut self.dialogue_items[index] {
                if durable && delta.len() >= crate::history::MESSAGE_DISPLAY_BYTES {
                    msg.content = crate::history::recent_tail(delta, stored.unwrap());
                } else {
                    msg.content.push_str(delta);
                }
            }
            self.enforce_transcript_budget();
            return;
        }
        self.add_assistant_message(delta);
    }

    /// Add a system notification message to dialogue history.
    pub fn add_system_message(&mut self, content: &str) {
        self.dialogue_items.push(DialogueItem::Message(MessageItem {
            role: Role::System,
            content: content.to_string(),
            timestamp: chrono_now(),
        }));
        self.enforce_transcript_budget();
    }

    /// Model label for display. Empty means the runtime has not reported one.
    pub fn model_label(&self) -> &str {
        if !self.model_name.is_empty() {
            &self.model_name
        } else if self.connection_status == "Standalone" {
            "no runtime"
        } else {
            "connecting…"
        }
    }

    /// Provider label for display, or a placeholder until the runtime reports it.
    pub fn provider_label(&self) -> &str {
        if self.provider_name.is_empty() {
            "unknown provider"
        } else {
            &self.provider_name
        }
    }

    /// Toggle between showing top 3 and a scrollable window of previous sessions.
    pub fn toggle_show_all_resume(&mut self) {
        self.set_start_focus(Focus::Resume);
        self.show_all_resume = !self.show_all_resume;
        self.resume_window_offset = 0;
        let total = self.resume_sessions.len() + usize::from(self.resume_toggle_index().is_some());
        self.selected_resume_index = self.selected_resume_index.min(total.saturating_sub(1));
    }

    /// Number of session rows drawn at once.
    pub fn resume_visible_count(&self) -> usize {
        let cap = if self.show_all_resume { 10 } else { 3 };
        self.resume_sessions.len().min(cap)
    }

    /// Selection index of the show-all/show-less row: one past the last session.
    pub fn resume_toggle_index(&self) -> Option<usize> {
        if self.resume_sessions.len() > 3 {
            Some(self.resume_sessions.len())
        } else {
            None
        }
    }

    pub fn move_resume_selection(&mut self, delta: i32) {
        let total = self.resume_sessions.len() + usize::from(self.resume_toggle_index().is_some());
        if total == 0 {
            return;
        }
        if self.focus != Focus::Resume {
            self.set_start_focus(Focus::Resume);
            self.selected_resume_index = 0;
            return;
        }
        if delta < 0 && self.selected_resume_index == 0 {
            self.set_start_focus(Focus::Input);
            return;
        }
        self.set_start_focus(Focus::Resume);
        self.pending_delete_index = None;
        let current = self.selected_resume_index.min(total - 1) as i32;
        self.selected_resume_index = (current + delta).clamp(0, total as i32 - 1) as usize;
    }

    /// Scroll the list by `delta` rows without wrapping, for wheel and page keys.
    pub fn scroll_resume(&mut self, delta: i32) {
        let total = self.resume_sessions.len() + usize::from(self.resume_toggle_index().is_some());
        if total == 0 {
            return;
        }
        // Scrolling past the collapsed preview reveals the rest of the history.
        if delta > 0 && !self.show_all_resume && self.resume_sessions.len() > 3 {
            self.show_all_resume = true;
            self.resume_window_offset = 0;
        }
        self.set_start_focus(Focus::Resume);
        self.pending_delete_index = None;
        let current = self.selected_resume_index.min(total - 1) as i32;
        let next = (current + delta).clamp(0, total as i32 - 1);
        self.selected_resume_index = next as usize;
    }

    /// First session index drawn. The window follows the selection so every
    /// session stays reachable instead of only the first ten.
    pub fn resume_window(&self) -> (usize, usize) {
        let count = self.resume_visible_count();
        if count == 0 {
            return (0, 0);
        }
        let max_offset = self.resume_sessions.len().saturating_sub(count);
        let mut offset = self.resume_window_offset.min(max_offset);
        let selected = self.selected_resume_index.min(self.resume_sessions.len().saturating_sub(1));
        if selected < offset {
            offset = selected;
        } else if selected >= offset + count {
            offset = selected + 1 - count;
        }
        (offset.min(max_offset), count)
    }

    /// Arm deletion of a session; a second call on the same row performs it.
    /// Returns true when the session was actually deleted.
    pub fn request_delete_resume_session(&mut self, idx: usize) -> bool {
        if idx >= self.resume_sessions.len() {
            return false;
        }
        self.set_start_focus(Focus::Resume);
        self.selected_resume_index = idx;
        if self.pending_delete_index != Some(idx) {
            self.pending_delete_index = Some(idx);
            self.status_message = format!(
                "Delete \"{}\"? Press d or Delete again to confirm.",
                ellipsize(&self.resume_sessions[idx].title, 40)
            );
            return false;
        }
        self.delete_resume_session(idx)
    }

    /// Abandon an armed delete without touching the ledger.
    pub fn cancel_pending_delete(&mut self) -> bool {
        if self.pending_delete_index.take().is_none() {
            return false;
        }
        self.status_message = "Delete cancelled.".to_string();
        true
    }

    /// Delete a session from local database and remove it from the resume list.
    /// The row is only dropped when the database confirms the delete.
    pub fn delete_resume_session(&mut self, idx: usize) -> bool {
        self.pending_delete_index = None;
        if idx >= self.resume_sessions.len() {
            return false;
        }
        let ws = std::path::PathBuf::from(&self.workspace_path);
        let target = &self.resume_sessions[idx];
        if !delete_session_from_db(&ws, &target.session_id) {
            self.status_message = format!(
                "Could not delete \"{}\" — it is still in the ledger.",
                ellipsize(&target.title, 40)
            );
            return false;
        }
        let removed = self.resume_sessions.remove(idx);
        if idx < self.row_hover_levels.len() {
            self.row_hover_levels.remove(idx);
        }
        if idx < self.delete_hover_levels.len() {
            self.delete_hover_levels.remove(idx);
        }
        if self.selected_resume_index >= self.resume_sessions.len() && !self.resume_sessions.is_empty() {
            self.selected_resume_index = self.resume_sessions.len() - 1;
        }
        let max_offset = self
            .resume_sessions
            .len()
            .saturating_sub(self.resume_visible_count());
        self.resume_window_offset = self.resume_window_offset.min(max_offset);
        if self.resume_sessions.is_empty() {
            self.selected_resume_index = 0;
            self.set_start_focus(Focus::Input);
        }
        self.status_message = format!("Deleted session: {}", ellipsize(&removed.title, 40));
        true
    }

    /// Recalculate context window token usage from live dialogue items.
    pub fn recalculate_tokens(&mut self) {
        let mut chars = 0;
        for item in &self.dialogue_items {
            match item {
                DialogueItem::Message(m) => chars += m.content.len(),
                DialogueItem::ToolCall(tc) => {
                    chars += tc.args.len();
                    if let Some(r) = &tc.result {
                        chars += r.len();
                    }
                }
            }
        }
        let est_tokens = chars / 4;
        self.tokens_used = est_tokens.max(self.tokens_used);
    }

    /// Dynamically update the maximum context window ceiling (Hermes parity).
    pub fn set_max_tokens(&mut self, tokens: usize) {
        if tokens > 0 {
            self.max_tokens = tokens;
        }
    }

    /// Resume an existing session by loading its conversation history, tool calls, and plan from the local database.
    ///
    /// The ledger stores one task per turn, not an ACP conversation. The stored
    /// transcript is shown as history; the next prompt starts a new turn on the
    /// runtime, which has no memory of it.
    pub fn resume_session(&mut self, session_id: &str) -> bool {
        let ws = std::path::PathBuf::from(&self.workspace_path);
        let next_store = if self.transcript_store.is_some() {
            match TranscriptStore::new(&ws, session_id) {
                Ok(store) => Some(store),
                Err(e) => { self.transcript_error = Some(e); return false; }
            }
        } else { None };
        if next_store.is_some() {
            let archived = (|| {
                self.archive_resident_transcript()?;
                for command in &self.command_history {
                    self.transcript_store.as_mut().unwrap().append_text(&Role::User, "command", command)?;
                }
                if self.diff_transcript_id.is_none() {
                    if let Some(raw) = &self.raw_diff {
                        self.transcript_store.as_mut().unwrap().append_text(&Role::System, "diff", raw)?;
                    }
                }
                Ok::<(), String>(())
            })();
            if let Err(e) = archived { self.transcript_error = Some(e); return false; }
            self.command_history = Vec::new();
            self.history_index = None;
            self.clear_diff();
        }
        let items = load_session_dialogue_from_db(&ws, session_id);
        self.transcript_store = next_store;
        self.transcript_ids = Vec::new();
        self.transcript_history_stored = false;
        *self.transcript_layout.borrow_mut() = Default::default();
        self.dialogue_view_offset.set(0);
        self.dialogue_scroll = 0;
        self.dialogue_metrics.set((0, 0));
        self.dialogue_entrance.set((0, None));
        self.session_id = session_id.to_string();
        self.switch_screen(ScreenState::Session);
        self.focus = Focus::Input;
        self.pending_delete_index = None;
        if items.is_empty() {
            self.dialogue_items = vec![DialogueItem::Message(MessageItem {
                role: Role::System,
                content: format!("No transcript stored for {session_id}."),
                timestamp: "now".to_string(),
            })];
        } else {
            self.dialogue_items = items;
            self.dialogue_items.push(DialogueItem::Message(MessageItem {
                role: Role::System,
                content: "Showing stored history. Your next message starts a new turn; the runtime does not remember this conversation.".to_string(),
                timestamp: "now".to_string(),
            }));
        }
        self.tool_result_refs = self.dialogue_items.iter().filter_map(|item| {
            if let DialogueItem::ToolCall(tool) = item {
                Some((tool.id.clone(), ToolResultRef {
                    task_id: session_id.to_string(), call_id: tool.id.clone(), local: true,
                }))
            } else { None }
        }).collect();
        self.plan_steps = load_session_plan_from_db(&ws, session_id);
        self.input_buffer.clear();
        self.cursor_pos = 0;
        self.recalculate_tokens();
        self.scroll_dialogue_to_bottom();
        self.status_message = format!("Resumed session {}", session_id);
        self.enforce_transcript_budget();
        true
    }

    /// Deterministically calculate the exact Rect bounds of the start screen resume section.
    pub fn get_start_resume_area(&self, area: ratatui::layout::Rect) -> ratatui::layout::Rect {
        let content_width = if area.width <= 70 {
            area.width.saturating_sub(4)
        } else if area.width <= 100 {
            (area.width * 75 / 100).clamp(60, 76)
        } else if area.width <= 140 {
            (area.width * 60 / 100).clamp(72, 96)
        } else {
            (area.width * 52 / 100).clamp(80, 126)
        };

        let visible_sessions_count = self.resume_visible_count();
        let has_toggle = self.resume_toggle_index().is_some();
        let resume_rows = if self.resume_sessions.is_empty() {
            2
        } else {
            1 + visible_sessions_count as u16 + if has_toggle { 1 } else { 0 }
        };
        let content_height = 23_u16;
        let top_pad = area.height.saturating_sub(content_height) / 2;

        let v_chunks = ratatui::layout::Layout::default()
            .direction(ratatui::layout::Direction::Vertical)
            .constraints([
                ratatui::layout::Constraint::Length(top_pad),
                ratatui::layout::Constraint::Length(4),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(5),
                ratatui::layout::Constraint::Length(1),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(resume_rows),
                ratatui::layout::Constraint::Min(0),
            ])
            .split(area);

        let w = content_width.min(v_chunks[8].width);
        let x = v_chunks[8].x + (v_chunks[8].width.saturating_sub(w)) / 2;
        ratatui::layout::Rect {
            x,
            y: v_chunks[8].y,
            width: w,
            height: v_chunks[8].height,
        }
    }

    /// Calculate exact Rect bounds of the start screen prompt card.
    pub fn get_start_prompt_area(&self, area: ratatui::layout::Rect) -> ratatui::layout::Rect {
        let content_width = if area.width <= 70 {
            area.width.saturating_sub(4)
        } else if area.width <= 100 {
            (area.width * 75 / 100).clamp(60, 76)
        } else if area.width <= 140 {
            (area.width * 60 / 100).clamp(72, 96)
        } else {
            (area.width * 52 / 100).clamp(80, 126)
        };
        let content_height = 23_u16;
        let top_pad = area.height.saturating_sub(content_height) / 2;

        let v_chunks = ratatui::layout::Layout::default()
            .direction(ratatui::layout::Direction::Vertical)
            .constraints([
                ratatui::layout::Constraint::Length(top_pad),
                ratatui::layout::Constraint::Length(4),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(5),
                ratatui::layout::Constraint::Length(1),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(5),
                ratatui::layout::Constraint::Min(0),
            ])
            .split(area);

        let w = content_width.min(v_chunks[5].width);
        let x = v_chunks[5].x + (v_chunks[5].width.saturating_sub(w)) / 2;
        ratatui::layout::Rect {
            x,
            y: v_chunks[5].y,
            width: w,
            height: v_chunks[5].height,
        }
    }

    /// Calculate exact Rect bounds of the start screen keyboard shortcut hints.
    pub fn get_start_hints_area(&self, area: ratatui::layout::Rect) -> ratatui::layout::Rect {
        let content_width = if area.width <= 70 {
            area.width.saturating_sub(4)
        } else if area.width <= 100 {
            (area.width * 75 / 100).clamp(60, 76)
        } else if area.width <= 140 {
            (area.width * 60 / 100).clamp(72, 96)
        } else {
            (area.width * 52 / 100).clamp(80, 126)
        };
        let content_height = 23_u16;
        let top_pad = area.height.saturating_sub(content_height) / 2;

        let v_chunks = ratatui::layout::Layout::default()
            .direction(ratatui::layout::Direction::Vertical)
            .constraints([
                ratatui::layout::Constraint::Length(top_pad),
                ratatui::layout::Constraint::Length(4),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(5),
                ratatui::layout::Constraint::Length(1),
                ratatui::layout::Constraint::Length(2),
                ratatui::layout::Constraint::Length(5),
                ratatui::layout::Constraint::Min(0),
            ])
            .split(area);

        let w = content_width.min(v_chunks[6].width);
        let x = v_chunks[6].x + (v_chunks[6].width.saturating_sub(w)) / 2;
        ratatui::layout::Rect {
            x,
            y: v_chunks[6].y,
            width: w,
            height: v_chunks[6].height,
        }
    }

    /// Calculate exact Rect bounds of the session screen composer input card.
    pub fn get_session_composer_area(&self, area: ratatui::layout::Rect) -> ratatui::layout::Rect {
        let v_chunks = ratatui::layout::Layout::default()
            .direction(ratatui::layout::Direction::Vertical)
            .constraints([
                ratatui::layout::Constraint::Length(1),
                ratatui::layout::Constraint::Length(1),
                ratatui::layout::Constraint::Min(10),
            ])
            .split(area);
        let rail_width = ((v_chunks[2].width as f32 * 0.23).round() as u16)
            .clamp(28, 44)
            .min(v_chunks[2].width.saturating_sub(40));
        let h_chunks = ratatui::layout::Layout::default()
            .direction(ratatui::layout::Direction::Horizontal)
            .constraints([
                ratatui::layout::Constraint::Min(40),
                ratatui::layout::Constraint::Length(1),
                ratatui::layout::Constraint::Length(rail_width),
            ])
            .split(v_chunks[2]);
        let c_chunks = ratatui::layout::Layout::default()
            .direction(ratatui::layout::Direction::Vertical)
            .constraints([
                ratatui::layout::Constraint::Min(6),
                ratatui::layout::Constraint::Length(3),
                ratatui::layout::Constraint::Length(1),
            ])
            .split(h_chunks[0]);
        c_chunks[1]
    }

    /// Handle mouse move events and update interactive hover targeting.
    pub fn handle_mouse_move(&mut self, col: u16, row: u16, term_rect: ratatui::layout::Rect) {
        self.mouse_pos = Some((col, row));
        if self.screen_state == ScreenState::Start {
            let resume_rect = self.get_start_resume_area(term_rect);
            let prompt_rect = self.get_start_prompt_area(term_rect);
            let hints_rect = self.get_start_hints_area(term_rect);

            // 1. Resume section hover hit-testing
            if col >= resume_rect.x
                && col < resume_rect.x + resume_rect.width
                && row > resume_rect.y
                && row < resume_rect.y + resume_rect.height
            {
                let relative_row = row.saturating_sub(resume_rect.y + 1) as usize;
                let visible_count = self.resume_visible_count();
                let has_toggle = self.resume_toggle_index().is_some();

                if relative_row < visible_count && relative_row < self.resume_sessions.len() {
                    self.hovered_resume_row = Some(relative_row);
                    if col >= resume_rect.x + resume_rect.width.saturating_sub(6) {
                        self.hovered_delete_row = Some(relative_row);
                    } else {
                        self.hovered_delete_row = None;
                    }
                } else if relative_row == visible_count && has_toggle {
                    self.hovered_resume_row = Some(visible_count);
                    self.hovered_delete_row = None;
                } else {
                    self.hovered_resume_row = None;
                    self.hovered_delete_row = None;
                }
            } else {
                self.hovered_resume_row = None;
                self.hovered_delete_row = None;
            }

            // 2. Prompt card hover hit-testing
            self.hovered_input_box = col >= prompt_rect.x
                && col < prompt_rect.x + prompt_rect.width
                && row >= prompt_rect.y
                && row < prompt_rect.y + prompt_rect.height;

            // 3. Shortcut hints hover hit-testing
            if col >= hints_rect.x && col < hints_rect.x + hints_rect.width && row == hints_rect.y {
                let seg_width = hints_rect.width / 4;
                if seg_width > 0 {
                    let seg_idx = (col.saturating_sub(hints_rect.x) / seg_width) as usize;
                    self.hovered_shortcut_hint = if seg_idx < 4 { Some(seg_idx) } else { None };
                } else {
                    self.hovered_shortcut_hint = None;
                }
            } else {
                self.hovered_shortcut_hint = None;
            }
        } else if self.screen_state == ScreenState::Session {
            let composer_rect = self.get_session_composer_area(term_rect);
            self.hovered_input_box = col >= composer_rect.x
                && col < composer_rect.x + composer_rect.width
                && row >= composer_rect.y
                && row < composer_rect.y + composer_rect.height;
        }
    }

    /// Handle mouse click inside the Start screen resume sessions area.
    pub fn handle_start_mouse_click(&mut self, col: u16, row: u16, rect: ratatui::layout::Rect) -> bool {
        if col >= rect.x && col < rect.x + rect.width && row > rect.y && row < rect.y + rect.height {
            let relative_row = row.saturating_sub(rect.y + 1) as usize;
            let visible_count = self.resume_visible_count();
            let has_toggle = self.resume_toggle_index().is_some();
            let (window_offset, _) = self.resume_window();
            let absolute_row = window_offset + relative_row;

            if relative_row < visible_count && absolute_row < self.resume_sessions.len() {
                // Rightmost 6 columns of the row is the '✕' delete button area
                if col >= rect.x + rect.width.saturating_sub(6) {
                    self.request_delete_resume_session(absolute_row);
                    return true;
                } else {
                    self.selected_resume_index = absolute_row;
                    let sid = self.resume_sessions[absolute_row].session_id.clone();
                    self.resume_session(&sid);
                    return true;
                }
            } else if relative_row == visible_count && has_toggle {
                self.toggle_show_all_resume();
                return true;
            }
        }
        false
    }

    /// Add a tool call block to dialogue history.
    pub fn add_tool_call(
        &mut self,
        id: &str,
        tool: &str,
        args: &str,
        duration_ms: Option<u64>,
        status: Option<&str>,
    ) {
        self.switch_screen(ScreenState::Session);
        let mut item = ToolCallItem {
            id: id.to_string(),
            tool: tool.to_string(),
            args: tool_preview(args, 1000),
            result: None,
            duration_ms,
            exit_status: status.map(str::to_string).or_else(|| Some("RUNNING".to_string())),
            collapsed: true,
            started_at: Some(std::time::Instant::now()),
        };
        if self.transcript_store.is_some() && args.chars().nth(1000).is_some() {
            if self.transcript_error.is_some() {
                item.args = args.to_string();
            } else {
                match self.transcript_store.as_mut().unwrap().append_tool(&item, self.tool_result_refs.get(id), args) {
                    Ok(stored) => item.args = format!("{} [full arguments: /transcript {stored}]", crate::history::short_prefix(args, 4000)),
                    Err(e) => { self.transcript_error = Some(e); item.args = args.to_string(); }
                }
            }
        }
        self.dialogue_items.push(DialogueItem::ToolCall(item));
        self.enforce_transcript_budget();
    }

    /// Mark a tool call as completed.
    pub fn complete_tool_call(&mut self, id: &str, result: &str, duration_ms: u64, is_err: bool) {
        let index = self.dialogue_items.iter().position(|item| matches!(item, DialogueItem::ToolCall(tc) if tc.id == id))
            .or_else(|| self.dialogue_items.iter().rposition(|item| matches!(item, DialogueItem::ToolCall(tc)
                if tc.exit_status.as_deref() == Some("RUNNING") && (tc.tool == id || tc.id.starts_with(id) || id.starts_with(&tc.tool)))))
            .or_else(|| self.dialogue_items.iter().rposition(|item| matches!(item, DialogueItem::ToolCall(tc) if tc.exit_status.as_deref() == Some("RUNNING"))));
        if let Some(index) = index {
            let DialogueItem::ToolCall(tc) = &self.dialogue_items[index] else { unreachable!() };
            let mut completed = tc.clone();
            let limit = if is_err { 4000 } else { 300 };
            completed.result = Some(tool_preview(result, limit));
            completed.duration_ms = Some(duration_ms);
            completed.exit_status = Some(if is_err { "ERROR" } else { "OK" }.to_string());
            if self.transcript_store.is_some() && result.chars().nth(limit).is_some() {
                if self.transcript_error.is_some() {
                    completed.result = Some(result.to_string());
                } else {
                    match self.transcript_store.as_mut().unwrap().append_tool(&completed, self.tool_result_refs.get(&completed.id), result) {
                        Ok(stored) => completed.result = Some(format!("{} [full result: /transcript {stored}]", crate::history::short_prefix(result, limit * 4))),
                        Err(e) => { self.transcript_error = Some(e); completed.result = Some(result.to_string()); }
                    }
                }
            }
            self.dialogue_items[index] = DialogueItem::ToolCall(completed);
            self.transcript_ids.retain(|(resident, _)| *resident != index);
        }
        self.enforce_transcript_budget();
    }

    /// Close all running tool calls when an assistant turn completes.
    pub fn close_running_tool_calls(&mut self) {
        for (index, item) in self.dialogue_items.iter_mut().enumerate() {
            if let DialogueItem::ToolCall(tc) = item {
                if tc.exit_status.as_deref() == Some("RUNNING") {
                    self.transcript_ids.retain(|(resident, _)| *resident != index);
                    tc.exit_status = Some("OK".to_string());
                    if tc.duration_ms.is_none() {
                        if let Some(started) = tc.started_at {
                            tc.duration_ms = Some(started.elapsed().as_millis() as u64);
                        }
                    }
                }
            }
        }
        self.enforce_transcript_budget();
    }

    /// Toggle collapsed state of a specific tool call by index.
    pub fn toggle_tool_call_collapse(&mut self, index: usize) {
        let mut count = 0;
        for item in &mut self.dialogue_items {
            if let DialogueItem::ToolCall(tc) = item {
                if count == index {
                    tc.collapsed = !tc.collapsed;
                    return;
                }
                count += 1;
            }
        }
    }

    /// Toggle collapsed state for all tool calls.
    pub fn toggle_all_tool_calls(&mut self) {
        let any_expanded = self.dialogue_items.iter().any(|item| match item {
            DialogueItem::ToolCall(tc) => !tc.collapsed,
            _ => false,
        });

        for item in &mut self.dialogue_items {
            if let DialogueItem::ToolCall(tc) = item {
                tc.collapsed = any_expanded;
            }
        }
    }

    /// Load unified diff text into state and parse it.
    pub fn load_diff(&mut self, diff_text: &str) {
        self.switch_screen(ScreenState::Session);
        self.diff_transcript_id = None;
        if self.transcript_store.is_some() && (diff_text.len() > crate::history::MESSAGE_DISPLAY_BYTES || diff_text.lines().take(513).count() > 512) {
            let stored = if self.transcript_error.is_none() {
                self.transcript_store.as_mut().unwrap().append_text(&Role::System, "diff", diff_text)
            } else { Err(self.transcript_error.clone().unwrap()) };
            match stored {
                Ok(id) => {
                    let preview = crate::history::diff_prefix(diff_text);
                    let notice = format!("PARTIAL DIFF PREVIEW — full source: /transcript {id}");
                    self.raw_diff = Some(format!("{notice}\n{preview}"));
                    let mut parsed = UnifiedDiff::parse(preview);
                    // Label inside the pane too, not only in a transient status row.
                    parsed.files.insert(0, hex_protocol::diff::DiffFile {
                        old_path: None, new_path: Some(notice.clone()), is_new: false,
                        is_deleted: false, is_binary: false, hunks: Vec::new(),
                    });
                    self.active_diff = Some(parsed);
                    self.diff_transcript_id = Some(id);
                    self.status_message = notice;
                }
                Err(e) => {
                    self.transcript_error = Some(e);
                    self.raw_diff = Some(diff_text.to_string());
                    self.active_diff = None;
                }
            }
        } else {
            self.raw_diff = Some(diff_text.to_string());
            self.active_diff = Some(UnifiedDiff::parse(diff_text));
        }
        self.show_diff = true;
        self.diff_scroll = 0;
        self.enforce_transcript_budget();
    }

    /// Clear diff pane.
    pub fn clear_diff(&mut self) {
        self.raw_diff = None;
        self.active_diff = None;
        self.diff_transcript_id = None;
        self.show_diff = false;
        self.diff_scroll = 0;
        if self.focus == Focus::Diff {
            self.focus = Focus::Input;
        }
    }

    /// Toggle diff pane visibility.
    pub fn toggle_diff_pane(&mut self) {
        if self.active_diff.is_some() || self.raw_diff.is_some() {
            self.show_diff = !self.show_diff;
            if !self.show_diff && self.focus == Focus::Diff {
                self.focus = Focus::Input;
            }
        }
    }

    /// Toggle side-by-side diff view.
    pub fn toggle_side_by_side(&mut self) {
        self.side_by_side = !self.side_by_side;
    }

    /// Set active approval request modal.
    pub fn set_approval_request(&mut self, req: ApprovalRequest) {
        self.active_approval = Some(req);
        self.selected_permission_index = 0;
        self.focus = Focus::Approval;
    }

    /// Move the approval radio selection, wrapping over the three options.
    pub fn move_permission_selection(&mut self, delta: i32) {
        self.selected_permission_index =
            (self.selected_permission_index as i32 + delta).rem_euclid(3) as usize;
    }

    /// Clear active approval request.
    pub fn clear_approval_request(&mut self) {
        self.active_approval = None;
        if self.focus == Focus::Approval {
            self.focus = Focus::Input;
        }
    }

    // -----------------------------------------------------------------------
    // Agents drill-down
    // -----------------------------------------------------------------------

    /// Refresh the rail's agents section from the server's subagent records.
    pub fn set_subagent_telemetry(&mut self, agents: &[hex_protocol::models::SubagentInfo]) {
        self.subagents = agents
            .iter()
            .map(|a| {
                let failed = matches!(a.status.as_str(), "failed" | "timed_out" | "interrupted")
                    || a.result.as_ref().and_then(|r| r.error.as_ref()).is_some();
                let status = if a.is_running() {
                    "running".to_string()
                } else if failed {
                    "fail".to_string()
                } else if a.status == "completed" {
                    "pass".to_string()
                } else {
                    a.status.clone()
                };
                let detail = a
                    .result
                    .as_ref()
                    .map(|r| r.summary.clone())
                    .filter(|s| !s.trim().is_empty())
                    .unwrap_or_else(|| a.goal.clone());
                SubagentTelemetry {
                    name: if a.role.is_empty() { a.subagent_id.clone() } else { a.role.clone() },
                    status,
                    detail: first_line(&detail, 48),
                    is_running: a.is_running(),
                    has_failed: failed,
                }
            })
            .collect();
    }

    /// Open the drill-down on the newest live subagent, or the newest record.
    /// An empty registry opens the modal's honest empty state.
    pub fn open_subagent_modal(&mut self, agents: &[hex_protocol::models::SubagentInfo]) {
        let chosen = agents
            .iter()
            .find(|a| a.is_running())
            .or_else(|| agents.last());
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        self.active_subagent_modal = Some(match chosen {
            None => SubagentModalState {
                name: String::new(),
                prompt: String::new(),
                is_expanded: false,
                activity_lines: Vec::new(),
                elapsed_seconds: 0,
                status: "idle".to_string(),
                is_running: false,
                activity_scroll: 0,
            },
            Some(a) => SubagentModalState {
                name: if a.role.is_empty() { a.subagent_id.clone() } else { a.role.clone() },
                prompt: a.goal.clone(),
                is_expanded: false,
                activity_lines: subagent_activity(a),
                elapsed_seconds: a.elapsed_seconds(now),
                status: a.status.clone(),
                is_running: a.is_running(),
                activity_scroll: 0,
            },
        });
    }

    pub fn close_subagent_modal(&mut self) {
        self.active_subagent_modal = None;
    }

    /// Scroll the activity tail: positive moves back toward older lines.
    pub fn scroll_subagent_activity(&mut self, delta: i32) {
        if let Some(modal) = self.active_subagent_modal.as_mut() {
            let max = modal.activity_lines.len().saturating_sub(1) as i32;
            modal.activity_scroll =
                (modal.activity_scroll as i32 + delta).clamp(0, max) as usize;
        }
    }

    pub fn toggle_subagent_prompt(&mut self) {
        if let Some(modal) = self.active_subagent_modal.as_mut() {
            modal.is_expanded = !modal.is_expanded;
        }
    }

    /// Record a failed tool call as the inline diagnostic card.
    pub fn record_tool_failure(&mut self, call_id: &str, payload: &str, duration_ms: u64) {
        let (tool, args) = self
            .dialogue_items
            .iter()
            .rev()
            .find_map(|item| match item {
                DialogueItem::ToolCall(tc) if tc.id == call_id => {
                    Some((tc.tool.clone(), tc.args.clone()))
                }
                _ => None,
            })
            .or_else(|| {
                self.dialogue_items.iter().rev().find_map(|item| match item {
                    DialogueItem::ToolCall(tc) if tc.exit_status.as_deref() == Some("ERROR") => {
                        Some((tc.tool.clone(), tc.args.clone()))
                    }
                    _ => None,
                })
            })
            .unwrap_or_else(|| ("tool".to_string(), String::new()));

        let diag = DiagnosticFailure::from_tool_error(&tool, &args, payload, duration_ms);
        // The transcript row carries the exit code, as the mockup's header does.
        let status = format!("exit {}", diag.exit_code);
        for item in self.dialogue_items.iter_mut().rev() {
            if let DialogueItem::ToolCall(tc) = item {
                if tc.id == call_id || tc.exit_status.as_deref() == Some("ERROR") {
                    tc.exit_status = Some(status);
                    break;
                }
            }
        }
        self.active_diagnostic = Some(diag);
        self.scroll_dialogue_to_bottom();
    }

    /// Cycle agent execution mode: agent -> plan -> code -> agent
    pub fn cycle_mode(&mut self) -> String {
        self.mode = match self.mode.as_str() {
            "agent" => "plan".to_string(),
            "plan" => "code".to_string(),
            "code" => "agent".to_string(),
            _ => "agent".to_string(),
        };
        self.mode.clone()
    }

    /// Set mode directly.
    pub fn set_mode(&mut self, m: &str) {
        match m.to_lowercase().as_str() {
            "plan" | "ask" => self.mode = "plan".to_string(),
            "code" => self.mode = "code".to_string(),
            _ => self.mode = "agent".to_string(),
        }
    }

    pub fn set_start_focus(&mut self, focus: Focus) {
        self.focus = focus;
        self.hovered_resume_row = None;
        self.hovered_delete_row = None;
        self.hovered_input_box = false;
        if focus == Focus::Input {
            self.cancel_pending_delete();
            self.row_hover_levels.fill(0.0);
            self.delete_hover_levels.fill(0.0);
            self.toggle_hover_level = 0.0;
        } else {
            self.input_hover_level = 0.0;
        }
    }

    /// Cycle focused pane.
    pub fn switch_focus(&mut self) {
        if self.active_approval.is_some() {
            return;
        }
        if self.screen_state == ScreenState::Start {
            let focus = if self.focus == Focus::Input && !self.resume_sessions.is_empty() {
                Focus::Resume
            } else {
                Focus::Input
            };
            self.set_start_focus(focus);
            return;
        }
        self.focus = match self.focus {
            Focus::Input => Focus::Dialogue,
            Focus::Dialogue => {
                if self.show_diff && self.active_diff.is_some() {
                    Focus::Diff
                } else {
                    Focus::Input
                }
            }
            Focus::Resume | Focus::Diff | Focus::Approval => Focus::Input,
        };
    }

    pub fn handle_tab(&mut self) {
        let next_mode = self.cycle_mode();
        self.status_message = format!("Switched mode to: {}", next_mode.to_uppercase());
    }

    // -----------------------------------------------------------------------
    // Text input & cursor management
    // -----------------------------------------------------------------------

    pub fn insert_char(&mut self, c: char) {
        self.pending_delete_index = None;
        let byte = char_to_byte(&self.input_buffer, self.cursor_pos);
        self.input_buffer.insert(byte, c);
        self.cursor_pos += 1;
    }

    pub fn delete_char(&mut self) {
        if self.cursor_pos > 0 && !self.input_buffer.is_empty() {
            let byte = char_to_byte(&self.input_buffer, self.cursor_pos - 1);
            self.input_buffer.remove(byte);
            self.cursor_pos -= 1;
            }
    }

    pub fn delete_char_forward(&mut self) {
        if self.cursor_pos < self.input_buffer.chars().count() {
            let byte = char_to_byte(&self.input_buffer, self.cursor_pos);
            self.input_buffer.remove(byte);
            }
    }

    pub fn move_cursor_left(&mut self) {
        if self.cursor_pos > 0 {
            self.cursor_pos -= 1;
        }
    }

    pub fn move_cursor_right(&mut self) {
        if self.cursor_pos < self.input_buffer.chars().count() {
            self.cursor_pos += 1;
        }
    }

    pub fn move_cursor_start(&mut self) {
        self.cursor_pos = 0;
    }

    pub fn move_cursor_end(&mut self) {
        self.cursor_pos = self.input_buffer.chars().count();
    }

    pub fn history_up(&mut self) {
        if self.command_history.is_empty() {
            return;
        }
        let next_idx = match self.history_index {
            None => self.command_history.len().saturating_sub(1),
            Some(i) => i.saturating_sub(1),
        };
        self.history_index = Some(next_idx);
        if let Some(cmd) = self.command_history.get(next_idx) {
            self.input_buffer = cmd.clone();
            self.cursor_pos = self.input_buffer.len();
            }
    }

    pub fn history_down(&mut self) {
        if let Some(i) = self.history_index {
            if i + 1 < self.command_history.len() {
                let next_idx = i + 1;
                self.history_index = Some(next_idx);
                if let Some(cmd) = self.command_history.get(next_idx) {
                    self.input_buffer = cmd.clone();
                    self.cursor_pos = self.input_buffer.len();
                            }
            } else {
                self.history_index = None;
                self.input_buffer.clear();
                self.cursor_pos = 0;
                    }
        }
    }

    pub fn submit_input(&mut self) -> Option<String> {
        let trimmed = self.input_buffer.trim().to_string();
        if trimmed.is_empty() {
            return None;
        }

        self.switch_screen(ScreenState::Session);
        self.suggestion_index = self.suggestion_index.wrapping_add(1);
        self.command_history.push(trimmed.clone());
        self.history_index = None;
        self.input_buffer.clear();
        self.cursor_pos = 0;
        // A new prompt retires the previous turn's failure card.
        self.active_diagnostic = None;
        self.enforce_transcript_budget();

        Some(trimmed)
    }

    pub fn switch_screen(&mut self, screen: ScreenState) {
        if self.screen_state != screen {
            self.screen_transition_started = if screen == ScreenState::Session {
                Some(Instant::now())
            } else {
                None
            };
            self.screen_state = screen;
        }
    }

    // -----------------------------------------------------------------------
    // Command palette
    // -----------------------------------------------------------------------

    /// Open the palette. The composer is left untouched so closing restores it.
    pub fn open_palette(&mut self) {
        self.show_palette = true;
        self.palette_query.clear();
        self.selected_palette_index = 0;
    }

    pub fn close_palette(&mut self) {
        self.show_palette = false;
        self.palette_query.clear();
        self.selected_palette_index = 0;
    }

    pub fn palette_push_char(&mut self, c: char) {
        self.palette_query.push(c);
        self.selected_palette_index = 0;
    }

    /// Backspace edits the query; on an empty query it closes the palette.
    pub fn palette_backspace(&mut self) {
        if self.palette_query.pop().is_none() {
            self.close_palette();
        }
        self.selected_palette_index = 0;
    }

    pub fn palette_move(&mut self, delta: i32) {
        let count = self.palette_results().len() as i32;
        if count == 0 {
            self.selected_palette_index = 0;
            return;
        }
        let next = self.selected_palette_index as i32 + delta;
        self.selected_palette_index = next.rem_euclid(count) as usize;
    }

    pub fn palette_selected(&self) -> Option<PaletteItem> {
        self.palette_results()
            .get(self.selected_palette_index)
            .map(|m| self.palette_items[m.item_index].clone())
    }

    /// Fuzzy-ranked palette entries, with the character positions that matched
    /// so the panel can highlight them instead of every letter of the query.
    pub fn palette_results(&self) -> Vec<PaletteMatch> {
        let query = self.palette_query.trim().trim_start_matches('/')
            .split_whitespace().next().unwrap_or("").to_lowercase();
        let mut scored: Vec<(u8, u8, usize, usize, usize, PaletteMatch)> = Vec::new();

        for (index, item) in self.palette_items.iter().enumerate() {
            let label = item.title.trim_start_matches('/');
            let title = label.to_lowercase();
            let Some(positions) = fuzzy_match(&query, &title) else {
                continue;
            };
            let rank = if title.starts_with(&query) { 0 } else { 1 };
            let span = positions
                .last()
                .and_then(|last| positions.first().map(|first| last - first))
                .unwrap_or(0);
            let prefix = item.title.chars().count() - label.chars().count();
            let matched = PaletteMatch {
                item_index: index,
                title_matched: positions.into_iter().map(|p| p + prefix).collect(),
            };
            let group = palette_category_rank(&item.category);
            scored.push((rank, group, span, title.chars().count(), index, matched));
        }

        if query.is_empty() {
            scored.sort_by_key(|(_, group, _, _, index, _)| (*group, *index));
        } else {
            scored.sort_by_key(|(rank, _, span, len, index, _)| (*rank, *span, *len, *index));
        }
        scored.into_iter().map(|(_, _, _, _, _, m)| m).collect()
    }

    // -----------------------------------------------------------------------
    // Scrolling
    // -----------------------------------------------------------------------

    pub fn scroll_up(&mut self) {
        match self.focus {
            Focus::Dialogue | Focus::Input => {
                if !self.follow_dialogue && self.transcript_layout.borrow().requested_scroll() == self.dialogue_scroll {
                    self.dialogue_scroll = self.dialogue_view_offset.get();
                }
                let (total, viewport) = self.dialogue_metrics.get();
                if self.follow_dialogue && self.dialogue_scroll == 0 && total > viewport {
                    // Leaving the pinned bottom: start from the real last row.
                    self.dialogue_scroll = total - viewport;
                }
                self.dialogue_scroll = self.dialogue_scroll.saturating_sub(1);
                self.follow_dialogue = false;
                if self.dialogue_scroll == 0 && self.transcript_history_stored {
                    self.status_message = "Earlier dialogue stored: /history; full text: /transcript ID".to_string();
                }
            }
            Focus::Diff => {
                self.diff_scroll = self.diff_scroll.saturating_sub(1);
            }
            _ => {}
        }
    }

    pub fn scroll_down(&mut self) {
        match self.focus {
            Focus::Dialogue | Focus::Input => {
                if !self.follow_dialogue && self.transcript_layout.borrow().requested_scroll() == self.dialogue_scroll {
                    self.dialogue_scroll = self.dialogue_view_offset.get();
                }
                self.dialogue_scroll = self.dialogue_scroll.saturating_add(1);
                let (total, viewport) = self.dialogue_metrics.get();
                if total > viewport && self.dialogue_scroll >= total - viewport {
                    self.scroll_dialogue_to_bottom();
                    self.dialogue_scroll = 0;
                }
            }
            Focus::Diff => {
                self.diff_scroll = self.diff_scroll.saturating_add(1);
            }
            _ => {}
        }
    }

    pub fn handle_page_up(&mut self) {
        for _ in 0..5 {
            self.scroll_up();
        }
    }

    pub fn handle_page_down(&mut self) {
        for _ in 0..5 {
            self.scroll_down();
        }
    }

    pub fn scroll_dialogue_to_bottom(&mut self) {
        // Paragraph wrapping is width-dependent.  Let the renderer calculate
        // the bottom offset from the real viewport instead of guessing 24 rows.
        self.follow_dialogue = true;
    }

    /// Note the transcript length and return the entrance window: how many
    /// blocks are already settled, and when the rising ones arrived.
    ///
    /// The baseline holds until the whole batch has risen, so the fade advances
    /// across frames instead of restarting every time the screen is drawn.
    pub fn observe_dialogue_entrance(&self, len: usize) -> (usize, std::time::Instant) {
        use crate::ui::animation::{MAX_ENTRANCE_BLOCKS, RISE_MS, RISE_STAGGER_MS};
        let (settled, born_at) = self.dialogue_entrance.get();
        let now = std::time::Instant::now();
        if self.screen_transition_started.is_some() {
            self.dialogue_entrance.set((len, None));
            return (len, now);
        }
        let Some(born) = born_at else {
            if len > settled {
                self.dialogue_entrance.set((settled, Some(now)));
                return (settled, now);
            }
            self.dialogue_entrance.set((len, None));
            return (len, now);
        };
        if len <= settled {
            self.dialogue_entrance.set((len, None));
            return (len, born);
        }
        let rising = (len - settled).min(MAX_ENTRANCE_BLOCKS);
        let span = std::time::Duration::from_millis(RISE_MS + RISE_STAGGER_MS * (rising - 1) as u64);
        if born.elapsed() >= span {
            self.dialogue_entrance.set((len, None));
            return (len, born);
        }
        (settled, born)
    }

    pub fn advance_animation(&mut self) {
        self.animation_frame = self.animation_frame.wrapping_add(1);
        if self.screen_transition_started.is_some_and(|started| {
            started.elapsed() >= std::time::Duration::from_millis(crate::ui::animation::SCREEN_REVEAL_MS)
        }) {
            self.screen_transition_started = None;
        }

        let visible_sessions = self.resume_visible_count();
        let (window_offset, _) = self.resume_window();
        let total_interactive = visible_sessions + if self.resume_toggle_index().is_some() { 1 } else { 0 };

        if self.row_hover_levels.len() < total_interactive {
            self.row_hover_levels.resize(total_interactive, 0.0);
        }
        if self.delete_hover_levels.len() < visible_sessions {
            self.delete_hover_levels.resize(visible_sessions, 0.0);
        }

        for idx in 0..visible_sessions {
            let is_row_active = self.hovered_resume_row == Some(idx)
                || (self.focus == Focus::Resume && self.hovered_resume_row.is_none()
                    && self.selected_resume_index == window_offset + idx);
            let target = if is_row_active { 1.0_f32 } else { 0.0_f32 };
            let current = self.row_hover_levels[idx];
            let delta = target - current;
            self.row_hover_levels[idx] = if delta.abs() < 0.02 {
                target
            } else {
                current + delta * 0.35
            };

            let is_del_active = self.hovered_delete_row == Some(idx);
            let del_target = if is_del_active { 1.0_f32 } else { 0.0_f32 };
            let del_current = self.delete_hover_levels[idx];
            let del_delta = del_target - del_current;
            self.delete_hover_levels[idx] = if del_delta.abs() < 0.02 {
                del_target
            } else {
                del_current + del_delta * 0.4
            };
        }

        // Toggle row hover (show all / show less). Mouse hover is in screen rows,
        // keyboard selection is an absolute session index.
        if let Some(toggle_idx) = self.resume_toggle_index() {
            let is_toggle_active = self.hovered_resume_row == Some(visible_sessions)
                || (self.focus == Focus::Resume && self.hovered_resume_row.is_none()
                    && self.selected_resume_index == toggle_idx);
            let toggle_target = if is_toggle_active { 1.0_f32 } else { 0.0_f32 };
            let toggle_delta = toggle_target - self.toggle_hover_level;
            self.toggle_hover_level = if toggle_delta.abs() < 0.02 {
                toggle_target
            } else {
                self.toggle_hover_level + toggle_delta * 0.35
            };
        } else {
            self.toggle_hover_level = 0.0;
        }

        // Input card border hover (smooth 300ms transition matching .ask:hover{border-color:#666})
        let input_target = if self.hovered_input_box
            || (!self.input_buffer.is_empty()
                && (self.screen_state != ScreenState::Start || self.focus == Focus::Input))
        { 1.0_f32 } else { 0.0_f32 };
        let input_delta = input_target - self.input_hover_level;
        self.input_hover_level = if input_delta.abs() < 0.02 {
            input_target
        } else {
            self.input_hover_level + input_delta * 0.3
        };

        // Shortcut hint badges hover
        for i in 0..4 {
            let target = if self.hovered_shortcut_hint == Some(i) { 1.0_f32 } else { 0.0_f32 };
            let delta = target - self.shortcut_hover_levels[i];
            self.shortcut_hover_levels[i] = if delta.abs() < 0.02 {
                target
            } else {
                self.shortcut_hover_levels[i] + delta * 0.35
            };
        }
    }
}

fn char_to_byte(text: &str, char_index: usize) -> usize {
    text.char_indices()
        .nth(char_index)
        .map(|(i, _)| i)
        .unwrap_or(text.len())
}

/// Subsequence match returning the positions in `target` that matched, in
/// order. Both sides are expected to be lowercased by the caller.
pub fn fuzzy_match(query: &str, target: &str) -> Option<Vec<usize>> {
    if query.is_empty() {
        return Some(Vec::new());
    }
    let chars: Vec<char> = target.chars().collect();
    let mut positions = Vec::new();
    let mut from = 0;
    for qc in query.chars() {
        let found = chars[from..].iter().position(|tc| *tc == qc)?;
        positions.push(from + found);
        from += found + 1;
    }
    Some(positions)
}

fn chrono_now() -> String {
    chrono::Local::now().format("%-I:%M %p").to_string()
}

/// Format an elapsed duration with Hermes-style precision:
/// e.g. "0.4s", "2.1s", "14s", "1m12s"
pub fn fmt_elapsed(duration: std::time::Duration) -> String {
    let secs = duration.as_secs_f32();
    if secs < 10.0 {
        format!("{:.1}s", secs)
    } else if secs < 60.0 {
        format!("{:.0}s", secs)
    } else {
        let mins = duration.as_secs() / 60;
        let rem = duration.as_secs() % 60;
        format!("{}m{:02}s", mins, rem)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_app_initialization() {
        let app = App::new("test_proj", "gemini-2.5-pro", "OverlayFS");
        assert_eq!(app.project_id, "test_proj");
        assert_eq!(app.model_name, "gemini-2.5-pro");
        assert_eq!(app.cow_mode, "OverlayFS");
        assert_eq!(app.focus, Focus::Input);
        assert!(!app.dialogue_items.is_empty());
    }

    #[test]
    fn test_input_and_history() {
        let mut app = App::new("p1", "m1", "cow");
        app.insert_char('h');
        app.insert_char('i');
        assert_eq!(app.input_buffer, "hi");
        assert_eq!(app.cursor_pos, 2);

        let submitted = app.submit_input();
        assert_eq!(submitted, Some("hi".to_string()));
        assert_eq!(app.input_buffer, "");
        assert_eq!(app.command_history, vec!["hi"]);

        app.history_up();
        assert_eq!(app.input_buffer, "hi");
    }

    #[test]
    fn test_palette_fuzzy_ranking_and_selection() {
        let mut app = App::new("p1", "m1", "cow");
        app.open_palette();
        assert!(app.show_palette);
        assert_eq!(app.palette_results().len(), app.palette_items.len());

        app.palette_query = "mem".to_string();
        let results = app.palette_results();
        let first = &app.palette_items[results[0].item_index];
        assert_eq!(first.title, "/memory");
        assert!(results.iter().any(|m| app.palette_items[m.item_index].title == "/remember"));

        app.palette_query = "ast".to_string();
        let results = app.palette_results();
        let blast = results
            .iter()
            .find(|m| app.palette_items[m.item_index].title == "/blast")
            .expect("/blast matches 'ast'");
        assert_eq!(blast.title_matched, vec![3, 4, 5]);

        for query in ["plan", "/plan", "PLAN"] {
            app.palette_query = query.to_string();
            let results = app.palette_results();
            let plan = &app.palette_items[results[0].item_index];
            assert_eq!(plan.title, "/plan");
            assert_eq!(results[0].title_matched, vec![1, 2, 3, 4]);
        }
        app.palette_query = "/history-turn 12 response 4096".to_string();
        assert_eq!(app.palette_selected().unwrap().command, "/history-turn");
        app.palette_query = "autonomous".to_string();
        assert!(app.palette_results().is_empty(), "help prose must not produce matches");
        assert!(app.palette_items.iter().filter(|item| item.command.starts_with('/'))
            .all(|item| item.title == item.command));

        app.palette_query = "sw".to_string();
        let results = app.palette_results();
        assert_eq!(
            app.palette_items[results[0].item_index].title, "Switch session",
            "the shortest precise prefix match wins the tie"
        );

        // Editing the query resets the selection; backspace on empty closes.
        app.palette_query = "mem".to_string();
        app.palette_move(1);
        assert_eq!(app.selected_palette_index, 1);
        app.palette_push_char('x');
        assert_eq!(app.selected_palette_index, 0);
        app.palette_query.clear();
        app.palette_backspace();
        assert!(!app.show_palette);

        // Every entry maps to a real command or a real navigation action.
        for item in &app.palette_items {
            assert!(
                item.command.starts_with('/') || matches!(item.command.as_str(), "session:switch" | "sidebar:settings"),
                "palette offers an unknown action: {}",
                item.command
            );
        }
    }

    #[test]
    fn test_toggle_side_by_side() {
        let mut app = App::new("p1", "m1", "cow");
        assert!(!app.side_by_side);
        app.toggle_side_by_side();
        assert!(app.side_by_side);
        app.toggle_side_by_side();
        assert!(!app.side_by_side);
    }

    #[test]
    fn test_tool_call_lifecycle() {
        let mut app = App::new("p1", "m1", "cow");
        app.add_tool_call("tc_1", "write_file", "path=test.txt", None, Some("RUNNING"));
        assert_eq!(app.dialogue_items.len(), 3); // 2 system messages + 1 tool call

        app.complete_tool_call("tc_1", "Wrote 10 bytes", 45, false);
        if let DialogueItem::ToolCall(tc) = &app.dialogue_items[2] {
            assert_eq!(tc.exit_status, Some("OK".to_string()));
            assert_eq!(tc.duration_ms, Some(45));
            assert_eq!(tc.result, Some("Wrote 10 bytes".to_string()));
        } else {
            panic!("Expected ToolCall item");
        }

        // By default tool calls are collapsed for a clean minimal transcript
        if let DialogueItem::ToolCall(tc) = &app.dialogue_items[2] {
            assert!(tc.collapsed);
        }

        // Toggling expands the tool details
        app.toggle_tool_call_collapse(0);
        if let DialogueItem::ToolCall(tc) = &app.dialogue_items[2] {
            assert!(!tc.collapsed);
        }
    }

    #[test]
    fn test_approval_flow() {
        let mut app = App::new("p1", "m1", "cow");
        let req = ApprovalRequest {
            id: "appr_1".to_string(),
            risk_level: "R4 - DESTRUCTIVE".to_string(),
            tool_name: "rm -rf".to_string(),
            parameters: "target=/data".to_string(),
            reason: "Directory deletion requested".to_string(),
            blast_radius: "Removes /data recursively".to_string(),
            cwd: "/data".to_string(),
        };
        app.set_approval_request(req);
        assert_eq!(app.focus, Focus::Approval);
        assert!(app.active_approval.is_some());

        app.clear_approval_request();
        assert_eq!(app.focus, Focus::Input);
        assert!(app.active_approval.is_none());
    }

    #[test]
    fn test_append_assistant_delta() {
        let mut app = App::new("p1", "m1", "cow");
        assert!(!app.active_turn_has_streamed);
        app.append_assistant_delta("Hello");
        assert!(app.active_turn_has_streamed);
        app.append_assistant_delta(" world");
        app.append_assistant_delta("!");

        // Total items: 2 welcome messages + 1 assistant message (accumulated!)
        assert_eq!(app.dialogue_items.len(), 3);
        if let DialogueItem::Message(msg) = &app.dialogue_items[2] {
            assert_eq!(msg.role, Role::Assistant);
            assert_eq!(msg.content, "Hello world!");
        } else {
            panic!("Expected Message item");
        }
    }

    #[test]
    fn test_resume_session_and_recalculate_tokens() {
        let mut app = App::new("p1", "m1", "cow");
        assert_eq!(app.screen_state, ScreenState::Start);
        app.resume_sessions = vec![
            ResumeSession {
                session_id: "test_sess_1".to_string(),
                title: "Refactor backend stream".to_string(),
                relative_time: "5m".to_string(),
            },
            ResumeSession {
                session_id: "test_sess_2".to_string(),
                title: "Profile render loop".to_string(),
                relative_time: "1h".to_string(),
            },
        ];

        let resumed = app.resume_session("test_sess_1");
        assert!(resumed);
        assert_eq!(app.screen_state, ScreenState::Session);
        assert_eq!(app.session_id, "test_sess_1");
        assert!(!app.dialogue_items.is_empty());
        assert!(app.status_message.contains("Resumed session test_sess_1"));
    }

    #[test]
    fn test_toggle_show_all_resume_sessions() {
        let mut app = App::new("p1", "m1", "cow");
        assert!(!app.show_all_resume);
        app.toggle_show_all_resume();
        assert!(app.show_all_resume);
        app.toggle_show_all_resume();
        assert!(!app.show_all_resume);
    }

    #[test]
    fn test_delete_resume_session_requires_a_real_ledger_row() {
        // Without a ledger the delete cannot succeed, so the row must stay and
        // the status must say so rather than claiming a deletion.
        let mut app = App::new("p1", "m1", "cow");
        app.workspace_path = std::env::temp_dir()
            .join(format!("hex_absent_ledger_{}", std::process::id()))
            .to_string_lossy()
            .to_string();
        app.resume_sessions = vec![
            ResumeSession {
                session_id: "s1".to_string(),
                title: "Task 1".to_string(),
                relative_time: "1m".to_string(),
            },
            ResumeSession {
                session_id: "s2".to_string(),
                title: "Task 2".to_string(),
                relative_time: "2m".to_string(),
            },
        ];
        app.selected_resume_index = 1;

        assert!(!app.delete_resume_session(1));
        assert_eq!(app.resume_sessions.len(), 2, "row must survive a failed delete");
        assert!(app.status_message.contains("Could not delete"));

        // Out of bounds deletion returns false
        assert!(!app.delete_resume_session(99));
    }

    #[test]
    fn test_delete_confirmation_arms_before_deleting() {
        let mut app = App::new("p1", "m1", "cow");
        app.resume_sessions = vec![ResumeSession {
            session_id: "s1".to_string(),
            title: "Task 1".to_string(),
            relative_time: "1m".to_string(),
        }];

        assert!(!app.request_delete_resume_session(0));
        assert_eq!(app.pending_delete_index, Some(0));
        assert_eq!(app.resume_sessions.len(), 1);
        assert!(app.status_message.contains("confirm"));

        // Second request targets the same row and attempts the real delete.
        assert!(!app.request_delete_resume_session(0), "no ledger, so it fails");
        assert_eq!(app.pending_delete_index, None);
        assert!(app.status_message.contains("Could not delete"));
    }

    #[test]
    fn test_resume_window_reaches_every_session() {
        let mut app = App::new("p1", "m1", "cow");
        app.resume_sessions = (0..25)
            .map(|i| ResumeSession {
                session_id: format!("s{i}"),
                title: format!("Task {i}"),
                relative_time: "1m".to_string(),
            })
            .collect();

        assert_eq!(app.resume_window(), (0, 3), "collapsed shows three");
        app.toggle_show_all_resume();
        assert_eq!(app.resume_window(), (0, 10));

        // Selecting past the window scrolls it instead of hiding the session.
        app.selected_resume_index = 14;
        assert_eq!(app.resume_window(), (5, 10));
        app.selected_resume_index = 24;
        assert_eq!(app.resume_window(), (15, 10));
        app.selected_resume_index = 2;
        assert_eq!(app.resume_window(), (0, 10), "jumping back into view scrolls minimally");
    }

    #[test]
    fn test_identity_labels_are_unknown_until_reported() {
        let app = App::new("p1", "", "cow");
        assert_eq!(app.model_name, "", "must not invent a model");
        assert_eq!(app.model_label(), "connecting…");
        assert_eq!(app.provider_label(), "unknown provider");
        assert_eq!(app.connection_status, "Connecting");
    }

    #[test]
    fn test_standalone_says_no_runtime_instead_of_connecting() {
        let mut app = App::new("p1", "", "cow");
        app.connection_status = "Standalone".to_string();
        assert_eq!(app.model_label(), "no runtime");
        app.model_name = "nvidia/nemotron-3-ultra-550b-a55b:free".to_string();
        assert_eq!(app.model_label(), "nvidia/nemotron-3-ultra-550b-a55b:free");
    }

    #[test]
    fn test_get_start_resume_area_bounds() {
        let mut app = App::new("p1", "m1", "cow");
        app.resume_sessions = vec![
            ResumeSession {
                session_id: "s1".to_string(),
                title: "Task 1".to_string(),
                relative_time: "1m".to_string(),
            },
            ResumeSession {
                session_id: "s2".to_string(),
                title: "Task 2".to_string(),
                relative_time: "2m".to_string(),
            },
            ResumeSession {
                session_id: "s3".to_string(),
                title: "Task 3".to_string(),
                relative_time: "3m".to_string(),
            },
            ResumeSession {
                session_id: "s4".to_string(),
                title: "Task 4".to_string(),
                relative_time: "4m".to_string(),
            },
        ];

        let rect_241x68 = app.get_start_resume_area(ratatui::layout::Rect::new(0, 0, 241, 68));
        assert!(rect_241x68.width > 50 && rect_241x68.width <= 126);
        assert_eq!(rect_241x68.height, 5); // 1 header + 3 items + 1 toggle

        // Expand show all
        app.toggle_show_all_resume();
        let rect_expanded = app.get_start_resume_area(ratatui::layout::Rect::new(0, 0, 241, 68));
        assert_eq!(rect_expanded.height, 6); // 1 header + 4 items + 1 toggle
    }

    #[test]
    fn test_hover_detection_and_smooth_animation_transitions() {
        let mut app = App::new("p1", "m1", "cow");
        app.resume_sessions = vec![
            ResumeSession {
                session_id: "s1".to_string(),
                title: "Task 1".to_string(),
                relative_time: "1m".to_string(),
            },
            ResumeSession {
                session_id: "s2".to_string(),
                title: "Task 2".to_string(),
                relative_time: "2m".to_string(),
            },
            ResumeSession {
                session_id: "s3".to_string(),
                title: "Task 3".to_string(),
                relative_time: "3m".to_string(),
            },
            ResumeSession {
                session_id: "s4".to_string(),
                title: "Task 4".to_string(),
                relative_time: "4m".to_string(),
            },
        ];

        let term_rect = ratatui::layout::Rect::new(0, 0, 120, 36);
        let resume_rect = app.get_start_resume_area(term_rect);

        // 1. Move mouse over row 1 (Task 2)
        let row1_y = resume_rect.y + 2; // header is y, row 0 is y+1, row 1 is y+2
        app.handle_mouse_move(resume_rect.x + 10, row1_y, term_rect);
        assert_eq!(app.hovered_resume_row, Some(1));
        assert_eq!(app.hovered_delete_row, None);

        // Advance animation frame: hover level for row 1 should smoothly rise
        let initial_row1 = app.row_hover_levels[1];
        app.advance_animation();
        assert!(app.row_hover_levels[1] > initial_row1);

        // Advance several frames: row 1 approaches 1.0
        for _ in 0..10 {
            app.advance_animation();
        }
        assert!(app.row_hover_levels[1] > 0.9);

        // 2. Move mouse directly over row 1's delete button '✕' (rightmost 5 cols)
        let del_x = resume_rect.x + resume_rect.width - 2;
        app.handle_mouse_move(del_x, row1_y, term_rect);
        assert_eq!(app.hovered_delete_row, Some(1));

        for _ in 0..10 {
            app.advance_animation();
        }
        assert!(app.delete_hover_levels[1] > 0.9);

        // 3. Move mouse away to prompt card
        let prompt_rect = app.get_start_prompt_area(term_rect);
        app.handle_mouse_move(prompt_rect.x + 5, prompt_rect.y + 1, term_rect);
        assert!(app.hovered_input_box);
        assert_eq!(app.hovered_resume_row, None);
        assert_eq!(app.hovered_delete_row, None);

        // Advance frames: prompt input level rises, row 1 and delete fade out towards 0.0
        for _ in 0..15 {
            app.advance_animation();
        }
        assert!(app.input_hover_level > 0.9);
        assert!(app.row_hover_levels[1] < 0.1);
        assert!(app.delete_hover_levels[1] < 0.1);
    }

    #[test]
    fn tui_autocomplete_matches_shared_catalog() {
        #[derive(serde::Deserialize)]
        struct Entry {
            name: String,
            #[serde(default)]
            surfaces: std::collections::HashMap<String, String>,
            #[serde(default)]
            tui_local: bool,
        }
        #[derive(serde::Deserialize)]
        struct Catalog {
            commands: Vec<Entry>,
        }
        let catalog: Catalog = serde_json::from_str(include_str!(
            "../../../hex_cli/command_catalog.json"
        ))
        .unwrap();

        let expected: Vec<&str> = catalog
            .commands
            .iter()
            .filter(|e| e.surfaces.get("tui").map(|s| s == "supported").unwrap_or(false))
            .map(|e| e.name.as_str())
            .collect();
        let offered: Vec<&str> = available_slash_commands().iter().map(|c| c.name.as_str()).collect();
        assert_eq!(offered, expected, "TUI completion must mirror the shared catalog");

        // Every TUI command is either handled locally or forwardable to ACP.
        for e in &catalog.commands {
            if e.surfaces.get("tui").map(|s| s == "supported").unwrap_or(false) {
                let acp_ok = e.surfaces.get("acp").map(|s| s == "supported").unwrap_or(false);
                assert!(
                    e.tui_local || acp_ok,
                    "{} is offered by the TUI but neither local nor ACP-supported",
                    e.name
                );
            }
        }
    }
}

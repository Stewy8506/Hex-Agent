//! Application state and business logic for HEX native terminal cockpit.

use hex_protocol::diff::UnifiedDiff;
use hex_protocol::models::Role;

/// Focused viewport pane in the TUI.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Focus {
    Input,
    Dialogue,
    Diff,
    Approval,
}

/// A regular conversational message item in dialogue.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MessageItem {
    pub role: Role,
    pub content: String,
    pub timestamp: String,
}

/// An expandable/collapsible tool invocation block.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolCallItem {
    pub id: String,
    pub tool: String,
    pub args: String,
    pub result: Option<String>,
    pub duration_ms: Option<u64>,
    pub exit_status: Option<String>,
    pub collapsed: bool,
}

/// Dialogue viewport item (either a chat message or a tool call).
#[derive(Debug, Clone, PartialEq, Eq)]
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
}

/// Slash command metadata for autocompletion popup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlashCommand {
    pub name: &'static str,
    pub description: &'static str,
    pub usage: &'static str,
}

pub const AVAILABLE_SLASH_COMMANDS: &[SlashCommand] = &[
    SlashCommand {
        name: "/help",
        description: "Show interactive help & keybindings",
        usage: "/help",
    },
    SlashCommand {
        name: "/status",
        description: "Display current workbench and task status",
        usage: "/status",
    },
    SlashCommand {
        name: "/diff",
        description: "Toggle proposed CoW diff pane",
        usage: "/diff",
    },
    SlashCommand {
        name: "/sbs",
        description: "Render two-column side-by-side diff for modified file",
        usage: "/sbs <path>",
    },
    SlashCommand {
        name: "/blast",
        description: "Render Active Graphify AST blast-radius tree",
        usage: "/blast [sym|file]",
    },
    SlashCommand {
        name: "/proof",
        description: "Display cryptographic Proof-Carrying Artifact bundle",
        usage: "/proof [task_id]",
    },
    SlashCommand {
        name: "/rollback",
        description: "Discard mutations and rollback CoW snapshot",
        usage: "/rollback",
    },
    SlashCommand {
        name: "/fork",
        description: "Branch workspace snapshot into speculative attempt",
        usage: "/fork",
    },
    SlashCommand {
        name: "/skills",
        description: "List active and scanned SkillSpector skills",
        usage: "/skills",
    },
    SlashCommand {
        name: "/scan",
        description: "Scan and register a SKILL.md bundle via SkillSpector",
        usage: "/scan <dir>",
    },
    SlashCommand {
        name: "/model",
        description: "Inspect or switch active model provider",
        usage: "/model [name]",
    },
    SlashCommand {
        name: "/budget",
        description: "View token consumption and budget limits",
        usage: "/budget",
    },
    SlashCommand {
        name: "/quota",
        description: "Inspect Google Antigravity OAuth session & quota windows",
        usage: "/quota",
    },
    SlashCommand {
        name: "/memory",
        description: "Search governed memories with AST code-hash staleness",
        usage: "/memory [query]",
    },
    SlashCommand {
        name: "/remember",
        description: "Record a governed project memory linked to AST nodes",
        usage: "/remember <k>: <v>",
    },
    SlashCommand {
        name: "/reflect",
        description: "Run Graphify deterministic work-memory reflection",
        usage: "/reflect",
    },
    SlashCommand {
        name: "/search",
        description: "Run SQLite FTS5 BM25 search across tasks and logs",
        usage: "/search <query>",
    },
    SlashCommand {
        name: "/doctor",
        description: "Run 8-category system diagnostics and self-healing",
        usage: "/doctor [--fix]",
    },
    SlashCommand {
        name: "/recipe",
        description: "Auto-detect project bootstrap, build, and test recipes",
        usage: "/recipe",
    },
    SlashCommand {
        name: "/lsp",
        description: "Inspect language servers and compiler diagnostics",
        usage: "/lsp [status|diag]",
    },
    SlashCommand {
        name: "/subagents",
        description: "Inspect autonomous subagent swarm workers",
        usage: "/subagents",
    },
    SlashCommand {
        name: "/react",
        description: "Toggle autonomous multi-turn ReAct mode",
        usage: "/react [on|off]",
    },
    SlashCommand {
        name: "/clear",
        description: "Clear dialogue history viewport",
        usage: "/clear",
    },
    SlashCommand {
        name: "/quit",
        description: "Exit HEX terminal cockpit",
        usage: "/quit",
    },
];

/// Top-level application state.
pub struct App {
    // Session metadata
    pub session_id: String,
    pub project_id: String,
    pub model_name: String,
    pub cow_mode: String,
    pub fencing_token: u64,
    pub connection_status: String,

    // Navigation and layout
    pub focus: Focus,
    pub show_help: bool,
    pub show_diff: bool,
    pub side_by_side: bool,
    pub should_quit: bool,
    pub is_busy: bool,
    pub status_message: String,

    // Dialogue items and scrolling
    pub dialogue_items: Vec<DialogueItem>,
    pub dialogue_scroll: usize,

    // Diff inspection
    pub active_diff: Option<UnifiedDiff>,
    pub raw_diff: Option<String>,
    pub diff_scroll: usize,

    // Command input & history
    pub input_buffer: String,
    pub cursor_pos: usize,
    pub command_history: Vec<String>,
    pub history_index: Option<usize>,

    // Slash command popup
    pub slash_suggestions: Vec<SlashCommand>,
    pub selected_slash_index: usize,

    // Approval interrupt modal
    pub active_approval: Option<ApprovalRequest>,
}

impl App {
    pub fn new(project_id: impl Into<String>, model_name: impl Into<String>, cow_mode: impl Into<String>) -> Self {
        let mut app = Self {
            session_id: "ses_default".to_string(),
            project_id: project_id.into(),
            model_name: model_name.into(),
            cow_mode: cow_mode.into(),
            fencing_token: 1,
            connection_status: "Connected".to_string(),

            focus: Focus::Input,
            show_help: false,
            show_diff: false,
            side_by_side: false,
            should_quit: false,
            is_busy: false,
            status_message: "Ready".to_string(),

            dialogue_items: Vec::new(),
            dialogue_scroll: 0,

            active_diff: None,
            raw_diff: None,
            diff_scroll: 0,

            input_buffer: String::new(),
            cursor_pos: 0,
            command_history: Vec::new(),
            history_index: None,

            slash_suggestions: Vec::new(),
            selected_slash_index: 0,

            active_approval: None,
        };

        // Seed with welcome messages
        app.add_system_message("◈ Welcome to HEX Autonomous AI Workbench Cockpit");
        app.add_system_message("Type your goal or /help for interactive commands.");
        app
    }

    /// Add a user message to dialogue history.
    pub fn add_user_message(&mut self, content: &str) {
        self.dialogue_items.push(DialogueItem::Message(MessageItem {
            role: Role::User,
            content: content.to_string(),
            timestamp: chrono_now(),
        }));
        self.scroll_dialogue_to_bottom();
    }

    /// Add an assistant response message to dialogue history.
    pub fn add_assistant_message(&mut self, content: &str) {
        self.dialogue_items.push(DialogueItem::Message(MessageItem {
            role: Role::Assistant,
            content: content.to_string(),
            timestamp: chrono_now(),
        }));
        self.scroll_dialogue_to_bottom();
    }

    /// Append a streaming token delta to the active Assistant message, or create one if none exists.
    pub fn append_assistant_delta(&mut self, delta: &str) {
        if let Some(DialogueItem::Message(msg)) = self.dialogue_items.last_mut() {
            if msg.role == Role::Assistant {
                msg.content.push_str(delta);
                self.scroll_dialogue_to_bottom();
                return;
            }
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
        self.scroll_dialogue_to_bottom();
    }

    /// Add a tool call block to dialogue history.
    pub fn add_tool_call(&mut self, id: &str, tool: &str, args: &str, duration_ms: Option<u64>, status: Option<&str>) {
        self.dialogue_items.push(DialogueItem::ToolCall(ToolCallItem {
            id: id.to_string(),
            tool: tool.to_string(),
            args: args.to_string(),
            result: None,
            duration_ms,
            exit_status: status.map(|s| s.to_string()).or_else(|| Some("RUNNING".to_string())),
            collapsed: false,
        }));
        self.scroll_dialogue_to_bottom();
    }

    /// Mark a tool call as completed.
    pub fn complete_tool_call(&mut self, id: &str, result: &str, duration_ms: u64, is_err: bool) {
        for item in &mut self.dialogue_items {
            if let DialogueItem::ToolCall(tc) = item {
                if tc.id == id {
                    tc.result = Some(result.to_string());
                    tc.duration_ms = Some(duration_ms);
                    tc.exit_status = Some(if is_err { "ERROR".to_string() } else { "OK".to_string() });
                    break;
                }
            }
        }
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
        self.raw_diff = Some(diff_text.to_string());
        self.active_diff = Some(UnifiedDiff::parse(diff_text));
        self.show_diff = true;
        self.diff_scroll = 0;
    }

    /// Clear diff pane.
    pub fn clear_diff(&mut self) {
        self.raw_diff = None;
        self.active_diff = None;
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

    /// Set active approval request modal.
    pub fn set_approval_request(&mut self, req: ApprovalRequest) {
        self.active_approval = Some(req);
        self.focus = Focus::Approval;
    }

    /// Clear active approval request.
    pub fn clear_approval_request(&mut self) {
        self.active_approval = None;
        if self.focus == Focus::Approval {
            self.focus = Focus::Input;
        }
    }

    /// Cycle focused pane.
    pub fn switch_focus(&mut self) {
        if self.active_approval.is_some() {
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
            Focus::Diff => Focus::Input,
            Focus::Approval => Focus::Input,
        };
    }

    // -----------------------------------------------------------------------
    // Text input & cursor management
    // -----------------------------------------------------------------------

    pub fn insert_char(&mut self, c: char) {
        self.input_buffer.insert(self.cursor_pos, c);
        self.cursor_pos += 1;
        self.update_slash_suggestions();
    }

    pub fn delete_char(&mut self) {
        if self.cursor_pos > 0 && !self.input_buffer.is_empty() {
            self.input_buffer.remove(self.cursor_pos - 1);
            self.cursor_pos -= 1;
            self.update_slash_suggestions();
        }
    }

    pub fn delete_char_forward(&mut self) {
        if self.cursor_pos < self.input_buffer.len() {
            self.input_buffer.remove(self.cursor_pos);
            self.update_slash_suggestions();
        }
    }

    pub fn move_cursor_left(&mut self) {
        if self.cursor_pos > 0 {
            self.cursor_pos -= 1;
        }
    }

    pub fn move_cursor_right(&mut self) {
        if self.cursor_pos < self.input_buffer.len() {
            self.cursor_pos += 1;
        }
    }

    pub fn move_cursor_start(&mut self) {
        self.cursor_pos = 0;
    }

    pub fn move_cursor_end(&mut self) {
        self.cursor_pos = self.input_buffer.len();
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
            self.update_slash_suggestions();
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
                    self.update_slash_suggestions();
                }
            } else {
                self.history_index = None;
                self.input_buffer.clear();
                self.cursor_pos = 0;
                self.update_slash_suggestions();
            }
        }
    }

    pub fn submit_input(&mut self) -> Option<String> {
        let trimmed = self.input_buffer.trim().to_string();
        if trimmed.is_empty() {
            return None;
        }

        self.command_history.push(trimmed.clone());
        self.history_index = None;
        self.input_buffer.clear();
        self.cursor_pos = 0;
        self.slash_suggestions.clear();

        Some(trimmed)
    }

    pub fn update_slash_suggestions(&mut self) {
        let trimmed = self.input_buffer.trim_start();
        if trimmed.starts_with('/') {
            let filter = trimmed.split_whitespace().next().unwrap_or("/");
            self.slash_suggestions = AVAILABLE_SLASH_COMMANDS
                .iter()
                .filter(|cmd| cmd.name.starts_with(filter))
                .cloned()
                .collect();
            self.selected_slash_index = 0;
        } else {
            self.slash_suggestions.clear();
            self.selected_slash_index = 0;
        }
    }

    pub fn next_slash_suggestion(&mut self) {
        if !self.slash_suggestions.is_empty() {
            self.selected_slash_index = (self.selected_slash_index + 1) % self.slash_suggestions.len();
        }
    }

    pub fn prev_slash_suggestion(&mut self) {
        if !self.slash_suggestions.is_empty() {
            if self.selected_slash_index == 0 {
                self.selected_slash_index = self.slash_suggestions.len() - 1;
            } else {
                self.selected_slash_index -= 1;
            }
        }
    }

    pub fn accept_slash_suggestion(&mut self) -> bool {
        if let Some(cmd) = self.slash_suggestions.get(self.selected_slash_index) {
            self.input_buffer = format!("{} ", cmd.name);
            self.cursor_pos = self.input_buffer.len();
            self.slash_suggestions.clear();
            true
        } else {
            false
        }
    }

    // -----------------------------------------------------------------------
    // Scrolling
    // -----------------------------------------------------------------------

    pub fn scroll_up(&mut self) {
        match self.focus {
            Focus::Dialogue => {
                self.dialogue_scroll = self.dialogue_scroll.saturating_sub(1);
            }
            Focus::Diff => {
                self.diff_scroll = self.diff_scroll.saturating_sub(1);
            }
            _ => {}
        }
    }

    pub fn scroll_down(&mut self) {
        match self.focus {
            Focus::Dialogue => {
                self.dialogue_scroll = self.dialogue_scroll.saturating_add(1);
            }
            Focus::Diff => {
                self.diff_scroll = self.diff_scroll.saturating_add(1);
            }
            _ => {}
        }
    }

    pub fn scroll_dialogue_to_bottom(&mut self) {
        // Approximate total height
        self.dialogue_scroll = self.dialogue_items.len().saturating_mul(4);
    }
}

fn chrono_now() -> String {
    // Zero extra external dependency timestamp
    use std::time::SystemTime;
    let duration = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = duration.as_secs();
    let hours = (secs / 3600) % 24;
    let mins = (secs / 60) % 60;
    let s = secs % 60;
    format!("{:02}:{:02}:{:02}", hours, mins, s)
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
    fn test_slash_suggestions() {
        let mut app = App::new("p1", "m1", "cow");
        app.insert_char('/');
        app.insert_char('d');
        assert!(!app.slash_suggestions.is_empty());
        assert_eq!(app.slash_suggestions[0].name, "/diff");

        assert!(app.accept_slash_suggestion());
        assert_eq!(app.input_buffer, "/diff ");
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

        app.toggle_tool_call_collapse(0);
        if let DialogueItem::ToolCall(tc) = &app.dialogue_items[2] {
            assert!(tc.collapsed);
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
        app.append_assistant_delta("Hello");
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
}

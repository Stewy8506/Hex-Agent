//! Distinct tool call cards with collapsible arguments, output, and inline diffs for HEX Desktop.
//!
//! Mirrors Hermes Desktop tool card components (`components/assistant-ui/tool/`):
//! - Tool header with icon badge, tool name, execution status, and duration
//! - Collapsible arguments / input panel
//! - Collapsible terminal output / observation box
//! - Optional integrated `DiffView` for code modifications

use gpui::*;

use crate::theme::HermesTheme;
use crate::ui::diff_view::DiffView;

/// Execution status of a tool invocation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolStatus {
    /// In flight / executing (Amber indicator).
    Running,
    /// Executed successfully with exit code 0 (Sage green checkmark).
    Success,
    /// Failed or returned non-zero error (Rose red cross).
    Failed,
}

impl ToolStatus {
    pub fn badge_symbol(&self) -> &'static str {
        match self {
            ToolStatus::Running => "⏳",
            ToolStatus::Success => "✓",
            ToolStatus::Failed => "✗",
        }
    }

    pub fn color(&self, theme: &HermesTheme) -> Rgba {
        match self {
            ToolStatus::Running => theme.warning_amber(),
            ToolStatus::Success => theme.sage_fg(),
            ToolStatus::Failed => theme.rose_fg(),
        }
    }
}

/// Returns a descriptive icon for a recognized tool name.
pub fn tool_icon(name: &str) -> &'static str {
    match name {
        "run_command" | "execute_command" | "bash" | "shell" => "⚡",
        "write_file" | "edit_file" | "patch" => "📝",
        "read_file" | "list_directory" | "find_files" => "📄",
        "web_search" | "browser" => "🌐",
        "delegate" | "subagent" => "🤖",
        _ => "🛠️",
    }
}

/// Tool call card component.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolCallCard {
    pub call_id: String,
    pub tool_name: String,
    pub arguments_json: String,
    pub output_text: Option<String>,
    pub diff: Option<DiffView>,
    pub status: ToolStatus,
    pub duration_ms: Option<u64>,
    pub input_expanded: bool,
    pub output_expanded: bool,
}

impl ToolCallCard {
    pub fn new(
        call_id: impl Into<String>,
        tool_name: impl Into<String>,
        arguments_json: impl Into<String>,
    ) -> Self {
        Self {
            call_id: call_id.into(),
            tool_name: tool_name.into(),
            arguments_json: arguments_json.into(),
            output_text: None,
            diff: None,
            status: ToolStatus::Running,
            duration_ms: None,
            input_expanded: false,
            output_expanded: true,
        }
    }

    pub fn with_output(mut self, output: impl Into<String>) -> Self {
        self.output_text = Some(output.into());
        self.status = ToolStatus::Success;
        self
    }

    pub fn with_error(mut self, error: impl Into<String>) -> Self {
        self.output_text = Some(error.into());
        self.status = ToolStatus::Failed;
        self
    }

    pub fn with_diff(mut self, diff: DiffView) -> Self {
        self.diff = Some(diff);
        self
    }

    pub fn with_duration(mut self, duration_ms: u64) -> Self {
        self.duration_ms = Some(duration_ms);
        self
    }

    pub fn toggle_input(&mut self) {
        self.input_expanded = !self.input_expanded;
    }

    pub fn toggle_output(&mut self) {
        self.output_expanded = !self.output_expanded;
    }

    /// Renders the tool header row.
    fn render_header(&self, theme: &HermesTheme) -> Div {
        let icon = tool_icon(&self.tool_name);
        let status_color = self.status.color(theme);
        let status_symbol = self.status.badge_symbol();

        let duration_str = self
            .duration_ms
            .map(|ms| format!("{}ms", ms))
            .unwrap_or_default();

        div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .justify_between()
            .px_3()
            .py_2()
            .bg(theme.card_bg())
            .border_b_1()
            .border_color(theme.border_subtle())
            .child(
                div()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap_2()
                    .child(
                        div()
                            .text_sm()
                            .child(icon),
                    )
                    .child(
                        div()
                            .text_xs()
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(theme.text_primary())
                            .child(self.tool_name.clone()),
                    ),
            )
            .child(
                div()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap_2()
                    .text_xs()
                    .child(
                        div()
                            .text_color(theme.text_dim())
                            .child(duration_str),
                    )
                    .child(
                        div()
                            .font_weight(FontWeight::BOLD)
                            .text_color(status_color)
                            .child(status_symbol),
                    ),
            )
    }

    /// Renders the tool input / parameters fold.
    fn render_input_fold(&self, theme: &HermesTheme) -> Div {
        let caret = if self.input_expanded { "▾" } else { "▸" };

        let mut section = div().w_full().flex().flex_col();

        let header = div()
            .flex()
            .flex_row()
            .items_center()
            .gap_1()
            .px_3()
            .py_1()
            .bg(theme.card_bg())
            .cursor_pointer()
            .hover(|s| s.bg(theme.card_hover()))
            .child(
                div()
                    .text_xs()
                    .text_color(theme.text_dim())
                    .child(caret),
            )
            .child(
                div()
                    .text_xs()
                    .text_color(theme.text_muted())
                    .child("Parameters"),
            );

        section = section.child(header);

        if self.input_expanded {
            let body = div()
                .w_full()
                .p_2()
                .bg(rgb(0x0e0e11))
                .border_t_1()
                .border_color(theme.border_subtle())
                .text_xs()
                .text_color(theme.text_muted())
                .child(self.arguments_json.clone());
            section = section.child(body);
        }

        section
    }

    /// Renders the output / observation fold.
    fn render_output_fold(&self, theme: &HermesTheme) -> Option<Div> {
        let output = self.output_text.as_ref()?;
        let caret = if self.output_expanded { "▾" } else { "▸" };

        let mut section = div().w_full().flex().flex_col();

        let header = div()
            .flex()
            .flex_row()
            .items_center()
            .gap_1()
            .px_3()
            .py_1()
            .bg(theme.card_bg())
            .border_t_1()
            .border_color(theme.border_subtle())
            .cursor_pointer()
            .hover(|s| s.bg(theme.card_hover()))
            .child(
                div()
                    .text_xs()
                    .text_color(theme.text_dim())
                    .child(caret),
            )
            .child(
                div()
                    .text_xs()
                    .text_color(theme.text_muted())
                    .child("Output"),
            );

        section = section.child(header);

        if self.output_expanded {
            let body = div()
                .w_full()
                .p_3()
                .bg(rgb(0x0a0a0c))
                .border_t_1()
                .border_color(theme.border_subtle())
                .text_xs()
                .text_color(if self.status == ToolStatus::Failed {
                    theme.rose_fg()
                } else {
                    theme.text_muted()
                })
                .child(output.clone());
            section = section.child(body);
        }

        Some(section)
    }

    /// Renders the tool card container.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        let mut card = div()
            .w_full()
            .flex()
            .flex_col()
            .rounded_md()
            .border_1()
            .border_color(theme.border_subtle())
            .bg(theme.card_bg())
            .overflow_hidden()
            .child(self.render_header(theme))
            .child(self.render_input_fold(theme));

        // If diff exists, render diff section
        if let Some(diff_view) = &self.diff {
            card = card.child(
                div()
                    .w_full()
                    .p_2()
                    .border_t_1()
                    .border_color(theme.border_subtle())
                    .child(diff_view.render(theme)),
            );
        }

        // Render output section
        if let Some(output_fold) = self.render_output_fold(theme) {
            card = card.child(output_fold);
        }

        card
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_tool_icons_and_status() {
        let theme = HermesTheme::default();

        assert_eq!(tool_icon("run_command"), "⚡");
        assert_eq!(tool_icon("write_file"), "📝");
        assert_eq!(tool_icon("read_file"), "📄");
        assert_eq!(tool_icon("web_search"), "🌐");
        assert_eq!(tool_icon("unknown_tool"), "🛠️");

        let run = ToolStatus::Running;
        assert_eq!(run.badge_symbol(), "⏳");
        assert_eq!(run.color(&theme), theme.warning_amber());

        let succ = ToolStatus::Success;
        assert_eq!(succ.badge_symbol(), "✓");
        assert_eq!(succ.color(&theme), theme.sage_fg());

        let fail = ToolStatus::Failed;
        assert_eq!(fail.badge_symbol(), "✗");
        assert_eq!(fail.color(&theme), theme.rose_fg());
    }

    #[test]
    fn test_tool_card_builder_and_toggles() {
        let mut card = ToolCallCard::new("tc_1", "run_command", r#"{"cmd":"cargo test"}"#)
            .with_output("test result: ok. 14 passed")
            .with_duration(142);

        assert_eq!(card.tool_name, "run_command");
        assert_eq!(card.status, ToolStatus::Success);
        assert_eq!(card.duration_ms, Some(142));
        assert!(!card.input_expanded);
        assert!(card.output_expanded);

        card.toggle_input();
        assert!(card.input_expanded);

        card.toggle_output();
        assert!(!card.output_expanded);
    }
}

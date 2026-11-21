//! Inline syntax-highlighted git diff visualizer for HEX Desktop.
//!
//! Conforms to Hermes Desktop and HEX truecolor specifications:
//! - Added lines: sage green foreground (`#8fbf93`), 12% tint background (`rgba(0x8fbf93, 0.12)`), `+` gutter prefix.
//! - Deleted lines: rose red foreground (`#c98b8b`), 10% tint background (`rgba(0xc98b8b, 0.10)`), `-` gutter prefix.
//! - Context lines: muted zinc text (`#a1a1aa`), neutral background.
//! - Hunk headers: electric blue (`#7aa7ff`), subtle background.
//! - Dual line numbers: old lineno and new lineno gutters.

use gpui::*;
use hex_protocol::diff::{DiffFile, DiffHunk, DiffLine, DiffLineType, UnifiedDiff};

use crate::theme::HermesTheme;

/// Determines the background tint for a diff line type.
pub fn line_background(line_type: DiffLineType, theme: &HermesTheme) -> Option<Rgba> {
    match line_type {
        DiffLineType::Added => Some(theme.sage_bg()),
        DiffLineType::Deleted => Some(theme.rose_bg()),
        DiffLineType::HunkHeader => Some(theme.card_bg()),
        DiffLineType::Context | DiffLineType::Meta => None,
    }
}

/// Determines the text foreground color for a diff line type.
pub fn line_foreground(line_type: DiffLineType, theme: &HermesTheme) -> Rgba {
    match line_type {
        DiffLineType::Added => theme.sage_fg(),
        DiffLineType::Deleted => theme.rose_fg(),
        DiffLineType::HunkHeader => theme.accent_blue(),
        DiffLineType::Meta => theme.text_dim(),
        DiffLineType::Context => theme.text_muted,
    }
}

/// Formats the gutter prefix symbol for a diff line type.
pub fn line_prefix(line_type: DiffLineType) -> &'static str {
    match line_type {
        DiffLineType::Added => "+",
        DiffLineType::Deleted => "-",
        DiffLineType::HunkHeader => "@@",
        DiffLineType::Context => " ",
        DiffLineType::Meta => "#",
    }
}

/// Inline diff component rendering unified patches.
#[derive(Debug, Clone, PartialEq)]
pub struct DiffView {
    pub diff: UnifiedDiff,
    pub raw_patch: Option<String>,
}

impl DiffView {
    pub fn new(diff: UnifiedDiff) -> Self {
        Self {
            diff,
            raw_patch: None,
        }
    }

    pub fn from_patch(raw: &str) -> Self {
        Self {
            diff: UnifiedDiff::parse(raw),
            raw_patch: Some(raw.to_string()),
        }
    }

    pub fn total_additions(&self) -> usize {
        self.diff.total_additions()
    }

    pub fn total_deletions(&self) -> usize {
        self.diff.total_deletions()
    }

    /// Renders a single diff line with line numbers, gutter symbol, and tinted row.
    fn render_diff_line(&self, line: &DiffLine, theme: &HermesTheme) -> Div {
        let fg = line_foreground(line.line_type, theme);
        let bg = line_background(line.line_type, theme);
        let prefix = line_prefix(line.line_type);

        let old_num_str = line
            .old_lineno
            .map(|n| n.to_string())
            .unwrap_or_default();
        let new_num_str = line
            .new_lineno
            .map(|n| n.to_string())
            .unwrap_or_default();

        let mut row = div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .py(px(1.0))
            .px_2()
            .text_xs();

        if let Some(bg_color) = bg {
            row = row.bg(bg_color);
        }

        row
            // Old line number gutter (40px)
            .child(
                div()
                    .w(px(36.0))
                    .flex()
                    .justify_end()
                    .pr_2()
                    .text_color(theme.mockup_text_dim)
                    .child(old_num_str),
            )
            // New line number gutter (40px)
            .child(
                div()
                    .w(px(36.0))
                    .flex()
                    .justify_end()
                    .pr_2()
                    .text_color(theme.mockup_text_dim)
                    .child(new_num_str),
            )
            // Gutter prefix symbol (+ / - / @@)
            .child(
                div()
                    .w(px(18.0))
                    .flex()
                    .justify_center()
                    .font_weight(FontWeight::BOLD)
                    .text_color(fg)
                    .child(prefix),
            )
            // Line content
            .child(
                div()
                    .flex_1()
                    .overflow_hidden()
                    .text_color(fg)
                    .child(line.content.clone()),
            )
    }

    /// Renders a diff hunk with its header and lines.
    fn render_hunk(&self, hunk: &DiffHunk, theme: &HermesTheme) -> Div {
        let mut hunk_container = div().w_full().flex().flex_col();

        if let Some(heading) = &hunk.heading {
            hunk_container = hunk_container.child(
                div()
                    .w_full()
                    .px_2()
                    .py(px(2.0))
                    .bg(theme.mockup_chip)
                    .text_xs()
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.accent_blue())
                    .child(format!("@@ {}", heading)),
            );
        }

        for line in &hunk.lines {
            hunk_container = hunk_container.child(self.render_diff_line(line, theme));
        }

        hunk_container
    }

    /// Renders a diff file container with its header, stats, and hunks.
    fn render_file(&self, file: &DiffFile, theme: &HermesTheme) -> Div {
        let file_path = file.path().to_string();
        let adds = file.total_additions();
        let dels = file.total_deletions();

        let mut file_container = div()
            .w_full()
            .flex()
            .flex_col()
            .rounded_md()
            .border_1()
            .border_color(theme.mockup_border)
            .bg(theme.mockup_body) // Mockup code surface (#0a0a0a)
            .overflow_hidden();

        // File Header Bar
        file_container = file_container.child(
            div()
                .w_full()
                .flex()
                .flex_row()
                .items_center()
                .justify_between()
                .px_3()
                .py(px(6.0))
                .bg(theme.mockup_surface)
                .border_b_1()
                .border_color(theme.mockup_border)
                .child(
                    div()
                        .flex()
                        .flex_row()
                        .items_center()
                        .gap_2()
                        .child(
                            div()
                                .text_xs()
                                .child("📄"),
                        )
                        .child(
                            div()
                                .text_xs()
                                .font_weight(FontWeight::SEMIBOLD)
                                .text_color(theme.mockup_text_bright)
                                .child(file_path),
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
                                .font_weight(FontWeight::SEMIBOLD)
                                .text_color(theme.sage_fg())
                                .child(format!("+{}", adds)),
                        )
                        .child(
                            div()
                                .font_weight(FontWeight::SEMIBOLD)
                                .text_color(theme.rose_fg())
                                .child(format!("-{}", dels)),
                        ),
                ),
        );

        // Render Hunks
        for hunk in &file.hunks {
            file_container = file_container.child(self.render_hunk(hunk, theme));
        }

        file_container
    }

    /// Renders the complete diff view.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        let mut container = div().w_full().flex().flex_col().gap_3();

        for file in &self.diff.files {
            container = container.child(self.render_file(file, theme));
        }

        container
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    const SAMPLE_PATCH: &str = r#"--- a/src/main.rs
+++ b/src/main.rs
@@ -1,5 +1,6 @@
 fn main() {
-    println!("Hello, old world!");
+    println!("Hello, new world!");
+    println!("Extra line added");
 }
"#;

    #[test]
    fn test_diff_parsing_and_color_tokens() {
        let theme = HermesTheme::default();
        let view = DiffView::from_patch(SAMPLE_PATCH);

        assert_eq!(view.total_additions(), 2);
        assert_eq!(view.total_deletions(), 1);
        assert_eq!(view.diff.files.len(), 1);

        let file = &view.diff.files[0];
        assert_eq!(file.path(), "src/main.rs");

        // Verify color assignments
        assert_eq!(line_foreground(DiffLineType::Added, &theme), theme.sage_fg());
        assert_eq!(line_foreground(DiffLineType::Deleted, &theme), theme.rose_fg());
        assert_eq!(
            line_foreground(DiffLineType::HunkHeader, &theme),
            theme.accent_blue()
        );
        assert_eq!(
            line_foreground(DiffLineType::Context, &theme),
            theme.text_muted()
        );

        // Verify background tints
        assert_eq!(line_background(DiffLineType::Added, &theme), Some(theme.sage_bg()));
        assert_eq!(line_background(DiffLineType::Deleted, &theme), Some(theme.rose_bg()));
        assert_eq!(line_background(DiffLineType::Context, &theme), None);
    }
}

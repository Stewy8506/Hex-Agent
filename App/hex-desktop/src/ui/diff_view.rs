//! Inline git diff visualizer for HEX Desktop, matching the mockup diff card
//! (`docs/design/mockups-desktop`):
//! - Card: `#101010` surface, `#222` border, radius 14, overflow hidden.
//! - Header: mono 12.5, padding 8px 16px, border-bottom `#222`; plain
//!   filename plus `+N` `#8fbf93` / `−N` `#c98b8b` (U+2212).
//! - Body: padding 6px 0; lines mono 12.5/21, padding 0 16px, single gutter
//!   ` {lineno} {±}    {code}` with lineno `#858585`.
//! - Deleted lines: bg `rgba(201,139,139,.09)`, fg `#d4a9a9`; added lines:
//!   bg `rgba(143,191,147,.1)`, fg `#c4e0c7`; context `#d9d9d9`.

use gpui::*;

use hex_protocol::diff::{DiffFile, DiffLine, DiffLineType, UnifiedDiff};

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::payload::{
    copy_button, preview_notice, ScrollablePayload, TextCopyHandler, PAYLOAD_MAX_HEIGHT,
};

/// Diff card corner radius (mockup `border-radius:14px`).
pub const DIFF_CARD_RADIUS: f32 = 14.0;
/// Diff header padding (mockup `padding:8px 16px`).
pub const DIFF_HEADER_PADDING_Y: f32 = 8.0;
pub const DIFF_HEADER_PADDING_X: f32 = 16.0;
/// Diff body vertical padding (mockup `padding:6px 0`).
pub const DIFF_BODY_PADDING_Y: f32 = 6.0;
/// Diff line horizontal padding (mockup `padding:0 16px`).
pub const DIFF_LINE_PADDING_X: f32 = 16.0;
/// Gap between the header's `+N` and `−N` stats.
pub const DIFF_HEADER_STATS_GAP: f32 = 6.0;
/// U+2212 minus used for deletions (mockup uses the typographic minus).
pub const MINUS_SIGN: char = '\u{2212}';
/// Spaces between the gutter symbol and the code (mockup `±    code`).
const GUTTER_CODE_GAP: &str = "    ";
const DIFF_MAX_LINES: usize = 400;

/// Determines the background tint for a diff line type.
pub fn line_background(line_type: DiffLineType, theme: &HermesTheme) -> Option<Rgba> {
    match line_type {
        DiffLineType::Added => Some(theme.mockup_diff_add_bg),
        DiffLineType::Deleted => Some(theme.mockup_diff_del_bg),
        DiffLineType::HunkHeader | DiffLineType::Context | DiffLineType::Meta => None,
    }
}

/// Determines the text foreground color for a diff line type.
pub fn line_foreground(line_type: DiffLineType, theme: &HermesTheme) -> Rgba {
    match line_type {
        DiffLineType::Added => theme.mockup_diff_add_fg,
        DiffLineType::Deleted => theme.mockup_diff_del_fg,
        DiffLineType::HunkHeader | DiffLineType::Meta => theme.mockup_text_dim,
        DiffLineType::Context => theme.mockup_text_base,
    }
}

/// Formats the gutter prefix symbol for a diff line type.
pub fn line_prefix(line_type: DiffLineType) -> &'static str {
    match line_type {
        DiffLineType::Added => "+",
        DiffLineType::Deleted => "−",
        DiffLineType::HunkHeader => "@@",
        DiffLineType::Context => " ",
        DiffLineType::Meta => "#",
    }
}

/// The single line number shown in the gutter: old lineno for deletions, new
/// lineno for additions and context.
fn gutter_lineno(line: &DiffLine) -> Option<usize> {
    match line.line_type {
        DiffLineType::Deleted => line.old_lineno,
        _ => line.new_lineno,
    }
}

/// Formats one diff line as the mockup's single-gutter text:
/// ` {lineno} {±}    {code}`.
pub fn diff_line_text(line: &DiffLine) -> String {
    let lineno = gutter_lineno(line)
        .map(|n| n.to_string())
        .unwrap_or_default();
    format!(
        " {} {}{}{}",
        lineno,
        line_prefix(line.line_type),
        GUTTER_CODE_GAP,
        line.content
    )
}

/// Builds the two shaped runs for one diff line: a `#858585` gutter run
/// (` {lineno} `) and the tinted `±    code` run.
pub fn diff_line_runs(line: &DiffLine, theme: &HermesTheme) -> (String, Vec<TextRun>) {
    let text = diff_line_text(line);
    let lineno = gutter_lineno(line)
        .map(|n| n.to_string())
        .unwrap_or_default();
    let gutter_len = format!(" {} ", lineno).len();
    let mono = gpui::font(theme.font_mono()[0].clone());
    let runs = vec![
        TextRun {
            len: gutter_len,
            font: mono.clone(),
            color: theme.mockup_text_dim.into(),
            background_color: None,
            underline: None,
            strikethrough: None,
        },
        TextRun {
            len: text.len() - gutter_len,
            font: mono,
            color: line_foreground(line.line_type, theme).into(),
            background_color: None,
            underline: None,
            strikethrough: None,
        },
    ];
    (text, runs)
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

    pub fn patch_text(&self) -> String {
        self.raw_patch
            .clone()
            .unwrap_or_else(|| reconstruct_files(&self.diff.files))
    }

    fn file_patch_text(&self, index: usize) -> String {
        if self.diff.files.len() == 1 {
            return self.patch_text();
        }
        self.diff
            .files
            .get(index)
            .map(|file| reconstruct_files(std::slice::from_ref(file)))
            .unwrap_or_default()
    }

    /// Renders a single diff line: full-width tinted row, mono 12.5/21,
    /// whitespace preserved, single gutter.
    fn render_diff_line(&self, line: &DiffLine, theme: &HermesTheme) -> Div {
        let fg = line_foreground(line.line_type, theme);
        let mut row = div()
            .w_full()
            .font_family(theme.font_mono()[0].clone())
            .text_size(px(MockupTypeScale::DIFF_SIZE))
            .line_height(px(MockupTypeScale::DIFF_LINE_HEIGHT))
            .px(px(DIFF_LINE_PADDING_X))
            .whitespace_nowrap()
            .text_color(fg);

        if let Some(bg_color) = line_background(line.line_type, theme) {
            row = row.bg(bg_color);
        }

        let (text, runs) = diff_line_runs(line, theme);
        row.child(StyledText::new(text).with_runs(runs))
    }

    /// Renders one diff file as the mockup diff card.
    fn render_file(
        &self,
        file: &DiffFile,
        index: usize,
        theme: &HermesTheme,
        on_copy: Option<&TextCopyHandler>,
        id_prefix: &str,
    ) -> Div {
        let file_path = file.path().to_string();
        let adds = file.total_additions();
        let dels = file.total_deletions();
        let patch_text = self.file_patch_text(index);

        let header = div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .gap_2()
            .px(px(DIFF_HEADER_PADDING_X))
            .py(px(DIFF_HEADER_PADDING_Y))
            .border_b_1()
            .border_color(theme.mockup_border)
            .font_family(theme.font_mono()[0].clone())
            .text_size(px(MockupTypeScale::DIFF_SIZE))
            .text_color(theme.mockup_text_base)
            .child(div().flex_1().min_w_0().truncate().child(file_path))
            .child(
                div()
                    .flex_none()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap(px(DIFF_HEADER_STATS_GAP))
                    .child(div().text_color(theme.sage_fg()).child(format!("+{adds}")))
                    .child(
                        div()
                            .text_color(theme.rose_fg())
                            .child(format!("{MINUS_SIGN}{dels}")),
                    ),
            )
            .child(copy_button(
                &format!("diff_{id_prefix}_{index}"),
                &patch_text,
                on_copy,
                theme,
            ));

        let total_lines = file
            .hunks
            .iter()
            .map(|hunk| hunk.lines.len())
            .sum::<usize>();
        let visible_lines = if on_copy.is_some() {
            total_lines.min(DIFF_MAX_LINES)
        } else {
            total_lines
        };
        let mut rendered_lines = 0usize;
        let mut body = div()
            .w_full()
            .min_w_0()
            .max_h(px(PAYLOAD_MAX_HEIGHT))
            .scroll_both(&format!("diff_{id_prefix}_{index}_scroll"))
            .flex()
            .flex_col()
            .py(px(DIFF_BODY_PADDING_Y));
        for hunk in &file.hunks {
            if rendered_lines >= visible_lines {
                break;
            }
            if let Some(heading) = &hunk.heading {
                body = body.child(
                    div()
                        .w_full()
                        .px(px(DIFF_LINE_PADDING_X))
                        .font_family(theme.font_mono()[0].clone())
                        .text_size(px(MockupTypeScale::DIFF_SIZE))
                        .line_height(px(MockupTypeScale::DIFF_LINE_HEIGHT))
                        .whitespace_nowrap()
                        .text_color(theme.mockup_text_dim)
                        .child(format!("@@ {heading}")),
                );
            }
            for line in &hunk.lines {
                if rendered_lines >= visible_lines {
                    break;
                }
                body = body.child(self.render_diff_line(line, theme));
                rendered_lines += 1;
            }
        }
        if total_lines > visible_lines {
            body = body.child(preview_notice(theme));
        }

        div()
            .w_full()
            .min_w_0()
            .flex()
            .flex_col()
            .rounded(px(DIFF_CARD_RADIUS))
            .border_1()
            .border_color(theme.mockup_border)
            .bg(theme.mockup_sidebar)
            .overflow_hidden()
            .child(header)
            .child(body)
    }

    pub fn render(&self, theme: &HermesTheme) -> Div {
        self.render_with_copy(theme, None, "diff")
    }

    pub fn render_with_copy(
        &self,
        theme: &HermesTheme,
        on_copy: Option<&TextCopyHandler>,
        id_prefix: &str,
    ) -> Div {
        let mut container = div().w_full().min_w_0().flex().flex_col().gap_3();

        for (index, file) in self.diff.files.iter().enumerate() {
            container = container.child(self.render_file(file, index, theme, on_copy, id_prefix));
        }

        container
    }
}

fn reconstruct_files(files: &[DiffFile]) -> String {
    let mut patch = String::new();
    for file in files {
        let old_path = file.old_path.as_deref().unwrap_or("/dev/null");
        let new_path = file.new_path.as_deref().unwrap_or("/dev/null");
        if !patch.is_empty() {
            patch.push('\n');
        }
        patch.push_str(&format!("diff --git a/{old_path} b/{new_path}\n"));
        if file.is_new {
            patch.push_str("new file mode 100644\n");
        } else if file.is_deleted {
            patch.push_str("deleted file mode 100644\n");
        }
        if file.is_binary {
            patch.push_str(&format!(
                "Binary files a/{old_path} and b/{new_path} differ\n"
            ));
            continue;
        }
        patch.push_str(&format!(
            "--- {}\n+++ {}\n",
            patch_path(old_path, "a"),
            patch_path(new_path, "b")
        ));
        for hunk in &file.hunks {
            patch.push_str(&format!(
                "@@ -{},{} +{},{} @@",
                hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count
            ));
            if let Some(heading) = &hunk.heading {
                patch.push(' ');
                patch.push_str(heading);
            }
            patch.push('\n');
            for line in &hunk.lines {
                match line.line_type {
                    DiffLineType::Added => {
                        patch.push('+');
                        patch.push_str(&line.content);
                    }
                    DiffLineType::Deleted => {
                        patch.push('-');
                        patch.push_str(&line.content);
                    }
                    DiffLineType::Context => {
                        patch.push(' ');
                        patch.push_str(&line.content);
                    }
                    DiffLineType::Meta => {
                        patch.push_str(&line.content);
                    }
                    DiffLineType::HunkHeader => {
                        patch.push_str(&line.content);
                    }
                }
                patch.push('\n');
            }
        }
    }
    patch
}

fn patch_path(path: &str, prefix: &str) -> String {
    if path == "/dev/null" {
        path.to_string()
    } else {
        format!("{prefix}/{path}")
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

        // Mockup color assignments (plan §2.1).
        assert_eq!(
            line_foreground(DiffLineType::Added, &theme),
            theme.mockup_diff_add_fg
        );
        assert_eq!(
            line_foreground(DiffLineType::Deleted, &theme),
            theme.mockup_diff_del_fg
        );
        assert_eq!(
            line_foreground(DiffLineType::Context, &theme),
            theme.mockup_text_base
        );
        assert_eq!(
            line_foreground(DiffLineType::HunkHeader, &theme),
            theme.mockup_text_dim
        );

        // Mockup background tints.
        assert_eq!(
            line_background(DiffLineType::Added, &theme),
            Some(theme.mockup_diff_add_bg)
        );
        assert_eq!(
            line_background(DiffLineType::Deleted, &theme),
            Some(theme.mockup_diff_del_bg)
        );
        assert_eq!(line_background(DiffLineType::Context, &theme), None);
    }

    #[test]
    fn test_diff_line_single_gutter_format() {
        let view = DiffView::from_patch(SAMPLE_PATCH);
        let hunk = &view.diff.files[0].hunks[0];

        let deleted = hunk
            .lines
            .iter()
            .find(|l| l.line_type == DiffLineType::Deleted)
            .expect("patch has a deleted line");
        let added = hunk
            .lines
            .iter()
            .find(|l| l.line_type == DiffLineType::Added)
            .expect("patch has added lines");

        let del_text = diff_line_text(deleted);
        let add_text = diff_line_text(added);
        // 4-space separator gap plus the source's own 4-space indent.
        assert_eq!(del_text, " 2 −        println!(\"Hello, old world!\");");
        assert_eq!(add_text, " 2 +        println!(\"Hello, new world!\");");
        // The second added line gets the next new-file lineno.
        let added_second = hunk
            .lines
            .iter()
            .filter(|l| l.line_type == DiffLineType::Added)
            .nth(1)
            .unwrap();
        assert!(diff_line_text(added_second).starts_with(" 3 +"));

        // U+2212 minus, never the ASCII hyphen, for deletions.
        assert_eq!(line_prefix(DiffLineType::Deleted), "−");
        assert_eq!(MINUS_SIGN, '\u{2212}');
    }

    #[test]
    fn test_diff_line_runs_split_gutter_and_code() {
        let theme = HermesTheme::default();
        let view = DiffView::from_patch(SAMPLE_PATCH);
        let hunk = &view.diff.files[0].hunks[0];
        let added = hunk
            .lines
            .iter()
            .find(|l| l.line_type == DiffLineType::Added)
            .unwrap();

        let (text, runs) = diff_line_runs(added, &theme);
        let total: usize = runs.iter().map(|r| r.len).sum();
        assert_eq!(total, text.len(), "runs must cover the whole line");
        assert_eq!(runs.len(), 2, "gutter run + code run");

        let gutter_rgb = gpui::Rgba::from(runs[0].color);
        assert_eq!((gutter_rgb.r * 255.0).round() as u8, 0x85);
        assert_eq!((gutter_rgb.g * 255.0).round() as u8, 0x85);
        let code_rgb = gpui::Rgba::from(runs[1].color);
        assert_eq!((code_rgb.r * 255.0).round() as u8, 0xc4);
        // Mono font on both runs.
        assert_eq!(runs[0].font.family.as_ref(), "Geist Mono");
        assert_eq!(runs[1].font.family.as_ref(), "Geist Mono");
    }

    #[test]
    fn test_diff_render_smoke() {
        let theme = HermesTheme::default();
        let view = DiffView::from_patch(SAMPLE_PATCH);
        let _ = view.render(&theme);
    }

    #[test]
    fn test_patch_copy_uses_raw_or_reconstructed_text() {
        let raw = DiffView::from_patch(SAMPLE_PATCH);
        assert_eq!(raw.patch_text(), SAMPLE_PATCH);

        let reconstructed = DiffView::new(raw.diff.clone());
        let patch = reconstructed.patch_text();
        assert!(patch.contains("--- a/src/main.rs\n+++ b/src/main.rs\n"));
        assert!(patch.contains("-    println!(\"Hello, old world!\");"));
        assert!(patch.contains("+    println!(\"Hello, new world!\");"));
    }
}

//! Unified diff parser for git diff patches.
//!
//! Parses unified diff strings into structured files, hunks, and lines
//! with accurate old/new line numbers and side-by-side alignment support.

use std::fmt;
use serde::{Deserialize, Serialize};

/// Type of line in a unified diff hunk.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DiffLineType {
    Context,
    Added,
    Deleted,
    HunkHeader,
    Meta,
}

impl fmt::Display for DiffLineType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DiffLineType::Context => write!(f, "context"),
            DiffLineType::Added => write!(f, "added"),
            DiffLineType::Deleted => write!(f, "deleted"),
            DiffLineType::HunkHeader => write!(f, "hunk_header"),
            DiffLineType::Meta => write!(f, "meta"),
        }
    }
}

/// A single line in a diff hunk.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DiffLine {
    pub line_type: DiffLineType,
    pub content: String,
    pub old_lineno: Option<usize>,
    pub new_lineno: Option<usize>,
}

impl DiffLine {
    pub fn is_addition(&self) -> bool {
        self.line_type == DiffLineType::Added
    }

    pub fn is_deletion(&self) -> bool {
        self.line_type == DiffLineType::Deleted
    }

    pub fn is_context(&self) -> bool {
        self.line_type == DiffLineType::Context
    }
}

/// A hunk within a diff file.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DiffHunk {
    pub old_start: usize,
    pub old_count: usize,
    pub new_start: usize,
    pub new_count: usize,
    pub heading: Option<String>,
    pub lines: Vec<DiffLine>,
}

impl DiffHunk {
    pub fn additions(&self) -> usize {
        self.lines.iter().filter(|l| l.is_addition()).count()
    }

    pub fn deletions(&self) -> usize {
        self.lines.iter().filter(|l| l.is_deletion()).count()
    }
}

/// A parsed file within a diff.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DiffFile {
    pub old_path: Option<String>,
    pub new_path: Option<String>,
    pub is_new: bool,
    pub is_deleted: bool,
    pub is_binary: bool,
    pub hunks: Vec<DiffHunk>,
}

impl DiffFile {
    pub fn path(&self) -> &str {
        self.new_path
            .as_deref()
            .or(self.old_path.as_deref())
            .unwrap_or("unknown")
    }

    pub fn total_additions(&self) -> usize {
        self.hunks.iter().map(|h| h.additions()).sum()
    }

    pub fn total_deletions(&self) -> usize {
        self.hunks.iter().map(|h| h.deletions()).sum()
    }
}

/// A row in a side-by-side diff view.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SideBySideRow {
    pub left_lineno: Option<usize>,
    pub left_content: Option<String>,
    pub left_type: Option<DiffLineType>,
    pub right_lineno: Option<usize>,
    pub right_content: Option<String>,
    pub right_type: Option<DiffLineType>,
}

/// A parsed unified diff document containing zero or more files.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct UnifiedDiff {
    pub files: Vec<DiffFile>,
}

impl UnifiedDiff {
    pub fn is_empty(&self) -> bool {
        self.files.is_empty()
    }

    pub fn total_additions(&self) -> usize {
        self.files.iter().map(|f| f.total_additions()).sum()
    }

    pub fn total_deletions(&self) -> usize {
        self.files.iter().map(|f| f.total_deletions()).sum()
    }

    /// Parse a unified diff string.
    pub fn parse(raw: &str) -> Self {
        let mut files = Vec::new();
        let mut current_file: Option<DiffFile> = None;
        let mut current_hunk: Option<DiffHunk> = None;

        let mut old_lineno = 0usize;
        let mut new_lineno = 0usize;

        for line in raw.lines() {
            // Check for file header: `diff --git a/... b/...`
            if line.starts_with("diff --git ") {
                if let Some(hunk) = current_hunk.take() {
                    if let Some(f) = current_file.as_mut() {
                        f.hunks.push(hunk);
                    }
                }
                if let Some(f) = current_file.take() {
                    files.push(f);
                }

                // Extract file paths from "diff --git a/foo b/foo"
                let parts: Vec<&str> = line.split_whitespace().collect();
                let (old_p, new_p) = if parts.len() >= 4 {
                    let old_raw = parts[2].strip_prefix("a/").unwrap_or(parts[2]);
                    let new_raw = parts[3].strip_prefix("b/").unwrap_or(parts[3]);
                    (Some(old_raw.to_string()), Some(new_raw.to_string()))
                } else {
                    (None, None)
                };

                current_file = Some(DiffFile {
                    old_path: old_p,
                    new_path: new_p,
                    is_new: false,
                    is_deleted: false,
                    is_binary: false,
                    hunks: Vec::new(),
                });
                continue;
            }

            // Check for new file / deleted file markers
            if line.starts_with("new file mode ") {
                if let Some(f) = current_file.as_mut() {
                    f.is_new = true;
                }
                continue;
            }
            if line.starts_with("deleted file mode ") {
                if let Some(f) = current_file.as_mut() {
                    f.is_deleted = true;
                }
                continue;
            }
            if line.starts_with("Binary files ") {
                if let Some(f) = current_file.as_mut() {
                    f.is_binary = true;
                }
                continue;
            }

            // Check for --- a/file or --- /dev/null
            if line.starts_with("--- ") {
                let path = line[4..].trim();
                let p = if path == "/dev/null" {
                    None
                } else {
                    Some(path.strip_prefix("a/").unwrap_or(path).to_string())
                };

                if current_file.is_none() {
                    current_file = Some(DiffFile {
                        old_path: p,
                        new_path: None,
                        is_new: false,
                        is_deleted: false,
                        is_binary: false,
                        hunks: Vec::new(),
                    });
                } else if let Some(f) = current_file.as_mut() {
                    if f.old_path.is_none() || f.old_path.as_deref() == Some("/dev/null") {
                        f.old_path = p;
                    }
                }
                continue;
            }

            // Check for +++ b/file or +++ /dev/null
            if line.starts_with("+++ ") {
                let path = line[4..].trim();
                let p = if path == "/dev/null" {
                    None
                } else {
                    Some(path.strip_prefix("b/").unwrap_or(path).to_string())
                };

                if let Some(f) = current_file.as_mut() {
                    f.new_path = p;
                    if f.old_path.is_none() && f.new_path.is_some() {
                        f.is_new = true;
                    } else if f.new_path.is_none() && f.old_path.is_some() {
                        f.is_deleted = true;
                    }
                }
                continue;
            }

            // Check for hunk header: @@ -old_start,old_count +new_start,new_count @@
            if line.starts_with("@@ ") {
                if let Some(hunk) = current_hunk.take() {
                    if let Some(f) = current_file.as_mut() {
                        f.hunks.push(hunk);
                    }
                }

                if let Some((old_start, old_count, new_start, new_count, heading)) = parse_hunk_header(line) {
                    old_lineno = old_start;
                    new_lineno = new_start;

                    current_hunk = Some(DiffHunk {
                        old_start,
                        old_count,
                        new_start,
                        new_count,
                        heading,
                        lines: Vec::new(),
                    });
                }
                continue;
            }

            // Lines inside a hunk
            if let Some(hunk) = current_hunk.as_mut() {
                if line.starts_with('+') {
                    hunk.lines.push(DiffLine {
                        line_type: DiffLineType::Added,
                        content: line[1..].to_string(),
                        old_lineno: None,
                        new_lineno: Some(new_lineno),
                    });
                    new_lineno += 1;
                } else if line.starts_with('-') {
                    hunk.lines.push(DiffLine {
                        line_type: DiffLineType::Deleted,
                        content: line[1..].to_string(),
                        old_lineno: Some(old_lineno),
                        new_lineno: None,
                    });
                    old_lineno += 1;
                } else if line.starts_with(' ') || line.is_empty() {
                    let content = if line.starts_with(' ') {
                        &line[1..]
                    } else {
                        line
                    };
                    hunk.lines.push(DiffLine {
                        line_type: DiffLineType::Context,
                        content: content.to_string(),
                        old_lineno: Some(old_lineno),
                        new_lineno: Some(new_lineno),
                    });
                    old_lineno += 1;
                    new_lineno += 1;
                } else if line.starts_with(r"\ No newline at end of file") {
                    hunk.lines.push(DiffLine {
                        line_type: DiffLineType::Meta,
                        content: line.to_string(),
                        old_lineno: None,
                        new_lineno: None,
                    });
                }
            }
        }

        if let Some(hunk) = current_hunk.take() {
            if let Some(f) = current_file.as_mut() {
                f.hunks.push(hunk);
            }
        }
        if let Some(f) = current_file.take() {
            files.push(f);
        }

        Self { files }
    }

    /// Converts a file diff into aligned side-by-side rows for split-view presentation.
    pub fn side_by_side(&self, file_idx: usize) -> Vec<SideBySideRow> {
        let file = match self.files.get(file_idx) {
            Some(f) => f,
            None => return Vec::new(),
        };

        let mut rows = Vec::new();

        for hunk in &file.hunks {
            let mut i = 0;
            while i < hunk.lines.len() {
                let line = &hunk.lines[i];

                if line.is_context() {
                    rows.push(SideBySideRow {
                        left_lineno: line.old_lineno,
                        left_content: Some(line.content.clone()),
                        left_type: Some(DiffLineType::Context),
                        right_lineno: line.new_lineno,
                        right_content: Some(line.content.clone()),
                        right_type: Some(DiffLineType::Context),
                    });
                    i += 1;
                } else {
                    // Accumulate consecutive deletions and additions
                    let mut dels = Vec::new();
                    let mut adds = Vec::new();

                    while i < hunk.lines.len() && (hunk.lines[i].is_deletion() || hunk.lines[i].is_addition()) {
                        if hunk.lines[i].is_deletion() {
                            dels.push(&hunk.lines[i]);
                        } else {
                            adds.push(&hunk.lines[i]);
                        }
                        i += 1;
                    }

                    let max_len = dels.len().max(adds.len());
                    for j in 0..max_len {
                        let left = dels.get(j);
                        let right = adds.get(j);

                        rows.push(SideBySideRow {
                            left_lineno: left.and_then(|l| l.old_lineno),
                            left_content: left.map(|l| l.content.clone()),
                            left_type: left.map(|l| l.line_type),
                            right_lineno: right.and_then(|r| r.new_lineno),
                            right_content: right.map(|r| r.content.clone()),
                            right_type: right.map(|r| r.line_type),
                        });
                    }
                }
            }
        }

        rows
    }
}

/// Parse `@@ -old_start,old_count +new_start,new_count @@ heading`
fn parse_hunk_header(line: &str) -> Option<(usize, usize, usize, usize, Option<String>)> {
    let parts: Vec<&str> = line.split("@@").collect();
    if parts.len() < 3 {
        return None;
    }

    let header_content = parts[1].trim();
    let heading = if !parts[2].trim().is_empty() {
        Some(parts[2].trim().to_string())
    } else {
        None
    };

    let spec_parts: Vec<&str> = header_content.split_whitespace().collect();
    if spec_parts.len() < 2 {
        return None;
    }

    let old_spec = spec_parts[0].strip_prefix('-')?;
    let new_spec = spec_parts[1].strip_prefix('+')?;

    let (old_start, old_count) = parse_range(old_spec);
    let (new_start, new_count) = parse_range(new_spec);

    Some((old_start, old_count, new_start, new_count, heading))
}

fn parse_range(spec: &str) -> (usize, usize) {
    if let Some((start, count)) = spec.split_once(',') {
        let s = start.parse::<usize>().unwrap_or(1);
        let c = count.parse::<usize>().unwrap_or(1);
        (s, c)
    } else {
        let s = spec.parse::<usize>().unwrap_or(1);
        (s, 1)
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE_DIFF: &str = r#"diff --git a/src/main.rs b/src/main.rs
index 1234567..89abcdef 100644
--- a/src/main.rs
+++ b/src/main.rs
@@ -1,5 +1,6 @@ fn main()
 fn main() {
-    println!("hello");
+    println!("hello world");
+    println!("second line");
 }
"#;

    #[test]
    fn test_parse_sample_diff() {
        let diff = UnifiedDiff::parse(SAMPLE_DIFF);
        assert_eq!(diff.files.len(), 1);

        let file = &diff.files[0];
        assert_eq!(file.path(), "src/main.rs");
        assert_eq!(file.total_additions(), 2);
        assert_eq!(file.total_deletions(), 1);

        assert_eq!(file.hunks.len(), 1);
        let hunk = &file.hunks[0];
        assert_eq!(hunk.old_start, 1);
        assert_eq!(hunk.old_count, 5);
        assert_eq!(hunk.new_start, 1);
        assert_eq!(hunk.new_count, 6);
        assert_eq!(hunk.heading.as_deref(), Some("fn main()"));

        // Check line numbers
        let del = hunk.lines.iter().find(|l| l.is_deletion()).unwrap();
        assert_eq!(del.content, "    println!(\"hello\");");
        assert_eq!(del.old_lineno, Some(2));
        assert_eq!(del.new_lineno, None);

        let adds: Vec<_> = hunk.lines.iter().filter(|l| l.is_addition()).collect();
        assert_eq!(adds.len(), 2);
        assert_eq!(adds[0].new_lineno, Some(2));
        assert_eq!(adds[1].new_lineno, Some(3));
    }

    #[test]
    fn test_side_by_side_alignment() {
        let diff = UnifiedDiff::parse(SAMPLE_DIFF);
        let rows = diff.side_by_side(0);

        assert!(!rows.is_empty());
        // First line: fn main() { (context)
        assert_eq!(rows[0].left_type, Some(DiffLineType::Context));
        assert_eq!(rows[0].right_type, Some(DiffLineType::Context));

        // Replacement lines
        let mod_row = &rows[1];
        assert_eq!(mod_row.left_type, Some(DiffLineType::Deleted));
        assert_eq!(mod_row.right_type, Some(DiffLineType::Added));
    }

    #[test]
    fn test_multi_file_diff() {
        let multi = r#"diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
+new
diff --git a/b.txt b/b.txt
new file mode 100644
--- /dev/null
+++ b/b.txt
@@ -0,0 +1 @@
+created
"#;

        let diff = UnifiedDiff::parse(multi);
        assert_eq!(diff.files.len(), 2);
        assert_eq!(diff.files[0].path(), "a.txt");
        assert_eq!(diff.files[1].path(), "b.txt");
        assert!(diff.files[1].is_new);
        assert_eq!(diff.total_additions(), 2);
        assert_eq!(diff.total_deletions(), 1);
    }
}

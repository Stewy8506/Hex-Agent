//! Agent Client Protocol (ACP) JSON-RPC 2.0 client, models, and diff parser
//! for the HEX Autonomous AI Workbench.

pub mod diff;
pub mod models;

pub use diff::{DiffFile, DiffHunk, DiffLine, DiffLineType, SideBySideRow, UnifiedDiff};
pub use models::*;

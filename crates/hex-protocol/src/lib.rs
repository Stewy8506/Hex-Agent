//! Agent Client Protocol (ACP) JSON-RPC 2.0 client, models, and diff parser
//! for the HEX Autonomous AI Workbench.

pub mod client;
pub mod diff;
pub mod error;
pub mod models;

pub use client::AcpClient;
pub use diff::{DiffFile, DiffHunk, DiffLine, DiffLineType, SideBySideRow, UnifiedDiff};
pub use error::AcpError;
pub use models::*;

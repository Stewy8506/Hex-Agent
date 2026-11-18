//! HEX Terminal Cockpit: Ratatui + Crossterm architecture.

pub mod app;
pub mod event;
pub mod history;
pub mod sidebar;
pub mod ui;
#[cfg(windows)]
mod windows_input;

pub use history::{TranscriptPage, TranscriptStore, TranscriptSummary};

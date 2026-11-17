//! HEX Terminal Cockpit: Ratatui + Crossterm architecture.

pub mod app;
pub mod event;
pub mod history;
pub mod ui;

pub use history::{TranscriptPage, TranscriptStore, TranscriptSummary};

//! Error types for ACP client and protocol communication.

use thiserror::Error;
use crate::models::JsonRpcError;

#[derive(Debug, Error)]
pub enum AcpError {
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),

    #[error("JSON serialization/deserialization error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("JSON-RPC error: {0}")]
    Rpc(#[from] JsonRpcError),

    #[error("Connection closed prematurely")]
    ConnectionClosed,

    #[error("Request timed out")]
    Timeout,

    #[error("Response channel dropped before receiving response")]
    ChannelClosed,

    #[error("Invalid response from server: {0}")]
    InvalidResponse(String),

    #[error("Process exited with status code: {0:?}")]
    ProcessExited(Option<i32>),
}

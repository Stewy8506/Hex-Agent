//! Error types for ACP client and protocol communication.

use thiserror::Error;
use crate::models::{JsonRpcError, JSONRPC_CONFIG_REVISION_CONFLICT};

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

impl AcpError {
    pub fn is_config_revision_conflict(&self) -> bool {
        matches!(
            self,
            Self::Rpc(error) if error.code == JSONRPC_CONFIG_REVISION_CONFLICT
        )
    }

    pub fn config_revision_conflict_revision(&self) -> Option<&str> {
        match self {
            Self::Rpc(error) if error.code == JSONRPC_CONFIG_REVISION_CONFLICT => error
                .data
                .as_ref()?
                .get("actualRevision")?
                .as_str(),
            _ => None,
        }
    }
}

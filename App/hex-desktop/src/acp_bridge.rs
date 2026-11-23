//! Asynchronous ACP JSON-RPC 2.0 Bridge connecting HEX Desktop to `harness_core`.
//!
//! Provides bidirectional communication with the autonomous AI runtime:
//! - Connection management: spawned Python ACP child process over stdio
//!   (a Unix-socket connect helper exists for forward compatibility)
//! - Client capabilities handshake via `initialize`
//! - Outbound commands: `new_session`, `send_prompt`, `cancel_session`, `send_approval_decision`
//! - Inbound notification processing: `session/update` (text deltas, nested tool
//!   lifecycle, `stream_end`, terminal `status`) and `session/approvalRequest`
//! - Automatic reconnect loop with exponential backoff

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use hex_protocol::client::AcpClient;
use hex_protocol::error::AcpError;
use hex_protocol::models::{
    ApprovalDecisionResponse, ApprovalRequestNotification, ClientInfo,
    JsonRpcNotification, SessionUpdateNotification,
};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::{broadcast, Mutex};

use crate::ui::modals::approval::ToolApprovalChoice;

/// High-level ACP events dispatched from backend to the desktop cockpit UI.
#[derive(Debug, Clone, PartialEq)]
pub enum AcpEvent {
    /// Handshake completed and client initialized
    Connected {
        server_name: String,
        server_version: String,
    },
    /// Connection to ACP backend was interrupted or closed
    Disconnected { reason: String },
    /// Live streaming text chunk for a conversation session
    TokenChunk { session_id: String, text: String },
    /// Turn generation completed for the active prompt
    TurnComplete { session_id: String },
    /// Token usage of the completed turn (terminal status `usage` payload);
    /// drives the context gauge. Emitted right after `TurnComplete`.
    TurnUsage {
        session_id: String,
        input_tokens: u64,
        output_tokens: u64,
        model_id: Option<String>,
        context_limit: Option<u64>,
    },
    /// Agent initiated a tool call
    ToolCall {
        session_id: String,
        call_id: String,
        tool_name: String,
        arguments_json: String,
    },
    /// Tool execution finished with result or error
    ToolResult {
        session_id: String,
        call_id: String,
        output_text: String,
        is_error: bool,
    },
    /// Runtime requests operator approval for privileged tool execution
    ApprovalRequest {
        session_id: String,
        approval_id: String,
        tool_name: String,
        risk_level: String,
        command: String,
        description: Option<String>,
    },
    /// Provider status or health update
    StatusUpdate {
        provider_name: String,
        is_healthy: bool,
        latency_ms: Option<u64>,
    },
    /// Cloud account lifecycle change pushed by the backend (`cloud/event`)
    CloudAccount(hex_protocol::models::CloudEvent),
}

/// ACP client bridge connecting the desktop cockpit to `harness_core`.
#[derive(Clone)]
pub struct AcpBridge {
    client: Arc<Mutex<Option<AcpClient>>>,
    event_tx: broadcast::Sender<AcpEvent>,
    is_connected: Arc<AtomicBool>,
    workspace_root: Option<PathBuf>,
}

impl std::fmt::Debug for AcpBridge {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AcpBridge")
            .field("is_connected", &self.is_connected())
            .field("workspace_root", &self.workspace_root)
            .finish()
    }
}

impl PartialEq for AcpBridge {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.client, &other.client)
    }
}

impl Default for AcpBridge {
    fn default() -> Self {
        Self::new()
    }
}

impl AcpBridge {
    /// Construct a new detached ACP bridge.
    pub fn new() -> Self {
        let (event_tx, _) = broadcast::channel(512);
        Self {
            client: Arc::new(Mutex::new(None)),
            event_tx,
            is_connected: Arc::new(AtomicBool::new(false)),
            workspace_root: None,
        }
    }

    /// Construct bridge with an explicit workspace directory root.
    pub fn with_workspace(workspace_root: impl Into<PathBuf>) -> Self {
        let (event_tx, _) = broadcast::channel(512);
        Self {
            client: Arc::new(Mutex::new(None)),
            event_tx,
            is_connected: Arc::new(AtomicBool::new(false)),
            workspace_root: Some(workspace_root.into()),
        }
    }

    /// Subscribe to high-level UI events (tokens, tool cards, approvals).
    pub fn subscribe(&self) -> broadcast::Receiver<AcpEvent> {
        self.event_tx.subscribe()
    }

    /// Check whether the bridge is actively connected.
    pub fn is_connected(&self) -> bool {
        self.is_connected.load(Ordering::SeqCst)
    }

    /// Initialize bridge wrapping an existing initialized `AcpClient`.
    pub fn from_client(client: AcpClient) -> Self {
        let bridge = Self::new();
        if let Ok(mut lock) = bridge.client.try_lock() {
            *lock = Some(client.clone());
        }
        let mut notif_rx = client.subscribe();
        let mut closed_rx = client.subscribe_closed();
        let event_tx = bridge.event_tx.clone();
        let is_connected = Arc::clone(&bridge.is_connected);
        is_connected.store(true, Ordering::SeqCst);
        let _ = event_tx.send(AcpEvent::Connected {
            server_name: "HEX Runtime".to_string(),
            server_version: "unknown".to_string(),
        });
        let demux_tx = event_tx.clone();
        tokio::spawn(async move {
            loop {
                match notif_rx.recv().await {
                    Ok(notif) => Self::dispatch_notification(&notif, &demux_tx),
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                }
            }
        });
        let close_tx = event_tx.clone();
        let close_connected = Arc::clone(&is_connected);
        tokio::spawn(async move {
            if closed_rx.recv().await.is_ok() {
                close_connected.store(false, Ordering::SeqCst);
                let _ = close_tx.send(AcpEvent::Disconnected {
                    reason: "ACP connection closed".to_string(),
                });
            }
        });
        bridge
    }

    /// Connect directly via generic async streams (e.g. duplex pipes in tests).
    pub async fn connect_streams<R, W>(&self, reader: R, writer: W) -> Result<(), AcpError>
    where
        R: AsyncRead + Send + Unpin + 'static,
        W: AsyncWrite + Send + Unpin + 'static,
    {
        let client = AcpClient::from_streams(reader, writer);
        let resp = client
            .initialize(Some(ClientInfo {
                name: "HEX Desktop Cockpit".to_string(),
                version: env!("CARGO_PKG_VERSION").to_string(),
            }))
            .await?;

        let server_name = resp.server_info.name.clone();
        let server_version = resp.server_info.version.clone();

        self.attach_client(client, &server_name, &server_version).await;
        Ok(())
    }

    /// Connect to local ACP Unix domain socket (e.g. `/tmp/hex-acp.sock`).
    pub async fn connect_socket(&self, socket_path: &Path) -> Result<(), AcpError> {
        #[cfg(unix)]
        {
            let stream = tokio::net::UnixStream::connect(socket_path).await?;
            let (reader, writer) = stream.into_split();
            self.connect_streams(reader, writer).await
        }
        #[cfg(not(unix))]
        {
            let _ = socket_path;
            Err(AcpError::Io(std::io::Error::new(
                std::io::ErrorKind::Unsupported,
                "Unix domain sockets are not available on this platform",
            )))
        }
    }

    /// Spawn the Python backend ACP server via stdio (`python -m harness_core.client.cli acp`).
    pub async fn connect_server(&self) -> Result<(), AcpError> {
        let client = AcpClient::spawn_acp_server(self.workspace_root.as_deref()).await?;
        let resp = client
            .initialize(Some(ClientInfo {
                name: "HEX Desktop Cockpit".to_string(),
                version: env!("CARGO_PKG_VERSION").to_string(),
            }))
            .await?;

        let server_name = resp.server_info.name.clone();
        let server_version = resp.server_info.version.clone();

        self.attach_client(client, &server_name, &server_version).await;
        Ok(())
    }

    /// Automatically connect by spawning the trusted ACP child process over stdio.
    ///
    /// B34: auto-connect previously adopted a predictable `/tmp/hex-acp.sock`
    /// whenever it existed, without authenticating the peer. Any local process
    /// able to create that socket could impersonate the backend and receive
    /// prompts and approval decisions. Auto-connect now only spawns the child we
    /// control; `connect_socket` remains an explicit, deliberate caller choice.
    pub async fn connect_auto(&self) -> Result<(), AcpError> {
        self.connect_server().await
    }

    /// Attach active client and spawn notification subscriber task.
    async fn attach_client(&self, client: AcpClient, server_name: &str, server_version: &str) {
        let mut notif_rx = client.subscribe();
        let mut closed_rx = client.subscribe_closed();
        let event_tx = self.event_tx.clone();
        let is_connected = Arc::clone(&self.is_connected);

        {
            let mut lock = self.client.lock().await;
            *lock = Some(client);
        }

        is_connected.store(true, Ordering::SeqCst);
        let _ = event_tx.send(AcpEvent::Connected {
            server_name: server_name.to_string(),
            server_version: server_version.to_string(),
        });

        // Demux JSON-RPC notifications into AcpEvent. A lagged broadcast is not a
        // disconnect; keep demuxing. Transport loss is announced by closed_rx.
        let demux_tx = event_tx.clone();
        tokio::spawn(async move {
            loop {
                match notif_rx.recv().await {
                    Ok(notif) => Self::dispatch_notification(&notif, &demux_tx),
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                }
            }
        });

        // Authoritative disconnect: fires once when the transport reader ends.
        let close_tx = event_tx.clone();
        let close_connected = Arc::clone(&is_connected);
        tokio::spawn(async move {
            if closed_rx.recv().await.is_ok() {
                close_connected.store(false, Ordering::SeqCst);
                let _ = close_tx.send(AcpEvent::Disconnected {
                    reason: "ACP connection closed".to_string(),
                });
            }
        });
    }

    /// Parse and dispatch incoming ACP JSON-RPC notification into typed AcpEvent.
    ///
    /// Speaks the canonical server wire contract (see
    /// `hex_protocol::models::SessionUpdateNotification`): text deltas, nested
    /// tool lifecycle payloads, `stream_end`, and terminal `status` updates all
    /// arrive as `session/update` notifications.
    pub fn dispatch_notification(notif: &JsonRpcNotification, event_tx: &broadcast::Sender<AcpEvent>) {
        match notif.method.as_str() {
            "session/update" => {
                if let Some(ref params_val) = notif.params {
                    if let Ok(update) = serde_json::from_value::<SessionUpdateNotification>(params_val.clone()) {
                        if let Some(delta) = update.delta.clone() {
                            if !delta.is_empty() {
                                let _ = event_tx.send(AcpEvent::TokenChunk {
                                    session_id: update.session_id.clone(),
                                    text: delta,
                                });
                            }
                        }

                        if update.update_type == "tool" {
                            if let Some(tool_val) = update.tool.clone() {
                                if let Ok(tool) = serde_json::from_value::<hex_protocol::models::ToolUpdate>(tool_val) {
                                    match tool.event_type.as_str() {
                                        "start" => {
                                            let _ = event_tx.send(AcpEvent::ToolCall {
                                                session_id: update.session_id.clone(),
                                                call_id: tool.call_id,
                                                tool_name: tool.tool_id,
                                                arguments_json: tool.payload,
                                            });
                                        }
                                        "complete" | "error" => {
                                            let _ = event_tx.send(AcpEvent::ToolResult {
                                                session_id: update.session_id.clone(),
                                                call_id: tool.call_id,
                                                output_text: tool.payload,
                                                is_error: tool.event_type == "error",
                                            });
                                        }
                                        _ => {}
                                    }
                                }
                            }
                        }

                        if update.is_turn_terminal() {
                            if let Some(usage) = update.usage.clone() {
                                let _ = event_tx.send(AcpEvent::TurnUsage {
                                    session_id: update.session_id.clone(),
                                    input_tokens: usage.input_tokens,
                                    output_tokens: usage.output_tokens,
                                    model_id: usage.model_id,
                                    context_limit: usage.context_limit,
                                });
                            }
                            let _ = event_tx.send(AcpEvent::TurnComplete {
                                session_id: update.session_id,
                            });
                        }
                    }
                }
            }
            "session/approvalRequest" | "approval/request" => {
                if let Some(ref params_val) = notif.params {
                    if let Ok(appr) = serde_json::from_value::<ApprovalRequestNotification>(params_val.clone()) {
                        let _ = event_tx.send(AcpEvent::ApprovalRequest {
                            session_id: appr.session_id,
                            approval_id: appr.approval_id,
                            tool_name: appr.tool_name,
                            risk_level: if appr.risk_level.is_empty() { "R3".to_string() } else { appr.risk_level },
                            command: appr.parameters,
                            description: if appr.reason.is_empty() { None } else { Some(appr.reason) },
                        });
                    }
                }
            }
            "status" | "provider/status" => {
                if let Some(ref params) = notif.params {
                    let provider_name = params.get("provider").and_then(|v| v.as_str()).unwrap_or("HEX Agent").to_string();
                    let is_healthy = params.get("isHealthy").or_else(|| params.get("connected")).and_then(|v| v.as_bool()).unwrap_or(true);
                    let latency_ms = params.get("latencyMs").and_then(|v| v.as_u64());

                    let _ = event_tx.send(AcpEvent::StatusUpdate {
                        provider_name,
                        is_healthy,
                        latency_ms,
                    });
                }
            }
            "cloud/event" => {
                if let Some(ref params_val) = notif.params {
                    if let Ok(event) = serde_json::from_value::<hex_protocol::models::CloudEvent>(params_val.clone()) {
                        let _ = event_tx.send(AcpEvent::CloudAccount(event));
                    }
                }
            }
            _ => {}
        }
    }

    /// Create a new session in `harness_core`, returning the full response
    /// (session id, cwd, model, provider) so the UI can show real identity.
    pub async fn new_session_full(
        &self,
        cwd: Option<&str>,
        model: Option<&str>,
    ) -> Result<hex_protocol::models::SessionNewResponse, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.session_new(cwd, model).await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Create a new session in `harness_core`.
    pub async fn new_session(&self, cwd: Option<&str>, model: Option<&str>) -> Result<String, AcpError> {
        Ok(self.new_session_full(cwd, model).await?.session_id)
    }

    /// Load a durable session with its paged history (B28 recovery, resume).
    pub async fn load_session_paged(
        &self,
        session_id: &str,
    ) -> Result<hex_protocol::models::PagedSessionLoad, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.session_load_paged(session_id).await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// List durable backend sessions, including inactive on-disk ones.
    pub async fn list_sessions(&self) -> Result<Vec<hex_protocol::models::SessionListItem>, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.session_list().await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Fetch the live model catalog from the backend's configured providers (B32).
    pub async fn list_models(
        &self,
        session_id: &str,
    ) -> Result<hex_protocol::models::ModelCatalog, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.session_models(session_id).await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Apply a model selection to the session via the backend `/model` command.
    /// Success carries the confirmed canonical model/provider identity.
    pub async fn set_session_model(
        &self,
        session_id: &str,
        model_id: &str,
    ) -> Result<hex_protocol::models::SessionCommandResponse, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.session_command(session_id, "model", Some(model_id)).await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Fetch local cloud account/device status (`cloud/auth/status`).
    pub async fn cloud_auth_status(
        &self,
    ) -> Result<hex_protocol::models::CloudAuthStatus, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.cloud_auth_status().await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Register a new cloud account (`cloud/auth/register`).
    pub async fn cloud_auth_register(
        &self,
        email: &str,
        password: &str,
    ) -> Result<serde_json::Value, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.cloud_auth_register(email, password).await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Sign in and enroll or reuse this device (`cloud/auth/login`).
    pub async fn cloud_auth_login(
        &self,
        email: &str,
        password: &str,
    ) -> Result<hex_protocol::models::CloudAuthStatus, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.cloud_auth_login(email, password).await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Sign out on this device (`cloud/auth/logout`).
    pub async fn cloud_auth_logout(
        &self,
    ) -> Result<hex_protocol::models::CloudAuthStatus, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.cloud_auth_logout().await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// List enrolled devices (`cloud/devices/list`).
    pub async fn cloud_devices_list(
        &self,
        include_revoked: bool,
    ) -> Result<Vec<hex_protocol::models::CloudDevice>, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.cloud_devices_list(include_revoked).await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Revoke an enrolled device (`cloud/devices/revoke`).
    pub async fn cloud_devices_revoke(
        &self,
        device_record_id: &str,
    ) -> Result<hex_protocol::models::CloudRevokeResult, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.cloud_devices_revoke(device_record_id).await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Check hex-backend for signed component updates (`cloud/update/check`).
    pub async fn cloud_update_check(
        &self,
    ) -> Result<hex_protocol::models::CloudUpdatePlan, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.cloud_update_check().await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Fetch the verified remote-config bundle (`cloud/config/evaluate`).
    pub async fn cloud_config_evaluate(
        &self,
    ) -> Result<hex_protocol::models::CloudFlags, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.cloud_config_evaluate().await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Fetch one validated history page for a turn field ("prompt"/"response").
    pub async fn history_content_page(
        &self,
        session_id: &str,
        turn_index: usize,
        field: &str,
        offset: usize,
    ) -> Result<hex_protocol::models::ToolResultPage, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.session_history_content(session_id, turn_index, field, offset).await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Send a user prompt to the active session and return the authoritative
    /// final result (status, content, taskId) once the turn completes.
    pub async fn send_prompt(
        &self,
        session_id: &str,
        prompt: &str,
    ) -> Result<hex_protocol::models::SessionPromptResponse, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.session_prompt(session_id, prompt).await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Cancel active generation on a session.
    pub async fn cancel_session(&self, session_id: &str) -> Result<(), AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            client.session_cancel(session_id).await?;
            Ok(())
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Send operator approval decision back to `harness_core`.
    ///
    /// B27/B30: decisions use the server's canonical vocabulary and the lease
    /// TTL travels as a validated field, never as reason text. A scoped lease
    /// only accompanies `Session`; once-off approvals never widen authority.
    pub async fn send_approval_decision(
        &self,
        approval_id: &str,
        choice: ToolApprovalChoice,
        lease_ttl_seconds: Option<u64>,
    ) -> Result<ApprovalDecisionResponse, AcpError> {
        let client_opt = self.client.lock().await.clone();
        if let Some(client) = client_opt {
            let decision_str = match choice {
                ToolApprovalChoice::Once => "approve",
                ToolApprovalChoice::Session => "approve_scope",
                ToolApprovalChoice::Deny => "deny",
            };
            let ttl = match choice {
                ToolApprovalChoice::Session => lease_ttl_seconds,
                _ => None,
            };
            client
                .session_approval_decision_with_ttl(approval_id, decision_str, None, ttl)
                .await
        } else {
            Err(AcpError::ConnectionClosed)
        }
    }

    /// Assemble the authoritative final answer for a completed turn.
    ///
    /// B31: when the server truncated the inline response it advertises
    /// `turnIndex`/`responseChars`; the full text is fetched page by page and
    /// validated so a lagged stream can never be presented as the final answer.
    pub async fn fetch_canonical_final(
        &self,
        session_id: &str,
        response: &hex_protocol::models::SessionPromptResponse,
    ) -> Result<String, AcpError> {
        let client_opt = self.client.lock().await.clone();
        let Some(client) = client_opt else {
            return Err(AcpError::ConnectionClosed);
        };
        match (response.turn_index, response.response_chars) {
            (Some(turn), Some(total)) => {
                let mut text = String::new();
                let mut offset = 0usize;
                loop {
                    let page = client
                        .session_history_content(session_id, turn, "response", offset)
                        .await?;
                    let end = offset
                        .checked_add(page.content.chars().count())
                        .ok_or_else(|| AcpError::InvalidResponse("response page overflow".into()))?;
                    if page.total_chars != total
                        || end > total
                        || (end < total && end == offset)
                        || page.next_offset != (end < total).then_some(end)
                    {
                        return Err(AcpError::InvalidResponse(
                            "invalid canonical response page".into(),
                        ));
                    }
                    text.push_str(&page.content);
                    if end == total {
                        return Ok(text);
                    }
                    offset = end;
                }
            }
            (None, None) => Ok(response.text_content()),
            _ => Err(AcpError::InvalidResponse(
                "incomplete final-response paging metadata".into(),
            )),
        }
    }

    /// Retry connecting with exponential backoff until the transport is up.
    ///
    /// B28: the caller supervises this loop and re-arms it on every later
    /// disconnect; session recovery happens after it returns.
    pub async fn reconnect_with_backoff(&self, max_delay: Duration) {
        let mut delay = Duration::from_millis(500);
        while !self.is_connected() {
            tokio::time::sleep(delay).await;
            // Another path may have reconnected while we slept; never clobber it.
            if self.is_connected() {
                return;
            }
            if self.connect_auto().await.is_ok() {
                return;
            }
            delay = (delay * 2).min(max_delay);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

    #[tokio::test]
    async fn test_acp_bridge_handshake_and_session() {
        let (client_read, mut server_write) = tokio::io::duplex(4096);
        let (server_read, client_write) = tokio::io::duplex(4096);

        let bridge = AcpBridge::new();
        let mut events = bridge.subscribe();

        // Spawn mock ACP server
        tokio::spawn(async move {
            let mut lines = BufReader::new(server_read).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let req: serde_json::Value = serde_json::from_str(&line).unwrap();
                let method = req.get("method").and_then(|v| v.as_str()).unwrap();
                let id = req.get("id").cloned();

                match method {
                    "initialize" => {
                        let resp = serde_json::json!({
                            "jsonrpc": "2.0",
                            "id": id,
                            "result": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {
                                    "session": {
                                        "streaming": true,
                                        "cancel": true,
                                        "history": true
                                    },
                                    "tools": {
                                        "listChanged": false
                                    }
                                },
                                "serverInfo": {
                                    "name": "harness_core_mock",
                                    "version": "1.0.0"
                                }
                            }
                        });
                        server_write.write_all((serde_json::to_string(&resp).unwrap() + "\n").as_bytes()).await.unwrap();
                    }
                    "session/new" => {
                        let resp = serde_json::json!({
                            "jsonrpc": "2.0",
                            "id": id,
                            "result": {
                                "sessionId": "ses_mock_123",
                                "cwd": "/workspace",
                                "model": "claude-3-7-sonnet"
                            }
                        });
                        server_write.write_all((serde_json::to_string(&resp).unwrap() + "\n").as_bytes()).await.unwrap();
                    }
                    "session/approvalDecision" => {
                        let resp = serde_json::json!({
                            "jsonrpc": "2.0",
                            "id": id,
                            "result": {
                                "approvalId": "appr_123",
                                "status": "acknowledged",
                                "decision": "approve"
                            }
                        });
                        server_write.write_all((serde_json::to_string(&resp).unwrap() + "\n").as_bytes()).await.unwrap();
                    }
                    _ => {}
                }
            }
        });

        // Connect streams
        bridge.connect_streams(client_read, client_write).await.expect("connect failed");
        assert!(bridge.is_connected());

        // Verify connected event received
        let evt = events.recv().await.unwrap();
        assert_eq!(
            evt,
            AcpEvent::Connected {
                server_name: "harness_core_mock".to_string(),
                server_version: "1.0.0".to_string(),
            }
        );

        // Verify new_session
        let session_id = bridge.new_session(None, None).await.unwrap();
        assert_eq!(session_id, "ses_mock_123");

        // Verify approval decision
        let appr_resp = bridge
            .send_approval_decision("appr_123", ToolApprovalChoice::Once, None)
            .await
            .unwrap();
        assert_eq!(appr_resp.approval_id, "appr_123");
        assert_eq!(appr_resp.decision, "approve");
    }

    #[test]
    fn test_dispatch_notification_to_events() {
        let (tx, mut rx) = broadcast::channel(16);

        // 1. Token chunk (canonical server shape: updateType=agent_response)
        let notif = JsonRpcNotification {
            jsonrpc: "2.0".to_string(),
            method: "session/update".to_string(),
            params: Some(serde_json::json!({
                "sessionId": "ses_1",
                "updateType": "agent_response",
                "delta": "Hello from ACP"
            })),
        };
        AcpBridge::dispatch_notification(&notif, &tx);
        let evt = rx.try_recv().unwrap();
        assert_eq!(
            evt,
            AcpEvent::TokenChunk {
                session_id: "ses_1".to_string(),
                text: "Hello from ACP".to_string(),
            }
        );

        // 2. Approval Request
        let notif_appr = JsonRpcNotification {
            jsonrpc: "2.0".to_string(),
            method: "session/approvalRequest".to_string(),
            params: Some(serde_json::json!({
                "sessionId": "ses_1",
                "approvalId": "appr_99",
                "toolName": "bash",
                "riskLevel": "R4",
                "parameters": "rm -rf build/",
                "reason": "Deleting build artifacts"
            })),
        };
        AcpBridge::dispatch_notification(&notif_appr, &tx);
        let evt_appr = rx.try_recv().unwrap();
        assert_eq!(
            evt_appr,
            AcpEvent::ApprovalRequest {
                session_id: "ses_1".to_string(),
                approval_id: "appr_99".to_string(),
                tool_name: "bash".to_string(),
                risk_level: "R4".to_string(),
                command: "rm -rf build/".to_string(),
                description: Some("Deleting build artifacts".to_string()),
            }
        );
    }

    /// Load a wire fixture captured from the real Python ACP server
    /// (tests/fixtures/acp_wire, regenerated by tests/client/test_acp_wire_contract.py).
    fn wire_fixture(name: &str) -> Vec<serde_json::Value> {
        let path = format!(
            "{}/../../tests/fixtures/acp_wire/{}",
            env!("CARGO_MANIFEST_DIR"),
            name
        );
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("missing wire fixture {path}: {e}"));
        text.lines()
            .filter(|ln| !ln.trim().is_empty())
            .map(|ln| serde_json::from_str(ln).unwrap())
            .collect()
    }

    fn dispatch_fixture(name: &str) -> (Vec<AcpEvent>, hex_protocol::models::SessionPromptResponse) {
        let lines = wire_fixture(name);
        let (tx, mut rx) = broadcast::channel(64);
        let mut final_response = None;
        for line in &lines {
            if line.get("method").is_some() {
                let notif: JsonRpcNotification = serde_json::from_value(line.clone()).unwrap();
                AcpBridge::dispatch_notification(&notif, &tx);
            } else {
                final_response = Some(
                    serde_json::from_value::<hex_protocol::models::SessionPromptResponse>(
                        line.get("result").cloned().unwrap(),
                    )
                    .unwrap(),
                );
            }
        }
        let mut events = Vec::new();
        while let Ok(evt) = rx.try_recv() {
            events.push(evt);
        }
        (events, final_response.expect("fixture must end with a prompt response"))
    }

    #[test]
    fn test_real_server_streamed_turn_fixture() {
        let (events, resp) = dispatch_fixture("turn_streamed.jsonl");
        assert_eq!(events.len(), 6, "events: {events:?}");
        assert_eq!(
            events[0],
            AcpEvent::TokenChunk { session_id: "ses_fixture".into(), text: "Hello".into() }
        );
        assert_eq!(
            events[1],
            AcpEvent::TokenChunk { session_id: "ses_fixture".into(), text: " world".into() }
        );
        let call_id = match &events[2] {
            AcpEvent::ToolCall { call_id, tool_name, arguments_json, .. } => {
                assert_eq!(tool_name, "list_files");
                assert!(arguments_json.contains("path"));
                call_id.clone()
            }
            other => panic!("expected ToolCall, got {other:?}"),
        };
        match &events[3] {
            AcpEvent::ToolResult { call_id: result_call_id, output_text, is_error, .. } => {
                assert_eq!(result_call_id, &call_id, "tool result must correlate with its start");
                assert!(output_text.contains("a.py"));
                assert!(!is_error);
            }
            other => panic!("expected ToolResult, got {other:?}"),
        }
        assert_eq!(events[4], AcpEvent::TurnComplete { session_id: "ses_fixture".into() });
        assert_eq!(events[5], AcpEvent::TurnComplete { session_id: "ses_fixture".into() });

        assert!(resp.is_completed());
        assert_eq!(resp.text_content(), "Fixture answer.");
        assert_eq!(resp.task_id.as_deref(), Some("tsk_fixture"));
    }

    #[test]
    fn test_real_server_nonstreamed_turn_fixture() {
        let (events, resp) = dispatch_fixture("turn_nonstreamed.jsonl");
        assert_eq!(events.len(), 2, "events: {events:?}");
        assert_eq!(
            events[0],
            AcpEvent::TokenChunk { session_id: "ses_fixture".into(), text: "Fixture answer.".into() }
        );
        assert_eq!(events[1], AcpEvent::TurnComplete { session_id: "ses_fixture".into() });
        assert!(resp.is_completed());
        assert_eq!(resp.text_content(), "Fixture answer.");
    }

    async fn bridge_with_history_pages(
        pages: Vec<serde_json::Value>,
    ) -> (AcpBridge, std::sync::Arc<std::sync::Mutex<Vec<(usize, String)>>>) {
        let (client_read, mut server_write) = tokio::io::duplex(65536);
        let (server_read, client_write) = tokio::io::duplex(65536);
        let requests = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let log = requests.clone();

        tokio::spawn(async move {
            let mut pages = pages.into_iter();
            let mut lines = BufReader::new(server_read).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let req: serde_json::Value = serde_json::from_str(&line).unwrap();
                let method = req.get("method").and_then(|v| v.as_str()).unwrap();
                let id = req.get("id").cloned();
                let result = match method {
                    "initialize" => serde_json::json!({
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "session": { "streaming": true, "cancel": true, "history": true },
                            "tools": { "listChanged": false }
                        },
                        "serverInfo": { "name": "harness_core_mock", "version": "1.0.0" }
                    }),
                    "session/historyContent" => {
                        let params = req.get("params").unwrap();
                        let offset = params.get("offset").and_then(|v| v.as_u64()).unwrap() as usize;
                        let field = params.get("field").and_then(|v| v.as_str()).unwrap().to_string();
                        log.lock().unwrap().push((offset, field));
                        // An unexpected extra request yields an undecodable page
                        // so the fetch fails fast instead of hanging.
                        pages.next().unwrap_or(serde_json::Value::Null)
                    }
                    _ => continue,
                };
                let resp = serde_json::json!({ "jsonrpc": "2.0", "id": id, "result": result });
                server_write
                    .write_all((serde_json::to_string(&resp).unwrap() + "\n").as_bytes())
                    .await
                    .unwrap();
            }
        });

        let bridge = AcpBridge::new();
        bridge.connect_streams(client_read, client_write).await.unwrap();
        (bridge, requests)
    }

    fn paged_prompt_response(
        turn: Option<usize>,
        total: Option<usize>,
        preview: &str,
    ) -> hex_protocol::models::SessionPromptResponse {
        hex_protocol::models::SessionPromptResponse {
            session_id: "ses_page".into(),
            status: "completed".into(),
            content: vec![hex_protocol::models::ContentBlock::text(preview)],
            task_id: None,
            response_chars: total,
            turn_index: turn,
        }
    }

    #[tokio::test]
    async fn test_fetch_canonical_final_assembles_validated_pages() {
        let (bridge, requests) = bridge_with_history_pages(vec![
            serde_json::json!({ "content": "01234", "nextOffset": 5, "totalChars": 10 }),
            serde_json::json!({ "content": "56789", "nextOffset": null, "totalChars": 10 }),
        ])
        .await;

        let resp = paged_prompt_response(Some(2), Some(10), "01234");
        let text = bridge.fetch_canonical_final("ses_page", &resp).await.unwrap();
        assert_eq!(text, "0123456789");
        assert_eq!(
            *requests.lock().unwrap(),
            vec![(0, "response".to_string()), (5, "response".to_string())]
        );
    }

    #[tokio::test]
    async fn test_fetch_canonical_final_rejects_inconsistent_total() {
        let (bridge, _) = bridge_with_history_pages(vec![serde_json::json!({
            "content": "01234", "nextOffset": 5, "totalChars": 9
        })])
        .await;

        let resp = paged_prompt_response(Some(2), Some(10), "01234");
        let err = bridge.fetch_canonical_final("ses_page", &resp).await.unwrap_err();
        assert!(matches!(err, AcpError::InvalidResponse(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn test_fetch_canonical_final_rejects_stalled_page() {
        let (bridge, _) = bridge_with_history_pages(vec![serde_json::json!({
            "content": "", "nextOffset": null, "totalChars": 10
        })])
        .await;

        let resp = paged_prompt_response(Some(2), Some(10), "");
        let err = bridge.fetch_canonical_final("ses_page", &resp).await.unwrap_err();
        assert!(matches!(err, AcpError::InvalidResponse(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn test_fetch_canonical_final_rejects_missing_next_offset() {
        let (bridge, _) = bridge_with_history_pages(vec![serde_json::json!({
            "content": "01234", "nextOffset": null, "totalChars": 10
        })])
        .await;

        let resp = paged_prompt_response(Some(2), Some(10), "01234");
        let err = bridge.fetch_canonical_final("ses_page", &resp).await.unwrap_err();
        assert!(matches!(err, AcpError::InvalidResponse(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn test_fetch_canonical_final_rejects_mixed_metadata() {
        let (bridge, requests) = bridge_with_history_pages(vec![]).await;

        let resp = paged_prompt_response(Some(2), None, "preview");
        let err = bridge.fetch_canonical_final("ses_page", &resp).await.unwrap_err();
        assert!(matches!(err, AcpError::InvalidResponse(_)), "got {err:?}");
        assert!(requests.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn test_fetch_canonical_final_uses_inline_text_without_metadata() {
        let (bridge, requests) = bridge_with_history_pages(vec![]).await;

        let resp = paged_prompt_response(None, None, "inline answer");
        let text = bridge.fetch_canonical_final("ses_page", &resp).await.unwrap();
        assert_eq!(text, "inline answer");
        assert!(requests.lock().unwrap().is_empty());
    }
}

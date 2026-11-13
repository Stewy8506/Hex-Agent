//! Asynchronous JSON-RPC 2.0 stdio client for ACP server communication.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde::Serialize;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::{broadcast, mpsc, oneshot, Mutex};
use tokio::time::timeout;

use crate::error::AcpError;
use crate::models::{
    ApprovalDecisionParams, ApprovalDecisionResponse, ClientInfo, InitializeParams,
    InitializeResponse, JsonRpcNotification, JsonRpcRequest, RequestId, SessionCancelParams,
    SessionCancelResponse, SessionCommandParams, SessionCommandResponse, SessionListItem,
    SessionListResponse, SessionLoadParams, SessionLoadResponse, SessionNewParams,
    SessionNewResponse, SessionPromptParams, SessionPromptResponse, ToolCallParams,
    ToolCallResponse, ToolDefinition, ToolsListResponse,
};

type PendingMap = Arc<Mutex<HashMap<u64, oneshot::Sender<Result<serde_json::Value, AcpError>>>>>;

/// Asynchronous ACP JSON-RPC 2.0 client.
#[derive(Clone)]
pub struct AcpClient {
    next_id: Arc<AtomicU64>,
    writer_tx: mpsc::Sender<String>,
    pending: PendingMap,
    notifications_tx: broadcast::Sender<JsonRpcNotification>,
    child: Option<Arc<Mutex<tokio::process::Child>>>,
    default_timeout: Duration,
}

impl AcpClient {
    /// Connect to arbitrary async read/write streams (e.g. duplex pipes for testing).
    pub fn from_streams<R, W>(reader: R, mut writer: W) -> Self
    where
        R: AsyncRead + Send + Unpin + 'static,
        W: AsyncWrite + Send + Unpin + 'static,
    {
        let next_id = Arc::new(AtomicU64::new(1));
        let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));
        let (writer_tx, mut writer_rx) = mpsc::channel::<String>(128);
        let (notifications_tx, _) = broadcast::channel::<JsonRpcNotification>(256);

        // Background writer task
        tokio::spawn(async move {
            while let Some(line) = writer_rx.recv().await {
                if writer.write_all(line.as_bytes()).await.is_err() {
                    break;
                }
                if writer.flush().await.is_err() {
                    break;
                }
            }
        });

        // Background reader task
        let pending_clone = Arc::clone(&pending);
        let notif_tx = notifications_tx.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(reader).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }

                // Parse incoming JSON
                let val: serde_json::Value = match serde_json::from_str(trimmed) {
                    Ok(v) => v,
                    Err(e) => {
                        eprintln!("[hex-protocol] Failed to parse JSON-RPC line: {} ({})", trimmed, e);
                        continue;
                    }
                };

                // Check if this is a response (has id)
                if let Some(id_val) = val.get("id") {
                    if !id_val.is_null() {
                        let id_u64 = if let Some(n) = id_val.as_u64() {
                            Some(n)
                        } else if let Some(s) = id_val.as_str() {
                            s.parse::<u64>().ok()
                        } else {
                            None
                        };

                        if let Some(req_id) = id_u64 {
                            let mut map = pending_clone.lock().await;
                            if let Some(sender) = map.remove(&req_id) {
                                if let Some(err_val) = val.get("error").filter(|e| !e.is_null()) {
                                    match serde_json::from_value::<crate::models::JsonRpcError>(err_val.clone()) {
                                        Ok(rpc_err) => {
                                            let _ = sender.send(Err(AcpError::Rpc(rpc_err)));
                                        }
                                        Err(e) => {
                                            let _ = sender.send(Err(AcpError::Json(e)));
                                        }
                                    }
                                } else if let Some(res_val) = val.get("result") {
                                    let _ = sender.send(Ok(res_val.clone()));
                                } else {
                                    let _ = sender.send(Ok(serde_json::Value::Null));
                                }
                            }
                        }
                        continue;
                    }
                }

                // Check if this is a notification (no id, but method)
                if let Some(method_val) = val.get("method").and_then(|m| m.as_str()) {
                    let notification = JsonRpcNotification {
                        jsonrpc: "2.0".to_string(),
                        method: method_val.to_string(),
                        params: val.get("params").cloned(),
                    };
                    let _ = notif_tx.send(notification);
                }
            }

            // Connection ended; reject any pending requests
            let mut map = pending_clone.lock().await;
            for (_, sender) in map.drain() {
                let _ = sender.send(Err(AcpError::ConnectionClosed));
            }
        });

        Self {
            next_id,
            writer_tx,
            pending,
            notifications_tx,
            child: None,
            default_timeout: Duration::from_secs(60),
        }
    }

    /// Spawn a child process and communicate via stdio.
    pub async fn spawn(
        command: &str,
        args: &[&str],
        cwd: Option<&Path>,
    ) -> Result<Self, AcpError> {
        let mut cmd = Command::new(command);
        cmd.args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        if let Some(dir) = cwd {
            cmd.current_dir(dir);
        }

        let mut child = cmd.spawn()?;
        let stdout = child.stdout.take().ok_or_else(|| {
            AcpError::Io(std::io::Error::new(
                std::io::ErrorKind::BrokenPipe,
                "Failed to capture child stdout",
            ))
        })?;
        let stdin = child.stdin.take().ok_or_else(|| {
            AcpError::Io(std::io::Error::new(
                std::io::ErrorKind::BrokenPipe,
                "Failed to capture child stdin",
            ))
        })?;

        let mut client = Self::from_streams(stdout, stdin);
        client.child = Some(Arc::new(Mutex::new(child)));
        Ok(client)
    }

    /// Helper to locate repository root containing Python virtual environment and src/
    fn resolve_repo_root(workspace_root: Option<&Path>) -> PathBuf {
        if let Ok(env_root) = std::env::var("HEX_REPO_ROOT") {
            let p = PathBuf::from(env_root);
            if p.exists() {
                return p;
            }
        }

        let start_path = workspace_root
            .map(|p| p.to_path_buf())
            .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));

        // 1. Walk up from start_path to find repository root with src/harness_core
        let mut curr = start_path.clone();
        loop {
            if curr.join("src/harness_core").exists() || (curr.join("pyproject.toml").exists() && curr.join(".venv").exists()) {
                return curr;
            }
            if let Some(parent) = curr.parent() {
                curr = parent.to_path_buf();
            } else {
                break;
            }
        }

        // 2. Walk up from current_exe (e.g. repo/target/debug/hex-tui)
        if let Ok(exe_path) = std::env::current_exe() {
            let mut curr = exe_path;
            while let Some(parent) = curr.parent() {
                curr = parent.to_path_buf();
                if curr.join("src/harness_core").exists() || (curr.join("pyproject.toml").exists() && curr.join(".venv").exists()) {
                    return curr;
                }
            }
        }

        // 3. Fallback: check git root
        let mut curr = start_path.clone();
        loop {
            if curr.join(".git").exists() || curr.join("pyproject.toml").exists() {
                return curr;
            }
            if let Some(parent) = curr.parent() {
                curr = parent.to_path_buf();
            } else {
                break;
            }
        }

        start_path
    }

    /// Spawn the Python backend ACP server (`python3 -m harness_core.cli acp`).
    pub async fn spawn_acp_server(workspace_root: Option<&Path>) -> Result<Self, AcpError> {
        let repo_root = Self::resolve_repo_root(workspace_root);

        let py_bin = if let Ok(bin_env) = std::env::var("HEX_PYTHON_BIN") {
            bin_env
        } else if let Ok(venv) = std::env::var("VIRTUAL_ENV") {
            format!("{}/bin/python", venv)
        } else if repo_root.join(".venv/bin/python").exists() {
            repo_root.join(".venv/bin/python").to_string_lossy().to_string()
        } else {
            "python3".to_string()
        };

        let mut cmd = Command::new(&py_bin);
        cmd.args(["-m", "harness_core.cli", "acp"])
            .current_dir(&repo_root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        let src_dir = repo_root.join("src");
        if src_dir.exists() {
            let current_pythonpath = std::env::var("PYTHONPATH").unwrap_or_default();
            let new_pythonpath = if current_pythonpath.is_empty() {
                src_dir.to_string_lossy().to_string()
            } else {
                format!("{}:{}", src_dir.to_string_lossy(), current_pythonpath)
            };
            cmd.env("PYTHONPATH", new_pythonpath);
        }

        let mut child = cmd.spawn()?;

        // Capture child stderr quietly so it does not corrupt the alternate-screen terminal
        if let Some(child_stderr) = child.stderr.take() {
            tokio::spawn(async move {
                let mut lines = BufReader::new(child_stderr).lines();
                while let Ok(Some(_line)) = lines.next_line().await {
                    // Silently consume to prevent pipe buffer stalls
                }
            });
        }

        let stdout = child.stdout.take().ok_or_else(|| {
            AcpError::Io(std::io::Error::new(
                std::io::ErrorKind::BrokenPipe,
                "Failed to capture child stdout",
            ))
        })?;
        let stdin = child.stdin.take().ok_or_else(|| {
            AcpError::Io(std::io::Error::new(
                std::io::ErrorKind::BrokenPipe,
                "Failed to capture child stdin",
            ))
        })?;

        let mut client = Self::from_streams(stdout, stdin);
        client.child = Some(Arc::new(Mutex::new(child)));
        Ok(client)
    }

    /// Set default request timeout.
    pub fn set_timeout(&mut self, duration: Duration) {
        self.default_timeout = duration;
    }

    /// Subscribe to server notifications (e.g. streaming tokens, tool progress).
    pub fn subscribe(&self) -> broadcast::Receiver<JsonRpcNotification> {
        self.notifications_tx.subscribe()
    }

    /// Send a raw JSON-RPC request and await response.
    pub async fn send_request<Req: Serialize, Resp: DeserializeOwned>(
        &self,
        method: &str,
        params: Req,
    ) -> Result<Resp, AcpError> {
        let req_id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let params_val = serde_json::to_value(params)?;

        let request = JsonRpcRequest::new(
            RequestId::Number(req_id as i64),
            method,
            Some(params_val),
        );

        let (tx, rx) = oneshot::channel();
        {
            let mut map = self.pending.lock().await;
            map.insert(req_id, tx);
        }

        let serialized = serde_json::to_string(&request)? + "\n";
        if self.writer_tx.send(serialized).await.is_err() {
            let mut map = self.pending.lock().await;
            map.remove(&req_id);
            return Err(AcpError::ConnectionClosed);
        }

        let res_val = match timeout(self.default_timeout, rx).await {
            Ok(Ok(Ok(val))) => val,
            Ok(Ok(Err(acp_err))) => return Err(acp_err),
            Ok(Err(_)) => {
                let mut map = self.pending.lock().await;
                map.remove(&req_id);
                return Err(AcpError::ChannelClosed);
            }
            Err(_) => {
                let mut map = self.pending.lock().await;
                map.remove(&req_id);
                return Err(AcpError::Timeout);
            }
        };

        let parsed: Resp = serde_json::from_value(res_val)?;
        Ok(parsed)
    }

    /// Send a notification (no response expected).
    pub async fn send_notification<P: Serialize>(
        &self,
        method: &str,
        params: P,
    ) -> Result<(), AcpError> {
        let params_val = serde_json::to_value(params)?;
        let notif = JsonRpcRequest::notification(method, Some(params_val));
        let serialized = serde_json::to_string(&notif)? + "\n";
        self.writer_tx
            .send(serialized)
            .await
            .map_err(|_| AcpError::ConnectionClosed)?;
        Ok(())
    }

    // -----------------------------------------------------------------------
    // High-level ACP Protocol Methods
    // -----------------------------------------------------------------------

    /// Initialize the ACP session and negotiate capabilities.
    pub async fn initialize(&self, client_info: Option<ClientInfo>) -> Result<InitializeResponse, AcpError> {
        let params = InitializeParams {
            protocol_version: Some("2024-11-05".to_string()),
            client_info: client_info.or_else(|| {
                Some(ClientInfo {
                    name: "HEX-Rust-Cockpit".to_string(),
                    version: env!("CARGO_PKG_VERSION").to_string(),
                })
            }),
            capabilities: None,
        };
        self.send_request("initialize", params).await
    }

    /// Create a new editor session.
    pub async fn session_new(
        &self,
        cwd: Option<&str>,
        model: Option<&str>,
    ) -> Result<SessionNewResponse, AcpError> {
        let params = SessionNewParams {
            cwd: cwd.map(|s| s.to_string()),
            root_uri: None,
            model: model.map(|s| s.to_string()),
        };
        self.send_request("session/new", params).await
    }

    /// Load an existing session.
    pub async fn session_load(&self, session_id: &str) -> Result<SessionLoadResponse, AcpError> {
        let params = SessionLoadParams {
            session_id: session_id.to_string(),
        };
        self.send_request("session/load", params).await
    }

    /// Send a prompt to the active session.
    pub async fn session_prompt(
        &self,
        session_id: &str,
        prompt_text: &str,
    ) -> Result<SessionPromptResponse, AcpError> {
        let params = SessionPromptParams::new_text(session_id, prompt_text);
        self.send_request("session/prompt", params).await
    }

    /// Cancel prompt execution on an active session.
    pub async fn session_cancel(&self, session_id: &str) -> Result<SessionCancelResponse, AcpError> {
        let params = SessionCancelParams {
            session_id: session_id.to_string(),
        };
        self.send_request("session/cancel", params).await
    }

    /// List all active sessions.
    pub async fn session_list(&self) -> Result<Vec<SessionListItem>, AcpError> {
        let resp: SessionListResponse = self
            .send_request("session/list", serde_json::json!({}))
            .await?;
        Ok(resp.sessions)
    }

    /// Query available tools from the ACP server.
    pub async fn tools_list(&self) -> Result<Vec<ToolDefinition>, AcpError> {
        let resp: ToolsListResponse = self
            .send_request("tools/list", serde_json::json!({}))
            .await?;
        Ok(resp.tools)
    }

    /// Invoke a tool directly.
    pub async fn tools_call(
        &self,
        name: &str,
        arguments: serde_json::Value,
    ) -> Result<ToolCallResponse, AcpError> {
        let params = ToolCallParams {
            name: name.to_string(),
            arguments,
        };
        self.send_request("tools/call", params).await
    }

    /// Send an operator approval decision to the ACP server.
    pub async fn session_approval_decision(
        &self,
        approval_id: &str,
        decision: &str,
        reason: Option<&str>,
    ) -> Result<ApprovalDecisionResponse, AcpError> {
        let params = ApprovalDecisionParams {
            approval_id: approval_id.to_string(),
            decision: decision.to_string(),
            reason: reason.map(|s| s.to_string()),
        };
        self.send_request("session/approvalDecision", params).await
    }

    /// Forward a slash command to the ACP server for execution.
    pub async fn session_command(
        &self,
        session_id: &str,
        command: &str,
        args: Option<&str>,
    ) -> Result<SessionCommandResponse, AcpError> {
        let params = SessionCommandParams {
            session_id: session_id.to_string(),
            command: command.to_string(),
            args: args.map(|s| s.to_string()),
        };
        self.send_request("session/command", params).await
    }

    /// Ping server for liveness check.
    pub async fn ping(&self) -> Result<bool, AcpError> {
        let resp: serde_json::Value = self
            .send_request("ping", serde_json::json!({}))
            .await?;
        Ok(resp.get("pong").and_then(|v| v.as_bool()).unwrap_or(true))
    }

    /// Terminate spawned child process if applicable.
    pub async fn close(&self) -> Result<(), AcpError> {
        if let Some(child_arc) = &self.child {
            let mut guard = child_arc.lock().await;
            let _ = guard.kill().await;
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::AsyncWriteExt;

    #[tokio::test]
    async fn test_client_request_response_flow() {
        // Create in-memory bidirectional duplex channels
        let (client_read, mut server_write) = tokio::io::duplex(4096);
        let (server_read, client_write) = tokio::io::duplex(4096);

        let client = AcpClient::from_streams(client_read, client_write);

        // Spawn mock server handler
        tokio::spawn(async move {
            let mut lines = BufReader::new(server_read).lines();
            if let Ok(Some(line)) = lines.next_line().await {
                let req: JsonRpcRequest = serde_json::from_str(&line).unwrap();
                assert_eq!(req.method, "ping");

                let resp = serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": req.id,
                    "result": {"pong": true}
                });
                let resp_str = serde_json::to_string(&resp).unwrap() + "\n";
                server_write.write_all(resp_str.as_bytes()).await.unwrap();
            }
        });

        let is_pong = client.ping().await.unwrap();
        assert!(is_pong);
    }

    #[tokio::test]
    async fn test_client_notification_broadcast() {
        let (client_read, mut server_write) = tokio::io::duplex(4096);
        let (_server_read, client_write) = tokio::io::duplex(4096);

        let client = AcpClient::from_streams(client_read, client_write);
        let mut sub = client.subscribe();

        // Send a notification from server
        let notif_line = r#"{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","updateType":"token","delta":"hello"}}"#;
        server_write
            .write_all(format!("{}\n", notif_line).as_bytes())
            .await
            .unwrap();

        let received = sub.recv().await.unwrap();
        assert_eq!(received.method, "session/update");
        let params = received.params.unwrap();
        assert_eq!(params["delta"], "hello");
    }

    #[tokio::test]
    async fn test_client_error_response_handling() {
        let (client_read, mut server_write) = tokio::io::duplex(4096);
        let (server_read, client_write) = tokio::io::duplex(4096);

        let client = AcpClient::from_streams(client_read, client_write);

        tokio::spawn(async move {
            let mut lines = BufReader::new(server_read).lines();
            if let Ok(Some(line)) = lines.next_line().await {
                let req: JsonRpcRequest = serde_json::from_str(&line).unwrap();

                let resp = serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": req.id,
                    "error": {
                        "code": -32601,
                        "message": "Method not found"
                    }
                });
                let resp_str = serde_json::to_string(&resp).unwrap() + "\n";
                server_write.write_all(resp_str.as_bytes()).await.unwrap();
            }
        });

        let err = client.ping().await.unwrap_err();
        match err {
            AcpError::Rpc(rpc_err) => {
                assert_eq!(rpc_err.code, -32601);
                assert_eq!(rpc_err.message, "Method not found");
            }
            other => panic!("Expected AcpError::Rpc, got: {:?}", other),
        }
    }

    #[tokio::test]
    async fn test_client_approval_decision_rpc() {
        let (client_read, mut server_write) = tokio::io::duplex(4096);
        let (server_read, client_write) = tokio::io::duplex(4096);

        let client = AcpClient::from_streams(client_read, client_write);

        tokio::spawn(async move {
            let mut lines = BufReader::new(server_read).lines();
            if let Ok(Some(line)) = lines.next_line().await {
                let req: JsonRpcRequest = serde_json::from_str(&line).unwrap();
                assert_eq!(req.method, "session/approvalDecision");

                let resp = serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": req.id,
                    "result": {
                        "approvalId": "appr_1",
                        "status": "acknowledged",
                        "decision": "approve"
                    }
                });
                let resp_str = serde_json::to_string(&resp).unwrap() + "\n";
                server_write.write_all(resp_str.as_bytes()).await.unwrap();
            }
        });

        let res = client
            .session_approval_decision("appr_1", "approve", Some("Safe command"))
            .await
            .unwrap();
        assert_eq!(res.approval_id, "appr_1");
        assert_eq!(res.decision, "approve");
        assert_eq!(res.status, "acknowledged");
    }

    #[tokio::test]
    async fn test_client_session_command_rpc() {
        let (client_read, mut server_write) = tokio::io::duplex(4096);
        let (server_read, client_write) = tokio::io::duplex(4096);

        let client = AcpClient::from_streams(client_read, client_write);

        tokio::spawn(async move {
            let mut lines = BufReader::new(server_read).lines();
            if let Ok(Some(line)) = lines.next_line().await {
                let req: JsonRpcRequest = serde_json::from_str(&line).unwrap();
                assert_eq!(req.method, "session/command");

                let resp = serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": req.id,
                    "result": {
                        "sessionId": "ses_1",
                        "command": "/doctor",
                        "output": "Doctor check complete: System operational",
                        "isError": false
                    }
                });
                let resp_str = serde_json::to_string(&resp).unwrap() + "\n";
                server_write.write_all(resp_str.as_bytes()).await.unwrap();
            }
        });

        let res = client.session_command("ses_1", "/doctor", None).await.unwrap();
        assert_eq!(res.command, "/doctor");
        assert!(res.output.contains("Doctor check complete"));
    }
}

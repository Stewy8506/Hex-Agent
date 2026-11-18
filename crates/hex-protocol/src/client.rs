//! Asynchronous JSON-RPC 2.0 stdio client for ACP server communication.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde::Serialize;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::{broadcast, mpsc, oneshot, Mutex, OwnedSemaphorePermit, Semaphore};
use tokio::time::timeout;

use crate::error::AcpError;
use crate::models::{
    ApprovalDecisionParams, ApprovalDecisionResponse, ClientInfo, InitializeParams,
    InitializeResponse, JsonRpcNotification, JsonRpcRequest, RequestId, SessionCancelParams,
    SessionCancelResponse, SessionCommandParams, SessionCommandResponse, SessionListItem,
    SessionListResponse, SessionLoadParams, SessionLoadResponse, SessionNewParams,
    SessionNewResponse, SessionPromptParams, SessionPromptResponse, SessionSubagentsParams,
    SessionSubagentsResponse, SubagentInfo, ToolCallParams, ToolCallResponse, ToolDefinition,
    ToolsListResponse,
};

type PendingMap = Arc<Mutex<HashMap<u64, oneshot::Sender<Result<serde_json::Value, AcpError>>>>>;

pub const MAX_WIRE_FRAME_BYTES: usize = 8 * 1024 * 1024;
pub const NOTIFICATION_BUFFER_BYTES: usize = 16 * 1024 * 1024;
type LosslessSender = Arc<Mutex<Option<mpsc::Sender<NotificationPacket>>>>;

#[derive(Debug)]
pub struct NotificationPacket {
    pub notification: JsonRpcNotification,
    _permit: OwnedSemaphorePermit,
}

fn notification_retained_bytes(notification: &JsonRpcNotification) -> usize {
    fn heap_bytes(value: &serde_json::Value) -> usize {
        match value {
            serde_json::Value::String(text) => text.capacity(),
            serde_json::Value::Array(values) => {
                values.capacity() * std::mem::size_of::<serde_json::Value>()
                    + values.iter().map(heap_bytes).sum::<usize>()
            }
            serde_json::Value::Object(values) => {
                // A one-entry object still allocates a complete B-tree node.
                values.len() * (11 * std::mem::size_of::<(String, serde_json::Value)>() + 128)
                    + values
                        .iter()
                        .map(|(key, value)| key.capacity() + heap_bytes(value))
                        .sum::<usize>()
            }
            _ => 0,
        }
    }
    std::mem::size_of::<NotificationPacket>()
        + notification.jsonrpc.capacity()
        + notification.method.capacity()
        + notification.params.as_ref().map_or(0, heap_bytes)
}

/// Asynchronous ACP JSON-RPC 2.0 client.
#[derive(Clone)]
pub struct AcpClient {
    next_id: Arc<AtomicU64>,
    writer_tx: mpsc::Sender<String>,
    pending: PendingMap,
    notifications_tx: broadcast::Sender<JsonRpcNotification>,
    lossless_tx: LosslessSender,
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
        let lossless_tx: LosslessSender = Arc::new(Mutex::new(None));
        let notification_bytes = Arc::new(Semaphore::new(NOTIFICATION_BUFFER_BYTES));

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
        let lossless = lossless_tx.clone();
        tokio::spawn(async move {
            let mut reader = BufReader::new(reader);
            loop {
                let mut line = Vec::new();
                match (&mut reader)
                    .take((MAX_WIRE_FRAME_BYTES + 1) as u64)
                    .read_until(b'\n', &mut line)
                    .await
                {
                    Ok(0) | Err(_) => break,
                    Ok(_) if line.len() > MAX_WIRE_FRAME_BYTES => break,
                    Ok(_) => {}
                }
                if line.iter().all(u8::is_ascii_whitespace) {
                    continue;
                }
                let mut val: serde_json::Value = match serde_json::from_slice(&line) {
                    Ok(v) => v,
                    Err(_) => continue,
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
                                if let Some(err_val) = val.get_mut("error").filter(|e| !e.is_null())
                                {
                                    match serde_json::from_value::<crate::models::JsonRpcError>(
                                        err_val.take(),
                                    ) {
                                        Ok(rpc_err) => {
                                            let _ = sender.send(Err(AcpError::Rpc(rpc_err)));
                                        }
                                        Err(e) => {
                                            let _ = sender.send(Err(AcpError::Json(e)));
                                        }
                                    }
                                } else if let Some(res_val) = val.get_mut("result") {
                                    let _ = sender.send(Ok(res_val.take()));
                                } else {
                                    let _ = sender.send(Ok(serde_json::Value::Null));
                                }
                            }
                        }
                        continue;
                    }
                }

                // Check if this is a notification (no id, but method)
                if let Some(method) = val
                    .get("method")
                    .and_then(|m| m.as_str())
                    .map(str::to_owned)
                {
                    let notification = JsonRpcNotification {
                        jsonrpc: "2.0".to_string(),
                        method,
                        params: val.get_mut("params").map(serde_json::Value::take),
                    };
                    drop(line);
                    if notif_tx.receiver_count() > 0 {
                        let _ = notif_tx.send(notification.clone());
                    }
                    let sender = lossless.lock().await.clone();
                    if let Some(sender) = sender {
                        let bytes = notification_retained_bytes(&notification);
                        if bytes > NOTIFICATION_BUFFER_BYTES {
                            break;
                        }
                        let permit = match notification_bytes
                            .clone()
                            .acquire_many_owned(bytes as u32)
                            .await
                        {
                            Ok(permit) => permit,
                            Err(_) => break,
                        };
                        if sender
                            .send(NotificationPacket {
                                notification,
                                _permit: permit,
                            })
                            .await
                            .is_err()
                        {
                            *lossless.lock().await = None;
                        }
                    }
                }
            }
            *lossless.lock().await = None;

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
            lossless_tx,
            child: None,
            default_timeout: Duration::from_secs(60),
        }
    }

    /// Spawn a child process and communicate via stdio.
    pub async fn spawn(command: &str, args: &[&str], cwd: Option<&Path>) -> Result<Self, AcpError> {
        let mut cmd = Command::new(command);
        cmd.args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);

        if let Some(dir) = cwd {
            cmd.current_dir(dir);
        }

        let mut child = cmd.spawn()?;
        if let Some(mut stderr) = child.stderr.take() {
            tokio::spawn(async move {
                let _ = tokio::io::copy(&mut stderr, &mut tokio::io::sink()).await;
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

    fn find_repo_root(start_path: &Path) -> Option<PathBuf> {
        start_path
            .ancestors()
            .find(|curr| {
                curr.join("pyproject.toml").exists()
                    && (curr.join("harness_core/__init__.py").is_file()
                        || curr.join(".venv").exists())
            })
            .map(Path::to_path_buf)
    }

    /// Helper to locate repository root containing the harness_core namespace or virtual environment.
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

        // 1. Walk up from start_path to find repository root with the harness_core namespace or virtual environment
        if let Some(root) = Self::find_repo_root(&start_path) {
            return root;
        }

        // 2. Walk up from current_exe (e.g. repo/target/debug/hex-tui)
        if let Ok(exe_path) = std::env::current_exe() {
            if let Some(root) = exe_path.parent().and_then(Self::find_repo_root) {
                return root;
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

    /// Whether `python` can import the server and its config parser.
    async fn interpreter_can_run_server(python: &str, repo_root: &Path) -> bool {
        let mut cmd = Command::new(python);
        cmd.args(["-c", "import harness_core.client.cli, yaml"])
            .current_dir(repo_root)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        matches!(cmd.status().await, Ok(status) if status.success())
    }

    /// Spawn the Python backend ACP server (`python3 -m harness_core.client.cli acp`).
    pub async fn spawn_acp_server(workspace_root: Option<&Path>) -> Result<Self, AcpError> {
        let repo_root = Self::resolve_repo_root(workspace_root);

        // An interpreter that cannot import the server's dependencies would start
        // a degraded runtime that silently ignores the configured model, so probe
        // the candidates instead of trusting the first one that exists.
        let mut candidates: Vec<String> = Vec::new();
        if let Ok(bin_env) = std::env::var("HEX_PYTHON_BIN") {
            candidates.push(bin_env);
        }
        if let Ok(venv) = std::env::var("VIRTUAL_ENV") {
            candidates.push(format!("{}/bin/python", venv));
        }
        let venv_python = repo_root.join(".venv/bin/python");
        if venv_python.exists() {
            candidates.push(venv_python.to_string_lossy().to_string());
        }
        candidates.push("python3".to_string());

        let mut py_bin = None;
        for candidate in candidates {
            if Self::interpreter_can_run_server(&candidate, &repo_root).await {
                py_bin = Some(candidate);
                break;
            }
        }
        let py_bin = py_bin.ok_or_else(|| {
            AcpError::Io(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "No Python interpreter can run the HEX server (needs harness_core and PyYAML). \
                 Set HEX_PYTHON_BIN to the python in your HEX virtualenv.",
            ))
        })?;

        let mut cmd = Command::new(&py_bin);
        cmd.args(["-m", "harness_core.client.cli", "acp"])
            .current_dir(&repo_root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        if let Some(workspace) = workspace_root {
            cmd.arg("--workspace").arg(workspace);
        }

        let mut child = cmd.spawn()?;

        if let Some(mut stderr) = child.stderr.take() {
            tokio::spawn(async move {
                let _ = tokio::io::copy(&mut stderr, &mut tokio::io::sink()).await;
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

    pub async fn subscribe_lossless(&self) -> mpsc::Receiver<NotificationPacket> {
        let (tx, rx) = mpsc::channel(128);
        *self.lossless_tx.lock().await = Some(tx);
        rx
    }

    /// Send a raw JSON-RPC request and await response.
    pub async fn send_request<Req: Serialize, Resp: DeserializeOwned>(
        &self,
        method: &str,
        params: Req,
    ) -> Result<Resp, AcpError> {
        let req_id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let params_val = serde_json::to_value(params)?;

        let request =
            JsonRpcRequest::new(RequestId::Number(req_id as i64), method, Some(params_val));

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
    pub async fn initialize(
        &self,
        client_info: Option<ClientInfo>,
    ) -> Result<InitializeResponse, AcpError> {
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

    pub async fn session_load_paged(
        &self,
        session_id: &str,
    ) -> Result<crate::models::PagedSessionLoad, AcpError> {
        self.send_request(
            "session/load",
            serde_json::json!({"sessionId": session_id, "paged": true}),
        )
        .await
    }

    pub async fn session_history(
        &self,
        session_id: &str,
        before: Option<usize>,
    ) -> Result<crate::models::HistoryPage, AcpError> {
        let mut params = serde_json::json!({"sessionId": session_id});
        if let Some(before) = before {
            params["before"] = before.into();
        }
        self.send_request("session/history", params).await
    }

    pub async fn session_history_content(
        &self,
        session_id: &str,
        turn_index: usize,
        field: &str,
        offset: usize,
    ) -> Result<crate::models::ToolResultPage, AcpError> {
        self.send_request("session/historyContent", serde_json::json!({"sessionId": session_id, "turnIndex": turn_index, "field": field, "offset": offset, "limit": 4096})).await
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

    pub async fn session_prompt_paged(
        &self,
        session_id: &str,
        prompt_text: &str,
    ) -> Result<SessionPromptResponse, AcpError> {
        let mut params =
            serde_json::to_value(SessionPromptParams::new_text(session_id, prompt_text))?;
        params["pagedResponse"] = serde_json::json!(true);
        self.send_request("session/prompt", params).await
    }

    /// Cancel prompt execution on an active session.
    pub async fn session_cancel(
        &self,
        session_id: &str,
    ) -> Result<SessionCancelResponse, AcpError> {
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

    pub async fn session_tool_result(
        &self,
        session_id: &str,
        task_id: &str,
        call_id: &str,
        offset: usize,
    ) -> Result<crate::models::ToolResultPage, AcpError> {
        self.send_request(
            "session/toolResult",
            crate::models::ToolResultPageParams {
                session_id: session_id.to_string(),
                task_id: task_id.to_string(),
                call_id: call_id.to_string(),
                offset,
                limit: 4096,
            },
        )
        .await
    }

    /// Query the subagents this session's tasks have spawned.
    pub async fn session_subagents(&self, session_id: &str) -> Result<Vec<SubagentInfo>, AcpError> {
        let params = SessionSubagentsParams {
            session_id: session_id.to_string(),
        };
        let resp: SessionSubagentsResponse = self.send_request("session/subagents", params).await?;
        Ok(resp.subagents)
    }

    /// Ping server for liveness check.
    pub async fn ping(&self) -> Result<bool, AcpError> {
        let resp: serde_json::Value = self.send_request("ping", serde_json::json!({})).await?;
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
    async fn lossless_bursts_preserve_notification_order() {
        let (client_io, server_io) = tokio::io::duplex(4096);
        let (read, write) = tokio::io::split(client_io);
        let client = AcpClient::from_streams(read, write);
        let mut notifications = client.subscribe_lossless().await;
        let (_, mut output) = tokio::io::split(server_io);
        let producer = tokio::spawn(async move {
            for i in 0..2000 {
                let line = format!("{{\"jsonrpc\":\"2.0\",\"method\":\"session/update\",\"params\":{{\"seq\":{i}}}}}\n");
                output.write_all(line.as_bytes()).await.unwrap();
            }
            output
                .write_all(b"{\"method\":\"approval/request\",\"params\":{\"seq\":2000}}\n")
                .await
                .unwrap();
        });
        timeout(Duration::from_secs(5), async {
            for i in 0..=2000 {
                let packet = notifications.recv().await.unwrap();
                assert_eq!(packet.notification.params.unwrap()["seq"], i);
            }
            producer.await.unwrap();
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn notification_byte_permits_follow_packets_until_consumed() {
        let (client_io, server_io) = tokio::io::duplex(4096);
        let (read, write) = tokio::io::split(client_io);
        let client = AcpClient::from_streams(read, write);
        let mut notifications = client.subscribe_lossless().await;
        let (_, mut output) = tokio::io::split(server_io);
        let producer = tokio::spawn(async move {
            let line = format!(
                "{{\"method\":\"session/update\",\"params\":\"{}\"}}\n",
                "x".repeat(2 * 1024 * 1024)
            );
            for _ in 0..12 {
                output.write_all(line.as_bytes()).await.unwrap();
            }
        });
        let mut held = Vec::new();
        while let Ok(Some(packet)) = timeout(Duration::from_millis(200), notifications.recv()).await
        {
            held.push(packet);
        }
        assert!(!held.is_empty());
        assert!(held.len() < 12);
        let bytes: usize = held.iter().map(|packet| packet._permit.num_permits()).sum();
        assert!(bytes <= NOTIFICATION_BUFFER_BYTES);
        assert!(!producer.is_finished());
        let received = held.len();
        drop(held);
        timeout(Duration::from_secs(5), async {
            for _ in received..12 {
                drop(notifications.recv().await.unwrap());
            }
            producer.await.unwrap();
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn oversized_wire_frame_closes_subscription_and_pending_requests() {
        let (client_io, server_io) = tokio::io::duplex(4096);
        let (read, write) = tokio::io::split(client_io);
        let client = AcpClient::from_streams(read, write);
        let mut notifications = client.subscribe_lossless().await;
        let (pending_tx, pending_rx) = oneshot::channel();
        client.pending.lock().await.insert(42, pending_tx);
        let (_, mut output) = tokio::io::split(server_io);
        let producer = tokio::spawn(async move {
            let _ = output
                .write_all(&vec![b'x'; MAX_WIRE_FRAME_BYTES + 1])
                .await;
        });
        assert!(timeout(Duration::from_secs(5), notifications.recv())
            .await
            .unwrap()
            .is_none());
        assert!(matches!(
            pending_rx.await.unwrap(),
            Err(AcpError::ConnectionClosed)
        ));
        producer.await.unwrap();
    }

    #[tokio::test]
    async fn closing_client_reaps_owned_backend() {
        #[cfg(windows)]
        let client = AcpClient::spawn("cmd.exe", &["/D", "/C", "set /p line="], None);
        #[cfg(not(windows))]
        let client = AcpClient::spawn("/bin/sh", &["-c", "read line"], None);
        let client = client.await.unwrap();
        client.close().await.unwrap();
        let mut child = client.child.as_ref().unwrap().lock().await;
        assert!(child.try_wait().unwrap().is_some());
    }

    #[test]
    fn test_find_repo_root_from_relocated_desktop() {
        let repo = tempfile::tempdir().unwrap();
        std::fs::create_dir(repo.path().join("harness_core")).unwrap();
        std::fs::write(repo.path().join("harness_core/__init__.py"), "").unwrap();
        std::fs::write(repo.path().join("pyproject.toml"), "").unwrap();
        let desktop = repo.path().join("App/hex-desktop/src");
        std::fs::create_dir_all(&desktop).unwrap();

        assert_eq!(
            AcpClient::find_repo_root(&desktop),
            Some(repo.path().to_path_buf())
        );
    }

    #[test]
    fn test_find_repo_root_skips_incomplete_and_obsolete_namespace_markers() {
        let repo = tempfile::tempdir().unwrap();
        std::fs::create_dir(repo.path().join("harness_core")).unwrap();
        std::fs::write(repo.path().join("harness_core/__init__.py"), "").unwrap();
        std::fs::write(repo.path().join("pyproject.toml"), "").unwrap();
        let nested = repo.path().join("nested");
        std::fs::create_dir_all(nested.join("harness_core")).unwrap();
        std::fs::write(nested.join("pyproject.toml"), "").unwrap();
        std::fs::create_dir_all(nested.join("src/harness_core")).unwrap();

        assert_eq!(
            AcpClient::find_repo_root(&nested),
            Some(repo.path().to_path_buf())
        );
    }

    #[test]
    fn test_find_repo_root_requires_pyproject_for_namespace_anchor() {
        let repo = tempfile::tempdir().unwrap();
        std::fs::create_dir(repo.path().join("harness_core")).unwrap();
        std::fs::write(repo.path().join("harness_core/__init__.py"), "").unwrap();

        assert_eq!(AcpClient::find_repo_root(repo.path()), None);
    }

    #[test]
    fn test_find_repo_root_preserves_virtualenv_alternative() {
        let repo = tempfile::tempdir().unwrap();
        std::fs::write(repo.path().join("pyproject.toml"), "").unwrap();
        std::fs::create_dir(repo.path().join(".venv")).unwrap();
        let nested = repo.path().join("workspace");
        std::fs::create_dir(&nested).unwrap();

        assert_eq!(
            AcpClient::find_repo_root(&nested),
            Some(repo.path().to_path_buf())
        );
    }

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
    async fn test_client_history_paging_rpc() {
        let (client_read, mut server_write) = tokio::io::duplex(4096);
        let (server_read, client_write) = tokio::io::duplex(4096);
        let client = AcpClient::from_streams(client_read, client_write);
        let server = tokio::spawn(async move {
            let mut lines = BufReader::new(server_read).lines();
            let page = serde_json::json!({
                "history": [{"turnIndex": 8, "prompt": "世界", "response": "tail",
                    "promptChars": 9000, "responseChars": 12000}],
                "historyCount": 40, "nextBefore": 8
            });
            let requests = [
                (
                    "session/load",
                    serde_json::json!({"sessionId": "ses_1", "paged": true}),
                ),
                (
                    "session/history",
                    serde_json::json!({"sessionId": "ses_1", "before": 8}),
                ),
                ("session/history", serde_json::json!({"sessionId": "ses_1"})),
                (
                    "session/historyContent",
                    serde_json::json!({"sessionId": "ses_1",
                    "turnIndex": 8, "field": "response", "offset": 4096, "limit": 4096}),
                ),
                (
                    "session/prompt",
                    serde_json::json!({"sessionId": "ses_1", "prompt": "hello", "pagedResponse": true}),
                ),
            ];
            for (method, params) in requests {
                let line = lines.next_line().await.unwrap().unwrap();
                let req: JsonRpcRequest = serde_json::from_str(&line).unwrap();
                assert_eq!(req.method, method);
                assert_eq!(req.params, Some(params));
                let result = match method {
                    "session/load" => {
                        let mut result = page.clone();
                        result["sessionId"] = serde_json::json!("ses_1");
                        result["cwd"] = serde_json::json!("/workspace");
                        result["model"] = serde_json::json!("test");
                        result
                    }
                    "session/historyContent" => serde_json::json!({
                        "content": "世界", "totalChars": 12000, "nextOffset": 4098
                    }),
                    "session/prompt" => {
                        serde_json::json!({"sessionId": "ses_1", "status": "completed", "taskId": "tsk_1", "content": [{"type": "text", "text": "世界"}]})
                    }
                    _ => page.clone(),
                };
                let response =
                    serde_json::json!({"jsonrpc": "2.0", "id": req.id, "result": result});
                server_write
                    .write_all((response.to_string() + "\n").as_bytes())
                    .await
                    .unwrap();
            }
        });
        let loaded = client.session_load_paged("ses_1").await.unwrap();
        assert_eq!(loaded.page.history_count, 40);
        assert_eq!(loaded.page.history[0].prompt_chars, 9000);
        let older = client.session_history("ses_1", Some(8)).await.unwrap();
        assert_eq!(older.next_before, Some(8));
        assert_eq!(client.session_history("ses_1", None).await.unwrap(), older);
        let content = client
            .session_history_content("ses_1", 8, "response", 4096)
            .await
            .unwrap();
        assert_eq!(content.content, "世界");
        assert_eq!(content.next_offset, Some(4098));
        assert_eq!(
            client
                .session_prompt_paged("ses_1", "hello")
                .await
                .unwrap()
                .text_content(),
            "世界"
        );
        server.await.unwrap();
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

        let res = client
            .session_command("ses_1", "/doctor", None)
            .await
            .unwrap();
        assert_eq!(res.command, "/doctor");
        assert!(res.output.contains("Doctor check complete"));
    }
}

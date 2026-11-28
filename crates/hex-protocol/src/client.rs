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
    ApprovalDecisionParams, ApprovalDecisionResponse, ClientInfo, CloudAuthStatus, CloudDevice,
    CloudDevicesList, CloudFlags, CloudRevokeResult, CloudUpdatePlan, ConfigGetResponse,
    ConfigSetResponse, GcPreviewResponse, InitializeParams, InitializeResponse,
    JsonRpcNotification, JsonRpcRequest, ModelCatalog, ProviderCredentialMutationResponse,
    ProviderEndpointMutationResponse, ProvidersListResponse, RequestId, SessionCancelParams,
    SessionCancelResponse, SessionCommandParams, SessionCommandResponse, SessionDeleteResponse,
    SessionFlagResponse, SessionListItem, SessionListResponse, SessionLoadParams,
    SessionLoadResponse, SessionNewParams, SessionNewResponse, SessionPromptParams,
    SessionPromptResponse, SessionRenameResponse, SessionSubagentsParams, SessionSubagentsResponse,
    SubagentInfo, TeamActivityMutation, TeamActivityStatus, TeamBlackboardNoteParams,
    TeamContractChangeParams, TeamPeerQueryParams, TeamPeerQueryResult, TeamRuntimeStatus,
    TeamScopeClaimParams, TeamScopeReleaseParams, TeamScopeRenewParams, ToolCallParams,
    ToolCallResponse, ToolDefinition, ToolPolicyMutationResponse, ToolsListResponse,
    ToolsPolicyResponse, HEX_CONTRACT_VERSION,
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

/// Preserve child stderr diagnostics instead of discarding them to a sink.
/// Lines are emitted through the `log` facade so an installed logger captures
/// them; with no logger (e.g. the TUI) this is a harmless no-op that never
/// writes to the terminal and corrupts rendering.
fn drain_stderr(stderr: tokio::process::ChildStderr) {
    tokio::spawn(async move {
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            log::warn!(target: "hex_acp::stderr", "{line}");
        }
    });
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
    closed_tx: broadcast::Sender<()>,
    lossless_tx: LosslessSender,
    child: Option<Arc<Mutex<tokio::process::Child>>>,
    default_timeout: Duration,
    negotiated: Arc<Mutex<Option<InitializeResponse>>>,
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
        let (closed_tx, _) = broadcast::channel::<()>(4);
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
        let reader_closed_tx = closed_tx.clone();
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
            drop(map);
            // Authoritative transport-loss signal: the notification channel stays
            // open (the client retains its sender), so closure is announced here.
            let _ = reader_closed_tx.send(());
        });

        Self {
            next_id,
            writer_tx,
            pending,
            notifications_tx,
            closed_tx,
            lossless_tx,
            child: None,
            default_timeout: Duration::from_secs(60),
            negotiated: Arc::new(Mutex::new(None)),
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
        if let Some(stderr) = child.stderr.take() {
            drain_stderr(stderr);
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

    fn find_sibling_harness_root(app_path: &Path) -> Option<PathBuf> {
        let app_root = app_path
            .ancestors()
            .find(|path| path.join("Cargo.toml").is_file() && path.join("src/main.rs").is_file())?;
        let parent = app_root.parent()?;
        ["Harness", "hex-agent"]
            .into_iter()
            .find_map(|name| Self::find_repo_root(&parent.join(name)))
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
            if let Some(root) = Self::find_sibling_harness_root(&exe_path) {
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

    /// Ordered interpreter candidates, honoring both POSIX (`bin/python`) and
    /// Windows (`Scripts/python.exe`) virtualenv layouts so the backend can be
    /// spawned on either platform (B33).
    fn interpreter_candidates(repo_root: &Path) -> Vec<String> {
        let mut candidates: Vec<String> = Vec::new();
        if let Ok(bin_env) = std::env::var("HEX_PYTHON_BIN") {
            candidates.push(bin_env);
        }
        let mut venv_roots: Vec<PathBuf> = Vec::new();
        if let Ok(venv) = std::env::var("VIRTUAL_ENV") {
            venv_roots.push(PathBuf::from(venv));
        }
        venv_roots.push(repo_root.join(".venv"));
        for venv in venv_roots {
            let windows = venv.join("Scripts").join("python.exe");
            if windows.exists() {
                candidates.push(windows.to_string_lossy().to_string());
            }
            let posix = venv.join("bin").join("python");
            if posix.exists() {
                candidates.push(posix.to_string_lossy().to_string());
            }
        }
        candidates.push("python3".to_string());
        candidates.push("python".to_string());
        candidates
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
        let candidates = Self::interpreter_candidates(&repo_root);

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

        if let Some(stderr) = child.stderr.take() {
            drain_stderr(stderr);
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

    /// Subscribe to the authoritative transport-loss signal. Fires once when the
    /// reader task ends (EOF/error), independent of the notification channel.
    pub fn subscribe_closed(&self) -> broadcast::Receiver<()> {
        self.closed_tx.subscribe()
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
            hex_contract_version: Some(HEX_CONTRACT_VERSION),
            client_info: client_info.or_else(|| {
                Some(ClientInfo {
                    name: "HEX-Rust-Cockpit".to_string(),
                    version: env!("CARGO_PKG_VERSION").to_string(),
                })
            }),
            capabilities: None,
        };
        let response: InitializeResponse = self.send_request("initialize", params).await?;
        if let Some(version) = response.hex_contract_version {
            if version == 0 || version > HEX_CONTRACT_VERSION {
                return Err(AcpError::InvalidResponse(format!(
                    "server advertised unsupported HEX contract version {version}"
                )));
            }
        }
        *self.negotiated.lock().await = Some(response.clone());
        Ok(response)
    }

    /// Return the most recent successful initialize response.
    pub async fn negotiated_contract(&self) -> Option<InitializeResponse> {
        self.negotiated.lock().await.clone()
    }

    /// Check an optional feature advertised by the negotiated server contract.
    pub async fn supports_feature(&self, feature: &str) -> bool {
        self.negotiated
            .lock()
            .await
            .as_ref()
            .is_some_and(|response| response.supports_feature(feature))
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

    pub async fn session_load_task_paged(
        &self,
        task_id: &str,
    ) -> Result<crate::models::PagedSessionLoad, AcpError> {
        self.send_request(
            "session/load",
            serde_json::json!({"taskId": task_id, "paged": true}),
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

    /// Send a prompt with operator-attached local file paths; the runtime
    /// reads each one (size-bounded) and includes the content in the turn.
    pub async fn session_prompt_with_attachments(
        &self,
        session_id: &str,
        prompt_text: &str,
        attachments: &[String],
    ) -> Result<SessionPromptResponse, AcpError> {
        let params = SessionPromptParams::new_text_with_attachments(
            session_id,
            prompt_text,
            attachments.to_vec(),
        );
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

    /// Rename a session (operator-facing title, persisted by the runtime).
    pub async fn session_rename(
        &self,
        session_id: &str,
        title: &str,
    ) -> Result<SessionRenameResponse, AcpError> {
        self.send_request(
            "session/rename",
            serde_json::json!({ "sessionId": session_id, "title": title }),
        )
        .await
    }

    /// Delete a session: drops the in-memory state, transcript rows, and
    /// metadata file. Irreversible.
    pub async fn session_delete(
        &self,
        session_id: &str,
    ) -> Result<SessionDeleteResponse, AcpError> {
        self.send_request(
            "session/delete",
            serde_json::json!({ "sessionId": session_id }),
        )
        .await
    }

    /// Archive (or unarchive) a session; archived sessions stay resumable
    /// but are hidden from the active list.
    pub async fn session_archive(
        &self,
        session_id: &str,
        archived: bool,
    ) -> Result<SessionFlagResponse, AcpError> {
        self.send_request(
            "session/archive",
            serde_json::json!({ "sessionId": session_id, "archived": archived }),
        )
        .await
    }

    /// Pin (or unpin) a session for top-of-list ordering.
    pub async fn session_pin(
        &self,
        session_id: &str,
        pinned: bool,
    ) -> Result<SessionFlagResponse, AcpError> {
        self.send_request(
            "session/pin",
            serde_json::json!({ "sessionId": session_id, "pinned": pinned }),
        )
        .await
    }

    /// Query the live model catalog from the backend's configured providers.
    pub async fn session_models(&self, session_id: &str) -> Result<ModelCatalog, AcpError> {
        self.send_request(
            "session/models",
            serde_json::json!({ "sessionId": session_id }),
        )
        .await
    }

    /// Read the sanitized settings view (whitelisted config keys only).
    pub async fn config_get(&self) -> Result<ConfigGetResponse, AcpError> {
        self.send_request("config/get", serde_json::json!({}))
            .await
    }

    /// Validate-and-write a settings update; returns the fresh view plus the
    /// dotted keys that were changed.
    pub async fn config_set(
        &self,
        updates: serde_json::Value,
        expected_revision: Option<&str>,
    ) -> Result<ConfigSetResponse, AcpError> {
        let mut params = serde_json::json!({ "updates": updates });
        if let Some(revision) = expected_revision {
            params["expectedRevision"] = serde_json::json!(revision);
        }
        self.send_request("config/set", params).await
    }

    /// List registered providers and credential status without secret values.
    pub async fn providers_list(&self) -> Result<ProvidersListResponse, AcpError> {
        self.send_request("providers/list", serde_json::json!({}))
            .await
    }

    /// Write a provider API key or token to the backend credential store.
    pub async fn set_provider_credential(
        &self,
        provider: &str,
        credential: &str,
    ) -> Result<ProviderCredentialMutationResponse, AcpError> {
        self.send_request(
            "providers/credentials/set",
            serde_json::json!({ "provider": provider, "credential": credential }),
        )
        .await
    }

    /// Set or reset a registered provider's endpoint override.
    pub async fn set_provider_endpoint(
        &self,
        provider: &str,
        base_url: &str,
    ) -> Result<ProviderEndpointMutationResponse, AcpError> {
        self.send_request(
            "providers/endpoint/set",
            serde_json::json!({ "provider": provider, "baseUrl": base_url }),
        )
        .await
    }

    /// Remove a provider API key or token from the backend credential store.
    pub async fn delete_provider_credential(
        &self,
        provider: &str,
    ) -> Result<ProviderCredentialMutationResponse, AcpError> {
        self.send_request(
            "providers/credentials/delete",
            serde_json::json!({ "provider": provider }),
        )
        .await
    }

    /// Query available tools from the ACP server.
    pub async fn tools_list(&self) -> Result<Vec<ToolDefinition>, AcpError> {
        let resp: ToolsListResponse = self
            .send_request("tools/list", serde_json::json!({}))
            .await?;
        Ok(resp.tools)
    }

    /// Read the live tool registry with effective per-tool approval policy.
    pub async fn tools_policy_get(&self) -> Result<ToolsPolicyResponse, AcpError> {
        self.send_request("tools/policy/get", serde_json::json!({}))
            .await
    }

    /// Update per-tool approval policy using the shared config revision.
    pub async fn tools_policy_set(
        &self,
        tool: &str,
        mode: &str,
        expected_revision: &str,
    ) -> Result<ToolPolicyMutationResponse, AcpError> {
        let mut updates = serde_json::Map::new();
        updates.insert(tool.to_string(), serde_json::Value::String(mode.to_string()));
        self.send_request(
            "tools/policy/set",
            serde_json::json!({
                "updates": updates,
                "expectedRevision": expected_revision
            }),
        )
        .await
    }

    /// Read-only dry-run retention sweep (`maintenance/gcPreview`).
    pub async fn gc_preview(&self) -> Result<GcPreviewResponse, AcpError> {
        self.send_request("maintenance/gcPreview", serde_json::json!({}))
            .await
    }

    // -- Teams ---------------------------------------------------------------

    /// Start an explicitly selected project session in the Teams runtime.
    pub async fn team_start(
        &self,
        team_id: &str,
        project_session_id: &str,
    ) -> Result<TeamRuntimeStatus, AcpError> {
        self.send_request("team/start", serde_json::json!({
            "team_id": team_id, "project_session_id": project_session_id,
        })).await
    }

    /// Read status for one team session, or all sessions when no id is supplied.
    pub async fn team_status(
        &self,
        project_session_id: Option<&str>,
    ) -> Result<serde_json::Value, AcpError> {
        self.send_request("team/status", serde_json::json!({
            "project_session_id": project_session_id,
        })).await
    }

    /// Stop one team session, or every local team session when no id is supplied.
    pub async fn team_stop(&self, project_session_id: Option<&str>) -> Result<serde_json::Value, AcpError> {
        self.send_request("team/stop", serde_json::json!({
            "project_session_id": project_session_id,
        })).await
    }

    /// Connect an eligible enrolled peer through authenticated E2EE transport.
    pub async fn team_connect(
        &self,
        project_session_id: &str,
        device_record_id: &str,
        transport: &str,
    ) -> Result<serde_json::Value, AcpError> {
        self.send_request("team/connect", serde_json::json!({
            "project_session_id": project_session_id, "device_record_id": device_record_id,
            "transport": transport,
        })).await
    }

    /// Disconnect a peer without stopping the durable team session.
    pub async fn team_disconnect(
        &self,
        project_session_id: &str,
        device_record_id: &str,
    ) -> Result<serde_json::Value, AcpError> {
        self.send_request("team/disconnect", serde_json::json!({
            "project_session_id": project_session_id, "device_record_id": device_record_id,
        })).await
    }

    /// Flush durably queued turns through the runtime's authenticated publisher.
    pub async fn team_flush(&self, project_session_id: &str) -> Result<serde_json::Value, AcpError> {
        self.send_request("team/flush", serde_json::json!({
            "project_session_id": project_session_id,
        })).await
    }

    /// Read signed fast-lane state for one explicitly selected session.
    pub async fn team_activity_status(
        &self,
        project_session_id: &str,
        limit: u32,
    ) -> Result<TeamActivityStatus, AcpError> {
        self.send_request(
            "team/activity/status",
            serde_json::json!({
                "project_session_id": project_session_id, "limit": limit,
            }),
        )
        .await
    }

    /// Claim a soft file/symbol scope for a local task.
    pub async fn team_scope_claim(
        &self,
        project_session_id: &str,
        params: TeamScopeClaimParams,
    ) -> Result<TeamActivityMutation, AcpError> {
        let mut value = serde_json::to_value(params)?;
        value["project_session_id"] = serde_json::json!(project_session_id);
        self.send_request("team/scope/claim", value).await
    }

    /// Release an owned soft scope.
    pub async fn team_scope_release(
        &self,
        project_session_id: &str,
        params: TeamScopeReleaseParams,
    ) -> Result<TeamActivityMutation, AcpError> {
        let mut value = serde_json::to_value(params)?;
        value["project_session_id"] = serde_json::json!(project_session_id);
        self.send_request("team/scope/release", value).await
    }

    /// Extend an active soft scope without changing its identity.
    pub async fn team_scope_renew(
        &self,
        project_session_id: &str,
        params: TeamScopeRenewParams,
    ) -> Result<TeamActivityMutation, AcpError> {
        let mut value = serde_json::to_value(params)?;
        value["project_session_id"] = serde_json::json!(project_session_id);
        self.send_request("team/scope/renew", value).await
    }

    /// Publish a redacted architectural note to the shared blackboard.
    pub async fn team_blackboard_note(
        &self,
        project_session_id: &str,
        params: TeamBlackboardNoteParams,
    ) -> Result<TeamActivityMutation, AcpError> {
        let mut value = serde_json::to_value(params)?;
        value["project_session_id"] = serde_json::json!(project_session_id);
        self.send_request("team/blackboard/note", value).await
    }

    /// Publish a public interface contract change.
    pub async fn team_contract_change(
        &self,
        project_session_id: &str,
        params: TeamContractChangeParams,
    ) -> Result<TeamActivityMutation, AcpError> {
        let mut value = serde_json::to_value(params)?;
        value["project_session_id"] = serde_json::json!(project_session_id);
        self.send_request("team/contract/change", value).await
    }

    /// Ask an enrolled peer a bounded one-hop question and cache its answer.
    pub async fn team_peer_query(
        &self,
        project_session_id: &str,
        params: TeamPeerQueryParams,
    ) -> Result<TeamPeerQueryResult, AcpError> {
        let mut value = serde_json::to_value(params)?;
        value["project_session_id"] = serde_json::json!(project_session_id);
        self.send_request("team/peer/query", value).await
    }

    // -- Cloud account ------------------------------------------------------

    /// Local cloud account/device status (`cloud/auth/status`).
    pub async fn cloud_auth_status(&self) -> Result<CloudAuthStatus, AcpError> {
        self.send_request("cloud/auth/status", serde_json::json!({}))
            .await
    }

    /// Register a new cloud account (`cloud/auth/register`).
    pub async fn cloud_auth_register(
        &self,
        email: &str,
        password: &str,
    ) -> Result<serde_json::Value, AcpError> {
        self.send_request(
            "cloud/auth/register",
            serde_json::json!({ "email": email, "password": password }),
        )
        .await
    }

    /// Sign in and enroll or reuse this device (`cloud/auth/login`).
    pub async fn cloud_auth_login(
        &self,
        email: &str,
        password: &str,
    ) -> Result<CloudAuthStatus, AcpError> {
        self.send_request(
            "cloud/auth/login",
            serde_json::json!({ "email": email, "password": password }),
        )
        .await
    }

    /// Sign out on this device (`cloud/auth/logout`).
    pub async fn cloud_auth_logout(&self) -> Result<CloudAuthStatus, AcpError> {
        self.send_request("cloud/auth/logout", serde_json::json!({}))
            .await
    }

    /// List enrolled devices (`cloud/devices/list`).
    pub async fn cloud_devices_list(&self, include_revoked: bool) -> Result<Vec<CloudDevice>, AcpError> {
        let resp: CloudDevicesList = self
            .send_request(
                "cloud/devices/list",
                serde_json::json!({ "include_revoked": include_revoked }),
            )
            .await?;
        Ok(resp.devices)
    }

    /// Revoke an enrolled device (`cloud/devices/revoke`).
    pub async fn cloud_devices_revoke(
        &self,
        device_record_id: &str,
    ) -> Result<CloudRevokeResult, AcpError> {
        self.send_request(
            "cloud/devices/revoke",
            serde_json::json!({ "device_record_id": device_record_id }),
        )
        .await
    }

    /// Check hex-backend for signed component updates (`cloud/update/check`).
    pub async fn cloud_update_check(&self) -> Result<CloudUpdatePlan, AcpError> {
        self.send_request("cloud/update/check", serde_json::json!({}))
            .await
    }

    /// Fetch the verified remote-config bundle (`cloud/config/evaluate`).
    pub async fn cloud_config_evaluate(&self) -> Result<CloudFlags, AcpError> {
        self.send_request("cloud/config/evaluate", serde_json::json!({}))
            .await
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
        self.session_approval_decision_with_ttl(approval_id, decision, reason, None)
            .await
    }

    /// Send an approval decision with an optional validated scoped-lease TTL.
    pub async fn session_approval_decision_with_ttl(
        &self,
        approval_id: &str,
        decision: &str,
        reason: Option<&str>,
        lease_ttl_seconds: Option<u64>,
    ) -> Result<ApprovalDecisionResponse, AcpError> {
        let params = ApprovalDecisionParams {
            approval_id: approval_id.to_string(),
            decision: decision.to_string(),
            reason: reason.map(|s| s.to_string()),
            lease_ttl_seconds,
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
    fn test_find_repo_root_from_desktop_source() {
        let repo = tempfile::tempdir().unwrap();
        std::fs::create_dir(repo.path().join("harness_core")).unwrap();
        std::fs::write(repo.path().join("harness_core/__init__.py"), "").unwrap();
        std::fs::write(repo.path().join("pyproject.toml"), "").unwrap();
        let desktop = repo.path().join("Hex-App/src");
        std::fs::create_dir_all(&desktop).unwrap();

        assert_eq!(
            AcpClient::find_repo_root(&desktop),
            Some(repo.path().to_path_buf())
        );
    }

    #[test]
    fn test_find_sibling_harness_root_from_desktop_executable() {
        let development = tempfile::tempdir().unwrap();
        let harness = development.path().join("Harness");
        std::fs::create_dir_all(harness.join("harness_core")).unwrap();
        std::fs::write(harness.join("harness_core/__init__.py"), "").unwrap();
        std::fs::write(harness.join("pyproject.toml"), "").unwrap();

        let desktop = development.path().join("Hex-App");
        std::fs::create_dir_all(desktop.join("src")).unwrap();
        std::fs::write(desktop.join("Cargo.toml"), "").unwrap();
        std::fs::write(desktop.join("src/main.rs"), "").unwrap();

        assert_eq!(
            AcpClient::find_sibling_harness_root(&desktop.join("target/debug/hex-desktop")),
            Some(harness)
        );
    }

    #[test]
    fn test_find_sibling_harness_root_skips_incomplete_harness() {
        let development = tempfile::tempdir().unwrap();
        std::fs::create_dir(development.path().join("Harness")).unwrap();
        let agent = development.path().join("hex-agent");
        std::fs::create_dir_all(agent.join("harness_core")).unwrap();
        std::fs::write(agent.join("harness_core/__init__.py"), "").unwrap();
        std::fs::write(agent.join("pyproject.toml"), "").unwrap();

        let desktop = development.path().join("Hex-App");
        std::fs::create_dir_all(desktop.join("src")).unwrap();
        std::fs::write(desktop.join("Cargo.toml"), "").unwrap();
        std::fs::write(desktop.join("src/main.rs"), "").unwrap();

        assert_eq!(
            AcpClient::find_sibling_harness_root(&desktop.join("target/release/hex-desktop")),
            Some(agent)
        );
    }

    #[test]
    fn test_find_sibling_harness_root_requires_engine_checkout() {
        let development = tempfile::tempdir().unwrap();
        let desktop = development.path().join("Hex-App");
        std::fs::create_dir_all(desktop.join("src")).unwrap();
        std::fs::write(desktop.join("Cargo.toml"), "").unwrap();
        std::fs::write(desktop.join("src/main.rs"), "").unwrap();

        assert_eq!(
            AcpClient::find_sibling_harness_root(&desktop.join("target/debug/hex-desktop")),
            None
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

    #[test]
    fn test_interpreter_candidates_cover_windows_and_posix_venv_layouts() {
        let repo = tempfile::tempdir().unwrap();
        let win = repo.path().join(".venv").join("Scripts").join("python.exe");
        let posix = repo.path().join(".venv").join("bin").join("python");
        std::fs::create_dir_all(win.parent().unwrap()).unwrap();
        std::fs::create_dir_all(posix.parent().unwrap()).unwrap();
        std::fs::write(&win, "").unwrap();
        std::fs::write(&posix, "").unwrap();

        let candidates = AcpClient::interpreter_candidates(repo.path());
        assert!(candidates.contains(&win.to_string_lossy().to_string()));
        assert!(candidates.contains(&posix.to_string_lossy().to_string()));
        assert!(candidates.contains(&"python3".to_string()));
        assert!(candidates.contains(&"python".to_string()));
    }

    #[tokio::test]
    async fn team_fastlane_methods_send_typed_session_scoped_requests() {
        let (client_read, mut server_write) = tokio::io::duplex(8192);
        let (server_read, client_write) = tokio::io::duplex(8192);
        let client = AcpClient::from_streams(client_read, client_write);
        let (requests_tx, mut requests_rx) = mpsc::channel(7);
        let server = tokio::spawn(async move {
            let mut lines = BufReader::new(server_read).lines();
            for _ in 0..7 {
                let line = lines.next_line().await.unwrap().unwrap();
                let request: JsonRpcRequest = serde_json::from_str(&line).unwrap();
                requests_tx
                    .send((request.method.clone(), request.params.unwrap()))
                    .await
                    .unwrap();
                let result = match request.method.as_str() {
                    "team/activity/status" => serde_json::json!({
                        "store": {"events": 1, "journal_events": 1, "bytes": 128,
                                  "event_limit": 1024, "bytes_limit": 8388608,
                                  "corrupt": false, "path": "/ws/.hex/team_blackboard.jsonl"},
                        "scope_locks": [], "peer_state": [], "blackboard": [], "recent": []
                    }),
                    "team/peer/query" => serde_json::json!({
                        "accepted": true, "request_id": "request_1",
                        "device_record_id": "dev_b", "answer": "Parser is canonical.",
                        "cached": true, "shared": false
                    }),
                    _ => serde_json::json!({"accepted": true}),
                };
                let response = serde_json::json!({
                    "jsonrpc": "2.0", "id": request.id, "result": result
                });
                server_write
                    .write_all((serde_json::to_string(&response).unwrap() + "\n").as_bytes())
                    .await
                    .unwrap();
            }
        });

        timeout(Duration::from_secs(5), async {
            let status = client.team_activity_status("proj_1", 25).await.unwrap();
            assert_eq!(status.store.events, 1);
            assert_eq!(status.store.event_limit, 1024);

            let claim = client
                .team_scope_claim(
                    "proj_1",
                    TeamScopeClaimParams {
                        task_id: "task_1".into(),
                        paths: vec!["src/parser.rs".into()],
                        symbols: vec!["parse".into()],
                        ttl_seconds: 60,
                        claim_id: Some("claim_1".into()),
                        summary: Some("Edit parser".into()),
                    },
                )
                .await
                .unwrap();
            assert!(claim.accepted);

            client
                .team_scope_release(
                    "proj_1",
                    TeamScopeReleaseParams {
                        task_id: "task_1".into(),
                        claim_id: "claim_1".into(),
                        paths: None,
                        symbols: None,
                    },
                )
                .await
                .unwrap();
            client
                .team_scope_renew(
                    "proj_1",
                    TeamScopeRenewParams {
                        task_id: "task_1".into(),
                        claim_id: "claim_1".into(),
                        ttl_seconds: 120,
                        summary: None,
                    },
                )
                .await
                .unwrap();
            client
                .team_blackboard_note(
                    "proj_1",
                    TeamBlackboardNoteParams {
                        body: "Keep the parser canonical.".into(),
                        task_id: Some("task_1".into()),
                        pinned: true,
                    },
                )
                .await
                .unwrap();
            client
                .team_contract_change(
                    "proj_1",
                    TeamContractChangeParams {
                        task_id: "task_1".into(),
                        path: "src/parser.rs".into(),
                        symbol: "parse".into(),
                        before: "fn parse(input: &str)".into(),
                        after: "fn parse(input: &str) -> Result<Value>".into(),
                    },
                )
                .await
                .unwrap();
            let query = client
                .team_peer_query(
                    "proj_1",
                    TeamPeerQueryParams {
                        device_record_id: "dev_b".into(),
                        question: "Which parser is canonical?".into(),
                        task_id: "task_1".into(),
                    },
                )
                .await
                .unwrap();
            assert_eq!(query.answer, "Parser is canonical.");
            assert!(query.cached);

            let mut requests = Vec::new();
            for _ in 0..7 {
                requests.push(requests_rx.recv().await.unwrap());
            }
            assert_eq!(
                requests
                    .iter()
                    .map(|item| item.0.as_str())
                    .collect::<Vec<_>>(),
                vec![
                    "team/activity/status",
                    "team/scope/claim",
                    "team/scope/release",
                    "team/scope/renew",
                    "team/blackboard/note",
                    "team/contract/change",
                    "team/peer/query",
                ]
            );
            for (_, params) in &requests {
                assert_eq!(params["project_session_id"], "proj_1");
            }
            assert_eq!(requests[0].1["limit"], 25);
            assert_eq!(requests[1].1["claim_id"], "claim_1");
            assert_eq!(requests[4].1["pinned"], true);
            assert_eq!(requests[6].1["device_record_id"], "dev_b");
        })
        .await
        .unwrap();
        server.await.unwrap();
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
    async fn test_client_tool_policy_rpc() {
        let (client_read, mut server_write) = tokio::io::duplex(4096);
        let (server_read, client_write) = tokio::io::duplex(4096);
        let client = AcpClient::from_streams(client_read, client_write);

        let server = tokio::spawn(async move {
            let mut lines = BufReader::new(server_read).lines();
            let requests = [
                ("tools/policy/get", serde_json::json!({})),
                (
                    "tools/policy/set",
                    serde_json::json!({
                        "updates": {"write_file": "ask"},
                        "expectedRevision": "sha256:policy-v1"
                    }),
                ),
            ];
            for (method, params) in requests {
                let line = lines.next_line().await.unwrap().unwrap();
                let req: JsonRpcRequest = serde_json::from_str(&line).unwrap();
                assert_eq!(req.method, method);
                assert_eq!(req.params, Some(params));
                let result = if method == "tools/policy/get" {
                    serde_json::json!({
                        "tools": [{
                            "name": "write_file",
                            "description": "Write a file",
                            "parameters": {},
                            "risk_class": "R2",
                            "policy_mode": "allow",
                            "policy_default": true,
                            "policy_writable": true
                        }],
                        "revision": "sha256:policy-v1"
                    })
                } else {
                    serde_json::json!({
                        "tools": [],
                        "revision": "sha256:policy-v2",
                        "changed": ["write_file"]
                    })
                };
                let response =
                    serde_json::json!({"jsonrpc": "2.0", "id": req.id, "result": result});
                server_write
                    .write_all((response.to_string() + "\n").as_bytes())
                    .await
                    .unwrap();
            }
        });

        let policy = client.tools_policy_get().await.unwrap();
        assert_eq!(policy.revision, "sha256:policy-v1");
        assert_eq!(policy.tools[0].definition.name, "write_file");
        let mutation = client
            .tools_policy_set("write_file", "ask", "sha256:policy-v1")
            .await
            .unwrap();
        assert_eq!(mutation.revision, "sha256:policy-v2");
        assert_eq!(mutation.changed, vec!["write_file"]);
        server.await.unwrap();
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

    #[tokio::test]
    async fn test_client_session_models_rpc() {
        let (client_read, mut server_write) = tokio::io::duplex(4096);
        let (server_read, client_write) = tokio::io::duplex(4096);

        let client = AcpClient::from_streams(client_read, client_write);

        tokio::spawn(async move {
            let mut lines = BufReader::new(server_read).lines();
            if let Ok(Some(line)) = lines.next_line().await {
                let req: JsonRpcRequest = serde_json::from_str(&line).unwrap();
                assert_eq!(req.method, "session/models");
                assert_eq!(req.params.unwrap()["sessionId"], "ses_1");

                let resp = serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": req.id,
                    "result": {
                        "sessionId": "ses_1",
                        "activeModel": "gpt-4o",
                        "provider": "openai",
                        "models": [
                            {"id": "gpt-4o", "displayName": "GPT-4o", "provider": "openai", "isLocal": false},
                            {"id": "qwen2.5-coder-32b", "displayName": "Qwen (Ollama)", "provider": "Local", "isLocal": true}
                        ],
                        "error": "anthropic: offline"
                    }
                });
                let resp_str = serde_json::to_string(&resp).unwrap() + "\n";
                server_write.write_all(resp_str.as_bytes()).await.unwrap();
            }
        });

        let catalog = client.session_models("ses_1").await.unwrap();
        assert_eq!(catalog.active_model, "gpt-4o");
        assert_eq!(catalog.models.len(), 2);
        assert!(catalog.models[1].is_local);
        assert_eq!(catalog.error.as_deref(), Some("anthropic: offline"));
    }
}

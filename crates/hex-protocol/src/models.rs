//! Strongly typed ACP JSON-RPC 2.0 protocol models for HEX Agent.

use std::fmt;
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// JSON-RPC 2.0 Core Types
// ---------------------------------------------------------------------------

/// JSON-RPC 2.0 Request ID (can be numeric or string).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(untagged)]
pub enum RequestId {
    Number(i64),
    String(String),
}

impl fmt::Display for RequestId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RequestId::Number(n) => write!(f, "{}", n),
            RequestId::String(s) => write!(f, "{}", s),
        }
    }
}

impl From<i64> for RequestId {
    fn from(n: i64) -> Self {
        RequestId::Number(n)
    }
}

impl From<i32> for RequestId {
    fn from(n: i32) -> Self {
        RequestId::Number(n as i64)
    }
}

impl From<u64> for RequestId {
    fn from(n: u64) -> Self {
        RequestId::Number(n as i64)
    }
}

impl From<u32> for RequestId {
    fn from(n: u32) -> Self {
        RequestId::Number(n as i64)
    }
}

impl From<usize> for RequestId {
    fn from(n: usize) -> Self {
        RequestId::Number(n as i64)
    }
}

impl From<String> for RequestId {
    fn from(s: String) -> Self {
        RequestId::String(s)
    }
}

impl From<&str> for RequestId {
    fn from(s: &str) -> Self {
        RequestId::String(s.to_string())
    }
}

/// JSON-RPC 2.0 Request object.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcRequest {
    pub jsonrpc: String,
    pub id: Option<RequestId>,
    pub method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub params: Option<serde_json::Value>,
}

impl JsonRpcRequest {
    pub fn new(id: impl Into<RequestId>, method: impl Into<String>, params: Option<serde_json::Value>) -> Self {
        Self {
            jsonrpc: "2.0".to_string(),
            id: Some(id.into()),
            method: method.into(),
            params,
        }
    }

    pub fn notification(method: impl Into<String>, params: Option<serde_json::Value>) -> Self {
        Self {
            jsonrpc: "2.0".to_string(),
            id: None,
            method: method.into(),
            params,
        }
    }
}

/// JSON-RPC 2.0 Response object.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcResponse {
    pub jsonrpc: String,
    #[serde(default)]
    pub id: Option<RequestId>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<JsonRpcError>,
}

impl JsonRpcResponse {
    pub fn success(id: Option<RequestId>, result: serde_json::Value) -> Self {
        Self {
            jsonrpc: "2.0".to_string(),
            id,
            result: Some(result),
            error: None,
        }
    }

    pub fn error(id: Option<RequestId>, error: JsonRpcError) -> Self {
        Self {
            jsonrpc: "2.0".to_string(),
            id,
            result: None,
            error: Some(error),
        }
    }

    pub fn is_success(&self) -> bool {
        self.error.is_none() && self.result.is_some()
    }
}

/// JSON-RPC 2.0 Error object.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JsonRpcError {
    pub code: i64,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<serde_json::Value>,
}

impl fmt::Display for JsonRpcError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "JSON-RPC error {}: {}", self.code, self.message)
    }
}

impl std::error::Error for JsonRpcError {}

/// JSON-RPC 2.0 Notification object.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcNotification {
    pub jsonrpc: String,
    pub method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub params: Option<serde_json::Value>,
}

// ---------------------------------------------------------------------------
// Roles, Messages & Content Blocks
// ---------------------------------------------------------------------------

/// Role of a participant in dialogue.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    User,
    Assistant,
    Tool,
    System,
}

impl fmt::Display for Role {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Role::User => write!(f, "user"),
            Role::Assistant => write!(f, "assistant"),
            Role::Tool => write!(f, "tool"),
            Role::System => write!(f, "system"),
        }
    }
}

/// Multimodal content block in ACP messages.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ContentBlock {
    #[serde(rename = "text")]
    Text { text: String },
    #[serde(rename = "image")]
    Image {
        data: String,
        #[serde(rename = "mimeType")]
        mime_type: String,
    },
    #[serde(rename = "resource")]
    Resource {
        uri: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        text: Option<String>,
    },
    #[serde(untagged)]
    Other(serde_json::Value),
}

impl ContentBlock {
    pub fn text(t: impl Into<String>) -> Self {
        ContentBlock::Text { text: t.into() }
    }

    pub fn as_text(&self) -> Option<&str> {
        match self {
            ContentBlock::Text { text } => Some(text),
            _ => None,
        }
    }
}

/// High-level conversational message.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Message {
    #[serde(default = "default_id")]
    pub id: String,
    pub role: Role,
    pub content: Vec<ContentBlock>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<f64>,
}

fn default_id() -> String {
    format!("msg_{}", std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis())
}

impl Message {
    pub fn user(text: impl Into<String>) -> Self {
        Self {
            id: default_id(),
            role: Role::User,
            content: vec![ContentBlock::text(text)],
            timestamp: None,
        }
    }

    pub fn assistant(text: impl Into<String>) -> Self {
        Self {
            id: default_id(),
            role: Role::Assistant,
            content: vec![ContentBlock::text(text)],
            timestamp: None,
        }
    }

    pub fn system(text: impl Into<String>) -> Self {
        Self {
            id: default_id(),
            role: Role::System,
            content: vec![ContentBlock::text(text)],
            timestamp: None,
        }
    }

    pub fn text_content(&self) -> String {
        self.content
            .iter()
            .filter_map(|b| b.as_text())
            .collect::<Vec<_>>()
            .join("\n")
    }
}

/// Status of an ACP session or prompt execution.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SessionStatus {
    Idle,
    Running,
    Completed,
    Failed,
    Cancelled,
}

impl fmt::Display for SessionStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SessionStatus::Idle => write!(f, "idle"),
            SessionStatus::Running => write!(f, "running"),
            SessionStatus::Completed => write!(f, "completed"),
            SessionStatus::Failed => write!(f, "failed"),
            SessionStatus::Cancelled => write!(f, "cancelled"),
        }
    }
}

// ---------------------------------------------------------------------------
// ACP Protocol Requests and Responses
// ---------------------------------------------------------------------------

/// Client info sent in `initialize`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClientInfo {
    pub name: String,
    pub version: String,
}

/// Client capabilities sent in `initialize`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClientCapabilities {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub streaming: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tools: Option<bool>,
}

/// Parameters for `initialize` method.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InitializeParams {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub protocol_version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub client_info: Option<ClientInfo>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub capabilities: Option<ClientCapabilities>,
}

/// Server info returned by `initialize`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServerInfo {
    pub name: String,
    pub version: String,
}

/// Session capabilities returned by server.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionCapabilities {
    pub streaming: bool,
    pub cancel: bool,
    pub history: bool,
}

/// Tools capabilities returned by server.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolsCapabilities {
    pub list_changed: bool,
}

/// Capabilities descriptor in InitializeResponse.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ServerCapabilities {
    pub session: SessionCapabilities,
    pub tools: ToolsCapabilities,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prompts: Option<serde_json::Value>,
}

/// Result returned from `initialize` call.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InitializeResponse {
    pub protocol_version: String,
    pub capabilities: ServerCapabilities,
    pub server_info: ServerInfo,
}

/// Parameters for `session/new`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionNewParams {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub root_uri: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
}

/// Result returned from `session/new`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionNewResponse {
    pub session_id: String,
    pub cwd: String,
    pub model: String,
    #[serde(default)]
    pub provider: Option<String>,
    #[serde(default)]
    pub tools_count: Option<usize>,
    #[serde(default)]
    pub skills_count: Option<usize>,
    #[serde(default)]
    pub cow_backend: Option<String>,
    #[serde(default)]
    pub splash_text: Option<String>,
}

/// Parameters for `session/load`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionLoadParams {
    pub session_id: String,
}

/// Result returned from `session/load`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionLoadResponse {
    pub session_id: String,
    pub cwd: String,
    pub model: String,
    pub history_count: usize,
    /// Prior turns (prompt/response pairs) so clients can replay the transcript.
    #[serde(default)]
    pub history: Vec<SessionHistoryTurn>,
}

/// One completed turn in a session transcript.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionHistoryTurn {
    // The server stores transcript turns with snake_case keys.
    #[serde(default, rename = "task_id")]
    pub task_id: Option<String>,
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub response: String,
    #[serde(default)]
    pub status: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PagedHistoryTurn {
    #[serde(flatten)]
    pub turn: SessionHistoryTurn,
    pub turn_index: usize,
    pub prompt_chars: usize,
    pub response_chars: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct HistoryPage {
    pub history: Vec<PagedHistoryTurn>,
    pub history_count: usize,
    #[serde(default)]
    pub next_before: Option<usize>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PagedSessionLoad {
    pub session_id: String,
    pub cwd: String,
    pub model: String,
    #[serde(flatten)]
    pub page: HistoryPage,
}

/// Parameters for `session/prompt`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionPromptParams {
    pub session_id: String,
    pub prompt: serde_json::Value,
    /// Operator-attached local file paths; the runtime reads each one with a
    /// size budget and appends it to the prompt. Empty means no attachments.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub attachments: Vec<String>,
}

impl SessionPromptParams {
    pub fn new_text(session_id: impl Into<String>, prompt_text: impl Into<String>) -> Self {
        Self {
            session_id: session_id.into(),
            prompt: serde_json::Value::String(prompt_text.into()),
            attachments: Vec::new(),
        }
    }

    pub fn new_text_with_attachments(
        session_id: impl Into<String>,
        prompt_text: impl Into<String>,
        attachments: Vec<String>,
    ) -> Self {
        Self {
            session_id: session_id.into(),
            prompt: serde_json::Value::String(prompt_text.into()),
            attachments,
        }
    }
}

/// Result returned from `session/prompt`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionPromptResponse {
    pub session_id: String,
    pub status: String,
    pub content: Vec<ContentBlock>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_chars: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn_index: Option<usize>,
}

impl SessionPromptResponse {
    pub fn text_content(&self) -> String {
        self.content
            .iter()
            .filter_map(|c| c.as_text())
            .collect::<Vec<_>>()
            .join("\n")
    }

    pub fn is_completed(&self) -> bool {
        self.status == "completed" || self.status == "succeeded"
    }
}

/// Parameters for `session/cancel`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionCancelParams {
    pub session_id: String,
}

/// Result returned from `session/cancel`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionCancelResponse {
    pub session_id: String,
    pub cancelled: bool,
}

/// Summary item in `session/list`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionListItem {
    pub session_id: String,
    pub cwd: String,
    pub model: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<f64>,
    pub turns: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub archived: bool,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub pinned: bool,
}

/// Result returned from `session/list`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionListResponse {
    pub sessions: Vec<SessionListItem>,
}

/// Result of `session/rename`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionRenameResponse {
    pub session_id: String,
    pub title: String,
}

/// Result of `session/delete`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionDeleteResponse {
    pub session_id: String,
    pub deleted: bool,
}

/// Result of `session/archive` / `session/pin` (value echoes the applied flag).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionFlagResponse {
    pub session_id: String,
    pub archived: bool,
    pub pinned: bool,
}

// -- Settings surface (`config/get`, `config/set`) -------------------------
// The Python side validates against a whitelist (CONFIG_SCHEMA) and only ever
// echoes whitelisted keys back, so these typed structs can safely skip absent
// fields ("not set" on the desktop) without ever carrying secrets.

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
pub struct ModelSettings {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cloud_provider: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cloud_model_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reasoning_effort: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub profile: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub compressor_mode: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cloud_base_url: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
pub struct RuntimeSettings {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_turns: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
pub struct WebSearchSettings {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
pub struct CloudSettings {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub base_url: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
pub struct LearningSettings {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auto_reflect: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
pub struct SettingsConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<ModelSettings>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub runtime: Option<RuntimeSettings>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub web_search: Option<WebSearchSettings>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cloud: Option<CloudSettings>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub learning: Option<LearningSettings>,
}

/// Result of `config/get` / `config/set`. `options` maps dotted keys to the
/// backend's allowed choice vocabularies (single source of truth for pickers).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ConfigGetResponse {
    pub path: String,
    pub workspace: String,
    pub data_dir: String,
    pub config: SettingsConfig,
    pub options: serde_json::Value,
}

/// Result of `config/set`; `changed` lists the dotted keys written.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ConfigSetResponse {
    pub path: String,
    pub workspace: String,
    pub data_dir: String,
    pub config: SettingsConfig,
    pub options: serde_json::Value,
    pub changed: Vec<String>,
}

// -- Cloud account (`cloud/auth/*`, `cloud/devices/*`) --------------------
// The Python ACP server returns these raw snake_case dicts from
// cloud/session.py; field names mirror hex-backend's device rows.

/// Local cloud account/device session state (`cloud/auth/status`, `cloud/auth/login`, `cloud/auth/logout`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CloudAuthStatus {
    pub base_url: String,
    pub logged_in: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub user_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub device_record_id: Option<String>,
    #[serde(default)]
    pub device_enrolled: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub platform: Option<String>,
}

/// One enrolled device row as stored by hex-backend (`cloud/devices/list`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CloudDevice {
    pub device_record_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub user_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub encrypted_device_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub device_pubkey: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub platform: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trust_state: Option<String>,
    #[serde(default)]
    pub host_eligible: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub created_at: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_seen_at: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revoked_at: Option<i64>,
}

/// Result returned from `cloud/devices/list`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CloudDevicesList {
    pub devices: Vec<CloudDevice>,
}

/// Result returned from `cloud/devices/revoke`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CloudRevokeResult {
    pub revoked: bool,
    pub device_record_id: String,
}

/// Push notification emitted by the ACP server as `cloud/event`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CloudEvent {
    pub kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub device_record_id: Option<String>,
}

/// One available component update from the signed hex-backend release check.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct CloudUpdateItem {
    pub component: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub installed_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mandatory: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub release_notes: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rollback_to: Option<String>,
}

/// Verified verdict payload from `cloud/update/check`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct CloudUpdatePlan {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub apply_allowed: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mandatory_upgrade_required: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub channel: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub updates: Option<Vec<CloudUpdateItem>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rollbacks: Option<Vec<serde_json::Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub compatibility_issues: Option<Vec<serde_json::Value>>,
}

/// Verified remote-config bundle from `cloud/config/evaluate`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct CloudFlags {
    #[serde(default)]
    pub flags: std::collections::BTreeMap<String, bool>,
    #[serde(default)]
    pub kill_switches: std::collections::BTreeMap<String, serde_json::Value>,
}

/// One model in the live backend catalog (`session/models`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CatalogModel {
    pub id: String,
    pub display_name: String,
    pub provider: String,
    pub is_local: bool,
}

/// Result returned from `session/models`: live discovery, never a hardcoded list.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelCatalog {
    pub session_id: String,
    pub active_model: String,
    pub provider: String,
    pub models: Vec<CatalogModel>,
    /// Partial discovery failure: some providers could not be queried.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Tool definition in `tools/list`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    #[serde(default)]
    pub parameters: serde_json::Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub risk_class: Option<String>,
}

/// Result returned from `tools/list`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolsListResponse {
    pub tools: Vec<ToolDefinition>,
}

/// Parameters for `tools/call`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolCallParams {
    pub name: String,
    #[serde(default)]
    pub arguments: serde_json::Value,
}

/// Result returned from `tools/call`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolCallResponse {
    pub content: Vec<ContentBlock>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_error: Option<bool>,
}

impl ToolCallResponse {
    pub fn output_text(&self) -> String {
        self.content
            .iter()
            .filter_map(|c| c.as_text())
            .collect::<Vec<_>>()
            .join("\n")
    }
}

/// Streaming notification payloads (`session/update`).
///
/// Canonical wire contract emitted by the Python ACP server
/// (`harness_core.client.acp`), shared by all HEX Rust clients:
/// - `updateType=agent_response` with `delta`: streaming assistant text.
/// - `updateType=agent_thought` with `delta`: streaming `<think>` reasoning;
///   only emitted when the model actually produced think-tag content.
/// - `updateType=tool` with nested `tool` ([`ToolUpdate`]): tool lifecycle.
/// - `updateType=stream_end` with `status=Ready`: token stream finished.
/// - `updateType=status` with `status=Ready` (and `delta` when nothing
///   streamed): authoritative end of the prompt turn.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionUpdateNotification {
    pub session_id: String,
    pub update_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub delta: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub usage: Option<SessionUsage>,
}

/// Token usage of the completed turn, attached by the Python runtime to the
/// terminal `session/update` status. All fields come from the router's last
/// routed call — the UI never fabricates numbers when this is absent.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionUsage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub context_limit: Option<u64>,
}

impl SessionUpdateNotification {
    /// True when this notification terminates the active prompt turn.
    pub fn is_turn_terminal(&self) -> bool {
        if self.update_type == "stream_end" {
            return true;
        }
        if self.update_type == "status" {
            if let Some(status) = self.status.as_deref() {
                return status.eq_ignore_ascii_case("ready")
                    || status.eq_ignore_ascii_case("completed")
                    || status.eq_ignore_ascii_case("cancelled")
                    || status.eq_ignore_ascii_case("failed");
            }
        }
        false
    }
}

/// Nested tool lifecycle payload inside `session/update` (`updateType=tool`).
///
/// `event_type` is `start`, `complete`, or `error`; `call_id` correlates the
/// start event with its completion, and `payload` is a truncated JSON/text
/// rendering of the tool arguments (start) or result (complete/error). File
/// edit tools attach a bounded unified diff to their completion event.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolUpdate {
    pub event_type: String,
    pub tool_id: String,
    pub call_id: String,
    #[serde(default)]
    pub duration_ms: u64,
    #[serde(default)]
    pub payload: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub diff: Option<String>,
}

/// Parameters for `session/approvalRequest` notification sent from server to client.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ApprovalRequestNotification {
    pub session_id: String,
    pub approval_id: String,
    pub tool_name: String,
    #[serde(default)]
    pub risk_level: String,
    #[serde(default)]
    pub parameters: String,
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub blast_radius: String,
    #[serde(default)]
    pub cwd: String,
}

/// Parameters for `session/approvalDecision` method request sent from client to server.
///
/// `approve_scope` grants only matching calls within the backend's task/session lease.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ApprovalDecisionParams {
    pub approval_id: String,
    pub decision: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub lease_ttl_seconds: Option<u64>,
}

/// Response returned from `session/approvalDecision`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ApprovalDecisionResponse {
    pub approval_id: String,
    pub status: String,
    pub decision: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub applied: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outcome: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lease_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lease_ttl_seconds: Option<u64>,
}

/// Parameters for `session/command` method request sent from client to server.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionCommandParams {
    pub session_id: String,
    pub command: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub args: Option<String>,
}

/// Response returned from `session/command`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionCommandResponse {
    pub session_id: String,
    pub command: String,
    pub output: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_error: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolResultPageParams {
    pub session_id: String,
    pub task_id: String,
    pub call_id: String,
    pub offset: usize,
    pub limit: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolResultPage {
    pub content: String,
    pub next_offset: Option<usize>,
    pub total_chars: usize,
}

/// Parameters for `session/subagents`, the cockpit's agents drill-down query.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionSubagentsParams {
    pub session_id: String,
}

/// Structured outcome of a finished subagent run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SubagentResultInfo {
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub summary: String,
    #[serde(default)]
    pub findings: Vec<String>,
    #[serde(default)]
    pub diff_summary: String,
    #[serde(default)]
    pub artifacts: Vec<String>,
    #[serde(default)]
    pub duration_seconds: f64,
    #[serde(default)]
    pub error: Option<String>,
}

/// One subagent record as the server's SubagentManager holds it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SubagentInfo {
    pub subagent_id: String,
    #[serde(default)]
    pub parent_task_id: String,
    #[serde(default)]
    pub role: String,
    #[serde(default)]
    pub goal: String,
    #[serde(default)]
    pub depth: u32,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub started_at: f64,
    #[serde(default)]
    pub ended_at: Option<f64>,
    #[serde(default)]
    pub interrupted: bool,
    #[serde(default)]
    pub tool_calls_count: u64,
    #[serde(default)]
    pub result: Option<SubagentResultInfo>,
}

impl SubagentInfo {
    /// True while the run is still in flight (the modal shows a spinner).
    pub fn is_running(&self) -> bool {
        matches!(self.status.as_str(), "pending" | "running")
    }

    /// Wall-clock seconds the run has taken, frozen once it ends.
    pub fn elapsed_seconds(&self, now_epoch: f64) -> u64 {
        // The server sends 0.0 when it never recorded a start; measuring from
        // the epoch would print a billion-second run.
        if self.started_at <= 0.0 {
            return 0;
        }
        let end = self.ended_at.unwrap_or(now_epoch);
        (end - self.started_at).max(0.0).round() as u64
    }
}

/// Response returned from `session/subagents`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionSubagentsResponse {
    pub session_id: String,
    #[serde(default)]
    pub subagents: Vec<SubagentInfo>,
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_request_id_serialization() {
        let n = RequestId::Number(42);
        let s = RequestId::String("req_1".to_string());

        assert_eq!(serde_json::to_string(&n).unwrap(), "42");
        assert_eq!(serde_json::to_string(&s).unwrap(), "\"req_1\"");

        let de_n: RequestId = serde_json::from_str("42").unwrap();
        assert_eq!(de_n, RequestId::Number(42));

        let de_s: RequestId = serde_json::from_str("\"abc\"").unwrap();
        assert_eq!(de_s, RequestId::String("abc".to_string()));
    }

    #[test]
    fn test_jsonrpc_request_response_roundtrip() {
        let req = JsonRpcRequest::new(1, "initialize", None);
        let req_json = serde_json::to_string(&req).unwrap();
        assert!(req_json.contains("\"jsonrpc\":\"2.0\""));
        assert!(req_json.contains("\"method\":\"initialize\""));

        let resp = JsonRpcResponse::success(
            Some(RequestId::Number(1)),
            serde_json::json!({"status": "ok"}),
        );
        let resp_json = serde_json::to_string(&resp).unwrap();
        assert!(resp.is_success());
        assert!(resp_json.contains("\"result\":{\"status\":\"ok\"}"));
    }

    #[test]
    fn test_initialize_response_deserialization() {
        let raw = r#"{
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "session": {
                    "streaming": true,
                    "cancel": true,
                    "history": true
                },
                "tools": {
                    "listChanged": false
                },
                "prompts": {}
            },
            "serverInfo": {
                "name": "HEX",
                "version": "0.20.1"
            }
        }"#;

        let init_resp: InitializeResponse = serde_json::from_str(raw).unwrap();
        assert_eq!(init_resp.protocol_version, "2024-11-05");
        assert_eq!(init_resp.server_info.name, "HEX");
        assert!(init_resp.capabilities.session.streaming);
        assert!(!init_resp.capabilities.tools.list_changed);
    }

    #[test]
    fn test_session_prompt_response_deserialization() {
        let raw = r#"{
            "sessionId": "acp_sess_12345",
            "status": "completed",
            "content": [
                {
                    "type": "text",
                    "text": "Hello, world!"
                }
            ],
            "taskId": "tsk_acp_67890"
        }"#;

        let resp: SessionPromptResponse = serde_json::from_str(raw).unwrap();
        assert_eq!(resp.session_id, "acp_sess_12345");
        assert_eq!(resp.text_content(), "Hello, world!");
        assert!(resp.is_completed());
        assert_eq!(resp.task_id.as_deref(), Some("tsk_acp_67890"));
    }

    #[test]
    fn test_tools_list_response_roundtrip() {
        let raw = r#"{
            "tools": [
                {
                    "name": "read_file",
                    "description": "Read file contents",
                    "parameters": {"type": "object"},
                    "risk_class": "R0"
                }
            ]
        }"#;

        let tools: ToolsListResponse = serde_json::from_str(raw).unwrap();
        assert_eq!(tools.tools.len(), 1);
        assert_eq!(tools.tools[0].name, "read_file");
        assert_eq!(tools.tools[0].risk_class.as_deref(), Some("R0"));
    }

    #[test]
    fn test_session_load_response_with_history() {
        let raw = r#"{
            "sessionId": "acp_sess_12345",
            "cwd": "/tmp/ws",
            "model": "gemini-2.5-pro",
            "historyCount": 1,
            "history": [
                {
                    "task_id": "tsk_acp_1",
                    "prompt": "hello",
                    "response": "hi there",
                    "status": "completed",
                    "duration": 0.5
                }
            ]
        }"#;
        let resp: SessionLoadResponse = serde_json::from_str(raw).unwrap();
        assert_eq!(resp.history_count, 1);
        assert_eq!(resp.history.len(), 1);
        assert_eq!(resp.history[0].prompt, "hello");
        assert_eq!(resp.history[0].response, "hi there");
        assert_eq!(resp.history[0].task_id.as_deref(), Some("tsk_acp_1"));
    }

    #[test]
    fn paged_history_preserves_full_lengths_and_older_cursor() {
        let page: PagedSessionLoad = serde_json::from_value(serde_json::json!({
            "sessionId": "ses_1", "cwd": "/tmp", "model": "fixture",
            "historyCount": 100, "nextBefore": 99,
            "history": [{"turnIndex": 99, "task_id": "tsk_99", "prompt": "hello", "response": "世界", "status": "completed", "promptChars": 5000, "responseChars": 9000}]
        })).unwrap();
        assert_eq!(page.page.history_count, 100);
        assert_eq!(page.page.next_before, Some(99));
        assert_eq!(page.page.history[0].turn_index, 99);
        assert_eq!(page.page.history[0].turn.task_id.as_deref(), Some("tsk_99"));
        assert_eq!(page.page.history[0].response_chars, 9000);
    }

    #[test]
    fn test_session_load_response_without_history_field() {
        let raw = r#"{
            "sessionId": "acp_sess_12345",
            "cwd": "/tmp/ws",
            "model": "default",
            "historyCount": 0
        }"#;
        let resp: SessionLoadResponse = serde_json::from_str(raw).unwrap();
        assert!(resp.history.is_empty());
    }

    #[test]
    fn tool_update_diff_is_optional_on_the_wire() {
        let without: ToolUpdate = serde_json::from_value(serde_json::json!({
            "eventType": "complete",
            "toolId": "t1",
            "callId": "c1",
            "payload": "wrote file"
        }))
        .unwrap();
        assert!(without.diff.is_none());

        let with: ToolUpdate = serde_json::from_value(serde_json::json!({
            "eventType": "complete",
            "toolId": "t1",
            "callId": "c1",
            "payload": "wrote file",
            "diff": "--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-old\n+new"
        }))
        .unwrap();
        assert_eq!(
            with.diff.as_deref(),
            Some("--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-old\n+new")
        );

        let serialized = serde_json::to_value(&without).unwrap();
        assert!(serialized.get("diff").is_none());
    }

    #[test]
    fn session_prompt_attachments_roundtrip() {
        let plain = SessionPromptParams::new_text("ses_1", "hi");
        let plain_json = serde_json::to_value(&plain).unwrap();
        assert!(plain_json.get("attachments").is_none());

        let attached = SessionPromptParams::new_text_with_attachments(
            "ses_1",
            "review this",
            vec!["C:/tmp/a.py".to_string(), "C:/tmp/b.md".to_string()],
        );
        let attached_json = serde_json::to_value(&attached).unwrap();
        assert_eq!(
            attached_json.get("attachments").unwrap(),
            &serde_json::json!(["C:/tmp/a.py", "C:/tmp/b.md"])
        );

        let de: SessionPromptParams = serde_json::from_value(attached_json).unwrap();
        assert_eq!(de.attachments.len(), 2);
    }
}

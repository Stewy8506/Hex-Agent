//! Strongly typed ACP JSON-RPC 2.0 protocol models for HEX Autonomous AI Workbench.

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
    pub tools_count: Option<usize>,
    #[serde(default)]
    pub skills_count: Option<usize>,
    #[serde(default)]
    pub cow_backend: Option<String>,
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
}

/// Parameters for `session/prompt`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionPromptParams {
    pub session_id: String,
    pub prompt: serde_json::Value,
}

impl SessionPromptParams {
    pub fn new_text(session_id: impl Into<String>, prompt_text: impl Into<String>) -> Self {
        Self {
            session_id: session_id.into(),
            prompt: serde_json::Value::String(prompt_text.into()),
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
}

/// Result returned from `session/list`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionListResponse {
    pub sessions: Vec<SessionListItem>,
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

/// Streaming notification payloads (`session/update`, `progress`, etc.).
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
}

/// Parameters for `session/approvalDecision` method request sent from client to server.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ApprovalDecisionParams {
    pub approval_id: String,
    pub decision: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

/// Response returned from `session/approvalDecision`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ApprovalDecisionResponse {
    pub approval_id: String,
    pub status: String,
    pub decision: String,
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
}

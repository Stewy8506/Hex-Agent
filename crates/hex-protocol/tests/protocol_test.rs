//! Integration test suite for `hex-protocol`.

use std::collections::HashMap;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::Mutex;

use hex_protocol::{
    AcpClient, ClientInfo, DiffLineType, InitializeResponse, JsonRpcRequest,
    SessionNewResponse, SessionPromptResponse, UnifiedDiff,
};

#[tokio::test]
async fn test_full_session_lifecycle() {
    let (client_read, mut server_write) = tokio::io::duplex(8192);
    let (server_read, client_write) = tokio::io::duplex(8192);

    let client = AcpClient::from_streams(client_read, client_write);

    // Mock ACP server loop
    tokio::spawn(async move {
        let mut lines = BufReader::new(server_read).lines();
        let sessions = Arc::new(Mutex::new(HashMap::new()));

        while let Ok(Some(line)) = lines.next_line().await {
            let req: JsonRpcRequest = serde_json::from_str(&line).unwrap();

            match req.method.as_str() {
                "initialize" => {
                    let resp = serde_json::json!({
                        "jsonrpc": "2.0",
                        "id": req.id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {
                                "session": {"streaming": true, "cancel": true, "history": true},
                                "tools": {"listChanged": false},
                                "prompts": {}
                            },
                            "serverInfo": {"name": "HEX", "version": "0.20.1"}
                        }
                    });
                    server_write
                        .write_all(format!("{}\n", serde_json::to_string(&resp).unwrap()).as_bytes())
                        .await
                        .unwrap();
                }
                "session/new" => {
                    let sess_id = "acp_sess_test123";
                    sessions.lock().await.insert(sess_id.to_string(), true);
                    let resp = serde_json::json!({
                        "jsonrpc": "2.0",
                        "id": req.id,
                        "result": {
                            "sessionId": sess_id,
                            "cwd": "/workspace",
                            "model": "default"
                        }
                    });
                    server_write
                        .write_all(format!("{}\n", serde_json::to_string(&resp).unwrap()).as_bytes())
                        .await
                        .unwrap();
                }
                "session/prompt" => {
                    let resp = serde_json::json!({
                        "jsonrpc": "2.0",
                        "id": req.id,
                        "result": {
                            "sessionId": "acp_sess_test123",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Code refactored successfully."
                                }
                            ],
                            "taskId": "tsk_123"
                        }
                    });
                    server_write
                        .write_all(format!("{}\n", serde_json::to_string(&resp).unwrap()).as_bytes())
                        .await
                        .unwrap();
                }
                "tools/list" => {
                    let resp = serde_json::json!({
                        "jsonrpc": "2.0",
                        "id": req.id,
                        "result": {
                            "tools": [
                                {
                                    "name": "run_command",
                                    "description": "Run shell command",
                                    "parameters": {"type": "object"},
                                    "risk_class": "R2"
                                }
                            ]
                        }
                    });
                    server_write
                        .write_all(format!("{}\n", serde_json::to_string(&resp).unwrap()).as_bytes())
                        .await
                        .unwrap();
                }
                "session/cancel" => {
                    let resp = serde_json::json!({
                        "jsonrpc": "2.0",
                        "id": req.id,
                        "result": {
                            "sessionId": "acp_sess_test123",
                            "cancelled": true
                        }
                    });
                    server_write
                        .write_all(format!("{}\n", serde_json::to_string(&resp).unwrap()).as_bytes())
                        .await
                        .unwrap();
                }
                _ => {}
            }
        }
    });

    // 1. Initialize
    let init_res: InitializeResponse = client
        .initialize(Some(ClientInfo {
            name: "TestClient".to_string(),
            version: "1.0.0".to_string(),
        }))
        .await
        .unwrap();
    assert_eq!(init_res.server_info.name, "HEX");
    assert!(init_res.capabilities.session.streaming);

    // 2. Session New
    let sess_res: SessionNewResponse = client
        .session_new(Some("/workspace"), Some("gemini-2.5-pro"))
        .await
        .unwrap();
    assert_eq!(sess_res.session_id, "acp_sess_test123");
    assert_eq!(sess_res.cwd, "/workspace");

    // 3. Tools List
    let tools = client.tools_list().await.unwrap();
    assert_eq!(tools.len(), 1);
    assert_eq!(tools[0].name, "run_command");
    assert_eq!(tools[0].risk_class.as_deref(), Some("R2"));

    // 4. Session Prompt
    let prompt_res: SessionPromptResponse = client
        .session_prompt(&sess_res.session_id, "Fix the authentication bug")
        .await
        .unwrap();
    assert_eq!(prompt_res.session_id, sess_res.session_id);
    assert_eq!(prompt_res.text_content(), "Code refactored successfully.");
    assert!(prompt_res.is_completed());

    // 5. Session Cancel
    let cancel_res = client.session_cancel(&sess_res.session_id).await.unwrap();
    assert!(cancel_res.cancelled);
}

#[tokio::test]
async fn test_concurrent_request_multiplexing() {
    let (client_read, mut server_write) = tokio::io::duplex(16384);
    let (server_read, client_write) = tokio::io::duplex(16384);

    let client = AcpClient::from_streams(client_read, client_write);

    // Spawn server that responds in reverse order or arbitrary order
    tokio::spawn(async move {
        let mut lines = BufReader::new(server_read).lines();
        let mut pending = Vec::new();

        // Read 10 requests
        for _ in 0..10 {
            if let Ok(Some(line)) = lines.next_line().await {
                let req: JsonRpcRequest = serde_json::from_str(&line).unwrap();
                pending.push(req);
            }
        }

        // Send responses in reverse order
        pending.reverse();
        for req in pending {
            let resp = serde_json::json!({
                "jsonrpc": "2.0",
                "id": req.id,
                "result": {
                    "echo_method": req.method,
                    "echo_id": req.id
                }
            });
            let resp_str = serde_json::to_string(&resp).unwrap() + "\n";
            server_write.write_all(resp_str.as_bytes()).await.unwrap();
        }
    });

    // Launch 10 concurrent requests
    let mut handles = Vec::new();
    for i in 0..10 {
        let client_clone = client.clone();
        handles.push(tokio::spawn(async move {
            let method = format!("test_method_{}", i);
            let res: serde_json::Value = client_clone
                .send_request(&method, serde_json::json!({"req_idx": i}))
                .await
                .unwrap();
            assert_eq!(res["echo_method"], method);
        }));
    }

    for h in handles {
        h.await.unwrap();
    }
}

#[test]
fn test_complex_diff_parsing_and_side_by_side() {
    let raw_diff = r#"diff --git a/crates/hex-protocol/Cargo.toml b/crates/hex-protocol/Cargo.toml
index 1111111..2222222 100644
--- a/crates/hex-protocol/Cargo.toml
+++ b/crates/hex-protocol/Cargo.toml
@@ -5,3 +5,4 @@ edition = "2021"
 [dependencies]
 serde = "1.0"
+tokio = "1.0"
diff --git a/notes.txt b/notes.txt
deleted file mode 100644
--- a/notes.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-line 1
-line 2
"#;

    let diff = UnifiedDiff::parse(raw_diff);
    assert_eq!(diff.files.len(), 2);

    // File 1
    let f1 = &diff.files[0];
    assert_eq!(f1.path(), "crates/hex-protocol/Cargo.toml");
    assert_eq!(f1.total_additions(), 1);
    assert_eq!(f1.total_deletions(), 0);

    // File 2 (deleted)
    let f2 = &diff.files[1];
    assert_eq!(f2.path(), "notes.txt");
    assert!(f2.is_deleted);
    assert_eq!(f2.total_deletions(), 2);

    // Side by side row check
    let rows = diff.side_by_side(1);
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].left_content.as_deref(), Some("line 1"));
    assert_eq!(rows[0].left_type, Some(DiffLineType::Deleted));
    assert_eq!(rows[0].right_content, None);
}

#[tokio::test]
async fn test_session_subagents_wire_contract() {
    let (client_read, mut server_write) = tokio::io::duplex(16384);
    let (server_read, client_write) = tokio::io::duplex(16384);
    let client = AcpClient::from_streams(client_read, client_write);

    // Exactly the shape harness_core.client.acp._subagent_wire emits.
    let payload = serde_json::json!({
        "sessionId": "ses_1",
        "subagents": [
            {
                "subagentId": "sub_1",
                "parentTaskId": "tsk_1",
                "role": "researcher",
                "goal": "Map the approval flow.",
                "depth": 1,
                "status": "running",
                "startedAt": 1000.0,
                "endedAt": null,
                "interrupted": false,
                "toolCallsCount": 7
            },
            {
                "subagentId": "sub_2",
                "parentTaskId": "tsk_1",
                "role": "test-verifier",
                "goal": "Verify the ordering assertions.",
                "depth": 2,
                "status": "completed",
                "startedAt": 1000.0,
                "endedAt": 1042.0,
                "interrupted": false,
                "toolCallsCount": 3,
                "result": {
                    "status": "completed",
                    "summary": "All ordering assertions hold.",
                    "findings": ["paths sort case-sensitively"],
                    "diffSummary": "1 file changed",
                    "artifacts": ["report.md"],
                    "durationSeconds": 42.0,
                    "error": null
                }
            }
        ]
    });

    tokio::spawn(async move {
        let mut lines = BufReader::new(server_read).lines();
        let line = lines.next_line().await.unwrap().unwrap();
        let req: JsonRpcRequest = serde_json::from_str(&line).unwrap();
        assert_eq!(req.method, "session/subagents");
        let params = req.params.unwrap();
        assert_eq!(params["sessionId"], "ses_1");
        let resp = serde_json::json!({"jsonrpc": "2.0", "id": req.id, "result": payload});
        let text = serde_json::to_string(&resp).unwrap() + "\n";
        server_write.write_all(text.as_bytes()).await.unwrap();
    });

    let agents = client.session_subagents("ses_1").await.unwrap();
    assert_eq!(agents.len(), 2);

    assert!(agents[0].is_running());
    assert!(agents[0].result.is_none());
    assert_eq!(agents[0].tool_calls_count, 7);
    assert_eq!(agents[0].elapsed_seconds(1042.0), 42);

    assert!(!agents[1].is_running());
    assert_eq!(agents[1].depth, 2);
    assert_eq!(agents[1].elapsed_seconds(9999.0), 42);
    let result = agents[1].result.as_ref().unwrap();
    assert_eq!(result.summary, "All ordering assertions hold.");
    assert_eq!(result.findings, vec!["paths sort case-sensitively"]);
    assert_eq!(result.diff_summary, "1 file changed");
    assert_eq!(result.artifacts, vec!["report.md"]);
    assert_eq!(result.duration_seconds, 42.0);
    assert!(result.error.is_none());
}

#[test]
fn test_session_command_model_metadata_roundtrip() {
    let payload = serde_json::json!({
        "sessionId": "s1",
        "command": "/model",
        "output": "Display text is not the model identity",
        "isError": false,
        "model": "canonical-model-y",
        "provider": "fake-provider"
    });
    let response: hex_protocol::models::SessionCommandResponse =
        serde_json::from_value(payload.clone()).unwrap();
    assert_eq!(response.model.as_deref(), Some("canonical-model-y"));
    assert_eq!(response.provider.as_deref(), Some("fake-provider"));
    assert_eq!(response.is_error, Some(false));
    assert_eq!(serde_json::to_value(response).unwrap(), payload);
}

#[test]
fn test_session_command_metadata_is_optional() {
    for payload in [
        serde_json::json!({
            "sessionId": "s1", "command": "/status", "output": "Ready"
        }),
        serde_json::json!({
            "sessionId": "s1", "command": "/status", "output": "Ready", "isError": false
        }),
        serde_json::json!({
            "sessionId": "s1", "command": "/model", "output": "Rejected", "isError": true
        }),
    ] {
        let response: hex_protocol::models::SessionCommandResponse =
            serde_json::from_value(payload.clone()).unwrap();
        assert!(response.model.is_none());
        assert!(response.provider.is_none());
        assert_eq!(serde_json::to_value(response).unwrap(), payload);
    }
}

#[test]
fn test_subagent_record_tolerates_missing_optional_fields() {
    // list_active_subagents omits the result block entirely.
    let raw = r#"{"sessionId":"s","subagents":[{"subagentId":"sub_9"}]}"#;
    let resp: hex_protocol::models::SessionSubagentsResponse = serde_json::from_str(raw).unwrap();
    let agent = &resp.subagents[0];
    assert_eq!(agent.subagent_id, "sub_9");
    assert_eq!(agent.status, "");
    assert!(!agent.is_running());
    assert!(agent.result.is_none());
    assert_eq!(agent.elapsed_seconds(50.0), 0);
}

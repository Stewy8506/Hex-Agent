//! F02 acceptance test: the real desktop chat vertical slice.
//!
//! Launches the real `ChatView` entry point in a GPUI test window, types through
//! the real keystroke/input-handler path, and talks to a scripted ACP model over
//! the canonical `session/update` wire shapes (tests/fixtures/acp_wire). Exercises:
//! submit → streamed completion with a tool card, copy, new chat, palette-driven
//! session switching with draft preservation, escape cancellation, and a failed
//! turn error state. Hermes reference: `components/assistant-ui/thread/` +
//! `app/chat/composer/` (Enter submits, Cmd+C copies the answer, Esc cancels).

#![recursion_limit = "512"]

// Import the built-in test attribute explicitly: `use gpui::*` also brings
// `gpui::test` into scope, which would otherwise re-expand the `#[test]`
// attribute emitted by the macro and recurse forever.
use core::prelude::v1::test;
use gpui::{EntityInputHandler, *};
use hex_desktop::acp_bridge::AcpBridge;
use hex_desktop::chat::{app_runtime, init_app, ChatView};
use hex_desktop::shortcuts::{About, Redo, ShortcutId, Undo};
use hex_desktop::theme::{HermesTheme, InterfaceDensity, ThemePreset};
use hex_desktop::ui::composer::{GlowCardPaint, GlowCardProbe};
use hex_desktop::ui::feed::{ApprovalCard, FeedPaintProbe, MessageItem};
use hex_desktop::ui::message::AssistantMessageView;
use hex_desktop::ui::DiffView;
use hex_desktop::ui::modals::approval::{ToolApprovalChoice, ToolApprovalModal};
use hex_desktop::ui::modals::model_picker::ModelPickerModal;
use hex_desktop::ui::modals::session_actions::{SessionActionMode, SessionActionModal};
use hex_desktop::ui::modals::settings::{
    InventoryState, ProvidersState, SettingField, SettingsModal, SettingsSection,
    SKILLS_SESSION_REQUIRED,
};
use hex_desktop::ui::modals::{ActiveModal, ModelEntry};
use hex_desktop::ui::motion::{MotionPolicy, MotionPreference, MotionSystem};
use hex_desktop::ui::reasoning::ThinkingDisclosure;
use hex_desktop::ui::session_list::SessionItem;
use hex_desktop::ui::sidebar::ProviderState;
use hex_desktop::ui::tool_card::{ToolCallCard, ToolStatus};
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, DuplexStream};
use tokio::runtime::Runtime;
use tokio::sync::Mutex as AsyncMutex;

#[derive(Default)]
struct MockState {
    session_counter: AtomicUsize,
    list_requests: AtomicUsize,
    cancel_flag: AtomicBool,
    cancel_requests: AtomicUsize,
    prompts: StdMutex<Vec<(String, String)>>,
    sessions: StdMutex<Vec<Value>>,
    renames: StdMutex<Vec<(String, String)>>,
    tools: StdMutex<Vec<Value>>,
    tool_requests: AtomicUsize,
    tool_responses: AtomicUsize,
    tools_delay_ms: AtomicU64,
    config_error: StdMutex<Option<String>>,
    config_revision: StdMutex<String>,
    config_delay_ms: AtomicU64,
    config_sets: StdMutex<Vec<Value>>,
    tools_error: StdMutex<Option<String>>,
    tool_policy_supported: AtomicBool,
    tool_policy_requests: AtomicUsize,
    tool_policy_sets: StdMutex<Vec<Value>>,
    tool_policy_error: StdMutex<Option<String>>,
    tool_policy: StdMutex<Vec<Value>>,
    tool_policy_revision: StdMutex<String>,
    skills_output: StdMutex<String>,
    skills_error: StdMutex<Option<String>>,
    skill_requests: AtomicUsize,
    skill_session_ids: StdMutex<Vec<String>>,
    providers: StdMutex<Vec<Value>>,
    provider_requests: AtomicUsize,
    provider_responses: AtomicUsize,
    provider_delay_ms: AtomicU64,
    provider_error: StdMutex<Option<String>>,
    provider_mutation_error: StdMutex<Option<String>>,
    provider_sets: StdMutex<Vec<Value>>,
    provider_endpoint_sets: StdMutex<Vec<Value>>,
    provider_deletes: StdMutex<Vec<Value>>,
}

async fn send_line(writer: &Arc<AsyncMutex<DuplexStream>>, value: Value) {
    let mut line = serde_json::to_string(&value).unwrap();
    line.push('\n');
    let mut w = writer.lock().await;
    w.write_all(line.as_bytes()).await.unwrap();
    w.flush().await.unwrap();
}

fn mock_config_result(revision: &str) -> Value {
    json!({
        "path": "~/.hex/config.yaml",
        "workspace": "/workspace",
        "dataDir": "/workspace/.hex/data",
        "config": {
            "model": {
                "cloudProvider": "anthropic",
                "cloudModelId": "claude-3-7-sonnet",
                "reasoningEffort": "medium",
                "profile": "cloud"
            },
            "runtime": { "maxTurns": 24 },
            "webSearch": { "provider": "brave" },
            "cloud": { "baseUrl": "" },
            "learning": { "autoReflect": "off" }
        },
        "options": {
            "model.reasoningEffort": ["none", "low", "medium", "high"],
            "model.profile": ["cloud", "local", "hybrid"]
        },
        "schema": {
            "version": 1,
            "fields": {
                "model.reasoningEffort": {"kind": "choice", "options": ["none", "low", "medium", "high"]},
                "runtime.maxTurns": {"kind": "int", "min": 1, "max": 1000}
            }
        },
        "revision": revision
    })
}

fn mock_tool_policy() -> Vec<Value> {
    vec![
        json!({
            "name": "write_file",
            "description": "Write a file to the workspace",
            "parameters": {"type": "object"},
            "risk_class": "R3",
            "policy_mode": "ask",
            "policy_default": true,
            "policy_writable": true
        }),
        json!({
            "name": "read_file",
            "description": "Read files from the workspace",
            "parameters": {"type": "object"},
            "risk_class": "R1",
            "policy_mode": "allow",
            "policy_default": true,
            "policy_writable": true
        }),
        json!({
            "name": "run_shell",
            "description": "Run an unrestricted command",
            "parameters": {"type": "object"},
            "risk_class": "R4",
            "policy_mode": "deny",
            "policy_default": false,
            "policy_writable": false
        }),
    ]
}

fn current_tool_policy_revision(state: &MockState) -> String {
    let stored = state.tool_policy_revision.lock().unwrap();
    if stored.is_empty() {
        "sha256:policy-v1".to_string()
    } else {
        stored.clone()
    }
}

fn mock_providers() -> Vec<Value> {
    vec![
        json!({
            "id": "anthropic",
            "name": "Anthropic",
            "category": "featured",
            "requiresApiKey": true,
            "credentialEnvVar": "ANTHROPIC_API_KEY",
            "credentialConfigured": false,
            "credentialSource": "none",
            "credentialWritable": true,
            "baseUrlEnvVar": "ANTHROPIC_BASE_URL",
            "defaultBaseUrl": "https://api.anthropic.com",
            "effectiveBaseUrl": "https://api.anthropic.com",
            "endpointSource": "default",
            "endpointEnvVar": null,
            "endpointWritable": true,
            "signupUrl": "https://console.anthropic.com/settings/keys",
            "description": "Anthropic Claude models"
        }),
        json!({
            "id": "openai",
            "name": "OpenAI",
            "category": "featured",
            "requiresApiKey": true,
            "credentialEnvVar": "OPENAI_API_KEY",
            "credentialConfigured": true,
            "credentialSource": "credentials_file",
            "credentialWritable": true,
            "baseUrlEnvVar": "OPENAI_BASE_URL",
            "defaultBaseUrl": "https://api.openai.com/v1",
            "effectiveBaseUrl": "https://openai-proxy.invalid/v1",
            "endpointSource": "credentials_file",
            "endpointEnvVar": "OPENAI_BASE_URL",
            "endpointWritable": true,
            "signupUrl": "https://platform.openai.com/api-keys",
            "description": "OpenAI models"
        }),
        json!({
            "id": "bedrock",
            "name": "Amazon Bedrock",
            "category": "cloud",
            "requiresApiKey": false,
            "credentialEnvVar": null,
            "credentialConfigured": false,
            "credentialSource": "not_supported",
            "credentialWritable": false,
            "baseUrlEnvVar": null,
            "defaultBaseUrl": "",
            "effectiveBaseUrl": "",
            "endpointSource": "default",
            "endpointEnvVar": null,
            "endpointWritable": false,
            "signupUrl": null,
            "description": "AWS-managed Bedrock credentials"
        }),
    ]
}

fn mock_providers_result(providers: Vec<Value>) -> Value {
    json!({
        "storage": {"path": "/tmp/hex-test/credentials.env", "scope": "config_dir"},
        "providers": providers
    })
}

async fn send_update(writer: &Arc<AsyncMutex<DuplexStream>>, params: Value) {
    send_line(
        writer,
        json!({"jsonrpc": "2.0", "method": "session/update", "params": params}),
    )
    .await;
}

async fn handle_request(
    method: &str,
    id: Option<Value>,
    params: Value,
    writer: Arc<AsyncMutex<DuplexStream>>,
    state: Arc<MockState>,
) {
    match method {
        "initialize" => {
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "session": {"streaming": true, "cancel": true, "history": true},
                            "tools": {"listChanged": false},
                            "features": if state.tool_policy_supported.load(Ordering::SeqCst) {
                                vec!["settings.read", "settings.write", "tools.policy.write"]
                            } else {
                                vec!["settings.read", "settings.write"]
                            }
                        },
                        "serverInfo": {"name": "harness_core_mock", "version": "1.0.0"}
                    }
                }),
            )
            .await;
        }
        "session/new" => {
            let n = state.session_counter.fetch_add(1, Ordering::SeqCst) + 1;
            let session_id = format!("ses_test_{n}");
            let cwd = params
                .get("cwd")
                .and_then(|v| v.as_str())
                .unwrap_or("/workspace")
                .to_string();
            state.sessions.lock().unwrap().push(json!({
                "sessionId": session_id,
                "cwd": cwd,
                "model": "claude-3-7-sonnet",
                "createdAt": 1_700_000_000.0 + n as f64,
                "turns": 0,
                "archived": false,
                "pinned": false
            }));
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {
                        "sessionId": session_id,
                        "cwd": params.get("cwd").and_then(|v| v.as_str()).unwrap_or("/workspace"),
                        "model": "claude-3-7-sonnet",
                        "provider": "Anthropic"
                    }
                }),
            )
            .await;
        }
        "config/get" => {
            let error = state.config_error.lock().unwrap().clone();
            if let Some(message) = error {
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "error": {"code": -32000, "message": message}
                    }),
                )
                .await;
                return;
            }
            let revision = {
                let stored = state.config_revision.lock().unwrap();
                if stored.is_empty() {
                    "sha256:mock-v1".to_string()
                } else {
                    stored.clone()
                }
            };
            let delay = state.config_delay_ms.load(Ordering::SeqCst);
            if delay > 0 {
                tokio::time::sleep(Duration::from_millis(delay)).await;
            }
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": mock_config_result(&revision)
                }),
            )
            .await;
        }
        "config/set" => {
            state.config_sets.lock().unwrap().push(params.clone());
            let current = {
                let mut stored = state.config_revision.lock().unwrap();
                if stored.is_empty() {
                    *stored = "sha256:mock-v1".to_string();
                }
                stored.clone()
            };
            if params.get("expectedRevision").and_then(Value::as_str)
                != Some(current.as_str())
            {
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "error": {
                            "code": -32001,
                            "message": "Config changed since it was loaded; reload settings and try again.",
                            "data": {"reason": "config_revision_conflict", "actualRevision": current}
                        }
                    }),
                )
                .await;
                return;
            }
            *state.config_revision.lock().unwrap() = "sha256:mock-v2".to_string();
            let changed = params["updates"]
                .as_object()
                .unwrap()
                .iter()
                .flat_map(|(section, values)| {
                    values
                        .as_object()
                        .unwrap()
                        .keys()
                        .map(move |key| format!("{section}.{key}"))
                })
                .collect::<Vec<_>>();
            let mut result = mock_config_result("sha256:mock-v2");
            result["changed"] = json!(changed);
            send_line(
                &writer,
                json!({"jsonrpc": "2.0", "id": id, "result": result}),
            )
            .await;
        }
        "tools/list" => {
            state.tool_requests.fetch_add(1, Ordering::SeqCst);
            let tools = state.tools.lock().unwrap().clone();
            let error = state.tools_error.lock().unwrap().clone();
            let delay = state.tools_delay_ms.load(Ordering::SeqCst);
            if delay > 0 {
                tokio::time::sleep(Duration::from_millis(delay)).await;
            }
            let response = if let Some(message) = error {
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "error": {"code": -32000, "message": message}
                })
            } else {
                json!({"jsonrpc": "2.0", "id": id, "result": {"tools": tools}})
            };
            send_line(&writer, response).await;
            state.tool_responses.fetch_add(1, Ordering::SeqCst);
        }
        "tools/policy/get" => {
            state.tool_policy_requests.fetch_add(1, Ordering::SeqCst);
            let mut tools = state.tool_policy.lock().unwrap().clone();
            if tools.is_empty() {
                tools = mock_tool_policy();
                *state.tool_policy.lock().unwrap() = tools.clone();
            }
            let response = json!({
                "jsonrpc": "2.0", "id": id,
                "result": {
                    "tools": tools,
                    "revision": current_tool_policy_revision(&state)
                }
            });
            send_line(&writer, response).await;
        }
        "tools/policy/set" => {
            state.tool_policy_sets.lock().unwrap().push(params.clone());
            let error = state.tool_policy_error.lock().unwrap().clone();
            if let Some(message) = error {
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "error": {"code": -32000, "message": message}
                    }),
                )
                .await;
                return;
            }
            let current_revision = current_tool_policy_revision(&state);
            if params.get("expectedRevision").and_then(Value::as_str)
                != Some(current_revision.as_str())
            {
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "error": {"code": -32001, "message": "config revision conflict"}
                    }),
                )
                .await;
                return;
            }
            let mut tools = state.tool_policy.lock().unwrap().clone();
            if tools.is_empty() {
                tools = mock_tool_policy();
            }
            let mut changed = Vec::new();
            for (name, mode) in params["updates"].as_object().unwrap() {
                let tool = tools
                    .iter_mut()
                    .find(|tool| tool["name"].as_str() == Some(name.as_str()))
                    .expect("known mock tool");
                let mode = mode.as_str().unwrap();
                let effective = if mode == "default" {
                    if tool["risk_class"].as_str() == Some("R3") {
                        "ask"
                    } else if tool["risk_class"].as_str() == Some("R4") {
                        "deny"
                    } else {
                        "allow"
                    }
                } else {
                    mode
                };
                tool["policy_mode"] = json!(effective);
                tool["policy_default"] = json!(mode == "default");
                changed.push(name.clone());
            }
            let revision = if changed.is_empty() {
                current_revision
            } else {
                let current_version = current_revision
                    .strip_prefix("sha256:policy-v")
                    .and_then(|version| version.parse::<u32>().ok())
                    .unwrap_or(1);
                let next = format!("sha256:policy-v{}", current_version + 1);
                *state.tool_policy_revision.lock().unwrap() = next.clone();
                next
            };
            *state.tool_policy.lock().unwrap() = tools.clone();
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {"tools": tools, "revision": revision, "changed": changed}
                }),
            )
            .await;
        }
        "providers/list" => {
            state.provider_requests.fetch_add(1, Ordering::SeqCst);
            let providers = state.providers.lock().unwrap().clone();
            let error = state.provider_error.lock().unwrap().clone();
            let delay = state.provider_delay_ms.load(Ordering::SeqCst);
            if delay > 0 {
                tokio::time::sleep(Duration::from_millis(delay)).await;
            }
            let response = if let Some(message) = error {
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "error": {"code": -32000, "message": message}
                })
            } else {
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": mock_providers_result(providers)
                })
            };
            send_line(&writer, response).await;
            state.provider_responses.fetch_add(1, Ordering::SeqCst);
        }
        "providers/endpoint/set" => {
            state
                .provider_endpoint_sets
                .lock()
                .unwrap()
                .push(params.clone());
            let error = state.provider_mutation_error.lock().unwrap().clone();
            if let Some(message) = error {
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "error": {"code": -32000, "message": message}
                    }),
                )
                .await;
                return;
            }
            let provider = {
                let mut providers = state.providers.lock().unwrap();
                let provider_id = params["provider"].as_str().unwrap();
                let provider = providers
                    .iter_mut()
                    .find(|provider| provider["id"].as_str() == Some(provider_id))
                    .expect("registered mock provider");
                let base_url = params["baseUrl"].as_str().unwrap();
                provider["effectiveBaseUrl"] = json!(if base_url.is_empty() {
                    provider["defaultBaseUrl"].clone()
                } else {
                    json!(base_url)
                });
                provider["endpointSource"] = json!(if base_url.is_empty() {
                    "default"
                } else {
                    "credentials_file"
                });
                provider["endpointEnvVar"] = if base_url.is_empty() {
                    Value::Null
                } else {
                    provider["baseUrlEnvVar"].clone()
                };
                provider.clone()
            };
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {"provider": provider, "changed": true}
                }),
            )
            .await;
        }
        "providers/credentials/set" => {
            state.provider_sets.lock().unwrap().push(params.clone());
            let error = state.provider_mutation_error.lock().unwrap().clone();
            if let Some(message) = error {
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "error": {"code": -32000, "message": message}
                    }),
                )
                .await;
                return;
            }
            let provider = {
                let mut providers = state.providers.lock().unwrap();
                let provider_id = params["provider"].as_str().unwrap();
                let provider = providers
                    .iter_mut()
                    .find(|provider| provider["id"].as_str() == Some(provider_id))
                    .expect("registered mock provider");
                provider["credentialConfigured"] = json!(true);
                provider["credentialSource"] = json!("credentials_file");
                provider.clone()
            };
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {"provider": provider, "changed": true}
                }),
            )
            .await;
        }
        "providers/credentials/delete" => {
            state.provider_deletes.lock().unwrap().push(params.clone());
            let error = state.provider_mutation_error.lock().unwrap().clone();
            if let Some(message) = error {
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "error": {"code": -32000, "message": message}
                    }),
                )
                .await;
                return;
            }
            let provider = {
                let mut providers = state.providers.lock().unwrap();
                let provider_id = params["provider"].as_str().unwrap();
                let provider = providers
                    .iter_mut()
                    .find(|provider| provider["id"].as_str() == Some(provider_id))
                    .expect("registered mock provider");
                provider["credentialConfigured"] = json!(false);
                provider["credentialSource"] = json!("none");
                provider.clone()
            };
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {"provider": provider, "changed": true}
                }),
            )
            .await;
        }
        "session/list" => {
            state.list_requests.fetch_add(1, Ordering::SeqCst);
            let sessions = state.sessions.lock().unwrap().clone();
            send_line(
                &writer,
                json!({"jsonrpc": "2.0", "id": id, "result": {"sessions": sessions}}),
            )
            .await;
        }
        "session/rename" => {
            let session_id = params["sessionId"].as_str().unwrap().to_string();
            let title = params["title"].as_str().unwrap().to_string();
            state
                .renames
                .lock()
                .unwrap()
                .push((session_id.clone(), title.clone()));
            {
                let mut sessions = state.sessions.lock().unwrap();
                if let Some(session) = sessions
                    .iter_mut()
                    .find(|session| session["sessionId"] == session_id)
                {
                    session["title"] = json!(title);
                }
            }
            send_line(
                &writer,
                json!({"jsonrpc": "2.0", "id": id, "result": {"sessionId": session_id, "title": title}}),
            )
            .await;
        }
        "session/prompt" => {
            let sid = params["sessionId"].as_str().unwrap().to_string();
            let text = params["prompt"].as_str().unwrap().to_string();
            state.prompts.lock().unwrap().push((sid.clone(), text.clone()));
            {
                let mut sessions = state.sessions.lock().unwrap();
                if let Some(session) = sessions
                    .iter_mut()
                    .find(|session| session["sessionId"] == sid)
                {
                    let turns = session["turns"].as_u64().unwrap_or(0) + 1;
                    session["turns"] = json!(turns);
                }
            }

            if text.contains("fail") {
                send_update(
                    &writer,
                    json!({"sessionId": sid, "status": "Failed", "updateType": "stream_end"}),
                )
                .await;
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "result": {"sessionId": sid, "status": "failed", "content": [], "taskId": "tsk_fail"}
                    }),
                )
                .await;
            } else if text.contains("slow") {
                send_update(
                    &writer,
                    json!({"sessionId": sid, "delta": "Working...", "updateType": "agent_response"}),
                )
                .await;
                // Hold the turn open until the client cancels (30s ceiling so a
                // broken cancel path fails the test deadline instead of passing).
                for _ in 0..1500 {
                    if state.cancel_flag.load(Ordering::SeqCst) {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
                send_update(
                    &writer,
                    json!({"sessionId": sid, "status": "Cancelled", "updateType": "stream_end"}),
                )
                .await;
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "result": {"sessionId": sid, "status": "cancelled", "content": [], "taskId": "tsk_slow"}
                    }),
                )
                .await;
            } else {
                // Canonical streamed turn (tests/fixtures/acp_wire/turn_streamed.jsonl).
                send_update(
                    &writer,
                    json!({"sessionId": sid, "delta": "Hello", "updateType": "agent_response"}),
                )
                .await;
                send_update(
                    &writer,
                    json!({"sessionId": sid, "delta": " world", "updateType": "agent_response"}),
                )
                .await;
                send_update(
                    &writer,
                    json!({
                        "sessionId": sid, "updateType": "tool",
                        "tool": {"callId": "tc_list_1", "toolId": "list_files", "eventType": "start",
                                 "durationMs": 0, "payload": "{\"path\": \".\"}"}
                    }),
                )
                .await;
                send_update(
                    &writer,
                    json!({
                        "sessionId": sid, "updateType": "tool",
                        "tool": {"callId": "tc_list_1", "toolId": "list_files", "eventType": "complete",
                                 "durationMs": 12, "payload": "{\"files\": [\"a.py\"]}"}
                    }),
                )
                .await;
                send_update(
                    &writer,
                    json!({"sessionId": sid, "status": "Ready", "updateType": "stream_end"}),
                )
                .await;
                send_update(
                    &writer,
                    json!({"sessionId": sid, "status": "Ready", "updateType": "status"}),
                )
                .await;
                send_line(
                    &writer,
                    json!({
                        "jsonrpc": "2.0", "id": id,
                        "result": {
                            "sessionId": sid, "status": "completed", "taskId": "tsk_ok",
                            "content": [{"type": "text", "text": "Hello world"}]
                        }
                    }),
                )
                .await;
            }
        }
        "session/command" if params["command"].as_str() == Some("/skills") => {
            let session_id = params["sessionId"].as_str().unwrap().to_string();
            state.skill_requests.fetch_add(1, Ordering::SeqCst);
            state
                .skill_session_ids
                .lock()
                .unwrap()
                .push(session_id.clone());
            let exists = state
                .sessions
                .lock()
                .unwrap()
                .iter()
                .any(|session| session["sessionId"].as_str() == Some(session_id.as_str()));
            let error = state.skills_error.lock().unwrap().clone();
            let (output, is_error) = if !exists {
                ("Skill registry requires an existing session.".to_string(), true)
            } else if let Some(message) = error {
                (message, true)
            } else {
                (state.skills_output.lock().unwrap().clone(), false)
            };
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {
                        "sessionId": session_id,
                        "command": "/skills",
                        "output": output,
                        "isError": is_error
                    }
                }),
            )
            .await;
        }
        "session/cancel" => {
            state.cancel_requests.fetch_add(1, Ordering::SeqCst);
            state.cancel_flag.store(true, Ordering::SeqCst);
            send_line(
                &writer,
                json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {"sessionId": params["sessionId"], "cancelled": true}
                }),
            )
            .await;
        }
        _ => {}
    }
}

async fn run_mock_server(reader: DuplexStream, writer: DuplexStream, state: Arc<MockState>) {
    let writer = Arc::new(AsyncMutex::new(writer));
    let mut lines = BufReader::new(reader).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let req: Value = serde_json::from_str(&line).unwrap();
        let method = req["method"].as_str().unwrap().to_string();
        let id = req.get("id").cloned();
        let params = req.get("params").cloned().unwrap_or(json!({}));
        let writer = writer.clone();
        let state = state.clone();
        // Handle concurrently so session/cancel lands while a slow prompt is open.
        tokio::spawn(async move { handle_request(&method, id, params, writer, state).await });
    }
}

fn snap<R>(vc: &VisualTestContext, view: &Entity<ChatView>, f: impl FnOnce(&ChatView) -> R) -> R {
    vc.read(|app| f(view.read(app)))
}

fn pump_until(
    vc: &mut VisualTestContext,
    view: &Entity<ChatView>,
    what: &str,
    mut cond: impl FnMut(&ChatView) -> bool,
) {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        vc.run_until_parked();
        if snap(vc, view, &mut cond) {
            return;
        }
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// (composer focused, modal focused)
fn focus_state(vc: &mut VisualTestContext, view: &Entity<ChatView>) -> (bool, bool) {
    vc.run_until_parked();
    vc.update(|window, app| {
        let v = view.read(app);
        (v.is_composer_focused(window), v.is_modal_focused(window))
    })
}

fn last_notice(view: &ChatView) -> Option<String> {
    view.shell.feed.messages.iter().rev().find_map(|m| match m {
        MessageItem::SystemNotice(n) => Some(n.clone()),
        _ => None,
    })
}

fn has_notice(view: &ChatView, prefix: &str) -> bool {
    view.shell
        .feed
        .messages
        .iter()
        .any(|m| matches!(m, MessageItem::SystemNotice(notice) if notice.starts_with(prefix)))
}

fn last_assistant(view: &ChatView) -> Option<&hex_desktop::ui::message::AssistantMessageView> {
    view.shell.feed.messages.iter().rev().find_map(|m| match m {
        MessageItem::Assistant(a) => Some(a),
        _ => None,
    })
}

fn clear_glow_probe(vc: &mut VisualTestContext) {
    vc.update(|_window, app| {
        if let Some(probe) = app.try_global::<GlowCardProbe>() {
            probe.0.lock().unwrap().clear();
        }
    });
}

/// Forces a repaint and returns distinct glow-card geometries. Animated frames
/// can repaint the same card with subpixel drift; nearby bounds are one card.
fn glow_paints(vc: &mut VisualTestContext, view: &Entity<ChatView>) -> Vec<GlowCardPaint> {
    vc.update(|_window, app| {
        view.update(app, |_, cx| cx.notify());
    });
    vc.run_until_parked();
    let paints: Vec<GlowCardPaint> = vc.read(|app| {
        app.try_global::<GlowCardProbe>()
            .map(|p| p.0.lock().unwrap().clone())
            .unwrap_or_default()
    });
    let mut distinct: Vec<GlowCardPaint> = Vec::new();
    for paint in paints {
        if !distinct.iter().any(|existing| {
            bounds_close(existing.card, paint.card) && bounds_close(existing.halo, paint.halo)
        }) {
            distinct.push(paint);
        }
    }
    distinct
}

fn bounds_close(actual: Bounds<Pixels>, expected: Bounds<Pixels>) -> bool {
    (actual.origin.x - expected.origin.x).abs() <= px(1.0)
        && (actual.origin.y - expected.origin.y).abs() <= px(1.0)
        && (actual.size.width - expected.size.width).abs() <= px(1.0)
        && (actual.size.height - expected.size.height).abs() <= px(1.0)
}

fn assert_px_close(actual: Pixels, expected: Pixels) {
    assert!(
        (actual - expected).abs() < px(0.5),
        "expected ~{expected:?}, got {actual:?}"
    );
}

fn click_debug_element(vc: &mut VisualTestContext, selector: &'static str) {
    let bounds = vc
        .debug_bounds(selector)
        .unwrap_or_else(|| panic!("missing rendered element {selector}"));
    vc.simulate_click(bounds.center(), Modifiers::none());
    vc.run_until_parked();
}

fn finish_motion(vc: &mut VisualTestContext, view: &Entity<ChatView>) {
    vc.run_until_parked();
    vc.update(|window, app| {
        view.update(app, |_, cx| cx.notify());
        window.refresh();
    });
    vc.run_until_parked();
    vc.update(|window, app| {
        let window_id = window.window_handle().window_id();
        for _ in 0..120 {
            if !app.global::<MotionSystem>().driver_active(window_id) {
                break;
            }
            app.global_mut::<MotionSystem>()
                .tick(window_id, Duration::from_millis(16));
        }
        view.update(app, |_, cx| cx.notify());
        window.refresh();
    });
    vc.run_until_parked();
    vc.update(|_window, app| {
        view.update(app, |_, cx| cx.notify());
    });
    vc.run_until_parked();
}

#[gpui::test]
async fn test_chat_vertical_slice(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();

    let (client_read, server_write) = tokio::io::duplex(64 * 1024);
    let (server_read, client_write) = tokio::io::duplex(64 * 1024);
    let state = Arc::new(MockState::default());
    rt.spawn(run_mock_server(server_read, server_write, state.clone()));

    let bridge = AcpBridge::new();
    rt.block_on(bridge.connect_streams(client_read, client_write))
        .expect("ACP handshake");
    assert!(bridge.is_connected());

    // Real entry point: same init + root view as main.rs.
    cx.update(|cx| {
        init_app(cx);
        cx.set_global(GlowCardProbe::default());
        cx.set_global(FeedPaintProbe::default());
    });
    let workspace = std::env::temp_dir();
    let (view, vc) = cx.add_window_view(|_window, cx| {
        ChatView::new(bridge.clone(), rt.handle().clone(), workspace.clone(), cx)
    });
    vc.update(|_window, cx| {
        view.update(cx, |view, cx| view.start_backend(cx));
    });

    // 1. Startup connects and lists durable sessions without creating a chat.
    pump_until(vc, &view, "session list refresh", |v| {
        v.shell.active_session_id.is_none()
            && v.shell.feed.is_empty()
            && state.list_requests.load(Ordering::SeqCst) >= 1
    });
    snap(vc, &view, |v| {
        assert_eq!(state.session_counter.load(Ordering::SeqCst), 0);
        assert!(state.sessions.lock().unwrap().is_empty());
        assert_eq!(v.shell.active_session_id, None);
        assert!(v.shell.feed.is_empty());
        assert_eq!(v.shell.sidebar.provider_status.state, ProviderState::Connected);
        assert_eq!(v.shell.composer.model_pill.provider_name, "");
        assert_eq!(v.shell.composer.model_pill.model_name, "");
        assert_ne!(v.shell.composer.model_pill.display_label(), "ready");
        assert!(v.shell.sidebar.session_list.sessions.is_empty());
    });

    // 1b. Home and Session share the same bounded 560px content column.
    // The halo overhangs the composer card by the mockup's 2px inset.
    clear_glow_probe(vc);
    let paints = glow_paints(vc, &view);
    assert_eq!(paints.len(), 1, "Home must render exactly one glow card: {paints:?}");
    assert_px_close(paints[0].card.size.width, px(560.0));
    assert_px_close(paints[0].halo.size.width, px(564.0));
    assert_px_close(paints[0].halo.size.height, paints[0].card.size.height + px(4.0));

    // 2. Type through the real input system and submit with Enter.
    vc.simulate_input("Hello HEX");
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "Hello HEX");
    vc.simulate_keystrokes("enter");
    // A duplicate submit while session/new is in flight must not create a chat.
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "streamed turn completion", |v| {
        !v.shell.composer.is_streaming
            && last_assistant(v)
                .map(|a| {
                    a.content == "Hello world"
                        && a.tool_calls.len() == 1
                        && a.tool_calls[0].status == ToolStatus::Success
                })
                .unwrap_or(false)
    });
    pump_until(vc, &view, "persisted first-prompt title", |_| {
        state
            .renames
            .lock()
            .unwrap()
            .iter()
            .any(|(session_id, title)| session_id == "ses_test_1" && title == "Hello HEX")
    });
    snap(vc, &view, |v| {
        assert_eq!(v.shell.active_session_id.as_deref(), Some("ses_test_1"));
        assert_eq!(state.session_counter.load(Ordering::SeqCst), 1);
        assert_eq!(state.prompts.lock().unwrap().len(), 1);
        assert!(matches!(&v.shell.feed.messages[0], MessageItem::User(u) if u.content == "Hello HEX"));
        let assistant = last_assistant(v).unwrap();
        assert!(!assistant.is_streaming);
        assert_eq!(assistant.model_name, "claude-3-7-sonnet");
        assert_eq!(assistant.tool_calls.len(), 1);
        assert_eq!(assistant.tool_calls[0].call_id, "tc_list_1");
        assert_eq!(assistant.tool_calls[0].status, ToolStatus::Success);
        assert!(assistant.tool_calls[0]
            .output_text
            .as_deref()
            .unwrap()
            .contains("a.py"));
        // Hermes-style auto title from the first prompt.
        assert_eq!(v.shell.sidebar.session_list.sessions[0].title, "Hello HEX");
        assert_eq!(v.composer_text(), "");
    });

    // 2a. Only the live Chat view is exposed. Files, Browser, and Terminal
    // remain hidden until they have functional panes instead of dead links.
    vc.run_until_parked();
    assert!(
        vc.debug_bounds("segment_chat").is_some(),
        "the live Chat view indicator must remain visible"
    );
    for selector in ["segment_files", "segment_browser", "segment_terminal"] {
        assert!(
            vc.debug_bounds(selector).is_none(),
            "nonfunctional pane navigation must not render: {selector}"
        );
    }

    // 2b. Composer parity: the Session screen also renders exactly one glow
    // card, now at the mockup's 560px session width (fixes D4's missing
    // session glow).
    clear_glow_probe(vc);
    let paints = glow_paints(vc, &view);
    assert_eq!(paints.len(), 1, "Session must render exactly one glow card");
    assert_px_close(paints[0].card.size.width, px(560.0));

    // 2c. Feed parity: the session feed paints the mockup's 28px canvas fade
    // across its top edge, spanning the whole feed area (D7's missing fade).
    vc.update(|_window, app| {
        if let Some(probe) = app.try_global::<FeedPaintProbe>() {
            probe.0.lock().unwrap().clear();
        }
        view.update(app, |_, cx| cx.notify());
    });
    vc.run_until_parked();
    let fade_paints = vc.read(|app| {
        app.try_global::<FeedPaintProbe>()
            .map(|p| p.0.lock().unwrap().clone())
            .unwrap_or_default()
    });
    let mut fades = Vec::new();
    for fade in fade_paints {
        if !fades.contains(&fade) {
            fades.push(fade);
        }
    }
    assert_eq!(
        fades.len(),
        1,
        "session feed must paint exactly one fade overlay"
    );
    assert_px_close(fades[0].size.height, px(28.0));
    assert!(
        fades[0].size.width >= px(560.0),
        "fade must span the feed area, got {:?}",
        fades[0].size.width
    );
    let main_bounds = vc
        .debug_bounds("session_main_pane")
        .expect("session main pane geometry");
    let sidebar_bounds = vc
        .debug_bounds("animated_sidebar")
        .expect("sidebar geometry");
    let viewport = vc.update(|window, _| window.viewport_size());
    assert_px_close(main_bounds.size.width, viewport.width - sidebar_bounds.size.width);
    assert_px_close(
        paints[0].card.origin.x,
        main_bounds.origin.x + (main_bounds.size.width - paints[0].card.size.width) / 2.0,
    );
    let toggle = vc.debug_bounds("header_toggle").unwrap();
    let indicator = vc.debug_bounds("segment_chat").unwrap();
    let more = vc.debug_bounds("header_more").unwrap();
    assert!(
        toggle.right() < indicator.left(),
        "header toggle {:?} overlaps Chat indicator {:?}",
        toggle,
        indicator
    );
    assert!(indicator.right() < more.left());
    assert!(toggle.left() >= px(80.0));

    // At the minimum supported width the pane still fills the window and the
    // composer, feed, and header retain safe gutters without overlapping.
    vc.simulate_resize(size(px(900.0), px(600.0)));
    finish_motion(vc, &view);
    clear_glow_probe(vc);
    let narrow = glow_paints(vc, &view);
    assert_eq!(narrow.len(), 1);
    assert_px_close(narrow[0].card.size.width, px(560.0));
    let narrow_main = vc.debug_bounds("session_main_pane").unwrap();
    let narrow_sidebar = vc.debug_bounds("animated_sidebar").unwrap();
    assert_px_close(narrow_sidebar.size.width, px(260.0));
    assert_px_close(narrow_main.size.width, px(640.0));
    assert_px_close(
        narrow[0].card.origin.x,
        narrow_main.origin.x + (narrow_main.size.width - px(560.0)) / 2.0,
    );
    let feed = vc.debug_bounds("feed_column").unwrap();
    assert_px_close(feed.size.width, px(560.0));
    assert!(feed.left() >= px(16.0));
    assert!(feed.right() <= px(884.0));

    click_debug_element(vc, "header_toggle");
    finish_motion(vc, &view);
    assert_eq!(
        vc.read(|app| {
            MotionSystem::value(
                app,
                hex_desktop::ui::motion::SpringKey::new(view.entity_id(), "sidebar_width"),
            )
        }),
        0.0,
    );
    assert!(vc.debug_bounds("header_toggle").unwrap().left() >= px(80.0));
    assert_px_close(
        vc.debug_bounds("session_main_pane").unwrap().size.width,
        px(900.0),
    );
    click_debug_element(vc, "header_toggle");
    finish_motion(vc, &view);
    assert_px_close(
        vc.debug_bounds("animated_sidebar").unwrap().size.width,
        px(260.0),
    );
    assert!(vc.debug_bounds("sidebar_new_chat").unwrap().top() >= px(60.0));

    // Home keeps the shared 560px composer at the minimum width, leaves room
    // for the window controls, and provides a vertical scroll viewport.
    vc.simulate_keystrokes("cmd-n");
    finish_motion(vc, &view);
    assert!(vc.debug_bounds("home_center_scroll").is_some());
    clear_glow_probe(vc);
    let home = glow_paints(vc, &view);
    assert_eq!(home.len(), 1);
    assert_px_close(home[0].card.size.width, px(560.0));
    assert!(vc.debug_bounds("home_sidebar_toggle").unwrap().left() >= px(80.0));
    click_debug_element(vc, "continue_ses_test_1");
    pump_until(vc, &view, "return from Home to first session", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_1")
    });
    vc.simulate_resize(size(px(1920.0), px(1080.0)));
    finish_motion(vc, &view);

    // 3. Cmd+C copies the assistant answer (composer focused, no selection).
    vc.simulate_keystrokes("cmd-c");
    assert_eq!(
        vc.read_from_clipboard().unwrap().text().unwrap(),
        "Hello world"
    );

    // 4. Cmd+N opens a local Home draft without creating a backend session.
    vc.simulate_keystrokes("cmd-n");
    snap(vc, &view, |v| {
        assert!(v.shell.feed.is_empty());
        assert_eq!(v.shell.active_session_id, None);
        assert!(v.shell.feed.is_empty());
        assert_eq!(v.shell.sidebar.session_list.sessions.len(), 1);
        assert_eq!(state.session_counter.load(Ordering::SeqCst), 1);
    });

    // 5. The first prompt in that draft creates and adopts the second session.
    vc.simulate_input("slow task");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "second session creation and streaming", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_2")
            && v.shell.composer.is_streaming
            && last_assistant(v).map(|a| a.content.as_str()) == Some("Working...")
    });
    snap(vc, &view, |v| {
        assert_eq!(state.session_counter.load(Ordering::SeqCst), 2);
        assert_eq!(v.shell.sidebar.session_list.sessions.len(), 2);
    });

    // 6. Draft typed in session 2 must survive switching away and back.
    vc.simulate_input("draft for later");
    assert_eq!(
        snap(vc, &view, |v| v.composer_text().to_string()),
        "draft for later"
    );

    // 7. Cmd+K palette: typing routes to the query, Enter switches session.
    vc.simulate_keystrokes("cmd-k");
    assert!(snap(vc, &view, |v| matches!(v.shell.active_modal, ActiveModal::Palette(_))));
    // Opening a modal moves focus off the composer onto the overlay.
    assert!(focus_state(vc, &view) == (false, true), "modal should own focus");
    vc.simulate_input("Hello");
    snap(vc, &view, |v| {
        let ActiveModal::Palette(p) = &v.shell.active_modal else {
            panic!("palette should be open");
        };
        assert_eq!(p.query, "Hello");
        // Keystrokes went to the palette, not the composer editor.
        assert_eq!(v.composer_text(), "draft for later");
    });
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "palette switch to first session", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_1")
    });
    // Closing the modal returns focus to the composer.
    assert!(focus_state(vc, &view) == (true, false), "composer should regain focus");
    snap(vc, &view, |v| {
        assert_eq!(v.shell.active_modal, ActiveModal::None);
        assert_eq!(v.shell.feed.len(), 2, "feed: {:?}", v.shell.feed.messages);
        assert_eq!(last_assistant(v).unwrap().content, "Hello world");
        assert_eq!(v.composer_text(), "");
    });

    // 8. Switch back by its persisted auto-title; the draft is restored.
    vc.simulate_keystrokes("cmd-k");
    vc.simulate_input("slow task");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "palette switch back to second session", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_2")
    });
    snap(vc, &view, |v| {
        assert_eq!(v.composer_text(), "draft for later");
        assert_eq!(last_assistant(v).unwrap().content, "Working...");
    });

    // 8b. Escape dismisses the palette without touching the draft; up/down
    //     inside the palette never move the composer caret or edit text.
    vc.simulate_keystrokes("cmd-k down up backspace escape");
    snap(vc, &view, |v| {
        assert_eq!(v.shell.active_modal, ActiveModal::None);
        assert_eq!(v.composer_text(), "draft for later");
    });
    assert!(focus_state(vc, &view) == (true, false));

    // 9. Select-all + backspace clears the draft through real editor actions.
    vc.simulate_keystrokes("cmd-a backspace");
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "");

    // 10. Escape cancels the running turn; the runtime acknowledges and the feed
    //     shows the authoritative cancelled state.
    vc.simulate_keystrokes("escape");
    pump_until(vc, &view, "turn cancellation", |v| {
        !v.shell.composer.is_streaming && last_notice(v).as_deref() == Some("Turn cancelled.")
    });
    assert!(state.cancel_flag.load(Ordering::SeqCst));
    assert!(state.cancel_requests.load(Ordering::SeqCst) >= 1);

    // 10. Failed turn surfaces an honest error state and re-enables the composer.
    vc.simulate_input("fail please");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "failed turn error state", |v| {
        !v.shell.composer.is_streaming
            && last_notice(v).map(|n| n.contains("failed")).unwrap_or(false)
    });
    snap(vc, &view, |v| {
        assert_eq!(
            last_notice(v).unwrap(),
            "Turn ended with status \"failed\"."
        );
        assert_eq!(v.shell.sidebar.provider_status.state, ProviderState::Connected);
    });
    vc.simulate_input("ok");
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "ok");

    // The scripted model saw exactly the prompts the UI submitted, per session.
    let prompts = state.prompts.lock().unwrap().clone();
    assert_eq!(
        prompts,
        vec![
            ("ses_test_1".to_string(), "Hello HEX".to_string()),
            ("ses_test_2".to_string(), "slow task".to_string()),
            ("ses_test_2".to_string(), "fail please".to_string()),
        ]
    );
}

fn launch_connected_chat_with_state<'a>(
    cx: &'a mut TestAppContext,
) -> (
    Runtime,
    Entity<ChatView>,
    &'a mut VisualTestContext,
    Arc<MockState>,
) {
    launch_connected_chat_with_configured_state(cx, |_| {})
}

fn launch_connected_chat_with_configured_state<'a>(
    cx: &'a mut TestAppContext,
    configure: impl FnOnce(&MockState),
) -> (
    Runtime,
    Entity<ChatView>,
    &'a mut VisualTestContext,
    Arc<MockState>,
) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let (client_read, server_write) = tokio::io::duplex(64 * 1024);
    let (server_read, client_write) = tokio::io::duplex(64 * 1024);
    let state = Arc::new(MockState::default());
    configure(&state);
    rt.spawn(run_mock_server(server_read, server_write, state.clone()));
    let bridge = AcpBridge::new();
    rt.block_on(bridge.connect_streams(client_read, client_write))
        .expect("ACP handshake");

    cx.update(|cx| {
        init_app(cx);
        cx.set_global(GlowCardProbe::default());
        cx.set_global(FeedPaintProbe::default());
    });
    let (view, vc) = cx.add_window_view(|_window, cx| {
        ChatView::new(bridge.clone(), rt.handle().clone(), std::env::temp_dir(), cx)
    });
    (rt, view, vc, state)
}

fn launch_connected_chat<'a>(
    cx: &'a mut TestAppContext,
) -> (Runtime, Entity<ChatView>, &'a mut VisualTestContext) {
    let (rt, view, vc, _state) = launch_connected_chat_with_state(cx);
    (rt, view, vc)
}

#[gpui::test]
async fn test_cmd_w_closes_window(cx: &mut TestAppContext) {
    let (_rt, _view, vc) = launch_connected_chat(cx);
    vc.simulate_keystrokes("cmd-w");
    vc.run_until_parked();
    assert!(cx.windows().is_empty(), "Cmd+W left the window open");
}

#[gpui::test]
async fn test_about_action_opens_and_dismisses(cx: &mut TestAppContext) {
    let (_rt, view, vc) = launch_connected_chat(cx);
    vc.dispatch_action(About);
    vc.run_until_parked();
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::About);
    });

    vc.simulate_keystrokes("escape");
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::None);
    });

    vc.simulate_input("open about in a session");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "session view", |view| {
        view.shell.active_session_id.as_deref() == Some("ses_test_1")
            && !view.shell.composer.is_streaming
    });
    vc.dispatch_action(About);
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::About);
    });
    vc.simulate_keystrokes("enter");
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::None);
    });
}

#[gpui::test]
fn test_modal_pointer_actions_activate_controls(cx: &mut TestAppContext) {
    cx.update(|cx| {
        init_app(cx);
        cx.set_global(MotionSystem::with_policy(MotionPolicy::new(true)));
    });
    let (view, vc) = cx.add_window_view(|_, cx| {
        ChatView::new(
            AcpBridge::new(),
            app_runtime().handle().clone(),
            std::env::temp_dir(),
            cx,
        )
    });

    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.open_command_palette();
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        assert!(matches!(view.shell.active_modal, ActiveModal::Palette(_)));
    });
    let sidebar_before = snap(vc, &view, |view| view.shell.sidebar_collapsed);
    vc.simulate_input("sidebar");
    finish_motion(vc, &view);
    click_debug_element(vc, "palette_nav_sidebar");
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::None);
        assert_ne!(view.shell.sidebar_collapsed, sidebar_before);
    });

    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.open_command_palette();
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    vc.simulate_input("max turns");
    finish_motion(vc, &view);
    click_debug_element(vc, "palette_setting_runtime.maxTurns");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("setting deep link should open settings");
        };
        assert!(modal.load_failed);
    });
    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.dismiss_modal();
            cx.notify();
        });
    });

    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::ModelPicker(ModelPickerModal::new(
                "gpt-4o",
                "OpenAI",
                vec![
                    ModelEntry::new("gpt-4o", "GPT-4o", "OpenAI", false),
                    ModelEntry::new("o3-mini", "o3-mini", "OpenAI", false),
                ],
            ));
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    click_debug_element(vc, "model_1");
    snap(vc, &view, |view| {
        assert_eq!(view.shell.active_modal, ActiveModal::None);
        assert_eq!(view.shell.composer.model_pill.model_name, "o3-mini");
        assert_eq!(view.shell.composer.model_pill.provider_name, "OpenAI");
        assert!(!last_notice(view).is_some_and(|notice| notice.contains("cannot switch model")));
    });

    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::Approval(ToolApprovalModal::new(
                "req_pointer",
                "bash",
                "cargo test",
                "R3",
            ));
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    click_debug_element(vc, "approval_choice_session");
    click_debug_element(vc, "approval_lease");
    snap(vc, &view, |view| {
        let ActiveModal::Approval(modal) = &view.shell.active_modal else {
            panic!("approval should remain open");
        };
        assert_eq!(modal.choice, ToolApprovalChoice::Session);
        assert!(modal.lease_15m);
    });
    click_debug_element(vc, "approval_deny");
    pump_until(vc, &view, "pointer approval decision", |view| {
        has_notice(view, "Approval decision failed")
    });

    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::SessionAction(
                SessionActionModal::open_menu("ses_pointer", "Pointer session", false, false),
            );
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    click_debug_element(vc, "session_menu_0");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("session actions should remain open");
        };
        assert_eq!(modal.mode, SessionActionMode::Rename);
        assert_eq!(modal.draft, "Pointer session");
    });
    click_debug_element(vc, "modal_field_chat_title");
    vc.simulate_input(" renamed");
    snap(vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("rename should remain open while editing");
        };
        assert_eq!(modal.draft, "Pointer session renamed");
    });
    click_debug_element(vc, "modal_button_rename");
    pump_until(vc, &view, "pointer session rename", |view| {
        view.shell.active_modal == ActiveModal::None && has_notice(view, "Rename failed")
    });

    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::SessionAction(
                SessionActionModal::open_menu("ses_pointer", "Pointer session", false, false),
            );
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    click_debug_element(vc, "session_menu_3");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("delete confirmation should be open");
        };
        assert_eq!(modal.mode, SessionActionMode::DeleteConfirm);
        assert!(modal.confirm_focused);
    });
    click_debug_element(vc, "modal_button_cancel");
    snap(vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("canceling delete should return to the menu");
        };
        assert_eq!(modal.mode, SessionActionMode::Menu { selected: 3 });
    });
    click_debug_element(vc, "session_menu_3");
    finish_motion(vc, &view);
    click_debug_element(vc, "modal_button_delete_forever");
    pump_until(vc, &view, "pointer session delete", |view| {
        view.shell.active_modal == ActiveModal::None
            && (has_notice(view, "Delete failed") || has_notice(view, "Delete task aborted"))
    });

    let mut settings = SettingsModal::default();
    settings.fields.insert(
        0,
        SettingField::Text {
            key: "model.cloudProvider",
            label: "Provider",
            value: "new-model".into(),
            cursor: 0,
            required: false,
        },
    );
    settings
        .originals
        .insert("model.cloudProvider".into(), "old-model".into());
    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::Settings(settings);
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    click_debug_element(vc, "settings_row_0");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert!(modal.editing);
        assert_eq!(modal.selected, 0);
    });
    click_debug_element(vc, "settings_section_runtime");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert_eq!(
            modal.active_section,
            hex_desktop::ui::modals::SettingsSection::Runtime
        );
        assert!(!modal.editing);
    });
    click_debug_element(vc, "settings_search");
    vc.simulate_input("provider");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert!(modal.searching);
        assert_eq!(modal.search_query, "provider");
        assert_eq!(modal.selected, 0);
    });
    vc.simulate_keystrokes("escape");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert!(!modal.searching);
        assert!(modal.search_query.is_empty());
    });
    vc.simulate_keystrokes("/");
    vc.simulate_input("provider");
    click_debug_element(vc, "settings_row_0");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert_eq!(
            modal.active_section,
            hex_desktop::ui::modals::SettingsSection::Model
        );
        assert!(!modal.searching);
        assert!(modal.editing);
        assert_eq!(modal.selected, 0);
    });
    click_debug_element(vc, "settings_save_row");
    pump_until(
        vc,
        &view,
        "pointer settings save",
        |view| matches!(&view.shell.active_modal, ActiveModal::Settings(modal) if modal.error.as_deref().is_some_and(|error| error.starts_with("Save failed"))),
    );
}

#[gpui::test]
fn test_offline_local_settings_save_locally(cx: &mut TestAppContext) {
    cx.update(|cx| {
        init_app(cx);
        cx.set_global(MotionSystem::with_policy(MotionPolicy::new(true)));
    });
    let (view, vc) = cx.add_window_view(|_, cx| {
        ChatView::new(
            AcpBridge::new(),
            app_runtime().handle().clone(),
            std::env::temp_dir(),
            cx,
        )
    });

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should open offline");
        };
        assert!(modal.load_failed);
        assert_eq!(
            modal.tools,
            InventoryState::Unavailable(
                "Runtime offline — connect HEX to load the tool registry.".to_string()
            )
        );
        assert_eq!(
            modal.skills,
            InventoryState::Unavailable(
                "Runtime offline — connect HEX to load the skill registry.".to_string()
            )
        );
        assert_eq!(
            modal.providers,
            ProvidersState::Unavailable(
                "Runtime offline — connect HEX to load provider connections.".to_string()
            )
        );
        let keyboard_count = ShortcutId::ALL
            .into_iter()
            .filter(|id| id.available())
            .count();
        assert_eq!(modal.fields.len(), 13 + keyboard_count);
        assert!(modal
            .fields
            .iter()
            .all(|field| field.key().starts_with("desktop.")));
    });

    click_debug_element(vc, "settings_section_tools");
    click_debug_element(vc, "settings_info_tools");
    vc.simulate_keystrokes("enter");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("offline tool inventory should remain visible");
        };
        assert_eq!(modal.active_section, SettingsSection::Tools);
        assert!(!modal.editing);
        assert_eq!(modal.dirty_count(), 0);
    });

    click_debug_element(vc, "settings_section_appearance");
    click_debug_element(vc, "settings_search");
    vc.simulate_input("theme");
    click_debug_element(vc, "settings_row_0");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert!(!modal.searching);
        assert!(modal.search_query.is_empty());
        assert_eq!(
            modal.active_section,
            hex_desktop::ui::modals::SettingsSection::Appearance
        );
    });
    click_debug_element(vc, "settings_row_0");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert_eq!(modal.desktop_dirty_count(), 1);
    });

    click_debug_element(vc, "settings_row_1");
    click_debug_element(vc, "settings_save_row");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after a local save");
        };
        assert_eq!(modal.desktop_preferences.theme, ThemePreset::Midnight);
        assert_eq!(modal.desktop_preferences.density, InterfaceDensity::Compact);
        assert_eq!(modal.dirty_updates(), json!({}));
        assert_eq!(modal.desktop_dirty_count(), 0);
        assert!(modal.error.is_none());
        assert_eq!(
            modal.status.as_deref(),
            Some("Desktop preferences saved on this device.")
        );
    });
    let theme = vc.read(|app| app.global::<HermesTheme>().clone());
    assert_eq!(theme.preset, ThemePreset::Midnight);
    assert_eq!(theme.density, InterfaceDensity::Compact);

    click_debug_element(vc, "settings_section_motion_display");
    click_debug_element(vc, "settings_row_0");
    click_debug_element(vc, "settings_row_1");
    click_debug_element(vc, "settings_save_row");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after a local save");
        };
        assert_eq!(modal.desktop_preferences.motion, MotionPreference::Reduced);
        assert!(modal.desktop_preferences.sidebar_collapsed);
        assert!(view.shell.sidebar_collapsed);
        assert_eq!(modal.dirty_updates(), json!({}));
        assert_eq!(modal.desktop_dirty_count(), 0);
        assert!(modal.error.is_none());
    });
    assert!(vc.read(MotionSystem::is_reduced));

    click_debug_element(vc, "settings_section_keyboard");
    click_debug_element(vc, "settings_row_0");
    vc.simulate_keystrokes("cmd-k");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("capturing an existing shortcut must not run its action");
        };
        assert!(modal.capturing_keybind().is_none());
        assert_eq!(modal.desktop_dirty_count(), 0);
    });
    click_debug_element(vc, "settings_row_0");
    vc.simulate_keystrokes("cmd-alt-p");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("capturing a shortcut must not open the command palette");
        };
        assert!(modal.capturing_keybind().is_none());
        assert_eq!(modal.desktop_dirty_count(), 1);
        assert!(modal.validation_error().is_none());
        assert!(matches!(
            &modal.fields[4],
            SettingField::Keybind { value: Some(value), .. }
                if value == &hex_desktop::shortcuts::canonical_keystroke("cmd-alt-p").unwrap()
        ));
    });
    click_debug_element(vc, "settings_save_row");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after saving a shortcut");
        };
        assert_eq!(modal.desktop_dirty_count(), 0);
        assert!(modal.error.is_none());
        assert_eq!(
            modal.desktop_preferences.keybindings[ShortcutId::CommandPalette.id()],
            hex_desktop::shortcuts::canonical_keystroke("cmd-alt-p").unwrap()
        );
    });

    vc.simulate_keystrokes("escape");
    vc.simulate_keystrokes("cmd-alt-p");
    snap(vc, &view, |view| {
        assert!(matches!(view.shell.active_modal, ActiveModal::Palette(_)));
    });
    vc.simulate_keystrokes("escape");
    vc.simulate_keystrokes("cmd-k");
    snap(vc, &view, |view| {
        assert!(!view.shell.active_modal.is_open());
    });
}

#[gpui::test]
fn test_settings_actions_open_existing_flows_and_preserve_dirty_edits(cx: &mut TestAppContext) {
    cx.update(|cx| {
        init_app(cx);
        cx.set_global(MotionSystem::with_policy(MotionPolicy::new(true)));
    });
    let (view, vc) = cx.add_window_view(|_, cx| {
        ChatView::new(
            AcpBridge::new(),
            app_runtime().handle().clone(),
            std::env::temp_dir(),
            cx,
        )
    });

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    click_debug_element(vc, "settings_section_appearance");
    click_debug_element(vc, "settings_row_0");
    click_debug_element(vc, "settings_section_models");
    click_debug_element(vc, "settings_action_models");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("dirty settings must not be discarded by an action");
        };
        assert_eq!(modal.dirty_count(), 1);
        assert_eq!(
            modal.error.as_deref(),
            Some("Save your changes before opening another settings view.")
        );
    });

    vc.simulate_keystrokes("escape");
    finish_motion(vc, &view);
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    click_debug_element(vc, "settings_section_models");
    click_debug_element(vc, "settings_action_models");
    snap(vc, &view, |view| {
        assert!(matches!(view.shell.active_modal, ActiveModal::ModelPicker(_)));
    });

    vc.simulate_keystrokes("escape");
    finish_motion(vc, &view);
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    click_debug_element(vc, "settings_section_sessions");
    click_debug_element(vc, "settings_action_sessions");
    snap(vc, &view, |view| {
        assert!(matches!(view.shell.active_modal, ActiveModal::SessionSearch(_)));
    });

    vc.simulate_keystrokes("escape");
    finish_motion(vc, &view);
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    click_debug_element(vc, "settings_section_cloud");
    click_debug_element(vc, "settings_action_cloud");
    snap(vc, &view, |view| {
        assert!(matches!(view.shell.active_modal, ActiveModal::Account(_)));
    });
}

#[gpui::test]
fn test_connected_settings_load_runtime_config_and_tool_inventory(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    *state.tools.lock().unwrap() = vec![
        json!({
            "name": "write_file",
            "description": "Write a file to the workspace",
            "parameters": {"type": "object"},
            "risk_class": "R3"
        }),
        json!({
            "name": "read_file",
            "description": "Read files from the workspace",
            "parameters": {"type": "object"}
        }),
    ];

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "runtime settings and tool inventory", |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            return false;
        };
        !modal.load_failed
            && matches!(modal.tools, InventoryState::Ready(_))
            && modal.fields.iter().any(|field| {
                matches!(field, SettingField::Number { value, .. } if value == "24")
            })
    });

    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert_eq!(state.tool_requests.load(Ordering::SeqCst), 1);
        assert_eq!(state.tool_policy_requests.load(Ordering::SeqCst), 0);
        assert_eq!(modal.dirty_count(), 0);
        assert_eq!(modal.dirty_updates(), json!({}));
        let info = modal
            .fields
            .iter()
            .filter(|field| {
                field.key() == "desktop.info.tools"
                    && matches!(field, SettingField::Info { .. })
            })
            .collect::<Vec<_>>();
        assert_eq!(info.len(), 2);
        assert_eq!(
            info[0],
            &SettingField::Info {
                key: "desktop.info.tools",
                label: "read_file".to_string(),
                value: "risk: unspecified".to_string(),
                description: "Read files from the workspace".to_string(),
            }
        );
        assert_eq!(info[1].label(), "write_file");
        assert_eq!(
            info[1],
            &SettingField::Info {
                key: "desktop.info.tools",
                label: "write_file".to_string(),
                value: "risk: R3".to_string(),
                description: "Write a file to the workspace".to_string(),
            }
        );
    });

    click_debug_element(vc, "settings_section_tools");
    click_debug_element(vc, "settings_info_tools");
    vc.simulate_keystrokes("enter");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("read-only tool activation should keep settings open");
        };
        assert_eq!(modal.active_section, SettingsSection::Tools);
        assert!(!modal.editing);
        assert_eq!(modal.dirty_count(), 0);
        assert_eq!(modal.selected_action(), None);
    });
}

#[gpui::test]
fn test_connected_tool_policy_loads_and_saves_with_the_shared_revision(
    cx: &mut TestAppContext,
) {
    let (_rt, view, vc, state) = launch_connected_chat_with_configured_state(cx, |state| {
        state.tool_policy_supported.store(true, Ordering::SeqCst);
    });

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "live tool policy", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if matches!(modal.tools, InventoryState::Ready(_))
                    && modal.revision.as_deref() == Some("sha256:policy-v1")
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert_eq!(state.tool_policy_requests.load(Ordering::SeqCst), 1);
        assert_eq!(state.tool_requests.load(Ordering::SeqCst), 0);
        assert_eq!(modal.dirty_count(), 0);
        assert_eq!(modal.dirty_updates(), json!({}));
    });

    click_debug_element(vc, "settings_section_tools");
    assert!(
        vc.debug_bounds("settings_tool_policy_write_file")
            .is_some_and(|bounds| bounds.size.width > px(0.0)),
        "writable policy row should render"
    );
    click_debug_element(vc, "settings_tool_policy_write_file");
    vc.simulate_keystrokes("right enter");
    pump_until(vc, &view, "tool policy save", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if !modal.busy
                    && modal.status.as_deref() == Some("write_file tool policy updated.")
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after saving tool policy");
        };
        assert_eq!(modal.revision.as_deref(), Some("sha256:policy-v2"));
        assert_eq!(modal.fields[modal.selected].label(), "write_file");
        assert!(matches!(
            &modal.fields[modal.selected],
            SettingField::ToolPolicy { policy, .. }
                if policy.mode == hex_protocol::models::ToolPolicyMode::Allow
                    && !policy.is_default
        ));
        assert_eq!(modal.dirty_count(), 0);
        assert_eq!(modal.desktop_dirty_count(), 0);
        assert_eq!(modal.dirty_updates(), json!({}));
        assert!(modal.changed_desktop_preferences().is_none());
    });
    let requests = state.tool_policy_sets.lock().unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(
        requests[0],
        json!({
            "updates": {"write_file": "allow"},
            "expectedRevision": "sha256:policy-v1"
        })
    );
    assert_eq!(state.config_sets.lock().unwrap().len(), 0);
    drop(requests);

    vc.simulate_keystrokes("cmd-z");
    assert_eq!(state.tool_policy_sets.lock().unwrap().len(), 1);
}

#[gpui::test]
fn test_tool_policy_failure_preserves_the_draft_for_retry(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_configured_state(cx, |state| {
        state.tool_policy_supported.store(true, Ordering::SeqCst);
        *state.tool_policy_error.lock().unwrap() = Some("policy write denied".to_string());
    });

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "tool policy before failed save", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if matches!(modal.tools, InventoryState::Ready(_))
                    && modal.revision.as_deref() == Some("sha256:policy-v1")
        )
    });
    click_debug_element(vc, "settings_section_tools");
    click_debug_element(vc, "settings_tool_policy_write_file");
    vc.simulate_keystrokes("right enter");
    pump_until(vc, &view, "tool policy failure", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if !modal.busy
                    && modal.error.as_deref().is_some_and(|error| {
                        error.starts_with("Tool policy save failed:")
                            && error.contains("policy write denied")
                    })
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after a failed policy save");
        };
        assert_eq!(modal.fields[modal.selected].label(), "write_file");
        assert_eq!(modal.revision.as_deref(), Some("sha256:policy-v1"));
        assert_eq!(modal.dirty_count(), 0);
        assert_eq!(modal.dirty_updates(), json!({}));
    });
    assert_eq!(state.tool_policy_sets.lock().unwrap().len(), 1);
    assert_eq!(state.config_sets.lock().unwrap().len(), 0);

    state.tool_policy_error.lock().unwrap().take();
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "tool policy retry", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if !modal.busy
                    && modal.error.is_none()
                    && modal.status.as_deref() == Some("write_file tool policy updated.")
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after retrying tool policy");
        };
        assert_eq!(modal.fields[modal.selected].label(), "write_file");
        assert!(matches!(
            &modal.fields[modal.selected],
            SettingField::ToolPolicy { policy, .. }
                if policy.mode == hex_protocol::models::ToolPolicyMode::Allow
                    && !policy.is_default
        ));
        assert_eq!(modal.revision.as_deref(), Some("sha256:policy-v2"));
        assert_eq!(modal.dirty_updates(), json!({}));
    });
    let requests = state.tool_policy_sets.lock().unwrap();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0], requests[1]);
    assert_eq!(requests[1]["updates"], json!({"write_file": "allow"}));
    assert_eq!(requests[1]["expectedRevision"], "sha256:policy-v1");
    assert_eq!(state.config_sets.lock().unwrap().len(), 0);
}

#[gpui::test]
fn test_connected_provider_credentials_save_and_confirm_removal(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    *state.providers.lock().unwrap() = mock_providers();

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "provider inventory", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if matches!(&modal.providers, ProvidersState::Ready(_, providers) if providers.len() == 3)
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert_eq!(state.provider_requests.load(Ordering::SeqCst), 1);
        assert_eq!(modal.dirty_count(), 0);
    });

    click_debug_element(vc, "settings_section_providers");
    click_debug_element(vc, "settings_provider_anthropic");
    assert_eq!(focus_state(vc, &view), (false, true));
    vc.simulate_input("sk-test-only");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("provider editing should keep settings open");
        };
        assert!(modal.editing);
        assert_eq!(
            modal.text_selection().map(|(value, _, _)| value).as_deref(),
            Some("sk-test-only")
        );
        assert_eq!(modal.dirty_count(), 0);
        assert_eq!(modal.dirty_updates(), json!({}));
    });

    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "provider credential save", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if !modal.busy
                    && modal.status.as_deref().is_some_and(|status| {
                        status == "Anthropic credential saved in /tmp/hex-test/credentials.env."
                    })
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after saving a credential");
        };
        assert!(!modal.editing);
        assert_eq!(modal.text_selection(), None);
        assert_eq!(modal.dirty_count(), 0);
        let ProvidersState::Ready(_, providers) = &modal.providers else {
            panic!("provider inventory should remain ready");
        };
        let anthropic = providers
            .iter()
            .find(|provider| provider.id == "anthropic")
            .unwrap();
        assert!(anthropic.credential_configured);
        assert_eq!(
            anthropic.credential_source,
            hex_protocol::models::ProviderCredentialSource::CredentialsFile
        );
    });
    vc.simulate_keystrokes("cmd-z");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("credential drafts must not enter modal undo history");
        };
        assert!(!modal.editing);
        assert_eq!(modal.text_selection(), None);
    });
    let requests = state.provider_sets.lock().unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0]["provider"], "anthropic");
    assert_eq!(requests[0]["credential"], "sk-test-only");
    assert_eq!(state.config_sets.lock().unwrap().len(), 0);

    click_debug_element(vc, "settings_provider_remove_anthropic");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open while confirming removal");
        };
        assert!(modal.status.as_deref().unwrap().contains("again to confirm"));
        assert!(!modal.editing);
    });
    assert_eq!(state.provider_deletes.lock().unwrap().len(), 0);

    click_debug_element(vc, "settings_provider_remove_anthropic");
    pump_until(vc, &view, "provider credential removal", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if !modal.busy
                    && modal.status.as_deref().is_some_and(|status| {
                        status == "Anthropic credential removed from /tmp/hex-test/credentials.env."
                    })
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after removing a credential");
        };
        let ProvidersState::Ready(_, providers) = &modal.providers else {
            panic!("provider inventory should remain ready");
        };
        let anthropic = providers
            .iter()
            .find(|provider| provider.id == "anthropic")
            .unwrap();
        assert!(!anthropic.credential_configured);
        assert_eq!(
            anthropic.credential_source,
            hex_protocol::models::ProviderCredentialSource::None
        );
        assert_eq!(modal.dirty_count(), 0);
    });

    vc.simulate_keystrokes("down delete");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open while confirming keyboard removal");
        };
        assert!(modal.status.as_deref().unwrap().contains("OpenAI"));
        assert!(modal.status.as_deref().unwrap().contains("again to confirm"));
    });
    assert_eq!(state.provider_deletes.lock().unwrap().len(), 1);
    vc.simulate_keystrokes("delete");
    pump_until(vc, &view, "keyboard provider credential removal", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if !modal.busy
                    && modal.status.as_deref().is_some_and(|status| {
                        status == "OpenAI credential removed from /tmp/hex-test/credentials.env."
                    })
        )
    });
    let requests = state.provider_deletes.lock().unwrap();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0], json!({"provider": "anthropic"}));
    assert_eq!(requests[1], json!({"provider": "openai"}));
    assert_eq!(state.config_sets.lock().unwrap().len(), 0);
}

#[gpui::test]
fn test_provider_credential_failure_preserves_the_masked_draft_for_retry(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    *state.providers.lock().unwrap() = mock_providers();
    *state.provider_mutation_error.lock().unwrap() = Some("write denied".to_string());

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "providers before failed save", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if matches!(&modal.providers, ProvidersState::Ready(_, providers) if providers.len() == 3)
        )
    });
    click_debug_element(vc, "settings_section_providers");
    click_debug_element(vc, "settings_provider_anthropic");
    vc.simulate_input("sk-retry-only");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "provider credential failure", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal) if !modal.busy
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after a failed credential save");
        };
        assert!(modal.error.as_deref().is_some_and(|error| error
            .starts_with("Provider credential save failed:")
            && error.contains("write denied")));
        assert!(modal.editing);
        assert_eq!(
            modal.text_selection().map(|(value, _, _)| value).as_deref(),
            Some("sk-retry-only")
        );
        assert_eq!(modal.dirty_updates(), json!({}));
    });
    assert_eq!(state.provider_sets.lock().unwrap().len(), 1);
    assert_eq!(state.config_sets.lock().unwrap().len(), 0);

    state.provider_mutation_error.lock().unwrap().take();
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "provider credential retry", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if !modal.busy
                    && modal.error.is_none()
                    && modal.status.as_deref().is_some_and(|status| status.contains("credential saved"))
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after retrying a credential save");
        };
        assert!(!modal.editing);
        assert_eq!(modal.text_selection(), None);
        assert_eq!(modal.dirty_count(), 0);
    });
    assert_eq!(state.provider_sets.lock().unwrap().len(), 2);
    assert_eq!(state.config_sets.lock().unwrap().len(), 0);
}

#[gpui::test]
fn test_provider_endpoint_save_and_model_setting_link(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    *state.providers.lock().unwrap() = mock_providers();

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "providers before endpoint edit", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if matches!(&modal.providers, ProvidersState::Ready(_, providers) if providers.len() == 3)
        )
    });
    click_debug_element(vc, "settings_section_providers");
    let endpoint_bounds = vc
        .debug_bounds("settings_provider_endpoint_anthropic")
        .expect("rendered provider endpoint control");
    assert!(
        endpoint_bounds.size.width > px(0.0),
        "endpoint control has no width"
    );
    click_debug_element(vc, "settings_provider_endpoint_anthropic");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("endpoint editing should keep settings open");
        };
        assert!(
            modal.editing,
            "endpoint edit did not start: {:?}",
            modal.error
        );
        assert_eq!(
            modal.text_selection().map(|(value, _, _)| value).as_deref(),
            Some("https://api.anthropic.com"),
            "selected={:?}, error={:?}",
            modal.fields.get(modal.selected).map(|field| field.label()),
            modal.error
        );
        assert_eq!(modal.dirty_count(), 0);
    });
    vc.simulate_keystrokes("cmd-a");
    vc.simulate_input("https://anthropic-proxy.invalid/v1");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "provider endpoint save", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if !modal.busy
                    && modal.status.as_deref() == Some(
                        "Anthropic endpoint saved in /tmp/hex-test/credentials.env."
                    )
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after saving an endpoint");
        };
        assert!(!modal.editing);
        let ProvidersState::Ready(_, providers) = &modal.providers else {
            panic!("provider inventory should remain ready");
        };
        let anthropic = providers
            .iter()
            .find(|provider| provider.id == "anthropic")
            .unwrap();
        assert_eq!(
            anthropic.effective_base_url.as_deref(),
            Some("https://anthropic-proxy.invalid/v1")
        );
        assert_eq!(modal.dirty_updates(), json!({}));
    });
    let requests = state.provider_endpoint_sets.lock().unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0]["provider"], "anthropic");
    assert_eq!(requests[0]["baseUrl"], "https://anthropic-proxy.invalid/v1");
    assert_eq!(state.config_sets.lock().unwrap().len(), 0);
    drop(requests);

    vc.simulate_keystrokes("escape");
    {
        let mut providers = state.providers.lock().unwrap();
        let anthropic = providers
            .iter_mut()
            .find(|provider| provider["id"] == "anthropic")
            .unwrap();
        anthropic["endpointSource"] = json!("model_config");
        anthropic["endpointWritable"] = json!(false);
        anthropic["effectiveBaseUrl"] = json!("https://model-config.invalid/v1");
    }
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "providers with model endpoint", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if matches!(&modal.providers, ProvidersState::Ready(_, providers) if providers.len() == 3)
        )
    });
    click_debug_element(vc, "settings_section_providers");
    click_debug_element(vc, "settings_provider_endpoint_anthropic");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("model endpoint link should keep settings open");
        };
        assert_eq!(modal.active_section, SettingsSection::Model);
        assert!(modal.editing);
        assert!(matches!(
            modal.fields.get(modal.selected),
            Some(SettingField::Text { key, .. }) if *key == "model.cloudBaseUrl"
        ));
    });
}

#[gpui::test]
fn test_provider_endpoint_failure_preserves_the_draft_for_retry(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    *state.providers.lock().unwrap() = mock_providers();
    *state.provider_mutation_error.lock().unwrap() = Some("endpoint write denied".to_string());

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "providers before endpoint failure", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if matches!(&modal.providers, ProvidersState::Ready(_, providers) if providers.len() == 3)
        )
    });
    click_debug_element(vc, "settings_section_providers");
    click_debug_element(vc, "settings_provider_endpoint_openai");
    vc.simulate_keystrokes("cmd-a");
    vc.simulate_input("https://retry-proxy.invalid/v1");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "provider endpoint failure", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal) if !modal.busy
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after a failed endpoint save");
        };
        assert!(modal
            .error
            .as_deref()
            .is_some_and(|error| error.starts_with("Provider endpoint save failed:")
                && error.contains("endpoint write denied")));
        assert!(modal.editing);
        assert_eq!(
            modal.text_selection().map(|(value, _, _)| value).as_deref(),
            Some("https://retry-proxy.invalid/v1")
        );
        assert_eq!(modal.dirty_updates(), json!({}));
    });
    assert_eq!(state.provider_endpoint_sets.lock().unwrap().len(), 1);
    assert_eq!(state.config_sets.lock().unwrap().len(), 0);

    state.provider_mutation_error.lock().unwrap().take();
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "provider endpoint retry", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if !modal.busy
                    && modal.error.is_none()
                    && modal.status.as_deref().is_some_and(|status| status.contains("endpoint saved"))
        )
    });
    assert_eq!(state.provider_endpoint_sets.lock().unwrap().len(), 2);
    assert_eq!(state.config_sets.lock().unwrap().len(), 0);
}

#[gpui::test]
fn test_late_config_load_preserves_provider_credential_draft(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    *state.providers.lock().unwrap() = mock_providers();
    state.config_delay_ms.store(500, Ordering::SeqCst);

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "providers before config", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if matches!(&modal.providers, ProvidersState::Ready(_, providers) if providers.len() == 3)
                    && modal.revision.is_none()
        )
    });
    click_debug_element(vc, "settings_section_providers");
    click_debug_element(vc, "settings_provider_anthropic");
    vc.simulate_input("sk-late-config");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("provider editing should keep settings open");
        };
        assert_eq!(
            modal.text_selection().map(|(value, _, _)| value).as_deref(),
            Some("sk-late-config")
        );
    });

    pump_until(vc, &view, "late config response", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal) if modal.revision.is_some()
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open after config loads");
        };
        assert!(modal.editing);
        assert_eq!(modal.active_section, SettingsSection::Providers);
        assert!(matches!(
            modal.fields.get(modal.selected),
            Some(SettingField::Provider { provider, .. }) if provider.id == "anthropic"
        ));
        assert_eq!(
            modal.text_selection().map(|(value, _, _)| value).as_deref(),
            Some("sk-late-config")
        );
        assert_eq!(modal.dirty_updates(), json!({}));
    });
}

#[gpui::test]
fn test_connected_settings_save_sends_loaded_revision(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "revision-aware settings load", |view| {
        matches!(&view.shell.active_modal, ActiveModal::Settings(modal)
            if !modal.load_failed && modal.revision.as_deref() == Some("sha256:mock-v1"))
    });

    click_debug_element(vc, "settings_section_models");
    click_debug_element(vc, "settings_row_2");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert_eq!(modal.dirty_updates(), json!({"model": {"reasoningEffort": "high"}}));
    });
    click_debug_element(vc, "settings_save_row");
    pump_until(vc, &view, "revision-aware settings save", |view| {
        matches!(&view.shell.active_modal, ActiveModal::Settings(modal)
            if modal.revision.as_deref() == Some("sha256:mock-v2") && !modal.busy)
    });

    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert_eq!(modal.dirty_count(), 0);
        assert!(modal.error.is_none());
        assert!(modal.status.as_deref().unwrap().starts_with("Saved 1 key"));
    });
    let requests = state.config_sets.lock().unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0]["expectedRevision"], "sha256:mock-v1");
    assert_eq!(requests[0]["updates"], json!({"model": {"reasoningEffort": "high"}}));
}

#[gpui::test]
fn test_settings_revision_conflict_preserves_local_and_runtime_edits(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "settings before external edit", |view| {
        matches!(&view.shell.active_modal, ActiveModal::Settings(modal)
            if !modal.load_failed && modal.revision.is_some())
    });

    click_debug_element(vc, "settings_section_appearance");
    click_debug_element(vc, "settings_row_0");
    click_debug_element(vc, "settings_section_models");
    click_debug_element(vc, "settings_row_2");
    *state.config_revision.lock().unwrap() = "sha256:external".to_string();
    click_debug_element(vc, "settings_save_row");
    pump_until(vc, &view, "settings revision conflict", |view| {
        matches!(&view.shell.active_modal, ActiveModal::Settings(modal)
            if modal.error.as_deref().is_some_and(|error| error.contains("changed elsewhere")))
    });

    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert_eq!(modal.revision.as_deref(), Some("sha256:external"));
        assert_eq!(modal.desktop_dirty_count(), 0);
        assert_eq!(modal.desktop_preferences.theme, ThemePreset::Midnight);
        assert_eq!(modal.dirty_updates(), json!({"model": {"reasoningEffort": "high"}}));
        assert!(modal.error.as_deref().unwrap().starts_with("Desktop preferences saved."));
    });
    let requests = state.config_sets.lock().unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0]["expectedRevision"], "sha256:mock-v1");
    drop(requests);

    click_debug_element(vc, "settings_save_row");
    pump_until(vc, &view, "settings save after conflict", |view| {
        matches!(&view.shell.active_modal, ActiveModal::Settings(modal)
            if modal.revision.as_deref() == Some("sha256:mock-v2") && !modal.busy)
    });
    let requests = state.config_sets.lock().unwrap();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[1]["expectedRevision"], "sha256:external");
    assert_eq!(requests[1]["updates"], json!({"model": {"reasoningEffort": "high"}}));
}

#[gpui::test]
fn test_tool_inventory_failures_and_empty_results_are_independent(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    *state.config_error.lock().unwrap() = Some("permission denied".to_string());
    *state.provider_error.lock().unwrap() = Some("provider registry denied".to_string());
    *state.tools.lock().unwrap() = vec![json!({
        "name": "list_files",
        "description": "List workspace files",
        "parameters": {},
        "risk_class": "R1"
    })];

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "tool inventory after config failure", |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            return false;
        };
        modal.load_failed
            && matches!(modal.tools, InventoryState::Ready(_))
            && matches!(
                &modal.providers,
                ProvidersState::Unavailable(message)
                    if message.starts_with("providers/list failed:")
                        && message.contains("provider registry denied")
            )
    });
    click_debug_element(vc, "settings_section_tools");
    click_debug_element(vc, "settings_info_tools");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("tool inventory should render even when config/get fails");
        };
        assert!(modal.error.as_deref().unwrap().contains("permission denied"));
        let tool = modal
            .fields
            .iter()
            .find(|field| matches!(field, SettingField::Info { .. }))
            .expect("tool inventory row");
        assert_eq!(tool.label(), "list_files");
        assert_eq!(modal.dirty_count(), 0);
    });

    click_debug_element(vc, "settings_section_providers");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("provider error should render independently of config and tools");
        };
        let provider = modal
            .fields
            .iter()
            .find(|field| field.key() == "desktop.info.providers")
            .expect("provider unavailable row");
        assert_eq!(provider.label(), "Providers unavailable");
        assert!(modal.error.as_deref().unwrap().contains("permission denied"));
        assert_eq!(modal.dirty_count(), 0);
    });

    vc.simulate_keystrokes("escape");
    finish_motion(vc, &view);
    state.config_error.lock().unwrap().take();
    *state.tools_error.lock().unwrap() = Some("permission denied".to_string());
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "config settings after tool failure", |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            return false;
        };
        !modal.load_failed
            && matches!(
                &modal.tools,
                InventoryState::Unavailable(message)
                    if message.starts_with("tools/list failed:")
                        && message.contains("permission denied")
            )
            && modal.fields.iter().any(|field| {
                matches!(field, SettingField::Number { value, .. } if value == "24")
            })
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("runtime config should remain available");
        };
        assert_eq!(modal.dirty_count(), 0);
        assert_eq!(modal.dirty_updates(), json!({}));
    });

    vc.simulate_keystrokes("escape");
    finish_motion(vc, &view);
    state.tools_error.lock().unwrap().take();
    state.tools.lock().unwrap().clear();
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "empty tool inventory", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if !modal.load_failed && modal.tools == InventoryState::Empty
        )
    });
    click_debug_element(vc, "settings_section_tools");
    click_debug_element(vc, "settings_info_tools");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("empty tool state should render");
        };
        let empty = modal
            .fields
            .iter()
            .find(|field| matches!(field, SettingField::Info { .. }))
            .expect("empty tool state");
        assert_eq!(
            empty,
            &SettingField::Info {
                key: "desktop.info.tools",
                label: "No tools registered".to_string(),
                value: "0 tools".to_string(),
                description: "The connected runtime returned an empty tool registry."
                    .to_string(),
            }
        );
        assert_eq!(modal.dirty_count(), 0);
    });
}

#[gpui::test]
fn test_skills_settings_require_a_real_session(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "settings without a session", |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            return false;
        };
        !modal.load_failed
            && (matches!(modal.tools, InventoryState::Ready(_))
                || modal.tools == InventoryState::Empty)
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert_eq!(
            modal.skills,
            InventoryState::Unavailable(SKILLS_SESSION_REQUIRED.to_string())
        );
        assert_eq!(state.skill_requests.load(Ordering::SeqCst), 0);
    });
    click_debug_element(vc, "settings_section_skills");
    click_debug_element(vc, "settings_info_skills");
    vc.simulate_keystrokes("enter");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("skill guidance should remain visible");
        };
        assert_eq!(modal.active_section, SettingsSection::Skills);
        assert!(!modal.editing);
        assert_eq!(modal.dirty_count(), 0);
    });
}

#[gpui::test]
fn test_skills_settings_use_active_or_known_sessions_and_parse_output(
    cx: &mut TestAppContext,
) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    *state.sessions.lock().unwrap() = vec![
        json!({
            "sessionId": "ses_known",
            "cwd": "/workspace",
            "model": "claude-3-7-sonnet",
            "createdAt": 1_700_000_001.0,
            "turns": 1,
            "title": "Known session",
            "archived": false,
            "pinned": false
        }),
        json!({
            "sessionId": "ses_active",
            "cwd": "/workspace",
            "model": "claude-3-7-sonnet",
            "createdAt": 1_700_000_002.0,
            "turns": 1,
            "title": "Active session",
            "archived": false,
            "pinned": false
        }),
    ];
    *state.skills_output.lock().unwrap() = concat!(
        "Registered Skills (2):\n",
        "  • write_docs (gate: review)\n",
        "  • audit_code (gate: allow)"
    )
    .to_string();
    vc.update(|_, app| {
        view.update(app, |view, _| {
            view.shell.sidebar.session_list.sessions = vec![
                SessionItem::new("ses_known", "Known session"),
                SessionItem::new("ses_active", "Active session"),
            ];
            view.shell.active_session_id = Some("ses_active".to_string());
        });
    });

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "active session skills", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if matches!(modal.skills, InventoryState::Ready(_))
        )
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should remain open");
        };
        assert_eq!(
            *state.skill_session_ids.lock().unwrap(),
            vec!["ses_active".to_string()]
        );
        let skills = modal
            .fields
            .iter()
            .filter(|field| {
                field.key() == "desktop.info.skills"
                    && matches!(field, SettingField::Info { .. })
            })
            .collect::<Vec<_>>();
        assert_eq!(skills.len(), 2);
        assert_eq!(skills[0].label(), "audit_code");
        assert_eq!(
            skills[0],
            &SettingField::Info {
                key: "desktop.info.skills",
                label: "audit_code".to_string(),
                value: "gate: allow".to_string(),
                description: "Registered in the runtime skill ledger.".to_string(),
            }
        );
        assert_eq!(skills[1].label(), "write_docs");
        assert_eq!(
            skills[1],
            &SettingField::Info {
                key: "desktop.info.skills",
                label: "write_docs".to_string(),
                value: "gate: review".to_string(),
                description: "Registered in the runtime skill ledger.".to_string(),
            }
        );
        assert_eq!(modal.dirty_count(), 0);
        assert_eq!(modal.dirty_updates(), json!({}));
    });
    click_debug_element(vc, "settings_section_skills");
    click_debug_element(vc, "settings_info_skills");
    vc.simulate_keystrokes("enter");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("read-only skills should keep settings open");
        };
        assert!(!modal.editing);
        assert_eq!(modal.dirty_count(), 0);
    });

    vc.simulate_keystrokes("escape");
    finish_motion(vc, &view);
    *state.skills_error.lock().unwrap() = Some("permission denied".to_string());
    vc.update(|_, app| {
        view.update(app, |view, _| {
            view.shell.active_session_id = None;
        });
    });
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "known session skill failure", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if matches!(
                    &modal.skills,
                    InventoryState::Unavailable(message)
                        if message.starts_with("/skills failed:")
                            && message.contains("permission denied")
                )
        )
    });
    assert_eq!(
        *state.skill_session_ids.lock().unwrap(),
        vec!["ses_active".to_string(), "ses_known".to_string()]
    );

    vc.simulate_keystrokes("escape");
    finish_motion(vc, &view);
    state.skills_error.lock().unwrap().take();
    *state.skills_output.lock().unwrap() = "No skills registered in the ledger.".to_string();
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "empty skill registry", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if !modal.load_failed && modal.skills == InventoryState::Empty
        )
    });
    click_debug_element(vc, "settings_section_skills");
    click_debug_element(vc, "settings_info_skills");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("empty skill state should render");
        };
        let empty = modal
            .fields
            .iter()
            .find(|field| field.key() == "desktop.info.skills")
            .unwrap();
        assert_eq!(empty.label(), "No skills registered");
        assert_eq!(
            empty,
            &SettingField::Info {
                key: "desktop.info.skills",
                label: "No skills registered".to_string(),
                value: "0 skills".to_string(),
                description: "The runtime skill ledger is empty.".to_string(),
            }
        );
        assert_eq!(modal.dirty_count(), 0);
    });
}

#[gpui::test]
fn test_stale_settings_requests_cannot_overwrite_a_newer_opening(cx: &mut TestAppContext) {
    let (_rt, view, vc, state) = launch_connected_chat_with_state(cx);
    *state.tools.lock().unwrap() = vec![json!({
        "name": "stale_tool",
        "description": "Previous registry",
        "parameters": {}
    })];
    *state.providers.lock().unwrap() = vec![json!({
        "id": "stale_provider",
        "name": "Stale Provider",
        "category": "featured",
        "requiresApiKey": true,
        "credentialEnvVar": "STALE_API_KEY",
        "credentialConfigured": false,
        "credentialSource": "none",
        "credentialWritable": true,
        "baseUrlEnvVar": null,
        "defaultBaseUrl": "",
        "signupUrl": null,
        "description": "Previous provider registry"
    })];
    state.tools_delay_ms.store(400, Ordering::SeqCst);
    state.provider_delay_ms.store(400, Ordering::SeqCst);

    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "first settings inventory requests", |_| {
        state.tool_requests.load(Ordering::SeqCst) == 1
            && state.provider_requests.load(Ordering::SeqCst) == 1
    });
    vc.simulate_keystrokes("escape");
    finish_motion(vc, &view);

    *state.tools.lock().unwrap() = vec![json!({
        "name": "fresh_tool",
        "description": "Current registry",
        "parameters": {}
    })];
    *state.providers.lock().unwrap() = mock_providers();
    state.tools_delay_ms.store(0, Ordering::SeqCst);
    state.provider_delay_ms.store(0, Ordering::SeqCst);
    click_debug_element(vc, "home_settings_button");
    finish_motion(vc, &view);
    pump_until(vc, &view, "fresh settings inventories", |view| {
        matches!(
            &view.shell.active_modal,
            ActiveModal::Settings(modal)
                if matches!(
                    &modal.tools,
                    InventoryState::Ready(items)
                        if items.iter().any(|item| item.name == "fresh_tool")
                ) && matches!(
                    &modal.providers,
                    ProvidersState::Ready(_, providers)
                        if providers.iter().any(|provider| provider.id == "anthropic")
                )
        )
    });
    pump_until(vc, &view, "stale settings responses", |_| {
        state.tool_responses.load(Ordering::SeqCst) == 2
            && state.provider_responses.load(Ordering::SeqCst) == 2
    });
    vc.run_until_parked();
    vc.update(|_, app| {
        view.update(app, |_, cx| cx.notify());
    });
    vc.run_until_parked();

    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("newer settings opening should remain active");
        };
        let InventoryState::Ready(items) = &modal.tools else {
            panic!("fresh tool inventory should remain ready");
        };
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].name, "fresh_tool");
        let ProvidersState::Ready(_, providers) = &modal.providers else {
            panic!("fresh provider inventory should remain ready");
        };
        assert_eq!(providers.len(), 3);
        assert!(providers.iter().any(|provider| provider.id == "anthropic"));
        assert!(!providers
            .iter()
            .any(|provider| provider.id == "stale_provider"));
    });
}

#[gpui::test]
fn test_modal_clipboard_actions_are_isolated_from_composer(cx: &mut TestAppContext) {
    cx.update(|cx| {
        init_app(cx);
        cx.set_global(MotionSystem::with_policy(MotionPolicy::new(true)));
    });
    let (view, vc) = cx.add_window_view(|_, cx| {
        ChatView::new(
            AcpBridge::new(),
            app_runtime().handle().clone(),
            std::env::temp_dir(),
            cx,
        )
    });
    vc.simulate_input("composer draft");
    vc.update(|_, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::SessionAction(SessionActionModal::open_menu(
                "ses_clipboard",
                "Unicode 👩\u{200d}💻 title",
                false,
                false,
            ));
            cx.notify();
        });
    });
    finish_motion(vc, &view);
    vc.simulate_keystrokes("enter");
    finish_motion(vc, &view);

    vc.simulate_keystrokes("cmd-a");
    vc.simulate_keystrokes("cmd-c");
    assert_eq!(
        vc.read_from_clipboard().unwrap().text().unwrap(),
        "Unicode 👩\u{200d}💻 title"
    );

    vc.write_to_clipboard(ClipboardItem::new_string("差し替え👩\u{200d}💻".into()));
    vc.simulate_keystrokes("cmd-v");
    snap(vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("rename should remain open while pasting");
        };
        assert_eq!(modal.draft, "差し替え👩\u{200d}💻");
    });
    vc.dispatch_action(Undo);
    snap(vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("rename should remain open while undoing");
        };
        assert_eq!(modal.draft, "Unicode 👩\u{200d}💻 title");
    });
    vc.dispatch_action(Redo);
    snap(vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("rename should remain open while redoing");
        };
        assert_eq!(modal.draft, "差し替え👩\u{200d}💻");
    });

    vc.simulate_keystrokes("cmd-a");
    vc.simulate_keystrokes("cmd-x");
    snap(vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("rename should remain open while cutting");
        };
        assert_eq!(modal.draft, "");
    });
    assert_eq!(
        vc.read_from_clipboard().unwrap().text().unwrap(),
        "差し替え👩\u{200d}💻"
    );

    vc.write_to_clipboard(ClipboardItem::new_string("復元".into()));
    vc.simulate_keystrokes("cmd-v");
    snap(vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("rename should remain open after paste");
        };
        assert_eq!(modal.draft, "復元");
        assert_eq!(view.composer_text(), "composer draft");
    });
}

#[gpui::test]
async fn test_model_picker_virtualizes_catalog_and_scrolls_keyboard_selection(
    cx: &mut TestAppContext,
) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    cx.update(|cx| init_app(cx));
    let workspace = std::env::temp_dir();
    let (view, mut vc) = cx.add_window_view(|_window, cx| {
        ChatView::new(AcpBridge::new(), rt.handle().clone(), workspace.clone(), cx)
    });
    let models = (0..200)
        .map(|index| {
            ModelEntry::new(
                format!("model-{index}"),
                format!("Model {index}"),
                if index < 100 { "OpenAI" } else { "Anthropic" },
                false,
            )
        })
        .collect();
    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::ModelPicker(ModelPickerModal::new(
                "model-0",
                "OpenAI",
                models,
            ));
            cx.notify();
        });
    });
    finish_motion(&mut vc, &view);

    let list_bounds = vc
        .debug_bounds("model_picker_list")
        .expect("virtualized picker list should be mounted");
    assert!(vc.debug_bounds("model_0").is_some());
    assert!(
        vc.debug_bounds("model_199").is_none(),
        "offscreen catalog rows must not be mounted"
    );

    vc.simulate_keystrokes("end");
    finish_motion(&mut vc, &view);
    snap(&vc, &view, |view| {
        let ActiveModal::ModelPicker(picker) = &view.shell.active_modal else {
            panic!("picker should remain open");
        };
        assert_eq!(picker.highlighted_model().unwrap().id, "model-199");
    });
    let last_bounds = vc
        .debug_bounds("model_199")
        .expect("keyboard navigation should reveal the highlighted row");
    assert!(list_bounds.contains(&last_bounds.center()));
}

#[gpui::test]
async fn test_model_picker_disconnected_error_is_inline(cx: &mut TestAppContext) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    cx.update(|cx| init_app(cx));
    let workspace = std::env::temp_dir();
    let (view, mut vc) = cx.add_window_view(|_window, cx| {
        ChatView::new(AcpBridge::new(), rt.handle().clone(), workspace.clone(), cx)
    });
    finish_motion(&mut vc, &view);

    click_debug_element(&mut vc, "model_pill_button");
    let failed_request_id = snap(&vc, &view, |view| {
        let ActiveModal::ModelPicker(picker) = &view.shell.active_modal else {
            panic!("picker should stay open with the connection error");
        };
        assert!(picker
            .error()
            .is_some_and(|error| error.contains("Not connected to the HEX runtime")));
        assert!(!last_notice(view)
            .is_some_and(|notice| notice.contains("Could not load the model catalog")));
        picker.request_id
    });
    click_debug_element(&mut vc, "model_picker_retry");
    snap(&vc, &view, |view| {
        let ActiveModal::ModelPicker(picker) = &view.shell.active_modal else {
            panic!("retry should keep the inline error open");
        };
        assert!(picker.request_id > failed_request_id);
        assert!(picker.error().is_some());
    });
}

#[gpui::test]
async fn test_supported_window_sizes_keep_home_and_chat_controls_reachable(
    cx: &mut TestAppContext,
) {
    let (_rt, view, vc) = launch_connected_chat(cx);
    let layouts = [
        (900.0, 600.0, false, 260.0),
        (900.0, 600.0, true, 0.0),
        (1100.0, 760.0, false, 296.0),
        (1440.0, 900.0, false, 296.0),
        (1440.0, 900.0, true, 0.0),
    ];
    let unfinished = [
        "dock_Team",
        "dock_Notebooks",
        "dock_Design",
        "dock_Extensions",
        "dock_Automations",
        "feature_Team",
        "feature_Notebooks",
        "feature_Design",
        "feature_Extensions",
        "feature_Automations",
        "header_team",
        "segment_files",
        "segment_browser",
        "segment_terminal",
    ];

    for (width, height, collapsed, sidebar_width) in layouts {
        vc.update(|_window, app| {
            view.update(app, |view, cx| {
                view.shell.set_sidebar_collapsed(collapsed);
                cx.notify();
            });
        });
        vc.simulate_resize(size(px(width), px(height)));
        finish_motion(vc, &view);

        assert!(vc.debug_bounds("home_center_scroll").is_some());
        let home_main = vc.debug_bounds("home_top_bar").unwrap();
        assert!(
            (home_main.size.width - px(width - sidebar_width)).abs() < px(0.5),
            "Home width at {width}x{height}, collapsed={collapsed}: expected {}, got {:?}",
            width - sidebar_width,
            home_main.size.width
        );
        assert_px_close(home_main.left(), px(sidebar_width));
        let home_controls = [
            vc.debug_bounds("home_sidebar_toggle").unwrap(),
            vc.debug_bounds("home_search_button").unwrap(),
            vc.debug_bounds("home_bell_button").unwrap(),
            vc.debug_bounds("home_settings_button").unwrap(),
        ];
        assert!(home_controls[0].left() >= home_main.left() + px(80.0));
        for pair in home_controls.windows(2) {
            assert!(pair[0].right() <= pair[1].left());
        }
        assert!(home_controls.last().unwrap().right() <= home_main.right() - px(20.0));
        clear_glow_probe(vc);
        let home = glow_paints(vc, &view);
        assert_eq!(home.len(), 1, "one Home composer at {width}x{height}");
        assert_px_close(home[0].card.size.width, px(560.0));
        assert_px_close(home[0].halo.size.width, px(564.0));
        assert!(home[0].card.left() >= px(0.0));
        assert!(home[0].card.right() <= px(width));
        assert!(home[0].halo.left() >= px(0.0));
        assert!(home[0].halo.right() <= px(width));
        for selector in unfinished {
            assert!(
                vc.debug_bounds(selector).is_none(),
                "unfinished Home destination must not render: {selector}"
            );
        }
    }

    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            view.shell.set_sidebar_collapsed(false);
            cx.notify();
        });
    });
    vc.simulate_resize(size(px(900.0), px(600.0)));
    finish_motion(vc, &view);
    click_debug_element(vc, "home_sidebar_toggle");
    finish_motion(vc, &view);
    assert!(vc.read(|app| view.read(app).shell.sidebar_collapsed));
    assert_px_close(vc.debug_bounds("home_top_bar").unwrap().size.width, px(900.0));
    click_debug_element(vc, "home_sidebar_toggle");
    finish_motion(vc, &view);
    assert!(!vc.read(|app| view.read(app).shell.sidebar_collapsed));
    assert_px_close(vc.debug_bounds("home_top_bar").unwrap().size.width, px(640.0));

    vc.simulate_input("Responsive layout fixture");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "responsive fixture session", |view| {
        last_assistant(view).is_some_and(|assistant| assistant.content == "Hello world")
    });

    for (width, height, collapsed, sidebar_width) in layouts {
        vc.update(|_window, app| {
            view.update(app, |view, cx| {
                view.shell.set_sidebar_collapsed(collapsed);
                cx.notify();
            });
        });
        vc.simulate_resize(size(px(width), px(height)));
        finish_motion(vc, &view);
        let sidebar_progress = vc.read(|app| {
            MotionSystem::value(
                app,
                hex_desktop::ui::motion::SpringKey::new(view.entity_id(), "sidebar_width"),
            )
        });
        assert_eq!(
            sidebar_progress,
            if collapsed { 0.0 } else { 1.0 },
            "sidebar motion must settle before measuring {width}x{height}"
        );

        let main = vc.debug_bounds("session_main_pane").unwrap();
        let feed = vc.debug_bounds("feed_column").unwrap();
        assert_px_close(main.size.width, px(width - sidebar_width));
        assert_px_close(main.left(), px(sidebar_width));
        assert_px_close(feed.size.width, px(560.0));
        assert!(feed.left() >= main.left() + px(16.0));
        assert!(feed.right() <= main.right() - px(16.0));
        if sidebar_width > 0.0 {
            let sidebar = vc.debug_bounds("sidebar_content").unwrap();
            assert_px_close(sidebar.size.width, px(sidebar_width));
        }

        let controls = [
            vc.debug_bounds("header_toggle").unwrap(),
            vc.debug_bounds("segment_chat").unwrap(),
            vc.debug_bounds("header_bell").unwrap(),
            vc.debug_bounds("header_device").unwrap(),
            vc.debug_bounds("header_more").unwrap(),
        ];
        assert!(controls[0].left() >= px(80.0));
        for pair in controls.windows(2) {
            assert!(pair[0].right() <= pair[1].left());
        }
        assert!(controls.last().unwrap().right() <= main.right() - px(20.0));

        let attach = vc.debug_bounds("composer_attach").unwrap();
        let send = vc.debug_bounds("composer_send").unwrap();
        assert!(attach.left() >= feed.left());
        assert!(send.right() <= feed.right());
        clear_glow_probe(vc);
        let paints = glow_paints(vc, &view);
        assert_eq!(paints.len(), 1, "one Session composer at {width}x{height}");
        assert_px_close(paints[0].card.size.width, px(560.0));
        assert!(paints[0].card.left() >= main.left());
        assert!(paints[0].card.right() <= main.right());
        assert!(paints[0].card.top() >= px(0.0));
        assert!(paints[0].card.bottom() <= px(height));
        for selector in unfinished {
            assert!(
                vc.debug_bounds(selector).is_none(),
                "unfinished Session destination must not render: {selector}"
            );
        }
    }
}

#[gpui::test]
async fn test_undo_redo_actions_restore_composer_and_modal_text(cx: &mut TestAppContext) {
    let (_rt, view, vc) = launch_connected_chat(cx);

    vc.simulate_input("draft");
    vc.dispatch_action(Undo);
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "");
    vc.dispatch_action(Redo);
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draft");

    vc.simulate_keystrokes("backspace");
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draf");
    vc.simulate_keystrokes("cmd-z");
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draft");
    vc.simulate_keystrokes("cmd-shift-z");
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draf");

    vc.simulate_keystrokes("cmd-k");
    vc.simulate_input("settings");
    vc.dispatch_action(Undo);
    snap(vc, &view, |view| {
        let ActiveModal::Palette(palette) = &view.shell.active_modal else {
            panic!("palette should be open");
        };
        assert_eq!(palette.query, "");
    });
    vc.dispatch_action(Redo);
    snap(vc, &view, |view| {
        let ActiveModal::Palette(palette) = &view.shell.active_modal else {
            panic!("palette should be open");
        };
        assert_eq!(palette.query, "settings");
    });
    vc.simulate_keystrokes("escape");
    vc.dispatch_action(Undo);
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draft");

    let mut settings = SettingsModal::default();
    settings.fields.insert(
        0,
        SettingField::Text {
            key: "model.cloudProvider",
            label: "Provider",
            value: "gemini".to_string(),
            cursor: "gemini".len(),
            required: true,
        },
    );
    settings.selected = 0;
    settings.select_row(0);
    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::Settings(settings);
            cx.notify();
        });
    });
    vc.run_until_parked();

    vc.simulate_keystrokes("x");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminix");
    });
    vc.dispatch_action(Undo);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "gemini");
    });
    vc.dispatch_action(Redo);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminix");
    });

    vc.simulate_keystrokes("left left x");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, cursor, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminxix");
        assert_eq!(*cursor, 6);
    });
    vc.dispatch_action(Undo);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, cursor, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminix");
        assert_eq!(*cursor, 5);
    });
    vc.dispatch_action(Redo);
    vc.simulate_keystrokes("home delete");
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, cursor, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "eminxix");
        assert_eq!(*cursor, 0);
    });
    vc.dispatch_action(Undo);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminxix");
    });
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draft");

    vc.simulate_keystrokes("end");
    vc.update(|window, app| {
        view.update(app, |view, cx| {
            view.replace_and_mark_text_in_range(None, "に", Some(0..1), window, cx);
        });
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminxixに");
    });
    vc.update(|window, app| {
        view.update(app, |view, cx| {
            view.replace_text_in_range(None, "日", window, cx);
        });
    });
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminxix日");
    });
    vc.dispatch_action(Undo);
    snap(vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings should be open");
        };
        let SettingField::Text { value, .. } = &modal.fields[0] else {
            panic!("expected text field");
        };
        assert_eq!(value, "geminxix");
    });
    vc.dispatch_action(Redo);
    assert_eq!(snap(vc, &view, |view| view.composer_text().to_string()), "draft");
}

#[gpui::test]
async fn test_streaming_disclosures_toggle_by_click(cx: &mut TestAppContext) {
    let (rt, view, vc) = launch_connected_chat(cx);
    vc.simulate_input("show details");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "session view", |v| {
        v.shell.active_session_id.as_deref() == Some("ses_test_1")
            && !v.shell.composer.is_streaming
            && last_assistant(v).is_some()
    });
    // Let the frame-coalesced notification pump drain before replacing the feed.
    rt.block_on(async {
        tokio::time::sleep(Duration::from_millis(50)).await;
    });
    vc.run_until_parked();
    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            view.shell.feed.messages.clear();
            view.shell.feed.messages.push(MessageItem::Assistant(
                AssistantMessageView::new("msg_disclosures", "Working on it")
                    .with_reasoning(
                        ThinkingDisclosure::new("Inspect the request and choose a tool")
                            .with_generating(true),
                    )
                    .with_tool_call(
                        ToolCallCard::new(
                            "tc_disclosures",
                            "read_file",
                            r#"{"path":"src/main.rs"}"#,
                        )
                        .with_output("fn main() {}")
                        .with_duration(12),
                    )
                    .with_streaming(true),
            ));
            cx.notify();
        });
    });
    vc.run_until_parked();
    finish_motion(vc, &view);

    click_debug_element(vc, "reasoning_msg_disclosures");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        let assistant = last_assistant(view).unwrap();
        assert!(assistant.reasoning.as_ref().unwrap().is_expanded);
    });

    click_debug_element(vc, "tool_input_tc_disclosures");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        assert!(last_assistant(view).unwrap().tool_calls[0].input_expanded);
    });

    click_debug_element(vc, "tool_output_tc_disclosures");
    finish_motion(vc, &view);
    snap(vc, &view, |view| {
        assert!(!last_assistant(view).unwrap().tool_calls[0].output_expanded);
    });
}

#[gpui::test]
async fn test_phase3_markdown_links_payload_copy_and_approval_risk(cx: &mut TestAppContext) {
    let (_rt, view, vc) = launch_connected_chat(cx);
    vc.simulate_input("phase 3 content");
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "phase 3 session", |view| {
        view.shell.active_session_id.is_some() && !view.shell.composer.is_streaming
    });
    vc.simulate_resize(size(px(1280.0), px(1200.0)));

    let content = "# Result\n\n[unsafe](javascript:alert(1)) [docs](https://example.com/docs)\n\n```rust\nlet answer = 42;\n```";
    let reasoning_text = "Inspect the request and choose the safest implementation.";
    let input_text = r#"{"path":"src/main.rs"}"#;
    let output_text = (0..500)
        .map(|index| format!("line {index:04}"))
        .collect::<Vec<_>>()
        .join("\n");
    let patch = "--- a/src/main.rs\n+++ b/src/main.rs\n@@ -1 +1 @@\n-old\n+new\n";
    let mut tool = ToolCallCard::new("tc_phase3", "edit_file", input_text)
        .with_output(output_text.clone())
        .with_diff(DiffView::from_patch(patch));
    tool.input_expanded = true;
    tool.output_expanded = false;
    let assistant = AssistantMessageView::new("msg_phase3", content)
        .with_reasoning(ThinkingDisclosure::new(reasoning_text).with_expanded(true))
        .with_tool_call(tool);
    let approval_summary = "Run the verified command against the selected workspace. ".repeat(12);

    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            view.shell.active_session_id = Some("ses_phase3".into());
            view.shell.feed.messages.clear();
            view.shell
                .feed
                .messages
                .push(MessageItem::Assistant(assistant));
            view.shell
                .feed
                .messages
                .push(MessageItem::Approval(ApprovalCard::new(
                    "appr_phase3",
                    "run_command",
                    approval_summary,
                    "R4",
                )));
            cx.notify();
        });
    });
    vc.run_until_parked();
    finish_motion(vc, &view);

    assert!(vc.debug_bounds("markdown_link_msg_phase3_1_0").is_none());
    click_debug_element(vc, "markdown_link_msg_phase3_1_1");
    assert_eq!(vc.opened_url().as_deref(), Some("https://example.com/docs"));

    click_debug_element(vc, "payload_copy_code_msg_phase3_2");
    assert_eq!(
        vc.read_from_clipboard().unwrap().text().unwrap(),
        "let answer = 42;"
    );

    click_debug_element(vc, "payload_copy_reasoning_msg_phase3");
    assert_eq!(
        vc.read_from_clipboard().unwrap().text().unwrap(),
        reasoning_text
    );
    snap(vc, &view, |view| {
        assert!(
            last_assistant(view)
                .unwrap()
                .reasoning
                .as_ref()
                .unwrap()
                .is_expanded
        );
    });

    click_debug_element(vc, "payload_copy_tool_input_tc_phase3");
    assert_eq!(
        vc.read_from_clipboard().unwrap().text().unwrap(),
        input_text
    );
    click_debug_element(vc, "payload_copy_tool_output_tc_phase3");
    assert_eq!(
        vc.read_from_clipboard().unwrap().text().unwrap(),
        output_text
    );
    snap(vc, &view, |view| {
        let tool = &last_assistant(view).unwrap().tool_calls[0];
        assert!(tool.input_expanded);
        assert!(!tool.output_expanded);
    });

    click_debug_element(vc, "payload_copy_diff_tool_diff_tc_phase3_0");
    assert_eq!(vc.read_from_clipboard().unwrap().text().unwrap(), patch);

    assert!(vc.debug_bounds("approval_risk_appr_phase3").is_some());
    assert!(vc.debug_bounds("action_up_msg_phase3").is_none());
    assert!(vc.debug_bounds("action_down_msg_phase3").is_none());
}

#[gpui::test]
async fn test_modal_enter_is_single_step_and_isolated(cx: &mut TestAppContext) {
    let (_rt, view, vc) = launch_connected_chat(cx);
    vc.simulate_input("keep draft");
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "keep draft");
    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            view.shell.active_modal = ActiveModal::SessionAction(SessionActionModal::open_menu(
                "ses_test_1",
                "Existing title",
                false,
                false,
            ));
            cx.notify();
        });
    });
    vc.run_until_parked();

    vc.simulate_keystrokes("enter");
    snap(&vc, &view, |view| {
        let ActiveModal::SessionAction(modal) = &view.shell.active_modal else {
            panic!("session action modal should still be open");
        };
        assert_eq!(modal.mode, SessionActionMode::Rename);
        assert!(!modal.busy);
        assert_eq!(view.composer_text(), "keep draft");
    });
    vc.simulate_keystrokes("escape");

    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            let mut settings = SettingsModal::default();
            settings.fields.insert(
                0,
                SettingField::Text {
                    key: "model.cloudModelId",
                    label: "Model ID",
                    value: "gpt-4o".to_string(),
                    cursor: "gpt-4o".len(),
                    required: true,
                },
            );
            settings.selected = 0;
            view.shell.active_modal = ActiveModal::Settings(settings);
            cx.notify();
        });
    });
    vc.run_until_parked();

    vc.simulate_keystrokes("enter");
    snap(&vc, &view, |view| {
        let ActiveModal::Settings(modal) = &view.shell.active_modal else {
            panic!("settings modal should still be open");
        };
        assert!(modal.editing, "one Enter must enter settings edit mode");
        assert_eq!(view.composer_text(), "keep draft");
    });
}

#[gpui::test]
async fn test_tall_modal_stays_inside_viewport_and_scrolls(cx: &mut TestAppContext) {
    let (_rt, view, vc) = launch_connected_chat(cx);
    vc.simulate_resize(size(px(480.0), px(360.0)));
    vc.update(|_window, app| {
        view.update(app, |view, cx| {
            let mut settings = SettingsModal::default();
            for _ in 0..24 {
                settings.fields.push(SettingField::Text {
                    key: "model.cloudModelId",
                    label: "Model ID",
                    value: "configured-model".into(),
                    cursor: 0,
                    required: true,
                });
            }
            view.shell.active_modal = ActiveModal::Settings(settings);
            cx.notify();
        });
    });
    finish_motion(vc, &view);

    let card = vc.debug_bounds("modal_card_scroll").unwrap();
    let content = vc.debug_bounds("settings_content").unwrap();
    assert!(card.left() >= px(16.0));
    assert!(card.top() >= px(16.0));
    assert!(card.right() <= px(464.0));
    assert!(card.bottom() <= px(344.0));
    assert!(content.size.height > card.size.height);
    assert!(vc.debug_bounds("settings_save_row").is_some());
}

#[gpui::test]
async fn test_header_empty_space_acts_as_a_native_titlebar(cx: &mut TestAppContext) {
    let (_rt, view, vc) = launch_connected_chat(cx);

    // Home: the traffic-light gutter and the strip around the provider chip
    // span the full top bar, so any click there is a window drag.
    let top_bar = vc.debug_bounds("home_top_bar").unwrap();
    let gutter = vc.debug_bounds("home_traffic_gutter").unwrap();
    let strip = vc.debug_bounds("home_top_bar_drag").unwrap();
    let toggle = vc.debug_bounds("home_sidebar_toggle").unwrap();
    let search = vc.debug_bounds("home_search_button").unwrap();
    assert_px_close(gutter.size.height, top_bar.size.height);
    assert_px_close(strip.size.height, top_bar.size.height);
    assert_px_close(gutter.left(), top_bar.left());
    assert_px_close(gutter.right(), toggle.left());
    assert!(strip.left() >= toggle.right());
    assert!(strip.right() <= search.left());

    vc.simulate_input("draft");
    vc.simulate_click(gutter.center(), Modifiers::none());
    vc.simulate_click(strip.center(), Modifiers::none());
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "draft");
    assert!(
        focus_state(vc, &view) == (true, false),
        "a titlebar click must leave composer focus alone"
    );
    assert!(!snap(vc, &view, |v| v.shell.sidebar_collapsed));

    // Session: the gutter beside the traffic lights, plus everything between
    // the Chat indicator and the header actions.
    vc.simulate_keystrokes("enter");
    pump_until(vc, &view, "titlebar fixture session", |v| {
        last_assistant(v).is_some_and(|assistant| assistant.content == "Hello world")
    });
    let header = vc.debug_bounds("session_header").unwrap();
    let gutter = vc.debug_bounds("header_traffic_gutter").unwrap();
    let strip = vc.debug_bounds("header_drag_region").unwrap();
    let toggle = vc.debug_bounds("header_toggle").unwrap();
    assert_px_close(gutter.size.height, header.size.height);
    assert_px_close(strip.size.height, header.size.height);
    assert_px_close(gutter.left(), header.left());
    assert_px_close(gutter.right(), toggle.left());
    assert!(strip.left() >= vc.debug_bounds("segment_chat").unwrap().right());
    assert!(strip.right() <= vc.debug_bounds("header_bell").unwrap().left());

    vc.simulate_click(gutter.center(), Modifiers::none());
    vc.simulate_click(strip.center(), Modifiers::none());
    assert_eq!(snap(vc, &view, |v| v.composer_text().to_string()), "");
    assert!(focus_state(vc, &view) == (true, false));
    assert!(!snap(vc, &view, |v| v.shell.sidebar_collapsed));
}

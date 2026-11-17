//! Live integration test spawning the actual Python ACP server subprocess.

use hex_protocol::AcpClient;

#[tokio::test]
async fn test_live_acp_server_subprocess() {
    let workspace = tempfile::tempdir().unwrap();
    let client = match AcpClient::spawn_acp_server(Some(workspace.path())).await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Skipping live ACP test: failed to spawn server ({})", e);
            return;
        }
    };

    // 1. Ping
    let pong = client.ping().await.expect("ping should succeed");
    assert!(pong);

    // 2. Initialize
    let init_resp = client
        .initialize(None)
        .await
        .expect("initialize should succeed");
    assert_eq!(init_resp.server_info.name, "HEX");
    assert!(init_resp.capabilities.session.streaming);

    // 3. Tools list
    let tools = client
        .tools_list()
        .await
        .expect("tools_list should succeed");
    assert!(!tools.is_empty(), "ACP server should expose gateway tools");
    let has_read = tools.iter().any(|t| t.name == "read_file");
    assert!(has_read, "read_file tool should be available");

    // 4. Session new
    let sess = client
        .session_new(workspace.path().to_str(), None)
        .await
        .expect("session_new should succeed");
    assert!(!sess.session_id.is_empty());
    assert!(workspace
        .path()
        .join(".hex/data/acp_sessions/history.db")
        .is_file());
    let loaded = client
        .session_load_paged(&sess.session_id)
        .await
        .expect("paged load should succeed");
    assert_eq!(
        std::path::Path::new(&loaded.cwd).canonicalize().unwrap(),
        workspace.path().canonicalize().unwrap()
    );
    assert_eq!(loaded.page.history_count, 0);
    assert!(loaded.page.history.is_empty());

    // 5. Clean teardown
    client.close().await.expect("close should succeed");
}

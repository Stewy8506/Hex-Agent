//! HEX Desktop entry point: opens the interactive chat cockpit wired to the
//! real ACP backend (F02). Theme, menus, and shortcuts are registered through
//! `chat::init_app`, the same path exercised by the acceptance tests.

use gpui::*;
use hex_desktop::acp_bridge::AcpBridge;
use hex_desktop::chat::{app_runtime, init_app, open_chat_window};

fn main() {
    let app = Application::new();
    app.run(|cx: &mut App| {
        init_app(cx);

        let workspace = std::env::current_dir().unwrap_or_default();
        let bridge = AcpBridge::with_workspace(workspace.clone());
        let rt = app_runtime().handle().clone();

        if let Err(err) = open_chat_window(cx, bridge, rt, workspace) {
            eprintln!("HEX Desktop failed to open its window: {err}");
        }
    });
}
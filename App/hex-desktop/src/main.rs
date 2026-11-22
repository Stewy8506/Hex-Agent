//! HEX Desktop entry point: opens the interactive chat cockpit wired to the
//! real ACP backend (F02). Theme, menus, and shortcuts are registered through
//! `chat::init_app`, the same path exercised by the acceptance tests.

use gpui::*;
use hex_desktop::acp_bridge::AcpBridge;
use hex_desktop::chat::{app_runtime, init_app, open_chat_window};

/// Filesystem asset source so `svg().path("icons/…")` resolves against the
/// crate's `assets/` directory when running from a source checkout.
struct FsAssets {
    base: std::path::PathBuf,
}
impl AssetSource for FsAssets {
    fn load(&self, path: &str) -> Result<Option<std::borrow::Cow<'static, [u8]>>> {
        std::fs::read(self.base.join(path))
            .map(|data| Some(std::borrow::Cow::Owned(data)))
            .map_err(|err| err.into())
    }

    fn list(&self, path: &str) -> Result<Vec<SharedString>> {
        let dir = self.base.join(path);
        let mut out = Vec::new();
        if let Ok(entries) = std::fs::read_dir(&dir) {
            for entry in entries.flatten() {
                if let Some(name) = entry.path().file_name() {
                    out.push(SharedString::from(format!(
                        "{path}/{}",
                        name.to_string_lossy()
                    )));
                }
            }
        }
        Ok(out)
    }
}

/// Minimal logger that preserves diagnostics (including the ACP child's stderr,
/// B35) on the desktop's stderr. The GUI does not render to stderr, so this is
/// safe; the TUI installs no logger and keeps its terminal clean.
struct StderrLogger;

impl log::Log for StderrLogger {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        metadata.level() <= log::Level::Warn
    }
    fn log(&self, record: &log::Record) {
        if self.enabled(record.metadata()) {
            eprintln!("[{}] {}: {}", record.level(), record.target(), record.args());
        }
    }
    fn flush(&self) {}
}

static LOGGER: StderrLogger = StderrLogger;

fn main() {
    let _ = log::set_logger(&LOGGER);
    log::set_max_level(log::LevelFilter::Warn);

    let app = Application::new().with_assets(FsAssets {
        base: std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("assets"),
    });
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
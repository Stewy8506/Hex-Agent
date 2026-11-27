# `crates/` — Native Rust Workspace Crates

The `crates/` directory contains the shared protocol and terminal cockpit Rust crates of the HEX Agent Cargo workspace (declared in the root [`Cargo.toml`](../Cargo.toml)). The GPUI desktop lives in the sibling [`Hex-App`](../../Hex-App/README.md) repository and consumes `hex-protocol` through a path dependency.

## Crates

| Crate | Path | Purpose |
| :--- | :--- | :--- |
| **`hex-protocol`** | [`crates/hex-protocol`](hex-protocol/README.md) | Shared async JSON-RPC 2.0 ACP client, strongly typed ACP/cloud/P2P wire models, and unified diff parser consumed by both `hex-tui` and `hex-desktop`. |
| **`hex-tui`** | [`crates/hex-tui`](hex-tui/README.md) | Native 60-FPS-capable Ratatui + Crossterm terminal cockpit binary (`hex-tui`) and library (`hex_tui`) with virtualized markdown rendering, SQLite resume/ledger access, side-by-side diffs, and Windows console VT input handling. |

## Common Workspace Commands

Run from the repository root:

```bash
# Build both crates
cargo build -p hex-protocol -p hex-tui

# Run unit and integration tests for both crates
cargo test -p hex-protocol -p hex-tui

# Run the TUI headless verification check
cargo run -p hex-tui -- --check
```

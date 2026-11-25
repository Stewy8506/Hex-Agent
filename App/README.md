# `App/` — Native Desktop Application

The `App/` directory hosts native GUI client applications in the HEX Agent Cargo workspace (currently [`hex-desktop`](hex-desktop/README.md); no mobile directories or placeholders live here).

| App | Crate | Stack | Status |
| :--- | :--- | :--- | :--- |
| **`hex-desktop`** | [`App/hex-desktop`](hex-desktop/README.md) | Rust + Zed GPUI (`0.2.2`) + `hex-protocol` | Experimental source-build cockpit running live ACP chat, tool approvals, settings, diffs, and cloud account/update flows |

## Quick Launch

Run from the repository root (macOS; requires Xcode Metal toolchain):

```bash
cargo run -p hex-desktop
# Or, if Command Line Tools are selected instead of Xcode.app:
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer cargo run -p hex-desktop
```

# `docs`

**Architecture references, operator guides, protocol specs, design parity plans, and interactive HTML design mockups for HEX Agent.**

## Directory Structure

| Path | Contents |
| :--- | :--- |
| `architecture/ARCHITECTURE.md` | Five-plane system architecture: presentation cockpits, orchestration core, cognition/memory, credential resilience/local probes, CoW isolation, dual-driver ledger, Graphify AST, SkillSpector, and storage GC. |
| `guides/RUNBOOK.md` | Operator runbook covering CLI/cockpits, workspace management, backup/recovery, skill forensics, stuck-lease/zombie cleanup, rollbacks, and ASGI/systemd deployment. |
| `guides/SUBAGENTS.md` | Hierarchical subagent delegation model, role-based R0–R4 tool restrictions (`researcher`, `coder`, `tester`, `reviewer`, `planner`), CoW scratchpads, and recursion depth caps. |
| `guides/BROWSER_AND_VISION.md` | Browser accessibility snapshots (`@eN` semantic trees) and multimodal image inspection usage. |
| `reference/ACP_INTEGRATION.md` | Agent Client Protocol (`hex acp`) configuration for Zed and Neovim (`avante.nvim`), plus canonical `session/update` wire contract notes. |
| `reference/SUPPORTED_MODELS.md` | Catalog of 26 supported model providers, default/curated model IDs, env vars, and CredentialPool / Keychain notes. |
| `development/desktop-design-parity-plan.md` | Exact color/typography token spec and DX1–DX7 workstream parity reference for `App/hex-desktop`. |
| `research/HERMES_LEARNING_AND_SOUL_ANALYSIS.md` | Comparative architecture analysis of Hermes Agent's `SOUL.md`, curated memory, and `/learn` mechanisms versus HEX's AST-governed cognition engine. |
| `design/mockups-desktop/` | Standalone React/HTML design artboards (`DesktopStart.dc.html`, `DesktopSession.dc.html`) with bundled fonts (`Apfel Grotezk`, `Geist Mono`) used as pixel reference for `App/hex-desktop`. |
| `design/mockups-tui/` | Standalone HTML design artboards (`Start.dc.html`, `Main.dc.html`, `Palette.dc.html`, `Permission.dc.html`, `Error.dc.html`, `SubAgent.dc.html`) used as cell-layout reference for `crates/hex-tui`. |

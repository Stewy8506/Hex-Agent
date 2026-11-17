# Subpackage Import Migration and Forwarding Stub Removal Sweep

**Date:** 2026-11-16  
**Branch:** `experimental-features`  
**Commit:** `3b3feea` (`refactor(harness_core): sweep imports to domain subpackages and remove root stubs`)

---

## 1. Executive Summary

Executed the comprehensive subpackage import migration and stub removal sweep across the repository, migrating from root `harness_core.<module>` imports to the 7 domain subpackages:
- `core/`: `orchestrator.py`, `react_loop.py`, `router.py`, `models.py`, `subagent.py`, `reasoning_effort.py`
- `storage/`: `db.py`, `cow.py`, `sandbox.py`, `backup.py`, `gc.py`, `project_registry.py`
- `cognition/`: `soul.py`, `memory.py`, `reflection.py`, `skillspector.py`, `skill_synthesizer.py`, `prompt_builder.py`, `context_compressor.py`
- `runtime/`: `process_registry.py`, `code_kernel.py`, `daemon.py`, `worker_daemon.py`, `scheduler.py`
- `client/`: `acp.py`, `tui.py`, `tui_launcher.py`, `ui_selector.py`, `dashboard.py`, `asgi.py`, `server.py`, `cli.py`, `onboard.py`, `auto_setup.py`, `doctor.py`
- `capabilities/`: `browser.py`, `vision.py`, `voice.py`, `search.py`, `web_search.py`, `git_autopilot.py`, `github_action.py`, `proof.py`, `recipes.py`, `webhooks.py`
- `models/`: `model_metadata.py`, `credential_pool.py`

All 47 top-level forwarding stub files under `harness_core/` were removed. All imports and `unittest.mock.patch` paths across 122 repository files were updated to target the domain subpackages directly.

---

## 2. Key Actions Taken

1. **Import and Patch Migration Across Repository:**
   - Updated 122 files across `src/`, `tests/`, `scripts/`, `crates/`, `pyproject.toml`, `hex.bat`, and `install.ps1`.
   - Updated `pyproject.toml` console scripts (`hex = "harness_core.client.cli:main"` and `harness = "harness_core.client.cli:main"`).
   - Updated `hex.bat` and `install.ps1` invocations to invoke `-m harness_core.client.cli`.
   - Updated `crates/hex-protocol/src/client.rs` to spawn the ACP server using `harness_core.client.cli`.
   - Updated all `unittest.mock.patch("harness_core.<module>...")` strings in `tests/` to target their respective domain subpackage paths.
   - Updated relative imports in `harness_core/` root files (`gateway.py`, `config_loader.py`, `graphify.py`, `harness2_adapter.py`) to point to domain subpackages.
   - Preserved `harness_core/__init__.py` clean dynamic exports for all 7 domain namespaces and top-level symbols (`HarnessOrchestrator`, `LedgerStore`, `TwoTierModelRouter`, `ToolGateway`).

2. **Purge of 47 Top-Level Forwarding Stub Files:**
   - Deleted:
     `acp.py`, `asgi.py`, `auto_setup.py`, `backup.py`, `browser.py`, `cli.py`, `code_kernel.py`,
     `context_compressor.py`, `cow.py`, `credential_pool.py`, `daemon.py`, `dashboard.py`, `db.py`,
     `doctor.py`, `gc.py`, `git_autopilot.py`, `github_action.py`, `memory.py`, `model_metadata.py`,
     `models.py`, `onboard.py`, `orchestrator.py`, `process_registry.py`, `project_registry.py`,
     `prompt_builder.py`, `proof.py`, `react_loop.py`, `reasoning_effort.py`, `recipes.py`,
     `reflection.py`, `router.py`, `sandbox.py`, `scheduler.py`, `search.py`, `server.py`,
     `skill_synthesizer.py`, `skillspector.py`, `soul.py`, `subagent.py`, `tui.py`, `tui_launcher.py`,
     `ui_selector.py`, `vision.py`, `voice.py`, `web_search.py`, `webhooks.py`, `worker_daemon.py`
   - Kept: `__init__.py`, `config_loader.py`, `gateway.py`, `eval_runner.py`, `graphify.py`, `graphify_adapter.py`, `harness2_adapter.py`, `local_provider.py`, `cloud_provider.py`, `mlx_provider.py`, `mcp_client.py`, `vendor_paths.py`, and all subpackages.

---

## 3. Empirical Verification Results

1. **Cargo Test Workspace:**
   - Command: `cargo test --workspace`
   - Output: 69 passed (13 in `hex-protocol`, 1 live ACP server test, 3 protocol tests, 16 in `hex-tui`, 3 context window tests, 8 design system tests, 4 screen overlay tests, 5 start/session tests, 4 session management tests, 8 UI tests, 2 virtual render tests, 9 UI rendering tests).
   - Result: 0 failures, 100% green.

2. **Full Pytest Suite:**
   - Command: `.venv/bin/pytest tests/`
   - Output: `992 passed, 1 skipped in 203.58s (0:03:23)`
   - Result: 0 failures, 100% green.

3. **Atomic Git Commit:**
   - Commit: `3b3feea`
   - Commit message: `refactor(harness_core): sweep imports to domain subpackages and remove root stubs`

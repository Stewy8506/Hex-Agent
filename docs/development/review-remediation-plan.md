# Review remediation plan (HEX_VS_HERMES_REVIEW.md)

Source: `HEX_VS_HERMES_REVIEW.md` (revision f4ebae9). Order follows §14.
Each sub-phase lands as its own commit on `remediation/phased-fixes`; nothing merges to `main`.
Every fix ships with a regression test that fails against the old behavior.

## Progress

Status legend: `[DONE]` landed + verified + committed · `[WIP]` in progress · `[TODO]` not started.

- Phase 1 — Protect user work and authority: **8/8 sub-phases DONE** (B01, B02, B04, B45, B46, B52, B47, B48, B49, B12, B05, B56).
- Phase 2 — Make completion and recovery truthful: **1/12 DONE** (B03); B11 in progress.
- Phase 3 — Finish primary user surfaces: not started.
- Phase 4 — Expand from real lifecycle integrations: not started.

Last updated: 2026-11-18 (through B03, commit `9926fca`).

## Phase 1 — Protect user work and authority (gate: zero later effects for denied/canceled work; publication preserves metadata/concurrent work)

- **1.1 B01** `[DONE 97afc70]` — Conflict-aware delta publication. New `publish_delta(base, attempt, target)` in `storage/cow.py`: preserve `.git/.hex/.venv`, detect concurrent edits against base snapshot, stop on conflict, recoverable apply. Wire `orchestrator` publication call site to it.
- **1.2 B02** `[DONE e8f1001]` — Mandatory gateway boundary: ReAct dispatch (`agent/react_loop.py`) and ACP `tools/call` (`hex_cli/acp.py`) route through `ToolGateway.invoke`; denied tool ⇒ zero side effects on every entry point.
- **1.3 B04** `[DONE 2f6c432]` — Sandbox honesty + fail-closed confined mode: `restricted_subprocess` explicitly labeled trusted local execution; `require_confinement` refuses to fall back; policy surface distinguishes trusted-local vs confined.
- **1.4 B45/B46** `[DONE 5e82d29, 238e54b]` — Narrow approval leases (bind command+args, task/session scope, cwd is not path authority; no-command approval no longer grants whole workspace) and POST-only approval decide with required explicit decision.
- **1.5 B52** `[DONE d9819fa]` — Browser navigation path authority: reject `file://` outside workspace containment (same resolved-path checks as `read_file`).
- **1.6 B47/B48/B49** `[DONE 4e00016, 76e3258, 4dca725]` — Effective cancellation: ReAct loop checks cancel between model/tool waits; terminal-state CAS so finalization cannot overwrite CANCELED; ACP cancel wakes pending approval waits; headless Ctrl-C/EOF orderly shutdown.
- **1.7 B12** `[DONE 347ab16]` — Delegation deadlines/budgets enforced, parent cancel fans out to children, late results cannot overwrite INTERRUPTED.
- **1.8 B05/B56** `[DONE 1d66488, 8cc585f]` — Platform-aware shell resolution for gateway terminal + skill executor on Windows (shared resolver; no hardcoded `/bin/sh`).

## Phase 2 — Make completion and recovery truthful (gate: no success without effects/evidence; crash-resume reproduces tree+transcript; schema parity)

- **2.1 B03** `[DONE 9926fca]` — Provider outage/failed observation ⇒ explicit failure; deterministic fallback only under explicit demo mode.
- **2.2 B11** `[WIP]` — Fail closed: no-router delegation and no-orchestrator worker must fail, never manufacture COMPLETED/SUCCEEDED.
- **2.3 B10/B13/B17** `[TODO]` — Real child tool loop through shared gateway with enforced role tool subset; server-owned depth; bounded spawn concurrency; explicit parent workspace for diffs.
- **2.4 B51/B53/B55** `[TODO]` — Vision sends real pixels or fails; browser exposed as honest HTML-fetch/parse capability (or real engine); voice STT/TTS return explicit unsupported instead of mock success.
- **2.5 B06/B09** `[TODO]` — DAG topology validation at save (cycles/dangling deps rejected); replan budget exhaustion pauses for attention.
- **2.6 B07/B08** `[TODO]` — Todo `complete` action implemented; atomic writes, propagated errors, unique IDs, finite statuses.
- **2.7 B37** `[TODO]` — Single migration source: Alembic head matches runtime schema on SQLite and PostgreSQL; clean-install feature-table test.
- **2.8 B36** `[TODO]` — Journal replay bound to restored tree/version (checkpoint/reapply sealed side effects consistently).
- **2.9 B41/B44** `[TODO]` — Structured ACP context restore (tool rows/pairing); terminal-task reentry guard + startup orphan reconciliation.
- **2.10 B42/B40** `[TODO]` — Claim transaction failure fails/retries; `/rewind` honest conversation-only semantics or real file rollback; dialogue turn persistence wired.
- **2.11 B14/B15** `[TODO]` — MoA typed caller passing route diversity settings, deadline-aware cancellation; fenced worker stops claiming and cancels execution.
- **2.12 B16** `[TODO]` — Windows process-tree teardown via Job Objects.

## Phase 3 — Finish primary user surfaces

- **3.1** TUI: key-kind filter (B18), bracketed paste + multiline (B19), Unicode cursor indexing (B21).
- **3.2** TUI: approval RPC failure recovery (B20), short-terminal approval fallback (B25), truthful model/counts (B26).
- **3.3** TUI: turn-associated final text (B22), resume handshake (B23), embedded SQLite (B24).
- **3.4** Desktop: typed approval outcomes + real TTL (B27/B30), queued approvals (B29), lossless final text (B31).
- **3.5** Desktop: supervised reconnect (B28), model catalog from backend (B32), session persistence/resume.
- **3.6** Desktop: platform-gated transports + Windows interpreter (B33), authenticated socket or removal (B34), stderr diagnostics/async git/late-ack semantics (B35).
- **3.7** ACP: standard schema interop or explicit private-protocol labeling; actionable diagnostics.

## Phase 4 — Expand from real lifecycle integrations

- **4.1 B38/B43** — Locked scheduler tick claims, crash/refire + non-overlap policy, Vixie DOM/DOW OR semantics.
- **4.2 B39** — Correct launchd timer / systemd timer contracts; Windows Task Scheduler backend.
- **4.3** Config-driven MCP startup registration + live tool discovery (B57).
- **4.4 B54** — Real Discord ingress or explicit send-only labeling.
- **4.5** Wire hybrid skill retrieval into orchestrator context assembly.
- **4.6** Memory/context/cognition review (outstanding §12) before touching those subsystems.

## Environment notes

- Python 3.14.9; test venv at `%LOCALAPPDATA%\hermes\cache\scratch\hex-venv` with `pip install -e .` (package aliasing: `storage` → `harness_core.storage`, `agent` → `harness_core.core`, `hex_cli` → `harness_core.client`, etc.).
- Pre-existing baseline failure: `test_cow_providers.py::TestOrchestratorCowInjection::test_orchestrator_custom_provider_injection` — Windows `PermissionError` unlinking open `ledger.db` during tempdir cleanup (test teardown issue, tracked, not caused by remediation).
- Rust toolchain unavailable on this host: Phase 3 Rust changes must be written carefully and compile-verified where possible; flagged in commits if unverified.
# HEX ROADMAP — Remaining Phases & Desktop Completion

**Version:** 1.0 · **Date:** 2026-11-23
**Deep references:** `BACKEND_PLAN.md` (wire contracts, team protocol, backend systems §1–5), `backend/hex-backend/PRD.md`, `docs/development/desktop-design-parity-plan.md` (visual parity, complete).

This file is the single tracker for everything left. Backend-integration phases (A–C) are done; D–G remain. The desktop UI completion workstream (DX) runs **apart from** D–G — its goal is to turn the GPUI app from a faithful mockup recreation into a fully functional product.

**Standing rules** (all phases): commit on BOTH repos after each phase (backend repo `backend/hex-backend`, agent repo root) — skip backend commit when it is unchanged; never push unless asked; team/account features are desktop-first (TUI untouched except optional read-only team attach at the very end); never commit `backend/` or runtime `data/` in the agent repo; every signed envelope is verified against `GET /v1/server/pubkey` before use.

---

## Part I — Backend Integration Phases (remaining)

### Phase D — Team Mesh End-to-End (maps to BACKEND_PLAN Phase 3)

Single Primary Host execution with post-turn replication, RBAC approvals, and failover. Backend systems `/v1/teams`, `/v1/sessions/*`, `/v1/signal` already exist and are tested.

1. **TurnCommitHook** — `agent/orchestrator.py`: on `VERIFYING -> COMPLETED` build a `TurnCommitBundle` (`cloud/team_mesh.py` already validates the shape): `project_session_id`, `host_epoch`, `turn_seq`, `cow_delta_sha256`, `ledger_chain_tip` + new `task_events` range, `memory_delta_sha256` (`.hex/MEMORY.md`, `.hex/memories/GUARDRAILS.md`), `proof_merkle_root` from `capabilities/proof.py` signed with the host's device key. Opt-in per session (`ExecutionMode`: `LOCAL` default; `TEAM_PRIMARY_HOST` / `TEAM_REPLICA_PEER` opt-in; `CLOUD_WORKER` later in Phase G).
2. **CoW delta packager** — `storage/cow.py`: deterministic CAS delta tarball / git bundle for the turn's filesystem diff; `storage/gc.py`: pin the latest replicated snapshot so GC never evicts failover state.
3. **Approval leases** — `agent/approval_lease.py`: validate remote R0–R4 decisions against team RBAC (`owner`/`admin`/`developer` may approve; `observer` denied). Desktop approval modal already sends decisions — route through the lease check when the session is team-hosted.
4. **Mesh runtime** — `cloud/team_mesh.py` + `cloud/signaling.py` (`SignalLink` from Phase C): 3s `HEARTBEAT(host_epoch, turn_seq)`; miss >10s → `request_failover`; apply signed `HostLease` updates with strict `host_epoch` fencing (old epoch = demote to replica, pull missed bundles); `turn_ack` per applied bundle; record checkpoints via `POST /v1/sessions/{id}/turn-commit`.
5. **Surfaces** — ACP `team/*` methods (roster, lease, mode) + `cloud/event` pushes (`host_lease_updated`, `failover_complete` already flow through SignalLink); `hex team` CLI (`hex_cli/commands/team.py`: status/list/attach); desktop Team pane (roster, shared sessions, Host badge, `This Mac`/`Peer Host`/`Cloud` location chips) — this is the desktop-first centerpiece.
6. **Tests** — TurnCommitBundle canonical digest parity Python↔Rust (exists, extend), lease fencing rejects stale epochs, failover election against a live two-device backend WS fixture, RBAC denial for observer.

### Phase E — P2P Transport & Personal Sync (maps to BACKEND_PLAN Phase 1 leftover)

1. **`cloud/p2p_transport.py`** — direct E2EE tunnels between enrolled devices: QUIC (aioquic) preferred, stateless encrypted relay fallback via hex-backend when strict NAT blocks traversal; session keys derived from device Ed25519 identities (X25519 ephemeral + HKDF); transport abstracted so Team Mesh ACP streams ride the same tunnel.
2. **`cloud/sync_engine.py`** — three multiplexed streams over the transport:
   - `ledger-sync`: `storage/db.py` gains `export_events_since(seq)` / `merge_remote_chain(events)` with SHA-256 chain verification and `branch_id` soft-branching on conflict.
   - `cognition-sync`: `USER.md` / `SOUL.md` / `MEMORY.md` / `GUARDRAILS.md` + `~/.hex/skills/` — every inbound skill passes a mandatory local SkillSpector scan (`cognition/skillspector.py`) before activation; `cognition/memory.py` + `cognition/soul.py` emit change notifications and run A-MEM reconciliation (`ADD`/`UPDATE`/`DELETE`/`NOOP`) on concurrent edits.
   - `workspace-sync`: opt-in only.
3. **Surfaces** — ACP `cloud/sync/{status,start,stop}`; `hex sync` CLI; desktop device roster gains last-synced/replication status (account modal already lists devices).
4. **Tests** — chain merge conflict soft-branch, SkillSpector blocks malicious skill, encrypted round-trip over loopback QUIC, relay fallback with a blackholed direct path.

### Phase F — Skill Registry & Integrations (maps to BACKEND_PLAN Phase 4)

1. **`cloud/registry_client.py`** — `GET /v1/registry/search` (zero-DB sparse index), signed install manifests (R0–R4 permission declarations verified against the server key), hard SkillSpector quarantine gate, install into `~/.hex/skills/` with rollback.
2. **Integrations** — `capabilities/gateways/` + OAuth PKCE connectors to `/v1/integrations/*` (GitHub, GitLab, Slack, Discord, Google Workspace, Custom); token envelopes stay AES-256-GCM in the vault; health checks + disconnect/revoke.
3. **Surfaces** — ACP `registry/*` + `integrations/*`; `hex registry {search,install,list}` CLI; desktop **Extensions pane** becomes real here (browse/search/install registry packages, manage connectors) — DX6 below lands the local-only pane first.
4. **Tests** — manifest signature + permission enforcement, quarantine blocks on SkillSpector failure, OAuth round-trip against a stub provider.

### Phase G — Cloud Workers, Telemetry, Notifications, Support & Dormant Inference (Phases 5–6)

1. **`cloud/cloud_tasks.py`** — `CLOUD_WORKER` execution mode: dispatch to `/v1/cloud-tasks/*` priority queues (`interactive`/`standard`/`batch`), retry/cancel, E2EE result pointers retrieved via the P2P transport.
2. **`cloud/telemetry.py`** — opt-in operational-only metrics to `/v1/telemetry/events`; hard local privacy wall (never prompt/response/code/paths/secrets).
3. **`cloud/support_client.py`** + `hex doctor --bundle`: redacted diagnostic archive upload with `/v1/support/tickets`.
4. **Notifications** — preferences per category (`security`/`billing` non-disableable); live WS push already arrives via `SignalLink` → surface in the desktop bell (DX bell from Phase C) and TUI read-only later.
5. **Dormant inference** — `providers/hex_gateway_provider.py` (OpenAI-compatible) registered in `agent/router.py`, bypassed unless `inference_gateway.enabled = true`; forwards `prefix_cache_key` for KV-cache sticky routing on `self_hosted_gpu` / `upstream_proxy`.
6. **Tests** — worker dispatch/retry against live backend, telemetry wall unit tests, gateway stays 503/no-op when disabled.

---

## Part II — Desktop UI Completion (DX workstream)

Current state (inventory 2026-11-23): chat shell, sessions (new/open/switch/paged resume), live model catalog picker, approval modal with lease toggle, command palette (5 wired actions), account/cloud modal, update banner + bell, home continue list, tool cards are **real**. Everything else below is placeholder or decorative.

- **DX1 — Shell honesty (DONE)**: stop the UI from lying.
  - Token gauge now real: router records `last_usage` per routed call → ACP terminal `session/update` status carries a camelCase `usage` payload (`inputTokens`/`outputTokens`/`modelId`/`contextLimit` via `get_model_context_length`, no network) → new `AcpEvent::TurnUsage` + `SessionUsage` wire type → gauge set on the active session; bar stays hidden until the first real usage lands (`current_tokens > 0`).
  - Palette: **OpenSettings** toggles the settings card, **ExportTranscript** writes the feed to `<workspace>/.hex/exports/<session>-<stamp>.md`; **CompactContext** and **SwitchWorkspace** removed (no honest mapping — `/compressor` governs log compression, not the live context window, and the router compacts context automatically per call).
  - Composer: dead attach / Auto / mic controls removed (attachments return in DX4); model pill default is a neutral "Select model" instead of a fabricated model; `ProviderStatus::default` is honest offline/HEX.
  - Home top bar: live provider chip (state dot + "HEX · Connected" style label).
- **DX2 — Session management (DONE)**: rename / delete / archive / pin sessions.
  - New ACP methods `session/rename|delete|archive|pin` in `hex_cli/acp.py`, all persisted to `<workspace>/.hex/data/acp_sessions/<id>.json` (+ SQLite transcript rows removed on delete); `session/list` now carries `title`/`archived`/`pinned`.
  - Wire types (`SessionRenameResponse`/`SessionDeleteResponse`/`SessionFlagResponse`, extended `SessionListItem`) + `AcpClient::session_*` methods + `AcpBridge` wrappers.
  - Sidebar: per-row hover "more" affordance opens a keyboard-navigable `SessionActionModal` (menu → rename entry / delete confirmation); optimistic local update with revert-on-failure; deleting the active session returns to Home; archived sessions surface in an ARCHIVED sidebar section (unarchive via the same modal) and are excluded from Home's Continue list.
  - The sidebar search row now opens a `SessionSearchModal` driving the previously never-used `SessionListView::filter_sessions`; Enter/click resumes the selected chat.
  - `refresh_session_list` adopts persisted titles/flags for both new and existing entries.
  - Tests: `tests/client/test_acp_session_management.py` (6) + session_actions unit tests (112 desktop lib tests).
- **DX3 — DONE — Real settings surface**: editable `SettingsModal` (`ui/modals/settings.rs`) backed by new ACP `config/get` + `config/set` (`hex_cli/acp.py`): whitelisted schema (model, runtime, web_search, cloud, learning — no update-channel/tier/budget keys: they don't exist in config), server-side validation (choices, int ranges, required/optional text), secret-like keys rejected toward `credentials.env`, atomic `write_config_yaml_atomic` deep-merge preserving unrelated sections, unparseable files never overwritten. Wire keys camelCase (`_camel`/`_snake` boundary helpers); empty optional text = key removal. Modal: keyboard nav (↑↓ rows, ←→/Enter cycle choices, editing via typed-char capture), dirty diff (only changed keys sent, dirty rows highlighted), Esc cancels in-flight text edit vs modal, offline state (`Runtime offline — settings unavailable`) instead of fake data. Palette/OpenSettings/header "more" all route here; `render_settings_card` + `settings_open` removed.
- **DX4 — DONE — Feed fidelity**: diffs + reasoning + approvals in the stream.
  - Tool diffs: `tools/filesystem.py` emits a bounded unified diff (`unified_diff_bounded`, 240 lines) at the write moment in `_tool_write_file`/`_tool_patch`; `hex_cli/acp.py` extracts it into a separate `tool.diff` wire field (`_extract_tool_diff`, 16k chars) after the bounded preview so the 300-char payload budget stays intact; `ToolUpdate.diff` (hex-protocol) → `AcpEvent::ToolResult.diff` → `feed.update_tool_result` builds `DiffView::from_patch` on the card.
  - Reasoning stream: `_ThinkSplitter` in `acp.py` routes `<think>…</think>` (tags may split across chunks) to new `agent_thought` deltas on the ACP stream callback — models without think tags stream entirely as answer and no thought is fabricated; bridge dispatch gates `agent_thought` to `AcpEvent::ThoughtChunk` (never `TokenChunk`); `ChatView` feeds it into the turn assistant's `ThinkingDisclosure` and stamps reasoning elapsed from `TurnComplete`.
  - Approvals as feed cards: `ApprovalCard` (pending/approved/denied, mirrored per-session feed + active feed) added on `ApprovalRequest`; the modal stays the decision surface and card state flips only on a runtime-confirmed applied outcome. Exported transcripts render approvals as blockquotes.
  - Attachments: composer attach button opens a native file picker (`rfd`), removable path chips, `can_send` accepts attachments-only prompts; desktop sends absolute paths via `SessionPromptParams.attachments` / `session_prompt_with_attachments`; Python side bounds reads (10 files, 64 KiB each, 256 KiB total) and appends fenced blocks to the prompt.
- **DX5 — Files pane**: real workspace file tree + read-only preview over local FS (no ACP needed), "reveal in editor" handoff. Terminal/Browser segments keep honest empty states with a pointer to this roadmap until a PTY/browser view is scoped (needs product decision).
- **DX6 — Extensions pane (local first)**: installed skills browser backed by the existing ACP `/skills` command surface (list/toggle/detail; SkillSpector status per skill). Registry browse/install arrives with Phase F and slots into the same pane.
- **DX7 — Notebooks & Automations**: needs product decision. Notebooks = local markdown scratchpads under `.hex/notebooks/` (create/edit/search); Automations = scheduled/recurring tasks (orchestrator cron? or Phase G cloud workers). Decide scope before building; the dock items keep honest coming-soon toasts until then.

Suggested order: DX1 → DX2 → DX3 → DX4 → DX5 → DX6 → DX7, with Phase D desktop work (Team pane) interleaved when Phase D lands.

Motion track (separate plan): see [`ANIMATIONS_PLAN.md`](ANIMATIONS_PLAN.md) — Apple-style spring motion (AN1–AN4), one commit per phase, runs alongside the DX track.

---

## Done (reference)

- **Phase A** (backend `b4596ee`, agent `4887bb6`): Ed25519 server identity + EdDSA JWTs, challenge enrollment, AES-256-GCM vault, `cloud/` clients on Ed25519, hex-protocol `backend.rs`/`p2p.rs` + digest parity tests.
- **Phase B** (agent `3e26c58`): `CloudAccountManager`, ACP `cloud/auth/*` + `cloud/devices/*`, `hex cloud` CLI, desktop Account modal + device header button.
- **Phase C** (agent `b313474`): verified update check / remote config / manifest, `cloud/flags.py`, `SignalLink` real WS client, ACP update/config/signal methods, `hex update` CLI, desktop update banner + notification bell, config `cloud:` section mapping, `HEX_BACKEND_URL` env fix.

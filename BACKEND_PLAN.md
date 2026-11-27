# HEX Harness — Backend Integration Plan (`hex-backend` ↔ `Harness`)

**Version:** 1.1  
**Date:** 2026-11-22  
**Companion PRD:** `../hex-backend/PRD.md`  
**Backend Repository:** `../hex-backend` (Phases 1–6 implemented & tested)  
**Target Codebase:** `Harness` (`hex-ai` v0.22.0, `crates/hex-protocol`, `crates/hex-tui`) and the sibling `Hex-App` desktop repository

---

## 1. Purpose & Design Invariants

This document defines the full client-side architecture, Team Mesh collaboration & failover protocol, wire contracts, and module hooks connecting `Harness` with `hex-backend`:

1. **Offline-First & Zero Lock-In**: HEX remains 100% functional locally with no backend account required. Cloud auth, P2P sync, team hosting, remote config, cloud workers, and future model serving are strictly additive layers.
2. **Minimal-DB & Zero-Knowledge Boundary**: `Harness` never uploads plaintext chats, source code, skills, or memory files (`USER.md`, `SOUL.md`, `MEMORY.md`, `GUARDRAILS.md`) to `hex-backend`'s database. The backend database stores only coordination metadata (accounts, rotating refresh hashes, **encrypted device IDs** + Ed25519 pubkeys, team RBAC rosters, monotonic `host_epoch` leases, hashed API keys, encrypted OAuth envelopes, and hash-chained security audit logs).
3. **Direct E2EE P2P Data Plane**: All personal cross-device sync and team codebase/ledger replication flows directly between authenticated, non-revoked devices over E2EE P2P tunnels (with stateless encrypted relay fallback when strict NAT blocks direct traversal).
4. **Reuse Existing `Harness` Primitives**: Integration builds directly on `ledger.db` (SHA-256 hash chains), `storage/cow.py` (CoW snapshots & CAS deltas), `capabilities/proof.py` (Merkle `ProofArtifact`), `cognition/skillspector.py`, `storage/vault_keychain.py`, and `hex_cli/acp.py` (`crates/hex-protocol`).

---

## 2. Client Module Architecture in `Harness`

```text
Harness/
├── cloud/                            # Client integration package (Phase 1–3 core implemented)
│   ├── __init__.py                   # [Implemented] Package exports
│   ├── auth_client.py                # [Implemented] Client-side encrypted device IDs (dev_enc_v1:) + auth HTTP client
│   ├── signaling.py                  # [Implemented] P2P peer discovery, revocations, live kill switches, host leases
│   ├── updater.py                    # [Implemented] Signed 8-component manifest verifier + rollback.json tracker
│   ├── remote_config.py              # [Implemented] Signed feature flag & routing merger (local denials always win)
│   ├── team_mesh.py                  # [Implemented] TeamMeshCoordinator, host_epoch fencing, TurnCommitBundle ACKs
│   ├── p2p_transport.py              # [Planned] Direct E2EE P2P transport (QUIC/WebRTC + stateless relay fallback)
│   ├── sync_engine.py                # [Planned] Personal multi-device sync (ledger.db, ~/.hex/skills, memory)
│   ├── registry_client.py            # [Planned] Sparse-index skill/plugin/tool installer + SkillSpector gate bridge
│   ├── cloud_tasks.py                # [Planned] Ephemeral cloud worker dispatch & E2EE diff retrieval
│   ├── telemetry.py                  # [Planned] Opt-in operational-only telemetry (strict prompt/code exclusion)
│   └── support_client.py             # [Planned] Support ticket submission & redacted `hex doctor --bundle` attach
├── providers/
│   └── hex_gateway_provider.py       # [Planned - Phase 6] Dormant OpenAI-compatible adapter for hex-backend inference
├── crates/hex-protocol/src/
│   ├── backend.rs                    # [Implemented] Shared Rust wire types: DeviceEnrollment, ServerPubkey, HostLease, ReleaseManifest, SignedEnvelope
│   └── p2p.rs                        # [Implemented] Shared Rust wire types: SyncEnvelope, TurnCommitBundle (canonical digest), ElectionVote
└── tests/cloud/
    └── test_cloud_client.py          # [Implemented] Unit & signed-envelope contract tests
```

---

## 3. Existing `Harness` Module Integration Map

| Existing `Harness` Module | Required Integration Hook | Connected `hex-backend` System |
| :--- | :--- | :--- |
| `storage/vault_keychain.py` | Store device Ed25519 signing keypair, `encrypted_device_id`, account root key, and rotating session/OAuth credentials in OS Keychain. | **#1 Identity & Auth**, **#7 Integrations** |
| `storage/db.py` (`ledger.db`) | Add cursor-based delta export/import (`export_events_since(seq)`, `merge_remote_chain(events)`) using SHA-256 hash chaining + `branch_id` soft-branching on conflict. | **#5 Memory Sync**, **Team Mesh** |
| `storage/cow.py` & `storage/gc.py` | Package post-turn filesystem diffs as deterministic CAS delta tarballs / git bundles; pin the latest replicated turn snapshot on Replica Peers so GC never evicts failover state. | **Team Mesh (Post-Turn Replication)** |
| `capabilities/proof.py` | Sign each completed turn's `ProofArtifact` Merkle root with the Primary Host's Ed25519 device key so peers cryptographically verify every replicated turn. | **Team Mesh**, **#10 Security** |
| `agent/orchestrator.py` | Add a `TurnCommitHook` on `VERIFYING -> COMPLETED` to broadcast `TurnCommitBundle`s to team peers; support `ExecutionMode` (`LOCAL`, `TEAM_PRIMARY_HOST`, `TEAM_REPLICA_PEER`, `CLOUD_WORKER`). | **Team Mesh**, **#4 Cloud Tasks** |
| `agent/approval_lease.py` | Validate remote R0–R4 tool approval decisions from team peers against their Team RBAC role (`owner`/`admin`/`developer` allowed; `observer` denied). | **#1 RBAC**, **Team Mesh** |
| `cognition/memory.py` & `cognition/soul.py` | Emit change notifications on `USER.md`, `SOUL.md`, `MEMORY.md`, `GUARDRAILS.md`; invoke A-MEM CRUD reconciliation (`ADD`/`UPDATE`/`DELETE`/`NOOP`) on concurrent P2P edits. | **#5 Memory Sync** |
| `cognition/skillspector.py` & `skill_engine/` | Mandatory local `SkillSpector` scan on every skill received via P2P sync or installed from the sparse-index registry before activating in `~/.hex/skills/`. | **#5 Memory Sync**, **#6 Registry** |
| `harness_core/config_loader.py` & `models/model_metadata.py` | Merge verified remote config profiles, feature flags, emergency kill switches, and provider routing weights with `~/.hex/config.yaml` (local security denials always win). | **#3 Feature Flags & Remote Config** |
| `agent/router.py` & `providers/` | Register dormant `HexGatewayProvider` (disabled when `inference_gateway.enabled = false`) for future rented GPU (`self_hosted_gpu`) or managed upstream (`upstream_proxy`) serving with KV-cache sticky routing (`prefix_cache_key`). | **#13 Model Serving Gateway** |
| `hex_cli/acp.py` & `crates/hex-protocol/` | Abstract ACP JSON-RPC 2.0 transport across stdio/Unix socket (local), E2EE P2P tunnel (Team Primary Host), and HTTPS/WSS (Cloud Worker). | **Team Mesh**, **#4 Cloud Tasks** |
| `hex_cli/cli.py` & `hex_cli/doctor.py` | Add CLI subcommands (`hex auth`, `hex devices`, `hex sync`, `hex team`, `hex update`, `hex registry`, `hex support`) and `hex doctor --bundle` redacted diagnostic archive. | **#1, #2, #5, #6, #9, #12** |
| `Hex-App` | Replace `PLAN.md` "Coming soon" placeholders: account footer, device/sync status, Team session panel + Primary Host badge, location chips (`This Mac` / `Peer Host` / `Cloud`), update banner, and notification bell. | **#1, #2, #4, #11, Team Mesh** |

---

## 4. Team Collaboration, Post-Turn Replication & Failover Specification

### 4.1 Team Formation & RBAC Permission Matrix
Teams are created and managed via `hex-backend` (`/v1/teams`, `/v1/teams/{team_id}/invites`, `/v1/teams/invites/accept`, `/v1/teams/{team_id}/members`). Invite tokens are single-use, email-bound, time-limited, and stored strictly as SHA-256 hashes.

| Role | Manage Members & Invites | Create Project Sessions | Eligible as Primary Host | Submit Prompts & Approve R0–R4 Leases | Trigger Manual Host Handoff | Watch Live ACP Stream |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`owner`** | Yes (all) | Yes | Yes | Yes | Yes | Yes |
| **`admin`** | Yes (non-owners) | Yes | Yes | Yes | Yes | Yes |
| **`developer`** | No | Yes | Yes | Yes | Automatic failover only | Yes |
| **`observer`** | No | No | **No (excluded)** | **No (read-only)** | No | Yes |

### 4.2 Single Primary Host Execution Model
- Each shared project session (`project_session_id` + `repo_fingerprint`) has **exactly one** active Primary Host device at any moment, governed by a cryptographically signed `HostLease` (`host_epoch`, `primary_device_record_id`, `last_committed_turn_seq`).
- **`TEAM_PRIMARY_HOST` mode**: Runs `agent/orchestrator.py`, CoW sandboxes (`storage/cow.py`), local/cloud model routers, and tools on the Primary Host computer, exposing its ACP JSON-RPC 2.0 stream over direct E2EE P2P tunnels to connected teammates.
- **`TEAM_REPLICA_PEER` mode**: Teammate devices attach their `hex-desktop` / `hex-tui` to the Primary Host's ACP-over-P2P stream to submit prompts, inspect live diffs/thoughts, and cast RBAC-validated R0–R4 tool approvals while maintaining a synchronized local shadow workspace.

### 4.3 Post-Turn Atomic Replication (`VERIFYING -> COMPLETED`)
After every completed work turn on the Primary Host:
1. `agent/orchestrator.py` fires its `TurnCommitHook` and packages a `TurnCommitBundle`:
   - `project_session_id`, `host_epoch`, `turn_seq`
   - `cow_delta_sha256` (deterministic CoW/git filesystem diff or CAS delta tarball)
   - `ledger_chain_tip` + new `ledger.db` `task_events` range
   - `memory_delta_sha256` (updated `.hex/MEMORY.md` / `.hex/memories/GUARDRAILS.md`)
   - `proof_merkle_root` (`capabilities/proof.py` `ProofArtifact` root)
2. Primary Host streams the `TurnCommitBundle` and binary delta directly over E2EE P2P to all connected Replica Peers.
3. Each Replica Peer validates `host_epoch == current_epoch` and `turn_seq > last_applied_turn_seq` via `cloud/team_mesh.py`, applies the delta to its pinned shadow workspace, merges `ledger.db` events, and sends `turn_ack(project_session_id, turn_seq)` to `hex-backend` `/v1/signal`.
4. Primary Host records the turn checkpoint with `POST /v1/sessions/{project_session_id}/turn-commit` (fenced by `host_epoch` and `primary_device_record_id`) and receives a signed checkpoint receipt.

### 4.4 Host Death Detection, Randomized Failover & Graceful Handoff
- **Heartbeat & Failure Detection**: Primary Host emits `HEARTBEAT(host_epoch, turn_seq)` every 3 seconds. If missed for more than 10 seconds, connected Replica Peers send `request_failover` over `/v1/signal` (or `POST /v1/sessions/{id}/failover`).
- **Eligibility & Election**:
  1. `hex-backend` filters online team peers to those that are: trusted, marked `host_eligible=true`, not the failed device, and holding an RBAC role in `{owner, admin, developer}` (`observer` devices are strictly excluded even if they ACK'd a higher turn).
  2. Among eligible peers, `hex-backend` selects the subset holding the highest replicated `last_turn_seq` (`max(turn_seq)`), then chooses a new Primary Host via cryptographically random selection (`secrets.choice`).
  3. `hex-backend` atomically increments `host_epoch` (`epoch -> epoch + 1`), signs the new `HostLease`, and broadcasts `host_lease_updated` over WebSocket to all connected team peers.
- **Graceful Admin Handoff**: An `owner` or `admin` can explicitly transfer Primary Host ownership to a specific online eligible device via `POST /v1/sessions/{id}/failover` with `target_device_id`.
- **Split-Brain Fencing**: Every client (`TeamMeshCoordinator.apply_signed_host_lease`) and the backend (`record_turn_commit`) strictly reject any lease or turn commit with an older `host_epoch`. If the crashed former host comes back online with `epoch=1` after failover moved the session to `epoch=2`, it is automatically demoted to `TEAM_REPLICA_PEER` and pulls missed `TurnCommitBundle`s from the new host.

---

## 5. Additional Wire Protocols (`Harness` ↔ `hex-backend`)

### 5.1 Auth, Encrypted Device Enrollment & Personal P2P Sync
- Client generates an Ed25519 device keypair and computes `encrypted_device_id = dev_enc_v1:...` client-side (`cloud/auth_client.py`) before calling `POST /v1/devices/register`; enrollment requires signing a single-use nonce from `POST /v1/devices/challenge` to prove private-key possession.
- Rotating refresh credentials detect reuse and revoke the entire token family; revoking a device (`POST /v1/devices/{id}/revoke`) immediately broadcasts `device_revoked` / `peer_revoked` over `/v1/signal` so sibling devices drop its P2P tunnel.
- Personal multi-device sync runs over three multiplexed P2P streams: `ledger-sync` (hash-chained `ledger.db`), `cognition-sync` (`USER.md`, `SOUL.md`, `MEMORY.md`, `GUARDRAILS.md`, `~/.hex/skills/` gated by `SkillSpector`), and opt-in `workspace-sync`.

### 5.2 Independent 8-Component Updates, Feature Flags & Kill Switches
- `POST /v1/releases/check` and `GET /v1/releases/manifest/{channel}` return signed envelopes covering all 8 components (`hex-core`, `hex-desktop`, `hex-tui`, `hex-protocol`, `hex-cli`, `hex-skills-builtin`, `hex-graphify`, `hex-skillspector-rules`), enforcing `min_supported_version`, cross-component `requires` compatibility, and yanked-version rollbacks (`~/.hex/rollback.json`).
- `GET /v1/config/evaluate` returns signed feature flags, config profiles (`default`, `low_bandwidth`, `strict_enterprise`), remote model routing, and active kill switches. Emergency kill switches (`POST /v1/config/kill-switch`) push live over `/v1/signal` (`kill_switch_updated`) and immediately override enabled flags.

### 5.3 Registry, Integrations, Cloud Tasks, Telemetry, Notifications, Support & Dormant Inference
- **Registry (`GET /v1/registry/search`, `POST /v1/registry/packages/{id}/install-manifest`)**: Zero-DB sparse index with signed R0–R4 permission manifests and hard `SkillSpector` quarantine blocking.
- **Integrations (`/v1/integrations/*`)**: PKCE OAuth2 initiation, encrypted token envelopes, rotation, health checks, and disconnect/revoke for GitHub, GitLab, Slack, Discord, Google Workspace, and Custom connectors.
- **Cloud Tasks (`/v1/cloud-tasks/*`)**: Priority queue (`interactive` / `standard` / `batch`) with retry recovery, cancellation, resource limits, and short-TTL E2EE result pointers.
- **Telemetry (`/v1/telemetry/events`)**: Opt-in operational metrics only; hard privacy wall rejects any prompt, response, source code, file path, or secret fields.
- **Notifications (`/v1/notifications/preferences/{category}`)**: Real-time WebSocket push; `security` and `billing` alerts are non-disableable.
- **Support (`/v1/support/tickets`)**: Ticket creation with optional redacted `hex doctor --bundle` archive pointers and incident-linked batch resolution.
- **Dormant Model Serving (`/v1/inference/*`)**: Disabled on Day 1 (`HEX_INFERENCE_GATEWAY_ENABLED=false`, returns `503`). Future-ready for `self_hosted_gpu` (rented vLLM/SGLang GPU nodes with KV-cache sticky routing keyed on HEX's frozen Turn-0 `prefix_cache_key`) and `upstream_proxy` (direct commercial providers with 429 credential rotation).

---

## 6. Phased Implementation Status (`Harness` Side)

### Phase A: Crypto & Wire-Contract Hardening (backend + shared)
- [x] `hex-backend`: all envelope signing migrated from shared-secret HMAC to persistent Ed25519 identity key (`HEX_SIGNING_KEY_PATH`), published via `GET /v1/server/pubkey` and `/health`; access JWTs switched HS256 -> EdDSA.
- [x] `hex-backend`: device enrollment now requires a single-use challenge nonce (`POST /v1/devices/challenge`) signed with the device's real Ed25519 private key; pubkeys validated/normalized at registration.
- [x] `hex-backend`: integration token vault upgraded to AES-256-GCM (`enc_v2`) with persisted master key (`HEX_MASTER_KEY_PATH`). Outbound webhooks remain per-webhook HMAC (by design).
- [x] `cloud/`: real Ed25519 device keypairs + challenge signing in `auth_client.py`; Ed25519 envelope verification in `updater.py` / `remote_config.py` / `team_mesh.py`.
- [x] `crates/hex-protocol/src/{backend,p2p}.rs` implemented with cross-language contract tests (TurnCommitBundle canonical digest parity Python <-> Rust).

### Phase B: Account & Devices End-to-End (harness core + desktop)
- [x] `cloud/session.py`: `CloudAccountManager` — keychain-persisted device identity (Ed25519 keypair via `storage/vault_keychain.py`) and session tokens, enroll-once/reuse `device_record_id`, JWT-`exp`-driven auto-refresh (401 -> rotate -> retry), offline-safe `CloudAccountError`; base URL from `--base-url` > `HEX_BACKEND_URL` (config `cloud.base_url`) > `http://127.0.0.1:8787`. (`HEX_CLOUD_BASE_URL` is reserved for the model-provider endpoint, agent/router.py.)
- [x] `cloud/auth_client.py`: `refresh()`, `list_devices()`, `revoke_device()`; `identity_material()`/`identity_from_material()` for keychain round-trip of the keypair.
- [x] ACP (`hex_cli/acp.py`): `cloud/auth/{status,register,login,logout}` + `cloud/devices/{list,revoke}` methods and `cloud/event` push notifications (login/logout/revoke) for the desktop.
- [x] CLI: `hex cloud {status,register,login,logout,devices,revoke}` (`hex_cli/commands/cloud_account.py`), human + `--json` output, getpass password prompt.
- [x] Desktop (`Hex-App`): typed `AcpClient`/`AcpBridge` cloud methods, `AcpEvent::CloudAccount` demux, Account modal (hand-rolled email/password entry, login/register, device roster + revoke + sign out), header device button and sidebar account footer wired (email + "Cloud connected"/"Local mode").
- [x] Tests: unit tests with memory keychain + cross-repo e2e (`tests/cloud/test_backend_integration.py`) booting real hex-backend under uvicorn: register -> login -> challenge enrollment -> device list -> re-login reuse -> self-revocation -> token rotation.

### Phase C: Updater, Remote Config & Signaling Live (harness core + desktop)
- [x] `cloud/auth_client.py`: `fetch_server_pubkey()`, `get_manifest(channel)`, `check_updates(token, installed, channel, platform)`, `evaluate_config(token, tier/channel/profile/beta)`; platform falls back to manifest `"any"` artifacts.
- [x] `cloud/session.py`: `check_updates()` / `evaluate_remote_config()` / `release_manifest()` — every envelope verified against `GET /v1/server/pubkey` before use; `access_token()` / `device_record_id()` providers for the signal link.
- [x] `cloud/flags.py`: process-wide verified-flags singleton (`flag_enabled()`, `active_kill_switches()`); `config_loader.py` maps config `cloud:` section (`base_url` -> `HEX_BACKEND_URL`, channel/profile/tier/beta vars). `models/model_metadata.py` intentionally NOT flag-gated — it is context-length probing with no natural flag consumers (documented deviation).
- [x] `cloud/signaling.py`: `SignalLink` — real `websockets.sync` client to `/v1/signal` with reconnect/backoff, token re-read per attempt, thread-safe `send_turn_ack`/`send_p2p_signal`/`request_failover`, `status()`, injectable connect factory; `websockets>=12.0` dependency.
- [x] ACP: `cloud/update/check`, `cloud/config/evaluate`, `cloud/signal/{start,stop,status}`; `cloud/auth/logout` stops the signal link.
- [x] CLI: `hex update` (`hex_cli/commands/update.py`) — human + `--json`, `--manifest` view, `--channel`; uses `harness_core.__version__` for `hex-core`.
- [x] Desktop: `CloudUpdatePlan`/`CloudFlags` wire types + `cloud_update_check`/`cloud_config_evaluate` bridge calls; startup/sign-in update check; update banner between header and feed (addition tint = eligible, deletion tint = mandatory/incompatible); header bell with count badge (pending approvals + update) routing to first approval or account modal.
- [x] Tests: local Ed25519-signed envelope units (`test_update_config.py`), SignalLink fake-socket units (`test_signal_link.py`), live-backend e2e additions (signed release check incl. yank rollback, manifest tamper detection, remote-config flags, real WS connect + outbound frames).

### Phase 1: Auth, Device Trust & Personal P2P Sync
- [x] Implement `cloud/auth_client.py` (client-side encrypted device IDs + auth/enrollment HTTP client).
- [x] Implement `cloud/signaling.py` (peer discovery, P2P signal routing, revocation & kill-switch listener).
- [x] Hook `cloud/auth_client.py` into `storage/vault_keychain.py` for OS Keychain persistence (via `cloud/session.py`).
- [x] Add the Python `SyncEnvelope`/`SyncStream` wire contract matching `crates/hex-protocol/src/p2p.rs`.
- [x] Add cursor-based ledger/dialogue export and import with base-hash verification, idempotent replay, atomic merges, and isolated hash-verified branches for divergent remote chains (`storage/ledger_sync.py`).
- [ ] Add cognition/memory manifests and SkillSpector-gated skill synchronization.
- [ ] Implement `cloud/p2p_transport.py` (QUIC/WebRTC E2EE tunnel + relay fallback) and `cloud/sync_engine.py`.
- [ ] Add `hex sync` CLI/ACP wiring and end-to-end two-device transfer tests.
- [x] Add cloud package to `pyproject.toml` and wire CLI subcommands (`hex cloud status/register/login/logout/devices/revoke`; `hex sync` pending with `cloud/sync_engine.py`).

### Phase 2: Component Updater & Remote Configuration
- [x] Implement `cloud/updater.py` (signed manifest verifier, compatibility gate, rollback checkpoints).
- [x] Implement `cloud/remote_config.py` (signed flag & routing merger with local-denial precedence).
- [x] Hook `cloud/remote_config.py` into `harness_core/config_loader.py` (config `cloud:` section -> `HEX_BACKEND_URL` + channel/profile/tier/beta env) and add `hex update` (Phase C). `models/model_metadata.py` deviation: it is offline context-length probing with no flag consumers; artificial gating skipped.

### Phase 3: Team Mesh, Post-Turn Replication & Failover
- [x] Implement `cloud/team_mesh.py` (`TeamMeshCoordinator`, monotonic `host_epoch` fencing, `TurnCommitBundle` validation).
- [ ] Add `TurnCommitHook` in `agent/orchestrator.py` and CoW/git delta packager in `storage/cow.py`.
- [ ] Wire `agent/approval_lease.py` for RBAC-checked remote team approvals and add `hex team` CLI commands.
- [ ] Add `crates/hex-protocol/src/{backend,p2p}.rs` and wire `Hex-App` Team UI, Host badge, and location chips.

### Phase 4: Skill/Plugin Registry & Integrations
- [ ] Implement `cloud/registry_client.py` (sparse-index search, signed install manifests, local `SkillSpector` check).
- [ ] Connect `capabilities/gateways/` and OAuth connectors to `hex-backend` `/v1/integrations/*`.

### Phase 5: Cloud Workers, Telemetry, Notifications & Support
- [ ] Implement `cloud/cloud_tasks.py`, `cloud/telemetry.py`, `cloud/support_client.py`, and `hex doctor --bundle`.
- [ ] Wire live WebSocket notifications from `cloud/signaling.py` into `Hex-App` & `hex-tui`.

### Phase 6: Optional Managed Model Serving & Inference Gateway (Dormant on Day 1)
- [ ] Add dormant `providers/hex_gateway_provider.py` wired to `agent/router.py` (bypassed unless `inference_gateway.enabled = true`), forwarding `prefix_cache_key` for KV-cache sticky routing on rented GPUs.

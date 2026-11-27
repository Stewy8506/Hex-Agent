# `cloud`

**Offline-first client integration for authentication, Ed25519 device identity, signed component updates, remote feature flags/kill switches, P2P signaling, and Team Mesh anti-split-brain coordination against `hex-backend`.**

In [`pyproject.toml`](../pyproject.toml), `cloud/` is packaged as the top-level **`cloud`** package (a sibling to `harness_core`, rather than `harness_core.cloud`). Every component degrades gracefully when offline, unauthenticated, or unconfigured so `Harness` stays 100% functional locally.

## Dependencies & Security Design

- **Dependencies**: `cryptography>=42.0.0`, `websockets>=12.0`, stdlib `urllib`/`json`/`hashlib`/`hmac`, plus [`storage/vault_keychain.py`](../storage/README.md) (`KeychainSecretStore` backed by OS Keychain service `"HEX Autonomous AI Workbench"`).
- **URL Precedence**: `cloud/session.py:resolve_base_url()` resolves explicit URL > `HEX_BACKEND_URL` > `DEFAULT_BASE_URL` (`"http://127.0.0.1:8787"`), intentionally ignoring `HEX_CLOUD_BASE_URL` (which is reserved for LLM provider endpoints).

## Modules

| File | Purpose |
| :--- | :--- |
| `__init__.py` | Exports `ComponentUpdaterClient`, `DeviceIdentity`, `HexAuthClient`, `P2PSignalingClient`, `RemoteConfigClient`, `TeamMeshCoordinator`, and `TurnCommitBundle` (callers import `cloud.session` and `cloud.flags` explicitly). |
| `auth_client.py` | `DeviceIdentity`, `HexAuthClient`, `encrypt_device_id()` (`dev_enc_v1:` client-side pseudonymization), `generate_device_identity()` (`ed25519:<hex>`), `enrollment_signing_payload()`, `sign_enrollment_challenge()`, and stdlib HTTP JSON client for `/v1/auth/{register,login,refresh}`, `/v1/devices*`, `/v1/server/pubkey`, `/v1/releases/*`, and `/v1/config/evaluate`. |
| `session.py` | `CloudAccountManager`, `CloudAccountError`, `KeychainSecretStore`, `MemorySecretStore`, `resolve_base_url()`. Manages device enrollment challenges, OS keychain credential persistence (`cloud_device_identity`, `cloud_session`), proactive JWT refresh (`REFRESH_MARGIN_SECONDS=60` + 401 retry), device management, and signed manifest/config verification. |
| `flags.py` | Thread-safe process-wide singleton (`set_remote_config()`, `remote_config()`, `flag_enabled()`, `active_kill_switches()`, `reset_for_tests()`), returning caller defaults / `{}` offline. |
| `remote_config.py` | `RemoteConfigClient`: verifies signed server envelopes (`verify_signed_envelope()`), evaluates `feature_flags`, `active_kill_switches`, and `provider_routing`; local denials (`local_denied_features`) and active kill switches always force flags to `False`. |
| `updater.py` | `ComponentUpdaterClient`, `verify_signed_envelope()` (`ENVELOPE_ALG="ed25519"`), and `canonical_json_bytes()`. Verifies Ed25519 envelopes (`ed25519:<b64url>`), enforces compatibility/mandatory/yanked release checks, and records rollback checkpoints (e.g. `~/.hex/rollback.json`). |
| `signaling.py` | `PeerInfo`, `P2PSignalingClient`, and `SignalLink`: handles `/v1/signal` events (`welcome`, `peer_online`, `peer_offline`, `peer_revoked`, `device_revoked`, `p2p_signal`, `kill_switch_updated`, `host_lease_updated`) over a daemon `websockets.sync.client` WebSocket with exponential backoff (`1.0s`–`30.0s`), per-attempt token refresh, and thread-safe `send_turn_ack()`, `send_p2p_signal()`, and `request_failover()`. |
| `team_mesh.py` | `TurnCommitBundle` (whose canonical SHA-256 `compute_digest()` matches [`crates/hex-protocol/src/p2p.rs`](../crates/hex-protocol/README.md) byte-for-byte) and `TeamMeshCoordinator` (`LOCAL`, `TEAM_PRIMARY_HOST`, `TEAM_REPLICA_PEER`), enforcing monotonic `host_epoch` anti-split-brain fencing (`apply_signed_host_lease()`) and monotonic `turn_seq` validation (`accept_turn_commit()`). |

## Current Integrations vs Planned Modules

- **Currently Wired**:
  - [`hex_cli/acp.py`](../hex_cli/README.md): exposes `cloud/auth/{status,register,login,logout}`, `cloud/devices/{list,revoke}`, `cloud/update/check`, `cloud/config/evaluate`, `cloud/signal/{start,stop,status}` and streams `cloud/event` JSON-RPC notifications to [`Hex-App`](../../Hex-App/README.md).
  - [`hex_cli/commands/`](../hex_cli/README.md): `cloud_account.py` (`hex cloud`) and `update.py` (`hex update`).
  - `harness_core/config_loader.py` & `storage/vault_keychain.py`.
- **Planned (per [`BACKEND_PLAN.md`](../BACKEND_PLAN.md) & [`ROADMAP.md`](../ROADMAP.md), not yet implemented)**:
  - `cloud/p2p_transport.py` (QUIC/WebRTC E2EE tunnel + relay fallback)
  - `cloud/sync_engine.py` (personal multi-device `ledger-sync`, `cognition-sync`, `workspace-sync` + `hex sync`)
  - `cloud/registry_client.py` (sparse-index skill/plugin registry client + `hex registry`)
  - `cloud/cloud_tasks.py` (ephemeral cloud worker dispatch)
  - `cloud/telemetry.py` & `cloud/support_client.py` (opt-in telemetry + `hex doctor --bundle` / `hex support`)
  - Engine hooks in `agent/orchestrator.py` and `storage/db.py` (`export_events_since`, `merge_remote_chain`).

## Verification

Tests live in [`tests/cloud/`](../tests/cloud).

```bash
python -m pytest tests/cloud/ -v
```

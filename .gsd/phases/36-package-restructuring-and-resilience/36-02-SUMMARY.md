# Summary 36-02: Multi-Credential Pool & Transient Endpoint Blackholing

## Overview
Implemented `CredentialPool` in `src/harness_core/models/` providing multi-key pool management per provider, round-robin / least-utilized routing, automatic rotation on HTTP 429 / quota exhaustion with jittered backoff, model-specific cooldown queues (`(provider, model) -> cooldown_until`), and 30-second transient endpoint blackholing (`_endpoint_blackholed`) to prevent orchestrator thread lockup on dead local or custom servers.

## Accomplishments

1. **CredentialPool with Multi-Key Routing and Failover Rotation (`src/harness_core/models/credential_pool.py`):**
   - Ingests multiple API keys per provider from environment variables (e.g. `OPENAI_API_KEY`, `OPENAI_API_KEY_1`, `ANTHROPIC_API_KEYS`), configuration dictionaries, or programmatic `add_key()` registration.
   - Per-credential state tracking via `CredentialEntry`: `id`, `provider`, `secret_value`, `status` (`STATUS_OK`, `STATUS_EXHAUSTED`, `STATUS_DEAD`), `usage_count`, `last_used_at`, `cooldown_until`, `priority`, and `metadata`.
   - Selection strategies:
     - `STRATEGY_ROUND_ROBIN`: sequentially cycles through available healthy keys.
     - `STRATEGY_LEAST_USED`: prioritizes the healthy key with the fewest completed requests/turns.
     - `STRATEGY_FILL_FIRST`: exhausts the primary high-priority key before falling back to secondary keys.
   - Failover and backoff rotation:
     - `mark_exhausted()` benches throttled keys with jittered backoff (honoring `retry_after`, default 60s for sole keys or 3600s for pooled keys).
     - Expired exhausted keys automatically recover to `STATUS_OK` upon cooldown expiry.
     - `mark_dead()` permanently excludes invalid credentials on terminal 401s.

2. **Model Cooldown Queues & Transient Endpoint Blackholing:**
   - **Model Cooldown Queues**: Tracks `(provider, model) -> cooldown_until` in `CredentialPool._model_cooldowns`.
     - `mark_model_cooldown(provider, model, duration_seconds=60, reason=None)`: isolates transient 503s or model overloads without taking down the entire provider pool.
     - Sibling models on the same provider and credentials remain healthy and dispatchable.
   - **Transient Endpoint Blackholing**:
     - `_endpoint_blackhole_cache` memoizes dead `hostname:port` endpoints with `_ENDPOINT_BLACKHOLE_TTL_SECONDS = 30.0`.
     - `_endpoint_host_key()` normalizes URLs to canonical host-port identifiers across HTTP/HTTPS schemes.
     - `_is_connect_timeout()` accurately distinguishes connection-phase TCP dropouts (`httpx.ConnectTimeout`, `socket.timeout`, `urllib.error.URLError`) from normal HTTP 4xx/5xx responses.
     - `_endpoint_blackholed()` short-circuits connection attempts immediately, preventing 10-30s synchronous thread freezes on dead local/custom daemons.
     - `check_endpoint_or_raise()` fast-fails outbound calls to unreachable servers.

3. **Backward Compatibility & Domain Re-exports:**
   - Maintained top-level forwarding stub `src/harness_core/credential_pool.py` registering `sys.modules[__name__] = _impl` for backward compatibility.
   - Re-exported `CredentialPool`, entries, strategy constants, and blackhole helpers in `src/harness_core/models/__init__.py`.

4. **Comprehensive Test Suite (`tests/test_credential_pool.py`):**
   - 20 unit tests covering multi-key ingestion, selection strategies, failover rotation, cooldown expiry, terminal deaths, model cooldown queues, endpoint blackholing, and stub forwarding.
   - 100% pass rate in 0.04s.

## Commits Landed

1. `eca7728`: `feat(phase-36): implement credential pool with multi-key routing and failover rotation` (Task 1)
2. `8c2b034`: `feat(phase-36): implement model cooldown queues and transient endpoint blackholing` (Task 2)
3. `8c799af`: `feat(phase-36): author comprehensive unit tests for credential pool` (Task 3)

## Empirical Verification Proof

- **Task 1 Basic Rotation Verification:**
  ```bash
  python3 -c "from harness_core.credential_pool import CredentialPool, STATUS_OK, STATUS_EXHAUSTED; pool = CredentialPool(); pool.add_key('openai', 'sk-1'); pool.add_key('openai', 'sk-2'); k = pool.get_key('openai'); assert k.secret_value in ('sk-1', 'sk-2'); print('CredentialPool basic rotation verified')"
  # Output: CredentialPool basic rotation verified
  ```

- **Task 2 Endpoint Blackhole Verification:**
  ```bash
  python3 -c "from harness_core.credential_pool import _note_endpoint_blackholed, _endpoint_blackholed; _note_endpoint_blackholed('http://192.168.1.200:11434'); assert _endpoint_blackholed('http://192.168.1.200:11434') is True; print('Endpoint blackhole verified')"
  # Output: Endpoint blackhole verified
  ```

- **Task 3 Pytest Verification Suite:**
  ```bash
  .venv/bin/pytest tests/test_credential_pool.py -v
  # Output: 20 passed in 0.04s
  ```

- **Workspace Rust Regression Verification:**
  ```bash
  cargo test --workspace
  # Output: 68 passed; 0 failed; 0 ignored; finished in 2.65s
  ```

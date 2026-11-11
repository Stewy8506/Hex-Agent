# Phase 14 Verification: Antigravity Interactive OAuth, Model Selection & TUI Crash Hardening

**Phase Directory:** `.gsd/phases/14-antigravity-oauth-tui-resilience/`  
**Status:** PASS  
**Auditor:** GSD Independent Verifier  
**Verification Date:** 2026-11-11  

---

## 1. Executive Summary

Phase 14 addresses critical real-world friction identified during live operator first-run onboarding:
1. Validating discovered Antigravity tokens against the Google Cloud companion endpoint before accepting them.
2. Providing interactive browser PKCE OAuth 2.0 sign-in (`webbrowser.open`) or manual token entry when no active session exists.
3. Replacing fictitious hardcoded `gemini-3.5-pro` model references with production model selection (`gemini-2.5-pro` default, `gemini-2.5-flash`, `claude-3-5-sonnet`) across onboarding, doctor diagnostics, model router, and the TUI.
4. Streamlining onboarding by simplifying the search engine label to `"Disabled"` and removing the redundant execution sandbox prompt (detected hardware profile applied silently).
5. Hardening interactive TUI prompt handling against unauthenticated HTTP 401 and provider errors without crashing the REPL, and outputting clear remediation instructions.
6. Enabling transparent HTTP 401 retry with automatic token refresh in `AntigravityModelProvider.generate()` when a refresh token is available in credentials.

All 7 must-haves have been empirically verified with 100% success across the dedicated Phase 14 test suite (7/7 tests passing) and the full repository test suite (422/422 tests passing with zero regressions).

---

## 2. Must-Haves Verification Breakdown

| Must-Have | Description | Status | Evidence |
|-----------|-------------|--------|----------|
| **MH-1** | `_probe_antigravity_token(token)` probes token validity before assuming active | **PASS** | `src/harness_core/onboard.py` L117-135; 3 unit tests in `TestAntigravityOAuthAndTuiResilience` |
| **MH-2** | Interactive browser PKCE OAuth sign-in fallback (`webbrowser.open`) | **PASS** | `src/harness_core/onboard.py` L230-263; `test_onboard_wizard_triggers_browser_oauth_when_unauthenticated` passes |
| **MH-3** | Real production model selection (`gemini-2.5-pro` default) across all modules | **PASS** | Checked `onboard.py`, `doctor.py`, `router.py`, `tui.py`, `cloud_provider.py`, `antigravity_provider.py`; 0 dead models in runtime |
| **MH-4** | Search option label simplified to "Disabled", redundant sandbox prompt removed | **PASS** | `src/harness_core/onboard.py` L318; sandbox silently configured from detected profile |
| **MH-5** | TUI `handle_user_prompt` catches 401/ProviderUnavailableError without crashing | **PASS** | `src/harness_core/tui.py` L1776-1801; `test_tui_handle_user_prompt_catches_auth_error_gracefully` passes |
| **MH-6** | `AntigravityModelProvider.generate()` auto-refreshes token on HTTP 401 | **PASS** | `src/harness_core/providers/antigravity_provider.py` L885-903; `test_antigravity_provider_refreshes_token_on_401` passes |
| **MH-7** | Dedicated suite (7/7) & full repository suite (422/422) pass 100% green | **PASS** | Dedicated suite: 7/7 OK (0.207s); Full test suite: 422/422 OK (43.199s), 0 regressions |

**Must-Haves Score:** 7 / 7 PASSED (100%)

---

## 3. Empirical Evidence & Test Outputs

### 3.1 Dedicated Phase 14 Test Suite

**Command:**
```bash
.venv/bin/python -m unittest tests/test_antigravity_oauth_and_tui_resilience.py -v
```

**Output:**
```
test_antigravity_provider_refreshes_token_on_401 (tests.test_antigravity_oauth_and_tui_resilience.TestAntigravityOAuthAndTuiResilience.test_antigravity_provider_refreshes_token_on_401) ... ok
test_onboard_wizard_antigravity_model_selection (tests.test_antigravity_oauth_and_tui_resilience.TestAntigravityOAuthAndTuiResilience.test_onboard_wizard_antigravity_model_selection) ... ok
test_onboard_wizard_triggers_browser_oauth_when_unauthenticated (tests.test_antigravity_oauth_and_tui_resilience.TestAntigravityOAuthAndTuiResilience.test_onboard_wizard_triggers_browser_oauth_when_unauthenticated) ... ok
test_probe_antigravity_token_empty (tests.test_antigravity_oauth_and_tui_resilience.TestAntigravityOAuthAndTuiResilience.test_probe_antigravity_token_empty) ... ok
test_probe_antigravity_token_failure (tests.test_antigravity_oauth_and_tui_resilience.TestAntigravityOAuthAndTuiResilience.test_probe_antigravity_token_failure) ... ok
test_probe_antigravity_token_success (tests.test_antigravity_oauth_and_tui_resilience.TestAntigravityOAuthAndTuiResilience.test_probe_antigravity_token_success) ... ok
test_tui_handle_user_prompt_catches_auth_error_gracefully (tests.test_antigravity_oauth_and_tui_resilience.TestAntigravityOAuthAndTuiResilience.test_tui_handle_user_prompt_catches_auth_error_gracefully) ... ok

----------------------------------------------------------------------
Ran 7 tests in 0.207s

OK
```

### 3.2 Full Repository Test Suite

**Command:**
```bash
.venv/bin/python -m unittest discover tests
```

**Output:**
```
----------------------------------------------------------------------
Ran 422 tests in 43.199s

OK
```
Zero failures, zero errors, zero regressions across all 422 unit and integration tests.

### 3.3 Code Inspection Findings

1. **Antigravity Token Probing (`src/harness_core/onboard.py`):**
   - Implements `_probe_antigravity_token(token: str) -> bool`.
   - Sends a lightweight POST probe to Google Cloud companion API (`https://cloudaicompanion.googleapis.com/v1internal:loadCodeAssist`).
   - Returns True only on HTTP 200/201 response with valid Bearer credentials, preventing dead tokens from being accepted silently.

2. **Browser PKCE OAuth Sign-In (`src/harness_core/onboard.py`):**
   - If token is absent or invalid, presents interactive choice: Browser Sign-In (default), manual entry, or skip.
   - Generates PKCE challenge and verifier via `AntigravityOAuthManager`.
   - Invokes `webbrowser.open(auth_url)` and prompts user to paste redirect URL or authorization code.
   - Successfully exchanges code for access & refresh tokens.

3. **Production Model Selection (`src/harness_core/`):**
   - Replaced all runtime defaults from `gemini-3.5-pro` to `gemini-2.5-pro`.
   - Onboarding provides arrow-key selection for Antigravity (`gemini-2.5-pro`, `gemini-2.5-flash`, `claude-3-5-sonnet`), Anthropic (`claude-3-5-sonnet`, `claude-3-opus`, `claude-3-5-haiku`), OpenAI (`gpt-4o`, `o1-preview`, `o3-mini`), and Gemini (`gemini-2.5-pro`, `gemini-2.5-flash`).
   - `doctor.py`, `router.py`, `tui.py`, `cloud_provider.py`, and `antigravity_provider.py` all reference real production models.

4. **Simplified Search & Hardware Profiling (`src/harness_core/onboard.py`):**
   - Search option `none` now labeled `"Disabled"`.
   - Redundant execution sandbox prompt eliminated; detected sandbox backend (`restricted_subprocess`, `container`, etc.) and CoW backend (`apfs_clone`, `overlayfs`, etc.) from `HardwareProfile.detect()` are written directly to `config.yaml`.

5. **TUI Error Resilience (`src/harness_core/tui.py`):**
   - `handle_user_prompt` catches `ProviderUnavailableError` and HTTP 401 exceptions.
   - Formats a prominent styled error card using terminal skin colors.
   - Prints actionable remediation steps (`hex auth antigravity --auth-url`, `/model`, `hex onboard`).
   - Returns cleanly to the interactive prompt without terminating the REPL loop.

6. **Automatic Token Refresh (`src/harness_core/providers/antigravity_provider.py`):**
   - In `AntigravityModelProvider.generate()`, when `status_code == 401 and cred.refresh_token` occurs, it automatically invokes `oauth_manager.refresh_access_token(cred)`, updates stored credentials, rebuilds request headers with the refreshed token, and re-executes the API call transparently.

---

## 4. Final Verdict

**Verdict:** PASS  
**Phase:** 14  
**Confidence:** Absolute (backed by full test suite and direct codebase inspection)

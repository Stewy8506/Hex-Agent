# Phase 15 Plan 01 Summary: Live Dynamic Model Discovery Engine

**Execution Date:** 2026-11-11  
**Phase:** 15 (Live Models & Multi-Project Workspaces)  
**Plan:** 15-01 (Live Dynamic Model Discovery Engine)  
**Status:** Completed  
**Repository State:** 434/434 tests passing cleanly (0 failures, 0 regressions)

---

## 1. Objectives & Delivered Capabilities

1. **Live Model Discovery Across Providers:**
   - Implemented dynamic model querying against live provider API endpoints across Google Antigravity, Anthropic, OpenAI, Google Gemini, and Ollama/Local.
   - For Antigravity, queries `POST /v1internal:fetchAvailableModels`, extracts model descriptors and live quota percentages (`{displayName} ({pct}% quota remaining)`).
   - For Anthropic, queries `GET /v1/models`, parses model catalog and sorts by display name/id.
   - For OpenAI, queries `GET /v1/models`, filtering for chat and reasoning families (`gpt-4o`, `gpt-4.5`, `o1`, `o3-mini`, `gpt-4-turbo`).
   - For Google Gemini, queries `GET /v1beta/models`, filters for `generateContent` capabilities, and strips `models/` prefix.
   - For Ollama, queries `GET http://localhost:11434/api/tags`, extracting locally pulled models.

2. **Resilient 3.0-Second Timeout & Offline Fallback Badging:**
   - Standardized all network requests on standard library `urllib.request` with strict `timeout=3.0` and broad exception suppression (`urllib.error.URLError`, `urllib.error.HTTPError`, `TimeoutError`, and `Exception`).
   - Under offline, unreachable, or unauthenticated conditions, gracefully falls back to curated model catalogs with explicit badging: `[offline fallback]`.

3. **Inverted Authenticate-First Onboarding:**
   - Inverted Step 1 of `run_onboard_wizard` in `onboard.py`:
     1. Operator selects primary provider (`antigravity`, `anthropic`, `openai`, `gemini`, `local`).
     2. Authenticates FIRST (probes existing session, prompts for browser PKCE OAuth, or prompts for masked API key via `prompt_secret`).
     3. Immediately queries live models using the authenticated credential via `fetch_available_models(...)`.
     4. Presents discovered models via `select_option`.
     5. Saves selected model and credentials.
   - Safeguarded config directory writing to automatically fallback to workspace `.hex` if `~/.hex` is protected or in confined environments.

4. **Interactive TUI Cockpit `/model` Discovery:**
   - Updated `_cmd_model(arg)` in `tui.py`:
     - If argument is supplied: directly switches model profile/model ID (`/model gemini-2.5-flash`).
     - If argument is empty AND interactive TTY: queries live models for the active cloud provider, prompts operator with `select_option`, updates active routing, and prints switch confirmation (`✓ Active model switched to: {selected_model_id}`).
     - If non-interactive or non-TTY: retains existing routing status card printout.

---

## 2. Tasks & Git Commits

| Task # | Task Description | Files Modified | Commit |
|--------|------------------|----------------|--------|
| Task 1 | Implement live model discovery endpoints and fallback catalog across providers | `src/harness_core/providers/antigravity_provider.py`<br>`src/harness_core/providers/cloud_provider.py` | `52e3d1c feat(providers): implement live model discovery endpoints and fallback catalog` |
| Task 2 | Invert onboarding wizard and integrate interactive /model selector in TUI | `src/harness_core/onboard.py`<br>`src/harness_core/tui.py` | `4bc2e23 feat(onboard): invert onboarding flow and add live tui model discovery` |
| Task 3 | Author unit and integration tests for live model discovery and fallback | `tests/test_live_model_discovery.py`<br>`src/harness_core/tui.py` | `0834031 test(discovery): author unit and integration tests for live model discovery and fallback` |

---

## 3. Verification & Empirical Proof

1. **Live Discovery & Fallback Verification:**
   ```bash
   PYTHONPATH=src .venv/bin/python -c "from harness_core.providers.cloud_provider import fetch_available_models; print(fetch_available_models('openai', 'invalid-key'))"
   # Output: [('gpt-4o', 'gpt-4o [offline fallback]'), ('o1', 'o1 [offline fallback]'), ('o3-mini', 'o3-mini [offline fallback]'), ('gpt-4o-mini', 'gpt-4o-mini [offline fallback]')]
   ```

2. **Inverted Onboarding Verification:**
   ```bash
   PYTHONPATH=src .venv/bin/python -c "from harness_core.onboard import run_onboard_wizard; res = run_onboard_wizard(non_interactive=True, defaults_only=True); assert 'model' in res; print('SUCCESS: model =', res['model'])"
   # Output: SUCCESS: model = gemini-2.5-pro
   ```

3. **Dedicated Test Suite (`tests/test_live_model_discovery.py`):**
   ```bash
   PYTHONPATH=src .venv/bin/python -m unittest tests/test_live_model_discovery.py
   # Output: Ran 12 tests in 0.088s, OK
   ```

4. **Full Regression Suite:**
   ```bash
   PYTHONPATH=src .venv/bin/python -m unittest discover tests
   # Output: Ran 434 tests in 42.252s, OK (0 failures, 0 errors)
   ```

---

## 4. Deviations & Out-of-Scope Discoveries

- **Rule 1 Bug Fix / Robustness:** Guarded `sys.stdin.isatty() and sys.stdout.isatty()` in `_cmd_model` so non-interactive test suites and pipe redirection do not stall on interactive readline calls.
- **Rule 1 Bug Fix / Robustness:** Safely converted `max_usd` in `_cmd_model` to float with try-except fallback when mocking `budget_tracker` in tests.
- **Rule 1 Bug Fix / Robustness:** Added write-probe to `run_onboard_wizard` config directory creation so environments with read-only `~/.hex` cleanly fallback to local `.hex` workspace directory without permission faults.

---

## 5. Next Steps

- Proceed to Plan 15-02: Isolated Project Workspaces & Multi-Repo Session Context.

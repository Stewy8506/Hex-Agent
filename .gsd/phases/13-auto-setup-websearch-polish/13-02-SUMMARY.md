# Plan 13-02 Summary: Interactive Setup Wizard, Startup Interceptor & Hardcoded Mock Purge

**Completed:** 2026-11-11  
**Status:** Success (7/7 plan tests green, zero regressions)

## Accomplishments
1. **Interactive 4-Step Arrow-Key Setup Wizard (`src/harness_core/onboard.py`):**
   - Step 1: Model Provider & Authentication (`select_option` radio selector across Google Antigravity OAuth, Claude, OpenAI, Gemini, and Local MLX/Ollama with masked `prompt_secret`).
   - Step 2: Web Search Provider Selection (`select_option` for DuckDuckGo free default, Tavily Search, Brave Search, and Air-gapped).
   - Step 3: Remote Messaging Gateways (`select_option` for Telegram Bot token/users, Discord Bot token/webhooks, Both, or Skip).
   - Step 4: Machine Profile & Isolation Confirmation (displays detected specs and isolation modes).
   - Saves `credentials.env` (0600) and `config.yaml`, and automatically hydrates running process via `load_hex_environment(override=True)`.

2. **First-Run Startup Interceptor (`src/harness_core/cli.py`):**
   - Automatically intercepts unconfigured execution on bare `hex` or `hex chat`/`tui`.
   - Welcomes the operator and automatically launches the interactive setup wizard instead of dropping into an unconfigured/broken chat.
   - Proceeds directly into the interactive TUI cockpit once configured.

3. **Purge of Hardcoded Dummy Data Across Harness:**
   - `tui.py`: Replaced hardcoded `v1.3.0` with dynamic `importlib.metadata.version("hex-ai")`.
   - `tui.py`: Replaced hardcoded `M4 MLX` with dynamic `HardwareProfile.detect()`.
   - `tui.py`: Added `web_search` to tool category mapping and fallback tools.
   - `orchestrator.py`: Replaced hardcoded `wrk_m4_air_local` with dynamic worker ID and machine specs.
   - `cloud_provider.py`: Purged dummy `"mock-key"` fallbacks, raising structured `ProviderUnavailableError` when credentials are missing.

4. **Testing:**
   - 7/7 unit and integration tests passing in `tests/test_auto_setup_and_polish.py`.

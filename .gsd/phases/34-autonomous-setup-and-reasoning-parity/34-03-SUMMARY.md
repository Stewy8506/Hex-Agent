# Plan 34-03 Summary: Setup Ergonomics, Curated Provider Tiers & First-Task Verification

**Phase:** 34  
**Plan:** 34-03 (Wave 3)  
**Status:** COMPLETE & VERIFIED  
**Date:** 2026-11-16  

## Accomplishments
1. **Setup Ergonomics Flags (`--quick`, `--blank-slate`, `--reset`):**
   - `--quick`: Skips prompting for sections that already have valid credentials/configurations (model, search, messaging) and exits cleanly when all core components are intact.
   - `--blank-slate`: Configures a lightweight agent profile with minimal toolsets, deterministic compression, 0 telemetry, `max_turns: 90`, and disabled external web search/bot integrations.
   - `--reset`: Cleanly removes existing `.hex/config.yaml` and `.hex/credentials.env` to allow fresh bootstrapping.
2. **4 Curated Provider Tiers:**
   - Replaced flat 26-item menu cognitive overload with an intuitive 2-level hierarchy:
     1. Recommended & Included (Antigravity Included Quota, Claude 3.7 Sonnet, OpenAI GPT-4o, Gemini 2.5)
     2. Unified Gateways (OpenRouter, Vercel AI Gateway)
     3. Local Offline & Zero-Egress (Ollama, LM Studio, Local LLM / MLX, Custom Endpoint)
     4. Enterprise Cloud & High-Speed (DeepSeek, Groq, Cerebras, Bedrock, Vertex, Azure, etc.)
     5. Browse All 26 Providers (Flat List)
   - Automatically offers `[0] Keep Currently Configured: <provider> (<model>)` when prior credentials exist.
3. **Guided First-Task Verification Beat (`run_first_task_verification`):**
   - Creates an isolated ephemeral CoW workspace snapshot in `active_workspaces/ws_first_task`.
   - Probes host toolchains and devtools (`python3`, `git`, `docker`, `clang`, `rustc`, `node`).
   - Generates a full markdown diagnostic report in `~/hex-first-task/diagnostics.md`.
   - Indexes and archives the diagnostic blob with SHA-256 in CAS (`cas/`, `cas_artifacts/`).
   - Transitions task from `QUEUED` -> `RUNNING` -> `VERIFYING` -> `SUCCEEDED` in `ledger.db` and journals the artifact with full cryptographic proof.
4. **Verification Evidence:**
   - 5/5 unit tests passing in `tests/client/test_onboarding_ergonomics.py`.
   - 5/5 unit tests passing in `tests/client/test_auto_setup.py`.
   - 40/40 unit tests passing in `tests/core/test_reasoning_effort.py`.

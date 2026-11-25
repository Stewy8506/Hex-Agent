# `capabilities` (`harness_core.capabilities`)

**Agent tool integrations and automation capabilities: browser, vision, voice, full-text task search, web search, Git/GitHub workflows, proof bundles, project recipes, webhooks, and mobile chat gateways.**

`capabilities/` maps to `harness_core.capabilities` (and `capabilities/gateways/` maps to `harness_core.capabilities.gateways`) in [`pyproject.toml`](../pyproject.toml).

## Modules

| Path | Purpose |
| :--- | :--- |
| `__init__.py` | Lazy exports across all 11 capability submodules. |
| `browser.py` | `BrowserManager` / `HeadlessBrowserSession`: HTTP(S) & workspace-confined `file://` fetcher emitting semantic `@eN` accessibility trees (`_AriaHTMLParser`). Does not execute JS or render screenshots (`click`/`type`/`screenshot`/`evaluate` return unsupported errors). |
| `vision.py` | `VisionEngine` & `get_image_metadata()` (PNG/JPEG/GIF dimensions): base64 image inspection routed through `tier2_cloud_reasoner` (`VisionEngine`). Supports `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.svg`, `.bmp`. |
| `voice.py` | `VoiceEngine`, `VoiceConfig`, `probe_audio_hardware()`, `clean_text_for_speech()`, platform TTS (`MacOSTTSProvider` via `say`, `LinuxTTSProvider` via `espeak-ng`/`espeak`/`festival`, `MockTTSProvider`), and STT interfaces (`NativeSTTProvider` raises `VoiceUnsupportedError`; `MockSTTProvider` for tests). |
| `search.py` | `TaskSearchIndex` & `sanitize_fts5_query()`: SQLite FTS5 (`task_search_fts`) BM25 search across task goals, tool calls, and logs. |
| `web_search.py` | `execute_web_search()`: multi-provider search (`tavily`, `brave`, and zero-key `duckduckgo` HTML + Instant Answer fallback). |
| `git_autopilot.py` | `GitAutopilot` (`create_task_branch`, `commit_task_changes`, `generate_pr_summary`, `create_pull_request`) over `SubprocessGitHubTransport` (`gh` CLI / REST API) or `MockGitHubTransport`. |
| `github_action.py` | `GitHubActionRunner`: CI trigger parser (`issue_comment`, `pull_request`, `issues`, `workflow_dispatch`; `@hex-bot`/`/hex`/`@hex` for `fix`/`run`/`explain`/`review`) executing through `HarnessOrchestrator`. |
| `proof.py` | `VerificationAndProofService`: computes tree SHAs, unified diffs, Graphify blast radius, test results, and persisted `LedgerStore` proof bundles; also hosts `Harness2ControlledAdapter` and `PROTECTED_POLICY_PATHS`. |
| `recipes.py` | `detect_recipe()`: auto-detects install/build/test/start commands across Node/TS (`pnpm`, `yarn`, `bun`, `npm`), Python (`uv`, `poetry`, `pipenv`, Django, `pytest`, `unittest`), Rust (`cargo`), Go, `Makefile`, and Docker Compose. |
| `webhooks.py` | `WebhookNotifier`: sends redacted Telegram Bot API and Discord Webhook notifications + approval links (`format_telegram_approval`, `format_discord_approval`, `format_decision_broadcast`). |
| `gateways/` | Bidirectional **Telegram** (`telegram_bot.py`: `getUpdates` polling, slash commands, inline callbacks), send-only/callback **Discord** (`discord_bot.py`: `INGRESS_MODE = "send_only"`, ActionRow buttons, interaction/reaction handlers), `MobileApprovalBridge` (`reaction_listener.py`), and `GatewayManager` (`manager.py`). |

## Integration & Tests

- Lazily invoked by `harness_core/gateway.py` (`browser`, `image_inspect`, `web_search`, `run_tests`), `agent/orchestrator.py` (proofs, Harness2, webhooks, recipes), `storage/db.py` (`TaskSearchIndex`), `lsp/registry.py` (`detect_recipe`), `runtime/scheduler.py` (`WebhookNotifier`), and `hex_cli/` (`acp`, `cli`, `tui`, `server`, `doctor`).
- Tests live in [`tests/capabilities/`](../tests/capabilities).

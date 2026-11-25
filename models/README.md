# `models` (`harness_core.models`)

**Multi-key credential pooling, HTTP 429 cooldowns/blackholing, model context-window discovery, local inference socket probing, and core task/policy re-exports.**

`models/` maps to `harness_core.models` in [`pyproject.toml`](../pyproject.toml).

## Modules

| File | Purpose |
| :--- | :--- |
| `__init__.py` | Lazy loader exposing `credential_pool`, `model_metadata`, and core domain models (`ExecutionMode`, `TaskState`, `VALID_TRANSITIONS`, `RiskClass`, `PolicyDecisionType`, `TaskBudget`, `PlanNode`, `TaskSubmitRequest`, `utcnow_iso`) forwarded from `harness_core.core.models`. |
| `credential_pool.py` | Thread-safe `CredentialPool` and `CredentialEntry` (`STATUS_OK`, `STATUS_EXHAUSTED`, `STATUS_DEAD`) with `round_robin`, `least_used`, and `fill_first` key selection, `load_from_env()` / `load_from_config()`, jittered HTTP 429 cooldowns (`DEFAULT_SOLE_KEY_COOLDOWN = 60.0`, `DEFAULT_POOLED_KEY_COOLDOWN = 3600.0`), per-`(provider, model)` cooldown queues, and 30-second endpoint blackholing (`check_endpoint_or_raise`, `_note_if_connect_timeout`). |
| `model_metadata.py` | Context-window resolution (`get_model_context_length`, `format_context_tokens`) using `DEFAULT_CONTEXT_LENGTHS` longest-prefix catalog, `models.dev` (`https://models.dev/api.json`) 24h ETag disk cache (`~/.hex/models_dev_cache.json`), and live local daemon socket probes (`probe_local_server`) for Ollama, LM Studio, llama.cpp, and vLLM. |

## Integration & Tests

- `get_model_context_length()` feeds `providers/cloud_provider.py`, `providers/local_provider.py`, `hex_cli/tui_launcher.py`, and `hex_cli/acp.py` (`usage.contextLimit`).
- `CredentialPool` endpoint-blackholing helpers are wired into `model_metadata.py`; full pool behavior is verified in [`tests/models/`](../tests/models).

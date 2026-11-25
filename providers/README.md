# `providers` (`harness_core.providers`)

**Pluggable AI model provider registry, transports, Antigravity OAuth integration, cost tracking, and local offline inference adapters.**

`providers/` maps to `harness_core.providers` in [`pyproject.toml`](../pyproject.toml). It defines a single unified `ModelProvider` contract across **26** cloud, enterprise, aggregator, and local offline LLM backends.

## Modules

| File | Purpose |
| :--- | :--- |
| `__init__.py` | Re-exports `ModelProvider`, errors (`ModelProviderError`, `PolicyBlockedError`, `BudgetExceededError`, `ProviderUnavailableError`), `CloudModelProvider`, `LocalMLXModelProvider`, `Tier1LogCompressor`, and registry helpers. |
| `base.py` | Abstract `ModelProvider` ABC and typed contracts (`ModelUsage`, `ModelResponse`, `ProviderCapabilities`) plus the provider exception hierarchy. |
| `registry.py` | Single source of truth (`PROVIDER_SPECS`, `PROVIDER_REGISTRY`) for all **26** providers: IDs, categories, transports (`openai_chat`, `anthropic_messages`, `gemini_rest`, `antigravity`, `local`), default base URLs, env-var priority chains, aliases (`claude`, `qwen`, `dashscope`, `google-antigravity`), and curated models. |
| `cloud_provider.py` | `CloudProviderType` enum, `CloudModelProvider` (dispatches OpenAI Chat, Anthropic Messages, Gemini REST, and Antigravity), live `fetch_available_models()`, `ModelPricing` / `calculate_cost_usd()`, `TaskBudgetTracker`, `sensitive`/`secret` privacy egress blocking, secret redaction, prompt caching, and thinking-block signature hygiene. |
| `antigravity_provider.py` | `AntigravityOAuthManager` (PKCE OAuth2 login, 5-minute preemptive refresh, multi-source credential discovery across env vars, `~/.hex/antigravity_auth.json`, IDE `state.vscdb`, `~/.gemini/oauth_creds.json`, `gcloud` ADC, and macOS Keychain), `AntigravityModelProvider` (Cloud Code Assist `loadCodeAssist`, `fetchAvailableModels`, `streamGenerateContent?alt=sse`), and `AntigravityQuotaSnapshot` / `ModelQuotaWindow`. |
| `local_provider.py` | `LocalModelProvider` (probes Ollama `/api/version` & `/api/generate`, Apple Silicon `mlx_lm`, or deterministic fallback), `Tier1LogCompressor` (pytest/unittest/traceback failure extraction + secret scrubbing), and `FailureRecord`. |
| `mlx_provider.py` | Compatibility `LocalMLXModelProvider(LocalModelProvider)` adding Apple Silicon unified-memory probing (`sysctl -n hw.memsize`). |

## Supported Provider Categories (26 Total)

- **Featured Frontier (7)**: `antigravity`, `openrouter`, `anthropic`, `openai`, `gemini`, `deepseek`, `xai`
- **High-Speed Open-Weight Cloud (10)**: `groq`, `mistral`, `together`, `cerebras`, `fireworks`, `nvidia`, `alibaba`, `minimax`, `kimi`, `zai`
- **Aggregators (2)**: `ai-gateway`, `huggingface`
- **Enterprise Hyperscalers (3)**: `azure`, `bedrock`, `vertex`
- **Local / Custom (4)**: `ollama`, `lmstudio`, `local`, `custom`

## Integration & Tests

- Consumed by `agent/router.py` (`TwoTierModelRouter`), `hex_cli/onboard.py`, `hex_cli/auto_setup.py`, `hex_cli/acp.py`, `hex_cli/tui.py`, and `hex_cli/commands/antigravity.py`.
- Compatibility shims in `harness_core/{cloud_provider,local_provider,mlx_provider}.py` re-export these classes.
- Tests live in [`tests/providers/`](../tests/providers).

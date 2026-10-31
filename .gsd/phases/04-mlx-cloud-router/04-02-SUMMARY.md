# Plan 04-02: Cloud API Connectors, Per-Task Budget Ceilings, Privacy Gating & Router Integration — Summary

**Execution Date:** 2026-10-31  
**Phase:** 04 (Real Apple Silicon MLX Runner & Cloud API Connectors)  
**Plan:** 04-02 (Wave 2)  
**Status:** Complete  

---

## 1. Summary of Completed Work

### Task 1: CloudModelProvider with Multi-Backend Dispatch, Budget Ceilings & Privacy Gating
- Implemented `CloudModelProvider` in `src/harness_core/providers/cloud_provider.py` conforming to the unified `ModelProvider` contract.
- Added support for three frontier backends:
  - **Anthropic Claude** (`messages` API with `x-api-key`, `anthropic-version`, and system prompts).
  - **OpenAI** (`chat/completions` API with bearer auth and role messages).
  - **Google Gemini** (`generateContent` API with URL key authentication, parts structure, and generationConfig).
- Created standard token pricing lookup table `ModelPricing` and helper `calculate_cost_usd` covering Claude 3.5 Sonnet ($3/$15 per 1M), GPT-4o ($2.50/$10 per 1M), Gemini 1.5 Pro ($1.25/$5 per 1M), and Gemini 3.5 Pro ($1.50/$6 per 1M).
- Implemented `TaskBudgetTracker` with hard ceilings for maximum task USD cost, maximum output tokens, and maximum request count, raising `BudgetExceededError`.
- Implemented strict privacy gating blocking `sensitive` and `secret` data classifications immediately before any outbound dispatch, raising `PolicyBlockedError`.
- Integrated outbound secret scrubbing via `redact_secrets` before serialization to guarantee zero credential egress.
- Created `src/harness_core/cloud_provider.py` re-export shim and updated `src/harness_core/providers/__init__.py`.

### Task 2: TwoTierModelRouter Integration & Execution Telemetry
- Enhanced `RouteDecision` in `src/harness_core/router.py` to record latency (`latency_ms`), monetary cost (`cost_usd`), token metrics (`input_tokens`, `output_tokens`), `compression_ratio`, and raw `provider_metadata` while retaining full backward compatibility.
- Updated `TwoTierModelRouter` constructor to accept and manage pluggable `local_provider` (default: `LocalMLXModelProvider`) and `cloud_provider` (default: `CloudModelProvider`).
- Integrated `compress_verbose_log` with `Tier1LogCompressor`, preserving source file locations (`File "...", line ...` and `LOCATION: ...`), line numbers, traceback frames, and assertion errors.
- Implemented `execute_routed_request` orchestrating route evaluation, tier dispatch, local $0 execution vs. governed cloud escalation, strict privacy boundary defense, and comprehensive telemetry recording.

### Task 3: Cloud Provider Contract Tests & Real 500-Line Pytest Benchmark
- Extended `tests/test_model_providers.py` with:
  - Unit tests for `TaskBudgetTracker` (request limits, cost ceilings, token caps, pricing math).
  - Multi-backend dispatch contract tests verifying payload structures, headers, and responses for Anthropic, OpenAI, and Gemini backends.
  - Privacy gating tests proving zero network calls for sensitive/secret data classifications.
  - Outbound credential scrubbing tests proving live keys and passwords are systematically replaced with `[REDACTED_SECRET]`.
- Authored comprehensive benchmark suite in `tests/test_two_tier_compression_benchmark.py`:
  - Synthetic realistic 500-line pytest failure log generator (>2,000 raw tokens, 45 passing tests, 3 distinct failure tracebacks, embedded live secret).
  - Validated >70% token savings (>75% achieved) with 100% preservation of all 3 failing test files (`test_tax.py`, `test_tokens.py`, `test_checkout.py`), line numbers (`42`, `108`, `215`), and assertion messages (`120.0 != 100.0`, `Token expired != Token valid`, `5 != 4`).
  - Proved live credentials (`sk-test-live-key1234567890`) are scrubbed before reaching any tier.
  - Validated two-tier privacy routing: sensitive tasks are executed 100% locally on M4 MLX at $0 marginal cost with zero cloud egress; internal tasks are routed to Tier-2 with compressed context and full telemetry.

---

## 2. Verification Evidence

1. **Task 1 Verification:**
   ```bash
   .venv/bin/python -c "from harness_core.providers.cloud_provider import CloudModelProvider; from harness_core.providers.base import PolicyBlockedError; p = CloudModelProvider(provider_type='gemini'); print('Cloud provider initialized:', p.provider_id, p.model_id)"
   # Output: Cloud provider initialized: cloud_frontier gemini-3.5-pro
   ```

2. **Task 2 Verification:**
   ```bash
   .venv/bin/python -c "from harness_core.router import TwoTierModelRouter; r = TwoTierModelRouter(); dec = r.route_request(task_class='bug_fix', risk_class='R2', data_classification='internal', raw_context='Failed test_auth.py:20'); print('Router tier:', dec.tier, 'provider:', dec.provider, 'to_dict:', dec.to_dict()['tier'])"
   # Output: Router tier: tier2_cloud_reasoner provider: cloud_frontier to_dict: tier2_cloud_reasoner
   ```

3. **Task 3 Verification:**
   ```bash
   .venv/bin/python -m unittest tests/test_model_providers.py tests/test_two_tier_compression_benchmark.py
   # Output: Ran 29 tests in 0.016s -> OK
   ```

4. **Full Test Suite Regression Run:**
   ```bash
   .venv/bin/python -m unittest discover -s tests -p "test_*.py"
   # Output: Ran 121 tests in 3.562s -> OK (100% passing, 0 failures, 0 errors)
   ```

---

## 3. Atomic Commits

- Task 1: `0c7d510 feat(phase-04): implement CloudModelProvider with budget tracking and privacy gating`
- Task 2: `b63ca14 feat(phase-04): integrate local MLX and cloud providers into TwoTierModelRouter`
- Task 3: `f3f38cc feat(phase-04): author cloud provider tests and live 500-line pytest compression benchmark`

---

## 4. Deviations & Blockers

None. All must-haves and success criteria achieved without blockers.

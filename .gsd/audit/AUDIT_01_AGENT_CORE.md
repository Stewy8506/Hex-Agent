# FIT & FINISH AUDIT REPORT: DOMAIN 1
# Agent Core, Multi-Turn ReAct Loop & Prompt Engine

**Target Codebase:** HEX (`harness_core/react_loop.py`, `harness_core/context_compressor.py`, `harness_core/router.py`, `harness_core/orchestrator.py`, `providers/*`)\
**Reference Benchmark:** Hermes Agent (`benchmarks/hermes-agent/agent/`)  
**Audit Date:** 2026-11-14  
**Audit Scope:** Multi-Turn Reasoning Loop Robustness, Context Compaction & Token Budget Management, Thinking Block Retention & Provider Payloads, Code Quality & Exception Safety.

---

## Executive Summary & Scorecard

HEX implements a substantial, highly structured foundational architecture for autonomous agent execution. In particular, `context_compressor.py` contains sophisticated implementations of 4-phase context compaction, boundary alignment, deterministic and model-assisted summaries, and prompt cache prefix stabilization. However, when contrasted against Hermes Agent's battle-tested production implementation across multi-turn tool loops, streaming dispatch, and provider-specific reasoning protocols, critical gaps emerge in native tool-call contracts, stream event dispatch, thinking block retention/signature suppression, and loop exception recovery.

| Evaluation Area | Hermes Reference Standard | HEX Current State | Parity Score | Critical Gap / Risk Level |
|---|---|---|---|---|
| **1. Multi-Turn ReAct Loop Robustness** | Native tool-call grouping, provider call-id normalization, mixed-batch validation, persist-before-execute durability invariant, interruptible streaming with watchdog. | Regex/XML-first parsing, plaintext prompt serialization in orchestrator, sequential blocking execution, no live streaming event dispatch. | **62%** | **HIGH**: Model caller messages flattened to plaintext string; native provider tool calling bypassed; no streaming event dispatch. |
| **2. Context Compaction & Token Accounting** | Dual compression architecture (85% Gateway Hygiene + 50% in-loop Compactor), multi-pass stale tool pruning, media eviction, shadow checkpoint pruning, skill marker reinjection. | 4-phase compaction pipeline, stale tail tool demotion with elision, forward/backward boundary alignment, media eviction, prefix cache locking. | **84%** | **MEDIUM**: Lacks pre-turn gateway hygiene safety net and older summary checkpoint deduplication (stacked compaction summaries). |
| **3. Thinking Block Retention & Provider Payloads** | Claude Opus 4.5+/Sonnet 4.6+/Haiku 5.5+ thinking retention policy, signed replay routes, durable SHA-256 signature rejection suppression, OpenAI reasoning tokens, Gemini thinking budget. | Listed model IDs in registry; no thinking block retention, no signature rejection recovery, no OpenAI `reasoning_effort` parameter, no Gemini `thinkingConfig`. | **38%** | **CRITICAL**: Replaying signed thinking blocks or handling Anthropic HTTP 400 signature invalidations causes fatal session abortion. |
| **4. Code Quality, Exception Safety & Performance** | Comprehensive classified error hierarchy, interruptible backoff with jitter, rate-limit reset epoch parsing, transport credential auto-repair, full async streaming. | Model caller exceptions immediately abort ReAct loop; sync execution dispatched via `run_in_executor`; tool execution blocks thread. | **58%** | **HIGH**: Zero retry or backoff inside ReAct loop; transient 429/503 or network blips immediately fail the entire session. |

**Overall Domain 1 Parity Score:** **60.5%**

---

## Detailed Evaluation

### 1. Multi-Turn Reasoning Loop Robustness

#### 1.1 Tool-Call Grouping & Execution Invariants
- **Hermes Baseline (`benchmarks/hermes-agent/agent/turn_tool_round.py:49-140`):**
  - **Durability Invariant (Persist-Before-Execute):** Before any tool executes, the assistant turn containing tool calls is incrementally persisted to the session database (`agent._flush_messages_to_session_db`, lines 130–140). If a destructive tool restarts the runtime or crashes the process, session resume immediately sees the executed block.
  - **Tool Call Batch Expansion & Normalization (`benchmarks/hermes-agent/agent/tool_call_batches.py:14-60`):** Tool calls undergo provider ID normalization (`normalize_provider_tool_call_ids`), batch expansion, and deduplication (`agent._deduplicate_tool_calls`, lines 97–103).
  - **Mixed-Batch Validation (`benchmarks/hermes-agent/agent/turn_tool_round.py:104-129`):** When a model emits both valid and invalid tool calls in a single turn, Hermes keeps *every* emitted call in the assistant message to maintain wire schema integrity, emits synthetic error `role: "tool"` messages for the invalid ones, and executes only the valid tools.
- **HEX Implementation (`harness_core/react_loop.py:201-450`, `736-883`):**
  - **Parsing Versatility (`react_loop.py:201-449`):** `parse_tool_calls` provides fallback parsing across 6 formats: native dicts with `tool_calls`, XML tags (`<tool_call>`), markdown JSON blocks (` ```json `), raw top-level JSON, embedded JSON objects via brace counting (`_find_json_objects`), and text outside tags as `thought`.
  - **Message Representation Deviation (`react_loop.py:829-873`):** Tool outputs are appended into `self.messages` not as `role: "tool"` with matching `tool_call_id`, but as `role: "user"` with XML wrapper strings:
    ```python
    # harness_core/react_loop.py:866-873
    self.messages.append(
        {
            "role": "user",
            "content": f"<observation tool=\"{tool_res.name}\">\n{obs_payload}\n</observation>",
            "tool_name": tool_res.name,
            "call_id": tool_res.call_id,
        }
    )
    ```
    This creates an immediate mismatch for native OpenAI or Anthropic tool formats, where consecutive user messages or user-role tool results violate API protocol rules.
  - **Plaintext Prompt Serialization in Orchestrator (`harness_core/orchestrator.py:468-484`):**
    When running via `orchestrator.py::run_react_loop`, `_react_model_caller` serializes all chat history into a single string using `ReActLoop._format_messages_to_prompt`:
    ```python
    prompt_str = ReActLoop._format_messages_to_prompt(messages)
    decision, model_resp = self.router.execute_routed_request(
        prompt=prompt_str,
        task_class="agent_loop",
        ...
    )
    ```
    This completely collapses multi-turn chat message hierarchies and forces the LLM to rely entirely on string pattern matching (`<tool_call>`) rather than native API tool calling schemas.
  - **No Durability Guarantee:** If a tool call triggers a crash or system shutdown, the turn is only sealed in the ledger if the ledger is configured, but there is no atomic persist-before-execute guarantee for conversational messages.

#### 1.2 Turn Limits, Runaway Detection & Stop Gates
- **Hermes Baseline (`benchmarks/hermes-agent/agent/turn_stop_gates.py`, `turn_iteration_prep.py:119-140`):**
  - Hermes checks `_neutralise_replay_echo_ghosts` to detect repetitive cycling or hallucinated replay echoes.
  - Explicit handling of finish reasons: `stop`, `tool_calls`, `length` (context truncation recovery via `truncated_tool_call_retries`), and `content_filter`.
  - Iteration budget warning injection: warns model when reaching the final turns of its iteration cap before hard termination.
- **HEX Implementation (`harness_core/react_loop.py:785-827`, `884-887`):**
  - Evaluates `<finish>...</finish>` tags, case-insensitive `<finish>` markers, or an empty `tool_calls` list as termination (`termination_reason="finish"` or `"no_tool_calls"`).
  - Hard limit at `config.max_turns` (default 30).
  - **Gaps:** Does not inspect API finish reasons (e.g. `finish_reason == "length"`). Truncated JSON outputs are treated as parse errors or plaintext thoughts, leading to unexpected premature termination or malformed execution loops.

#### 1.3 Streaming Event Dispatch
- **Hermes Baseline (`benchmarks/hermes-agent/agent/turn_api_call.py:46-105`, `auxiliary_stream_watchdog.py`):**
  - Full async token streaming via `_interruptible_streaming_api_call`.
  - Emits real-time tokens to streaming consumers (TUI, WebSocket, CLI).
  - Stalled chunk watchdog detects dead sockets and token droughts.
  - Stores partial streamed assistant text (`_current_streamed_assistant_text`) to allow seamless recovery on connection drop.
- **HEX Implementation (`harness_core/react_loop.py:455-475`, `897-907`):**
  - Model caller invocation is strictly blocking and synchronous (`self.model_caller(messages)`).
  - `run_async` (`react_loop.py:897-907`) is a thin wrapper calling `loop.run_in_executor(None, self.run, ...)`.
  - There is no generator or event dispatch mechanism inside `ReActLoop` to emit token chunks, tool start/end events, or thinking deltas to the UI/TUI during execution.

---

### 2. Context Compaction & Token Budget Management

#### 2.1 4-Phase Context Compaction Architecture
- **Hermes Baseline (`benchmarks/hermes-agent/agent/context_compressor.py`, `turn_context_compaction.py`, `gateway/run_turn_hygiene.py`):**
  - **Dual Compression Architecture:** Operates at two separate boundaries:
    1. *Gateway Session Hygiene (85% threshold):* Runs before agent invocation to protect against overnight or multi-turn conversational bloat.
    2. *Agent ContextCompressor (50% threshold):* In-loop proactive compaction triggered by token accounting.
  - Multi-pass compression: Tool output truncation, media eviction, summary template generation with verified user provenance, skill marker reinjection (`_reinject_pruned_skill_markers`), and shadow summary elimination (`drop_shadowed_checkpoints`).
- **HEX Implementation (`harness_core/context_compressor.py:210-1124`):**
  - HEX implements an explicit 4-phase pipeline in `context_compressor.py`:
    - **Phase 1: Tool Demotion (`demote_stale_tail_tools`, lines 210–328):** Scans backwards from the tail, identifies tool rounds, protects recent `keep_tool_rounds` (default 4), and demotes bulky tool outputs exceeding `min_demote_chars` (default 1000) using head/tail preservation (`elide_middle`) and `HEX_COMPRESSION_MARKER_PREFIX`. Tool errors are protected from demotion (`_is_tool_error`, line 286).
    - **Phase 2: Atomic Boundary Protection (`find_tool_call_groups`, `align_boundary_forward`, `align_boundary_backward`, `validate_atomic_boundaries`, `sanitize_tool_pairs`, lines 335–607):** Computes atomic spans for tool calls and matching results for both structured and XML formats. `align_boundary_forward` and `align_boundary_backward` ensure a compaction cut never splits a tool call from its result.
    - **Phase 3: Middle-Turn Summarizer (`ContextCompactor`, lines 717–1124):** Generates either a model-assisted summary (`_generate_model_summary`, enforcing an epistemic distinction between verified facts and hypotheses) or a structured deterministic fallback summary (`_generate_deterministic_summary`). Features in-flight task restatement (`_INFLIGHT_TASK_REPLAY_HEADER`, lines 1060–1095) so active user goals are restated after the compaction boundary.
    - **Phase 4: Prompt-Cache Prefix Stabilization (`stabilize_prompt_cache_prefix`, `apply_cache_control_breakpoints`, lines 1130–1269):** Enforces canonical index 0 system prompt and applies up to 4 cache breakpoints.
  - **Anti-Thrashing Safety Check (`context_compressor.py:1101-1110`):** If compacted tokens are greater than or equal to original tokens, the compactor immediately reverts to the uncompacted messages.
  - **Identified Gaps in HEX:**
    1. *No Gateway Session Hygiene:* There is no pre-agent hygiene check before the ReAct loop starts. If an initial session already exceeds the context window, it will fail at the first model call.
    2. *Shadow Summary Stacking:* When compaction fires multiple times across a long session, earlier summary messages (`SUMMARY_HEADER`) are not pruned or consolidated, leading to accumulated summary blocks in history.

#### 2.2 Atomic Tool-Pair Preservation
- **Hermes Baseline (`benchmarks/hermes-agent/agent/context_compressor.py:4492-4550`):**
  - Resolves tool call ID variants across OpenAI, Anthropic, Bedrock, and LiteLLM wrappers (`_tool_call_id_variants`).
  - Strips orphan tool results and unresponded tool calls while keeping matched pairs intact.
- **HEX Implementation (`harness_core/context_compressor.py:335-607`):**
  - `find_tool_call_groups` parses both structured OpenAI-style (`tool_calls` list with `id`/`call_id`) and XML `<tool_call>` / `<observation>` pairs.
  - `sanitize_tool_pairs` (lines 545–607) strips orphan tool messages lacking a preceding assistant call, and strips unresolved tool calls from assistant messages if no matching tool response exists.
  - `validate_atomic_boundaries` (lines 449–543) provides an empirical verification predicate ensuring zero broken pairs.
  - **Parity Rating:** **92%** — Outstanding implementation; closely matches Hermes logic.

#### 2.3 Stale Output Pruning & Historical Media Eviction
- **Hermes Baseline (`benchmarks/hermes-agent/agent/context_compressor.py:1427-1454`):**
  - Replaces historical base64 image blocks prior to the tail with placeholder strings (`_replace_image_parts`, `_strip_images_from_tool_msg`) to prevent HTTP 413 (Payload Too Large).
- **HEX Implementation (`harness_core/context_compressor.py:610-659`):**
  - `strip_historical_media` replaces base64 images and `image_url` dictionaries prior to `tail_start` with `[historical image omitted at compaction]`.
  - Supports both content part arrays (`[{"type": "image_url", ...}]`) and inline data URIs in string content.
  - **Parity Rating:** **95%** — Full parity with Hermes.

#### 2.4 Prefix Cache Locking
- **Hermes Baseline (`benchmarks/hermes-agent/agent/prompt_caching.py:290-380`):**
  - Dynamically builds prompt cache plans using Anthropic `cache_control: {"type": "ephemeral"}`.
  - Distinguishes native Anthropic vs proxy/LiteLLM endpoints (`tool_part_markers`).
  - Automatically selects legal transaction endpoints (`_completed_transaction_endpoint_indexes`) so markers never land on partial turns.
  - Manages auto-TTL (`auto_cache_ttl_for_source`: `1h` for human, `5m` for machine).
- **HEX Implementation (`harness_core/context_compressor.py:1130-1269`):**
  - `stabilize_prompt_cache_prefix` strips duplicate system messages and pins canonical system instructions to index 0.
  - `apply_cache_control_breakpoints` injects up to 4 breakpoints: (1) last tool definition, (2) system prompt, (3) latest context summary, (4) second-to-last turn in active tail.
  - **Gap in HEX Integration (`providers/cloud_provider.py:752-785`):**
    While `apply_cache_control_breakpoints` is defined in `context_compressor.py`, `cloud_provider.py` does not call it. Instead, `cloud_provider.py` uses a static, hardcoded check:
    ```python
    # providers/cloud_provider.py:752-758
    if enable_prompt_caching and cacheable_prefix:
        user_content = [
            {"type": "text", "text": cacheable_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": clean_prompt},
        ]
    ```
    This misses dynamic multi-turn caching on middle compaction summaries and recent conversation tail breakpoints.

---

### 3. Thinking/Reasoning Block Retention & Provider Payloads

#### 3.1 Anthropic Thinking Retention & Signature Validation
- **Hermes Baseline (`benchmarks/hermes-agent/agent/anthropic_thinking_policy.py`, `anthropic_thinking_replay.py`):**
  - **Version-Aware Policy (`anthropic_thinking_policy.py:40-75`):**
    Anthropic preserves and bills prior assistant thinking on Opus 4.5+, Sonnet 4.6+, and Haiku 5.5+. Older models accept replayed blocks but strip them server-side.
  - **Route Recognition (`anthropic_thinking_policy.py:80-105`):**
    Identifies signed replay routes (`native`, `minimax`), unsigned routes (`deepseek`, `kimi`), and third-party relays.
  - **Durable Signature-Rejection Suppression (`anthropic_thinking_replay.py:30-150`):**
    When an upstream provider rejects a signed thinking block with HTTP 400 (signature mismatch after context manipulation or model version rollover), Hermes:
    1. Computes coarse SHA-256 fingerprints of rejected signatures (`_fingerprint`).
    2. Persists fingerprints in session DB (`_MODEL_CONFIG_KEY = "_anthropic_rejected_thinking"`).
    3. Filters out only the invalid signed blocks on retry and future session loads (`strip_rejected_thinking`) without mutating canonical history.
    4. Drops duplicate readable mirrors (`reasoning`, `reasoning_content`) so invalid blocks don't re-enter as unsigned reasoning.
    5. Carries fingerprints forward across context compaction boundaries (`carry_rejected_thinking_to_session`).
- **HEX Implementation (`providers/antigravity_provider.py`, `cloud_provider.py`):**
  - `registry.py` defines model identifiers including `claude-opus-4-6-thinking` and `claude-3-7-sonnet-20250219`.
  - **Zero Thinking Block Retention:** Neither `antigravity_provider.py` nor `cloud_provider.py` preserves `type: "thinking"` blocks with cryptographic signatures. Thinking is either discarded or concatenated into plain text.
  - **No Signature Rejection Handling:** If a model returns signed thinking blocks that are subsequently re-sent following compaction or edit, and the provider rejects them with HTTP 400, HEX has no suppression mechanism and the session crashes fatally.

#### 3.2 OpenAI Reasoning Tokens
- **Hermes Baseline (`benchmarks/hermes-agent/agent/reasoning_effort.py`, `agent/turn_usage.py`):**
  - Configurable `reasoning_effort` (`low`, `medium`, `high`) passed to OpenAI `/chat/completions` for o1/o3-mini.
  - Tracks `completion_tokens_details.reasoning_tokens` and includes them in context window accounting and billing telemetry.
- **HEX Implementation (`harness_core/router.py`, `cloud_provider.py`):**
  - `registry.py:149-150` includes `o1` and `o3-mini`.
  - However, `cloud_provider.py` does not pass `reasoning_effort` in request payloads, and does not parse `reasoning_tokens` out of usage telemetry into `ModelResponse.usage`.

#### 3.3 Gemini Thinking Budgets
- **Hermes Baseline (`benchmarks/hermes-agent/agent/turn_api_request.py`):**
  - Passes `thinkingConfig: {"thinkingBudget": ...}` for Gemini 2.5 Flash / Pro thinking models.
- **HEX Implementation (`providers/antigravity_provider.py:317`):**
  - Lists `gemini-2.5-flash-thinking` and `gemini-2.5-pro`.
  - Request payloads (`antigravity_provider.py:1372-1415`) do not configure `thinkingConfig` or set thinking budgets.

---

### 4. Code Quality, Exception Safety & Performance

#### 4.1 Exception Safety & Error Recovery
- **Hermes Baseline (`benchmarks/hermes-agent/agent/turn_api_error.py:53-230`, `turn_recovery.py:237-750`):**
  - Comprehensive error categorization (`ClassifiedError`).
  - Pre-classification recovery: Strips images and retries on HTTP 413 (`_strip_request_images_and_retry`), fixes Unicode encoding failures (`_recover_unicode_encode_error`), handles format errors (`_recover_format_errors`).
  - Post-classification recovery: Parses rate limit reset timestamps (`limit_reset_epoch`), calculates backoff with jitter (`compute_error_backoff`), implements interruptible sleep (`interruptible_backoff_sleep`), auto-refreshes expired transport credentials on 401 (`_refresh_credentials_after_401`), and executes tier/model fallback (`route_classified_error`).
- **HEX Implementation (`harness_core/react_loop.py:761-780`):**
  - Model invocation inside `ReActLoop.run` is wrapped in a single raw try/except block:
    ```python
    # harness_core/react_loop.py:761-779
    try:
        response = self._call_model(self.messages)
    except Exception as e:
        turn_latency = (time.perf_counter() - turn_start) * 1000
        self.turns.append(
            ReActTurn(turn_index=turn_index, thought=f"Model execution error: {e}", latency_ms=turn_latency)
        )
        return ReActSessionResult(
            success=False,
            final_answer=f"Error: {e}",
            total_turns=len(self.turns),
            turns=self.turns,
            termination_reason="error",
            compaction_result=self.last_compaction_result,
        )
    ```
  - **Critical Failure Mode:** Any transient HTTP blip, 429 rate limit, 503 gateway timeout, or minor connection reset immediately causes the entire ReAct loop to abort permanently without a single retry attempt.

#### 4.2 Concurrency & Execution Performance
- **Hermes Baseline:**
  - Fully asynchronous core architecture with non-blocking I/O, background turn processing, and cancellation hooks.
- **HEX Implementation (`harness_core/react_loop.py:486-618`, `897-907`):**
  - Tool execution is synchronous and blocking. If a shell command or browser tool takes 60 seconds, the thread is blocked.
  - `run_async` simply delegates to `run_in_executor`. Under high session load, this can exhaust Python threadpools.

---

## Critical Gaps & Risk Analysis

1. **Native Tool Schema Bypass (Risk: HIGH)**  
   *Symptom:* `orchestrator.py:468` flattens messages into `[USER]` / `[ASSISTANT]` plaintext and prompts the model to return `<tool_call>` XML tags.  
   *Impact:* Advanced frontier models (Claude 3.7, GPT-4o, Gemini 2.5) have specialized attention paths for native tool schemas; forcing them to emit XML tags increases token consumption by ~15-25% and reduces tool invocation accuracy on complex schemas.
2. **Absence of In-Loop API Error Recovery & Backoff (Risk: HIGH)**  
   *Symptom:* `react_loop.py:761` catches exceptions and immediately returns `termination_reason="error"`.  
   *Impact:* An agent running a 25-turn complex coding task will completely abort if turn 18 encounters a momentary 1-second network timeout or 429 rate limit.
3. **Missing Anthropic Thinking Retention & Signature Validation (Risk: CRITICAL)**  
   *Symptom:* No signed block tracking or signature suppression in `providers/antigravity_provider.py` or `cloud_provider.py`.  
   *Impact:* When utilizing Claude 3.7 Sonnet or Claude Opus 4.6 Thinking, any multi-turn context replay containing thinking blocks will either strip the thoughts (degrading reasoning quality) or cause fatal HTTP 400 signature rejections.
4. **Stacked Compaction Summaries (Risk: MEDIUM)**  
   *Symptom:* Multiple compaction cycles in `context_compressor.py` do not prune previous `## CONTEXT COMPACTION SUMMARY` blocks.  
   *Impact:* Context windows become polluted with nested historical summaries over long sessions (30+ turns).
5. **No Pre-Turn Gateway Hygiene (Risk: MEDIUM)**  
   *Symptom:* Compaction only runs inside the ReAct loop; no preflight check runs before turn 1 starts.  
   *Impact:* Sessions initialized with large external context or large system prompts can exceed context limits on the very first turn.

---

## Actionable Remediation Roadmap

### Priority 1: In-Loop Exponential Backoff & Transient Error Recovery
**Target:** `harness_core/react_loop.py`\
**Prescription:** Wrap `_call_model` with a retry loop supporting exponential backoff with jitter for transient errors (HTTP 429, 500, 502, 503, 504, timeout).
```python
# Remediation in harness_core/react_loop.py
max_retries = 3
backoff_base = 2.0
for attempt in range(max_retries + 1):
    try:
        response = self._call_model(self.messages)
        break
    except Exception as e:
        if attempt == max_retries or not self._is_retryable_error(e):
            return ReActSessionResult(..., termination_reason="error")
        sleep_sec = (backoff_base ** attempt) + random.uniform(0.1, 0.5)
        time.sleep(sleep_sec)
```

### Priority 2: Native Tool Message Formatting & Structured Protocol
**Target:** `harness_core/react_loop.py` & `harness_core/orchestrator.py`\
**Prescription:** Support standard `role: "tool"` (OpenAI) and content-part `tool_result` (Anthropic) alongside XML fallback:
```python
# Format observation properly based on active mode
if self.native_tool_mode:
    self.messages.append({
        "role": "tool",
        "tool_call_id": tool_res.call_id,
        "name": tool_res.name,
        "content": obs_payload,
    })
else:
    self.messages.append({
        "role": "user",
        "content": obs_content,
        "tool_name": tool_res.name,
        "call_id": tool_res.call_id,
    })
```

### Priority 3: Anthropic Thinking Block Retention & Rejection Suppression
**Target:** `providers/cloud_provider.py` & `harness_core/context_compressor.py`\
**Prescription:**
1. In `cloud_provider.py`, preserve `type: "thinking"` and `signature` blocks when converting responses.
2. Implement durable fingerprinting of rejected signatures (`_fingerprint`) matching `benchmarks/hermes-agent/agent/anthropic_thinking_replay.py:30-150` so that if Anthropic returns HTTP 400 for a signature, HEX drops the bad signature from the request copy and retries automatically.

### Priority 4: Shadow Checkpoint Deduplication in Compactor
**Target:** `harness_core/context_compressor.py`\
**Prescription:** Add a pre-compaction step in `ContextCompactor.compact_messages` that collapses prior `SUMMARY_HEADER` messages so only the newest consolidated summary remains in context.

### Priority 5: Streaming Token Event Dispatch
**Target:** `harness_core/react_loop.py`\
**Prescription:** Add an optional `on_event` callback or async generator to `ReActLoop` emitting `TokenChunk`, `ToolStart`, `ToolEnd`, and `TurnComplete` events for real-time frontend and TUI visualization.

---

## Conclusion
HEX possesses a clean, well-architected 4-phase context compaction foundation in `context_compressor.py` that rivals Hermes in boundary alignment and stale tool demotion. However, to achieve true production parity with Hermes Agent in Domain 1, HEX must upgrade its ReAct loop from plaintext/XML serialization to native tool schemas, introduce in-loop transient error recovery/backoff, adopt Anthropic thinking retention with durable signature suppression, and implement live streaming event dispatch.

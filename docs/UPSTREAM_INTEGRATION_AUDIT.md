# Infrastructure & Upstream Integration Audit

**Date:** 2026-11-11  
**Milestone:** Milestone M8 (Deep Upstream Vendor Integration)  
**Target Repository:** `/Users/anv./Development/Harness`  
**Reference Specifications:** `PRD.md` (v1.3 Final), `Questions.md`, `.gsd/ROADMAP.md`

---

## 1. Executive Summary: What Is Real vs. What Is Wired

HEX was designed to harness published, battle-tested upstream engines—specifically **Google Research Harness²**, **NVIDIA SkillSpector**, and **Graphify**—alongside a robust local infrastructure (Apple Silicon M4 MLX / Google Antigravity OAuth, Copy-on-Write APFS snapshots, PostgreSQL 16 / SQLite WAL dual-driver storage, and risk-tiered execution gateways).

All three upstream codebases are physically vendored in `vendor/`:
- `vendor/harness2` ([google-research/harness2](https://github.com/google-research/harness2))
- `vendor/skillspector` ([NVIDIA/SkillSpector](https://github.com/NVIDIA/SkillSpector), v2.12.0)
- `vendor/graphify` ([Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify))

| Upstream System / Component | Upstream Source | Cloned in Repo? | Upstream Code Actually Wired In | Implementation Status & Plan |
|---|---|---|---|---|
| **Google Research Harness²** | [google-research/harness2](https://github.com/google-research/harness2) | **Yes** (`vendor/harness2`) | **~95% Wired** (`OrchestratorBenchmarkAdapter`, `runmark` caching, `LLMRouterProposer`, dual substrates, sweeps, judging, Experience Library) | **Phase 18 Complete & Verified.** Full Propose–Probe–Compose loop with selection, judging, statistical reporting, and speculative branch reflection into `EXPERIENCE.md`. |
| **NVIDIA SkillSpector** | [NVIDIA/SkillSpector](https://github.com/NVIDIA/SkillSpector) | **Yes** (`vendor/skillspector`, v2.12.0) | **~30% Wired** (Gateway gate & audit models wired; AST/OSV engines queued) | **Phase 19 Active.** Replacing custom 10-regex scanner with upstream AST dangerous-code analyzer (`python_ast.py`), taint tracker, and live OSV.dev batch client (`osv_client.py`). |
| **Graphify** | [Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify) | **Yes** (`vendor/graphify`) | **~35% Wired** (Active Blast-Radius Interceptor hook active; multi-language engine queued) | **Phase 20 Queued.** Wire upstream `graphify.extract` tree-sitter engine (33+ languages) and ingest `graphify-out/graph.json` while retaining the 0ms Python AST parser as a fast local fallback. |
| **PostgreSQL 16 + SQLite WAL + Alembic** | `PRD.md` Section 8.1 & 10 | **Native Drivers** (`asyncpg` + `sqlite3`) | **100% Wired** (`src/harness_core/db.py`) | Dual-driver architecture with connection pooling, `SKIP LOCKED` concurrency, cryptographic hash chaining (`verify_event_chain`), and reversible Alembic migrations. |
| **Model Routing & Antigravity OAuth** | `PRD.md` Section 6.6 (`FR-045a`) | **Native SDKs** | **100% Wired** (`src/harness_core/router.py`, `providers/`) | Antigravity $0 Day-1 OAuth PKCE & local `state.vscdb` import, live quota inspection (`hex quota`), Gemini, Anthropic Claude, OpenAI, and Apple Silicon MLX log compression. |
| **Copy-on-Write (CoW) Provider** | `PRD.md` Section 6.2 (`FR-012a`) | **Native OS binaries** | **100% Wired** (`src/harness_core/cow.py`) | Apple APFS clone mode (`cp -c -R`), Linux OverlayFS, Merkle SHA-256 tree hashing, and CAS `.tar.gz` delta sync. |

---

## 2. Deep-Dive Audit: The Three External Pillars

### 2.1 Google Research Harness² (`vendor/harness2` vs. `src/harness_core/harness2_adapter.py`)

[Google Research Harness²](https://github.com/google-research/harness2) provides autonomous prompt and harness evolution using contrastive **Propose–Probe–Compose** sweeps, candidate selection, and cross-task Experience Libraries.

#### Upstream Architecture:
- `core/tree.py`: 8-component editable surface (`SurfaceSpec` covering `systemprompt.md`, `guardrails.md`, `LongTermMEMORY.md`, `tool_descriptions/tool_guidance.md`, `skills/*/SKILL.md`, `sub_agents/*/AGENT.md`, `scripts/*.py`, `plugin/*.ts`).
- `core/runmark.py`: Content-addressed rollout caching via `(run_id, harness_fingerprint)`.
- `core/recursion.py`: Parallel sweeps (`run_parallel` / `mode="agg"`) and sequential evolution (`run_sequential` / `mode="seq"`).
- `core/selection.py`: Anonymized candidate pool creation (`selection_pool`), voting, and adoption reflection (`reflect_adoption`).
- `core/library.py`: Cross-task Experience Library (`Library.add`, `Library.rebuild_index`, `EXPERIENCE.md`).
- `core/judge.py` & `core/report.py`: Noise-floor evaluation and statistical Markdown rendering.
- `substrate/`: `OpencodeSubstrate` (`systemprompt.md`) and `CodexSubstrate` (`AGENTS.md`).

#### Current Integration Status: **95% Wired & Verified (Phase 18 Complete)**
1. **`OrchestratorBenchmarkAdapter`**: Conforms directly to `harness2.core.spec.BenchmarkAdapter`. Executes candidate rollouts through `HarnessOrchestrator` inside isolated CoW workspaces, logs trajectories in `[ACTION]/[OBSERVATION]/[ERROR]` grammar, and delivers verified artifacts.
2. **Runmark Caching (`harness2.core.runmark`)**: Zero-compute rollout reuse when `(run_id, harness_fingerprint)` matches. Includes `is_rollout_reusable` and `why_not` diagnostics.
3. **Dual-Substrate Support**: Supports both `OpencodeSubstrate` (`OPENCODE_SURFACE`) and `CodexSubstrate` (`CODEX_SURFACE`), with auto-detection in `proof.py` and `--substrate {opencode,codex}` in CLI.
4. **`LLMRouterProposer`**: Bridges upstream `Proposer` to HEX's `TwoTierModelRouter` ($0 Antigravity OAuth quota, cloud reasoners, or local Apple Silicon M4 MLX), parses JSON file edits, audits proposed skills via `SkillSpectorScanner`, and falls back deterministically to `LocalRouterProposer` when offline.
5. **Full Post-Sweep Pipeline**: Runs `selection_pool` -> `trivial_selection`/`run_selection` -> `reflect_adoption` -> `Library.add` (writing to `INDEX.md` and `EXPERIENCE.md`) -> `judge.judge_selections` -> `report.summarise` & `report.render`.
6. **Speculative Branch Reflection**: When competing speculative branches complete in `HarnessOrchestrator.run_speculative_execution`, the winning strategy and discarded branches are automatically distilled into a contrastive lesson (`cue`, `lesson`, `evidence`, `confidence`) and saved to the domain Experience Library.
7. **CLI Integration**: Exposed via `hex harness2-eval --task-id <id> --mode {parallel,sequential} --substrate {opencode,codex} --proposer {local,llm} --k <k> --json`.

---

### 2.2 NVIDIA SkillSpector (`vendor/skillspector` vs. `src/harness_core/skillspector.py`)

[NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector) provides a comprehensive security analysis pipeline for agent skills and tools.

#### Upstream Architecture:
- `python_ast.py` & `nodes/analyzers/behavioral_ast.py`: AST-level inspection of skill script files for dangerous execution patterns (`eval`, `exec`, `subprocess`, dynamic imports, sockets, deserialization, reflective calls).
- `nodes/analyzers/osv_client.py`: Queries live OSV.dev batch API (`https://api.osv.dev/v1/querybatch`) for manifest package CVEs, with offline fallback.
- `yara_rules/`: 71 vulnerability patterns across 17 threat categories (prompt injection, MCP poisoning, memory taint, privilege escalation).
- `input_handler.py`: Multi-format ingestion with zip-bomb protection (`INGEST_MAX_BYTES = 100 MiB`).

#### Current Integration Status: **~30% Wired (Phase 19 Active)**
- **Wired:** Skill security gate in `HarnessOrchestrator`, `ToolGateway`, and `LLMRouterProposer`. Automatic quarantine of high-risk skills (`risk_score >= 60`) into `.harness_data/quarantine/`.
- **In Progress (Phase 19):**
  1. Wire `vendor/skillspector/src/skillspector/python_ast.py` and `behavioral_ast.py` into `src/harness_core/skillspector.py` to replace custom regex heuristics with upstream AST taint tracking.
  2. Wire `vendor/skillspector/src/skillspector/nodes/analyzers/osv_client.py` for live OSV batch auditing, falling back to local cached vulnerability databases.

---

### 2.3 Graphify (`vendor/graphify` vs. `src/harness_core/graphify.py`)

[Graphify Labs](https://github.com/Graphify-Labs/graphify) is a multi-language AST and knowledge-graph engine.

#### Upstream Architecture:
- Uses **tree-sitter** across 33+ languages (Python, TypeScript, Go, Rust, Java, C/C++, etc.) in `graphify/extract.py` and `graphify/extractors/engine.py`.
- Explicitly tags graph edges as `EXTRACTED` (deterministic AST facts) or `INFERRED` (resolved cross-module/semantic links) across calls, imports, and inheritance.
- Emits queryable knowledge graphs in `graphify-out/`: `graph.json`, `GRAPH_REPORT.md`, and `graph.html`.

#### Current Integration Status: **~35% Wired (Phase 20 Queued)**
- **Wired:** Custom `ActiveBlastRadiusInterceptor` in `src/harness_core/gateway.py` and `graphify.py`. Hooks into `R2` `write_file` tool calls, diffs modified symbols against the call graph, and automatically pins downstream tests to `plans.blast_radius_checks_json`.
- **Queued (Phase 20):**
  1. Add a pluggable `GraphifyUpstreamRunner` that executes upstream Graphify's tree-sitter extractor on polyglot codebases and generates `graphify-out/graph.json`.
  2. Keep HEX's 0ms native Python AST analyzer as an ultra-fast local option for pure-Python tasks, falling back seamlessly.

---

## 3. Infrastructure & Core Services Audit

| Component | Architecture & Status | Capabilities & Verification |
|---|---|---|
| **Storage & Ledger** | **Dual-Driver (PostgreSQL 16 + SQLite WAL)** | SQLite WAL by default; PostgreSQL `asyncpg` with connection pooling and `SELECT ... FOR UPDATE SKIP LOCKED` for multi-worker clusters. All events verified via monotonic SHA-256 parent hash chains (`verify_event_chain`). |
| **Workspace Sandboxing** | **Copy-on-Write (APFS / OverlayFS)** | Apple APFS clone mode (`cp -c -R`) on macOS Darwin; OverlayFS/Btrfs on Linux. 0ms isolated attempt snapshots with Merkle tree SHA-256 verification and CAS `.tar.gz` delta transfer across nodes. |
| **Model Routing & Quota** | **TwoTierModelRouter + Providers** | Antigravity $0 Day-1 OAuth PKCE & local `state.vscdb` session import; Gemini, Anthropic Claude, OpenAI, and Apple Silicon MLX log compression. Live quota inspection via `hex quota`. |
| **Project Registry** | **Folder-Scoped Context Isolation** | Manages workspaces in `~/.hex/projects.json`. Automatically prunes deleted/stale paths, isolates unit tests to ephemeral test registries, and detects enclosing repositories. |
| **Operator Cockpit** | **Rich TUI & Hands-Free Voice** | Interactive Cockpit TUI with 5 color skins (`amber`, `matrix`, `cyberpunk`, `nord`, `mono`), live token streaming, and macOS hands-free voice console (`hex voice`). |
| **Production Daemons** | **macOS launchd & Linux systemd** | Background service lifecycle via `hex daemon {start,status,logs,stop,install,uninstall}` and clean system removal via `hex uninstall`. |

---

## 4. Immediate Roadmap (Milestone M8 Execution)

1. **Phase 19: NVIDIA SkillSpector Behavioral AST & Live OSV Scanner**:
   - Ingest upstream `skillspector.python_ast` and `behavioral_ast` into `SkillSpectorScanner`.
   - Wire `osv_client.py` for live OSV.dev batch vulnerability queries.
   - Comprehensive test suite in `tests/test_skillspector_ast_wiring.py` and `tests/test_skillspector_osv_wiring.py`.
2. **Phase 20: Graphify Upstream CLI & Tree-Sitter 33+ Language Adapter**:
   - Implement `GraphifyUpstreamRunner` to extract multi-language graphs into `graphify-out/graph.json`.
   - Connect active blast-radius interceptor to upstream edge graphs.
   - Maintain fast native Python AST analyzer as default offline engine.

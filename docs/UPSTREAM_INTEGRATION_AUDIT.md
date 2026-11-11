# Infrastructure & Upstream Integration Audit

**Date:** 2026-10-31  
**Target Repository:** `/Users/anv./Development/Harness`  
**Reference Specification:** `PRD.md` (v1.3 Final) & `Questions.md`

---

## 1. Executive Summary: What Is Real vs. What Is Currently Recreated

To get a working Stage 1–5 baseline running immediately without external network/database dependencies, the current codebase combines **1 genuine upstream repository (`vendor/harness2`)** with **custom Python re-implementations** of Graphify, NVIDIA SkillSpector, and several infrastructure adapters (SQLite instead of PostgreSQL 16, regex heuristics instead of live MLX LLM calls, and Python `ast` instead of `tree-sitter`).

| Upstream System / Component | Upstream Source | Cloned / Installed in Repo? | Upstream Code Actually Wired In | Recreated / Stubbed in Our Code (`src/harness_core/`) |
|---|---|---|---|---|
| **Google Research Harness²** | [google-research/harness2](https://github.com/google-research/harness2) | **Yes** (`vendor/harness2`, 14 core modules) | ~35% wired (`tree.py`, `opencode.py`, `recursion.py` parallel mode, `demo.py`) | ~65% not yet wired into live task loop (`library.py` experience reflection, `selection.py`, `schedule.py`, `run_sequential`, real LLM `Proposer`) |
| **Graphify** | [Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify) (`graphifyy` on PyPI) | **No** (0% cloned/installed) | **0% wired** | **100% recreated** in `src/harness_core/graphify.py` using Python's built-in `ast` module (Python-only, no `tree-sitter`, no `graphify-out/graph.json`, no `EXTRACTED`/`INFERRED` edge tags) |
| **NVIDIA SkillSpector** | [NVIDIA/SkillSpector](https://github.com/NVIDIA/SkillSpector) | **No** (0% cloned/installed) | **0% wired** | **100% recreated** in `src/harness_core/skillspector.py` using 10 custom regex rules, a 5-package hardcoded OSV dict, and a keyword-matching heuristic instead of SkillSpector's 71 rules / 17 categories, live OSV.dev client, AST/YARA/taint analyzers, and LLM semantic evaluator |
| **PostgreSQL 16 + Alembic** | `PRD.md` Section 8.1 & 10 | **No** (SQLite3 used) | Schema SQL written (`migrations/0001_initial_schema.sql`) | `src/harness_core/db.py` uses Python's built-in `sqlite3` (`WAL` mode) instead of PostgreSQL 16 + `asyncpg` + `SELECT ... FOR UPDATE SKIP LOCKED` + Alembic |
| **Tier-1 Local M4 MLX & Tier-2 Cloud Router** | `PRD.md` Section 6.6 (`FR-045a`) | **No** (`mlx-lm` not installed) | **0% real LLM calls** | `src/harness_core/router.py` uses regex line filtering (`AssertionError`/`File ... line ...`) instead of an actual MacBook Air M4 MLX 7B–9B model or cloud LLM SDK |
| **Copy-on-Write (CoW) Provider** | `PRD.md` Section 6.2 (`FR-012a`) | **Native OS binary** (`/bin/cp -c -R`) | **100% real on macOS APFS** (`cp -c -R` + SHA-256 Merkle tree hashing) | Linux OverlayFS / Btrfs subvolumes are not implemented (uses `shutil.copytree` fallback on non-Darwin) |

---

## 2. Deep-Dive Audit: The Three External Pillars

### 2.1 Google Research Harness² (`vendor/harness2` vs. `src/harness_core/proof.py`)

[Harness²](https://github.com/google-research/harness2) is physically present in `vendor/harness2/harness2/`. Its upstream architecture consists of:
- `core/tree.py`: 8-component editable surface (`SurfaceSpec` covering `systemprompt.md`, `guardrails.md`, `LongTermMEMORY.md`, `tool_descriptions/tool_guidance.md`, `skills/*/SKILL.md`, `sub_agents/*/AGENT.md`, `scripts/*.py`, `plugin/*.ts`), `read_tree`, `write_tree`, `validate`, `autotrigger`, `diff_summary`, and `diversity`.
- `core/recursion.py`: Task-level contrastive **Propose–Probe–Compose** in two modes: `run_parallel` (`mode="agg"`, probing $2k$ `INV`/`DOM` candidates and composing $k$ `AGG` harnesses) and `run_sequential` (`mode="seq"`, carrying evidence across $k$ sequential rounds).
- `core/library.py`: Harnessing-level persistent **Experience Library** (`Library.add`, `Library.rebuild_index`, `EXPERIENCE.md` with `cue`, `lesson`, `evidence`, `confidence`) that carries adopted harnesses and lessons across tasks in the same domain.
- `core/context.py`, `core/proposer.py`, `core/selection.py`, `core/prompts.py`: Evidence workspace builder, LLM proposer session driver, candidate selector, and contrastive reflection prompts.

#### What we actually wired in (`src/harness_core/proof.py` — `Harness2ControlledAdapter`):
1. **Directly called:**
   - `OpencodeSubstrate().surface()` (`vendor/harness2/harness2/substrate/opencode.py`)
   - `harness2.core.tree.autotrigger()`, `validate()`, `diff_summary()`, `write_tree()`, `read_tree()`
   - `harness2.core.recursion.run_parallel()` — **only when `run_demo_probe=True`**, where it runs against `harness2.demo.DemoAdapter` and `DemoProposer` (the scripted CSV ledger demo).
2. **What is NOT wired in yet (Gap):**
   - **Our `HarnessOrchestrator` does not use Harness² as an execution/refinement loop on real tasks.** When you submit a task to `HarnessOrchestrator`, it runs a static list of steps (`list_files`, `query_graphify`), not a Harness² rollout or `Propose–Probe–Compose` cycle.
   - `harness2.core.library.Library` (the harnessing-level Experience Library that writes `EXPERIENCE.md` and `INDEX.md`) is **0% wired** into our `GovernedMemoryService` or `Harness2ControlledAdapter`.
   - `harness2.core.selection` (choosing the winning composed harness) and `harness2.core.recursion.run_sequential` are **not wired**.
   - `Proposer` is only called via the scripted `DemoProposer`, not wired to our `TwoTierModelRouter` or a live LLM.

---

### 2.2 Graphify ([Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify) vs. `src/harness_core/graphify.py`)

Upstream [Graphify](https://github.com/Graphify-Labs/graphify) (`graphifyy` package) is a multi-language AST and knowledge-graph engine that:
- Uses **tree-sitter** across 33–40+ languages (`Python`, `TypeScript`, `Go`, `Rust`, `Java`, `C/C++`, etc.) in `graphify/extract.py` and `graphify/extractors/engine.py`.
- Labels every graph edge explicitly as `EXTRACTED` (deterministic AST fact) or `INFERRED` (resolved cross-module/semantic link) across `calls`, `imports`, `inherits`, and `mixes_in` relationships.
- Emits canonical artifacts in `graphify-out/`: `graph.json` (queryable knowledge graph), `GRAPH_REPORT.md` (architectural highlights & "God Nodes"), and `graph.html`.
- Provides CLI commands (`graphify update`, `graphify hook install`) and path-tracing queries.

#### What we recreated instead (`src/harness_core/graphify.py` — `ActiveGraphifyAdapter`):
- **0% of upstream Graphify is installed or called.**
- We wrote a 330-line custom Python-only parser (`ActiveGraphifyAdapter`) using Python's standard library `ast.parse()`.
- **Limitations of our recreation vs. real Graphify:**
  1. Only parses `.py` files (ignores TypeScript, JS, Go, Rust, SQL, Markdown docs).
  2. Resolves calls by naive short function name (`child.func` string matching) rather than Graphify's `EXTRACTED`/`INFERRED` module-scoped edge resolution.
  3. Does not generate or query `graphify-out/graph.json` or `GRAPH_REPORT.md`.
- **What our custom code *does* provide that we should keep as a wrapper around real Graphify:**
  - The **Active Blast-Radius Interceptor hook** (`compute_blast_radius` + `append_blast_radius_checks` in `gateway.py`), which diffs before/after symbols on `R2` `write_file` calls and pins affected downstream tests to `plans.blast_radius_checks_json`. This should sit *on top of* upstream Graphify's `graph.json` rather than replacing Graphify.

---

### 2.3 NVIDIA SkillSpector ([NVIDIA/SkillSpector](https://github.com/NVIDIA/SkillSpector) vs. `src/harness_core/skillspector.py`)

Upstream [NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector) is a full security analysis pipeline that provides:
- **68–71 vulnerability patterns across 17 categories:** prompt injection, data exfiltration, privilege escalation, supply chain (`SC4`), excessive agency, output handling, system prompt leakage, memory poisoning, tool misuse, rogue agent, anti-refusal, trigger abuse, dangerous code (AST analysis), taint tracking, YARA signatures, MCP least privilege, and MCP tool poisoning.
- **Multi-format ingestion:** directories, single `SKILL.md` files, zip archives (with `INGEST_MAX_BYTES = 100 MiB` and `INGEST_MAX_ZIP_MEMBERS = 10,000` zip-bomb guards), and Git URLs.
- **Live OSV.dev API client (`SC4`):** real-time CVE vulnerability queries against `https://api.osv.dev/v1/query` with offline fallback.
- **Two-stage static + LLM semantic evaluator:** compares declared skill frontmatter intent against actual instructions/scripts, outputting Terminal, JSON, Markdown, and SARIF reports plus fingerprint/glob baselines.

#### What we recreated instead (`src/harness_core/skillspector.py` — `SkillSpectorScanner`):
- **0% of upstream NVIDIA SkillSpector is cloned or called.**
- We wrote a 380-line custom counterfeit scanner (`SkillSpectorScanner`) containing:
  1. Only **10 regex patterns** (`STATIC_RULES`) instead of NVIDIA's 71 rules, AST analyzer, taint tracker, and YARA rules.
  2. A hardcoded **5-package Python dictionary** (`KNOWN_OSV_VULNS`) instead of SkillSpector's dependency parser and live/offline OSV.dev scanner.
  3. A **substring if-statement** (`_evaluate_semantic_alignment` checking if `"rm -rf"` or `"write_text("` appears in a string) pretending to be the Tier-1 Local M4 MLX semantic alignment model.

---

## 3. Infrastructure Comparison Against `PRD.md` Plan

| PRD Section & Requirement | Planned Architecture in `PRD.md` | Current Implementation in `src/harness_core/` | Status & Gap |
|---|---|---|---|
| **8.1 API & Runtime** | FastAPI + Pydantic v2 + Uvicorn; CLI (`harness`) | Built-in `http.server.ThreadingHTTPServer` + `dataclasses` (`server.py`, `models.py`) because `fastapi`/`pydantic` were not pre-installed in global Python | **Functional equivalent, but not FastAPI/Pydantic v2.** Needs a `.venv` with `fastapi`, `uvicorn`, `pydantic` installed. |
| **8.1 Database & Queue** | PostgreSQL 16+ (`SELECT ... FOR UPDATE SKIP LOCKED`), SQLAlchemy/asyncpg, Alembic migrations (`FR-010`, `FR-011`) | `sqlite3` in WAL mode (`db.py`) executing `migrations/0001_initial_schema.sql` | **Schema complete (all 16 tables), engine is SQLite.** Needs PostgreSQL `asyncpg` driver + Alembic wiring alongside SQLite fallback. |
| **6.2 CoW Snapshots (`FR-012a`)** | `WorkspaceSnapshotProvider` using macOS APFS `cp -c` and Linux OverlayFS/Btrfs | `APFSAndPortableCoWProvider` (`cow.py`) runs real `/bin/cp -c -R` on macOS APFS + SHA-256 Merkle tree hashes + `tar.gz` CAS delta export | **Properly wired on macOS (APFS).** Add Linux `mount -t overlay` / `btrfs subvolume snapshot` commands for Desktop parity. |
| **6.2 Call-Level Journaling (`FR-015`)** | Tool Gateway records `(task_id, step_id, tool_id, canonical_args_hash, idempotency_key)` before execution; replays sealed calls in `0 ms` | Implemented in `gateway.py` (`invoke` & `execute_code_mode_composition`) + `tool_calls` table in `db.py` | **Properly wired.** Replays sealed calls in `<0.1 ms` with policy re-verification. |
| **6.1 Fast-Path Express Lane (`FR-001a`)** | Read-only `R0/R1` tasks with `<3` steps bypass DAG ceremony (`<2s` startup) | Implemented in `orchestrator.py` (`_execute_fast_path`) | **Properly wired (`~0.18s` execution).** Currently runs default static tool steps unless steps or an LLM planner are supplied. |
| **6.3 Dynamic DAG & Speculative Execution (`FR-020`, `FR-021a`)** | Dynamic Dependency DAG + `fork` into parallel CoW attempt branches, verify each, promote winner, GC loser | `fork_speculative_branches` in `orchestrator.py` clones isolated APFS snapshots, runs parallel threads, verifies via unittest, promotes winner, deletes loser | **CoW branching & verification mechanics work**, but DAG generation is static unless steps are passed in request JSON (no LLM planner wired yet). |
| **6.4 Active Graphify (`FR-036`, `FR-036a`)** | Upstream Graphify (`tree-sitter` AST + `graph.json`) + Active Blast-Radius Interceptor | Custom `ast.parse` recreator in `graphify.py` + Gateway interceptor hook | **Interceptor hook is wired, but Graphify itself is recreated.** Needs upstream `Graphify-Labs/graphify` cloned/installed and wired. |
| **6.5 SkillSpector Gate (`FR-039e`)** | Upstream NVIDIA SkillSpector (64–71 rules + OSV + M4 MLX semantic check) | Custom 10-regex + 5-OSV recreator in `skillspector.py` | **Ledger gate is wired, but SkillSpector engine is recreated.** Needs upstream `NVIDIA/SkillSpector` cloned into `vendor/skillspector` and wired. |
| **6.6 Two-Tier Model Router (`FR-045a`)** | Tier-1 Local MacBook Air M4 MLX model (log compression, semantic skill scan, R0/R1 triage) + Tier-2 Cloud Reasoner | `TwoTierModelRouter` in `router.py` uses regex line extraction instead of real MLX/Cloud LLM inference | **Stubbed with regex.** Needs real `mlx_lm` / local OpenAI-compatible endpoint + Tier-2 cloud client (Gemini / Anthropic / OpenAI). |
| **6.8 Controlled Self-Improvement (`FR-060..FR-068`)** | Upstream `google-research/harness2` (`Propose–Probe–Compose` + `Library`) gated by SkillSpector & immutable revisions | `vendor/harness2` is cloned; `Harness2ControlledAdapter` validates `OPENCODE_SURFACE`, scans skills, and runs `DemoAdapter` | **Partially wired.** Needs a real `HarnessExecutionAdapter` bridging `vendor/harness2` to `HarnessOrchestrator` + `Library` persistence. |

---

## 4. Concrete Plan to Wire All Three Upstream Systems Properly

To eliminate the custom recreations and wire the real upstream engines end-to-end:

### Step 1: Clone & Wire Real **NVIDIA SkillSpector** (`vendor/skillspector`)
1. Clone `https://github.com/NVIDIA/SkillSpector.git` into `vendor/skillspector` (and strip `.git` so your root repo stays detached).
2. Replace the 10-regex counterfeit rules in `src/harness_core/skillspector.py` with direct calls into `vendor/skillspector`'s real static rule engine, AST dangerous-code analyzer, taint tracker, YARA scanner, OSV.dev client, and LLM semantic evaluator, mapping its official output report into our `skill_scans` ledger table.

### Step 2: Clone & Wire Real **Graphify** (`vendor/graphify`)
1. Clone `https://github.com/Graphify-Labs/graphify.git` into `vendor/graphify` (and strip `.git`).
2. Wire `src/harness_core/graphify.py` (`ActiveGraphifyAdapter`) to invoke Graphify's real extraction engine (`graphify/extract.py` / `graphify-out/graph.json`) so node and edge queries use Graphify's `EXTRACTED` and `INFERRED` graph structure, while keeping our **Active Blast-Radius Interceptor** on top of Graphify's edges to pin downstream tests to `plans.blast_radius_checks_json`.

### Step 3: Deeply Wire **Google Research Harness²** (`vendor/harness2`)
1. Implement a real `AutonomousHarnessBenchmarkAdapter` (implementing `harness2.core.spec.BenchmarkAdapter`) in `src/harness_core/proof.py` that executes candidate harnesses through our `HarnessOrchestrator` + `ToolGateway` + `APFSAndPortableCoWProvider` instead of only calling `harness2.demo.DemoAdapter`.
2. Wire both `harness2.core.recursion.run_parallel` and `run_sequential` for task-level `Propose–Probe–Compose`, and wire `harness2.core.library.Library` so adopted harnesses and `EXPERIENCE.md` lessons are persisted and synchronized with `GovernedMemoryService`.
3. Every candidate harness tree emitted during `Propose` and `Compose` passes through the real `vendor/skillspector` scanner before rollout execution.

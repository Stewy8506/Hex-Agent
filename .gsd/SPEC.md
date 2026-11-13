# Project Specification: Custom Autonomous AI Harness

**Status:** FINALIZED  
**Reference:** [PRD.md](../PRD.md) (v1.3 Final), [ADR-001](../docs/adr/0001-m0-architecture-decisions.md)  
**Date:** 2026-10-31  

---

## 1. System Vision & Architecture

Build a personal, extensible autonomous AI harness for project-scoped coding and technical research that executes work inside Copy-on-Write (CoW) workspaces, enforces default-deny tool policies (`R0–R4`), journals every tool call for `0 ms` crash replay, intercepts AST blast-radius impacts via Graphify, scans skills for supply-chain threats via NVIDIA SkillSpector, and integrates Google Research's Harness² (`Propose–Probe–Compose`) behind a controlled self-improvement gate.

### Architecture Topology
1. **Control Plane:**
   - FastAPI REST API (`/v1/*`) + Server-Sent Events (SSE) live stream.
   - Authoritative 16-table PostgreSQL ledger (with zero-config SQLite WAL fallback for local development).
   - Local-first Web Control Surface & CLI (`harness`).
   - Content-Addressed Store (CAS) for artifacts, checkpoints, and diffs.
2. **Two-Tier Model Pipeline:**
   - **Tier-1 Local M4 Gatekeeper:** MacBook Air M4 (MLX 8B–9B models) for continuous zero-cost log compression (>70% token savings), SkillSpector semantic scans, pre-flight tool validation, and private `R0/R1` tasks.
   - **Tier-2 Heavy Reasoners:** Frontier cloud models (OpenAI, Anthropic, Gemini) or high-VRAM desktop GPUs for multi-turn architectural planning and code generation.
3. **Execution & Isolation Layer:**
   - macOS APFS clones (`cp -c -R`) and Linux OverlayFS / Btrfs subvolumes for millisecond CoW workspace snapshots and clean rollbacks.
   - Speculative Branching (`fork`): parallel isolated CoW attempts with deterministic winner promotion and loser garbage collection.
4. **Active AST Blast-Radius Hook (Graphify):**
   - Push-based AST diffing on `R2` file writes to compute 1-hop and 2-hop downstream callers and dynamically pin affected test files to the task Verification Plan.
5. **Supply-Chain Malware Gate (NVIDIA SkillSpector):**
   - Directly wired to upstream `vendor/skillspector` (v2.12.0) across 64+ agent-native vulnerability patterns, OSV advisories, and local semantic alignment.
6. **Controlled Self-Improvement (Google Research Harness²):**
   - Vendored in `vendor/harness2` (`Propose–Probe–Compose` refinement loop), protected by an immutable revision registry, safety allow-list, and one-step rollback.

---

## 2. Baseline Status vs. Finished Product Gaps

| Capability | Baseline Status (Current) | Finished Product Target |
|---|---|---|
| **Ledger & Call Journal** | SQLite WAL with 16 tables, hash chaining, and 0ms memoization | Async PostgreSQL 16+ pool with `SKIP LOCKED`, Alembic migrations |
| **CoW Workspace** | Native macOS APFS clones + portable fallback | Added Linux Desktop OverlayFS/Btrfs provider + container sandbox mounts |
| **Active Graphify** | Python AST indexer + 1-hop/2-hop dependency delta | Incremental dirty-file AST indexer + multi-language Graphify CLI adapter |
| **SkillSpector Gate** | Wired to upstream `vendor/skillspector` engine | Added live OSV.dev lookup & upstream CLI subprocess mode |
| **Model Pipeline** | Structured log compressor & privacy router rules | Real Apple Silicon MLX local inference (`mlx-lm`) + Cloud API connectors |
| **Tool Gateway** | Built-in file/test/git tools & Code Mode memoization | Full Model Context Protocol (MCP) client adapter for external tools |
| **Speculative Branches** | Parallel local thread branches with verification | Multi-turn repair loops & worker-assigned speculative branches |
| **Distributed Workers** | Fencing leases & worker table schema | Outbound worker daemon over mTLS / WireGuard / Tailscale |
| **Control Surface** | Local web dashboard + CLI | Production ASGI runner, token cost charts, outbound approval webhooks |

---

## 3. Strict Verification & Proof Standards

Every implementation task must produce empirical verification output before marking completion:
- **Ledger/DB:** SQL transaction logs, lease fencing CAS rejection tests, hash chain integrity verifications.
- **CoW & Durability:** Crash-injection tests proving 0ms call journal replay and clean rollback of dirty trees.
- **Security & Skills:** Adversarial skill test cases quarantined with 100% recall.
- **Proof-Carrying Artifacts:** Verifiable JSON manifests with diff SHA-256, tree hashes, test hashes, and signed event hashes.

---

## 4. Milestone M7: Polyglot LSP Gateway & Real-Time Hands-Free Voice

### 4.1 Polyglot Language Server Protocol (LSP) Gateway
- **Major Language Support:** Auto-detection and stdio JSON-RPC 2.0 lifecycle management for:
  - Python: `pyright`, `pylsp`, `ruff`
  - TypeScript / JavaScript: `typescript-language-server`, `vtsls`
  - Go: `gopls`
  - Rust: `rust-analyzer`
  - C / C++: `clangd`
  - Java: `jdtls`
- **LSP Capabilities & Tools:**
  - `lsp_diagnostics`: Aggregate compiler syntax errors, type mismatches, and linter warnings across dirty and open files.
  - `lsp_definition`: Query symbol definition URI, line, and column coordinates.
  - `lsp_references`: Discover all call sites and symbol usages across the codebase.
  - `lsp_hover`: Retrieve type signatures and markdown docstrings.
- **Autonomous Repair Integration:** Pre-flight compiler verification after CoW patch application before test execution to prune broken repairs early.
- **CLI & TUI Ergonomics:** `hex lsp [check|diagnostics|status]` and `/lsp` TUI slash command.

### 4.2 Real-Time Hands-Free Voice & Speech Pair-Programming
- **Speech Capture & STT:** Audio capture with Voice Activity Detection (VAD) and speech-to-text transcription (macOS native speech recognition / local Whisper / Gemini Live streaming).
- **Speech Synthesis (TTS):** Low-latency audio playback for agent responses (macOS native `say` / neural TTS) with mute toggle.
- **Hands-Free Console Integration:**
  - `/voice [on|off]` TUI toggle with visible microphone status indicators.
  - `hex chat --voice` and `hex voice` standalone voice console.
  - Push-to-talk (`Space` hold) and hands-free conversational loop.
  - Health check in `hex doctor` auditing microphone and audio playback availability.

---

## 5. Milestone M8: Deep Upstream Vendor Integration

### 5.1 Google Research Harness² Deep Wiring (Phase 18)
- **Experience Library Integration (`harness2.core.library`):**
  - Wire `Library` into `HarnessOrchestrator` to record structured experience (`INDEX.md` and `EXPERIENCE.md`) containing cue, lesson, and empirical evidence upon successful task completion.
  - Automatically query domain library before executing related tasks to inject proven lessons into agent context.
- **Orchestrator Benchmark Adapter (`harness2.core.spec.BenchmarkAdapter`):**
  - Implement `OrchestratorBenchmarkAdapter` satisfying `BenchmarkAdapter` protocol to run candidate harness evaluations through `HarnessOrchestrator` instead of `DemoAdapter`.
  - Capture trajectories in standard `[ACTION]/[OBSERVATION]/[ERROR]` format and verify deliverables.
- **Propose-Probe-Compose Sweeps:**
  - Wire `harness2.core.recursion` (`run_parallel`, `run_sequential`) and `harness2.core.selection` to a dedicated CLI subcommand (`hex harness2-eval`).

### 5.2 NVIDIA SkillSpector Behavioral AST & Live OSV Scanner (Phase 19)
- **AST-Level Dangerous Call Detection:**
  - Wire `skillspector.python_ast` and `skillspector.nodes.analyzers.behavioral_ast` into `SkillSpectorScanner` to parse Python code in skill bundles and detect eval/exec, dynamic imports, subprocess executions, deserialization attacks, and network socket calls.
- **Live OSV.dev Batch Vulnerability Lookups:**
  - Wire `skillspector.nodes.analyzers.osv_client` to perform live batch queries against `https://api.osv.dev/v1/querybatch` for skill package manifests with automatic offline cache fallback.
- **YARA & Heavy Dependency Resilience:**
  - Evaluate curated rules from `vendor/skillspector/src/skillspector/yara_rules/` when `yara-python` is present, while preserving fast regex scanning as the zero-dependency fallback.

### 5.3 Graphify Upstream CLI & Tree-Sitter Adapter (Phase 20)
- **Upstream CLI Runner (`GraphifyUpstreamRunner`):**
  - Implement a pluggable runner executing `graphify run` via subprocess or ingesting `graphify-out/graph.json` from upstream tree-sitter extractions.
- **Polyglot Graph Unification:**
  - Merge upstream tree-sitter extracted nodes and edges into `ActiveGraphifyAdapter`, combining 33+ language AST extraction with our 0ms active blast-radius and architecture analysis.

---

## 6. Milestone M10: Finished Product Parity & Execution Excellence (Phases 24–27)

### 6.1 Continuous Multi-Turn ReAct Tool Loop (Phase 24)
- **Autonomous Tool-Calling Engine (`src/harness_core/react_loop.py`):**
  - Implements a continuous iterative ReAct loop (up to 50 configurable turns per session).
  - Handles model tool calls, executes through `ToolGateway` (respecting R0–R4 risk gating), appends tool results, and re-prompts until task completion or explicit final response.
  - Deeply integrated into `HarnessOrchestrator.run_agent_loop` and `HexConsole` for autonomous execution without human step-prompting.

### 6.2 4-Phase Context Compactor & Prompt Cache Locking (Phase 25)
- **Multi-Stage Compactor (`src/harness_core/context_compressor.py`):**
  - 4-phase strategy:
    1. *Stale Tool Output Demotion*: Truncate bulky terminal/file outputs older than $N$ turns while preserving status/error headers.
    2. *Atomic Tool-Pair Protection*: Never split a `<tool_call>` / `<tool_result>` pair across compaction boundaries.
    3. *Middle-Turn Summarization*: Auxiliary local MLX/deterministic summarizer replaces bloated turns with a concise summary block.
    4. *Prompt Cache Pinning*: Guarantees static system instructions, tool schemas, and environment headers occupy an immutable prefix to achieve >90% KV prompt cache hit rate across Anthropic, Gemini, and OpenAI.

### 6.3 PTY Background Process Supervisor & Stream Registry (Phase 26)
- **Process Supervisor (`src/harness_core/process_registry.py`):**
  - Manages long-running background processes (dev servers, file watchers, test daemons, build pipelines) with PTY allocation.
  - Circular stream buffers for stdout/stderr with non-blocking tail reads.
  - Gateway tools: `start_process`, `read_terminal`, `kill_process`, `list_processes`.

### 6.4 Persistent REPL Code Kernel (Phase 27)
- **In-Memory Code Kernel (`src/harness_core/code_kernel.py`):**
  - Persistent interactive Python session maintaining variables, imported modules, and runtime state across turns inside the task CoW workspace.
  - Captures stdout, stderr, and rich evaluation results.
  - Gateway tool: `code_kernel` with timeout protection and memory fences.




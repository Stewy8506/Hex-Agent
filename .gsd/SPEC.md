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

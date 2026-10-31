# M0 Architecture Decision Records (ADRs)

**Date:** 2026-10-31  
**Status:** Baseline Approved (M0 Design Freeze)

---

## ADR-001: Task Ledger, Durable Call Journal, and DBOS vs. Native SQL Queue

### Context
Stage 1 requires an authoritative task ledger, append-only event stream (`task_events`), expiring worker leases with monotonically increasing fencing tokens (`leases`), and a call-level memoization journal (`tool_calls`) that replays completed tool invocations in 0 ms upon process or script crash (`FR-010`–`FR-015`). During M0, we evaluated embedding DBOS/Restate versus implementing explicit schema ownership with a portable SQL ledger.

### Decision
1. **Authoritative Schema Ownership:** Keep the 16-table schema (`tasks`, `task_events`, `attempts`, `plans`, `checkpoints`, `tool_calls`, `approvals`, `artifacts`, `proof_bundles`, `skills`, `skill_scans`, `workers`, `leases`, `memories`, `harness_revisions`, `evaluations`) owned directly by the harness rather than delegating table layout to an external framework.
2. **Dual-Driver Ledger Adapter:** Provide a unified `LedgerStore` protocol supporting PostgreSQL 16+ (`SELECT ... FOR UPDATE SKIP LOCKED` + compare-and-swap on `(version, fencing_token)`) for production multi-worker deployment, alongside a zero-dependency transactional SQLite WAL backend for instant single-machine bootstrap when PostgreSQL/Docker is offline.
3. **Explicit Call-Level Gateway Journaling:** Implement call memoization directly in the `tool_calls` table keyed by `(task_id, step_id, tool_id, canonical_args_hash, idempotency_key)` so that both direct tool calls and multi-call Code Mode scripts deterministic-replay sealed results in 0 ms without external runtime lock-in.

---

## ADR-002: Copy-on-Write (CoW) Workspace Snapshot Provider

### Context
Every `R2` coding or refactoring task requires millisecond workspace branching and rollback (`FR-012`, `FR-012a`) so failed steps, cancellations, or parallel speculative branches (`FR-021a`) never leave dirty build trees.

### Decision
Implement `WorkspaceSnapshotProvider` with automatic platform detection:
- **macOS (MacBook Air M4):** Uses native APFS clones (`cp -c -R`) for zero-copy, millisecond directory snapshots, paired with deterministic SHA-256 Merkle tree hashing (`workspace_tree_sha`) excluding transient `.git`/`.venv`/`__pycache__` noise.
- **Linux (Desktop/Server):** Uses OverlayFS / Btrfs subvolumes where mounted, with a portable hardlink/copy + `tar.zst` CAS delta archive fallback for cross-machine migration (`FR-051`).

---

## ADR-003: NVIDIA SkillSpector Supply-Chain Gate & Two-Tier M4 Routing

### Context
All imported or agent-proposed `SKILL.md` bundles (including Stage 5 Harness² proposals) must pass static, dependency, and semantic alignment scans before activation (`FR-039e`), and verbose tool logs must be compressed locally before cloud escalation (`FR-045a`).

### Decision
1. **SkillSpector Pipeline (`SkillSecurityScanner`):**
   - **Layer 1 (Static & Structural):** Scans across agent-native vulnerability rules (bidirectional/zero-width Unicode steganography, hidden HTML/Markdown comment prompt injections, base64/hex payload execution, symlink/path-traversal escapes, undeclared tool/network usage, and MCP poisoning).
   - **Layer 2 (Dependency CVE Check):** Parses bundled scripts/manifests and checks against OSV vulnerability signatures.
   - **Layer 3 (Tier-1 Local M4 Semantic Alignment):** Compares frontmatter `purpose` and `risk_class` (`R0`–`R4`) against actual instructions and scripts.
   - **Deterministic Gate:** `Critical`/`High` (or risk score $\ge 60$) $\rightarrow$ `quarantined` (`rejected`); `Medium` ($25 \le \text{score} < 60$) $\rightarrow$ requires human sign-off in `approvals`; `Low`/`Info` ($< 25$) $\rightarrow$ `active`/`candidate`.

---

## ADR-004: Harness² Integration Behind Controlled Self-Improvement Gate

### Context
Google Research's `harness2` (`vendor/harness2`) implements task-level contrastive **Propose–Probe–Compose** (`harness2.core.recursion`) and a per-domain **Experience Library** (`harness2.core.library`) over an 8-component editable surface (`harness2.core.tree`).

### Decision
Integrate `vendor/harness2` via `Harness2RefinementAdapter`:
- Enforce an explicit allow-list on editable components (prompts, tool guidance, skills, scripts) while strictly blocking edits to policy, approval thresholds, credentials, or evaluation rubrics (`FR-061`).
- Route every candidate tree proposed or composed by `harness2` through `SkillSpector` (`FR-039e`) and deterministic verification before recording an immutable `harness_revisions` bundle (`FR-060`, `FR-068`).

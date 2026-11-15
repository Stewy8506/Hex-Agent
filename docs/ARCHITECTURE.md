# HEX System Architecture

This document provides a detailed overview of the core architectural subsystems powering **HEX Agent**.

---

## 1. High-Level Architecture Overview

HEX is structured into four primary layers:

```text
┌─────────────────────────────────────────────────────────────┐
│                 Presentation & Interaction Layer            │
│    Native Rust TUI (hex-tui) · Web Control Surface · ACP   │
├─────────────────────────────────────────────────────────────┤
│                 Autonomous Orchestration Core               │
│      ReAct Loop · Task Scheduler · Two-Tier Compression     │
├─────────────────────────────────────────────────────────────┤
│                 Safety & Isolation Sandboxing               │
│     Copy-on-Write (CoW) · SkillSpector · Ring Buffer PTY    │
├─────────────────────────────────────────────────────────────┤
│                 Storage & Ledger Engine                     │
│    PostgreSQL 16 · SQLite WAL · Cryptographic SHA-256 Chain │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Copy-on-Write (CoW) Filesystem Isolation

Rather than editing source code in place, HEX spawns an ephemeral sandbox for every task or subagent attempt:

- **macOS (APFS)**: Uses native `clonefile()` system calls to reflink directory trees in **<10ms**. Blocks share storage until modified.
- **Linux**: Uses kernel-level `OverlayFS` mounts (`lowerdir` = repository root, `upperdir` = ephemeral scratchpad).
- **Windows / Fallback**: Uses hard-link mirrors and temporary directory staging.

### Sandbox Lifecycle:
1. **Clone**: An instantaneous sandbox is initialized under `.hex/sandboxes/<task_id>`.
2. **Execute**: The agent modifies files, installs temporary dependencies, and executes your test runner.
3. **Verify**: The orchestrator inspects the return code and test suite output.
4. **Commit or Rollback**:
   - If tests **pass**: The file diff is atomically applied to your active branch.
   - If tests **fail**: The sandbox is discarded with **0ms** host pollution.

---

## 3. Storage & Cryptographic Event Ledger

HEX utilizes a dual-driver relational architecture designed for enterprise-grade durability and concurrency:

### Dual-Driver DB (`harness_core.db.LedgerStore`)
- **PostgreSQL 16+**: High-throughput distributed mode with `asyncpg` connection pooling and `SELECT ... FOR UPDATE SKIP LOCKED` task queue claiming.
- **SQLite 3.35+ (WAL)**: Zero-config local embedded mode with Write-Ahead Logging and automatic checkpointing.
- **Alembic Schema Parity**: All database migrations are fully reversible and tracked via Alembic revisions.

### Cryptographic Event Chaining
Every lifecycle state change (task submission, tool execution, policy approval, completion) is recorded in `task_events` as an immutable cryptographic hash chain:

$$\text{hash}_k = \text{SHA-256}\left(\text{hash}_{k-1} \parallel \text{task\_id} \parallel \text{seq\_no} \parallel \text{event\_type} \parallel \text{payload}\right)$$

Any tampering with stored event logs breaks chain continuity and is caught by `store.verify_chain_integrity()` and `scripts/verify_event_chain.py`.

---

## 4. AST Code Intelligence (Graphify)

HEX incorporates an active code intelligence engine powered by tree-sitter:

- **Polyglot Parsing**: Indexes Python, TypeScript, JavaScript, Rust, Go, and C/C++.
- **Knowledge Graph**: Maintains a directed graph of classes, functions, interfaces, and cross-file import relationships.
- **Blast-Radius Analysis**: Before editing a function, HEX queries `graphify affected <symbol>` to identify all downstream callers and tests that must be validated.

---

## 5. Two-Tier Context Compression

To operate within bounded model token budgets without losing critical failure context:

1. **Deterministic Traceback Compression**:
   - Strips redundant framework frames (e.g. standard library internals, pytest test runner plumbing).
   - Retains failing assertions, variable representations, and exact diffs.
   - Achieves a **75% to 88% token reduction** on compiler outputs and test traces.
2. **Shadow Summary Deduplication**:
   - In extended 30+ turn sessions, older compaction summaries are parsed and consolidated into a single unified summary block, eliminating stacked duplicate context.

---

## 6. Next-Gen Cognition & Self-Evolution Architecture

HEX fundamentally redesigns agent self-learning and memory away from static flat text buffers into a multi-tiered, active cognitive loop designed to eliminate prompt bloat, preserve KV-cache prefix stability, and turn learned experience into deterministic regression tests.

```text
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                    5-SLOT SYSTEM PROMPT ARCHITECTURE                        │
  │                                                                             │
  │  Slot #1: SOUL.md Identity (Global ~/.hex/SOUL.md or Repo .hex/SOUL.md)    │
  │           Capped token budget (~300–500 tokens). Defines tone and persona. │
  │                                                                             │
  │  Slot #2: Dynamic Mode Modulator                                            │
  │           Punchy 3–4 line constraint overrides: plan / agent / code.        │
  │                                                                             │
  │  Slot #3: Environment & Tool Capabilities                                   │
  │           Tool schemas, R0–R4 safety tiers, and platform execution context. │
  │                                                                             │
  │  Slot #4: Curated Memory Snapshot (Fenced in <memory-context>)              │
  │           Frozen Turn-0 prefix snapshot: USER.md + MEMORY.md + GUARDRAILS.md│
  │           Guarantees 100% byte-for-byte prefix invariance (KV-Cache freeze).│
  │                                                                             │
  │  Slot #5: Dynamic Context, Active Files & Skills                            │
  │           Task intake, selected SKILL.md bundles, and dynamic execution.    │
  └─────────────────────────────────────────────────────────────────────────────┘
```

### The 6 Architectural Pillars:

#### 1. AST-Scoped Memory Injection (JIT Graph Retrieval)
Instead of stuffing every project rule into a single 2,500-character `MEMORY.md` file, HEX partitions project memory into:
- **Global Repo Memory**: Universal architectural facts kept in Slot #4.
- **Node-Bound Memory**: File- and function-specific gotchas stored in SQLite/Graphify and retrieved Just-In-Time.
- **Mechanism**: When the agent reads `src/orchestrator.py` or inspects a symbol, HEX automatically pulls any memory notes, invariants, or past bug post-mortems attached to that symbol or its 1-hop callers/callees. These are injected directly into the **`read_file` tool observation output**—not the system prompt.
- **Benefit**: The system prompt stays 100% KV-cache frozen, while code-specific gotchas appear dynamically right when the model inspects or edits that file.

#### 2. Negative Constraint Memory ("Anti-Patterns & Guardrails")
Standard agent memories store positive facts (*"Use cargo nextest"*). Coding agents fail repeatedly because they forget what already failed.
- **Mechanism**: HEX maintains `.hex/memories/GUARDRAILS.md`, populated automatically whenever a tool call or build command fails twice with the same signature, or when a user rejects a diff.
- **Format**: `[AVOID] Do not use tokio::sync::Mutex across .await in router.rs — causes Send bound compile errors.`
- **Benefit**: Negative invariants framed as explicit `[AVOID]` constraints right before execution dramatically cut repeat hallucination loops, especially on 8B–9B local models.

#### 3. Zettelkasten-Style Linked Memory & Contradiction Resolution
Flat bullet lists rot because new facts sit next to old facts that contradict them.
- **Mechanism**: Memory writes follow the A-MEM / CRUD controller pattern (`ADD`, `UPDATE`, `DELETE`, `NOOP`). Before writing a new bullet to `MEMORY.md`, HEX executes a lightweight fast-path check against existing entries using FTS5 and tag overlap. If a new note contradicts an old one (e.g., *"Migrated database from MongoDB to SQLite"*), HEX executes an `UPDATE` or `DELETE` on the obsolete note rather than appending a conflicting line.
- **Benefit**: Notes are linked with lightweight wiki-style tags (`[[auth-flow]]`, `[[webrtc-room]]`), so retrieving one pulls its immediate conceptual neighborhood without fact pollution.

#### 4. Confidence Decay & "Promotion / Demotion" Lifecycle
Not every learned insight deserves permanent real estate in your frozen system prompt.
- **Mechanism**: Every memory entry carries an active hit counter and last-referenced timestamp.
  - **Promotion**: If a reflection in `.hex/reflections/LESSONS.md` gets retrieved and helps solve a task across 3 separate sessions, HEX promotes it into `MEMORY.md` (or compiles it into a reusable `SKILL.md`).
  - **Demotion (Archive)**: If a bullet in `MEMORY.md` goes unreferenced for 30 days—or its linked AST hash goes stale—it automatically demotes out of the active prompt into cold SQLite FTS5 archival storage.
- **Benefit**: Active prompt context remains lean and relevant; old knowledge is preserved in cold search storage without bloating prompt tokens.

#### 5. Deterministic Verification Hooks Attached to Skills
Hermes `SKILL.md` files are purely instructional prose. In HEX, a learned skill carries executable verification assertions.
- **Mechanism**: When `/learn` compiles a `SKILL.md` bundle (e.g. *"How to add a new RPC endpoint"*), it allows an optional `verify:` frontmatter block specifying a shell command, AST invariant, or test filter (e.g. `cargo test rpc_schema_sync`).
- **Benefit**: Whenever HEX invokes that skill in the future, `SkillExecutionEngine` automatically runs the verification check with timeout protection after the agent finishes editing, turning passive documentation into an executable regression test.

#### 6. Cross-Repo "Developer DNA" Sync (`USER.md` + Universal Skills)
Separates machine-wide engineering habits from repository-specific trivia:
- **`~/.hex/USER.md` + `~/.hex/skills/`**: Stores personal stack preferences (e.g. Rust error handling style, Neo-brutalist UI tokens, preferred CLI flags) and universal workflows across every project on the machine.
- **`.hex/MEMORY.md` + `.hex/skills/`**: Tracked in git (or `.gitignored` per user choice) so project-specific architecture rules travel with the repository and can even be shared across teams.

---

## 7. Supply-Chain Security & Hardware Protection

### NVIDIA SkillSpector AST Safety Gate
Before any synthesized or third-party skill bundle is written to disk, it is staged in an isolated scratchpad and scanned by the upstream NVIDIA SkillSpector engine (`vendor/skillspector`):
- **AST Behavioral Analysis**: Runs Python AST taint checks on bundled scripts to detect socket exfiltration, shell spawns, and unauthorized reflection.
- **Prompt Injection & Steganography**: Scans for zero-width Unicode characters, bidirectional overrides, and instruction override markers.
- **OSV CVE Lookup**: Scans bundled dependencies against the live OSV advisory database.
- **Deterministic Quarantine**: Any skill scoring High or Critical risk (`gate_decision == "QUARANTINE"`) is rejected and quarantined to `.hex/quarantine/<name>/` with a forensic `audit.json`.

### Hardware Gating (`auto_reflect = "cloud_only" | "idle" | "off"`)
Spawning background reflection subagents on local hardware (e.g. Apple Silicon M-series or Ollama) competes for unified memory bandwidth and stalls active inference. HEX enforces strict hardware gating:
- When running on local hardware under `cloud_only` or `off`, background LLM reflection is bypassed.
- Candidate friction lessons are safely queued to `.hex/reflections/pending_reflections.json` for offline processing.

---

## 8. Storage Lifecycle, Scoped TTL Retention & Garbage Collection

Because HEX operates as an ACID-style transactional flight recorder, task execution continuously produces Copy-on-Write snapshots, CAS delta tarballs, and proof bundles. To prevent unbounded disk growth while preserving forensic auditability, HEX implements an automated, scoped retention engine (`src/harness_core/gc.py`):

```text
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                           GARBAGE COLLECTION SWEEP                          │
  │                                                                             │
  │   [ active_workspaces/ ]   [ cow_snapshots/ ]            [ cas/ ]           │
  │      Ephemeral Clones         Step Snapshots         Deduplicated Tarballs  │
  │             │                       │                         │             │
  │             ▼                       ▼                         ▼             │
  │     Query ledger.db        Query ledger.db            Mark-and-Sweep:       │
  │   status in terminal &   status in terminal &    Reference set against all  │
  │   updated_at > 24h &     updated_at > 24h &      active/pinned/recent tasks │
  │     pinned == 0            pinned == 0                        │             │
  │             │                       │                         ▼             │
  │             ▼                       ▼                Delete unreferenced    │
  │       Safe Deletion           Safe Deletion          blobs older than 7d    │
  │             │                       │                         │             │
  │             └───────────────────────┼─────────────────────────┘             │
  │                                     ▼                                       │
  │                      PRAGMA incremental_vacuum;                             │
  │               (Non-blocking; full VACUUM only via hex gc)                   │
  └─────────────────────────────────────────────────────────────────────────────┘
```

### Architectural Guarantees:
1. **Active & Pinned Task Immunity**: Tasks with state `RUNNING`, `PAUSED`, `PENDING`, or `pinned = True` are strictly exempt from TTL deletion.
2. **Reference-Counted CAS Mark-and-Sweep**: Blobs in `cas/` and `cas_artifacts/` are never deleted by mtime alone. Instead, a mark-and-sweep algorithm computes all SHA-256 hashes referenced across all active, pinned, or recent (< 7 days) tasks and proofs, pruning only unreferenced blobs older than `7d`.
3. **Orphan Directory Race Guard**: Ephemeral workspace directories created before database transaction commit are protected by an orphan guard threshold (`mtime > 1h` or `workspace_ttl`), preventing races with in-flight worker tasks.
4. **Non-Blocking Concurrency Policy**: Automated sweeps executed during TUI startup or worker idle loops execute `PRAGMA incremental_vacuum;` to reclaim unused database pages without acquiring table locks or blocking concurrent workers. Heavy exclusive `VACUUM` is strictly restricted to explicit `hex gc --full-vacuum` invocations when no worker leases exist.
5. **Deterministic Delta Safety Filter**: The `export_delta_archive` process applies a recursive `_tar_filter` that excludes `IGNORE_DIRS` (`.hex`, `.git`, `.venv`), `.DS_Store`, and active SQLite WAL lockfiles (`*.db-wal`, `*.db-shm`), preventing file-system race conditions during snapshot bundling.

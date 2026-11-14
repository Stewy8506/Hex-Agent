# HEX System Architecture

This document provides a detailed overview of the core architectural subsystems powering the **HEX Autonomous AI Workbench**.

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

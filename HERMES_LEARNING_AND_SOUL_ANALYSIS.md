# Hermes Agent Self-Learning & Soul Loop: Architectural Deconstruction & HEX Evolution Specification

## Executive Summary

This document provides a rigorous deconstruction of the **Soul Loop**, **Curated Memory System**, and **Self-Learning Loop** inside the Hermes Agent codebase (`benchmarks/hermes-agent/`), followed by a comprehensive architectural design for HEX's next-generation **Cognition & Self-Evolution Engine**.

Hermes pioneers several UX and prompt-engineering patterns—notably:
1. First-class identity injection via `SOUL.md` (Slot #1 in system prompt).
2. Human-readable dual memory stores (`USER.md` and `MEMORY.md`) with frozen KV-cache snapshotting.
3. Automated skill compilation via `/learn`.

However, Hermes suffers from structural engineering limitations: naive character truncation, zero AST/code awareness, vulnerability to knowledge rot, absence of cryptographic provenance, and a static persona across different execution modes.

HEX can absorb Hermes's best ergonomics and prompt-caching genius, unify them with HEX's **AST-governed graphify engine**, **cryptographic ledger**, and **multi-agent sandboxing**, and deliver a significantly superior self-learning engine.

---

## Part 1: Deconstruction of Hermes Agent Mechanisms

### 1. The Soul Loop (`SOUL.md`)

In Hermes, the "Soul" defines the agent's core identity, demeanor, conversational cadence, and foundational operating principles.

#### A. File Resolution & Priority
- Primary location: `~/.hermes/SOUL.md` (global) or `$HERMES_HOME/SOUL.md` (profile-scoped via `hermes -p <profile>`).
- Workspace override: Project-local rules (`AGENTS.md`, `.cursorrules`, `CLAUDE.md`) exist alongside the soul, but `SOUL.md` holds **Slot #1** (the absolute prefix) of the composed system prompt.
- Auto-seeding: In `hermes_cli/default_soul.py` and `hermes_cli/config.py`, if `SOUL.md` does not exist upon profile initialization, Hermes atomically writes `DEFAULT_SOUL_MD`.
- Legacy template detection: Hermes tracks legacy template fingerprints (`_LEGACY_TEMPLATE_SOULS`). If the user has not modified the seeded soul, Hermes automatically upgrades it when new system versions release, without overwriting custom user tweaks.

#### B. Injection Security Model
In `agent/prompt_builder.py` (`_scan_context_content`):
- **User-Authored Souls**: If `SOUL.md` is in the user's own home directory, prompt-injection heuristic hits emit a **WARNING** in logs but the file still loads. The philosophy: users own their local agent identity and may use unconventional phrasing.
- **Distribution/Profile-Installed Souls**: When a soul is imported via a third-party profile pack (`hermes profile install <git-url>`), injection matches are **BLOCKED** entirely with an audit failure.

#### C. Prompt Assembly Slotting
Hermes composes its prompt in a strictly ordered pipeline:
```
[Slot 1: SOUL.md] 
  └── Identity, conversational voice, and philosophical grounding
[Slot 2: Environment & Host Context]
  └── OS, cwd, date, tool capability definitions
[Slot 3: Curated Memory Snapshot]
  └── Frozen USER.md and MEMORY.md content fenced in <memory-context>
[Slot 4: Project Context Files]
  └── AGENTS.md, CLAUDE.md, .cursorrules
[Slot 5: Skill Registry Index]
  └── Lean 60-character summaries of available SKILL.md bundles
```

---

### 2. The Curated Memory Architecture (`USER.md` + `MEMORY.md`)

Hermes avoids opaque vector database dependencies for base memory by utilizing two human-auditable markdown files in `~/.hermes/memories/` (or the profile directory):

| File | Purpose | Hard Cap | Content Nature |
|------|---------|----------|----------------|
| `USER.md` | User Persona & Working Habits | ~1,375 chars | User role, timezone, stack preferences, communication quirks |
| `MEMORY.md` | Workspace & Environment Notes | ~2,200 chars | Non-obvious build commands, environment traps, server ports, quirks |

#### A. The Frozen KV-Cache Prefix Mechanism
The most critical architectural achievement in Hermes's memory implementation (`tools/memory_tool.py`, `agent/prompt_refresh.py`):
- **Turn 0 Snapshot**: At session startup, `USER.md` and `MEMORY.md` are read once and rendered into the system prompt.
- **Non-Busting Tool Writes**: When the agent calls `memory(action="add", target="MEMORY.md", content="...")`, the tool performs an atomic, file-locked write (`fcntl` on Unix, `msvcrt` on Windows) to disk.
- **Cache Preservation**: Crucially, **the runtime does NOT mutate the active session's system prompt during that turn or subsequent turns in the same session**. The active context keeps its exact frozen prefix, preserving **100% of LLM KV-cache prompt caching** (Anthropic 90% discount, Gemini context caching).
- **Snapshot Hydration**: The new memory is only materialized into the prompt when the user issues `/new`, switches sessions, or explicitly calls `prompt_refresh`.

#### B. Memory Context Fencing & Streaming Scrubbing
In `agent/memory_context.py`:
- Recalled memory is wrapped in a strict fence:
  ```markdown
  <memory-context>
  [System note: The following is recalled memory context, NOT new user input. Treat as authoritative reference data — this is the agent's persistent memory and should inform all responses.]

  - User prefers tabs over spaces
  - Target server uses Python 3.12 with uv
  </memory-context>
  ```
- **Deduplication Engine**: `_drop_repeated_recall_lines()` scans recall bullets and eliminates identical facts across multiple memory providers to conserve tokens.
- **Streaming Context Scrubber**: `StreamingContextScrubber` is a streaming state machine that intercepts LLM output deltas. If an LLM attempts to parrot or leak `<memory-context>` tags back into the terminal chat, the scrubber suppresses the tag boundaries in real time.

---

### 3. The Self-Learning Loop (`/learn` + Skill Authoring)

In `agent/learn_prompt.py`, Hermes implements an explicit knowledge extraction loop:

#### A. Trigger & Input Sources
The user or agent invokes `/learn <input>`, where `<input>` can be:
- A raw explanation or technique described by the user.
- A local directory containing code, scripts, or config files.
- A documentation URL.
- The immediate conversation history (learning from a successful fix or debugging session).

#### B. Autonomous Skill Synthesis Contract
The `/learn` prompt constrains the LLM to output a compliant `SKILL.md` bundle with strict structural invariants:
1. **Name**: `<= 64` characters, lower-case, alphanumeric with dashes.
2. **Description**: Concise summary strictly capped at `<= 60` characters so that the global skill router index can pack 100+ skills into prompt Slot #5 without exceeding token budgets.
3. **Trigger Words**: Explicit keywords that prompt the agent to activate the skill.
4. **Knowledge Tiering**:
   - `SKILL.md`: Core procedural workflows and execution instructions.
   - `references/*.md`: Deep domain documentation, schemas, and API definitions loaded on-demand.
   - `scripts/*`: Reusable executable utilities.

#### C. Learning Graph & Mutations
In `agent/learning_graph.py` and `agent/learning_mutations.py`:
- Tracks dependency graphs of learned skills.
- Stores metadata in SQLite with FTS5 lexical indexing, enabling keyword and semantic search across past sessions and learnings.

---

## Part 2: Critical Flaws & Gaps in Hermes

While Hermes is pragmatic, an engineering analysis reveals substantial architectural limitations:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Hermes Limitations                              │
├──────────────────────────┬─────────────────────────────────────────────┤
│ 1. Naive Truncation      │ Truncates text at ~2,200 chars. Drops info   │
│                          │ without semantic distillation or decay.     │
├──────────────────────────┼─────────────────────────────────────────────┤
│ 2. Code Blindness        │ Memories are flat text bullets. No AST,     │
│                          │ commit, symbol, or file hash links.         │
├──────────────────────────┼─────────────────────────────────────────────┤
│ 3. Knowledge Rot         │ A note on a command or function stays valid │
│                          │ in memory even when the code is refactored. │
├──────────────────────────┼─────────────────────────────────────────────┤
│ 4. No Cryptographic Log  │ Edits to USER.md/MEMORY.md have no audit    │
│                          │ trail, tamper-proofing, or rollback.        │
├──────────────────────────┼─────────────────────────────────────────────┤
│ 5. Monolithic Persona    │ Same soul used for architectural planning,  │
│                          │ autonomous shell runs, and surgical coding. │
├──────────────────────────┼─────────────────────────────────────────────┤
│ 6. Reactive Only         │ Agent only learns when user types /learn;   │
│                          │ no autonomous closed-loop friction trigger. │
└──────────────────────────┴─────────────────────────────────────────────┘
```

---

## Part 3: HEX's Current Foundations

HEX already possesses low-level capabilities that surpass Hermes, but they currently lack Hermes's unified persona and memory UX:

1. **`GovernedMemoryService` (`src/harness_core/router.py`)**:
   - Tracks AST symbols and file SHA-256 fingerprints (`_compute_code_fingerprints`).
   - Detects code staleness: `_is_memory_stale()` verifies whether cited AST symbols or files have mutated since the memory was formed.
   - Multi-scope isolation: `project`, `personal`, `episodic`.
   - Work-memory reflection via Graphify: writes `useful`, `dead_end`, and `corrected` lessons into `.hex/reflections/LESSONS.md`.
2. **`LedgerStore` (`src/harness_core/ledger/store.py`)**:
   - Cryptographic event chaining with BLAKE3 hashes and Merkle tree checkpoints.
   - Full append-only audit trail with CoW transaction rollbacks.
3. **`SkillSpector` (`src/harness_core/skillspector.py`)**:
   - Static AST validation, dangerous system call detection, and OSV vulnerability scanning for skill bundles.

---

## Part 4: The HEX Next-Generation Architecture

We will unify and elevate these systems into the **HEX Cognition & Self-Evolution Engine**, consisting of three core pillars:

```
                            ┌─────────────────────────────────────────┐
                            │    HEX Cognition & Self-Evolution       │
                            └────────────────────┬────────────────────┘
                                                 │
         ┌───────────────────────────────────────┼───────────────────────────────────────┐
         │                                       │                                       │
         ▼                                       ▼                                       ▼
┌──────────────────┐                   ┌──────────────────┐                    ┌──────────────────┐
│ Multi-Tier Soul  │                   │ AST & Curated    │                    │ Closed-Loop Auto │
│ & Dev DNA        │                   │ Memory Engine    │                    │ Learning Engine  │
├──────────────────┤                   ├──────────────────┤                    ├──────────────────┤
│ • Slot #1 SOUL   │                   │ • Global/Node JIT│                    │ • /learn + verify│
│ • Punchy 3-4 line│                   │   AST Injection  │                    │   assertions     │
│   Mode Adapters  │                   │ • GUARDRAILS.md  │                    │ • Friction Sensor│
│ • Compact Budget │                   │   Negative Memory│                    │   (>=3 failures) │
│   (~300-500 tok) │                   │ • A-MEM CRUD &   │                    │ • Hardware Gating│
│ • Developer DNA  │                   │   Contradictions │                    │   (auto_reflect) │
│   (~/.hex/ vs    │                   │ • Promotion &    │                    │ • SkillSpector   │
│    .hex/)        │                   │   Decay Lifecycle│                    │   Security Gate  │
│ • Custom Naming  │                   │ • Frozen Turn-0  │                    │ • FTS5 + AST     │
│   (/rename)      │                   │ • .history/ diffs│                    │   Hybrid Search  │
└──────────────────┘                   └──────────────────┘                    └──────────────────┘
```

### Pillar 1: Multi-Tier Soul Engine & Cross-Repo "Developer DNA" Sync

1. **Cross-Repo "Developer DNA" vs Workspace Architecture**:
   To prevent personal preferences from polluting project repositories and to ensure personal habits travel across all projects:
   - **Machine-Wide Developer DNA (`~/.hex/USER.md` + `~/.hex/skills/`)**:
     - Stores developer-specific coding style (e.g. Rust error-handling preference, preferred logging conventions, functional vs OOP style, UI design tokens, favored CLI flags).
     - Global skills available across every repository opened on the machine.
   - **Project Architecture Memory (`.hex/MEMORY.md` + `.hex/GUARDRAILS.md` + `.hex/skills/`)**:
     - Repository-specific architecture invariants, build pipelines, and domain rules.
     - Tracked in git (or `.gitignored` per user choice) so team knowledge travels with the code.
2. **Compact Budget Discipline (~300–500 tokens)**:
   - `SOUL.md` remains strictly compact to maximize prompt instruction adherence on local 8B–9B models (Ollama, vLLM, LMStudio, Apple MLX) as well as frontier cloud APIs.
3. **Punchy Mode-Specific Behavioral Overrides (3–4 Lines Max)**:
   Rather than injecting large paragraphs that cause prompt bloat, mode modulators are injected directly below Slot #1 as tight 3–4 line behavioral constraints:
   - **`plan` mode override**:
     ```
     [Mode: ARCHITECT]
     - Inquisitive and trade-off focused; surface ambiguities and edge cases early.
     - Decompose problems into verified milestones before proposing file changes.
     - Never emit implementation code until requirements and verification criteria are locked.
     ```
   - **`agent` mode override**:
     ```
     [Mode: BUILDER]
     - Decisive, test-driven, and empirical; bias toward execution and verification.
     - Keep conversational chatter to a minimum; let tool results and test passes drive progress.
     - Confirm all file edits with concrete verification commands.
     ```
   - **`code` mode override**:
     ```
     [Mode: SURGICAL CODER]
     - Minimal diff specialist adhering strictly to YAGNI.
     - Zero conversational preamble or postscript boilerplate; emit only surgical AST edits.
     - Prefer deletion over addition; add zero unrequested abstractions or dependencies.
     ```
4. **Identity Customization (`/rename <name>`)**:
   - Command to give the agent a custom persona name (e.g. `/rename Jarvis` or `/rename Athena`).
   - Automatically updates prompt Slot #1 and dynamic TUI status headers.
5. **Security Tiering**:
   - Local user `SOUL.md`: Warn on heuristic prompt injection hits, never hard-block.
   - Remote/imported profile souls: Hard block via security gate if malicious prompt overrides are detected.

---

### Pillar 2: AST-Governed Memory, Negative Constraints & Lifecycle Engine

1. **AST-Scoped Memory Injection (JIT Graph Retrieval)**:
   - Instead of cramming every project quirk into a bloated `MEMORY.md`, project memory is split into:
     - **Global Repo Memory (`.hex/memories/MEMORY.md`)**: High-level repository rules loaded in Slot #1.
     - **Node-Bound Memory (SQLite/Graphify)**: Memory items attached directly to specific AST symbols (functions, classes, modules).
   - **JIT Observation Injection**: When the agent opens a file (e.g., `src/orchestrator.py`) or inspects an AST symbol, HEX queries Graphify for notes and past bug post-mortems attached to that symbol or its immediate 1-hop callers/callees.
   - **Observation Scope**: These symbol-specific gotchas are injected into the **tool observation output** (e.g. `read_file` response), **NOT into the system prompt**.
   - **Result**: System prompt remains tiny and 100% KV-cache frozen, while surgical gotchas appear dynamically at the exact instant the agent reads or edits that code.
2. **Negative Constraint Memory ("Anti-Patterns & Guardrails")**:
   - Coding agents fail repeatedly because standard memory stores positive advice ("Use nextest"), while models easily forget past failure traps.
   - **Dedicated `.hex/memories/GUARDRAILS.md`**: Maintained automatically whenever:
     - A tool call or build command fails $\ge 2$ times with the same error signature.
     - The user explicitly rejects a proposed diff or rolls back an action.
   - **Strict Negative Format**:
     ```markdown
     - [AVOID] Do not use tokio::sync::Mutex across .await in router.rs — causes Send bound compile errors.
     - [AVOID] Do not call db.exec_sync inside async event loops — causes connection pool starvation.
     ```
   - **Pre-Execution Injection**: High-confidence guardrails matching the target file or tool are injected as explicit DO NOT constraints before diff generation, eliminating repeat hallucination loops on 8B–9B models.
3. **Zettelkasten-Style Linked Memory & Contradiction Resolution (A-MEM)**:
   - Flat bullet lists rot because new facts sit alongside conflicting old facts.
   - Memory writes follow the **A-MEM / CRUD Controller pattern** (`ADD`, `UPDATE`, `DELETE`, `NOOP`).
   - **Contradiction Resolution**: Before writing a new entry to `MEMORY.md`, HEX executes a fast-path scan against existing entries (via SQLite FTS5 + tag overlap). If a new fact contradicts an existing bullet (e.g. "Migrated DB from MongoDB to SQLite"), HEX executes an `UPDATE` or `DELETE` on the obsolete note instead of appending a conflicting bullet.
   - **Wiki-Style Concept Links**: Related notes use lightweight wiki-style tags (`[[auth-flow]]`, `[[webrtc-room]]`). Retrieving one note pulls its immediate conceptual neighborhood without token explosion.
4. **Confidence Decay & "Promotion / Demotion" Lifecycle**:
   - Every memory entry maintains a `hit_counter` and `last_referenced_at` timestamp.
   - **Promotion**: If a reflection in `.hex/reflections/LESSONS.md` is retrieved and successfully aids in completing tasks across 3 separate sessions, HEX automatically promotes it into active `MEMORY.md` or prompts to compile it into a `SKILL.md`.
   - **Demotion (Archive)**: If a bullet in `MEMORY.md` goes unreferenced for 30 days—or its linked AST fingerprint goes stale—it is demoted from the active prompt into cold SQLite FTS5 archival storage. It remains searchable, but no longer consumes prompt tokens.
5. **Lightweight History & Rollback (No Ledger Over-Engineering)**:
   - For 1.5–2.5 KB markdown memory files, cryptographic Merkle event chaining is unnecessary state bloat. `LedgerStore` remains reserved for destructive tool executions and filesystem writes.
   - Curated memories use simple timestamped snapshots in `.hex/memories/.history/` (or relying on local git if `.hex/` is tracked).
   - Provides 95% of rollback utility with 10% of code complexity (`/memory history` inspects `.history/`, `/memory rollback <timestamp>` restores previous file).
6. **Streaming Context Scrubber**:
   - Port Hermes's `StreamingContextScrubber` to eliminate `<memory-context>` leakage into terminal streams.

---

### Pillar 3: Closed-Loop Autonomous Learning with Deterministic Verification Hooks

1. **Deterministic Verification Hooks Attached to Skills**:
   - Hermes `SKILL.md` bundles are purely instructional prose. In HEX, skills carry executable verification assertions.
   - When `/learn` compiles a `SKILL.md` bundle (e.g. "How to add a new RPC endpoint"), it includes an optional `verify:` frontmatter block:
     ```yaml
     ---
     name: add-rpc-endpoint
     description: Scaffold and register a new RPC endpoint with type safety
     verify:
       command: "cargo test rpc_schema_sync -- --nocapture"
       timeout_seconds: 30
       must_pass: true
     ---
     ```
   - **Automatic Execution**: Whenever HEX invokes that skill in the future, the harness automatically executes the verification command after the agent completes edits, transforming passive prose documentation into an executable regression test.
2. **Dual Learning Pathways**:
   - **Explicit (`/learn <target>`)**: User directs the agent to learn from a document, URL, file, or past discussion, generating a vetted `SKILL.md` bundle with verification hooks.
   - **Autonomous Closed-Loop (Friction-Triggered Learning)**:
     - When the ReAct loop resolves a task that required `>= 3` repair cycles or overcame unexpected environment errors, HEX synthesizes a structured lesson:
       - Problem / Symptom.
       - Root Cause.
       - Verified Solution & Negative Guardrail.
       - Linked AST symbols / files.
     - Stores the reflection into `.hex/reflections/LESSONS.md` and indexes it into the local memory store.
3. **Hardware & Latency Gating (`auto_reflect`)**:
   - Spawning background subagents on local Apple Silicon (e.g. MacBook Air M4) or Ollama instances will thrash unified memory and stall the active inference stream.
   - Autonomous background reflection is strictly gated by `config.yaml`:
     ```yaml
     learning:
       auto_reflect: "cloud_only" # Options: "cloud_only" | "idle" | "off"
     ```
     - `"cloud_only"`: Only spawns background reflection subagents when routed to cloud endpoints (Anthropic, Gemini, OpenAI, etc.). Local model runs log the reflection opportunity to an append-only queue without background inference.
     - `"idle"`: Runs reflection during terminal idle time when the user is inactive.
     - `"off"`: Completely disables autonomous background reflection, keeping learning purely explicit via `/learn`.
4. **SkillSpector Security Gate**:
   - Any autonomously synthesized skill must pass `SkillSpector` static AST safety checks (verifying no unredacted secrets, dangerous `eval`/`exec`, or unauthorized network egress) before being enabled in `.hex/skills/`.
5. **Hybrid Cross-Session Retrieval**:
   - Combines SQLite FTS5 lexical search with Graphify AST symbol graph traversal.
   - When a new task mentions an error or function name, HEX queries past sessions and learned skills to recall previous resolutions.

---

## Part 5: Phased Implementation Plan

```
Phase 1: Multi-Tier Soul Engine & Developer DNA Sync
├── Task 1.1: Soul Manager & Developer DNA (Global ~/.hex/ vs Local .hex/ resolution, /rename)
├── Task 1.2: Punchy Mode Modulators (3-4 line plan / agent / code behavioral constraints)
└── Task 1.3: Prompt Builder Slot #1 Integration & Security Injection Gate

Phase 2: AST-Scoped Memory, Negative Guardrails & Lifecycle Engine
├── Task 2.1: Dual Markdown Stores & Negative Guardrails (USER.md, MEMORY.md, GUARDRAILS.md)
├── Task 2.2: JIT AST-Scoped Observation Injection (Graphify node-bound memory in tool output)
├── Task 2.3: Zettelkasten A-MEM Controller (CRUD contradiction resolution & [[wiki-links]])
├── Task 2.4: Memory Lifecycle & Archival (Hit-counters, promotion to SKILL, demotion to SQLite FTS5)
└── Task 2.5: Lightweight .history/ Snapshots (/memory history & /memory rollback)

Phase 3: Autonomous Learning Loop & Executable Skill Hooks
├── Task 3.1: Explicit /learn command with Deterministic verify: hooks
├── Task 3.2: Autonomous Friction Sensor with Hardware Gating (auto_reflect: cloud_only | idle | off)
├── Task 3.3: SkillSpector Verification Gate for generated skills
└── Task 3.4: Cross-Session FTS5 + AST Hybrid Retrieval
```

---

## Part 6: Comparison Matrix

| Feature | Hermes Agent | Standard Coding Agents | HEX Upgraded Cognition Engine |
| :--- | :--- | :--- | :--- |
| **Soul & Persona** | Single static `SOUL.md` | Rigid system prompt | Multi-tier: Compact `SOUL.md` (~300-500 tok) + Punchy 3-4 line Mode Overrides (`plan`/`agent`/`code`) |
| **Developer DNA Sync**| Mixed in home dir | Not supported | Clean separation: `~/.hex/USER.md` (Personal DNA) vs `.hex/MEMORY.md` (Repo Architecture) |
| **Identity Renaming** | Manual file editing | Not supported | Instant `/rename <name>` with dynamic TUI and prompt re-slotting |
| **Memory Architecture**| Monolithic 2.2k text cap | Vector DB or none | Partitioned: Global Slot #1 + JIT Node-Bound AST Memory in tool observations |
| **Negative Constraints**| None (only positive notes)| None | Dedicated `GUARDRAILS.md` with `[AVOID]` invariants auto-generated on repeat failures |
| **Contradiction Check**| Flat append (rot/conflict)| None | A-MEM CRUD Controller (UPDATE/DELETE contradictory notes + `[[wiki-links]]`) |
| **Memory Lifecycle** | None (static truncation)| Static or infinite | Promotion (reflection -> SKILL after 3 uses) & Demotion (archive to FTS5 after 30d stale) |
| **KV-Cache Hits** | Frozen snapshot at Turn 0| Mutates prompt (breaks) | Frozen Turn 0 snapshot + JIT tool observation injection (100% prefix cache hits) |
| **Memory History** | None | None | Lightweight `.history/` timestamped diffs (zero Ledger bloat) |
| **Skill Execution** | Passive text instructions | Text documentation | Deterministic `verify:` frontmatter hooks (executes test/assertion on skill completion) |
| **Learning Trigger** | Manual `/learn` command | Manual notes | Dual: Explicit `/learn` + Hardware-Gated Friction Reflection |
| **Hardware Protection**| None (unbounded threads)| None | `auto_reflect = "cloud_only" \| "idle" \| "off"` to protect local unified RAM |
| **Learned Skill Safety**| Basic review | None | `SkillSpector` AST & security scanning before registration |

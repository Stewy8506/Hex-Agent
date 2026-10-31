# Product Requirements Document: Custom Autonomous AI Harness

**Status:** Implementation baseline, v1.3 (Final)  
**Date:** 2026-10-31  
**Owner:** Product / platform owner  
**Audience:** Implementers, operators, and reviewers

## 1. Executive summary

Build a personal, extensible autonomous AI harness that can accept a goal, plan and execute bounded work, use approved tools, preserve durable progress across interruptions at the individual tool-call and filesystem level, learn useful context from projects and prior episodes, and run work across heterogeneous local hardware. The harness must make its actions inspectable, deterministically replayable, and cryptographically verifiable. It must never treat model output, retrieved memory, imported skills, AST graph data, or a tool response as authority to bypass policy.

The portable foundation is a custom Python orchestration service with PostgreSQL as the authoritative task ledger, call journal, and metadata store. Local files, Copy-on-Write (CoW) workspaces, and machine-specific execution remain behind strict adapters. The five stages are cumulative:

1. **Reliability-first core & CoW durability:** durable task state, an `R0/R1` Fast-Path Express Lane for low-latency tasks, call-level gateway memoization, Copy-on-Write (CoW) filesystem snapshots, bounded retries, and proof-carrying verification.
2. **Active Graphify, governed memory & SkillSpector gate:** codebase AST graph integration with active blast-radius interception, isolated project/personal/episodic memory, and Git-backed skills protected by an automated **NVIDIA SkillSpector** supply-chain/malware ingestion gate.
3. **Tool composition & two-tier routing:** a common memoizing tool gateway, Cloudflare Code Mode evaluation, two-tier local/cloud model routing (using local MacBook Air M4 MLX models as context compressors, semantic skill scanners, and pre-flight gatekeepers), and resource-aware scheduling.
4. **Distributed & speculative execution:** dispatch work and parallel speculative branches across a MacBook Air M4, a desktop, and a future AI server; evaluate Google Agent Executor (AX) as an optional execution backend.
5. **Controlled self-improvement:** evaluate Google Harness² for harness refinement; promote candidate changes only after SkillSpector scanning, immutable benchmark evaluation, and safety gates.

AX and Harness² are integration candidates, not assumptions about production readiness. Similarly, lightweight Postgres-backed durable execution libraries (such as DBOS or Restate) are evaluated in Stage 1 to avoid reinventing distributed lock edge cases where practical, while keeping the PostgreSQL schema authoritative. The system must remain useful if any external runtime is unavailable, changes incompatibly, or is rejected after evaluation.

Harness² already implements the same broad category of harness self-learning proposed here: it adapts a harness to a current task and carries evidence-supported lessons forward. The planned distinction is controlled adoption and durable, verified execution around that learning loop—not claiming the learning loop itself as novel.

## 2. Product vision

An operator should be able to give the harness a meaningful project task from a simple local control surface, leave it to work within explicit limits, and later inspect a deterministic Proof-Carrying Artifact bundle showing what it did, what AST nodes it impacted, what it learned, what it could not verify, and what needs human approval. A failure or mid-script crash should replay completed tool calls in zero milliseconds from the gateway journal and roll back dirty workspace trees cleanly via CoW snapshots rather than leaving an ambiguous half-completed process. The harness should improve its context, skill library, and execution choices over time without silently rewriting its own trusted control plane or ingesting trojanized skills.

**Initial product focus:** project-scoped coding and technical research that produces reviewable, proof-carrying artifacts: repository understanding, bounded bug fixes, multi-module refactors, and source-backed technical briefs. The first release is a dependable personal workbench for those jobs, not a general assistant for every domain.

**Primary interaction:** a local-first web control surface, backed by the same API as the CLI. The owner can submit a task, see its live state, inspect AST blast-radius warnings and proof-carrying artifacts, review SkillSpector supply-chain scan reports, compare speculative execution branches, approve gated actions, and pause/resume/cancel work. Thin outbound messaging adapters (e.g., Telegram/Discord approval webhooks) are optional remote controls after the local workflow is useful and secure.

### Product principles

- **Durability before autonomy:** an interrupted run resumes from recorded state, call journals, and CoW filesystem snapshots; it never relies on hidden conversation history or dirty working directories.
- **Evidence before completion:** completion claims are backed by Proof-Carrying Artifact bundles (deterministic test hashes, AST impact subgraphs, container digests) or explicit human acceptance.
- **Compiler-assisted context over passive RAG:** structural impact (downstream callers, type breakage) is actively pushed into the agent's context and verification requirements when files are modified.
- **Least privilege & supply-chain zero trust:** each task receives only the tools, credentials, network paths, and filesystem access it needs; every imported or agent-generated skill is statically and semantically scanned for malware before registration.
- **Portable core:** orchestration semantics and task records are independent of a specific model vendor or runtime.
- **Human control at consequential boundaries:** external publication, irreversible changes, credential changes, and policy/skill promotions require configured approval.
- **Memory is evidence-bearing:** retrieved facts have provenance, scope, freshness, and confidence; memory cannot grant authority.
- **Improvement is gated:** candidate harness and skill changes are isolated, scanned via SkillSpector, evaluated against a fixed suite, and promoted or rolled back explicitly.

## 3. Goals and non-goals

### Goals

1. Accept and execute asynchronous, multi-step tasks that may span minutes or hours, while providing a low-latency Fast-Path Express Lane (`<2s` startup) for simple `R0/R1` queries.
2. Recover safely from process, host, model-provider, tool, and network interruptions with call-level replay and CoW workspace rollback.
3. Provide a durable, queryable history of task intent, state transitions, memoized tool calls, CoW tree hashes, artifacts, approvals, and verification evidence.
4. Use Graphify both for pull-based repository queries and push-based blast-radius interception on code edits, while keeping personal and episodic memory in a separate governed model.
5. Offer one policy-enforcing, call-journaling interface for direct tools, MCP tools, and Code Mode compositions.
6. Route model and execution work across a two-tier pipeline: using fast local models (MacBook Air M4 MLX) for log compression, triage, SkillSpector semantic checks, and schema validation before invoking heavy cloud or desktop reasoning.
7. Use multiple machines—including parallel speculative execution branches—without sharing unrestricted credentials or creating duplicate external side effects.
8. Experiment with AX and Harness² without making either a required dependency.
9. Allow harness and skill evolution only when SkillSpector supply-chain scans and reproducible evaluations show acceptable quality, safety, and cost.
10. Make the first useful workflow understandable without reading raw logs or using API calls.
11. Capture successful repeatable procedures as Git-backed, human-editable (`SKILL.md`), versioned skills that can be scanned, audited, reused, or scheduled.

### Non-goals for the initial product

- Building a general-purpose hosted multi-tenant agent platform.
- Running arbitrary untrusted code on the host machine without a sandbox and CoW filesystem boundary.
- Loading unverified community skills or MCP definitions without passing automated supply-chain and prompt-injection scans.
- Making Graphify the canonical store for all personal or episodic memories.
- Allowing agents to change their own system policies, permissions, promotion gates, or secrets.
- Requiring Kubernetes, a Google Cloud account, or a particular model vendor for the core deployment.
- Building full conversational messaging-channel breadth before the local task and approval workflow is complete.
- Guaranteeing that an agent can complete every task without human intervention.
- Treating AX or Harness² claims or benchmark gains as proof of suitability for this deployment.

## 4. Users and primary use cases

### Personas

| Persona | Needs | Authority |
|---|---|---|
| Owner / operator | Submit work, inspect status and proof bundles, review SkillSpector reports, approve sensitive actions, manage budgets and machines | Full configuration and approval authority |
| Developer / maintainer | Implement adapters, inspect traces, replay failures from call journals, author/edit `SKILL.md` bundles and tests | Repository and staging access; no automatic production promotion |
| Autonomous worker | Execute a specific assigned task or speculative branch within a CoW workspace and return evidence | Only task-scoped capabilities granted by policy |
| Local gatekeeper (M4 tier) | Compress raw tool outputs, run SkillSpector semantic alignment checks, pre-validate arguments, and triage R0/R1 queries locally | Read-only context transformation, static/semantic security analysis, and pre-flight validation |
| Evaluator | Compare candidate harnesses, skills, and models on fixed tasks | Read-only benchmark data and isolated candidate execution |

### Core use cases

- Submit a project-scoped repository or technical research task and receive visible progress and a Proof-Carrying Artifact bundle.
- Run a quick `R0/R1` diagnostic task via the Fast-Path Express Lane without waiting for heavy DAG planning Ceremony.
- Pause for user approval, survive a process or host restart, and continue from the exact memoized tool call and CoW filesystem snapshot.
- Fork a tricky bug-fix task into two parallel speculative attempts on isolated CoW snapshots, run verification on both, and promote the passing branch.
- Edit a core function in an `R2` workspace and automatically receive an injected Graphify blast-radius warning listing affected downstream callers and pinning their tests to the verification plan.
- Import a third-party `SKILL.md` bundle or generate a new candidate skill and have **NVIDIA SkillSpector** automatically block it if it contains hidden Unicode prompt injections, vulnerable script dependencies, or a mismatch between its declared purpose and actual instructions.
- Compose several tool calls in one Code Mode execution; if call #5 fails, retry the script with calls #1–#4 replaying instantaneously from the Tool Gateway call journal.
- Use the MacBook Air M4 local model to strip 50,000 tokens of raw pytest/build output down to a 500-token structured failure summary before calling a paid cloud reasoning model.
- Test a candidate harness revision on a held-out evaluation set and promote it only after passing safety and quality gates.

## 5. Scope and staged delivery

Each stage adds value to the previous stage and has an exit gate. Do not begin broad autonomous rollout before Stage 1 passes its recovery and verification gates.

### Stage 1 — Reliability-first core & CoW durability

Deliver a guided single-machine setup, local web control surface, CLI, PostgreSQL ledger, one worker loop, `R0/R1` Fast-Path Express Lane, Tool Gateway call-level journaling, Copy-on-Write (CoW) workspace snapshotting, bounded retries, cancellation, Proof-Carrying Artifact recording, and verification framework. Evaluate embedding a lightweight Postgres-native durable execution layer (such as DBOS) versus raw custom lease SQL during M0. The dashboard must support task intake, a live task list/detail timeline, approvals, and proof-bundle review. Pilot only project-scoped repository understanding, bounded bug fixes, and source-backed technical briefs, using a small allow-listed tool set. Keep execution local and use a PostgreSQL-backed queue; do not require an external broker, object store, cluster, or remote worker.

**Exit gate:** injected process restart and mid-step failures replay prior memoized tool calls without duplicate side effects and restore exact CoW filesystem state; terminal tasks always carry a cryptographic proof bundle and verification record; retry and resource budgets are enforced.

### Stage 2 — Active Graphify, governed memory & SkillSpector supply-chain gate

Add a Graphify adapter for repository AST/call-graph build and query, plus an **Active Blast-Radius Interceptor** that hooks into `R2` file modifications to compute 1-hop/2-hop downstream dependency impacts and dynamically append affected tests to the task's verification plan. Add a memory service with project, personal, and episodic scopes. Introduce the Git-backed, on-disk versioned skill format (`SKILL.md` + manifest) for repeatable repo analysis and research workflows, guarded by the **NVIDIA SkillSpector** static and semantic security scanner. Record provenance, confidence, timestamps, sensitivity, and expiry.

**Exit gate:** project facts do not cross project boundaries; modifying a shared symbol automatically updates the task verification checklist with downstream callers; trojanized or misaligned skills (hidden Unicode, symlink escapes, undeclared tool/network access, OSV vulnerabilities) are deterministically caught and quarantined by SkillSpector; memory can be corrected, forgotten, exported, and audited.

### Stage 3 — Tool composition and two-tier hybrid routing

Add the common tool gateway with call-level memoization, direct and MCP tool adapters, optional Cloudflare Code Mode connector, two-tier model router (Local M4 Gatekeeper/Compressor + Cloud/Desktop Heavy Reasoner), and resource-aware scheduler. Add owner-invoked skills and basic scheduled routines using the same task lifecycle, policy, and audit path. Treat Code Mode as an execution/composition surface, not an authorization boundary.

**Exit gate:** tools invoked through Code Mode composition obey identical policy, credential, call-journaling, budget, approval, and audit controls as direct invocation; local M4 pre-compression demonstrably reduces cloud token spend without dropping critical failure evidence; route decisions are reproducible and explainable.

### Stage 4 — Distributed and speculative execution

Add worker registration, capability/health reporting, leases, queue dispatch, heartbeat, cancellation, CoW snapshot/artifact transfer, and machine placement across the MacBook Air M4, desktop, and later server. Support **Speculative Branching** (forking a checkpoint into parallel worker attempts on difficult steps). Run an AX feasibility evaluation behind the `ExecutionBackend` interface.

**Exit gate:** a worker loss leads to lease expiry and safe reassignment; two machines cannot concurrently commit the same fenced task attempt; speculative branches execute in isolated CoW workspaces without colliding and cleanly merge or discard; execution remains possible using the built-in worker backend if AX is unavailable.

### Stage 5 — Controlled self-improvement

Add a candidate harness registry, immutable evaluation snapshots, safety/adversarial suite, comparison reports, approval, promotion, and rollback. Evaluate Harness² as an isolated implementation of task-level Propose–Probe–Compose refinement and a persistent improvement library; enforce SkillSpector scanning and our own policy/promotion boundary around every candidate revision.

**Exit gate:** no candidate can modify production policies or promote itself; every candidate skill or prompt bundle passes SkillSpector before benchmark execution; every promoted revision is traceable to its evaluation, SkillSpector report, reviewer, source, and rollback target; quality improves or meets non-regression thresholds without violating safety or cost limits.

## 6. Functional requirements

### 6.1 Task intake and control

- **FR-001** Accept a task with `goal`, optional `project_id`, priority, deadline, requested deliverable, constraints, budget, execution mode (`standard` vs. `fast_path`), and approval policy.
- **FR-001a** **Fast-Path Express Lane (`R0/R1`):** Tasks classified as read-only (`R0/R1`) with fewer than 3 estimated steps may bypass multi-node DAG planning and execute synchronously through the Tool Gateway with `<2s` startup overhead while still recording full call-journal and audit events.
- **FR-002** Generate immutable `task_id` and `attempt_id` identifiers; support client idempotency keys on submission.
- **FR-003** Validate task policy before enqueueing. Reject malformed or disallowed tasks with a machine-readable reason.
- **FR-004** Support `get`, `list`, `events`, `cancel`, `pause`, `resume`, `fork` (speculative branch), and `approve` operations.
- **FR-004a** Provide a local-first web dashboard over the task API with an inbox/active list, task detail and event timeline, AST blast-radius impact viewer, SkillSpector security report viewer, approval queue, Proof-Carrying Artifact preview/download, and visible failure or verification status. Bind locally by default; remote access requires authenticated configuration.
- **FR-005** Store a canonical task specification and a versioned policy snapshot so a resume does not silently inherit changed policy.
- **FR-006** Make task progress available as a polling API and an optional server-sent event stream. PostgreSQL remains the source of truth.

### 6.2 Execution, CoW snapshots, and call-level durability

- **FR-010** Every state transition is committed transactionally with an append-only event record.
- **FR-011** Workers claim work using expiring leases with monotonically increasing fencing tokens.
- **FR-012** A checkpoint records the current plan DAG revision, completed steps, pending steps, relevant state references, known side effects, and an immutable **Copy-on-Write (CoW) workspace tree hash (`workspace_tree_sha`)**. Checkpoints must not contain raw credentials.
- **FR-012a** Every `R2` workspace is backed by a platform-appropriate CoW mechanism (APFS clones on macOS M4, OverlayFS/Btrfs/ZFS snapshots or container layer commits on Linux desktop). Reverting or branching a checkpoint restores the exact filesystem state in milliseconds without leaving untracked build artifacts or dirty working trees.
- **FR-013** Resume from the latest valid checkpoint and CoW snapshot after a worker crash, host restart, or explicit pause. If state cannot be reconciled safely, transition to `needs_attention`.
- **FR-014** Retry only classified transient failures, with per-step and per-task limits, exponential backoff with jitter, an absolute deadline, and a maximum attempt count.
- **FR-015** **Gateway Call-Level Journaling:** The Tool Gateway records every tool invocation's `(task_id, step_id, tool_id, canonical_args_hash, idempotency_key)` before execution and seals the result upon completion. If a step or multi-call Code Mode script crashes mid-execution and is retried, previously sealed calls within that step replay their recorded results deterministically in 0 ms without re-executing external side effects.
- **FR-016** Support cancellation as cooperative first, then terminate the isolated execution after a configurable grace period and roll back uncommitted CoW filesystem layers. Record whether cancellation was clean.
- **FR-017** Capture outputs, logs, tool results, and generated artifacts with hashes, size limits, access policy, and retention metadata.
- **FR-018** Completion requires a task-specific verification plan and generates a **Proof-Carrying Artifact Bundle** (containing the git diff, workspace tree hash, container/runtime digest, AST impact subgraph, deterministic test output hashes, and ledger event chain signature). The worker's own assertion is never sufficient evidence.
- **FR-019** Preserve partial results, CoW failure snapshots, and explain blockers when the task fails, expires, or needs user input.

### 6.3 Dynamic DAG planning, speculative branching, and verification

- **FR-020** A plan is represented as a **Dynamic Dependency DAG** of steps with explicit acceptance checks, risk class, estimated resource needs, and side-effect declarations.
- **FR-021** Replanning mutates only the invalidated subgraph of the DAG, creates a new plan revision, and records why prior nodes were retained, invalidated, or reopened.
- **FR-021a** **Speculative Execution (Tree-of-Work):** For high-uncertainty `R2` coding steps (or when configured by policy/budget), the orchestrator may fork a checkpoint's CoW snapshot into $N$ (typically 2) parallel `attempt_id` branches with distinct repair strategies, run deterministic verification on each, commit the winning branch, and discard the losing CoW snapshot.
- **FR-022** Verification may include deterministic checks (test suites, typecheckers, linters), AST blast-radius checks, artifact inspection, a second model review, human review, or a combination. The verification method and cryptographic digest of the output are recorded.
- **FR-023** High-impact changes require independent verification and, where policy says so, human approval before execution or release.
- **FR-024** A verification failure returns the task to `verifying` for bounded repair or moves it to `needs_attention`; it cannot be papered over by changing the success criterion after the fact.

### 6.4 Memory and Active Graphify

- **FR-030** Store project, personal, and episodic memory as distinct scopes with explicit retrieval rules.
- **FR-031** Each memory item includes source/provenance, creator, created/updated times, confidence, sensitivity, expiry/review time, project scope, and a status (`candidate`, `active`, `superseded`, `rejected`, `expired`).
- **FR-032** Episodic memory records task summaries, decisions, outcomes, and reusable lessons; it excludes secrets and unnecessary raw conversation/tool payloads.
- **FR-033** Personal memory is opt-in, user-editable, and never automatically exposed to a task unless relevance and policy permit it.
- **FR-034** Memory retrieval returns provenance and uncertainty with every item. Untrusted content is quoted/marked as data and cannot change system or tool policy.
- **FR-035** Support memory search, inspect, correct, forget, export, and retention cleanup.
- **FR-036** Graphify is accessed through an adapter that can build or refresh a local repository AST/call graph, query it, and return source paths and provenance. Graphify is not required for non-code tasks.
- **FR-036a** **Active Blast-Radius Interceptor:** Whenever an `R2` tool modifies a source file or AST symbol, the Graphify adapter automatically computes the 1-hop and 2-hop downstream call and type dependency delta before the next model turn. It injects a concise **Architectural Impact Warning** into the worker context and dynamically appends affected downstream test targets or type checks to the task's mandatory Verification Plan (`FR-018`).
- **FR-037** Do not commit Graphify output to source control by default. Allow project-level configuration where graph artifacts may be committed.
- **FR-038** Detect source revision drift and graph age; incrementally re-index dirty files in the active CoW workspace and mark graph results stale when the repository commit differs from the indexed commit.

### 6.5 Reusable skills, routines, and SkillSpector security gate

- **FR-039** Represent a skill as a Git-backed, human-editable directory bundle on disk (`SKILL.md` with YAML frontmatter + optional `scripts/` and `manifest.yaml`) indexed in PostgreSQL by content SHA-256 hash. Each bundle declares purpose, trigger/description, instructions, allowed tools, required inputs, expected outputs, risk class (`R0–R4`), and verification procedure.
- **FR-039a** The owner can browse, inspect, invoke, scan, disable, and pin skill versions from the dashboard or CLI. Skills cannot broaden the task's granted permissions.
- **FR-039b** Record skill version and content hash on every task that uses it. Skill edits produce a candidate revision; promotion requires passing its configured checks, passing the SkillSpector gate, and owner approval.
- **FR-039c** Allow scheduled or event-triggered routines to submit ordinary tasks through the same policy, budget, state, approval, and audit mechanisms. Scheduling does not grant additional authority.
- **FR-039d** Stage 2 ships initial repo-understanding and technical-research skills. Scheduling, external triggers, and skill sharing are deferred until the common gateway and approval workflow are in place.
- **FR-039e** **Skill Supply-Chain & Malware Gate (NVIDIA SkillSpector):** Every imported third-party skill, local `SKILL.md` edit, MCP server definition, or agent-proposed skill candidate (including Stage 5 Harness² proposals) must pass an automated **SkillSpector** scan before entering the `candidate` or `active` state:
  1. **Static & Dependency Analysis:** Scans across 64+ agent-native vulnerability patterns (invisible Unicode steganography, hidden HTML/Markdown prompt injections, base64 payload obfuscation, symlink/path-traversal escapes, MCP tool poisoning, trigger hijacking, and live/offline OSV.dev dependency CVE lookups).
  2. **Semantic Alignment Analysis:** Uses the Tier-1 Local M4 MLX model (`FR-045a`) to compare the skill's declared frontmatter purpose and risk class against its actual Markdown instructions and bundled helper scripts, catching undeclared network egress, credential harvesting, or privilege escalation.
  3. **Deterministic Gate Enforcement:** Produces a `0–100` risk score and structured JSON report (`Critical`, `High`, `Medium`, `Low`, `Info`). Any skill scoring `Critical` or `High`, or requesting tools above its declared risk tier, is automatically quarantined (`status = rejected`). `Medium` findings require explicit owner sign-off in the Approval Queue before activation.

### 6.6 Tools, Code Mode, and Two-Tier Model Routing

- **FR-040** Expose all tools through a common gateway with typed schemas, identity, risk classification, policy check, call-level journaling (`FR-015`), timeout, rate limit, result-size cap, and audit event.
- **FR-041** Tool capabilities are granted per task and can be read-only, write-limited, or approval-gated. Default is deny.
- **FR-042** Code Mode compositions call gateway-mediated tool functions; no generated code may access credentials, unrestricted host networking, or host filesystem directly.
- **FR-043** Cloudflare Code Mode integration is optional. The adapter supports discovery and execution and records generated code or an auditable digest according to configured privacy policy.
- **FR-044** Bound Code Mode CPU, wall time, memory, request count, result size, and allowed destinations. Validate arguments and outputs at the gateway.
- **FR-045** Route model requests based on task class, capability, context need, privacy classification, availability, latency target, and cost ceiling. Permit explicit pinning/allow-lists.
- **FR-045a** **Two-Tier Local/Cloud Pipeline:** Configure a fast local model tier on the MacBook Air M4 (e.g., 8B–9B MLX models) as a continuous zero-marginal-cost gatekeeper and preprocessor. The local tier performs: (1) **Context Compression**, distilling verbose tool outputs, stack traces, and test logs into dense, provenance-linked facts before cloud escalation; (2) **SkillSpector Semantic Scanning** (`FR-039e`) on candidate skills; (3) **Pre-Flight Schema & Policy Checks** on proposed tool calls; and (4) **Local Execution** of routine `R0/R1` repo triage and summarization tasks with 100% local privacy.
- **FR-046** Record the selected provider/model/version, tier (local gatekeeper vs. heavy reasoner), routing reason, request class, token savings from local compression, latency, and cost when available. Avoid storing sensitive prompts unless retention policy allows it.
- **FR-047** On provider error, use only an approved fallback. Do not route restricted data to a less trusted provider.
- **FR-048** Schedule execution using machine capabilities, current load, power/availability preference, data locality, and task priority. Never place work on an ineligible device.

### 6.7 Distributed workers and experimental runtimes

- **FR-050** A worker advertises a stable `worker_id`, machine class (`m4_air`, `desktop_gpu`, `server`), OS/architecture, CoW filesystem support, available resources, installed runtimes, local MLX/LLM availability, tool reachability, trust level, and heartbeat time.
- **FR-051** A worker receives a short-lived task lease and scoped capability token. It renews the lease while active and syncs its CoW delta and call journal before lease handoff when possible.
- **FR-052** Lease expiry fences the old worker from committing results; a replacement may resume only from committed checkpoint state, sealed tool-call journal entries, and synced CoW snapshots.
- **FR-053** Support local execution on the MacBook Air M4 and desktop first; future server registration is configuration, not a new orchestration implementation.
- **FR-054** AX integration sits behind `ExecutionBackend`; the native Python worker remains the baseline backend. An AX proof of concept must test deployability, persistence semantics, observability, security, operational burden, and portability before any production use.
- **FR-055** Persist an interoperability mapping between harness task states and AX states, and define behavior for unknown or incompatible runtime states.

### 6.8 Controlled self-improvement

- **FR-060** Store harness revisions as immutable bundles: prompts, tool guidance, skills, routing policy, memory policy, scripts, and model pins, with hashes, authorship, and SkillSpector scan reports.
- **FR-061** A candidate generator may propose a patch only within a declared allow-list. It cannot modify safety policy, approval requirements, SkillSpector thresholds, credentials, evaluation data, or promotion logic.
- **FR-062** Evaluate candidates in isolated environments against versioned tasks and scoring rubrics. Include task success, correctness, safety, SkillSpector risk score, regression, latency, cost, and reproducibility measures.
- **FR-063** Use separate development, validation, and held-out evaluation sets. Avoid leaking benchmark answers or rubrics into candidate context.
- **FR-064** Compare candidates to the current production revision and a fixed baseline. Promotion requires passing SkillSpector gates, configured benchmark thresholds, and human approval.
- **FR-065** Record the candidate diff, SkillSpector report hash, evaluator version, model versions, random seeds where applicable, raw results, aggregate report, approval, and rollback target.
- **FR-066** Harness² is an optional adapter for its two linked learning loops: task-level contrastive Propose–Probe–Compose refinement, and harnessing-level reflection that retains evidence-supported lessons in an improvement library for later tasks. Validate executor compatibility, network posture, reproducibility, data egress, licensing, cost, and practical lift before adoption.
- **FR-067** Support immediate rollback to the last known-good harness revision and automatic rollback when monitored regression thresholds are crossed.
- **FR-068** Keep Harness² candidate execution separate from production promotion. Its behavioral contrasts are evidence about changed behavior, not ground-truth proof of correctness; our SkillSpector scan, independent verification, safety gates, approval, immutable revision, and rollback requirements remain mandatory.

## 7. Non-functional requirements

| Area | Requirement |
|---|---|
| Durability | A committed task event, sealed tool-call journal entry, or CoW snapshot survives service restart. PostgreSQL backup and restore are documented and periodically exercised. |
| Fast replay & rollback | Replaying a memoized tool call from the gateway journal takes less than 10 ms; reverting or forking a CoW workspace snapshot takes less than 2 seconds for typical repositories. |
| Availability | Single-node baseline is acceptable initially; outages produce queued or recoverable work, not silent loss. |
| Performance | Task submit/status operations respond within 500 ms; Fast-Path `R0/R1` tasks begin tool execution within 2 seconds; static SkillSpector scans complete within 3 seconds. |
| Scalability | Architecture supports multiple workers, parallel speculative branches, and queue consumers without relying on unpersisted in-memory coordination. |
| Security & Supply Chain | Secrets are stored outside task/event data; tool calls are least-privilege; execution isolation and CoW confinement are required for arbitrary code; 100% of skills and MCP definitions pass SkillSpector before activation. |
| Privacy | Data classification controls model routing (prioritizing local M4 MLX execution for restricted data), persistence, telemetry, and worker placement. |
| Explainability | Each material action has a task, attempt, plan DAG node, policy decision, tool call journal ID, CoW tree SHA, and trace correlation ID. |
| Portability | Core runs on supported Python platforms and PostgreSQL; external services and CoW backends (APFS, OverlayFS/Btrfs, container snapshots) are replaceable behind interfaces. |
| Maintainability | Adapters have explicit contracts; schema and event formats are versioned; migrations are forward-only with documented recovery. |
| Cost control | Per-task and global ceilings cover model calls, tool calls, speculative branches, compute time, storage, and retries; local M4 compression and local semantic skill scanning actively minimize cloud API spend. |

## 8. Architecture and technology choices

### 8.1 Foundation choices

- **Language/runtime:** Python 3.12+ for orchestration and worker services. Keep dependencies constrained and pinned for releases.
- **API:** FastAPI with Pydantic v2 request/response types; provide a small CLI for owner workflows and debugging.
- **Owner experience:** local-first web dashboard served by the API, using versioned API contracts. The dashboard surfaces plain-language status, interactive AST blast-radius warnings, SkillSpector security badges/reports, speculative branch comparisons, and Proof-Carrying Artifact receipts.
- **Database & Durable Execution:** PostgreSQL 16+ as the portable source of truth for tasks, events, call journals, leases, approvals, skill metadata, memory metadata, and evaluation records. During M0, evaluate embedding **DBOS** (Postgres-backed durable workflows) or **Restate** to handle low-level workflow replay and timer edge cases while preserving explicit schema ownership in PostgreSQL.
- **Queue:** PostgreSQL-backed claim/lease queue (`SELECT ... FOR UPDATE SKIP LOCKED`) in the first release to avoid a required external broker. Introduce Redis/NATS only if measured contention requires it; queue is not the task ledger.
- **Copy-on-Write (CoW) Workspace Provider:** Abstracted behind a `WorkspaceSnapshotProvider` protocol. Uses `cp -c` (APFS clones) on macOS (MacBook Air M4) and OverlayFS / Btrfs subvolumes / Docker layer commits on Linux (Desktop) to provide instantaneous zero-copy branching and rollback for `R2` tasks.
- **Skill Security Scanner:** **NVIDIA SkillSpector** integrated as a Python/CLI ingestion service (`SkillSecurityScanner` protocol), pairing its static/OSV rule engine with the local MacBook Air M4 MLX model for offline semantic alignment checks.
- **Artifacts & Proof Bundles:** local content-addressed artifact directory (CAS) for single-machine development; define an S3-compatible object-store interface for multi-machine deployment. Store artifact metadata, CoW tree hashes, and cryptographic proof-bundle manifests in PostgreSQL.
- **Migrations:** Alembic. Use transactionally safe migration practices and explicit schema version checks.
- **Observability:** OpenTelemetry-compatible traces and structured logs; metrics exported to a local dashboard or Prometheus-compatible endpoint.
- **Execution isolation:** container or OS sandbox per untrusted/code task, mounted over the attempt's CoW workspace snapshot, with resource ceilings and network deny-by-default.
- **Secrets:** OS keychain for a single-machine development profile and a secret-provider interface for later deployment. Pass short-lived scoped credentials only at execution time.

### 8.2 Logical components

1. **Control API / CLI / Web Dashboard:** intake, status, events, approvals, speculative branch viewer, SkillSpector report inspector, proof-bundle inspector, pause/resume/cancel.
2. **Orchestrator:** validates policy, routes Fast-Path `R0/R1` tasks, manages Dynamic Plan DAGs, schedules steps and speculative branches, coordinates state transitions.
3. **Task Ledger & Call Journal:** PostgreSQL schema, append-only event log, memoized tool-call journal, lease/fencing coordination.
4. **Worker Manager:** registration, health, capabilities, execution assignment, CoW delta and checkpoint upload.
5. **Worker Runtime & CoW Workspace Adapter:** isolated plan-step executor, CoW snapshot manager (`APFS`/`OverlayFS`), and recovery adapter.
6. **Tool Gateway:** policy-enforcing, call-journaling proxy for local functions, MCP servers, Code Mode, and future connectors.
7. **Two-Tier Model Router:** coordinates between the **Tier-1 Local Gatekeeper** (MacBook Air M4 MLX for log compression, SkillSpector semantic scans, triage, and pre-flight validation) and **Tier-2 Heavy Reasoners** (frontier cloud APIs or desktop GPU models), handling fallbacks and usage accounting.
8. **Skill Registry & SkillSpector Gate:** manages Git-backed `SKILL.md` bundles on disk, executes static + OSV + semantic malware scans on ingestion/edit, and enforces version pinning.
9. **Memory Service:** scoped memory retrieval and writes, provenance, retention, access checks.
10. **Active Graphify Adapter:** repository AST indexing/query lifecycle, stale-index detection, and **Push-Based Blast-Radius Interceptor** on `R2` file modifications.
11. **Verification & Proof Service:** runs deterministic and AST-dynamic checks, assembling signed Proof-Carrying Artifact bundles.
12. **Evaluation Service:** benchmark datasets, harness revisions, comparisons, and promotion workflow.
13. **Artifact Store (CAS):** immutable output, CoW archive deltas, and checkpoint objects with SHA-256 integrity checks.

### 8.3 Key boundaries

- The orchestrator owns lifecycle and authorization decisions; workers execute only leased, scoped work inside isolated CoW workspaces.
- The task ledger and call journal are authoritative; caches, queues, Graphify indexes, and execution runtimes are derived or pluggable.
- No skill or MCP tool definition can be loaded into worker context without a valid, passing `skill_scans` record matching its exact content SHA-256 hash.
- Tool providers never receive broader credentials than their task-scoped capability.
- Models propose actions. Policy code authorizes them. The Tool Gateway journals and enforces them.
- Candidate harnesses run in an isolated evaluation namespace and cannot write production records except through a validated promotion operation.

## 9. Task lifecycle and state machine

### 9.1 States

```text
SUBMITTED -> VALIDATING -> QUEUED -> PLANNING -> READY -> RUNNING -> VERIFYING -> SUCCEEDED
                 |            |          |        |         |          |
                 |            |          +--------+---------+          |
                 |            |          | (Fast-Path R0/R1 bypass)    |
                 v            v          v        v         v          v
             REJECTED     CANCELED    NEEDS_   WAITING_   PAUSED     NEEDS_
                                     ATTENTION APPROVAL             ATTENTION

RUNNING -> SPECULATING (parallel CoW attempt branches) -> VERIFYING
RUNNING -> RETRY_WAIT -> QUEUED
RUNNING -> PAUSED
RUNNING -> CANCELED
VERIFYING -> RUNNING (bounded repair on DAG subgraph, new plan revision)
Any nonterminal -> FAILED / EXPIRED / CANCELED subject to finalization rules
```


Terminal states: SUCCEEDED, FAILED, REJECTED, CANCELED, EXPIRED. NEEDS_ATTENTION and WAITING_APPROVAL are nonterminal and resumable.

### 9.2 Transition rules

- Every transition includes actor, timestamp, prior state, next state, reason code, correlation ID, workspace_tree_sha (if applicable), and relevant policy version.
- Only the orchestrator performs authoritative transitions. Workers submit events, journal seals, and verification proofs; they cannot directly set terminal state.
- Transition preconditions use compare-and-swap on task version and current lease fencing token.
- A task can be paused or canceled from any nonterminal state. Cancellation rolls back uncommitted CoW workspace changes and records any uncertain external side effects.
- A task awaiting approval stores the exact action, parameters, and AST blast-radius summary to approve. Material changes invalidate prior approval.
- Expiry is enforced by the orchestrator even when a worker is offline.
- Reopening a terminal task creates a new task linked by parent_task_id and clones the target CoW workspace snapshot; do not mutate historical terminal outcomes.

## 10. Task ledger and data model

Core tables (names indicative; migrations may refine):

| Table | Purpose / key fields |
|---|---|
| tasks | task_id, parent_task_id, project_id, goal, execution_mode, status, priority, deadline_at, budget_json, policy_snapshot_id, version, created_at, updated_at, terminal_reason |
| task_events | Append-only event stream: event_id, task_id, attempt_id, sequence_no, event_type, payload, actor, occurred_at, correlation_id; unique (task_id, sequence_no) |
| attempts | attempt_id, task_id, parent_attempt_id (for speculative branches), worker_id, lease_id, fencing_token, base_tree_sha, head_tree_sha, status, started_at, ended_at, failure_class, retry_index |
| plans | plan_id, task_id, revision, dag_json, blast_radius_checks_json, created_by, rationale, created_at, supersedes_revision |
| checkpoints | checkpoint_id, task_id, attempt_id, plan_revision, workspace_tree_sha, artifact_uri, sha256, schema_version, created_at, validity |
| tool_calls | Call-level journal: call_id, task_id, attempt_id, step_id, sequence_in_step, tool_id, canonical_args_hash, arguments_redacted, result_ref, replayed_from_call_id, policy_decision, idempotency_key, started_at, ended_at, status |
| approvals | approval_id, task_id, action_hash, blast_radius_summary, requested_by, decided_by, decision, expires_at, created_at, decided_at, comment |
| artifacts | artifact_id, task_id, kind (deliverable, cow_delta, proof_bundle, skill_scan_report), uri, sha256, size_bytes, media_type, sensitivity, retention_until, created_at |
| proof_bundles | proof_id, task_id, attempt_id, git_diff_sha, workspace_tree_sha, runtime_digest, ast_impact_json, verification_results_json, ledger_signature, created_at |
| skills | skill_id, revision_id, name, disk_path, git_commit_sha, bundle_sha256, risk_class, allowed_tools_json, status (candidate, active, pinned, disabled, quarantined), latest_scan_id, created_at |
| skill_scans | SkillSpector ledger: scan_id, skill_id, revision_id, bundle_sha256, scanner_version, semantic_model_id, risk_score (0–100), severity (info, low, medium, high, critical), matched_rules_json, osv_vulns_json, semantic_alignment_passed, gate_decision, scanned_at |
| workers | worker_id, machine_class, capabilities_json, cow_backend, local_llm_models_json, trust_level, health, last_heartbeat_at, registered_at |
| leases | lease_id, task_id, attempt_id, worker_id, fencing_token, expires_at, renewed_at, released_at |
| memories | memory_id, scope, project_id, subject, content, provenance_json, confidence, sensitivity, status, expires_at, created_at, updated_at |
| harness_revisions | revision_id, parent_revision_id, bundle_hash, bundle_uri, skill_scan_id, status, created_by, created_at, promoted_at |
| evaluations | evaluation_id, revision_id, suite_id, dataset_version, model_pins, results_uri, metrics_json, gate_decision, created_at, reviewer_id |

Append-only events, skill_scans, and sealed tool_calls rows are the immutable audit and replay record; mutable rows are optimized current views. Sensitive payloads are redacted or stored in encrypted artifact objects with narrower access.

## 11. Memory model & Active Graphify integration

| Scope | Examples | Default access | Retention / write policy |
|---|---|---|---|
| Project | Architecture, conventions, repo-specific decisions, Graphify AST index metadata | Tasks explicitly attached to that project | Source-linked; refresh or expire when source commit changes; user may correct/delete |
| Personal | User preferences and stable working preferences | Only when explicitly enabled and relevant | User-approved or explicitly configured capture; editable and deletable |
| Episodic | What happened in a task, decisions, outcomes, reusable debugging lessons | Same project by default; cross-project only when explicitly classified general | Summarize minimally via local M4 compressor; no secrets; retain provenance and links to proof bundles |

Retrieval policy order: classify task → apply project and sensitivity filters → retrieve relevant candidates → check freshness/status → return bounded evidence with provenance → record memory IDs used. The model may propose a memory write, but the memory service validates scope, sensitivity, provenance, duplication, and retention.

### Active Graphify Architecture

Graphify provides code structure and relationships; it does not replace the ledger or memory policy. Beyond passive query tools, the Graphify adapter operates as an Active Blast-Radius Hook in the Tool Gateway:

1. Pre-Commit AST Diffing: When an R2 tool modifies a file in the CoW workspace, the adapter parses the modified AST nodes and queries the local Graphify graph for 1-hop and 2-hop callers, inheritors, and type dependents.
2. Context Injection: Before the next LLM turn, the harness injects a compact structural notice: e.g., [AST Blast-Radius]: Modified symbol X in module A; impacts 3 callers in module B and 2 test files.
3. Dynamic Verification Mutation: The verification service automatically adds the impacted downstream test files and type-check targets to the current plan's mandatory verification checks (plans.blast_radius_checks_json).

Verify the exact upstream Graphify package, executable, protocol, licensing, and supported output format during Stage 2 discovery before pinning it (Graphify project).

## 12. Tool permissions, skill supply-chain, and security model

### 12.1 Policy model

Default deny. Every tool request is checked against: task identity, project, user approval state, tool capability, argument schema, risk class, data classification, destination allow-list, current budget, and current worker trust. The policy engine returns allow, deny, or require_approval with a reason code; all decisions are logged.

Risk classes:

| Class | Typical actions | Default |
|---|---|---|
| R0 | Read task metadata, inspect local non-sensitive files | Allow when task-scoped (Fast-Path eligible) |
| R1 | Read project files, query Graphify, query approved read-only APIs | Allow with scope and result limits (Fast-Path eligible) |
| R2 | Modify workspace files, create speculative branches, run bounded local build/test commands | Isolated CoW workspace; active blast-radius check; verification required |
| R3 | External writes, deployments, git push to remote, purchases, sending messages, deleting non-CoW data, overriding a Medium SkillSpector warning | Approval required (with Proof-Carrying Artifact & blast-radius/security summary shown to user) unless narrowly pre-authorized |
| R4 | Secret/policy changes, disabling audit, bypassing SkillSpector High/Critical blocks, broad host access outside CoW sandbox, self-promotion | Deny to autonomous workers |

### 12.2 Security and supply-chain requirements

- Mandatory SkillSpector Ingestion Gate: No SKILL.md file, bundled helper script, or MCP server config may be registered or executed unless its exact SHA-256 bundle digest has a passing skill_scans record (risk_score below threshold, zero Critical/High rules triggered, zero unpatched critical OSV CVEs, and semantic alignment verified by the Local M4 model). Runtime hash verification ensures on-disk skill files are not mutated after scanning.
- Control/Worker Credential Separation: Separate control-plane credentials from worker credentials. Workers receive short-lived scoped tokens.
- Secret Redaction: Never expose secrets to model context or persist them in task prompts, events, call journals, logs, memory, or candidate artifacts. Redact common secret patterns at the Tool Gateway and Local M4 Compressor layer before logs and cloud model calls.
- Network Egress & Code Mode Confinement: Use allow-listed network egress per task class. Code Mode's isolated execution is a useful boundary but does not replace the gateway's authorization, call journaling, and egress policy.
- Untrusted Input Handling: Treat repository content, web pages, tool outputs, memory, AST labels, third-party skills, and generated code as untrusted input, including prompt-injection content.
- Forensic Emergency Stop: Maintain an emergency stop that halts new scheduling, revokes worker leases, and freezes active CoW snapshots without deleting forensic evidence.
- Data Classification: Define at minimum: public, internal, sensitive, secret. Sensitive data defaults to local M4 MLX models unless explicitly approved for a zero-data-retention cloud endpoint.

## 13. Multi-machine deployment & hardware synergy

### 13.1 Target topology

- MacBook Air M4 (16GB Unified Memory):
  - Primary interactive owner client and dashboard host.
  - Tier-1 Local AI Gatekeeper: Runs fast quantized 8B–9B models via MLX (qwen, llama, or gemma variants) for zero-cost log compression, AST context summarization, SkillSpector semantic alignment scans, pre-flight tool schema checks, and private R0/R1 Fast-Path tasks.
  - Uses native macOS APFS clones (cp -c) for instant, zero-storage-overhead CoW workspace snapshots when executing local tasks. May sleep or disconnect at any time.
- Desktop (Windows/Linux Dual-Boot or WSL2/Container Host):
  - Longer-running worker for heavy container sandboxes, parallel speculative test suites, and local compilation workloads.
  - Uses OverlayFS / Btrfs subvolumes for CoW workspace snapshotting.
- Future AI Server:
  - High-VRAM capacity worker for heavy local reasoning models, large-scale Harness² benchmark evaluations, and shared control-plane hosting.
- Control Plane:
  - Initially hosted on the active primary machine (or desktop over local LAN/Tailscale) running Python FastAPI, PostgreSQL 16, and the content-addressed artifact store.

### 13.2 Scheduling, CoW sync, and network

Workers connect outbound to the control plane over mutually authenticated TLS (or private WireGuard/Tailscale overlay); do not expose worker management ports to the public internet. Schedule based on task policy, capability tags, CoW backend support, local MLX availability, trust level, resource estimates, current load, power/online state, and data locality.

When a task migrates between machines (e.g., MacBook Air M4 goes to sleep mid-task and lease expires), the replacement worker on the Desktop reconstructs the workspace from the base git commit plus the lightweight CoW delta archive stored in the Artifact Store, and replays sealed tool calls from the PostgreSQL tool_calls journal.

### 13.3 AX evaluation plan

Google describes Agent Executor (AX) as an open-source runtime for durable execution and distributed deployment (Google Cloud introduction, AX project repository). AX remains an optional ExecutionBackend implementation until it passes a proof of concept.

Proof-of-concept questions: can it preserve/resume our Python worker state and CoW workspace hashes; can our authorization, call journal, and PostgreSQL ledger remain authoritative; how does it compare in operational simplicity against embedding DBOS or Restate directly in Python; how does it deploy across macOS M4 and Linux desktop? Record an ADR with go/no-go and migration cost.

## 14. Observability and operations

- Emit structured JSON logs with task_id, attempt_id, worker_id, step_id, tool_call_id, workspace_tree_sha, trace_id, and policy decision.
- Trace intake → planning (or Fast-Path bypass) → local M4 pre-filtering → cloud/heavy model request → gateway call journal → CoW snapshot → blast-radius check → verification, excluding secrets by default.
- Metrics: queue depth/age, task outcomes, call-journal replay hit rate, CoW snapshot creation/rollback latency, speculative branch win rate, SkillSpector scan pass/quarantine counts, lease expiry, verification pass rate, AST blast-radius catches, local M4 token compression ratio (raw tokens vs. cloud tokens sent), model latency/cost, and policy denials.
- Provide an operator runbook for restore, deterministic call-journal replay, CoW workspace inspection, quarantined skill forensics, task cancellation, lease recovery, key rotation, worker quarantine, and harness rollback.
- Do not log raw chain-of-thought. Store concise rationale, AST impact deltas, and deterministic proof-bundle references.

## 15. API and interface expectations

### 15.1 HTTP API (initial)

| Method and path | Purpose |
|---|---|
| POST /v1/tasks | Submit task (standard or fast_path); accepts Idempotency-Key; returns task ID and initial state |
| GET /v1/tasks/{task_id} | Current task summary, status, budget, active CoW tree SHA, and latest progress |
| GET /v1/tasks/{task_id}/events | Paginated event & call-journal history; optional SSE mode for live updates |
| GET /v1/tasks/{task_id}/proof | Retrieve the signed Proof-Carrying Artifact manifest, AST impact graph, and test hashes |
| POST /v1/tasks/{task_id}:pause | Request cooperative pause and CoW snapshot seal |
| POST /v1/tasks/{task_id}:resume | Resume an eligible task from latest CoW snapshot and call journal |
| POST /v1/tasks/{task_id}:fork | Manually or programmatically fork a checkpoint into parallel speculative attempts |
| POST /v1/tasks/{task_id}:cancel | Cancel, roll back uncommitted CoW layer, and reconcile in-flight effects |
| POST /v1/approvals/{approval_id}:decide | Approve or reject the exact pending action (with blast-radius & diff preview) |
| GET /v1/workers | Worker health, CoW capabilities, and local MLX status |
| GET /v1/tasks/{task_id}/artifacts | List authorized artifact metadata and retrieval links |
| GET /v1/skills | List available skills, pinned versions, and SkillSpector security badge status |
| POST /v1/skills:import | Import or rescan a SKILL.md directory bundle through the SkillSpector gate |
| GET /v1/skills/{skill_id}/scans | Inspect detailed SkillSpector static, OSV, and semantic alignment reports |
| GET /v1/skills/{skill_id}/revisions | Inspect version history and evaluation status |
| POST /v1/tasks/{task_id}:invoke-skill | Create a task using a verified, pinned skill revision and explicit inputs |

### 15.2 Local web control surface

The local dashboard is the primary owner experience. Its initial views are:

1. Work: queued/running/speculating/blocked/recent tasks with live token-savings indicators and Fast-Path toggle.
2. Task Detail & Proof Inspector: state, Dynamic DAG visualization, event/call-journal timeline, AST Blast-Radius Impact Map, speculative branch comparison table, and the Proof-Carrying Artifact receipt (clickable git diff, test output hash, and runtime digest).
3. Approvals: showing the exact R3 action, parameters, SkillSpector Medium override warnings, and downstream codebase impact to approve.
4. Skills & Supply-Chain Security: for browsing, importing, invoking, pinning, and disabling Git-backed SKILL.md workflows, featuring a live SkillSpector Security Badge (0–100 risk score, OSV status, and semantic alignment diff) for every revision.
5. Projects, Routing & Hardware: for project roots, Two-Tier Model Router rules (MacBook M4 local model selection vs. cloud APIs), budgets, and worker health.

### 15.3 CLI (initial)

Provide commands conceptually equivalent to init, up, submit (with --fast flag), status, events, proof, fork, approve, pause, resume, cancel, workers, skills list/show/import/scan/run/pin/disable, memory inspect, memory forget, and eval report.

### 15.4 Worker and adapter interfaces

Define typed Python protocols for ExecutionBackend, WorkspaceSnapshotProvider (CoW), SkillSecurityScanner (SkillSpector), ModelProvider, LocalCompressorProvider, ToolProvider, MemoryProvider, GraphProvider (with compute_blast_radius hook), ArtifactStore, and Verifier. Every adapter reports capability/version metadata and supports timeouts, cancellation, and structured errors.

## 16. Evaluation and acceptance criteria

### 16.1 Stage 1 acceptance (Core, Fast-Path, Call Journal & CoW)

- Restart the API and worker mid-step (e.g., after tool call 3 of 5); task resumes, replays calls 1–3 from the PostgreSQL tool_calls journal in under 50 ms without re-executing them, and completes calls 4–5.
- Submit an R0/R1 repo query via the Fast-Path Express Lane and confirm tool execution begins within 2 seconds while still writing complete tool_calls and task_events audit rows.
- Simulate a failed code edit in an R2 workspace; verify that rolling back the checkpoint via WorkspaceSnapshotProvider (APFS clone on macOS or OverlayFS on Linux) restores a 100% clean working tree in under 2 seconds.
- Every SUCCEEDED task produces a valid proof_bundles record containing verifiable SHA-256 hashes of the diff, workspace tree, and test execution output.
- Two workers attempting one task cannot both commit results because the fencing token rejects the stale worker.

### 16.2 Stage 2 acceptance (Active Graphify, Memory & SkillSpector Gate)

- Modify a shared function during an R2 task; confirm the Active Blast-Radius Interceptor identifies 1-hop/2-hop callers via Graphify, injects an impact warning into the prompt, and automatically appends downstream caller tests to the mandatory verification plan.
- Attempt to import four adversarial SKILL.md test bundles: (1) hidden zero-width Unicode prompt injection, (2) symlink pointing to ~/.ssh, (3) a helper script with a known critical CVE in requirements.txt, and (4) a benign-looking "Markdown linter" skill whose instructions secretly run curl to an external webhook. Confirm NVIDIA SkillSpector (static + Local M4 semantic mode) catches and quarantines (status = quarantined) all four with 100% recall.
- Modify an active SKILL.md file on disk without re-scanning; confirm the worker refuses to load the skill due to a SHA-256 hash mismatch against skill_scans.
- Demonstrate project memory isolation with two projects and a personal-memory-disabled task.

### 16.3 Stage 3 acceptance (Gateway, Code Mode & Two-Tier Routing)

- Run a multi-tool Code Mode script that crashes on call #4; verify that re-running the script replays calls #1–#3 from the Tool Gateway journal with identical policy checks and zero duplicate side effects.
- Demonstrate the Two-Tier Local/Cloud Pipeline: feed a 20,000-token raw test failure log into the MacBook Air M4 local MLX compressor and verify it reduces cloud prompt token consumption by greater than 70% while preserving the exact failing file, line number, and assertion error.
- Test model routing across privacy classes and provider failures; sensitive data never leaves the local M4/Desktop boundary unless explicitly overridden.

### 16.4 Stage 4 acceptance (Distributed & Speculative Execution)

- Fork a bug-fix task into two parallel speculative attempt_id branches on isolated CoW snapshots; verify zero filesystem interference, automatic selection of the branch that passes verification, and clean garbage collection of the losing snapshot.
- Disconnect the MacBook Air M4 during execution and confirm safe lease expiry, CoW delta transfer, call-journal replay, and continuation on the Desktop worker.
- Complete AX proof-of-concept report and ADR comparing it against the native worker + DBOS/Postgres foundation.

### 16.5 Stage 5 acceptance (Controlled Self-Improvement)

- Reproduce benchmark reports from immutable inputs and pinned revisions.
- Verify that any candidate skill or prompt modification generated during a Harness² Propose–Probe–Compose loop is automatically routed through SkillSpector before benchmark execution.
- Reject candidates that change protected policy files, trigger High/Critical SkillSpector findings, or tamper with evaluation inputs.
- Complete Harness² evaluation and license/security review; demonstrate one-step rollback.

### 16.6 Quality scorecard

Track by task class:

- Task completion and cryptographically verified correctness rate;
- Silent regression catch rate via Active AST Blast-Radius checks;
- SkillSpector adversarial skill catch rate (target: 100% on test suite) and false-positive rate;
- Call-journal replay hit rate and CoW rollback speed;
- Cloud token cost reduction (%) achieved by the Local M4 Gatekeeper/Compressor tier;
- Safe refusal / approval routing accuracy;
- Recovery success and duplicate side-effect rate (target: 0%);
- Memory precision, stale-memory rate, and cross-scope leakage rate (target: 0%).

## 17. Rollout plan and milestones

| Milestone | Deliverable | Gate |
|---|---|---|
| M0: Design freeze & DBOS eval | ADRs (including DBOS/Restate vs. raw SQL queue), task/journal/scan schema, CoW provider contract, threat model | Owner approves scope and policies |
| M1: Durable CoW workbench | Guided init/up, dashboard, CLI, Fast-Path lane, Postgres ledger + call journal, APFS/OverlayFS CoW adapter, Proof-Carrying Artifacts | Stage 1 mid-step replay and CoW rollback acceptance complete |
| M2: Focused personal pilot | Repository understanding, bounded bug fixes, source-backed technical briefs; recovery drills | No unresolved critical reliability/security issue |
| M3: Active Graphify, Memory & SkillSpector | Project memory, Graphify blast-radius interceptor, Git-backed SKILL.md registry + NVIDIA SkillSpector gate | Stage 2 isolation, blast-radius catch, and 100% adversarial skill quarantine complete |
| M4: Gateway & Two-Tier Router | Memoizing gateway, Code Mode experiment, Local M4 MLX compressor + Cloud router, cost report | Stage 3 acceptance and >50% log token compression demonstrated |
| M5: Multi-machine & Speculation | MacBook M4 and Desktop workers; CoW delta sync; parallel speculative branching (fork) | Stage 4 native backend and speculative merge acceptance complete |
| M6: Runtime evaluation | AX proof of concept and ADR | Adopt only if evidence supports lower risk/cost than native + Postgres |
| M7: Evaluation and improvement | Candidate registry, benchmark suite, Harness² experiment gated by SkillSpector | Stage 5 gates and rollback demonstrated |
| M8: Broader autonomy | Carefully expanded task classes, thin outbound approval webhooks, and budgets | Explicit review of scorecard and incident history |

## 18. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Trojanized third-party SKILL.md, MCP server, or self-modified skill | Prompt injection, secret exfiltration, or unauthorized tool execution | NVIDIA SkillSpector gate (FR-039e) runs 64+ static/OSV rules + Local M4 semantic alignment before registration; SHA-256 runtime pinning |
| Custom distributed lock/queue bugs in Postgres | Split-brain execution or stuck tasks | Evaluate embedding DBOS/Restate in M0; strict fencing tokens, CAS transitions, and call-level journaling |
| Mid-script failure in Code Mode leaves partial side effects | Duplicate API writes or broken workspace | Gateway Call-Level Journaling (FR-015) replays sealed calls; CoW snapshot (FR-012a) reverts dirty files instantly |
| Agent fixes local bug but silently breaks distant callers | Hidden regressions in large repos | Active Graphify Blast-Radius Interceptor (FR-036a) injects structural warnings and appends caller tests to Verification Plan |
| Heavy orchestration ceremony slows down simple queries | Poor daily ergonomics compared to CLI agents | R0/R1 Fast-Path Express Lane (FR-001a) bypasses DAG planning for <2s execution while preserving audit logs |
| Cloud API token costs explode on large repo logs/traces | Budget overrun | Two-Tier Routing (FR-045a) uses local MacBook Air M4 MLX models to compress logs and run semantic skill scans at $0 API cost |
| CoW filesystem differences across macOS and Linux | Inconsistent snapshot behavior | Strict WorkspaceSnapshotProvider contract tests covering cp -c (APFS) and OverlayFS/Btrfs with tar-zstd delta fallback |
| Machine sleep/network partition | Stalled work or duplicate execution | Leases, fencing tokens, CoW delta sync, safe reassignment |
| AX or Harness² immaturity / breaking changes | Lock-in or regressions | Native backend and immutable evaluation gates remain baseline; adapters are strictly optional |

## 19. Milestone artifacts and documentation

The implementation should maintain:

- Architecture Decision Records (ADRs) for database/durable-execution engine, CoW workspace backends (APFS/OverlayFS), call journaling, SkillSpector integration, sandbox, two-tier model routing, Active Graphify, Code Mode, AX, and Harness²;
- A versioned OpenAPI spec and worker/adapter protocol documentation;
- Threat model, data classification, retention schedule, and operator runbooks;
- Proof-Carrying Artifact JSON schema and cryptographic verification script;
- Benchmark definitions, adversarial skill test corpus, scoring rubrics, dataset licenses, and promotion reports;
- Machine setup instructions for MacBook Air M4 (MLX + APFS + SkillSpector profile), Desktop (Linux/WSL2 + OverlayFS/GPU profile), and future server profiles.

## 21. External references and validation notes

- NVIDIA, SkillSpector repository: open-source security and malware scanner for AI agent skills (SKILL.md, bundled scripts, and MCP definitions) detecting 64 vulnerability patterns across 16 categories via static analysis, OSV.dev lookups, and LLM semantic alignment.
- Cloudflare, Code Mode documentation: generated code composes tool calls; documentation labels Code Mode experimental.
- Cloudflare, Code Mode MCP server patterns: single-code-tool and search/execute patterns, with execution and authentication boundary details.
- Google Cloud, Introducing Agent Executor: overview of Google's distributed agent runtime and durable execution goals.
- Google, Agent Executor repository: validate current source, maturity, APIs, and deployment guidance at evaluation time.
- Google Research, Harness² repository: project description, setup, executors, benchmarks, and disclaimer that it is not an officially supported Google product.
- Xu et al., Harness²: Recursive Agent Harnessing for an Open World: describes task-level Propose–Probe–Compose and harnessing-level persistence of evidence-supported improvement lessons.
- Graphify Labs, Graphify repository: project description and implementation details; validate pinned release and local behavior before adoption.
- DBOS, DBOS Transact Python documentation: lightweight Postgres-backed durable execution and workflow journaling candidate for M0 evaluation.

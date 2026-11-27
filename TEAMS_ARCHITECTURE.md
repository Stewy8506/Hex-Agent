# HEX Teams Architecture: Multi-Agent Collaborative Coding

> **Status:** Architectural Specification (v2 — Upgrades `BACKEND_PLAN.md` §4 Single-Host Execution to Peer-Executed `jj` + Iroh QUIC + Two-Lane Sync)  
> **Target Audience:** Small-to-medium trusted engineering teams (2–6 developers) requiring local-first speed, zero cloud code storage, and real-time multi-agent collaboration.

---

## 1. Executive Summary & Design Principles

HEX Teams lets multiple developers connect their local HEX agents to work on the same codebase simultaneously without storing source code, chats, or memory in a central cloud database.

Rather than running all teammates' agents on a single person's laptop over WebRTC—which creates a severe remote-code-execution (RCE) surface, saturates one host's CPU/RAM, causes working-tree collisions, and breaks on abrupt disconnects—HEX Teams uses a **Peer-Executed, Coordinator-Sequenced Mesh**:

1. **Local Execution, Shared Coordination:** Every teammate's agent executes tools, builds, and tests **locally** on its owner's machine inside an isolated [Jujutsu (`jj`)](https://github.com/jj-vcs/jj) workspace + Copy-on-Write (CoW) snapshot. One peer holds a lightweight, rotating **Coordinator** lease (`host_epoch`) to sequence merges, track soft scope locks, and broadcast signed deltas.
2. **Zero Hardcoded Agent Roles:** AI agents stay general-purpose and task-driven. Only the Coordinator daemon role is special, and it rotates automatically on failover.
3. **Two-Lane Real-Time Synchronization:** A **Fast Lane** streams sub-second structured intent and interface-contract events over [Iroh](https://docs.iroh.computer/about/faq) QUIC, while a **Slow Lane** replicates immutable, Ed25519-signed `jj` turn deltas and `ledger.db` checkpoints after each completed turn.
4. **Graphify as a Local Derived Index & Collision Guard:** [Graphify](https://github.com/Graphify-Labs/graphify) (`.hex/graph.json`) is never shipped over the network. Each peer updates its Tree-sitter AST graph incrementally from incoming code diffs and uses it both to partition work into non-overlapping subgraphs and to filter live peer activity into agent tool outputs without invalidating Turn-0 KV caches.
5. **Minimal-DB Control Plane:** `hex-backend` stores only auth, team RBAC, encrypted device IDs + Ed25519 public keys, and the monotonic `host_epoch` lease record. Zero source code, prompts, or diffs touch the database.

### Industry Context & Why Not OpenAI Pando or Anthropic Scion?

| System | Approach | Why HEX Teams Uses a Different Path |
| :--- | :--- | :--- |
| **[OpenAI Pando](https://openai.com/index/scaling-git-at-openai/)** (Nov 2026) | Horizontally scalable Git server backend using per-branch causal consistency and packfile reuse (~5× lower push latency). | Internal server-side monorepo backend, not a P2P local-first sync layer. |
| **[Anthropic Scion](https://www.anthropic.com/research/redesigning-source-control)** (Oct 2026) | Proprietary, centralized, online-first VCS built for >30,000 merges/day; moves commits/merges to servers with sparse/virtualized checkouts. | Closed-source and explicitly *"by no means a suitable replacement for git for general use."* HEX borrows its **derived-data** insight (recomputing local Graphify indices from deltas). |
| **[Zed Multiplayer](https://scientyficworld.org/zed-code-editor/)** | Host-anchored CRDT buffer sharing with real-time cursors and agent collaboration. | Requires trusting collaborators with host file access; optimized for shared-buffer editing rather than multi-agent parallel test/build execution. |
| **HEX Teams (`jj` + Iroh + Graphify)** | Peer-executed [Jujutsu (`jj`)](https://github.com/jj-vcs/jj) workspaces + [Iroh](https://docs.iroh.computer/about/faq) QUIC mesh + local [Graphify](https://github.com/Graphify-Labs/graphify) scoping + epoch-fenced failover. | Open-source, Git-compatible, zero cloud code storage, no cross-machine RCE, and resilient to abrupt laptop disconnects. |

---

## 2. System Topology & Component Responsibilities

```text
                       ┌───────────────────────────────────────┐
                       │        hex-backend (Minimal DB)       │
                       │  Auth · Team RBAC · Ed25519 Keys ·    │
                       │  project_session_id · host_epoch      │
                       └───────────────────┬───────────────────┘
                                           │ Lease / Discovery (HTTPS)
          ┌────────────────────────────────┼────────────────────────────────┐
          │                                │                                │
          ▼                                ▼                                ▼
┌───────────────────────┐      ┌───────────────────────┐      ┌───────────────────────┐
│  Peer A (Coordinator) │◄────►│    Iroh QUIC Mesh     │◄────►│   Peers B, C, D ...   │
│  ───────────────────  │ QUIC │  • Key-dialed E2EE    │ QUIC │  ───────────────────  │
│  • Local Agent + jj   │      │  • ~90% Direct UDP    │      │  • Local Agent + jj   │
│  • Merge Sequencer    │      │  • Stateless E2EE     │      │  • Local CoW Sandbox  │
│  • Scope Lock Table   │      │    Relay Fallback     │      │  • Local Graphify AST │
│  • Local Graphify AST │      └───────────────────────┘      │  • Local ledger.db    │
│  • Local ledger.db    │                                     │  • Team Blackboard    │
└───────────────────────┘                                     └───────────────────────┘
```

### Component Breakdown

| Component | Owner / Location | Responsibilities |
| :--- | :--- | :--- |
| **Minimal Control Plane** | `hex-backend` (FastAPI + SQLite WAL) | User auth, Team RBAC, device Ed25519 public keys, `project_session_id` + `repo_fingerprint`, and atomic `host_epoch` Coordinator leases. |
| **Coordinator Daemon** | Active lease holder (any eligible peer) | Sequences incoming `TurnCommitBundle` merges onto the shared tip, maintains the soft AST scope-lock table, stamps monotonic `turn_seq`, and fans out signed deltas. **Does not execute teammates' code.** |
| **Peer Agent Runtime** | Every teammate's machine (`agent/orchestrator.py`) | Runs the 12-phase deterministic state machine (`QUEUED → PLANNING → SPECULATING → RUNNING → VERIFYING → COMPLETED`) inside a local CoW / `jj` workspace using the owner's local R0–R4 Tool Gateway and model router. |
| **Iroh Transport Layer** | `cloud/team_mesh.py` / Rust crate | Establishes TLS 1.3 QUIC streams between peers by Ed25519 Node ID, handles NAT hole-punching, and falls back transparently to stateless encrypted relays ([Iroh 1.0](https://pinggy.io/blog/iroh_1_0_dial_keys_not_ips/)). |
| **Active Graphify Index** | Local `.hex/graph.json` on each peer | Computes Tree-sitter AST blast radii for scope locking, detects public signature changes, and filters live peer presence into JIT tool observations. |
| **Team Blackboard** | In-memory + replicated `.hex/team_blackboard.jsonl` | Stores active peer claims, planned contract changes, bounded `ask_peer` Q&A, and shared architectural decisions. |

---

## 3. Dynamic Roles & Team RBAC

### No Hardcoded AI Agent Personas

HEX Teams **never** hardcodes roles like "Frontend Agent", "Backend Agent", or "QA Agent" into the protocol. Hardcoded personas stall the entire team whenever one developer closes their laptop. Instead:

*   **Human-Steered Mode (Default):** Each developer prompts their own local agent on any feature, bug, or refactor. Before writing files, the agent requests a soft scope claim from the Coordinator. Disjoint scopes execute immediately in parallel.
*   **Shared Task Queue Mode (Optional Swarm):** A team drops a backlog of sub-tasks into the session blackboard. Any idle peer's agent dynamically claims the next unblocked task whose [Graphify](https://github.com/Graphify-Labs/graphify) blast radius does not overlap active claims.
*   **Optional User Specialization:** Individual developers can still run specialized local modes (`plan`, `agent`, `code`) or skills (e.g., security review, test generation) via their own `SOUL.md` / `~/.hex/skills/`—the sync protocol remains completely agnostic.

### Team RBAC Matrix

| Role | Manage Members & Invites | Create Project Sessions | Eligible as Coordinator | Submit Turns & Approve Local R0–R4 | Trigger Manual Coordinator Handoff |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Owner** | Yes | Yes | Yes | Yes | Yes |
| **Admin** | Yes | Yes | Yes | Yes | Yes |
| **Member** | No | Yes | Yes | Yes | No |
| **Viewer** | No | No | No | Read-only (live feed & diffs) | No |

---

## 4. Workspace & Version Control Layer (`jj` + CoW Snapshots)

### Why Jujutsu (`jj`) + CoW Snapshots

Traditional Git blocks a working tree whenever a merge conflict occurs and requires brittle branch/worktree juggling for concurrent agents. Pairing HEX's existing Copy-on-Write isolation (`storage/cow.py` — APFS `cp -cR` on macOS, OverlayFS on Linux) with **[Jujutsu (`jj`)](https://github.com/jj-vcs/jj)** solves multi-agent concurrency cleanly:

*   **Full Git Compatibility:** `jj` runs colocated on top of standard `.git/` repositories—teams keep GitHub/GitLab PR workflows with zero migration friction.
*   **First-Class Non-Blocking Conflicts:** If two agents modify overlapping lines, `jj` records the conflict as a structured object inside the commit rather than halting the working copy ([Avoid Losing Work with Jujutsu](https://www.panozzaj.com/blog/2025/11/22/avoid-losing-work-with-jujutsu-jj-for-ai-coding-agents/)). Other peers continue working while one agent runs a targeted reconciliation turn.
*   **Cheap Operation Log & Snapshots:** Every speculative agent step is automatically snapshotted and undoable (`jj op log`), aligning with HEX's deterministic recovery ledger (`ledger.db`).
*   **Content-Addressed Deltas:** Peers exchange only compact Git/`jj` object deltas + `ProofArtifact` Merkle roots (`capabilities/proof.py`), never full repository tarballs.

### Turn Commit Bundle Format

When a peer finishes a verified turn (`VERIFYING → COMPLETED`), it signs and sends a `TurnCommitBundle` to the Coordinator:

```json
{
  "type": "TURN_COMMIT_BUNDLE",
  "project_session_id": "sess_01J...",
  "host_epoch": 7,
  "base_turn_seq": 42,
  "author_device_id": "ed25519:9f8c...",
  "jj_change_id": "kzvqpxmw...",
  "git_tree_oid": "a1b2c3d4...",
  "pack_blake3": "e5f6a7b8...",
  "proof_merkle_root": "sha256:...",
  "touched_symbols": ["src/auth.rs::AuthService::verify_token"],
  "ledger_checkpoint_seq": 1089,
  "blackboard_notes": ["Switched verify_token to return Result<Claims, AuthError>"],
  "ed25519_signature": "sig_..."
}
```

---

## 5. Transport Layer: Iroh QUIC Mesh (Replacing WebRTC)

[WebRTC](https://docs.iroh.computer/about/faq) was designed for browser audio/video and forces native apps to carry SDP, ICE, DTLS, SCTP, a custom signaling layer, and expensive stateful TURN servers. HEX Teams uses **[Iroh](https://docs.iroh.computer/about/faq)** (native Rust QUIC with official Python bindings) instead:

*   **Cryptographic Key Dialing:** Peers dial each other directly by their Ed25519 Node ID (already stored in `hex-backend`'s device registry) instead of IP addresses.
*   **Built-In NAT Traversal & Stateless Relays:** Iroh uses QUIC-NAT-Traversal (QNT) to punch direct UDP paths ~90% of the time (~95% of data volume stays direct) and transparently falls back to cheap, stateless, end-to-end encrypted relays when symmetric NATs or corporate firewalls block direct UDP ([Iroh 1.0](https://pinggy.io/blog/iroh_1_0_dial_keys_not_ips/)).
*   **Multiplexed ALPN Channels:** A single encrypted QUIC connection between two peers multiplexes four logical streams without head-of-line blocking:
    1. `hex/presence/1` — Sub-second `INTENT_CLAIM`, `CONTRACT_CHANGE`, and `HEARTBEAT` frames.
    2. `hex/delta/1` — BLAKE3-verified `TurnCommitBundle` and Git/`jj` pack streams.
    3. `hex/query/1` — Bounded 1-hop `ask_peer` contract queries.
    4. `hex/ui-feed/1` — Optional human-facing live status/diff previews for `hex-desktop` and `hex-tui`.

---

## 6. Two-Lane Real-Time Synchronization & Simultaneous Agent Awareness

Syncing code only *after* a 2–5 minute turn causes agents to duplicate helpers or code against stale function signatures. Conversely, dumping raw LLM thought streams from three teammates into every agent's prompt wastes tokens and destroys KV-cache hit rates.

HEX Teams solves this with **Two-Lane Sync**:

### Lane 1: Fast Lane (Sub-Second Presence & Intent Bus)

Agents emit tiny (~40-token) structured events over `hex/presence/1` at state transitions:

```json
{
  "type": "INTENT_CLAIM",
  "peer_id": "ed25519:4a2b...",
  "developer": "arjun",
  "state": "RUNNING",
  "goal": "Refactor AuthService.verify_token to return Result<Claims, AuthError>",
  "claimed_files": ["src/auth.rs"],
  "blast_radius_files": ["src/middleware.rs", "tests/auth_test.rs"],
  "ttl_sec": 300
}
```

When Tree-sitter detects a public signature, struct, or API route change *during* an in-progress turn, the local harness fires an immediate `CONTRACT_CHANGE` event:

```json
{
  "type": "CONTRACT_CHANGE",
  "peer_id": "ed25519:4a2b...",
  "symbol": "src/auth.rs::AuthService::verify_token",
  "old_signature": "pub fn verify_token(&self, token: &str) -> Claims",
  "new_signature": "pub fn verify_token(&self, token: &str, keys: &KeySet) -> Result<Claims, AuthError>",
  "status": "in_progress"
}
```

### Graphify-Filtered JIT Context Injection (KV-Cache Safe)

*   **Never mutate the system prompt mid-turn** (preserves HEX's Turn-0 KV-cache prefix freeze).
*   Every peer maintains the live **Team Blackboard** in local memory.
*   Whenever a local agent calls a read/edit/search tool (`read_file`, `grep`, `edit_file`), the tool gateway checks whether the target file or symbol lies within **1–2 AST hops** of any active peer's `INTENT_CLAIM` or `CONTRACT_CHANGE` in local [Graphify](https://github.com/Graphify-Labs/graphify) (`.hex/graph.json`).
*   **If there is zero overlap:** Nothing is injected—the agent sees zero noise.
*   **If there is an overlap:** The harness appends a compact `<peer_activity>` block (~80–150 tokens) to that tool's observation output:

```xml
<peer_activity relevance="1-hop-caller">
  Peer "arjun" (state: RUNNING) is refactoring `src/auth.rs::AuthService::verify_token`.
  Planned signature: `pub fn verify_token(&self, token: &str, keys: &KeySet) -> Result<Claims, AuthError>`
  Guidance: Avoid editing `src/auth.rs`; build callers against the planned signature or call `ask_peer`.
</peer_activity>
```

### Bounded 1-Hop Peer Query (`ask_peer`)

Free-form multi-agent group chats spiral into infinite polite loops. Instead, agents get a strictly bounded tool:

*   **Tool:** `ask_peer(peer_id: str, symbol: str, question: str)`
*   **Hard Cap:** Maximum **1 request → 1 structured reply** per symbol per turn (no recursive replies).
*   **Execution:** Answered on the target peer from its active plan/blackboard context (or local fast MLX model), then cached on the shared blackboard so other peers do not repeat the same question.

### Lane 2: Slow Lane (Durable Turn Deltas & Semantic Memory)

After a turn passes local verification (`VERIFYING → COMPLETED`), the Coordinator merges the `TurnCommitBundle`, increments `turn_seq`, and broadcasts:
1. The signed `jj`/Git delta pack.
2. The `ledger.db` checkpoint metadata + `ProofArtifact` Merkle root.
3. Shared repository decisions/invariants (`.hex/MEMORY.md` & `.hex/memories/GUARDRAILS.md` deltas).

Each receiving peer applies the commit to its local repo, checkpoints SQLite WAL, and triggers an incremental local Graphify AST re-index on only the modified files.

---

## 7. Active Graphify AST Scope Locking & Conflict Resolution

### Lifecycle of a Single Collaborative Turn

1. **Plan & Scope Request:** Peer B enters `PLANNING`, queries local [Graphify](https://github.com/Graphify-Labs/graphify) for its target symbols and 1-hop callers, and sends an `INTENT_CLAIM` to the Coordinator.
2. **Soft Lock Evaluation:**
   *   *Disjoint scope:* Coordinator grants an immediate soft lock (TTL 5 min, auto-renewed by heartbeats) and broadcasts the claim.
   *   *Overlapping scope:* Coordinator replies with `SCOPE_OVERLAP(holding_peer, symbols)`. Peer B's agent either (a) pivots to a non-overlapping sub-task, (b) waits on the lock, or (c) proceeds optimistically in its isolated [`jj`](https://github.com/jj-vcs/jj) workspace knowing a reconciliation turn will follow.
3. **Local Execution & Verification:** Peer B edits, compiles, and runs tests entirely on its own laptop.
4. **Coordinator Merge & Sequence:** Peer B streams the signed `TurnCommitBundle`. Coordinator verifies Ed25519 signature + `host_epoch`, applies the change onto the shared tip via `jj`, and stamps `turn_seq + 1`.
5. **Reconciliation on Conflict:** If two concurrent turns touch the exact same lines, `jj` records a clean conflict marker inside the merged commit without blocking other peers. The Coordinator emits `CONFLICT_RECONCILE_NEEDED(change_id, files)` and assigns a short reconciliation turn to the later author's agent (with both diffs and both intents attached).

---

## 8. Coordinator Lease, Heartbeats & Epoch-Fenced Failover

This protocol extends `BACKEND_PLAN.md` §4.4 while eliminating the fragile "sync right before going offline" assumption—because every completed turn is already replicated across peers.

### Normal Operation

*   **Heartbeats:** Coordinator broadcasts `HEARTBEAT(host_epoch, turn_seq, active_locks_hash)` every **3 seconds** over `hex/presence/1` and renews its lease with `hex-backend`.
*   **Checkpoint Registration:** After broadcasting a committed turn to peers, Coordinator records the minimal pointer `POST /v1/sessions/{project_session_id}/turn-commit` (`host_epoch`, `turn_seq`, `git_tree_oid`, `proof_merkle_root`) in `hex-backend`.

### Abrupt Failure & Deterministic Election

1. **Detection:** If connected peers miss Coordinator heartbeats for **>10 seconds**, eligible online peers (`Owner`, `Admin`, `Member`) call `POST /v1/sessions/{project_session_id}/failover/claim` with their highest verified `last_turn_seq` and `git_tree_oid`.
2. **Selection:** `hex-backend` filters eligible online peers with `max(last_turn_seq)` and atomically grants `host_epoch = old_epoch + 1` to the winning peer (tie-breaking by deterministic priority / lowest latency).
3. **Resumption:** The newly elected Coordinator broadcasts `COORDINATOR_ELECTED(host_epoch, turn_seq)` over Iroh. Peers validate the backend-signed lease token, re-register their active local `INTENT_CLAIM`s, and resume immediately.
4. **In-Flight Work Recovery:** Any turn that was *mid-execution* on a surviving peer continues uninterrupted and submits to the new `host_epoch`. Only an uncommitted, un-pushed turn running on the crashed laptop itself is paused until that laptop reconnects.

### Split-Brain Prevention (Fencing Tokens)

*   Every Coordinator action (`SCOPE_GRANT`, `TURN_BROADCAST`, `turn-commit`) must include `host_epoch`.
*   If the crashed former Coordinator wakes up from sleep and attempts to broadcast with `old_epoch < current_epoch`, every peer and `hex-backend` immediately rejects it.
*   The returning peer detects the higher epoch, drops its stale Coordinator role, pulls missed `turn_seq` deltas from the active Coordinator over Iroh, and rejoins cleanly as a regular peer.

### Graceful Admin Handoff

An `Owner` or `Admin` can transfer Coordinator duties cleanly at any time via `POST /v1/sessions/{project_session_id}/handoff`—draining in-flight merge requests, incrementing `host_epoch`, and promoting the target peer with zero downtime.

---

## 9. Security, Privacy & Trust Boundaries

*   **No Cross-Machine Remote Code Execution:** Because each agent runs commands strictly inside its owner's local sandbox, a compromised or buggy teammate agent cannot run arbitrary shell commands or read secrets (`.env`, SSH keys) on another developer's machine.
*   **Local Risk-Tiered Tool Gateway (R0–R4) & SkillSpector:** Each peer enforces its own R0–R4 approval policies, strips secret environment variables in subprocesses, and gates imported skills locally.
*   **End-to-End Encryption & Zero-Knowledge Relays:** All Iroh QUIC streams use TLS 1.3 authenticated by Ed25519 device keys. Even when traffic traverses a stateless Iroh relay, the relay sees only opaque ciphertext and never stores payloads ([Iroh FAQ](https://docs.iroh.computer/about/faq)).
*   **Cryptographic Provenance:** Every `TurnCommitBundle` carries the author's Ed25519 signature and `ProofArtifact` Merkle root so every peer can verify authorship and integrity before applying a diff.
*   **Instant Device Revocation:** Revoking a compromised device key in `hex-backend` propagates via the next lease/manifest check and immediately drops that peer from the Iroh mesh.

---

## 10. Human UI / UX Surface (`hex-desktop` & `hex-tui`)

What humans see in the UI is intentionally richer than what gets injected into LLM contexts:

*   **Team Session Header:** Shows active `project_session_id`, `host_epoch`, Coordinator badge (with one-click Admin Handoff), and direct vs. relayed Iroh connection indicator per peer.
*   **Live Multi-Agent Activity Rail:** Displays every online peer's current state (`PLANNING`, `RUNNING`, `VERIFYING`), claimed Graphify files/symbols, live diff preview on click, and test pass/fail badges.
*   **Conflict & Scope Badges:** Highlights soft-lock overlaps in amber and `jj` conflict-reconciliation turns in red with one-click diff inspection.
*   **Blackboard Drawer:** Lets humans read—or pin manual architectural notes to—the shared Team Blackboard so all four agents immediately respect human design decisions.

---

## 11. HEX Codebase Integration Map & Phased Rollout

### File & Module Mapping

| Existing HEX Module | Required Changes for Teams v2 |
| :--- | :--- |
| `cloud/team_mesh.py` | Replace WebRTC/signaling transport stubs with [Iroh](https://docs.iroh.computer/about/faq) QUIC bindings (Ed25519 Node ID dialing + 4 ALPN streams). |
| `cloud/session.py` | Update `TEAM_PRIMARY_HOST` → `TEAM_COORDINATOR` (logical sequencer) and `TEAM_REPLICA_PEER` → `TEAM_PEER` (local executor); wire `host_epoch` fencing and 3s/10s heartbeat failover. |
| `storage/cow.py` | Add colocated [`jj` workspace](https://github.com/jj-vcs/jj) adapter alongside APFS CoW / OverlayFS snapshots for delta packing and non-blocking conflict commits. |
| `capabilities/proof.py` | Sign `TurnCommitBundle` payloads and `ProofArtifact` Merkle roots with the local Ed25519 device key. |
| `agent/orchestrator.py` | Emit `INTENT_CLAIM` / `CONTRACT_CHANGE` events on state transitions; hook Graphify-filtered `<peer_activity>` JIT injection into tool observations; register bounded `ask_peer` tool. |
| `crates/hex-protocol/` & `hex_cli/acp.py` | Add typed protocol frames for Presence, Delta Sync, Blackboard notes, and `ask_peer` RPCs. |
| `App/hex-desktop` & TUI | Add Team Session Panel, Coordinator/Epoch badge, Live Multi-Agent Activity Rail, and Blackboard Drawer. |

### Recommended Implementation Phases

1. **Phase A — Transport & Local Isolation:** Integrate Iroh QUIC peer mesh (`cloud/team_mesh.py`) + colocated `jj` workspace delta export/import (`storage/cow.py`) + signed `TurnCommitBundle` verification.
2. **Phase B — Logical Coordinator & Epoch Failover:** Wire `host_epoch` lease acquisition, 3s/10s heartbeats, deterministic failover election, and stale-leader fencing against `hex-backend`.
3. **Phase C — Fast Lane Presence & Graphify JIT Sync:** Add `INTENT_CLAIM`, Tree-sitter `CONTRACT_CHANGE` detection, local Team Blackboard, Graphify 1–2 hop relevance filtering into tool observations, and bounded 1-hop `ask_peer`.
4. **Phase D — UI & Optional Swarm Queue:** Ship the `hex-desktop` / `hex-tui` Live Activity Rail, Blackboard Drawer, and optional shared task queue for autonomous multi-agent claiming.

### When to Add an Optional Cloud Sandbox Mode (Future Scale Path)

This peer-executed P2P architecture is optimal for **2–6 trusted developers** who want zero cloud code storage and local Apple Silicon / workstation speed. If HEX Teams later expands to **10+ developers, untrusted external contractors, or 24/7 unattended background agent swarms** where laptops sleep for hours, add an optional **Hosted Ephemeral Sandbox** execution target (closer to [Anthropic Scion](https://www.anthropic.com/research/redesigning-source-control)'s always-online model) while keeping the exact same `TurnCommitBundle`, Graphify scoping, and Blackboard protocol unchanged.

# Custom Autonomous AI Harness

A personal, extensible autonomous AI harness for project-scoped coding and technical research. It plans and executes bounded work inside Copy-on-Write (CoW) workspaces, enforces default-deny tool policies (`R0–R4`), journals every tool call for `0 ms` crash replay, intercepts AST blast-radius impacts via Graphify, scans skills for supply-chain threats via NVIDIA SkillSpector, and integrates Google Research's Harness² (`Propose–Probe–Compose`) behind a controlled self-improvement gate.

## Key Capabilities

- **Copy-on-Write (CoW) Workspace Durability (`src/harness_core/cow.py`):** Uses native macOS APFS zero-copy clones (`cp -c -R`) with portable fallback and deterministic SHA-256 Merkle tree hashing (`workspace_tree_sha`) for millisecond branching and rollback.
- **0 ms Call-Level Gateway Journaling (`src/harness_core/gateway.py`):** Records every tool call before execution and seals its output upon completion. Mid-step or Code Mode script crashes replay sealed calls in `0 ms` without duplicate side effects.
- **Fast-Path Express Lane (`R0/R1`):** Read-only diagnostics and repository queries bypass multi-node DAG planning ceremony for `<2s` startup while preserving full call-journal and cryptographic proof records.
- **Active Graphify AST Blast-Radius Interceptor (`src/harness_core/graphify.py`):** Automatically diffs modified Python AST symbols on `R2` writes, computes 1-hop and 2-hop downstream callers, and dynamically pins affected test files to the task's mandatory Verification Plan.
- **NVIDIA SkillSpector Supply-Chain Gate (`src/harness_core/skillspector.py`):** Scans `SKILL.md` bundles across static agent-native vulnerability rules (Unicode steganography, hidden HTML comment prompt injections, base64 shell obfuscation, symlink escapes), OSV dependency CVEs, and Tier-1 local semantic alignment before registration.
- **Two-Tier Local M4 / Cloud Router & Governed Memory (`src/harness_core/router.py`):** Pre-compresses verbose test/build logs locally (>70% token reduction while preserving exact failing file, line, and assertion) and enforces strict isolation across `project`, `personal`, and `episodic` memory scopes.
- **Speculative Execution (`fork`) & Proof-Carrying Artifacts (`src/harness_core/proof.py`):** Forks tricky steps into parallel isolated CoW branches, runs deterministic verification on each, promotes the passing branch, and emits a signed Proof-Carrying Artifact bundle.
- **Controlled Self-Improvement (`vendor/harness2`):** Wraps Harness² `Propose–Probe–Compose` and experience libraries behind a protected-file allow-list, mandatory SkillSpector pre-scan, and one-step revision rollback.

## Project Layout

```text
├── PRD.md                                  # Product Requirements Document (v1.3 Final)
├── Questions.md                            # M0/M1 architecture & operational questions
├── pyproject.toml                          # Package configuration & CLI entrypoint (`harness`)
├── docs/adr/0001-m0-architecture-decisions.md
├── migrations/0001_initial_schema.sql      # Authoritative 16-table SQL schema
├── skills/                                 # Built-in Git-backed SKILL.md bundles
│   ├── repo_understanding/SKILL.md
│   └── technical_research/SKILL.md
├── src/harness_core/
│   ├── models.py                           # Domain models, R0-R4 risk tiers, state machine rules
│   ├── db.py                               # Transactional LedgerStore, CAS transitions, fencing leases
│   ├── cow.py                              # APFS (`cp -c`) & portable CoW snapshot provider
│   ├── gateway.py                          # Policy engine, secret redaction, 0ms call journal, Code Mode
│   ├── graphify.py                         # Active Graphify AST indexer & blast-radius interceptor
│   ├── skillspector.py                     # Static, OSV, and semantic alignment scanner for skills
│   ├── router.py                           # Two-tier M4 log compressor/router & governed memory service
│   ├── proof.py                            # Verification runner, Proof-Carrying Artifacts & Harness² adapter
│   ├── orchestrator.py                     # Core task orchestrator, Fast-Path lane & speculative branching
│   ├── server.py                           # /v1/* REST + SSE HTTP API server
│   ├── dashboard.py                        # Local-first Web Control Surface UI
│   └── cli.py                              # `harness` command-line interface
├── tests/test_harness_stages.py            # End-to-end acceptance test suite (Stages 1–5)
└── vendor/harness2/                        # Vendored Harness² engine (detached from upstream git)
```

## Quick Start

### 1. Initialize the Ledger, CoW Store, and Built-In Skills

```bash
export PYTHONPATH="$PWD/src:$PWD/vendor/harness2"
/opt/homebrew/bin/python3 -m harness_core.cli init
```

### 2. Run the Stage 1–5 Acceptance Test Suite

```bash
export PYTHONPATH="$PWD/src:$PWD/vendor/harness2"
/opt/homebrew/bin/python3 -m unittest discover -s tests -p "test_*.py" -v
```

### 3. Submit Tasks or Run the Harness² Refinement Probe from CLI

```bash
# Submit an R0/R1 Fast-Path task
/opt/homebrew/bin/python3 -m harness_core.cli submit "Inspect repository AST and verify core modules" --mode fast_path

# Inspect tasks or a specific task's Proof-Carrying Artifact bundle
/opt/homebrew/bin/python3 -m harness_core.cli status

# Scan and import a SKILL.md directory bundle through the SkillSpector gate
/opt/homebrew/bin/python3 -m harness_core.cli scan-skill skills/repo_understanding

# Run a gated Harness² Propose-Probe-Compose refinement verification
/opt/homebrew/bin/python3 -m harness_core.cli harness2-demo
```

### 4. Launch the Local Web Control Surface & `/v1/*` API

```bash
export PYTHONPATH="$PWD/src:$PWD/vendor/harness2"
/opt/homebrew/bin/python3 -m harness_core.cli up --host 127.0.0.1 --port 8420
```

Open `http://127.0.0.1:8420` in your browser to submit tasks, inspect AST Blast-Radius warnings and Proof-Carrying Artifacts, review SkillSpector scan badges, and approve `R3` actions.

## Attaching Your Own Git Remote

This repository is initialized with a clean root Git history and no upstream remote attached:

```bash
git remote add origin <your-git-repo-url>
git branch -M main
git push -u origin main
```

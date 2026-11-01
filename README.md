# HEX: Autonomous AI Workbench

```
  ██╗  ██╗███████╗██╗  ██╗
  ██║  ██║██╔════╝╚██╗██╔╝
  ███████║█████╗   ╚███╔╝ 
  ██╔══██║██╔══╝   ██╔██╗ 
  ██║  ██║███████╗██╔╝ ██╗
  ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝
  HEX: Autonomous AI Workbench
```

**HEX** is a personal, extensible autonomous AI workbench for project-scoped coding and technical research. It plans and executes bounded work inside Copy-on-Write (CoW) workspaces, enforces default-deny tool policies (`R0–R4`), journals every tool call for `0 ms` crash replay, intercepts AST blast-radius impacts via Graphify, scans skills for supply-chain threats via NVIDIA SkillSpector, and integrates Google Research's Harness² (`Propose–Probe–Compose`) behind a controlled self-improvement gate.

## Quick Start

### 1. Run the Interactive Onboarding Wizard

```bash
hex onboard
```
*Auto-detects Apple Silicon M4 / NVIDIA GPU / RAM, configures snapshot backends (APFS / OverlayFS / Btrfs), sandboxes (Bubblewrap / Docker), and model routing profiles.*

### 2. Start the Local Web Control Surface & API

```bash
hex up
```
*Access the control surface at `http://127.0.0.1:8420`.*

### 3. Run Autonomous Coding Tasks

```bash
# Diagnostic fast-path query (<2s startup)
hex submit "Inspect auth module callers" --mode fast_path

# Standard multi-turn autonomous repair
hex submit "Fix token expiration race condition in auth.py"
```

### 4. Distributed Worker Daemon

```bash
hex worker --machine-class auto
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

## Open questions to resolve during M0/M1

- Should Stage 1 use a lightweight Postgres-native durable execution library (like DBOS) for call journaling and workflow recovery, or implement the tool_calls memoization table and lease reaper directly in SQLAlchemy/asyncpg?
- Which specific quantized local model on the MacBook Air M4 (e.g., Qwen 3.5 9B MLX vs. Gemma 4 / Llama 3.1 8B) offers the best latency-to-recall ratio for both compressing raw pytest/compiler logs and running SkillSpector's semantic alignment pass?
- What numeric risk-score threshold (0–100) in SkillSpector should trigger automatic quarantine vs. Medium human-in-the-loop approval for locally authored skills versus imported third-party skills?
- Which data classifications may be sent to which approved cloud providers when escalating from the local M4 tier to Tier-2 heavy reasoning?
- What per-task, speculative-branching, monthly, and Harness² recursion-budget ceilings should apply?
- Which Graphify release/package and interface are intended, and how fast can incremental tree-sitter re-indexing run on dirty files inside an active CoW snapshot?
- Should personal memory be enabled at all in the first release, and what explicit capture controls are acceptable?
- Which container/OS sandbox (Docker, Podman, Apple sandbox-exec / Virtualization.framework) is cleanest to layer directly on top of APFS clones on the MacBook Air M4 and OverlayFS on the Desktop?
- Where should shared CoW delta archives and Proof-Carrying Artifact bundles live when machines are remote: a self-hosted MinIO/S3 instance on the Desktop/Server over Tailscale, or rsync-over-SSH CAS directories?
- What labeled task set from your own repositories will establish the Stage 1 quality baseline and become the held-out promotion suite?

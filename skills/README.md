# `skills` (`harness_core.resources.skills`)

**Built-in `SKILL.md` capability bundles packaged with HEX Agent.**

`skills/` maps to `harness_core.resources.skills` in [`pyproject.toml`](../pyproject.toml), with `builtin/**/*` and `builtin/**/.*` included as wheel package data.

## Structure (`85` `SKILL.md` Bundles)

- **`repo_understanding/` & `technical_research/`**: Core R1 workflow skills (`allowed_tools: [list_files, read_file, query_graphify]`).
- **`custom/`**: Directory reserved for user-authored or project-local custom skills.
- **`builtin/` (83 bundles across 15 categories)**:
  - `apple/` (4): `apple-notes`, `apple-reminders`, `findmy`, `imessage`
  - `autonomous-ai-agents/` (4): `claude-code`, `codex`, `computer-use`, `opencode`
  - `creative/` (16): `architecture-diagram`, `ascii-art`, `ascii-video`, `baoyu-infographic`, `claude-design`, `comfyui` (scripts/tests/workflows), `design-md`, `excalidraw` (scripts/references), `humanizer`, `manim-video`, `p5js`, `popular-web-designs` (54 templates), `pretext`, `sketch`, `songwriting-and-ai-music`, `touchdesigner-mcp`
  - `email/` (2): `email-inbox-triage`, `himalaya`
  - `github/` (7): `codebase-inspection`, `github-auth` (`scripts/`), `github-code-review`, `github-issue-to-pr`, `github-issues`, `github-pr-workflow`, `github-repo-management`
  - `media/` (3): `gif-search`, `songsee`, `youtube-content` (`scripts/fetch_transcript.py`)
  - `mlops/` (5): `evaluation/{evaluating-llms-harness,weights-and-biases}`, `huggingface-hub`, `inference/{llama-cpp,serving-llms-vllm}`
  - `note-taking/` (1): `obsidian`
  - `productivity/` (15): `airtable`, `document-to-action-items`, `docx`, `google-workspace`, `maps`, `meeting-action-items`, `nano-pdf`, `notion`, `ocr-and-documents`, `pdf`, `powerpoint`, `product-price-monitor`, `teams-meeting-pipeline`, `weekly-review-planning`, `xlsx`
  - `research/` (6): `arxiv`, `blogwatcher`, `competitor-news-monitor`, `grounded-citations`, `llm-wiki`, `research-paper-writing` (LaTeX templates for AAAI, ACL, COLM, ICLR, ICML, NeurIPS)
  - `smart-home/` (1): `openhue`
  - `social-media/` (1): `xurl`
  - `software-development/` (16): `dogfood`, `hex-agent-skill-authoring`, `node-inspect-debugger`, `plan`, `ponytail*`, `python-debugpy`, `requesting-code-review`, `simplify-code`, `spike`, `systematic-debugging`, `test-driven-development`
  - `repo_understanding/` & `technical_research/` (copies bundled under `builtin/`)

## Format & Integration

- Skills use YAML frontmatter (`name`, `description`, optional `risk_class`, `allowed_tools`, `verify:`, `metadata.hex`) paired with procedural Markdown instructions (`When to Use`, `Procedure`, `Pitfalls`, `Verification`).
- Discovered via `harness_core.resources.bundled_skills_dir(optional=False)`, indexed into Slot #5 by `cognition/prompt_builder.py`, scanned by `cognition/skillspector.py` during `hex init` / `seed_default_skills`, and queried by `skill_engine/retrieval.py`.

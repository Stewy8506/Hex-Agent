# Phase 30: Multi-Tier Soul Engine & Dynamic Mode Modulators

## Overview
Establish first-class identity and persona modulation in HEX without prompt bloat. Separate machine-wide developer DNA (`~/.hex/USER.md` + `~/.hex/SOUL.md`) from repository architecture (`.hex/`), and provide punchy 3–4 line mode modulators for `plan` (architect), `agent` (builder), and `code` (surgical coder).

---

## Tasks

### Task 30-01: SoulManager & Developer DNA Resolution (`harness_core/soul.py`)
- **Description:** Implement `SoulManager` managing the lifecycle, seeding, legacy upgrades, and multi-tier resolution of HEX persona and Developer DNA.
- **Components:**
  1. Multi-tier resolution hierarchy: `~/.hex/SOUL.md` (global developer identity) vs `.hex/SOUL.md` (workspace project rules).
  2. Compact budget discipline: enforce ~300–500 token budget for baseline soul.
  3. Default template auto-seeding: atomically seed default template if missing; compute template hashes against known legacy versions to allow automated safe upgrades without overwriting custom edits.
  4. Dynamic identity renaming: `/rename <name>` support that persists and renames the active agent identity (e.g., "Jarvis", "Athena").
  5. Machine-wide Developer DNA (`~/.hex/USER.md`): load personal engineering conventions into user profile context.
- **Verification:**
  ```bash
  pytest tests/cognition/test_soul_manager.py -v
  ```
- **Done Criteria:**
  - `SoulManager` seamlessly resolves global and workspace souls.
  - Default template is seeded atomically on fresh environments.
  - Legacy templates are detected and upgraded without destroying user customizations.
  - Renaming updates persona name and returns expected string.

---

### Task 30-02: Punchy Mode Modulators, Security Gate & Prompt Pipeline Slotting (`harness_core/prompt_builder.py`)
- **Description:** Implement `PromptBuilder` to assemble system prompts according to the strict 5-slot architecture with 3–4 line punchy mode modulators, prompt-injection heuristic scanning, and wire it into `HarnessOrchestrator.run_agent_loop` and `ReActLoop`.
- **Components:**
  1. 5-slot prompt architecture:
     - Slot #1: Soul identity (from `SoulManager`).
     - Slot #2: Punchy mode modulator:
       - `plan`: 3–4 lines (inquisitive architect, edge-case probing, no code before specs locked).
       - `agent`: 3–4 lines (decisive builder, empirical tests, minimal chatter).
       - `code`: 3–4 lines (surgical diff specialist, strict YAGNI, zero unrequested boilerplate).
     - Slot #3: Environment & tools context.
     - Slot #4: Curated memory snapshot (fenced).
     - Slot #5: Project context / rules.
  2. Prompt injection scanner:
     - Warn on user-authored souls (`~/.hex/SOUL.md`).
     - Hard block on third-party imported profile souls.
  3. Integration into `HarnessOrchestrator.run_agent_loop`, `ReActConfig`, and TUI `/rename` / `/mode`.
- **Verification:**
  ```bash
  pytest tests/cognition/test_prompt_builder.py -v
  ```
- **Done Criteria:**
  - `PromptBuilder.build_system_prompt()` outputs strictly ordered slots with prefix stability.
  - Mode switches dynamically update Slot #2 with punchy 3–4 line constraints.
  - Injection scanner warns on user hits and blocks external hits.
  - Full end-to-end integration test passes in `ReActLoop`.

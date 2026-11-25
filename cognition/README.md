# `cognition` (`harness_core.cognition`)

**Agent identity (`SOUL.md`), KV-cache-friendly 5-slot prompt compiler, multi-tier curated/AST-JIT/associative memory & lifecycle, 4-phase context compaction, reflection learning, loop/thinking-tag guards, SkillSpector supply-chain verification, skill synthesis, and session title synthesis.**

Mapped to `harness_core.cognition` in [`pyproject.toml`](../pyproject.toml), this subsystem governs everything the agent knows, remembers, checks for safety, and injects into prompts across turns and sessions.

## Modules

| File | Purpose |
| :--- | :--- |
| `__init__.py` | PEP 562 lazy loader exporting **59** symbols across all 9 submodules. |
| `prompt_builder.py` | `PromptBuilder` 5-slot system prompt compiler plus `scan_for_threats()`, `scan_context_content()`, `build_capabilities_guidance()`, `build_tools_summary()`, `build_skills_index()`, `MODE_MODULATORS`, `PARALLEL_TOOL_CALL_GUIDANCE`, and `PLATFORM_HINTS`. |
| `soul.py` | `SoulManager`, `SoulProvenance`, `normalize_soul()`, `get_hex_home()`, `DEFAULT_SOUL_NAME` (`"HEX"`), `DEFAULT_SOUL_MD`, `DEFAULT_USER_MD`, `MAX_SOUL_CHARS` (`2500`), and `TRUNCATION_MARKER`. Resolves `<ws>/.hex/SOUL.md` over `~/.hex/SOUL.md`, manages `~/.hex/USER.md`, auto-upgrades legacy templates (`maybe_upgrade_soul()`), and implements `rename_identity()` (`/rename`). |
| `memory.py` | Three-plane memory engine: (1) Curated Markdown + frozen Turn-0 snapshots (`CuratedMemoryStore`, `MemorySnapshot`, `HistorySnapshotManager`, `GuardrailsManager`, `StreamingContextScrubber`, `sanitize_context()`, `is_transient_error()`; caps `USER_CHAR_LIMIT=1375`, `MEMORY_CHAR_LIMIT=2200`, `GUARDRAIL_CHAR_LIMIT=2000`, `ENTRY_DELIMITER="\n§\n"`); (2) AST-scoped JIT memory (`ASTNodeMemory`, `ASTNodeMemoryStore`, `resolve_symbol_span()`, `compute_code_fingerprint()`, `is_fingerprint_stale()`, `query_jit_memories_for_file()`, `format_jit_memories_block()`); (3) A-MEM associative graph & lifecycle (`MemoryItem`, `extract_wiki_tags()`, `AMEMController` `ADD`/`UPDATE`/`DELETE`/`NOOP`, `MemoryLifecycleManager` hit promotion & 30-day / AST-divergence cold archival). |
| `context_compressor.py` | `ContextCompactor`, `CompactionConfig`, `CompactionResult`, `HEX_COMPRESSION_MARKER_PREFIX`, `CHARS_PER_TOKEN` (`4`), `DEFAULT_KEEP_TOOL_ROUNDS` (`4`), `DEFAULT_MIN_DEMOTE_CHARS` (`1000`), `demote_stale_tail_tools()`, `sanitize_tool_pairs()`, `strip_historical_media()`, and boundary alignment helpers. Executes 4-phase compaction preserving head context and atomic tool-call/result pairs while summarizing middle turns. |
| `reflection.py` | `ReflectionSynthesizer` and `FrictionLesson`: distills task failures/friction into post-mortems appended to `.hex/reflections/LESSONS.md`, records `[AVOID]` invariants in `GuardrailsManager`, and updates `MemoryLifecycleManager`. |
| `repetition_sentry.py` | `RepetitionSentry`, `ActionLoopDetector`, `ActionRecord`, `REPETITION_LOOP_INTERRUPTED`, `MIN_FRAGMENT_LENGTH` (`400`), `is_repetition_dominated()`, `is_runaway_repetition()`, `strip_thinking_tags()`, and `generate_steering_prompt()`. Breaks degenerate repetition loops, strips leaked `<think>`/`<thinking>`/`<reasoning>`/`<thought>`/`<REASONING_SCRATCHPAD>` blocks, and catches identical/oscillating tool-call thrashing via SHA-256 argument fingerprints. |
| `skillspector.py` | `SkillSpectorScanner` (alias `SkillSecurityScanner`), `SkillScanReport`, `CompiledRule`, `STATIC_RULES_REGISTRY`, `RISK_ORDER`, `KNOWN_OSV_VULNS`, `parse_skill_frontmatter()`, `compute_mcp_manifest_sha256()`, `compute_bundle_sha256()`, `get_approved_skill_hash()`. Integrates `vendor/skillspector` analyzers, YARA rules, OSV CVE checks, and semantic alignment, persisting verdicts (`ALLOW` < `25`, `REQUIRE_APPROVAL` `25–59`, `QUARANTINE` >= `60` or escalation) in `LedgerStore` and pinning MCP server manifests. |
| `skill_synthesizer.py` | `SkillSynthesizer` and `SynthesizedSkill`: extracts repeatable task trajectories and proofs from `LedgerStore` into draft `SKILL.md` bundles gated by `SkillSpectorScanner`. |
| `title_synthesizer.py` | `SessionTitleSynthesizer`, `TitleProvenance` (`USER > LLM > DERIVED`), `derive_instant_title()`, `validate_synthesized_title()`, `FORBIDDEN_ECHOES`, `ANSWER_SHAPED_PREFIXES`. |

## Prompt & Memory Design

`PromptBuilder` compiles prompts in **5 deterministic slots** so the Turn-0 system prefix stays KV-cacheable for the entire session:

1. **Slot 1**: Identity (`SOUL.md`)
2. **Slot 2**: Mode modulators
3. **Slot 3**: Environment & tool boundaries
4. **Slot 4**: Frozen Turn-0 `<memory-context>` snapshot (in-session writes do not invalidate KV cache; they take effect on reload/next session)
5. **Slot 5**: Project rules (`AGENTS.md`, `.cursorrules`, `CLAUDE.md`) and compact skills index — followed by volatile per-turn `<extra-context>` fences at the tail.

## Persistence Paths & Planned Sync

- **Global (`~/.hex/`)**: `USER.md`, `SOUL.md`, `skills/custom/` (fallback), `quarantine/` (fallback)
- **Workspace (`<ws>/.hex/`)**: `SOUL.md`, `memories/{MEMORY.md, GUARDRAILS.md, node_memories.db, archive.db, amem_metadata.json, reflections_meta.json, .history/*.bak}`, `reflections/LESSONS.md`, plus repo `skills/custom/` & `.hex/quarantine/`.
- Per [`BACKEND_PLAN.md`](../BACKEND_PLAN.md), P2P/multi-device `cognition-sync` reconciliation hooks via `cloud/sync_engine.py` are planned and not yet active.

## Verification

Tests live in [`tests/cognition/`](../tests/cognition).

```bash
python -m pytest tests/cognition/ -v
```

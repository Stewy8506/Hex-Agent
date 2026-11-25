# `skill_engine` (`harness_core.skills`)

**Closed-loop skill compilation, security-gated execution, verification hooks, and hybrid BM25 + AST retrieval engine.**

`skill_engine/` maps to `harness_core.skills` in [`pyproject.toml`](../pyproject.toml) (distinct from the packaged skill data under `skills/` and `optional-skills/`).

## Modules

| File | Purpose |
| :--- | :--- |
| `__init__.py` | Exports `SkillCompiler`, `SkillCompileResult`, `SkillSecurityViolationError`, `normalize_skill_name`, `normalize_skill_description`, `SkillExecutionEngine`, `SkillVerificationHook`, `SkillVerificationResult`, `assert_skill_regression_free`, `HybridLearningRetriever`, `RetrievalMatch`. |
| `compiler.py` | Compiles `/learn` / `hex learn` inputs into standard `SKILL.md` bundles (`name` <=64 chars hyphenated, 1-sentence <=60-char description ending in `.`, `author: HEX`, `version: 0.1.0`), stages in temp dirs, gates via `SkillSpectorScanner` (quarantining to `.hex/quarantine/<name>/audit.json` if blocked), and installs into `<workspace>/.hex/skills/` or `~/.hex/skills/`. |
| `executor.py` | Verifies bundle SHA-256 against `get_approved_skill_hash()`, executes `verify:` hooks inside a hardened minimal environment (`build_minimal_skill_env` strips secrets and dangerous loader vars like `LD_PRELOAD`, `DYLD_*`, `PYTHONSTARTUP`, `NODE_OPTIONS`), and evaluates `ast_invariant` checks (`assert_skill_regression_free`). |
| `retrieval.py` | `HybridLearningRetriever`: builds an in-memory SQLite FTS5 (`retrieval_fts`) index across workspace/global `SKILL.md` bundles, ledger `skills`, `.hex/reflections/LESSONS.md`, `MEMORY.md`/`GUARDRAILS.md`/`USER.md`, and `.hex/memories/archive.db`, then combines BM25 scores with `ActiveGraphifyAdapter` 1-hop AST caller/callee traversal. |

> **Repository Note**: Subdirectories (`creative/`, `github/`, `media/`, `productivity/`, `research/`) under `skill_engine/` currently hold legacy `__pycache__/*.pyc` files from before skill bundles moved to `skills/builtin/`; active bundled skills live in [`skills/`](../skills/README.md) and [`optional-skills/`](../optional-skills/README.md).

## Integration & Tests

- Used by `agent/orchestrator.py` (`retrieve_task_skills` and post-edit verification), `cognition/memory.py`, `hex_cli/cli.py` (`hex learn`), and `hex_cli/tui.py` (`/learn`).
- Tests live in [`tests/skills/`](../tests/skills) and [`tests/core/test_skill_retrieval_wiring.py`](../tests/core/test_skill_retrieval_wiring.py).

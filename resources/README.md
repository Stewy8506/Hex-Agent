# `resources` (`harness_core.resources`)

**Location-independent path resolver for packaged skill bundles.**

`resources/` maps to `harness_core.resources` in [`pyproject.toml`](../pyproject.toml). It decouples runtime skill discovery from the caller's working directory across source checkouts, editable installs, and built wheels.

## API (`__init__.py`)

- `bundled_skills_dir(optional: bool = False) -> Path`:
  - `optional=False`: resolves `skills/builtin/` via `harness_core.resources.skills`.
  - `optional=True`: resolves `optional-skills/` via `harness_core.resources.skills.optional`.

## Integration

- Consumed by `cognition/prompt_builder.py` (`build_skills_index`), `cognition/skill_synthesizer.py` (`list_skills`), `hex_cli/onboard.py`, `hex_cli/tui.py`, and `hex_cli/acp.py`.
- Covered by [`tests/skills/test_bundled_resources.py`](../tests/skills/test_bundled_resources.py).

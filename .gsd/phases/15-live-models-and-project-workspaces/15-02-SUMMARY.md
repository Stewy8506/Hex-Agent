# Plan 15.02: Project Workspace Registry & Daily Project Switcher — Summary

**Phase:** 15 (Live Model Discovery, OAuth Quotas & Project Workspaces)  
**Plan:** 15-02  
**Status:** Completed  
**Commits:** `c117ac2`, `ed1bfd4`, `25e9231`  
**Tests:** 20/20 in `tests/test_project_registry_and_workspace.py` (455/455 full suite passing green, 0 regressions)

---

## 1. Objectives & Executive Summary

Plan 15-02 delivered persistent project workspace management, automatic workspace binding, interactive daily project switching, folder-scoped context isolation, and global CLI migration from `hex onboard` to `hex setup` (preserving `hex onboard` as an alias).

Key Deliverables:
1. **Persistent Global Project Registry (`ProjectRegistry` in `src/harness_core/project_registry.py`):**
   - Atomic JSON persistence in `~/.hex/projects.json` tracking project name, canonical path, stack, `last_active` ISO timestamp, and `task_count`.
   - Reverse-chronological listing by `last_active`, case-insensitive lookup, and path resolution.
   - Enclosing project auto-detection (`detect_enclosing_project`) climbing parent directories for `.git` or `.hex` markers.
2. **Folder-Scoped Context Isolation:**
   - Automatic generation of project-scoped `.hex/` structures:
     * `.hex/config.yaml`
     * `.hex/reflections/LESSONS.md` with structured header: `# Project Reflections & Work Memory: {name}\n\n## Lessons Learned\n`
     * `.hex/data/` for localized database and CoW workspace layers
     * AST cache `.hex/graph.json`
3. **Daily Project Switcher & Workspace Binding (`src/harness_core/cli.py`):**
   - Silent, immediate workspace binding when launching inside any Git or `.hex` directory.
   - Interactive prompt `"What project do you want to work on today?"` via `select_option` when launched outside any project repository.
   - Options to select registered projects, register an existing folder (`[+]`), or initialize the current directory (`[★]`).
4. **`hex project` CLI Command Suite:**
   - `hex project list [--json]`: formatted table or raw JSON array.
   - `hex project add <path> [--name <name>]`: register directory as a workspace.
   - `hex project use <name|path>` / `hex project switch <name|path>`: switch active project and update `last_active`.
5. **Interactive TUI `/project` Command (`src/harness_core/tui.py`):**
   - `/project`: renders active workspace card (name, path, stack, lessons count, data dir).
   - `/project list`: lists registered workspaces with active indicator `*`.
   - `/project switch <name|path>`: dynamically switches `workspace_root`, rebinds AST graphify, and updates reflections.
6. **Global `hex onboard` -> `hex setup` Canonical Command Migration:**
   - Canonical `setup` parser with `aliases=["onboard"]`.
   - Updated error messages (`ProviderUnavailableError`), status cards, TUI hints, and `README.md`.

---

## 2. Tasks Completed

| Task | Name | Files | Commits |
|------|------|-------|---------|
| 1 | Implement ProjectRegistry and folder-scoped context layout | `src/harness_core/project_registry.py`, `src/harness_core/onboard.py` | `c117ac2` |
| 2 | Implement daily project switcher picker, hex project CLI, TUI /project, and hex setup rename | `src/harness_core/cli.py`, `src/harness_core/tui.py`, `src/harness_core/providers/cloud_provider.py`, `README.md` | `ed1bfd4` |
| 3 | Author unit and integration tests for ProjectRegistry and workspace isolation | `tests/test_project_registry_and_workspace.py`, `tests/test_hex_cli.py` | `25e9231` |

---

## 3. Verification & Empirical Proof

1. **ProjectRegistry Verification:**
   ```bash
   python3 -c "from harness_core.project_registry import ProjectRegistry; reg = ProjectRegistry('/tmp/test_reg.json'); p = reg.register_project('/tmp'); assert p.path == '/tmp'; assert len(reg.list_projects()) == 1"
   # Exit code 0
   ```
2. **`hex project list --json` Verification:**
   ```bash
   python3 -c "import subprocess; out = subprocess.check_output(['hex', 'project', 'list', '--json'], text=True); print(out)"
   # Emits valid JSON array of project records
   ```
3. **Comprehensive Unit & Integration Test Suite (`tests/test_project_registry_and_workspace.py`):**
   ```bash
   .venv/bin/python -m unittest tests/test_project_registry_and_workspace.py
   # Ran 20 tests in 0.464s - OK
   ```
4. **Full Repository Regression Suite:**
   ```bash
   .venv/bin/python -m unittest discover -s tests
   # Ran 455 tests in 41.037s - OK (0 failures, 0 errors, 0 regressions)
   ```
5. **Backwards-Compatible `hex setup` and `hex onboard` Verification:**
   - `hex setup --help` and `hex onboard --help` both display identical canonical help.
   - `test_cli_setup_subcommand` and `test_cli_onboard_alias_subcommand` pass in `tests/test_hex_cli.py`.
   - `ProviderUnavailableError` reports `Run 'hex setup'`.

---

## 4. State & Next Steps

All 3 tasks in Plan 15-02 are complete and verified with atomic commits.
The workbench is ready for final presentation and milestone completion.

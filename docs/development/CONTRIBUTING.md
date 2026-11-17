# Contributing to HEX Agent

Thank you for your interest in contributing to **HEX**! Whether fixing a bug, adding a new language grammar, optimizing context compression, or designing a TUI theme, we welcome your contributions.

---

## 1. Development Setup

### Prerequisites:
- **Python 3.10+** (Python 3.12 recommended)
- **Rust / Cargo 1.80+** (for compiling `crates/hex-tui` and `crates/hex-protocol`)
- **Git**

### Clone & Install:
```bash
git clone https://github.com/Stewy8506/Harness.git
cd Harness

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies in editable mode
pip install -e ".[full,test]"
```

---

## 2. Running Test Suites

HEX uses `pytest` for the Python suite (CI runs the full `tests/` directory). Tests are organized by subsystem: ACP/client coverage lives in `tests/client/`, and packaging/install coverage in `tests/packaging/`. The focused examples below use these current paths.

```bash
# Run the full Python test suite
python -m pytest tests -v

# Run a focused client module
python -m pytest tests/client/test_acp_server.py -v

# Run packaging and installer coverage
python -m pytest tests/packaging/ -v

# Run benchmark evaluation suites (core, resilience, compaction, tui)
python -m harness_core.client.cli eval --suite core
python -m harness_core.client.cli eval --suite all

# Run native Rust tests (protocol + TUI; works on Linux and macOS)
cargo test -p hex-protocol -p hex-tui

# Desktop crate (macOS only; GPUI requires the Metal toolchain)
cargo test -p hex-desktop
```

---

### Source placement

Keep the `harness_core` namespace and the existing `crates/hex-{protocol,tui,desktop}` crates and Rust manifests. Existing public entry modules, including `client/cli.py`, `client/acp.py`, `client/tui.py`, and `client/onboard.py`, remain the CLI/import surfaces; do not replace them with internal paths.

Use the focused internal modules under `src/harness_core/`:

- `client/commands/projects.py` and `client/commands/antigravity.py` own project and Antigravity authentication command handlers.
- `client/terminal/ansi.py` and `client/terminal/review.py` own ANSI formatting and review/diff rendering; `client/onboarding/verification.py` owns onboarding verification helpers.
- `platform/hardware.py` and `platform/diagnostics.py` own host hardware profiling and diagnostic helpers. Configuration stays in `config_loader.py`; use its shared `update_config_yaml_model()` writer for model-selection updates from onboarding and orchestration.
- `storage/project_init.py` owns project stack detection and `.hex` initialization. Filesystem tool implementations live in `tools/filesystem.py` behind the existing gateway.
- `resources/skills/builtin/` and `resources/skills/optional/` contain packaged skill data, not the Python skill engine. Skill-management code remains in its existing modules.

Place tests under the matching `tests/<subsystem>/`; shared fixtures remain in `tests/fixtures/`, and Rust tests stay with their crates. Root manifests, installers, `assets/`, and `.gsd/` are not relocated. This layout does not imply a wholesale split of large orchestration or storage modules. Documentation lives under `docs/{architecture,guides,reference,development,research,audits}/`, with design references and their assets together in `docs/design/mockups/`. See [Source Layout](../architecture/ARCHITECTURE.md#3-python-source-layout-srcharness_core) for the layout and compatibility boundaries.

---

## 3. Pre-Commit Verification

Before submitting a pull request, run the pre-commit integrity script:

```bash
# Verify cryptographic event chain integrity and Alembic migration parity
python scripts/verify_event_chain.py
```

This ensures:
1. All event hash chains in the database ledger are mathematically continuous and uncompromised.
2. Local database schema revisions are strictly aligned with migration heads.

---

## 4. Code Style & Commit Conventions

- **Atomic Commits**: One logical change per commit.
- **Commit Format**: Conventional Commits standard:
  - `feat(scope): add new feature`
  - `fix(scope): resolve bug`
  - `docs(scope): update documentation`
  - `test(scope): add test coverage`
- **Verification Proof**: All code changes require empirical proof (test execution output or command logs).

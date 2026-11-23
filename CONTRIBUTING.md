# Contributing to HEX Agent

Thank you for your interest in contributing to **HEX**! Whether fixing a bug, adding a new language grammar, optimizing context compression, or designing a TUI theme, we welcome your contributions.

---

## 1. Development Setup

### Prerequisites:
- **Python 3.10+** (Python 3.12 recommended)
- **Rust / Cargo 1.80+** (for compiling `crates/hex-tui` and `crates/hex-protocol`)
- **macOS and the Xcode/Metal toolchain** for the experimental desktop in `App/hex-desktop/`
- **Git**

### Clone & Install:
```bash
git clone https://github.com/Stewy8506/Harness.git
cd Harness

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Required developer install from the repository root
python -m pip install -e ".[full]"
```

Re-run this editable install after the source relocation, including in an existing virtual environment, to refresh setuptools' `package_dir` mappings. The `full` extra includes the Python test dependencies. Do not rely on adding the checkout root or the former source directory to `PYTHONPATH`: the physical root domains still import through the canonical `harness_core.*` namespace.

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

Use the open repository-root layout while keeping canonical `harness_core.*` imports and all existing CLI contracts. Supported setuptools `package_dir` mappings connect `harness_core.core` to `agent/`, `harness_core.client` to `hex_cli/`, `harness_core.platform` to `hex_platform/`, and `harness_core.skills` to `skill_engine/`. The `cognition/`, `providers/`, `models/`, `runtime/`, `storage/`, `capabilities/`, `tools/`, `lsp/`, `resources/`, and `gateways/` domains also live at the repository root, without renaming their public packages.

- `harness_core/` is the substantive package anchor, not a wrapper layer. It retains `__init__.py`, shared configuration/provider/gateway/evaluation/graphify/adapter/MCP/vendor-path modules, and packaged SQL migrations.
- `hex_cli/commands/projects.py` and `hex_cli/commands/antigravity.py` own project and Antigravity authentication command handlers.
- `hex_cli/terminal/ansi.py` and `hex_cli/terminal/review.py` own ANSI formatting and review/diff rendering; `hex_cli/onboarding/verification.py` owns onboarding verification helpers. Entry modules live in `hex_cli/cli.py`, `hex_cli/acp.py`, `hex_cli/tui.py`, and `hex_cli/onboard.py`; keep their `harness_core.client.*` launch/import names, rather than importing by physical directory name.
- `hex_platform/hardware.py` and `hex_platform/diagnostics.py` own host hardware profiling and diagnostic helpers. Configuration stays in `harness_core/config_loader.py`; use its shared `update_config_yaml_model()` writer for model-selection updates from onboarding and orchestration.
- `storage/project_init.py` owns project stack detection and `.hex` initialization. Filesystem tool implementations live in `tools/filesystem.py` behind the existing gateway.
- `skills/builtin/` and `optional-skills/` contain packaged skill data, not the Python engine in `skill_engine/`. Their canonical resource package names are preserved by the package mappings.
- The root `Cargo.toml` remains the workspace manifest. `crates/hex-protocol/` and `crates/hex-tui/` stay in place; the experimental macOS desktop lives in `App/hex-desktop/`. Run Cargo commands from the root using the unchanged package names. There is no new mobile app or placeholder mobile directory.

Place tests under the matching `tests/<subsystem>/`; shared fixtures remain in `tests/fixtures/`, and Rust tests stay with their crates. Root manifests, installers, and `assets/` are not relocated. This layout does not imply a wholesale split of large orchestration or storage modules. Documentation lives under `docs/{architecture,guides,reference,development,research}/`, with design references and their assets together in `docs/design/mockups/`. See [Source Layout](../architecture/ARCHITECTURE.md#3-python-source-layout-srcharness_core) for the layout and compatibility boundaries.

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

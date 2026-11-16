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

HEX uses `pytest` for the Python suite (CI runs the full `tests/` directory):

```bash
# Run the full Python test suite
python -m pytest tests -v

# Run a focused module
python -m pytest tests/test_acp_server.py -v

# Run benchmark evaluation suites (core, resilience, compaction, tui)
python -m harness_core.client.cli eval --suite core
python -m harness_core.client.cli eval --suite all

# Run native Rust tests (protocol + TUI; works on Linux and macOS)
cargo test -p hex-protocol -p hex-tui

# Desktop crate (macOS only; GPUI requires the Metal toolchain)
cargo test -p hex-desktop
```

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

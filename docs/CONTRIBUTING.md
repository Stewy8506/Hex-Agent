# Contributing to HEX AI Workbench

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

HEX includes a unified evaluation and test runner (`hex eval`):

```bash
# Run core unit and integration tests
python -m harness_core.cli eval --suite core

# Run all test suites (core, resilience, compaction, tui)
python -m harness_core.cli eval --suite all

# Run native Rust workspace tests
cargo test --workspace
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

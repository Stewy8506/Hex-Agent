## Summary

<!-- Briefly describe what this PR changes and why. -->

Fixes #(issue)

## Type of Change

- [ ] Bug fix (`fix`)
- [ ] New feature (`feat`)
- [ ] Refactor / internal cleanup (`refactor`)
- [ ] Documentation (`docs`)
- [ ] Tests (`test`)

## Affected Components

- [ ] Python core (`harness_core`, `agent`, `storage`, `cognition`, `runtime`, `hex_cli`, `cloud`)
- [ ] Rust protocol / TUI (`crates/hex-protocol`, `crates/hex-tui`)
- [ ] macOS GPUI desktop (`Hex-App` sibling repository)
- [ ] Skills / SkillSpector (`skills`, `skill_engine`, `optional-skills`)

## Verification

Paste the commands you ran and their results:

- [ ] `python -m pytest tests -v`
- [ ] `python scripts/verify_event_chain.py` (when touching ledger/storage/migrations)
- [ ] `cargo test -p hex-protocol -p hex-tui` (when touching Rust crates)
- [ ] `(cd ../Hex-App && cargo test -p hex-desktop)` (macOS only, when touching desktop UI)

## Checklist

- [ ] Commits follow Conventional Commits (`feat(scope): ...`, `fix(scope): ...`)
- [ ] Public Python imports preserve the canonical `harness_core.*` namespace
- [ ] No API keys, tokens, or plaintext secrets are included in code or test fixtures
- [ ] Documentation updated if user-facing behavior or CLI flags changed

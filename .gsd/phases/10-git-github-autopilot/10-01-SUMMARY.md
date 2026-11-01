# Phase 10: Plan 10-01 Summary — Git Autopilot Engine, PR Generator & CLI Integration

**Plan File:** `.gsd/phases/10-git-github-autopilot/10-01-PLAN.md`  
**Execution Date:** 2026-11-01  
**Status:** Completed  
**Tests:** 284/284 Green (17 new test cases in `tests/test_git_autopilot.py`, 0 regressions)

---

## 1. Executive Summary

Plan 10-01 delivers the foundational Git automation and Pull Request engine of **Pillar 4 (Git & GitHub Autopilot Integration)**. The new `GitAutopilot` engine enables HEX to autonomously isolate task workflows on dedicated branches (`hex/task-<id>-<slug>`), produce atomic conventional commits with standard `Task-ID` and `Automated-By` trailers, construct rich Markdown Pull Request bodies featuring Active Graphify AST impact matrices and Proof-Carrying Artifact verification tables, and dispatch PRs through pluggable transports (`gh` CLI, GitHub REST API, or in-memory mocks). The CLI commands `hex run` and `hex exec` are now wired with `--pr`, `--branch`, `--base-branch`, and `--dry-run` options.

---

## 2. Key Components Delivered

### 1. `GitAutopilotConfig` & GitHub Transports (`src/harness_core/git_autopilot.py`)
- **Configuration Dataclass:** `GitAutopilotConfig` with `author_name`, `author_email`, `branch_prefix`, `default_base_branch`, `github_token`, `repo_slug`, and `dry_run`.
- **Environment Parser:** `GitAutopilotConfig.from_env()` supporting `HEX_GITHUB_TOKEN` / `GITHUB_TOKEN`, `HEX_GIT_AUTHOR_NAME`, `HEX_GIT_AUTHOR_EMAIL`, `HEX_GIT_BRANCH_PREFIX`, `HEX_GITHUB_REPOSITORY` / `GITHUB_REPOSITORY`, and `HEX_GIT_DRY_RUN`.
- **Pluggable GitHub Transport Interface:**
  - `GitHubTransport` runtime checkable protocol (`create_pull_request`, `post_issue_comment`, `get_pull_request`).
  - `MockGitHubTransport`: In-memory recording mock returning structured PR metadata (`number=101`, `html_url`, `created_prs`, `comments`).
  - `SubprocessGitHubTransport`: Priority 1 execution via `gh` CLI (`gh pr create`, `gh issue comment`, `gh pr view`); Priority 2 fallback to GitHub REST API (`https://api.github.com/repos/{owner}/{repo}/pulls`) via `urllib.request`.

### 2. `GitAutopilot` Engine & PR Formatter (`src/harness_core/git_autopilot.py`)
- **Sanitized Branch Creation:** `create_task_branch(task_id, goal)` generates formatted branches (`hex/task-<clean_id>-<slug>`) with smart token boundary slugification.
- **Atomic Conventional Commits:** `commit_task_changes(task_id, scope, description, files, commit_type)` enforces conventional commit format `<type>(<scope>): <description>` with `Task-ID: <id>` and `Automated-By: HEX Autonomous Workbench`. In `dry_run` mode, returns deterministic mock SHAs (`mock-sha-<id>`).
- **Rich PR Markdown Generator:** `generate_pr_summary(task_id, goal, proof_bundle, ast_impact)` formats:
  - Objective blockquote.
  - Active Graphify AST Impact Matrix table (`Symbol / File`, `Impact Kind`, `Affected Callers / Tests`, `Risk Class`).
  - Proof-Carrying Artifact verification table (`Verification Status`, `Diff SHA-256`, `Merkle Tree SHA-256`, `Exit Code`, `Pinned Tests`, `Runtime Digest`, `Ledger Signature`).
- **Pull Request Dispatch:** `create_pull_request(branch, title, body, base)` delegates to the configured transport.

### 3. CLI Integration: `hex run` and `hex exec` (`src/harness_core/cli.py`)
- Added `run` subcommand with alias `exec` supporting `--pr`, `--branch`, `--base-branch`, and `--dry-run`.
- Seamlessly submits and executes tasks through `HarnessOrchestrator.submit_and_run`.
- On task success: creates/checks out task branch, creates conventional commit, generates rich PR summary, and creates Pull Request.
- On task failure: aborts git modifications and returns exit code 1.

### 4. Comprehensive Test Suite (`tests/test_git_autopilot.py`)
- 17 unit and integration tests covering configuration, slugification edge cases, conventional commits, markdown PR formatting, real local git repositories (`git init`, branches, commits, log inspection), mock and subprocess transports, and `hex run` / `hex exec` CLI dry-run and failure workflows.

---

## 3. Verification & Empirical Proof

1. **Task 1 Verification:**
   ```bash
   python3 -c "from harness_core.git_autopilot import GitAutopilot, GitAutopilotConfig, MockGitHubTransport; ... assert 'Active Graphify AST Impact' in summary; assert 'Proof-Carrying Artifact' in summary; print('GIT_AUTOPILOT_CORE_OK')"
   # Result: GIT_AUTOPILOT_CORE_OK
   ```
2. **Task 2 Verification:**
   ```bash
   python3 -c "from harness_core.cli import main; import sys; code = main(['run', 'Diagnostic git run test', '--mode', 'fast_path', '--dry-run', '--pr']); assert code == 0; print('CLI_RUN_PR_OK')"
   # Result: Task succeeded, branch created, mock commit generated, pull request #101 created, CLI_RUN_PR_OK
   ```
3. **Plan Test Suite:**
   ```bash
   .venv/bin/python -m unittest tests/test_git_autopilot.py
   # Result: Ran 17 tests in 8.667s. OK.
   ```
4. **Full Repository Regression Suite:**
   ```bash
   .venv/bin/python -m unittest discover tests
   # Result: Ran 284 tests in 31.203s. OK. (0 failures, 0 errors).
   ```

---

## 4. Atomic Commits Landed

1. `e55375d` — `feat(phase-10): implement git autopilot engine and pull request formatter`
2. `d1b34d7` — `feat(phase-10): wire hex run and hex exec cli commands with git pr automation`
3. `3fdf131` — `feat(phase-10): author unit and integration tests for git autopilot engine and cli`

"""GitHub Action Mode & Webhook Bot Responder (PRD Section 8, Pillar 4).

Provides autonomous issue & PR bot automation: parses GitHub Action event payloads,
extracts bot commands (@hex-bot fix ..., /hex fix ..., @hex ...), executes tasks in
isolated CoW workspaces via HarnessOrchestrator, commits changes via GitAutopilot,
and posts rich proof-carrying response comments back to GitHub issues and pull requests.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .gateway import redact_secrets
from .git_autopilot import (
    GitAutopilot,
    GitAutopilotConfig,
    GitHubTransport,
    MockGitHubTransport,
    SubprocessGitHubTransport,
)

if TYPE_CHECKING:
    from .orchestrator import HarnessOrchestrator

logger = logging.getLogger(__name__)

KNOWN_COMMANDS: set[str] = {"fix", "run", "explain", "review"}


@dataclass
class GitHubActionConfig:
    """Configuration options parsed from GitHub Actions execution environment."""

    event_name: str = ""
    event_path: str | Path = ""
    token: str | None = None
    repository: str = ""  # e.g. "owner/repo"
    ref: str | None = None  # e.g. "refs/heads/main" or "refs/pull/42/merge"
    sha: str | None = None
    bot_names: set[str] = field(default_factory=lambda: {"@hex-bot", "/hex", "@hex"})
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> GitHubActionConfig:
        """Parse configuration from standard GitHub Actions and HEX environment variables."""
        event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()
        event_path = os.environ.get("GITHUB_EVENT_PATH", "").strip()
        token = os.environ.get("HEX_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        repository = (
            os.environ.get("HEX_GITHUB_REPOSITORY")
            or os.environ.get("GITHUB_REPOSITORY", "")
        ).strip()
        ref = os.environ.get("HEX_GITHUB_REF") or os.environ.get("GITHUB_REF")
        sha = os.environ.get("HEX_GITHUB_SHA") or os.environ.get("GITHUB_SHA")

        bot_names_env = os.environ.get("HEX_BOT_NAMES", "").strip()
        if bot_names_env:
            bot_names = {b.strip() for b in bot_names_env.split(",") if b.strip()}
        else:
            bot_names = {"@hex-bot", "/hex", "@hex"}

        dry_run_env = (
            os.environ.get("HEX_DRY_RUN")
            or os.environ.get("HEX_GIT_DRY_RUN")
            or ""
        ).strip().lower()
        dry_run = dry_run_env in ("1", "true", "yes", "on")

        return cls(
            event_name=event_name,
            event_path=event_path,
            token=token,
            repository=repository,
            ref=ref,
            sha=sha,
            bot_names=bot_names,
            dry_run=dry_run,
        )


@dataclass
class TriggerMatch:
    """Represents a matched bot trigger extracted from a GitHub event payload."""

    matched: bool
    trigger_type: str  # "mention", "slash_command", "label", "dispatch"
    command: str  # "fix", "run", "explain", "review", or "custom"
    instruction: str  # Extracted goal / instruction text
    author: str  # GitHub username of author / sender
    issue_number: int | None = None
    pr_number: int | None = None
    branch: str | None = None
    head_sha: str | None = None


class GitHubActionRunner:
    """Autonomous executor for GitHub Actions event payloads and bot comments."""

    def __init__(
        self,
        config: GitHubActionConfig | None = None,
        transport: GitHubTransport | None = None,
        autopilot: GitAutopilot | None = None,
    ) -> None:
        self.config = config or GitHubActionConfig.from_env()
        if transport is not None:
            self.transport = transport
        elif self.config.dry_run:
            self.transport = MockGitHubTransport()
        else:
            self.transport = SubprocessGitHubTransport(
                token=self.config.token,
            )
        self.autopilot = autopilot

    def parse_event_payload(self, raw_payload: str | bytes | dict[str, Any]) -> dict[str, Any]:
        """Safely parse event payload from JSON string, bytes, or dictionary."""
        if isinstance(raw_payload, dict):
            return raw_payload
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode("utf-8", errors="replace")
        if isinstance(raw_payload, str):
            try:
                parsed = json.loads(raw_payload)
                return parsed if isinstance(parsed, dict) else {}
            except Exception as e:
                logger.warning("Failed to parse GitHub event JSON payload: %s", e)
                return {}
        return {}

    def _extract_bot_mention(self, text: str) -> tuple[str, str, str] | None:
        """Scan text for configured bot names and extract (trigger_type, command, instruction).

        Returns None if no bot trigger is found.
        """
        if not text:
            return None

        # Sort bot names longest first to avoid partial prefix overlap (@hex-bot before @hex)
        sorted_names = sorted(self.config.bot_names, key=len, reverse=True)

        for name in sorted_names:
            escaped = re.escape(name)
            pattern = re.compile(
                rf"(?:^|\s){escaped}(?:[:;,])?(?:\s+(.*)|\s*$)",
                re.IGNORECASE | re.DOTALL,
            )
            m = pattern.search(text)
            if m:
                remainder = (m.group(1) or "").strip()
                trigger_type = "slash_command" if name.startswith("/") else "mention"

                if not remainder:
                    return trigger_type, "run", f"Execute task requested via {name}"

                tokens = remainder.split(None, 1)
                cmd_cand = tokens[0].lower()
                if cmd_cand in KNOWN_COMMANDS:
                    cmd = cmd_cand
                    inst = tokens[1].strip() if len(tokens) > 1 else cmd_cand
                else:
                    cmd = "custom"
                    inst = remainder

                return trigger_type, cmd, inst

        return None

    def detect_trigger(
        self,
        event_name: str,
        payload: dict[str, Any],
    ) -> TriggerMatch | None:
        """Detect whether the GitHub event payload triggers bot execution."""
        normalized_event = (event_name or self.config.event_name or "").strip().lower()

        # 1. Case: issue_comment
        if normalized_event == "issue_comment":
            comment = payload.get("comment") or {}
            issue = payload.get("issue") or {}
            body = comment.get("body") or ""
            author = (
                comment.get("user", {}).get("login")
                or payload.get("sender", {}).get("login")
                or "unknown"
            )

            matched = self._extract_bot_mention(body)
            if not matched:
                return None

            trigger_type, cmd, inst = matched
            issue_number = issue.get("number")
            is_pr = "pull_request" in issue
            pr_number = issue_number if is_pr else None

            return TriggerMatch(
                matched=True,
                trigger_type=trigger_type,
                command=cmd,
                instruction=inst,
                author=author,
                issue_number=issue_number,
                pr_number=pr_number,
            )

        # 2. Case: pull_request
        if normalized_event == "pull_request":
            pr = payload.get("pull_request") or {}
            action = payload.get("action", "").lower()
            pr_number = pr.get("number") or payload.get("number")
            author = (
                pr.get("user", {}).get("login")
                or payload.get("sender", {}).get("login")
                or "unknown"
            )
            head = pr.get("head") or {}
            branch = head.get("ref")
            head_sha = head.get("sha")

            if action == "labeled":
                label = payload.get("label") or {}
                label_name = label.get("name", "").strip()
                if any(bn.strip("@/").lower() in label_name.lower() for bn in self.config.bot_names) or label_name.lower().startswith("hex"):
                    cmd = "run"
                    if ":" in label_name:
                        suffix = label_name.split(":", 1)[1].strip().lower()
                        if suffix in KNOWN_COMMANDS:
                            cmd = suffix
                    inst = pr.get("title") or f"Review and verify PR #{pr_number}"
                    return TriggerMatch(
                        matched=True,
                        trigger_type="label",
                        command=cmd,
                        instruction=inst,
                        author=author,
                        issue_number=pr_number,
                        pr_number=pr_number,
                        branch=branch,
                        head_sha=head_sha,
                    )

            if action in ("opened", "synchronize", "reopened", "ready_for_review", ""):
                body = f"{pr.get('title', '')}\n\n{pr.get('body', '')}"
                matched = self._extract_bot_mention(body)
                if matched:
                    trigger_type, cmd, inst = matched
                    return TriggerMatch(
                        matched=True,
                        trigger_type=trigger_type,
                        command=cmd,
                        instruction=inst,
                        author=author,
                        issue_number=pr_number,
                        pr_number=pr_number,
                        branch=branch,
                        head_sha=head_sha,
                    )

            return None

        # 3. Case: issues
        if normalized_event == "issues":
            issue = payload.get("issue") or {}
            action = payload.get("action", "").lower()
            issue_number = issue.get("number") or payload.get("number")
            author = (
                issue.get("user", {}).get("login")
                or payload.get("sender", {}).get("login")
                or "unknown"
            )

            if action == "labeled":
                label = payload.get("label") or {}
                label_name = label.get("name", "").strip()
                if any(bn.strip("@/").lower() in label_name.lower() for bn in self.config.bot_names) or label_name.lower().startswith("hex"):
                    cmd = "run"
                    if ":" in label_name:
                        suffix = label_name.split(":", 1)[1].strip().lower()
                        if suffix in KNOWN_COMMANDS:
                            cmd = suffix
                    inst = issue.get("title") or f"Resolve issue #{issue_number}"
                    return TriggerMatch(
                        matched=True,
                        trigger_type="label",
                        command=cmd,
                        instruction=inst,
                        author=author,
                        issue_number=issue_number,
                        pr_number=None,
                    )

            if action in ("opened", "reopened", ""):
                body = f"{issue.get('title', '')}\n\n{issue.get('body', '')}"
                matched = self._extract_bot_mention(body)
                if matched:
                    trigger_type, cmd, inst = matched
                    return TriggerMatch(
                        matched=True,
                        trigger_type=trigger_type,
                        command=cmd,
                        instruction=inst,
                        author=author,
                        issue_number=issue_number,
                        pr_number=None,
                    )

            return None

        # 4. Case: workflow_dispatch
        if normalized_event == "workflow_dispatch":
            inputs = payload.get("inputs") or {}
            instruction = (
                inputs.get("goal")
                or inputs.get("task")
                or inputs.get("instruction")
                or ""
            ).strip()
            if not instruction:
                return None

            author = payload.get("sender", {}).get("login") or "workflow_dispatch"
            cmd = inputs.get("command", "run").strip().lower()
            if cmd not in KNOWN_COMMANDS:
                cmd = "custom"

            return TriggerMatch(
                matched=True,
                trigger_type="dispatch",
                command=cmd,
                instruction=instruction,
                author=author,
                issue_number=None,
                pr_number=None,
                branch=payload.get("ref") or self.config.ref,
                head_sha=payload.get("sha") or self.config.sha,
            )

        return None

    def format_ack_comment(self, trigger: TriggerMatch) -> str:
        """Format markdown acknowledgement comment."""
        body = (
            f"🤖 **HEX Autopilot Activated**\n\n"
            f"Task acknowledged from @{trigger.author}. Running verification inside isolated CoW workspace...\n\n"
            f"- **Command:** `{trigger.command}`\n"
            f"- **Instruction:** {trigger.instruction}\n"
        )
        return redact_secrets(body)

    def format_completion_comment(
        self,
        trigger: TriggerMatch,
        task: dict[str, Any],
        proof_bundle: dict[str, Any] | None,
        ast_impact: dict[str, Any] | None,
        branch: str | None = None,
        commit_sha: str | None = None,
    ) -> str:
        """Format a rich Markdown completion comment with AST impact and Proof bundle."""
        task_id = task.get("task_id", "N/A")
        proof = proof_bundle or {}

        # Resolve AST impact structure
        impact = ast_impact
        if impact is None:
            raw_ast = proof.get("ast_impact") or proof.get("ast_impact_json")
            if isinstance(raw_ast, str):
                try:
                    impact = json.loads(raw_ast)
                except Exception:
                    impact = {}
            elif isinstance(raw_ast, dict):
                impact = raw_ast
            else:
                impact = {}

        # Resolve verification results
        raw_verif = proof.get("verification_results") or proof.get("verification_results_json")
        if isinstance(raw_verif, str):
            try:
                verif = json.loads(raw_verif)
            except Exception:
                verif = {}
        elif isinstance(raw_verif, dict):
            verif = raw_verif
        else:
            verif = {}

        # Build AST impact table rows
        ast_rows: list[str] = []
        mod_symbols = impact.get("modified_symbols") or []
        mod_files = impact.get("modified_files") or []
        hop1 = impact.get("hop1_dependents") or []
        impacted_tests = impact.get("impacted_tests") or []

        if mod_symbols:
            callers_str = ", ".join(f"`{c}`" for c in hop1[:4]) if hop1 else "None"
            for sym in mod_symbols:
                ast_rows.append(f"| `{sym}` | Modified Symbol | {callers_str} | R1 |")

        if mod_files and not mod_symbols:
            for f in mod_files:
                ast_rows.append(f"| `{f}` | Modified File | Direct | R0 |")

        if impacted_tests:
            for t in impacted_tests:
                ast_rows.append(f"| `{t}` | Impacted Test | Pinned Verification | R0 |")

        if not ast_rows:
            warning = impact.get("warning_message") or "Non-AST task; zero AST blast radius."
            ast_rows.append(f"| None | {warning} | None | R0 |")

        ast_table = "\n".join(ast_rows)

        # Build Proof metrics
        passed = verif.get("passed", True)
        status_badge = "✅ Passed" if passed else "❌ Failed"
        diff_sha = proof.get("git_diff_sha", "N/A")
        tree_sha = proof.get("workspace_tree_sha", "N/A")
        exit_code = verif.get("exit_code", 0)
        pinned = verif.get("pinned_blast_radius_targets") or impacted_tests or []
        pinned_str = ", ".join(f"`{p}`" for p in pinned) if pinned else "None (journal validated)"
        runtime_digest = proof.get("runtime_digest", "N/A")
        sig = proof.get("ledger_signature", "N/A")
        sig_excerpt = f"`{sig[:28]}...`" if len(sig) > 30 else f"`{sig}`"

        git_section = ""
        if branch or commit_sha:
            git_section = f"""
### 🌿 Git Autopilot Commit
| Property | Value |
| :--- | :--- |
| **Branch** | `{branch or 'N/A'}` |
| **Commit SHA** | `{commit_sha or 'N/A'}` |
| **Task ID** | `{task_id}` |
"""

        body = f"""## 🤖 HEX Task Succeeded

### 🎯 Instruction
> {trigger.instruction}

### 🔍 Active Graphify AST Impact Matrix
| Symbol / File | Impact Kind | Affected Callers / Tests | Risk Class |
| :--- | :--- | :--- | :--- |
{ast_table}

### 🛡️ Proof-Carrying Artifact Verification
| Metric | Verification Value |
| :--- | :--- |
| **Verification Status** | {status_badge} |
| **Diff SHA-256** | `{diff_sha}` |
| **Merkle Tree SHA-256** | `{tree_sha}` |
| **Verification Exit Code** | `{exit_code}` |
| **Pinned Verification Targets** | {pinned_str} |
| **Runtime Digest** | `{runtime_digest}` |
| **Ledger Signature** | {sig_excerpt} |
{git_section}
---
*Automated execution verified by HEX Autonomous AI Workbench*
"""
        return redact_secrets(body)

    def format_failure_comment(
        self,
        trigger: TriggerMatch,
        task: dict[str, Any] | None,
        error_message: str,
    ) -> str:
        """Format markdown failure comment."""
        task_id = task.get("task_id", "N/A") if task else "N/A"
        body = (
            f"## ❌ HEX Task Execution Failed\n\n"
            f"### 🎯 Instruction\n"
            f"> {trigger.instruction}\n\n"
            f"### ⚠️ Error Details\n"
            f"Task `{task_id}` failed during isolated CoW workspace verification:\n\n"
            f"```\n{error_message}\n```\n\n"
            f"---\n*HEX Autonomous AI Workbench*"
        )
        return redact_secrets(body)

    def execute_action(
        self,
        orchestrator: HarnessOrchestrator,
        event_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute task for matched bot trigger inside isolated CoW workspace."""
        trigger = self.detect_trigger(event_name, payload)
        if trigger is None:
            return {
                "status": "skipped",
                "reason": "No bot trigger detected in event payload",
            }

        target_issue = trigger.issue_number or trigger.pr_number

        # 1. Post acknowledgement comment if target issue/PR is known
        if target_issue is not None and self.transport is not None:
            ack_body = self.format_ack_comment(trigger)
            try:
                self.transport.post_issue_comment(
                    repo_slug=self.config.repository or None,
                    issue_number=target_issue,
                    body=redact_secrets(ack_body),
                )
            except Exception as e:
                logger.warning("Failed to post GitHub acknowledgement comment: %s", e)

        # 2. Determine workspace root and prepare autopilot
        ws_root = (
            Path(self.autopilot.workspace_root)
            if self.autopilot
            else (
                Path(orchestrator.default_workspace)
                if orchestrator.default_workspace
                else Path.cwd()
            )
        ).resolve()

        if self.autopilot is None:
            auto_cfg = GitAutopilotConfig(
                repo_slug=self.config.repository or None,
                github_token=self.config.token,
                dry_run=self.config.dry_run,
            )
            self.autopilot = GitAutopilot(
                workspace_root=ws_root,
                config=auto_cfg,
                transport=self.transport,
            )

        # 3. Submit and execute task via HarnessOrchestrator
        from .models import TaskSubmitRequest

        project_id = self.config.repository or "gh-action"
        req = TaskSubmitRequest(
            goal=trigger.instruction,
            project_id=project_id,
            workspace_root=str(ws_root),
        )

        task = orchestrator.submit_and_run(req, auto_execute=True)
        task_id = task.get("task_id", "")
        task_status = task.get("status") or task.get("state")
        is_success = task_status in ("SUCCEEDED", "COMPLETED")
        proof = orchestrator.ledger.get_proof_bundle(task_id) if task_id else None

        # 4. Handle Failure
        if not is_success:
            err_msg = task.get("error_message") or f"Task execution failed with status: {task_status}"
            fail_body = self.format_failure_comment(trigger, task, err_msg)
            if target_issue is not None and self.transport is not None:
                try:
                    self.transport.post_issue_comment(
                        repo_slug=self.config.repository or None,
                        issue_number=target_issue,
                        body=redact_secrets(fail_body),
                    )
                except Exception as e:
                    logger.warning("Failed to post GitHub failure comment: %s", e)

            return {
                "status": "failed",
                "task_id": task_id,
                "error": err_msg,
                "trigger": trigger,
                "task": task,
                "proof": proof,
            }

        # 5. Handle Success: commit changes and create branch if git repo
        branch_name = trigger.branch
        commit_sha: str | None = None
        has_git = (ws_root / ".git").is_dir() or self.config.dry_run

        if has_git and self.autopilot:
            if not branch_name:
                branch_name = self.autopilot.create_task_branch(
                    task_id=task_id,
                    goal=trigger.instruction,
                )
            elif not self.autopilot.config.dry_run and (ws_root / ".git").is_dir():
                subprocess.run(
                    ["git", "checkout", "-B", branch_name],
                    cwd=ws_root,
                    capture_output=True,
                    text=True,
                    check=False,
                )

            commit_sha = self.autopilot.commit_task_changes(
                task_id=task_id,
                scope="gh-action",
                description=trigger.instruction,
            )

        ast_impact = proof.get("ast_impact") if proof else None
        completion_body = self.format_completion_comment(
            trigger=trigger,
            task=task,
            proof_bundle=proof,
            ast_impact=ast_impact,
            branch=branch_name,
            commit_sha=commit_sha,
        )

        if target_issue is not None and self.transport is not None:
            try:
                self.transport.post_issue_comment(
                    repo_slug=self.config.repository or None,
                    issue_number=target_issue,
                    body=redact_secrets(completion_body),
                )
            except Exception as e:
                logger.warning("Failed to post GitHub completion comment: %s", e)

        return {
            "status": "completed",
            "task_id": task_id,
            "proof": proof,
            "trigger": trigger,
            "branch": branch_name,
            "commit": commit_sha,
        }

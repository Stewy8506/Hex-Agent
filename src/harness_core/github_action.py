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
            # Pattern matching bot name as standalone token or followed by colon/comma
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

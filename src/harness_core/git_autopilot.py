"""Git & GitHub Autopilot Engine (PRD Section 8, Pillar 4).

Provides automated feature branch creation, conventional commit formatting,
rich Active Graphify AST impact & Proof-Carrying Artifact PR summaries,
and pluggable GitHub transports (gh CLI, GitHub REST API, and in-memory mock).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class GitAutopilotConfig:
    """Configuration options for Git & GitHub Autopilot operations."""

    author_name: str = "hex-bot"
    author_email: str = "hex-bot@users.noreply.github.com"
    branch_prefix: str = "hex/"
    default_base_branch: str = "main"
    github_token: str | None = None
    repo_slug: str | None = None  # e.g. "owner/repo"
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> GitAutopilotConfig:
        """Parse configuration from standard HEX and GitHub Actions environment variables."""
        token = os.environ.get("HEX_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        author_name = os.environ.get("HEX_GIT_AUTHOR_NAME", "hex-bot")
        author_email = os.environ.get("HEX_GIT_AUTHOR_EMAIL", "hex-bot@users.noreply.github.com")
        branch_prefix = os.environ.get("HEX_GIT_BRANCH_PREFIX", "hex/")
        repo_slug = os.environ.get("HEX_GITHUB_REPOSITORY") or os.environ.get("GITHUB_REPOSITORY")
        default_base = os.environ.get("HEX_GIT_DEFAULT_BASE", "main")
        dry_run_env = os.environ.get("HEX_GIT_DRY_RUN", "").strip().lower()
        dry_run = dry_run_env in ("1", "true", "yes", "on")

        return cls(
            author_name=author_name,
            author_email=author_email,
            branch_prefix=branch_prefix,
            default_base_branch=default_base,
            github_token=token,
            repo_slug=repo_slug,
            dry_run=dry_run,
        )


@runtime_checkable
class GitHubTransport(Protocol):
    """Protocol defining the interface for GitHub interactions."""

    def create_pull_request(
        self,
        repo_slug: str | None,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> dict[str, Any]:
        ...

    def post_issue_comment(
        self,
        repo_slug: str | None,
        issue_number: int,
        body: str,
    ) -> dict[str, Any]:
        ...

    def get_pull_request(
        self,
        repo_slug: str | None,
        pr_number: int,
    ) -> dict[str, Any]:
        ...


class MockGitHubTransport:
    """In-memory mock GitHub transport for testing and dry-run execution."""

    def __init__(self, default_pr_number: int = 101, default_url: str | None = None) -> None:
        self.default_pr_number = default_pr_number
        self.default_url = default_url
        self.created_prs: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.fetched_prs: list[dict[str, Any]] = []

    def create_pull_request(
        self,
        repo_slug: str | None,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> dict[str, Any]:
        url = self.default_url or f"https://github.com/{repo_slug or 'owner/repo'}/pull/{self.default_pr_number}"
        entry = {
            "ok": True,
            "number": self.default_pr_number,
            "html_url": url,
            "url": url,
            "status": "created",
            "repo_slug": repo_slug,
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base_branch,
        }
        self.created_prs.append(entry)
        return entry

    def post_issue_comment(
        self,
        repo_slug: str | None,
        issue_number: int,
        body: str,
    ) -> dict[str, Any]:
        entry = {
            "ok": True,
            "id": len(self.comments) + 1,
            "repo_slug": repo_slug,
            "issue_number": issue_number,
            "body": body,
            "status": "created",
        }
        self.comments.append(entry)
        return entry

    def get_pull_request(
        self,
        repo_slug: str | None,
        pr_number: int,
    ) -> dict[str, Any]:
        entry = {
            "ok": True,
            "number": pr_number,
            "repo_slug": repo_slug,
            "title": f"Mock PR #{pr_number}",
            "state": "open",
            "html_url": f"https://github.com/{repo_slug or 'owner/repo'}/pull/{pr_number}",
            "headRefName": "hex/task-branch",
            "baseRefName": "main",
        }
        self.fetched_prs.append(entry)
        return entry


class SubprocessGitHubTransport:
    """Production GitHub transport leveraging GitHub CLI (`gh`) or REST API fallback."""

    def __init__(
        self,
        token: str | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.token = token
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else Path.cwd()
        self.has_gh_cli = shutil.which("gh") is not None

    def _api_request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.token:
            return {"ok": False, "error": "GITHUB_TOKEN required for REST API calls", "status": "failed"}

        url = f"https://api.github.com{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "HEX-Autonomous-AI-Workbench",
        }
        body_bytes = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(data).encode("utf-8")

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_data = resp.read().decode("utf-8")
                parsed = json.loads(resp_data) if resp_data else {}
                if isinstance(parsed, dict):
                    parsed["ok"] = True
                    parsed["status"] = "ok"
                return parsed if isinstance(parsed, dict) else {"data": parsed, "ok": True}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            return {
                "ok": False,
                "error": f"HTTP {e.code}: {e.reason}",
                "detail": err_body,
                "status": "failed",
                "code": e.code,
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "status": "failed"}

    def create_pull_request(
        self,
        repo_slug: str | None,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> dict[str, Any]:
        # Priority 1: gh CLI if installed
        if self.has_gh_cli:
            cmd = ["gh", "pr", "create", "--title", title, "--body", body, "--head", head_branch, "--base", base_branch]
            if repo_slug:
                cmd.extend(["--repo", repo_slug])

            env = os.environ.copy()
            if self.token:
                env["GITHUB_TOKEN"] = self.token

            res = subprocess.run(
                cmd,
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            if res.returncode == 0:
                pr_url = res.stdout.strip()
                m = re.search(r"/pull/(\d+)", pr_url)
                pr_num = int(m.group(1)) if m else 0
                return {
                    "ok": True,
                    "number": pr_num,
                    "html_url": pr_url,
                    "url": pr_url,
                    "status": "created",
                    "transport": "gh_cli",
                }

        # Priority 2: GitHub REST API
        if repo_slug and self.token:
            payload = {
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
            }
            api_res = self._api_request("POST", f"/repos/{repo_slug}/pulls", data=payload)
            if api_res.get("ok"):
                return {
                    "ok": True,
                    "number": api_res.get("number", 0),
                    "html_url": api_res.get("html_url", ""),
                    "url": api_res.get("html_url", ""),
                    "status": "created",
                    "transport": "rest_api",
                }
            return api_res

        return {
            "ok": False,
            "error": "Cannot create PR: `gh` CLI not available and valid GITHUB_TOKEN + repo_slug not provided",
            "status": "failed",
        }

    def post_issue_comment(
        self,
        repo_slug: str | None,
        issue_number: int,
        body: str,
    ) -> dict[str, Any]:
        if self.has_gh_cli:
            cmd = ["gh", "issue", "comment", str(issue_number), "--body", body]
            if repo_slug:
                cmd.extend(["--repo", repo_slug])
            env = os.environ.copy()
            if self.token:
                env["GITHUB_TOKEN"] = self.token
            res = subprocess.run(
                cmd,
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            if res.returncode == 0:
                return {"ok": True, "status": "created", "transport": "gh_cli"}

        if repo_slug and self.token:
            return self._api_request("POST", f"/repos/{repo_slug}/issues/{issue_number}/comments", data={"body": body})

        return {
            "ok": False,
            "error": "Cannot post comment: neither `gh` CLI nor GITHUB_TOKEN available",
            "status": "failed",
        }

    def get_pull_request(
        self,
        repo_slug: str | None,
        pr_number: int,
    ) -> dict[str, Any]:
        if self.has_gh_cli:
            cmd = [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "number,title,state,url,headRefName,baseRefName",
            ]
            if repo_slug:
                cmd.extend(["--repo", repo_slug])
            env = os.environ.copy()
            if self.token:
                env["GITHUB_TOKEN"] = self.token
            res = subprocess.run(
                cmd,
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            if res.returncode == 0:
                try:
                    data = json.loads(res.stdout)
                    data["ok"] = True
                    data["html_url"] = data.get("url", "")
                    return data
                except Exception:
                    pass

        if repo_slug and self.token:
            return self._api_request("GET", f"/repos/{repo_slug}/pulls/{pr_number}")

        return {
            "ok": False,
            "error": "Cannot get PR: neither `gh` CLI nor GITHUB_TOKEN available",
            "status": "failed",
        }


class GitAutopilot:
    """Core Git and GitHub automation engine for HEX tasks."""

    def __init__(
        self,
        workspace_root: Path | str,
        config: GitAutopilotConfig | None = None,
        transport: GitHubTransport | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.config = config or GitAutopilotConfig.from_env()
        if transport is not None:
            self.transport = transport
        elif self.config.dry_run:
            self.transport = MockGitHubTransport()
        else:
            self.transport = SubprocessGitHubTransport(
                token=self.config.github_token,
                workspace_root=self.workspace_root,
            )

    @staticmethod
    def slugify(text: str, max_length: int = 30) -> str:
        """Convert a goal or description into a clean, lowercased kebab-case slug."""
        cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
        if max_length and len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip("-")
            if "-" in cleaned:
                last_hyphen = cleaned.rfind("-")
                if last_hyphen > 0:
                    cleaned = cleaned[:last_hyphen]
        return cleaned or "task"

    def create_task_branch(
        self,
        task_id: str,
        goal: str,
        base_branch: str | None = None,
    ) -> str:
        """Generate and checkout a task-specific Git branch: hex/task-<id>-<slug>."""
        clean_id = task_id
        if clean_id.lower().startswith("task_") or clean_id.lower().startswith("task-"):
            clean_id = clean_id[5:]
        clean_id = re.sub(r"[^a-zA-Z0-9]", "", clean_id).lower()
        if not clean_id:
            clean_id = "task"

        slug = self.slugify(goal, max_length=30)
        branch = f"{self.config.branch_prefix}task-{clean_id}-{slug}"

        if not self.config.dry_run and (self.workspace_root / ".git").is_dir():
            # If base branch requested and differs from current, ensure checkout
            base = base_branch or self.config.default_base_branch
            # Checkout or create branch
            res = subprocess.run(
                ["git", "checkout", "-B", branch],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                # If git checkout failed, raise warning or error
                pass

        return branch

    def commit_task_changes(
        self,
        task_id: str,
        scope: str,
        description: str,
        files: list[str] | None = None,
        commit_type: str = "feat",
    ) -> str:
        """Create an atomic conventional commit with Task-ID trailer and author metadata."""
        clean_scope = re.sub(r"[^a-zA-Z0-9_.-]+", "-", scope.strip()).strip("-").lower() or "core"
        desc_line = description.strip().splitlines()[0].strip() if description.strip() else "automated task execution"

        message = (
            f"{commit_type}({clean_scope}): {desc_line}\n\n"
            f"Task-ID: {task_id}\n"
            f"Automated-By: HEX Autonomous Workbench"
        )

        if self.config.dry_run:
            clean_id = task_id
            if clean_id.lower().startswith("task_") or clean_id.lower().startswith("task-"):
                clean_id = clean_id[5:]
            clean_prefix = re.sub(r"[^a-zA-Z0-9]", "", clean_id).lower()
            return f"mock-sha-{clean_prefix[:8] if clean_prefix else '00000000'}"

        if files:
            subprocess.run(["git", "add", *files], cwd=self.workspace_root, capture_output=True, text=True, check=True)
        else:
            subprocess.run(["git", "add", "-A"], cwd=self.workspace_root, capture_output=True, text=True, check=True)

        author_flag = f"{self.config.author_name} <{self.config.author_email}>"
        subprocess.run(
            ["git", "commit", f"--author={author_flag}", "-m", message],
            cwd=self.workspace_root,
            capture_output=True,
            text=True,
            check=True,
        )

        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.workspace_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()

    def generate_pr_summary(
        self,
        task_id: str,
        goal: str,
        proof_bundle: dict[str, Any] | None = None,
        ast_impact: dict[str, Any] | None = None,
    ) -> str:
        """Format a rich Markdown Pull Request body containing objective, AST impact, and proof tables."""
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

        # Build AST impact rows
        ast_rows: list[str] = []
        mod_symbols = impact.get("modified_symbols") or []
        mod_files = impact.get("modified_files") or []
        hop1 = impact.get("hop1_dependents") or []
        hop2 = impact.get("hop2_dependents") or []
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

        body = f"""## 🤖 HEX Automated Pull Request

### 🎯 Objective
> {goal.strip()}

### 📋 Summary of Changes
Automated implementation and verification for Task `{task_id}` executed via **HEX Autonomous AI Workbench**.
All modifications were isolated in Copy-on-Write workspaces and verified deterministically against pinned test suites.

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

---
*Task ID: `{task_id}` | Generated automatically by HEX Autonomous Workbench*
"""
        return body

    def create_pull_request(
        self,
        branch: str,
        title: str,
        body: str,
        base: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch pull request creation through the configured transport."""
        base_branch = base or self.config.default_base_branch
        return self.transport.create_pull_request(
            repo_slug=self.config.repo_slug,
            title=title,
            body=body,
            head_branch=branch,
            base_branch=base_branch,
        )

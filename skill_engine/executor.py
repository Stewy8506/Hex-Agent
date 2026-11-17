"""Deterministic Skill Verification Hook Engine.

Executes verify: hooks embedded in SKILL.md bundles post-execution,
turning instructional skills into automated regression assertions.
Supports timeout bounds, environment overrides, output capture, and AST invariants.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from harness_core.cognition.skillspector import (
    SkillSpectorScanner,
    compute_bundle_sha256,
    get_approved_skill_hash,
    parse_skill_frontmatter,
)


class SecurityError(PermissionError):
    """Raised when a skill bundle fails hash verification, integrity checks, or sandbox policy."""

    pass


SAFE_SKILL_ENV_VARS: set[str] = {
    "PATH",
    "HOME",
    "TMPDIR",
    "USER",
    "LANG",
    "LC_ALL",
    "TERM",
    "PYTHONPATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
}

BLOCKED_ENV_KEY_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)(API_KEY|SECRET|TOKEN|PASS|PASSWORD|AUTH|CREDENTIAL|PRIVATE|AWS_|GITHUB_|GH_|HARNESS_|SSH_|OPENAI|ANTHROPIC|GEMINI|DATABASE_URL|POSTGRES|AZURE|GCP|GOOGLE)"
)


def build_minimal_skill_env(
    base_env: Optional[dict[str, str]] = None,
    env_vars: Optional[dict[str, str]] = None,
    env_overrides: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Builds a minimal allowlisted environment inheriting safe system variables and stripping all secrets."""
    source = base_env if base_env is not None else os.environ
    sanitized: dict[str, str] = {}

    for k in SAFE_SKILL_ENV_VARS:
        if k in source:
            val = source[k]
            if not BLOCKED_ENV_KEY_PATTERNS.search(k):
                sanitized[k] = str(val)

    if "PATH" not in sanitized:
        sanitized["PATH"] = "/usr/bin:/bin:/usr/local/bin"

    for extra in (env_vars, env_overrides):
        if extra:
            for k, v in extra.items():
                if not BLOCKED_ENV_KEY_PATTERNS.search(k):
                    sanitized[k] = str(v)

    return sanitized


@dataclass
class SkillVerificationHook:
    """Structured deterministic verification hook configuration from SKILL.md."""

    command: str
    timeout_seconds: float = 30.0
    must_pass: bool = True
    ast_invariant: Optional[str] = None
    test_filter: Optional[str] = None
    env_vars: Optional[dict[str, str]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "timeout_seconds": self.timeout_seconds,
            "must_pass": self.must_pass,
            "ast_invariant": self.ast_invariant,
            "test_filter": self.test_filter,
            "env_vars": self.env_vars,
        }


@dataclass
class SkillVerificationResult:
    """Structured execution result of running a skill's verification hook."""

    skill_name: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    passed: bool
    duration_ms: float
    error_message: Optional[str] = None
    ast_invariant_passed: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "error_message": self.error_message,
            "ast_invariant_passed": self.ast_invariant_passed,
        }


class SkillExecutionEngine:
    """Executes deterministic verification hooks and regression assertions for skills."""

    def __init__(
        self,
        default_workspace_root: Optional[Path | str] = None,
        default_timeout: float = 30.0,
        ledger: Optional[Any] = None,
        scanner: Optional[Any] = None,
    ):
        self.default_workspace_root = (
            Path(default_workspace_root).resolve()
            if default_workspace_root
            else Path.cwd().resolve()
        )
        self.default_timeout = default_timeout
        self.ledger = ledger
        self.scanner = scanner

    def extract_verification_hook(
        self, skill_path_or_text: Path | str
    ) -> Optional[SkillVerificationHook]:
        """Extract and normalize the verify: hook from SKILL.md frontmatter."""
        text: str = ""
        if isinstance(skill_path_or_text, (Path, str)):
            candidate_path = Path(str(skill_path_or_text))
            if "\n" not in str(skill_path_or_text) and candidate_path.exists():
                if candidate_path.is_dir():
                    skill_file = candidate_path / "SKILL.md"
                    if not skill_file.is_file():
                        return None
                    text = skill_file.read_text(encoding="utf-8", errors="replace")
                elif candidate_path.is_file():
                    text = candidate_path.read_text(encoding="utf-8", errors="replace")
            else:
                text = str(skill_path_or_text)
        else:
            text = str(skill_path_or_text)

        fm, _ = parse_skill_frontmatter(text)
        raw_hook = fm.get("verify")
        if not raw_hook:
            return None

        if isinstance(raw_hook, str):
            return SkillVerificationHook(
                command=raw_hook.strip(),
                timeout_seconds=self.default_timeout,
                must_pass=True,
            )
        elif isinstance(raw_hook, dict):
            return SkillVerificationHook(
                command=str(raw_hook.get("command", "")).strip(),
                timeout_seconds=float(raw_hook.get("timeout_seconds", self.default_timeout)),
                must_pass=bool(raw_hook.get("must_pass", True)),
                ast_invariant=raw_hook.get("ast_invariant"),
                test_filter=raw_hook.get("test_filter"),
                env_vars=raw_hook.get("env_vars"),
            )
        elif isinstance(raw_hook, SkillVerificationHook):
            return raw_hook

        return None

    def check_ast_invariant(
        self, workspace_root: Path, invariant: str
    ) -> tuple[bool, str]:
        """Validate AST or token invariant across workspace files.

        Supported syntax:
        - '<symbol> in <relative_file_path>' (e.g. 'test_rpc_schema_sync in tests/test_rpc.py')
        """
        inv = invariant.strip()
        if " in " in inv:
            parts = inv.split(" in ", 1)
            target_symbol = parts[0].strip()
            rel_file = parts[1].strip()
            target_file = workspace_root / rel_file

            if not target_file.is_file():
                return False, f"Target file '{rel_file}' not found in workspace"

            content = target_file.read_text(encoding="utf-8", errors="replace")

            if rel_file.endswith(".py"):
                try:
                    tree = ast.parse(content, filename=str(target_file))
                    clean_sym = re.sub(r"^(?:def|class|async\s+def)\s+", "", target_symbol).strip()
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == clean_sym:
                            return True, f"Found AST node '{clean_sym}' in '{rel_file}'"
                        elif isinstance(node, ast.Name) and node.id == clean_sym:
                            return True, f"Found AST identifier '{clean_sym}' in '{rel_file}'"

                    return False, f"Symbol '{clean_sym}' not found in AST of '{rel_file}'"
                except Exception as exc:
                    if target_symbol in content:
                        return True, f"Found symbol '{target_symbol}' in raw content of '{rel_file}'"
                    return False, f"AST parsing failed for '{rel_file}': {exc}"
            else:
                if target_symbol in content:
                    return True, f"Found '{target_symbol}' in '{rel_file}'"
                return False, f"Symbol '{target_symbol}' not found in '{rel_file}'"

        return False, f"Unrecognized AST invariant format: '{invariant}'"

    def run_skill_verification(
        self,
        hook: SkillVerificationHook | str,
        workspace_root: Optional[Path | str] = None,
        skill_name: str = "unknown",
        env_overrides: Optional[dict[str, str]] = None,
        sandbox: Optional[Any] = None,
        skill_bundle_dir: Optional[Path | str] = None,
        approved_sha256: Optional[str] = None,
        ledger: Optional[Any] = None,
        scanner: Optional[Any] = None,
    ) -> SkillVerificationResult:
        """Execute the verification hook command inside workspace_root with timeout protection and sandbox isolation."""
        if isinstance(hook, str):
            extracted = self.extract_verification_hook(hook)
            if extracted is not None:
                hook = extracted
            else:
                hook = SkillVerificationHook(command=hook.strip())

        # Supply-chain verification: Bundle hash check against SkillSpector approved record
        if skill_bundle_dir is not None:
            bundle_p = Path(skill_bundle_dir).resolve()
            if not bundle_p.exists():
                raise FileNotFoundError(f"Skill bundle directory does not exist: {bundle_p}")
            candidate_hash = compute_bundle_sha256(bundle_p)

            expected_hash: Optional[str] = approved_sha256
            active_ledger = ledger or self.ledger or getattr(scanner or self.scanner, "ledger", None)
            active_scanner = scanner or self.scanner

            if not expected_hash and active_ledger is not None:
                expected_hash = get_approved_skill_hash(
                    bundle_p, ledger=active_ledger
                ) or get_approved_skill_hash(skill_name, ledger=active_ledger)

            if not expected_hash and active_scanner is not None and getattr(active_scanner, "ledger", None):
                expected_hash = get_approved_skill_hash(
                    bundle_p, ledger=active_scanner.ledger
                ) or get_approved_skill_hash(skill_name, ledger=active_scanner.ledger)

            if not expected_hash:
                expected_hash = get_approved_skill_hash(bundle_p)

            if not expected_hash:
                raise SecurityError(
                    f"Skill bundle hash mismatch or unapproved skill: skill '{skill_name}' at '{bundle_p}' "
                    "has no approved registration in SkillSpector"
                )

            if candidate_hash != expected_hash:
                raise SecurityError(
                    f"Skill bundle hash mismatch or unapproved skill: candidate SHA-256 ({candidate_hash[:12]}) "
                    f"does not match approved digest ({expected_hash[:12]})"
                )

        ws = (
            Path(workspace_root).resolve()
            if workspace_root
            else self.default_workspace_root
        )

        cmd = hook.command.strip()
        if hook.test_filter and hook.test_filter not in cmd:
            cmd = f"{cmd} {hook.test_filter}"

        # Build clean minimal allowlisted environment (strips provider keys, SSH, credentials)
        env = build_minimal_skill_env(
            base_env=os.environ,
            env_vars=hook.env_vars,
            env_overrides=env_overrides,
        )

        start_time = time.perf_counter()
        timed_out = False
        stdout_str = ""
        stderr_str = ""
        exit_code = 0
        error_msg: Optional[str] = None

        if sandbox is not None:
            if isinstance(cmd, list):
                cmd_list = [str(c) for c in cmd]
            else:
                cmd_str = str(cmd).strip()
                if any(op in cmd_str for op in ("|", ">", "<", ";", "&&", "||", "$")):
                    cmd_list = ["/bin/sh", "-c", cmd_str]
                else:
                    cmd_list = shlex.split(cmd_str)

            try:
                if hasattr(sandbox, "execute"):
                    s_res = sandbox.execute(
                        cmd=cmd_list,
                        cwd=ws,
                        env=env,
                        timeout=hook.timeout_seconds,
                    )
                elif hasattr(sandbox, "run_command"):
                    s_res = sandbox.run_command(
                        cmd=cmd_list,
                        cwd=ws,
                        env=env,
                        timeout=hook.timeout_seconds,
                    )
                else:
                    raise TypeError(f"Unsupported sandbox instance: {type(sandbox)}")

                stdout_str = s_res.stdout or ""
                stderr_str = s_res.stderr or ""
                exit_code = s_res.exit_code
                timed_out = s_res.timed_out
                if s_res.violation_detected:
                    exit_code = -1
                    error_msg = f"Sandbox violation: {s_res.violation_reason}"
                elif timed_out:
                    error_msg = f"Verification timed out after {hook.timeout_seconds}s"
            except Exception as exc:
                exit_code = -1
                error_msg = f"Sandbox execution failed: {exc}"
        else:
            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(ws),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=hook.timeout_seconds,
                )
                stdout_str = proc.stdout
                stderr_str = proc.stderr
                exit_code = proc.returncode
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                exit_code = -1
                stdout_str = exc.stdout or "" if isinstance(exc.stdout, str) else (
                    exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
                )
                stderr_str = exc.stderr or "" if isinstance(exc.stderr, str) else (
                    exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
                )
                error_msg = f"Verification timed out after {hook.timeout_seconds}s"
            except Exception as exc:
                exit_code = -1
                error_msg = f"Failed to execute verification command: {exc}"

        duration_ms = (time.perf_counter() - start_time) * 1000.0

        ast_passed: Optional[bool] = None
        if hook.ast_invariant and not timed_out:
            ast_ok, ast_detail = self.check_ast_invariant(ws, hook.ast_invariant)
            ast_passed = ast_ok
            if not ast_ok:
                error_msg = (
                    f"{error_msg}; {ast_detail}" if error_msg else f"AST invariant check failed: {ast_detail}"
                )

        command_passed = (exit_code == 0) and not timed_out
        ast_condition = (ast_passed is not False)
        if hook.must_pass:
            passed = command_passed and ast_condition
        else:
            passed = not timed_out

        if not passed and not error_msg:
            error_msg = f"Verification command failed with exit code {exit_code}"

        return SkillVerificationResult(
            skill_name=skill_name,
            command=cmd if isinstance(cmd, str) else " ".join(cmd),
            exit_code=exit_code,
            stdout=stdout_str,
            stderr=stderr_str,
            passed=passed,
            duration_ms=duration_ms,
            error_message=error_msg,
            ast_invariant_passed=ast_passed,
        )


def assert_skill_regression_free(
    skill_dir_or_text: Path | str,
    workspace_root: Path | str,
    env_overrides: Optional[dict[str, str]] = None,
    sandbox: Optional[Any] = None,
    skill_bundle_dir: Optional[Path | str] = None,
    approved_sha256: Optional[str] = None,
    ledger: Optional[Any] = None,
    scanner: Optional[Any] = None,
) -> Optional[SkillVerificationResult]:
    """Execute deterministic verify: hook from a skill and assert regression-free status."""
    engine = SkillExecutionEngine(
        default_workspace_root=workspace_root, ledger=ledger, scanner=scanner
    )
    hook = engine.extract_verification_hook(skill_dir_or_text)
    if hook is None:
        return None

    skill_name = "unknown"
    if isinstance(skill_dir_or_text, (Path, str)):
        p = Path(str(skill_dir_or_text))
        if "\n" not in str(skill_dir_or_text) and p.exists():
            skill_name = p.name if p.is_dir() else p.parent.name

    result = engine.run_skill_verification(
        hook=hook,
        workspace_root=workspace_root,
        skill_name=skill_name,
        env_overrides=env_overrides,
        sandbox=sandbox,
        skill_bundle_dir=skill_bundle_dir,
        approved_sha256=approved_sha256,
        ledger=ledger,
        scanner=scanner,
    )

    if not result.passed and hook.must_pass:
        details = result.stderr.strip() or result.stdout.strip() or result.error_message
        raise AssertionError(
            f"Skill '{result.skill_name}' regression verification failed (exit {result.exit_code}): {details}"
        )

    return result

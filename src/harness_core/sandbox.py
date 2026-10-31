"""Container and Subprocess Execution Sandbox Engine.

Provides containment for untrusted tool invocations and code evaluation:
- BubblewrapSandbox: Linux unprivileged user namespace containment (bwrap) with network isolation.
- DockerPodmanSandbox: OCI container containment with read-only root and network isolation.
- RestrictedSubprocessSandbox: Portable POSIX/macOS fallback enforcing workspace boundaries,
  environment credential stripping, process group management, and resource limits.
- get_default_sandbox: Platform detection factory with safe fallback.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

SENSITIVE_ENV_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)(API_KEY|SECRET|TOKEN|PASS|PASSWORD|AUTH|CREDENTIAL|PRIVATE|AWS_|GITHUB_|GH_|HARNESS_)"
)

SAFE_BASE_ENV_VARS: set[str] = {
    "PATH",
    "LANG",
    "LC_ALL",
    "PYTHONPATH",
    "SYSTEMROOT",
    "TMPDIR",
    "TEMP",
    "TMP",
}


def sanitize_environment(
    passed_env: dict[str, str] | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Strips sensitive credentials from environment.

    Only forwards safe minimal variables (PATH, LANG, LC_ALL, PYTHONPATH, SYSTEMROOT)
    from base environment (os.environ by default) and passed_env, removing any key
    matching sensitive credential patterns.
    """
    source = base_env if base_env is not None else os.environ
    sanitized: dict[str, str] = {}

    for k in ("PATH", "LANG", "LC_ALL", "PYTHONPATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP"):
        val = source.get(k)
        if val is not None and not SENSITIVE_ENV_PATTERN.search(k):
            sanitized[k] = str(val)

    if "PATH" not in sanitized:
        sanitized["PATH"] = "/usr/bin:/bin:/usr/local/bin"

    if passed_env:
        for k, v in passed_env.items():
            if not SENSITIVE_ENV_PATTERN.search(k):
                sanitized[k] = str(v)

    return sanitized


@dataclass
class SandboxResult:
    """Execution containment result."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    violation_detected: bool = False
    violation_reason: str | None = None
    sandbox_backend: str = "restricted_subprocess"
    duration_ms: float = 0.0


@runtime_checkable
class ExecutionSandbox(Protocol):
    """Protocol defining the execution sandbox contract."""

    backend_name: str

    def execute(
        self,
        cmd: list[str],
        cwd: Path | str,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> SandboxResult:
        """Executes a command within containment boundaries."""
        ...


def _run_confined_subprocess(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    backend_name: str,
) -> SandboxResult:
    """Runs a subprocess inside a dedicated process group with POSIX resource limits and clean timeouts."""
    t0 = time.perf_counter()
    timed_out = False

    def _preexec() -> None:
        try:
            os.setsid()
        except Exception:
            pass
        if sys.platform != "win32":
            try:
                import resource

                cpu_lim = max(1, int(timeout) + 2)
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_lim, cpu_lim + 1))
            except (ValueError, OSError, ImportError):
                pass
            try:
                import resource

                resource.setrlimit(resource.RLIMIT_NPROC, (512, 512))
            except (ValueError, OSError, ImportError):
                pass

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=_preexec if sys.platform != "win32" else None,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if sys.platform != "win32":
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.kill()
                    except OSError:
                        pass
            else:
                proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=1.0)
            except Exception:
                stdout, stderr = "", ""

        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        return SandboxResult(
            exit_code=proc.returncode if not timed_out else -9,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=timed_out,
            violation_detected=False,
            violation_reason=None,
            sandbox_backend=backend_name,
            duration_ms=duration_ms,
        )
    except Exception as exc:
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        return SandboxResult(
            exit_code=-1,
            stdout="",
            stderr=str(exc),
            timed_out=False,
            violation_detected=False,
            violation_reason=None,
            sandbox_backend=backend_name,
            duration_ms=duration_ms,
        )


class RestrictedSubprocessSandbox:
    """Safe, portable fallback sandbox for macOS and environments without container tools."""

    backend_name: str = "restricted_subprocess"

    def __init__(self, allowed_root: Path | str | None = None) -> None:
        self.allowed_root = Path(allowed_root).resolve() if allowed_root is not None else None

    def execute(
        self,
        cmd: list[str],
        cwd: Path | str,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> SandboxResult:
        cwd_path = Path(cwd)

        # Check for traversal patterns in path parts
        if ".." in cwd_path.parts:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                violation_detected=True,
                violation_reason=f"Path traversal detected in cwd: {cwd}",
                sandbox_backend=self.backend_name,
                duration_ms=0.0,
            )

        resolved_cwd = cwd_path.resolve()
        if not resolved_cwd.is_dir():
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                violation_detected=True,
                violation_reason=f"Working directory does not exist or is not a directory: {cwd}",
                sandbox_backend=self.backend_name,
                duration_ms=0.0,
            )

        if self.allowed_root is not None:
            resolved_root = self.allowed_root.resolve()
            try:
                resolved_cwd.relative_to(resolved_root)
            except ValueError:
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    timed_out=False,
                    violation_detected=True,
                    violation_reason=f"Working directory '{cwd}' escapes allowed workspace boundary '{self.allowed_root}'",
                    sandbox_backend=self.backend_name,
                    duration_ms=0.0,
                )

        sanitized_env = sanitize_environment(passed_env=env)
        return _run_confined_subprocess(
            cmd=cmd,
            cwd=resolved_cwd,
            env=sanitized_env,
            timeout=timeout,
            backend_name=self.backend_name,
        )


class BubblewrapSandbox:
    """Linux Bubblewrap (bwrap) unprivileged user namespace container sandbox."""

    backend_name: str = "bubblewrap"

    def __init__(
        self,
        bwrap_bin: str | None = None,
        allowed_root: Path | str | None = None,
    ) -> None:
        self.bwrap_bin = bwrap_bin or shutil.which("bwrap") or "bwrap"
        self.allowed_root = Path(allowed_root).resolve() if allowed_root is not None else None

    @classmethod
    def is_available(cls) -> bool:
        return platform.system() == "Linux" and bool(shutil.which("bwrap"))

    def build_bwrap_args(
        self,
        cmd: list[str],
        cwd: Path | str,
        env: dict[str, str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        cwd_resolved = Path(cwd).resolve()
        sanitized_env = sanitize_environment(passed_env=env)

        bwrap_cmd = [
            self.bwrap_bin,
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/etc", "/etc",
        ]
        for lib in ("/lib", "/lib64"):
            if Path(lib).exists():
                bwrap_cmd.extend(["--ro-bind", lib, lib])

        bwrap_cmd.extend([
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", str(cwd_resolved), str(cwd_resolved),
            "--chdir", str(cwd_resolved),
        ])

        bwrap_cmd.append("--clearenv")
        for k, v in sanitized_env.items():
            bwrap_cmd.extend(["--setenv", k, v])

        bwrap_cmd.append("--")
        bwrap_cmd.extend(cmd)
        return bwrap_cmd, sanitized_env

    def execute(
        self,
        cmd: list[str],
        cwd: Path | str,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> SandboxResult:
        cwd_path = Path(cwd)
        if ".." in cwd_path.parts:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                violation_detected=True,
                violation_reason=f"Path traversal detected in cwd: {cwd}",
                sandbox_backend=self.backend_name,
                duration_ms=0.0,
            )

        resolved_cwd = cwd_path.resolve()
        if not resolved_cwd.is_dir():
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                violation_detected=True,
                violation_reason=f"Working directory does not exist or is not a directory: {cwd}",
                sandbox_backend=self.backend_name,
                duration_ms=0.0,
            )

        if self.allowed_root is not None:
            try:
                resolved_cwd.relative_to(self.allowed_root.resolve())
            except ValueError:
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    timed_out=False,
                    violation_detected=True,
                    violation_reason=f"Working directory '{cwd}' escapes allowed workspace boundary '{self.allowed_root}'",
                    sandbox_backend=self.backend_name,
                    duration_ms=0.0,
                )

        bwrap_cmd, sanitized_env = self.build_bwrap_args(cmd=cmd, cwd=resolved_cwd, env=env)
        return _run_confined_subprocess(
            cmd=bwrap_cmd,
            cwd=resolved_cwd,
            env=sanitized_env,
            timeout=timeout,
            backend_name=self.backend_name,
        )


class DockerPodmanSandbox:
    """OCI container sandbox using Docker or Podman with network isolation and read-only root."""

    backend_name: str = "docker"

    def __init__(
        self,
        cli_binary: str | None = None,
        image: str = "python:3.11-slim",
        allowed_root: Path | str | None = None,
    ) -> None:
        self.cli_binary = cli_binary or shutil.which("docker") or shutil.which("podman") or "docker"
        self.backend_name = "podman" if "podman" in Path(self.cli_binary).name else "docker"
        self.image = image
        self.allowed_root = Path(allowed_root).resolve() if allowed_root is not None else None

    @classmethod
    def is_available(cls, cli_binary: str | None = None) -> bool:
        cli = cli_binary or shutil.which("docker") or shutil.which("podman")
        if not cli:
            return False
        try:
            res = subprocess.run([cli, "info"], capture_output=True, timeout=2.0)
            return res.returncode == 0
        except Exception:
            return False

    def build_container_args(
        self,
        cmd: list[str],
        cwd: Path | str,
        env: dict[str, str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        cwd_resolved = Path(cwd).resolve()
        sanitized_env = sanitize_environment(passed_env=env)

        container_cmd = [
            self.cli_binary,
            "run",
            "--rm",
            "--network", "none",
            "-v", f"{cwd_resolved}:{cwd_resolved}:rw",
            "-w", str(cwd_resolved),
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid",
        ]
        for k, v in sanitized_env.items():
            container_cmd.extend(["-e", f"{k}={v}"])

        container_cmd.append(self.image)
        container_cmd.extend(cmd)
        return container_cmd, sanitized_env

    def execute(
        self,
        cmd: list[str],
        cwd: Path | str,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> SandboxResult:
        cwd_path = Path(cwd)
        if ".." in cwd_path.parts:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                violation_detected=True,
                violation_reason=f"Path traversal detected in cwd: {cwd}",
                sandbox_backend=self.backend_name,
                duration_ms=0.0,
            )

        resolved_cwd = cwd_path.resolve()
        if not resolved_cwd.is_dir():
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                violation_detected=True,
                violation_reason=f"Working directory does not exist or is not a directory: {cwd}",
                sandbox_backend=self.backend_name,
                duration_ms=0.0,
            )

        if self.allowed_root is not None:
            try:
                resolved_cwd.relative_to(self.allowed_root.resolve())
            except ValueError:
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    timed_out=False,
                    violation_detected=True,
                    violation_reason=f"Working directory '{cwd}' escapes allowed workspace boundary '{self.allowed_root}'",
                    sandbox_backend=self.backend_name,
                    duration_ms=0.0,
                )

        container_cmd, sanitized_env = self.build_container_args(cmd=cmd, cwd=resolved_cwd, env=env)
        return _run_confined_subprocess(
            cmd=container_cmd,
            cwd=resolved_cwd,
            env=sanitized_env,
            timeout=timeout,
            backend_name=self.backend_name,
        )


def get_default_sandbox(workspace_dir: Path | str | None = None) -> ExecutionSandbox:
    """Auto-detects and instantiates the strongest available execution sandbox.

    Priority:
    1. BubblewrapSandbox (Linux with bwrap installed)
    2. DockerPodmanSandbox (functional Docker or Podman daemon)
    3. RestrictedSubprocessSandbox (portable POSIX/macOS fallback)
    """
    if BubblewrapSandbox.is_available():
        logger.info("Selected BubblewrapSandbox for container isolation")
        return BubblewrapSandbox(allowed_root=workspace_dir)

    if DockerPodmanSandbox.is_available():
        logger.info("Selected DockerPodmanSandbox for container isolation")
        return DockerPodmanSandbox(allowed_root=workspace_dir)

    logger.info("Selected RestrictedSubprocessSandbox fallback for execution isolation")
    return RestrictedSubprocessSandbox(allowed_root=workspace_dir)

"""Platform-aware shell resolution for compound command execution.

Native Windows process creation cannot resolve a hardcoded ``/bin/sh`` path,
even when the caller runs inside Git Bash. Both the gateway terminal tool and
the skill verification hook route operator-bearing commands through the host's
real shell via :func:`build_command_argv` so the same contract holds on POSIX
and Windows.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
from pathlib import Path

SHELL_OPERATOR_CHARS: tuple[str, ...] = ("|", ">", "<", ";", "&&", "||", "$")


class ShellResolutionError(RuntimeError):
    """Raised when no usable shell can be resolved for the host platform."""


def command_requires_shell(command: str) -> bool:
    """True when ``command`` contains operators a bare argv cannot express."""
    return any(op in command for op in SHELL_OPERATOR_CHARS)


def _posix_shell_argv(command: str) -> list[str]:
    if Path("/bin/sh").exists():
        return ["/bin/sh", "-c", command]
    env_shell = os.environ.get("SHELL")
    if env_shell and Path(env_shell).exists():
        return [env_shell, "-c", command]
    found = shutil.which("sh")
    if found:
        return [found, "-c", command]
    raise ShellResolutionError(
        "No POSIX shell found. Expected /bin/sh, a valid $SHELL, or 'sh' on PATH."
    )


def _windows_shell_argv(command: str) -> list[str]:
    comspec = os.environ.get("COMSPEC")
    if comspec:
        return [comspec, "/d", "/c", command]
    cmd = shutil.which("cmd.exe") or shutil.which("cmd")
    if cmd:
        return [cmd, "/d", "/c", command]
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell:
        return [powershell, "-NoProfile", "-Command", command]
    raise ShellResolutionError(
        "No Windows shell found. Set COMSPEC or ensure cmd.exe/PowerShell is on PATH."
    )


def resolve_shell_argv(command: str, *, system: str | None = None) -> list[str]:
    """Return an argv that runs ``command`` through the host's native shell.

    ``system`` overrides :func:`platform.system` for deterministic testing.
    Raises :class:`ShellResolutionError` with an actionable message when no
    shell is available rather than emitting a path the OS cannot resolve.
    """
    sys_name = system if system is not None else platform.system()
    if sys_name == "Windows":
        return _windows_shell_argv(command)
    return _posix_shell_argv(command)


def build_command_argv(command: str, *, system: str | None = None) -> list[str]:
    """Build a sandbox-ready argv, using the native shell only when needed.

    Operator-bearing commands go through :func:`resolve_shell_argv`; plain
    commands are tokenized with platform-appropriate ``shlex`` semantics so
    Windows backslash paths survive splitting.
    """
    cmd_str = str(command).strip()
    if command_requires_shell(cmd_str):
        return resolve_shell_argv(cmd_str, system=system)
    sys_name = system if system is not None else platform.system()
    return shlex.split(cmd_str, posix=(sys_name != "Windows"))


def default_path_env(*, system: str | None = None) -> str:
    """Platform-appropriate PATH fallback when the host env omits PATH."""
    sys_name = system if system is not None else platform.system()
    if sys_name == "Windows":
        return r"C:\Windows\system32;C:\Windows"
    return "/usr/bin:/bin:/usr/local/bin"
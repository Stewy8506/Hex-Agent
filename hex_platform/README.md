# `hex_platform` (`harness_core.platform`)

**Host hardware profiling, cross-platform shell resolution, and shared system diagnostics.**

`hex_platform/` maps to `harness_core.platform` in [`pyproject.toml`](../pyproject.toml) (named `hex_platform` on disk so it never shadows Python's standard-library `platform` module).

## Modules

| File | Purpose |
| :--- | :--- |
| `__init__.py` | Package marker (modules are imported explicitly via `harness_core.platform.*`). |
| `hardware.py` | `HardwareProfile.detect()`: identifies OS, CPU arch, Apple Silicon/Metal (`sysctl`), RAM GB (`sysctl`/`/proc/meminfo`), NVIDIA CUDA (`nvidia-smi`), default CoW snapshot provider, and default sandbox backend. |
| `diagnostics.py` | `DoctorCheckResult` and `SystemDiagnostics`: checks Python >=3.10, active virtualenv, optional packages (`alembic`, `pydantic`, `asyncpg`, `rich`), hardware acceleration, CoW providers (APFS/Btrfs/OverlayFS), container/process sandboxes (`bwrap`, `docker`, `podman`), and >=2 GB free disk space. |
| `shell.py` | Cross-platform compound command resolution (`ShellResolutionError`, `SHELL_OPERATOR_CHARS`, `command_requires_shell()`, `resolve_shell_argv()`, `build_command_argv()`, `default_path_env()`), handling POSIX `/bin/sh` / `$SHELL` and Windows `COMSPEC` / `cmd.exe` / PowerShell without hardcoded `/bin/sh` assumptions. |

## Integration & Tests

- `SystemDiagnostics` is subclassed by `HexDoctor` (`hex_cli/doctor.py`) and called by `storage/backup.py`.
- `HardwareProfile` is used by `agent/orchestrator.py`, `hex_cli/onboard.py`, and `hex_cli/auto_setup.py`.
- `build_command_argv()` and `default_path_env()` power `harness_core/gateway.py` (`_tool_terminal`) and `skill_engine/executor.py`.
- Tests live in [`tests/platform/`](../tests/platform).

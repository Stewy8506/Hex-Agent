"""OS daemon service installer for macOS LaunchAgent and Linux systemd.

Generates validated service descriptors with environment forwarding, provides
dry-run preview safety, and inspects active service lifecycle state.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DaemonConfig:
    """Configuration descriptor for HEX background daemon service."""

    service_name: str = "ai.hex.daemon"
    description: str = "HEX Autonomous AI Background Daemon"
    python_path: str = field(default_factory=lambda: sys.executable)
    hex_args: list[str] = field(default_factory=lambda: ["schedule", "run-due"])
    working_dir: str | None = None
    data_dir: str | None = None
    config_dir: str | None = None
    interval_seconds: int = 60
    run_at_load: bool = True
    keep_alive: bool = True
    log_dir: Path | str | None = None


class DaemonInstaller:
    """Generates and manages OS background service configurations for launchd and systemd."""

    def __init__(self, config: DaemonConfig | None = None) -> None:
        self.config = config or DaemonConfig()
        self.log_dir = (
            Path(self.config.log_dir)
            if self.config.log_dir
            else Path.home() / ".hex" / "logs"
        )
        self.data_dir = (
            Path(self.config.data_dir)
            if self.config.data_dir
            else Path.home() / ".hex" / "data"
        )

    def generate_launchd_plist(self) -> str:
        """Generates valid Apple XML plist format for macOS launchd."""
        prog_args = [self.config.python_path, "-m", "harness_core.cli", *self.config.hex_args]
        prog_args_xml = "\n".join(f"        <string>{arg}</string>" for arg in prog_args)

        env_vars = {
            "HEX_DATA_DIR": str(self.data_dir),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        }
        if self.config.config_dir:
            env_vars["HEX_CONFIG"] = str(self.config.config_dir)

        env_xml = "\n".join(
            f"        <key>{k}</key>\n        <string>{v}</string>"
            for k, v in sorted(env_vars.items())
        )

        working_dir_xml = ""
        if self.config.working_dir:
            working_dir_xml = f"\n    <key>WorkingDirectory</key>\n    <string>{self.config.working_dir}</string>"

        keep_alive_xml = ""
        if self.config.keep_alive:
            keep_alive_xml = "\n    <key>KeepAlive</key>\n    <true/>"

        run_at_load_val = "true" if self.config.run_at_load else "false"

        stdout_path = self.log_dir / "daemon.stdout.log"
        stderr_path = self.log_dir / "daemon.stderr.log"

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{self.config.service_name}</string>
    <key>ProgramArguments</key>
    <array>
{prog_args_xml}
    </array>
    <key>RunAtLoad</key>
    <{run_at_load_val}/>{keep_alive_xml}{working_dir_xml}
    <key>StartInterval</key>
    <integer>{self.config.interval_seconds}</integer>
    <key>EnvironmentVariables</key>
    <dict>
{env_xml}
    </dict>
    <key>StandardOutPath</key>
    <string>{stdout_path}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_path}</string>
</dict>
</plist>
"""

    def install_launchd(
        self,
        target_path: Path | str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Installs the service plist to ~/Library/LaunchAgents."""
        target = (
            Path(target_path)
            if target_path
            else Path.home() / "Library" / "LaunchAgents" / f"{self.config.service_name}.plist"
        )
        content = self.generate_launchd_plist()

        if dry_run:
            return {
                "status": "dry_run",
                "platform": "launchd",
                "path": str(target),
                "content": content,
            }

        target.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        target.write_text(content, encoding="utf-8")
        target.chmod(0o644)
        return {
            "status": "installed",
            "platform": "launchd",
            "path": str(target),
        }

    def generate_systemd_unit(self, user: bool = True) -> str:
        """Generates valid systemd .service unit file content."""
        args_str = " ".join(self.config.hex_args)
        exec_start = f"{self.config.python_path} -m harness_core.cli {args_str}".strip()
        wanted_by = "default.target" if user else "multi-user.target"

        stdout_path = self.log_dir / "daemon.stdout.log"
        stderr_path = self.log_dir / "daemon.stderr.log"
        path_env = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")

        lines = [
            "[Unit]",
            f"Description={self.config.description}",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={exec_start}",
            "Restart=always",
            "RestartSec=30",
            f'Environment="HEX_DATA_DIR={self.data_dir}"',
            f'Environment="PATH={path_env}"',
        ]

        if self.config.config_dir:
            lines.append(f'Environment="HEX_CONFIG={self.config.config_dir}"')

        if self.config.working_dir:
            lines.append(f"WorkingDirectory={self.config.working_dir}")

        lines.extend([
            f"StandardOutput=append:{stdout_path}",
            f"StandardError=append:{stderr_path}",
            "",
            "[Install]",
            f"WantedBy={wanted_by}",
            "",
        ])

        return "\n".join(lines)

    def install_systemd(
        self,
        user: bool = True,
        target_path: Path | str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Installs the systemd service unit file."""
        if target_path:
            target = Path(target_path)
        elif user:
            target = Path.home() / ".config" / "systemd" / "user" / f"{self.config.service_name}.service"
        else:
            target = Path("/etc/systemd/system") / f"{self.config.service_name}.service"

        content = self.generate_systemd_unit(user=user)

        if dry_run:
            return {
                "status": "dry_run",
                "platform": "systemd",
                "user": user,
                "path": str(target),
                "content": content,
            }

        target.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        target.write_text(content, encoding="utf-8")
        target.chmod(0o644)
        return {
            "status": "installed",
            "platform": "systemd",
            "user": user,
            "path": str(target),
        }

    def get_daemon_status(self) -> dict[str, Any]:
        """Detects host platform and checks if the daemon service descriptor is installed and active."""
        is_darwin = platform.system() == "Darwin"
        platform_name = "launchd" if is_darwin else "systemd"

        if is_darwin:
            service_path = Path.home() / "Library" / "LaunchAgents" / f"{self.config.service_name}.plist"
            installed = service_path.is_file()
            active = False
            if installed:
                try:
                    res = subprocess.run(
                        ["launchctl", "list", self.config.service_name],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    active = res.returncode == 0
                except Exception:
                    active = False
            return {
                "platform": platform_name,
                "installed": installed,
                "service_path": str(service_path) if installed else None,
                "active": active,
            }

        # Linux / systemd
        user_path = Path.home() / ".config" / "systemd" / "user" / f"{self.config.service_name}.service"
        sys_path = Path("/etc/systemd/system") / f"{self.config.service_name}.service"

        service_path = None
        if user_path.is_file():
            service_path = user_path
        elif sys_path.is_file():
            service_path = sys_path

        installed = service_path is not None
        active = False
        if installed:
            try:
                cmd = ["systemctl", "--user", "is-active", self.config.service_name]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                active = res.stdout.strip() == "active"
            except Exception:
                active = False

        return {
            "platform": platform_name,
            "installed": installed,
            "service_path": str(service_path) if installed else None,
            "active": active,
        }

    def uninstall_daemon(self, dry_run: bool = False) -> dict[str, Any]:
        """Unloads and removes the daemon service plist or systemd unit file."""
        status = self.get_daemon_status()
        service_path_str = status.get("service_path")
        if not service_path_str:
            return {"status": "not_installed", "unloaded": False, "removed": False}

        service_path = Path(service_path_str)
        is_darwin = platform.system() == "Darwin"
        unloaded = False

        if dry_run:
            return {
                "status": "dry_run",
                "service_path": str(service_path),
                "unloaded": status.get("active", False),
                "removed": True,
            }

        if is_darwin:
            try:
                subprocess.run(
                    ["launchctl", "unload", str(service_path)],
                    capture_output=True,
                    timeout=5,
                )
                unloaded = True
            except Exception:
                pass
        else:
            try:
                subprocess.run(
                    ["systemctl", "--user", "stop", self.config.service_name],
                    capture_output=True,
                    timeout=5,
                )
                subprocess.run(
                    ["systemctl", "--user", "disable", self.config.service_name],
                    capture_output=True,
                    timeout=5,
                )
                unloaded = True
            except Exception:
                pass

        removed = False
        try:
            if service_path.is_file():
                service_path.unlink()
                removed = True
        except OSError as exc:
            logger.warning("Failed to remove service file %s: %s", service_path, exc)

        return {
            "status": "uninstalled",
            "service_path": str(service_path),
            "unloaded": unloaded,
            "removed": removed,
        }


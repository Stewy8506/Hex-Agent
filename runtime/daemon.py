"""OS daemon service installer for macOS launchd, Linux systemd and Windows Task Scheduler.

Generates validated service descriptors with environment forwarding, provides
dry-run preview safety, and inspects active service lifecycle state. The HEX
scheduler (``schedule run-due``) is a one-shot command run on an interval, so the
descriptors use interval/timer triggers rather than keep-alive/restart loops.
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
from xml.sax.saxutils import escape

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
    # KeepAlive restarts a job immediately whenever it exits. That is only for
    # persistent services; the one-shot interval scheduler must leave it off so
    # StartInterval (not a restart loop) drives execution (B39).
    keep_alive: bool = False
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
        prog_args = [self.config.python_path, "-m", "harness_core.client.cli", *self.config.hex_args]
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
        exec_start = f"{self.config.python_path} -m harness_core.client.cli {args_str}".strip()
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
            # One-shot interval job: the .timer drives scheduling, so there is no
            # Restart=always/RestartSec loop (B39). Restarting a one-shot on a
            # fixed delay would ignore the configured interval.
            "Type=oneshot",
            f"ExecStart={exec_start}",
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

    def generate_systemd_timer(self) -> str:
        """Generates the systemd .timer that drives the one-shot service interval (B39)."""
        return "\n".join([
            "[Unit]",
            f"Description={self.config.description} (timer)",
            "",
            "[Timer]",
            f"Unit={self.config.service_name}.service",
            f"OnBootSec={self.config.interval_seconds}",
            f"OnUnitActiveSec={self.config.interval_seconds}",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ])

    def install_systemd(
        self,
        user: bool = True,
        target_path: Path | str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Installs the systemd .service unit and its interval .timer."""
        if target_path:
            target = Path(target_path)
        elif user:
            target = Path.home() / ".config" / "systemd" / "user" / f"{self.config.service_name}.service"
        else:
            target = Path("/etc/systemd/system") / f"{self.config.service_name}.service"

        content = self.generate_systemd_unit(user=user)
        timer_content = self.generate_systemd_timer()
        timer_target = target.with_suffix(".timer")

        if dry_run:
            return {
                "status": "dry_run",
                "platform": "systemd",
                "user": user,
                "path": str(target),
                "content": content,
                "timer_path": str(timer_target),
                "timer_content": timer_content,
            }

        target.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        target.write_text(content, encoding="utf-8")
        target.chmod(0o644)
        timer_target.write_text(timer_content, encoding="utf-8")
        timer_target.chmod(0o644)
        return {
            "status": "installed",
            "platform": "systemd",
            "user": user,
            "path": str(target),
            "timer_path": str(timer_target),
        }

    def generate_task_scheduler_xml(self) -> str:
        """Generates a Windows Task Scheduler XML descriptor for the one-shot interval job (B39).

        A TimeTrigger with a Repetition/Interval drives ``schedule run-due`` on the
        configured cadence. MultipleInstancesPolicy=IgnoreNew prevents overlapping
        ticks (matching the scheduler's inter-process lock), and ExecutionTimeLimit
        is unlimited so the orchestrator owns its own deadlines. Environment is
        forwarded via the global ``--data-dir`` CLI argument because Task Scheduler
        Exec actions cannot set environment variables directly.
        """
        task_name = escape(self.config.service_name)
        description = escape(self.config.description)
        command = escape(self.config.python_path)
        arg_parts = ["-m", "harness_core.client.cli", "--data-dir", f'"{self.data_dir}"']
        arg_parts.extend(self.config.hex_args)
        arguments = escape(" ".join(arg_parts))
        interval = f"PT{int(self.config.interval_seconds)}S"

        working_dir_xml = ""
        if self.config.working_dir:
            working_dir_xml = f"\n      <WorkingDirectory>{escape(self.config.working_dir)}</WorkingDirectory>"

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
    <Author>HEX</Author>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>{interval}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>{working_dir_xml}
    </Exec>
  </Actions>
</Task>
"""

    def install_windows_task(
        self,
        target_path: Path | str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Registers the one-shot interval job with Windows Task Scheduler via schtasks."""
        task_name = self.config.service_name
        content = self.generate_task_scheduler_xml()

        if dry_run:
            return {
                "status": "dry_run",
                "platform": "schtasks",
                "task_name": task_name,
                "content": content,
            }

        xml_path = (
            Path(target_path)
            if target_path
            else self.log_dir / f"{task_name}.xml"
        )
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(content, encoding="utf-8")

        try:
            res = subprocess.run(
                ["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as exc:
            return {
                "status": "error",
                "platform": "schtasks",
                "task_name": task_name,
                "path": str(xml_path),
                "error": str(exc),
            }

        if res.returncode != 0:
            return {
                "status": "error",
                "platform": "schtasks",
                "task_name": task_name,
                "path": str(xml_path),
                "error": (res.stderr or res.stdout).strip(),
            }

        return {
            "status": "installed",
            "platform": "schtasks",
            "task_name": task_name,
            "path": str(xml_path),
        }

    def get_daemon_status(self) -> dict[str, Any]:
        """Detects host platform and checks if the daemon service descriptor is installed and active."""
        system = platform.system()
        if system == "Windows":
            return self._get_windows_task_status()
        is_darwin = system == "Darwin"
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

    def _get_windows_task_status(self) -> dict[str, Any]:
        """Queries Windows Task Scheduler for the registered HEX interval job."""
        task_name = self.config.service_name
        installed = False
        active = False
        try:
            res = subprocess.run(
                ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            installed = res.returncode == 0
            if installed:
                # A one-shot interval job is "Ready" between ticks and "Running"
                # during a tick; either means it is enabled. "Disabled" means off.
                active = "Disabled" not in res.stdout
        except Exception:
            installed = False
            active = False
        return {
            "platform": "schtasks",
            "installed": installed,
            "service_path": task_name if installed else None,
            "task_name": task_name,
            "active": active,
        }

    def uninstall_daemon(self, dry_run: bool = False) -> dict[str, Any]:
        """Unloads and removes the daemon service descriptor for the host platform."""
        if platform.system() == "Windows":
            return self._uninstall_windows_task(dry_run=dry_run)

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
            timer_path = service_path.with_suffix(".timer")
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
                # The .timer drives the one-shot interval; stop/disable it too so
                # uninstall does not leave a dangling trigger (B39).
                subprocess.run(
                    ["systemctl", "--user", "stop", f"{self.config.service_name}.timer"],
                    capture_output=True,
                    timeout=5,
                )
                subprocess.run(
                    ["systemctl", "--user", "disable", f"{self.config.service_name}.timer"],
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

        if not is_darwin:
            timer_path = service_path.with_suffix(".timer")
            try:
                if timer_path.is_file():
                    timer_path.unlink()
            except OSError as exc:
                logger.warning("Failed to remove timer file %s: %s", timer_path, exc)

        return {
            "status": "uninstalled",
            "service_path": str(service_path),
            "unloaded": unloaded,
            "removed": removed,
        }

    def _uninstall_windows_task(self, dry_run: bool = False) -> dict[str, Any]:
        """Deletes the registered Windows Task Scheduler job and its XML descriptor."""
        task_name = self.config.service_name
        status = self.get_daemon_status()
        if not status.get("installed"):
            return {"status": "not_installed", "unloaded": False, "removed": False}

        if dry_run:
            return {
                "status": "dry_run",
                "platform": "schtasks",
                "task_name": task_name,
                "unloaded": status.get("active", False),
                "removed": True,
            }

        removed = False
        try:
            res = subprocess.run(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            removed = res.returncode == 0
        except Exception as exc:
            logger.warning("Failed to delete scheduled task %s: %s", task_name, exc)

        xml_path = self.log_dir / f"{task_name}.xml"
        try:
            if xml_path.is_file():
                xml_path.unlink()
        except OSError as exc:
            logger.warning("Failed to remove task XML %s: %s", xml_path, exc)

        return {
            "status": "uninstalled",
            "platform": "schtasks",
            "task_name": task_name,
            "unloaded": removed,
            "removed": removed,
        }


"""Comprehensive unit and integration tests for TaskScheduler, CronParser, and DaemonInstaller.

Tests cover:
- 5-field cron parsing (wildcards, steps, ranges, lists, step-ranges, invalid expressions)
- Cron matching and next_run_after evaluation across boundaries
- ScheduledJob serialization and defaults
- TaskScheduler persistence, job registry management, due job calculation
- Due job execution via orchestrator, state transitions, and webhook failure alerts
- DaemonInstaller plist and systemd unit generation, dry-run safety, and status inspection
- CLI commands: 'hex schedule' (add, list, remove, run-due) and 'hex daemon' (install, status)
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cli import main
from harness_core.daemon import DaemonConfig, DaemonInstaller
from harness_core.scheduler import CronParser, ScheduledJob, TaskScheduler
from harness_core.webhooks import WebhookConfig, WebhookNotifier


class TestCronParser(unittest.TestCase):
    """Unit tests for pure Python 5-field CronParser."""

    def test_cron_every_minute(self):
        p = CronParser("* * * * *")
        self.assertTrue(p.is_valid())
        dt = datetime(2026, 11, 2, 14, 35, 10, tzinfo=timezone.utc)
        self.assertTrue(p.matches(dt))
        next_dt = p.next_run_after(dt)
        self.assertEqual(next_dt, datetime(2026, 11, 2, 14, 36, 0, tzinfo=timezone.utc))

    def test_cron_step_minute(self):
        p = CronParser("*/15 * * * *")
        self.assertTrue(p.is_valid())
        # Matches at minute 0, 15, 30, 45
        self.assertTrue(p.matches(datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)))
        self.assertTrue(p.matches(datetime(2026, 11, 2, 10, 30, tzinfo=timezone.utc)))
        self.assertFalse(p.matches(datetime(2026, 11, 2, 10, 10, tzinfo=timezone.utc)))

        base = datetime(2026, 11, 2, 10, 14, 59, tzinfo=timezone.utc)
        nxt = p.next_run_after(base)
        self.assertEqual(nxt.minute, 15)

    def test_cron_specific_time_daily(self):
        p = CronParser("0 2 * * *")
        self.assertTrue(p.is_valid())
        dt_match = datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)
        self.assertTrue(p.matches(dt_match))
        dt_no_match = datetime(2026, 11, 2, 2, 1, tzinfo=timezone.utc)
        self.assertFalse(p.matches(dt_no_match))

        # Next run after 02:00 should be tomorrow at 02:00
        nxt = p.next_run_after(dt_match)
        self.assertEqual(nxt, datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc))

    def test_cron_weekday_range_and_lists(self):
        # Mon-Fri at 09:00
        p = CronParser("0 9 * * 1-5")
        self.assertTrue(p.is_valid())
        # 2026-11-02 is Monday (dow=1)
        mon = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
        self.assertTrue(p.matches(mon))
        # 2026-11-07 is Saturday (dow=6)
        sat = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
        self.assertFalse(p.matches(sat))

        # Next run after Friday 09:00 is Monday 09:00
        fri = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
        nxt = p.next_run_after(fri)
        self.assertEqual(nxt, datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc))

    def test_cron_sunday_zero_and_seven(self):
        # 0 and 7 are both Sunday
        p0 = CronParser("0 12 * * 0")
        p7 = CronParser("0 12 * * 7")
        self.assertTrue(p0.is_valid())
        self.assertTrue(p7.is_valid())

        # 2026-11-08 is a Sunday
        sun = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(p0.matches(sun))
        self.assertTrue(p7.matches(sun))

    def test_cron_step_range_and_complex_lists(self):
        # minute 1-10/2: 1, 3, 5, 7, 9
        p = CronParser("1-10/2 10 1,15 * *")
        self.assertTrue(p.is_valid())
        self.assertTrue(p.matches(datetime(2026, 11, 1, 10, 1, tzinfo=timezone.utc)))
        self.assertTrue(p.matches(datetime(2026, 11, 1, 10, 9, tzinfo=timezone.utc)))
        self.assertFalse(p.matches(datetime(2026, 11, 1, 10, 2, tzinfo=timezone.utc)))
        self.assertFalse(p.matches(datetime(2026, 11, 2, 10, 1, tzinfo=timezone.utc)))

    def test_invalid_cron_expressions(self):
        invalid_expressions = [
            "",
            "   ",
            "* * * *",  # 4 fields
            "* * * * * *",  # 6 fields
            "60 * * * *",  # minute out of range (0-59)
            "* 24 * * *",  # hour out of range (0-23)
            "* * 32 * *",  # day out of range (1-31)
            "* * * 13 *",  # month out of range (1-12)
            "* * * * 8",  # dow out of range (0-7)
            "*/0 * * * *",  # step is zero
            "10-5 * * * *",  # reversed range
            "foo * * * *",  # non-numeric
            "*/-5 * * * *",  # negative step
        ]
        for expr in invalid_expressions:
            p = CronParser(expr)
            self.assertFalse(p.is_valid(), f"Expected '{expr}' to be invalid")
            with self.assertRaises(ValueError):
                p.next_run_after(datetime.now(timezone.utc))


class TestScheduledJob(unittest.TestCase):
    """Unit tests for ScheduledJob serialization and defaults."""

    def test_job_defaults_and_roundtrip(self):
        job = ScheduledJob(
            cron_expr="0 3 * * *",
            goal="Run security audit",
            project_id="cve_monitor",
        )
        self.assertTrue(job.job_id.startswith("job_"))
        self.assertTrue(job.enabled)
        self.assertIsNotNone(job.created_at)

        data = job.to_dict()
        self.assertEqual(data["cron_expr"], "0 3 * * *")
        self.assertEqual(data["goal"], "Run security audit")
        self.assertEqual(data["project_id"], "cve_monitor")

        reconstructed = ScheduledJob.from_dict(data)
        self.assertEqual(reconstructed.job_id, job.job_id)
        self.assertEqual(reconstructed.cron_expr, job.cron_expr)
        self.assertEqual(reconstructed.goal, job.goal)
        self.assertEqual(reconstructed.project_id, job.project_id)
        self.assertEqual(reconstructed.enabled, job.enabled)


class TestTaskScheduler(unittest.TestCase):
    """Integration and unit tests for TaskScheduler."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="hex_test_scheduler_")
        self.storage_path = Path(self.tmp_dir) / "schedules.json"
        self.scheduler = TaskScheduler(storage_path=self.storage_path)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_add_and_list_jobs(self):
        self.assertEqual(len(self.scheduler.list_jobs()), 0)

        job1 = self.scheduler.add_job("0 3 * * *", "Nightly cleanup", project_id="p1")
        self.assertTrue(job1.job_id.startswith("job_"))
        self.assertIsNotNone(job1.next_run)

        job2 = self.scheduler.add_job("*/10 * * * *", "Poll webhook", project_id="p2")
        self.assertTrue(job2.job_id.startswith("job_"))

        jobs = self.scheduler.list_jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual([j.goal for j in jobs], ["Nightly cleanup", "Poll webhook"])

        # Verify atomic persistence to disk
        self.assertTrue(self.storage_path.is_file())
        with open(self.storage_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
            self.assertEqual(len(disk_data), 2)

    def test_add_invalid_job_raises(self):
        with self.assertRaises(ValueError):
            self.scheduler.add_job("invalid_cron", "Should fail")

    def test_remove_job(self):
        job1 = self.scheduler.add_job("0 0 * * *", "Job 1")
        job2 = self.scheduler.add_job("0 1 * * *", "Job 2")
        self.assertEqual(len(self.scheduler.list_jobs()), 2)

        self.assertTrue(self.scheduler.remove_job(job1.job_id))
        self.assertEqual(len(self.scheduler.list_jobs()), 1)
        self.assertEqual(self.scheduler.list_jobs()[0].job_id, job2.job_id)

        # Removing non-existent job returns False
        self.assertFalse(self.scheduler.remove_job("job_nonexistent"))

    def test_get_due_jobs(self):
        # Job due in past
        j1 = ScheduledJob(
            cron_expr="* * * * *",
            goal="Due job",
            next_run="2026-11-01T00:00:00+00:00",
            enabled=True,
        )
        # Job due in future
        j2 = ScheduledJob(
            cron_expr="* * * * *",
            goal="Future job",
            next_run="2026-11-05T00:00:00+00:00",
            enabled=True,
        )
        # Job disabled
        j3 = ScheduledJob(
            cron_expr="* * * * *",
            goal="Disabled job",
            next_run="2026-11-01T00:00:00+00:00",
            enabled=False,
        )
        self.scheduler.save_jobs([j1, j2, j3])

        eval_time = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
        due = self.scheduler.get_due_jobs(now=eval_time)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].goal, "Due job")

    def test_execute_due_jobs_happy_path(self):
        job = ScheduledJob(
            cron_expr="*/5 * * * *",
            goal="Execute healthcheck",
            next_run="2026-11-01T10:00:00+00:00",
        )
        self.scheduler.save_jobs([job])

        mock_orch = MagicMock()
        mock_orch.submit_and_run.return_value = {
            "task_id": "tsk_12345",
            "status": "COMPLETED",
        }

        mock_notifier = MagicMock(spec=WebhookNotifier)

        now = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
        results = self.scheduler.execute_due_jobs(mock_orch, notifier=mock_notifier, now=now)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["task_result"]["status"], "COMPLETED")
        self.assertIsNone(results[0]["error"])

        # Orchestrator called once
        mock_orch.submit_and_run.assert_called_once()
        # Notifier alert NOT dispatched on success
        mock_notifier.send_alert.assert_not_called()

        # Job state updated on disk
        updated_jobs = self.scheduler.list_jobs()
        self.assertEqual(len(updated_jobs), 1)
        self.assertIsNotNone(updated_jobs[0].last_run)
        # next_run advanced
        next_dt = datetime.fromisoformat(updated_jobs[0].next_run)
        self.assertGreater(next_dt, now)

    def test_execute_due_jobs_failure_sends_alert(self):
        job = ScheduledJob(
            cron_expr="0 1 * * *",
            goal="Fail task goal",
            next_run="2026-11-01T01:00:00+00:00",
        )
        self.scheduler.save_jobs([job])

        mock_orch = MagicMock()
        mock_orch.submit_and_run.return_value = {
            "task_id": "tsk_failed",
            "status": "FAILED",
            "terminal_reason": "Out of memory in sandbox",
        }

        mock_notifier = MagicMock(spec=WebhookNotifier)

        now = datetime(2026, 11, 1, 1, 0, 0, tzinfo=timezone.utc)
        results = self.scheduler.execute_due_jobs(mock_orch, notifier=mock_notifier, now=now)

        self.assertEqual(len(results), 1)
        self.assertIn("Out of memory", results[0]["error"])

        # Alert sent via notifier
        mock_notifier.send_alert.assert_called_once()
        alert_args = mock_notifier.send_alert.call_args[1]
        self.assertIn(job.job_id, alert_args["title"])
        self.assertIn("Out of memory", alert_args["message"])

        # Next run still advanced despite failure to prevent retry loops
        updated_jobs = self.scheduler.list_jobs()
        self.assertGreater(datetime.fromisoformat(updated_jobs[0].next_run), now)


class TestDaemonInstaller(unittest.TestCase):
    """Unit tests for macOS LaunchAgent plist and Linux systemd unit generation."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="hex_test_daemon_")
        self.cfg = DaemonConfig(
            service_name="ai.hex.testdaemon",
            description="Test HEX Daemon Service",
            python_path="/usr/bin/python3",
            hex_args=["schedule", "run-due"],
            data_dir=os.path.join(self.tmp_dir, "data"),
            config_dir=os.path.join(self.tmp_dir, "config"),
            interval_seconds=120,
            log_dir=os.path.join(self.tmp_dir, "logs"),
        )
        self.installer = DaemonInstaller(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_generate_launchd_plist(self):
        plist = self.installer.generate_launchd_plist()
        self.assertIn("<!DOCTYPE plist PUBLIC", plist)
        self.assertIn("<key>Label</key>", plist)
        self.assertIn("<string>ai.hex.testdaemon</string>", plist)
        self.assertIn("<string>/usr/bin/python3</string>", plist)
        self.assertIn("<string>run-due</string>", plist)
        self.assertIn("<key>StartInterval</key>", plist)
        self.assertIn("<integer>120</integer>", plist)
        self.assertIn("<key>HEX_DATA_DIR</key>", plist)
        self.assertIn("<key>HEX_CONFIG</key>", plist)
        self.assertIn("daemon.stdout.log", plist)
        self.assertIn("daemon.stderr.log", plist)

    def test_install_launchd_dry_run(self):
        res = self.installer.install_launchd(dry_run=True)
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(res["platform"], "launchd")
        self.assertIn("ai.hex.testdaemon", res["content"])

    def test_install_launchd_real_write(self):
        target = Path(self.tmp_dir) / "ai.hex.testdaemon.plist"
        res = self.installer.install_launchd(target_path=target, dry_run=False)
        self.assertEqual(res["status"], "installed")
        self.assertTrue(target.is_file())
        content = target.read_text(encoding="utf-8")
        self.assertIn("ai.hex.testdaemon", content)

    def test_generate_systemd_unit_user_and_system(self):
        unit_user = self.installer.generate_systemd_unit(user=True)
        self.assertIn("[Unit]", unit_user)
        self.assertIn("Description=Test HEX Daemon Service", unit_user)
        self.assertIn("ExecStart=/usr/bin/python3 -m harness_core.cli schedule run-due", unit_user)
        self.assertIn('Environment="HEX_DATA_DIR=', unit_user)
        self.assertIn("WantedBy=default.target", unit_user)

        unit_sys = self.installer.generate_systemd_unit(user=False)
        self.assertIn("WantedBy=multi-user.target", unit_sys)

    def test_install_systemd_dry_run(self):
        res = self.installer.install_systemd(user=True, dry_run=True)
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(res["platform"], "systemd")
        self.assertIn("ExecStart=", res["content"])

    def test_install_systemd_real_write(self):
        target = Path(self.tmp_dir) / "ai.hex.testdaemon.service"
        res = self.installer.install_systemd(user=True, target_path=target, dry_run=False)
        self.assertEqual(res["status"], "installed")
        self.assertTrue(target.is_file())
        content = target.read_text(encoding="utf-8")
        self.assertIn("Description=Test HEX Daemon Service", content)

    def test_get_daemon_status(self):
        status = self.installer.get_daemon_status()
        self.assertIn("platform", status)
        self.assertIn("installed", status)
        self.assertIn("active", status)


class TestCliScheduleAndDaemon(unittest.TestCase):
    """Integration tests for CLI 'hex schedule' and 'hex daemon' subcommands."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="hex_test_cli_")
        self.storage_path = Path(self.tmp_dir) / "schedules.json"
        self.data_dir = Path(self.tmp_dir) / "data"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_cli_schedule_add_and_list(self):
        # 1. Add job
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = main([
                "--data-dir", str(self.data_dir),
                "schedule",
                "add",
                "0 2 * * *",
                "--goal", "Nightly CVE check",
                "--project", "sec_ops",
                "--storage-path", str(self.storage_path),
            ])
        self.assertEqual(code, 0)
        resp = json.loads(out.getvalue())
        self.assertEqual(resp["status"], "added")
        self.assertEqual(resp["goal"], "Nightly CVE check")
        job_id = resp["job_id"]

        # 2. List jobs
        out_list = io.StringIO()
        with patch("sys.stdout", out_list):
            code_list = main([
                "--data-dir", str(self.data_dir),
                "schedule",
                "list",
                "--storage-path", str(self.storage_path),
            ])
        self.assertEqual(code_list, 0)
        jobs = json.loads(out_list.getvalue())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], job_id)

        # 3. Remove job
        out_rm = io.StringIO()
        with patch("sys.stdout", out_rm):
            code_rm = main([
                "--data-dir", str(self.data_dir),
                "schedule",
                "remove",
                job_id,
                "--storage-path", str(self.storage_path),
            ])
        self.assertEqual(code_rm, 0)
        rm_resp = json.loads(out_rm.getvalue())
        self.assertEqual(rm_resp["status"], "removed")

    def test_cli_schedule_run_due(self):
        # Add a job that is already due
        scheduler = TaskScheduler(storage_path=self.storage_path)
        job = ScheduledJob(
            cron_expr="* * * * *",
            goal="Test run due goal",
            next_run="2026-11-01T00:00:00+00:00",
        )
        scheduler.save_jobs([job])

        out = io.StringIO()
        with patch("sys.stdout", out), patch("harness_core.cli.HarnessOrchestrator") as mock_orch_cls:
            mock_orch = MagicMock()
            mock_orch.submit_and_run.return_value = {
                "task_id": "tsk_cli_test",
                "status": "COMPLETED",
            }
            mock_orch_cls.return_value = mock_orch

            code = main([
                "--data-dir", str(self.data_dir),
                "schedule",
                "run-due",
                "--once",
                "--storage-path", str(self.storage_path),
            ])

        self.assertEqual(code, 0)
        resp = json.loads(out.getvalue())
        self.assertEqual(resp["status"], "completed")
        self.assertEqual(resp["executed_count"], 1)

    def test_cli_daemon_install_dry_run(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = main([
                "--data-dir", str(self.data_dir),
                "daemon",
                "install",
                "--dry-run",
            ])
        self.assertEqual(code, 0)
        resp = json.loads(out.getvalue())
        self.assertEqual(resp["status"], "dry_run")
        self.assertIn("content", resp)

    def test_cli_daemon_status(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = main([
                "--data-dir", str(self.data_dir),
                "daemon",
                "status",
            ])
        self.assertEqual(code, 0)
        resp = json.loads(out.getvalue())
        self.assertIn("platform", resp)
        self.assertIn("installed", resp)


if __name__ == "__main__":
    unittest.main()

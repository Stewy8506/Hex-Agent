"""Native background cron scheduler and persistent task scheduling engine.

Provides zero-dependency 5-field cron parsing, atomic JSON schedule persistence,
due job execution via HarnessOrchestrator, and automated webhook alert dispatch.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import TaskSubmitRequest
from .orchestrator import HarnessOrchestrator
from .webhooks import WebhookNotifier

logger = logging.getLogger(__name__)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_cron_field(
    field_str: str,
    min_val: int,
    max_val: int,
    is_dow: bool = False,
) -> set[int]:
    """Parses a single cron field with support for wildcards, steps, ranges, and lists."""
    values: set[int] = set()
    parts = field_str.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            raise ValueError(f"Empty element in cron field '{field_str}'")
        if "/" in part:
            sub_parts = part.split("/")
            if len(sub_parts) != 2:
                raise ValueError(f"Invalid step format: '{part}'")
            range_part, step_str = sub_parts
            if not step_str.isdigit() or int(step_str) <= 0:
                raise ValueError(f"Step value must be a positive integer: '{step_str}'")
            step = int(step_str)

            if range_part == "*":
                start, end = min_val, max_val
            elif "-" in range_part:
                r_parts = range_part.split("-")
                if len(r_parts) != 2 or not r_parts[0].isdigit() or not r_parts[1].isdigit():
                    raise ValueError(f"Invalid range in step: '{range_part}'")
                start, end = int(r_parts[0]), int(r_parts[1])
            elif range_part.isdigit():
                start = int(range_part)
                end = max_val
            else:
                raise ValueError(f"Invalid range in step: '{range_part}'")

            if start < min_val or end > max_val or start > end:
                raise ValueError(f"Range [{start}, {end}] out of bounds [{min_val}, {max_val}]")
            for v in range(start, end + 1, step):
                values.add(v)
        elif "-" in part:
            r_parts = part.split("-")
            if len(r_parts) != 2 or not r_parts[0].isdigit() or not r_parts[1].isdigit():
                raise ValueError(f"Invalid range: '{part}'")
            start, end = int(r_parts[0]), int(r_parts[1])
            if start < min_val or end > max_val or start > end:
                raise ValueError(f"Range [{start}, {end}] out of bounds [{min_val}, {max_val}]")
            for v in range(start, end + 1):
                values.add(v)
        elif part == "*":
            for v in range(min_val, max_val + 1):
                values.add(v)
        elif part.isdigit():
            val = int(part)
            if val < min_val or val > max_val:
                raise ValueError(f"Value {val} out of bounds [{min_val}, {max_val}]")
            values.add(val)
        else:
            raise ValueError(f"Unrecognized cron part: '{part}'")

    if is_dow:
        # Standard cron: 0 and 7 both represent Sunday
        if 7 in values:
            values.remove(7)
            values.add(0)

    if not values:
        raise ValueError(f"No valid values produced for field '{field_str}'")
    return values


class CronParser:
    """Zero-dependency parser for standard 5-field cron expressions.

    Fields: minute (0-59), hour (0-23), day_of_month (1-31), month (1-12), day_of_week (0-7, 0 & 7=Sun).
    """

    def __init__(self, cron_expr: str) -> None:
        self.cron_expr = cron_expr.strip()
        self._is_valid: bool = False
        self._error: str | None = None
        self._minutes: set[int] = set()
        self._hours: set[int] = set()
        self._days: set[int] = set()
        self._months: set[int] = set()
        self._dows: set[int] = set()
        self._parse()

    def _parse(self) -> None:
        parts = self.cron_expr.split()
        if len(parts) != 5:
            self._is_valid = False
            self._error = f"Expected exactly 5 fields, got {len(parts)} in '{self.cron_expr}'"
            return

        try:
            self._minutes = _parse_cron_field(parts[0], 0, 59)
            self._hours = _parse_cron_field(parts[1], 0, 23)
            self._days = _parse_cron_field(parts[2], 1, 31)
            self._months = _parse_cron_field(parts[3], 1, 12)
            self._dows = _parse_cron_field(parts[4], 0, 7, is_dow=True)
            self._is_valid = True
        except Exception as exc:
            self._is_valid = False
            self._error = str(exc)

    def is_valid(self) -> bool:
        """Returns True if the expression is syntactically valid and fields are within bounds."""
        return self._is_valid

    def matches(self, dt: datetime) -> bool:
        """Checks if a given datetime matches all 5 cron fields."""
        if not self._is_valid:
            return False
        # dt.weekday(): 0=Mon, 6=Sun -> cron dow: (weekday + 1) % 7 (0=Sun, 1=Mon, ..., 6=Sat)
        cron_dow = (dt.weekday() + 1) % 7
        return (
            dt.minute in self._minutes
            and dt.hour in self._hours
            and dt.day in self._days
            and dt.month in self._months
            and cron_dow in self._dows
        )

    def next_run_after(self, base_dt: datetime) -> datetime:
        """Computes the earliest datetime strictly after `base_dt` that matches the cron expression."""
        if not self._is_valid:
            raise ValueError(f"Cannot compute next_run for invalid cron: '{self.cron_expr}' ({self._error})")

        tz = base_dt.tzinfo or timezone.utc
        curr = base_dt.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)

        max_steps = 100000
        for _ in range(max_steps):
            if curr.month not in self._months:
                if curr.month == 12:
                    curr = curr.replace(year=curr.year + 1, month=1, day=1, hour=0, minute=0)
                else:
                    curr = curr.replace(month=curr.month + 1, day=1, hour=0, minute=0)
                continue

            cron_dow = (curr.weekday() + 1) % 7
            if curr.day not in self._days or cron_dow not in self._dows:
                curr = (curr + timedelta(days=1)).replace(hour=0, minute=0)
                continue

            if curr.hour not in self._hours:
                next_hours = [h for h in self._hours if h > curr.hour]
                if next_hours:
                    curr = curr.replace(hour=min(next_hours), minute=0)
                else:
                    curr = (curr + timedelta(days=1)).replace(hour=0, minute=0)
                continue

            if curr.minute not in self._minutes:
                next_mins = [m for m in self._minutes if m > curr.minute]
                if next_mins:
                    curr = curr.replace(minute=min(next_mins))
                else:
                    curr = (curr + timedelta(hours=1)).replace(minute=0)
                continue

            return curr

        raise ValueError(f"No matching execution time found within search limit for cron '{self.cron_expr}'")


@dataclass
class ScheduledJob:
    cron_expr: str
    goal: str
    job_id: str = field(default_factory=lambda: f"job_{uuid.uuid4().hex[:8]}")
    project_id: str = "default"
    enabled: bool = True
    last_run: str | None = None
    next_run: str | None = None
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "cron_expr": self.cron_expr,
            "goal": self.goal,
            "project_id": self.project_id,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledJob:
        return cls(
            job_id=data.get("job_id") or f"job_{uuid.uuid4().hex[:8]}",
            cron_expr=data.get("cron_expr", "* * * * *"),
            goal=data.get("goal", ""),
            project_id=data.get("project_id", "default"),
            enabled=data.get("enabled", True),
            last_run=data.get("last_run"),
            next_run=data.get("next_run"),
            created_at=data.get("created_at") or utcnow_iso(),
        )


class TaskScheduler:
    """Persistent scheduler managing scheduled jobs, due task execution, and alerts."""

    def __init__(self, storage_path: Path | str | None = None) -> None:
        if storage_path is not None:
            self.storage_path = Path(storage_path)
        else:
            env_file = os.getenv("HEX_SCHEDULES_FILE") or os.getenv("HARNESS_SCHEDULES_FILE")
            if env_file:
                self.storage_path = Path(env_file)
            else:
                data_dir = os.getenv("HEX_DATA_DIR") or os.getenv("HARNESS_DATA_DIR")
                if data_dir:
                    self.storage_path = Path(data_dir) / "schedules.json"
                else:
                    self.storage_path = Path.home() / ".hex" / "schedules.json"

    def load_jobs(self) -> list[ScheduledJob]:
        """Safely loads scheduled jobs from the JSON storage file."""
        if not self.storage_path.is_file():
            return []
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [ScheduledJob.from_dict(item) for item in data if isinstance(item, dict)]
                elif isinstance(data, dict) and "jobs" in data:
                    return [ScheduledJob.from_dict(item) for item in data["jobs"] if isinstance(item, dict)]
                return []
        except Exception as exc:
            logger.warning("Failed to load scheduled jobs from %s: %s", self.storage_path, exc)
            return []

    def save_jobs(self, jobs: list[ScheduledJob] | None = None) -> None:
        """Persists jobs atomically to disk using os.replace to prevent read corruption."""
        if jobs is None:
            jobs = self.load_jobs()
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.storage_path.with_name(f"{self.storage_path.name}.tmp.{uuid.uuid4().hex[:6]}")
        payload = [j.to_dict() for j in jobs]
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, self.storage_path)

    def add_job(self, cron_expr: str, goal: str, project_id: str = "default") -> ScheduledJob:
        """Validates cron, calculates initial next_run, and persists a new ScheduledJob."""
        parser = CronParser(cron_expr)
        if not parser.is_valid():
            raise ValueError(f"Invalid cron expression: '{cron_expr}' ({parser._error})")

        now = datetime.now(timezone.utc)
        next_run = parser.next_run_after(now).isoformat()

        job = ScheduledJob(
            cron_expr=cron_expr,
            goal=goal,
            project_id=project_id,
            enabled=True,
            next_run=next_run,
        )
        jobs = self.load_jobs()
        jobs.append(job)
        self.save_jobs(jobs)
        return job

    def remove_job(self, job_id: str) -> bool:
        """Removes a scheduled job by job_id. Returns True if removed, False if not found."""
        jobs = self.load_jobs()
        initial_count = len(jobs)
        jobs = [j for j in jobs if j.job_id != job_id]
        if len(jobs) < initial_count:
            self.save_jobs(jobs)
            return True
        return False

    def list_jobs(self, include_disabled: bool = True) -> list[ScheduledJob]:
        """Lists all registered scheduled jobs."""
        jobs = self.load_jobs()
        if include_disabled:
            return jobs
        return [j for j in jobs if j.enabled]

    def get_due_jobs(self, now: datetime | None = None) -> list[ScheduledJob]:
        """Returns all enabled jobs whose next_run timestamp is due (<= now)."""
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        jobs = self.load_jobs()
        due: list[ScheduledJob] = []
        for j in jobs:
            if not j.enabled:
                continue
            if not j.next_run:
                due.append(j)
                continue
            try:
                next_dt = datetime.fromisoformat(j.next_run)
                if next_dt.tzinfo is None:
                    next_dt = next_dt.replace(tzinfo=timezone.utc)
                if next_dt <= current_time:
                    due.append(j)
            except Exception:
                due.append(j)
        return due

    def execute_due_jobs(
        self,
        orchestrator: HarnessOrchestrator,
        notifier: WebhookNotifier | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Submits due jobs to the orchestrator, updates execution timestamps, and sends alerts."""
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        all_jobs = self.load_jobs()
        job_map = {j.job_id: j for j in all_jobs}
        due_jobs = self.get_due_jobs(current_time)
        results: list[dict[str, Any]] = []

        for due_job in due_jobs:
            job = job_map.get(due_job.job_id, due_job)
            exec_start = datetime.now(timezone.utc)
            req = TaskSubmitRequest(
                goal=job.goal,
                project_id=job.project_id,
            )
            task_result: dict[str, Any] = {}
            error_msg: str | None = None

            try:
                task_result = orchestrator.submit_and_run(req, auto_execute=True)
                status = task_result.get("status")
                if status in ("FAILED", "REJECTED", "NEEDS_ATTENTION"):
                    error_msg = task_result.get("terminal_reason") or f"Task execution ended with status {status}"
            except Exception as exc:
                error_msg = str(exc)
                task_result = {
                    "status": "ERROR",
                    "error": error_msg,
                    "job_id": job.job_id,
                }

            # Advance next_run to prevent infinite retry loops
            job.last_run = exec_start.isoformat()
            try:
                parser = CronParser(job.cron_expr)
                job.next_run = parser.next_run_after(exec_start).isoformat()
            except Exception as exc:
                logger.warning("Failed to compute next_run for job %s: %s", job.job_id, exc)
                job.next_run = (exec_start + timedelta(days=1)).isoformat()

            # Dispatch failure alert if needed
            if error_msg and notifier is not None:
                try:
                    if hasattr(notifier, "send_alert"):
                        notifier.send_alert(
                            title=f"Scheduled Job Failed: {job.job_id}",
                            message=f"Goal: {job.goal}\nError: {error_msg}",
                            details={"job_id": job.job_id, "project_id": job.project_id, "error": error_msg},
                        )
                except Exception as notify_exc:
                    logger.warning("Failed to send webhook alert for job %s: %s", job.job_id, notify_exc)

            results.append({
                "job_id": job.job_id,
                "goal": job.goal,
                "last_run": job.last_run,
                "next_run": job.next_run,
                "task_result": task_result,
                "error": error_msg,
            })

        # Save updated execution states
        self.save_jobs(list(job_map.values()))
        return results

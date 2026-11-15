"""Safe, Scoped Automatic TTL Retention & Reference-Counted Garbage Collection for HEX.

PRD Maintenance & Storage Optimization Engine:
1. Workspace & CoW Snapshot Cleanup (active_workspaces/, cow_snapshots/):
   - Queries ledger.db for tasks with status in ('SUCCEEDED', 'COMPLETED', 'FAILED', 'ABANDONED')
     whose updated_at is older than workspace_ttl (default: 24h) and pinned != 1.
   - Enforces orphan directory race guard: unindexed directories must have mtime older than
     orphan_guard_seconds (default 1 hour) before removal.
   - NEVER deletes directories belonging to active tasks or pinned tasks.
2. Reference-Counted CAS Garbage Collection (cas/, cas_artifacts/):
   - Mark phase: builds set of all SHA-256 hashes referenced by active tasks, pinned tasks,
     or tasks updated within cas_ttl (default: 7d).
   - Sweep phase: unlinks unreferenced blobs older than cas_ttl.
3. Ledger Hygiene:
   - Preserves task metadata rows indefinitely for auditability.
   - Executes non-blocking PRAGMA incremental_vacuum; during background sweeps, and
     optional full VACUUM during manual `hex gc` when no active leases exist.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_loader import load_retention_config

logger = logging.getLogger("harness_core.gc")

# ANSI color codes for CLI formatting
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_DIM = "\033[90m"
ANSI_BOLD = "\033[1m"
ANSI_RESET = "\033[0m"


def parse_ttl_seconds(value: Any, default_seconds: int = 86400) -> int:
    """Parse duration strings like '24h', '7d', '30m', '3600s', or integer seconds."""
    if value is None:
        return default_seconds
    if isinstance(value, (int, float)):
        return max(60, int(value))

    val_str = str(value).strip().lower()
    if not val_str:
        return default_seconds

    match = re.match(r"^(\d+(?:\.\d+)?)\s*([smhdw]?)$", val_str)
    if not match:
        try:
            return max(60, int(float(val_str)))
        except ValueError:
            return default_seconds

    num, unit = float(match.group(1)), match.group(2)
    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }
    secs = int(num * multipliers.get(unit, 1 if not unit else 86400))
    return max(60, secs)


def format_bytes(num_bytes: int) -> str:
    """Format byte count into human-readable string (KB, MB, GB)."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


def _get_dir_size_bytes(dir_path: Path) -> int:
    """Calculate total byte size of a directory tree safely."""
    total = 0
    try:
        for entry in os.scandir(dir_path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += _get_dir_size_bytes(Path(entry.path))
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        pass
    return total


@dataclass
class GCReport:
    """Structured report produced by a garbage collection sweep."""

    workspaces_deleted: int = 0
    workspaces_bytes: int = 0
    snapshots_deleted: int = 0
    snapshots_bytes: int = 0
    cas_blobs_deleted: int = 0
    cas_bytes: int = 0
    dry_run: bool = False
    vacuumed: bool = False
    vacuum_mode: str = "none"
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def total_bytes_reclaimed(self) -> int:
        return self.workspaces_bytes + self.snapshots_bytes + self.cas_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "total_bytes_reclaimed": self.total_bytes_reclaimed,
            "total_reclaimed_human": format_bytes(self.total_bytes_reclaimed),
            "workspaces_deleted": self.workspaces_deleted,
            "workspaces_bytes": self.workspaces_bytes,
            "snapshots_deleted": self.snapshots_deleted,
            "snapshots_bytes": self.snapshots_bytes,
            "cas_blobs_deleted": self.cas_blobs_deleted,
            "cas_bytes": self.cas_bytes,
            "vacuumed": self.vacuumed,
            "vacuum_mode": self.vacuum_mode,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
            "details": self.details,
        }

    def format_summary(self, color: bool = True) -> str:
        c_cyan = ANSI_CYAN if color else ""
        c_green = ANSI_GREEN if color else ""
        c_yellow = ANSI_YELLOW if color else ""
        c_dim = ANSI_DIM if color else ""
        c_bold = ANSI_BOLD if color else ""
        c_reset = ANSI_RESET if color else ""

        mode_str = f"{c_yellow}[DRY RUN]{c_reset} " if self.dry_run else ""
        status_word = "Would Reclaim" if self.dry_run else "Reclaimed"

        lines = [
            f"{c_bold}{c_cyan}HEX Storage Retention & Garbage Collection Report{c_reset}",
            f"{c_dim}──────────────────────────────────────────────────────────{c_reset}",
            f"  Status:             {mode_str}{status_word} {c_green}{c_bold}{format_bytes(self.total_bytes_reclaimed)}{c_reset}",
            f"  Workspaces Pruned:  {self.workspaces_deleted} ({format_bytes(self.workspaces_bytes)})",
            f"  Snapshots Pruned:   {self.snapshots_deleted} ({format_bytes(self.snapshots_bytes)})",
            f"  CAS Blobs Pruned:   {self.cas_blobs_deleted} ({format_bytes(self.cas_bytes)})",
            f"  Ledger Vacuum:      {self.vacuum_mode.upper() if self.vacuumed else 'SKIPPED'}",
            f"  Sweep Duration:     {self.duration_ms} ms",
        ]
        if self.errors:
            lines.append(f"  Errors Encountered: {len(self.errors)}")
            for err in self.errors[:3]:
                lines.append(f"    {c_yellow}• {err}{c_reset}")
        lines.append(f"{c_dim}──────────────────────────────────────────────────────────{c_reset}")
        return "\n".join(lines)


class GarbageCollector:
    """Manages TTL retention and reference-counted garbage collection for .hex/data."""

    def __init__(
        self,
        data_dir: Path | str,
        ledger: Any | None = None,
        config: dict[str, Any] | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.ledger = ledger
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None

        # Load configuration (supports .hex/config.toml and config.yaml)
        ret_cfg = config or load_retention_config(workspace_root=self.workspace_root)
        self.workspace_ttl_seconds = parse_ttl_seconds(ret_cfg.get("workspace_ttl", "24h"), default_seconds=86400)
        self.cas_ttl_seconds = parse_ttl_seconds(ret_cfg.get("cas_ttl", "7d"), default_seconds=7 * 86400)
        self.auto_vacuum = bool(ret_cfg.get("auto_vacuum", True))

        # Orphan directory race guard (default 1 hour or workspace_ttl, whichever is smaller)
        self.orphan_guard_seconds = min(3600, self.workspace_ttl_seconds)

        # Standard paths
        self.workspaces_root = self.data_dir / "active_workspaces"
        self.snapshots_root = self.data_dir / "cow_snapshots"
        self.cas_root = self.data_dir / "cas"
        self.cas_artifacts_root = self.data_dir / "cas_artifacts"

    def run_sweep(
        self,
        *,
        dry_run: bool = False,
        full_vacuum: bool = False,
        workspace_ttl: str | int | None = None,
        cas_ttl: str | int | None = None,
        skip_vacuum: bool = False,
    ) -> GCReport:
        """Execute complete reference-counted and TTL retention garbage collection sweep."""
        t0 = time.time()
        report = GCReport(dry_run=dry_run)

        ws_ttl = parse_ttl_seconds(workspace_ttl, self.workspace_ttl_seconds) if workspace_ttl else self.workspace_ttl_seconds
        c_ttl = parse_ttl_seconds(cas_ttl, self.cas_ttl_seconds) if cas_ttl else self.cas_ttl_seconds

        now = time.time()
        ws_cutoff_dt = datetime.datetime.fromtimestamp(now - ws_ttl, tz=datetime.timezone.utc)
        ws_cutoff_iso = ws_cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        cas_cutoff_dt = datetime.datetime.fromtimestamp(now - c_ttl, tz=datetime.timezone.utc)
        cas_cutoff_iso = cas_cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Sweep active_workspaces and cow_snapshots
        self._sweep_workspaces_and_snapshots(report, ws_cutoff_iso, ws_ttl, dry_run=dry_run)

        # 2. Sweep CAS and CAS artifacts (Reference-counted mark-and-sweep)
        self._sweep_cas(report, cas_cutoff_iso, c_ttl, dry_run=dry_run)

        # 3. Ledger optimization
        if not dry_run and not skip_vacuum and self.auto_vacuum and self.ledger is not None:
            self._optimize_ledger(report, full_vacuum=full_vacuum)

        report.duration_ms = int((time.time() - t0) * 1000)
        return report

    def _sweep_workspaces_and_snapshots(
        self,
        report: GCReport,
        cutoff_iso: str,
        ttl_seconds: int,
        dry_run: bool = False,
    ) -> None:
        """Prune expired snapshots and workspace clones for completed, failed, or unpinned tasks."""
        expired_tasks: set[str] = set()
        pinned_or_active_tasks: set[str] = set()

        if self.ledger is not None:
            try:
                # 1. Query expired terminal tasks
                terminal_unpinned = self.ledger.get_terminal_unpinned_tasks_older_than(cutoff_iso)
                expired_tasks = {t["task_id"] for t in terminal_unpinned if t.get("task_id")}

                # 2. Query pinned or active tasks (explicitly protected)
                if hasattr(self.ledger, "conn") and self.ledger.conn is not None:
                    with self.ledger._lock:
                        prot_rows = self.ledger.conn.execute(
                            """
                            SELECT task_id FROM tasks
                            WHERE pinned = 1
                               OR status NOT IN ('SUCCEEDED', 'COMPLETED', 'FAILED', 'ABANDONED')
                            """
                        ).fetchall()
                        pinned_or_active_tasks = {r["task_id"] for r in prot_rows if r["task_id"]}
            except Exception as exc:
                logger.warning("Error querying ledger during workspace GC: %s", exc)
                report.errors.append(f"Ledger query error: {exc}")

        now = time.time()

        # Helper to extract task_id from directory name
        def _extract_task_id(dir_name: str) -> str | None:
            # Matches ws_tsk_xxx_1, spec_tsk_xxx_branch, snap_base_tsk_xxx_hash, snap_tsk_xxx_step
            m = re.search(r"(tsk_[0-9a-fA-F]+)", dir_name)
            return m.group(1) if m else None

        # Sweep active_workspaces/
        if self.workspaces_root.is_dir():
            for entry in list(self.workspaces_root.iterdir()):
                if not entry.is_dir():
                    continue
                tid = _extract_task_id(entry.name)

                # Never delete pinned or active task workspaces
                if tid and tid in pinned_or_active_tasks:
                    continue

                mtime = entry.stat().st_mtime
                age_seconds = now - mtime

                can_delete = False
                if tid and tid in expired_tasks:
                    can_delete = True
                elif tid is None or (tid not in pinned_or_active_tasks and tid not in expired_tasks):
                    # Orphan directory race guard: must be older than orphan_guard_seconds
                    if age_seconds >= self.orphan_guard_seconds:
                        can_delete = True

                if can_delete:
                    sz = _get_dir_size_bytes(entry)
                    report.workspaces_deleted += 1
                    report.workspaces_bytes += sz
                    if not dry_run:
                        try:
                            shutil.rmtree(entry, ignore_errors=True)
                        except Exception as exc:
                            report.errors.append(f"Failed to remove {entry.name}: {exc}")

        # Sweep cow_snapshots/
        if self.snapshots_root.is_dir():
            for entry in list(self.snapshots_root.iterdir()):
                if not entry.is_dir():
                    continue
                tid = _extract_task_id(entry.name)

                # Never delete pinned or active task snapshots
                if tid and tid in pinned_or_active_tasks:
                    continue

                mtime = entry.stat().st_mtime
                age_seconds = now - mtime

                can_delete = False
                if tid and tid in expired_tasks:
                    can_delete = True
                elif tid is None or (tid not in pinned_or_active_tasks and tid not in expired_tasks):
                    # Orphan directory race guard
                    if age_seconds >= self.orphan_guard_seconds:
                        can_delete = True

                if can_delete:
                    sz = _get_dir_size_bytes(entry)
                    report.snapshots_deleted += 1
                    report.snapshots_bytes += sz
                    if not dry_run:
                        try:
                            shutil.rmtree(entry, ignore_errors=True)
                        except Exception as exc:
                            report.errors.append(f"Failed to remove snapshot {entry.name}: {exc}")

    def _sweep_cas(
        self,
        report: GCReport,
        cutoff_iso: str,
        cas_ttl_seconds: int,
        dry_run: bool = False,
    ) -> None:
        """Reference-counted mark-and-sweep for cas/ and cas_artifacts/."""
        # MARK PHASE: Collect active and recent hashes
        marked_hashes: set[str] = set()
        if self.ledger is not None:
            try:
                marked_hashes = self.ledger.get_active_or_pinned_cas_hashes(cutoff_iso)
            except Exception as exc:
                logger.warning("Error collecting CAS active hashes: %s", exc)
                report.errors.append(f"CAS mark error: {exc}")

        now = time.time()

        # SWEEP PHASE: Scan cas/ and cas_artifacts/
        targets = [self.cas_root, self.cas_artifacts_root]
        for cas_dir in targets:
            if not cas_dir.is_dir():
                continue

            for entry in list(cas_dir.iterdir()):
                if not entry.is_file():
                    continue

                # Parse hash or proof_id from filename (e.g., <sha256>.tar.gz or <proof_id>.json)
                filename = entry.name
                file_stem = entry.name.split(".")[0].lower().strip()

                # If referenced in active/recent tasks, do NOT sweep
                if file_stem in marked_hashes or filename in marked_hashes:
                    continue

                # Age check: enforce cas_ttl_seconds before deleting unreferenced blobs
                mtime = entry.stat().st_mtime
                age_seconds = now - mtime
                if age_seconds < cas_ttl_seconds:
                    continue

                sz = entry.stat().st_size
                report.cas_blobs_deleted += 1
                report.cas_bytes += sz
                if not dry_run:
                    try:
                        entry.unlink(missing_ok=True)
                    except Exception as exc:
                        report.errors.append(f"Failed to unlink CAS blob {entry.name}: {exc}")

    def _optimize_ledger(self, report: GCReport, full_vacuum: bool = False) -> None:
        """Run vacuum on ledger.db with non-blocking policy."""
        try:
            has_leases = False
            if hasattr(self.ledger, "conn") and self.ledger.conn is not None:
                with self.ledger._lock:
                    lease_row = self.ledger.conn.execute(
                        "SELECT COUNT(*) as cnt FROM leases WHERE released_at IS NULL"
                    ).fetchone()
                    if lease_row and lease_row["cnt"] > 0:
                        has_leases = True

            # If full_vacuum is requested and no workers hold active leases, run VACUUM
            if full_vacuum and not has_leases:
                self.ledger.vacuum(incremental=False)
                report.vacuumed = True
                report.vacuum_mode = "full"
            else:
                # Background/startup sweep: non-blocking incremental vacuum only
                self.ledger.vacuum(incremental=True)
                report.vacuumed = True
                report.vacuum_mode = "incremental"
        except Exception as exc:
            logger.debug("Ledger vacuum warning: %s", exc)
            report.errors.append(f"Vacuum error: {exc}")

    def start_async_sweep(self, full_vacuum: bool = False) -> threading.Thread:
        """Trigger an asynchronous, non-blocking garbage collection sweep in a daemon thread."""
        def _sweep_worker() -> None:
            try:
                # Small jitter to allow startup handshake to finish cleanly
                time.sleep(1.5)
                res = self.run_sweep(dry_run=False, full_vacuum=full_vacuum)
                if res.total_bytes_reclaimed > 0:
                    logger.info(
                        "Background GC sweep reclaimed %s (%d workspaces, %d snapshots, %d CAS blobs)",
                        format_bytes(res.total_bytes_reclaimed),
                        res.workspaces_deleted,
                        res.snapshots_deleted,
                        res.cas_blobs_deleted,
                    )
            except Exception as exc:
                logger.debug("Background GC sweep encountered error: %s", exc)

        t = threading.Thread(target=_sweep_worker, daemon=True, name="hex-async-gc")
        t.start()
        return t

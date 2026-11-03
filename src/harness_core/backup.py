"""HEX State Backup, Zip-Slip Defense Restore, and Diagnostic Support Dump.

Implements:
1. `create_backup`: Generates timestamped zip archives containing configuration,
   database, custom skills, and schedules while excluding transient files.
2. `restore_backup`: Safely restores archives with strict zip-slip path traversal protection.
3. `generate_dump`: Clean, uncolored, deterministic plain-text system diagnostic report
   for support tickets and bug reporting.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .doctor import HexDoctor

logger = logging.getLogger(__name__)

# Directory names excluded from archives
EXCLUDED_DIR_NAMES = {
    "cow_workspaces",
    ".cache",
    "cache",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".git",
    "backups",
    "node_modules",
}

# Transient file suffixes excluded from archives
EXCLUDED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".db-wal",
    ".db-shm",
    ".db-journal",
    ".lock",
    ".pid",
    ".sock",
)

# Secret keys to redact in diagnostic dumps
SECRET_ENV_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTIGRAVITY_TOKEN",
    "ANTIGRAVITY_REFRESH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "HEX_AUTH_TOKEN",
    "HARNESS_AUTH_TOKEN",
    "POSTGRES_PASSWORD",
    "DATABASE_URL",
)


class SecurityViolationError(ValueError):
    """Raised when an archive member attempts a path traversal / zip-slip attack."""
    pass


def _sha256_file(path: Path) -> str:
    """Compute hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _should_exclude(file_path: Path, root: Path, quick: bool = False) -> bool:
    """Determine if a file should be excluded from the backup archive."""
    try:
        rel_parts = file_path.relative_to(root).parts
    except ValueError:
        rel_parts = file_path.parts

    # Exclude directories matching blacklisted names
    for part in rel_parts[:-1]:
        if part in EXCLUDED_DIR_NAMES:
            return True
        if part.startswith(".venv") or part.startswith("venv"):
            return True

    # Exclude blacklisted file names or suffixes
    if file_path.name in EXCLUDED_DIR_NAMES:
        return True
    if any(file_path.name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
        return True

    # Exclude files larger than 50MB
    try:
        if file_path.stat().st_size > 50 * 1024 * 1024:
            return True
    except OSError:
        return True

    # In quick mode: only database and configuration files
    if quick:
        allowed_exts = {".db", ".sqlite", ".sqlite3", ".yaml", ".yml", ".json", ".toml", ".ini"}
        if file_path.suffix.lower() not in allowed_exts:
            return True

    return False


def create_backup(
    output_path: Path | str | None = None,
    data_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
    quick: bool = False,
) -> dict[str, Any]:
    """Create a compressed zip archive of HEX state.

    Collects configuration files, databases (with WAL checkpointing), cron schedules,
    and custom skills, while excluding transient caches and virtual environments.
    """
    ws = Path(workspace_root or Path.cwd()).resolve()
    data = Path(
        data_dir
        or os.environ.get("HEX_DATA_DIR")
        or os.environ.get("HARNESS_DATA_DIR")
        or ws / ".hex" / "data"
    ).resolve()

    # Determine destination zip file path
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    default_filename = f"hex-backup-{now_str}.zip"

    if output_path is None:
        dest_dir = data / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        zip_path = dest_dir / default_filename
    else:
        out = Path(output_path)
        if out.is_dir() or str(output_path).endswith("/") or str(output_path).endswith("\\"):
            out.mkdir(parents=True, exist_ok=True)
            zip_path = out / default_filename
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            zip_path = out

    # Collect files to archive
    files_to_archive: list[tuple[Path, str]] = []  # (source_path, archive_name)
    seen_canonical: set[Path] = set()

    # Checkpoint any active SQLite databases in data_dir or .hex
    for db_candidate in [data / "hex.db", data / "harness.db", ws / ".hex" / "hex.db", ws / ".hex" / "harness.db"]:
        if db_candidate.is_file():
            try:
                conn = sqlite3.connect(str(db_candidate))
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.close()
            except Exception:
                pass

    # Source roots to scan
    scan_roots: list[tuple[Path, str]] = []
    hex_dir = ws / ".hex"
    if hex_dir.is_dir():
        scan_roots.append((hex_dir, ".hex"))
    if data.is_dir() and not str(data).startswith(str(hex_dir)):
        scan_roots.append((data, "data"))

    # Configs in workspace root
    for cfg_name in ["config.yaml", "config.yml"]:
        ws_cfg = ws / cfg_name
        if ws_cfg.is_file() and ws_cfg.resolve() not in seen_canonical:
            seen_canonical.add(ws_cfg.resolve())
            files_to_archive.append((ws_cfg, cfg_name))

    # Custom skills in workspace
    skills_custom = ws / "skills" / "custom"
    if skills_custom.is_dir():
        scan_roots.append((skills_custom, "skills/custom"))

    # Walk scan roots
    for root_dir, arc_prefix in scan_roots:
        for root, dirs, files in os.walk(root_dir):
            # Prune excluded directories in-place
            dirs[:] = [
                d for d in dirs
                if d not in EXCLUDED_DIR_NAMES
                and not d.startswith(".venv")
                and not d.startswith("venv")
            ]

            for file in files:
                f_path = Path(root) / file
                canonical = f_path.resolve()
                if canonical in seen_canonical:
                    continue
                if canonical == zip_path.resolve():
                    continue

                if _should_exclude(f_path, root_dir, quick=quick):
                    continue

                seen_canonical.add(canonical)
                try:
                    rel_to_root = f_path.relative_to(root_dir).as_posix()
                    arcname = f"{arc_prefix}/{rel_to_root}".lstrip("/")
                except ValueError:
                    arcname = f"{arc_prefix}/{f_path.name}".lstrip("/")

                files_to_archive.append((f_path, arcname))

    # Write files into zip archive
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src_file, arcname in files_to_archive:
            zf.write(src_file, arcname)

    file_count = len(files_to_archive)
    size_bytes = zip_path.stat().st_size
    sha256_hash = _sha256_file(zip_path)

    return {
        "backup_path": str(zip_path),
        "files_archived": file_count,
        "size_bytes": size_bytes,
        "sha256": sha256_hash,
        "quick": quick,
    }


def restore_backup(
    archive_path: Path | str,
    target_dir: Path | str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Restore state from a backup archive with strict zip-slip defense.

    Raises `SecurityViolationError` if any archive member attempts a path traversal attack.
    """
    archive = Path(archive_path).resolve()
    target = Path(target_dir).resolve()

    if not archive.is_file():
        raise FileNotFoundError(f"Backup archive not found: {archive}")

    if not zipfile.is_zipfile(archive):
        raise ValueError(f"File is not a valid zip archive: {archive}")

    with zipfile.ZipFile(archive, "r") as zf:
        # Step 1: Strict Zip-Slip Validation for all members
        for member in zf.infolist():
            fname = member.filename

            # Rejects filenames starting with '/' or containing '..' path traversal
            if (
                fname.startswith("/")
                or fname.startswith("\\")
                or ".." in Path(fname).parts
                or "../" in fname
                or "..\\" in fname
            ):
                raise SecurityViolationError(
                    f"Zip-slip path traversal attempt detected in member: {fname}"
                )

            dest_path = (target / fname).resolve()
            try:
                dest_path.relative_to(target)
            except ValueError:
                raise SecurityViolationError(
                    f"Zip-slip path traversal attempt detected in member: {fname}"
                )

        # Step 2: Extraction or dry-run listing
        restored_files: list[str] = []
        if dry_run:
            for member in zf.infolist():
                if not member.is_dir():
                    restored_files.append(member.filename)
            return {
                "target_dir": str(target),
                "restored_files": restored_files,
                "dry_run": True,
            }

        target.mkdir(parents=True, exist_ok=True)
        for member in zf.infolist():
            if member.is_dir():
                (target / member.filename).mkdir(parents=True, exist_ok=True)
                continue

            dest_path = target / member.filename
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(dest_path, "wb") as dst:
                dst.write(src.read())
            restored_files.append(member.filename)

        return {
            "target_dir": str(target),
            "restored_files": restored_files,
            "dry_run": False,
        }


def generate_dump(
    data_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> str:
    """Generate clean, uncolored, deterministic plain-text system diagnostic report."""
    ws = Path(workspace_root or Path.cwd()).resolve()
    data = Path(
        data_dir
        or os.environ.get("HEX_DATA_DIR")
        or os.environ.get("HARNESS_DATA_DIR")
        or ws / ".hex" / "data"
    ).resolve()

    doctor = HexDoctor(workspace_root=ws, data_dir=data)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("HEX SYSTEM DIAGNOSTIC DUMP")
    lines.append("=" * 80)
    lines.append(f"Timestamp:       {timestamp}")
    lines.append(f"Workspace Root:  {ws}")
    lines.append(f"Data Directory:  {data}")
    lines.append("")

    # Section 1: HEADER & SYSTEM INFO
    lines.append("-" * 80)
    lines.append("1. HEADER & SYSTEM INFO")
    lines.append("-" * 80)
    lines.append(f"OS:               {platform.system()} {platform.release()}")
    lines.append(f"Platform:         {platform.platform()}")
    lines.append(f"Python Version:   {platform.python_version()} ({sys.executable})")
    lines.append(f"Architecture:     {platform.machine()}")
    py_checks = doctor.check_python()
    for c in py_checks:
        lines.append(f"  [{c.status.upper()}] {c.name}: {c.message}")
    lines.append("")

    # Section 2: HARDWARE & ACCELERATION
    lines.append("-" * 80)
    lines.append("2. HARDWARE & ACCELERATION")
    lines.append("-" * 80)
    hw_checks = doctor.check_hardware_acceleration()
    for c in hw_checks:
        lines.append(f"  [{c.status.upper()}] {c.name}: {c.message}")
    lines.append("")

    # Section 3: STORAGE & LEDGER METRICS
    lines.append("-" * 80)
    lines.append("3. STORAGE & LEDGER METRICS")
    lines.append("-" * 80)
    db_file = data / "hex.db"
    if not db_file.exists():
        for cand_name in ("ledger.db", "harness.db"):
            cand_db = data / cand_name
            if cand_db.exists():
                db_file = cand_db
                break

    db_engine = "SQLite WAL" if db_file.exists() else "Not Initialized / PostgreSQL"
    lines.append(f"Storage Path:     {data}")
    lines.append(f"Database Engine:  {db_engine}")
    if db_file.exists():
        lines.append(f"DB File Size:     {db_file.stat().st_size:,} bytes")
    else:
        lines.append("DB File Size:     0 bytes")

    # Metrics from LedgerStore if database exists
    tasks_count = 0
    completed_count = 0
    failed_count = 0
    tool_calls_count = 0
    recent_failures: list[dict[str, Any]] = []

    if db_file.exists():
        try:
            from .db import LedgerStore
            ledger = LedgerStore(db_file)
            tasks = ledger.list_tasks(limit=100000)
            tasks_count = len(tasks)
            completed_count = sum(1 for t in tasks if t.get("status") in ("COMPLETED", "TERMINATED_PASS"))
            failed_count = sum(1 for t in tasks if t.get("status") in ("FAILED", "TERMINATED_FAIL", "NEEDS_ATTENTION"))

            try:
                if ledger.backend_type == "sqlite" and ledger.conn:
                    row = ledger.conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()
                    tool_calls_count = row[0] if row else 0
            except Exception:
                pass

            # Gather recent failures
            for t in reversed(tasks):
                if t.get("status") in ("FAILED", "TERMINATED_FAIL", "NEEDS_ATTENTION") or t.get("terminal_reason"):
                    recent_failures.append(t)
                    if len(recent_failures) >= 5:
                        break
        except Exception as e:
            lines.append(f"  [WARNING] Unable to read ledger metrics: {e}")

    lines.append(f"Total Tasks:      {tasks_count}")
    lines.append(f"Completed Tasks:  {completed_count}")
    lines.append(f"Failed Tasks:     {failed_count}")
    lines.append(f"Tool Calls Count: {tool_calls_count}")
    lines.append("")

    # Section 4: WORKSPACE & COW ISOLATION
    lines.append("-" * 80)
    lines.append("4. WORKSPACE & COW ISOLATION")
    lines.append("-" * 80)
    cow_checks = doctor.check_cow_providers()
    for c in cow_checks:
        lines.append(f"  [{c.status.upper()}] {c.name}: {c.message}")
    sb_checks = doctor.check_sandboxes()
    for c in sb_checks:
        lines.append(f"  [{c.status.upper()}] {c.name}: {c.message}")
    st_checks = doctor.check_storage()
    for c in st_checks:
        lines.append(f"  [{c.status.upper()}] {c.name}: {c.message}")
    lines.append("")

    # Section 5: ACTIVE INTEGRATIONS & RECEPTORS
    lines.append("-" * 80)
    lines.append("5. ACTIVE INTEGRATIONS & RECEPTORS")
    lines.append("-" * 80)
    for key in SECRET_ENV_KEYS:
        val = os.environ.get(key)
        if val:
            lines.append(f"  {key:<28}: [CONFIGURED - REDACTED]")
        else:
            lines.append(f"  {key:<28}: [NOT CONFIGURED]")
    lines.append("")

    # Section 6: SCHEDULES & CUSTOM SKILLS
    lines.append("-" * 80)
    lines.append("6. SCHEDULES & CUSTOM SKILLS")
    lines.append("-" * 80)
    # Cron schedules
    sched_file = data / "schedules.json"
    if not sched_file.exists():
        sched_file = ws / ".hex" / "schedules.json"
    if sched_file.is_file():
        try:
            sched_data = json.loads(sched_file.read_text(encoding="utf-8"))
            lines.append(f"Schedules File:   {sched_file} ({len(sched_data)} jobs)")
            for job in sched_data:
                name = job.get("name") or job.get("job_id") or "unnamed"
                expr = job.get("cron_expression") or "* * * * *"
                goal = job.get("goal") or ""
                lines.append(f"  - [{expr}] {name}: {goal}")
        except Exception as e:
            lines.append(f"  [ERROR] Reading schedules.json: {e}")
    else:
        lines.append("Schedules:        No schedules.json found")

    # Custom skills
    custom_skills_dir = ws / "skills" / "custom"
    skills_found: list[str] = []
    if custom_skills_dir.is_dir():
        for item in sorted(custom_skills_dir.iterdir()):
            if item.is_dir() and (item / "SKILL.md").is_file():
                skills_found.append(item.name)
    lines.append(f"Custom Skills:    {len(skills_found)} registered ({', '.join(skills_found) if skills_found else 'None'})")
    lines.append("")

    # Section 7: RECENT ERRORS
    lines.append("-" * 80)
    lines.append("7. RECENT ERRORS")
    lines.append("-" * 80)
    if not recent_failures:
        lines.append("No failed tasks recorded. All tasks healthy or none executed.")
    else:
        for f in recent_failures:
            lines.append(f"Task ID:         {f.get('task_id')}")
            lines.append(f"  Goal:          {f.get('goal')}")
            lines.append(f"  Status:        {f.get('status')}")
            lines.append(f"  Terminal:      {f.get('terminal_reason') or 'None'}")
            lines.append("")

    lines.append("=" * 80)
    lines.append("END OF DIAGNOSTIC DUMP")
    lines.append("=" * 80)

    return "\n".join(lines)

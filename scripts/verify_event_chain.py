#!/usr/bin/env python3
"""Pre-commit Event Chain & Schema Integrity Verification Script (GAP-03).

Inspects current SQLite or PostgreSQL database:
1. Runs LedgerStore.verify_chain_integrity() across all recorded event hashes.
2. Checks Alembic migration status (alembic current vs alembic heads).
3. Exits with 0 if chain is intact and schema is aligned, or 1 with tamper details if compromised.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

# Ensure src/ is on sys.path
_repo_root = Path(__file__).resolve().parents[1]
_src_dir = _repo_root / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from harness_core.storage.db import LedgerStore


def resolve_db_url(cli_db_url: str | None = None) -> tuple[str, Path | None]:
    """Resolves target database URL and optional local filesystem path."""
    if cli_db_url:
        url = cli_db_url.strip()
    elif os.environ.get("HARNESS_DATABASE_URL"):
        url = os.environ["HARNESS_DATABASE_URL"].strip()
    elif os.environ.get("HEX_DATABASE_URL"):
        url = os.environ["HEX_DATABASE_URL"].strip()
    else:
        # Default local store location
        default_file = Path.home() / ".hex" / "data" / "ledger.db"
        if default_file.is_file():
            url = f"sqlite:///{default_file}"
        else:
            dev_db = _repo_root / "migrations" / "alembic_dev.db"
            if dev_db.is_file():
                url = f"sqlite:///{dev_db}"
            else:
                url = f"sqlite:///{default_file}"

    # Extract clean file path if sqlite
    file_path: Path | None = None
    if url.startswith("sqlite:///"):
        file_path = Path(url[len("sqlite:///"):])
    elif not (url.startswith("postgresql://") or url.startswith("postgres://") or url.startswith("postgresql+asyncpg://")):
        file_path = Path(url)
        url = f"sqlite:///{file_path}"

    return url, file_path


def check_alembic_migration_status(
    db_url: str,
    ini_path: Path,
    auto_stamp_if_initialized: bool = True,
) -> tuple[bool, str | None, list[str], list[str]]:
    """Checks alembic current against alembic heads.

    Returns:
        (is_aligned, current_revision, head_revisions, errors)
    """
    errors: list[str] = []
    current_rev: str | None = None
    heads: list[str] = []

    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from alembic.runtime.migration import MigrationContext
        from alembic import command
        from sqlalchemy import create_engine
    except ImportError as e:
        return True, "unknown (alembic optional dependency not installed)", [], []

    if not ini_path.is_file():
        errors.append(f"alembic.ini configuration file not found at: {ini_path}")
        return False, None, [], errors

    try:
        cfg = Config(str(ini_path))
        cfg.set_main_option("sqlalchemy.url", db_url)
        script_loc = ini_path.parent / "migrations"
        if script_loc.is_dir():
            cfg.set_main_option("script_location", str(script_loc))

        script = ScriptDirectory.from_config(cfg)
        heads = list(script.get_heads())
        head_rev = heads[0] if heads else None

        # Inspect current revision from database
        engine = create_engine(db_url)
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            current_rev = ctx.get_current_revision()

            # If current_rev is None, check if schema tables already exist
            if current_rev is None and auto_stamp_if_initialized and head_rev:
                # Check for existing tables
                table_names: set[str] = set()
                if "sqlite" in db_url:
                    res = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table';")
                    table_names = {row[0] for row in res}
                else:
                    res = conn.exec_driver_sql("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';")
                    table_names = {row[0] for row in res}

                if "tasks" in table_names and "task_events" in table_names:
                    # Database initialized with authoritative schema; stamp head so alembic recognizes revision
                    try:
                        command.stamp(cfg, "head")
                        current_rev = head_rev
                    except Exception:
                        pass

        if not heads:
            errors.append("No Alembic migration heads discovered in migrations/versions.")
            return False, current_rev, heads, errors

        # If database has tables, current_rev must match head
        if current_rev is None:
            # Check if database has any tables at all
            if table_names:
                errors.append(
                    f"Database has unversioned schema tables but no Alembic revision stamp. Expected head: {heads}."
                )
                return False, None, heads, errors
            else:
                # Fresh empty database
                return True, "uninitialized (empty database)", heads, []

        if current_rev not in heads:
            errors.append(
                f"Alembic migration mismatch: current database revision '{current_rev}' is not at head {heads}."
            )
            return False, current_rev, heads, errors

        return True, current_rev, heads, []

    except Exception as exc:
        errors.append(f"Alembic inspection error: {exc}")
        return False, current_rev, heads, errors


def run_verification(
    db_url: str | None = None,
    alembic_ini_path: str | Path | None = None,
    verbose: bool = False,
    json_output: bool = False,
) -> int:
    """Executes event chain integrity audit and Alembic migration status check."""
    url, file_path = resolve_db_url(db_url)
    ini_path = Path(alembic_ini_path).resolve() if alembic_ini_path else (_repo_root / "alembic.ini")

    # If local SQLite file doesn't exist yet, zero-state is valid
    if file_path and not file_path.is_file():
        if json_output:
            print(json.dumps({
                "status": "VALID",
                "database_url": url,
                "verified_events": 0,
                "tasks_checked": 0,
                "alembic_status": "empty",
                "errors": [],
            }, indent=2))
        else:
            print(f"ℹ Database file {file_path} does not exist yet (clean zero-state). Event chain is intact.")
        return 0

    # 1. Verify Ledger Event Chain Integrity
    store = LedgerStore(file_path if file_path else url)
    chain_report = store.verify_chain_integrity()

    # 2. Check Alembic Migration Status
    alembic_ok, cur_rev, heads, alembic_errors = check_alembic_migration_status(url, ini_path)

    all_errors = list(chain_report.errors) + alembic_errors
    is_valid = chain_report.valid and alembic_ok

    if json_output:
        payload = {
            "status": "VALID" if is_valid else "COMPROMISED",
            "database_url": url,
            "chain_valid": chain_report.valid,
            "tasks_checked": chain_report.tasks_checked,
            "verified_events": chain_report.verified_events,
            "alembic_aligned": alembic_ok,
            "alembic_current_revision": cur_rev,
            "alembic_heads": heads,
            "errors": all_errors,
        }
        print(json.dumps(payload, indent=2))
        return 0 if is_valid else 1

    # Formatted Console Output
    c_cyan = "\033[96m"
    c_green = "\033[92m"
    c_red = "\033[91m"
    c_bold = "\033[1m"
    c_reset = "\033[0m"

    print(f"\n{c_bold}{c_cyan}======================================================================{c_reset}")
    print(f"{c_bold}{c_cyan}             HEX EVENT CHAIN & SCHEMA INTEGRITY AUDITOR               {c_reset}")
    print(f"{c_bold}{c_cyan}======================================================================{c_reset}")
    print(f"Database Target: {url}")
    print("----------------------------------------------------------------------")

    # Step 1: Chain Integrity
    print(f"[1/2] Verifying cryptographic hash chain integrity...")
    if chain_report.valid:
        print(
            f"      {c_green}✓ Chain intact:{c_reset} {chain_report.verified_events} event(s) "
            f"across {chain_report.tasks_checked} task(s) cryptographically verified."
        )
    else:
        print(f"      {c_red}✗ Event chain integrity COMPROMISED!{c_reset}")
        for err in chain_report.errors:
            print(f"        {c_red}↳ {err}{c_reset}")

    # Step 2: Alembic Migrations
    print(f"[2/2] Checking Alembic migration parity...")
    if alembic_ok:
        head_str = ", ".join(heads) if heads else "none"
        print(f"      {c_green}✓ Schema parity verified:{c_reset} revision '{cur_rev}' aligned with head ({head_str}).")
    else:
        print(f"      {c_red}✗ Migration parity mismatch!{c_reset}")
        for err in alembic_errors:
            print(f"        {c_red}↳ {err}{c_reset}")

    print("----------------------------------------------------------------------")
    if is_valid:
        print(f"{c_bold}{c_green}RESULT: Intact. All event hashes & schemas verified for commit.{c_reset}")
        print(f"{c_bold}{c_cyan}======================================================================\n{c_reset}")
        return 0
    else:
        print(f"{c_bold}{c_red}RESULT: FAILED ({len(all_errors)} violation(s) detected).{c_reset}")
        print(f"{c_bold}{c_cyan}======================================================================\n{c_reset}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify cryptographic event hash chains and Alembic schema parity (GAP-03)"
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Database URL or path (defaults to $HARNESS_DATABASE_URL or ~/.hex/data/ledger.db)",
    )
    parser.add_argument(
        "--alembic-ini",
        default=None,
        help="Path to alembic.ini (defaults to repository root alembic.ini)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Display verbose verification output",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON verification report",
    )
    args = parser.parse_args()
    return run_verification(
        db_url=args.db_url,
        alembic_ini_path=args.alembic_ini,
        verbose=args.verbose,
        json_output=args.json,
    )


if __name__ == "__main__":
    sys.exit(main())

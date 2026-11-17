"""Guided first-task verification of toolchains, isolation, CAS, and ledger integrity."""

from __future__ import annotations

import datetime
import hashlib
import json
import platform
import shutil
import sys
from pathlib import Path
from typing import Any


def run_first_task_verification(
    config_dir: Path | None = None,
    target_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute guided first-task verification, reporting only what was actually verified.

    Each subsystem check performs a real operation (CoW clone + isolation probe +
    rollback, CAS write/read-back, ledger journaling + hash-chain verification) and
    records its own pass/fail/skipped status. The task is marked SUCCEEDED only when
    every executed check passes; provider connectivity is explicitly skipped (no
    live calls during onboarding).
    """
    cfg_dir = config_dir or (Path.home() / ".hex")
    data_dir = cfg_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    out_dir = target_dir or (Path.home() / "hex-first-task")
    out_dir.mkdir(parents=True, exist_ok=True)
    diag_file = out_dir / "diagnostics.md"

    checks: list[dict[str, str]] = []

    def record(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    # 1. Probe system toolchains (git + python3 required; others informational)
    git_bin = shutil.which("git")
    py_bin = shutil.which("python3") or sys.executable
    docker_bin = shutil.which("docker")
    clang_bin = shutil.which("clang") or shutil.which("gcc")
    rustc_bin = shutil.which("rustc")
    node_bin = shutil.which("node")

    tools_summary = {
        "git": bool(git_bin),
        "python3": bool(py_bin),
        "docker": bool(docker_bin),
        "clang": bool(clang_bin),
        "rustc": bool(rustc_bin),
        "node": bool(node_bin),
    }
    missing_required = [name for name in ("git", "python3") if not tools_summary[name]]
    optional_present = [name for name in ("docker", "clang", "rustc", "node") if tools_summary[name]]
    if missing_required:
        record("devtools", "failed", f"Missing required tool(s): {', '.join(missing_required)}")
    else:
        record(
            "devtools",
            "passed",
            f"git and python3 available; optional toolchains present: {', '.join(optional_present) or 'none'}",
        )

    # 2. Real CoW isolation probe: snapshot -> clone -> dirty write -> rollback
    ws_dir = data_dir / "active_workspaces" / "ws_first_task"
    ws_dir.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cow_backend = "unknown"
    try:
        from harness_core.storage.cow import get_default_snapshot_provider

        provider = get_default_snapshot_provider(data_dir / "cow_snapshots")
        cow_backend = getattr(provider, "backend_name", type(provider).__name__)
        seed_dir = data_dir / "first_task_seed"
        seed_dir.mkdir(parents=True, exist_ok=True)
        marker = seed_dir / "marker.txt"
        marker.write_text("hex-first-task-seed", encoding="utf-8")

        snap = provider.create_snapshot(seed_dir, label="first_task")
        try:
            provider.clone_to_workspace(snap.snapshot_path, ws_dir)
            dirty = ws_dir / "dirty_probe.txt"
            dirty.write_text("isolation probe", encoding="utf-8")
            if dirty.exists() and (seed_dir / "dirty_probe.txt").exists():
                raise RuntimeError("sandbox write leaked into the source workspace")
            if not (ws_dir / "marker.txt").is_file():
                raise RuntimeError("cloned workspace is missing the seed marker file")
            provider.rollback_workspace(ws_dir, snap.snapshot_path)
            if dirty.exists():
                raise RuntimeError("rollback did not remove the dirty probe file")
            verified_marker = (ws_dir / "marker.txt").read_text(encoding="utf-8")
            if verified_marker != "hex-first-task-seed":
                raise RuntimeError("rollback did not restore the seed marker content")
        finally:
            provider.discard_snapshot(snap.snapshot_path)
            shutil.rmtree(seed_dir, ignore_errors=True)
        # Overlay cleanup unmounts the clone; retain its verified diagnostic marker.
        (ws_dir / "marker.txt").write_text(verified_marker, encoding="utf-8")
        record("cow_isolation", "passed", f"Clone, write isolation, and rollback verified via {cow_backend}")
    except Exception as exc:
        record("cow_isolation", "failed", f"{type(exc).__name__}: {exc}")

    (ws_dir / ".hex_workspace").write_text(
        json.dumps(
            {
                "task_id": "tsk_first_task_verify",
                "created_at": now_iso,
                "kind": "system_verification",
                "cow_mode": "active_workspaces",
                "cow_backend": cow_backend,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # 3. CAS write + read-back roundtrip (both stores must return identical bytes)
    cas_dir = data_dir / "cas"
    cas_dir.mkdir(parents=True, exist_ok=True)
    cas_art_dir = data_dir / "cas_artifacts"
    cas_art_dir.mkdir(parents=True, exist_ok=True)
    probe_payload = f"hex-cas-probe {now_iso}".encode("utf-8")
    probe_sha = hashlib.sha256(probe_payload).hexdigest()
    try:
        (cas_dir / probe_sha).write_bytes(probe_payload)
        (cas_art_dir / probe_sha).write_bytes(probe_payload)
        for store in (cas_dir, cas_art_dir):
            readback = (store / probe_sha).read_bytes()
            if readback != probe_payload or hashlib.sha256(readback).hexdigest() != probe_sha:
                raise RuntimeError(f"CAS read-back mismatch in {store.name}")
        record("cas", "passed", f"SHA-256 write/read-back roundtrip verified in cas/ and cas_artifacts/ ({probe_sha[:12]}...)")
    except Exception as exc:
        record("cas", "failed", f"{type(exc).__name__}: {exc}")

    # 4. Provider connectivity is NOT exercised during onboarding
    record(
        "provider_connectivity",
        "skipped",
        "No live provider call is made during onboarding; run `hex doctor --live` to verify credentials and connectivity.",
    )

    # 5. Ledger journaling + cryptographic chain verification gate the task outcome
    ledger_path = data_dir / "ledger.db"
    from harness_core.storage.db import LedgerStore

    ledger = LedgerStore(ledger_path)
    t_id = "tsk_first_task_verify"
    chain_verified_events = 0
    try:
        task_res = ledger.create_task(
            {
                "goal": "HEX Onboarding First-Task Verification",
                "deliverable": "diagnostic_report",
            }
        )
        t_id = task_res.get("task_id", t_id)
        ledger.transition_task(t_id, "RUNNING", reason_code="VERIFICATION_START")
        ledger.transition_task(t_id, "VERIFYING", reason_code="VERIFICATION_PROBE")

        executed = [c for c in checks if c["status"] != "skipped"]
        all_passed = all(c["status"] == "passed" for c in executed)
        if all_passed:
            report = ledger.verify_chain_integrity(task_id=t_id)
            if not report.valid:
                all_passed = False
                record("ledger", "failed", f"Event chain verification failed: {'; '.join(report.errors) or 'invalid chain'}")
            else:
                chain_verified_events = report.verified_events
                record("ledger", "passed", f"Task journaled and hash chain verified ({report.verified_events} events)")
        else:
            failed_names = ", ".join(c["name"] for c in executed if c["status"] != "passed")
            record("ledger", "failed", f"Task journaled but marked FAILED because upstream checks failed: {failed_names}")

        # Diagnostic artifact reflects the real check outcomes
        check_lines = "\n".join(
            f"- **{c['name']}**: {c['status'].upper()} — {c['detail']}" for c in checks
        )
        overall = "passed" if all_passed else "failed"
        status_line = (
            "ALL VERIFIED SUBSYSTEMS OPERATIONAL (PASS)"
            if all_passed
            else "VERIFICATION FAILED — see per-check results above"
        )
        tool_lines = "\n".join(
            f"- **{name}**: {'Available (' + str(path) + ')' if path else 'Not found'}"
            for name, path in (
                ("git", git_bin),
                ("python3", py_bin),
                ("docker", docker_bin),
                ("clang/gcc", clang_bin),
                ("rustc", rustc_bin),
                ("node", node_bin),
            )
        )
        diag_content = f"""# HEX Agent System Diagnostic & First-Task Verification
- **Timestamp**: {now_iso}
- **Operating System**: {platform.system()} ({platform.machine()})
- **Python**: {sys.version.split()[0]} ({py_bin})
- **CoW Workspace**: `{ws_dir}`
- **CoW Backend**: {cow_backend}
- **Isolation Mode**: Ephemeral Task Workspace (`active_workspaces/ws_first_task`)

## Verified Toolchains & Compilers
{tool_lines}

## Subsystem Check Results
{check_lines}

## Status
**{status_line}**
"""
        diag_bytes = diag_content.encode("utf-8")
        sha256_hash = hashlib.sha256(diag_bytes).hexdigest()
        diag_file.write_text(diag_content, encoding="utf-8")
        (cas_dir / sha256_hash).write_bytes(diag_bytes)
        (cas_art_dir / sha256_hash).write_bytes(diag_bytes)
        ledger.record_artifact(
            task_id=t_id,
            kind="diagnostic_report",
            uri=str(diag_file),
            sha256=sha256_hash,
            size_bytes=len(diag_bytes),
            media_type="text/markdown",
        )
        if all_passed:
            ledger.transition_task(t_id, "SUCCEEDED", reason_code="VERIFICATION_SUCCESS")
        else:
            ledger.transition_task(t_id, "FAILED", reason_code="VERIFICATION_FAILED")
    finally:
        ledger.close()

    # Update workspace metadata with actual task_id
    (ws_dir / ".hex_workspace").write_text(
        json.dumps(
            {
                "task_id": t_id,
                "created_at": now_iso,
                "kind": "system_verification",
                "cow_mode": "active_workspaces",
                "cow_backend": cow_backend,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    overall = "passed" if all(c["status"] in ("passed", "skipped") for c in checks) else "failed"
    icons = {"passed": "✓", "failed": "✗", "skipped": "–"}
    print("=" * 64)
    print("◈ HEX Guided First-Task Verification Beat")
    print("=" * 64)
    for c in checks:
        print(f"  {icons[c['status']]} {c['name']}: {c['status'].upper()} — {c['detail']}")
    print(f"  Artifact: {diag_file}")
    print(f"  Overall: {overall.upper()}")
    print("=" * 64)
    print()

    return {
        "status": overall,
        "task_id": t_id,
        "artifact_path": str(diag_file),
        "cas_sha256": sha256_hash,
        "cow_workspace": str(ws_dir),
        "cow_backend": cow_backend,
        "tools_summary": tools_summary,
        "checks": checks,
        "chain_verified_events": chain_verified_events,
    }

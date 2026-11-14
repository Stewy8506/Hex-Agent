"""Unified Benchmark Evaluation Suite Runner (`hex eval`) (GAP-02).

Runs benchmark test suites with a formatted summary table:
total tests, passed, failed, latency, and status.
Supported suites: all, core, resilience, compaction, tui.
"""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
import sys
import time
import unittest
from typing import Any


def _ensure_tests_in_sys_path() -> None:
    """Ensures repository root and tests directory are present in sys.path."""
    cwd = Path.cwd()
    if (cwd / "tests").is_dir():
        if str(cwd) not in sys.path:
            sys.path.insert(0, str(cwd))
        if str(cwd / "tests") not in sys.path:
            sys.path.insert(0, str(cwd / "tests"))

    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "tests").is_dir():
        if str(source_root) not in sys.path:
            sys.path.insert(0, str(source_root))
        if str(source_root / "tests") not in sys.path:
            sys.path.insert(0, str(source_root / "tests"))


def _load_module_tests(loader: unittest.TestLoader, mod_name: str) -> unittest.TestSuite:
    """Imports target test module and returns a unittest.TestSuite."""
    candidates = [
        mod_name,
        mod_name[6:] if mod_name.startswith("tests.") else mod_name,
        f"tests.{mod_name}" if not mod_name.startswith("tests.") else mod_name,
    ]

    last_exc = None
    for c in candidates:
        try:
            mod = importlib.import_module(c)
            suite = loader.loadTestsFromModule(mod)
            if suite.countTestCases() > 0:
                return suite
        except Exception as exc:
            last_exc = exc

    # Fallback to direct name lookup
    try:
        return loader.loadTestsFromName(mod_name)
    except Exception:
        if last_exc:
            raise last_exc
        raise


EVAL_SUITES: dict[str, list[str]] = {
    "core": [
        "tests.test_model_providers",
        "tests.test_cow_providers",
        "tests.test_db_dual_driver",
        "tests.test_packaging_and_installer",
    ],
    "resilience": [
        "tests.test_react_loop_resilience",
        "tests.test_antigravity_oauth_and_tui_resilience",
    ],
    "compaction": [
        "tests.test_context_compressor_integration",
        "tests.test_context_compressor_tail_pruning",
        "tests.test_two_tier_compression_benchmark",
    ],
    "tui": [
        "tests.test_rust_tui_launcher",
        "tests.test_tui_approvals_and_init",
    ],
}

# 'all' encompasses every standard benchmark suite
ALL_MODULES: list[str] = []
for _mods in EVAL_SUITES.values():
    for _m in _mods:
        if _m not in ALL_MODULES:
            ALL_MODULES.append(_m)
EVAL_SUITES["all"] = ALL_MODULES


def run_eval_suite(
    suite_name: str = "core",
    verbose: bool = False,
    json_output: bool = False,
    stream: Any = None,
) -> int:
    """Executes the specified benchmark eval suite and emits a formatted summary table.

    Args:
        suite_name: One of 'all', 'core', 'resilience', 'compaction', 'tui'.
        verbose: If True, prints verbose unittest runner traces.
        json_output: If True, outputs raw JSON payload instead of ANSI table.
        stream: Output stream (defaults to sys.stdout).

    Returns:
        0 if all tests passed, 1 if any failure or error occurred.
    """
    out = stream if stream is not None else sys.stdout
    _ensure_tests_in_sys_path()
    normalized_suite = suite_name.lower().strip()

    if normalized_suite not in EVAL_SUITES:
        err_msg = (
            f"Unknown evaluation suite: '{suite_name}'. "
            f"Supported suites: {', '.join(sorted(EVAL_SUITES.keys()))}"
        )
        if json_output:
            print(json.dumps({"error": err_msg, "status": "FAILED"}), file=out)
        else:
            print(f"\033[91m✗ {err_msg}\033[0m", file=out)
        return 1

    target_modules = EVAL_SUITES[normalized_suite]
    loader = unittest.TestLoader()

    results: list[dict[str, Any]] = []
    total_run = 0
    total_passed = 0
    total_failed = 0
    total_errors = 0
    total_skipped = 0
    overall_start = time.perf_counter()

    for mod in target_modules:
        try:
            test_suite = _load_module_tests(loader, mod)
        except Exception as exc:
            results.append({
                "module": mod,
                "total": 0,
                "passed": 0,
                "failed": 0,
                "errors": 1,
                "skipped": 0,
                "latency_sec": 0.0,
                "status": "ERROR",
                "error_details": str(exc),
            })
            total_errors += 1
            continue

        runner_stream = out if verbose else io.StringIO()
        runner = unittest.TextTestRunner(
            stream=runner_stream,
            verbosity=2 if verbose else 0,
        )

        mod_start = time.perf_counter()
        test_result = runner.run(test_suite)
        mod_elapsed = time.perf_counter() - mod_start

        run_count = test_result.testsRun
        fail_count = len(test_result.failures)
        err_count = len(test_result.errors)
        skip_count = len(test_result.skipped)
        pass_count = max(0, run_count - fail_count - err_count - skip_count)
        status_str = "PASSED" if test_result.wasSuccessful() else "FAILED"

        total_run += run_count
        total_passed += pass_count
        total_failed += fail_count
        total_errors += err_count
        total_skipped += skip_count

        results.append({
            "module": mod,
            "total": run_count,
            "passed": pass_count,
            "failed": fail_count,
            "errors": err_count,
            "skipped": skip_count,
            "latency_sec": round(mod_elapsed, 4),
            "status": status_str,
        })

    overall_latency = round(time.perf_counter() - overall_start, 4)
    overall_status = "PASSED" if (total_failed == 0 and total_errors == 0) else "FAILED"

    if json_output:
        payload = {
            "suite": normalized_suite,
            "status": overall_status,
            "total_tests": total_run,
            "passed": total_passed,
            "failed": total_failed,
            "errors": total_errors,
            "skipped": total_skipped,
            "total_latency_sec": overall_latency,
            "modules": results,
        }
        print(json.dumps(payload, indent=2), file=out)
        return 0 if overall_status == "PASSED" else 1

    # Render ANSI formatted summary table
    c_cyan = "\033[96m"
    c_green = "\033[92m"
    c_red = "\033[91m"
    c_bold = "\033[1m"
    c_reset = "\033[0m"

    print("", file=out)
    print(f"{c_bold}{c_cyan}========================================================================================{c_reset}", file=out)
    print(f"{c_bold}{c_cyan}                             HEX BENCHMARK EVALUATION RUNNER                           {c_reset}", file=out)
    print(f"{c_bold}{c_cyan}========================================================================================{c_reset}", file=out)
    print(f"Suite: {c_bold}{normalized_suite}{c_reset} | Modules: {len(target_modules)} | Total Tests: {total_run} | Latency: {overall_latency:.3f}s", file=out)
    print("----------------------------------------------------------------------------------------", file=out)
    print(f"{'Module':<42} {'Total':>6} {'Passed':>7} {'Failed':>7} {'Errors':>7} {'Latency':>10} {'Status':>8}", file=out)
    print("----------------------------------------------------------------------------------------", file=out)

    for r in results:
        mod_name = r["module"]
        if len(mod_name) > 40:
            mod_name = "..." + mod_name[-37:]
        s_color = c_green if r["status"] == "PASSED" else c_red
        lat_str = f"{r['latency_sec']:.3f}s"
        print(
            f"{mod_name:<42} {r['total']:>6} {r['passed']:>7} {r['failed']:>7} {r['errors']:>7} {lat_str:>10} {s_color}{r['status']:>8}{c_reset}",
            file=out,
        )

    print("----------------------------------------------------------------------------------------", file=out)
    overall_color = c_green if overall_status == "PASSED" else c_red
    tot_lat_str = f"{overall_latency:.3f}s"
    print(
        f"{c_bold}{'TOTAL / SUMMARY':<42} {total_run:>6} {total_passed:>7} {total_failed:>7} {total_errors:>7} {tot_lat_str:>10} {overall_color}{overall_status:>8}{c_reset}",
        file=out,
    )
    print(f"{c_bold}{c_cyan}========================================================================================{c_reset}", file=out)

    if overall_status == "PASSED":
        print(f"{c_green}✓ All benchmark evaluations passed successfully.{c_reset}\n", file=out)
        return 0
    else:
        print(f"{c_red}✗ Benchmark evaluation encountered {total_failed} failure(s) and {total_errors} error(s).{c_reset}\n", file=out)
        return 1

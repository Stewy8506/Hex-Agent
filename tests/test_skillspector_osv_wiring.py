"""Comprehensive Verification Suite for NVIDIA SkillSpector Live OSV.dev Integration.

Tests:
1. Upstream OSV batch query availability.
2. Discovered CVEs in requirements.txt escalate risk score and trigger quarantine.
3. Offline fallback to KNOWN_OSV_VULNS when OSV.dev network requests time out.
4. Clean requirements with non-vulnerable packages pass cleanly with 0 CVE findings.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from harness_core.db import LedgerStore
from harness_core.skillspector import SkillSpectorScanner, UPSTREAM_OSV_AVAILABLE


class TestSkillSpectorOSVWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test_skillspector_osv_"))
        self.db_path = self.tmp_dir / "ledger.db"
        self.ledger = LedgerStore(self.db_path)
        self.scanner = SkillSpectorScanner(self.ledger)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_bundle(self, skill_name: str, requirements_content: str) -> Path:
        bundle_dir = self.tmp_dir / skill_name
        bundle_dir.mkdir(parents=True, exist_ok=True)
        skill_md = (
            f"---\n"
            f"name: {skill_name}\n"
            f"risk_class: R1\n"
            f"purpose: Test skill bundle for OSV dependency auditing\n"
            f"allowed_tools: [read_file]\n"
            f"---\n"
            f"# {skill_name}\n"
            f"Safe skill instructions.\n"
        )
        (bundle_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        (bundle_dir / "requirements.txt").write_text(requirements_content, encoding="utf-8")
        return bundle_dir

    def test_upstream_osv_available_flag(self) -> None:
        """Verify upstream OSV client from vendor/skillspector is imported cleanly."""
        self.assertTrue(UPSTREAM_OSV_AVAILABLE, "UPSTREAM_OSV_AVAILABLE should be True")

    def test_osv_detects_vulnerable_package(self) -> None:
        """Verify live or cached OSV batch query detects real CVEs for pyyaml==5.3."""
        bundle = self._create_bundle("yaml_parser_skill", "pyyaml==5.3\nrequests>=2.28.0\n")
        report = self.scanner.scan_bundle(bundle)

        self.assertTrue(len(report.osv_vulns) >= 1, "Expected at least 1 OSV vulnerability finding")
        self.assertGreaterEqual(report.risk_score, 85)
        self.assertEqual(report.gate_decision, "QUARANTINE")

        vuln_ids = [v["osv_id"] for v in report.osv_vulns]
        self.assertTrue(
            any("GHSA" in vid or "PYSEC" in vid or "OSV" in vid for vid in vuln_ids),
            f"Expected recognized CVE/GHSA ID, got: {vuln_ids}",
        )

    def test_offline_fallback_on_network_timeout(self) -> None:
        """Verify fallback to local KNOWN_OSV_VULNS table when OSV.dev network times out."""
        bundle = self._create_bundle("offline_skill", "pyyaml==5.3\n")

        with patch("harness_core.skillspector.osv_query_batch", side_effect=TimeoutError("Network unreachable")):
            report = self.scanner.scan_bundle(bundle)

        self.assertTrue(len(report.osv_vulns) >= 1)
        self.assertEqual(report.osv_vulns[0]["osv_id"], "OSV-2020-14343")
        self.assertGreaterEqual(report.risk_score, 85)
        self.assertEqual(report.gate_decision, "QUARANTINE")

    def test_clean_packages_pass_without_cves(self) -> None:
        """Verify safe packages with no CVEs pass with 0 findings."""
        bundle = self._create_bundle("safe_skill", "# clean dependencies\npackaging>=24.0\n")
        report = self.scanner.scan_bundle(bundle)

        self.assertEqual(len(report.osv_vulns), 0)
        self.assertEqual(report.gate_decision, "ALLOW")
        self.assertLess(report.risk_score, 25)


if __name__ == "__main__":
    unittest.main()

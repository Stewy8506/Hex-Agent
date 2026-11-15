"""Tests for deterministic skill verification wiring, sandboxing, minimal env, and SkillSpector hash checks (Fixes F-01)."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.db import LedgerStore
from harness_core.models import TaskSubmitRequest
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.sandbox import RestrictedSubprocessSandbox
from harness_core.skills.executor import (
    SecurityError,
    SkillExecutionEngine,
    SkillVerificationHook,
    build_minimal_skill_env,
)
from harness_core.skillspector import SkillSpectorScanner, compute_bundle_sha256


class TestSkillVerificationWiring(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test_skill_wiring_"))
        self.workspace_root = self.tmp_dir / "workspace"
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.ledger_db = self.tmp_dir / "ledger.db"
        self.ledger = LedgerStore(self.ledger_db)
        self.scanner = SkillSpectorScanner(self.ledger)
        self.engine = SkillExecutionEngine(
            default_workspace_root=self.workspace_root,
            ledger=self.ledger,
            scanner=self.scanner,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_minimal_environment_strips_secrets_and_provider_keys(self):
        """Proof that provider keys, cloud tokens, database URLs, and SSH configs are completely stripped."""
        test_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/Users/test",
            "USER": "tester",
            "OPENAI_API_KEY": "sk-secret-key-12345",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "GEMINI_API_KEY": "AIzaSySecret",
            "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
            "SSH_AUTH_SOCK": "/private/tmp/com.apple.launchd.xxx/Listeners",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "GITHUB_TOKEN": "ghp_1234567890abcdef",
        }

        sanitized = build_minimal_skill_env(base_env=test_env)

        # Safe system vars retained
        self.assertEqual(sanitized.get("PATH"), "/usr/bin:/bin")
        self.assertEqual(sanitized.get("HOME"), "/Users/test")
        self.assertEqual(sanitized.get("USER"), "tester")

        # Secret vars stripped
        self.assertNotIn("OPENAI_API_KEY", sanitized)
        self.assertNotIn("ANTHROPIC_API_KEY", sanitized)
        self.assertNotIn("GEMINI_API_KEY", sanitized)
        self.assertNotIn("DATABASE_URL", sanitized)
        self.assertNotIn("SSH_AUTH_SOCK", sanitized)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", sanitized)
        self.assertNotIn("GITHUB_TOKEN", sanitized)

        # Hook env_vars attempting to inject secrets are also stripped
        malicious_hook_vars = {
            "OPENAI_API_KEY": "injected-key",
            "SAFE_VAR": "safe_value",
        }
        sanitized2 = build_minimal_skill_env(
            base_env=test_env, env_vars=malicious_hook_vars
        )
        self.assertNotIn("OPENAI_API_KEY", sanitized2)
        self.assertEqual(sanitized2.get("SAFE_VAR"), "safe_value")

    def test_run_skill_verification_executes_with_minimal_env(self):
        """Test verification execution cannot observe sensitive provider keys in os.environ."""
        # Inject provider key into current process environment
        os.environ["OPENAI_API_KEY"] = "must-be-stripped"
        try:
            hook = SkillVerificationHook(
                command="python3 -c \"import os, sys; sys.exit(1 if 'OPENAI_API_KEY' in os.environ else 0)\"",
                must_pass=True,
            )
            res = self.engine.run_skill_verification(
                hook=hook,
                workspace_root=self.workspace_root,
                skill_name="env-clean-skill",
            )
            self.assertTrue(res.passed, f"Verification failed with: {res.stderr}")
            self.assertEqual(res.exit_code, 0)
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    def test_run_skill_verification_in_sandbox(self):
        """Test verification execution using RestrictedSubprocessSandbox."""
        sandbox = RestrictedSubprocessSandbox(allowed_root=self.workspace_root)
        hook = SkillVerificationHook(
            command="python3 -c \"print('SANDBOX_OK')\"",
            must_pass=True,
        )
        res = self.engine.run_skill_verification(
            hook=hook,
            workspace_root=self.workspace_root,
            skill_name="sandbox-skill",
            sandbox=sandbox,
        )
        self.assertTrue(res.passed)
        self.assertIn("SANDBOX_OK", res.stdout)

    def test_tampered_skill_bundle_triggers_hash_verification_failure(self):
        """Proof that any mutation to SKILL.md or bundle files after scanning fails hash verification."""
        skill_dir = self.tmp_dir / "verified_skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            """---
name: verified-math
purpose: "Provide verified math operations"
risk_class: R1
allowed_tools:
  - run_tests
verify:
  command: "python3 -c \\"print('MATH_OK')\\""
  must_pass: true
---
# Math Skill Body
""",
            encoding="utf-8",
        )

        # 1. Scan and register skill properly into ledger
        reg = self.scanner.scan_and_register_skill(skill_dir)
        self.assertEqual(reg["status"], "active")
        approved_sha = reg["bundle_sha256"]

        hook = self.engine.extract_verification_hook(skill_dir)
        self.assertIsNotNone(hook)

        # 2. Run verification on untampered bundle -> succeeds
        res = self.engine.run_skill_verification(
            hook=hook,
            workspace_root=self.workspace_root,
            skill_name="verified-math",
            skill_bundle_dir=skill_dir,
            ledger=self.ledger,
        )
        self.assertTrue(res.passed)

        # 3. Tamper with SKILL.md content
        skill_md.write_text(
            skill_md.read_text(encoding="utf-8") + "\n# Tampered line\n",
            encoding="utf-8",
        )

        # 4. Run verification on tampered bundle -> SecurityError raised
        with self.assertRaises(SecurityError) as ctx:
            self.engine.run_skill_verification(
                hook=hook,
                workspace_root=self.workspace_root,
                skill_name="verified-math",
                skill_bundle_dir=skill_dir,
                ledger=self.ledger,
            )
        self.assertIn("bundle hash mismatch", str(ctx.exception).lower())

    def test_unapproved_skill_bundle_raises_security_error(self):
        """Proof that an unvetted / unregistered skill bundle is rejected."""
        unvetted_dir = self.tmp_dir / "unvetted_skill"
        unvetted_dir.mkdir(parents=True, exist_ok=True)
        (unvetted_dir / "SKILL.md").write_text(
            """---
name: unvetted-skill
verify:
  command: "echo ok"
---
# Unvetted
""",
            encoding="utf-8",
        )
        hook = self.engine.extract_verification_hook(unvetted_dir)
        with self.assertRaises(SecurityError) as ctx:
            self.engine.run_skill_verification(
                hook=hook,
                workspace_root=self.workspace_root,
                skill_name="unvetted-skill",
                skill_bundle_dir=unvetted_dir,
                ledger=self.ledger,
            )
        self.assertIn("no approved registration", str(ctx.exception).lower())

    def test_orchestrator_gates_task_completion_on_skill_verification(self):
        """End-to-end proof: must_pass skill verify hook failure blocks SUCCEEDED in orchestrator."""
        orchestrator = HarnessOrchestrator(
            default_workspace=self.workspace_root,
            data_dir=self.tmp_dir / "harness_data",
        )

        # Allocate attempt_ws inside orchestrator's workspaces_root for sandbox containment
        attempt_ws = orchestrator.workspaces_root / "test_attempt_001"
        attempt_ws.mkdir(parents=True, exist_ok=True)

        # Create a registered skill with a failing verify: hook
        failing_skill_dir = self.tmp_dir / "failing_skill"
        failing_skill_dir.mkdir(parents=True, exist_ok=True)
        (failing_skill_dir / "SKILL.md").write_text(
            """---
name: regression-gate-skill
purpose: "Regression gate testing"
risk_class: R1
allowed_tools:
  - run_tests
verify:
  command: "python3 -c \\"import sys; sys.stderr.write('REGRESSION_DETECTOR_FAILED\\\\n'); sys.exit(2)\\""
  must_pass: true
---
# Regression gate skill
""",
            encoding="utf-8",
        )
        orchestrator.scanner.scan_and_register_skill(failing_skill_dir)

        # Submit task in orchestrator ledger
        submitted = orchestrator.submit_task(
            TaskSubmitRequest(
                goal="test skill gate",
                constraints={"skills": ["regression-gate-skill"]},
            )
        )
        task_id = submitted["task_id"]

        # Run verification check via _run_skill_verifications
        outcome = orchestrator._run_skill_verifications(
            task_id=task_id,
            attempt_id="att_001",
            attempt_ws=attempt_ws,
            modified_files=["app.py"],
            task={
                "task_id": task_id,
                "constraints": {"skills": ["regression-gate-skill"]},
            },
            workspace_tree_sha="000000000000",
        )

        self.assertFalse(outcome["passed"])
        self.assertEqual(outcome["failed_skill"], "regression-gate-skill")
        self.assertIn("REGRESSION_DETECTOR_FAILED", outcome["result"]["stderr"])

        # Check ledger recorded event
        events = orchestrator.ledger.list_events(task_id)
        gate_events = [e for e in events if e.get("event_type") == "SKILL_VERIFICATION_FAILED"]
        self.assertTrue(len(gate_events) > 0)
        self.assertEqual(gate_events[0]["reason_code"], "SKILL_HOOK_FAILED")

        # Now test passing skill hook
        passing_skill_dir = self.tmp_dir / "passing_skill"
        passing_skill_dir.mkdir(parents=True, exist_ok=True)
        (passing_skill_dir / "SKILL.md").write_text(
            """---
name: passing-gate-skill
purpose: "Passing regression gate testing"
risk_class: R1
allowed_tools:
  - run_tests
verify:
  command: "python3 -c \\"print('ALL_GOOD')\\""
  must_pass: true
---
# Passing gate skill
""",
            encoding="utf-8",
        )
        orchestrator.scanner.scan_and_register_skill(passing_skill_dir)

        submitted2 = orchestrator.submit_task(
            TaskSubmitRequest(
                goal="test passing skill gate",
                constraints={"skills": ["passing-gate-skill"]},
            )
        )
        task_id2 = submitted2["task_id"]

        pass_outcome = orchestrator._run_skill_verifications(
            task_id=task_id2,
            attempt_id="att_002",
            attempt_ws=attempt_ws,
            modified_files=["app.py"],
            task={
                "task_id": task_id2,
                "constraints": {"skills": ["passing-gate-skill"]},
            },
            workspace_tree_sha="111111111111",
        )
        self.assertTrue(pass_outcome["passed"])


if __name__ == "__main__":
    unittest.main()

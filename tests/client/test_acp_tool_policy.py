"""Revision-aware per-tool policy APIs and gateway guardrail regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness_core.config_loader import parse_config_yaml
from harness_core.core.models import PolicyDecisionType
from harness_core.gateway import ToolGateway
from harness_core.storage.db import LedgerStore
from hex_cli.acp import ACPServer


def _req(id_, method, params=None):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


class TestACPToolPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_file = self.root / "config.yaml"
        self.ledger = LedgerStore(self.root / "ledger.db")
        self.gateway = ToolGateway(self.ledger)
        self.server = ACPServer(
            orchestrator=SimpleNamespace(gateway=self.gateway, ledger=self.ledger),
            workspace_root=self.root,
            config_dir=self.root,
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def policy_get(self):
        response = self.server.handle_request(_req(1, "tools/policy/get"))
        self.assertIn("result", response, response)
        return response["result"]

    def policy_set(self, updates, revision=None):
        response = self.server.handle_request(
            _req(
                2,
                "tools/policy/set",
                {
                    "updates": updates,
                    "expectedRevision": revision or self.policy_get()["revision"],
                },
            )
        )
        self.assertIn("result", response, response)
        return response["result"]

    def test_get_reports_effective_defaults_and_fixed_r4_deny(self) -> None:
        result = self.policy_get()
        tools = {tool["name"]: tool for tool in result["tools"]}

        self.assertEqual(tools["read_file"]["policy_mode"], "allow")
        self.assertTrue(tools["read_file"]["policy_default"])
        self.assertTrue(tools["read_file"]["policy_writable"])
        self.assertEqual(tools["write_file"]["policy_mode"], "allow")
        self.assertEqual(tools["git_push_remote"]["policy_mode"], "ask")
        self.assertEqual(tools["modify_system_policy"]["policy_mode"], "deny")
        self.assertFalse(tools["modify_system_policy"]["policy_writable"])
        self.assertTrue(result["revision"].startswith("sha256:"))

    def test_r4_stays_denied_even_if_config_is_manually_changed(self) -> None:
        self.config_file.write_text(
            "tools:\n  policy:\n    modify_system_policy: allow\n", encoding="utf-8"
        )
        tools = {tool["name"]: tool for tool in self.policy_get()["tools"]}
        self.assertEqual(tools["modify_system_policy"]["policy_mode"], "deny")
        self.assertFalse(tools["modify_system_policy"]["policy_writable"])
        decision, _ = self.gateway.evaluate_policy(
            {"task_id": "task", "execution_mode": "standard"},
            self.gateway.tools["modify_system_policy"],
            {"policy_key": "audit"},
        )
        self.assertEqual(decision, PolicyDecisionType.DENY)

    def test_set_persists_and_immediately_changes_gateway_policy(self) -> None:
        initial = self.policy_get()
        result = self.policy_set(
            {"write_file": "ask", "git_push_remote": "allow"}, initial["revision"]
        )
        tools = {tool["name"]: tool for tool in result["tools"]}

        self.assertEqual(result["changed"], ["git_push_remote", "write_file"])
        self.assertNotEqual(result["revision"], initial["revision"])
        self.assertEqual(tools["write_file"]["policy_mode"], "ask")
        self.assertFalse(tools["write_file"]["policy_default"])
        self.assertEqual(tools["git_push_remote"]["policy_mode"], "allow")
        self.assertEqual(
            parse_config_yaml(self.config_file)["tools"]["policy"],
            {"write_file": "ask", "git_push_remote": "allow"},
        )

        task = {"task_id": "task-policy", "execution_mode": "standard"}
        decision, _ = self.gateway.evaluate_policy(
            task, self.gateway.tools["write_file"], {"path": "a.txt", "content": "x"}
        )
        self.assertEqual(decision, PolicyDecisionType.REQUIRE_APPROVAL)
        decision, _ = self.gateway.evaluate_policy(
            task, self.gateway.tools["git_push_remote"], {"branch": "main"}
        )
        self.assertEqual(decision, PolicyDecisionType.ALLOW)

    def test_reset_removes_only_the_selected_override(self) -> None:
        self.config_file.write_text(
            "model:\n  cloud_provider: anthropic\n"
            "tools:\n  other: keep\n  policy:\n    read_file: deny\n    write_file: ask\n",
            encoding="utf-8",
        )
        result = self.policy_set({"read_file": "default"})
        config = parse_config_yaml(self.config_file)

        self.assertEqual(result["changed"], ["read_file"])
        self.assertEqual(config["model"], {"cloud_provider": "anthropic"})
        self.assertEqual(config["tools"]["other"], "keep")
        self.assertEqual(config["tools"]["policy"], {"write_file": "ask"})
        tools = {tool["name"]: tool for tool in result["tools"]}
        self.assertEqual(tools["read_file"]["policy_mode"], "allow")
        self.assertTrue(tools["read_file"]["policy_default"])

    def test_deny_precedes_approval_leases_and_hard_guardrails_still_win(self) -> None:
        self.policy_set({"read_file": "deny", "write_file": "allow", "git_push_remote": "allow"})
        self.gateway.approval_lease_manager = SimpleNamespace(check_lease=lambda *a, **k: (True, "lease"))

        decision, reason = self.gateway.evaluate_policy(
            {"task_id": "task", "execution_mode": "standard"},
            self.gateway.tools["read_file"],
            {"path": "a.txt"},
        )
        self.assertEqual(decision, PolicyDecisionType.DENY)
        self.assertIn("tool policy", reason)

        decision, _ = self.gateway.evaluate_policy(
            {"task_id": "task", "execution_mode": "standard", "constraints": {"allowed_tools": ["read_file"]}},
            self.gateway.tools["write_file"],
            {"path": "a.txt", "content": "x"},
        )
        self.assertEqual(decision, PolicyDecisionType.DENY)

        decision, _ = self.gateway.evaluate_policy(
            {"task_id": "task", "execution_mode": "fast_path"},
            self.gateway.tools["write_file"],
            {"path": "a.txt", "content": "x"},
        )
        self.assertEqual(decision, PolicyDecisionType.DENY)

        decision, _ = self.gateway.evaluate_policy(
            {"task_id": "task", "execution_mode": "standard"},
            self.gateway.tools["modify_system_policy"],
            {"policy_key": "audit"},
        )
        self.assertEqual(decision, PolicyDecisionType.DENY)

    def test_allow_does_not_bypass_global_user_memory_approval(self) -> None:
        self.policy_set({"memory": "allow"})
        decision, reason = self.gateway.evaluate_policy(
            {"task_id": "task", "execution_mode": "standard"},
            self.gateway.tools["memory"],
            {"target": "user", "action": "add", "content": "remember this"},
        )
        self.assertEqual(decision, PolicyDecisionType.REQUIRE_APPROVAL)
        self.assertIn("global user profile", reason)

    def test_stale_unknown_invalid_and_r4_updates_are_rejected(self) -> None:
        initial = self.policy_get()
        stale = self.server.handle_request(
            _req(
                2,
                "tools/policy/set",
                {"updates": {"read_file": "deny"}, "expectedRevision": "sha256:stale"},
            )
        )
        self.assertEqual(stale["error"]["code"], -32001)

        for updates in (
            {"missing_tool": "deny"},
            {"read_file": "sometimes"},
            {"read_file": True},
            {"modify_system_policy": "allow"},
        ):
            response = self.server.handle_request(
                _req(
                    3,
                    "tools/policy/set",
                    {"updates": updates, "expectedRevision": initial["revision"]},
                )
            )
            self.assertEqual(response["error"]["code"], -32602, response)
        self.assertFalse(self.config_file.exists())

    def test_malformed_policy_config_is_reported_instead_of_silently_ignored(self) -> None:
        self.config_file.write_text("tools:\n  policy:\n    read_file: unsafe\n", encoding="utf-8")
        response = self.server.handle_request(_req(1, "tools/policy/get"))
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32000)
        self.assertIn("tool policy mode", response["error"]["message"])


if __name__ == "__main__":
    unittest.main()

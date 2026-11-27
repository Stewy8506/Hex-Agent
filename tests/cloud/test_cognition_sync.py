"""Personal cognition sync, conflict, and skill-gating tests."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cloud.cognition_sync import CognitionSyncStore, _bundle_hash
from cloud.sync_contract import canonical_json_bytes
from harness_core.storage.db import LedgerStore


class CognitionSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="hex_cognition_sync_")
        self.root = Path(self.temporary.name)
        self.source_workspace = self.root / "source-workspace"
        self.target_workspace = self.root / "target-workspace"
        self.source_home = self.root / "source-home"
        self.target_home = self.root / "target-home"
        for path in (self.source_workspace, self.target_workspace, self.source_home, self.target_home):
            path.mkdir()
        self.ledger = LedgerStore(self.target_home / "ledger.db")
        self.source = CognitionSyncStore(
            self.source_workspace,
            global_home=self.source_home,
            workspace_id="workspace-a",
        )
        self.target = CognitionSyncStore(
            self.target_workspace,
            global_home=self.target_home,
            workspace_id="workspace-a",
            ledger=self.ledger,
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def _write_document(self, store: CognitionSyncStore, document_id: str, content: str) -> Path:
        path, _root = store._document_path(document_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _write_skill(
        self,
        store: CognitionSyncStore,
        name: str,
        *,
        body: str = "Follow the documented procedure.",
        script: str | None = None,
    ) -> Path:
        bundle = store.global_home / "skills" / name
        if bundle.exists():
            shutil.rmtree(bundle)
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "SKILL.md").write_text(
            "---\n"
            f"name: {name.rsplit('/', 1)[-1]}\n"
            "description: Perform the documented workflow.\n"
            "purpose: Perform the documented workflow.\n"
            "risk_class: R1\n"
            "---\n"
            f"# {name}\n\n{body}\n",
            encoding="utf-8",
        )
        if script is not None:
            (bundle / "scripts").mkdir()
            (bundle / "scripts" / "run.py").write_text(script, encoding="utf-8")
        return bundle

    def _sync(self) -> dict:
        return self.target.apply_manifest(
            self.source.export_manifest(), sender_device_record_id="dev_source"
        )

    def _resign(self, payload: dict) -> None:
        unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
        payload["manifest_sha256"] = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()

    def test_documents_round_trip_and_delete_with_recoverable_backup(self) -> None:
        expected = {
            "global/user": "User prefers concise updates.\n",
            "global/soul": "Direct and empirical.\n",
            "workspace/memory": "Project uses a ledger.\n",
            "workspace/guardrails": "- [AVOID] Never overwrite local work.\n",
        }
        for document_id, content in expected.items():
            self._write_document(self.source, document_id, content)
        first = self.source.export_manifest()
        result = self.target.apply_manifest(first, sender_device_record_id="dev_source")
        self.assertEqual(result["status"], "applied")
        for document_id, content in expected.items():
            self.assertEqual(self.target._read_document(document_id), content.encode())

        memory_path = self._write_document(self.source, "workspace/memory", "")
        memory_path.unlink()
        deleted = self.source.export_manifest()
        self.assertGreater(deleted["revision"], first["revision"])
        result = self.target.apply_manifest(deleted, sender_device_record_id="dev_source")
        self.assertEqual(result["status"], "applied")
        self.assertIsNone(self.target._read_document("workspace/memory"))
        backups = list((self.target_home / ".sync" / "backups").rglob("*"))
        self.assertTrue(any(path.is_file() and path.read_text() == expected["workspace/memory"] for path in backups))

    def test_concurrent_document_edits_preserve_both_versions(self) -> None:
        self._write_document(self.source, "global/user", "shared\n")
        self._sync()
        self._write_document(self.source, "global/user", "source version\n")
        self._write_document(self.target, "global/user", "target version\n")

        result = self._sync()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(self.target._read_document("global/user"), b"target version\n")
        conflict = Path(result["conflicts"][0]["path"])
        self.assertEqual((conflict / "remote.md").read_text(), "source version\n")
        metadata = json.loads((conflict / "conflict.json").read_text())
        self.assertEqual(metadata["local_sha256"], hashlib.sha256(b"target version\n").hexdigest())

        reverse = self.source.apply_manifest(
            self.target.export_manifest(), sender_device_record_id="dev_target"
        )
        self.assertEqual(reverse["status"], "conflict")
        self.assertEqual(self.source._read_document("global/user"), b"source version\n")

    def test_concurrent_deletion_does_not_remove_local_edit(self) -> None:
        self._write_document(self.source, "workspace/memory", "shared\n")
        self._sync()
        self._write_document(self.target, "workspace/memory", "local edit\n")
        (self.source_workspace / ".hex" / "memories" / "MEMORY.md").unlink()

        result = self._sync()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(self.target._read_document("workspace/memory"), b"local edit\n")
        metadata = json.loads((Path(result["conflicts"][0]["path"]) / "conflict.json").read_text())
        self.assertTrue(metadata["deleted"])

    def test_legacy_workspace_memory_location_is_synchronized(self) -> None:
        legacy = self.source_workspace / ".hex" / "MEMORY.md"
        legacy.parent.mkdir()
        legacy.write_text("legacy memory\n")
        self._sync()
        self.assertEqual(
            (self.target_workspace / ".hex" / "memories" / "MEMORY.md").read_text(),
            "legacy memory\n",
        )

    def test_clean_skill_activates_and_registers_intended_path(self) -> None:
        bundle = self._write_skill(
            self.source,
            "clean-skill",
            script="def add(a, b):\n    return a + b\n",
        )
        (bundle / "scripts" / "run.py").chmod(0o755)
        result = self._sync()
        skill_result = next(item for item in result["skills"] if item["id"] == "skill:clean-skill")
        self.assertEqual(skill_result["status"], "active")
        self.assertEqual(skill_result["gate_decision"], "ALLOW")
        active = self.target_home / "skills" / "clean-skill"
        self.assertTrue((active / "SKILL.md").is_file())
        self.assertTrue(os.access(active / "scripts" / "run.py", os.X_OK))
        row = self.ledger.conn.execute(
            "SELECT disk_path, status FROM skills WHERE name = 'clean-skill'"
        ).fetchone()
        self.assertEqual(row["disk_path"], str(active.resolve()))
        self.assertEqual(row["status"], "active")

    def test_bundle_hash_matches_skillspector_traversal_order(self) -> None:
        bundle = self._write_skill(self.source, "ordered-skill")
        (bundle / "a-b").mkdir()
        (bundle / "a-b" / "data.txt").write_text("nested")
        (bundle / "a.txt").write_text("root")
        result = self._sync()
        self.assertEqual(result["status"], "applied")
        self.assertTrue((self.target_home / "skills" / "ordered-skill" / "a-b" / "data.txt").is_file())

    def test_failed_skill_install_restores_previous_active_bundle(self) -> None:
        self._write_skill(self.source, "rollback-skill", body="Original version.")
        self._sync()
        self._write_skill(self.source, "rollback-skill", body="Replacement version.")
        with mock.patch.object(self.target, "_install_directory", side_effect=OSError("simulated")):
            with self.assertRaisesRegex(OSError, "simulated"):
                self._sync()
        active = self.target_home / "skills" / "rollback-skill" / "SKILL.md"
        self.assertIn("Original version.", active.read_text())
        result = self._sync()
        self.assertEqual(result["status"], "applied")
        self.assertIn("Replacement version.", active.read_text())

    def test_approval_and_quarantine_gates_never_activate_skills(self) -> None:
        self._write_skill(self.source, "approval-skill", body="trigger: always")
        self._write_skill(
            self.source,
            "dangerous-skill",
            script="import os\nos.system('echo unsafe')\n",
        )
        result = self._sync()
        statuses = {item["id"]: item["status"] for item in result["skills"] if "status" in item}
        self.assertEqual(statuses["skill:approval-skill"], "candidate")
        self.assertEqual(statuses["skill:dangerous-skill"], "quarantined")
        self.assertFalse((self.target_home / "skills" / "approval-skill").exists())
        self.assertFalse((self.target_home / "skills" / "dangerous-skill").exists())
        self.assertTrue((self.target_home / ".sync" / "candidates" / "approval-skill" / "SKILL.md").is_file())
        self.assertTrue((self.target_home / ".sync" / "quarantine" / "dangerous-skill" / "scripts" / "run.py").is_file())
        rows = self.ledger.conn.execute("SELECT name, status FROM skills").fetchall()
        self.assertEqual({row["name"]: row["status"] for row in rows}, {
            "approval-skill": "candidate",
            "dangerous-skill": "quarantined",
        })

    def test_gated_skill_update_uses_quarantined_base_and_can_later_activate(self) -> None:
        self._write_skill(self.source, "changing-skill", body="Safe first version.")
        self._sync()
        self._write_skill(
            self.source,
            "changing-skill",
            body="Unsafe second version.",
            script="eval('2 + 2')\n",
        )
        second = self._sync()
        self.assertEqual(second["status"], "applied")
        self.assertFalse((self.target_home / "skills" / "changing-skill").exists())
        self.assertTrue((self.target_home / ".sync" / "quarantine" / "changing-skill").exists())

        self._write_skill(self.source, "changing-skill", body="Safe third version.")
        third = self._sync()
        self.assertEqual(third["status"], "applied")
        self.assertTrue((self.target_home / "skills" / "changing-skill" / "SKILL.md").is_file())
        self.assertIn("Safe third version.", (self.target_home / "skills" / "changing-skill" / "SKILL.md").read_text())

    def test_concurrent_skill_edits_are_preserved_outside_active_directory(self) -> None:
        self._write_skill(self.source, "conflict-skill", body="Shared version.")
        self._sync()
        self._write_skill(self.source, "conflict-skill", body="Source version.")
        self._write_skill(self.target, "conflict-skill", body="Target version.")

        result = self._sync()
        self.assertEqual(result["status"], "conflict")
        active = self.target_home / "skills" / "conflict-skill" / "SKILL.md"
        self.assertIn("Target version.", active.read_text())
        conflict = Path(result["conflicts"][0]["path"])
        self.assertIn("Source version.", (conflict / "bundle" / "SKILL.md").read_text())
        self.assertFalse((conflict / "bundle" / "SKILL.md").samefile(active))

    def test_skill_deletion_retires_active_candidate_and_quarantine_copies(self) -> None:
        self._write_skill(self.source, "deleted-skill")
        self._sync()
        (self.source_home / "skills" / "deleted-skill" / "SKILL.md").unlink()
        (self.source_home / "skills" / "deleted-skill").rmdir()
        result = self._sync()
        self.assertEqual(result["status"], "applied")
        self.assertFalse((self.target_home / "skills" / "deleted-skill").exists())
        backups = list((self.target_home / ".sync" / "backups" / "workspace-a" / "skills").rglob("SKILL.md"))
        self.assertTrue(backups)
        row = self.ledger.conn.execute("SELECT status FROM skills WHERE name = 'deleted-skill'").fetchone()
        self.assertEqual(row["status"], "disabled")

    def test_unchanged_exports_are_byte_for_byte_stable(self) -> None:
        self._write_document(self.source, "global/user", "stable\n")
        self._write_skill(self.source, "stable-skill")
        first = self.source.export_manifest()
        second = self.source.export_manifest()
        self.assertEqual(first, second)
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))

    def test_replay_is_idempotent_and_revision_fork_is_rejected(self) -> None:
        self._write_document(self.source, "global/user", "first\n")
        manifest = self.source.export_manifest()
        self.target.apply_manifest(manifest, sender_device_record_id="dev_source")
        replay = self.target.apply_manifest(manifest, sender_device_record_id="dev_source")
        self.assertEqual(replay["status"], "unchanged")

        fork = copy.deepcopy(manifest)
        content = b"forked\n"
        fork["files"][0].update({
            "content_base64": base64.b64encode(content).decode(),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
        self._resign(fork)
        with self.assertRaisesRegex(ValueError, "revision conflicts"):
            self.target.apply_manifest(fork, sender_device_record_id="dev_source")
        self.assertEqual(self.target._read_document("global/user"), b"first\n")

    def test_workspace_mismatch_is_rejected(self) -> None:
        self._write_document(self.source, "global/user", "private\n")
        manifest = self.source.export_manifest()
        other_workspace = CognitionSyncStore(
            self.target_workspace,
            global_home=self.target_home,
            workspace_id="workspace-b",
        )
        with self.assertRaisesRegex(ValueError, "different workspace"):
            other_workspace.apply_manifest(manifest, sender_device_record_id="dev_source")
        self.assertIsNone(self.target._read_document("global/user"))

    def test_path_traversal_and_absolute_paths_are_rejected_before_any_write(self) -> None:
        self._write_document(self.source, "global/user", "remote\n")
        self._write_skill(self.source, "safe-skill")
        for malicious_path in ("../escape.md", "/tmp/escape.md", "scripts/../../escape.md", "scripts\\escape.md"):
            with self.subTest(path=malicious_path):
                manifest = self.source.export_manifest()
                manifest["skills"][0]["files"].append({
                    "path": malicious_path,
                    "content_base64": base64.b64encode(b"escape").decode(),
                    "executable": False,
                })
                self._resign(manifest)
                with self.assertRaises(ValueError):
                    self.target.apply_manifest(manifest, sender_device_record_id="dev_source")
                self.assertIsNone(self.target._read_document("global/user"))
                self.assertFalse((self.target_home / "skills" / "safe-skill").exists())

    def test_duplicate_casefolded_bundle_paths_are_rejected(self) -> None:
        self._write_skill(self.source, "safe-skill")
        manifest = self.source.export_manifest()
        manifest["skills"][0]["files"].append({
            "path": "skill.md",
            "content_base64": base64.b64encode(b"duplicate").decode(),
            "executable": False,
        })
        self._resign(manifest)
        with self.assertRaisesRegex(ValueError, "duplicate paths"):
            self.target.apply_manifest(manifest, sender_device_record_id="dev_source")

    def test_file_directory_prefix_collision_is_rejected(self) -> None:
        self._write_skill(self.source, "safe-skill")
        manifest = self.source.export_manifest()
        manifest["skills"][0]["files"].extend([
            {
                "path": "scripts",
                "content_base64": base64.b64encode(b"file").decode(),
                "executable": False,
            },
            {
                "path": "scripts/run.py",
                "content_base64": base64.b64encode(b"nested").decode(),
                "executable": False,
            },
        ])
        self._resign(manifest)
        with self.assertRaisesRegex(ValueError, "file and directory"):
            self.target.apply_manifest(manifest, sender_device_record_id="dev_source")

    def test_document_and_bundle_tampering_are_rejected(self) -> None:
        self._write_document(self.source, "global/user", "authentic\n")
        self._write_skill(self.source, "safe-skill")
        manifest = self.source.export_manifest()
        manifest["files"][0]["content_base64"] = base64.b64encode(b"tampered\n").decode()
        self._resign(manifest)
        with self.assertRaisesRegex(ValueError, "document hash mismatch"):
            self.target.apply_manifest(manifest, sender_device_record_id="dev_source")

        manifest = self.source.export_manifest()
        manifest["skills"][0]["files"][0]["content_base64"] = base64.b64encode(b"tampered").decode()
        self._resign(manifest)
        with self.assertRaisesRegex(ValueError, "bundle hash mismatch"):
            self.target.apply_manifest(manifest, sender_device_record_id="dev_source")
        self.assertFalse((self.target_home / "skills" / "safe-skill").exists())

    def test_manifest_tampering_is_rejected(self) -> None:
        self._write_document(self.source, "global/user", "authentic\n")
        manifest = self.source.export_manifest()
        manifest["revision"] += 1
        with self.assertRaisesRegex(ValueError, "manifest hash mismatch"):
            self.target.apply_manifest(manifest, sender_device_record_id="dev_source")

    def test_symlink_escape_in_source_skill_is_rejected(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("must not be exported")
        bundle = self._write_skill(self.source, "linked-skill")
        (bundle / "escape.txt").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.source.export_manifest()

    def test_managed_document_symlink_escape_is_rejected(self) -> None:
        outside = self.root / "outside-user.md"
        outside.write_text("outside")
        (self.source_home / "USER.md").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.source.export_manifest()

    def test_skill_frontmatter_must_match_bundle_identity(self) -> None:
        self._write_skill(self.source, "safe-skill")
        manifest = self.source.export_manifest()
        skill_file = next(item for item in manifest["skills"][0]["files"] if item["path"] == "SKILL.md")
        text = base64.b64decode(skill_file["content_base64"]).decode().replace("name: safe-skill", "name: other-skill")
        skill_file["content_base64"] = base64.b64encode(text.encode()).decode()
        files = [
            (item["path"], base64.b64decode(item["content_base64"]), item["executable"])
            for item in manifest["skills"][0]["files"]
        ]
        manifest["skills"][0]["sha256"] = _bundle_hash(files)
        self._resign(manifest)
        with self.assertRaisesRegex(ValueError, "uniquely match"):
            self.target.apply_manifest(manifest, sender_device_record_id="dev_source")


if __name__ == "__main__":
    unittest.main()

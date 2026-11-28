"""Signed Teams fast-lane events and durable blackboard state."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cloud.p2p_transport import PeerIdentity
from cloud.team_fastlane import (
    TeamActivityError,
    TeamActivityStore,
    make_activity_event,
    sign_activity_event,
    verify_activity_event,
)


class TeamFastLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.alice = PeerIdentity.generate("dev_alice")
        self.bob = PeerIdentity.generate("dev_bob")
        self.now = int(time.time())
        self.store = TeamActivityStore(self.root, "proj_team")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def event(self, signer=None, **overrides):
        signer = signer or self.alice
        values = {
            "kind": "INTENT_CLAIM",
            "summary": "Edit parser",
            "paths": ["src/parser.py"],
            "symbols": ["Parser.parse"],
            "data": {"task_id": "task_a", "claim_id": "claim_a", "ttl_seconds": 300},
            "created_at": self.now,
        }
        values.update(overrides)
        return make_activity_event(
            signer=signer,
            project_session_id="proj_team",
            host_epoch=3,
            **values,
        )

    def accept(self, envelope, signer=None):
        signer = signer or (self.bob if envelope["event"]["author_device_record_id"] == self.bob.device_id else self.alice)
        return self.store.accept(envelope, signer.signing_public_key, now=self.now)

    def test_scope_claims_expire_and_detect_only_real_overlaps(self) -> None:
        self.accept(self.event())
        self.accept(self.event(
            signer=self.bob,
            summary="Edit adjacent parser code",
            paths=["src/parser.py"],
            symbols=["Parser.parse"],
            data={"task_id": "task_b", "claim_id": "claim_b", "ttl_seconds": 120},
        ))
        self.accept(self.event(
            signer=self.bob,
            summary="Edit unrelated code",
            paths=["src/ui.py"],
            symbols=["Renderer.draw"],
            data={"task_id": "task_c", "claim_id": "claim_c", "ttl_seconds": 120},
        ))

        locks = {item["data"]["task_id"]: item for item in self.store.scope_locks(now=self.now, host_epoch=3)}
        self.assertTrue(locks["task_a"]["conflicts"])
        self.assertTrue(locks["task_b"]["conflicts"])
        self.assertFalse(locks["task_c"]["conflicts"])
        self.assertEqual(locks["task_a"]["expires_at"], self.now + 300)
        self.assertEqual(self.store.scope_locks(now=self.now + 301, host_epoch=3), [])

    def test_scope_release_must_match_the_active_claim(self) -> None:
        self.accept(self.event())
        self.accept(self.event(
            kind="SCOPE_RELEASE",
            summary="Release wrong claim",
            data={"task_id": "task_a", "claim_id": "claim_wrong"},
        ))
        self.assertEqual(len(self.store.scope_locks(now=self.now, host_epoch=3)), 1)
        self.accept(self.event(
            kind="SCOPE_RELEASE",
            summary="Release parser scope",
            data={"task_id": "task_a", "claim_id": "claim_a"},
        ))
        self.assertEqual(self.store.scope_locks(now=self.now, host_epoch=3), [])

    def test_epoch_fencing_ignores_stale_claims_and_peer_states(self) -> None:
        self.accept(self.event())
        self.accept(self.event(
            kind="PEER_STATE",
            summary="Planning",
            paths=[],
            symbols=[],
            data={"state": "PLANNING"},
        ))
        self.assertEqual(self.store.scope_locks(now=self.now, host_epoch=4), [])
        self.assertEqual(self.store.peer_activity(host_epoch=4), [])
        self.assertEqual(self.store.peer_activity(host_epoch=3)[0]["data"]["state"], "PLANNING")

    def test_blackboard_prioritizes_pinned_notes_and_caches_query_answers(self) -> None:
        self.accept(self.event(
            kind="BLACKBOARD_NOTE",
            summary="API invariant",
            paths=["src/api.py"],
            symbols=[],
            data={"task_id": None, "body": "Keep the parser deterministic.", "pinned": True},
        ))
        self.accept(self.event(
            signer=self.bob,
            kind="PEER_QUERY_ANSWER",
            summary="Answered parser question",
            paths=[],
            symbols=[],
            data={
                "request_id": "a" * 32,
                "question": "Which parser is canonical?",
                "answer": "src/parser.py",
                "peer_device_record_id": self.bob.device_id,
            },
        ))
        notes = self.store.blackboard()
        self.assertEqual([item["kind"] for item in notes], ["BLACKBOARD_NOTE", "PEER_QUERY_ANSWER"])
        self.assertEqual(notes[0]["data"]["body"], "Keep the parser deterministic.")

    def test_contract_change_normalizes_paths_and_symbols(self) -> None:
        envelope = self.event(
            kind="CONTRACT_CHANGE",
            summary="Rename parser entry point",
            paths=["src\\api.py"],
            symbols=[],
            data={
                "task_id": "task_a", "path": "src/api.py", "symbol": "parse",
                "before": "parse(text)", "after": "parse(text, strict=False)",
            },
        )
        self.accept(envelope)
        event = verify_activity_event(
            envelope, project_session_id="proj_team", public_key=self.alice.signing_public_key, now=self.now
        )
        self.assertEqual(event["paths"], ["src/api.py"])
        self.assertEqual(event["symbols"], ["parse"])
        self.assertEqual(event["data"]["after"], "parse(text, strict=False)")

    def test_signature_session_timestamp_and_author_are_enforced(self) -> None:
        envelope = self.event()
        with self.assertRaisesRegex(TeamActivityError, "signature"):
            self.store.accept(envelope, self.bob.signing_public_key, now=self.now)
        with self.assertRaisesRegex(TeamActivityError, "different session"):
            verify_activity_event(
                envelope, project_session_id="proj_other", public_key=self.alice.signing_public_key, now=self.now
            )
        with self.assertRaisesRegex(TeamActivityError, "acceptance window"):
            self.store.accept(envelope, self.alice.signing_public_key, now=self.now + 301)
        forged = json.loads(json.dumps(envelope))
        forged["event"]["author_device_record_id"] = self.bob.device_id
        with self.assertRaisesRegex(TeamActivityError, "author"):
            sign_activity_event(forged["event"], self.alice, "proj_team")

    def test_duplicate_events_are_idempotent_but_conflicting_ids_are_rejected(self) -> None:
        envelope = self.event()
        self.assertFalse(self.accept(envelope)["duplicate"])
        self.assertTrue(self.accept(envelope)["duplicate"])
        conflicting = json.loads(json.dumps(envelope))
        conflicting["event"]["summary"] = "Different signed event"
        conflicting = sign_activity_event(conflicting["event"], self.alice, "proj_team")
        with self.assertRaisesRegex(TeamActivityError, "Conflicting"):
            self.accept(conflicting)
        self.assertEqual(self.store.status()["events"], 1)

    def test_credential_paths_traversal_and_secret_values_are_rejected_or_redacted(self) -> None:
        for path in (".env", "config/.env.production", "../outside.py", "/absolute.py", "id_rsa"):
            with self.subTest(path=path), self.assertRaises(TeamActivityError):
                self.event(paths=[path])
        envelope = self.event(
            kind="BLACKBOARD_NOTE",
            summary="Configuration note",
            paths=[],
            symbols=[],
            data={
                "task_id": None,
                "body": "provider api_key=abcdefghijklmnop",
                "pinned": False,
            },
        )
        self.accept(envelope)
        self.assertEqual(
            self.store.blackboard()[0]["data"]["body"],
            "provider [REDACTED_SECRET]",
        )

    def test_journal_survives_restart_and_detects_tampering(self) -> None:
        self.accept(self.event())
        restored = TeamActivityStore(self.root, "proj_team")
        self.assertEqual(len(restored.scope_locks(now=self.now, host_epoch=3)), 1)
        entries = [json.loads(line) for line in restored.path.read_text().splitlines()]
        entries[0]["envelope"]["event"]["summary"] = "tampered"
        restored.path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        with self.assertRaisesRegex(TeamActivityError, "malformed"):
            TeamActivityStore(self.root, "proj_team")

    def test_shared_workspace_journal_isolates_sessions_and_serializes_writes(self) -> None:
        other = TeamActivityStore(self.root, "proj_other")

        def write(store: TeamActivityStore, session_id: str, prefix: str) -> None:
            for index in range(10):
                envelope = make_activity_event(
                    signer=self.alice,
                    project_session_id=session_id,
                    host_epoch=1,
                    kind="BLACKBOARD_NOTE",
                    summary=f"{prefix} note {index}",
                    data={
                        "task_id": None,
                        "body": f"{prefix} body {index}",
                        "pinned": False,
                    },
                    created_at=self.now,
                )
                store.accept(
                    envelope, self.alice.signing_public_key, now=self.now
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(write, self.store, "proj_team", "first")
            second = executor.submit(write, other, "proj_other", "other")
            first.result()
            second.result()

        self.assertEqual(self.store.status()["events"], 10)
        self.assertEqual(other.status()["events"], 10)
        self.assertEqual(self.store.status()["journal_events"], 20)
        self.assertTrue(all(
            note["data"]["body"].startswith("first ")
            for note in self.store.blackboard(limit=100)
        ))
        restored = TeamActivityStore(self.root, "proj_other")
        self.assertEqual(restored.status()["journal_events"], 20)
        self.assertTrue(all(
            note["data"]["body"].startswith("other ")
            for note in restored.blackboard(limit=100)
        ))

    def test_event_and_byte_limits_are_enforced(self) -> None:
        store = TeamActivityStore(self.root, "proj_team", event_limit=2, bytes_limit=5000)
        for index in range(4):
            envelope = self.event(
                summary=f"Claim {index}",
                data={"task_id": f"task_{index}", "claim_id": f"claim_{index}", "ttl_seconds": 300},
            )
            store.accept(envelope, self.alice.signing_public_key, now=self.now)
        self.assertEqual(store.status()["events"], 2)
        self.assertLessEqual(store.status()["bytes"], 5000)
        self.assertEqual(len(store.recent_activity(limit=2)), 2)


if __name__ == "__main__":
    unittest.main()

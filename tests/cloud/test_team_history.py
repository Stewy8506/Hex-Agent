"""Regression tests for durable verified team-turn history."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from cloud.p2p_transport import PeerIdentity
from cloud.team_history import TeamHistory
from cloud.team_mesh import TurnCommitBundle, sign_turn_commit


class TeamHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.signer = PeerIdentity.generate("dev_host")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _turn(self, sequence: int):
        bundle = TurnCommitBundle(
            project_session_id="proj_1",
            host_epoch=1,
            turn_seq=sequence,
            cow_delta_sha256="sha256:" + str(sequence).zfill(64),
            ledger_chain_tip="sha256:" + str(sequence + 10).zfill(64),
            memory_delta_sha256="sha256:" + str(sequence + 20).zfill(64),
            proof_merkle_root="sha256:" + str(sequence + 30).zfill(64),
        )
        signed = sign_turn_commit(bundle, self.signer)
        payload = {"version": 1, "sequence": sequence}
        receipt = {"payload": {"turn_seq": sequence}, "signature": "test"}
        return signed, payload, receipt

    def test_exact_turn_and_receipt_survive_restart_with_private_permissions(self) -> None:
        history = TeamHistory(self.root)
        signed, payload, receipt = self._turn(1)

        archived = history.archive(signed, payload, receipt)
        restarted = TeamHistory(self.root)
        records = restarted.history_range("proj_1", 1, 1)

        self.assertEqual(records, [archived])
        self.assertEqual(records[0]["signed"], signed.to_dict())
        self.assertEqual(records[0]["payload"], payload)
        self.assertEqual(records[0]["receipt"], receipt)
        path = next(history.root.glob("*.json"))
        self.assertEqual(stat.S_IMODE(history.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_conflicting_sequence_reuse_is_rejected_without_replacing_history(self) -> None:
        history = TeamHistory(self.root)
        signed, payload, receipt = self._turn(1)
        original = history.archive(signed, payload, receipt)

        with self.assertRaisesRegex(ValueError, "Conflicting team turn"):
            history.archive(signed, {**payload, "changed": True}, receipt)

        self.assertEqual(history.history_range("proj_1", 1, 1), [original])

    def test_retention_keeps_the_newest_exact_turn(self) -> None:
        history = TeamHistory(self.root, history_limit=1)
        first = self._turn(1)
        second = self._turn(2)
        history.archive(*first)
        history.archive(*second)

        self.assertEqual(history.history_range("proj_1", 1, 1), [])
        self.assertEqual(
            [record["signed"]["bundle"]["turn_seq"] for record in history.history_range("proj_1", 1, 2)],
            [2],
        )

    def test_tampered_history_is_never_served(self) -> None:
        history = TeamHistory(self.root)
        history.archive(*self._turn(1))
        path = next(history.root.glob("*.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record["payload"]["sequence"] = 99
        path.write_text(json.dumps(record), encoding="utf-8")

        self.assertEqual(history.history_range("proj_1", 1, 1), [])
        self.assertEqual(history.status()["corrupt_records"], 1)

    def test_invalid_history_ranges_are_rejected(self) -> None:
        history = TeamHistory(self.root)
        for first, last in ((0, 1), (2, 1), (1, 10_001)):
            with self.subTest(first=first, last=last), self.assertRaises(ValueError):
                history.history_range("proj_1", first, last)


if __name__ == "__main__":
    unittest.main()

"""Personal-sync wire contract tests."""

from __future__ import annotations

import json
import unittest

from cloud.sync_contract import (
    SyncEnvelope,
    SyncStream,
    canonical_json_bytes,
    ledger_scope,
    task_id_from_ledger_scope,
)


class SyncContractTests(unittest.TestCase):
    def test_envelope_matches_rust_snake_case_contract(self) -> None:
        envelope = SyncEnvelope(
            SyncStream.LEDGER, ledger_scope("tsk_1"), "dev_a", 42,
            {"version": 1, "events": [{"sequence_no": 43}]},
        )
        raw = json.dumps(envelope.to_dict())
        self.assertEqual(json.loads(raw), {
            "stream": "ledger",
            "scope": "ledger:tsk_1",
            "sender_device_record_id": "dev_a",
            "since_seq": 42,
            "payload": {"version": 1, "events": [{"sequence_no": 43}]},
        })
        self.assertEqual(SyncEnvelope.from_dict(json.loads(raw)), envelope)
        self.assertEqual(task_id_from_ledger_scope(envelope.scope), "tsk_1")

    def test_canonical_json_is_compact_sorted_utf8(self) -> None:
        self.assertEqual(
            canonical_json_bytes({"z": "é", "a": {"b": 2, "a": 1}}),
            '{"a":{"a":1,"b":2},"z":"é"}'.encode(),
        )

    def test_malformed_envelopes_are_rejected(self) -> None:
        valid = {
            "stream": "ledger", "scope": "ledger:tsk_1",
            "sender_device_record_id": "dev_a", "since_seq": 0, "payload": {},
        }
        invalid = [
            {**valid, "stream": "unknown"},
            {**valid, "scope": ""},
            {**valid, "sender_device_record_id": ""},
            {**valid, "since_seq": True},
            {**valid, "since_seq": -1},
            {**valid, "payload": []},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                SyncEnvelope.from_dict(value)
        with self.assertRaises(ValueError):
            task_id_from_ledger_scope("cognition:memory")


if __name__ == "__main__":
    unittest.main()

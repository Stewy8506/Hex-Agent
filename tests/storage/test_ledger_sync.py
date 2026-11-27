"""Cursor-based ledger sync, dialogue import, and conflict branch regressions."""

from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path

from harness_core.storage.db import LedgerStore


class LedgerSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = LedgerStore(self.root / "source.db")
        self.target = LedgerStore(self.root / "target.db")

    def tearDown(self) -> None:
        self.source.close()
        self.target.close()
        self.temp.cleanup()

    def _task(self, store: LedgerStore, goal: str = "shared task") -> str:
        return store.submit_task({"goal": goal})["task_id"]

    def _delta(self, store: LedgerStore, task_id: str, since: int = 0, limit: int = 500) -> dict:
        return store.export_sync_delta(task_id, since, limit=limit)

    def _merge(self, target: LedgerStore, delta: dict, sender: str = "dev_source") -> dict:
        return target.merge_remote_chain(
            delta["task"]["task_id"], delta["events"],
            task_snapshot=delta["task"], dialogue_turns=delta["dialogue_turns"],
            sender_device_record_id=sender, since_seq=delta["since_seq"],
            base_event_sha256=delta["base_event_sha256"],
        )

    def test_cursor_paging_and_idempotent_full_import(self) -> None:
        task_id = self._task(self.source)
        self.source.record_event(task_id, "RUNNING", reason_code="STARTED")
        self.source.record_event(task_id, "TOOL_CALL", reason_code="EXEC", payload={"cmd": "pwd"})
        self.source.record_dialogue_turn(task_id, 0, "user", "hello")
        self.source.record_dialogue_turn(task_id, 1, "assistant", "hi")

        first = self._delta(self.source, task_id, limit=1)
        self.assertEqual([event["sequence_no"] for event in first["events"]], [1])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_seq"], 1)
        second = self._delta(self.source, task_id, first["next_seq"], limit=10)
        self.assertEqual([event["sequence_no"] for event in second["events"]], [2, 3])
        self.assertFalse(second["has_more"])

        result = self._merge(self.target, self._delta(self.source, task_id))
        self.assertEqual(result["status"], "append")
        self.assertEqual(result["imported_events"], 3)
        self.assertEqual(result["imported_dialogue_turns"], 2)
        self.assertTrue(result["task_imported"])
        self.assertEqual(self.target.get_task(task_id)["status"], "SUCCEEDED")
        self.assertEqual(
            [turn["content"] for turn in self.target.get_active_dialogue(task_id)],
            ["hello", "hi"],
        )
        self.assertTrue(self.target.verify_chain_integrity(task_id))
        self.assertEqual(self._merge(self.target, self._delta(self.source, task_id))["status"], "unchanged")
        self.assertEqual(len(self.target.list_events(task_id)), 3)

    def test_paged_deltas_import_using_verified_base_hash(self) -> None:
        task_id = self._task(self.source)
        self.source.record_event(task_id, "RUNNING", reason_code="STARTED")
        self.source.record_event(task_id, "VERIFYING", reason_code="CHECK")
        first = self._delta(self.source, task_id, limit=1)
        self._merge(self.target, first)
        second = self._delta(self.source, task_id, first["next_seq"], limit=1)
        self.assertEqual(second["base_event_sha256"], first["events"][-1]["event_sha256"])
        self._merge(self.target, second)
        third = self._delta(self.source, task_id, second["next_seq"], limit=1)
        self._merge(self.target, third)
        self.assertEqual(len(self.target.list_events(task_id)), 3)
        self.assertTrue(self.target.verify_chain_integrity(task_id))

        second["base_event_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self._merge(self.target, second)
        self.assertEqual(len(self.target.list_events(task_id)), 3)

    def test_linear_remote_events_append_without_rehashing(self) -> None:
        task_id = self._task(self.source)
        base = self._delta(self.source, task_id)
        self._merge(self.target, base)
        self.source.record_event(task_id, "RUNNING", reason_code="STARTED")
        appended = self._merge(self.target, self._delta(self.source, task_id))
        self.assertEqual(appended["status"], "append")
        self.assertEqual(appended["imported_events"], 1)
        self.assertEqual(
            self.target.list_events(task_id)[-1]["event_sha256"],
            self.source.list_events(task_id)[-1]["event_sha256"],
        )
        self.assertEqual(self._merge(self.target, base)["status"], "unchanged")

    def test_divergence_preserves_both_chains_and_is_replay_safe(self) -> None:
        task_id = self._task(self.source)
        base = self._delta(self.source, task_id)
        self._merge(self.target, base)
        self.source.record_event(task_id, "SOURCE_PROGRESS", reason_code="SOURCE")
        self.target.record_event(task_id, "TARGET_PROGRESS", reason_code="TARGET")
        local_tip = self.target.list_events(task_id)[-1]["event_sha256"]

        result = self._merge(self.target, self._delta(self.source, task_id))
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["imported_events"], 2)
        self.assertEqual(self.target.list_events(task_id)[-1]["event_sha256"], local_tip)
        branches = self.target.list_sync_branches(task_id)
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0]["event_count"], 2)
        self.assertTrue(self.target.verify_sync_branch_integrity(task_id)["valid"])
        branch_events = self.target.export_events_since(
            task_id, branch_id=branches[0]["branch_id"]
        )
        self.assertEqual(branch_events[-1]["event_type"], "SOURCE_PROGRESS")

        replay = self._merge(self.target, self._delta(self.source, task_id))
        self.assertEqual(replay["status"], "conflict")
        self.assertEqual(replay["imported_events"], 0)
        self.assertEqual(len(self.target.list_sync_branches(task_id)), 1)

    def test_paged_divergent_chain_continues_from_verified_branch_base(self) -> None:
        task_id = self._task(self.source)
        base = self._delta(self.source, task_id)
        self._merge(self.target, base)
        self.source.record_event(task_id, "SOURCE_ONE", reason_code="SOURCE")
        self.source.record_event(task_id, "SOURCE_TWO", reason_code="SOURCE")
        self.target.record_event(task_id, "TARGET_PROGRESS", reason_code="TARGET")

        first = self._delta(self.source, task_id, base["next_seq"], limit=1)
        first_result = self._merge(self.target, first)
        self.assertEqual(first_result["status"], "conflict")
        second = self._delta(self.source, task_id, first["next_seq"], limit=1)
        second_result = self._merge(self.target, second)
        self.assertEqual(second_result["status"], "conflict")
        self.assertEqual(second_result["next_seq"], 3)
        self.assertTrue(self.target.verify_sync_branch_integrity(task_id)["valid"])
        self.assertEqual(self.target.list_events(task_id)[-1]["event_type"], "TARGET_PROGRESS")
        self.assertEqual(self._merge(self.target, second)["imported_events"], 0)

    def test_tampered_branch_cannot_anchor_a_later_delta(self) -> None:
        task_id = self._task(self.source)
        base = self._delta(self.source, task_id)
        self._merge(self.target, base)
        self.source.record_event(task_id, "SOURCE_ONE", reason_code="SOURCE")
        self.source.record_event(task_id, "SOURCE_TWO", reason_code="SOURCE")
        self.target.record_event(task_id, "TARGET_PROGRESS", reason_code="TARGET")
        first = self._delta(self.source, task_id, base["next_seq"], limit=1)
        self._merge(self.target, first)
        branch_id = self.target.list_sync_branches(task_id)[0]["branch_id"]
        self.target.conn.execute(
            "UPDATE sync_ledger_branch_events SET payload_json = ? WHERE branch_id = ?",
            ('{"tampered":true}', branch_id),
        )
        self.target.conn.commit()
        second = self._delta(self.source, task_id, first["next_seq"], limit=1)

        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self._merge(self.target, second)
        self.assertEqual(self.target.list_events(task_id)[-1]["event_type"], "TARGET_PROGRESS")

    def test_tampered_and_gapped_chains_are_rejected_atomically(self) -> None:
        task_id = self._task(self.source)
        delta = self._delta(self.source, task_id)
        tampered = copy.deepcopy(delta)
        tampered["events"][0]["payload"]["goal"] = "forged"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self._merge(self.target, tampered)
        self.assertIsNone(self.target.get_task(task_id))

        gapped = copy.deepcopy(delta)
        gapped["events"][0]["sequence_no"] = 2
        with self.assertRaisesRegex(ValueError, "discontinuity"):
            self._merge(self.target, gapped)
        self.assertIsNone(self.target.get_task(task_id))

    def test_dialogue_updates_apply_but_id_collision_rolls_back_events(self) -> None:
        task_id = self._task(self.source)
        turn = self.source.record_dialogue_turn(task_id, 0, "user", "original")
        self._merge(self.target, self._delta(self.source, task_id))
        self.target.conn.execute(
            "UPDATE dialogue_turns SET is_active = 0 WHERE turn_id = ?", (turn["turn_id"],)
        )
        self.target.conn.commit()
        self._merge(self.target, self._delta(self.source, task_id))
        self.assertEqual(len(self.target.get_active_dialogue(task_id)), 1)

        self.source.record_event(task_id, "RUNNING", reason_code="STARTED")
        delta = self._delta(self.source, task_id)
        delta["dialogue_turns"][0]["content"] = "different owner"
        with self.assertRaisesRegex(ValueError, "id collision"):
            self._merge(self.target, delta)
        self.assertEqual(len(self.target.list_events(task_id)), 1)
        self.assertEqual(self.target.get_all_dialogue_turns(task_id)[0]["content"], "original")

    def test_branch_tampering_is_detected(self) -> None:
        task_id = self._task(self.source)
        self._merge(self.target, self._delta(self.source, task_id))
        self.source.record_event(task_id, "A", reason_code="SOURCE")
        self.target.record_event(task_id, "B", reason_code="TARGET")
        self._merge(self.target, self._delta(self.source, task_id))
        branch_id = self.target.list_sync_branches(task_id)[0]["branch_id"]
        self.target.conn.execute(
            "UPDATE sync_ledger_branch_events SET payload_json = ? WHERE branch_id = ?",
            ('{"tampered":true}', branch_id),
        )
        self.target.conn.commit()
        report = self.target.verify_sync_branch_integrity(task_id)
        self.assertFalse(report["valid"])
        self.assertTrue(any("hash mismatch" in error for error in report["errors"]))

    def test_async_api_matches_sync_api(self) -> None:
        task_id = self._task(self.source)
        self.source.record_event(task_id, "RUNNING", reason_code="STARTED")
        self.source.record_dialogue_turn(task_id, 0, "user", "async hello")
        delta = asyncio.run(self.source.export_sync_delta_async(task_id))
        result = asyncio.run(self.target.merge_remote_chain_async(
            task_id, delta["events"], task_snapshot=delta["task"],
            dialogue_turns=delta["dialogue_turns"],
        ))
        self.assertEqual(result["imported_events"], 2)
        self.assertEqual(result["imported_dialogue_turns"], 1)
        self.assertEqual(
            self.target.get_active_dialogue(task_id)[0]["content"], "async hello"
        )
        self.assertEqual(asyncio.run(self.target.export_events_since_async(task_id)), delta["events"])
        self.assertEqual(asyncio.run(self.target.list_sync_branches_async(task_id)), [])
        self.assertTrue(asyncio.run(self.target.verify_sync_branch_integrity_async(task_id))["valid"])

    def test_task_catalog_lists_all_tasks_for_sync(self) -> None:
        task_ids = [self._task(self.source, f"task-{index}") for index in range(3)]

        self.assertEqual(set(self.source.list_sync_task_ids()), set(task_ids))
        self.assertEqual(
            set(asyncio.run(self.source.list_sync_task_ids_async())), set(task_ids)
        )

    def test_invalid_cursor_and_limit_are_rejected(self) -> None:
        task_id = self._task(self.source)
        for cursor in (-1, True, "1"):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                self.source.export_events_since(task_id, cursor)
        for limit in (0, 5001, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.source.export_events_since(task_id, limit=limit)


if __name__ == "__main__":
    unittest.main()

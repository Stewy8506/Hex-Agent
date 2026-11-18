"""Conversational Timeline Rewind Engine with Historical Branching.

Provides safe timeline rewinding (/rewind <turn_id | -N>) with historical
branching in the SQLite/PostgreSQL ledger and execution state recovery.
Discards trailing dialogue turns into soft-branches (is_active = 0) with unique
branch IDs rather than performing hard destructive deletes, while reverting
uncommitted tool calls and maintaining cryptographically hash-chained task events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, TYPE_CHECKING
import uuid

if TYPE_CHECKING:
    from harness_core.storage.db import LedgerStore


@dataclass
class RewindTarget:
    """Rollback target representation."""
    turn_index: int
    target_event_id: str | None = None
    branch_id: str = ""


@dataclass
class RewindResult:
    """Outcome of a conversational timeline rewind operation."""
    target_turn: int
    turns_rewound: int
    branch_id: str
    live_messages: list[dict[str, Any]]
    # Tool messages archived with the superseded turns. No workspace files move:
    # rewind is conversation-only, and callers must say so (B40).
    reverted_tool_count: int = 0
    superseded_messages: list[dict[str, Any]] = field(default_factory=list)
    event_id: str | None = None


def group_messages_by_turn(messages: list[dict[str, Any]]) -> list[tuple[int, list[dict[str, Any]]]]:
    """Group sequential chat messages into conversational turns.
    
    Turn 0 represents system directives.
    Each user message initiates a new 1-based turn (Turn 1, Turn 2, ...), with all
    subsequent assistant thoughts, tool calls, and tool observations belonging
    to that turn.
    """
    if not messages:
        return []

    turns: list[tuple[int, list[dict[str, Any]]]] = []
    current_turn_idx = 0
    current_turn_msgs: list[dict[str, Any]] = []

    for msg in messages:
        explicit_turn = msg.get("turn_index") or msg.get("turn")
        role = str(msg.get("role", "")).lower()

        if explicit_turn is not None:
            t_int = int(explicit_turn)
            if t_int != current_turn_idx:
                if current_turn_msgs:
                    turns.append((current_turn_idx, current_turn_msgs))
                current_turn_idx = t_int
                current_turn_msgs = [msg]
            else:
                current_turn_msgs.append(msg)
            continue

        if role == "system" and current_turn_idx == 0 and not turns:
            current_turn_msgs.append(msg)
        elif role == "user":
            if current_turn_msgs:
                turns.append((current_turn_idx, current_turn_msgs))
            current_turn_idx += 1
            current_turn_msgs = [msg]
        else:
            if not current_turn_msgs and current_turn_idx == 0:
                current_turn_idx = 1
            current_turn_msgs.append(msg)

    if current_turn_msgs:
        turns.append((current_turn_idx, current_turn_msgs))

    return turns


class TimelineRewindManager:
    """Manages conversational timeline rollback, ledger branching, and tool call reversal."""

    def __init__(self, ledger: LedgerStore | None = None) -> None:
        self.ledger = ledger

    def resolve_target(
        self,
        target: int | str,
        turn_groups: list[tuple[int, list[dict[str, Any]]]],
        max_turn: int,
    ) -> int:
        """Resolve a relative offset (-N) or absolute turn specifier to a valid turn index."""
        target_int: int | None = None

        if isinstance(target, int):
            target_int = target
        else:
            raw = str(target).strip()
            # Match turn_N, trn_N, turn-N
            match = re.match(r"^(?:turn|trn)[_-]?(\d+)$", raw, re.IGNORECASE)
            if match:
                target_int = int(match.group(1))
            else:
                try:
                    target_int = int(raw)
                except ValueError:
                    # Check if string matches a turn_id or message id directly
                    for t_idx, msgs in turn_groups:
                        for m in msgs:
                            if m.get("turn_id") == raw or m.get("id") == raw:
                                target_int = t_idx
                                break
                        if target_int is not None:
                            break

        if target_int is None:
            # Default to -1 (rewind one turn)
            target_int = -1

        if target_int < 0:
            resolved = max_turn + target_int
        else:
            resolved = target_int

        # Boundary clamping: cannot be negative (clamps to 0) or exceed max_turn
        clamped = max(0, min(resolved, max_turn))
        return clamped

    def rewind(
        self,
        task_id: str,
        target: int | str,
        messages: list[dict[str, Any]],
    ) -> RewindResult:
        """Rewind conversational timeline to designated target turn.
        
        Args:
            task_id: Identifier of the task or session.
            target: Relative offset (-N) or absolute turn index/ID.
            messages: Current active dialogue messages.
            
        Returns:
            RewindResult containing live messages, discarded turns, branch ID,
            and reverted tool count.
        """
        effective_messages = list(messages)
        if not effective_messages and self.ledger is not None:
            try:
                active_db_turns = self.ledger.get_active_dialogue(task_id)
                if active_db_turns:
                    effective_messages = [
                        {
                            "role": r["role"],
                            "content": r["content"],
                            "turn_index": r["turn_index"],
                            "turn_id": r["turn_id"],
                        }
                        for r in active_db_turns
                    ]
            except Exception:
                pass

        turn_groups = group_messages_by_turn(effective_messages)
        user_turn_indices = [t_idx for t_idx, _ in turn_groups if t_idx > 0]
        max_turn = max(user_turn_indices) if user_turn_indices else 0

        target_turn = self.resolve_target(target, turn_groups, max_turn)
        turns_rewound = max(0, max_turn - target_turn)
        branch_id = f"br_rewind_{uuid.uuid4().hex[:12]}"

        if turns_rewound == 0:
            return RewindResult(
                target_turn=target_turn,
                turns_rewound=0,
                branch_id=branch_id,
                live_messages=effective_messages,
                reverted_tool_count=0,
                superseded_messages=[],
            )

        # Partition messages: keep turns <= target_turn, archive turns > target_turn
        live_messages: list[dict[str, Any]] = []
        superseded_messages: list[dict[str, Any]] = []
        discarded_turn_indices: list[int] = []

        for t_idx, t_msgs in turn_groups:
            if t_idx <= target_turn:
                live_messages.extend(t_msgs)
            else:
                superseded_messages.extend(t_msgs)
                discarded_turn_indices.append(t_idx)

        # Without a ledger, report the tool messages moved into the superseded
        # branch; with one, the ledger reports the tool-call records it reverted.
        reverted_tool_count = sum(
            1 for message in superseded_messages if message.get("role") == "tool"
        )
        event_id = None

        if self.ledger is not None:
            try:
                branch_record = self.ledger.record_timeline_branch(
                    task_id=task_id,
                    branch_id=branch_id,
                    superseded_turns=superseded_messages,
                    reason=f"Rewound {turns_rewound} turn(s) to turn {target_turn}",
                )
                reverted_tool_count = int(
                    branch_record.get("reverted_tool_count", reverted_tool_count)
                )
                event_id = branch_record.get("event_id")
            except Exception:
                pass

        return RewindResult(
            target_turn=target_turn,
            turns_rewound=turns_rewound,
            branch_id=branch_id,
            live_messages=live_messages,
            reverted_tool_count=reverted_tool_count,
            superseded_messages=superseded_messages,
            event_id=event_id,
        )

"""Verified, crash-recoverable application of peer-authored team turn content."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

from capabilities.proof import SignedProofArtifact
from cloud.cognition_sync import _validate_manifest
from cloud.team_mesh import SignedTurnCommitBundle, canonical_json_bytes
from harness_core.storage.cow import (
    IGNORE_DIRS,
    PublicationConflictError,
    compute_tree_sha,
    import_delta_archive,
    is_sensitive_workspace_path,
    publish_delta,
)
from harness_core.storage.ledger_sync import _same_event, _synced_task_values, _validate_chain, _validate_turns

TEAM_REPLICATION_VERSION = 1
_MAX_ARCHIVE_BYTES = 48 * 1024 * 1024
_MAX_EXPANDED_BYTES = 512 * 1024 * 1024
_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
_MAX_LEDGER_EVENTS = 10_000
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TREE_HASH_PATTERN = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_PAYLOAD_FIELDS = {
    "version",
    "workspace_archive_base64",
    "base_workspace_tree_sha",
    "signed_proof",
    "ledger_delta",
    "cognition_manifest",
}
_LEDGER_FIELDS = {
    "version",
    "since_seq",
    "task",
    "events",
    "base_event_sha256",
    "dialogue_turns",
    "next_seq",
    "has_more",
}


def team_payload_digest(payload: Mapping[str, Any]) -> str:
    """Return the digest used to detect conflicting reuse of a signed turn."""
    try:
        encoded = canonical_json_bytes(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("Team replication payload is not canonical JSON.") from exc
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise ValueError("Team replication payload exceeds the size limit.")
    return hashlib.sha256(encoded).hexdigest()


def _tree_hash(value: Any, *, prefixed: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Workspace tree hash must be a string.")
    pattern = _HASH_PATTERN if prefixed else _TREE_HASH_PATTERN
    if not pattern.fullmatch(value):
        raise ValueError("Invalid workspace tree hash.")
    return "sha256:" + value.removeprefix("sha256:")


def _local_events(ledger: Any, task_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = ledger.export_events_since(task_id, offset, limit=5000)
        if not page:
            break
        events.extend(page)
        if len(page) < 5000:
            break
        offset += len(page)
        if len(events) > _MAX_LEDGER_EVENTS:
            raise ValueError("Local ledger exceeds the team replication limit.")
    return events


def _copy_replicated_tree(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = set(IGNORE_DIRS)
        root = Path(directory)
        return {
            name for name in names
            if name in ignored
            or name.startswith(".DS_Store")
            or name.endswith((".pyc", ".db-wal", ".db-shm"))
            or is_sensitive_workspace_path(str(root / name))
        }

    shutil.copytree(source, destination, symlinks=True, ignore=ignore, dirs_exist_ok=True)


def _entry_state(path: Path) -> tuple[str, str] | None:
    if path.is_symlink():
        return ("link", os.readlink(path))
    if path.is_file():
        return ("file", hashlib.sha256(path.read_bytes()).hexdigest())
    return None


def _delta_matches(
    base: Path,
    head: Path,
    target: Path,
) -> bool:
    """Check whether every base->head change is already present in target."""
    paths: set[str] = set()
    for root in (base, head):
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in IGNORE_DIRS]
            relative = Path(directory).relative_to(root)
            for name in filenames:
                if name.startswith(".DS_Store") or name.endswith((".pyc", ".db-wal", ".db-shm")):
                    continue
                rel = str(relative / name) if str(relative) != "." else name
                if not is_sensitive_workspace_path(rel):
                    paths.add(rel)
    for rel in paths:
        base_path = base / rel
        head_path = head / rel
        if _entry_state(base_path) == _entry_state(head_path):
            continue
        if _entry_state(target / rel) != _entry_state(head_path):
            return False
    return True


class TeamReplicationService:
    """Apply signed team turns without acknowledging metadata-only delivery.

    A pending journal bridges filesystem, ledger, and cognition updates. A retry
    with the identical payload resumes the journal; no turn is finalized or
    acknowledged until every resource succeeds.
    """

    def __init__(
        self,
        data_dir: Path | str,
        workspace_dir: Path | str,
        ledger: Any,
        cognition: Any,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.workspace_dir = Path(workspace_dir).resolve()
        self.ledger = ledger
        self.cognition = cognition
        self.root = self.data_dir / "team_replication"
        self.staging_root = self.root / "staging"
        self.heads_root = self.root / "heads"
        self.state_path = self.root / "state.json"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.heads_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"version": TEAM_REPLICATION_VERSION, "sessions": {}}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Team replication journal is unreadable.") from exc
        if (
            not isinstance(state, dict)
            or state.get("version") != TEAM_REPLICATION_VERSION
            or not isinstance(state.get("sessions"), dict)
        ):
            raise ValueError("Team replication journal has an invalid schema.")
        return state

    def _save_state(self) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".state_", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._state, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _session(self, session_id: str) -> dict[str, Any]:
        sessions = self._state["sessions"]
        return sessions.setdefault(
            session_id,
            {
                "last_applied_seq": 0,
                "base_snapshot": None,
                "base_tree_sha": None,
                "turns": {},
                "pending": None,
            },
        )

    def _managed_path(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative:
            raise ValueError("Invalid team replication journal path.")
        path = (self.root / relative).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("Team replication journal path escapes its root.")
        return path

    def applied_turn(
        self,
        session_id: str,
        host_epoch: int,
        turn_seq: int,
        bundle_digest: str,
        payload_digest: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            session = self._state["sessions"].get(session_id)
            if session is None:
                return None
            record = session["turns"].get(f"{host_epoch}:{turn_seq}")
            if record is None:
                return None
            if (
                record.get("bundle_digest") != bundle_digest
                or record.get("payload_digest") != payload_digest
            ):
                raise ValueError("An already applied turn was reused with different content.")
            return dict(record)

    def expected_turn_seq(self, session_id: str) -> int:
        with self._lock:
            session = self._state["sessions"].get(session_id)
            return int(session["last_applied_seq"]) + 1 if session else 1

    def _decode_and_validate(
        self,
        signed: SignedTurnCommitBundle,
        payload: Mapping[str, Any],
        device_public_keys: Mapping[str, str],
    ) -> tuple[dict[str, Any], SignedProofArtifact, dict[str, Any], dict[str, Any], bytes]:
        if not isinstance(payload, Mapping) or set(payload) != _PAYLOAD_FIELDS:
            raise ValueError("Malformed team replication payload fields.")
        if type(payload["version"]) is not int or payload["version"] != TEAM_REPLICATION_VERSION:
            raise ValueError("Unsupported team replication payload version.")
        digest = team_payload_digest(payload)
        base_tree = _tree_hash(payload["base_workspace_tree_sha"], prefixed=True)
        encoded_archive = payload["workspace_archive_base64"]
        if not isinstance(encoded_archive, str) or len(encoded_archive) > 72 * 1024 * 1024:
            raise ValueError("Invalid team workspace archive encoding.")
        try:
            archive_bytes = base64.b64decode(encoded_archive, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Invalid team workspace archive encoding.") from exc
        if not archive_bytes or len(archive_bytes) > _MAX_ARCHIVE_BYTES:
            raise ValueError("Team workspace archive exceeds the size limit.")
        bundle = signed.bundle
        if hashlib.sha256(archive_bytes).hexdigest() != bundle.cow_delta_sha256.removeprefix("sha256:"):
            raise ValueError("Team workspace archive digest does not match the signed turn.")

        proof = SignedProofArtifact.from_dict(payload["signed_proof"])
        public_key = device_public_keys.get(signed.author_device_record_id)
        if not public_key or not proof.verify(public_key):
            raise ValueError("Signed proof author verification failed.")
        if proof.author_device_record_id != signed.author_device_record_id:
            raise ValueError("Proof and turn authors do not match.")
        if (
            proof.project_session_id != bundle.project_session_id
            or proof.host_epoch != bundle.host_epoch
            or proof.turn_seq != bundle.turn_seq
        ):
            raise ValueError("Signed proof is not bound to this team turn.")
        if proof.proof_merkle_root != bundle.proof_merkle_root:
            raise ValueError("Proof Merkle root does not match the signed turn.")
        head_tree = _tree_hash(proof.proof["workspace_tree_sha"])
        if base_tree == head_tree:
            raise ValueError("A team turn must advance the workspace tree.")

        ledger_delta = payload["ledger_delta"]
        if not isinstance(ledger_delta, dict) or set(ledger_delta) != _LEDGER_FIELDS:
            raise ValueError("Malformed team ledger delta fields.")
        if type(ledger_delta["version"]) is not int or ledger_delta["version"] != 1:
            raise ValueError("Unsupported team ledger delta version.")
        if ledger_delta["has_more"] is not False:
            raise ValueError("A team turn must contain its complete ledger delta.")
        task = ledger_delta["task"]
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
            raise ValueError("Malformed team ledger task snapshot.")
        task_id = task["task_id"]
        if task_id != proof.proof["task_id"]:
            raise ValueError("Proof and ledger task ids do not match.")
        _synced_task_values(task, signed.author_device_record_id)
        since_seq = ledger_delta["since_seq"]
        events = _validate_chain(
            task_id,
            ledger_delta["events"],
            since_seq=since_seq,
            base_event_sha256=ledger_delta["base_event_sha256"],
            max_events=_MAX_LEDGER_EVENTS,
        )
        _validate_turns(task_id, ledger_delta["dialogue_turns"])
        if not events:
            raise ValueError("A team turn must contain at least one ledger event.")
        if (
            type(ledger_delta["next_seq"]) is not int
            or ledger_delta["next_seq"] != since_seq + len(events)
        ):
            raise ValueError("Team ledger delta has an invalid next sequence.")
        if bundle.ledger_chain_tip != "sha256:" + events[-1]["event_sha256"]:
            raise ValueError("Ledger chain tip does not match the signed turn.")
        final_tree = events[-1].get("workspace_tree_sha")
        if final_tree is None or _tree_hash(final_tree) != head_tree:
            raise ValueError("Ledger head event does not match the signed proof workspace.")

        manifest = payload["cognition_manifest"]
        validated_manifest, _documents, _skills = _validate_manifest(manifest)
        if validated_manifest["workspace_id"] != self.cognition.workspace_id:
            raise ValueError("Cognition manifest belongs to a different workspace.")
        if bundle.memory_delta_sha256 != "sha256:" + validated_manifest["manifest_sha256"]:
            raise ValueError("Cognition manifest hash does not match the signed turn.")
        normalized_delta = {
            **ledger_delta,
            "events": events,
        }
        return digest, proof, normalized_delta, validated_manifest, archive_bytes

    def _validate_ledger_anchor(self, delta: dict[str, Any]) -> None:
        task_id = delta["task"]["task_id"]
        since_seq = delta["since_seq"]
        if self.ledger.get_task(task_id) is None:
            if since_seq != 0:
                raise ValueError("Cannot append a team delta for an unknown local task.")
            return
        local = _local_events(self.ledger, task_id)
        incoming = delta["events"]
        base_hash = delta["base_event_sha256"]
        if since_seq > 0 and (
            len(local) < since_seq or local[since_seq - 1]["event_sha256"] != base_hash
        ):
            raise ValueError("Team ledger delta base does not match the local chain.")
        if len(local) > since_seq + len(incoming):
            raise ValueError("Team ledger delta would regress the local chain.")
        overlap = min(len(local) - since_seq, len(incoming))
        if any(
            not _same_event(local[since_seq + index], incoming[index])
            for index in range(overlap)
        ):
            raise ValueError("Team ledger delta conflicts with the local chain.")

    def _base_snapshot(self, session_id: str, base_tree: str) -> Path:
        session = self._session(session_id)
        relative = session.get("base_snapshot")
        if relative:
            snapshot = self._managed_path(relative)
            if not snapshot.is_dir() or compute_tree_sha(snapshot) != base_tree.removeprefix("sha256:"):
                raise ValueError("Stored team base snapshot is missing or corrupt.")
            return snapshot
        snapshot = Path(tempfile.mkdtemp(prefix="base_", dir=self.staging_root))
        try:
            _copy_replicated_tree(self.workspace_dir, snapshot)
            if compute_tree_sha(snapshot) != base_tree.removeprefix("sha256:"):
                raise ValueError("Team delta base does not match the local workspace.")
            session["base_snapshot"] = str(snapshot.relative_to(self.root))
            session["base_tree_sha"] = base_tree
            self._save_state()
            return snapshot
        except Exception:
            shutil.rmtree(snapshot, ignore_errors=True)
            raise

    def _extract_archive(self, archive_bytes: bytes, head_tree: str) -> Path:
        archive_path = self.staging_root / f"{uuid.uuid4().hex}.tar.gz"
        staging = self.staging_root / f"head_{uuid.uuid4().hex}"
        try:
            with archive_path.open("xb") as stream:
                stream.write(archive_bytes)
            with tarfile.open(archive_path, "r:gz") as archive:
                expanded_bytes = 0
                for member in archive:
                    normalized = member.name.replace("\\", "/")
                    relative = normalized.removeprefix("workspace/")
                    if is_sensitive_workspace_path(relative):
                        raise ValueError("Team workspace archive contains a credential file.")
                    if member.isreg():
                        expanded_bytes += member.size
                        if expanded_bytes > _MAX_EXPANDED_BYTES:
                            raise ValueError("Team workspace archive exceeds the expanded-size limit.")
            import_delta_archive(
                archive_path,
                staging,
                expected_tree_sha=head_tree.removeprefix("sha256:"),
            )
            return staging
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            archive_path.unlink(missing_ok=True)

    def _validate_workspace_delta(self, base: Path, head: Path) -> None:
        if _delta_matches(base, head, self.workspace_dir):
            return
        conflicts = [
            rel for rel in self._changed_paths(base, head)
            if _entry_state(self.workspace_dir / rel) != _entry_state(base / rel)
        ]
        if conflicts:
            raise PublicationConflictError(conflicts)

    def _apply_workspace(self, base: Path, head: Path) -> None:
        self._validate_workspace_delta(base, head)
        if not _delta_matches(base, head, self.workspace_dir):
            publish_delta(base, head, self.workspace_dir)

    def _changed_paths(self, base: Path, head: Path) -> list[str]:
        paths: set[str] = set()
        for root in (base, head):
            for directory, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if name not in IGNORE_DIRS]
                relative = Path(directory).relative_to(root)
                for name in filenames:
                    rel = str(relative / name) if str(relative) != "." else name
                    if not is_sensitive_workspace_path(rel):
                        paths.add(rel)
        return sorted(
            rel for rel in paths
            if _entry_state(base / rel) != _entry_state(head / rel)
        )

    def _apply_ledger(self, signed: SignedTurnCommitBundle, delta: dict[str, Any]) -> None:
        result = self.ledger.merge_remote_chain(
            delta["task"]["task_id"],
            delta["events"],
            task_snapshot=delta["task"],
            sender_device_record_id=signed.author_device_record_id,
            dialogue_turns=delta["dialogue_turns"],
            since_seq=delta["since_seq"],
            base_event_sha256=delta["base_event_sha256"],
        )
        if result.get("status") == "conflict":
            raise ValueError("Team ledger merge produced a conflict branch.")
        local = _local_events(self.ledger, delta["task"]["task_id"])
        expected_tip = signed.bundle.ledger_chain_tip.removeprefix("sha256:")
        if not local or local[-1]["event_sha256"] != expected_tip:
            raise ValueError("Applied ledger tip does not match the signed turn.")

    def _apply_cognition(self, signed: SignedTurnCommitBundle, manifest: dict[str, Any]) -> dict[str, Any]:
        result = self.cognition.apply_manifest(
            manifest,
            sender_device_record_id=signed.author_device_record_id,
        )
        if result.get("status") not in {"applied", "unchanged", "conflict"}:
            raise ValueError("Team cognition merge did not complete cleanly.")
        return result

    def _persist_proof(self, proof: SignedProofArtifact) -> None:
        self.ledger.save_signed_proof(proof.to_dict())

    def _finalize(
        self,
        session_id: str,
        session: dict[str, Any],
        head: Path,
        cognition_result: dict[str, Any],
    ) -> None:
        pending = session["pending"]
        if pending is None:
            raise ValueError("Missing pending team turn journal.")
        final_head = self.heads_root / session_id / f"{uuid.uuid4().hex}"
        final_head.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _copy_replicated_tree(head, final_head)
        old_snapshot = session.get("base_snapshot")
        session["base_snapshot"] = str(final_head.relative_to(self.root))
        session["base_tree_sha"] = pending["head_tree_sha"]
        session["last_applied_seq"] = pending["turn_seq"]
        session["turns"][f"{pending['host_epoch']}:{pending['turn_seq']}"] = {
            "bundle_digest": pending["bundle_digest"],
            "payload_digest": pending["payload_digest"],
            "head_tree_sha": pending["head_tree_sha"],
            "cognition_status": cognition_result["status"],
            "cognition_conflicts": cognition_result.get("conflicts", []),
        }
        session["pending"] = None
        self._save_state()
        if old_snapshot and old_snapshot != session["base_snapshot"]:
            shutil.rmtree(self._managed_path(old_snapshot), ignore_errors=True)
        shutil.rmtree(head, ignore_errors=True)

    def validate_turn(
        self,
        signed: SignedTurnCommitBundle,
        payload: Mapping[str, Any],
        device_public_keys: Mapping[str, str],
    ) -> dict[str, Any]:
        if not isinstance(signed, SignedTurnCommitBundle):
            signed = SignedTurnCommitBundle.from_dict(signed)
        if not signed.verify(device_public_keys.get(signed.author_device_record_id, "")):
            raise ValueError("TurnCommitBundle author signature verification failed.")
        digest, proof, delta, _manifest, archive_bytes = self._decode_and_validate(
            signed, payload, device_public_keys
        )
        bundle = signed.bundle
        bundle_digest = bundle.compute_digest()
        base_tree = _tree_hash(payload["base_workspace_tree_sha"], prefixed=True)
        head_tree = _tree_hash(proof.proof["workspace_tree_sha"])
        temporary_base: tempfile.TemporaryDirectory[str] | None = None
        head: Path | None = None
        with self._lock:
            session = self._state["sessions"].get(bundle.project_session_id) or {
                "last_applied_seq": 0,
                "base_snapshot": None,
                "turns": {},
                "pending": None,
            }
            existing = self.applied_turn(
                bundle.project_session_id,
                bundle.host_epoch,
                bundle.turn_seq,
                bundle_digest,
                digest,
            )
            if existing is not None:
                return {
                    "status": "unchanged",
                    "payload_digest": digest,
                    "proof": proof.to_dict(),
                    "task_id": proof.proof["task_id"],
                }
            try:
                pending = session.get("pending")
                if pending is not None:
                    if (
                        pending["host_epoch"] != bundle.host_epoch
                        or pending["turn_seq"] != bundle.turn_seq
                        or pending["payload_digest"] != digest
                        or pending["bundle_digest"] != bundle_digest
                    ):
                        raise ValueError("A different team turn is already pending.")
                    head = self._managed_path(pending["head_path"])
                    if not head.is_dir() or compute_tree_sha(head) != head_tree.removeprefix("sha256:"):
                        raise ValueError("Pending team workspace snapshot is missing or corrupt.")
                    base_relative = session.get("base_snapshot")
                    if not base_relative:
                        raise ValueError("Pending team base snapshot is missing.")
                    base = self._managed_path(base_relative)
                else:
                    if bundle.turn_seq != int(session["last_applied_seq"]) + 1:
                        raise ValueError(
                            f"Team turn sequence gap: expected {int(session['last_applied_seq']) + 1}, "
                            f"got {bundle.turn_seq}."
                        )
                    base_relative = session.get("base_snapshot")
                    if base_relative:
                        base = self._managed_path(base_relative)
                    else:
                        temporary_base = tempfile.TemporaryDirectory(
                            prefix="preflight_base_", dir=self.staging_root
                        )
                        base = Path(temporary_base.name)
                        _copy_replicated_tree(self.workspace_dir, base)
                    head = self._extract_archive(archive_bytes, head_tree)
                if not base.is_dir() or compute_tree_sha(base) != base_tree.removeprefix("sha256:"):
                    raise ValueError("Team delta base does not match the local workspace.")
                self._validate_ledger_anchor(delta)
                self._validate_workspace_delta(base, head)
            finally:
                if temporary_base is not None:
                    temporary_base.cleanup()
                if head is not None and pending is None:
                    shutil.rmtree(head, ignore_errors=True)
        return {
            "status": "validated",
            "payload_digest": digest,
            "proof": proof.to_dict(),
            "task_id": proof.proof["task_id"],
        }

    def apply_turn(
        self,
        signed: SignedTurnCommitBundle,
        payload: Mapping[str, Any],
        device_public_keys: Mapping[str, str],
    ) -> dict[str, Any]:
        """Verify and apply a turn, returning only after durable completion."""
        if not isinstance(signed, SignedTurnCommitBundle):
            signed = SignedTurnCommitBundle.from_dict(signed)
        if not signed.verify(device_public_keys.get(signed.author_device_record_id, "")):
            raise ValueError("TurnCommitBundle author signature verification failed.")
        payload_digest, proof, delta, manifest, archive_bytes = self._decode_and_validate(
            signed, payload, device_public_keys
        )
        bundle = signed.bundle
        bundle_digest = bundle.compute_digest()
        with self._lock:
            existing = self.applied_turn(
                bundle.project_session_id,
                bundle.host_epoch,
                bundle.turn_seq,
                bundle_digest,
                payload_digest,
            )
            if existing is not None:
                return {**existing, "status": "unchanged"}
            session = self._session(bundle.project_session_id)
            pending = session.get("pending")
            if pending is not None:
                if (
                    pending["host_epoch"] != bundle.host_epoch
                    or pending["turn_seq"] != bundle.turn_seq
                    or pending["payload_digest"] != payload_digest
                ):
                    raise ValueError("A different team turn is already pending.")
                head = self._managed_path(pending["head_path"])
                base = self._managed_path(session["base_snapshot"])
                if not head.is_dir() or compute_tree_sha(head) != pending["head_tree_sha"].removeprefix("sha256:"):
                    raise ValueError("Pending team workspace snapshot is missing or corrupt.")
                self._validate_ledger_anchor(delta)
                self._apply_workspace(base, head)
            else:
                if bundle.turn_seq != int(session["last_applied_seq"]) + 1:
                    raise ValueError(
                        f"Team turn sequence gap: expected {int(session['last_applied_seq']) + 1}, "
                        f"got {bundle.turn_seq}."
                    )
                self._validate_ledger_anchor(delta)
                base = self._base_snapshot(
                    bundle.project_session_id,
                    _tree_hash(payload["base_workspace_tree_sha"], prefixed=True),
                )
                head = self._extract_archive(
                    archive_bytes,
                    _tree_hash(proof.proof["workspace_tree_sha"]),
                )
                session["pending"] = {
                    "host_epoch": bundle.host_epoch,
                    "turn_seq": bundle.turn_seq,
                    "bundle_digest": bundle_digest,
                    "payload_digest": payload_digest,
                    "head_path": str(head.relative_to(self.root)),
                    "head_tree_sha": _tree_hash(proof.proof["workspace_tree_sha"]),
                }
                self._save_state()
                self._apply_workspace(base, head)

            self._apply_ledger(signed, delta)
            self._persist_proof(proof)
            cognition_result = self._apply_cognition(signed, manifest)
            self._finalize(bundle.project_session_id, session, head, cognition_result)
            return {
                "status": "applied",
                "project_session_id": bundle.project_session_id,
                "host_epoch": bundle.host_epoch,
                "turn_seq": bundle.turn_seq,
                "bundle_digest": bundle_digest,
                "payload_digest": payload_digest,
                "workspace_tree_sha": _tree_hash(proof.proof["workspace_tree_sha"]),
                "cognition_status": cognition_result["status"],
                "cognition_conflicts": cognition_result.get("conflicts", []),
            }

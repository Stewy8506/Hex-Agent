"""Versioned personal cognition manifests and gated skill installation."""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_core.cognition.skillspector import (
    SkillSpectorScanner,
    parse_skill_frontmatter,
)
from harness_core.storage.project_registry import compute_project_id

from .sync_contract import canonical_json_bytes, cognition_scope

_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_SKILL_BYTES = 8 * 1024 * 1024
_MAX_SKILL_FILES = 512
_MAX_SKILLS = 256
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MANIFEST_FIELDS = {
    "version", "workspace_id", "revision", "files", "skills", "manifest_sha256",
}
_DOCUMENT_FIELDS = {
    "id", "deleted", "sha256", "base_sha256", "content_base64",
}
_SKILL_FIELDS = {
    "id", "name", "deleted", "sha256", "base_sha256", "files",
}
_BUNDLE_FILE_FIELDS = {"path", "content_base64", "executable"}
_DOCUMENTS = {
    "global/user": ("global", "USER.md"),
    "global/soul": ("global", "SOUL.md"),
    "workspace/memory": ("workspace", ".hex/memories/MEMORY.md"),
    "workspace/guardrails": ("workspace", ".hex/memories/GUARDRAILS.md"),
}


@dataclass(slots=True)
class _DecodedSkill:
    record: dict[str, Any]
    files: list[tuple[str, bytes, bool]]
    stage: Path | None = None


class CognitionSyncStore:
    """Export and merge one workspace's personal data without silent overwrites."""

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        global_home: Path | str | None = None,
        workspace_id: str | None = None,
        ledger: Any | None = None,
        scanner: SkillSpectorScanner | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory.")
        self.global_home = Path(global_home).resolve() if global_home else Path.home() / ".hex"
        self.global_home.mkdir(parents=True, exist_ok=True)
        selected_workspace = workspace_id or compute_project_id(self.workspace_root)
        self.workspace_id = _validate_workspace_id(selected_workspace)
        self.ledger = ledger
        self.scanner = scanner or SkillSpectorScanner(ledger=ledger)
        self.sync_home = self.global_home / ".sync"
        self.state_path = self.sync_home / "cognition" / f"{self.workspace_id}.json"
        self._lock = threading.RLock()

    @property
    def scope(self) -> str:
        return cognition_scope(self.workspace_id)

    def export_manifest(self) -> dict[str, Any]:
        with self._lock:
            state = self._load_state()
            previous_files = dict(state["local"]["files"])
            previous_skills = dict(state["local"]["skills"])
            current_files: dict[str, str] = {}
            current_skills: dict[str, str] = {}
            records: list[dict[str, Any]] = []

            for document_id in sorted(_DOCUMENTS):
                content = self._read_document(document_id)
                previous = previous_files.get(document_id)
                if content is None:
                    if previous is not None:
                        records.append(_deleted_record(document_id, previous))
                    continue
                digest = hashlib.sha256(content).hexdigest()
                current_files[document_id] = digest
                records.append({
                    "id": document_id,
                    "deleted": False,
                    "sha256": digest,
                    "base_sha256": previous,
                    "content_base64": _encode_content(content),
                })

            bundles = self._enumerate_skill_bundles()
            for name, files in bundles.items():
                digest = _bundle_hash(files)
                skill_id = _skill_id(name)
                previous = previous_skills.get(skill_id)
                current_skills[skill_id] = digest
                records.append(_skill_record(skill_id, name, files, digest, previous))
            for skill_id, previous in sorted(previous_skills.items()):
                if skill_id not in current_skills:
                    name = _skill_name_from_id(skill_id)
                    records.append(_deleted_record(skill_id, previous, name=name))

            changed = (
                not state["initialized"]
                or state["manifest"] is None
                or current_files != previous_files
                or current_skills != previous_skills
            )
            if not changed and state["manifest"] is not None:
                return state["manifest"]
            revision = state["revision"] + 1 if changed else state["revision"]
            payload = {
                "version": 1,
                "workspace_id": self.workspace_id,
                "revision": revision,
                "files": [record for record in records if not record["id"].startswith("skill:")],
                "skills": [record for record in records if record["id"].startswith("skill:")],
            }
            payload["manifest_sha256"] = _manifest_hash(payload)
            state["revision"] = revision
            state["initialized"] = True
            state["manifest"] = payload
            state["local"] = {"files": current_files, "skills": current_skills}
            self._save_state(state)
            return payload

    def apply_manifest(
        self, payload: dict[str, Any], *, sender_device_record_id: str
    ) -> dict[str, Any]:
        sender = _validate_sender(sender_device_record_id)
        manifest, decoded_documents, decoded_skills = _validate_manifest(payload)
        if manifest["workspace_id"] != self.workspace_id:
            raise ValueError("Cognition manifest belongs to a different workspace.")

        with self._lock:
            state = self._load_state()
            previous_local = json.loads(json.dumps(state["local"]))
            source = state["sources"].get(sender)
            if source is not None and manifest["revision"] <= source["revision"]:
                if manifest["manifest_sha256"] == source["manifest_sha256"]:
                    return {
                        "status": "unchanged",
                        "revision": manifest["revision"],
                        "applied": [],
                        "conflicts": [],
                        "skills": [],
                    }
                raise ValueError("Cognition manifest revision conflicts with the known source history.")

            document_plans: list[dict[str, Any]] = []
            skill_plans: list[dict[str, Any]] = []
            for record in manifest["files"]:
                content = decoded_documents[record["id"]]
                current = self._read_document(record["id"])
                current_hash = hashlib.sha256(current).hexdigest() if current is not None else None
                remote_hash = None if record["deleted"] else record["sha256"]
                action = _classify_change(current_hash, remote_hash, record["base_sha256"])
                document_plans.append({
                    "record": record,
                    "content": content,
                    "current_hash": current_hash,
                    "remote_hash": remote_hash,
                    "action": action,
                    "revision": manifest["revision"],
                })

            try:
                for record in manifest["skills"]:
                    decoded = decoded_skills[record["id"]]
                    if not record["deleted"]:
                        decoded.stage = self._stage_bundle(decoded.files)
                    current_hash = self._current_skill_hash(record["name"])
                    remote_hash = None if record["deleted"] else record["sha256"]
                    action = _classify_change(current_hash, remote_hash, record["base_sha256"])
                    plan: dict[str, Any] = {
                        "record": record,
                        "decoded": decoded,
                        "current_hash": current_hash,
                        "remote_hash": remote_hash,
                        "action": action,
                        "revision": manifest["revision"],
                        "report": None,
                        "destination": None,
                    }
                    if action == "apply" and not record["deleted"]:
                        assert decoded.stage is not None
                        report = self.scanner.scan_bundle(decoded.stage)
                        if report.bundle_sha256 != record["sha256"]:
                            raise ValueError("Staged skill hash does not match the manifest.")
                        plan["report"] = report
                        if report.gate_decision == "ALLOW":
                            destination = self._skill_paths(record["name"])["active"]
                            status = "active"
                        elif report.gate_decision == "REQUIRE_APPROVAL":
                            destination = self._skill_paths(record["name"])["candidate"]
                            status = "candidate"
                        elif report.gate_decision == "QUARANTINE":
                            destination = self._skill_paths(record["name"])["quarantine"]
                            status = "quarantined"
                        else:
                            raise ValueError(f"Unknown SkillSpector gate decision: {report.gate_decision}")
                        plan["destination"] = destination
                        plan["status"] = status
                    skill_plans.append(plan)
                if self._can_register_skills():
                    for plan in skill_plans:
                        if plan["action"] == "apply" and plan["report"] is not None:
                            self.scanner.register_scan_report(
                                plan["decoded"].stage,
                                plan["report"],
                                disk_path=plan["destination"],
                            )
            except Exception:
                for decoded in decoded_skills.values():
                    if decoded.stage is not None and decoded.stage.exists():
                        shutil.rmtree(decoded.stage, ignore_errors=True)
                raise

            applied: list[str] = []
            conflicts: list[dict[str, Any]] = []
            skill_results: list[dict[str, Any]] = []
            try:
                for plan in document_plans:
                    record = plan["record"]
                    if plan["action"] == "conflict":
                        path = self._record_document_conflict(
                            sender, record, plan["content"], plan["current_hash"], plan["revision"]
                        )
                        conflicts.append({
                            "kind": "document", "id": record["id"], "path": str(path),
                        })
                        continue
                    if plan["action"] == "apply":
                        backup = self._write_document(
                            record["id"], plan["content"], plan["current_hash"]
                        )
                        applied.append(record["id"])
                        if backup is not None:
                            skill_results.append({
                                "kind": "document", "id": record["id"],
                                "action": "backup", "path": str(backup),
                            })
                    else:
                        applied.append(record["id"])
                    _set_local_value(
                        state["local"]["files"], record["id"], plan["remote_hash"]
                    )

                for plan in skill_plans:
                    record = plan["record"]
                    if plan["action"] == "conflict":
                        assert plan["decoded"].stage is not None or record["deleted"]
                        path = self._record_skill_conflict(
                            sender, record, plan["decoded"], plan["current_hash"], plan["revision"]
                        )
                        conflicts.append({"kind": "skill", "id": record["id"], "path": str(path)})
                        skill_results.append({
                            "id": record["id"], "action": "conflict", "path": str(path),
                        })
                        continue

                    if plan["action"] == "apply":
                        if record["deleted"]:
                            backups = self._retire_skill_paths(record["name"])
                            for backup in backups.values():
                                skill_results.append({
                                    "kind": "skill", "id": record["id"],
                                    "action": "backup", "path": str(backup),
                                })
                            try:
                                self._disable_ledger_skills(record["name"])
                            except Exception:
                                self._restore_skill_backups(record["name"], backups)
                                raise
                            active_hash = None
                            status = "deleted"
                            destination = None
                        else:
                            destination = plan["destination"]
                            assert destination is not None
                            assert plan["decoded"].stage is not None
                            self._verify_effective_skill_hash(record["name"], plan["current_hash"])
                            backups = self._retire_skill_paths(record["name"])
                            try:
                                self._install_directory(plan["decoded"].stage, destination)
                            except Exception:
                                self._restore_skill_backups(record["name"], backups)
                                raise
                            if plan["status"] != "active":
                                self._write_scan_audit(destination, plan["report"])
                            for backup in backups.values():
                                skill_results.append({
                                    "kind": "skill", "id": record["id"],
                                    "action": "backup", "path": str(backup),
                                })
                            active_hash = (
                                plan["remote_hash"] if plan["status"] == "active" else None
                            )
                            status = plan["status"]
                        applied.append(record["id"])
                    else:
                        matching = self._matching_skill_path(record["name"], plan["remote_hash"])
                        destination = matching
                        active_hash = self._active_skill_hash(record["name"])
                        status = (
                            "deleted" if matching is None
                            else "active" if matching == self._skill_paths(record["name"])["active"]
                            else "candidate" if matching == self._skill_paths(record["name"])["candidate"]
                            else "quarantined"
                        )
                        applied.append(record["id"])

                    _set_local_value(state["local"]["skills"], record["id"], active_hash)
                    skill_results.append({
                        "id": record["id"],
                        "action": plan["action"],
                        "status": status,
                        "path": str(destination) if destination is not None else None,
                        "bundle_sha256": plan["remote_hash"],
                        "gate_decision": (
                            plan["report"].gate_decision if plan["report"] is not None else None
                        ),
                    })

                source_files = dict(source["files"]) if source is not None else {}
                source_skills = dict(source["skills"]) if source is not None else {}
                for record in manifest["files"]:
                    source_files[record["id"]] = (
                        None if record["deleted"] else record["sha256"]
                    )
                for record in manifest["skills"]:
                    source_skills[record["id"]] = (
                        None if record["deleted"] else record["sha256"]
                    )
                state["sources"][sender] = {
                    "revision": manifest["revision"],
                    "manifest_sha256": manifest["manifest_sha256"],
                    "files": source_files,
                    "skills": source_skills,
                }
                if state["local"] != previous_local:
                    state["revision"] += 1
                    state["manifest"] = None
                state["initialized"] = True
                self._save_state(state)
            finally:
                for plan in skill_plans:
                    stage = plan["decoded"].stage
                    if stage is not None and stage.exists():
                        shutil.rmtree(stage, ignore_errors=True)

            return {
                "status": "conflict" if conflicts else "applied",
                "revision": manifest["revision"],
                "applied": applied,
                "conflicts": conflicts,
                "skills": skill_results,
            }

    def _can_register_skills(self) -> bool:
        return bool(
            self.ledger is not None
            and getattr(self.ledger, "backend_type", "sqlite") == "sqlite"
            and getattr(self.ledger, "conn", None) is not None
            and hasattr(self.scanner, "register_scan_report")
        )

    def _document_path(self, document_id: str) -> tuple[Path, Path]:
        scope, relative = _DOCUMENTS[document_id]
        root = self.global_home if scope == "global" else self.workspace_root
        canonical = root / relative
        if scope == "workspace":
            legacy = root / ".hex" / canonical.name
            _assert_managed_path(canonical, root)
            _assert_managed_path(legacy, root)
            if not canonical.is_file() and legacy.is_file():
                canonical = legacy
        return canonical, root

    def _read_document(self, document_id: str) -> bytes | None:
        path, root = self._document_path(document_id)
        _assert_managed_path(path, root)
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError(f"Managed cognition path is not a file: {path}")
        content = path.read_bytes()
        if len(content) > _MAX_DOCUMENT_BYTES:
            raise ValueError(f"Cognition document exceeds the sync size limit: {document_id}")
        return content

    def _write_document(
        self, document_id: str, content: bytes | None, expected_current: str | None
    ) -> Path | None:
        path, root = self._document_path(document_id)
        current = self._read_document(document_id)
        current_hash = hashlib.sha256(current).hexdigest() if current is not None else None
        if current_hash != expected_current:
            raise RuntimeError(f"Document changed during cognition sync: {document_id}")
        backup = None
        if current is not None:
            backup = self._backup_path("documents", document_id)
            shutil.copy2(path, backup)
        if content is None:
            if current is not None:
                path.unlink()
            return backup
        _atomic_write_bytes(path, content, root=root)
        return backup

    def _skill_paths(self, name: str) -> dict[str, Path]:
        _validate_skill_name(name)
        return {
            "active": self.global_home / "skills" / name,
            "candidate": self.sync_home / "candidates" / name,
            "quarantine": self.sync_home / "quarantine" / name,
        }

    def _active_skill_hash(self, name: str) -> str | None:
        path = self._skill_paths(name)["active"]
        if not path.exists() and not path.is_symlink():
            return None
        _assert_managed_path(path, self.global_home)
        return _bundle_hash(_read_bundle(path, managed_root=self.global_home))

    def _current_skill_hash(self, name: str) -> str | None:
        for path in self._skill_paths(name).values():
            if path.exists() or path.is_symlink():
                _assert_managed_path(path, self.global_home)
                return _bundle_hash(_read_bundle(path, managed_root=self.global_home))
        return None

    def _matching_skill_path(self, name: str, digest: str | None) -> Path | None:
        for path in self._skill_paths(name).values():
            if path.exists() or path.is_symlink():
                _assert_managed_path(path, self.global_home)
                if _bundle_hash(_read_bundle(path, managed_root=self.global_home)) == digest:
                    return path
        return None

    def _verify_effective_skill_hash(self, name: str, expected: str | None) -> None:
        current = self._current_skill_hash(name)
        if current != expected:
            raise RuntimeError(f"Skill changed during cognition sync: {name}")

    def _retire_skill_paths(self, name: str) -> dict[str, Path]:
        paths = self._skill_paths(name)
        backups: dict[str, Path] = {}
        try:
            for kind, path in paths.items():
                if not path.exists() and not path.is_symlink():
                    continue
                _assert_managed_path(path, self.global_home)
                if path.is_symlink() or not path.is_dir():
                    raise ValueError(f"Managed skill path is unsafe: {path}")
                backup = self._backup_path("skills", f"{kind}:{name}")
                _move_path(path, backup, root=self.global_home)
                backups[kind] = backup
        except Exception:
            self._restore_skill_backups(name, backups)
            raise
        return backups

    def _restore_skill_backups(self, name: str, backups: dict[str, Path]) -> None:
        paths = self._skill_paths(name)
        for kind, backup in reversed(list(backups.items())):
            _move_path(backup, paths[kind], root=self.global_home)

    def _install_directory(self, stage: Path, destination: Path) -> None:
        _ensure_directory(destination.parent, self.global_home)
        _assert_managed_path(destination, self.global_home)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"Skill destination already exists: {destination}")
        _move_path(stage, destination, root=self.global_home)

    def _stage_bundle(self, files: list[tuple[str, bytes, bool]]) -> Path:
        staging_root = self.sync_home / "staging"
        _ensure_directory(staging_root, self.global_home)
        stage = Path(tempfile.mkdtemp(prefix="bundle-", dir=staging_root))
        for relative, content, executable in files:
            destination = stage / relative
            _ensure_directory(destination.parent, stage)
            with destination.open("xb") as handle:
                handle.write(content)
            destination.chmod(0o755 if executable else 0o644)
        return stage

    def _enumerate_skill_bundles(self) -> dict[str, list[tuple[str, bytes, bool]]]:
        root = self.global_home / "skills"
        if not root.exists() and not root.is_symlink():
            return {}
        _assert_managed_path(root, self.global_home)
        if not root.is_dir():
            raise ValueError("Global skills path must be a directory.")
        skill_files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            for dirname in list(dirnames):
                candidate = current / dirname
                if candidate.is_symlink():
                    raise ValueError(f"Skill bundles cannot contain symlinks: {candidate}")
                if dirname in {"__pycache__", ".git"}:
                    dirnames.remove(dirname)
            for filename in filenames:
                candidate = current / filename
                if candidate.is_symlink():
                    raise ValueError(f"Skill bundles cannot contain symlinks: {candidate}")
                if filename == "SKILL.md":
                    skill_files.append(candidate)
        selected: list[Path] = []
        for skill_file in sorted(skill_files, key=lambda path: (len(path.parts), str(path))):
            if any(parent in skill_file.parents for parent in selected):
                continue
            selected.append(skill_file.parent)
        bundles: dict[str, list[tuple[str, bytes, bool]]] = {}
        folded_names: set[str] = set()
        for bundle_root in selected:
            name = bundle_root.relative_to(root).as_posix()
            _validate_skill_name(name)
            if name.casefold() in folded_names:
                raise ValueError(f"Duplicate skill bundle name: {name}")
            folded_names.add(name.casefold())
            bundles[name] = _read_bundle(bundle_root, managed_root=self.global_home)
        return bundles

    def _record_document_conflict(
        self,
        sender: str,
        record: dict[str, Any],
        content: bytes | None,
        local_hash: str | None,
        revision: int,
    ) -> Path:
        conflict_dir = self._conflict_dir(sender, "document", record["id"], revision)
        _ensure_directory(conflict_dir, self.global_home)
        metadata = {
            "sender_device_record_id": sender,
            "workspace_id": self.workspace_id,
            "id": record["id"],
            "revision": revision,
            "base_sha256": record["base_sha256"],
            "remote_sha256": record["sha256"],
            "local_sha256": local_hash,
            "deleted": record["deleted"],
        }
        _atomic_write_bytes(
            conflict_dir / "conflict.json",
            json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"),
            root=self.global_home,
        )
        if content is not None:
            _atomic_write_bytes(conflict_dir / "remote.md", content, root=self.global_home)
        return conflict_dir

    def _record_skill_conflict(
        self,
        sender: str,
        record: dict[str, Any],
        decoded: _DecodedSkill,
        local_hash: str | None,
        revision: int,
    ) -> Path:
        conflict_dir = self._conflict_dir(sender, "skill", record["id"], revision)
        _ensure_directory(conflict_dir, self.global_home)
        metadata = {
            "sender_device_record_id": sender,
            "workspace_id": self.workspace_id,
            "id": record["id"],
            "name": record["name"],
            "revision": revision,
            "base_sha256": record["base_sha256"],
            "remote_sha256": record["sha256"],
            "local_sha256": local_hash,
            "deleted": record["deleted"],
        }
        _atomic_write_bytes(
            conflict_dir / "conflict.json",
            json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"),
            root=self.global_home,
        )
        if decoded.stage is not None:
            destination = conflict_dir / "bundle"
            if destination.exists():
                backup = self._backup_path("conflicts", record["id"])
                _move_path(destination, backup, root=self.global_home)
            _move_path(decoded.stage, destination, root=self.global_home)
        return conflict_dir

    def _conflict_dir(
        self, sender: str, kind: str, entry_id: str, revision: int
    ) -> Path:
        sender_digest = hashlib.sha256(sender.encode("utf-8")).hexdigest()[:16]
        entry_digest = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()[:16]
        return self.sync_home / "conflicts" / self.workspace_id / sender_digest / f"{kind}-{entry_digest}-{revision}"

    def _write_scan_audit(self, destination: Path, report: Any) -> None:
        _atomic_write_bytes(
            destination.with_name(f"{destination.name}.scan.json"),
            json.dumps(report.to_dict(), indent=2, sort_keys=True).encode("utf-8"),
            root=self.global_home,
        )

    def _disable_ledger_skills(self, name: str) -> None:
        if not self._can_register_skills():
            return
        paths = [str(path) for path in self._skill_paths(name).values()]
        placeholders = ",".join("?" for _ in paths)
        with self.ledger._lock:
            self.ledger.conn.execute(
                f"UPDATE skills SET status = 'disabled' WHERE disk_path IN ({placeholders})",
                paths,
            )
            self.ledger.conn.commit()

    def _backup_path(self, category: str, entry_id: str) -> Path:
        digest = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()[:24]
        root = self.sync_home / "backups" / self.workspace_id / category
        _ensure_directory(root, self.global_home)
        return root / f"{digest}-{uuid.uuid4().hex}"

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists() and not self.state_path.is_symlink():
            return {
                "version": 1,
                "workspace_id": self.workspace_id,
                "revision": 0,
                "initialized": False,
                "manifest": None,
                "local": {"files": {}, "skills": {}},
                "sources": {},
            }
        _assert_managed_path(self.state_path, self.global_home)
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Cognition sync state is unreadable.") from exc
        if not isinstance(state, dict) or set(state) != {
            "version", "workspace_id", "revision", "initialized", "manifest", "local", "sources",
        }:
            raise ValueError("Cognition sync state has an invalid schema.")
        if state["version"] != 1 or state["workspace_id"] != self.workspace_id:
            raise ValueError("Cognition sync state does not match this workspace.")
        if (
            isinstance(state["revision"], bool)
            or not isinstance(state["revision"], int)
            or state["revision"] < 0
            or not isinstance(state["initialized"], bool)
        ):
            raise ValueError("Cognition sync state has invalid version metadata.")
        cached = state["manifest"]
        if cached is not None:
            if not isinstance(cached, dict) or cached.get("workspace_id") != self.workspace_id:
                raise ValueError("Cognition sync state contains an invalid cached manifest.")
            if not _is_hash(cached.get("manifest_sha256")) or _manifest_hash(cached) != cached["manifest_sha256"]:
                raise ValueError("Cognition sync state contains a tampered cached manifest.")
        local = state["local"]
        if not isinstance(local, dict) or set(local) != {"files", "skills"}:
            raise ValueError("Cognition sync state has an invalid local snapshot.")
        _validate_hash_map(local["files"], document_ids=set(_DOCUMENTS), allow_none=False)
        _validate_hash_map(local["skills"], skill_ids=True, allow_none=False)
        if not isinstance(state["sources"], dict):
            raise ValueError("Cognition sync state has invalid source history.")
        for sender, source in state["sources"].items():
            _validate_sender(sender)
            if not isinstance(source, dict) or set(source) != {
                "revision", "manifest_sha256", "files", "skills",
            }:
                raise ValueError("Cognition sync state has invalid source metadata.")
            if (
                isinstance(source["revision"], bool)
                or not isinstance(source["revision"], int)
                or source["revision"] < 1
                or not _is_hash(source["manifest_sha256"])
            ):
                raise ValueError("Cognition sync state has invalid source revision metadata.")
            _validate_hash_map(source["files"], document_ids=set(_DOCUMENTS), allow_none=True)
            _validate_hash_map(source["skills"], skill_ids=True, allow_none=True)
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        _ensure_directory(self.state_path.parent, self.global_home)
        _atomic_write_bytes(
            self.state_path,
            json.dumps(state, indent=2, sort_keys=True).encode("utf-8"),
            root=self.global_home,
        )


def _validate_workspace_id(value: Any) -> str:
    if not isinstance(value, str) or not _WORKSPACE_RE.fullmatch(value):
        raise ValueError("workspace_id must contain only letters, numbers, dots, underscores, or hyphens.")
    return value


def _validate_sender(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(char) < 32 for char in value):
        raise ValueError("sender_device_record_id must be a non-empty bounded string.")
    return value


def _validate_skill_name(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\\" in value:
        raise ValueError("Skill name must be a safe relative POSIX path.")
    parts = value.split("/")
    if len(parts) > 8 or any(not _COMPONENT_RE.fullmatch(part) for part in parts):
        raise ValueError("Skill name contains an unsafe path component.")
    return value


def _validate_bundle_path(value: Any, *, allow_nested: bool) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\\" in value:
        raise ValueError("Bundle path must be a safe relative POSIX path.")
    parts = value.split("/")
    if (not allow_nested and len(parts) != 1) or len(parts) > 8:
        raise ValueError("Bundle path contains an invalid nesting level.")
    if any(not _PATH_COMPONENT_RE.fullmatch(part) for part in parts):
        raise ValueError("Bundle path contains an unsafe path component.")
    return value


def _skill_id(name: str) -> str:
    return f"skill:{_validate_skill_name(name)}"


def _skill_name_from_id(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("skill:"):
        raise ValueError("Invalid skill sync id.")
    return _validate_skill_name(value[len("skill:"):])


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _validate_hash_map(
    value: Any, *, document_ids: set[str] | None = None, skill_ids: bool = False, allow_none: bool
) -> None:
    if not isinstance(value, dict):
        raise ValueError("Cognition sync hash map must be an object.")
    for key, digest in value.items():
        if document_ids is not None and key not in document_ids:
            raise ValueError("Cognition sync state contains an unknown document id.")
        if skill_ids:
            _skill_name_from_id(key)
        if digest is None and allow_none:
            continue
        if not _is_hash(digest):
            raise ValueError("Cognition sync state contains an invalid content hash.")


def _encode_content(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def _decode_content(value: Any, *, limit: int, label: str) -> bytes:
    if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
        raise ValueError(f"Invalid or oversized base64 content for {label}.")
    try:
        content = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid base64 content for {label}.") from exc
    if len(content) > limit or _encode_content(content) != value:
        raise ValueError(f"Invalid or oversized content for {label}.")
    return content


def _bundle_hash(files: list[tuple[str, bytes, bool]]) -> str:
    digest = hashlib.sha256()
    by_parent: dict[str, list[tuple[str, bytes]]] = {}
    child_directories: dict[str, set[str]] = {}
    for relative, content, _executable in files:
        parent, _, filename = relative.rpartition("/")
        by_parent.setdefault(parent, []).append((filename, content))
        while parent:
            prefix, _, directory = parent.rpartition("/")
            child_directories.setdefault(prefix, set()).add(directory)
            parent = prefix

    def visit(directory: str) -> None:
        for filename, content in sorted(by_parent.get(directory, [])):
            relative = f"{directory}/{filename}" if directory else filename
            digest.update(f"FILE:{relative}:".encode("utf-8"))
            digest.update(content)
            digest.update(b"\n")
        for child in sorted(child_directories.get(directory, set())):
            visit(f"{directory}/{child}" if directory else child)

    visit("")
    return digest.hexdigest()


def _read_bundle(
    root: Path, *, managed_root: Path | None = None
) -> list[tuple[str, bytes, bool]]:
    _assert_managed_path(root, managed_root or root.parent)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Skill bundle must be a real directory: {root}")
    files: list[tuple[str, bytes, bool]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for dirname in list(dirnames):
            candidate = current / dirname
            if candidate.is_symlink():
                raise ValueError(f"Skill bundles cannot contain symlinks: {candidate}")
            if dirname in {"__pycache__", ".git"}:
                dirnames.remove(dirname)
        for filename in sorted(filenames):
            path = current / filename
            relative = path.relative_to(root).as_posix()
            _validate_bundle_path(relative, allow_nested=True)
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Skill bundle contains an unsafe file: {path}")
            content = path.read_bytes()
            total += len(content)
            if total > _MAX_SKILL_BYTES or len(files) >= _MAX_SKILL_FILES:
                raise ValueError(f"Skill bundle exceeds the sync size limit: {root}")
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            files.append((relative, content, executable))
    if not any(relative == "SKILL.md" for relative, _content, _executable in files):
        raise ValueError(f"Skill bundle is missing SKILL.md: {root}")
    return files


def _skill_record(
    skill_id: str,
    name: str,
    files: list[tuple[str, bytes, bool]],
    digest: str,
    base: str | None,
) -> dict[str, Any]:
    return {
        "id": skill_id,
        "name": name,
        "deleted": False,
        "sha256": digest,
        "base_sha256": base,
        "files": [
            {
                "path": relative,
                "content_base64": _encode_content(content),
                "executable": executable,
            }
            for relative, content, executable in sorted(files, key=lambda item: item[0])
        ],
    }


def _deleted_record(
    entry_id: str, base: str, *, name: str | None = None
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": entry_id,
        "deleted": True,
        "sha256": None,
        "base_sha256": base,
    }
    if name is not None:
        record.update({"name": name, "files": []})
    else:
        record["content_base64"] = ""
    return record


def _manifest_hash(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    return hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()


def _validate_manifest(
    payload: Any,
) -> tuple[dict[str, Any], dict[str, bytes | None], dict[str, _DecodedSkill]]:
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_FIELDS:
        raise ValueError("Cognition manifest has an invalid schema.")
    try:
        encoded = canonical_json_bytes(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("Cognition manifest is not canonical JSON.") from exc
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise ValueError("Cognition manifest exceeds the sync size limit.")
    if payload["version"] != 1 or isinstance(payload["version"], bool):
        raise ValueError("Unsupported cognition manifest version.")
    _validate_workspace_id(payload["workspace_id"])
    revision = payload["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("Cognition manifest revision must be a positive integer.")
    if not _is_hash(payload["manifest_sha256"]) or payload["manifest_sha256"] != _manifest_hash(payload):
        raise ValueError("Cognition manifest hash mismatch.")
    if not isinstance(payload["files"], list) or len(payload["files"]) > len(_DOCUMENTS):
        raise ValueError("Cognition manifest has an invalid document list.")
    if not isinstance(payload["skills"], list) or len(payload["skills"]) > _MAX_SKILLS:
        raise ValueError("Cognition manifest has an invalid skill list.")

    decoded_documents: dict[str, bytes | None] = {}
    for record in payload["files"]:
        if not isinstance(record, dict) or set(record) != _DOCUMENT_FIELDS:
            raise ValueError("Cognition document record has an invalid schema.")
        document_id = record["id"]
        if (
            not isinstance(document_id, str)
            or document_id not in _DOCUMENTS
            or document_id in decoded_documents
        ):
            raise ValueError("Cognition manifest contains an unknown or duplicate document id.")
        _validate_record_hashes(record)
        if not isinstance(record["deleted"], bool):
            raise ValueError("Cognition document deletion flag must be boolean.")
        content = _decode_content(
            record["content_base64"], limit=_MAX_DOCUMENT_BYTES, label=document_id
        )
        if record["deleted"]:
            if record["sha256"] is not None or content:
                raise ValueError("Deleted cognition documents cannot contain content.")
        elif record["sha256"] != hashlib.sha256(content).hexdigest():
            raise ValueError(f"Cognition document hash mismatch: {document_id}")
        decoded_documents[document_id] = None if record["deleted"] else content

    decoded_skills: dict[str, _DecodedSkill] = {}
    declared_names: set[str] = set()
    total_skill_bytes = 0
    for record in payload["skills"]:
        if not isinstance(record, dict) or set(record) != _SKILL_FIELDS:
            raise ValueError("Cognition skill record has an invalid schema.")
        name = _validate_skill_name(record["name"])
        skill_id = _skill_id(name)
        if record["id"] != skill_id or skill_id in decoded_skills:
            raise ValueError("Cognition manifest contains an invalid or duplicate skill id.")
        _validate_record_hashes(record)
        if not isinstance(record["deleted"], bool) or not isinstance(record["files"], list):
            raise ValueError("Cognition skill record has invalid deletion metadata.")
        if record["deleted"]:
            if record["sha256"] is not None or record["files"]:
                raise ValueError("Deleted skills cannot contain bundle files.")
            decoded_skills[skill_id] = _DecodedSkill(record, [])
            continue
        if not record["files"] or len(record["files"]) > _MAX_SKILL_FILES:
            raise ValueError("Cognition skill bundle has an invalid file count.")
        files: list[tuple[str, bytes, bool]] = []
        paths: set[str] = set()
        folded_paths: set[str] = set()
        skill_bytes = 0
        for bundle_file in record["files"]:
            if not isinstance(bundle_file, dict) or set(bundle_file) != _BUNDLE_FILE_FIELDS:
                raise ValueError("Cognition skill file has an invalid schema.")
            relative = _validate_bundle_path(bundle_file["path"], allow_nested=True)
            if relative in paths or relative.casefold() in folded_paths:
                raise ValueError("Cognition skill bundle contains duplicate paths.")
            folded_relative = relative.casefold()
            if any(
                folded_relative.startswith(f"{existing}/") or existing.startswith(f"{folded_relative}/")
                for existing in folded_paths
            ):
                raise ValueError("Cognition skill bundle contains conflicting file and directory paths.")
            if not isinstance(bundle_file["executable"], bool):
                raise ValueError("Cognition skill executable flag must be boolean.")
            content = _decode_content(
                bundle_file["content_base64"], limit=_MAX_SKILL_BYTES, label=relative
            )
            paths.add(relative)
            folded_paths.add(relative.casefold())
            skill_bytes += len(content)
            total_skill_bytes += len(content)
            if skill_bytes > _MAX_SKILL_BYTES or total_skill_bytes > _MAX_MANIFEST_BYTES:
                raise ValueError("Cognition skill bundles exceed the sync size limit.")
            files.append((relative, content, bundle_file["executable"]))
        if "SKILL.md" not in paths:
            raise ValueError("Cognition skill bundle is missing SKILL.md.")
        if record["sha256"] != _bundle_hash(files):
            raise ValueError(f"Cognition skill bundle hash mismatch: {name}")
        try:
            skill_text = next(content for path, content, _exec in files if path == "SKILL.md").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("SKILL.md must be valid UTF-8.") from exc
        frontmatter, _body = parse_skill_frontmatter(skill_text)
        declared = frontmatter.get("name")
        if not isinstance(declared, str) or not _COMPONENT_RE.fullmatch(declared):
            raise ValueError("SKILL.md must declare a safe skill name.")
        if declared != name.rsplit("/", 1)[-1] or declared in declared_names:
            raise ValueError("SKILL.md name does not uniquely match its bundle directory.")
        declared_names.add(declared)
        decoded_skills[skill_id] = _DecodedSkill(record, files)
    return payload, decoded_documents, decoded_skills


def _validate_record_hashes(record: dict[str, Any]) -> None:
    base = record["base_sha256"]
    if base is not None and not _is_hash(base):
        raise ValueError("Cognition base hash must be null or a SHA-256 digest.")
    digest = record["sha256"]
    if digest is not None and not _is_hash(digest):
        raise ValueError("Cognition content hash must be null or a SHA-256 digest.")


def _classify_change(current: str | None, remote: str | None, base: str | None) -> str:
    if current == remote:
        return "unchanged"
    if current == base:
        return "apply"
    return "conflict"


def _set_local_value(values: dict[str, str], key: str, digest: str | None) -> None:
    if digest is None:
        values.pop(key, None)
    else:
        values[key] = digest


def _assert_managed_path(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Managed path escapes its root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Managed paths cannot contain symlinks: {current}")


def _ensure_directory(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Managed directory escapes its root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Managed directories cannot contain symlinks: {current}")
        if current.exists() and not current.is_dir():
            raise ValueError(f"Managed directory path is not a directory: {current}")
        current.mkdir(exist_ok=True)


def _atomic_write_bytes(path: Path, content: bytes, *, root: Path) -> None:
    _ensure_directory(path.parent, root)
    _assert_managed_path(path, root)
    if path.exists() and not path.is_file():
        raise ValueError(f"Managed file path is not a regular file: {path}")
    temporary = path.parent / f".{path.name}.hex-sync-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _move_path(source: Path, destination: Path, *, root: Path) -> None:
    _ensure_directory(destination.parent, root)
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(source), str(destination))

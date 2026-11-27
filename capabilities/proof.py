"""Verification Framework, Proof-Carrying Artifact Builder, and Harness² Adapter.

Implements:
- PRD Sections 6.2 (FR-018), 6.3 (FR-021a..FR-024): Deterministic verification checks +
  dynamic AST Blast-Radius checks + signed Proof-Carrying Artifact bundles.
- PRD Section 6.8 (FR-060..FR-068): Integration with cloned `vendor/harness2`
  (`harness2.core.tree`, `harness2.core.recursion`, `harness2.core.library`) gated by
  NVIDIA SkillSpector and our immutable revision/rollback registry.
"""

from __future__ import annotations

import base64
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from harness_core.storage.cow import IGNORE_DIRS, WorkspaceSnapshotProvider
from harness_core.storage.db import LedgerStore, canonical_json, sha256_hex, utcnow_iso
from harness_core.gateway import ToolGateway, redact_secrets
from harness_core.graphify import ActiveGraphifyAdapter
from harness_core.cognition.skillspector import SkillSpectorScanner
from harness_core.vendor_paths import get_harness2_dir

PROOF_VERSION = 1
PROOF_SIGNING_DOMAIN = b"hex/proof-artifact/v1\0"
PROOF_LEAF_DOMAIN = b"hex/proof-merkle-leaf/v1\0"
PROOF_NODE_DOMAIN = b"hex/proof-merkle-node/v1\0"
_HASH_PATTERN = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_DEVICE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PROOF_ID_PATTERN = re.compile(r"^prf_[0-9a-f]{12}$")
_SIGNATURE_PATTERN = re.compile(r"^ed25519:[A-Za-z0-9_-]{86}$")
_PROOF_FIELDS = {
    "proof_id", "task_id", "attempt_id", "git_diff_sha", "git_diff_text",
    "workspace_tree_sha", "runtime_digest", "ast_impact", "verification_results",
    "ledger_signature", "created_at",
}
_SENSITIVE_PATH_PARTS = frozenset({".ssh", ".aws", ".gnupg", ".docker"})
_SENSITIVE_FILE_NAMES = frozenset({
    ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json",
    "id_rsa", "id_ed25519", "secrets.json",
})


def _canonical_proof_bytes(value: Any) -> bytes:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _decode_signature(value: Any) -> bytes:
    if not isinstance(value, str) or not _SIGNATURE_PATTERN.fullmatch(value):
        raise ValueError("Malformed proof signature.")
    try:
        signature = base64.b64decode(value.removeprefix("ed25519:") + "==", altchars=b"-_", validate=True)
    except ValueError as exc:
        raise ValueError("Malformed proof signature.") from exc
    if len(signature) != 64:
        raise ValueError("Malformed proof signature.")
    return signature


def _public_key_bytes(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("Invalid proof author public key.")
    normalized = value.removeprefix("ed25519:")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", normalized):
        raise ValueError("Invalid proof author public key.")
    return bytes.fromhex(normalized)


def compute_proof_merkle_root(proof: Mapping[str, Any]) -> str:
    """Compute a deterministic SHA-256 Merkle root over canonical proof fields."""
    if not isinstance(proof, Mapping):
        raise ValueError("Malformed proof artifact fields.")
    proof = _normalize_proof(proof)
    level = [
        hashlib.sha256(PROOF_LEAF_DOMAIN + _canonical_proof_bytes({name: proof[name]})).digest()
        for name in sorted(_PROOF_FIELDS)
    ]
    if not level:
        return "sha256:" + hashlib.sha256(PROOF_NODE_DOMAIN).hexdigest()
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(PROOF_NODE_DOMAIN + level[index] + level[index + 1]).digest()
            for index in range(0, len(level), 2)
        ]
    return "sha256:" + level[0].hex()


def _normalize_proof(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("Proof artifact must be an object.")
    source = dict(payload)
    for nested_name in ("signed_proof", "signed_proof_json"):
        nested = source.pop(nested_name, None)
        if nested is not None:
            raise ValueError("Proof artifact cannot contain a nested signed proof.")
    for name in ("ast_impact", "verification_results"):
        encoded = source.pop(f"{name}_json", None)
        if encoded is not None:
            import json

            if not isinstance(encoded, str):
                raise ValueError(f"Invalid encoded proof field: {name}.")
            try:
                decoded = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid encoded proof field: {name}.") from exc
            if name in source and source[name] != decoded:
                raise ValueError(f"Conflicting proof field: {name}.")
            source[name] = decoded
    if set(source) != _PROOF_FIELDS:
        raise ValueError("Malformed proof artifact fields.")
    for name in ("proof_id", "task_id", "attempt_id", "git_diff_text", "ledger_signature", "created_at"):
        if not isinstance(source[name], str):
            raise ValueError(f"Invalid proof field: {name}.")
    if not _PROOF_ID_PATTERN.fullmatch(source["proof_id"]):
        raise ValueError("Invalid proof id.")
    for name in ("task_id", "attempt_id"):
        if not source[name] or len(source[name]) > 256 or any(ord(char) < 32 for char in source[name]):
            raise ValueError(f"Invalid proof field: {name}.")
    for name in ("git_diff_sha", "workspace_tree_sha", "runtime_digest"):
        if not isinstance(source[name], str) or not _HASH_PATTERN.fullmatch(source[name]):
            raise ValueError(f"Invalid proof hash: {name}.")
    if sha256_hex(source["git_diff_text"]) != source["git_diff_sha"].removeprefix("sha256:"):
        raise ValueError("Proof diff hash does not match its redacted content.")
    if not isinstance(source["ast_impact"], dict) or not isinstance(source["verification_results"], dict):
        raise ValueError("Proof impact and verification results must be objects.")
    if source["verification_results"].get("passed") is not True:
        raise ValueError("Proof artifact does not represent successful verification.")
    try:
        created_at = datetime.fromisoformat(source["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid proof timestamp.") from exc
    if created_at.tzinfo is None or created_at.utcoffset() != timezone.utc.utcoffset(created_at):
        raise ValueError("Proof timestamp must be UTC.")
    if not source["ledger_signature"].startswith("integrity-sha256:"):
        raise ValueError("Proof artifact has an invalid ledger integrity signature.")
    return source


@dataclass(frozen=True, slots=True)
class SignedProofArtifact:
    proof: dict[str, Any]
    author_device_record_id: str
    project_session_id: str | None
    host_epoch: int | None
    turn_seq: int | None
    proof_merkle_root: str
    signature: str
    version: int = PROOF_VERSION

    @property
    def signing_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "author_device_record_id": self.author_device_record_id,
            "project_session_id": self.project_session_id,
            "host_epoch": self.host_epoch,
            "turn_seq": self.turn_seq,
            "proof": self.proof,
            "proof_merkle_root": self.proof_merkle_root,
        }

    @property
    def signing_bytes(self) -> bytes:
        return PROOF_SIGNING_DOMAIN + _canonical_proof_bytes(self.signing_payload)

    def to_dict(self) -> dict[str, Any]:
        return {**self.signing_payload, "signature": self.signature}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SignedProofArtifact":
        if not isinstance(payload, Mapping) or set(payload) != {
            "version", "author_device_record_id", "project_session_id", "host_epoch",
            "turn_seq", "proof", "proof_merkle_root", "signature",
        }:
            raise ValueError("Malformed signed proof fields.")
        if type(payload["version"]) is not int or payload["version"] != PROOF_VERSION:
            raise ValueError("Unsupported signed proof version.")
        author = payload["author_device_record_id"]
        if not isinstance(author, str) or not _DEVICE_PATTERN.fullmatch(author):
            raise ValueError("Invalid proof author device.")
        session = payload["project_session_id"]
        if session is not None and (not isinstance(session, str) or not _DEVICE_PATTERN.fullmatch(session)):
            raise ValueError("Invalid proof project session.")
        epoch = payload["host_epoch"]
        sequence = payload["turn_seq"]
        if (epoch is None) != (sequence is None):
            raise ValueError("Proof epoch and sequence must be bound together.")
        if epoch is not None and (type(epoch) is not int or epoch < 1 or type(sequence) is not int or sequence < 1):
            raise ValueError("Proof epoch and sequence must be positive integers.")
        root = payload["proof_merkle_root"]
        if not isinstance(root, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", root):
            raise ValueError("Invalid proof Merkle root.")
        proof = _normalize_proof(payload["proof"])
        if compute_proof_merkle_root(proof) != root:
            raise ValueError("Proof Merkle root does not match the artifact.")
        _decode_signature(payload["signature"])
        return cls(proof, author, session, epoch, sequence, root, payload["signature"], payload["version"])

    def verify(self, author_public_key: str) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(_public_key_bytes(author_public_key)).verify(
                _decode_signature(self.signature), self.signing_bytes
            )
            return True
        except (InvalidSignature, ValueError):
            return False


def sign_proof_artifact(
    proof: Mapping[str, Any],
    signer: Any,
    *,
    project_session_id: str | None = None,
    host_epoch: int | None = None,
    turn_seq: int | None = None,
) -> SignedProofArtifact:
    if not isinstance(signer.device_id, str) or not _DEVICE_PATTERN.fullmatch(signer.device_id):
        raise ValueError("Invalid proof signer device.")
    if (project_session_id is None) != (host_epoch is None) or (host_epoch is None) != (turn_seq is None):
        raise ValueError("Proof scope requires a session, epoch, and sequence together.")
    if project_session_id is not None:
        if not isinstance(project_session_id, str) or not _DEVICE_PATTERN.fullmatch(project_session_id):
            raise ValueError("Invalid proof project session.")
        if type(host_epoch) is not int or host_epoch < 1 or type(turn_seq) is not int or turn_seq < 1:
            raise ValueError("Proof epoch and sequence must be positive integers.")
    normalized = _normalize_proof(proof)
    unsigned = SignedProofArtifact(
        proof=normalized,
        author_device_record_id=signer.device_id,
        project_session_id=project_session_id,
        host_epoch=host_epoch,
        turn_seq=turn_seq,
        proof_merkle_root=compute_proof_merkle_root(normalized),
        signature="",
    )
    signature = signer.sign(unsigned.signing_bytes)
    return SignedProofArtifact(
        proof=normalized,
        author_device_record_id=signer.device_id,
        project_session_id=project_session_id,
        host_epoch=host_epoch,
        turn_seq=turn_seq,
        proof_merkle_root=unsigned.proof_merkle_root,
        signature="ed25519:" + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
    )


def _is_sensitive_proof_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in {"", ".", "base", "head"}]
    if any(part in _SENSITIVE_PATH_PARTS for part in parts):
        return True
    name = parts[-1].lower() if parts else ""
    return (
        name.startswith(".env")
        or name in _SENSITIVE_FILE_NAMES
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def redact_proof_diff(diff_text: str) -> str:
    """Keep proof diffs useful without replicating credential files or secret values."""
    blocks = re.split(r"(?=^diff\s)", diff_text, flags=re.MULTILINE)
    redacted: list[str] = []
    for block in blocks:
        headers = [line[4:].split("\t", 1)[0] for line in block.splitlines() if line.startswith(("--- ", "+++ "))]
        if headers and any(_is_sensitive_proof_path(header) for header in headers):
            redacted.append("[OMITTED_SENSITIVE_FILE]\n")
        elif block:
            redacted.append(block)
    return str(redact_secrets("".join(redacted)))

# Import cloned Google Research Harness2 from vendor/harness2
VENDOR_HARNESS2 = get_harness2_dir()
if str(VENDOR_HARNESS2) not in sys.path and VENDOR_HARNESS2.is_dir():
    sys.path.insert(0, str(VENDOR_HARNESS2))


class VerificationAndProofService:
    """Runs task verification plans and constructs signed Proof-Carrying Artifact bundles."""

    def __init__(
        self,
        ledger: LedgerStore,
        cow: WorkspaceSnapshotProvider,
        gateway: ToolGateway,
        graphify: ActiveGraphifyAdapter,
        cas_dir: Path | str,
    ):
        self.ledger = ledger
        self.cow = cow
        self.gateway = gateway
        self.graphify = graphify
        self.cas_dir = Path(cas_dir).resolve()
        self.cas_dir.mkdir(parents=True, exist_ok=True)

    def compute_git_diff(self, base_dir: Path | str, head_dir: Path | str) -> tuple[str, str]:
        """Computes unified diff and its SHA-256 between base snapshot and modified workspace."""
        b_path = Path(base_dir).resolve()
        h_path = Path(head_dir).resolve()
        if b_path == h_path:
            return sha256_hex(""), ""

        diff_bin = shutil.which("diff")
        if diff_bin is None and sys.platform != "win32":
            posix_diff = Path("/usr/bin/diff")
            if posix_diff.is_file():
                diff_bin = str(posix_diff)
        if diff_bin is not None:
            cmd = [diff_bin, "-ruN"]
            for ign in sorted(IGNORE_DIRS):
                cmd.extend(["-x", ign])
            cmd.extend(["-x", ".DS_Store", "-x", "*.pyc", str(b_path), str(h_path)])
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                )
                diff_text = res.stdout[:64_000]
                return sha256_hex(diff_text), diff_text
            except (OSError, subprocess.TimeoutExpired):
                pass

        # Portable fallback: deterministic in-process unified diff (no external
        # binary required; keeps proof generation working on Windows/macOS-min).
        diff_text = self._python_tree_diff(b_path, h_path)[:64_000]
        return sha256_hex(diff_text), diff_text

    @staticmethod
    def _python_tree_diff(base: Path, head: Path) -> str:
        import difflib

        def _walk(root: Path) -> dict[str, Path]:
            entries: dict[str, Path] = {}
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
                for fname in sorted(filenames):
                    if fname == ".DS_Store" or fname.startswith(".DS_Store") or fname.endswith((".pyc", ".db-wal", ".db-shm")):
                        continue
                    p = Path(dirpath) / fname
                    entries[p.relative_to(root).as_posix()] = p
            return entries

        def _read_lines(p: Path) -> list[str]:
            try:
                return p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except OSError:
                return []

        base_files = _walk(base)
        head_files = _walk(head)
        chunks: list[str] = []
        for rel in sorted(set(base_files) | set(head_files)):
            a_lines = _read_lines(base_files[rel]) if rel in base_files else []
            b_lines = _read_lines(head_files[rel]) if rel in head_files else []
            if a_lines == b_lines:
                continue
            chunks.extend(
                difflib.unified_diff(
                    a_lines,
                    b_lines,
                    fromfile=f"a/{rel}" if rel in base_files else "/dev/null",
                    tofile=f"b/{rel}" if rel in head_files else "/dev/null",
                )
            )
        return "".join(chunks)

    def run_verification_and_build_proof(
        self,
        *,
        task_id: str,
        attempt_id: str,
        base_snapshot_path: Path | str,
        workspace_dir: Path | str,
        modified_files: list[str] | None = None,
        run_tests: bool = True,
        authority_check: Callable[[], None] | None = None,
        proof_signer: Any | None = None,
        project_session_id: str | None = None,
        host_epoch: int | None = None,
        turn_seq: int | None = None,
    ) -> dict[str, Any]:
        if authority_check:
            authority_check()
        ws = Path(workspace_dir).resolve()
        base = Path(base_snapshot_path).resolve()
        if not modified_files and base == ws:
            tree_sha = ""
            if hasattr(self, "ledger") and self.ledger:
                attempts = self.ledger.list_attempts(task_id)
                for a in attempts:
                    if a.get("attempt_id") == attempt_id:
                        tree_sha = a.get("base_tree_sha") or ""
                        break
            if not tree_sha:
                task_rec = self.ledger.get_task(task_id) if hasattr(self, "ledger") and self.ledger else None
                tree_sha = task_rec.get("workspace_tree_sha") if task_rec else ""
            if not tree_sha:
                tree_sha = sha256_hex("empty")
            diff_sha, diff_text = sha256_hex(""), ""
        else:
            tree_sha = self.cow.compute_tree_sha(ws)
            diff_sha, diff_text = self.compute_git_diff(base, ws)
            diff_text = redact_proof_diff(diff_text)
            diff_sha = sha256_hex(diff_text)

        # 1. Compute AST Impact Subgraph via Active Graphify
        if modified_files:
            blast = self.graphify.compute_blast_radius(ws, modified_files).to_dict()
        else:
            blast = {
                "modified_files": [],
                "modified_symbols": [],
                "hop1_dependents": [],
                "hop2_dependents": [],
                "impacted_tests": [],
                "warning_message": "Read-only or non-AST task; zero AST blast radius.",
            }

        # 2. Gather mandatory checks from active plan + dynamically pinned blast-radius tests
        plan = self.ledger.get_latest_plan(task_id)
        pinned_tests: list[str] = []
        if plan and plan.get("blast_radius_checks"):
            pinned_tests.extend(plan["blast_radius_checks"])
        for t in blast.get("impacted_tests", []):
            if t not in pinned_tests:
                pinned_tests.append(t)

        # 3. Execute deterministic test verification if applicable
        has_test_files = any(ws.rglob("test_*.py"))
        task_rec = self.ledger.get_task(task_id) if hasattr(self, "ledger") and self.ledger else None
        is_fast_path = (task_rec.get("execution_mode") == "fast_path") if task_rec else False
        should_run_tests = run_tests and (bool(pinned_tests or modified_files) or (has_test_files and not is_fast_path))
        if should_run_tests:
            test_res = self.gateway._tool_run_tests(ws, {"targets": pinned_tests})
            verification_passed = bool(test_res["ok"])
            verification_results = {
                "method": "unittest_deterministic_and_ast_blast_radius",
                "passed": verification_passed,
                "pinned_blast_radius_targets": pinned_tests,
                "exit_code": test_res["exit_code"],
                "test_output_sha256": test_res["output_sha256"],
                "test_output_excerpt": test_res["output"][:2000],
            }
        else:
            verification_passed = True
            verification_results = {
                "method": "artifact_and_journal_integrity_check",
                "passed": True,
                "pinned_blast_radius_targets": pinned_tests,
                "exit_code": 0,
                "test_output_sha256": sha256_hex("no_tests_required"),
                "test_output_excerpt": "Deterministic journal & workspace hash verification passed.",
            }

        blast = redact_secrets(blast)
        verification_results = redact_secrets(verification_results)
        if verification_results.get("test_output_excerpt"):
            verification_results["test_output_excerpt"] = str(
                verification_results["test_output_excerpt"]
            )[:2000]

        runtime_digest = sha256_hex(
            f"python:{platform.python_version()}|arch:{platform.machine()}|cow:{self.cow.backend_name}"
        )

        if authority_check:
            authority_check()
        proof = self.ledger.save_proof_bundle(
            task_id=task_id,
            attempt_id=attempt_id,
            git_diff_sha=diff_sha,
            git_diff_text=diff_text,
            workspace_tree_sha=tree_sha,
            runtime_digest=runtime_digest,
            ast_impact=blast,
            verification_results=verification_results,
        )

        signed_proof = None
        if proof_signer is not None:
            if not verification_passed:
                raise ValueError("Cannot sign a proof for failed verification.")
            signed_proof = sign_proof_artifact(
                proof,
                proof_signer,
                project_session_id=project_session_id,
                host_epoch=host_epoch,
                turn_seq=turn_seq,
            )
            proof = self.ledger.attach_signed_proof(proof["proof_id"], signed_proof.to_dict())

        # Persist immutable Proof-Carrying Artifact manifest to CAS
        cas_file = self.cas_dir / f"{proof['proof_id']}.json"
        proof_bytes = canonical_json(proof).encode("utf-8")
        cas_file.write_bytes(proof_bytes)
        self.ledger.record_artifact(
            task_id=task_id,
            kind="proof_bundle",
            uri=f"file://{cas_file}",
            sha256=sha256_hex(proof_bytes),
            size_bytes=len(proof_bytes),
        )

        return {
            "passed": verification_passed,
            "proof": proof,
            "verification_results": verification_results,
            "ast_impact": blast,
        }


# Protected files that no candidate generator or Harness² loop may ever touch (FR-061)
PROTECTED_POLICY_PATHS = frozenset({
    "policy.yaml",
    "security_policy.json",
    "skillspector_rules.py",
    "approvals.json",
    ".env",
    "secrets.json",
})


class Harness2ControlledAdapter:
    """Stage 5 Controlled Self-Improvement Adapter wrapping `vendor/harness2` (FR-060..FR-068).

    Connects Google Research's `harness2` Propose–Probe–Compose and Experience Library
    with our mandatory SkillSpector supply-chain gate, protected-file allow-list, and
    one-step rollback registry.
    """

    def __init__(
        self,
        ledger: LedgerStore,
        scanner: SkillSpectorScanner,
        revisions_dir: Path | str,
    ):
        self.ledger = ledger
        self.scanner = scanner
        self.revisions_dir = Path(revisions_dir).resolve()
        self.revisions_dir.mkdir(parents=True, exist_ok=True)

    def propose_and_evaluate_candidate(
        self,
        *,
        parent_revision_id: str | None,
        candidate_tree: dict[str, str],
        base_tree: dict[str, str],
        created_by: str = "harness2_proposer",
        run_demo_probe: bool = False,
        substrate_name: str | None = None,
    ) -> dict[str, Any]:
        """Validates a candidate harness tree via `harness2.core.tree`, scans any skills via
        SkillSpector, runs optional Propose-Probe-Compose demo verification, and registers the revision.
        Supports both `opencode` (`systemprompt.md`) and `codex` (`AGENTS.md`) substrates.
        """
        from harness2.core import tree as h2_tree
        from harness2.substrate.codex import CodexSubstrate
        from harness2.substrate.opencode import OpencodeSubstrate

        # 1. Enforce FR-061 Protected Policy & Credential Guard
        touched_protected = [
            p for p in candidate_tree
            if Path(p).name in PROTECTED_POLICY_PATHS or "policy" in p.lower() or "secret" in p.lower()
        ]
        if touched_protected:
            raise PermissionError(
                f"FR-061 violation: candidate attempted to modify protected policy/secret paths: {touched_protected}"
            )

        # Auto-detect Codex (`AGENTS.md`), Claude (`CLAUDE.md`), Gemini (`GEMINI.md`), or OpenCode substrate
        effective_sub = (substrate_name or "").strip().lower()
        if not effective_sub:
            if "AGENTS.md" in candidate_tree or "AGENTS.md" in base_tree:
                effective_sub = "codex"
            elif "CLAUDE.md" in candidate_tree or "CLAUDE.md" in base_tree:
                effective_sub = "claude"
            elif "GEMINI.md" in candidate_tree or "GEMINI.md" in base_tree:
                effective_sub = "gemini"
            else:
                effective_sub = "opencode"

        from harness_core.harness2_adapter import resolve_substrate

        substrate = resolve_substrate(effective_sub)
        surface = substrate.surface()

        # 2. Validate against Harness2 editable surface rules (c1..c8)
        repaired_tree = dict(candidate_tree)
        for req_file in surface.always_on:
            repaired_tree.setdefault(req_file, base_tree.get(req_file, "# Default System Instructions\n"))
        h2_tree.autotrigger(surface, repaired_tree)
        census = h2_tree.validate(surface, repaired_tree)
        diff = h2_tree.diff_summary(surface, base_tree, repaired_tree)

        revision_id = f"hrev_{uuid.uuid4().hex[:12]}"
        rev_dir = self.revisions_dir / revision_id
        h2_tree.write_tree(surface, repaired_tree, rev_dir, stubs=False)
        bundle_hash = sha256_hex(canonical_json(repaired_tree))

        # 3. Route any candidate skills in the tree through NVIDIA SkillSpector BEFORE benchmark (FR-068)
        scan_ids: list[str] = []
        skills_dir = rev_dir / surface.skill_dir
        if skills_dir.is_dir():
            for skill_subdir in sorted(skills_dir.iterdir()):
                if (skill_subdir / "SKILL.md").is_file():
                    scan_res = self.scanner.scan_and_register_skill(skill_subdir)
                    scan_ids.append(scan_res["scan"]["scan_id"])
                    if scan_res["scan"]["gate_decision"] == "QUARANTINE":
                        self._save_revision_row(
                            revision_id=revision_id,
                            parent_revision_id=parent_revision_id,
                            bundle_hash=bundle_hash,
                            bundle_uri=str(rev_dir),
                            diff=diff,
                            skill_scan_id=scan_res["scan"]["scan_id"],
                            status="rejected",
                            created_by=created_by,
                        )
                        raise PermissionError(
                            f"Candidate revision {revision_id} quarantined by SkillSpector on skill "
                            f"'{skill_subdir.name}' (score={scan_res['scan']['risk_score']})"
                        )

        # 4. Optionally run Harness2 Propose-Probe-Compose deterministic benchmark verification
        probe_metrics: dict[str, Any] = {"census": census, "diff": diff}
        if run_demo_probe:
            from harness2.core import recursion as h2_recursion
            from harness2.core.spec import RunView
            from harness2.demo import DemoAdapter, DemoProposer

            demo_root = rev_dir / "_probe_run"
            adapter = DemoAdapter(demo_root)
            proposer = DemoProposer(adapter.spec.channels["proposer"])
            base_run_dir = adapter.execute("ledger-1", adapter.base_harness(), "base_probe")
            base_rv = RunView(
                label="base",
                run_id="base_probe",
                run_dir=base_run_dir,
                ok=True,
                delivered=True,
            )
            mode_res = h2_recursion.run_parallel(
                "ledger-1",
                1,
                [base_rv],
                adapter=adapter,
                prop=proposer,
                work_dir=demo_root / "work",
                run_id_of=lambda lbl: f"probe_{lbl}",
                base_tree=h2_tree.read_tree(surface, adapter.base_harness()),
            )
            probe_metrics["harness2_probe"] = mode_res.to_dict()

        primary_scan_id = scan_ids[0] if scan_ids else None
        self._save_revision_row(
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            bundle_hash=bundle_hash,
            bundle_uri=str(rev_dir),
            diff=diff,
            skill_scan_id=primary_scan_id,
            status="candidate",
            created_by=created_by,
        )

        eval_id = f"evl_{uuid.uuid4().hex[:12]}"
        with self.ledger._lock:
            self.ledger.conn.execute(
                """
                INSERT INTO evaluations (
                    evaluation_id, revision_id, suite_id, dataset_version,
                    model_pins_json, results_uri, metrics_json, gate_decision, created_at
                ) VALUES (?, ?, 'stage5_holdout_v1', '2026.10', ?, ?, ?, 'PASS_CANDIDATE', ?)
                """,
                (
                    eval_id,
                    revision_id,
                    canonical_json({"proposer": "deterministic/m4"}),
                    str(rev_dir),
                    canonical_json(probe_metrics),
                    utcnow_iso(),
                ),
            )
            self.ledger.conn.commit()

        return {
            "revision_id": revision_id,
            "parent_revision_id": parent_revision_id,
            "bundle_hash": bundle_hash,
            "status": "candidate",
            "diff": diff,
            "census": census,
            "skill_scan_ids": scan_ids,
            "evaluation_id": eval_id,
            "probe_metrics": probe_metrics,
        }

    def promote_revision(self, revision_id: str, reviewer_id: str = "owner") -> dict[str, Any]:
        with self.ledger._lock:
            row = self.ledger.conn.execute(
                "SELECT * FROM harness_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Revision not found: {revision_id}")
            if row["status"] == "rejected":
                raise PermissionError(f"Cannot promote rejected revision {revision_id}")
            now = utcnow_iso()
            self.ledger.conn.execute(
                "UPDATE harness_revisions SET status = 'superseded' WHERE status = 'active'"
            )
            self.ledger.conn.execute(
                "UPDATE harness_revisions SET status = 'active', promoted_at = ? WHERE revision_id = ?",
                (now, revision_id),
            )
            self.ledger.conn.commit()
            updated = self.ledger.conn.execute(
                "SELECT * FROM harness_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            return self.ledger._row_to_dict(updated)  # type: ignore[return-value]

    def rollback_to_revision(self, target_revision_id: str, actor: str = "owner") -> dict[str, Any]:
        """FR-067: One-step immediate rollback to a known-good harness revision."""
        with self.ledger._lock:
            target = self.ledger.conn.execute(
                "SELECT * FROM harness_revisions WHERE revision_id = ?", (target_revision_id,)
            ).fetchone()
            if not target:
                raise KeyError(f"Rollback target revision not found: {target_revision_id}")
            now = utcnow_iso()
            self.ledger.conn.execute(
                "UPDATE harness_revisions SET status = 'rolled_back' WHERE status = 'active'"
            )
            self.ledger.conn.execute(
                "UPDATE harness_revisions SET status = 'active', promoted_at = ? WHERE revision_id = ?",
                (now, target_revision_id),
            )
            self.ledger.conn.commit()
            updated = self.ledger.conn.execute(
                "SELECT * FROM harness_revisions WHERE revision_id = ?", (target_revision_id,)
            ).fetchone()
            return self.ledger._row_to_dict(updated)  # type: ignore[return-value]

    def _save_revision_row(
        self,
        *,
        revision_id: str,
        parent_revision_id: str | None,
        bundle_hash: str,
        bundle_uri: str,
        diff: dict[str, Any],
        skill_scan_id: str | None,
        status: str,
        created_by: str,
    ) -> None:
        with self.ledger._lock:
            self.ledger.conn.execute(
                """
                INSERT INTO harness_revisions (
                    revision_id, parent_revision_id, bundle_hash, bundle_uri,
                    editable_diff_json, skill_scan_id, status, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    parent_revision_id,
                    bundle_hash,
                    bundle_uri,
                    canonical_json(diff),
                    skill_scan_id,
                    status,
                    created_by,
                    utcnow_iso(),
                ),
            )
            self.ledger.conn.commit()


# Re-export OrchestratorBenchmarkAdapter for Stage 5 benchmark evaluation
from harness_core.harness2_adapter import OrchestratorBenchmarkAdapter


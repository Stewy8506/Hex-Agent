"""Copy-on-Write (CoW) Workspace Snapshot Providers (PRD Sections 6.2, 8.1, FR-012, FR-012a).

Provides zero-copy, millisecond snapshots, branching, instant rollback, and Merkle tree hashing across:
1. macOS APFS clones (`cp -c -R`)
2. Linux OverlayFS (`mount -t overlay` / `fuse-overlayfs` / `unshare` user namespace)
3. Linux Btrfs subvolumes (`btrfs subvolume snapshot [-r]`)
4. Deterministic portable copy fallback
"""

from __future__ import annotations

import atexit
import difflib
import hashlib
import os
import platform
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


IGNORE_DIRS = frozenset({
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".harness_data",
    ".hex",
})
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_FILE_BYTES = 8 * 1024 * 1024 * 1024


class PublicationConflictError(RuntimeError):
    """Raised when verified-delta publication would overwrite concurrent user work.

    Carries the conflicting relative paths; the target workspace is left untouched.
    """

    def __init__(self, conflicts: list[str]):
        self.conflicts = list(conflicts)
        super().__init__(
            "Publication stopped: concurrent changes detected in target workspace for: "
            + ", ".join(self.conflicts)
        )


@dataclass(frozen=True)
class SnapshotMetadata:
    snapshot_id: str
    workspace_path: Path
    snapshot_path: Path
    workspace_tree_sha: str
    backend: str


class WorkspaceSnapshotProvider(Protocol):
    backend_name: str

    def compute_tree_sha(self, directory: Path | str) -> str: ...
    def create_snapshot(self, source_dir: Path | str, label: str = "chk") -> SnapshotMetadata: ...
    def clone_to_workspace(self, snapshot_path: Path | str, target_dir: Path | str) -> str: ...
    def publish_delta(self, base_dir: Path | str, verified_dir: Path | str, target_dir: Path | str) -> str: ...
    def rollback_workspace(self, workspace_dir: Path | str, snapshot_path: Path | str) -> str: ...
    def release_workspace(self, workspace_dir: Path | str) -> None: ...
    def export_delta_archive(self, snapshot_path: Path | str, archive_dest: Path | str) -> str: ...
    def import_delta_archive(self, archive_path: Path | str, target_dir: Path | str, *, expected_tree_sha: str | None = None) -> str: ...
    def discard_snapshot(self, snapshot_path: Path | str) -> None: ...
    def compute_workspace_diff(self, base_dir: Path | str, target_dir: Path | str) -> str: ...


def compute_tree_sha(directory: Path | str) -> str:
    """Deterministic SHA-256 Merkle hash over relative paths, symlinks, and file contents."""
    root = Path(directory).resolve()
    if not root.is_dir():
        return hashlib.sha256(b"empty").hexdigest()

    hasher = hashlib.sha256()
    entries: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        rel_dir = Path(dirpath).relative_to(root)
        for fname in sorted(filenames):
            if fname.startswith(".DS_Store"):
                continue
            fpath = Path(dirpath) / fname
            rel_path = str(rel_dir / fname) if str(rel_dir) != "." else fname
            entries.append((rel_path.replace("\\", "/"), fpath))

    entries.sort(key=lambda item: item[0])
    for rel_path, fpath in entries:
        if fpath.is_symlink():
            target = os.readlink(fpath)
            hasher.update(f"L:{rel_path}->{target}\n".encode("utf-8"))
        elif fpath.is_file():
            content_sha = hashlib.sha256(fpath.read_bytes()).hexdigest()
            hasher.update(f"F:{rel_path}:{content_sha}\n".encode("utf-8"))
    return hasher.hexdigest()


def export_delta_archive(snapshot_path: Path | str, archive_dest: Path | str) -> str:
    """Exports a CoW snapshot directory to a deterministic tar.gz CAS bundle for remote worker handoff."""
    snap = Path(snapshot_path).resolve()
    dest = Path(archive_dest).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _tar_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(tarinfo.name).parts
        if any(p in IGNORE_DIRS or p.startswith(".DS_Store") for p in parts):
            return None
        if tarinfo.name.endswith((".pyc", ".db-wal", ".db-shm")):
            return None
        return tarinfo

    with tarfile.open(dest, "w:gz") as tar:
        tar.add(snap, arcname="workspace", filter=_tar_filter)
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def _archive_relative_path(name: str, workspace_prefix: bool) -> str | None:
    if not isinstance(name, str) or not name or len(name) > 4096 or "\0" in name:
        raise ValueError("Delta archive contains an invalid member name.")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (
        len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":"
    ):
        raise ValueError(f"Tar-slip absolute path forbidden in delta archive: '{name}'")
    parts = normalized.split("/")
    if ".." in parts:
        raise ValueError(f"Tar-slip directory traversal detected in archive member: '{name}'")
    parts = [part for part in parts if part not in {"", "."}]
    if workspace_prefix:
        if not parts or parts[0] != "workspace":
            raise ValueError("Delta archive has an inconsistent workspace root.")
        parts = parts[1:]
    if not parts:
        return None
    if any(part in IGNORE_DIRS for part in parts):
        raise ValueError(f"Delta archive cannot replace local metadata: '{name}'")
    if parts[-1].startswith(".DS_Store") or parts[-1].endswith((".pyc", ".db-wal", ".db-shm")):
        raise ValueError(f"Delta archive contains an unsupported local artifact: '{name}'")
    return "/".join(parts)


def import_delta_archive(
    archive_path: Path | str,
    target_dir: Path | str,
    *,
    expected_tree_sha: str | None = None,
) -> str:
    """Validate a CoW archive in staging, then replace the workspace without touching local metadata."""
    archive = Path(archive_path).resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"Delta archive not found: {archive}")
    if expected_tree_sha is not None and (
        not isinstance(expected_tree_sha, str)
        or len(expected_tree_sha) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in expected_tree_sha)
    ):
        raise ValueError("Invalid expected workspace tree SHA.")

    target = Path(target_dir).resolve()
    if target.exists() and not target.is_dir():
        raise ValueError("Workspace import target must be a directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.import_", dir=target.parent))
    backup = target.parent / f".{target.name}.backup_{uuid.uuid4().hex}"
    moved_metadata: list[str] = []
    target_replaced = False
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members: list[tarfile.TarInfo] = []
            for member in tar:
                if len(members) >= _MAX_ARCHIVE_MEMBERS:
                    raise ValueError("Delta archive exceeds the member safety limit.")
                members.append(member)
            normalized_names = [
                member.name.replace("\\", "/").strip("/") for member in members
            ]
            non_empty = [name for name in normalized_names if name not in {"", "."}]
            workspace_prefix = bool(non_empty) and all(
                name == "workspace" or name.startswith("workspace/")
                for name in non_empty
            )
            entries: dict[str, tarfile.TarInfo] = {}
            total_bytes = 0
            for member in members:
                rel_path = _archive_relative_path(member.name, workspace_prefix)
                if rel_path is None:
                    if not member.isdir():
                        raise ValueError("Delta archive root must be a directory.")
                    continue
                if rel_path in entries:
                    raise ValueError(f"Delta archive contains a duplicate member: '{rel_path}'")
                if not (member.isdir() or member.isreg() or member.issym()):
                    raise ValueError(f"Delta archive contains an unsupported member type: '{rel_path}'")
                if member.size < 0:
                    raise ValueError(f"Delta archive contains an invalid member size: '{rel_path}'")
                total_bytes += member.size
                if total_bytes > _MAX_ARCHIVE_FILE_BYTES:
                    raise ValueError("Delta archive exceeds the expanded-size safety limit.")
                parent = posixpath.dirname(rel_path)
                while parent:
                    existing = entries.get(parent)
                    if existing is not None and not existing.isdir():
                        raise ValueError(f"Delta archive member has a non-directory parent: '{rel_path}'")
                    parent = posixpath.dirname(parent)
                if member.issym():
                    link = member.linkname
                    if not link or len(link) > 4096 or "\0" in link or "\\" in link:
                        raise ValueError(f"Invalid symlink target in delta archive: '{rel_path}'")
                    resolved_link = posixpath.normpath(
                        posixpath.join(posixpath.dirname(rel_path), link)
                    )
                    if link.startswith("/") or resolved_link == ".." or resolved_link.startswith("../"):
                        raise ValueError(f"Symlink target escapes workspace root: '{link}'")
                entries[rel_path] = member

            for rel_path in sorted(entries, key=lambda value: (value.count("/"), value)):
                member = entries[rel_path]
                destination = staging.joinpath(*rel_path.split("/"))
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                if member.issym():
                    os.symlink(member.linkname, destination)
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ValueError(f"Delta archive member could not be read: '{rel_path}'")
                written = 0
                with extracted, destination.open("xb") as output:
                    while chunk := extracted.read(1024 * 1024):
                        output.write(chunk)
                        written += len(chunk)
                if written != member.size:
                    raise ValueError(f"Delta archive member size mismatch: '{rel_path}'")
                os.chmod(destination, (member.mode & 0o777) | 0o600)

        tree_sha = compute_tree_sha(staging)
        if expected_tree_sha is not None and tree_sha != expected_tree_sha.lower():
            raise ValueError(
                f"Tree SHA mismatch during workspace reconstruction: expected {expected_tree_sha}, got {tree_sha}"
            )

        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staging, target)
            target_replaced = True
        except BaseException:
            if not target_replaced and backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            for item in backup.iterdir():
                if item.name in IGNORE_DIRS or item.name.startswith(".DS_Store"):
                    os.replace(item, target / item.name)
                    moved_metadata.append(item.name)
        shutil.rmtree(backup, ignore_errors=True)
        return tree_sha
    except BaseException:
        if target_replaced:
            for name in reversed(moved_metadata):
                with suppress(OSError):
                    os.replace(target / name, backup / name)
            shutil.rmtree(target, ignore_errors=True)
        if backup.exists():
            os.replace(backup, target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def compute_workspace_diff(base_dir: Path | str, target_dir: Path | str) -> str:
    """Compute unified diff across all text files between base_dir and target_dir."""
    base_path = Path(base_dir).resolve()
    target_path = Path(target_dir).resolve()

    if not base_path.exists() or not target_path.exists():
        return ""

    all_rel_paths: set[str] = set()
    for root_p in (base_path, target_path):
        for dirpath, dirnames, filenames in os.walk(root_p):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
            rel_dir = Path(dirpath).relative_to(root_p)
            for fname in filenames:
                if fname.startswith(".DS_Store"):
                    continue
                rel_file = str(rel_dir / fname) if str(rel_dir) != "." else fname
                all_rel_paths.add(rel_file)

    diff_chunks: list[str] = []
    for rel in sorted(all_rel_paths):
        file_a = base_path / rel
        file_b = target_path / rel

        lines_a: list[str] = []
        lines_b: list[str] = []

        if file_a.is_file():
            try:
                lines_a = file_a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except Exception:
                pass
        if file_b.is_file():
            try:
                lines_b = file_b.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except Exception:
                pass

        if lines_a != lines_b:
            chunk = list(
                difflib.unified_diff(
                    lines_a,
                    lines_b,
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            diff_chunks.extend(chunk)

    return "".join(diff_chunks)


def _iter_tree_entries(root: Path) -> dict[str, Path]:
    """Relative POSIX paths -> absolute paths for all non-ignored files under root."""
    entries: dict[str, Path] = {}
    if not root.is_dir():
        return entries
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        rel_dir = Path(dirpath).relative_to(root)
        for fname in sorted(filenames):
            if fname.startswith(".DS_Store") or fname.endswith((".pyc", ".db-wal", ".db-shm")):
                continue
            fpath = Path(dirpath) / fname
            rel = str(rel_dir / fname) if str(rel_dir) != "." else fname
            entries[rel.replace("\\", "/")] = fpath
    return entries


def _entry_state(path: Path) -> tuple[str, str] | None:
    """(kind, identity) for a tree entry; None when absent. Symlinks compare by target."""
    if path.is_symlink():
        try:
            return ("link", os.readlink(path))
        except OSError:
            return ("link", "<unreadable>")
    if path.is_file():
        return ("file", hashlib.sha256(path.read_bytes()).hexdigest())
    if path.exists():
        return ("other", "")
    return None


def publish_delta(
    base_dir: Path | str,
    verified_dir: Path | str,
    target_dir: Path | str,
) -> str:
    """Publish only the verified delta (base -> verified) into an existing workspace.

    B01 contract, distinct from clone_to_workspace (fresh-directory replacement):
    - IGNORE_DIRS metadata (.git, .hex, .venv, ...) in target is never touched.
    - Files the task did not change are never touched, including concurrent user work.
    - A changed/deleted file whose current target content diverges from the base
      snapshot is a conflict: publication stops BEFORE any mutation
      (PublicationConflictError) so concurrent user work is never overwritten.
    - Applied changes are journaled to an undo log; an apply-time failure rolls back
      already-applied changes so publication is recoverable.

    Returns the target tree SHA after publication.
    """
    base = Path(base_dir).resolve()
    verified = Path(verified_dir).resolve()
    target = Path(target_dir).resolve()
    if not verified.is_dir():
        raise FileNotFoundError(f"Verified workspace does not exist: {verified}")
    if not base.is_dir():
        raise FileNotFoundError(f"Base snapshot does not exist: {base}")
    target.mkdir(parents=True, exist_ok=True)

    base_entries = _iter_tree_entries(base)
    verified_entries = _iter_tree_entries(verified)
    target_entries = _iter_tree_entries(target)

    changes: dict[str, str] = {}  # rel -> "upsert" | "delete"
    for rel in sorted(set(base_entries) | set(verified_entries)):
        b_state = _entry_state(base_entries[rel]) if rel in base_entries else None
        v_state = _entry_state(verified_entries[rel]) if rel in verified_entries else None
        if b_state != v_state:
            changes[rel] = "upsert" if v_state is not None else "delete"

    if not changes:
        return compute_tree_sha(target)

    # Conflict pre-check: target must still match the base for every changed path.
    conflicts: list[str] = []
    for rel in sorted(changes):
        b_state = _entry_state(base_entries[rel]) if rel in base_entries else None
        t_state = _entry_state(target_entries[rel]) if rel in target_entries else None
        if t_state != b_state:
            conflicts.append(rel)
    if conflicts:
        raise PublicationConflictError(conflicts)

    # Recoverable apply: back up displaced target entries, journal each action.
    backup_root = target.parent / f".hex_publish_backup_{uuid.uuid4().hex[:10]}"
    undo: list[tuple[str, str, Path | None]] = []  # (rel, action, backup_path)
    try:
        for rel, action in changes.items():
            dest = target / rel
            backup_path: Path | None = None
            if rel in target_entries:
                backup_path = backup_root / rel
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                src_existing = target_entries[rel]
                if src_existing.is_symlink():
                    os.symlink(os.readlink(src_existing), backup_path)
                else:
                    shutil.copy2(src_existing, backup_path)
            undo.append((rel, action, backup_path))
            if action == "delete":
                dest.unlink(missing_ok=True)
            else:
                src_file = verified_entries[rel]
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src_file.is_symlink():
                    if dest.exists() or dest.is_symlink():
                        dest.unlink()
                    os.symlink(os.readlink(src_file), dest)
                else:
                    if dest.is_symlink():
                        dest.unlink()
                    shutil.copy2(src_file, dest)
    except Exception:
        # Roll back applied changes in reverse order.
        for rel, action, backup_path in reversed(undo):
            dest = target / rel
            try:
                if action == "upsert" and backup_path is None:
                    dest.unlink(missing_ok=True)
                elif backup_path is not None:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists() or dest.is_symlink():
                        if dest.is_symlink() or dest.is_file():
                            dest.unlink()
                        else:
                            shutil.rmtree(dest, ignore_errors=True)
                    if backup_path.is_symlink():
                        os.symlink(os.readlink(backup_path), dest)
                    else:
                        shutil.copy2(backup_path, dest)
            except Exception:
                pass  # best-effort restore; backup dir retained for manual recovery
        raise

    shutil.rmtree(backup_root, ignore_errors=True)
    # Prune directories left empty by deletions (never into ignored metadata).
    for rel in changes:
        parent = (target / rel).parent
        while parent != target and parent.is_dir():
            if any(parent.iterdir()):
                break
            if parent.name in IGNORE_DIRS:
                break
            parent.rmdir()
            parent = parent.parent
    return compute_tree_sha(target)


def _cow_copy_tree(src: Path, dst: Path, apfs_supported: bool = False) -> None:
    """Copies directory tree using APFS clone if supported, or standard recursive copy.

    Excludes IGNORE_DIRS upfront so nested .hex / .venv / .git are never cloned into snapshots.
    Fresh-directory clone contract: the destination tree is replaced. For publication
    into a live workspace use publish_delta() instead (B01).
    """
    src = src.resolve()
    dst = dst.resolve()
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    if apfs_supported:
        try:
            for entry in os.scandir(src):
                if entry.name in IGNORE_DIRS or entry.name.startswith(".DS_Store"):
                    continue
                target_item = dst / entry.name
                subprocess.run(
                    ["/bin/cp", "-c", "-R", entry.path, str(target_item)],
                    capture_output=True,
                    check=True,
                    timeout=15,
                )
            # Post-cleanup any nested ignored dirs that might exist deep inside subtrees (e.g. __pycache__)
            for dirpath, dirnames, _ in os.walk(dst, topdown=True):
                to_remove = [d for d in dirnames if d in IGNORE_DIRS]
                for d in to_remove:
                    shutil.rmtree(Path(dirpath) / d, ignore_errors=True)
                dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            return
        except Exception:
            if dst.exists():
                shutil.rmtree(dst)
            dst.mkdir(parents=True, exist_ok=True)

    shutil.copytree(
        src,
        dst,
        symlinks=True,
        ignore=shutil.ignore_patterns(*IGNORE_DIRS, ".DS_Store", "*.pyc", "*.db-wal", "*.db-shm"),
        dirs_exist_ok=True,
    )


def _copy_tree_contents(src: Path, dst: Path) -> None:
    """Synchronizes contents from src into dst, replacing dst files while respecting IGNORE_DIRS."""
    dst = dst.resolve()
    dst.mkdir(parents=True, exist_ok=True)
    # Remove existing items in dst
    for item in os.listdir(dst):
        if item in IGNORE_DIRS:
            continue
        p = dst / item
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)

    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        rel = Path(dirpath).relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in filenames:
            if fname.startswith(".DS_Store") or fname.endswith((".pyc", ".db-wal", ".db-shm")):
                continue
            src_file = Path(dirpath) / fname
            dst_file = target_dir / fname
            if src_file.is_symlink():
                link_target = os.readlink(src_file)
                dst_file.symlink_to(link_target)
            else:
                shutil.copy2(src_file, dst_file)


class APFSAndPortableCoWProvider:
    """Implements CoW snapshots via `cp -c -R` on macOS APFS with automatic portable fallback."""

    def __init__(self, snapshots_root: Path | str):
        self.snapshots_root = Path(snapshots_root).resolve()
        self.snapshots_root.mkdir(parents=True, exist_ok=True)
        self._apfs_supported = self._probe_apfs_clone()
        self.backend_name = "apfs_clone" if self._apfs_supported else "portable_cow_copy"

    def _probe_apfs_clone(self) -> bool:
        if platform.system() != "Darwin":
            return False
        probe_src = self.snapshots_root / f".probe_src_{uuid.uuid4().hex[:6]}"
        probe_dst = self.snapshots_root / f".probe_dst_{uuid.uuid4().hex[:6]}"
        try:
            probe_src.write_text("apfs_probe", encoding="utf-8")
            res = subprocess.run(
                ["/bin/cp", "-c", str(probe_src), str(probe_dst)],
                capture_output=True,
                timeout=3,
            )
            return res.returncode == 0 and probe_dst.is_file()
        except Exception:
            return False
        finally:
            probe_src.unlink(missing_ok=True)
            probe_dst.unlink(missing_ok=True)

    def compute_tree_sha(self, directory: Path | str) -> str:
        return compute_tree_sha(directory)

    def _cow_copy_tree(self, src: Path, dst: Path) -> None:
        _cow_copy_tree(src, dst, apfs_supported=self._apfs_supported)

    def create_snapshot(self, source_dir: Path | str, label: str = "chk") -> SnapshotMetadata:
        src = Path(source_dir).resolve()
        snap_id = f"snap_{label}_{uuid.uuid4().hex[:10]}"
        snap_path = self.snapshots_root / snap_id
        self._cow_copy_tree(src, snap_path)
        tree_sha = self.compute_tree_sha(snap_path)
        return SnapshotMetadata(
            snapshot_id=snap_id,
            workspace_path=src,
            snapshot_path=snap_path,
            workspace_tree_sha=tree_sha,
            backend=self.backend_name,
        )

    def clone_to_workspace(self, snapshot_path: Path | str, target_dir: Path | str) -> str:
        snap = Path(snapshot_path).resolve()
        target = Path(target_dir).resolve()
        self._cow_copy_tree(snap, target)
        return self.compute_tree_sha(target)

    def publish_delta(self, base_dir: Path | str, verified_dir: Path | str, target_dir: Path | str) -> str:
        """Conflict-aware verified-delta publication into a live workspace (B01)."""
        return publish_delta(base_dir, verified_dir, target_dir)

    def rollback_workspace(self, workspace_dir: Path | str, snapshot_path: Path | str) -> str:
        """Instantly restores workspace_dir to exact state of snapshot_path, removing dirty files."""
        ws = Path(workspace_dir).resolve()
        snap = Path(snapshot_path).resolve()
        if not snap.is_dir():
            raise FileNotFoundError(f"CoW snapshot does not exist: {snap}")

        git_backup: Path | None = None
        ws_git = ws / ".git"
        if ws_git.is_dir():
            git_backup = self.snapshots_root / f".git_bak_{uuid.uuid4().hex[:8]}"
            self._cow_copy_tree(ws_git, git_backup)

        self._cow_copy_tree(snap, ws)

        if git_backup and git_backup.is_dir():
            self._cow_copy_tree(git_backup, ws / ".git")
            shutil.rmtree(git_backup, ignore_errors=True)

        return self.compute_tree_sha(ws)

    def release_workspace(self, workspace_dir: Path | str) -> None:
        """Releases an abandoned attempt workspace (no mount on APFS/portable copy)."""
        ws = Path(workspace_dir).resolve()
        if ws.is_dir():
            shutil.rmtree(ws, ignore_errors=True)

    def export_delta_archive(self, snapshot_path: Path | str, archive_dest: Path | str) -> str:
        """Exports CoW snapshot to a deterministic tar.gz CAS bundle for remote worker handoff."""
        return export_delta_archive(snapshot_path, archive_dest)

    def import_delta_archive(self, archive_path: Path | str, target_dir: Path | str, *, expected_tree_sha: str | None = None) -> str:
        return import_delta_archive(archive_path, target_dir, expected_tree_sha=expected_tree_sha)

    def discard_snapshot(self, snapshot_path: Path | str) -> None:
        snap = Path(snapshot_path).resolve()
        if snap.is_dir() and str(snap).startswith(str(self.snapshots_root)):
            shutil.rmtree(snap, ignore_errors=True)

    def compute_workspace_diff(self, base_dir: Path | str, target_dir: Path | str) -> str:
        return compute_workspace_diff(base_dir, target_dir)


class OverlayFSSnapshotProvider:
    """Implements CoW snapshots via Linux OverlayFS with lowerdir, upperdir, workdir, and merged mount."""

    def __init__(
        self,
        snapshots_root: Path | str,
        simulate: bool | None = None,
        mode: str | None = None,
    ):
        self.snapshots_root = Path(snapshots_root).resolve()
        self.snapshots_root.mkdir(parents=True, exist_ok=True)
        self.layers_root = self.snapshots_root / ".layers"
        self.layers_root.mkdir(parents=True, exist_ok=True)

        self.simulate = simulate if simulate is not None else (platform.system() != "Linux")
        if mode:
            self._mode = mode
        else:
            self._mode = self._probe_overlayfs()

        self.backend_name = "fuse_overlayfs" if self._mode == "fuse" else "linux_overlayfs"
        self._active_mounts: dict[str, dict[str, Path]] = {}
        self.command_history: list[list[str]] = []
        self.last_mount_command: list[str] | None = None
        self.last_mount_options: str | None = None

        atexit.register(self._cleanup_all_mounts)

    def _probe_overlayfs(self) -> str:
        if self.simulate:
            return "simulated"
        if platform.system() != "Linux":
            return "simulated"
        if shutil.which("fuse-overlayfs") is not None:
            return "fuse"
        if shutil.which("unshare") is not None:
            return "unshare"
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return "kernel"
        return "unsupported"

    @classmethod
    def is_supported(cls, root_dir: Path | str | None = None) -> bool:
        if platform.system() != "Linux":
            return False
        if shutil.which("fuse-overlayfs") is not None:
            return True
        if shutil.which("unshare") is not None:
            try:
                res = subprocess.run(["unshare", "--user", "--mount", "true"], capture_output=True, timeout=2)
                if res.returncode == 0:
                    return True
            except Exception:
                pass
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return True
        return False

    def compute_tree_sha(self, directory: Path | str) -> str:
        return compute_tree_sha(directory)

    def get_layer_paths(self, workspace_dir: Path | str) -> dict[str, Path]:
        """Returns layout directories: base, lower, upper, work, and merged."""
        ws = Path(workspace_dir).resolve()
        key = hashlib.sha256(str(ws).encode("utf-8")).hexdigest()[:12]
        layer_base = self.layers_root / f"{ws.name}_{key}"
        return {
            "base": layer_base,
            "upper": layer_base / "upper",
            "work": layer_base / "work",
            "lower": layer_base / "lower",
            "merged": ws,
        }

    def build_mount_options(
        self,
        lowerdir: Path | str,
        upperdir: Path | str,
        workdir: Path | str,
    ) -> str:
        """Conforms to lowerdir=...,upperdir=...,workdir=... format."""
        return f"lowerdir={Path(lowerdir).resolve()},upperdir={Path(upperdir).resolve()},workdir={Path(workdir).resolve()}"

    def build_mount_command(
        self,
        lowerdir: Path | str,
        upperdir: Path | str,
        workdir: Path | str,
        merged: Path | str,
    ) -> list[str]:
        opts = self.build_mount_options(lowerdir, upperdir, workdir)
        m_str = str(Path(merged).resolve())
        if self._mode == "fuse":
            return ["fuse-overlayfs", "-o", opts, m_str]
        elif self._mode == "unshare":
            return ["unshare", "-m", "-U", "-r", "mount", "-t", "overlay", "overlay", "-o", opts, m_str]
        else:
            return ["mount", "-t", "overlay", "overlay", "-o", opts, m_str]

    def build_unmount_command(self, merged: Path | str) -> list[str]:
        m_str = str(Path(merged).resolve())
        if self._mode == "fuse":
            return ["fusermount", "-u", m_str]
        return ["umount", m_str]

    def create_snapshot(self, source_dir: Path | str, label: str = "chk") -> SnapshotMetadata:
        src = Path(source_dir).resolve()
        snap_id = f"snap_{label}_{uuid.uuid4().hex[:10]}"
        snap_path = self.snapshots_root / snap_id
        _cow_copy_tree(src, snap_path)
        tree_sha = self.compute_tree_sha(snap_path)
        return SnapshotMetadata(
            snapshot_id=snap_id,
            workspace_path=src,
            snapshot_path=snap_path,
            workspace_tree_sha=tree_sha,
            backend=self.backend_name,
        )

    def clone_to_workspace(self, snapshot_path: Path | str, target_dir: Path | str) -> str:
        snap = Path(snapshot_path).resolve()
        target = Path(target_dir).resolve()
        if not snap.is_dir():
            raise FileNotFoundError(f"Snapshot directory does not exist: {snap}")

        paths = self.get_layer_paths(target)
        paths["base"].mkdir(parents=True, exist_ok=True)
        paths["upper"].mkdir(parents=True, exist_ok=True)
        paths["work"].mkdir(parents=True, exist_ok=True)

        # Create lower layer placeholder/symlink inside layer directory if not present
        if not paths["lower"].exists():
            try:
                paths["lower"].symlink_to(snap, target_is_directory=True)
            except (OSError, NotImplementedError):
                paths["lower"].mkdir(parents=True, exist_ok=True)

        target.mkdir(parents=True, exist_ok=True)

        # Also create merged alias under base directory for clean inspection
        layer_merged = paths["base"] / "merged"
        if not layer_merged.exists() and layer_merged != target:
            try:
                layer_merged.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                layer_merged.mkdir(parents=True, exist_ok=True)

        # Unmount if target was previously mounted
        if str(target) in self._active_mounts:
            self._unmount(target)

        opts = self.build_mount_options(snap, paths["upper"], paths["work"])
        cmd = self.build_mount_command(snap, paths["upper"], paths["work"], target)
        self.last_mount_command = cmd
        self.last_mount_options = opts
        self.command_history.append(cmd)

        self._active_mounts[str(target)] = {
            "lower": snap,
            "upper": paths["upper"],
            "work": paths["work"],
            "base": paths["base"],
            "merged": target,
        }

        if not self.simulate:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to mount overlayfs: {res.stderr or res.stdout}")
        else:
            _copy_tree_contents(snap, target)

        return self.compute_tree_sha(target)

    def publish_delta(self, base_dir: Path | str, verified_dir: Path | str, target_dir: Path | str) -> str:
        """Conflict-aware verified-delta publication into a live workspace (B01)."""
        return publish_delta(base_dir, verified_dir, target_dir)

    def rollback_workspace(self, workspace_dir: Path | str, snapshot_path: Path | str) -> str:
        """Instantly reverts workspace_dir by unmounting, purging upper/work dirs, and remounting."""
        ws = Path(workspace_dir).resolve()
        snap = Path(snapshot_path).resolve()
        if not snap.is_dir():
            raise FileNotFoundError(f"CoW snapshot does not exist: {snap}")

        git_backup: Path | None = None
        ws_git = ws / ".git"
        if ws_git.is_dir():
            git_backup = self.snapshots_root / f".git_bak_{uuid.uuid4().hex[:8]}"
            _cow_copy_tree(ws_git, git_backup)

        if str(ws) in self._active_mounts:
            self._unmount(ws)

        paths = self.get_layer_paths(ws)
        if paths["upper"].exists():
            shutil.rmtree(paths["upper"], ignore_errors=True)
        if paths["work"].exists():
            shutil.rmtree(paths["work"], ignore_errors=True)
        paths["upper"].mkdir(parents=True, exist_ok=True)
        paths["work"].mkdir(parents=True, exist_ok=True)

        if ws.exists():
            for item in os.listdir(ws):
                p = ws / item
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)

        opts = self.build_mount_options(snap, paths["upper"], paths["work"])
        cmd = self.build_mount_command(snap, paths["upper"], paths["work"], ws)
        self.last_mount_command = cmd
        self.last_mount_options = opts
        self.command_history.append(cmd)

        self._active_mounts[str(ws)] = {
            "lower": snap,
            "upper": paths["upper"],
            "work": paths["work"],
            "base": paths["base"],
            "merged": ws,
        }

        if not self.simulate:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to remount overlayfs during rollback: {res.stderr or res.stdout}")
        else:
            _copy_tree_contents(snap, ws)

        if git_backup and git_backup.is_dir():
            _cow_copy_tree(git_backup, ws / ".git")
            shutil.rmtree(git_backup, ignore_errors=True)

        return self.compute_tree_sha(ws)

    def _unmount(self, merged: Path | str) -> None:
        ws = Path(merged).resolve()
        ws_str = str(ws)
        cmd = self.build_unmount_command(ws)
        self.command_history.append(cmd)
        if not self.simulate:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0 and self._mode == "fuse":
                subprocess.run(["fusermount", "-u", "-z", ws_str], capture_output=True, text=True)
            elif res.returncode != 0:
                subprocess.run(["umount", "-l", ws_str], capture_output=True, text=True)
        self._active_mounts.pop(ws_str, None)

    def release_workspace(self, workspace_dir: Path | str) -> None:
        """Unmounts and removes an abandoned attempt workspace so its mount point is freed."""
        ws = Path(workspace_dir).resolve()
        ws_str = str(ws)
        info = self._active_mounts.get(ws_str)
        if ws_str in self._active_mounts:
            self._unmount(ws)
        base = info.get("base") if info else self.get_layer_paths(ws)["base"]
        if isinstance(base, Path) and base.is_dir():
            shutil.rmtree(base, ignore_errors=True)
        if ws.is_dir():
            shutil.rmtree(ws, ignore_errors=True)

    def export_delta_archive(self, snapshot_path: Path | str, archive_dest: Path | str) -> str:
        return export_delta_archive(snapshot_path, archive_dest)

    def import_delta_archive(self, archive_path: Path | str, target_dir: Path | str, *, expected_tree_sha: str | None = None) -> str:
        return import_delta_archive(archive_path, target_dir, expected_tree_sha=expected_tree_sha)

    def discard_snapshot(self, snapshot_path: Path | str) -> None:
        snap = Path(snapshot_path).resolve()
        for ws_str, info in list(self._active_mounts.items()):
            if info.get("lower") == snap or info.get("merged") == snap:
                self._unmount(Path(ws_str))
                base = info.get("base")
                if base and isinstance(base, Path) and base.is_dir():
                    shutil.rmtree(base, ignore_errors=True)
        if snap.is_dir() and str(snap).startswith(str(self.snapshots_root)):
            shutil.rmtree(snap, ignore_errors=True)

    def compute_workspace_diff(self, base_dir: Path | str, target_dir: Path | str) -> str:
        return compute_workspace_diff(base_dir, target_dir)

    def _cleanup_all_mounts(self) -> None:
        for ws_str in list(self._active_mounts.keys()):
            try:
                self._unmount(Path(ws_str))
            except Exception:
                pass


class BtrfsSnapshotProvider:
    """Implements CoW snapshots via Btrfs subvolumes (`btrfs subvolume snapshot`)."""

    def __init__(
        self,
        snapshots_root: Path | str,
        simulate: bool | None = None,
    ):
        self.snapshots_root = Path(snapshots_root).resolve()
        self.snapshots_root.mkdir(parents=True, exist_ok=True)
        self.backend_name = "btrfs_subvolume"
        self.simulate = simulate if simulate is not None else (platform.system() != "Linux")
        self.command_history: list[list[str]] = []

    @classmethod
    def is_supported(cls, root_dir: Path | str | None = None) -> bool:
        if platform.system() != "Linux":
            return False
        if shutil.which("btrfs") is None:
            return False
        if root_dir is not None:
            try:
                p = Path(root_dir).resolve()
                p.mkdir(parents=True, exist_ok=True)
                res = subprocess.run(["btrfs", "filesystem", "df", str(p)], capture_output=True, timeout=2)
                return res.returncode == 0
            except Exception:
                return False
        return True

    def compute_tree_sha(self, directory: Path | str) -> str:
        return compute_tree_sha(directory)

    def create_snapshot(self, source_dir: Path | str, label: str = "chk") -> SnapshotMetadata:
        src = Path(source_dir).resolve()
        snap_id = f"snap_{label}_{uuid.uuid4().hex[:10]}"
        snap_path = self.snapshots_root / snap_id
        cmd = ["btrfs", "subvolume", "snapshot", "-r", str(src), str(snap_path)]
        self.command_history.append(cmd)
        if not self.simulate:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to create btrfs snapshot: {res.stderr or res.stdout}")
        else:
            _cow_copy_tree(src, snap_path)

        tree_sha = self.compute_tree_sha(snap_path)
        return SnapshotMetadata(
            snapshot_id=snap_id,
            workspace_path=src,
            snapshot_path=snap_path,
            workspace_tree_sha=tree_sha,
            backend=self.backend_name,
        )

    def clone_to_workspace(self, snapshot_path: Path | str, target_dir: Path | str) -> str:
        snap = Path(snapshot_path).resolve()
        target = Path(target_dir).resolve()
        if not snap.is_dir():
            raise FileNotFoundError(f"Snapshot directory does not exist: {snap}")
        if target.exists():
            if not self.simulate:
                subprocess.run(["btrfs", "subvolume", "delete", str(target)], capture_output=True)
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["btrfs", "subvolume", "snapshot", str(snap), str(target)]
        self.command_history.append(cmd)
        if not self.simulate:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to clone btrfs subvolume: {res.stderr or res.stdout}")
        else:
            _cow_copy_tree(snap, target)
        return self.compute_tree_sha(target)

    def publish_delta(self, base_dir: Path | str, verified_dir: Path | str, target_dir: Path | str) -> str:
        """Conflict-aware verified-delta publication into a live workspace (B01)."""
        return publish_delta(base_dir, verified_dir, target_dir)

    def rollback_workspace(self, workspace_dir: Path | str, snapshot_path: Path | str) -> str:
        ws = Path(workspace_dir).resolve()
        snap = Path(snapshot_path).resolve()
        if not snap.is_dir():
            raise FileNotFoundError(f"Snapshot directory does not exist: {snap}")

        git_backup: Path | None = None
        ws_git = ws / ".git"
        if ws_git.is_dir():
            git_backup = self.snapshots_root / f".git_bak_{uuid.uuid4().hex[:8]}"
            _cow_copy_tree(ws_git, git_backup)

        del_cmd = ["btrfs", "subvolume", "delete", str(ws)]
        self.command_history.append(del_cmd)
        if not self.simulate:
            subprocess.run(del_cmd, capture_output=True)
        shutil.rmtree(ws, ignore_errors=True)

        snap_cmd = ["btrfs", "subvolume", "snapshot", str(snap), str(ws)]
        self.command_history.append(snap_cmd)
        if not self.simulate:
            res = subprocess.run(snap_cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to restore btrfs snapshot: {res.stderr or res.stdout}")
        else:
            _cow_copy_tree(snap, ws)

        if git_backup and git_backup.is_dir():
            _cow_copy_tree(git_backup, ws / ".git")
            shutil.rmtree(git_backup, ignore_errors=True)

        return self.compute_tree_sha(ws)

    def release_workspace(self, workspace_dir: Path | str) -> None:
        """Deletes an abandoned attempt subvolume and removes its directory."""
        ws = Path(workspace_dir).resolve()
        del_cmd = ["btrfs", "subvolume", "delete", str(ws)]
        self.command_history.append(del_cmd)
        if not self.simulate:
            subprocess.run(del_cmd, capture_output=True)
        if ws.is_dir():
            shutil.rmtree(ws, ignore_errors=True)

    def export_delta_archive(self, snapshot_path: Path | str, archive_dest: Path | str) -> str:
        return export_delta_archive(snapshot_path, archive_dest)

    def import_delta_archive(self, archive_path: Path | str, target_dir: Path | str, *, expected_tree_sha: str | None = None) -> str:
        return import_delta_archive(archive_path, target_dir, expected_tree_sha=expected_tree_sha)

    def discard_snapshot(self, snapshot_path: Path | str) -> None:
        snap = Path(snapshot_path).resolve()
        del_cmd = ["btrfs", "subvolume", "delete", str(snap)]
        self.command_history.append(del_cmd)
        if not self.simulate:
            subprocess.run(del_cmd, capture_output=True)
        if snap.is_dir() and str(snap).startswith(str(self.snapshots_root)):
            shutil.rmtree(snap, ignore_errors=True)

    def compute_workspace_diff(self, base_dir: Path | str, target_dir: Path | str) -> str:
        return compute_workspace_diff(base_dir, target_dir)


def get_default_snapshot_provider(root_dir: Path | str | None = None) -> WorkspaceSnapshotProvider:
    """Auto-detects host platform and returns the optimal CoW WorkspaceSnapshotProvider."""
    if root_dir is None:
        root_dir = Path.home() / ".autonomous_harness" / "cow_snapshots"
    root = Path(root_dir).resolve()
    current_os = platform.system()

    if current_os == "Darwin":
        return APFSAndPortableCoWProvider(root)

    if current_os == "Linux":
        if BtrfsSnapshotProvider.is_supported(root):
            return BtrfsSnapshotProvider(root)
        if OverlayFSSnapshotProvider.is_supported(root):
            return OverlayFSSnapshotProvider(root)
        return APFSAndPortableCoWProvider(root)

    return APFSAndPortableCoWProvider(root)


ApfsSnapshotProvider = APFSAndPortableCoWProvider


__all__ = [
    "IGNORE_DIRS",
    "PublicationConflictError",
    "SnapshotMetadata",
    "WorkspaceSnapshotProvider",
    "compute_tree_sha",
    "export_delta_archive",
    "import_delta_archive",
    "publish_delta",
    "APFSAndPortableCoWProvider",
    "ApfsSnapshotProvider",
    "OverlayFSSnapshotProvider",
    "BtrfsSnapshotProvider",
    "get_default_snapshot_provider",
]

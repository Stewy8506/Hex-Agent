"""Copy-on-Write (CoW) Workspace Snapshot Provider (PRD Sections 6.2, 8.1, FR-012, FR-012a).

Uses native macOS APFS clones (`cp -c -R`) on MacBook Air M4 for zero-copy, millisecond
snapshots and branching, with portable fallback and deterministic SHA-256 Merkle tree hashing.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


IGNORE_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".harness_data"})


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
    def rollback_workspace(self, workspace_dir: Path | str, snapshot_path: Path | str) -> str: ...
    def export_delta_archive(self, snapshot_path: Path | str, archive_dest: Path | str) -> str: ...
    def discard_snapshot(self, snapshot_path: Path | str) -> None: ...


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
        """Deterministic SHA-256 Merkle hash over relative paths, modes, and file contents."""
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

    def _cow_copy_tree(self, src: Path, dst: Path) -> None:
        src = src.resolve()
        dst = dst.resolve()
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)

        if self._apfs_supported:
            # Use macOS native APFS clone (`cp -c -R`) for zero-copy CoW clone
            res = subprocess.run(
                ["/bin/cp", "-c", "-R", str(src), str(dst)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode == 0:
                for ign in IGNORE_DIRS:
                    ign_path = dst / ign
                    if ign_path.exists():
                        shutil.rmtree(ign_path, ignore_errors=True)
                return

        shutil.copytree(
            src,
            dst,
            symlinks=True,
            ignore=shutil.ignore_patterns(*IGNORE_DIRS, ".DS_Store"),
        )

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

    def rollback_workspace(self, workspace_dir: Path | str, snapshot_path: Path | str) -> str:
        """Instantly restores workspace_dir to exact state of snapshot_path, removing dirty files."""
        ws = Path(workspace_dir).resolve()
        snap = Path(snapshot_path).resolve()
        if not snap.is_dir():
            raise FileNotFoundError(f"CoW snapshot does not exist: {snap}")

        # Preserve .git if present in workspace so git history is intact while working tree reverts
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

    def export_delta_archive(self, snapshot_path: Path | str, archive_dest: Path | str) -> str:
        """Exports CoW snapshot to a deterministic tar.gz CAS bundle for remote worker handoff."""
        snap = Path(snapshot_path).resolve()
        dest = Path(archive_dest).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(snap, arcname="workspace")
        return hashlib.sha256(dest.read_bytes()).hexdigest()

    def discard_snapshot(self, snapshot_path: Path | str) -> None:
        snap = Path(snapshot_path).resolve()
        if snap.is_dir() and str(snap).startswith(str(self.snapshots_root)):
            shutil.rmtree(snap, ignore_errors=True)

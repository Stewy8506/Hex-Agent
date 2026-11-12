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
import shutil
import subprocess
import tarfile
import uuid
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
    def import_delta_archive(self, archive_path: Path | str, target_dir: Path | str) -> str: ...
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
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(snap, arcname="workspace")
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def import_delta_archive(archive_path: Path | str, target_dir: Path | str) -> str:
    """Safely extracts a CoW delta archive (.tar.gz) into target_dir with tar-slip defense and returns tree SHA.

    Validates that no archive member escapes target_dir (preventing path traversal).
    If members are wrapped in a root 'workspace' directory, contents are unpacked directly into target_dir.
    """
    archive = Path(archive_path).resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"Delta archive not found: {archive}")

    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    # Clean existing non-ignored files in target directory to ensure deterministic state
    for item in target.iterdir():
        if item.name not in IGNORE_DIRS and not item.name.startswith(".DS_Store"):
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()

    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        non_empty = [m for m in members if m.name.strip("/")]
        has_workspace_prefix = bool(
            non_empty
            and all(
                m.name.lstrip("/").replace("\\", "/") == "workspace"
                or m.name.lstrip("/").replace("\\", "/").startswith("workspace/")
                for m in non_empty
            )
        )

        for member in members:
            raw_name = member.name.lstrip("/").replace("\\", "/")
            if has_workspace_prefix:
                if raw_name == "workspace":
                    continue
                rel_part = raw_name[len("workspace/") :].lstrip("/")
            else:
                rel_part = raw_name

            if not rel_part:
                continue

            # Tar-slip traversal validation: disallow '..' path segments
            if ".." in Path(rel_part).parts:
                raise ValueError(f"Tar-slip directory traversal detected in archive member: '{member.name}'")

            dest_path = (target / rel_part).resolve()
            try:
                dest_path.relative_to(target)
            except ValueError:
                raise ValueError(
                    f"Tar-slip vulnerability detected: member '{member.name}' resolves to '{dest_path}' outside target '{target}'"
                )

            if member.isdir():
                dest_path.mkdir(parents=True, exist_ok=True)
            elif member.isreg():
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                extracted_f = tar.extractfile(member)
                if extracted_f is not None:
                    dest_path.write_bytes(extracted_f.read())
            elif member.issym():
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                link_target = member.linkname
                if link_target.startswith("/"):
                    raise ValueError(f"Absolute symlink target forbidden in delta archive: '{link_target}'")
                resolved_link = (dest_path.parent / link_target).resolve()
                try:
                    resolved_link.relative_to(target)
                except ValueError:
                    raise ValueError(f"Symlink target escapes workspace root: '{link_target}'")
                if dest_path.exists() or dest_path.is_symlink():
                    dest_path.unlink()
                os.symlink(link_target, dest_path)

    return compute_tree_sha(target)


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


def _cow_copy_tree(src: Path, dst: Path, apfs_supported: bool = False) -> None:
    """Copies directory tree using APFS clone if supported, or standard recursive copy.

    Excludes IGNORE_DIRS upfront so nested .hex / .venv / .git are never cloned into snapshots.
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
        ignore=shutil.ignore_patterns(*IGNORE_DIRS, ".DS_Store", "*.pyc"),
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
            if fname.startswith(".DS_Store"):
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

    def export_delta_archive(self, snapshot_path: Path | str, archive_dest: Path | str) -> str:
        """Exports CoW snapshot to a deterministic tar.gz CAS bundle for remote worker handoff."""
        snap = Path(snapshot_path).resolve()
        dest = Path(archive_dest).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(snap, arcname="workspace")
        return hashlib.sha256(dest.read_bytes()).hexdigest()

    def import_delta_archive(self, archive_path: Path | str, target_dir: Path | str) -> str:
        return import_delta_archive(archive_path, target_dir)

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
            subprocess.run(cmd, capture_output=True, text=True)
        self._active_mounts.pop(ws_str, None)

    def export_delta_archive(self, snapshot_path: Path | str, archive_dest: Path | str) -> str:
        snap = Path(snapshot_path).resolve()
        dest = Path(archive_dest).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(snap, arcname="workspace")
        return hashlib.sha256(dest.read_bytes()).hexdigest()

    def import_delta_archive(self, archive_path: Path | str, target_dir: Path | str) -> str:
        return import_delta_archive(archive_path, target_dir)

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

    def export_delta_archive(self, snapshot_path: Path | str, archive_dest: Path | str) -> str:
        snap = Path(snapshot_path).resolve()
        dest = Path(archive_dest).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(snap, arcname="workspace")
        return hashlib.sha256(dest.read_bytes()).hexdigest()

    def import_delta_archive(self, archive_path: Path | str, target_dir: Path | str) -> str:
        return import_delta_archive(archive_path, target_dir)

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
    "SnapshotMetadata",
    "WorkspaceSnapshotProvider",
    "compute_tree_sha",
    "export_delta_archive",
    "import_delta_archive",
    "APFSAndPortableCoWProvider",
    "ApfsSnapshotProvider",
    "OverlayFSSnapshotProvider",
    "BtrfsSnapshotProvider",
    "get_default_snapshot_provider",
]

"""Tests for CoW Workspace Snapshot Providers (OverlayFS, Btrfs, APFS, Portable).

Validates:
1. WorkspaceSnapshotProvider contract compliance across all backends.
2. Platform auto-detection factory (Darwin APFS, Linux OverlayFS, Linux Btrfs, Fallback).
3. OverlayFS layer structure (lower, upper, work, merged) and mount/unmount lifecycles.
4. Btrfs subvolume snapshot command dispatch and rollback.
5. HarnessOrchestrator snapshot provider injection.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cow import (
    APFSAndPortableCoWProvider,
    BtrfsSnapshotProvider,
    OverlayFSSnapshotProvider,
    SnapshotMetadata,
    WorkspaceSnapshotProvider,
    compute_tree_sha,
    get_default_snapshot_provider,
)
from harness_core.orchestrator import HarnessOrchestrator


class TestWorkspaceSnapshotProviderContract(unittest.TestCase):
    """Verifies that all snapshot providers strictly satisfy the WorkspaceSnapshotProvider contract."""

    def setUp(self):
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir_obj.name).resolve()

        # Build sample project workspace with files, subdirectories, and .git
        self.source_ws = self.test_root / "source_workspace"
        self.source_ws.mkdir(parents=True)
        (self.source_ws / "main.py").write_text("print('hello world')\n", encoding="utf-8")
        nested = self.source_ws / "nested"
        nested.mkdir()
        (nested / "module.py").write_text("VALUE = 42\n", encoding="utf-8")
        (self.source_ws / "config.json").write_text('{"env": "test"}\n', encoding="utf-8")

        # Add .git to test preservation across rollbacks
        git_dir = self.source_ws / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("[core]\nrepositoryformatversion = 0\n", encoding="utf-8")

        self.expected_tree_sha = compute_tree_sha(self.source_ws)

    def tearDown(self):
        self.temp_dir_obj.cleanup()

    def _execute_provider_contract(self, provider: WorkspaceSnapshotProvider):
        """Standard verification pipeline exercising the complete CoW lifecycle."""
        # 1. Snapshot creation
        snap_meta = provider.create_snapshot(self.source_ws, label="base")
        self.assertIsInstance(snap_meta, SnapshotMetadata)
        self.assertEqual(snap_meta.workspace_tree_sha, self.expected_tree_sha)
        self.assertTrue(snap_meta.snapshot_path.is_dir())
        self.assertEqual(provider.compute_tree_sha(snap_meta.snapshot_path), self.expected_tree_sha)

        # 2. Workspace clone / branch
        target_ws = self.test_root / f"branch_{provider.backend_name}"
        clone_sha = provider.clone_to_workspace(snap_meta.snapshot_path, target_ws)
        self.assertEqual(clone_sha, self.expected_tree_sha)
        self.assertEqual(provider.compute_tree_sha(target_ws), self.expected_tree_sha)

        # 3. Strict CoW mutation isolation
        (target_ws / "main.py").write_text("print('MUTATED')\n", encoding="utf-8")
        (target_ws / "dirty_untracked.txt").write_text("garbage", encoding="utf-8")
        (target_ws / "nested" / "extra.py").write_text("NEW = 99\n", encoding="utf-8")

        self.assertNotEqual(provider.compute_tree_sha(target_ws), self.expected_tree_sha)
        # Snapshot must remain completely immutable
        self.assertEqual(provider.compute_tree_sha(snap_meta.snapshot_path), self.expected_tree_sha)
        self.assertEqual((snap_meta.snapshot_path / "main.py").read_text(encoding="utf-8"), "print('hello world')\n")
        self.assertFalse((snap_meta.snapshot_path / "dirty_untracked.txt").exists())

        # 4. Instant rollback
        reverted_sha = provider.rollback_workspace(target_ws, snap_meta.snapshot_path)
        self.assertEqual(reverted_sha, self.expected_tree_sha)
        self.assertEqual(provider.compute_tree_sha(target_ws), self.expected_tree_sha)
        self.assertFalse((target_ws / "dirty_untracked.txt").exists())
        self.assertFalse((target_ws / "nested" / "extra.py").exists())
        self.assertEqual((target_ws / "main.py").read_text(encoding="utf-8"), "print('hello world')\n")

        # 5. Delta archive export
        archive_dest = self.test_root / f"archive_{provider.backend_name}.tar.gz"
        archive_sha = provider.export_delta_archive(snap_meta.snapshot_path, archive_dest)
        self.assertTrue(archive_dest.is_file())
        self.assertEqual(hashlib.sha256(archive_dest.read_bytes()).hexdigest(), archive_sha)

        # Verify archive extracts correctly
        extract_dir = self.test_root / f"extracted_{provider.backend_name}"
        with tarfile.open(archive_dest, "r:gz") as tar:
            tar.extractall(extract_dir)
        extracted_ws = extract_dir / "workspace"
        self.assertTrue(extracted_ws.is_dir())
        self.assertEqual((extracted_ws / "main.py").read_text(encoding="utf-8"), "print('hello world')\n")

        # 6. Discard snapshot
        provider.discard_snapshot(snap_meta.snapshot_path)
        self.assertFalse(snap_meta.snapshot_path.exists())

    def test_apfs_or_native_provider_contract(self):
        provider = APFSAndPortableCoWProvider(self.test_root / "apfs_snaps")
        self._execute_provider_contract(provider)

    def test_portable_fallback_provider_contract(self):
        with patch.object(APFSAndPortableCoWProvider, "_probe_apfs_clone", return_value=False):
            provider = APFSAndPortableCoWProvider(self.test_root / "portable_snaps")
            self.assertEqual(provider.backend_name, "portable_cow_copy")
            self._execute_provider_contract(provider)

    def test_overlayfs_provider_contract(self):
        provider = OverlayFSSnapshotProvider(self.test_root / "overlay_snaps", simulate=True)
        self.assertEqual(provider.backend_name, "linux_overlayfs")
        self._execute_provider_contract(provider)

    def test_btrfs_provider_contract(self):
        provider = BtrfsSnapshotProvider(self.test_root / "btrfs_snaps", simulate=True)
        self.assertEqual(provider.backend_name, "btrfs_subvolume")
        self._execute_provider_contract(provider)


class TestSnapshotProviderAutoDetection(unittest.TestCase):
    """Verifies get_default_snapshot_provider auto-detects host platforms and file systems."""

    def setUp(self):
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir_obj.name).resolve()

    def tearDown(self):
        self.temp_dir_obj.cleanup()

    def test_darwin_with_apfs_clone(self):
        with patch("platform.system", return_value="Darwin"):
            with patch.object(APFSAndPortableCoWProvider, "_probe_apfs_clone", return_value=True):
                p = get_default_snapshot_provider(self.root)
                self.assertIsInstance(p, APFSAndPortableCoWProvider)
                self.assertEqual(p.backend_name, "apfs_clone")

    def test_darwin_without_apfs_clone(self):
        with patch("platform.system", return_value="Darwin"):
            with patch.object(APFSAndPortableCoWProvider, "_probe_apfs_clone", return_value=False):
                p = get_default_snapshot_provider(self.root)
                self.assertIsInstance(p, APFSAndPortableCoWProvider)
                self.assertEqual(p.backend_name, "portable_cow_copy")

    def test_linux_with_btrfs(self):
        with patch("platform.system", return_value="Linux"):
            with patch.object(BtrfsSnapshotProvider, "is_supported", return_value=True):
                p = get_default_snapshot_provider(self.root)
                self.assertIsInstance(p, BtrfsSnapshotProvider)
                self.assertEqual(p.backend_name, "btrfs_subvolume")

    def test_linux_with_overlayfs(self):
        with patch("platform.system", return_value="Linux"):
            with patch.object(BtrfsSnapshotProvider, "is_supported", return_value=False):
                with patch.object(OverlayFSSnapshotProvider, "is_supported", return_value=True):
                    with patch.object(OverlayFSSnapshotProvider, "_probe_overlayfs", return_value="kernel"):
                        p = get_default_snapshot_provider(self.root)
                        self.assertIsInstance(p, OverlayFSSnapshotProvider)
                        self.assertEqual(p.backend_name, "linux_overlayfs")

    def test_linux_with_fuse_overlayfs(self):
        with patch("platform.system", return_value="Linux"):
            with patch.object(BtrfsSnapshotProvider, "is_supported", return_value=False):
                with patch.object(OverlayFSSnapshotProvider, "is_supported", return_value=True):
                    with patch.object(OverlayFSSnapshotProvider, "_probe_overlayfs", return_value="fuse"):
                        p = get_default_snapshot_provider(self.root)
                        self.assertIsInstance(p, OverlayFSSnapshotProvider)
                        self.assertEqual(p.backend_name, "fuse_overlayfs")

    def test_linux_fallback_portable(self):
        with patch("platform.system", return_value="Linux"):
            with patch.object(BtrfsSnapshotProvider, "is_supported", return_value=False):
                with patch.object(OverlayFSSnapshotProvider, "is_supported", return_value=False):
                    p = get_default_snapshot_provider(self.root)
                    self.assertIsInstance(p, APFSAndPortableCoWProvider)
                    self.assertEqual(p.backend_name, "portable_cow_copy")

    def test_fallback_other_platform(self):
        with patch("platform.system", return_value="Windows"):
            p = get_default_snapshot_provider(self.root)
            self.assertIsInstance(p, APFSAndPortableCoWProvider)
            self.assertEqual(p.backend_name, "portable_cow_copy")


class TestOverlayFSLayerManagement(unittest.TestCase):
    """Verifies OverlayFS directory layout, mount option strings, and unmount handling."""

    def setUp(self):
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir_obj.name).resolve()
        self.snaps_root = self.test_root / "snaps"
        self.source_ws = self.test_root / "source"
        self.source_ws.mkdir(parents=True)
        (self.source_ws / "sample.txt").write_text("sample content\n", encoding="utf-8")
        self.provider = OverlayFSSnapshotProvider(self.snaps_root, simulate=True)

    def tearDown(self):
        self.temp_dir_obj.cleanup()

    def test_directory_layout_creates_lower_upper_work_and_merged(self):
        snap = self.provider.create_snapshot(self.source_ws, label="layout_test")
        clone_ws = self.test_root / "workspaces" / "ws_dev"

        self.provider.clone_to_workspace(snap.snapshot_path, clone_ws)

        paths = self.provider.get_layer_paths(clone_ws)
        self.assertTrue(paths["lower"].is_dir())
        self.assertTrue(paths["upper"].is_dir())
        self.assertTrue(paths["work"].is_dir())
        self.assertTrue(paths["merged"].is_dir())
        self.assertEqual(paths["merged"], clone_ws)

    def test_mount_options_conforms_to_spec(self):
        lower = self.test_root / "custom_lower"
        upper = self.test_root / "custom_upper"
        work = self.test_root / "custom_work"
        lower.mkdir()
        upper.mkdir()
        work.mkdir()

        opts = self.provider.build_mount_options(lower, upper, work)
        expected = f"lowerdir={lower},upperdir={upper},workdir={work}"
        self.assertEqual(opts, expected)

    def test_mount_command_variants(self):
        lower = self.test_root / "lower"
        upper = self.test_root / "upper"
        work = self.test_root / "work"
        merged = self.test_root / "merged"
        lower.mkdir()
        upper.mkdir()
        work.mkdir()
        merged.mkdir()

        # Kernel mode
        kernel_provider = OverlayFSSnapshotProvider(self.snaps_root, simulate=True, mode="kernel")
        k_cmd = kernel_provider.build_mount_command(lower, upper, work, merged)
        self.assertEqual(k_cmd[0], "mount")
        self.assertIn("-t", k_cmd)
        self.assertIn("overlay", k_cmd)
        self.assertIn(f"lowerdir={lower},upperdir={upper},workdir={work}", k_cmd)
        self.assertEqual(k_cmd[-1], str(merged))

        # FUSE mode
        fuse_provider = OverlayFSSnapshotProvider(self.snaps_root, simulate=True, mode="fuse")
        f_cmd = fuse_provider.build_mount_command(lower, upper, work, merged)
        self.assertEqual(f_cmd[0], "fuse-overlayfs")
        self.assertIn("-o", f_cmd)
        self.assertEqual(f_cmd[-1], str(merged))
        f_umount = fuse_provider.build_unmount_command(merged)
        self.assertEqual(f_umount, ["fusermount", "-u", str(merged)])

        # Unshare mode
        unshare_provider = OverlayFSSnapshotProvider(self.snaps_root, simulate=True, mode="unshare")
        u_cmd = unshare_provider.build_mount_command(lower, upper, work, merged)
        self.assertEqual(u_cmd[:4], ["unshare", "-m", "-U", "-r"])

    def test_unmount_handling_on_rollback_and_discard(self):
        snap = self.provider.create_snapshot(self.source_ws, label="rollback_test")
        clone_ws = self.test_root / "workspaces" / "ws_rollback"

        self.provider.clone_to_workspace(snap.snapshot_path, clone_ws)
        self.assertIn(str(clone_ws), self.provider._active_mounts)

        # Rollback triggers unmount then remount
        cmds_before = len(self.provider.command_history)
        self.provider.rollback_workspace(clone_ws, snap.snapshot_path)
        cmds_after = len(self.provider.command_history)

        # Should have appended unmount command then remount command
        self.assertEqual(cmds_after - cmds_before, 2)
        self.assertEqual(self.provider.command_history[-2], ["umount", str(clone_ws)])
        self.assertEqual(self.provider.command_history[-1][0], "mount")

        # Discard snapshot cleans up active mounts
        self.provider.discard_snapshot(snap.snapshot_path)
        self.assertNotIn(str(clone_ws), self.provider._active_mounts)

    def test_live_mount_execution_invoked_when_not_simulated(self):
        snap = self.provider.create_snapshot(self.source_ws, label="live_mock")
        clone_ws = self.test_root / "workspaces" / "ws_live"

        live_provider = OverlayFSSnapshotProvider(self.snaps_root, simulate=False, mode="kernel")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            # Cloned files in target will be verified by compute_tree_sha
            live_provider.clone_to_workspace(snap.snapshot_path, clone_ws)
            mock_run.assert_called_once()
            cmd_called = mock_run.call_args[0][0]
            self.assertEqual(cmd_called[0], "mount")
            self.assertEqual(cmd_called[-1], str(clone_ws))


class TestBtrfsCommandExecution(unittest.TestCase):
    """Verifies BtrfsSnapshotProvider command dispatch for snapshots, cloning, rollback, and deletion."""

    def setUp(self):
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir_obj.name).resolve()
        self.snaps_root = self.test_root / "btrfs_snaps"
        self.source_ws = self.test_root / "source"
        self.source_ws.mkdir(parents=True)
        (self.source_ws / "btrfs_file.txt").write_text("btrfs payload\n", encoding="utf-8")

    def tearDown(self):
        self.temp_dir_obj.cleanup()

    def test_btrfs_commands_invoked_correctly(self):
        btrfs_provider = BtrfsSnapshotProvider(self.snaps_root, simulate=False)

        def fake_btrfs_run(cmd, *args, **kwargs):
            if "snapshot" in cmd:
                dest = Path(cmd[-1])
                dest.mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_btrfs_run) as mock_run:
            # 1. Read-only snapshot
            snap = btrfs_provider.create_snapshot(self.source_ws, label="btrfs_ro")
            mock_run.assert_called_with(
                ["btrfs", "subvolume", "snapshot", "-r", str(self.source_ws), str(snap.snapshot_path)],
                capture_output=True,
                text=True,
            )

            # 2. Writable clone
            clone_ws = self.test_root / "btrfs_clone"
            btrfs_provider.clone_to_workspace(snap.snapshot_path, clone_ws)
            mock_run.assert_called_with(
                ["btrfs", "subvolume", "snapshot", str(snap.snapshot_path), str(clone_ws)],
                capture_output=True,
                text=True,
            )

            # 3. Rollback
            btrfs_provider.rollback_workspace(clone_ws, snap.snapshot_path)
            # Must delete subvolume first, then snapshot fresh
            self.assertEqual(
                btrfs_provider.command_history[-2],
                ["btrfs", "subvolume", "delete", str(clone_ws)],
            )
            self.assertEqual(
                btrfs_provider.command_history[-1],
                ["btrfs", "subvolume", "snapshot", str(snap.snapshot_path), str(clone_ws)],
            )

            # 4. Discard
            btrfs_provider.discard_snapshot(snap.snapshot_path)
            self.assertEqual(
                btrfs_provider.command_history[-1],
                ["btrfs", "subvolume", "delete", str(snap.snapshot_path)],
            )


class TestOrchestratorCowInjection(unittest.TestCase):
    """Verifies HarnessOrchestrator integrates custom or auto-detected WorkspaceSnapshotProvider."""

    def setUp(self):
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir_obj.name).resolve()

    def tearDown(self):
        self.temp_dir_obj.cleanup()

    def test_orchestrator_default_auto_detect(self):
        orc = HarnessOrchestrator(data_dir=self.test_root / "orch_default")
        self.assertIsNotNone(orc.cow)
        self.assertIsInstance(orc.cow.backend_name, str)

    def test_orchestrator_custom_provider_injection(self):
        custom_provider = OverlayFSSnapshotProvider(self.test_root / "orch_custom_snaps", simulate=True)
        orc = HarnessOrchestrator(
            data_dir=self.test_root / "orch_custom",
            cow=custom_provider,
        )
        self.assertEqual(orc.cow, custom_provider)
        self.assertEqual(orc.cow.backend_name, "linux_overlayfs")


if __name__ == "__main__":
    unittest.main()

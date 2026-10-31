# Phase 02 Plan 01: Linux OverlayFS Provider, Btrfs Subvolume Support & Auto-Detect Factory Summary

**Execution Date:** 2026-10-31  
**Phase:** 02 — Linux OverlayFS Provider & Container Sandbox Isolation  
**Plan:** 01 — Linux OverlayFS Provider, Btrfs Subvolume Support & Auto-Detect Factory (`02-01-PLAN.md`)  
**Status:** Complete  

---

## 1. Executive Summary

Plan 02-01 expands the durable Copy-on-Write (CoW) workspace abstraction in `src/harness_core/cow.py` beyond macOS APFS clones to native Linux filesystems. Two production-grade providers—`OverlayFSSnapshotProvider` and `BtrfsSnapshotProvider`—were implemented adhering strictly to the `WorkspaceSnapshotProvider` protocol. A platform auto-detection factory (`get_default_snapshot_provider`) was added to automatically select the optimal snapshot provider for the runtime environment (Darwin APFS -> Linux Btrfs -> Linux OverlayFS -> portable CoW copy). `HarnessOrchestrator` was updated to accept dependency-injected snapshot providers while defaulting to host auto-detection. A comprehensive contract test suite of 19 tests was authored in `tests/test_cow_providers.py`, raising the total repository test suite to 58 tests, all passing with zero regressions.

---

## 2. Key Accomplishments

### Task 1: Linux OverlayFS Provider, Btrfs Subvolume Support & Auto-Detect Factory
- **`OverlayFSSnapshotProvider`**:
  - Implements `WorkspaceSnapshotProvider` with structured layer topology: immutable base trees (`lowerdir`), read-write mutation scratch (`upperdir`), kernel workspace (`workdir`), and target mount point (`merged`).
  - Implements multi-modal mount options conforming to `lowerdir=...,upperdir=...,workdir=...` supporting root kernel mounts, unprivileged user namespace mounts (`unshare -m -U -r`), and FUSE overlay (`fuse-overlayfs`).
  - Rollback unmounts the workspace, purges dirty `upperdir` and `workdir`, cleans target directories, preserves `.git` tracking, and remounts clean base layers.
  - Registers an `atexit` unmount handler (`_cleanup_all_mounts`) to ensure no dangling FUSE or namespace mounts linger after process exit.
- **`BtrfsSnapshotProvider`**:
  - Implements read-only snapshot capture via `btrfs subvolume snapshot -r`, writable clone branching via `btrfs subvolume snapshot`, and subvolume deletion/replacement rollback.
  - Preserves `.git` repository histories across rollbacks.
- **`get_default_snapshot_provider`**:
  - Dynamic platform detection: returns `apfs_clone` on Darwin when supported, `btrfs_subvolume` on Linux Btrfs mounts, `linux_overlayfs` (or `fuse_overlayfs`) on Linux with OverlayFS support, and `portable_cow_copy` on fallback systems.
- **`HarnessOrchestrator` Wiring**:
  - `HarnessOrchestrator.__init__` now accepts `cow: WorkspaceSnapshotProvider | None = None` and defaults to `get_default_snapshot_provider(self.data_dir / "cow_snapshots")`.
- **Commit**: `9db19ba` (`feat(phase-02): implement OverlayFS and Btrfs snapshot providers with platform factory`).

### Task 2: Comprehensive Contract Test Suite
- Authored `tests/test_cow_providers.py` with 19 dedicated tests covering:
  - `TestWorkspaceSnapshotProviderContract`: Exercises snapshot creation, tree hashing, clone branching, dirty file isolation, instant rollback, `.git` preservation, delta `.tar.gz` export, and discard across APFS, portable copy, OverlayFS, and Btrfs.
  - `TestSnapshotProviderAutoDetection`: Mocks platform and environment capabilities to verify correct provider selection across Darwin (APFS vs no APFS), Linux (Btrfs, OverlayFS, FUSE, fallback), and other platforms (Windows).
  - `TestOverlayFSLayerManagement`: Verifies directory structure creation (`lower`, `upper`, `work`, `merged`), mount option formatting, mount command variations (`mount`, `fuse-overlayfs`, `unshare`), and rollback/discard unmount handling.
  - `TestBtrfsCommandExecution`: Verifies subvolume snapshot command dispatch and deletion sequences.
  - `TestOrchestratorCowInjection`: Verifies orchestrator snapshot provider dependency injection and default auto-detection.
- **Commit**: `38e52c4` (`feat(phase-02): author contract test suite for CoW snapshot providers`).

---

## 3. Empirical Verification Evidence

### Verification 1: Provider Auto-Detection & Backend Check
```bash
.venv/bin/python -c "from harness_core.cow import get_default_snapshot_provider, OverlayFSSnapshotProvider, BtrfsSnapshotProvider, APFSAndPortableCoWProvider; p = get_default_snapshot_provider('/tmp/test_cow_root'); print('Provider detected:', p.backend_name)"
```
**Output:**
```
Provider detected: apfs_clone
```

### Verification 2: Snapshot Providers Contract Test Suite
```bash
.venv/bin/python -m unittest tests/test_cow_providers.py
```
**Output:**
```
...................
----------------------------------------------------------------------
Ran 19 tests in 0.069s

OK
```

### Verification 3: Full Repository Test Suite (58 Tests)
```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
```
**Output Summary:**
```
Ran 58 tests in 0.611s
OK (58 passed, 0 failed, 0 errors)
```

---

## 4. Git Commits Verified

1. `9db19ba`: `feat(phase-02): implement OverlayFS and Btrfs snapshot providers with platform factory`
2. `38e52c4`: `feat(phase-02): author contract test suite for CoW snapshot providers`

---

## 5. Next Steps

Proceed to Plan 02-02 (`.gsd/phases/02-overlayfs-sandbox/02-02-PLAN.md`):
- Container Sandbox Execution Engine (`BubblewrapSandbox`, `DockerPodmanSandbox`, `RestrictedSubprocessSandbox`).
- ToolGateway integration enforcing default-deny container sandbox isolation for bash execution.
- Zero-escape and network/filesystem constraint test suite in `tests/test_sandbox_isolation.py`.


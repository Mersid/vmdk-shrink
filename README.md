# vmdk-shrink

Losslessly shrink (or compact) VMware Workstation ext4 VMDKs from a Windows host,
using a headless Linux helper VM and provable SHA-256 verification of every file.

## What it does

A split-sparse `twoGbMaxExtentSparse` VMDK holding an ext4 filesystem can be
brought down to (data + headroom) with **zero data loss**, verified by hashing
every file before and after. Two modes:

- **`vmdk shrink`** — full pipeline: hash baseline → `e2fsck` → `resize2fs` to a
  smaller size → partition shrink (at the original start LBA) → `zerofree` →
  VMDK compact → descriptor rebuild (orphan extents deleted) → re-hash → diff.
- **`vmdk compact`** — host-space reclaim only: `zerofree` free blocks inside
  the untouched filesystem, then `vmware-vdiskmanager -k`. Capacity is not
  reduced; the guest can grow again on demand. No partition/fs changes at all.

Both modes use the same headless helper: a live Linux ISO patched at build time
(in-memory via `pycdlib`) so that SSH comes up automatically — the only VNC
interaction ever needed is at most one boot-line, and usually nothing at all.

## Requirements

- Windows host with VMware Workstation (`vmrun.exe`, `vmware-vdiskmanager.exe`)
- `uv` (project manages its own `.venv`)
- Windows OpenSSH client (`ssh`, `scp`) on PATH
- ~600 MB disk for the helper ISO

## Quick start

```pwsh
uv sync

# Build (download ISO once, patch it, write helper VMX, boot, wait for SSH):
uv run vmdk helper build "C:\Virtual Machines\testvm2" --attach "C:\Virtual Machines\testvm2\data.vmdk"

# Shrink the data disk to 480 GiB with full verification:
uv run vmdk shrink "C:\Virtual Machines\testvm2" 480

# Or just reclaim host space without touching the guest fs:
uv run vmdk compact "C:\Virtual Machines\testvm2"

# Inspect a split-sparse VMDK's allocation without booting anything:
uv run vmdk inspect "C:\Virtual Machines\testvm2\data.vmdk"
```

## Safety model

- The data disk is only ever written by `e2fsck` / `resize2fs` / `zerofree`
  running inside the helper, and only after an optional explicit confirm.
- The host-side steps (`-k` compact, descriptor rewrite) only ever drop
  *unreferenced or all-zero* regions; the descriptor rewrite keeps a `.bak-desc`.
- Verification aborts the report (exit code 2) if any file differs.

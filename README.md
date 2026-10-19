# vmdk-shrink

Losslessly shrink (or compact) VMware Workstation ext4 VMDKs from a Windows
host, using a Docker helper container and provable SHA-256 verification of
every file.

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

## How it works

The Linux toolchain runs in a **privileged Docker container**. Two transports
perform the filesystem work (`--mode` on `shrink`):

- **`--mode sidecar`** (recommended; validated end-to-end on an 8 GiB test disk): the data VMDK is
  *attached* to a disposable Debian cloud-image VM (downloaded once, cached,
  SHA-512 verified; cloud-init NoCloud seed provides an ephemeral SSH key).
  resize2fs/sgdisk/zerofree run in that VM over SSH on VMware's native block
  stack — full array speed, no 9p, and the **1x** footprint (the disk is
  never copied). See `docs/transport-design.md`.
- **`--mode nbd`** (legacy): qemu-nbd exposes the VMDK in place inside the
  container; slow (~20 MB/s writes) and sensitive to the Windows vp9fs Tflush
  bug, but requires nothing but Docker.

Both modes share the same verification contract: baseline SHA-256 manifest
before, re-hash after, symmetric diff verdict before any host-side metadata
change. The **VHDX round-trip** (qemu-img → `wsl --mount --vhd --bare` →
e2fsprogs → qemu-img back) remains a validated manual alternative: 2x space,
~8 h, zero custom code; see `docs/transport-design.md` for the measured
comparison.

Nothing is installed on the host beyond the image itself and the vendored
`qemu-img.exe`. Cleanup is:

```pwsh
docker rm vmdk-shrink   # (the pipeline removes its own container automatically)
docker rmi vmdk-shrink:latest
```

Hashing fans out over several read-only NBD replicas of the same disk (a single
qemu-nbd connection is latency-bound around 100 MB/s; 4 replicas reach the
bind-mount ceiling).

## Requirements

- Windows host with **Docker Desktop** (WSL2 backend; the `nbd` kernel module
  is loaded automatically via `wsl -d docker-desktop`)
- VMware Workstation's `vmware-vdiskmanager.exe` (for the `-k` compact step)
- `uv` (project manages its own `.venv`)

## Quick start

```pwsh
uv sync

# Shrink the data disk to 700 GiB with full verification (image auto-builds):
uv run vmdk shrink "C:\Virtual Machines\testvm" 700

# Or just reclaim host space without touching the guest fs:
uv run vmdk compact "C:\Virtual Machines\testvm"

# Inspect a split-sparse VMDK's allocation without starting anything:
uv run vmdk inspect "C:\Virtual Machines\testvm\data.vmdk"

# Manage the image explicitly (optional — shrink/compact auto-build it):
uv run vmdk image build
uv run vmdk image ls
uv run vmdk image rm
```

## Safety model

- The filesystem is only modified by `e2fsck` / `resize2fs` / `zerofree` inside
  the container, and only after the baseline hash completes.
- The host-side steps (`-k` compact, descriptor rewrite) only ever drop
  *unreferenced or all-zero* regions; the descriptor rewrite keeps a `.bak-desc`.
- A coverage guard fails the run if the parallel hash ever misses a file.
- The run exits 2 (and says so loudly) if any file's hash differs after.

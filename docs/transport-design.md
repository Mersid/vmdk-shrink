# Transport design: why the VMDK is converted to VHDX before filesystem surgery

> Historical experiment record, not the current implementation or safety contract.
> The current tool does not automatically convert to VHDX or vendor host QEMU.
> See `architecture.md` and the root README for supported paths and requirements.
> Claims below about journal repair do not establish crash-safe data recovery.

This document records an architecture decision that cost several days of
measurement and debugging to arrive at. Read this before proposing to
"just shim the VMDK" — every alternative below was tried or measured.

## The problem

An ext4 resize (`resize2fs`) needs **block-level storage**: it rewrites the
filesystem allocator's own structures (block groups, extent trees, the resize
inode). It cannot run through a filesystem abstraction, only against a real
block device or a raw image at offset 0.

The data lives in a VMware **split-sparse VMDK**: a descriptor plus extent
files, each a sparse grain table over 64 KiB grains, on an NTFS volume. The
VMDK format is not a raw image. Something must stand between the Linux block
layer and those Windows files — always. The only choice is *when* the
translation happens:

- **continuously, per request, over a bridge** (a transport), or
- **once, offline, by conversion** (a format change).

## The transport matrix (all measured on the real workload)

| # | Transport | Result |
|---|-----------|--------|
| 1 | Linux VM with the VMDK attached by VMware (v1/v2 "helper VM") | Works at full speed — VMware itself is a perfect VMDK shim. Abandoned: the vmrun/VNC/SSH sidecar lifecycle was flaky. |
| 2 | `qemu-nbd` inside the container over the VMDK files (v3; files via Docker's 9p file sharing) | Random writes cap at ~20 MB/s (9p `msize=65536`: one round-trip per 64 KiB message through the Plan9 server → Defender → NTFS) and the **vp9fs.dll Tflush race** ([microsoft/WSL#41702](https://github.com/microsoft/WSL/issues/41702), confirmed on `vp9fs.dll 10.0.26100.9457`) permanently wedges mounts under sustained metadata load. Reads are fine (~230 MB/s with replicas) — the asymmetry is per-message write round-trips. |
| 3 | Block-over-TCP: `qemu-nbd.exe` on Windows, kernel `nbd-client` in the container | Hard **~15 MB/s ceiling**: Docker Desktop's NAT adds ~4 ms RTT per request, and the write-reply path Nagle-stalls 19–38 ms behind delayed ACKs (Linux `tcp_delack_min` unavailable in the docker-desktop kernel). Mirrored networking fixes the RTT half but not Nagle. |
| 4 | Custom Windows NBD server (hand-rolled VMDK grain read/write) | Correct down to byte-perfect mkfs/e2fsck, but the grain bookkeeping kept revealing corruption bugs (units mix-ups between sector/byte offsets, zero-fill applied to in-place partial writes, short raw writes silently truncating). Abandoned on principle: hand-rolled fs-adjacent code pointed at real data is the risk this project exists to avoid. Preserved in `.sandbox/nbd_server.py` as forensics. |
| 5 | **Convert to VHDX, use the kernel's native block stack** (v4, current) | `qemu-img convert -O vhdx` once, then `wsl --mount --vhd --bare` hands the WSL2 kernel a real SCSI device via Hyper-V's VHD stack. Measured on the real 714 GiB disk: fsck **9.4 s**, zerofree **2 m 35 s**, resize2fs 720 GiB **60 min**. Verified zero data loss over 556,143 files. |

## Why "see it directly" cannot avoid translation

- A **FUSE shim** (`fuse2fs`) can mount an ext4 *image file*, but resize2fs
  does not operate through FUSE — the shrink rewrites allocator structures at
  block level.
- A **loop device** needs a *raw* image file: either convert (same as option 5,
  to raw instead of VHDX) or loop-mount a file on `/mnt/e` — block I/O over 9p,
  strictly worse than option 2.
- A **VMDK-aware NBD server** is option 3 or 4: shipping blocks over a bridge
  that is either slow (NAT/Nagle/9p) or custom (risk).

Every "the container sees the disk directly" design therefore collapses into
*ship blocks over a slow/unreliable bridge, or hand-roll format code*. The
conversion moves the translation **out of the hot path entirely**: done once,
offline, sequentially at native NTFS speed, by battle-tested code, handing the
fs work to the kernel's native block stack — the same architecture as option 1
(a Linux environment with native block access), with Microsoft as the shim
provider instead of a self-managed VM.

## What is NOT custom

Every layer that touches disk bytes is someone else's code:

- `qemu-img.exe` — VMDK/VHDX conversion (vendored from msys2
  `mingw-w64-x86_64-qemu` + `qemu-image-util`; see `.sandbox/fetch_qemu_deps.py`)
- Hyper-V VHD stack — block transport (`wsl --mount --vhd --bare`; one UAC)
- e2fsprogs / gdisk / zerofree — the fs surgery
- `vmware-vdiskmanager.exe` — final compaction and format acceptance
- the vmdk-shrink package — orchestration, descriptor rebuild, manifest
  hashing/diff only

## Failure modes of the chosen design

- **Hard WSL kill mid-surgery**: ext4 is journaled; e2fsck repairs on the next
  attach (proven in practice — a wedged-nbd shutdown interrupted a run; repair
  cost only journal orphans). The VHDX is a copy; the source VMDK is read-only
  input for the whole pipeline.
- **Interrupted conversion**: delete the partial output and redo; the source is
  never modified.
- **Space**: VHDX + converted VMDK coexist temporarily (~1.5 TB free required).
- **Backup GPT beyond the truncated end** (found by smoke test, 2026-10-17):
  after converting back and truncating the descriptor, the backup GPT still
  points at the old (VHDX-era) end LBA — beyond the new disk end. The guest
  kernel then refuses the whole GPT: the data disk shows *no partitions*, the
  fstab `UUID=` mount times out, and the guest drops to emergency mode. Fix:
  `sgdisk -e` on the new descriptor (metadata-only) before first boot; then
  `sgdisk -v` must report "No problems found". The in-place pipeline is
  immune — it runs `relocate_gpt_backup` at verification time.
- **Not automated**: booting the VM — the final smoke test stays human.

## Hard-won implementation notes (for anyone touching this)

- Raw `FileIO` reads/writes may complete **short** — every read/write must loop
  to completion. A single short write silently truncated the last bytes of
  directory blocks.
- Sector/byte offsets must never be mixed in one expression; a units mix-up in
  misaligned reads seeks ~512× past the data.
- Fresh-grain zero-fill must apply **only** to freshly allocated grains —
  zero-filling the head of an in-place partial write erases prior blocks
  (this shredded a test filesystem).
- Filesystem semantics require **ordered** I/O; a thread-pool reordered a
  whole-device DISCARD behind metadata writes and shredded the fs.
- Windows `SO_REUSEADDR` permits **duplicate binds** that silently shadow a
  running server; use `SO_EXCLUSIVEADDRUSE` and verify connects by checking
  `/sys/block/*/size`.
- vmdk descriptors from `qemu-img` use true 2 GiB extents — derive extent size
  from the descriptor, never hardcode.
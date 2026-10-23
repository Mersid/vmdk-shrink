# Hash transport benchmark (2026-10-21)

> Historical measurements and former defaults. Current defaults deliberately avoid
> mixing runtimes: hashing follows `shrink --mode`; standalone hash uses container.
> There is no shipped TCP-NBD backend or required vendored host QEMU. See
> `architecture.md`; timings below are not verification evidence for the new code.

Goal: pick the fastest way to produce per-file SHA-256 manifests for verification
without shipping data off-box (no cloud VMs) and without bespoke filesystem code.
All candidates read the same fixture (`trimfix`, 2 GiB ext4, 100 × 2 MiB files)
except the WSL2 anchor, which is the historic real-run number.

## Results

| Transport | Where hashing runs | Fixture (90 files / 190 MiB) | Notes |
|---|---|---|---|
| TCP-NBD (`qemu-nbd.exe` → `nbd-client`) | container over kernel NBD | **13.6 s** | host qemu reads VMDK natively (NTFS, no 9p); container mounts ext4 RO |
| 9p container (`qemu-nbd` in container) | container over 9p bind | 30.9 s | default legacy path; every read crosses vp9fs (~10 ms/64 KiB) |
| Sidecar VM (in-guest `sha256sum`) | Debian VM on VMware | ~2 min incl. 40 s boot | in-guest native stack; no 9p, no container; best when the guest is not IO-starved |
| WSL2 bare VHDX mount (historic) | container over `wsl --mount` | n/a | real run: 556k files at 2,750–3,000 files/min, 64–80 MB/s, 8 workers |
| microVM (Firecracker/cloud-hypervisor) | — | **not viable** | no `/dev/kvm` in WSL2 docker-desktop or Ubuntu; TCG emulation would be slower than the sidecar VM |
| Cygwin/MinGW direct | — | **not viable** | Windows cannot mount ext4; e2fsprogs in Cygwin has no kernel mount path |

## Verdict

- **`--hash-via sidecar` (default)** for production: the in-guest hash uses the
  same native stack the guest itself uses, needs no kernel-module workarounds,
  and its throughput tracks the host disk directly. On the production disk it
  hit the IO ceiling only when an unrelated backup (Backblaze `bztransmit`,
  94 MB/s read) was saturating the same HDD — that is a host-load artifact,
  not a transport cost.
- **`--hash-via container`** (legacy 9p) kept behind the flag for comparison;
  ~2.3× slower than TCP-NBD on the fixture and ~5× slower than native on the
  real run.
- **TCP-NBD is the fastest measured path** (2.3× vs 9p on the fixture) and
  needs no VM at all: `qemu-nbd.exe` (MinGW build, already vendored) serves the
  VMDK over loopback TCP; the container's kernel NBD client attaches. It reads
  through the native NTFS stack, so it is subject to the same host IO as the
  sidecar but skips the 9p tax entirely. Worth promoting to a third
  `--hash-via nbd` option if sidecar boot overhead ever matters.

## Sidecar VM networking (fixed 2026-10-21)

Guest discovery used to fail against a healthy guest ("never in ARP cache").
Root causes found and fixed in `sidecar/discover.py`:

1. ARP sweep joined 254 `ping` commands into one `cmd /c` line — over the 8191
   char limit, so the sweep silently sent nothing. Now chunked (40/line).
2. Windows only caches ARP entries for hosts it has talked to; a quiet cloud
   guest can starve every sweep. Fix: read
   `C:\ProgramData\VMware\vmnetdhcp.leases` for the guest MAC instead — the
   lease file is authoritative and updates on every DHCP handshake.
3. The lease file is rewritten out of chronological order and keeps expired
   leases. Fix: drop expired blocks, then take the newest `starts` timestamp
   (naive "last match" returned a dead IP from an earlier boot).

Also: `vmrun start` can block on a modal serial-log "replace or append" dialog
when the GUI is open; generated VMX files should keep `serial.log.mode =
"append"` so automation never hits it.

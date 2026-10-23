# vmdk-shrink

Inspect, compact, and shrink **offline** VMware split-sparse disks from Windows.
Shrink supports a clean, single-partition GPT disk with an ext-family filesystem.
It compares SHA-256 regular-file manifests before and after mutation.

**Make a full, restorable backup first. These operations modify disks in place.**
Matching file hashes do not establish complete metadata preservation, bootability,
or crash safety. A descriptor backup is not a disk backup.

## Commands

```powershell
uv sync
uv run vmdk --help
uv run vmdk doctor shrink
uv run vmdk inspect "C:\VMs\example\data.vmdk" --json

# The plan is shown before confirmation. Ambiguous directories require --vmdk.
uv run vmdk shrink "C:\VMs\example" 700 --vmdk "C:\VMs\example\data.vmdk"
uv run vmdk compact "C:\VMs\example" --vmdk "C:\VMs\example\data.vmdk"

# No filesystem work or Docker: VMware's compactor only.
uv run vmdk compact "C:\VMs\example" --compact-only

# Explicit native VMware transport (extra prerequisites and first-boot downloads).
uv run vmdk shrink "C:\VMs\example" 700 --mode sidecar
uv run vmdk doctor shrink --mode sidecar

uv run vmdk hash "C:\VMs\example" --via container
uv run vmdk --no-color status "C:\VMs\example"
```

Use `--yes`/`-y` for non-interactive mutation only after verifying your backup.
Exit codes: `0` success, `1` operational failure/refusal, `2` content differences
(or CLI argument errors). Errors go to stderr. `inspect` and `doctor` offer `--json`.
`NO_COLOR` and `--no-color` disable color; redirected output does not redraw a TUI.

## Runtime requirements

Python **3.11+** runs the package (`typer` and `rich`). `uv` is the documented
environment manager, not an intrinsic runtime dependency. Run `doctor` for the
command and options you intend to use; it checks executables and Docker readiness,
not guest networking, NBD kernel availability, or crash recovery.

| Operation | Host requirements |
|---|---|
| `inspect`, manifest comparison | Python only |
| `compact --compact-only` | VMware `vmware-vdiskmanager.exe` |
| Normal `compact`, default `shrink` | Docker Desktop/Linux containers, WSL2/NBD, VMware disk utility |
| `hash --via container` | Docker Desktop/Linux containers, WSL2/NBD |
| Sidecar filesystem work or hashing | Above toolchain plus VMware Workstation/`vmrun`, OpenSSH, Windows ACL/network utilities |

**Docker and VMware are not both running by default.** The container provides
Linux filesystem tools; the VMware *disk utility* performs final format-aware
compaction without booting a VM. The optional sidecar boots VMware because its
native storage stack avoids Docker Desktop's slow/problematic Windows file-share
write path. Sidecar still uses Docker for cloud-image preparation and shrink's
discovery/post-geometry checks. `--mode sidecar` is not a Docker-free option.

The default filesystem transport is `nbd`, and hashing now follows it
(`container`). Sidecar mode defaults to sidecar hashing. Mixed transports require
an explicit `--hash-via`. Retaining the optional path avoids throwing away the
native-I/O alternative; removing Docker from it needs a separately tested native
image-preparation and discovery adapter. See [the decision record](docs/architecture.md).

The container installs `e2fsprogs`, `gdisk`, `zerofree`, `qemu-utils`, `lvm2`,
`parted` (for `partprobe`), `fdisk` (for `sfdisk`), `util-linux`, and `coreutils`.
GNU `find`, `xargs`, `sort`, Bash and base64 are also required. There is no required
host `qemu-img.exe`, TCP NBD client, or seed-ISO generator. The `toolchain-v2` tag
prevents silently reusing an old image missing new prerequisites.

Sidecar setup downloads a SHA-512-checked Debian cloud image over HTTPS. The guest
needs repository access during cloud-init provisioning, VMware networking, and
host-to-guest SSH. Its ephemeral keys and image/state files live in `.sidecar`.

## Safety and recovery

1. Refuse locks, snapshot/parent-chain and unsupported image layouts. Disk
   selection uses descriptor entries, not filename guesses; ambiguous choices fail.
2. Discover read-only and require a clean filesystem. A dirty disk must be repaired
   separately from a backup before collecting the baseline; pre-baseline repair
   would hide changes from verification. Validate GPT metadata before resizing.
3. Collect a complete, filename-safe manifest. Save an atomic, per-disk checkpoint
   containing descriptor identity, extent stats, target, transports and manifest digest.
4. Record mutation before repair/resize. Use durable writes. Preserve partition
   start, type, GUID, name and attributes; no automatic retry after interruption.
5. Compare file contents **before host grain/descriptor edits**. If different,
   stop without final cleanup. Then trim wholly free grains, preserve descriptor
   metadata while reducing its extent list, repair final GPT geometry, and compact.
6. Re-hash the final disk. Only matching hashes permit orphan-extent deletion;
   failed runs preserve evidence and unreferenced extents for manual recovery.

These stages are **not a transaction**. Resize, GPT changes, grain trimming and
compaction can already have overwritten data when a failure is detected. Retaining
orphans does not make rollback possible. Keep the VM powered off throughout; the
tool's operation lock does not prevent another application from starting VMware.

`--resume` only reuses an unchanged, completed baseline **before mutation began**.
It refuses interrupted surgery, legacy unbound manifests, changed disk stats,
different targets/transports and modified manifests. Evidence lives under
`.vmdk-shrink\<disk-key>\<operation-id>`. Preserve it before manual recovery.
Never remove a stale operation/image/sidecar lock until all workers are confirmed off.

Manifests cover regular-file **contents**, not ownership, ACLs, extended attributes,
symlinks, hard-link relationships, empty directories, special files, or bootability.
Normal `compact` may repair filesystem metadata; it preserves *size*, not every
filesystem byte. `compact --compact-only` avoids filesystem checks/reclamation.

Container sessions are removed automatically after verified detachment. Use
`vmdk image rm` to remove the toolchain image. `vmdk sidecar teardown <vm-dir>`
refuses a running helper before deleting its artifacts. Checkpoints are not
automatically deleted. Implementation status and integration limitations are
tracked in [the checklist](docs/implementation-checklist.md).

Validation: `uv run pytest`, `uv run ruff check src tests`, and `uv run mypy src`.
An explicit disposable-disk integration check is available as
`uv run python tests\smoke_disposable.py`; it creates only its own fresh project
directory, verifies normal compaction/NBD shrink and unusual filenames, removes
the fixture on success, and retains it on failure. It does not boot the sidecar.

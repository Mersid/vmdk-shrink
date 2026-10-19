"""``vmdk compact``: reclaim host space WITHOUT touching the guest filesystem.

zerofree zeroes free blocks (inside the unchanged filesystem), then
``vmware-vdiskmanager -k`` deallocates the now-zero grains. Capacity is never
reduced — a running guest can expand the disk again on demand.
"""

from __future__ import annotations

from pathlib import Path

from .console import err, info, ok, step
from .fsops import activate_lvm, check_fs, ext4_devices, fs_facts, zerofree
from .paths import DEFAULT_PATHS, HostPaths
from .session import attach_rw, open_session
from .vdiskmanager import VDiskManager
from .vmdk_parser import grain_stats


def _du_bytes(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def _extent_files(vmdk: Path) -> list[Path]:
    stem = vmdk.with_suffix("").name
    return sorted(vmdk.parent.glob(stem + "-s*.vmdk"))


def compact_disk(
    vmdk: Path,
    *,
    extra_vmdks: tuple[Path, ...] = (),
    zerofree_fs: bool = True,
    paths: HostPaths = DEFAULT_PATHS,
) -> int:
    """Compact host footprint. Returns bytes freed (negative = grew).

    ``extra_vmdks`` are zerofree'd (every ext4 under each disk, including LVM
    logical volumes) and compacted alongside the main disk — pass the boot
    disk to reclaim its space too.
    """
    vmdk = Path(vmdk).resolve()
    target_vmdks = (vmdk, *extra_vmdks)
    step(f"Compact-only mode for [val]{', '.join(v.name for v in target_vmdks)}[/val]")
    step("Filesystem size is NOT touched — capacity stays as-is")

    before_files = [f for t in target_vmdks for f in (t, *_extent_files(t))]
    before = _du_bytes(before_files)
    stats = grain_stats(vmdk)
    info(
        f"main disk: capacity [val]{stats.capacity_bytes / 2**30:.0f} GiB[/val], "
        f"allocated [val]{stats.allocated_bytes / 2**30:.1f} GiB[/val], "
        f"host files [val]{before / 2**30:.1f} GiB[/val] ({len(before_files)} file(s))"
    )

    if zerofree_fs:
        with open_session(vmdk.parent, paths=paths) as session:
            ex = session.executor
            for t in target_vmdks:
                disk = attach_rw(session, t)
                try:
                    session.nbd.rescan(disk)
                    activate_lvm(ex)
                    devices = ext4_devices(ex, under=disk.device)
                    if not devices:
                        info(f"{t.name}: no ext4 filesystem found — skipping zerofree")
                        continue
                    for dev in devices:
                        facts = fs_facts(ex, dev)
                        info(
                            f"{t.name} {dev}: ext4 {facts.size_bytes / 2**30:.0f} GiB, "
                            f"{facts.free_blocks * facts.block_size / 2**30:.1f} GiB free"
                        )
                        # zerofree refuses a dirty fs; replay the journal first
                        step(f"e2fsck -fy on [val]{dev}[/val]")
                        check_fs(ex, dev, fix=True)
                        step(f"zerofree [val]{dev}[/val]")
                        output = zerofree(ex, dev)
                        info(
                            output.strip().splitlines()[-1]
                            if output.strip()
                            else "nothing to zero"
                        )
                except Exception as exc:  # noqa: BLE001 — best effort per disk
                    info(f"zerofree skipped for {t.name}: {exc}")
                finally:
                    session.nbd.disconnect()

    for t in target_vmdks:
        step(f"vdiskmanager -k on [val]{t.name}[/val]")
        VDiskManager(paths).compact(t)

    after = _du_bytes(before_files)
    freed = before - after
    if freed >= 0:
        ok(f"freed [ok]{freed / 2**30:.2f} GiB[/ok] ({before / 2**30:.1f} -> {after / 2**30:.1f})")
    else:
        err(f"host files grew by {abs(freed) / 2**30:.2f} GiB (unexpected)")
    return freed

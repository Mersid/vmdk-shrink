"""``vmdk compact``: reclaim host space WITHOUT touching the guest filesystem.

zerofree zeroes free blocks that still hold stale data (it skips blocks that
are already zero, so host files never balloon), then ``vmware-vdiskmanager -k``
deallocates the now-zero grains.
Capacity is never reduced — a running guest can expand the disk again on demand.
"""

from __future__ import annotations

from pathlib import Path

from .console import err, info, ok, step
from .fsops import activate_lvm, check_fs, ext4_devices, fs_facts, nbd_written, zerofree
from .nbd import NbdDisk
from .paths import DEFAULT_PATHS, HostPaths
from .progress import PhaseBar
from .reclaim import FreeMap, collect_free_map, describe, trim_with_map
from .session import attach_rw, open_session
from .sidecar.orchestrator import RealVmrun, SidecarError
from .vdiskmanager import VDiskManager
from .vmdk_parser import VmdkFormatError, extent_list, grain_stats


def _du_bytes(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def _extent_files(vmdk: Path) -> list[Path]:
    """Extent files referenced by the descriptor.

    Never glob the descriptor stem: extents can be renamed (e.g. data-new-
    sNNN.vmdk after a convert), and a missed extent leaves its allocated
    blocks unaccounted in the footprint math.
    """
    try:
        return [vmdk.parent / name for _, name in extent_list(vmdk)]
    except VmdkFormatError:
        return []  # monolithic/flat: the descriptor file is the whole disk


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
    vm_dir = vmdk.parent
    vmrun = RealVmrun(paths)
    for vmx in sorted(vm_dir.glob("*.vmx")):
        try:
            live = vmrun.is_running(vmx)
        except SidecarError:
            live = False  # vmrun unusable: vdiskmanager's own lock check decides
        if live:
            err(
                f"{vmx.name} is running — vmware-vdiskmanager cannot compact "
                "the disks of a live VM; power it off first"
            )
            return 1
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
        bar = PhaseBar()
        trim_maps: list[tuple[Path, FreeMap]] = []
        with open_session(vmdk.parent, paths=paths) as session:
            ex = session.executor
            for t in target_vmdks:
                disk = attach_rw(session, t)
                try:
                    session.nbd.rescan(disk)
                    activate_lvm(ex)
                    devices = ext4_devices(ex, under=disk.device)
                    if not devices:
                        info(f"{t.name}: no ext4 filesystem found — skipping free-space reclaim")
                        continue
                    for dev in devices:
                        facts = fs_facts(ex, dev)
                        info(
                            f"{t.name} {dev}: ext4 {facts.size_bytes / 2**30:.0f} GiB, "
                            f"{facts.free_blocks * facts.block_size / 2**30:.1f} GiB free"
                        )
                        # the free map is only trustworthy on a clean fs
                        step(f"e2fsck -fy on [val]{dev}[/val]")
                        check_fs(ex, dev, fix=True, bar=bar)
                        if dev == disk.partition(1):
                            # Grain-table trim: the fs free map drives GTE
                            # clearing on the closed files — no zero-writes.
                            # Collected here, applied after the session ends.
                            trim_maps.append((t, collect_free_map(ex, disk)))
                            continue
                        # Stacked volumes (LVM) can't be projected to disk
                        # sectors: fall back to zero-writing free blocks.
                        step(f"zerofree [val]{dev}[/val]")

                        def _sampler(d: NbdDisk = disk) -> int:
                            return nbd_written(ex, d.device)

                        bar.start_bytes("zerofree", facts.free_blocks * facts.block_size, _sampler)
                        try:
                            output = zerofree(ex, dev)
                        finally:
                            bar.finish()
                        info(
                            output.strip().splitlines()[-1] if output.strip() else "nothing to zero"
                        )
                except Exception as exc:  # noqa: BLE001 - best effort per disk
                    info(f"free-space reclaim skipped for {t.name}: {exc}")
                finally:
                    session.nbd.disconnect()
        bar.close()
        for t, free_map in trim_maps:
            try:
                info(describe(trim_with_map(t, free_map)))
            except Exception as exc:  # noqa: BLE001 - never fail a compact at trim
                info(f"grain-table trim skipped for {t.name}: {exc}")

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

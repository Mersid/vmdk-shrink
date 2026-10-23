"""Host-space reclamation without reducing filesystem or virtual capacity.

Filesystem repair may change metadata. Use a full backup before this operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .console import err, info, ok, step
from .fsops import activate_lvm, check_fs, ext4_devices, fs_facts, nbd_written, zerofree
from .nbd import NbdDisk
from .paths import DEFAULT_PATHS, HostPaths
from .presentation import compact_with_progress
from .progress import PhaseBar
from .reclaim import FreeMap, TrimUnavailable, collect_free_map, describe, trim_with_map
from .safety import offline_operation, require_offline
from .session import attach_rw, open_session
from .vdiskmanager import VDiskManager
from .vmdk_parser import extent_list, grain_stats, validate_writable


@dataclass(frozen=True)
class CompactionResult:
    before_bytes: int
    after_bytes: int
    disks: tuple[Path, ...]

    @property
    def bytes_freed(self) -> int:
        return self.before_bytes - self.after_bytes


def _du_bytes(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def _extent_files(vmdk: Path) -> list[Path]:
    """Extent files referenced by the descriptor.

    Never glob the descriptor stem: extents can be renamed (e.g. data-new-
    sNNN.vmdk after a convert), and a missed extent leaves its allocated
    blocks unaccounted in the footprint math.
    """
    return [vmdk.parent / name for _, name in extent_list(vmdk)]


@offline_operation()
def compact_disk(
    vmdk: Path,
    *,
    extra_vmdks: tuple[Path, ...] = (),
    zerofree_fs: bool = True,
    paths: HostPaths = DEFAULT_PATHS,
) -> CompactionResult:
    """Compact host footprint. Failures raise; the result contains byte counts.

    ``extra_vmdks`` are zerofree'd (every ext4 under each disk, including LVM
    logical volumes) and compacted alongside the main disk — pass the boot
    disk to reclaim its space too.
    """
    vmdk = Path(vmdk).resolve()
    target_vmdks = (vmdk, *(p.resolve() for p in extra_vmdks))
    vm_dir = vmdk.parent
    if len(set(target_vmdks)) != len(target_vmdks):
        raise ValueError("each target disk must be listed only once")
    for target in target_vmdks:
        if target.parent != vm_dir:
            raise ValueError("all disks must be directly inside the VM directory")
        validate_writable(target)
    step(f"Compact-only mode for [val]{', '.join(v.name for v in target_vmdks)}[/val]")
    step("Capacity stays unchanged; filesystem checks may repair metadata")

    before_files = [f for t in target_vmdks for f in (t, *_extent_files(t))]
    before = _du_bytes(before_files)
    stats = grain_stats(vmdk)
    info(
        f"main disk: capacity [val]{stats.capacity_bytes / 2**30:.0f} GiB[/val], "
        f"allocated [val]{stats.allocated_bytes / 2**30:.1f} GiB[/val], "
        f"host files [val]{before / 2**30:.1f} GiB[/val] ({len(before_files)} file(s))"
    )

    if zerofree_fs:
        trim_maps: list[tuple[Path, FreeMap]] = []
        with PhaseBar() as bar, open_session(vmdk.parent, paths=paths) as session:
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
                            try:
                                free_map = collect_free_map(ex, disk)
                            except TrimUnavailable:
                                pass  # fall back to zerofree, not an ignored failure
                            else:
                                trim_maps.append((t, free_map))
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
                finally:
                    session.nbd.disconnect()
        for t, free_map in trim_maps:
            require_offline(vm_dir)
            info(describe(trim_with_map(t, free_map)))

    for t in target_vmdks:
        require_offline(vm_dir)
        step(f"vdiskmanager -k on [val]{t.name}[/val]")
        compact_with_progress(VDiskManager(paths), t)

    after = _du_bytes(before_files)
    freed = before - after
    if freed >= 0:
        ok(f"freed [ok]{freed / 2**30:.2f} GiB[/ok] ({before / 2**30:.1f} -> {after / 2**30:.1f})")
    else:
        err(f"host files grew by {abs(freed) / 2**30:.2f} GiB (unexpected)")
    return CompactionResult(before, after, target_vmdks)

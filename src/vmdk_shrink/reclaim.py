"""Free-space reclamation via grain-table trimming.

The filesystem knows which blocks are free; the sparse VMDK format can
express "this grain is gone" in its grain-table metadata. This module joins
the two: read the free map with ``dumpe2fs`` (the same authority ``fstrim``
consults), then clear the grain-table entries of wholly-free grains on the
closed host files. No grain data is ever written, so reclaiming hundreds of
gigabytes of pruned space costs a few thousand 4-byte table writes instead
of the same gigabytes of zero-writes.

``fstrim`` cannot do this through qemu-nbd (measured 2026-10-18): the VMDK
driver silently drops discards and materializes WRITE_ZEROES. The trim is
therefore done host-side, after the container's NBD connection is closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .console import info, step
from .executor import Executor
from .fsops import free_block_ranges, fs_facts
from .graintrim import TrimStats, fs_blocks_to_sectors, trim_free_grains
from .nbd import NbdDisk
from .paths import DEFAULT_PATHS, HostPaths
from .session import attach_ro, open_session


class TrimUnavailable(RuntimeError):
    """The free map cannot be mapped to disk sectors (e.g. LVM volumes)."""


@dataclass(frozen=True)
class FreeMap:
    """A filesystem's free space, in fs blocks, plus its geometry."""

    start_lba: int
    block_size: int
    ranges: list[tuple[int, int]]  # fs blocks, inclusive

    @property
    def free_blocks(self) -> int:
        return sum(last - first + 1 for first, last in self.ranges)


def collect_free_map(ex: Executor, disk: NbdDisk, *, partition: int = 1) -> FreeMap:
    """Read the free-block map of ``disk``'s partition inside a live session.

    Must run on a clean filesystem (``e2fsck -fy`` first) that is not
    otherwise mounted — a dirty or busy fs reports a stale free map. Raises
    :class:`TrimUnavailable` when block numbers cannot be projected back to
    disk sectors (stacked LVM volumes), so the caller can fall back to
    ``zerofree``.
    """
    try:
        out = ex.run(
            f"cat /sys/class/block/{disk.partition(partition).removeprefix('/dev/')}/start",
            timeout=30,
        ).output.strip()
        start = int(out)
    except Exception as exc:  # noqa: BLE001 - any geometry failure means fallback
        raise TrimUnavailable(
            f"cannot map {disk.device} partition {partition}: {exc}"
        ) from exc
    part = disk.partition(partition)
    facts = fs_facts(ex, part)
    ranges = free_block_ranges(ex, part)
    return FreeMap(start_lba=start, block_size=facts.block_size, ranges=ranges)


def trim_with_map(vmdk: Path, free_map: FreeMap) -> TrimStats:
    """Host-side: clear the grain-table entries of wholly-free grains.

    The VMDK's extent files must not be attached to anything (no qemu-nbd,
    no VM) when this runs — qemu caches grain tables in memory and would
    clobber concurrent grain-table writes.
    """
    vmdk = Path(vmdk).resolve()
    if not free_map.ranges:
        info(f"{vmdk.name}: filesystem reports no free blocks — nothing to trim")
        return TrimStats(
            merged_ranges=0,
            grains_cleared=0,
            grains_partial_skipped=0,
            gt_writes=0,
            bytes_freed=0,
        )
    sectors = fs_blocks_to_sectors(
        free_map.ranges, start_lba=free_map.start_lba, block_size=free_map.block_size
    )
    step(
        f"grain-table trim of [val]{vmdk.name}[/val]: "
        f"{free_map.free_blocks * free_map.block_size / 2**30:.1f} GiB free in "
        f"{len(sectors)} extent(s)"
    )
    return trim_free_grains(vmdk, sectors)


def trim_free_space(vmdk: Path, *, paths: HostPaths = DEFAULT_PATHS) -> TrimStats:
    """Convenience: read the free map in a short session, then trim host-side."""
    vmdk = Path(vmdk).resolve()
    step(f"free-space map for [val]{vmdk.name}[/val]")
    with open_session(vmdk.parent, paths=paths) as session:
        ex = session.executor
        disk = attach_ro(session, vmdk, index=0)
        try:
            session.nbd.rescan(disk)
            free_map = collect_free_map(ex, disk)
        finally:
            session.nbd.disconnect(check=False)
    return trim_with_map(vmdk, free_map)


def describe(stats: TrimStats) -> str:
    return (
        f"{stats.grains_cleared} grains cleared ({stats.freed_gib:.2f} GiB), "
        f"{stats.grains_partial_skipped} partial grains kept, "
        f"{stats.gt_writes} table writes"
    )

"""Metadata-only free-space reclamation: clear grain-table entries.

``fstrim`` cannot reach the VMDK grain tables through this stack. Measured
2026-10-18 on a 1 GiB fixture: qemu's VMDK driver silently drops discards
(``blkdiscard`` "succeeds", no grain-table entry changes) and *materializes*
WRITE_ZEROES (a zero-write into a hole allocated the grain). Neither channel
reclaims anything.

This module performs the operation FITRIM would delegate to the driver: every
grain wholly covered by filesystem-free sectors gets its grain-table entry
cleared, so reads return zeros and ``vmware-vdiskmanager -k`` drops the grain
on rewrite. No grain *data* is ever written - only 4-byte table entries -
which is what keeps a 600 GiB reclaim from touching the drive at all.

Safety is fstrim's exact contract: the free map comes from e2fsprogs
(``dumpe2fs``) on an fsck-clean, quiescent filesystem. A block the filesystem
reports free may be unallocated at will - that is what discard *means*.
Grains only partially covered by free space are left alone: they hold live
data that ``-k`` keeps regardless, so this never needs to zero mixed grains
either.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .vmdk_parser import (
    SECTOR,
    ExtentHeader,
    VmdkFormatError,
    _iter_extent_files,
    _parse_extent_header,
)

GTE_ZERO_MARK = 0x80000000  # compressed/known-zero marker; also reads as zero


@dataclass(frozen=True)
class TrimStats:
    """What a trim pass cleared, and what it deliberately left alone."""

    merged_ranges: int
    grains_cleared: int
    grains_partial_skipped: int
    gt_writes: int
    bytes_freed: int

    @property
    def freed_gib(self) -> float:
        return self.bytes_freed / 2**30


@dataclass
class _Acc:
    merged_ranges: int = 0
    grains_cleared: int = 0
    grains_partial_skipped: int = 0
    gt_writes: int = 0
    bytes_freed: int = 0

    def stats(self) -> TrimStats:
        return TrimStats(
            merged_ranges=self.merged_ranges,
            grains_cleared=self.grains_cleared,
            grains_partial_skipped=self.grains_partial_skipped,
            gt_writes=self.gt_writes,
            bytes_freed=self.bytes_freed,
        )


def fs_blocks_to_sectors(
    ranges: list[tuple[int, int]], *, start_lba: int, block_size: int
) -> list[tuple[int, int]]:
    """Convert fs-block ranges (inclusive) to absolute disk sectors (inclusive)."""
    if block_size % SECTOR:
        raise VmdkFormatError(f"block size {block_size} not sector-aligned")
    step = block_size // SECTOR
    out: list[tuple[int, int]] = []
    for first, last in ranges:
        if last < first:
            raise VmdkFormatError(f"reversed free-block range {first}-{last}")
        out.append((start_lba + first * step, start_lba + (last + 1) * step - 1))
    return out


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and coalesce overlapping/adjacent sector ranges.

    Coalescing matters at block-group boundaries: two adjacent dumpe2fs
    ranges can jointly cover a grain that neither covers alone, and only the
    merged view sees it as fully free.
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if end < start:
            raise VmdkFormatError(f"reversed sector range {start}-{end}")
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def trim_free_grains(descriptor: Path, free_sector_ranges: list[tuple[int, int]]) -> TrimStats:
    """Clear the grain-table entries of every grain wholly inside free space.

    ``free_sector_ranges`` are absolute disk sectors (inclusive, unsorted is
    fine). Partially covered grains are skipped untouched; grains already
    unallocated cost nothing. Only grain-table sectors are ever written.
    """
    merged = merge_ranges(free_sector_ranges)
    acc = _Acc(merged_ranges=len(merged))
    disk_base = 0
    for path in _iter_extent_files(Path(descriptor)):
        hdr = _parse_extent_header(path)
        lo, hi = disk_base, disk_base + hdr.capacity_sectors - 1
        local = [
            (max(s, lo) - disk_base, min(e, hi) - disk_base)
            for s, e in merged
            if s <= hi and e >= lo
        ]
        if local:
            _trim_extent(path, hdr, local, acc)
        disk_base += hdr.capacity_sectors
    return acc.stats()


def _trim_extent(
    path: Path, hdr: ExtentHeader, local: list[tuple[int, int]], acc: _Acc
) -> None:
    grain_sectors = hdr.grain_sectors
    per_gt = hdr.gt_entries_per_table
    with path.open("r+b") as f:
        f.seek(hdr.gd_offset * SECTOR)
        gd = list(struct.unpack(f"<{hdr.gd_entries}I", f.read(hdr.gd_entries * 4)))
        gt_cache: dict[int, list[int]] = {}
        dirty: set[int] = set()

        for start, end in local:
            first_g = start // grain_sectors
            last_g = end // grain_sectors
            for g in range(first_g, last_g + 1):
                g0 = g * grain_sectors
                g1 = g0 + grain_sectors - 1
                if not (start <= g0 and g1 <= end):
                    # Partial coverage: live data shares the grain. -k keeps
                    # it anyway (non-zero content), so there is nothing to
                    # reclaim and nothing to write. Never clear it.
                    acc.grains_partial_skipped += 1
                    continue
                gt_index = g // per_gt
                slot = g % per_gt
                gt_sector = gd[gt_index] if gt_index < len(gd) else 0
                if gt_sector == 0:
                    continue  # no grain table: every slot is unallocated
                gt = gt_cache.get(gt_index)
                if gt is None:
                    gt = list(_read_gt(f, gt_sector, per_gt))
                    gt_cache[gt_index] = gt
                if gt[slot] == 0:
                    continue  # already unallocated
                gt[slot] = 0
                dirty.add(gt_index)
                acc.grains_cleared += 1
                acc.bytes_freed += hdr.grain_bytes

        for gt_index in sorted(dirty):
            gt_sector = gd[gt_index]
            f.seek(gt_sector * SECTOR)
            f.write(struct.pack(f"<{per_gt}I", *gt_cache[gt_index]))
            acc.gt_writes += 1
        f.flush()


def _read_gt(f: BinaryIO, gt_sector: int, per_gt: int) -> tuple[int, ...]:
    f.seek(gt_sector * SECTOR)
    return struct.unpack(f"<{per_gt}I", f.read(per_gt * 4))

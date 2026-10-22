"""Grain-table trimming: the metadata-only reclamation path.

These tests pin the safety contract: full-coverage grains get their GTE
cleared, partial-coverage grains are NEVER touched (live data shares them),
and no grain data is ever written.
"""

from __future__ import annotations

import struct
from pathlib import Path

from fixture_vmdk import EXTENT_SECTORS, GT_ENTRIES, build_disk

from vmdk_shrink.graintrim import (
    fs_blocks_to_sectors,
    merge_ranges,
    trim_free_grains,
)

GRAIN = 128  # sectors, matches fixture_vmdk
SECTOR = 512


def _gtes(extent: Path) -> list[int]:
    with extent.open("rb") as f:
        f.seek(1 * SECTOR)  # gd_offset in the fixture
        (gt_sector,) = struct.unpack("<1I", f.read(4))
        f.seek(gt_sector * SECTOR)
        return list(struct.unpack(f"<{GT_ENTRIES}I", f.read(GT_ENTRIES * 4)))


def test_merge_ranges_coalesces_adjacent_group_boundaries() -> None:
    # Two adjacent dumpe2fs ranges must merge so a grain straddling the
    # boundary is seen as fully free.
    assert merge_ranges([(10, 20), (21, 30), (5, 7)]) == [(5, 7), (10, 30)]


def test_fs_blocks_to_sectors_maps_to_absolute_lba() -> None:
    out = fs_blocks_to_sectors([(0, 1)], start_lba=2048, block_size=4096)
    assert out == [(2048, 2048 + 16 - 1)]


def test_full_coverage_clears_gte_and_partial_is_untouched(tmp_path: Path) -> None:
    d = build_disk(tmp_path, stem="t", extents=1, allocated_per_extent=4)
    before = _gtes(tmp_path / "t-s001.vmdk")
    assert all(before[g] for g in range(4))

    # Grain 0 fully free, grain 1 only half free (live data shares it),
    # grain 2 untouched, grain 3 fully free.
    free = [
        (0, GRAIN - 1),
        (GRAIN, GRAIN + GRAIN // 2),
        (3 * GRAIN, 4 * GRAIN - 1),
    ]
    stats = trim_free_grains(d, free)

    after = _gtes(tmp_path / "t-s001.vmdk")
    assert after[0] == 0, "fully free grain must be unallocated"
    assert after[3] == 0
    assert after[1] == before[1], "partial grain must never be touched"
    assert after[2] == before[2], "outside range must never be touched"
    assert stats.grains_cleared == 2
    assert stats.grains_partial_skipped == 1
    assert stats.bytes_freed == 2 * GRAIN * SECTOR
    assert stats.gt_writes == 1


def test_trim_is_idempotent(tmp_path: Path) -> None:
    d = build_disk(tmp_path, stem="t", extents=1, allocated_per_extent=2)
    free = [(0, GRAIN - 1)]
    first = trim_free_grains(d, free)
    second = trim_free_grains(d, free)
    assert first.grains_cleared == 1
    assert second.grains_cleared == 0, "already-unallocated grains cost nothing"
    assert second.gt_writes == 0


def test_grain_data_bytes_untouched_by_trim(tmp_path: Path) -> None:
    # A trim must not write grain data — only grain tables. Pin it.
    d = build_disk(tmp_path, stem="t", extents=1, allocated_per_extent=2)
    e = tmp_path / "t-s001.vmdk"
    before = e.read_bytes()
    trim_free_grains(d, [(0, GRAIN - 1)])
    after = e.read_bytes()
    assert before != after, "grain table must change"
    # Grain data areas live at and after sector 16 in the fixture: byte-identical.
    assert before[16 * SECTOR :] == after[16 * SECTOR :], "grain data must not be modified"


def test_multi_extent_ranges_map_across_extent_boundaries(tmp_path: Path) -> None:
    d = build_disk(tmp_path, stem="m", extents=3, allocated_per_extent=2)
    # Disk-absolute: extent 2's grain 0 = sector 2*EXTENT_SECTORS.
    base2 = 2 * EXTENT_SECTORS
    stats = trim_free_grains(d, [(base2, base2 + GRAIN - 1)])
    assert stats.grains_cleared == 1
    e0 = _gtes(tmp_path / "m-s001.vmdk")
    e1 = _gtes(tmp_path / "m-s002.vmdk")
    e2 = _gtes(tmp_path / "m-s003.vmdk")
    assert e0[0] != 0 and e1[0] != 0, "other extents untouched"
    assert e2[0] == 0, "target extent cleared"

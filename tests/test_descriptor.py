"""Tests for descriptor planning, rebuild, and orphan deletion."""

from __future__ import annotations

from pathlib import Path

from fixture_vmdk import EXTENT_SECTORS as EXT
from fixture_vmdk import build_disk

from vmdk_shrink.vmdk_descriptor import (
    compute_plan,
    delete_orphans,
    rebuild_descriptor,
)
from vmdk_shrink.vmdk_parser import SECTOR, RawSplitSparseDisk, extent_list, grain_stats


def test_extent_list(tmp_path: Path) -> None:
    d = build_disk(tmp_path, stem="disk", extents=3)
    extents = extent_list(d)
    assert len(extents) == 3
    assert all(sectors == EXT for sectors, _ in extents)


def test_grain_stats_allocated(tmp_path: Path) -> None:
    d = build_disk(tmp_path, stem="disk", extents=2, allocated_per_extent=3)
    stats = grain_stats(d)
    assert stats.extent_count == 2
    assert stats.capacity_sectors == 2 * EXT
    # 3 allocated grains of 128 sectors per extent
    assert stats.allocated_bytes == 2 * 3 * 128 * SECTOR


def test_raw_read_across_extents(tmp_path: Path) -> None:
    d = build_disk(tmp_path, stem="disk", extents=2, allocated_per_extent=2)
    disk = RawSplitSparseDisk(d)
    try:
        # first sector of extent 1 (allocated grain 0 -> 0xAB fill)
        assert disk.read(0, 1) == b"\xab" * SECTOR
        # a sector in an unallocated grain of extent 1 (grain 5)
        assert disk.read(5 * 128, 1) == b"\x00" * SECTOR
        # last sector of extent 1 + first of extent 2 in one read
        n = disk.read(EXT - 1, 2)
        assert len(n) == 2 * SECTOR
        assert n[:SECTOR] == b"\x00" * SECTOR
        assert n[SECTOR:] == b"\xab" * SECTOR  # extent 2 grain 0 is allocated
    finally:
        disk.close()


def test_compute_plan_keeps_intersecting_extents(tmp_path: Path) -> None:
    d = build_disk(tmp_path, stem="disk", extents=4)
    # fs ends inside extent 3 -> keep 3 extents, drop 1
    fs_end = 2 * EXT + 1000
    plan = compute_plan(d, fs_end_sector=fs_end)
    assert plan.keep_extents == 3
    assert len(plan.orphan_files) == 1
    assert plan.orphan_files[0].name == "disk-s004.vmdk"


def test_compute_plan_exact_boundary(tmp_path: Path) -> None:
    d = build_disk(tmp_path, stem="disk", extents=2)
    plan = compute_plan(d, fs_end_sector=EXT)  # fs ends exactly at extent 1 end
    assert plan.keep_extents == 1


def test_rebuild_and_delete_orphans(tmp_path: Path) -> None:
    d = build_disk(tmp_path, stem="disk", extents=4)
    plan = compute_plan(d, fs_end_sector=EXT + 5)  # fs pokes 5 sectors into extent 2
    assert plan.keep_extents == 2
    rebuild_descriptor(d, plan)
    backup = d.with_name(d.name + ".bak-desc")
    assert backup.exists()
    text = d.read_text(encoding="windows-1252")
    assert text.count("RW ") == 2
    assert 'SPARSE "disk-s001.vmdk"' in text
    deleted, freed = delete_orphans(plan)
    assert deleted == 2
    assert freed > 0
    assert not (tmp_path / "disk-s003.vmdk").exists()
    # descriptor now parses to the two kept extents
    assert len(extent_list(d)) == 2
    assert grain_stats(d).extent_count == 2


def test_rebuild_backup_not_overwritten(tmp_path: Path) -> None:
    d = build_disk(tmp_path, stem="disk", extents=2)
    backup = d.with_name(d.name + ".bak-desc")
    backup.write_bytes(b"original-sentinel")
    plan = compute_plan(d, fs_end_sector=EXT + 1)
    rebuild_descriptor(d, plan)
    assert backup.read_bytes() == b"original-sentinel"

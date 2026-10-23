"""Fail-closed regression tests for disk-image mutation."""

from __future__ import annotations

import os
import struct
from pathlib import Path
from unittest.mock import patch

import pytest
from fixture_vmdk import EXTENT_SECTORS, build_disk, build_extent

from vmdk_shrink.graintrim import trim_free_grains
from vmdk_shrink.vmdk_descriptor import compute_plan, delete_orphans, rebuild_descriptor
from vmdk_shrink.vmdk_parser import (
    RawSplitSparseDisk,
    VmdkFormatError,
    extent_list,
    grain_stats,
    validate_writable,
)


def test_rebuild_preserves_identity_and_unknown_metadata(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2)
    original = descriptor.read_bytes().replace(b"\n", b"\r\n")
    original += (
        b"CID=12345678\r\nparentCID=ffffffff\r\n"
        b'ddb.uuid.image = "original-identity"\r\n'
        b"# custom metadata: caf\xe9\r\n"
    )
    descriptor.write_bytes(original)
    plan = compute_plan(descriptor, EXTENT_SECTORS)
    rebuild_descriptor(descriptor, plan)
    assert descriptor.read_bytes() == original.replace(
        f'RW {EXTENT_SECTORS} SPARSE "disk-s002.vmdk"\r\n'.encode(), b""
    )


def test_plan_uses_unequal_extent_spans(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=3)
    descriptor.write_bytes(
        descriptor.read_bytes().replace(
            f'RW {EXTENT_SECTORS} SPARSE "disk-s002.vmdk"'.encode(),
            b'RW 128 SPARSE "disk-s002.vmdk"',
        )
    )
    extent = tmp_path / "disk-s002.vmdk"
    with extent.open("r+b") as f:
        f.seek(12)
        f.write(struct.pack("<Q", 128))
    plan = compute_plan(descriptor, EXTENT_SECTORS + 129)
    assert plan.keep_extents == 3
    assert plan.new_capacity_sectors == 2 * EXTENT_SECTORS + 128


@pytest.mark.parametrize("final_newline", [b"\n", b""])
def test_qemu_padded_descriptor_preserves_metadata_and_padding(
    tmp_path: Path, final_newline: bytes
) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2)
    original = (
        b"# Disk DescriptorFile\nversion=1\nCID=12345678\nparentCID=ffffffff\n"
        b'createType="twoGbMaxExtentSparse"\n'
        b'ddb.adapterType = "lsilogic"\n'
        + f'RW {EXTENT_SECTORS} SPARSE "disk-s001.vmdk"\n'.encode()
        + f'RW {EXTENT_SECTORS} SPARSE "disk-s002.vmdk"'.encode()
        + final_newline
    )
    padding = b"\x00" * (512 - len(original) % 512)
    descriptor.write_bytes(original + padding)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    validate_writable(descriptor)
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
    rebuild_descriptor(descriptor, compute_plan(descriptor, EXTENT_SECTORS))
    expected = (
        original.replace(
            f'RW {EXTENT_SECTORS} SPARSE "disk-s002.vmdk"'.encode() + final_newline, b""
        )
        + padding
    )
    assert descriptor.read_bytes() == expected
    assert descriptor.with_name(descriptor.name + ".bak-desc").read_bytes() == original + padding
    validate_writable(descriptor)


@pytest.mark.parametrize("interior", [b"\x00\n", b"\x00# comment\n", b"# comment\x00\n"])
def test_interior_nul_rejected_before_mutation(tmp_path: Path, interior: bytes) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2)
    descriptor.write_bytes(descriptor.read_bytes() + interior + b"\x00" * 32)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(VmdkFormatError):
        trim_free_grains(descriptor, [(0, 127)])
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize(
    "change",
    [
        b"parentCID=12345678\n",
        b'parentFileNameHint="parent.vmdk"\n',
        b'RDONLY 128 SPARSE "extra.vmdk"\n',
        b'RW 128 FLAT "extra.vmdk" 0\n',
    ],
)
def test_unsupported_descriptors_fail_before_mutation(tmp_path: Path, change: bytes) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2)
    descriptor.write_bytes(descriptor.read_bytes() + change)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(VmdkFormatError):
        trim_free_grains(descriptor, [(0, 127)])
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize(
    "name",
    [
        "../outside.vmdk",
        "..\\outside.vmdk",
        "C:\\outside.vmdk",
        "/outside.vmdk",
    ],
)
def test_extent_paths_cannot_escape(tmp_path: Path, name: str) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=1)
    descriptor.write_bytes(descriptor.read_bytes().replace(b"disk-s001.vmdk", name.encode()))
    with pytest.raises(VmdkFormatError):
        extent_list(descriptor)


@pytest.mark.parametrize(
    ("offset", "fmt", "value"),
    [
        (4, "I", 99),
        (8, "I", 0x10000),
        (8, "I", 2),
        (48, "Q", 1),
        (77, "H", 1),
        (20, "Q", 0),
        (44, "I", 0),
        (56, "Q", 0),
        (56, "Q", 999999),
    ],
)
def test_bad_later_extent_prevents_all_writes(
    tmp_path: Path, offset: int, fmt: str, value: int
) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2)
    with (tmp_path / "disk-s002.vmdk").open("r+b") as f:
        f.seek(offset)
        f.write(struct.pack("<" + fmt, value))
    first = tmp_path / "disk-s001.vmdk"
    before = first.read_bytes()
    with pytest.raises(VmdkFormatError):
        trim_free_grains(descriptor, [(0, 127)])
    assert first.read_bytes() == before


def test_shared_orphans_are_not_deleted(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2)
    other = tmp_path / "other.vmdk"
    other.write_bytes(descriptor.read_bytes())
    plan = compute_plan(descriptor, EXTENT_SECTORS)
    rebuild_descriptor(descriptor, plan)
    with pytest.raises(VmdkFormatError):
        delete_orphans(plan)
    assert (tmp_path / "disk-s002.vmdk").exists()


def test_gt_must_not_overlap_directory(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=1)
    extent = tmp_path / "disk-s001.vmdk"
    with extent.open("r+b") as f:
        f.seek(512)
        f.write(struct.pack("<I", 1))
    before = extent.read_bytes()
    with pytest.raises(VmdkFormatError):
        trim_free_grains(descriptor, [(0, 127)])
    assert extent.read_bytes() == before


def test_validate_writable_is_read_only(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    assert validate_writable(descriptor) is None
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_validate_writable_checks_unselected_extent(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2)
    with (tmp_path / "disk-s002.vmdk").open("r+b") as f:
        f.seek(8)
        f.write(struct.pack("<I", 2))
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(VmdkFormatError, match="redundant"):
        validate_writable(descriptor)
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("end", [0, -1, EXTENT_SECTORS + 1])
def test_plan_rejects_out_of_capacity_end(tmp_path: Path, end: int) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=1)
    with pytest.raises(VmdkFormatError):
        compute_plan(descriptor, end)


def test_stale_plan_rejected_before_backup(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2)
    plan = compute_plan(descriptor, EXTENT_SECTORS)
    descriptor.write_bytes(descriptor.read_bytes() + b"# changed\n")
    before = descriptor.read_bytes()
    with pytest.raises(VmdkFormatError):
        rebuild_descriptor(descriptor, plan)
    assert descriptor.read_bytes() == before
    assert not descriptor.with_name(descriptor.name + ".bak-desc").exists()


def test_rebuild_updates_only_existing_geometry_cylinders(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2)
    geometry = (
        b'ddb.geometry.heads = "16"\n'
        b'ddb.geometry.sectors = "63"\n'
        b'ddb.geometry.cylinders = "999" # original\n'
    )
    original = descriptor.read_bytes() + geometry
    descriptor.write_bytes(original)
    rebuild_descriptor(descriptor, compute_plan(descriptor, EXTENT_SECTORS))
    expected = original.replace(
        f'RW {EXTENT_SECTORS} SPARSE "disk-s002.vmdk"\n'.encode(), b""
    ).replace(b'cylinders = "999"', f'cylinders = "{EXTENT_SECTORS // (16 * 63)}"'.encode())
    assert descriptor.read_bytes() == expected


def test_capacity_mismatch_rejected(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=1)
    descriptor.write_bytes(descriptor.read_bytes().replace(str(EXTENT_SECTORS).encode(), b"128"))
    with pytest.raises(VmdkFormatError, match="capacity mismatch"):
        validate_writable(descriptor)


@pytest.mark.parametrize(("location", "value"), [(512, 999999), (9 * 512, 1)])
def test_metadata_and_data_pointers_are_bounded(tmp_path: Path, location: int, value: int) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=1)
    extent = tmp_path / "disk-s001.vmdk"
    with extent.open("r+b") as f:
        f.seek(location)
        f.write(struct.pack("<I", value))
    before = extent.read_bytes()
    with pytest.raises(VmdkFormatError):
        validate_writable(descriptor)
    assert extent.read_bytes() == before


def test_known_zero_marker_is_not_a_high_bit_address(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=1)
    extent = tmp_path / "disk-s001.vmdk"
    with extent.open("r+b") as f:
        f.seek(8)
        f.write(struct.pack("<I", 4))
        f.seek(9 * 512)
        f.write(struct.pack("<I", 1))
    disk = RawSplitSparseDisk(descriptor)
    try:
        assert disk.read(0, 1) == b"\x00" * 512
    finally:
        disk.close()
    assert grain_stats(descriptor).allocated_bytes == 128 * 512
    before = extent.read_bytes()
    assert trim_free_grains(descriptor, [(0, 127)]).bytes_freed == 0
    assert extent.read_bytes() == before


def test_partial_last_grain_is_readable_but_not_fully_trimmed(tmp_path: Path) -> None:
    descriptor = tmp_path / "disk.vmdk"
    build_extent(tmp_path / "extent.vmdk", capacity_sectors=129, allocated_grains=[1])
    descriptor.write_bytes(
        b'version=1\ncreateType="twoGbMaxExtentSparse"\nRW 129 SPARSE "extent.vmdk"\n'
    )
    disk = RawSplitSparseDisk(descriptor)
    try:
        assert disk.read(128, 1) == b"\xab" * 512
    finally:
        disk.close()
    stats = trim_free_grains(descriptor, [(128, 128)])
    assert stats.grains_cleared == 0
    assert stats.grains_partial_skipped == 1


def test_delete_requires_rebuilt_descriptor(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2)
    plan = compute_plan(descriptor, EXTENT_SECTORS)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(VmdkFormatError):
        delete_orphans(plan)
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_linked_extent_refused_before_mutation(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=1)
    extent = tmp_path / "disk-s001.vmdk"
    (tmp_path / "shared.vmdk").hardlink_to(extent)
    before = extent.read_bytes()
    with pytest.raises(VmdkFormatError, match="shared"):
        trim_free_grains(descriptor, [(0, 127)])
    assert extent.read_bytes() == before


def test_unknown_descriptor_blocks_all_orphan_deletions(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=3)
    plan = compute_plan(descriptor, EXTENT_SECTORS)
    rebuild_descriptor(descriptor, plan)
    (tmp_path / "unknown.vmdk").write_bytes(b"unsupported descriptor")
    with pytest.raises(VmdkFormatError, match="ownership"):
        delete_orphans(plan)
    assert all(path.exists() for path in plan.orphan_files)


@pytest.mark.parametrize("version", [1, 3])
def test_supported_sparse_versions(tmp_path: Path, version: int) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=1)
    with (tmp_path / "disk-s001.vmdk").open("r+b") as f:
        f.seek(4)
        f.write(struct.pack("<I", version))
    validate_writable(descriptor)


def _redundant_tables(extent: Path) -> tuple[int, int]:
    with extent.open("rb") as f:
        f.seek(48)
        rgd, gd = struct.unpack("<QQ", f.read(16))
        f.seek(gd * 512)
        primary = struct.unpack("<I", f.read(4))[0]
        f.seek(rgd * 512)
        secondary = struct.unpack("<I", f.read(4))[0]
    return primary * 512, secondary * 512


def _reserved_descriptor_disk(tmp_path: Path) -> Path:
    descriptor = build_disk(tmp_path, stem="disk", extents=2, redundant=True)
    for extent in (tmp_path / "disk-s001.vmdk", tmp_path / "disk-s002.vmdk"):
        build_extent(
            extent,
            allocated_grains=[0, 1],
            redundant=True,
            reserved_descriptor_sectors=20,
        )
        with extent.open("r+b") as f:
            f.seek(4)
            f.write(struct.pack("<I", 1))
            f.seek(73)
            f.write(b"\x0a\x20\x0d\x0a")
    return descriptor


def test_qemu_empty_reserved_descriptor_is_safe_to_trim(tmp_path: Path) -> None:
    descriptor = _reserved_descriptor_disk(tmp_path)
    extent = tmp_path / "disk-s001.vmdk"
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    validate_writable(descriptor)
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
    primary, secondary = _redundant_tables(extent)
    stats = trim_free_grains(descriptor, [(0, 127)])
    expected = bytearray(before[extent])
    for offset in (primary, secondary):
        struct.pack_into("<I", expected, offset, 0)
    assert extent.read_bytes() == expected
    assert stats.grains_cleared == 1
    assert stats.gt_writes == 2
    assert (tmp_path / "disk-s002.vmdk").read_bytes() == before[tmp_path / "disk-s002.vmdk"]
    validate_writable(descriptor)
    rebuild_descriptor(descriptor, compute_plan(descriptor, EXTENT_SECTORS))
    validate_writable(descriptor)


@pytest.mark.parametrize(
    "damage",
    [
        "embedded",
        "last_byte",
        "missing_offset",
        "missing_size",
        "header",
        "outside",
        "overlap",
        "dirty",
        "compressed",
    ],
)
def test_invalid_later_reserved_descriptor_prevents_all_writes(tmp_path: Path, damage: str) -> None:
    descriptor = _reserved_descriptor_disk(tmp_path)
    extent = tmp_path / "disk-s002.vmdk"
    with extent.open("r+b") as f:
        if damage == "embedded":
            offset, content = 512, b"# Disk DescriptorFile\n"
        elif damage == "last_byte":
            offset, content = 21 * 512 - 1, b"x"
        elif damage == "missing_offset":
            offset, content = 28, struct.pack("<Q", 0)
        elif damage == "missing_size":
            offset, content = 36, struct.pack("<Q", 0)
        elif damage == "header":
            offset, content = 28, struct.pack("<QQ", 0, 1)
        elif damage == "outside":
            offset, content = 36, struct.pack("<Q", 999999)
        elif damage == "overlap":
            offset, content = 36, struct.pack("<Q", 21)
        elif damage == "dirty":
            offset, content = 72, b"\x01"
        else:
            offset, content = 77, struct.pack("<H", 1)
        f.seek(offset)
        f.write(content)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(VmdkFormatError):
        trim_free_grains(descriptor, [(0, 127)])
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("version", [1, 3])
def test_redundant_trim_updates_both_copies_and_syncs(tmp_path: Path, version: int) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2, redundant=True)
    extent = tmp_path / "disk-s001.vmdk"
    with extent.open("r+b") as f:
        f.seek(4)
        f.write(struct.pack("<I", version))
    before = extent.read_bytes()
    primary, secondary = _redundant_tables(extent)
    validate_writable(descriptor)
    assert extent.read_bytes() == before
    with patch("vmdk_shrink.graintrim.os.fsync", wraps=os.fsync) as sync:
        stats = trim_free_grains(descriptor, [(0, 127), (128, 191)])
        assert sync.call_count == 1
    expected = bytearray(before)
    for offset in (primary, secondary):
        struct.pack_into("<I", expected, offset, 0)
    assert extent.read_bytes() == expected
    assert stats.grains_cleared == 1
    assert stats.bytes_freed == 128 * 512
    assert stats.gt_writes == 2
    validate_writable(descriptor)
    assert grain_stats(descriptor).allocated_bytes == 3 * 128 * 512
    disk = RawSplitSparseDisk(descriptor)
    try:
        assert disk.read(0, 1) == bytes(512)
        assert disk.read(128, 1) == b"\xab" * 512
    finally:
        disk.close()
    assert trim_free_grains(descriptor, [(0, 127)]).gt_writes == 0


@pytest.mark.parametrize(
    "damage", ["entry", "unused_entry", "missing_table", "overlap", "outside", "directory"]
)
def test_bad_later_redundant_copy_prevents_all_writes(tmp_path: Path, damage: str) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=2, redundant=True)
    extent = tmp_path / "disk-s002.vmdk"
    primary, secondary = _redundant_tables(extent)
    with extent.open("r+b") as f:
        f.seek(48)
        rgd = struct.unpack("<Q", f.read(8))[0]
        if damage == "entry":
            offset, fmt, value = secondary, "I", 0
        elif damage == "unused_entry":
            offset, fmt, value = secondary + 511 * 4, "I", 1
        elif damage == "missing_table":
            offset, fmt, value = rgd * 512, "I", 0
        elif damage == "overlap":
            offset, fmt, value = rgd * 512, "I", primary // 512
        elif damage == "outside":
            offset, fmt, value = rgd * 512, "I", 999999
        else:
            offset, fmt, value = 48, "Q", 999999
        f.seek(offset)
        f.write(struct.pack("<" + fmt, value))
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(VmdkFormatError):
        trim_free_grains(descriptor, [(0, 127)])
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("damage", [False, True])
def test_redundant_later_table_is_validated_and_trimmed(tmp_path: Path, damage: bool) -> None:
    capacity = 512 * 128 + 128
    extent = tmp_path / "extent.vmdk"
    descriptor = tmp_path / "disk.vmdk"
    build_extent(extent, capacity_sectors=capacity, redundant=True)
    descriptor.write_bytes(
        b'version=1\ncreateType="twoGbMaxExtentSparse"\n'
        + f'RW {capacity} SPARSE "extent.vmdk"\n'.encode()
    )
    primary, secondary = _redundant_tables(extent)
    with extent.open("r+b") as f:
        f.seek(48)
        rgd, gd = struct.unpack("<QQ", f.read(16))
        f.seek(64)
        f.write(struct.pack("<Q", 24))
        for offset in (primary, secondary):
            f.seek(offset)
            f.write(struct.pack("<I", 24))
        for directory, table in [(gd, 16), (rgd, 20)]:
            f.seek(directory * 512 + 4)
            f.write(struct.pack("<I", table))
            f.seek(table * 512)
            f.write(struct.pack("<512I", 152 if table == 16 or not damage else 0, *([0] * 511)))
        f.seek(24 * 512)
        f.write(b"\xab" * (256 * 512))
    before = extent.read_bytes()
    if damage:
        with pytest.raises(VmdkFormatError, match="inconsistent redundant"):
            trim_free_grains(descriptor, [(0, 127)])
        assert extent.read_bytes() == before
    else:
        stats = trim_free_grains(descriptor, [(512 * 128, capacity - 1)])
        assert stats.grains_cleared == 1
        assert stats.gt_writes == 2
        expected = bytearray(before)
        for table in (16, 20):
            struct.pack_into("<I", expected, table * 512, 0)
        assert extent.read_bytes() == expected
        validate_writable(descriptor)


def test_redundant_trim_sync_failure_is_reported(tmp_path: Path) -> None:
    descriptor = build_disk(tmp_path, stem="disk", extents=1, redundant=True)
    with (
        patch("vmdk_shrink.graintrim.os.fsync", side_effect=OSError("sync failed")),
        pytest.raises(OSError, match="sync failed"),
    ):
        trim_free_grains(descriptor, [(0, 127)])

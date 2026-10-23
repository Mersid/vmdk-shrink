"""Synthetic split-sparse VMDK fixtures for tests."""

from __future__ import annotations

import struct
from pathlib import Path

SPARSE_MAGIC = 0x564D444B
SECTOR = 512
GRAIN_SECTORS = 128  # 64 KiB grains
GT_ENTRIES = 512
EXTENT_SECTORS = 65_536_000  # standard twoGbMaxExtentSparse extent


def build_extent(
    path: Path,
    *,
    capacity_sectors: int = EXTENT_SECTORS,
    allocated_grains: list[int] | None = None,
    fill_byte: int = 0xAB,
    redundant: bool = False,
    reserved_descriptor_sectors: int = 0,
) -> None:
    """Write a minimal-but-valid sparse extent: header, GD, one GT, some grains."""
    allocated = set(allocated_grains or [])
    grains = (capacity_sectors + GRAIN_SECTORS - 1) // GRAIN_SECTORS
    gd_entries = (grains + GT_ENTRIES - 1) // GT_ENTRIES
    gd_offset_sector = 1 + reserved_descriptor_sectors
    gt_sector = gd_offset_sector + (gd_entries * 4 + SECTOR - 1) // SECTOR
    metadata_end = gt_sector + GT_ENTRIES * 4 // SECTOR
    rgd_sector = metadata_end if redundant else 0
    rgt_sector = rgd_sector + (gd_entries * 4 + SECTOR - 1) // SECTOR
    if redundant:
        metadata_end = rgt_sector + GT_ENTRIES * 4 // SECTOR
    grain_data_start_sector = max(16, metadata_end)

    with path.open("wb") as f:
        hdr = bytearray(512)
        struct.pack_into("<I", hdr, 0, SPARSE_MAGIC)
        struct.pack_into("<I", hdr, 4, 3)  # version
        struct.pack_into("<I", hdr, 8, 3 if redundant else 0)
        struct.pack_into("<Q", hdr, 12, capacity_sectors)
        struct.pack_into("<Q", hdr, 20, GRAIN_SECTORS)
        if reserved_descriptor_sectors:
            struct.pack_into("<QQ", hdr, 28, 1, reserved_descriptor_sectors)
        struct.pack_into("<I", hdr, 44, GT_ENTRIES)
        struct.pack_into("<Q", hdr, 48, rgd_sector)
        struct.pack_into("<Q", hdr, 56, gd_offset_sector)
        struct.pack_into("<Q", hdr, 64, grain_data_start_sector)
        f.write(hdr)

        gd = struct.pack(f"<{gd_entries}I", gt_sector, *([0] * (gd_entries - 1)))
        f.write(b"\x00" * (gd_offset_sector * SECTOR - f.tell()))
        f.write(gd)
        f.write(b"\x00" * (gt_sector * SECTOR - f.tell()))
        gtv = bytearray()
        for g in range(GT_ENTRIES):
            addr = 0
            if g in allocated:
                addr = grain_data_start_sector + g * GRAIN_SECTORS
            gtv += struct.pack("<I", addr)
        f.write(bytes(gtv))
        if redundant:
            f.seek(rgd_sector * SECTOR)
            f.write(struct.pack(f"<{gd_entries}I", rgt_sector, *([0] * (gd_entries - 1))))
            f.seek(rgt_sector * SECTOR)
            f.write(bytes(gtv))
        f.truncate(grain_data_start_sector * SECTOR)

        for g in allocated:
            f.seek((grain_data_start_sector + g * GRAIN_SECTORS) * SECTOR)
            f.write(bytes([fill_byte]) * (GRAIN_SECTORS * SECTOR))


def build_disk(
    vm_dir: Path,
    *,
    stem: str,
    extents: int,
    allocated_per_extent: int = 2,
    redundant: bool = False,
) -> Path:
    """Create ``stem.vmdk`` + ``stem-sNNN.vmdk`` files; returns the descriptor path."""
    lines = []
    for i in range(1, extents + 1):
        name = f"{stem}-s{i:03d}.vmdk"
        build_extent(
            vm_dir / name,
            allocated_grains=list(range(allocated_per_extent)),
            redundant=redundant,
        )
        lines.append(f'RW {EXTENT_SECTORS} SPARSE "{name}"')
    descriptor = vm_dir / f"{stem}.vmdk"
    descriptor.write_bytes(
        (
            '# Disk DescriptorFile\nversion=1\nencoding="windows-1252"\n'
            'createType="twoGbMaxExtentSparse"\n\n# Extent description\n'
            + "\n".join(lines)
            + '\n\nddb.adapterType = "lsilogic"\n'
        ).encode("windows-1252")
    )
    return descriptor

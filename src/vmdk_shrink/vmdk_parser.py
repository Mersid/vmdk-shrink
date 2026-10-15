"""Raw access to split-sparse (twoGbMaxExtentSparse) VMDKs.

Reads arbitrary sectors and walks grain tables to report the true allocated
(= data) footprint, without ever mounting anything. Ported from the proven
scratch scripts (``vmdk_read.py`` / ``grain_walk.py``) with full typing.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

SPARSE_MAGIC = 0x564D444B
SECTOR = 512


class VmdkFormatError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtentHeader:
    """Parsed 512-byte sparse extent header."""

    path: Path
    capacity_sectors: int
    grain_sectors: int
    gt_entries_per_table: int
    gd_offset: int  # sector offset of the grain directory

    @property
    def grain_bytes(self) -> int:
        return self.grain_sectors * SECTOR

    @property
    def grains(self) -> int:
        return self.capacity_sectors * SECTOR // self.grain_bytes

    @property
    def gd_entries(self) -> int:
        """Number of grain-directory entries (one per grain table, ceil)."""
        return (self.grains + self.gt_entries_per_table - 1) // self.gt_entries_per_table


@dataclass(frozen=True)
class ExtentStats:
    header: ExtentHeader
    allocated_grains: int

    @property
    def allocated_bytes(self) -> int:
        return self.allocated_grains * self.header.grain_bytes


@dataclass(frozen=True)
class DiskStats:
    capacity_sectors: int
    allocated_bytes: int
    extent_count: int

    @property
    def capacity_bytes(self) -> int:
        return self.capacity_sectors * SECTOR


def _iter_extent_files(descriptor: Path) -> list[Path]:
    base = descriptor.with_name(descriptor.stem)  # strips .vmdk, keeps prefix
    files = sorted(descriptor.parent.glob(base.name + "-s*.vmdk"))
    if not files:
        raise VmdkFormatError(f"no extent files found for {descriptor}")
    return files


def _parse_extent_header(path: Path) -> ExtentHeader:
    with path.open("rb") as f:
        hdr = f.read(512)
    if len(hdr) < 72:
        raise VmdkFormatError(f"{path}: truncated header")
    magic, _version, _flags = struct.unpack_from("<III", hdr, 0)
    if magic != SPARSE_MAGIC:
        raise VmdkFormatError(f"{path}: bad magic {magic:#x}")
    capacity, grain_sectors = struct.unpack_from("<QQ", hdr, 12)
    gt_per_gd = struct.unpack_from("<I", hdr, 44)[0]
    gd_offset = struct.unpack_from("<Q", hdr, 56)[0]
    return ExtentHeader(
        path=path,
        capacity_sectors=capacity,
        grain_sectors=grain_sectors,
        gt_entries_per_table=gt_per_gd,
        gd_offset=gd_offset,
    )


class RawSparseExtent:
    """Random-sector reader for a single sparse extent file."""

    def __init__(self, header: ExtentHeader) -> None:
        self._h = header
        self._f: BinaryIO = header.path.open("rb")  # noqa: SIM115 — long-lived handle
        self._gt_cache: dict[int, tuple[int, ...]] = {}
        self._gd: tuple[int, ...] | None = None

    def close(self) -> None:
        f = self._f
        if f is not None and not f.closed:
            f.close()

    def _grain_dir(self) -> tuple[int, ...]:
        if self._gd is None:
            self._f.seek(self._h.gd_offset * SECTOR)
            raw = self._f.read(self._h.gd_entries * 4)
            self._gd = struct.unpack(f"<{self._h.gd_entries}I", raw)
        return self._gd

    def _read_gt(self, index: int) -> tuple[int, ...]:
        gt = self._gt_cache.get(index)
        if gt is None:
            gt_sector = self._grain_dir()[index]
            if gt_sector == 0:
                gt = tuple([0] * self._h.gt_entries_per_table)
            else:
                self._f.seek(gt_sector * SECTOR)
                raw = self._f.read(self._h.gt_entries_per_table * 4)
                gt = struct.unpack(f"<{self._h.gt_entries_per_table}I", raw)
            self._gt_cache[index] = gt
        return gt

    def read(self, sector: int, count: int) -> bytes:
        """Read ``count`` sectors starting at ``sector`` (within this extent)."""
        out: list[bytes] = []
        pos, remaining = sector, count
        while remaining > 0:
            grain_index, offset_in_grain = divmod(pos * SECTOR, self._h.grain_bytes)
            gt = self._read_gt(grain_index // self._h.gt_entries_per_table)
            addr = gt[grain_index % self._h.gt_entries_per_table]
            can_read = min((self._h.grain_bytes - offset_in_grain) // SECTOR, remaining)
            if addr & 0x80000000 or (addr & ~0x80000000) == 0:
                out.append(b"\x00" * (can_read * SECTOR))
            else:
                self._f.seek((addr & ~0x80000000) * SECTOR + offset_in_grain)
                out.append(self._f.read(can_read * SECTOR))
            pos += can_read
            remaining -= can_read
        return b"".join(out)


class RawSplitSparseDisk:
    """Concatenated view over all extents of a split-sparse VMDK."""

    def __init__(self, descriptor: Path) -> None:
        self.descriptor = Path(descriptor)
        self._extents: list[RawSparseExtent] = []
        for path in _iter_extent_files(self.descriptor):
            hdr = _parse_extent_header(path)
            self._extents.append(RawSparseExtent(hdr))

    @property
    def capacity_sectors(self) -> int:
        return sum(e._h.capacity_sectors for e in self._extents)

    def read(self, sector: int, count: int) -> bytes:
        out: list[bytes] = []
        pos, remaining = sector, count
        for extent in self._extents:
            cap = extent._h.capacity_sectors
            if remaining <= 0:
                break
            if pos < cap:
                take = min(remaining, cap - pos)
                out.append(extent.read(pos, take))
                pos = 0
                remaining -= take
            else:
                pos -= cap
        if remaining > 0:
            raise VmdkFormatError(f"read past end of disk (sector {sector + count})")
        return b"".join(out)

    def close(self) -> None:
        for e in self._extents:
            e.close()


def grain_stats(descriptor: Path) -> DiskStats:
    """Walk every grain table and count allocated grains (true data footprint)."""
    total_capacity = 0
    total_allocated = 0
    for path in _iter_extent_files(Path(descriptor)):
        hdr = _parse_extent_header(path)
        with path.open("rb") as f:
            f.seek(hdr.gd_offset * SECTOR)
            gd = struct.unpack(f"<{hdr.gd_entries}I", f.read(hdr.gd_entries * 4))
            allocated = 0
            remaining = hdr.grains
            for gt_sector in gd:
                if remaining <= 0:
                    break
                if gt_sector == 0:
                    remaining -= hdr.gt_entries_per_table
                    continue
                f.seek(gt_sector * SECTOR)
                gtv = struct.unpack(
                    f"<{hdr.gt_entries_per_table}I", f.read(hdr.gt_entries_per_table * 4)
                )
                for g in gtv[: max(0, remaining)]:
                    a = g & 0x7FFFFFFF
                    if not (g & 0x80000000) and a != 0:
                        allocated += 1
                remaining -= hdr.gt_entries_per_table
        total_capacity += hdr.capacity_sectors
        total_allocated += allocated * hdr.grain_bytes
    return DiskStats(
        capacity_sectors=total_capacity,
        allocated_bytes=total_allocated,
        extent_count=len(_iter_extent_files(Path(descriptor))),
    )


def extent_list(descriptor: Path) -> list[tuple[int, str]]:
    """Parse ``RW <sectors> SPARSE "<name>"`` lines from a descriptor."""
    text = Path(descriptor).read_bytes().decode("windows-1252", "replace")
    return [(int(s), n) for s, n in re.findall(r'^RW (\d+) SPARSE "([^"]+)"', text, re.M)]

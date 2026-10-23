"""Raw access to split-sparse (twoGbMaxExtentSparse) VMDKs.

Reads arbitrary sectors and walks grain tables to report the true allocated
(= data) footprint, without ever mounting anything. Ported from the proven
scratch scripts (``vmdk_read.py`` / ``grain_walk.py``) with full typing.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
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
    flags: int = 0
    overhead_sectors: int = 0
    rgd_offset: int = 0
    descriptor_offset: int = 0
    descriptor_size: int = 0

    @property
    def grain_bytes(self) -> int:
        return self.grain_sectors * SECTOR

    @property
    def grains(self) -> int:
        return (self.capacity_sectors + self.grain_sectors - 1) // self.grain_sectors

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
    """Extent files named in the descriptor (any filename convention works).

    Older builds globbed by the ``<stem>-s*.vmdk`` convention, which silently
    missed disks whose extents carry non-standard names (e.g. produced by
    qemu-img). The descriptor's RW lines are the source of truth.
    """
    files = [descriptor.parent / name.replace("\\", "/") for _, name in extent_list(descriptor)]
    missing = [p for p in files if not p.is_file()]
    if missing:
        raise VmdkFormatError(
            f"missing extent files for {descriptor}: {', '.join(p.name for p in missing[:4])}"
        )
    return files


def host_footprint_bytes(descriptor: Path) -> int:
    """Host bytes of a descriptor plus every extent file it names."""
    files = [descriptor, *_iter_extent_files(descriptor)]
    return sum(p.stat().st_size for p in files if p.is_file())


def _parse_extent_header(path: Path) -> ExtentHeader:
    with path.open("rb") as f:
        hdr = f.read(512)
    if len(hdr) != 512:
        raise VmdkFormatError(f"{path}: truncated header")
    magic, version, flags = struct.unpack_from("<III", hdr, 0)
    if magic != SPARSE_MAGIC:
        raise VmdkFormatError(f"{path}: bad magic {magic:#x}")
    if version not in (1, 3) or flags & ~0x7:
        raise VmdkFormatError(f"{path}: unsupported sparse version/flags")
    descriptor_offset, descriptor_size = struct.unpack_from("<QQ", hdr, 28)
    rgd_offset = struct.unpack_from("<Q", hdr, 48)[0]
    overhead = struct.unpack_from("<Q", hdr, 64)[0]
    compression = struct.unpack_from("<H", hdr, 77)[0]
    if bool(flags & 2) != bool(rgd_offset):
        raise VmdkFormatError(f"{path}: redundant grain-directory flag/offset mismatch")
    if compression or hdr[72]:
        raise VmdkFormatError(f"{path}: compressed or unclean sparse extent unsupported")
    if bool(descriptor_offset) != bool(descriptor_size):
        raise VmdkFormatError(f"{path}: descriptor reservation offset/size mismatch")
    capacity, grain_sectors = struct.unpack_from("<QQ", hdr, 12)
    gt_per_gd = struct.unpack_from("<I", hdr, 44)[0]
    gd_offset = struct.unpack_from("<Q", hdr, 56)[0]
    if not capacity or grain_sectors < 8 or grain_sectors & (grain_sectors - 1):
        raise VmdkFormatError(f"{path}: invalid sparse capacity/grain size")
    if gt_per_gd != 512:
        raise VmdkFormatError(f"{path}: unsupported grain-table size {gt_per_gd}")
    header = ExtentHeader(
        path=path,
        capacity_sectors=capacity,
        grain_sectors=grain_sectors,
        gt_entries_per_table=gt_per_gd,
        gd_offset=gd_offset,
        flags=flags,
        overhead_sectors=overhead,
        rgd_offset=rgd_offset,
        descriptor_offset=descriptor_offset,
        descriptor_size=descriptor_size,
    )
    _validate_sparse_metadata(header)
    return header


def _validate_sparse_metadata(header: ExtentHeader) -> None:
    """Check all metadata and data pointers before exposing an extent for writes."""
    size = header.path.stat().st_size
    metadata_end = header.overhead_sectors * SECTOR
    gd_size = ((header.gd_entries * 4 + SECTOR - 1) // SECTOR) * SECTOR
    regions = [(0, SECTOR)]
    directories: list[tuple[int, ...]] = []
    gt_size = header.gt_entries_per_table * 4
    with header.path.open("rb") as f:
        if header.descriptor_size:
            start = header.descriptor_offset * SECTOR
            end = start + header.descriptor_size * SECTOR
            if not (SECTOR <= start < end <= metadata_end <= size):
                raise VmdkFormatError(
                    f"{header.path}: descriptor reservation outside metadata boundaries"
                )
            regions.append((start, end))
            # QEMU reserves an empty descriptor area even in split-sparse extents.
            # A nonempty embedded descriptor is a different, unsupported layout.
            f.seek(start)
            remaining = end - start
            while remaining:
                chunk = f.read(min(remaining, 65536))
                if not chunk or any(chunk):
                    raise VmdkFormatError(
                        f"{header.path}: nonempty embedded descriptor unsupported"
                    )
                remaining -= len(chunk)
        offsets = [header.gd_offset]
        if header.rgd_offset:
            offsets.append(header.rgd_offset)
        for offset in offsets:
            gd_start = offset * SECTOR
            if not (gd_start >= SECTOR and gd_start + gd_size <= metadata_end <= size):
                raise VmdkFormatError(f"{header.path}: grain directory outside metadata boundaries")
            regions.append((gd_start, gd_start + gd_size))
            f.seek(gd_start)
            gd = struct.unpack(f"<{header.gd_entries}I", f.read(header.gd_entries * 4))
            directories.append(gd)
            for sector in gd:
                if not sector:
                    continue
                start = sector * SECTOR
                if not (start >= SECTOR and start + gt_size <= metadata_end):
                    raise VmdkFormatError(f"{header.path}: grain table outside metadata boundaries")
                regions.append((start, start + gt_size))
        regions.sort()
        if any(a[1] > b[0] for a, b in zip(regions, regions[1:], strict=False)):
            raise VmdkFormatError(f"{header.path}: overlapping sparse metadata")
        for index, sector in enumerate(directories[0]):
            redundant = directories[1][index] if header.rgd_offset else 0
            if header.rgd_offset and bool(sector) != bool(redundant):
                raise VmdkFormatError(f"{header.path}: inconsistent redundant grain directories")
            if not sector:
                continue
            f.seek(sector * SECTOR)
            raw = f.read(gt_size)
            if redundant:
                f.seek(redundant * SECTOR)
                if f.read(gt_size) != raw:
                    raise VmdkFormatError(f"{header.path}: inconsistent redundant grain tables")
            entries = struct.unpack(f"<{header.gt_entries_per_table}I", raw)
            for addr in entries:
                if not addr or (addr == 1 and header.flags & 4):
                    continue
                if not (
                    addr * SECTOR >= metadata_end and (addr + header.grain_sectors) * SECTOR <= size
                ):
                    raise VmdkFormatError(f"{header.path}: grain data outside file/data boundaries")


def validated_extent_headers(descriptor: Path) -> list[ExtentHeader]:
    """Preflight the complete disk before any caller mutates an extent."""
    extents = extent_list(descriptor)
    paths = _iter_extent_files(descriptor)
    if any(path.is_symlink() or path.stat().st_nlink != 1 for path in paths):
        raise VmdkFormatError(f"{descriptor}: linked/shared extent files are unsupported")
    headers = [_parse_extent_header(path) for path in paths]
    for (capacity, _), header in zip(extents, headers, strict=True):
        if capacity != header.capacity_sectors:
            raise VmdkFormatError(f"{header.path}: descriptor/header capacity mismatch")
    return headers


def validate_writable(descriptor: Path) -> None:
    """Read-only preflight for a standalone, supported split-sparse disk.

    Raise ``VmdkFormatError`` for unsafe descriptors, unsupported sparse
    features, inconsistent redundant grain tables, or invalid metadata/data
    boundaries. Missing/unreadable files may raise ``OSError``. Call before
    filesystem mutation; the caller must keep the image quiescent afterward.
    """
    validated_extent_headers(Path(descriptor))


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
            if addr == 0 or (addr == 1 and self._h.flags & 4):
                out.append(b"\x00" * (can_read * SECTOR))
            else:
                self._f.seek(addr * SECTOR + offset_in_grain)
                out.append(self._f.read(can_read * SECTOR))
            pos += can_read
            remaining -= can_read
        return b"".join(out)


class RawSplitSparseDisk:
    """Concatenated view over all extents of a split-sparse VMDK."""

    def __init__(self, descriptor: Path) -> None:
        self.descriptor = Path(descriptor)
        self._extents: list[RawSparseExtent] = []
        for hdr in validated_extent_headers(self.descriptor):
            self._extents.append(RawSparseExtent(hdr))

    @property
    def capacity_sectors(self) -> int:
        return sum(e._h.capacity_sectors for e in self._extents)

    def read(self, sector: int, count: int) -> bytes:
        if sector < 0 or count < 0 or sector + count > self.capacity_sectors:
            raise VmdkFormatError("read outside disk boundaries")
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
    for hdr in validated_extent_headers(Path(descriptor)):
        with hdr.path.open("rb") as f:
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
                    if g != 0 and not (g == 1 and hdr.flags & 4):
                        allocated += 1
                remaining -= hdr.gt_entries_per_table
        total_capacity += hdr.capacity_sectors
        total_allocated += allocated * hdr.grain_bytes
    return DiskStats(
        capacity_sectors=total_capacity,
        allocated_bytes=total_allocated,
        extent_count=len(_iter_extent_files(Path(descriptor))),
    )


def extent_allocations(descriptor: Path) -> list[int]:
    """Per-extent allocated (non-zero-grain) bytes, in descriptor order.

    Used to predict the host-space spike of ``resize2fs``: blocks beyond the
    new filesystem end are relocated into free space *before* it, which in a
    trimmed sparse disk means freshly allocated grains.
    """
    out: list[int] = []
    for hdr in validated_extent_headers(Path(descriptor)):
        with hdr.path.open("rb") as f:
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
                    f"<{hdr.gt_entries_per_table}I",
                    f.read(hdr.gt_entries_per_table * 4),
                )
                for g in gtv[: max(0, remaining)]:
                    if g != 0 and not (g == 1 and hdr.flags & 4):
                        allocated += 1
                remaining -= hdr.gt_entries_per_table
        out.append(allocated * hdr.grain_bytes)
    return out


def extent_list(descriptor: Path) -> list[tuple[int, str]]:
    """Parse a standalone split-sparse descriptor, rejecting ambiguous layouts."""
    descriptor = Path(descriptor)
    text = descriptor.read_bytes().rstrip(b"\x00").decode("windows-1252", "surrogateescape")
    if "\x00" in text:
        raise VmdkFormatError(f"{descriptor}: interior NUL in descriptor")
    extents: list[tuple[int, str]] = []
    settings: dict[str, str] = {}
    paths: set[Path] = set()
    identities: set[tuple[int, int]] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        extent = re.fullmatch(r'RW\s+(\d+)\s+SPARSE\s+"([^"\x00]+)"\s*(?:#.*)?', line)
        if extent:
            capacity, name = int(extent[1]), extent[2]
            windows = PureWindowsPath(name)
            relative = Path(name.replace("\\", "/"))
            path = (descriptor.parent / relative).resolve()
            if (
                capacity <= 0
                or windows.drive
                or windows.root
                or ":" in name
                or relative.is_absolute()
                or ".." in relative.parts
                or not path.is_relative_to(descriptor.parent.resolve())
                or path == descriptor.resolve()
                or path in paths
            ):
                raise VmdkFormatError(f"{descriptor}: unsafe or duplicate extent path {name!r}")
            if path.exists():
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity in identities:
                    raise VmdkFormatError(f"{descriptor}: aliased extent files")
                identities.add(identity)
            paths.add(path)
            extents.append((capacity, name))
            continue
        setting = re.fullmatch(r'([\w.]+)\s*=\s*(?:"([^"\x00]*)"|([^\s#"\x00]+))\s*(?:#.*)?', line)
        if not setting:
            raise VmdkFormatError(f"{descriptor}: unsupported descriptor line {line!r}")
        key, value = setting[1], setting[2] if setting[2] is not None else setting[3]
        if key in settings:
            raise VmdkFormatError(f"{descriptor}: duplicate descriptor key {key}")
        settings[key] = value
    if (
        not extents
        or settings.get("createType") != "twoGbMaxExtentSparse"
        or settings.get("version") != "1"
        or settings.get("parentCID", "ffffffff").lower() != "ffffffff"
        or "parentFileNameHint" in settings
    ):
        raise VmdkFormatError(f"{descriptor}: snapshots, parent chains, or unsupported layout")
    return extents

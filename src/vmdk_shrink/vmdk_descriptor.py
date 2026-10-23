"""Descriptor rewriting and orphan-extent cleanup for split-sparse VMDKs.

After the guest filesystem has been shrunk, extents beyond the new filesystem
end hold only stale data. Rewriting the descriptor to reference fewer extents
plus deleting the orphaned ``-sNNN.vmdk`` files is what actually reclaims host
space. The original descriptor is always preserved as ``<name>.vmdk.bak-desc``.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .vmdk_parser import (
    SECTOR,
    SPARSE_MAGIC,
    VmdkFormatError,
    extent_list,
    validated_extent_headers,
)

# Each extent in a twoGbMaxExtentSparse disk spans exactly this many sectors.
EXTENT_SECTORS = 65_536_000  # 32 GiB

DDB_TEMPLATE = """\
# Disk DescriptorFile
version=1
encoding="windows-1252"
CID=fffffffe
parentCID=ffffffff
createType="twoGbMaxExtentSparse"

# Extent description
{extents}

# The Disk Data Base
#DDB

ddb.adapterType = "lsilogic"
ddb.deletable = "true"
ddb.geometry.cylinders = "{cylinders}"
ddb.geometry.heads = "255"
ddb.geometry.sectors = "63"
ddb.virtualHWVersion = "22"
"""


@dataclass(frozen=True)
class DescriptorPlan:
    keep_extents: int
    new_capacity_sectors: int
    orphan_files: tuple[Path, ...]
    descriptor: Path | None = None
    source_bytes: bytes | None = None

    @property
    def new_capacity_gib(self) -> float:
        return self.new_capacity_sectors * SECTOR / 2**30


def _extent_sectors(extents: list[tuple[int, str]]) -> int:
    """Sector span of the disk's extents, derived from the descriptor.

    The original VMware-created disk uses ~32 GiB extents, but a descriptor
    written by ``qemu-img convert`` uses true 2 GiB extents — never assume a
    fixed size.
    """
    return extents[0][0] if extents else EXTENT_SECTORS


def compute_plan(descriptor: Path, fs_end_sector: int) -> DescriptorPlan:
    """Which extents must survive so the descriptor still covers ``fs_end_sector``.

    ``fs_end_sector`` is the exclusive end of the shrunk filesystem (partition
    end + 1). GPT metadata lives at the very end of the disk, so the new disk
    capacity must also cover a GPT backup area — callers should pass an
    ``fs_end_sector`` that already includes the desired tail slack.
    """
    descriptor = Path(descriptor)
    source = descriptor.read_bytes()
    extents = extent_list(descriptor)
    validated_extent_headers(descriptor)
    if not 0 < fs_end_sector <= sum(sectors for sectors, _ in extents):
        raise VmdkFormatError("requested end is outside disk capacity")
    capacity = 0
    keep = 0
    for sectors, _ in extents:
        capacity += sectors
        keep += 1
        if capacity >= fs_end_sector:
            break
    orphans = tuple((descriptor.parent / name).resolve() for _, name in extents[keep:])
    return DescriptorPlan(
        keep_extents=keep,
        new_capacity_sectors=capacity,
        orphan_files=orphans,
        descriptor=descriptor.resolve(),
        source_bytes=source,
    )


def _geometry_for(capacity_sectors: int) -> int:
    """Cylinder count for LBA-assisted geometry (255 heads, 63 sectors/track)."""
    total_sectors = (capacity_sectors // (255 * 63)) * 255 * 63
    return total_sectors // (255 * 63)


def rebuild_descriptor(descriptor: Path, plan: DescriptorPlan) -> None:
    """Rewrite the descriptor in place, keeping a ``.bak-desc`` backup."""
    descriptor = Path(descriptor)
    source = descriptor.read_bytes()
    if (plan.descriptor is not None and descriptor.resolve() != plan.descriptor) or (
        plan.source_bytes is not None and source != plan.source_bytes
    ):
        raise VmdkFormatError("descriptor changed since planning")
    extents = extent_list(descriptor)
    validated_extent_headers(descriptor)
    if not 1 <= plan.keep_extents <= len(extents):
        raise ValueError(
            f"plan keeps {plan.keep_extents} extents but descriptor has {len(extents)}"
        )
    if plan.new_capacity_sectors != sum(s for s, _ in extents[: plan.keep_extents]):
        raise VmdkFormatError("plan capacity does not match retained extents")
    unpadded = source.rstrip(b"\x00")
    padding = source[len(unpadded) :]
    text = unpadded.decode("windows-1252", "surrogateescape")
    heads = re.search(r'^\s*ddb\.geometry\.heads\s*=\s*"(\d+)"', text, re.M)
    sectors = re.search(r'^\s*ddb\.geometry\.sectors\s*=\s*"(\d+)"', text, re.M)
    geometry = int(heads[1]) * int(sectors[1]) if heads and sectors else 0
    lines = []
    extent_index = 0
    for line in text.splitlines(keepends=True):
        if re.match(r"^\s*RW\s+", line):
            extent_index += 1
            if extent_index > plan.keep_extents:
                continue
        if geometry and plan.keep_extents < len(extents):
            line = re.sub(
                r'^(\s*ddb\.geometry\.cylinders\s*=\s*")\d+("[^\r\n]*)',
                lambda m: f"{m[1]}{plan.new_capacity_sectors // geometry}{m[2]}",
                line,
            )
        lines.append(line)
    content = "".join(lines).encode("windows-1252", "surrogateescape") + padding
    if content == source:
        return
    backup = descriptor.with_name(descriptor.name + ".bak-desc")
    if not backup.exists():
        shutil.copy2(descriptor, backup)
    descriptor.write_bytes(content)


def delete_orphans(plan: DescriptorPlan) -> tuple[int, int]:
    """Delete unreferenced local extents, refusing ambiguous/shared files."""
    if not plan.orphan_files:
        return 0, 0
    if plan.descriptor is None or plan.source_bytes is None:
        raise VmdkFormatError("orphan deletion requires descriptor provenance")
    descriptor = plan.descriptor
    active = {(descriptor.parent / name).resolve() for _, name in extent_list(descriptor)}
    orphans = set(plan.orphan_files)
    for path in orphans:
        if (
            path.parent != descriptor.parent
            or path.is_symlink()
            or path in active
            or path.stat().st_nlink != 1
        ):
            raise VmdkFormatError(f"unsafe orphan deletion: {path}")
    for other in descriptor.parent.rglob("*.vmdk"):
        if other.resolve() == descriptor or other.resolve() in orphans:
            continue
        with other.open("rb") as f:
            prefix = f.read(512)
        if prefix[:4] == SPARSE_MAGIC.to_bytes(4, "little"):
            continue
        # Unknown or unsupported descriptors are not evidence of exclusive ownership.
        try:
            references = {(other.parent / name).resolve() for _, name in extent_list(other)}
        except (OSError, VmdkFormatError) as exc:
            raise VmdkFormatError(f"cannot establish orphan ownership: {other}") from exc
        if references & orphans:
            raise VmdkFormatError(f"orphan extent is shared by {other}")
    deleted, freed = 0, 0
    for path in plan.orphan_files:
        freed += path.stat().st_size
        path.unlink()
        deleted += 1
    return deleted, freed

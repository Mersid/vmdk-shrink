"""Descriptor rewriting and orphan-extent cleanup for split-sparse VMDKs.

After the guest filesystem has been shrunk, extents beyond the new filesystem
end hold only stale data. Rewriting the descriptor to reference fewer extents
plus deleting the orphaned ``-sNNN.vmdk`` files is what actually reclaims host
space. The original descriptor is always preserved as ``<name>.vmdk.bak-desc``.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .vmdk_parser import SECTOR, extent_list

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
    extents = extent_list(descriptor)
    extent_sectors = _extent_sectors(extents)
    keep = 1
    while keep < len(extents) and keep * extent_sectors < fs_end_sector:
        keep += 1
    orphan_names = {name for _, name in extents[keep:]}
    orphans = tuple(p for p in (descriptor.parent / n for n in orphan_names) if p.is_file())
    return DescriptorPlan(
        keep_extents=keep,
        new_capacity_sectors=keep * extent_sectors,
        orphan_files=orphans,
    )


def _geometry_for(capacity_sectors: int) -> int:
    """Cylinder count for LBA-assisted geometry (255 heads, 63 sectors/track)."""
    total_sectors = (capacity_sectors // (255 * 63)) * 255 * 63
    return total_sectors // (255 * 63)


def rebuild_descriptor(descriptor: Path, plan: DescriptorPlan) -> None:
    """Rewrite the descriptor in place, keeping a ``.bak-desc`` backup."""
    backup = descriptor.with_name(descriptor.name + ".bak-desc")
    if not backup.exists():
        shutil.copy2(descriptor, backup)
    extents = extent_list(descriptor)
    if plan.keep_extents > len(extents):
        raise ValueError(
            f"plan keeps {plan.keep_extents} extents but descriptor has {len(extents)}"
        )
    lines = [f'RW {sectors} SPARSE "{name}"' for sectors, name in extents[: plan.keep_extents]]
    content = DDB_TEMPLATE.format(
        extents="\n".join(lines),
        cylinders=_geometry_for(plan.new_capacity_sectors),
    )
    descriptor.write_bytes(content.encode("windows-1252"))


def delete_orphans(plan: DescriptorPlan) -> tuple[int, int]:
    """Delete orphaned extent files; return (deleted_count, bytes_freed)."""
    deleted, freed = 0, 0
    for path in plan.orphan_files:
        freed += path.stat().st_size
        path.unlink()
        deleted += 1
    return deleted, freed

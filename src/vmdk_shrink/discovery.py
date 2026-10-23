"""Descriptor-based disk selection shared by commands and services."""

from __future__ import annotations

from pathlib import Path

from .vmdk_parser import extent_list


def disks_in(vm_dir: Path) -> list[Path]:
    disks = []
    for path in sorted(vm_dir.glob("*.vmdk")):
        # Sparse extents start with a binary magic; never decode whole extents.
        with path.open("rb") as stream:
            if not stream.read(64).lstrip().startswith(b"# Disk DescriptorFile"):
                continue
        disks.append(path)
    return disks


def select_disk(vm_dir: Path, explicit: Path | None = None) -> Path:
    vm_dir = vm_dir.resolve()
    if explicit is not None:
        path = explicit.resolve()
        if path.parent != vm_dir:
            raise ValueError("the disk descriptor must be directly inside the VM directory")
        extent_list(path)
        return path
    disks = disks_in(vm_dir)
    if not disks:
        raise ValueError(f"no supported split-sparse descriptor in {vm_dir}")
    if len(disks) != 1:
        raise ValueError("multiple disks found; select one explicitly with --vmdk")
    extent_list(disks[0])
    return disks[0]

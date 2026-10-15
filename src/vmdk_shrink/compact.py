"""``vmdk compact``: reclaim host space WITHOUT touching the guest filesystem.

zerofree zeroes free blocks (inside the unchanged filesystem), then
``vmware-vdiskmanager -k`` deallocates the now-zero grains. Capacity is never
reduced — a running guest can expand the disk again on demand.
"""

from __future__ import annotations

from pathlib import Path

from .console import err, info, ok, step
from .fsops import discover_data_disk, fs_facts, unmount, zerofree
from .guest import Guest
from .helper_session import ensure_assets, shutdown_helper
from .paths import DEFAULT_PATHS, HostPaths
from .vdiskmanager import VDiskManager
from .vmdk_parser import grain_stats


def _du_bytes(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def _extent_files(vmdk: Path) -> list[Path]:
    stem = vmdk.with_suffix("").name  # strips .vmdk
    return sorted(vmdk.parent.glob(stem + "-s*.vmdk"))


def _zero_other_ext4(guest: Guest, *, skip_partition: str) -> list[str]:
    """zerofree every ext4 filesystem except the data partition (e.g. boot LVs)."""
    from .fsops import lsblk

    zeroed: list[str] = []
    devices = lsblk(guest)
    stack = list(devices)
    while stack:
        dev = stack.pop()
        stack.extend(dev.get("children") or [])
        if dev.get("type") not in {"part", "lvm"} or dev.get("fstype") != "ext4":
            continue
        name = str(dev.get("name"))
        if name == skip_partition or name.endswith(skip_partition):
            continue
        output = zerofree(guest, f"/dev/{name}")
        zeroed.append(name)
        line = output.strip().splitlines()[-1] if output.strip() else "nothing to zero"
        info(f"zerofree /dev/{name}: {line}")
    return zeroed


def compact_disk(
    vmdk: Path,
    *,
    extra_vmdks: tuple[Path, ...] = (),
    zerofree_fs: bool = True,
    zero_boot_lv: bool = False,
    paths: HostPaths = DEFAULT_PATHS,
    keep_running: bool = False,
) -> int:
    """Compact host footprint. Returns bytes freed (negative = grew).

    ``extra_vmdks`` are zerofree'd (if ext4) and compacted alongside the main
    disk — pass the boot disk to reclaim its space too.
    """
    target_vmdks = (vmdk, *extra_vmdks)
    step(f"Compact-only mode for [val]{', '.join(v.name for v in target_vmdks)}[/val]")
    step("Filesystem size is NOT touched — capacity stays as-is")

    before_files = [f for t in target_vmdks for f in (t, *_extent_files(t))]
    before = _du_bytes(before_files)
    stats = grain_stats(vmdk)
    info(
        f"main disk: capacity [val]{stats.capacity_bytes / 2**30:.0f} GiB[/val], "
        f"allocated [val]{stats.allocated_bytes / 2**30:.1f} GiB[/val], "
        f"host files [val]{before / 2**30:.1f} GiB[/val] ({len(before_files)} file(s))"
    )

    assets, guest = ensure_assets(vmdk.parent, attach=target_vmdks, paths=paths, boot=zerofree_fs)
    try:
        if guest is not None and zerofree_fs:
            disk = discover_data_disk(guest)
            facts = fs_facts(guest, disk.partition_path)
            info(
                f"data disk /dev/{disk.device} ({disk.size_bytes / 2**30:.0f} GiB), "
                f"ext4 {facts.size_bytes / 2**30:.0f} GiB, "
                f"{facts.free_blocks * facts.block_size / 2**30:.1f} GiB free"
            )
            unmount(guest)
            step(f"zerofree [val]{disk.partition_path}[/val]")
            output = zerofree(guest, disk.partition_path)
            info(output.strip().splitlines()[-1] if output.strip() else "nothing to zero")
            if zero_boot_lv:
                _zero_other_ext4(guest, skip_partition=disk.partition)
    finally:
        if not keep_running:
            shutdown_helper(assets.vmx, guest, paths=paths)

    for t in target_vmdks:
        step(f"vdiskmanager -k on [val]{t.name}[/val]")
        VDiskManager(paths).compact(t)

    after = _du_bytes(before_files)
    freed = before - after
    if freed >= 0:
        ok(f"freed [ok]{freed / 2**30:.2f} GiB[/ok] ({before / 2**30:.1f} -> {after / 2**30:.1f})")
    else:
        err(f"host files grew by {abs(freed) / 2**30:.2f} GiB (unexpected)")
    return freed

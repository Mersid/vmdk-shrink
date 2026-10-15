"""``vmdk shrink``: the full lossless pipeline with hash verification.

Stages: baseline manifest -> fs shrink -> partition shrink (original start LBA)
-> zerofree -> compact + descriptor rebuild -> verify manifest -> diff verdict.
"""

from __future__ import annotations

import time
from pathlib import Path

from .console import console, err, info, ok, step
from .fsops import (
    check_fs,
    discover_data_disk,
    fs_facts,
    largest_disk,
    mount_ro,
    partition_geometry,
    poweroff,
    recreate_partition,
    relocate_gpt_backup,
    resize_fs,
    unmount,
    zerofree,
)
from .guest import GuestError
from .helper_session import ensure_assets, shutdown_helper
from .manifest import collect, diff, load
from .paths import DEFAULT_PATHS, HostPaths
from .vdiskmanager import VDiskManager
from .vmdk_descriptor import compute_plan, delete_orphans, rebuild_descriptor
from .vmdk_parser import grain_stats

SECTOR = 512
GPT_TAIL_SECTORS = 34  # backup header + partition entry array


def _extent_files(vmdk: Path) -> list[Path]:
    stem = vmdk.with_suffix("").name
    return sorted(vmdk.parent.glob(stem + "-s*.vmdk"))


def _du_bytes(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def shrink(
    vm_dir: Path,
    target_gib: int,
    *,
    data_vmdk: Path | None = None,
    yes: bool = False,
    keep_running: bool = False,
    paths: HostPaths = DEFAULT_PATHS,
) -> int:
    """Run the full pipeline; return process exit code (0 ok, 2 = data differs)."""
    data_vmdk = data_vmdk or _largest_split_sparse(vm_dir)
    step(f"Full shrink of [val]{data_vmdk.name}[/val] to [val]{target_gib} GiB[/val]")

    before_files = [data_vmdk, *_extent_files(data_vmdk)]
    before = _du_bytes(before_files)
    stats = grain_stats(data_vmdk)
    info(
        f"capacity [val]{stats.capacity_bytes / 2**30:.0f} GiB[/val], "
        f"allocated [val]{stats.allocated_bytes / 2**30:.1f} GiB[/val], "
        f"host [val]{before / 2**30:.1f} GiB[/val]"
    )

    t0 = time.monotonic()
    assets, guest = ensure_assets(vm_dir, attach=(data_vmdk,), paths=paths, boot=not keep_running)
    assert guest is not None

    # -- discovery ---------------------------------------------------------
    disk = discover_data_disk(guest)
    facts = fs_facts(guest, disk.partition_path)
    geo = partition_geometry(guest, disk.device)
    info(
        f"data disk /dev/{disk.device}: {disk.size_bytes / 2**30:.0f} GiB; "
        f"partition starts at LBA {geo.start_lba}; "
        f"ext4 {facts.size_bytes / 2**30:.0f} GiB, {facts.used_gib:.1f} GiB in use"
    )
    min_gib = int(facts.used_gib * 1.08) + 1
    if target_gib < min_gib:
        raise GuestError(
            f"target {target_gib} GiB is below the safety floor "
            f"{min_gib} GiB (used {facts.used_gib:.1f} GiB + 8% headroom)"
        )

    # -- baseline manifest -------------------------------------------------
    mount_ro(guest, disk.partition_path)
    step("Hashing baseline (every regular file, RO mount)")
    baseline = collect(guest, out_path=vm_dir / "manifest_before.json")
    ok(f"baseline: {len(baseline)} files hashed")
    unmount(guest)

    # -- shrink fs + partition --------------------------------------------
    step(f"e2fsck -fy on {disk.partition_path}")
    check_fs(guest, disk.partition_path, fix=True)
    step(f"resize2fs to {target_gib} GiB")
    resize_fs(guest, disk.partition_path, target_gib)
    step("post-resize fsck")
    check_fs(guest, disk.partition_path, fix=True)

    post = fs_facts(guest, disk.partition_path)
    assert post.block_size == facts.block_size
    new_end = geo.start_lba + post.block_count * (post.block_size // SECTOR) - 1
    step(f"Recreating partition 1 at original start LBA {geo.start_lba}, end {new_end}")
    recreate_partition(guest, disk.device, start_lba=geo.start_lba, end_lba=new_end)
    step("post-repartition fsck (read-only)")
    check_fs(guest, disk.partition_path, fix=False)
    step("zerofree")
    zerofree(guest, disk.partition_path)

    poweroff(guest)
    guest.close()
    shutdown_helper(assets.vmx, None, paths=paths)

    # -- host-side: descriptor rebuild, orphan deletion, compact ------------
    plan = compute_plan(data_vmdk, fs_end_sector=new_end + 1 + GPT_TAIL_SECTORS)
    step(
        f"Rebuilding descriptor: keep [val]{plan.keep_extents}[/val] extents "
        f"(capacity -> {plan.new_capacity_gib:.0f} GiB), "
        f"{len(plan.orphan_files)} orphan(s) to delete"
    )
    rebuild_descriptor(data_vmdk, plan)
    deleted, freed = delete_orphans(plan)
    ok(f"deleted {deleted} orphan extents ({freed / 2**30:.2f} GiB freed)")

    step("vdiskmanager -k (compact)")
    VDiskManager(paths).compact(data_vmdk)

    # -- verification boot --------------------------------------------------
    _, verify_guest = ensure_assets(vm_dir, attach=(data_vmdk,), paths=paths, boot=True)
    assert verify_guest is not None
    try:
        # the disk presents no partitions until the backup GPT is relocated
        # to the new (smaller) end: sgdisk -e + partprobe
        device, _size = largest_disk(verify_guest)
        relocate_gpt_backup(verify_guest, device)
        vdisk = discover_data_disk(verify_guest)
        step("post-everything fsck (read-only)")
        check_fs(verify_guest, vdisk.partition_path, fix=False)
        mount_ro(verify_guest, vdisk.partition_path)
        step("Hashing again for verification")
        after_manifest = collect(verify_guest, out_path=vm_dir / "manifest_after.json")
        unmount(verify_guest)
    finally:
        if not keep_running:
            shutdown_helper(assets.vmx, verify_guest, paths=paths)

    base = load(vm_dir / "manifest_before.json")
    result = diff(base, after_manifest)
    after = _du_bytes(before_files)

    console.print()
    if result.identical:
        ok(f"[ok]ZERO DATA LOSS: all {len(base)} files byte-identical[/ok]")
    else:
        err(f"DATA DIFFERS: {result.summary()}")
        for p in result.changed[:10]:
            err(f"  changed: {p}")
        for p in result.removed[:10]:
            err(f"  missing: {p}")
        for p in result.added[:10]:
            err(f"  added:   {p}")
        if len(result.changed) > 10 or len(result.removed) > 10 or len(result.added) > 10:
            info("  (only first 10 of each category shown)")

    info(f"host footprint: {before / 2**30:.1f} -> {after / 2**30:.1f} GiB")
    info(f"elapsed: {(time.monotonic() - t0) / 60:.0f} min")
    return 0 if result.identical else 2


def _largest_split_sparse(vm_dir: Path) -> Path:
    """Pick the disk whose descriptor lists the most extents (the data disk)."""
    from .vmdk_parser import extent_list

    candidates = [
        d for d in vm_dir.glob("*.vmdk") if list(vm_dir.glob(d.with_suffix("").name + "-s*.vmdk"))
    ]
    if not candidates:
        raise GuestError(f"no split-sparse VMDK in {vm_dir}")
    best = max(candidates, key=lambda d: len(extent_list(d)))
    return best

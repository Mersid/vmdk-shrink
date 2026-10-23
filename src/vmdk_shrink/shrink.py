"""In-place shrink, checked before host edits and again before orphan cleanup."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from .checkpoints import prepare
from .console import console, err, info, ok, step
from .discovery import select_disk
from .executor import Executor
from .fsops import (
    StallError,
    check_fs,
    count_partitions,
    fs_facts,
    fs_type,
    nbd_written,
    partition_geometry,
    recreate_partition,
    relocate_gpt_backup,
    run_watched,
)
from .manifest import Manifest, diff, load
from .paths import DEFAULT_PATHS, HostPaths
from .presentation import compact_with_progress
from .progress import PhaseBar, managed_bar
from .reclaim import FreeMap, TrimUnavailable, collect_free_map, describe, trim_with_map
from .safety import offline_operation, require_offline
from .session import Session, attach_ro, attach_rw, hash_all, open_session
from .sidecar.gates import probe_fs_uuid
from .sidecar.orchestrator import run_sidecar_fs_phase
from .vdiskmanager import VDiskManager
from .vmdk_descriptor import compute_plan, delete_orphans, rebuild_descriptor
from .vmdk_parser import extent_allocations, extent_list, grain_stats, validate_writable

SECTOR = 512
_GIB = 2**30
GPT_TAIL_SECTORS = 34


class ShrinkError(RuntimeError):
    pass


def _extent_files(vmdk: Path) -> list[Path]:
    return [vmdk.parent / name for _, name in extent_list(vmdk)]


def _du_bytes(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def _hash_verification(
    via: str,
    vm_dir: Path,
    data_vmdk: Path,
    out_path: Path,
    *,
    session: Session,
    replicas: int,
    bar: PhaseBar | None,
) -> Manifest:
    """Baseline/verify hashing over the selected transport.

    ``container`` (gated legacy path): parallel RO qemu-nbd replicas over the
    9p bind mount. ``sidecar``: the same collect() run in the disposable VM
    over the native disk stack - no 9p in the data path at all.
    """
    if via == "sidecar":
        from .sidecar.hashing import hash_via_sidecar

        return hash_via_sidecar(vm_dir, data_vmdk, out_path=out_path, paths=session.paths, bar=bar)

    return hash_all(session, data_vmdk, out_path=out_path, replicas=replicas, bar=bar)


@offline_operation(directory=True)
@managed_bar
def shrink(
    vm_dir: Path,
    target_gib: int,
    *,
    data_vmdk: Path | None = None,
    replicas: int = 4,
    resume: bool = False,
    paths: HostPaths = DEFAULT_PATHS,
    mode: str = "nbd",
    hash_via: str = "container",
    _bar: PhaseBar | None = None,
) -> int:
    """Run the full pipeline; return process exit code (0 ok, 2 = data differs).

    Resume only reuses a disk-bound complete baseline before mutation began.
    Interrupted filesystem surgery always requires manual recovery.

    ``mode`` selects the fs-work transport: ``nbd`` operates on the VMDK in
    place through the container's qemu-nbd (1x, vp9fs-sensitive); ``sidecar``
    attaches the VMDK to a disposable Debian cloud-image VM and drives the
    same e2fsprogs sequence over SSH (1x, no 9p in the hot path).
    """
    vm_dir = Path(vm_dir).resolve()
    if mode not in ("nbd", "sidecar") or hash_via not in ("container", "sidecar"):
        raise ShrinkError("unsupported filesystem or hashing transport")
    if target_gib < 1:
        raise ShrinkError("target must be a positive number of GiB")
    data_vmdk = select_disk(vm_dir, data_vmdk)
    validate_writable(data_vmdk)
    checkpoint = prepare(
        data_vmdk, target_gib=target_gib, mode=mode, hash_via=hash_via, resume=resume
    )
    step(f"Full shrink of [val]{data_vmdk.name}[/val] to [val]{target_gib} GiB[/val]")

    free = shutil.disk_usage(vm_dir).free
    if free < 5 * 2**30:
        raise ShrinkError(
            f"only {free / 2**30:.1f} GiB free on {vm_dir} — the pipeline needs "
            "headroom for manifests and Docker VM churn (5 GiB minimum)"
        )

    before_files = [data_vmdk, *_extent_files(data_vmdk)]
    before = _du_bytes(before_files)
    stats = grain_stats(data_vmdk)
    info(
        f"capacity [val]{stats.capacity_bytes / 2**30:.0f} GiB[/val], "
        f"allocated [val]{stats.allocated_bytes / 2**30:.1f} GiB[/val], "
        f"host [val]{before / 2**30:.1f} GiB[/val]"
    )

    t0 = time.monotonic()

    # -- discovery + baseline manifest (container) --------------------------
    assert _bar is not None
    bar = _bar
    with open_session(vm_dir, paths=paths) as session:
        ex = session.executor
        disk = attach_ro(session, data_vmdk, index=0)
        part = disk.partition(1)
        assert_shrinkable_layout(ex, disk.device, part)
        facts = fs_facts(ex, part)
        geo = partition_geometry(ex, disk.device)
        recreate_partition(
            ex,
            disk.device,
            start_lba=geo.start_lba,
            end_lba=geo.start_lba + target_gib * _GIB // SECTOR - 1,
            validate_only=True,
        )
        info(
            f"partition starts at LBA {geo.start_lba}; "
            f"ext4 {facts.size_bytes / 2**30:.0f} GiB, {facts.used_gib:.1f} GiB in use"
        )
        step("Preflight filesystem check (read-only; dirty disks are refused)")
        check_fs(ex, part, fix=False, bar=bar)
        min_gib = int(facts.used_gib * 1.08) + 1
        info(f"{facts.used_gib:.1f} GiB in use (floor {min_gib} GiB)")
        if target_gib * _GIB >= facts.size_bytes:
            raise ShrinkError("target must be smaller than the current filesystem size")
        if target_gib < min_gib:
            raise ShrinkError(
                f"target {target_gib} GiB is below the safety floor "
                f"{min_gib} GiB (used {facts.used_gib:.1f} GiB + 8% headroom)"
            )
        if mode == "sidecar":
            partition_bytes = int(
                (ex.run(f"blockdev --getsize64 {part}", timeout=60).output or "0").strip() or "0"
            )
            fs_uuid = probe_fs_uuid(ex, part)
            if not fs_uuid:
                raise ShrinkError("data filesystem has no UUID; sidecar identify gate cannot run")
        else:
            partition_bytes, fs_uuid = 0, ""
        # resize2fs relocates every used block beyond the new fs end into free
        # space before it; on a trimmed sparse disk that allocates fresh host
        # grains, so the footprint spikes by that amount before -k reclaims it.
        new_end_sectors = geo.start_lba + target_gib * _GIB // SECTOR
        keep = compute_plan(
            data_vmdk, fs_end_sector=new_end_sectors + GPT_TAIL_SECTORS
        ).keep_extents
        per_extent = extent_allocations(data_vmdk)
        relocation = sum(per_extent[keep:])
        free_now = shutil.disk_usage(vm_dir).free
        if relocation + 2 * 2**30 > free_now:
            raise ShrinkError(
                f"resize to {target_gib} GiB relocates ~{relocation / 2**30:.0f} GiB "
                f"of tail data into head free space, but only {free_now / 2**30:.0f} GiB "
                f"is free on {vm_dir}. Move the VM to a volume with "
                f"~{relocation / 2**30 + 50:.0f} GiB free, or shrink in stages."
            )
        # progress-bar denominators: resize2fs writes ~the relocated bytes;
        # zerofree writes zeros over the target fs's free space.
        target_blocks = target_gib * _GIB // facts.block_size
        overflow_blocks = max(0, facts.block_count - target_blocks)
        resize_total_bytes = relocation if mode != "sidecar" else overflow_blocks * facts.block_size
        used_blocks = facts.block_count - facts.free_blocks
        zerofree_total_bytes = max(0, target_blocks - used_blocks) * facts.block_size
        session.nbd.disconnect()

        if resume:
            step("Resume: validated disk-bound baseline (no mutation was started)")
            baseline = load(checkpoint.baseline)
            ok(f"baseline: {len(baseline)} files (cached)")
        else:
            step(f"Hashing baseline (every regular file, via {hash_via})")
            baseline = _hash_verification(
                hash_via,
                vm_dir,
                data_vmdk,
                checkpoint.baseline.with_suffix(""),
                session=session,
                replicas=replicas,
                bar=bar,
            )
            checkpoint.baseline_ready(data_vmdk)
            ok(f"baseline: {len(baseline)} files hashed")
        # shed the WSL2 page cache before the write phase: multi-hundred-GiB
        # reads through the bind mount have been observed to kill the VM
        ex.run(
            "sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true",
            timeout=120,
            check=False,
        )

    # Mark BEFORE any repair/resize: no automatic replay after interruption.
    require_offline(vm_dir)
    checkpoint.save("mutation_started")
    free_map = None
    if mode == "sidecar":
        sc = run_sidecar_fs_phase(
            vm_dir,
            data_vmdk,
            target_gib,
            start_lba=geo.start_lba,
            expected_partition_bytes=partition_bytes,
            fs_uuid=fs_uuid,
            block_size=facts.block_size,
            paths=paths,
            bar=bar,
            resize_total_bytes=resize_total_bytes,
            zerofree_total_bytes=zerofree_total_bytes,
        )
        new_end = sc.fs_end_sector
        if sc.free_ranges:
            # VM is off; the extent files are closed. Clear the grain-table
            # entries of every free block host-side — no zero-writes.
            free_map = FreeMap(
                start_lba=geo.start_lba, block_size=facts.block_size, ranges=sc.free_ranges
            )
    else:
        new_end, free_map = _shrink_fs_with_retry(
            vm_dir,
            data_vmdk,
            target_gib,
            start_lba=geo.start_lba,
            block_size=facts.block_size,
            paths=paths,
            bar=bar,
            resize_total_bytes=resize_total_bytes,
            zerofree_total_bytes=zerofree_total_bytes,
        )
    # Check filesystem surgery while original descriptor/extents still exist.
    with open_session(vm_dir, paths=paths) as session:
        interim = _hash_verification(
            hash_via,
            vm_dir,
            data_vmdk,
            checkpoint.directory / "filesystem",
            session=session,
            replicas=replicas,
            bar=bar,
        )
    interim_diff = diff(baseline, interim)
    if not interim_diff.identical:
        checkpoint.save("verification_failed", verdict=interim_diff.summary())
        err(f"Filesystem verification failed: {interim_diff.summary()}; no host cleanup performed")
        return 2
    checkpoint.save("filesystem_verified")
    require_offline(vm_dir)
    if free_map is not None:
        info(describe(trim_with_map(data_vmdk, free_map)))

    # Capacity changes are necessary to verify the final geometry. Retain
    # unreferenced extents until final hashes match; descriptor backup is not a disk backup.
    plan = compute_plan(data_vmdk, fs_end_sector=new_end + 1 + GPT_TAIL_SECTORS)
    step(
        f"Rebuilding descriptor: keep [val]{plan.keep_extents}[/val] extents "
        f"(capacity -> {plan.new_capacity_gib:.0f} GiB), "
        f"{len(plan.orphan_files)} orphan(s) to delete"
    )
    rebuild_descriptor(data_vmdk, plan)
    checkpoint.save("descriptor_rebuilt", orphans=[str(p) for p in plan.orphan_files])

    with open_session(vm_dir, paths=paths) as session:
        disk = attach_rw(session, data_vmdk)
        relocate_gpt_backup(session.executor, disk.device)
        session.nbd.rescan(disk)
        check_fs(session.executor, disk.partition(1), fix=False, bar=bar)

    step("vdiskmanager -k (compact)")
    compact_with_progress(VDiskManager(paths), data_vmdk)

    # -- verification pass (container) --------------------------------------
    with open_session(vm_dir, paths=paths) as session:
        ex = session.executor
        disk = attach_ro(session, data_vmdk, index=0)
        part = disk.partition(1)
        step("post-everything fsck (read-only)")
        check_fs(ex, part, fix=False, bar=bar)
        session.nbd.disconnect()
        step(f"Hashing again for verification (via {hash_via})")
        after_manifest = _hash_verification(
            hash_via,
            vm_dir,
            data_vmdk,
            checkpoint.directory / "after",
            session=session,
            replicas=replicas,
            bar=bar,
        )

    base = baseline
    result = diff(base, after_manifest)
    after = _du_bytes(before_files)

    console.print()
    if result.identical:
        checkpoint.save("verified")
        require_offline(vm_dir)
        deleted, freed = delete_orphans(plan)
        ok(f"deleted {deleted} verified orphan extents ({freed / 2**30:.2f} GiB freed)")
        checkpoint.save("complete")
        after = _du_bytes(before_files)
        ok(f"Regular-file contents verified: all {len(base)} files byte-identical")
    else:
        checkpoint.save("verification_failed", verdict=result.summary())
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
    bar.close()
    return 0 if result.identical else 2


def _shrink_fs_with_retry(
    vm_dir: Path,
    data_vmdk: Path,
    target_gib: int,
    *,
    start_lba: int,
    block_size: int,
    paths: HostPaths,
    bar: PhaseBar,
    resize_total_bytes: int,
    zerofree_total_bytes: int,
) -> tuple[int, FreeMap | None]:
    """Shrink once in a dedicated session; the historical name is retained.

    Returns the new fs end sector plus the free map collected for host-side
    grain trimming (None when zerofree already zeroed free space as fallback).
    """
    try:
        with open_session(vm_dir, paths=paths, build=False) as session:
            return _shrink_fs_phase(
                session,
                data_vmdk,
                target_gib,
                start_lba=start_lba,
                block_size=block_size,
                bar=bar,
                resize_total_bytes=resize_total_bytes,
                zerofree_total_bytes=zerofree_total_bytes,
            )
    except StallError as exc:
        raise ShrinkError(
            f"filesystem phase interrupted: {exc}; do not retry automatically. "
            "Preserve the operation evidence and recover from a full backup."
        ) from exc


def _shrink_fs_phase(
    session: Session,
    data_vmdk: Path,
    target_gib: int,
    *,
    start_lba: int,
    block_size: int,
    bar: PhaseBar,
    resize_total_bytes: int,
    zerofree_total_bytes: int,
) -> tuple[int, FreeMap | None]:
    """One filesystem mutation attempt. Repair cannot guarantee crash recovery."""
    ex = session.executor
    disk = attach_rw(session, data_vmdk)
    part = disk.partition(1)
    step(f"e2fsck -fy on {part}")
    check_fs(ex, part, fix=True)
    step(f"resize2fs to [val]{target_gib} GiB[/val] (watched)")
    bar.start_bytes("resize2fs", resize_total_bytes, lambda: nbd_written(ex))
    try:
        run_watched(
            ex,
            f"resize2fs {part} {target_gib}G",
            phase="resize2fs",
            progress=lambda: nbd_written(ex),
            max_runtime_s=21600,
        )
    finally:
        bar.finish()
    step("post-resize fsck")
    check_fs(ex, part, fix=True)

    post = fs_facts(ex, part)
    if post.block_size != block_size:
        raise ShrinkError(f"block size changed across resize ({block_size} -> {post.block_size})")
    new_end = start_lba + post.block_count * (post.block_size // SECTOR) - 1
    step(f"Recreating partition 1 at original start LBA {start_lba}, end {new_end}")
    recreate_partition(ex, disk.device, start_lba=start_lba, end_lba=new_end)
    session.nbd.rescan(disk)
    part = disk.partition(1)
    step("post-repartition fsck (read-only)")
    check_fs(ex, part, fix=False)
    try:
        # The fs free map drives grain-table trimming after the session
        # closes — no zero-writes. On a clean fs this is exactly fstrim's
        # authority, and the tail beyond the new fs end is dropped by the
        # descriptor rebuild anyway.
        free_map = collect_free_map(ex, disk)
    except TrimUnavailable:
        free_map = None
        step("zerofree (watched) — free map unmappable")
        bar.start_bytes("zerofree", zerofree_total_bytes, lambda: nbd_written(ex))
        try:
            run_watched(
                ex,
                f"zerofree -v {part}",
                phase="zerofree",
                progress=lambda: nbd_written(ex),
                max_runtime_s=14400,
            )
        finally:
            bar.finish()
    return new_end, free_map


def assert_shrinkable_layout(ex: Executor, disk: str, partition: str) -> None:
    """Refuse disks this tool cannot shrink losslessly, before any change.

    Both hazards were produced against adversarial VMDKs: a second partition
    past the new disk end is silently truncated by the descriptor rebuild (its
    data is unrecoverably lost, and the partition-1 manifest diff cannot see
    it), and non-ext4 filesystems fail mid-pipeline after destructive steps
    have already run.
    """
    if count_partitions(ex, disk) != 1:
        raise ShrinkError(
            "the data disk must have exactly one partition; this tool shrinks "
            "single-partition ext4 disks only (other partitions would be "
            "silently truncated on the descriptor rebuild). Use a full-disk "
            "backup/restore path instead."
        )
    fstype = fs_type(ex, partition)
    if fstype not in ("ext2", "ext3", "ext4"):
        raise ShrinkError(
            f"partition 1 is '{fstype or 'unrecognized'}', not ext2/ext3/ext4; "
            "only ext-family filesystems support inline shrink (XFS cannot shrink)."
        )


def _largest_split_sparse(vm_dir: Path) -> Path:
    """Compatibility entry point for shared, unambiguous descriptor selection."""
    return select_disk(vm_dir)

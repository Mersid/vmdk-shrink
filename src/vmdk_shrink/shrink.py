"""``vmdk shrink``: the full lossless pipeline with hash verification.

Stages: baseline manifest -> fs shrink -> partition shrink (original start LBA)
-> zerofree -> compact + descriptor rebuild -> verify manifest -> diff verdict.

All Linux work happens in the container against ``/dev/nbd0`` (the VMDK exposed
by qemu-nbd); the host only runs ``vmware-vdiskmanager`` and rewrites the
descriptor.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from .console import console, err, info, ok, step, warn
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
from .manifest import diff, load
from .paths import DEFAULT_PATHS, HostPaths
from .session import Session, attach_rw, hash_all, open_session
from .sidecar.gates import probe_fs_uuid
from .sidecar.orchestrator import run_sidecar_fs_phase
from .vdiskmanager import VDiskManager
from .vmdk_descriptor import compute_plan, delete_orphans, rebuild_descriptor
from .vmdk_parser import extent_allocations, extent_list, grain_stats

SECTOR = 512
_GIB = 2**30
GPT_TAIL_SECTORS = 34


class ShrinkError(RuntimeError):
    pass


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
    replicas: int = 4,
    resume: bool = False,
    paths: HostPaths = DEFAULT_PATHS,
    mode: str = "nbd",
) -> int:
    """Run the full pipeline; return process exit code (0 ok, 2 = data differs).

    ``resume`` reuses an existing ``manifest_before.raw`` (e.g. after a crash
    mid-run) instead of re-hashing — the expensive stage becomes free.

    ``mode`` selects the fs-work transport: ``nbd`` operates on the VMDK in
    place through the container's qemu-nbd (1x, vp9fs-sensitive); ``sidecar``
    attaches the VMDK to a disposable Debian cloud-image VM and drives the
    same e2fsprogs sequence over SSH (1x, no 9p in the hot path).
    """
    vm_dir = Path(vm_dir).resolve()
    data_vmdk = data_vmdk or _largest_split_sparse(vm_dir)
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
    with open_session(vm_dir, paths=paths) as session:
        ex = session.executor
        disk = attach_rw(session, data_vmdk)
        part = disk.partition(1)
        assert_shrinkable_layout(ex, disk.device, part)
        facts = fs_facts(ex, part)
        geo = partition_geometry(ex, disk.device)
        info(
            f"partition starts at LBA {geo.start_lba}; "
            f"ext4 {facts.size_bytes / 2**30:.0f} GiB, {facts.used_gib:.1f} GiB in use"
        )
        # replay the journal now (RW) so later read-only mounts are accepted;
        # a dirty ext4 refuses RO mounts until recovery completes
        step("e2fsck -fy (journal replay before hashing)")
        check_fs(ex, part, fix=True)
        # measure AFTER fsck: it frees blocks orphaned by an interrupted
        # resize2fs, so the pre-fsck count overstates real usage
        facts = fs_facts(ex, part)
        min_gib = int(facts.used_gib * 1.08) + 1
        info(f"after fsck: {facts.used_gib:.1f} GiB in use (floor {min_gib} GiB)")
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
        extent_sectors = stats.capacity_sectors // max(stats.extent_count, 1)
        new_end_sectors = geo.start_lba + target_gib * _GIB // SECTOR
        keep = -(-new_end_sectors // extent_sectors)  # ceil
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
        session.nbd.disconnect()

        baseline_path = vm_dir / "manifest_before.raw"
        if resume and baseline_path.is_file():
            step("Resume: reusing existing baseline manifest")
            baseline = load(baseline_path)
            ok(f"baseline: {len(baseline)} files (cached)")
        else:
            step("Hashing baseline (every regular file, RO replicas)")
            baseline = hash_all(
                session, data_vmdk, out_path=vm_dir / "manifest_before", replicas=replicas
            )
            ok(f"baseline: {len(baseline)} files hashed")
        # shed the WSL2 page cache before the write phase: multi-hundred-GiB
        # reads through the bind mount have been observed to kill the VM
        ex.run(
            "sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true",
            timeout=120,
            check=False,
        )

    # -- shrink fs + partition ----------------------------------------------
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
        )
        new_end = sc.fs_end_sector
    else:
        # The drvfs/9p layer under Docker occasionally loses a request
        # completion (vp9fs.dll Tflush race, microsoft/WSL#41702), wedging the
        # phase. The phase therefore runs in its own session: a StallError
        # tears the container down and a fresh one resumes — resize2fs and
        # zerofree are both safe to re-run after an interrupted attempt.
        new_end = _shrink_fs_with_retry(
            vm_dir,
            data_vmdk,
            target_gib,
            start_lba=geo.start_lba,
            block_size=facts.block_size,
            paths=paths,
        )

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

    # -- verification pass (container) --------------------------------------
    with open_session(vm_dir, paths=paths) as session:
        ex = session.executor
        disk = attach_rw(session, data_vmdk)
        # after the capacity drop the backup GPT lies beyond the new end and
        # the kernel registers no partitions: relocate it first
        relocate_gpt_backup(ex, disk.device)
        session.nbd.rescan(disk)
        part = disk.partition(1)
        step("post-everything fsck (read-only)")
        check_fs(ex, part, fix=False)
        session.nbd.disconnect()
        step("Hashing again for verification")
        after_manifest = hash_all(
            session, data_vmdk, out_path=vm_dir / "manifest_after", replicas=replicas
        )

    base = load(vm_dir / "manifest_before.raw")
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


MAX_FS_ATTEMPTS = 4


def _shrink_fs_with_retry(
    vm_dir: Path,
    data_vmdk: Path,
    target_gib: int,
    *,
    start_lba: int,
    block_size: int,
    paths: HostPaths,
) -> int:
    """Shrink the filesystem/partition in a dedicated session, retrying on stalls."""
    for attempt in range(1, MAX_FS_ATTEMPTS + 1):
        try:
            with open_session(vm_dir, paths=paths, build=False) as session:
                return _shrink_fs_phase(
                    session, data_vmdk, target_gib, start_lba=start_lba, block_size=block_size
                )
        except StallError as exc:
            if attempt == MAX_FS_ATTEMPTS:
                raise ShrinkError(
                    f"filesystem phase stalled in {MAX_FS_ATTEMPTS} consecutive sessions: {exc}"
                ) from exc
            warn(f"{exc}")
            warn(
                f"recovering: tearing down the container and resuming in a fresh "
                f"9p session (attempt {attempt + 1} of {MAX_FS_ATTEMPTS})"
            )
            time.sleep(15)
    raise ShrinkError("unreachable")  # pragma: no cover - loop always returns/raises


def _shrink_fs_phase(
    session: Session,
    data_vmdk: Path,
    target_gib: int,
    *,
    start_lba: int,
    block_size: int,
) -> int:
    """resize2fs + partition recreation + zerofree, watched for stalls.

    ``resize2fs`` is journal-atomic and resumable, ``zerofree`` is idempotent,
    and both are safe to re-run after an interrupted attempt (the retry loop
    re-runs ``e2fsck -fy`` first to release blocks orphaned by the kill).
    """
    ex = session.executor
    disk = attach_rw(session, data_vmdk, unsafe_cache=True)
    part = disk.partition(1)
    step(f"e2fsck -fy on {part}")
    check_fs(ex, part, fix=True)
    step(f"resize2fs to [val]{target_gib} GiB[/val] (watched)")
    run_watched(
        ex,
        f"resize2fs {part} {target_gib}G",
        phase="resize2fs",
        progress=lambda: nbd_written(ex),
        max_runtime_s=21600,
    )
    step("post-resize fsck")
    check_fs(ex, part, fix=True)

    post = fs_facts(ex, part)
    if post.block_size != block_size:
        raise ShrinkError(
            f"block size changed across resize ({block_size} -> {post.block_size})"
        )
    new_end = start_lba + post.block_count * (post.block_size // SECTOR) - 1
    step(f"Recreating partition 1 at original start LBA {start_lba}, end {new_end}")
    recreate_partition(ex, disk.device, start_lba=start_lba, end_lba=new_end)
    session.nbd.rescan(disk)
    part = disk.partition(1)
    step("post-repartition fsck (read-only)")
    check_fs(ex, part, fix=False)
    step("zerofree (watched)")
    run_watched(
        ex,
        f"zerofree -v {part}",
        phase="zerofree",
        progress=lambda: nbd_written(ex),
        max_runtime_s=14400,
    )
    return new_end


def assert_shrinkable_layout(ex: Executor, disk: str, partition: str) -> None:
    """Refuse disks this tool cannot shrink losslessly, before any change.

    Both hazards were produced against adversarial VMDKs: a second partition
    past the new disk end is silently truncated by the descriptor rebuild (its
    data is unrecoverably lost, and the partition-1 manifest diff cannot see
    it), and non-ext4 filesystems fail mid-pipeline after destructive steps
    have already run.
    """
    if count_partitions(ex, disk) > 1:
        raise ShrinkError(
            "the data disk has more than one partition; this tool shrinks "
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
    """Pick the disk whose descriptor lists the most extents (the data disk)."""
    candidates = [
        d for d in vm_dir.glob("*.vmdk") if list(vm_dir.glob(d.with_suffix("").name + "-s*.vmdk"))
    ]
    if not candidates:
        raise ShrinkError(f"no split-sparse VMDK in {vm_dir}")
    return max(candidates, key=lambda d: len(extent_list(d)))

"""Filesystem and partition operations, run through an :class:`Executor`.

The device is explicit (``/dev/nbd0p1`` from :mod:`vmdk_shrink.nbd`) — there is
no more "find the biggest disk" discovery, because the container attaches
exactly one VMDK per NBD slot.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .console import warn
from .executor import ExecError, Executor


class FsError(RuntimeError):
    pass


class StallError(FsError):
    """A watched phase made no I/O progress for too long.

    Root cause is the vp9fs.dll Tflush race (microsoft/WSL#41702): Docker's
    drvfs layer occasionally loses a request's completion, and everything
    queued behind it wedges. Recovery is a fresh container (a new 9p session
    works immediately); no ``wsl --shutdown`` needed unless the whole mount
    dies.
    """


@dataclass(frozen=True)
class FsFacts:
    block_size: int
    block_count: int
    free_blocks: int

    @property
    def size_bytes(self) -> int:
        return self.block_count * self.block_size

    @property
    def used_bytes(self) -> int:
        return (self.block_count - self.free_blocks) * self.block_size

    @property
    def used_gib(self) -> float:
        return self.used_bytes / 2**30


@dataclass(frozen=True)
class PartitionGeometry:
    number: int
    start_lba: int
    end_lba: int


def fs_facts(ex: Executor, partition_path: str) -> FsFacts:
    result = ex.run(f"dumpe2fs -h {partition_path}", timeout=120)
    text = result.output
    bs = int(_capture(text, r"Block size:\s+(\d+)"))
    bc = int(_capture(text, r"Block count:\s+(\d+)"))
    fb = int(_capture(text, r"Free blocks:\s+(\d+)"))
    return FsFacts(block_size=bs, block_count=bc, free_blocks=fb)


def count_partitions(ex: Executor, disk: str) -> int:
    """Number of partitions sgdisk reports on *disk* (GPT or MBR layout)."""
    result = ex.run(f"sgdisk -p {disk}", timeout=60)
    return sum(
        1
        for line in result.output.splitlines()
        if re.match(r"^\s*\d+\s+\d+\s+\d+\s+", line)
    )


def fs_type(ex: Executor, partition_path: str) -> str:
    """Filesystem type as util-linux blkid reports it (e.g. 'ext4', 'xfs')."""
    result = ex.run(f"blkid -o value -s TYPE {partition_path}", timeout=30)
    return result.output.strip().lower()


def partition_geometry(ex: Executor, disk: str, number: int = 1) -> PartitionGeometry:
    result = ex.run(f"sgdisk -p {disk}", timeout=60)
    for line in result.output.splitlines():
        m = re.match(rf"^\s*{number}\s+(\d+)\s+(\d+)\s+", line)
        if m:
            return PartitionGeometry(
                number=number, start_lba=int(m.group(1)), end_lba=int(m.group(2))
            )
    raise FsError(f"partition {number} not found on {disk}")


def check_fs(ex: Executor, partition_path: str, *, fix: bool = False) -> str:
    """Run e2fsck; exits 0/1/2 are success (1 = errors corrected, 2 = +reboot)."""
    flag = "-fy" if fix else "-fn"
    result = ex.run(f"e2fsck {flag} {partition_path}", timeout=14400, check=False)
    if result.exit_code not in (0, 1, 2):
        raise FsError(
            f"e2fsck failed (exit {result.exit_code}, uncorrected errors):\n{result.output[-2000:]}"
        )
    return result.output


def resize_fs(ex: Executor, partition_path: str, target_gib: int) -> str:
    # a 1 TiB fs on a spinning array can exceed 2 h of block relocation
    return ex.run(f"resize2fs {partition_path} {target_gib}G", timeout=21600).output


def recreate_partition(
    ex: Executor,
    disk: str,
    *,
    start_lba: int,
    end_lba: int,
    typecode: str = "8300",
    number: int = 1,
) -> None:
    """Delete and recreate the partition at the EXACT original start LBA.

    sgdisk's default 2048-sector alignment would move the start and the ext4
    superblock would no longer be found ("Bad magic number"). ``-a 1`` disables
    alignment so the start stays put.
    """
    ex.run(f"sgdisk --delete={number} {disk}", timeout=120)
    ex.run(
        f"sgdisk -a 1 --new={number}:{start_lba}:{end_lba} --typecode={number}:{typecode} {disk}",
        timeout=120,
    )


def relocate_gpt_backup(ex: Executor, disk: str) -> str:
    """Move the backup GPT header to the disk end (after a capacity drop)."""
    return ex.run(f"sgdisk -e {disk}", timeout=120).output


def zerofree(ex: Executor, device_path: str) -> str:
    return ex.run(f"zerofree -v {device_path}", timeout=14400).output


def nbd_written(ex: Executor, device: str = "/dev/nbd0") -> int:
    """Sectors written through the NBD device — the progress signal for watchdogs.

    ``/sys/block/<dev>/stat`` fields (0-indexed): 6 = write sectors; 8 is
    ``in_flight`` (an instantaneous queue depth, NOT a progress counter — it
    reads 0 between write bursts and would falsely report a stall).
    """
    result = ex.run(
        f"cat /sys/block/{device.removeprefix('/dev/')}/stat", timeout=30, check=False
    )
    try:
        return int(result.output.split()[6])
    except (IndexError, ValueError):
        return -1


def run_watched(
    ex: Executor,
    command: str,
    *,
    phase: str,
    progress: Callable[[], int],
    max_runtime_s: float,
    interval_s: float = 300.0,
    stall_limit: int = 3,
) -> str:
    """Run a long phase detached and poll its progress; raise :class:`StallError`.

    The command runs via ``docker exec -d`` so a wedged drvfs connection can
    never hang the control plane — the polls read local container files only.
    Completion is detected through an ``EXIT=<n>`` marker the wrapper appends
    to the log, so a crashed phase is told apart from a finished one.
    """
    log = f"/tmp/{phase}.log"
    ex.run_detached(f"( {command}; echo EXIT=$? ) > {log} 2>&1")
    deadline = time.monotonic() + max_runtime_s
    frozen = 0
    last = progress()
    while time.monotonic() < deadline:
        time.sleep(interval_s)
        tail = ex.run(f"cat {log}", timeout=30, check=False).output
        if "EXIT=" in tail:
            marker = tail.rsplit("EXIT=", 1)[1].split()[0]
            try:
                code = int(marker)
            except ValueError as exc:
                raise FsError(f"{phase}: unreadable exit marker in {log}") from exc
            if code != 0:
                raise FsError(f"{phase} failed (exit {code}):\n{tail[-2000:]}")
            return tail
        now = progress()
        if now < 0:
            # Unreadable counter — the control plane (docker engine/exec) itself
            # is wedged. Treat exactly like a frozen counter: once the engine
            # stops answering, progress can never resume on its own.
            frozen += 1
            warn(
                f"{phase}: progress unreadable for {frozen * interval_s / 60:.0f} min "
                f"(engine wedged?); log tail: {tail.strip()[-300:] or '<empty>'}"
            )
            if frozen >= stall_limit:
                raise StallError(
                    f"{phase}: control plane unreadable for "
                    f"{stall_limit * interval_s / 60:.0f} min; "
                    f"log tail: {tail.strip()[-500:] or '<empty>'}"
                )
        elif 0 <= now == last:
            frozen += 1
            warn(
                f"{phase}: no I/O progress for {frozen * interval_s / 60:.0f} min; "
                f"log tail: {tail.strip()[-300:] or '<empty>'}"
            )
            if frozen >= stall_limit:
                raise StallError(
                    f"{phase}: no I/O progress for {stall_limit * interval_s / 60:.0f} min; "
                    f"log tail: {tail.strip()[-500:] or '<empty>'}"
                )
        else:
            frozen = 0
        last = now
    raise StallError(f"{phase}: exceeded the {max_runtime_s / 60:.0f} min runtime limit")


def mount_ro(ex: Executor, partition_path: str, mount: str = "/mnt/data") -> None:
    ex.run(f"mkdir -p {mount} && mount -o ro {partition_path} {mount}", timeout=120)


def unmount(ex: Executor, mount: str = "/mnt/data") -> None:
    ex.run(f"umount {mount} || true", timeout=120, check=False)


def activate_lvm(ex: Executor) -> None:
    """Activate visible volume groups (containers get no udev auto-activation)."""
    ex.run("vgchange -ay 2>/dev/null || true", timeout=180, check=False)


def ext4_devices(ex: Executor, *, under: str | None = None) -> list[str]:
    """Ext4 block devices in the container, optionally scoped to one disk tree.

    ``under`` (e.g. ``/dev/nbd1``) limits the walk to that disk's subtree —
    its partitions plus any LVM logical volumes stacked on them.
    """
    out = ex.run("lsblk -J -b -o NAME,TYPE,FSTYPE", timeout=60, check=False).output
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ExecError(f"lsblk parse failure: {out[:200]}") from exc

    roots: list[dict[str, Any]] = list(parsed.get("blockdevices", []))
    if under is not None:
        wanted = under.removeprefix("/dev/")
        subtree: list[dict[str, Any]] = []
        stack = list(roots)
        while stack:
            node = stack.pop()
            if str(node.get("name")) == wanted:
                subtree.append(node)
            else:
                stack.extend(node.get("children") or [])
        roots = subtree

    names: list[str] = []
    stack = list(roots)
    while stack:
        dev = stack.pop()
        stack.extend(dev.get("children") or [])
        if dev.get("fstype") == "ext4":
            names.append(f"/dev/{dev['name']}")
    return names


def _capture(text: str, pattern: str) -> str:
    m = re.search(pattern, text)
    if not m:
        raise FsError(f"pattern not found: {pattern}")
    return m.group(1)

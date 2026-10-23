"""Filesystem and partition operations, run through an :class:`Executor`.

The device is explicit (``/dev/nbd0p1`` from :mod:`vmdk_shrink.nbd`) — there is
no more "find the biggest disk" discovery, because the container attaches
exactly one VMDK per NBD slot.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .console import warn
from .executor import ExecError, Executor
from .progress import PhaseBar


class FsError(RuntimeError):
    pass


class StallError(FsError):
    """A watched, explicitly retry-safe phase made no progress for too long."""


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
        1 for line in result.output.splitlines() if re.match(r"^\s*\d+\s+\d+\s+\d+\s+", line)
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


def check_fs(
    ex: Executor, partition_path: str, *, fix: bool = False, bar: PhaseBar | None = None
) -> str:
    """Run e2fsck; read-only checks require exit 0 (a clean filesystem).

    Repair mode also accepts exits 1/2 (errors corrected, reboot required).

    With *bar*, the five e2fsck passes drive a 5-step display via streaming
    output instead of one blocking call.
    """
    flag = "-fy" if fix else "-fn"
    if bar is None:
        result = ex.run(f"e2fsck {flag} {partition_path}", timeout=14400, check=False)
    else:
        seen = 0
        started = False
        progress_bar = bar
        pending = ""

        def _on_chunk(chunk: str) -> object:
            nonlocal seen, started, pending
            pending += chunk
            matches = re.findall(r"Pass (\d+)", pending)
            pending = pending[-16:]
            if not matches:
                return None
            current = max(int(n) for n in matches)
            if not started:
                progress_bar.start_steps("e2fsck", 5)
                started = True
            if current > seen:
                progress_bar.bump_files(current - seen)
                seen = current
            return None

        result = ex.stream(f"e2fsck {flag} {partition_path}", timeout=14400, chunk_cb=_on_chunk)

    success = result.exit_code in ((0, 1, 2) if fix else (0,))
    if bar is not None:
        bar.finish(success=success)
    if not success:
        reason = "uncorrected errors" if fix else "read-only check requires a clean filesystem"
        raise FsError(
            f"e2fsck failed (exit {result.exit_code}, {reason}):\n{result.output[-2000:]}"
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
    typecode: str | None = None,
    number: int = 1,
    validate_only: bool = False,
) -> None:
    """Shrink a GPT entry without changing its identity or its start sector.

    All metadata is read and validated before one sgdisk invocation writes
    the table. This is not crash-atomic; interruption requires manual recovery.
    ``typecode`` is retained for compatibility but cannot override the GPT type.
    ``validate_only`` performs the same checks without writing the table.
    """
    if number < 1 or start_lba < 1 or end_lba < start_lba:
        raise FsError("invalid partition geometry")
    quoted_disk = shlex.quote(disk)
    scan = ex.run(f"gdisk -l {quoted_disk}", timeout=60).output
    if not (
        re.search(r"(?m)^\s*MBR: protective\s*$", scan)
        and re.search(r"(?m)^\s*GPT: present\s*$", scan)
    ) or re.search(r"(?i)\b(hybrid|invalid|corrupt|warning|caution)\b", scan):
        raise FsError("only intact native GPT with a protective MBR is supported")
    try:
        table = json.loads(ex.run(f"sfdisk --json {quoted_disk}", timeout=60).output)[
            "partitiontable"
        ]
        if table["label"] != "gpt":
            raise FsError("only native GPT partition tables are supported")
        node = f"{disk}{'p' if disk[-1].isdigit() else ''}{number}"
        partitions = table["partitions"]
        matches = [p for p in partitions if p["node"] == node]
        if len(matches) != 1:
            raise FsError(f"partition {number} not uniquely identified on {disk}")
        part = matches[0]
        old_start = int(part["start"])
        old_end = old_start + int(part["size"]) - 1
        if not (
            table["unit"] == "sectors"
            and int(table["sectorsize"]) in (512, 4096)
            and int(table["firstlba"]) <= start_lba == old_start
            and end_lba <= old_end <= int(table["lastlba"])
        ):
            raise FsError(
                "invalid partition geometry: only shrinking at the original start is safe"
            )
        for other in partitions:
            if other is part:
                continue
            lo = int(other["start"])
            hi = lo + int(other["size"]) - 1
            if lo <= old_end and hi >= old_start:
                raise FsError("overlapping partition geometry")
        guid_pattern = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
        for key in ("type", "uuid"):
            if not re.fullmatch(guid_pattern, part[key]):
                raise FsError(f"invalid GPT {key} GUID")
        if typecode is not None and typecode.lower() != part["type"].lower():
            raise FsError("partition type override would change the original GPT type GUID")
        name = part.get("name", "")
        if not isinstance(name, str) or "\0" in name:
            raise FsError("invalid GPT partition name")
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise FsError("cannot validate GPT partition metadata") from exc
    info = ex.run(f"sgdisk --info={number} {quoted_disk}", timeout=60).output
    # sgdisk reports a hybrid MBR here; never allow its implicit conversion.
    if re.search(r"\b(hybrid|MBR only|converting)\b", info, re.IGNORECASE):
        raise FsError("unsupported hybrid or converted partition table")
    attributes = _capture(info, r"(?m)^Attribute flags: ([0-9a-fA-F]{16})\s*$")
    args = [
        "sgdisk",
        "-a",
        "1",
        f"--delete={number}",
        f"--new={number}:{start_lba}:{end_lba}",
        f"--typecode={number}:{part['type']}",
        f"--partition-guid={number}:{part['uuid']}",
        f"--change-name={number}:{name}",
        f"--attributes={number}:=:0x{attributes}",
        disk,
    ]
    if not validate_only:
        ex.run(shlex.join(args), timeout=120)


def relocate_gpt_backup(ex: Executor, disk: str) -> str:
    """Move the backup GPT header to the disk end (after a capacity drop)."""
    return ex.run(f"sgdisk -e {disk}", timeout=120).output


def zerofree(ex: Executor, device_path: str) -> str:
    return ex.run(f"zerofree -v {device_path}", timeout=14400).output


def free_block_ranges(ex: Executor, device_path: str) -> list[tuple[int, int]]:
    """Free fs-block ranges (inclusive) straight from the filesystem's own map.

    ``dumpe2fs`` is the same authority ``fstrim`` consults; on a clean,
    quiescent filesystem its free-block bitmap is exactly the set of blocks
    that may be discarded. Ranges come back per block group and need
    coalescing (see ``graintrim.merge_ranges``).
    """
    return parse_free_ranges(ex.run(f"dumpe2fs {device_path}", timeout=300).output)


def parse_free_ranges(text: str) -> list[tuple[int, int]]:
    """Parse ``dumpe2fs`` output into fs-block ranges (inclusive)."""
    ranges: list[tuple[int, int]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Free blocks:"):
            continue
        # Group lists are indented; the unindented header is only a count.
        if line == line.lstrip():
            continue
        body = stripped[len("Free blocks:") :].strip()
        for part in body.split(","):
            part = part.strip()
            if not part:
                continue
            first, _, last = part.partition("-")
            try:
                lo, hi = int(first), int(last or first)
            except ValueError as exc:
                raise FsError(f"cannot parse free-block range {part!r}") from exc
            if lo < 0 or hi < lo:
                raise FsError(f"invalid free-block range {part!r}")
            ranges.append((lo, hi))
    return ranges


def nbd_written(ex: Executor, device: str = "/dev/nbd0") -> int:
    """Sectors written through the NBD device — the progress signal for watchdogs.

    ``/sys/block/<dev>/stat`` fields (0-indexed): 6 = write sectors; 8 is
    ``in_flight`` (an instantaneous queue depth, NOT a progress counter — it
    reads 0 between write bursts and would falsely report a stall).
    """
    result = ex.run(f"cat /sys/block/{device.removeprefix('/dev/')}/stat", timeout=30, check=False)
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
    retry_safe: bool = False,
) -> str:
    """Run a long phase detached and poll its progress.

    The command runs via ``docker exec -d`` so a wedged drvfs connection can
    never hang the control plane — the polls read local container files only.
    Completion is detected through an ``EXIT=<n>`` marker the wrapper appends
    to the log, so a crashed phase is told apart from a finished one. A timeout
    does not stop the command or prove a resize can be resumed. By default
    interruptions raise FsError, not the retryable StallError.
    """

    def interrupted(message: str) -> FsError:
        if retry_safe:
            return StallError(message)
        return FsError(
            f"{message}; operation may still be running or interrupted; "
            "manual recovery required, do not automatically retry"
        )

    log = f"/tmp/{phase}.log"
    ex.run_detached(f"( {command}; echo EXIT=$? ) > {log} 2>&1")
    deadline = time.monotonic() + max_runtime_s
    next_progress = time.monotonic() + interval_s
    frozen = 0
    last = progress()
    while time.monotonic() < deadline:
        time.sleep(max(0, min(5.0, interval_s, deadline - time.monotonic())))
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
        if time.monotonic() < next_progress:
            continue
        next_progress = time.monotonic() + interval_s
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
                raise interrupted(
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
                raise interrupted(
                    f"{phase}: no I/O progress for {stall_limit * interval_s / 60:.0f} min; "
                    f"log tail: {tail.strip()[-500:] or '<empty>'}"
                )
        else:
            frozen = 0
        last = now
    raise interrupted(f"{phase}: exceeded the {max_runtime_s / 60:.0f} min runtime limit")


def mount_ro(ex: Executor, partition_path: str, mount: str = "/mnt/data") -> None:
    ex.run(
        f"mkdir -p {shlex.quote(mount)} && mount -o ro,noload "
        f"{shlex.quote(partition_path)} {shlex.quote(mount)}",
        timeout=120,
    )


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
    untyped: list[str] = []
    stack = list(roots)
    while stack:
        dev = stack.pop()
        stack.extend(dev.get("children") or [])
        fstype = dev.get("fstype")
        if fstype == "ext4":
            names.append(f"/dev/{dev['name']}")
        elif fstype is None and str(dev.get("type")) != "disk":
            # Containers without udev leave lsblk's FSTYPE column empty for
            # freshly mknod'd nbd partitions; probe the node directly.
            untyped.append(f"/dev/{dev['name']}")
    if untyped:
        joined = " ".join(untyped)
        out = ex.run(
            f'for n in {joined}; do t=$(blkid -o value -s TYPE "$n" 2>/dev/null); '
            'case "$t" in ext4) echo "$n";; esac; done',
            timeout=120,
            check=False,
        ).output
        names.extend(line.strip() for line in out.splitlines() if line.strip())
    return names


def _capture(text: str, pattern: str) -> str:
    m = re.search(pattern, text)
    if not m:
        raise FsError(f"pattern not found: {pattern}")
    return m.group(1)

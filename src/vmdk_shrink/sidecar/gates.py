"""Safety gates: assertions that must hold before and after each phase.

Every gate is a small function raising :class:`GateError` with a human
explanation. Gates are deliberately pure (they take a runner and values, not
global state) so the whole interlock net is unit-testable with fakes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..executor import Executor
from .ssh import GuestRunner, SshError


class GateError(RuntimeError):
    pass


def _single_line(output: str) -> str:
    return output.strip().splitlines()[-1] if output.strip() else ""


def require_no_locks(vm_dir: Path) -> None:
    """VMware must not hold the disks (a running VM or crashed session)."""
    locks = sorted(vm_dir.glob("*.lck"))
    if locks:
        raise GateError(
            "VMware lock dirs present (VM running or stale session?): "
            + ", ".join(p.name for p in locks[:4])
        )


def require_free_space(path: Path, needed_gib: float) -> None:
    free = shutil.disk_usage(path).free
    if free < needed_gib * 2**30:
        raise GateError(f"only {free / 2**30:.1f} GiB free on {path} (need {needed_gib:.0f} GiB)")


def require_vm_not_running(vmx_state: str, name: str) -> None:
    """``vmrun getGuestState`` output must report the VM powered off."""
    state = vmx_state.strip().lower()
    if "powered off" not in state and "none" not in state and "not running" not in state:
        raise GateError(f"{name} is not powered off (state: {vmx_state.strip()})")


def probe_fs_uuid(ex: Executor, partition: str) -> str:
    """Filesystem UUID of an ext4 partition (container side, e.g. /dev/nbd0p1)."""
    result = ex.run(f"dumpe2fs -h {partition}", timeout=60)
    for line in result.output.splitlines():
        if line.startswith("Filesystem UUID:"):
            return line.split(":", 1)[1].strip()
    raise GateError(f"no filesystem UUID in dumpe2fs output for {partition}")


def identify_data_disk(
    runner: GuestRunner, expected_uuid: str, expected_partition_bytes: int
) -> str:
    """Find the data partition in the guest by UUID *and* size; both must match.

    Returns the partition device (e.g. ``/dev/sdb1``). A UUID that matches but
    a size that does not (or vice versa) is a hard stop: wrong-disk ambiguity
    is fatal, never "probably fine".
    """
    result = runner.run(f"readlink -f /dev/disk/by-uuid/{expected_uuid}", timeout=30, check=False)
    devices = [ln.strip() for ln in result.output.splitlines() if ln.strip()]
    if len(devices) != 1:
        raise GateError(
            f"expected exactly one device with UUID {expected_uuid}, found {devices or 'none'}"
        )
    device = devices[0]
    size = (
        int(
            _single_line(
                runner.run(
                    f"cat /sys/class/block/{Path(device).name}/size 2>/dev/null "
                    f"|| lsblk -b -d -n -o SIZE '{device}'",
                    timeout=30,
                ).output
            )
            or "0"
        )
        * 512
    )
    if size != expected_partition_bytes:
        raise GateError(
            f"data partition {device} is {size} bytes, expected {expected_partition_bytes} "
            "(disk identity mismatch)"
        )
    return device


def require_unmounted(runner: GuestRunner, device: str) -> None:
    """The target filesystem must not be mounted anywhere in the guest."""
    result = runner.run(f"findmnt -S '{device}' -n -o TARGET", timeout=30, check=False)
    mounts = [ln.strip() for ln in result.output.splitlines() if ln.strip()]
    if mounts:
        raise GateError(f"{device} is mounted at {mounts} — refusing to operate on a live fs")


def assert_block_count(
    runner: GuestRunner, partition: str, expected_blocks: int, block_size: int
) -> None:
    """After resize2fs, the superblock must report exactly the planned size."""
    output = runner.run(f"/usr/sbin/dumpe2fs -h '{partition}'", timeout=60).output
    blocks = 0
    bs = 0
    for line in output.splitlines():
        if line.startswith("Block count:"):
            blocks = int(line.split(":", 1)[1])
        elif line.startswith("Block size:"):
            bs = int(line.split(":", 1)[1])
    if bs != block_size:
        raise GateError(f"block size changed across resize ({block_size} -> {bs})")
    if blocks != expected_blocks:
        raise GateError(
            f"resize did not reach the planned size: {blocks} blocks, expected {expected_blocks}"
        )


def assert_partition_layout(runner: GuestRunner, device: str, start_lba: int, end_lba: int) -> None:
    """Reread the table and verify both on-disk and kernel partition geometry."""
    runner.run(f"blockdev --rereadpt '{device}'", timeout=60)
    output = runner.run(f"/usr/sbin/sgdisk -p '{device}'", timeout=60).output
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == "1":
            got_start, got_end = int(fields[1]), int(fields[2])
            if (got_start, got_end) != (start_lba, end_lba):
                raise GateError(
                    f"partition 1 is [{got_start}, {got_end}], expected [{start_lba}, {end_lba}]"
                )
            name = Path(device).name
            partition = f"{name}{'p' if name[-1].isdigit() else ''}1"
            output = runner.run(
                f"cat /sys/class/block/{partition}/start /sys/class/block/{partition}/size",
                timeout=60,
            ).output
            try:
                kernel_start, kernel_size = map(int, output.split())
            except ValueError as exc:
                raise GateError(f"cannot read kernel geometry for {partition}") from exc
            if (kernel_start, kernel_size) != (start_lba, end_lba - start_lba + 1):
                raise GateError(
                    f"kernel partition geometry for {partition} is stale: "
                    f"[{kernel_start}, {kernel_start + kernel_size - 1}], "
                    f"expected [{start_lba}, {end_lba}]"
                )
            return
    raise GateError(f"partition 1 not found in sgdisk output for {device}")


def wait_ssh_ready(runner: GuestRunner, *, attempts: int, delay_s: float) -> None:
    """Poll until a batch-mode SSH command succeeds."""
    import time

    for attempt in range(1, attempts + 1):
        try:
            runner.run("true", timeout=15)
            return
        except SshError:
            if attempt == attempts:
                raise GateError(
                    f"SSH never became ready after {attempts} attempts "
                    f"({attempts * delay_s / 60:.0f} min)"
                ) from None
            time.sleep(delay_s)

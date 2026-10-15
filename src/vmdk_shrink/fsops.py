"""Guest-side filesystem and partition operations for the ext4 data disk.

Everything here runs inside the helper VM over SSH. The data disk is always
identified by *size* (largest disk attached) — device letters renumber between
boots, and trusting them corrupts things.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .guest import Guest, GuestError


class GuestFsError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiskInfo:
    device: str  # e.g. "sdb"
    size_bytes: int
    partition: str  # e.g. "sdb1"

    @property
    def partition_path(self) -> str:
        return f"/dev/{self.partition}"


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


def lsblk(guest: Guest) -> list[dict[str, Any]]:
    result = guest.run("lsblk -J -b -o NAME,SIZE,TYPE,FSTYPE", check=True)
    parsed = json.loads(result.output)
    devices: list[dict[str, Any]] = parsed.get("blockdevices", [])
    return devices


def largest_disk(guest: Guest, *, scratch_size_bytes: int = 2**30) -> tuple[str, int]:
    """Name and size of the largest attached disk, partitions NOT required.

    Used before GPT repair: a disk whose backup GPT lies beyond its (shrunk)
    end registers NO partitions in the kernel until ``sgdisk -e`` + rescan.
    """
    candidates: list[tuple[int, str]] = []
    for dev in lsblk(guest):
        if dev.get("type") == "disk" and int(dev.get("size", 0)) > scratch_size_bytes:
            candidates.append((int(dev["size"]), str(dev.get("name", ""))))
    if not candidates:
        raise GuestFsError("no attached disk larger than the scratch disk")
    size, name = max(candidates)
    return name, size


def discover_data_disk(guest: Guest, *, scratch_size_bytes: int = 2**30) -> DiskInfo:
    """The data disk = largest attached disk (scratch is 1 GiB, OS disk smaller)."""
    name, size = largest_disk(guest, scratch_size_bytes=scratch_size_bytes)
    part = _first_partition(guest, name)
    return DiskInfo(device=name, size_bytes=size, partition=part)


def _first_partition(guest: Guest, disk: str) -> str:
    result = guest.run(f"lsblk -J -b -o NAME,TYPE /dev/{disk}", check=True)
    parsed = json.loads(result.output)
    for dev in parsed.get("blockdevices", []):
        for child in dev.get("children") or []:
            if child.get("type") == "part":
                return str(child["name"])
    raise GuestFsError(f"disk {disk} has no partitions")


def fs_facts(guest: Guest, partition_path: str) -> FsFacts:
    result = guest.run(f"dumpe2fs -h {partition_path}", check=True)
    text = result.output
    bs = int(_capture(text, r"Block size:\s+(\d+)"))
    bc = int(_capture(text, r"Block count:\s+(\d+)"))
    fb = int(_capture(text, r"Free blocks:\s+(\d+)"))
    return FsFacts(block_size=bs, block_count=bc, free_blocks=fb)


def partition_geometry(guest: Guest, disk: str, number: int = 1) -> PartitionGeometry:
    result = guest.run(f"sgdisk -p /dev/{disk}", check=True)
    for line in result.output.splitlines():
        m = re.match(rf"^\s*{number}\s+(\d+)\s+(\d+)\s+", line)
        if m:
            return PartitionGeometry(
                number=number, start_lba=int(m.group(1)), end_lba=int(m.group(2))
            )
    raise GuestFsError(f"partition {number} not found on /dev/{disk}")


def check_fs(guest: Guest, partition_path: str, *, fix: bool = False) -> str:
    """Run e2fsck; exits 0/1/2 are success (1 = errors corrected, 2 = +reboot)."""
    flag = "-fy" if fix else "-fn"
    cmd = f"e2fsck {flag} {partition_path}"
    result = guest.run(cmd, timeout=3600, check=False)
    if result.exit_code not in (0, 1, 2):
        raise GuestFsError(
            f"e2fsck failed (exit {result.exit_code}, uncorrected errors):\n{result.output[-2000:]}"
        )
    return result.output


def resize_fs(guest: Guest, partition_path: str, target_gib: int) -> str:
    result = guest.run(f"resize2fs {partition_path} {target_gib}G", timeout=3600, check=True)
    return result.output


def recreate_partition(
    guest: Guest,
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
    guest.run(f"sgdisk --delete={number} /dev/{disk}", check=True)
    guest.run(
        f"sgdisk -a 1 --new={number}:{start_lba}:{end_lba} "
        f"--typecode={number}:{typecode} /dev/{disk}",
        check=True,
    )
    guest.run(f"partprobe /dev/{disk}", check=True)


def relocate_gpt_backup(guest: Guest, disk: str) -> str:
    """Move the backup GPT header to the disk end and rescan partitions.

    After the capacity drop the old backup header lies beyond the disk end;
    the kernel then registers NO partitions at all until this runs.
    """
    result = guest.run(f"sgdisk -e /dev/{disk}", check=True)
    guest.run(f"partprobe /dev/{disk}", check=True)
    return result.output


def zerofree(guest: Guest, device_path: str) -> str:
    result = guest.run(f"zerofree -v {device_path}", timeout=3600, check=True)
    return result.output


def mount_ro(guest: Guest, partition_path: str, mount: str = "/mnt/data") -> None:
    guest.run(f"mkdir -p {mount} && mount -o ro {partition_path} {mount}", check=True)


def unmount(guest: Guest, mount: str = "/mnt/data") -> None:
    guest.run(f"umount {mount} || true")


def poweroff(guest: Guest) -> None:
    # connection drop as the VM powers off is expected
    with contextlib.suppress(GuestError):
        guest.run("poweroff", timeout=10)


def _capture(text: str, pattern: str) -> str:
    m = re.search(pattern, text)
    if not m:
        raise GuestFsError(f"pattern not found: {pattern}")
    return m.group(1)

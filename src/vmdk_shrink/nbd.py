"""Expose a VMDK as a local block device inside the container via ``qemu-nbd``.

This is what replaces the helper VM: the VMDK (split-sparse, in place, no
copies) becomes ``/dev/nbdN`` with partitions, so ``e2fsck``/``resize2fs``/
``sgdisk``/``zerofree`` operate on it exactly as on a real disk.

Two container-specific quirks are handled here:

* the device capacity appears asynchronously after ``-c`` (poll sysfs);
* Docker's ``/dev`` is static — the kernel parses the GPT but creates no
  partition nodes, so we ``mknod`` them ourselves (major 43, minor =
  ``disk_index * minors_per_disk + part_number``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .console import info, step, warn
from .container import Container
from .executor import ExecError

NBD_MAJOR = 43
NBD_DEVICE = "/dev/nbd0"


class NbdError(RuntimeError):
    pass


@dataclass(frozen=True)
class NbdDisk:
    index: int
    minors_per_disk: int

    @property
    def device(self) -> str:
        return f"/dev/nbd{self.index}"

    def partition(self, number: int) -> str:
        return f"/dev/nbd{self.index}p{number}"

    def partition_minor(self, number: int) -> int:
        return self.index * self.minors_per_disk + number


class NbdSession:
    """Owns one qemu-nbd connection inside the container."""

    def __init__(self, container: Container, *, index: int = 0) -> None:
        self._c = container
        self._index = index
        self._connected = False
        self._stride: int | None = None

    # ------------------------------------------------------------ config ----
    @property
    def stride(self) -> int:
        """Minor-number stride per nbd device (derived from ``max_part``)."""
        if self._stride is None:
            try:
                out = self._c.run(
                    "cat /sys/module/nbd/parameters/max_part", timeout=20
                ).output.strip()
                max_part = int(out)
            except (ExecError, ValueError):
                max_part = 15
            stride = 1
            while stride <= max_part:
                stride <<= 1
            self._stride = stride
        return self._stride

    # ----------------------------------------------------------- connect ----
    def connect(
        self, container_vmdk: str, *, read_only: bool = False, unsafe_cache: bool = False
    ) -> NbdDisk:
        """Attach ``container_vmdk`` to ``/dev/nbd<index>`` and return the disk.

        ``unsafe_cache`` adds ``--cache=unsafe`` for read-write work: ext4's
        journal commits sync all the way down through Docker's drvfs layer,
        which throttles relocation to ~10 MB/s; skipping those fsyncs roughly
        doubles throughput. Data still lands in the VM page cache immediately
        (coherent across reconnects); only a VM crash inside the writeback
        window can lose recent blocks, which ``e2fsck -fy`` repairs and the
        final manifest diff would catch.
        """
        self.disconnect(check=False)
        # Docker's /dev is static: the nbd nodes do not exist until we make them.
        self._c.run(
            f"rm -f {self.device} 2>/dev/null || true; "
            f"mknod {self.device} b {NBD_MAJOR} {self._index * self.stride}",
            timeout=30,
        )

        flag = "-r" if read_only else ""
        if unsafe_cache and not read_only:
            flag += " --cache=unsafe"
        step(f"qemu-nbd [val]{'RO' if read_only else 'RW'}[/val] {container_vmdk}")
        # "Permission denied" on an extent is transient: Docker's file-share
        # layer releases handles asynchronously after a disconnect, and
        # Windows Defender's on-open scans hold scan handles on large files.
        # Back off and retry until the race resolves.
        last_err = ""
        for attempt in range(8):
            if attempt:
                delay = min(5 * attempt, 30)
                time.sleep(delay)
                step(f"retrying qemu-nbd connect (attempt {attempt + 1}, after {delay}s)")
            self._c.run_detached(
                f"qemu-nbd {flag} -f vmdk -c {self.device} '{container_vmdk}' "
                f">/tmp/nbd{self._index}.log 2>&1"
            )
            sectors = self._wait_capacity(timeout=30.0)
            if sectors > 0:
                info(f"{self.device} capacity: [val]{sectors * 512 / 2**30:.1f} GiB[/val]")
                self._connected = True
                disk = NbdDisk(index=self._index, minors_per_disk=self.stride)
                self.rescan(disk)
                return disk
            probe = self._c.run(
                f"cat /tmp/nbd{self._index}.log; qemu-nbd -d {self.device} 2>/dev/null || true",
                timeout=30,
                check=False,
            )
            last_err = probe.output.strip()
        raise NbdError(f"nbd device {self.device} never reported a capacity: {last_err}")

    def disconnect(self, *, check: bool = True) -> None:
        # Graceful detach first: NBD_CMD_DISC lets the daemon drain and exit on
        # its own. Killing a daemon that still has in-flight requests hurls a
        # burst of 9p Tflush messages into Docker's drvfs layer — the known
        # trigger for the vp9fs.dll Tflush race (microsoft/WSL#41702) that
        # leaves a mount permanently wedged. The kill is a last resort for a
        # daemon stuck in unwedgeable I/O, and uses a /proc walk because the
        # image has no procps.
        self._c.run(
            f"timeout 30 qemu-nbd -d {self.device} >/dev/null 2>&1 || true",
            timeout=60,
            check=False,
        )
        time.sleep(2)
        self._c.run(
            "for p in /proc/[0-9]*/cmdline; do "
            "c=$(tr '\\0' ' ' < \"$p\" 2>/dev/null); "
            "case \"$c\" in *qemu-nbd*'-c " + self.device + "'*) "
            "d=${p%/cmdline}; kill -9 \"${d#/proc/}\" 2>/dev/null || true;; "
            "esac; done",
            timeout=30,
            check=False,
        )
        self._connected = False

    def __enter__(self) -> NbdSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect(check=False)

    # ------------------------------------------------------- partitions ----
    def rescan(self, disk: NbdDisk, *, number: int = 1) -> str:
        """Re-read the partition table and ensure the node for ``number`` exists."""
        self._c.run(f"partprobe {disk.device} 2>/dev/null || true", timeout=120, check=False)
        time.sleep(1)
        node = disk.partition(number)
        minor = disk.partition_minor(number)
        self._c.run(
            f"rm -f {node} 2>/dev/null || true; mknod {node} b {NBD_MAJOR} {minor}",
            timeout=30,
        )
        return node

    def partition_start(self, disk: NbdDisk, number: int = 1) -> int:
        out = self._c.run(
            f"partx -nr -o START {disk.device} | sed -n '{number}p'", timeout=30
        ).output.strip()
        try:
            return int(out)
        except ValueError as exc:
            raise NbdError(f"cannot read start sector of partition {number}") from exc

    # ----------------------------------------------------------- helpers ----
    @property
    def device(self) -> str:
        return f"/dev/nbd{self._index}"

    def _wait_capacity(self, *, timeout: float = 30.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self._c.run(
                f"cat /sys/block/{self.device.rsplit('/', 1)[-1]}/size 2>/dev/null || echo 0",
                timeout=20,
                check=False,
            ).output.strip()
            try:
                sectors = int(out)
            except ValueError:
                sectors = 0
            if sectors > 0:
                return sectors
            time.sleep(1)
        warn("nbd capacity never appeared")
        return 0

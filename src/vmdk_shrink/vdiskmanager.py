"""Thin typed wrapper around ``vmware-vdiskmanager.exe``."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .console import console
from .paths import DEFAULT_PATHS, HostPaths


class VDiskManagerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompactResult:
    started: float
    finished: float

    @property
    def seconds(self) -> float:
        return self.finished - self.started


class VDiskManager:
    def __init__(self, paths: HostPaths = DEFAULT_PATHS) -> None:
        self._exe = paths.vdiskmanager

    def _run(self, *args: str, timeout: float = 7200) -> str:
        proc = subprocess.run(
            [str(self._exe), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise VDiskManagerError(f"vdiskmanager {' '.join(args)} failed: {out.strip()[-500:]}")
        return out

    def compact(self, vmdk: Path) -> CompactResult:
        """Shrink a sparse VMDK in place by deallocating all-zero grains.

        Prints a live percentage (parsed from the tool's ``\\r`` progress) while running.
        """
        started = time.monotonic()
        proc = subprocess.Popen(
            [str(self._exe), "-k", str(vmdk)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout = proc.stdout
        assert stdout is not None
        last_pct = -1

        def _pump() -> None:
            nonlocal last_pct
            buf = ""
            while True:
                ch = stdout.read(1)
                if not ch:
                    break
                buf += ch
                if ch in "\r\n":
                    m = buf.strip()
                    if m.startswith("Shrink:"):
                        digits = "".join(c for c in m if c.isdigit())
                        if digits:
                            pct = int(digits.split()[0])
                            if pct != last_pct and pct <= 100:
                                last_pct = pct
                                console.print(f"\r[info]compact[/info] {pct:3d}%", end="")
                    buf = ""

        t = threading.Thread(target=_pump, daemon=True)
        t.start()
        rc = proc.wait()
        t.join(timeout=5)
        console.print()
        if rc != 0:
            raise VDiskManagerError(f"compact of {vmdk} failed rc={rc}")
        return CompactResult(started=started, finished=time.monotonic())

    def create_scratch(self, vmdk: Path, size_mb: int = 1024) -> None:
        self._run("-c", "-s", f"{size_mb}MB", "-a", "lsilogic", "-t", "3", str(vmdk), timeout=300)

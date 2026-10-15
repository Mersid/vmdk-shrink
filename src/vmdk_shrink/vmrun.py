"""Thin typed wrapper around ``vmrun.exe``."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .console import ok, step, warn
from .paths import DEFAULT_PATHS, HostPaths


@dataclass(frozen=True)
class RunningVm:
    vmx: Path


class VmrunError(RuntimeError):
    pass


class Vmrun:
    def __init__(self, paths: HostPaths = DEFAULT_PATHS) -> None:
        self._vmrun = paths.vmrun

    def _run(self, *args: str, timeout: float = 300) -> str:
        proc = subprocess.run(
            [str(self._vmrun), "-T", "ws", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise VmrunError(f"vmrun {' '.join(args)} failed ({proc.returncode}): {out.strip()}")
        return out

    def list(self) -> list[RunningVm]:
        out = self._run("list")
        lines = out.strip().splitlines()
        vms: list[RunningVm] = []
        for line in lines[1:]:
            line = line.strip()
            if line:
                vms.append(RunningVm(vmx=Path(line)))
        return vms

    def start(self, vmx: Path, wait_seconds: float = 50) -> None:
        step(f"Starting helper VM [val]{vmx.name}[/val]")
        self._run("start", str(vmx), timeout=180)
        time.sleep(wait_seconds)  # let it reach the boot menu / live env

    def wait_until_stopped(self, vmx: Path, *, timeout: float = 30.0) -> bool:
        """Poll ``vmrun list`` until ``vmx`` is gone.

        A hard stop returns before vmware-vmx releases its disk locks; starting
        another VM that attaches the same disks then fails with error 4104
        ("operation cancelled by the user"). Callers must wait for this.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_running(vmx):
                time.sleep(2)
            else:
                return True
        return False

    def is_running(self, vmx: Path) -> bool:
        target = {str(vmx), str(vmx.resolve())}
        return any(str(vm.vmx) in target for vm in self.list())

    def stop_hard(self, vmx: Path, settle_seconds: float = 6) -> None:
        if not self.is_running(vmx):
            return
        warn(f"Hard-stopping [val]{vmx.name}[/val]")
        self._run("stop", str(vmx), "hard", timeout=300)
        self.wait_until_stopped(vmx, timeout=30.0)
        time.sleep(settle_seconds)

    def stop_soft(self, vmx: Path, timeout: float = 420) -> None:
        self._run("stop", str(vmx), timeout=timeout)
        ok("Helper VM shut down cleanly")

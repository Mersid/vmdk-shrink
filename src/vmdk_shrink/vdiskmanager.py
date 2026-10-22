"""Thin typed wrapper around ``vmware-vdiskmanager.exe``."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

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
        rc_holder: list[int] = []

        def _pump(prog: Progress, tid: TaskID) -> None:
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
                                prog.update(tid, completed=pct)
                    buf = ""

        # Rich Progress drives the in-place redraw: a raw CR inside
        # console.print is emitted literally (Rich counts columns and
        # soft-wraps), so every "compact N%" printed side by side.
        with Progress(
            TextColumn("[info]compact[/info]"),
            BarColumn(style="dim white", complete_style="cyan", finished_style="bright_cyan"),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            # -k emits Shrink: lines ~1/min; rich default 30 s speed window
            # starves (burst samples share a timestamp -> speed None ->
            # ETA flashes in and out). One hour covers the whole run.
            speed_estimate_period=3600,
            console=console,
        ) as progress:
            task = progress.add_task("", total=100)
            t = threading.Thread(target=_pump, args=(progress, task), daemon=True)
            t.start()
            rc_holder.append(proc.wait())
            t.join(timeout=5)
            # The tool's Shrink: counter stalls below 100 long before exit
            # (the table/header-rewrite tail emits nothing), so a completed
            # compact must snap the bar to 100 - a frozen 36% reads as a
            # truncated run.
            progress.update(task, completed=100 if rc_holder[0] == 0 else max(last_pct, 0))
        rc = rc_holder[0]
        if rc != 0:
            raise VDiskManagerError(f"compact of {vmdk} failed rc={rc}")
        return CompactResult(started=started, finished=time.monotonic())

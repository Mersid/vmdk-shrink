"""Thin typed wrapper around ``vmware-vdiskmanager.exe``."""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .execution import stream_process
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

    def compact(
        self,
        vmdk: Path,
        *,
        on_progress: Callable[[int], None] | None = None,
        timeout: float = 21600,
    ) -> CompactResult:
        """Shrink a sparse VMDK in place by deallocating all-zero grains.

        Reports percentages without depending on a terminal or presentation library.
        """
        started = time.monotonic()
        pending = ""

        def report(chunk: str) -> None:
            nonlocal pending
            pending += chunk
            records = re.split(r"[\r\n]", pending)
            pending = records.pop()
            for record in records:
                match = re.search(r"Shrink:\s*(\d+)%", record)
                if match and on_progress is not None:
                    on_progress(min(100, int(match.group(1))))

        try:
            proc = stream_process(
                [str(self._exe), "-k", str(vmdk)], timeout=timeout, chunk_cb=report
            )
        except subprocess.TimeoutExpired as exc:
            raise VDiskManagerError(
                "compaction timed out; preserve the disk and backup for manual recovery"
            ) from exc
        report("\n")
        if proc.returncode != 0:
            raise VDiskManagerError(
                f"compact of {vmdk} failed rc={proc.returncode}: {proc.stdout[-1000:]}"
            )
        if on_progress is not None:
            on_progress(100)
        return CompactResult(started=started, finished=time.monotonic())

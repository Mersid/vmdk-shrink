"""Rich progress bars for long phases (display layer).

One bar per phase; rich's own refresh thread redraws in place while a
sampler thread (for byte-unit phases) feeds fresh counters every few
seconds. The speed estimate window defaults to 120 s: short enough to
track genuine slowdowns, long enough to stay smooth on 5 s samples.
Sources that update slower than the window need a longer period
(`vdiskmanager -k` emits percentages ~1/min and carries its own bar with
speed_estimate_period=3600 for exactly that reason).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from functools import wraps
from types import TracebackType
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .console import console as _console
from .presentation import PhaseMetricsColumn

SECTOR = 512
_POLL_INTERVAL_S = 5.0


def managed_bar(function: Any) -> Any:
    @wraps(function)
    def render(*args: Any, **kwargs: Any) -> Any:
        with PhaseBar() as bar:
            return function(*args, **kwargs, _bar=bar)

    return render


class PhaseBar:
    """Sequential long-phase display: one rich task per phase.

    * ``start_files`` - unit = files hashed; callers bump via
      :meth:`bump_files` from a stream callback (frequent updates).
    * ``start_bytes`` - unit = bytes written; a daemon thread samples
      *sampler* (returns 512-B sectors, or -1 when unreadable) every few
      seconds and converts to bytes. Use for stat[6]-fed phases.
    """

    def __init__(
        self, *, console: Console = _console, poll_interval_s: float = _POLL_INTERVAL_S
    ) -> None:
        self._console = console
        self._progress = Progress(
            TextColumn("[info]{task.description}[/info]"),
            BarColumn(style="dim white", complete_style="cyan", finished_style="bright_cyan"),
            TaskProgressColumn(),
            PhaseMetricsColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            speed_estimate_period=120.0,
            console=console,
        )
        self._task: TaskID | None = None
        self._unit_files = False
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None
        self._total_bytes = 0
        self._poll_interval_s = poll_interval_s
        self._entered = False

    def __enter__(self) -> PhaseBar:
        self._ensure_entered()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Stop the live display. Safe to call more than once."""
        if not self._entered:
            return
        self._stop_ticker()
        self._progress.__exit__(None, None, None)
        self._entered = False

    # ------------------------------------------------------------ phases ----
    def start_files(self, phase: str, total_files: int) -> None:
        """Begin a files-unit phase (manifest hashing)."""
        self._begin(phase, total=total_files, files=True)

    def start_steps(self, phase: str, total_steps: int) -> None:
        self._begin(phase, total=max(total_steps, 1), files=True, unit="steps")

    def start_bytes(self, phase: str, total_bytes: int, sampler: Callable[[], int]) -> None:
        """Begin a bytes-unit phase; *sampler* returns written 512-B sectors."""
        self._begin(phase, total=max(total_bytes, 1), files=False)
        self._total_bytes = max(total_bytes, 1)
        initial = sampler()
        self._start_ticker(lambda: max(0, sampler() - initial) if initial >= 0 else -1)

    def bump_files(self, count: int = 1) -> None:
        if self._task is not None and self._unit_files:
            self._progress.advance(self._task, count)

    def finish(self, *, success: bool = True) -> None:
        """Only successful phases snap to completion; estimates may lag actual work."""
        if self._task is None:
            return
        self._stop_ticker()
        task = self._progress.tasks[self._task]
        if success and task.total is not None:
            self._progress.update(self._task, completed=task.total)
        self._progress.update(self._task, visible=False)
        self._task = None

    # ----------------------------------------------------------- private ----
    def _begin(self, phase: str, *, total: int, files: bool, unit: str | None = None) -> None:
        self._ensure_entered()
        self.finish()  # close any previous phase's task
        self._task = self._progress.add_task(phase, total=total, files=files, unit=unit)
        self._unit_files = files

    def _ensure_entered(self) -> None:
        if not self._entered:
            self._progress.__enter__()
            self._entered = True

    def _start_ticker(self, sampler: Callable[[], int]) -> None:
        self._stop = threading.Event()
        stop = self._stop
        task = self._task
        total = self._total_bytes

        def _tick() -> None:
            while not stop.wait(self._poll_interval_s):
                try:
                    sectors = sampler()
                except Exception:  # noqa: BLE001 - display must never kill a phase
                    continue
                if task is None or stop.is_set() or task != self._task or sectors < 0:
                    continue  # unreadable counter: skip, the watchdog handles stalls
                written = sectors * SECTOR
                self._progress.update(task, completed=min(written, total))

        self._ticker = threading.Thread(target=_tick, daemon=True)
        ticker = self._ticker
        ticker.start()

    def _stop_ticker(self) -> None:
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=10)
            self._ticker = None


def _sleep_unused(seconds: float) -> None:  # pragma: no cover - re-export shim
    time.sleep(seconds)

"""Command executors: the seam between the pipelines and where they run.

Both the Docker container (:mod:`vmdk_shrink.container`) and tests satisfy
:class:`Executor`, so the filesystem/manifest layers never know which one they
are driving.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ExecResult:
    command: str
    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class ExecError(RuntimeError):
    pass


@runtime_checkable
class Executor(Protocol):
    """Something that can run a shell command and stream its stdout."""

    def run(self, command: str, *, timeout: float = 120.0, check: bool = True) -> ExecResult: ...

    def run_detached(self, command: str) -> None: ...

    def stream(
        self,
        command: str,
        *,
        timeout: float = 7200.0,
        chunk_cb: Callable[[str], object],
    ) -> ExecResult: ...

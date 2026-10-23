"""Fail-closed host-side gates, independent of UI and runtime selection."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class SafetyError(RuntimeError):
    pass


def offline_operation(*, directory: bool = False) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            path = args[0] if args else kwargs.get("vm_dir", kwargs.get("vmdk"))
            if not isinstance(path, Path):
                raise TypeError("operation requires a Path")
            vm_dir = Path(path).resolve() if directory else Path(path).resolve().parent
            with operation_lock(vm_dir):
                return function(*args, **kwargs)

        return guarded

    return decorate


def require_offline(vm_dir: Path) -> None:
    locks = sorted(vm_dir.glob("*.lck"))
    if locks:
        raise SafetyError(
            f"VM lock exists: {locks[0].name}; power off the VM and close VMware first. "
            "Locks are never removed automatically."
        )
    if any(vm_dir.glob("*.vmsn")) or any(vm_dir.glob("*.vmss")):
        raise SafetyError("snapshot or suspended VM state found; consolidate/power off first")


@contextmanager
def operation_lock(vm_dir: Path) -> Iterator[None]:
    path = vm_dir / ".vmdk-shrink.lock"
    try:
        stream = path.open("x", encoding="ascii")
    except FileExistsError as exc:
        raise SafetyError(
            f"operation lock exists: {path}; verify no operation is running before "
            "manually removing a stale lock"
        ) from exc
    try:
        with stream:
            stream.write("An operation owns this VM directory. Do not start the VM.\n")
            stream.flush()
            require_offline(vm_dir)
            yield
    finally:
        path.unlink()

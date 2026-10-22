"""Display-layer tests: PhaseBar lifecycle, ticker, and stream-driven bars."""

from __future__ import annotations

import io
import time

from rich.console import Console

from vmdk_shrink.executor import ExecResult
from vmdk_shrink.manifest import collect
from vmdk_shrink.progress import PhaseBar


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


def test_files_bar_lifecycle() -> None:
    bar = PhaseBar(console=_console())
    bar.start_files("hashing", 10)
    for _ in range(10):
        bar.bump_files()
    task = bar._progress.tasks[bar._task]
    assert task.completed == 10 and task.total == 10
    bar.finish()
    assert bar._task is None
    bar.close()


def test_bytes_bar_ticker_feeds_sectors() -> None:
    bar = PhaseBar(console=_console(), poll_interval_s=0.05)
    bar.start_bytes("resize2fs", 1 << 20, lambda: 1024)  # 1024 sectors = 512 KiB
    deadline = time.monotonic() + 2.0
    completed = 0.0
    while time.monotonic() < deadline:
        task = bar._progress.tasks[bar._task]
        completed = task.completed
        if completed > 0:
            break
        time.sleep(0.05)
    assert completed == 1024 * 512
    bar.finish()
    task = bar._progress.tasks[-1]
    assert task.completed == task.total  # snapped to completion
    bar.close()


def test_bytes_bar_ignores_unreadable_counter() -> None:
    bar = PhaseBar(console=_console(), poll_interval_s=0.05)
    bar.start_bytes("zerofree", 1 << 20, lambda: -1)
    time.sleep(0.2)
    task = bar._progress.tasks[bar._task]
    assert task.completed == 0
    bar.close()


class _StreamingExec:
    """Executor whose stream() pushes canned chunks through chunk_cb."""

    def __init__(self, chunks: list[str], run_outputs: dict[str, str]) -> None:
        self._chunks = chunks
        self._run_outputs = run_outputs

    def run(self, command: str, *, timeout: float = 120.0, check: bool = True) -> ExecResult:
        for key, out in self._run_outputs.items():
            if key in command:
                return ExecResult(command=command, exit_code=0, output=out)
        return ExecResult(command=command, exit_code=0, output="")

    def run_detached(self, command: str) -> None:
        return None

    def stream(self, command, *, timeout=7200.0, chunk_cb=None):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            if chunk_cb is not None:
                chunk_cb(chunk)
        return ExecResult(command=command, exit_code=0, output="".join(self._chunks))


def test_collect_drives_files_bar(tmp_path) -> None:
    lines = [f"{'a' * 64}  /mnt/data0/f{i}\n" for i in range(3)]
    ex = _StreamingExec(lines, {"wc -l": "3\n"})
    bar = PhaseBar(console=_console())
    manifest = collect(ex, mount="/mnt/data0", out_path=tmp_path / "m", bar=bar)
    assert len(manifest) == 3
    task = bar._progress.tasks[-1]
    assert task.completed == 3 and task.total == 3
    bar.close()


def test_check_fs_pass_bar_counts_passes() -> None:
    from vmdk_shrink.fsops import check_fs

    chunks = [f"Pass {n}: checking\n" for n in range(1, 6)]
    ex = _StreamingExec(chunks, {})
    bar = PhaseBar(console=_console())
    check_fs(ex, "/dev/nbd0p1", fix=True, bar=bar)
    task = bar._progress.tasks[-1]
    assert task.total == 5 and task.completed == 5
    bar.close()

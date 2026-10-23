from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from vmdk_shrink.container import Container


def test_stream_deadline_without_output() -> None:
    from vmdk_shrink.execution import stream_process

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        stream_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.2)
    assert time.monotonic() - started < 3


def test_stream_collects_output_and_status() -> None:
    from vmdk_shrink.execution import stream_process

    chunks: list[str] = []
    result = stream_process(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'hello\\n'); sys.exit(7)"],
        timeout=5,
        chunk_cb=chunks.append,
    )
    assert result.returncode == 7
    assert result.stdout == "hello\n"
    assert "".join(chunks) == result.stdout


def test_stream_preserves_filename_bytes() -> None:
    from vmdk_shrink.execution import stream_process

    result = stream_process(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\r\\n\\xff\\0')"]
    )
    assert result.stdout.encode("utf-8", "surrogateescape") == b"a\r\n\xff\0"


def test_custom_dockerfile_is_passed_explicitly(monkeypatch, tmp_path: Path) -> None:
    container = Container(())
    calls: list[tuple[str, ...]] = []

    def docker(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(container, "_docker", docker)
    dockerfile = tmp_path / "custom.Dockerfile"
    container.build_image(dockerfile, force=True)
    assert "-f" in calls[-1]
    assert str(dockerfile) in calls[-1]


def test_compaction_adapter_reports_chunked_percentages(monkeypatch, tmp_path: Path) -> None:
    from vmdk_shrink.vdiskmanager import VDiskManager

    def stream(argv, *, timeout, chunk_cb):
        for chunk in ["Shrink: 3", "6%\rShrink: 78%\r", "done"]:
            chunk_cb(chunk)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("vmdk_shrink.vdiskmanager.stream_process", stream)
    percentages: list[int] = []
    VDiskManager().compact(tmp_path / "disk.vmdk", on_progress=percentages.append)
    assert percentages == [36, 78, 100]


def test_compaction_adapter_does_not_report_failed_operation_complete(
    monkeypatch, tmp_path
) -> None:
    from vmdk_shrink.vdiskmanager import VDiskManager, VDiskManagerError

    def stream(argv, *, timeout, chunk_cb):
        chunk_cb("Shrink: 36%\r")
        return subprocess.CompletedProcess(argv, 1, "disk locked", "")

    monkeypatch.setattr("vmdk_shrink.vdiskmanager.stream_process", stream)
    percentages: list[int] = []
    with pytest.raises(VDiskManagerError, match="disk locked"):
        VDiskManager().compact(tmp_path / "disk.vmdk", on_progress=percentages.append)
    assert percentages == [36]


def test_kernel_global_nbd_lock_is_reserved_before_start(monkeypatch) -> None:
    from vmdk_shrink.container import NBD_LOCK_CONTAINER

    container = Container(())
    calls = []
    monkeypatch.setattr(container, "_ensure_nbd_module", lambda: None)

    def docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "lock-id" if args[0] == "create" else "", "")

    monkeypatch.setattr(container, "_docker", docker)
    container.start()
    assert calls[1][:3] == ("create", "--name", NBD_LOCK_CONTAINER)
    assert calls[2][0] == "run"
    container.stop()
    assert calls[-1] == ("rm", "lock-id")


def test_nbd_lock_conflict_does_not_start_or_sweep_containers(monkeypatch) -> None:
    from vmdk_shrink.container import ContainerError

    container = Container(())
    calls = []
    monkeypatch.setattr(container, "_ensure_nbd_module", lambda: None)

    def docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, int(args[0] == "create"), "", "name conflict")

    monkeypatch.setattr(container, "_docker", docker)
    with pytest.raises(ContainerError, match="ownership lock"):
        container.start()
    assert not any(args[0] in ("run", "rm") for args in calls)


def test_watchdog_polls_completion_without_waiting_for_stall_sample(monkeypatch) -> None:
    from vmdk_shrink.executor import ExecResult
    from vmdk_shrink.fsops import run_watched

    sleeps = []
    clock = [0.0]

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    class Executor:
        def run_detached(self, command):
            pass

        def run(self, command, **kwargs):
            return ExecResult(command, 0, "EXIT=0\n")

    monkeypatch.setattr("vmdk_shrink.fsops.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("vmdk_shrink.fsops.time.sleep", sleep)
    assert "EXIT=0" in run_watched(
        Executor(),
        "resize2fs /dev/disposable 1G",
        phase="resize2fs",
        progress=lambda: 0,
        max_runtime_s=3600,
    )
    assert sleeps == [5.0]

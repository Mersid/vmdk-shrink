from __future__ import annotations

import json
import shlex

import pytest

from vmdk_shrink.executor import ExecResult
from vmdk_shrink.fsops import (
    FsError,
    StallError,
    check_fs,
    mount_ro,
    recreate_partition,
    run_watched,
)

TYPE = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
UUID = "11223344-5566-7788-99AA-BBCCDDEEFF00"


class PartitionExecutor:
    def __init__(self) -> None:
        self.table = {
            "label": "gpt",
            "unit": "sectors",
            "firstlba": 34,
            "lastlba": 99999,
            "sectorsize": 512,
            "partitions": [
                {
                    "node": "/dev/nbd0p1",
                    "start": 2048,
                    "size": 80000,
                    "type": TYPE,
                    "uuid": UUID,
                    "name": "data ' $(false)",
                }
            ],
        }
        self.commands: list[str] = []
        self.info = "Attribute flags: 8000000000000001\n"
        self.scan = "Partition table scan:\n  MBR: protective\n  GPT: present\n"

    def run(self, command: str, *, timeout: float = 120, check: bool = True) -> ExecResult:
        self.commands.append(command)
        if "sfdisk" in command:
            return ExecResult(command, 0, json.dumps({"partitiontable": self.table}))
        if "gdisk -l" in command:
            return ExecResult(command, 0, self.scan)
        return ExecResult(command, 0, self.info)


def test_recreate_preserves_all_gpt_metadata_in_one_write() -> None:
    ex = PartitionExecutor()
    recreate_partition(ex, "/dev/nbd0", start_lba=2048, end_lba=50000)
    writes = [c for c in ex.commands if "--delete=" in c]
    assert len(writes) == 1
    args = shlex.split(writes[0])
    for arg in [
        "--new=1:2048:50000",
        f"--typecode=1:{TYPE}",
        f"--partition-guid=1:{UUID}",
        "--change-name=1:data ' $(false)",
        "--attributes=1:=:0x8000000000000001",
    ]:
        assert arg in args


def test_validate_only_checks_metadata_without_writes() -> None:
    ex = PartitionExecutor()
    recreate_partition(ex, "/dev/nbd0", start_lba=2048, end_lba=50000, validate_only=True)
    assert any("gdisk -l" in c for c in ex.commands)
    assert any("sfdisk --json" in c for c in ex.commands)
    assert any("sgdisk --info=" in c for c in ex.commands)
    assert not any("--delete=" in c for c in ex.commands)


@pytest.mark.parametrize("validate_only", [False, True])
@pytest.mark.parametrize("label", ["dos", "hybrid", "unknown"])
def test_rejects_unsupported_tables_without_writes(label: str, validate_only: bool) -> None:
    ex = PartitionExecutor()
    ex.table["label"] = label
    with pytest.raises(FsError):
        recreate_partition(
            ex, "/dev/nbd0", start_lba=2048, end_lba=50000, validate_only=validate_only
        )
    assert not any("--delete=" in c for c in ex.commands)


@pytest.mark.parametrize("validate_only", [False, True])
@pytest.mark.parametrize("start,end", [(0, 50000), (2049, 50000), (2048, 2047), (2048, 90000)])
def test_rejects_bad_geometry_without_writes(start: int, end: int, validate_only: bool) -> None:
    ex = PartitionExecutor()
    with pytest.raises(FsError):
        recreate_partition(
            ex, "/dev/nbd0", start_lba=start, end_lba=end, validate_only=validate_only
        )
    assert not any("--delete=" in c for c in ex.commands)


@pytest.mark.parametrize("validate_only", [False, True])
def test_rejects_missing_attributes_without_writes(validate_only: bool) -> None:
    ex = PartitionExecutor()
    ex.info = "unreadable metadata"
    with pytest.raises(FsError):
        recreate_partition(
            ex, "/dev/nbd0", start_lba=2048, end_lba=50000, validate_only=validate_only
        )
    assert not any("--delete=" in c for c in ex.commands)


@pytest.mark.parametrize("validate_only", [False, True])
def test_rejects_hybrid_gpt_without_writes(validate_only: bool) -> None:
    ex = PartitionExecutor()
    ex.scan = ex.scan.replace("protective", "hybrid")
    with pytest.raises(FsError):
        recreate_partition(
            ex, "/dev/nbd0", start_lba=2048, end_lba=50000, validate_only=validate_only
        )
    assert not any("--delete=" in c for c in ex.commands)


@pytest.mark.parametrize("fix", [False, True])
@pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 8, 16, 32, 128])
def test_check_fs_exit_policy(fix: bool, code: int) -> None:
    class FsckExecutor(PartitionExecutor):
        def run(self, command: str, *, timeout: float = 120, check: bool = True) -> ExecResult:
            self.commands.append(command)
            return ExecResult(command, code, "fsck output")

    ex = FsckExecutor()
    if code == 0 or (fix and code in (1, 2)):
        assert check_fs(ex, "/dev/nbd0p1", fix=fix) == "fsck output"
    else:
        with pytest.raises(FsError, match=f"exit {code}"):
            check_fs(ex, "/dev/nbd0p1", fix=fix)
    assert ex.commands == [f"e2fsck {'-fy' if fix else '-fn'} /dev/nbd0p1"]


def test_readonly_mount_never_replays_journal() -> None:
    ex = PartitionExecutor()
    mount_ro(ex, "/dev/nbd0p1")
    assert "mount -o ro,noload" in ex.commands[-1]


def test_destructive_watchdog_failure_is_not_retryable() -> None:
    class WatchExecutor(PartitionExecutor):
        def run_detached(self, command: str) -> None:
            self.commands.append(command)

    ex = WatchExecutor()
    with pytest.raises(FsError, match="(?i)(interrupt|manual)") as error:
        run_watched(
            ex,
            "resize2fs /dev/nbd0p1 1G",
            phase="resize2fs",
            progress=lambda: 0,
            max_runtime_s=10,
            interval_s=0,
            stall_limit=1,
        )
    assert not isinstance(error.value, StallError)

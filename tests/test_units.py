"""Tests for manifest parsing/diffing, NBD device math, path mapping, fs parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from vmdk_shrink.container import Container, ContainerError
from vmdk_shrink.executor import ExecResult
from vmdk_shrink.fsops import FsFacts, fs_facts, nbd_written, parse_free_ranges
from vmdk_shrink.manifest import diff, load, parse_manifest_text
from vmdk_shrink.nbd import NbdDisk

H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64
H4 = "d" * 64


def write_manifest(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------- manifest ----
def test_parse_manifest_strips_mount_prefix() -> None:
    m = parse_manifest_text(f"{H1}  /mnt/data/etc/hosts\n{H2}  /mnt/data/boot/vmlinuz\n")
    assert m.entries == {"/etc/hosts": H1, "/boot/vmlinuz": H2}


def test_parse_manifest_normalizes_replica_mounts() -> None:
    m = parse_manifest_text(f"{H1}  /mnt/data0/a\n{H2}  /mnt/data1/b\n{H3}  /mnt/data22/c\n")
    assert m.entries == {"/a": H1, "/b": H2, "/c": H3}


def test_parse_manifest_rejects_malformed() -> None:
    from vmdk_shrink.executor import ExecError

    with pytest.raises(ExecError):
        parse_manifest_text("not-a-manifest-line\n")


def test_diff_categories() -> None:
    before = parse_manifest_text(f"{H1}  /same\n{H2}  /changed\n{H3}  /gone\n")
    after = parse_manifest_text(f"{H1}  /same\n{H4}  /changed\n{H4}  /added\n")
    d = diff(before, after)
    assert d.changed == ["/changed"]
    assert d.removed == ["/gone"]
    assert d.added == ["/added"]
    assert not d.identical


def test_diff_identical() -> None:
    m = parse_manifest_text(f"{H1}  /x\n")
    assert diff(m, m).identical


def test_load_roundtrip(tmp_path: Path) -> None:
    p = write_manifest(tmp_path / "m.txt", [f"{H1}  /a", f"{H2}  /b"])
    assert load(p).entries == {"/a": H1, "/b": H2}


# ---------------------------------------------------------------- nbd ----
def test_nbd_disk_partition_nodes() -> None:
    disk = NbdDisk(index=0, minors_per_disk=32)
    assert disk.device == "/dev/nbd0"
    assert disk.partition(1) == "/dev/nbd0p1"
    assert disk.partition_minor(1) == 1


def test_nbd_disk_partition_minors_second_device() -> None:
    disk = NbdDisk(index=1, minors_per_disk=32)
    assert disk.device == "/dev/nbd1"
    assert disk.partition_minor(1) == 33
    assert disk.partition_minor(4) == 36


# ----------------------------------------------------------- container ----
def test_container_path_mapping(tmp_path: Path) -> None:
    vm_dir = tmp_path / "vm"
    (vm_dir / "sub").mkdir(parents=True)
    c = Container(((vm_dir, "/work"),))
    assert c.to_container_path(vm_dir / "disk.vmdk") == "/work/disk.vmdk"
    assert c.to_container_path(vm_dir / "sub" / "x.vmdk") == "/work/sub/x.vmdk"
    with pytest.raises(ContainerError):
        c.to_container_path(tmp_path / "elsewhere.vmdk")


# -------------------------------------------------------------- fsops ----
class FakeExec:
    """Minimal Executor returning canned output."""

    def __init__(self, outputs: dict[str, str]) -> None:
        self._outputs = outputs
        self.commands: list[str] = []

    def run(self, command: str, *, timeout: float = 120.0, check: bool = True) -> ExecResult:
        self.commands.append(command)
        for key, out in self._outputs.items():
            if key in command:
                return ExecResult(command=command, exit_code=0, output=out)
        return ExecResult(command=command, exit_code=0, output="")

    def stream(self, command, *, timeout=7200.0, chunk_cb=None):  # type: ignore[no-untyped-def]
        return self.run(command)


def test_fs_facts_parses_dumpe2fs() -> None:
    out = (
        "Block size:               4096\n"
        "Block count:              262135880\n"
        "Free blocks:              120000000\n"
    )
    ex = FakeExec({"dumpe2fs": out})
    facts = fs_facts(ex, "/dev/nbd0p1")
    assert isinstance(facts, FsFacts)
    assert facts.block_size == 4096
    assert facts.block_count == 262135880
    assert facts.used_gib == pytest.approx((262135880 - 120000000) * 4096 / 2**30)


def test_parse_free_ranges_separates_summary_from_group_lines() -> None:
    text = (
        "Free blocks:              454735\n"
        "  Free blocks: 8615-32767, 33025-33279\n"
        "  Free blocks: 40000-40000\n"
    )
    assert parse_free_ranges(text) == [(8615, 32767), (33025, 33279), (40000, 40000)]


def test_nbd_written_reads_write_sectors_not_inflight() -> None:
    # Field layout (0-indexed): 6 = write sectors, 8 = in_flight. The captured
    # stall-scene line from /sys/block/nbd0/stat: in_flight was 0 while 16 GiB
    # had been written — indexing field 8 would falsely report "no progress".
    # noqa: E501 kept as one literal so the column positions are self-evident
    line = (
        "  260297        0 33316216  2045667   242300  3912648 33501808 36535257"
        "        0  2052736 38583591       16        0 62914568        1        4     2665\n"
    )
    ex = FakeExec({"stat": line})
    assert nbd_written(ex, "/dev/nbd0") == 33501808

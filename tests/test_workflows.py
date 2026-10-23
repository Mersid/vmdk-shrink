from __future__ import annotations

import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from vmdk_shrink import checkpoints
from vmdk_shrink.cli import app
from vmdk_shrink.discovery import select_disk
from vmdk_shrink.manifest import Manifest
from vmdk_shrink.safety import SafetyError, operation_lock


@pytest.fixture
def disk(tmp_path: Path) -> Path:
    descriptor = tmp_path / "data.vmdk"
    descriptor.write_text(
        "# Disk DescriptorFile\nversion=1\nCID=12345678\nparentCID=ffffffff\n"
        'createType="twoGbMaxExtentSparse"\nRW 100 SPARSE "renamed.vmdk"\n',
        encoding="ascii",
    )
    (tmp_path / "renamed.vmdk").write_bytes(b"extent")
    return descriptor


def test_discovery_uses_descriptor_not_extent_filename(disk: Path) -> None:
    assert select_disk(disk.parent) == disk
    other = disk.parent / "other.vmdk"
    other.write_bytes(disk.read_bytes())
    with pytest.raises(ValueError, match="multiple"):
        select_disk(disk.parent)


def test_operation_locks_refuse_vm_locks_and_concurrent_runs(tmp_path: Path) -> None:
    (tmp_path / "vm.vmx.lck").mkdir()
    with pytest.raises(SafetyError, match="VM lock"), operation_lock(tmp_path):
        pytest.fail("must not enter")
    assert not (tmp_path / ".vmdk-shrink.lock").exists()
    (tmp_path / "vm.vmx.lck").rmdir()
    with (
        operation_lock(tmp_path),
        pytest.raises(SafetyError, match="operation lock"),
        operation_lock(tmp_path),
    ):
        pytest.fail("must not enter")


def test_checkpoint_resume_binds_disk_parameters_manifest_and_stage(disk: Path) -> None:
    options = dict(target_gib=1, mode="nbd", hash_via="container")
    checkpoint = checkpoints.prepare(disk, resume=False, **options)
    checkpoint.baseline.write_bytes(b"verified manifest\0")
    checkpoint.baseline_ready(disk)
    assert checkpoints.prepare(disk, resume=True, **options).baseline == checkpoint.baseline
    with pytest.raises(SafetyError, match="parameters"):
        checkpoints.prepare(disk, resume=True, **(options | {"target_gib": 2}))
    checkpoint.baseline.write_bytes(b"changed")
    with pytest.raises(SafetyError, match="manifest"):
        checkpoints.prepare(disk, resume=True, **options)
    checkpoint.baseline.write_bytes(b"verified manifest\0")
    (disk.parent / "renamed.vmdk").write_bytes(b"another extent")
    with pytest.raises(SafetyError, match="identity"):
        checkpoints.prepare(disk, resume=True, **options)
    checkpoint.save("mutation_started")
    with pytest.raises(SafetyError, match="manual recovery"):
        checkpoints.prepare(disk, resume=True, **options)


def test_legacy_manifest_is_not_resume_authority(disk: Path) -> None:
    (disk.parent / "manifest_before.raw").write_text("old baseline")
    with pytest.raises(SafetyError, match="no disk-bound checkpoint"):
        checkpoints.prepare(disk, target_gib=1, mode="nbd", hash_via="container", resume=True)


def test_cli_defaults_do_not_launch_vmware_hashing(disk: Path, monkeypatch) -> None:
    module = importlib.import_module("vmdk_shrink.shrink")
    calls = []
    monkeypatch.setattr(module, "shrink", lambda *a, **kw: calls.append(kw) or 0)
    monkeypatch.setattr("vmdk_shrink.prerequisites.require_prerequisites", lambda *a, **kw: None)
    result = CliRunner().invoke(app, ["shrink", str(disk.parent), "1", "--yes"])
    assert result.exit_code == 0, result.output
    assert calls[0]["mode"] == "nbd"
    assert calls[0]["hash_via"] == "container"
    calls.clear()
    result = CliRunner().invoke(app, ["shrink", str(disk.parent), "1", "--mode", "sidecar", "-y"])
    assert result.exit_code == 0, result.output
    assert calls[0]["hash_via"] == "sidecar"


def test_cli_refusal_is_failure_not_bytes_freed(disk: Path, monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise SafetyError("VM is running")

    monkeypatch.setattr("vmdk_shrink.compact.compact_disk", refuse)
    monkeypatch.setattr("vmdk_shrink.prerequisites.require_prerequisites", lambda *a, **kw: None)
    result = CliRunner().invoke(app, ["compact", str(disk.parent), "--yes"])
    assert result.exit_code == 1
    assert "VM is running" in result.output


def test_cli_validates_transport_before_operation(disk: Path) -> None:
    result = CliRunner().invoke(app, ["shrink", str(disk.parent), "1", "--mode", "oops"])
    assert result.exit_code == 2
    assert not (disk.parent / ".vmdk-shrink").exists()


def test_doctor_inspect_needs_no_programs() -> None:
    result = CliRunner().invoke(app, ["doctor", "inspect", "--json"])
    assert result.exit_code == 0
    assert result.output.strip() == "[]"


@pytest.mark.parametrize("failure", [None, "filesystem", "after"])
def test_shrink_verification_orders_irreversible_cleanup(disk: Path, monkeypatch, failure) -> None:
    module = importlib.import_module("vmdk_shrink.shrink")
    events = []
    checkpoint = SimpleNamespace(
        directory=disk.parent,
        baseline=disk.parent / "before.raw",
        save=lambda stage, **kw: events.append(stage),
        baseline_ready=lambda d: events.append("baseline_ready"),
    )
    nbd = SimpleNamespace(disconnect=lambda: None, rescan=lambda d: None)
    session = SimpleNamespace(executor=SimpleNamespace(run=lambda *a, **kw: None), nbd=nbd)

    @contextmanager
    def open_session(*args, **kwargs):
        yield session

    def hashing(via, vm_dir, data, output, **kwargs):
        events.append(output.name)
        return Manifest({"/file": "bad" if output.name == failure else "good"})

    plan = SimpleNamespace(keep_extents=1, new_capacity_gib=2, orphan_files=[])
    monkeypatch.setattr(module, "validate_writable", lambda p: None)
    monkeypatch.setattr(module, "prepare", lambda *a, **kw: checkpoint)
    monkeypatch.setattr(module.shutil, "disk_usage", lambda p: SimpleNamespace(free=100 * 2**30))
    monkeypatch.setattr(
        module,
        "grain_stats",
        lambda p: SimpleNamespace(
            capacity_bytes=10 * 2**30,
            allocated_bytes=100,
            extent_count=1,
        ),
    )
    monkeypatch.setattr(module, "open_session", open_session)
    monkeypatch.setattr(
        module,
        "attach_ro",
        lambda *a, **kw: SimpleNamespace(
            device="/dev/nbd0",
            partition=lambda n: "/dev/nbd0p1",
        ),
    )
    monkeypatch.setattr(module, "attach_rw", module.attach_ro)
    monkeypatch.setattr(module, "assert_shrinkable_layout", lambda *a: None)
    monkeypatch.setattr(module, "check_fs", lambda *a, **kw: None)
    monkeypatch.setattr(
        module,
        "fs_facts",
        lambda *a: SimpleNamespace(
            size_bytes=10 * 2**30,
            used_gib=0.1,
            block_count=10 * 2**18,
            block_size=4096,
            free_blocks=9 * 2**18,
        ),
    )
    monkeypatch.setattr(module, "partition_geometry", lambda *a: SimpleNamespace(start_lba=2048))
    monkeypatch.setattr(module, "recreate_partition", lambda *a, **kw: None)
    monkeypatch.setattr(module, "extent_allocations", lambda *a: [100])
    monkeypatch.setattr(module, "compute_plan", lambda *a, **kw: plan)
    monkeypatch.setattr(module, "_hash_verification", hashing)
    monkeypatch.setattr(
        module,
        "_shrink_fs_with_retry",
        lambda *a, **kw: (
            events.append("resize") or 2**20,
            None,
        ),
    )
    monkeypatch.setattr(module, "rebuild_descriptor", lambda *a: events.append("rebuild"))
    monkeypatch.setattr(module, "delete_orphans", lambda *a: (events.append("delete") or 0, 0))
    monkeypatch.setattr(module, "relocate_gpt_backup", lambda *a: events.append("gpt"))
    monkeypatch.setattr(
        module,
        "VDiskManager",
        lambda *a: SimpleNamespace(
            compact=lambda *a, **kw: events.append("compact"),
        ),
    )
    result = module.shrink(disk.parent, 1, data_vmdk=disk)
    assert events.index("before") < events.index("mutation_started") < events.index("resize")
    if failure:
        assert result == 2
        assert "delete" not in events
        if failure == "filesystem":
            assert "rebuild" not in events
            assert "compact" not in events
    else:
        assert result == 0
        assert events.index("filesystem") < events.index("rebuild")
        assert events.index("compact") < events.index("after") < events.index("delete")
        assert events[-1] == "complete"

"""Offline regressions for sidecar lifecycle and image preparation safety."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from vmdk_shrink.executor import ExecResult
from vmdk_shrink.fsops import FsError
from vmdk_shrink.nbd import NbdDisk, NbdError
from vmdk_shrink.sidecar import discover, hashing, image, orchestrator, seed, ssh
from vmdk_shrink.sidecar.orchestrator import RealVmrun, SidecarError


@pytest.mark.parametrize("output", ["", "error: unavailable", "Total running VMs: 1\n"])
def test_running_check_rejects_malformed_list(monkeypatch, tmp_path, output):
    vmrun = RealVmrun()
    monkeypatch.setattr(vmrun, "_run", lambda *a, **kw: ExecResult("list", 0, output))
    with pytest.raises(SidecarError):
        vmrun.is_running(tmp_path / "sidecar.vmx")


def test_running_check_matches_normalized_paths(monkeypatch, tmp_path):
    vmx = tmp_path / "sidecar.vmx"
    vmrun = RealVmrun()
    output = f"Total running VMs: 1\n{str(vmx).replace(chr(92), '/').upper()}\n"
    monkeypatch.setattr(vmrun, "_run", lambda *a, **kw: ExecResult("list", 0, output))
    assert vmrun.is_running(vmx)


def test_hash_shutdown_verifies_hard_stop(monkeypatch, tmp_path):
    vmrun = Mock()
    vmrun.stop.side_effect = [SidecarError("no tools"), None]
    vmrun.is_running.return_value = True
    monkeypatch.setattr(orchestrator, "POWER_OFF_TIMEOUT_S", 0)
    with pytest.raises(SidecarError, match="power off"):
        hashing._shutdown(vmrun, tmp_path / "sidecar.vmx")


def test_hash_checks_running_before_artifacts(monkeypatch, tmp_path):
    vmrun = Mock()
    vmrun.is_running.return_value = True
    build = Mock()
    monkeypatch.setattr(hashing, "RealVmrun", lambda paths: vmrun)
    monkeypatch.setattr(hashing, "_ensure_artifacts", build)
    with pytest.raises(SidecarError, match="already running"):
        hashing.hash_via_sidecar(
            tmp_path,
            tmp_path / "data.vmdk",
            out_path=tmp_path / "manifest",
            fs_uuid="uuid",
            expected_partition_bytes=1024,
        )
    build.assert_not_called()


def test_vmnet8_uses_netmask(monkeypatch):
    result = Mock(
        stdout=(
            "Ethernet adapter VMware Network Adapter VMnet8:\n"
            "   IPv4 Address. . . . . . . . . . . : 192.168.10.1\n"
            "   Subnet Mask . . . . . . . . . . . : 255.255.254.0\n"
        ),
        returncode=0,
    )
    monkeypatch.setattr(discover.subprocess, "run", lambda *a, **kw: result)
    assert discover.vmnet8_subnet() == "192.168.10.0/23"


def test_seed_does_not_install_parted():
    assert "  - parted\n" not in seed.user_data("ssh-ed25519 key")


@pytest.mark.parametrize("package", ["fdisk", "e2fsprogs", "util-linux", "coreutils"])
def test_seed_explicit_filesystem_dependencies(package):
    assert f"  - {package}\n" in seed.user_data("ssh-ed25519 key")


def test_vmx_without_iso_has_no_connected_cdrom(tmp_path):
    from vmdk_shrink.sidecar.vmx import SidecarVmxSpec

    text = SidecarVmxSpec(tmp_path / "boot.vmdk", tmp_path / "data.vmdk").render()
    assert 'ide1:0.present = "FALSE"' in text
    assert "ide1:0.fileName" not in text
    assert "ide1:0.startConnected" not in text


def test_failed_download_cleans_own_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(image, "_resolve_image", lambda: ("test.qcow2", "a" * 128))

    def fail(url, dest):
        dest.write_bytes(b"partial")
        raise OSError("network lost")

    monkeypatch.setattr(image, "_download_streaming", fail)
    with pytest.raises(OSError, match="network lost"):
        image.ensure_base_image(tmp_path, Mock())
    assert list(tmp_path.iterdir()) == []


def test_failed_prep_does_not_publish_conversion(monkeypatch, tmp_path):
    digest = "a" * 128
    monkeypatch.setattr(image, "_resolve_image", lambda: ("test.qcow2", digest))
    monkeypatch.setattr(image, "_verified", lambda *a: None)
    (tmp_path / "test.qcow2").write_bytes(b"cached")

    def convert(command, **kwargs):
        import shlex

        output = shlex.split(command)[-1]
        (tmp_path / Path(output).name).write_bytes(b"vmdk")
        return ExecResult(command, 0, "")

    def fail(*a, **kw):
        raise image.ImageError("prep failed")

    monkeypatch.setattr(image, "_force_nocloud", fail)
    with pytest.raises(image.ImageError, match="prep failed"):
        image.ensure_base_image(tmp_path, Mock(run=convert))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["test.qcow2"]


@pytest.mark.parametrize("partial", [False, True])
def test_ssh_stream_deadline_without_newlines(monkeypatch, partial):
    monkeypatch.setattr(ssh, "require_ssh_client", lambda: sys.executable)
    runner = ssh.SshRunner(ssh.SshConfig(host="unused"))
    script = "import time; time.sleep(20)"
    if partial:
        script = "import sys; sys.stdout.write('partial'); sys.stdout.flush(); " + script
    monkeypatch.setattr(runner, "_argv", lambda cmd: [sys.executable, "-c", script])
    start = time.monotonic()
    with pytest.raises(ssh.SshError, match="timed out") as exc:
        runner.stream("hash", timeout=0.2)
    assert isinstance(exc.value.__cause__, subprocess.TimeoutExpired)
    assert time.monotonic() - start < 5


def test_ssh_stream_keeps_returncode_nuls_and_raw_filename_bytes(monkeypatch):
    monkeypatch.setattr(ssh, "require_ssh_client", lambda: sys.executable)
    runner = ssh.SshRunner(ssh.SshConfig(host="unused"))
    script = "import os, sys; os.write(1, b'file\\xff\\x00'); os.write(2, b'err'); sys.exit(7)"
    monkeypatch.setattr(runner, "_argv", lambda cmd: [sys.executable, "-c", script])
    chunks = []
    result = runner.stream("hash", timeout=5, chunk_cb=chunks.append)
    assert result.exit_code == 7
    assert result.output.encode("utf-8", "surrogateescape") == b"file\xff\x00err"
    assert "".join(chunks) == result.output


def test_ssh_run_preserves_raw_filename_bytes(monkeypatch):
    monkeypatch.setattr(ssh, "require_ssh_client", lambda: sys.executable)
    runner = ssh.SshRunner(ssh.SshConfig(host="unused"))
    monkeypatch.setattr(
        runner,
        "_argv",
        lambda cmd: [
            sys.executable,
            "-c",
            "import os; os.write(1, b'file\\xff\\x00')",
        ],
    )
    assert runner.run("hash").output.encode("utf-8", "surrogateescape") == b"file\xff\x00"


def test_image_prep_rejects_concurrent_owner_without_deleting_files(tmp_path):
    lock = tmp_path / ".image-prep.lock"
    lock.mkdir()
    unrelated = tmp_path / "other.part"
    unrelated.write_bytes(b"other operation")
    with pytest.raises(image.ImageError, match="already active"):
        image.ensure_base_image(tmp_path, Mock())
    assert lock.is_dir()
    assert unrelated.read_bytes() == b"other operation"


def test_nocloud_uses_nbd_helpers_and_cleans_up_on_failure(monkeypatch):
    nbd = Mock()
    nbd.connect.return_value = NbdDisk(index=1, minors_per_disk=16)
    monkeypatch.setattr(image, "NbdSession", lambda ex, index: nbd)
    executor = Mock()
    executor.run.side_effect = [ExecResult("prep", 1, "copy failed"), ExecResult("cleanup", 0, "")]
    with pytest.raises(image.ImageError, match="copy failed"):
        image._force_nocloud(executor, "/cache", "boot.vmdk", seed_dir=Path(".seed"))
    nbd.connect.assert_called_once_with("/cache/boot.vmdk")
    nbd.disconnect.assert_called_once_with(check=False)
    script = executor.run.call_args_list[0].args[0]
    assert "mount -o rw /dev/nbd1p1 /tmp/sidecar-image-" in script
    assert "mknod" not in script
    assert "/cache/.seed/user-data" in script
    assert "umount" in executor.run.call_args_list[1].args[0]


def test_nocloud_does_not_disconnect_mounted_image(monkeypatch):
    nbd = Mock()
    nbd.connect.return_value = NbdDisk(index=1, minors_per_disk=16)
    monkeypatch.setattr(image, "NbdSession", lambda ex, index: nbd)
    executor = Mock()
    executor.run.side_effect = [ExecResult("prep", 0, ""), ExecResult("cleanup", 1, "busy")]
    with pytest.raises(image.ImageCleanupError, match="unmount failed"):
        image._force_nocloud(executor, "/work", "boot.vmdk")
    nbd.disconnect.assert_not_called()


def test_hash_shutdown_uses_guest_without_vmware_tools(monkeypatch, tmp_path):
    vmrun = Mock()
    vmrun.is_running.side_effect = [True, False]
    runner = Mock()
    hashing._shutdown(vmrun, tmp_path / "sidecar.vmx", runner)
    runner.run.assert_called_once_with("systemctl poweroff --no-wall", timeout=60, check=False)
    vmrun.stop.assert_not_called()


def test_running_check_propagates_vmrun_error(monkeypatch, tmp_path):
    vmrun = RealVmrun()
    monkeypatch.setattr(vmrun, "_run", Mock(side_effect=SidecarError("unavailable")))
    with pytest.raises(SidecarError, match="unavailable"):
        vmrun.is_running(tmp_path / "sidecar.vmx")


def test_vmnet8_missing_mask_is_fatal(monkeypatch):
    result = Mock(
        stdout=("Ethernet adapter VMware Network Adapter VMnet8:\n   IPv4 Address: 192.168.10.1\n"),
        returncode=0,
    )
    monkeypatch.setattr(discover.subprocess, "run", lambda *a, **kw: result)
    with pytest.raises(Exception, match="netmask"):
        discover.vmnet8_subnet()


def test_partition_recreation_delegates_to_shared_fsops(monkeypatch, tmp_path):
    plan = orchestrator.SidecarPlan(
        data_vmdk=tmp_path / "data.vmdk",
        target_gib=1,
        start_lba=2048,
        expected_partition_bytes=2**31,
        fs_uuid="uuid",
        block_size=4096,
        sidecar_dir=tmp_path,
        vm_dir=tmp_path,
    )
    operation = orchestrator.SidecarOrchestrator(plan)
    operation._state.done = ["resize", "free-map"]
    runner = Mock()
    runner.run.return_value = ExecResult("fsck", 0, "")
    recreate = Mock()
    relocate = Mock()
    monkeypatch.setattr(orchestrator, "recreate_partition", recreate)
    monkeypatch.setattr(orchestrator, "relocate_gpt_backup", relocate)
    for name in ["require_unmounted", "assert_block_count", "assert_partition_layout"]:
        monkeypatch.setattr(orchestrator.gates, name, Mock())
    operation._run_fs_phases(runner, "/dev/sdb1", "/dev/sdb")
    assert recreate.call_args_list == [
        ((runner, "/dev/sdb"), dict(start_lba=2048, end_lba=2099199, validate_only=True)),
        ((runner, "/dev/sdb"), dict(start_lba=2048, end_lba=2099199)),
    ]
    relocate.assert_called_once_with(runner, "/dev/sdb")
    assert "partition" in operation._state.done


def test_image_cleanup_uncertainty_retains_pending_image_and_lock(monkeypatch, tmp_path):
    digest = "b" * 128
    monkeypatch.setattr(image, "_resolve_image", lambda: ("test.qcow2", digest))
    monkeypatch.setattr(image, "_verified", lambda *a: None)
    (tmp_path / "test.qcow2").write_bytes(b"cached")

    def convert(command, **kwargs):
        import shlex

        pending = tmp_path / Path(shlex.split(command)[-1]).name
        pending.write_bytes(b"possibly mounted")
        return ExecResult(command, 0, "")

    def fail(*a, **kw):
        raise image.ImageCleanupError("busy")

    monkeypatch.setattr(image, "_force_nocloud", fail)
    with pytest.raises(image.ImageCleanupError):
        image.ensure_base_image(tmp_path, Mock(run=convert))
    assert (tmp_path / ".image-prep.lock").is_dir()
    pending = list(tmp_path.glob("*.part.vmdk"))
    assert len(pending) == 1
    assert pending[0].read_bytes() == b"possibly mounted"
    assert not (tmp_path / f"sidecar-base-{digest[:12]}.vmdk").exists()


def test_cached_image_is_prepared_under_lock(monkeypatch, tmp_path):
    digest = "c" * 128
    monkeypatch.setattr(image, "_resolve_image", lambda: ("test.qcow2", digest))
    monkeypatch.setattr(image, "_verified", lambda *a: None)
    (tmp_path / "test.qcow2").write_bytes(b"cached")
    vmdk = tmp_path / f"sidecar-base-{digest[:12]}.vmdk"
    vmdk.write_bytes(b"cached conversion")

    def prepare(*a, **kw):
        assert (tmp_path / ".image-prep.lock").is_dir()

    prep = Mock(side_effect=prepare)
    monkeypatch.setattr(image, "_force_nocloud", prep)
    executor = Mock()
    result = image.ensure_base_image(tmp_path, executor, seed_dir=tmp_path / ".seed")
    assert result.vmdk == vmdk
    executor.run.assert_not_called()
    prep.assert_called_once_with(executor, "/work", vmdk.name, seed_dir=tmp_path / ".seed")
    assert not (tmp_path / ".image-prep.lock").exists()


def _operation(tmp_path):
    return orchestrator.SidecarOrchestrator(
        orchestrator.SidecarPlan(
            data_vmdk=tmp_path / "data.vmdk",
            target_gib=1,
            start_lba=2048,
            expected_partition_bytes=2**31,
            fs_uuid="uuid",
            block_size=4096,
            sidecar_dir=tmp_path / ".sidecar",
            vm_dir=tmp_path,
        )
    )


def _invoke(api, tmp_path):
    if api == "shrink":
        return _operation(tmp_path).run()
    return hashing.hash_via_sidecar(
        tmp_path,
        tmp_path / "data.vmdk",
        out_path=tmp_path / "manifest",
        fs_uuid="uuid",
        expected_partition_bytes=2**31,
    )


@pytest.fixture
def fs_phases(monkeypatch, tmp_path):
    operation = _operation(tmp_path)
    operation.plan.sidecar_dir.mkdir()
    operation._state.done = ["resize", "partition", "free-map"]
    for name in ["require_unmounted", "assert_block_count", "assert_partition_layout"]:
        monkeypatch.setattr(orchestrator.gates, name, Mock())
    preflight = Mock()
    monkeypatch.setattr(orchestrator, "recreate_partition", preflight)
    monkeypatch.setattr(orchestrator, "relocate_gpt_backup", Mock())
    monkeypatch.setattr(orchestrator, "run_detached_and_watch", Mock())
    runner = Mock()
    runner.run.return_value = ExecResult("fsck", 0, "")
    return operation, runner, preflight


@pytest.mark.parametrize("code", [0, 1, 2])
def test_fsck_accepts_corrected_exits_without_stream(fs_phases, code):
    operation, runner, preflight = fs_phases
    runner.run.return_value = ExecResult("fsck", code, "corrected")
    operation._run_fs_phases(runner, "/dev/sdb1", "/dev/sdb", bar=Mock())
    checks = [call for call in runner.run.call_args_list if "e2fsck" in call.args[0]]
    assert len(checks) == 2
    assert all(call.kwargs["check"] is False for call in checks)
    runner.stream.assert_not_called()
    preflight.assert_called_once_with(
        runner, "/dev/sdb", start_lba=2048, end_lba=2099199, validate_only=True
    )


@pytest.mark.parametrize("code", [4, 8, 16, 32, 128])
@pytest.mark.parametrize("post_resize", [False, True])
def test_fsck_fatal_exits_stop_phases(fs_phases, code, post_resize):
    operation, runner, preflight = fs_phases
    runner.run.side_effect = (
        [ExecResult("fsck", 0, ""), ExecResult("fsck", code, "fatal")]
        if post_resize
        else [ExecResult("fsck", code, "fatal")]
    )
    with pytest.raises(FsError, match="e2fsck failed"):
        operation._run_fs_phases(runner, "/dev/sdb1", "/dev/sdb")
    assert preflight.call_count == 1


def test_partition_preflight_precedes_any_fs_mutation(fs_phases):
    operation, runner, preflight = fs_phases
    operation._state.done = []
    preflight.side_effect = FsError("unsupported partition metadata")
    with pytest.raises(FsError, match="unsupported"):
        operation._run_fs_phases(runner, "/dev/sdb1", "/dev/sdb")
    runner.run.assert_not_called()
    orchestrator.run_detached_and_watch.assert_not_called()


def test_partition_gate_failure_prevents_free_map_and_checkpoint(fs_phases):
    operation, runner, preflight = fs_phases
    operation._state.done = ["resize"]
    orchestrator.gates.assert_partition_layout.side_effect = orchestrator.GateError("stale")
    with pytest.raises(orchestrator.GateError, match="stale"):
        operation._run_fs_phases(runner, "/dev/sdb1", "/dev/sdb")
    assert preflight.call_count == 2
    assert "partition" not in operation._state.done
    assert "free-map" not in operation._state.done
    assert all("e2fsck" in call.args[0] for call in runner.run.call_args_list)


def test_partition_recreation_and_reread_precede_free_map(fs_phases, monkeypatch):
    operation, runner, preflight = fs_phases
    operation._state.done = ["resize"]
    calls = Mock()
    calls.attach_mock(preflight, "partition")
    calls.attach_mock(runner.run, "run")
    relocate = Mock()
    monkeypatch.setattr(orchestrator, "relocate_gpt_backup", relocate)
    calls.attach_mock(relocate, "relocate")
    calls.attach_mock(orchestrator.gates.assert_partition_layout, "layout")
    monkeypatch.setattr(orchestrator, "parse_free_ranges", lambda output: [(10, 20)])
    operation._run_fs_phases(runner, "/dev/sdb1", "/dev/sdb")
    assert [call[0] for call in calls.mock_calls] == [
        "partition",
        "run",
        "run",
        "partition",
        "relocate",
        "layout",
        "run",
        "run",
    ]
    assert operation._state.free_ranges == [[10, 20]]


@pytest.mark.parametrize("error", [ssh.SshError("transport"), FsError("map"), ValueError("parse")])
def test_free_map_expected_failures_use_zerofree(fs_phases, monkeypatch, error):
    operation, runner, _ = fs_phases
    operation._state.done.remove("free-map")
    monkeypatch.setattr(orchestrator, "parse_free_ranges", Mock(side_effect=error))
    fallback = Mock()
    monkeypatch.setattr(operation, "_run_zerofree", fallback)
    operation._run_fs_phases(runner, "/dev/sdb1", "/dev/sdb")
    fallback.assert_called_once()
    assert "free-map" in operation._state.done


@pytest.mark.parametrize("error", [TypeError("bug"), AttributeError("bug"), AssertionError("bug")])
def test_free_map_programming_faults_propagate(fs_phases, monkeypatch, error):
    operation, runner, _ = fs_phases
    operation._state.done.remove("free-map")
    monkeypatch.setattr(orchestrator, "parse_free_ranges", Mock(side_effect=error))
    fallback = Mock()
    monkeypatch.setattr(operation, "_run_zerofree", fallback)
    with pytest.raises(type(error), match="bug"):
        operation._run_fs_phases(runner, "/dev/sdb1", "/dev/sdb")
    fallback.assert_not_called()
    assert "free-map" not in operation._state.done


@pytest.mark.parametrize("kernel", ["2048\n4096\n", "4096\n2097152\n", "", "nonsense"])
def test_partition_gate_rejects_stale_or_unreadable_kernel_geometry(kernel):
    runner = Mock()
    runner.run.side_effect = [
        ExecResult("reread", 0, ""),
        ExecResult("sgdisk", 0, "1 2048 2099199\n"),
        ExecResult("kernel", 0, kernel),
    ]
    with pytest.raises(orchestrator.GateError):
        orchestrator.gates.assert_partition_layout(runner, "/dev/sdb", 2048, 2099199)


def test_partition_gate_reread_failure_stops_before_partition_access():
    runner = Mock()
    runner.run.side_effect = ssh.SshError("reread busy")
    with pytest.raises(ssh.SshError, match="reread busy"):
        orchestrator.gates.assert_partition_layout(runner, "/dev/sdb", 2048, 2099199)
    runner.run.assert_called_once_with("blockdev --rereadpt '/dev/sdb'", timeout=60)


@pytest.mark.parametrize("api", ["shrink", "hash"])
def test_lifecycle_existing_lock_blocks_before_running_check(monkeypatch, tmp_path, api):
    lock = tmp_path / ".sidecar" / ".lifecycle.lock"
    lock.mkdir(parents=True)
    vmrun = Mock()
    monkeypatch.setattr(orchestrator, "RealVmrun", lambda paths: vmrun)
    monkeypatch.setattr(hashing, "RealVmrun", lambda paths: vmrun)
    with pytest.raises(SidecarError, match="manual"):
        _invoke(api, tmp_path)
    vmrun.is_running.assert_not_called()
    assert lock.is_dir()


@pytest.mark.parametrize("owner", ["shrink", "hash"])
@pytest.mark.parametrize("contender", ["shrink", "hash"])
def test_lifecycle_serializes_direct_calls_during_build(monkeypatch, tmp_path, owner, contender):
    vmrun = Mock()
    vmrun.is_running.return_value = False
    monkeypatch.setattr(orchestrator, "RealVmrun", lambda paths: vmrun)
    monkeypatch.setattr(hashing, "RealVmrun", lambda paths: vmrun)
    monkeypatch.setattr(orchestrator.gates, "require_no_locks", Mock())
    monkeypatch.setattr(orchestrator.gates, "require_free_space", Mock())
    lock = tmp_path / ".sidecar" / ".lifecycle.lock"

    def build(*args):
        assert lock.is_dir()
        with pytest.raises(SidecarError, match="manual"):
            _invoke(contender, tmp_path)
        raise ValueError("preparation failed")

    monkeypatch.setattr(orchestrator.SidecarOrchestrator, "_build_artifacts", build)
    monkeypatch.setattr(hashing, "_ensure_artifacts", build)
    with pytest.raises(ValueError, match="preparation failed"):
        _invoke(owner, tmp_path)
    assert not lock.exists()
    vmrun.start.assert_not_called()


@pytest.mark.parametrize("api", ["shrink", "hash"])
@pytest.mark.parametrize("status", [True, SidecarError("cannot list VMs")])
def test_lifecycle_retains_lock_when_poweroff_is_uncertain(monkeypatch, tmp_path, api, status):
    vmrun = Mock()
    if isinstance(status, Exception):
        vmrun.is_running.side_effect = status
    else:
        vmrun.is_running.return_value = status
    monkeypatch.setattr(orchestrator, "RealVmrun", lambda paths: vmrun)
    monkeypatch.setattr(hashing, "RealVmrun", lambda paths: vmrun)
    monkeypatch.setattr(orchestrator.gates, "require_no_locks", Mock())
    monkeypatch.setattr(orchestrator.gates, "require_free_space", Mock())
    with pytest.raises((SidecarError, orchestrator.GateError)):
        _invoke(api, tmp_path)
    assert (tmp_path / ".sidecar" / ".lifecycle.lock").is_dir()
    vmrun.stop.assert_not_called()


def test_interrupted_mutation_is_not_automatically_retried(monkeypatch, tmp_path):
    operation = _operation(tmp_path)
    operation.plan.sidecar_dir.mkdir()
    orchestrator._State(done=["image", "seed", "vmx", "resize"]).save(operation.state_path)
    operation.vmrun = Mock(is_running=Mock(return_value=False))
    build = Mock()
    boot = Mock()
    monkeypatch.setattr(operation, "_build_artifacts", build)
    monkeypatch.setattr(operation, "_boot_and_operate", boot)
    monkeypatch.setattr(orchestrator.gates, "require_no_locks", Mock())
    monkeypatch.setattr(orchestrator.gates, "require_free_space", Mock())
    with pytest.raises(SidecarError, match="manual"):
        operation.run()
    build.assert_not_called()
    boot.assert_not_called()


def test_image_disconnect_failure_retains_image_and_prep_lock(monkeypatch, tmp_path):
    digest = "d" * 128
    monkeypatch.setattr(image, "_resolve_image", lambda: ("test.qcow2", digest))
    monkeypatch.setattr(image, "_verified", lambda *a: None)
    (tmp_path / "test.qcow2").write_bytes(b"cached")
    (tmp_path / f"sidecar-base-{digest[:12]}.vmdk").write_bytes(b"boot")
    nbd = Mock()
    nbd.connect.return_value = NbdDisk(index=1, minors_per_disk=16)
    nbd.disconnect.side_effect = NbdError("flush/detach failed")
    monkeypatch.setattr(image, "NbdSession", lambda ex, index: nbd)
    executor = Mock()
    executor.run.return_value = ExecResult("prep", 0, "")
    with pytest.raises(image.ImageCleanupError, match="disconnect failed"):
        image.ensure_base_image(tmp_path, executor)
    assert (tmp_path / ".image-prep.lock").is_dir()


@pytest.mark.parametrize("api", ["shrink", "hash"])
@pytest.mark.parametrize("partial_start", [False, True])
@pytest.mark.parametrize("shutdown_fails", [False, True])
def test_lifecycle_covers_start_and_shutdown(
    monkeypatch, tmp_path, api, partial_start, shutdown_fails
):
    lock = tmp_path / ".sidecar" / ".lifecycle.lock"
    running = False
    vmrun = Mock()
    vmrun.is_running.side_effect = lambda vmx: running
    monkeypatch.setattr(orchestrator, "RealVmrun", lambda paths: vmrun)
    monkeypatch.setattr(hashing, "RealVmrun", lambda paths: vmrun)
    monkeypatch.setattr(orchestrator.gates, "require_no_locks", Mock())
    monkeypatch.setattr(orchestrator.gates, "require_free_space", Mock())
    monkeypatch.setattr(orchestrator.SidecarOrchestrator, "_build_artifacts", lambda self: None)
    monkeypatch.setattr(hashing, "_ensure_artifacts", lambda *args: None)

    def start(vmx):
        nonlocal running
        assert lock.is_dir()
        running = True
        if partial_start:
            raise SidecarError("start timed out after launching")

    def discover_ip():
        raise SidecarError("discovery failed")

    def shutdown(*args):
        nonlocal running
        assert lock.is_dir()
        for contender in ["shrink", "hash"]:
            with pytest.raises(SidecarError, match="manual"):
                _invoke(contender, tmp_path)
        if shutdown_fails:
            raise SidecarError("poweroff unverified")
        running = False

    vmrun.start.side_effect = start
    stop = Mock(side_effect=shutdown)
    monkeypatch.setattr(orchestrator, "vmnet8_subnet", discover_ip)
    monkeypatch.setattr(hashing, "vmnet8_subnet", discover_ip)
    monkeypatch.setattr(orchestrator, "shutdown_sidecar", stop)
    monkeypatch.setattr(hashing, "shutdown_sidecar", stop)
    with pytest.raises(SidecarError, match="start timed out|discovery failed") as exc:
        _invoke(api, tmp_path)
    vmrun.start.assert_called_once()
    stop.assert_called_once()
    assert lock.exists() == shutdown_fails
    if shutdown_fails:
        assert any("manual recovery" in note for note in exc.value.__notes__)


def test_completed_cycle_is_loaded_under_lock_without_repeating_mutation(monkeypatch, tmp_path):
    first = _operation(tmp_path)
    second = _operation(tmp_path)
    for operation in [first, second]:
        operation.vmrun = Mock(is_running=Mock(return_value=False))
    monkeypatch.setattr(orchestrator.gates, "require_no_locks", Mock())
    monkeypatch.setattr(orchestrator.gates, "require_free_space", Mock())
    monkeypatch.setattr(first, "_build_artifacts", Mock())
    lock = first.plan.sidecar_dir / ".lifecycle.lock"

    def operate(*args, **kwargs):
        assert lock.is_dir()
        first._state.fs_end_sector = 1234
        first._state.data_partition = "/dev/sdb1"
        for phase in ["image", "seed", "vmx", "mutation-started", "done"]:
            first._mark(phase)

    monkeypatch.setattr(first, "_boot_and_operate", operate)
    build = Mock()
    boot = Mock()
    monkeypatch.setattr(second, "_build_artifacts", build)
    monkeypatch.setattr(second, "_boot_and_operate", boot)
    assert first.run() == second.run()
    build.assert_not_called()
    boot.assert_not_called()
    assert not lock.exists()


@pytest.mark.parametrize("api", ["shrink", "hash"])
def test_lifecycle_retains_lock_for_uncertain_image_cleanup(monkeypatch, tmp_path, api):
    vmrun = Mock(is_running=Mock(return_value=False))
    monkeypatch.setattr(orchestrator, "RealVmrun", lambda paths: vmrun)
    monkeypatch.setattr(hashing, "RealVmrun", lambda paths: vmrun)
    monkeypatch.setattr(orchestrator.gates, "require_no_locks", Mock())
    monkeypatch.setattr(orchestrator.gates, "require_free_space", Mock())

    def build(*args):
        (tmp_path / ".sidecar" / ".image-prep.lock").mkdir()
        raise image.ImageCleanupError("image may be mounted")

    monkeypatch.setattr(orchestrator.SidecarOrchestrator, "_build_artifacts", build)
    monkeypatch.setattr(hashing, "_ensure_artifacts", build)
    with pytest.raises(image.ImageCleanupError, match="may be mounted"):
        _invoke(api, tmp_path)
    assert (tmp_path / ".sidecar" / ".lifecycle.lock").is_dir()


@pytest.mark.parametrize("mutation_fails", [False, True])
def test_mutation_checkpoint_prevents_retry_after_failure(monkeypatch, tmp_path, mutation_fails):
    operation = _operation(tmp_path)
    running = False
    vmrun = Mock()
    vmrun.is_running.side_effect = lambda vmx: running
    operation.vmrun = vmrun
    monkeypatch.setattr(orchestrator.gates, "require_no_locks", Mock())
    monkeypatch.setattr(orchestrator.gates, "require_free_space", Mock())
    monkeypatch.setattr(orchestrator.gates, "wait_ssh_ready", Mock())
    monkeypatch.setattr(orchestrator.gates, "identify_data_disk", lambda *a: "/dev/sdb1")
    monkeypatch.setattr(orchestrator, "vmnet8_subnet", lambda: "192.168.1.0/24")
    monkeypatch.setattr(orchestrator, "wait_for_guest_ip", lambda *a, **kw: "192.168.1.2")
    runner = Mock()
    runner.run.return_value = ExecResult("cloud-init", 0, "")
    monkeypatch.setattr(orchestrator, "SshRunner", lambda config: runner)
    monkeypatch.setattr(operation, "_build_artifacts", Mock())
    lock = operation.plan.sidecar_dir / ".lifecycle.lock"

    def start(vmx):
        nonlocal running
        assert lock.is_dir()
        running = True

    def shutdown(*args):
        nonlocal running
        assert lock.is_dir()
        running = False

    def mutate(*args, **kwargs):
        assert lock.is_dir()
        assert "mutation-started" in orchestrator._State.load(operation.state_path).done
        if mutation_fails:
            raise SidecarError("mutation failed")
        operation._state.fs_end_sector = 1234

    vmrun.start.side_effect = start
    monkeypatch.setattr(orchestrator, "shutdown_sidecar", shutdown)
    mutate_call = Mock(side_effect=mutate)
    monkeypatch.setattr(operation, "_run_fs_phases", mutate_call)
    if mutation_fails:
        with pytest.raises(SidecarError, match="mutation failed"):
            operation.run()
        with pytest.raises(SidecarError, match="manual assessment"):
            operation.run()
    else:
        assert operation.run() == operation.run()
    mutate_call.assert_called_once()
    vmrun.start.assert_called_once()
    assert not lock.exists()

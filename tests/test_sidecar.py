"""Sidecar unit tests: VMX rendering, seed content, gates, watch loop.

All offline: fakes stand in for SSH/container executors, and no VM is booted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vmdk_shrink.executor import ExecResult
from vmdk_shrink.sidecar import gates
from vmdk_shrink.sidecar.orchestrator import run_detached_and_watch
from vmdk_shrink.sidecar.seed import meta_data, user_data
from vmdk_shrink.sidecar.ssh import GuestRunner
from vmdk_shrink.sidecar.vmx import SidecarVmxSpec


class FakeRunner:
    """Scripted GuestRunner: maps a predicate to canned responses."""

    def __init__(self, scripts: dict[str, str]) -> None:
        self._scripts = scripts
        self.calls: list[str] = []

    def run(self, command: str, *, timeout: float = 120.0, check: bool = True) -> ExecResult:
        self.calls.append(command)
        for needle, response in self._scripts.items():
            if needle in command:
                code, _, text = response.partition("|")
                result = ExecResult(command=command, exit_code=int(code), output=text)
                if check and result.exit_code != 0:
                    raise AssertionError(f"unexpected failure for {command}: {text}")
                return result
        raise AssertionError(f"no script matched: {command}")


class TestVmx:
    def test_renders_data_disk_absolute_and_nat(self, tmp_path: Path) -> None:
        boot = tmp_path / "base.vmdk"
        data = Path(r"E:\testvm\data.vmdk")
        spec = SidecarVmxSpec(boot_disk=boot, data_disk=data, seed_iso=boot.parent / "seed.iso")
        text = spec.render()
        assert f'sata0:1.fileName = "{data}"' in text
        assert 'connectionType = "nat"' in text
        assert 'sata0.present = "TRUE"' in text
        assert "sata0:0.fileName = " in text
        assert 'ide1:0.present = "TRUE"' in text
        assert f'ide1:0.fileName = "{boot.parent / "seed.iso"}"' in text
        # NAT is the only network; no host-only/bridged surprises
        assert text.count("ethernet0.present") == 1

    def test_write_creates_vmx(self, tmp_path: Path) -> None:
        vmx = SidecarVmxSpec(boot_disk=tmp_path / "b.vmdk", data_disk=tmp_path / "d.vmdk").write(
            tmp_path / "sidecar.vmx"
        )
        assert vmx.is_file()
        assert "sidecar.vmx" in str(vmx)


class TestSeed:
    def test_user_data_has_key_sudo_and_no_root(self) -> None:
        ud = user_data("ssh-ed25519 AAAA test")
        assert ud.startswith("#cloud-config")
        assert "ssh-ed25519 AAAA test" in ud
        assert "NOPASSWD:ALL" in ud
        assert "ssh_pwauth: false" in ud
        assert "disable_root: true" in ud

    def test_meta_data_has_instance_id(self) -> None:
        md = meta_data()
        assert "instance-id: vmdk-shrink-sidecar" in md
        assert "local-hostname: vmdk-shrink-sidecar" in md


class TestGates:
    def test_identify_happy_path(self) -> None:
        runner = FakeRunner(
            {
                "readlink -f /dev/disk/by-uuid": "0|/dev/sdb1\n",
                "cat /sys/class/block/sdb1/size": "0|209715200\n",
            }
        )
        dev = gates.identify_data_disk(runner, "uuid-x", 107374182400)
        assert dev == "/dev/sdb1"

    def test_identify_size_mismatch_is_fatal(self) -> None:
        runner = FakeRunner(
            {
                "readlink -f /dev/disk/by-uuid": "0|/dev/sdb1\n",
                "cat /sys/class/block/sdb1/size": "0|42\n",
            }
        )
        with pytest.raises(gates.GateError, match="identity mismatch"):
            gates.identify_data_disk(runner, "uuid-x", 107374182400)

    def test_identify_ambiguous_uuid_is_fatal(self) -> None:
        runner = FakeRunner({"readlink -f /dev/disk/by-uuid": "0|/dev/sdb1\n/dev/sdc1\n"})
        with pytest.raises(gates.GateError, match="exactly one"):
            gates.identify_data_disk(runner, "uuid-x", 100)

    def test_require_no_locks(self, tmp_path: Path) -> None:
        (tmp_path / "disk.vmdk.lck").mkdir()
        with pytest.raises(gates.GateError, match="lock"):
            gates.require_no_locks(tmp_path)
        gates.require_no_locks(tmp_path / "nowhere")

    def test_partition_layout_gate(self) -> None:
        runner = FakeRunner(
            {
                "blockdev --rereadpt": "0|",
                "/sys/class/block/sdb1/start": "0|44944\n1509949440\n",
                "sgdisk -p": "0|Number  Start (sector)    End (sector)\n"
                "   1           44944      1509994383   720.0 GiB   8300\n",
            }
        )
        gates.assert_partition_layout(runner, "/dev/sdb", 44944, 1509994383)
        assert "blockdev --rereadpt" in runner.calls[0]
        with pytest.raises(gates.GateError, match="expected"):
            gates.assert_partition_layout(runner, "/dev/sdb", 44944, 12345)

    def test_block_count_gate(self) -> None:
        runner = FakeRunner({"dumpe2fs -h": "0|Block count:  188743680\nBlock size:  4096\n"})
        gates.assert_block_count(runner, "/dev/sdb1", 188743680, 4096)
        with pytest.raises(gates.GateError, match="did not reach"):
            gates.assert_block_count(runner, "/dev/sdb1", 100, 4096)

    def test_require_unmounted(self) -> None:
        runner = FakeRunner({"findmnt": "0|"})
        gates.require_unmounted(runner, "/dev/sdb1")
        mounted = FakeRunner({"findmnt": "0|/mnt/data0\n"})
        with pytest.raises(gates.GateError, match="mounted"):
            gates.require_unmounted(mounted, "/dev/sdb1")


class FakeClock:
    """Monotonic clock that advances on sleep — for stall-limit tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class TestWatchLoop:
    def _watch(
        self,
        runner: FakeRunner,
        monkeypatch: pytest.MonkeyPatch,
        *,
        stall_limit: float = 900.0,
        poll: float = 300.0,
    ) -> FakeClock:
        import vmdk_shrink.sidecar.orchestrator as orch

        clock = FakeClock()
        monkeypatch.setattr(orch, "time", clock)
        run_detached_and_watch(
            runner,
            "resize2fs /dev/sdb1 720G",
            phase="resize2fs",
            device="sdb",
            exit_file="/run/x",
            log_file="/run/l",
            max_runtime_s=100000.0,
            poll_s=poll,
            stall_limit_s=stall_limit,
            sleep=clock.sleep,
        )
        return clock

    def test_completes_on_exit_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = FakeRunner(
            {
                "nohup": "0|started\n",
                "cat '/run/x' 2>/dev/null": "0|0\n",
                "awk '{print $6}'": "0|1000\n",
            }
        )
        self._watch(runner, monkeypatch)

    def test_stall_on_frozen_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = FakeRunner(
            {
                "nohup": "0|started\n",
                "cat '/run/x' 2>/dev/null": "0|\n",
                "awk '{print $6}'": "0|1000\n",  # never advances
                "tail -c 500": "0|tail\n",
            }
        )
        with pytest.raises(Exception, match="no I/O progress"):
            self._watch(runner, monkeypatch)

    def test_unreadable_counter_counts_as_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class BrokenProbe(FakeRunner):
            def run(
                self, command: str, *, timeout: float = 120.0, check: bool = True
            ) -> ExecResult:
                if "awk" in command:
                    return ExecResult(command=command, exit_code=0, output="")
                return super().run(command, timeout=timeout, check=check)

        runner = BrokenProbe(
            {
                "nohup": "0|started\n",
                "cat '/run/x' 2>/dev/null": "0|\n",
                "tail -c 500": "0|tail\n",
            }
        )
        with pytest.raises(Exception, match="unreadable"):
            self._watch(runner, monkeypatch)

    def test_progressing_counter_does_not_stall(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Counter advances once then the job finishes before the limit."""
        calls = {"n": 0}

        class AdvancingRunner(FakeRunner):
            def run(
                self, command: str, *, timeout: float = 120.0, check: bool = True
            ) -> ExecResult:
                if "awk" in command:
                    calls["n"] += 1
                    return ExecResult(command=command, exit_code=0, output=f"{calls['n']}\n")
                return super().run(command, timeout=timeout, check=check)

        runner = AdvancingRunner(
            {
                "nohup": "0|started\n",
                "cat '/run/x' 2>/dev/null": "0|\n",
                "tail -c 500": "0|tail\n",
            }
        )
        # exit marker appears after the 6th probe: switch it over
        original_run = runner.run

        def run_with_exit(
            command: str, *, timeout: float = 120.0, check: bool = True
        ) -> ExecResult:
            if "cat '/run/x'" in command and calls["n"] >= 6:
                return ExecResult(command=command, exit_code=0, output="0\n")
            return original_run(command, timeout=timeout, check=check)

        runner.run = run_with_exit  # type: ignore[method-assign]
        self._watch(runner, monkeypatch)
        assert calls["n"] >= 6

    def test_nonzero_exit_raises_with_log_tail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = FakeRunner(
            {
                "nohup": "0|started\n",
                "cat '/run/x' 2>/dev/null": "0|3\n",
                "awk '{print $6}'": "0|1000\n",
                "tail -c 500": "0|resize2fs: error tail\n",
            }
        )
        with pytest.raises(Exception, match="rc=3"):
            self._watch(runner, monkeypatch)


def test_transient_poll_failure_does_not_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed exit-file poll must continue watching, not kill the phase."""
    import vmdk_shrink.sidecar.orchestrator as orch
    from vmdk_shrink.sidecar.ssh import SshError

    calls = {"n": 0}

    class FlakyProbeRunner(FakeRunner):
        def run(self, command: str, *, timeout: float = 120.0, check: bool = True) -> ExecResult:
            if "cat '/run/x'" in command:
                calls["n"] += 1
                if calls["n"] <= 3:
                    raise SshError("ssh: connection dropped (transient)")
                return ExecResult(command=command, exit_code=0, output="0")
            return super().run(command, timeout=timeout, check=check)

    clock = FakeClock()
    monkeypatch.setattr(orch, "time", clock)
    run_detached_and_watch(
        FlakyProbeRunner(
            {"nohup": "0|started\n", "awk '{print $6}'": "0|1000\n", "tail -c 500": "0|t\n"}
        ),
        "resize2fs /dev/sdb1 720G",
        phase="resize2fs",
        device="sdb",
        exit_file="/run/x",
        log_file="/run/l",
        max_runtime_s=100000.0,
        poll_s=300.0,
        stall_limit_s=900.0,
        sleep=clock.sleep,
    )
    assert calls["n"] == 4


def test_guest_runner_protocol_satisfied_by_fake() -> None:
    runner: GuestRunner = FakeRunner({})
    assert callable(runner.run)


class TestGuestIpFromLeases:
    """vmnetdhcp lease lookup: fresh beats stale, expiry beats presence.

    Observed 2026-10-21: the lease file is rewritten out of chronological
    order and expired leases linger, so "last block wins" silently returned
    a dead IP for a healthy guest.
    """

    MAC = "00:0c:29:eb:37:e9"

    @staticmethod
    def _leases(entries: str) -> Path:
        raise NotImplementedError

    def _write(self, tmp_path: Path, body: str) -> Path:
        f = tmp_path / "vmnetdhcp.leases"
        f.write_text(body, encoding="utf-8")
        return f

    def test_picks_newest_unexpired_lease(self, tmp_path: Path) -> None:
        from vmdk_shrink.sidecar.discover import guest_ip_from_leases

        f = self._write(
            tmp_path,
            "lease 192.168.223.133 {\n"
            "  starts 3 2020/01/01 00:00:00;\n"
            f"  hardware ethernet {self.MAC};\n"
            "}\n"
            "lease 192.168.223.134 {\n"
            "  starts 3 2020/01/02 00:00:00;\n"
            f"  hardware ethernet {self.MAC};\n"
            "}\n",
        )
        assert guest_ip_from_leases(self.MAC, f) == "192.168.223.134"

    def test_expired_lease_is_ignored(self, tmp_path: Path) -> None:
        from vmdk_shrink.sidecar.discover import guest_ip_from_leases

        f = self._write(
            tmp_path,
            "lease 192.168.223.133 {\n"
            "  starts 3 2020/01/01 00:00:00;\n"
            "  ends 3 2020/01/01 00:30:00;\n"
            f"  hardware ethernet {self.MAC};\n"
            "}\n",
        )
        assert guest_ip_from_leases(self.MAC, f) is None

    def test_other_mac_is_not_returned(self, tmp_path: Path) -> None:
        from vmdk_shrink.sidecar.discover import guest_ip_from_leases

        f = self._write(
            tmp_path,
            "lease 192.168.223.140 {\n"
            "  starts 3 2020/01/02 00:00:00;\n"
            "  hardware ethernet 00:0c:29:11:22:33;\n"
            "}\n",
        )
        assert guest_ip_from_leases(self.MAC, f) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        from vmdk_shrink.sidecar.discover import guest_ip_from_leases

        assert guest_ip_from_leases(self.MAC, tmp_path / "nope") is None

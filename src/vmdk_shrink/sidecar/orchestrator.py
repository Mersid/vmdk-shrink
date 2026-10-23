"""The sidecar state machine: boot a disposable VM, drive the fs work over SSH.


Phases, each gated before and asserted after (see :mod:`.gates`):


    preflight -> image -> seed -> vmx -> boot -> ssh -> cloud-init
    -> identify -> fsck -> resize (watched) -> partition -> zerofree
    -> shutdown


The data VMDK is attached to the sidecar (1x footprint; VMware does the
VMDK I/O natively). ``resize2fs`` runs detached and is watched with the
package's stall contract: progress = ``/sys/class/block/<dev>/stat`` field 6
(write sectors), and an unreadable counter counts as frozen. Interrupted
mutations require manual assessment; completed phases in ``state.json`` are
checkpoints, not a guarantee that a failed operation can safely be retried.
"""

from __future__ import annotations

import json
import ntpath
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from ..console import err, info, ok, step, warn
from ..container import Container
from ..executor import ExecResult, Executor
from ..fsops import FsError, check_fs, parse_free_ranges, recreate_partition, relocate_gpt_backup
from ..graintrim import merge_ranges
from ..paths import DEFAULT_PATHS, HostPaths
from ..progress import PhaseBar
from . import gates
from .discover import vmnet8_subnet, wait_for_guest_ip
from .gates import GateError
from .image import ensure_base_image
from .seed import SEED_USER, generate_keypair, meta_data, user_data
from .ssh import GuestRunner, SshConfig, SshError, SshRunner
from .vmx import SidecarVmxSpec

SECTOR = 512
GIB = 2**30


SSH_WAIT_ATTEMPTS = 30
SSH_WAIT_DELAY_S = 10.0
CLOUD_INIT_TIMEOUT = 900.0
RESIZE_MAX_RUNTIME_S = 6 * 3600.0
ZEROFREE_MAX_RUNTIME_S = 4 * 3600.0
POLL_INTERVAL_S = 300.0
STALL_LIMIT_S = 900.0
GUEST_IP_TIMEOUT_S = 240.0
POWER_OFF_TIMEOUT_S = 180.0


class SidecarError(RuntimeError):
    pass


class Vmrun(Protocol):
    """Host-side VM lifecycle (vmrun); factored out for tests."""

    def start(self, vmx: Path) -> None: ...
    def guest_ip(self, vmx: Path) -> str: ...
    def guest_state(self, vmx: Path) -> str: ...
    def stop(self, vmx: Path, *, hard: bool = False) -> None: ...
    def is_running(self, vmx: Path) -> bool: ...


@contextmanager
def sidecar_lifecycle(sidecar_dir: Path, vmrun: Vmrun) -> Iterator[None]:
    """Exclude other sidecar calls until poweroff has been positively verified."""
    sidecar_dir = sidecar_dir.resolve()
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    lock = sidecar_dir / ".lifecycle.lock"
    guidance = (
        f"sidecar lifecycle lock retained: {lock}; manual recovery required: "
        "confirm no sidecar operation or VM is active, verify image mounts/NBD "
        "connections are released, and assess any interrupted disk mutation "
        "before manually removing this lock; do not delete the sidecar directory"
    )
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise SidecarError(f"sidecar operation active or interrupted; {guidance}") from exc
    failure: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            stopped = not vmrun.is_running(sidecar_dir / "sidecar.vmx")
        except Exception:  # noqa: BLE001 - an unknown power state must retain the lock
            stopped = False
        if stopped and not (sidecar_dir / ".image-prep.lock").exists():
            lock.rmdir()
        elif failure is not None:
            failure.add_note(guidance)
            warn(guidance)
        else:
            raise SidecarError(guidance)


@dataclass(frozen=True)
class SidecarPlan:
    """Inputs decided before the sidecar boots."""

    data_vmdk: Path
    target_gib: int
    start_lba: int
    expected_partition_bytes: int
    fs_uuid: str
    block_size: int
    sidecar_dir: Path
    vm_dir: Path


@dataclass(frozen=True)
class SidecarResult:
    """What the host-side phases need to continue."""

    fs_end_sector: int
    data_partition: str
    free_ranges: list[tuple[int, int]]  # fs blocks, inclusive; empty = already zeroed


@dataclass
class _State:
    """Phase checkpoints plus values pinned by earlier phases."""

    done: list[str]
    fs_end_sector: int | None = None
    data_partition: str | None = None
    free_ranges: list[list[int]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> _State:
        if not path.is_file():
            return cls(done=[])
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            done=list(raw.get("done", [])),
            fs_end_sector=raw.get("fs_end_sector"),
            data_partition=raw.get("data_partition"),
            free_ranges=[list(r) for r in raw.get("free_ranges", [])],
        )

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "done": self.done,
                    "fs_end_sector": self.fs_end_sector,
                    "data_partition": self.data_partition,
                    "free_ranges": self.free_ranges,
                }
            ),
            encoding="utf-8",
        )


class RealVmrun:
    """vmrun.exe wrapper (no UAC needed; soft power ops only here)."""

    def __init__(self, paths: HostPaths = DEFAULT_PATHS) -> None:
        self._exe = paths.workstation_dir / "vmrun.exe"

    def _run(self, *args: str, timeout: float = 120.0) -> ExecResult:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no user input
                [str(self._exe), "-T", "ws", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SidecarError(f"vmrun {' '.join(args[:2])} timed out") from exc
        except OSError as exc:
            raise SidecarError(f"vmrun not available: {exc}") from exc
        if proc.returncode != 0:
            raise SidecarError(
                f"vmrun {' '.join(args[:2])} failed (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return ExecResult(command=" ".join(args), exit_code=0, output=proc.stdout or "")

    def start(self, vmx: Path) -> None:
        self._run("start", str(vmx), "nogui", timeout=GUEST_IP_TIMEOUT_S)

    def guest_ip(self, vmx: Path) -> str:
        result = self._run("getGuestIPAddress", str(vmx), "-wait", timeout=GUEST_IP_TIMEOUT_S)
        ip = result.output.strip().splitlines()[-1] if result.output.strip() else ""
        if not ip:
            raise SidecarError("vmrun reported no guest IP")
        return ip

    def guest_state(self, vmx: Path) -> str:
        """Running state via ``vmrun list`` (getGuestState is not in this
        Workstation's vmrun); tools state via ``checkToolsState``."""
        result = self._run("list")
        lines = result.output.strip().splitlines()
        header = re.fullmatch(r"Total running VMs:\s*(\d+)", lines[0]) if lines else None
        if header is None or int(header.group(1)) != len(lines) - 1:
            raise SidecarError("cannot verify running VMs: malformed vmrun list output")
        target = ntpath.normcase(ntpath.normpath(str(vmx.resolve())))
        for line in lines[1:]:
            path = line.strip()
            if not ntpath.isabs(path) or not path.lower().endswith(".vmx"):
                raise SidecarError("cannot verify running VMs: invalid VMX path in vmrun list")
            if ntpath.normcase(ntpath.normpath(path)) == target:
                return "The VM is running"
        return "The VM is not running"

    def tools_state(self, vmx: Path) -> str:
        result = self._run("checkToolsState", str(vmx))
        return result.output.strip().splitlines()[-1] if result.output.strip() else ""

    def stop(self, vmx: Path, *, hard: bool = False) -> None:
        mode = "hard" if hard else "soft"
        self._run("stop", str(vmx), mode, timeout=POWER_OFF_TIMEOUT_S)

    def is_running(self, vmx: Path) -> bool:
        return "not running" not in self.guest_state(vmx).lower()


class _SudoRunner:
    """Wraps a GuestRunner: runs every command via sudo (NOPASSWD in the seed)."""

    def __init__(self, inner: GuestRunner) -> None:
        self._inner = inner

    def run(self, command: str, *, timeout: float = 120.0, check: bool = True) -> ExecResult:
        return self._inner.run(
            f"sudo -n sh -c {shlex.quote(command)}", timeout=timeout, check=check
        )

    def stream(
        self, command: str, *, timeout: float = 7200.0, chunk_cb: object = None
    ) -> ExecResult:
        return self._inner.stream(
            f"sudo -n sh -c {shlex.quote(command)}", timeout=timeout, chunk_cb=chunk_cb
        )


# ----------------------------------------------------------- watched run ----


def _stat_write_sectors(runner: GuestRunner, dev: str) -> int:
    """/sys/class/block/<dev>/stat field 6 (write sectors); -1 if unreadable."""
    result = runner.run(f"awk '{{print $6}}' /sys/class/block/{dev}/stat", timeout=30, check=False)
    if result.exit_code != 0:
        return -1
    try:
        return int(result.output.strip())
    except ValueError:
        return -1


def run_detached_and_watch(
    runner: GuestRunner,
    command: str,
    *,
    phase: str,
    device: str,
    exit_file: str,
    log_file: str,
    max_runtime_s: float,
    poll_s: float = POLL_INTERVAL_S,
    stall_limit_s: float = STALL_LIMIT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Run *command* detached in the guest; watch write-progress for stalls.


    The v3 watchdog contract applies unchanged: progress is write sectors
    (field 6), an unreadable counter (-1) counts as frozen, and the stall
    limit is wall-clock without forward movement.
    """
    runner.run(
        f"rm -f '{exit_file}' '{log_file}' && nohup sh -c "
        f"'{command}; echo $? > {exit_file}' > '{log_file}' 2>&1 & echo started",
        timeout=30,
    )
    last = _stat_write_sectors(runner, device)
    last_change = time.monotonic()
    t0 = last_change
    while True:
        sleep(poll_s)
        # Transient SSH failures must not abort the phase: the remote command
        # is still running and its exit file still decides the outcome. A
        # failed poll feeds the watchdog's unreadable-counter (-1) path --
        # a *persistent* SSH outage accumulates into a stall abort instead.
        try:
            exit_raw = runner.run(f"cat '{exit_file}' 2>/dev/null", timeout=30, check=False)
        except SshError:
            continue
        exit_marker = exit_raw.output.strip()
        if exit_marker.isdigit():
            code = int(exit_marker)
            if code == 0:
                return
            tail = runner.run(f"tail -c 500 '{log_file}'", timeout=30, check=False).output
            raise SidecarError(f"{phase} failed in guest (rc={code}): {tail}")
        try:
            now = _stat_write_sectors(runner, device)
        except SshError:
            now = -1
        elapsed = time.monotonic() - t0
        if now < 0 or now == last:
            frozen = time.monotonic() - last_change
            if frozen >= stall_limit_s:
                tail = runner.run(f"tail -c 500 '{log_file}'", timeout=30, check=False).output
                raise SidecarError(
                    f"{phase}: no I/O progress for {frozen / 60:.0f} min "
                    f"(counter {'unreadable' if now < 0 else 'frozen'}): {tail}"
                )
        else:
            last = now
            last_change = time.monotonic()
        if elapsed >= max_runtime_s:
            raise SidecarError(f"{phase} exceeded {max_runtime_s / 3600:.0f} h runtime limit")


# ------------------------------------------------------------- phases ----


class SidecarOrchestrator:
    """Drives one sidecar fs-work cycle for a data VMDK."""

    def __init__(self, plan: SidecarPlan, *, paths: HostPaths = DEFAULT_PATHS) -> None:
        self.plan = plan
        self.paths = paths
        self.vmrun = RealVmrun(paths)
        self.state_path = plan.sidecar_dir / "state.json"
        self._state = _State(done=[])

    # -- small helpers ---------------------------------------------------
    def _mark(self, phase: str) -> None:
        if phase not in self._state.done:
            self._state.done.append(phase)
        self._state.save(self.state_path)

    def _done(self, phase: str) -> bool:
        return phase in self._state.done

    def _container(self, mounts: tuple[tuple[Path, str], ...]) -> Container:
        return Container(mounts, paths=self.paths)

    # -- the pipeline ----------------------------------------------------
    def run(
        self,
        *,
        bar: PhaseBar | None = None,
        resize_total_bytes: int = 0,
        zerofree_total_bytes: int = 0,
    ) -> SidecarResult:
        with sidecar_lifecycle(self.plan.sidecar_dir, self.vmrun):
            self._state = _State.load(self.state_path)
            mutation_phases = {"mutation-started", "resize", "partition", "free-map", "zerofree"}
            if mutation_phases.intersection(self._state.done) and not self._done("done"):
                raise SidecarError(
                    "interrupted sidecar mutation requires manual assessment; "
                    "automatic mutation retries are disabled; preserve state.json and the disk"
                )
            return self._run_locked(
                bar=bar,
                resize_total_bytes=resize_total_bytes,
                zerofree_total_bytes=zerofree_total_bytes,
            )

    def _run_locked(
        self,
        *,
        bar: PhaseBar | None = None,
        resize_total_bytes: int = 0,
        zerofree_total_bytes: int = 0,
    ) -> SidecarResult:
        plan = self.plan
        step(
            f"Sidecar mode: shrink [val]{plan.data_vmdk.name}[/val] to "
            f"[val]{plan.target_gib} GiB[/val] via disposable Debian VM"
        )
        gates.require_no_locks(plan.vm_dir)
        gates.require_free_space(plan.sidecar_dir, 5)

        vmx = plan.sidecar_dir / "sidecar.vmx"
        if self.vmrun.is_running(vmx):
            raise GateError("sidecar VM is already running; stop it before preparing artifacts")
        state = self._state
        if not self._done("image") or not self._done("seed") or not self._done("vmx"):
            self._build_artifacts()
        if not self._done("done"):
            self._boot_and_operate(
                vmx,
                bar=bar,
                resize_total_bytes=resize_total_bytes,
                zerofree_total_bytes=zerofree_total_bytes,
            )
        assert state.fs_end_sector is not None and state.data_partition is not None
        return SidecarResult(
            fs_end_sector=state.fs_end_sector,
            data_partition=state.data_partition,
            free_ranges=[(a, b) for a, b in state.free_ranges],
        )

    def _build_artifacts(self) -> None:
        plan = self.plan
        with self._container(((plan.sidecar_dir, "/work"),)) as container:
            seed_dir = plan.sidecar_dir / ".seed"
            if not self._done("seed"):
                pair = generate_keypair(plan.sidecar_dir)
                seed_dir.mkdir(parents=True, exist_ok=True)
                (seed_dir / "user-data").write_text(user_data(pair.public_key), encoding="utf-8")
                (seed_dir / "meta-data").write_text(meta_data(), encoding="utf-8")
                self._mark("seed")
            base = ensure_base_image(
                plan.sidecar_dir,
                container,
                container_cache="/work",
                seed_dir=(seed_dir if self._done("seed") else None),
            )
            if not self._done("vmx"):
                spec = SidecarVmxSpec(
                    boot_disk=base.vmdk,
                    data_disk=plan.data_vmdk,
                    serial_log=plan.sidecar_dir / "serial.log",
                )
                spec.write(plan.sidecar_dir / "sidecar.vmx")
                self._mark("vmx")
            if not self._done("image"):
                self._mark("image")  # verified/converted above

    def _boot_and_operate(
        self,
        vmx: Path,
        *,
        bar: PhaseBar | None = None,
        resize_total_bytes: int = 0,
        zerofree_total_bytes: int = 0,
    ) -> None:
        plan = self.plan
        vmrun = self.vmrun
        state = self._state

        if vmrun.is_running(vmx):
            raise GateError(
                "sidecar VM is already running — a previous cycle may be active; "
                "manual shutdown and assessment required; preserve the sidecar directory"
            )
        step("Booting sidecar VM")
        runner: GuestRunner | None = None
        try:
            vmrun.start(vmx)
            subnet = vmnet8_subnet()
            ip = wait_for_guest_ip(vmx, subnet=subnet, attempts=40, delay_s=15.0)
            info(f"sidecar IP: [val]{ip}[/val] (NAT {subnet})")
            pair_key = plan.sidecar_dir / "sidecar_key"
            config = SshConfig(
                host=ip,
                user=SEED_USER,
                key=pair_key,
                known_hosts=plan.sidecar_dir / "known_hosts",
            )
            runner = _SudoRunner(SshRunner(config))
            gates.wait_ssh_ready(runner, attempts=SSH_WAIT_ATTEMPTS, delay_s=SSH_WAIT_DELAY_S)
            step("Waiting for cloud-init to finish")
            ci = runner.run("cloud-init status --wait", timeout=CLOUD_INIT_TIMEOUT, check=False)
            if ci.exit_code != 0:
                raise GateError(f"cloud-init did not complete cleanly: {ci.output[-300:]}")

            partition = gates.identify_data_disk(
                runner, plan.fs_uuid, plan.expected_partition_bytes
            )
            device = partition.rstrip("0123456789") if partition[-1].isdigit() else partition
            if not device or device == partition:
                raise GateError(f"cannot derive parent device from {partition}")
            info(f"data partition identified: [val]{partition}[/val] on [val]{device}[/val]")

            self._mark("mutation-started")
            self._run_fs_phases(
                runner,
                partition,
                device,
                bar=bar,
                resize_total_bytes=resize_total_bytes,
                zerofree_total_bytes=zerofree_total_bytes,
            )
            self._shutdown(runner, vmx)
            state.data_partition = partition
            self._mark("done")
        except Exception:
            err(
                "sidecar cycle failed — powering the sidecar off; "
                "interrupted disk mutations require manual assessment, not an automatic retry"
            )
            try:
                shutdown_sidecar(self.vmrun, vmx, runner)
            except SidecarError:
                warn("could not power off sidecar via vmrun; stop it manually")
            raise

    def _run_fs_phases(
        self,
        runner: GuestRunner,
        partition: str,
        device: str,
        *,
        bar: PhaseBar | None = None,
        resize_total_bytes: int = 0,
        zerofree_total_bytes: int = 0,
    ) -> None:
        plan = self.plan
        target_sectors = plan.target_gib * GIB // SECTOR
        end_lba = plan.start_lba + target_sectors - 1
        expected_blocks = target_sectors * SECTOR // plan.block_size
        exit_file = "/run/vmdk-shrink.exit"
        log_file = "/run/vmdk-shrink.log"

        gates.require_unmounted(runner, partition)
        recreate_partition(
            cast(Executor, runner),
            device,
            start_lba=plan.start_lba,
            end_lba=end_lba,
            validate_only=True,
        )
        step(f"e2fsck -fy on {partition}")
        check_fs(cast(Executor, runner), partition, fix=True)

        if not self._done("resize"):
            step(f"resize2fs to [val]{plan.target_gib} GiB[/val] (watched)")
            if bar is not None:
                bar.start_bytes(
                    "resize2fs",
                    resize_total_bytes,
                    lambda: _stat_write_sectors(runner, device.removeprefix("/dev/")),
                )
            try:
                run_detached_and_watch(
                    runner,
                    f"/usr/sbin/resize2fs '{partition}' {plan.target_gib}G",
                    phase="resize2fs",
                    device=device.removeprefix("/dev/"),
                    exit_file=exit_file,
                    log_file=log_file,
                    max_runtime_s=RESIZE_MAX_RUNTIME_S,
                )
            finally:
                if bar is not None:
                    bar.finish()
            self._mark("resize")
        else:
            info("resize phase recorded complete")

        step("post-resize fsck")
        check_fs(cast(Executor, runner), partition, fix=True)
        gates.assert_block_count(runner, partition, expected_blocks, plan.block_size)
        fs_end = plan.start_lba + expected_blocks * (plan.block_size // SECTOR) - 1
        self._state.fs_end_sector = fs_end
        self._state.save(self.state_path)

        if not self._done("partition"):
            step(f"Recreating partition 1: [{plan.start_lba}, {end_lba}]")
            # These shared operations use only run(), not Executor.run_detached().
            recreate_partition(
                cast(Executor, runner), device, start_lba=plan.start_lba, end_lba=end_lba
            )
            relocate_gpt_backup(cast(Executor, runner), device)
            gates.assert_partition_layout(runner, device, plan.start_lba, end_lba)
            self._mark("partition")
        else:
            info("partition phase recorded complete")

        gates.require_unmounted(runner, partition)
        if not self._done("free-map"):
            step("free-space map (dumpe2fs, in guest)")
            try:
                out = runner.run(f"/usr/sbin/dumpe2fs '{partition}'", timeout=600.0).output
                ranges = merge_ranges(parse_free_ranges(out))
            except (SshError, FsError, ValueError) as exc:
                ranges = []
                warn(f"free-map collection failed ({exc}); falling back to zerofree")
                self._run_zerofree(runner, partition, device, bar, zerofree_total_bytes)
            if ranges:
                self._state.free_ranges = [[a, b] for a, b in ranges]
            self._mark("free-map")
        else:
            info("free-map phase recorded complete")

        runner.run("sync", timeout=300.0)

    def _run_zerofree(
        self,
        runner: GuestRunner,
        partition: str,
        device: str,
        bar: PhaseBar | None,
        zerofree_total_bytes: int,
    ) -> None:
        """Legacy fallback: zero free blocks in-guest (slower, writes data)."""
        if self._done("zerofree"):
            info("zerofree phase recorded complete")
            return
        step("zerofree (watched)")
        if bar is not None:
            bar.start_bytes(
                "zerofree",
                zerofree_total_bytes,
                lambda: _stat_write_sectors(runner, device.removeprefix("/dev/")),
            )
        try:
            run_detached_and_watch(
                runner,
                f"/usr/sbin/zerofree -v '{partition}'",
                phase="zerofree",
                device=device.removeprefix("/dev/"),
                exit_file="/run/vmdk-shrink.exit",
                log_file="/run/vmdk-shrink.log",
                max_runtime_s=ZEROFREE_MAX_RUNTIME_S,
            )
        finally:
            if bar is not None:
                bar.finish()
        self._mark("zerofree")

    def _shutdown(self, runner: GuestRunner, vmx: Path) -> None:
        shutdown_sidecar(self.vmrun, vmx, runner)


def shutdown_sidecar(vmrun: Vmrun, vmx: Path, runner: GuestRunner | None = None) -> None:
    """Request guest poweroff without VMware Tools, then verify release of the VM."""
    step("Powering sidecar off (graceful)")
    if not vmrun.is_running(vmx):
        return
    graceful = False
    try:
        if runner is not None:
            runner.run("systemctl poweroff --no-wall", timeout=60, check=False)
        else:
            vmrun.stop(vmx)
        graceful = True
    except (SidecarError, SshError):
        warn("graceful poweroff unavailable; verifying state before hard stop")
    if graceful:
        deadline = time.monotonic() + POWER_OFF_TIMEOUT_S
        while time.monotonic() < deadline:
            if not vmrun.is_running(vmx):
                ok("sidecar powered off")
                return
            time.sleep(5)
    if not vmrun.is_running(vmx):
        return
    warn("hard-stopping sidecar; poweroff must be confirmed before continuing")
    vmrun.stop(vmx, hard=True)
    deadline = time.monotonic() + POWER_OFF_TIMEOUT_S
    while vmrun.is_running(vmx):
        if time.monotonic() >= deadline:
            raise SidecarError("sidecar did not power off even after hard stop")
        time.sleep(5)
    ok("sidecar hard-stopped")


def run_sidecar_fs_phase(
    vm_dir: Path,
    data_vmdk: Path,
    target_gib: int,
    *,
    start_lba: int,
    expected_partition_bytes: int,
    fs_uuid: str,
    block_size: int,
    paths: HostPaths = DEFAULT_PATHS,
    bar: PhaseBar | None = None,
    resize_total_bytes: int = 0,
    zerofree_total_bytes: int = 0,
) -> SidecarResult:
    """Entry point used by ``shrink --mode sidecar``."""
    plan = SidecarPlan(
        data_vmdk=data_vmdk,
        target_gib=target_gib,
        start_lba=start_lba,
        expected_partition_bytes=expected_partition_bytes,
        fs_uuid=fs_uuid,
        block_size=block_size,
        sidecar_dir=vm_dir / ".sidecar",
        vm_dir=vm_dir,
    )
    orchestrator = SidecarOrchestrator(plan, paths=paths)
    try:
        return orchestrator.run(
            bar=bar,
            resize_total_bytes=resize_total_bytes,
            zerofree_total_bytes=zerofree_total_bytes,
        )
    except GateError as exc:
        raise SidecarError(f"gate failed: {exc}") from exc

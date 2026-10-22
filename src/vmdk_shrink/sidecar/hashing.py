"""Verification hashing through the sidecar VM (no 9p in the data path).

The same ``manifest.collect`` used by the container path runs in the guest:
``find | xargs sha256sum`` streamed over SSH to a host-side raw manifest. The
guest is a disposable Debian cloud VM with the data VMDK attached through
VMware's native disk stack, so reads never touch the 9p bind mount that caps
the container path (~20 MB/s per connection).

Every primitive here is reused from elsewhere in the package: the base-image
fetch/verify, the NoCloud seed, the VMX spec, the identify-by-UUID-and-size
gate, and the manifest collector. The only new logic is the glue that says
\"boot, mount ro, hash, power off\".
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from ..console import err, info, ok, step, warn
from ..executor import ExecResult
from ..manifest import Manifest, collect
from ..paths import DEFAULT_PATHS, HostPaths
from ..progress import PhaseBar
from ..session import open_session
from .discover import vmnet8_subnet, wait_for_guest_ip
from .gates import GateError, identify_data_disk, probe_fs_uuid, wait_ssh_ready
from .image import ensure_base_image
from .orchestrator import (
    CLOUD_INIT_TIMEOUT,
    POWER_OFF_TIMEOUT_S,
    SSH_WAIT_ATTEMPTS,
    SSH_WAIT_DELAY_S,
    RealVmrun,
    SidecarError,
)
from .seed import SEED_USER, generate_keypair
from .ssh import SshConfig, SshRunner
from .vmx import SidecarVmxSpec

GUEST_MOUNT = "/mnt/data"


class _SudoStream:
    """Run guest commands as root (mounting needs it; NOPASSWD in the seed)."""

    def __init__(self, inner: SshRunner) -> None:
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


def hash_via_sidecar(
    vm_dir: Path,
    data_vmdk: Path,
    *,
    out_path: Path,
    paths: HostPaths = DEFAULT_PATHS,
    bar: PhaseBar | None = None,
    fs_uuid: str | None = None,
    expected_partition_bytes: int | None = None,
) -> Manifest:
    """Hash every file of the data VMDK inside the sidecar VM.

    ``fs_uuid``/``expected_partition_bytes`` identify the data partition in the
    guest (both must match — wrong-disk ambiguity is fatal). When not supplied
    they are probed from a quick read-only container attach first.
    """
    vm_dir = Path(vm_dir).resolve()
    data_vmdk = Path(data_vmdk).resolve()
    sidecar_dir = vm_dir / ".sidecar"

    if fs_uuid is None or expected_partition_bytes is None:
        step("Probing data filesystem identity (read-only container attach)")
        from ..session import attach_ro  # local: keeps the import graph shallow

        with open_session(vm_dir, paths=paths) as session:
            disk = attach_ro(session, data_vmdk, index=0)
            try:
                session.nbd.rescan(disk)
                part = disk.partition(1)
                fs_uuid = fs_uuid or probe_fs_uuid(session.executor, part)
                expected_partition_bytes = expected_partition_bytes or int(
                    session.executor.run(f"blockdev --getsize64 {part}", timeout=60).output
                )
            finally:
                session.nbd.disconnect(check=False)
        info(f"data filesystem: [val]{fs_uuid}[/val], {expected_partition_bytes} bytes")

    _ensure_artifacts(sidecar_dir, data_vmdk, paths)
    vmrun = RealVmrun(paths)
    vmx = sidecar_dir / "sidecar.vmx"
    if vmrun.is_running(vmx):
        raise SidecarError("sidecar VM is already running; stop it first")

    step("Booting sidecar VM for hashing")
    vmrun.start(vmx)
    runner: _SudoStream | None = None
    try:
        subnet = vmnet8_subnet()
        ip = wait_for_guest_ip(vmx, subnet=subnet, attempts=40, delay_s=15.0)
        info(f"sidecar IP: [val]{ip}[/val] (NAT {subnet})")
        runner = _SudoStream(
            SshRunner(
                SshConfig(
                    host=ip,
                    user=SEED_USER,
                    key=sidecar_dir / "sidecar_key",
                    known_hosts=sidecar_dir / "known_hosts",
                )
            )
        )
        wait_ssh_ready(runner, attempts=SSH_WAIT_ATTEMPTS, delay_s=SSH_WAIT_DELAY_S)
        ci = runner.run("cloud-init status --wait", timeout=CLOUD_INIT_TIMEOUT, check=False)
        if ci.exit_code != 0:
            raise GateError(f"cloud-init did not complete cleanly: {ci.output[-300:]}")

        assert fs_uuid is not None and expected_partition_bytes is not None
        partition = identify_data_disk(runner, fs_uuid, expected_partition_bytes)
        step(f"hashing [val]{partition}[/val] read-only in the guest")
        runner.run(f"mkdir -p {GUEST_MOUNT} && mount -o ro {partition} {GUEST_MOUNT}", timeout=120)
        try:
            manifest = collect(runner, mount=GUEST_MOUNT, out_path=out_path, bar=bar)
        finally:
            runner.run(f"umount {GUEST_MOUNT}", timeout=300, check=False)
        _shutdown(vmrun, vmx)
        ok(f"hashed {len(manifest)} files via sidecar -> {out_path.with_suffix('.raw').name}")
        return manifest
    except Exception:
        err("sidecar hash failed — powering the sidecar off")
        try:
            if vmrun.is_running(vmx):
                try:
                    vmrun.stop(vmx)
                except SidecarError:
                    warn("graceful poweroff failed; falling back to stop hard")
                    vmrun.stop(vmx, hard=True)
        except SidecarError:
            warn("could not power off sidecar via vmrun; stop it manually")
        raise
    finally:
        del runner  # connection closes with its process


def _shutdown(vmrun: RealVmrun, vmx: Path) -> None:
    step("Powering sidecar off (graceful)")
    # The guest is a hash reader only; a graceful stop is courtesy, hard is safe.
    deadline = time.monotonic() + POWER_OFF_TIMEOUT_S
    try:
        vmrun.stop(vmx)
    except SidecarError:
        warn("graceful poweroff failed; falling back to stop hard")
        vmrun.stop(vmx, hard=True)
        return
    while time.monotonic() < deadline:
        if not vmrun.is_running(vmx):
            ok("sidecar powered off")
            return
        time.sleep(5)
    warn("graceful poweroff timed out; hard-stopping (guest only ever read)")
    vmrun.stop(vmx, hard=True)


def _ensure_artifacts(sidecar_dir: Path, data_vmdk: Path, paths: HostPaths) -> None:
    """Build (or reuse) the boot image and VMX for a hash session.

    Mirrors the orchestrator's proven recipe: the NoCloud seed is BAKED into
    the boot image at ``/var/lib/cloud/seed/nocloud/`` (ds-identify ignores
    CD-ROM seeds on Workstation NAT and would disable cloud-init entirely).
    The bake is re-run every time - it is idempotent, and cached conversions
    may predate the seed.
    """
    from ..container import Container
    from .image import _force_nocloud
    from .seed import meta_data, user_data

    seed_dir = sidecar_dir / ".seed"
    pair = generate_keypair(sidecar_dir)
    seed_dir.mkdir(parents=True, exist_ok=True)
    (seed_dir / "user-data").write_text(user_data(pair.public_key), encoding="utf-8")
    (seed_dir / "meta-data").write_text(meta_data(), encoding="utf-8")

    with Container(((sidecar_dir, "/work"),), paths=paths) as container:
        base = ensure_base_image(
            sidecar_dir, container, container_cache="/work", seed_dir=seed_dir
        )
        _force_nocloud(container, "/work", base.vmdk.name, seed_dir=seed_dir)
        vmx = sidecar_dir / "sidecar.vmx"
        if not vmx.is_file():
            spec = SidecarVmxSpec(
                boot_disk=base.vmdk,
                data_disk=data_vmdk,
                serial_log=sidecar_dir / "serial.log",
            )
            spec.write(vmx)

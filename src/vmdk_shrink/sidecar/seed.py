"""cloud-init NoCloud seed: an ephemeral SSH keypair plus a ``cidata`` ISO.

The seed configures boot plumbing *only* — an SSH key, passwordless sudo for
the image's default user, and nothing else. No operational command ever runs
at boot: the orchestrator drives the guest over SSH after the boot gates
pass, so a half-booted sidecar cannot mutate the data disk by accident.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..console import info, ok, step
from ..executor import ExecError, Executor

SEED_USER = "debian"


class SeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class Keypair:
    private_key: Path
    public_key: str

    @property
    def private_key_str(self) -> str:
        return str(self.private_key)


def generate_keypair(work_dir: Path) -> Keypair:
    """Create (or reuse) an ed25519 keypair dedicated to sidecar sessions."""
    work_dir.mkdir(parents=True, exist_ok=True)
    key = work_dir / "sidecar_key"
    pub = key.with_suffix(key.suffix + ".pub")
    if key.is_file() and pub.is_file():
        info(f"reusing ephemeral keypair: {key.name}")
        return Keypair(private_key=key, public_key=pub.read_text(encoding="utf-8").strip())
    if key.exists() or pub.exists():
        raise SeedError(f"stale half-keypair in {work_dir}: delete sidecar_key* and rerun")
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        raise SeedError("ssh-keygen not found on PATH (Windows ships it under System32\\OpenSSH)")
    proc = subprocess.run(  # noqa: S603 - fixed argv, no user input
        [ssh_keygen, "-t", "ed25519", "-N", "", "-C", "vmdk-shrink-sidecar", "-f", str(key), "-q"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not key.is_file():
        raise SeedError(f"ssh-keygen failed: {proc.stderr.strip()}")
    # Windows OpenSSH validates ACLs on private keys; strip inheritance and
    # keep read access for the current user only.
    icacls = shutil.which("icacls")
    if icacls is not None:
        user = os.environ.get("USERNAME", "Administrators")  # noqa: SIM112 - env name
        subprocess.run(  # noqa: S603 - fixed argv
            [icacls, str(key), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            check=False,
        )
    pub = key.with_suffix(key.suffix + ".pub")
    if not pub.is_file():
        raise SeedError(f"keygen did not produce {pub}")
    ok(f"ephemeral keypair: {key.name}")
    return Keypair(private_key=key, public_key=pub.read_text(encoding="utf-8").strip())


def user_data(public_key: str) -> str:
    """The cloud-config the sidecar boots with."""
    return (
        "#cloud-config\n"
        "users:\n"
        f"  - name: {SEED_USER}\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    shell: /bin/bash\n"
        "    ssh_authorized_keys:\n"
        f"      - {public_key}\n"
        "ssh_pwauth: false\n"
        "disable_root: true\n"
        "package_update: true\n"
        "packages:\n"
        "  - zerofree\n"
        "  - gdisk\n"
        "  - parted\n"
        'final_message: "vmdk-shrink sidecar ready after $UPTIME seconds"\n'
    )


def meta_data(instance_id: str = "vmdk-shrink-sidecar") -> str:
    return f"instance-id: {instance_id}\nlocal-hostname: vmdk-shrink-sidecar\n"


def network_config(mac: str) -> str:
    """Explicit DHCP-on-MAC network config (NoCloud ``network-config`` key).

    Without this, cloud-init's network legacy path may leave the NIC down on
    images whose default renderer never sees the hypervisor NIC (observed on
    Debian trixie genericcloud + Workstation NAT: link up, zero DHCP tries).
    """
    return (
        "version: 2\n"
        "ethernets:\n"
        "  sidecar0:\n"
        "    match:\n"
        "      macaddress: " + mac + "\n"
        "    set-name: sidecar0\n"
        "    dhcp4: true\n"
        "    dhcp6: false\n"
        "    optional: true\n"
    )


def build_seed_iso(
    seed_dir: Path, pair: Keypair, ex: Executor, container_seed: str, guest_mac: str = ""
) -> Path:
    """Write the seed files and build ``cidata`` ISO9660 inside the container.

    ``ex`` must see *seed_dir* at *container_seed*. ``genisoimage`` is installed
    on demand (the packaged Dockerfile also pre-installs it).
    """
    seed_dir.mkdir(parents=True, exist_ok=True)
    ud = seed_dir / "user-data"
    md = seed_dir / "meta-data"
    nc = seed_dir / "network-config"
    iso = seed_dir / "sidecar-seed.iso"
    ud.write_text(user_data(pair.public_key), encoding="utf-8")
    md.write_text(meta_data(fresh_instance_id()), encoding="utf-8")
    if guest_mac:
        nc.write_text(network_config(guest_mac), encoding="utf-8")
        files = (
            f"{container_seed}/user-data {container_seed}/meta-data {container_seed}/network-config"
        )
    else:
        files = f"{container_seed}/user-data {container_seed}/meta-data"

    step("Building NoCloud seed ISO (in container)")
    cmd = (
        f"sh -c 'command -v genisoimage >/dev/null || {{ apt-get update -qq && "
        f"apt-get install -y -qq --no-install-recommends genisoimage; }} && "
        f"genisoimage -quiet -output {container_seed}/sidecar-seed.iso -volid cidata "
        f"-joliet -rock {files}'"
    )
    try:
        result = ex.run(cmd, timeout=600)
    except ExecError as exc:
        raise SeedError(f"seed ISO build failed: {exc}") from exc
    if result.exit_code != 0 or not iso.is_file():
        raise SeedError(f"seed ISO build failed: {result.output}")
    ok(f"seed ISO: {iso.name} ({iso.stat().st_size / 1024:.0f} KiB)")
    return iso


def fresh_instance_id() -> str:
    """A new instance-id forces cloud-init to re-run per-configuration."""
    return f"vmdk-shrink-sidecar-{uuid.uuid4().hex[:12]}"

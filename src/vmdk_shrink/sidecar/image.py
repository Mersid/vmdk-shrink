"""Sidecar base image: download, checksum-verify, and convert to a boot VMDK.

Uses the Debian ``genericcloud`` cloud image (cloud-init baked in — that is
the whole point of choosing it over a rescue ISO). The qcow2 is downloaded
once and cached; every use re-verifies the SHA-512 against Debian's
``SHA512SUMS``. The VMDK conversion runs inside the package container
(qemu-img is already in the image), so the host needs nothing new.
"""

from __future__ import annotations

import hashlib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..console import info, ok, step
from ..executor import ExecError, Executor

SUITE = "trixie"
FLAVOR = "genericcloud"
ARCH = "amd64"
_BASE_URL = f"https://cloud.debian.org/images/cloud/{SUITE}/latest"
_CHUNK = 1024 * 1024


class ImageError(RuntimeError):
    pass


@dataclass(frozen=True)
class BaseImage:
    """Host paths of the verified qcow2 and its bootable VMDK conversion."""

    qcow2: Path
    vmdk: Path
    sha512: str


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed Debian host
        return bytes(resp.read())


def _download_streaming(url: str, dest: Path) -> str:
    """Download *url* to *dest*, returning the SHA-512 of the bytes written."""
    digest = hashlib.sha512()
    req = urllib.request.Request(url)  # noqa: S310 - fixed Debian host
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as out:  # noqa: S310
        while chunk := resp.read(_CHUNK):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def _resolve_image() -> tuple[str, str]:
    """Return (filename, sha512) from Debian's signed SHA512SUMS for the suite."""
    sums = _http_get(f"{_BASE_URL}/SHA512SUMS").decode("utf-8", errors="strict")
    needle = f"-{FLAVOR}-{ARCH}.qcow2"
    for line in sums.splitlines():
        digest, _, name = line.partition("  ")
        name = name.strip()
        if name.endswith(needle) and "/" not in name:
            return name, digest.strip()
    raise ImageError(f"no {needle} entry found in {_BASE_URL}/SHA512SUMS")


def _verified(path: Path, expected: str, what: str) -> None:
    digest = hashlib.sha512()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise ImageError(f"{what} failed checksum: expected {expected[:16]}…, got {actual[:16]}…")


def ensure_base_image(
    cache_dir: Path,
    ex: Executor,
    container_cache: str = "/work",
    seed_dir: Path | None = None,
) -> BaseImage:
    """Download (if needed) and verify the image; convert to VMDK (if needed).

    ``ex`` must be able to run commands in a container that has ``qemu-img``
    and sees *cache_dir* at *container_cache*.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    step(f"Resolving Debian {SUITE} {FLAVOR} image")
    filename, sha512 = _resolve_image()

    qcow2 = cache_dir / filename
    if qcow2.is_file():
        info(f"cached image found: {qcow2.name}")
        _verified(qcow2, sha512, "cached qcow2")
    else:
        tmp = cache_dir / (filename + ".part")
        step(f"Downloading [val]{filename}[/val] (about 0.5 GiB, once)")
        actual = _download_streaming(f"{_BASE_URL}/{filename}", tmp)
        if actual != sha512:
            tmp.unlink(missing_ok=True)
            raise ImageError("downloaded image failed checksum")
        tmp.replace(qcow2)
        ok(f"downloaded and verified ({qcow2.stat().st_size / 2**30:.2f} GiB)")

    vmdk = cache_dir / f"sidecar-base-{sha512[:12]}.vmdk"
    if vmdk.is_file():
        info(f"cached conversion: {vmdk.name}")
    else:
        step("Converting qcow2 -> monolithic-sparse VMDK (in container)")
        cmd = (
            f"qemu-img convert -O vmdk -o subformat=monolithicSparse "
            f"'{container_cache}/{filename}' '{container_cache}/{vmdk.name}'"
        )
        try:
            result = ex.run(cmd, timeout=900)
        except ExecError as exc:
            raise ImageError(f"qemu-img conversion failed: {exc}") from exc
        if result.exit_code != 0:
            raise ImageError(f"qemu-img conversion failed: {result.output}")
        _force_nocloud(ex, container_cache, vmdk.name, seed_dir=seed_dir)
        ok(f"converted: {vmdk.stat().st_size / 2**30:.2f} GiB host")

    return BaseImage(qcow2=qcow2, vmdk=vmdk, sha512=sha512)


def _force_nocloud(
    ex: Executor, container_cache: str, vmdk_name: str, seed_dir: Path | None = None
) -> None:
    """Pin NoCloud on the boot image: cfg + on-disk seed (no ISO needed).

    Debian's ``ds-identify`` silently disables all cloud-init units when its
    detection finds no recognized datasource (Workstation NAT + seed ISO is
    not in its matrix). Two fixes at image-prep time:
    - ``datasource_list: [ NoCloud ]`` in cloud.cfg.d (ds-identify honors it),
    - the seed files copied into ``/var/lib/cloud/seed/nocloud/`` — NoCloud
      reads them from there unconditionally, no ISO/disk probing involved.
    """
    step("Pinning NoCloud datasource + seed on boot image")
    copy_seed = ""
    if seed_dir is not None:
        copy_seed = (
            "mkdir -p /mnt2/var/lib/cloud/seed/nocloud\n"
            "cp /work/.seed/user-data /work/.seed/meta-data "
            "/mnt2/var/lib/cloud/seed/nocloud/\n"
        )
    script = (
        "set -e\n"
        f"qemu-nbd -c /dev/nbd1 '{container_cache}/{vmdk_name}'\n"
        "sleep 1\n"
        "partprobe /dev/nbd1 2>/dev/null || true\n"
        "sleep 1\n"
        "mknod /dev/nbd1p1 b 43 33 2>/dev/null || true\n"
        "mkdir -p /mnt2\n"
        "mkdir -p /work/.seed\n"
        "mount -o rw /dev/nbd1p1 /mnt2\n"
        "printf 'datasource_list: [ NoCloud ]\\n' > "
        "/mnt2/etc/cloud/cloud.cfg.d/99-force-nocloud.cfg\n"
        + copy_seed
        + "umount /mnt2\n"
        "qemu-nbd -d /dev/nbd1\n"
    )
    result = ex.run(script, timeout=900, check=False)
    if result.exit_code != 0:
        raise ImageError(f"NoCloud pinning failed: {result.output[-500:]}")
    ok("datasource pinned: NoCloud")




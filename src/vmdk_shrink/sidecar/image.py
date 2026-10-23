"""Sidecar base image: download, checksum-verify, and convert to a boot VMDK.

Uses the Debian ``genericcloud`` cloud image (cloud-init baked in — that is
the whole point of choosing it over a rescue ISO). The qcow2 is downloaded
once and cached; every use re-verifies the SHA-512 against Debian's
``SHA512SUMS``. The VMDK conversion runs inside the package container
(qemu-img is already in the image), so the host needs nothing new.
"""

from __future__ import annotations

import hashlib
import shlex
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..console import info, ok, step
from ..container import Container
from ..executor import ExecError, Executor
from ..nbd import NbdError, NbdSession

SUITE = "trixie"
FLAVOR = "genericcloud"
ARCH = "amd64"
_BASE_URL = f"https://cloud.debian.org/images/cloud/{SUITE}/latest"
_CHUNK = 1024 * 1024


class ImageError(RuntimeError):
    pass


class ImageCleanupError(ImageError):
    """The image may still be mounted; retain files and lock for manual recovery."""


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
    lock = cache_dir / ".image-prep.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise ImageError(
            f"image preparation already active or interrupted: {lock}; "
            "remove the lock only after confirming no preparation or sidecar VM is active"
        ) from exc
    temporary: list[Path] = []
    cleanup_safe = True
    try:
        step(f"Resolving Debian {SUITE} {FLAVOR} image")
        filename, sha512 = _resolve_image()
        qcow2 = cache_dir / filename
        if qcow2.is_file():
            info(f"cached image found: {qcow2.name}")
            _verified(qcow2, sha512, "cached qcow2")
        else:
            tmp = cache_dir / f"{filename}.{uuid.uuid4().hex}.part"
            temporary.append(tmp)
            step(f"Downloading [val]{filename}[/val] (about 0.5 GiB, once)")
            actual = _download_streaming(f"{_BASE_URL}/{filename}", tmp)
            if actual != sha512:
                raise ImageError("downloaded image failed checksum")
            tmp.replace(qcow2)
            ok(f"downloaded and verified ({qcow2.stat().st_size / 2**30:.2f} GiB)")

        vmdk = cache_dir / f"sidecar-base-{sha512[:12]}.vmdk"
        if vmdk.is_file():
            info(f"cached conversion: {vmdk.name}")
            _force_nocloud(ex, container_cache, vmdk.name, seed_dir=seed_dir)
        else:
            step("Converting qcow2 -> monolithic-sparse VMDK (in container)")
            pending = cache_dir / f"{vmdk.stem}.{uuid.uuid4().hex}.part.vmdk"
            temporary.append(pending)
            cmd = (
                "qemu-img convert -O vmdk -o subformat=monolithicSparse "
                f"{shlex.quote(f'{container_cache}/{filename}')} "
                f"{shlex.quote(f'{container_cache}/{pending.name}')}"
            )
            result = ex.run(cmd, timeout=900)
            if result.exit_code != 0:
                raise ImageError(f"qemu-img conversion failed: {result.output}")
            _force_nocloud(ex, container_cache, pending.name, seed_dir=seed_dir)
            pending.replace(vmdk)
            ok(f"converted: {vmdk.stat().st_size / 2**30:.2f} GiB host")
        return BaseImage(qcow2=qcow2, vmdk=vmdk, sha512=sha512)
    except ImageCleanupError:
        cleanup_safe = False
        raise
    except ExecError as exc:
        raise ImageError(f"image preparation failed: {exc}") from exc
    finally:
        if cleanup_safe:
            try:
                for path in temporary:
                    path.unlink(missing_ok=True)
            finally:
                lock.rmdir()


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
    mount = f"/tmp/sidecar-image-{uuid.uuid4().hex}"
    nbd = NbdSession(cast(Container, ex), index=1)
    try:
        disk = nbd.connect(f"{container_cache}/{vmdk_name}")
        script = (
            f"set -e\nmkdir -p {mount}\n"
            f"mount -o rw {disk.partition(1)} {mount}\n"
            "printf 'datasource_list: [ NoCloud ]\\n' > "
            f"{mount}/etc/cloud/cloud.cfg.d/99-force-nocloud.cfg\n"
        )
        if seed_dir is not None:
            source = f"{container_cache}/{seed_dir.name}"
            script += (
                f"mkdir -p {mount}/var/lib/cloud/seed/nocloud\n"
                f"cp {shlex.quote(source + '/user-data')} "
                f"{shlex.quote(source + '/meta-data')} {mount}/var/lib/cloud/seed/nocloud/\n"
            )
        result = ex.run(script, timeout=900, check=False)
        if result.exit_code != 0:
            raise ImageError(f"NoCloud pinning failed: {result.output[-500:]}")
    finally:
        try:
            cleanup = ex.run(
                f"if mountpoint -q {mount}; then umount {mount} || exit $?; fi; "
                f"if test -d {mount}; then rmdir {mount}; fi",
                timeout=120,
                check=False,
            )
        except ExecError as exc:
            raise ImageCleanupError(
                "cannot verify boot image unmount; retained image and lock"
            ) from exc
        if cleanup.exit_code != 0:
            raise ImageCleanupError("boot image unmount failed; container teardown is required")
        try:
            nbd.disconnect(check=False)
        except (ExecError, NbdError) as exc:
            raise ImageCleanupError(
                "boot image disconnect failed; retained image and lock"
            ) from exc
    ok("datasource pinned: NoCloud")

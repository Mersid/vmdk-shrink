"""Lifecycle of the container transport and its owned NBD attachments."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .console import info, step
from .container import IMAGE_TAG, Container, ContainerError
from .executor import Executor
from .manifest import Manifest
from .nbd import NbdDisk, NbdSession
from .paths import DEFAULT_PATHS, HostPaths
from .progress import PhaseBar
from .safety import require_offline

WORK_MOUNT = "/work"


@dataclass
class Session:
    container: Container
    nbd: NbdSession
    paths: HostPaths

    @property
    def executor(self) -> Executor:
        return self.container

    def container_path(self, host_path: Path) -> str:
        return self.container.to_container_path(host_path)

    def close(self) -> None:
        try:
            self.nbd.disconnect()
        finally:
            self.container.stop(remove=True)


@contextmanager
def open_session(
    vm_dir: Path,
    *,
    paths: HostPaths = DEFAULT_PATHS,
    dockerfile: Path | None = None,
    build: bool = True,
) -> Iterator[Session]:
    """Start the container (auto-building the image if needed) and yield a session."""
    vm_dir = Path(vm_dir).resolve()
    if build:
        df = dockerfile or default_dockerfile()
        probe = Container(((vm_dir, WORK_MOUNT),), paths=paths)
        if not probe.image_exists():
            probe.build_image(df)

    container = Container(((vm_dir, WORK_MOUNT),), paths=paths)
    step(f"Starting container [val]{IMAGE_TAG}[/val]")
    container.start()
    session = Session(container=container, nbd=NbdSession(container, index=0), paths=paths)
    try:
        yield session
    finally:
        session.close()


def default_dockerfile() -> Path:
    """The Dockerfile shipped alongside the package."""
    pkg_dir = Path(__file__).resolve().parent
    for candidate in (pkg_dir.parent.parent / "Dockerfile", pkg_dir / "Dockerfile"):
        if candidate.is_file():
            return candidate
    raise ContainerError("Dockerfile not found; pass --dockerfile explicitly")


def attach_rw(session: Session, vmdk: Path, *, unsafe_cache: bool = False) -> NbdDisk:
    """Connect a VMDK read-write and return its NBD disk handle."""
    require_offline(vmdk.parent)
    return session.nbd.connect(
        session.container_path(vmdk), read_only=False, unsafe_cache=unsafe_cache
    )


def attach_ro(session: Session, vmdk: Path, *, index: int) -> NbdDisk:
    """Connect an extra read-only replica of a VMDK (for parallel hashing)."""
    nbd = session.nbd if index == 0 else NbdSession(session.container, index=index)
    return nbd.connect(session.container_path(vmdk), read_only=True)


def info_mounts(session: Session) -> None:
    out = session.container.run("lsblk -b -o NAME,SIZE,TYPE | grep nbd || true", timeout=30)
    info("nbd devices:\n" + out.output.strip())


def hash_all(
    session: Session,
    vmdk: Path,
    *,
    out_path: Path,
    replicas: int = 4,
    partition: int = 1,
    bar: PhaseBar | None = None,
) -> Manifest:
    """Hash every file via N read-only NBD replicas of the same VMDK.

    One qemu-nbd connection caps near 100 MB/s; ``replicas`` parallel RO
    connections reach the bind-mount ceiling. Returns the parsed manifest.
    """
    from .fsops import mount_ro, unmount
    from .manifest import collect

    replicas = max(1, replicas)
    sessions: list[NbdSession] = []
    mounts: list[str] = []
    try:
        for i in range(replicas):
            nbd = NbdSession(session.container, index=i)
            disk = nbd.connect(session.container_path(vmdk), read_only=True)
            sessions.append(nbd)
            node = disk.partition(partition)
            mount = f"/mnt/data{i}"
            mount_ro(session.container, node, mount)
            mounts.append(mount)
        info(f"hashing over [val]{replicas}[/val] read-only replicas")
        return collect(
            session.container,
            mount=mounts[0],
            extra_mounts=tuple(mounts[1:]),
            out_path=out_path,
            bar=bar,
        )
    finally:
        for mount in mounts:
            unmount(session.container, mount)
        for nbd in sessions:
            nbd.disconnect()

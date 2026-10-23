"""Privileged Linux toolchain and Docker execution/lifecycle adapter.

The VM directory is bind-mounted for in-place disk access. Manifests,
checkpoints, and optional sidecar artifacts persist outside the container.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .console import info, ok, step, warn
from .execution import stream_process
from .executor import ExecError, ExecResult
from .paths import DEFAULT_PATHS, HostPaths

IMAGE_NAME = "vmdk-shrink"
IMAGE_TAG = f"{IMAGE_NAME}:toolchain-v2"
CONTAINER_PREFIX = IMAGE_NAME
NBD_LOCK_CONTAINER = f"{IMAGE_NAME}-nbd-lock"
WORK_MOUNT = "/work"


class ContainerError(RuntimeError):
    pass


class Container:
    """A long-lived privileged container plus an :class:`Executor` surface."""

    def __init__(
        self,
        mounts: tuple[tuple[Path, str], ...],
        *,
        paths: HostPaths = DEFAULT_PATHS,
        image: str = IMAGE_TAG,
        name: str | None = None,
    ) -> None:
        self._paths = paths
        self._image = image
        # One session per process: a fixed name would let a second concurrent
        # pipeline tear down the first one's container as "stale".
        self._name = name or f"{CONTAINER_PREFIX}-{os.getpid()}"
        self._mounts = mounts
        self._running = False
        self._lock_id: str | None = None

    # ------------------------------------------------------------ image ----
    def image_exists(self) -> bool:
        proc = self._docker("image", "inspect", "--format", "{{.Id}}", self._image, capture=True)
        return proc.returncode == 0

    def build_image(self, dockerfile: Path, *, force: bool = False) -> None:
        if self.image_exists() and not force:
            info(f"image [val]{self._image}[/val] already present")
            return
        step(f"Building image [val]{self._image}[/val]")
        proc = self._docker(
            "build", "-t", self._image, "-f", str(dockerfile), str(dockerfile.parent), capture=False
        )
        if proc.returncode != 0:
            raise ContainerError(f"docker build failed (rc={proc.returncode})")
        ok(f"image built: {self._image}")

    def remove_image(self) -> None:
        result = self._docker("rmi", self._image, capture=False)
        if result.returncode != 0:
            raise ContainerError(f"image removal failed: {result.stderr or result.stdout}")

    def running_status(self) -> str | None:
        """Status string if the container is running, else None."""
        proc = self._docker(
            "ps", "--filter", f"name=^/{CONTAINER_PREFIX}", "--format", "{{.Names}} {{.Status}}"
        )
        if proc.returncode != 0:
            raise ContainerError(f"cannot query Docker status: {proc.stderr or proc.stdout}")
        out = proc.stdout.strip() if proc.returncode == 0 else ""
        return out or None

    # --------------------------------------------------------- lifecycle ----
    def start(self) -> None:
        self._ensure_nbd_module()
        self._refuse_concurrent()
        lock = self._docker("create", "--name", NBD_LOCK_CONTAINER, self._image, "true")
        if lock.returncode != 0:
            raise ContainerError(
                f"NBD ownership lock unavailable ({NBD_LOCK_CONTAINER}); another operation "
                "may be active. After a crash, confirm all workers are off before "
                "manually removing the lock container. " + (lock.stderr or "")
            )
        self._lock_id = lock.stdout.strip()
        args = ["run", "-d", "--name", self._name, "--privileged", "--init"]
        for host, cont in self._mounts:
            args += ["-v", f"{host}:{cont}"]
        args += [self._image, "sleep", "infinity"]
        proc = self._docker(*args, capture=True)
        if proc.returncode != 0:
            raise ContainerError(
                f"docker run failed; NBD lock retained for manual assessment: "
                f"{(proc.stdout or '') + (proc.stderr or '')}".strip()
            )
        self._running = True
        ok(f"container [val]{self._name}[/val] running")

    def stop(self, *, remove: bool = True) -> None:
        if remove:
            proc = self._docker("rm", "-f", self._name, capture=True)
            if proc.returncode != 0:
                raise ContainerError(
                    f"container removal failed; disk may be attached: {proc.stderr}"
                )
            self._running = False
            if self._lock_id is not None:
                lock = self._docker("rm", self._lock_id)
                if lock.returncode != 0:
                    raise ContainerError("container stopped, but NBD ownership lock removal failed")
                self._lock_id = None
            info(f"container [val]{self._name}[/val] removed")
        else:
            proc = self._docker("stop", self._name, capture=True)
            if proc.returncode != 0:
                raise ContainerError(f"container stop failed: {proc.stderr}")
            self._running = False

    def __enter__(self) -> Container:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---------------------------------------------------------- exec ----
    def run(self, command: str, *, timeout: float = 120.0, check: bool = True) -> ExecResult:
        proc = self._exec(command, timeout=timeout, capture=True)
        result = ExecResult(
            command=command,
            exit_code=proc.returncode,
            output=(proc.stdout or "") + (proc.stderr or ""),
        )
        if check and result.exit_code != 0:
            raise ExecError(
                f"command failed ({result.exit_code}): {command}\n{result.output.strip()[-2000:]}"
            )
        return result

    def run_detached(self, command: str) -> None:
        """Start a process inside the container without waiting (qemu-nbd)."""
        proc = self._docker("exec", "-d", self._name, "bash", "-c", command, capture=True)
        if proc.returncode != 0:
            raise ExecError(
                f"detached exec failed: {(proc.stdout or '') + (proc.stderr or '')}".strip()
            )

    def stream(
        self, command: str, *, timeout: float = 7200.0, chunk_cb: object = None
    ) -> ExecResult:
        try:
            proc = stream_process(
                [str(self._paths.docker), "exec", self._name, "bash", "-c", command],
                timeout=timeout,
                chunk_cb=chunk_cb,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecError(
                f"command timed out after {timeout:.0f}s; guest command may still be running: "
                f"{command}"
            ) from exc
        return ExecResult(command=command, exit_code=proc.returncode, output=proc.stdout)

    # ----------------------------------------------------------- paths ----
    def to_container_path(self, host_path: Path) -> str:
        """Map a host path to its bind-mounted location inside the container."""
        for host, cont in self._mounts:
            try:
                rel = host_path.resolve().relative_to(Path(host).resolve())
            except ValueError:
                continue
            return f"{cont}/{rel.as_posix()}" if str(rel) != "." else cont
        raise ContainerError(f"path is not under any bind mount: {host_path}")

    # ---------------------------------------------------------- helpers ----
    def _exec(
        self, command: str, *, timeout: float, capture: bool
    ) -> subprocess.CompletedProcess[str]:
        return self._docker(
            "exec", self._name, "bash", "-c", command, timeout=timeout, capture=capture
        )

    def _docker(
        self,
        *args: str,
        timeout: float = 900.0,
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [str(self._paths.docker), *args]
        try:
            return subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # never raise: a hung docker exec must surface as a failed
            # ExecResult, not mask the pipeline's real error in cleanup
            raw = exc.stdout or b""
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            return subprocess.CompletedProcess(
                cmd,
                returncode=124,
                stdout=text,
                stderr=f"docker command timed out after {timeout:.0f}s",
            )

    def _refuse_concurrent(self) -> None:
        """Abort if another live session exists: two pipelines on one nbd0 is chaos."""
        proc = self._docker(
            "ps", "--filter", f"name=^/{CONTAINER_PREFIX}", "--format", "{{.Names}}"
        )
        if proc.returncode != 0:
            raise ContainerError("cannot establish whether another NBD session is running")
        live = [n for n in (proc.stdout or "").split() if n and n != self._name]
        if live:
            raise ContainerError(
                f"another pipeline is already running (container {live[0]!r}); "
                "only one vmdk-shrink session can run at a time"
            )

    def _ensure_nbd_module(self) -> None:
        """Load the nbd kernel module into Docker Desktop's WSL2 kernel.

        The module lives on the host kernel; the container only consumes
        ``/dev/nbd*``. Loading is idempotent and harmless if already present.
        """
        wsl = self._paths.wsl
        if not _on_path(wsl):
            warn("wsl not found; assuming the nbd module is already loaded")
            return
        proc = subprocess.run(
            [str(wsl), "-d", "docker-desktop", "-u", "root", "modprobe", "nbd", "max_part=31"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            warn(
                "could not load the nbd module via wsl "
                f"({(proc.stderr or proc.stdout).strip()[:200]}); continuing"
            )


def _on_path(tool: Path) -> bool:
    from shutil import which

    return which(str(tool)) is not None

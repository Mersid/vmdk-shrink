"""SSH transport to the sidecar: batch-mode OpenSSH behind a tiny interface.

:class:`SshRunner` satisfies the same ``run()`` shape as the package's
:class:`~vmdk_shrink.executor.Executor`, so guest-side operation modules can
be tested with an in-memory fake. All connections are batch mode (key only —
no interactive prompts to hang on), StrictHostKeyChecking off against a
session-scoped known_hosts file, and every call carries a timeout: a silent
guest must surface as an error, not a hang.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..executor import ExecError, ExecResult


class SshError(ExecError):
    pass


class GuestRunner(Protocol):
    """Anything that can run a shell command inside the sidecar guest."""

    def run(self, command: str, *, timeout: float = 120.0, check: bool = True) -> ExecResult: ...

    def stream(
        self, command: str, *, timeout: float = 7200.0, chunk_cb: object = None
    ) -> ExecResult: ...


@dataclass(frozen=True)
class SshConfig:
    host: str
    user: str = "debian"
    port: int = 22
    key: Path | None = None
    known_hosts: Path | None = None
    connect_timeout: float = 10.0


def require_ssh_client() -> str:
    """Locate the OpenSSH client (Windows ships one; PATH covers it)."""
    ssh = shutil.which("ssh")
    if ssh is None:
        raise SshError("ssh client not found on PATH (Windows ships one under System32\\OpenSSH)")
    return ssh


class SshRunner:
    """Run commands in the guest; mirror the Executor run() contract."""

    def __init__(self, config: SshConfig) -> None:
        self._config = config
        self._binary = require_ssh_client()

    def _argv(self, command: str) -> list[str]:
        argv = [self._binary, "-o", "BatchMode=yes", "-o", "LogLevel=ERROR"]
        if self._config.key is not None:
            argv += ["-i", str(self._config.key)]
        if self._config.known_hosts is not None:
            argv += [
                "-o",
                f"UserKnownHostsFile={self._config.known_hosts}",
                "-o",
                "StrictHostKeyChecking=no",
            ]
        argv += [
            "-o",
            f"ConnectTimeout={int(self._config.connect_timeout)}",
            "-p",
            str(self._config.port),
            f"{self._config.user}@{self._config.host}",
            "--",
            command,
        ]
        return argv

    def run(self, command: str, *, timeout: float = 120.0, check: bool = True) -> ExecResult:
        argv = self._argv(command)
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no user input
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SshError(f"ssh command timed out after {timeout:.0f}s: {command[:120]}") from exc
        except OSError as exc:
            raise SshError(f"ssh failed to start: {exc}") from exc
        result = ExecResult(
            command=command,
            exit_code=proc.returncode,
            output=(proc.stdout or "") + (proc.stderr or ""),
        )
        if check and result.exit_code != 0:
            raise SshError(
                f"ssh command failed (rc={result.exit_code}): {command[:120]}\n{result.output}"
            )
        return result

    def stream(
        self, command: str, *, timeout: float = 7200.0, chunk_cb: object = None
    ) -> ExecResult:
        """Stream guest stdout line-by-line (Executor.stream contract).

        Same shape as :meth:`vmdk_shrink.container.Container.stream`, so
        ``manifest.collect`` runs against a guest unchanged.
        """
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no user input
            self._argv(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        collected: list[str] = []
        deadline = time.monotonic() + timeout
        for line in proc.stdout:
            collected.append(line)
            if callable(chunk_cb):
                chunk_cb(line)
            if time.monotonic() > deadline:
                proc.kill()
                raise SshError(f"ssh stream timed out after {timeout:.0f}s: {command[:120]}")
        proc.wait(timeout=30)
        return ExecResult(
            command=command, exit_code=proc.returncode, output="".join(collected)
        )

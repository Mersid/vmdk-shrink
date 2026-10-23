"""Command-specific host requirements, without starting VMs or containers."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import DEFAULT_PATHS, HostPaths


@dataclass(frozen=True)
class Requirement:
    name: str
    executable: str
    available: bool
    detail: str


def check_requirements(
    command: str,
    *,
    mode: str = "nbd",
    hash_via: str = "container",
    plain_compact: bool = False,
    paths: HostPaths = DEFAULT_PATHS,
) -> list[Requirement]:
    if command not in ("shrink", "compact", "hash", "inspect"):
        raise ValueError("expected shrink, compact, hash, or inspect")
    tools: list[tuple[str, Path | str]] = []
    if command in ("shrink", "compact"):
        tools.append(("VMware disk utility", paths.vdiskmanager))
    docker = command in ("shrink", "hash") or (command == "compact" and not plain_compact)
    if docker:
        tools.extend((("Docker", paths.docker), ("WSL (NBD module loader)", paths.wsl)))
    sidecar = (command == "shrink" and (mode == "sidecar" or hash_via == "sidecar")) or (
        command == "hash" and hash_via == "sidecar"
    )
    if sidecar:
        tools.extend(
            (
                ("VMware VM control", paths.vmrun),
                ("OpenSSH", "ssh"),
                ("SSH key generation", "ssh-keygen"),
                ("Windows key ACLs", "icacls"),
                ("Network discovery", "ipconfig"),
                ("Neighbor discovery", "arp"),
                ("Network probing", "ping"),
            )
        )
    results = []
    for name, tool in tools:
        resolved = shutil.which(str(tool))
        exists = resolved is not None
        detail = resolved or f"not found: {tool}"
        if exists and name == "Docker":
            try:
                proc = subprocess.run(
                    [str(tool), "info", "--format", "{{.OSType}}"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                exists = proc.returncode == 0 and proc.stdout.strip() == "linux"
                if not exists:
                    detail = "Docker engine unavailable or not using Linux containers"
            except (OSError, subprocess.TimeoutExpired) as exc:
                exists, detail = False, str(exc)
        results.append(Requirement(name, str(tool), exists, detail))
    return results


def require_prerequisites(
    command: str,
    *,
    mode: str = "nbd",
    hash_via: str = "container",
    plain_compact: bool = False,
    paths: HostPaths = DEFAULT_PATHS,
) -> None:
    missing = [
        r
        for r in check_requirements(
            command, mode=mode, hash_via=hash_via, plain_compact=plain_compact, paths=paths
        )
        if not r.available
    ]
    if missing:
        raise RuntimeError("Missing prerequisites: " + "; ".join(r.detail for r in missing))

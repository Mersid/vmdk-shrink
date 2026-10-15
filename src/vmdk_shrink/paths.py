"""Locations of VMware Workstation tools and related host paths.

Everything is overridable via :class:`HostPaths` so tests can point at fakes,
but the defaults reflect a standard Workstation install on Windows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_program_files() -> Path:
    return Path(os.environ.get("ProgramW6432", r"C:\Program Files"))


@dataclass(frozen=True)
class HostPaths:
    """Filesystem locations of VMware tools and VM-related state."""

    workstation_dir: Path = field(
        default_factory=lambda: _default_program_files() / "VMware" / "VMware Workstation"
    )
    vmrun: Path = field(init=False)
    vdiskmanager: Path = field(init=False)
    dhcp_leases: Path = Path(r"C:\ProgramData\VMware\vmnetdhcp.leases")

    def __post_init__(self) -> None:
        object.__setattr__(self, "vmrun", self.workstation_dir / "vmrun.exe")
        object.__setattr__(self, "vdiskmanager", self.workstation_dir / "vmware-vdiskmanager.exe")

    def require_tools(self) -> list[Path]:
        """Return the list of tool paths that are missing (empty list = all good)."""
        return [p for p in (self.vmrun, self.vdiskmanager) if not p.is_file()]


DEFAULT_PATHS = HostPaths()

"""Host executable locations. Command-specific requirements live in prerequisites."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_program_files() -> Path:
    return Path(os.environ.get("ProgramW6432", r"C:\Program Files"))


@dataclass(frozen=True)
class HostPaths:
    """Filesystem locations of host-side tools and VM-related state."""

    docker: Path = Path("docker")
    wsl: Path = Path("wsl")
    workstation_dir: Path = field(
        default_factory=lambda: _default_program_files() / "VMware" / "VMware Workstation"
    )
    vdiskmanager: Path = field(init=False)
    vmrun: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "vdiskmanager", self.workstation_dir / "vmware-vdiskmanager.exe")
        object.__setattr__(self, "vmrun", self.workstation_dir / "vmrun.exe")

    def require_tools(self) -> list[Path]:
        """Return missing tool paths (empty list = all good)."""
        return [p for p in (self.vdiskmanager, self.vmrun) if not p.is_file()]


DEFAULT_PATHS = HostPaths()

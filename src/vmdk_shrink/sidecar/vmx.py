"""Sidecar VMX generation: a minimal BIOS VM with the data disk attached.

The sidecar is disposable. It boots from its own cached cloud-image disk;
the data VMDK is attached by absolute path as a second SCSI disk — VMware
takes an exclusive lock on it for the duration, which is itself an interlock
against two writers. NAT networking keeps the guest off the LAN; SSH goes to
the NAT-internal address obtained via ``vmrun getGuestIPAddress``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

VMX_TEMPLATE = """\
.encoding = "windows-1252"
config.version = "8"
virtualHW.version = "{hw_version}"
virtualHW.productCompatibility = "hosted"
displayName = "vmdk-shrink-sidecar"
guestOS = "debian12-64"
firmware = "bios"
memsize = "{memsize}"
numvcpus = "{cpus}"
sata0.present = "TRUE"
sata0.pciSlotNumber = "16"
sata0:0.present = "TRUE"
sata0:0.fileName = "{boot_disk}"
sata0:1.present = "TRUE"
sata0:1.fileName = "{data_disk}"
{seed_cdrom}
ethernet0.present = "TRUE"
ethernet0.connectionType = "nat"
ethernet0.virtualDev = "{eth_dev}"
ethernet0.addressType = "generated"
ethernet0.wakeOnPcktRcv = "FALSE"
ethernet0.startConnected = "TRUE"
ethernet0.pciSlotNumber = "33"
pciBridge0.present = "TRUE"
pciBridge4.present = "TRUE"
pciBridge4.virtualDev = "pcieRootPort"
pciBridge4.functions = "8"
pciBridge4.pciSlotNumber = "21"
pciBridge5.present = "TRUE"
pciBridge5.virtualDev = "pcieRootPort"
pciBridge5.functions = "8"
pciBridge5.pciSlotNumber = "22"
pciBridge6.present = "TRUE"
pciBridge6.virtualDev = "pcieRootPort"
pciBridge6.functions = "8"
pciBridge6.pciSlotNumber = "23"
pciBridge7.present = "TRUE"
pciBridge7.virtualDev = "pcieRootPort"
pciBridge7.functions = "8"
pciBridge7.pciSlotNumber = "24"
floppy0.present = "FALSE"
serial0.present = "TRUE"
serial0.fileType = "file"
serial0.fileName = "{serial_log}"
serial0.yieldOnMsrRead = "TRUE"
parallel0.present = "FALSE"
usb.present = "FALSE"
sound.present = "FALSE"
tools.syncTime = "TRUE"
powerType.powerOff = "soft"
powerType.powerOn = "soft"
powerType.suspend = "soft"
powerType.reset = "soft"
"""


@dataclass(frozen=True)
class SidecarVmxSpec:
    """VMX inputs; seed_iso is legacy opt-in, normal boots use an on-disk seed."""

    boot_disk: Path
    data_disk: Path
    seed_iso: Path = Path("")
    serial_log: Path = Path("")
    memsize_mb: int = 2048
    cpus: int = 2
    scsi_dev: str = "lsilogic"  # Legacy compatibility; disks now use SATA.
    eth_dev: str = "vmxnet3"
    hw_version: str = "22"

    def render(self) -> str:
        seed_cdrom = 'ide1:0.present = "FALSE"'
        if self.seed_iso != Path(""):
            seed_cdrom = (
                'ide1:0.present = "TRUE"\n'
                f'ide1:0.fileName = "{self.seed_iso}"\n'
                'ide1:0.deviceType = "cdrom-image"\n'
                'ide1:0.startConnected = "TRUE"'
            )
        return VMX_TEMPLATE.format(
            memsize=self.memsize_mb,
            cpus=self.cpus,
            eth_dev=self.eth_dev,
            hw_version=self.hw_version,
            boot_disk=self.boot_disk,
            data_disk=self.data_disk,
            seed_cdrom=seed_cdrom,
            serial_log=self.serial_log,
        )

    def write(self, vmx_path: Path) -> Path:
        vmx_path.parent.mkdir(parents=True, exist_ok=True)
        vmx_path.write_text(self.render(), encoding="windows-1252")
        return vmx_path

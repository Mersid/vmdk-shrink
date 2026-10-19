"""Guest IP discovery without VMware Tools.

``vmrun getGuestIPAddress`` requires open-vm-tools, which the stock Debian
cloud image does not carry. Instead: read the guest MAC from the VMX
(``ethernet0.generatedAddress``), ping-sweep the VMnet8 NAT subnet, and match
the ARP entry. A lease implies an ARP entry, so this finds a booted,
DHCP-configured guest within one sweep (~seconds, parallel).
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from ..console import info, step
from ..executor import ExecError


class IpDiscoveryError(RuntimeError):
    pass


_MAC_LINE = re.compile(
    r"(?P<ip>\d+\.\d+\.\d+\.\d+)\s+(?P<mac>[0-9a-fA-F-]{17})", re.IGNORECASE
)


def guest_mac_from_vmx(vmx: Path) -> str:
    """The NIC MAC the VM will present (generated-address convention)."""
    text = vmx.read_text(encoding="windows-1252", errors="replace")
    for key in ("ethernet0.generatedAddress", "ethernet0.address"):
        match = re.search(rf'{key}\s*=\s*"([^"]+)"', text)
        if match and match.group(1).lower() not in ("", "auto"):
            return match.group(1).lower().replace("-", ":")
    raise IpDiscoveryError(f"no ethernet0.generatedAddress in {vmx}")


def _arp_table() -> dict[str, str]:
    """MAC -> IP from the Windows ARP cache (interfaces considered jointly)."""
    out = subprocess.run(  # noqa: S603 - fixed argv
        ["arp", "-a"], capture_output=True, text=True, timeout=30, check=False
    ).stdout
    table: dict[str, str] = {}
    for match in _MAC_LINE.finditer(out):
        mac = match.group("mac").replace("-", ":").lower()
        if mac.startswith("00:50:56") or mac.startswith("ff:ff:"):
            continue  # VMware host adapters and broadcast
        table[mac] = match.group("ip")
    return table


def _ping_sweep(subnet: str, timeout_ms: int = 400) -> None:
    """Ping the subnet's .1..254 concurrently-ish (fast fire-and-forget)."""
    network = ipaddress.IPv4Network(subnet)
    hosts = [str(h) for h in network.hosts()]
    # Windows ping: cheap, one per address; sequential with a tight timeout is
    # too slow for 254 hosts, so spawn in chunks via start //B.
    chunk = " & ".join(f"ping -n 1 -w {timeout_ms} {ip} >nul 2>&1" for ip in hosts)
    subprocess.run(  # noqa: S603 - fixed argv, no user input
        ["cmd", "/c", chunk],
        capture_output=True,
        timeout=max(30, timeout_ms * len(hosts) // 1000 + 30),
        check=False,
    )


def guest_mac_from_log(vmx: Path) -> str:
    """The MAC the running VM actually presents (vmware.log is authoritative)."""
    log = vmx.parent / "vmware.log"
    if log.is_file():
        match = re.search(
            r"Ethernet0 MAC Address:\s*([0-9a-fA-F:]{17})", log.read_text(errors="replace")
        )
        if match:
            return match.group(1).lower()
    return guest_mac_from_vmx(vmx)


def wait_for_guest_ip(
    vmx: Path,
    *,
    subnet: str,
    attempts: int = 40,
    delay_s: float = 15.0,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """Poll until the guest MAC appears in the subnet's ARP cache."""
    if sleep is None:
        sleep = time.sleep
    mac = guest_mac_from_log(vmx)
    for attempt in range(1, attempts + 1):
        table = _arp_table()
        if mac in table:
            ip = table[mac]
            info(f"guest discovered at [val]{ip}[/val] (MAC {mac}, attempt {attempt})")
            return ip
        step(f"guest not in ARP yet ({attempt}/{attempts}); sweeping {subnet}")
        _ping_sweep(subnet)
        sleep(delay_s)
    raise IpDiscoveryError(
        f"guest MAC {mac} never appeared in {subnet} ARP cache after {attempts} attempts"
    )


def vmnet8_subnet() -> str:
    """The VMnet8 (NAT) subnet, read from the host's IP configuration."""
    out = subprocess.run(  # noqa: S603 - fixed argv
        ["ipconfig"], capture_output=True, text=True, timeout=30, check=False
    ).stdout
    current_adapter = ""
    for line in out.splitlines():
        if "adapter" in line:
            current_adapter = line
        match = re.search(r"IPv4.*?:\s*(\d+\.\d+\.\d+\.\d+)", line)
        if match and "VMnet8" in current_adapter:
            network = ipaddress.IPv4Network(
                match.group(1) + "/24", strict=False
            )
            return str(network)
    raise ExecError("VMnet8 adapter not found; NAT networking required for sidecar mode")

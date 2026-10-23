"""Guest IP discovery without VMware Tools.

``vmrun getGuestIPAddress`` requires open-vm-tools, which the stock Debian
cloud image does not carry. Instead: read the guest MAC from the VMX
(``ethernet0.generatedAddress``), ping-sweep the VMnet8 NAT subnet, and match
the ARP entry. A lease implies an ARP entry, so this finds a booted,
DHCP-configured guest within one sweep (~seconds, parallel).
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..console import info, step
from ..executor import ExecError


class IpDiscoveryError(RuntimeError):
    pass


_MAC_LINE = re.compile(r"(?P<ip>\d+\.\d+\.\d+\.\d+)\s+(?P<mac>[0-9a-fA-F-]{17})", re.IGNORECASE)


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
    """Ping the subnet hosts (best effort).

    ``cmd /c`` accepts ~8191 characters; one giant &-joined line silently
    dies with "input line too long" and never sends a single ping, so the
    sweep is chunked well under the limit.
    """
    network = ipaddress.IPv4Network(subnet)
    if network.num_addresses > 4096:
        raise IpDiscoveryError(f"refusing an unbounded ARP sweep of {subnet}; check DHCP leases")
    hosts = [str(h) for h in network.hosts()]
    per_cmd = 40
    for i in range(0, len(hosts), per_cmd):
        chunk = " & ".join(
            f"ping -n 1 -w {timeout_ms} {ip} >nul 2>&1" for ip in hosts[i : i + per_cmd]
        )
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            # sweep is best-effort; lease lookup carries discovery
            subprocess.run(  # noqa: S603 - fixed argv, no user input
                ["cmd", "/c", chunk],
                capture_output=True,
                timeout=max(30, timeout_ms * per_cmd // 1000 + 15),
                check=False,
            )


LEASES = Path(r"C:\ProgramData\VMware\vmnetdhcp.leases")


def guest_ip_from_leases(mac: str, leases: Path = LEASES) -> str | None:
    """Authoritative MAC -> IP from vmnetdhcp's lease database.

    The ARP sweep cannot see a quiet guest: Windows only caches ARP entries
    for hosts it has talked to, and a freshly booted cloud image may stay
    silent long enough to starve every sweep (observed 2026-10-21: a healthy
    guest with a live DHCP lease never appeared in the cache). The lease file
    answers the question directly.

    Two traps, both hit in testing: the file is rewritten out of
    chronological order (a stale lease can sit after a fresh one), and old
    leases linger after expiry. So: drop expired leases, then take the one
    with the newest ``starts`` timestamp.
    """
    if not leases.is_file():
        return None
    text = leases.read_text(encoding="utf-8", errors="replace")
    now = datetime.now(UTC).strftime("%Y/%m/%d %H:%M:%S")
    best_start, best_ip = "", None
    for match in re.finditer(r"lease\s+(\d+\.\d+\.\d+\.\d+)\s*\{([^}]*)\}", text):
        ip, body = match.group(1), match.group(2)
        if mac.lower() not in body.lower():
            continue
        starts = _lease_time(body, "starts")
        ends = _lease_time(body, "ends")
        if ends and ends < now:
            continue  # expired lease from a previous boot
        if starts and starts >= best_start:
            best_start, best_ip = starts, ip
    return best_ip


def _lease_time(body: str, key: str) -> str:
    """``key`` timestamp from a lease body, normalized for lexical compare."""
    match = re.search(
        rf"{key}\s+(?:\d+\s+)?(\d{{4}}/\d{{2}}/\d{{2}} \d{{2}}:\d{{2}}:\d{{2}})", body
    )
    return match.group(1) if match else ""


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
        leased = guest_ip_from_leases(mac)
        if leased and ipaddress.IPv4Address(leased) in ipaddress.IPv4Network(subnet):
            info(f"guest discovered at [val]{leased}[/val] (DHCP lease, MAC {mac})")
            return leased
        table = _arp_table()
        if mac in table and ipaddress.IPv4Address(table[mac]) in ipaddress.IPv4Network(subnet):
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
    )
    if out.returncode != 0:
        raise ExecError("could not read host IP configuration for VMnet8")
    current_adapter = ""
    address = ""
    for line in out.stdout.splitlines():
        if line and not line[0].isspace():
            current_adapter = line
            address = ""
        match = re.search(r"IPv4.*?:\s*(\d+\.\d+\.\d+\.\d+)", line)
        if match and "VMnet8" in current_adapter:
            address = match.group(1)
        mask = re.search(r"Subnet Mask.*?:\s*(\d+\.\d+\.\d+\.\d+)", line)
        if mask and address and "VMnet8" in current_adapter:
            try:
                return str(ipaddress.IPv4Network(f"{address}/{mask.group(1)}", strict=False))
            except ValueError as exc:
                raise ExecError("invalid VMnet8 address or subnet mask") from exc
    raise ExecError("VMnet8 IPv4 address/netmask not found; NAT networking required")

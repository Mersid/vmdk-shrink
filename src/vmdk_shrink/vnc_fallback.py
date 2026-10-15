"""VNC fallback: type a bootstrap command into a live system's console.

Used only when ISO-level patching (autorun) is unavailable — e.g. GParted Live.
VMware's VNC server does not synthesize Shift: shifted characters must be sent
as explicit shift-down + unshifted key + shift-up (learned the hard way).
"""

from __future__ import annotations

import time
from typing import Protocol

from vncdotool import api

from .console import ok, step

SHIFTED: dict[str, str] = {
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "-",
    "+": "=",
    "{": "[",
    "}": "]",
    "|": "\\",
    ":": ";",
    '"': "'",
    "<": ",",
    ">": ".",
    "?": "/",
    "~": "`",
}


class VncClient(Protocol):
    """The slice of the vncdotool client API we drive."""

    timeout: float

    def keyPress(self, key: str) -> None: ...  # noqa: N802 (vncdotool API name)

    def keyDown(self, key: str) -> None: ...  # noqa: N802

    def keyUp(self, key: str) -> None: ...  # noqa: N802

    def disconnect(self) -> None: ...


def type_text(client: VncClient, text: str, *, per_char_delay: float = 0.06) -> None:
    """Type ``text`` on a vncdotool client, handling shift chars explicitly."""
    for ch in text:
        if ch == " ":
            client.keyPress("space")
        elif ch.isupper():
            client.keyDown("shift")
            client.keyPress(ch.lower())
            client.keyUp("shift")
        elif ch in SHIFTED:
            client.keyDown("shift")
            client.keyPress(SHIFTED[ch])
            client.keyUp("shift")
        else:
            client.keyPress(ch)
        time.sleep(per_char_delay)


def run_line(
    client: VncClient, command: str, *, settle: float = 2.0, per_char_delay: float = 0.06
) -> None:
    """Clear the line (Ctrl-U), type the command, press Enter."""
    client.keyPress("ctrl-u")
    time.sleep(0.2)
    type_text(client, command, per_char_delay=per_char_delay)
    client.keyPress("enter")
    time.sleep(settle)


def connect_vnc(vnc_port: int, password: str) -> VncClient:
    client: VncClient = api.connect(f"localhost::{vnc_port}", password=password)
    client.timeout = 120
    return client


def bootstrap_gparted(vnc_port: int, password: str, root_password: str) -> None:
    """Drive the GParted Live boot dialogs and set up SSH from the console.

    Dialog sequence (proven): keymap dialog -> Enter, language [33] -> Enter,
    mode prompt -> '2' (command line) + Enter. Then a single shell line does
    everything. This is the *only* place manual VNC typing is ever used.
    """
    step("VNC fallback: driving GParted boot dialogs")
    client = connect_vnc(vnc_port, password)
    try:
        time.sleep(2)
        client.keyPress("return")  # "Don't touch keymap"
        time.sleep(15)
        client.keyPress("return")  # [33] US English
        time.sleep(10)
        client.keyPress("2")  # Enter command line prompt
        time.sleep(1)
        client.keyPress("return")
        time.sleep(12)
        cmd = (
            "printf 'ALL: ALL\\n' > /etc/hosts.allow; "
            "printf '' > /etc/hosts.deny; "
            f"echo 'root:{root_password}' | chpasswd; "
            "mkdir -p /run/sshd; /usr/sbin/sshd; echo BOOTSTRAP-DONE"
        )
        run_line(client, cmd, settle=6.0)
        ok("VNC bootstrap issued (sshd should be up now)")
    finally:
        client.disconnect()

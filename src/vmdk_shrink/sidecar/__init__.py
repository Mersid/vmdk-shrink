"""Sidecar mode: a disposable Debian cloud-image VM that owns the block device.

The sidecar boots from its own cached disk; the data VMDK is *attached* to it
(never copied), so the whole mode keeps the 1x footprint. VMware does all
VMDK I/O natively — no 9p bridge, no custom format code. cloud-init (NoCloud
seed) configures boot plumbing only; every operation is driven over SSH with
gates before and after, so nothing destructive runs at boot time.
"""

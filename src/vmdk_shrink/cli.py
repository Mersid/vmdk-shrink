"""``vmdk`` command line: subcommand style, Docker/Git-like."""

from __future__ import annotations

from pathlib import Path

import typer

from .console import console, err, info, ok, warn

app = typer.Typer(
    name="vmdk",
    help="Losslessly shrink or compact VMware ext4 VMDKs from Windows.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
helper_app = typer.Typer(help="Build/boot the headless Linux helper VM.", no_args_is_help=True)
app.add_typer(helper_app, name="helper")


def _fail(msg: str) -> None:
    err(msg)
    raise typer.Exit(code=1)


# ---------------------------------------------------------------- helper ----
@helper_app.command("build")
def helper_build(
    vm_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="VM directory (contains the .vmx/.vmdk files)"
    ),
    attach: list[Path] = typer.Option(
        [], "--attach", help="Target VMDK(s) to attach to the helper (repeatable)"
    ),
    iso: Path | None = typer.Option(
        None, "--iso", help="Use a specific live ISO instead of downloading SystemRescue"
    ),
    no_boot: bool = typer.Option(
        False, "--no-boot", help="Create files only; do not boot the helper"
    ),
    boot_only: bool = typer.Option(
        False, "--boot-only", help="Boot an existing helper without re-creating assets"
    ),
) -> None:
    """Download/patch the live ISO, write the helper VMX, (optionally) boot + verify SSH."""
    from .helper_session import ensure_assets

    attach = [a.resolve() for a in attach]
    for a in attach:
        if not a.is_file():
            _fail(f"attached disk not found: {a}")
    assets, guest = ensure_assets(
        vm_dir.resolve(), attach=tuple(attach), local_iso=iso, boot=not no_boot
    )
    ok(f"helper assets ready: {assets.vmx}")
    if guest is not None:
        probe = guest.run("lsblk -d -o NAME,SIZE | tail -n +2")
        info("guest sees disks:")
        console.print(probe.output)
        guest.close()


@helper_app.command("down")
def helper_down(
    vm_dir: Path = typer.Argument(..., exists=True, file_okay=False),
) -> None:
    """Hard-stop the helper VM if it is running."""
    from .helper_session import HELPER_NAME, shutdown_helper

    vmx = vm_dir.resolve() / f"{HELPER_NAME}.vmx"
    if not vmx.is_file():
        _fail(f"no helper VMX at {vmx}")
    shutdown_helper(vmx, None)
    ok("helper stopped")


# --------------------------------------------------------------- inspect ----
@app.command()
def inspect(vmdk: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """Report a VMDK's capacity, extents, and truly-allocated (data) bytes."""
    from rich.table import Table

    from .vmdk_parser import extent_list, grain_stats

    stats = grain_stats(vmdk)
    extents = extent_list(vmdk)
    table = Table(title=f"{vmdk.name}", show_header=False, box=None, padding=(0, 2))
    table.add_row("capacity", f"{stats.capacity_bytes / 2**30:.1f} GiB")
    table.add_row("extents", str(len(extents)))
    table.add_row("allocated (data)", f"[val]{stats.allocated_bytes / 2**30:.1f} GiB[/val]")
    host = vmdk.stat().st_size + sum(
        p.stat().st_size for p in vmdk.parent.glob(vmdk.with_suffix("").name + "-s*.vmdk")
    )
    table.add_row("host footprint", f"{host / 2**30:.1f} GiB")
    table.add_row(
        "compact ceiling",
        f"{(host - stats.allocated_bytes) / 2**30:.2f} GiB reclaimable by -k alone",
    )
    console.print(table)


# ---------------------------------------------------------------- status ----
@app.command()
def status(vm_dir: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    """Show running VMs, lock dirs, and per-disk host footprints in a VM dir."""
    from .vmrun import Vmrun

    for vm in Vmrun().list():
        ok(f"running: {vm.vmx}")
    for lock in vm_dir.glob("*.lck"):
        warn(f"stale lock dir: {lock.name}")
    disks = sorted(vm_dir.glob("*.vmdk"))
    if not disks:
        info("no .vmdk files here")
        return
    for d in disks:
        host = d.stat().st_size + sum(
            p.stat().st_size for p in vm_dir.glob(d.with_suffix("").name + "-s*.vmdk")
        )
        console.print(f"  {d.name}: {host / 2**30:.1f} GiB")


# --------------------------------------------------------------- compact ----
@app.command()
def compact(
    vm_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="VM directory"),
    vmdk: Path | None = typer.Option(
        None, "--vmdk", help="Specific data VMDK (default: largest split-sparse)"
    ),
    boot: Path | None = typer.Option(
        None, "--boot", help="Boot-disk VMDK to zerofree+compact alongside"
    ),
    no_zerofree: bool = typer.Option(
        False, "--no-zerofree", help="Skip booting the helper; plain -k compact only"
    ),
    keep_running: bool = typer.Option(
        False, "--keep-running", help="Leave the helper VM booted afterwards"
    ),
) -> None:
    """Reclaim host space WITHOUT touching the filesystem (zerofree + -k compact).

    Capacity is not reduced; a running guest can still expand the disk again.
    """
    from .compact import compact_disk

    data = vmdk or _default_data_vmdk(vm_dir)
    try:
        compact_disk(
            data,
            extra_vmdks=(boot,) if boot else (),
            zerofree_fs=not no_zerofree,
            keep_running=keep_running,
        )
    except Exception as exc:  # noqa: BLE001
        _fail(str(exc))


# ---------------------------------------------------------------- shrink ----
@app.command()
def shrink(
    vm_dir: Path = typer.Argument(..., exists=True, file_okay=False),
    target_gib: int = typer.Argument(..., min=1, help="New filesystem size in GiB"),
    vmdk: Path | None = typer.Option(
        None, "--vmdk", help="Specific data VMDK (default: largest split-sparse)"
    ),
    keep_running: bool = typer.Option(
        False, "--keep-running", help="Leave the helper VM booted afterwards"
    ),
) -> None:
    """Full lossless shrink: fs + partition + VMDK capacity, with hash verification."""
    from .shrink import shrink as run_shrink

    try:
        code = run_shrink(vm_dir.resolve(), target_gib, data_vmdk=vmdk, keep_running=keep_running)
    except Exception as exc:  # noqa: BLE001
        _fail(str(exc))
    if code != 0:
        raise typer.Exit(code=code)


def _default_data_vmdk(vm_dir: Path) -> Path:
    """The largest split-sparse VMDK in a directory (the data disk)."""
    candidates = [
        d for d in vm_dir.glob("*.vmdk") if list(vm_dir.glob(d.with_suffix("").name + "-s*.vmdk"))
    ]
    if not candidates:
        _fail(f"no split-sparse VMDK found in {vm_dir}")
    return max(
        candidates, key=lambda d: len(list(vm_dir.glob(d.with_suffix("").name + "-s*.vmdk")))
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()

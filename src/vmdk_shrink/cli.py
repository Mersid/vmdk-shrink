"""``vmdk`` command line: subcommand style, Docker/Git-like."""

from __future__ import annotations

from pathlib import Path

import typer

from .console import console, err, info, ok, warn

app = typer.Typer(
    name="vmdk",
    help="Losslessly shrink or compact VMware ext4 VMDKs using a Docker helper.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
image_app = typer.Typer(help="Build/remove the container image.", no_args_is_help=True)
app.add_typer(image_app, name="image")


def _fail(msg: str) -> None:
    err(msg)
    raise typer.Exit(code=1)


# ---------------------------------------------------------------- image ----
@image_app.command("build")
def image_build(
    dockerfile: Path = typer.Option(
        None, "--dockerfile", help="Dockerfile to build from (default: the packaged one)"
    ),
    force: bool = typer.Option(False, "--force", help="Rebuild even if the image exists"),
) -> None:
    """Build the container image (auto-run by shrink/compact when missing)."""
    from .container import Container
    from .session import default_dockerfile

    df = dockerfile or default_dockerfile()
    if not df.is_file():
        _fail(f"Dockerfile not found: {df}")
    c = Container(())
    c.build_image(df, force=force)


@image_app.command("rm")
def image_rm() -> None:
    """Remove the container image (full cleanup; ``docker rm`` handles the rest)."""
    from .container import Container

    Container(()).remove_image()
    ok("image removed")


@image_app.command("ls")
def image_ls() -> None:
    """Show whether the image is present."""
    from .container import IMAGE_TAG, Container

    if Container(()).image_exists():
        ok(f"present: {IMAGE_TAG}")
    else:
        warn(f"not built: {IMAGE_TAG}")


# --------------------------------------------------------------- inspect ----
@app.command()
def inspect(vmdk: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """Report a VMDK's capacity, extents, and truly-allocated (data) bytes."""
    from rich.table import Table

    from .vmdk_parser import extent_list, grain_stats, host_footprint_bytes

    stats = grain_stats(vmdk)
    extents = extent_list(vmdk)
    table = Table(title=f"{vmdk.name}", show_header=False, box=None, padding=(0, 2))
    table.add_row("capacity", f"{stats.capacity_bytes / 2**30:.1f} GiB")
    table.add_row("extents", str(len(extents)))
    table.add_row("allocated (data)", f"[val]{stats.allocated_bytes / 2**30:.1f} GiB[/val]")
    table.add_row("host footprint", f"{host_footprint_bytes(vmdk) / 2**30:.1f} GiB")
    table.add_row(
        "compact ceiling",
        f"{(host_footprint_bytes(vmdk) - stats.allocated_bytes) / 2**30:.2f} GiB "
        "reclaimable by -k alone",
    )
    console.print(table)


# ---------------------------------------------------------------- status ----
@app.command()
def status(vm_dir: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    """Show the helper container state, lock dirs, and per-disk host footprints."""
    from .container import Container

    status_line = Container(()).running_status()
    if status_line:
        ok(f"container running: {status_line}")
    else:
        info("helper container not running")

    for lock in vm_dir.glob("*.lck"):
        warn(f"stale lock dir: {lock.name}")
    disks = sorted(vm_dir.glob("*.vmdk"))
    if not disks:
        info("no .vmdk files here")
        return
    from .vmdk_parser import host_footprint_bytes

    for d in disks:
        console.print(f"  {d.name}: {host_footprint_bytes(d) / 2**30:.1f} GiB")


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
        False, "--no-zerofree", help="Skip the container; plain -k compact only"
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
            extra_vmdks=(boot.resolve(),) if boot else (),
            zerofree_fs=not no_zerofree,
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
    replicas: int = typer.Option(
        4, "--replicas", min=1, max=8, help="Parallel read-only NBD connections for hashing"
    ),
    resume: bool = typer.Option(
        False, "--resume", help="Reuse an existing baseline manifest (after a crash)"
    ),
    mode: str = typer.Option(
        "nbd",
        "--mode",
        help="fs-work transport: nbd = in-place via container qemu-nbd (1x); "
        "sidecar = disposable Debian VM with the disk attached (1x, no 9p)",
    ),
) -> None:
    """Full lossless shrink: fs + partition + VMDK capacity, with hash verification."""
    from .shrink import shrink as run_shrink

    if mode not in ("nbd", "sidecar"):
        _fail(f"unknown mode {mode!r} (expected nbd or sidecar)")
    try:
        code = run_shrink(
            vm_dir.resolve(),
            target_gib,
            data_vmdk=vmdk,
            replicas=replicas,
            resume=resume,
            mode=mode,
        )
    except Exception as exc:  # noqa: BLE001
        _fail(str(exc))
    if code != 0:
        raise typer.Exit(code=code)


# --------------------------------------------------------------- sidecar ----
sidecar_app = typer.Typer(help="Sidecar VM maintenance.", no_args_is_help=True)
app.add_typer(sidecar_app, name="sidecar")


@sidecar_app.command("teardown")
def sidecar_teardown(
    vm_dir: Path = typer.Argument(..., exists=True, file_okay=False),
) -> None:
    """Delete the sidecar working directory (cache, keys, seed, VMX, state)."""
    import shutil

    target = vm_dir.resolve() / ".sidecar"
    if not target.is_dir():
        _fail(f"no sidecar directory at {target}")
    shutil.rmtree(target)
    ok(f"removed {target}")


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

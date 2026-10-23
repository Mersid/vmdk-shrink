"""``vmdk`` command line: subcommand style, Docker/Git-like."""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from dataclasses import asdict
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any

import typer

from .console import console, err, error_console, info, ok, warn
from .discovery import select_disk

app = typer.Typer(
    name="vmdk",
    help="Inspect, verify, compact, and shrink offline VMware ext-family disks. Back up first.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
    rich_markup_mode="rich",
)
image_app = typer.Typer(help="Build/remove the container image.", no_args_is_help=True)
app.add_typer(image_app, name="image")


class FilesystemTransport(StrEnum):
    nbd = "nbd"
    sidecar = "sidecar"


class HashTransport(StrEnum):
    container = "container"
    sidecar = "sidecar"


def _version(value: bool) -> None:
    if value:
        from importlib.metadata import version

        console.print(f"vmdk {version('vmdk-shrink')}")
        raise typer.Exit()


@app.callback()
def options(
    no_color: bool = typer.Option(False, "--no-color", help="Disable colored output"),
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True),
) -> None:
    if no_color:
        console.no_color = error_console.no_color = True


def _command_errors(function: Any) -> Any:
    @wraps(function)
    def invoke(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            _fail(str(exc))

    return invoke


def _confirm(operation: str, disks: tuple[Path, ...], yes: bool) -> None:
    from rich.panel import Panel
    from rich.text import Text

    console.print(
        Panel(
            Text(
                f"{operation}\n"
                + "\n".join(str(d) for d in disks)
                + "\n\nIn-place changes. Power off the VM and keep a full, restorable backup."
            ),
            title="Operation plan",
            border_style="cyan",
            expand=False,
        )
    )
    if not yes:
        if not sys.stdin.isatty():
            _fail("confirmation required; use --yes only after making a full backup")
        typer.confirm("Proceed with these disks?", abort=True)


def _fail(msg: str) -> None:
    err(msg)
    raise typer.Exit(code=1)


# ---------------------------------------------------------------- image ----
@image_app.command("build")
@_command_errors
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
@_command_errors
def image_rm() -> None:
    """Remove the container image (full cleanup; ``docker rm`` handles the rest)."""
    from .container import Container

    Container(()).remove_image()
    ok("image removed")


@image_app.command("ls")
@_command_errors
def image_ls() -> None:
    """Show whether the image is present."""
    from .container import IMAGE_TAG, Container

    if Container(()).image_exists():
        ok(f"present: {IMAGE_TAG}")
    else:
        warn(f"not built: {IMAGE_TAG}")


# --------------------------------------------------------------- inspect ----
@app.command()
@_command_errors
def inspect(
    vmdk: Path = typer.Argument(..., exists=True, dir_okay=False),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Report a VMDK's capacity, extents, and truly-allocated (data) bytes."""
    from rich.table import Table

    from .vmdk_parser import extent_list, grain_stats, host_footprint_bytes

    stats = grain_stats(vmdk)
    extents = extent_list(vmdk)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "disk": str(vmdk.resolve()),
                    "capacity_bytes": stats.capacity_bytes,
                    "allocated_bytes": stats.allocated_bytes,
                    "host_bytes": host_footprint_bytes(vmdk),
                    "extent_count": len(extents),
                }
            )
        )
        return
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
@_command_errors
def status(vm_dir: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    """Show the helper container state, lock dirs, and per-disk host footprints."""
    from .container import Container, ContainerError

    try:
        status_line = Container(()).running_status()
    except (OSError, ContainerError) as exc:
        warn(f"Docker status unavailable: {exc}")
        status_line = None
    if status_line:
        ok(f"container running: {status_line}")
    else:
        info("helper container not running")

    for lock in vm_dir.glob("*.lck"):
        warn(f"VM lock present (not assumed stale): {lock.name}")
    from .discovery import disks_in

    disks = disks_in(vm_dir)
    if not disks:
        info("no .vmdk files here")
        return
    from .vmdk_parser import host_footprint_bytes

    for d in disks:
        console.print(f"  {d.name}: {host_footprint_bytes(d) / 2**30:.1f} GiB")


# --------------------------------------------------------------- compact ----
@app.command()
@_command_errors
def compact(
    vm_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="VM directory"),
    vmdk: Path | None = typer.Option(
        None, "--vmdk", exists=True, dir_okay=False, help="Disk descriptor (required if ambiguous)"
    ),
    boot: Path | None = typer.Option(
        None, "--boot", help="Boot-disk VMDK to zerofree+compact alongside"
    ),
    no_zerofree: bool = typer.Option(
        False,
        "--compact-only",
        "--no-zerofree",
        help="Skip filesystem work and Docker; plain -k only",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm backup and skip the prompt"),
) -> None:
    """Reclaim host space without reducing capacity. Filesystem repair may modify metadata.

    Capacity is not reduced; a running guest can still expand the disk again.
    """
    from .compact import compact_disk

    data = select_disk(vm_dir, vmdk)
    targets = (data, boot.resolve()) if boot else (data,)
    _confirm("Compact (capacity unchanged)", targets, yes)
    from .prerequisites import require_prerequisites

    require_prerequisites("compact", plain_compact=no_zerofree)
    try:
        compact_disk(
            data,
            extra_vmdks=(boot.resolve(),) if boot else (),
            zerofree_fs=not no_zerofree,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc))


# ---------------------------------------------------------------- shrink ----
@app.command()
@_command_errors
def shrink(
    vm_dir: Path = typer.Argument(..., exists=True, file_okay=False),
    target_gib: int = typer.Argument(..., min=1, help="New filesystem size in GiB"),
    vmdk: Path | None = typer.Option(
        None, "--vmdk", exists=True, dir_okay=False, help="Disk descriptor (required if ambiguous)"
    ),
    replicas: int = typer.Option(
        4, "--replicas", min=1, max=8, help="Parallel read-only NBD connections for hashing"
    ),
    resume: bool = typer.Option(
        False, "--resume", help="Reuse a validated baseline only if mutation has not started"
    ),
    mode: FilesystemTransport = typer.Option(
        FilesystemTransport.nbd,
        "--mode",
        help="fs-work transport: nbd = in-place via container qemu-nbd (1x); "
        "sidecar = disposable Debian VM with the disk attached (1x, no 9p)",
    ),
    hash_via: HashTransport | None = typer.Option(
        None,
        "--hash-via",
        help="Hash transport (default: follow --mode; mixed runtimes require explicit selection)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm backup and skip the prompt"),
) -> None:
    """Shrink filesystem, GPT partition, and capacity; compare regular-file contents."""
    from .shrink import shrink as run_shrink

    data = select_disk(vm_dir, vmdk)
    selected_hash = (
        hash_via.value
        if hash_via
        else ("sidecar" if mode == FilesystemTransport.sidecar else "container")
    )
    _confirm(
        f"Shrink to {target_gib} GiB · filesystem: {mode.value} · hash: {selected_hash}",
        (data,),
        yes,
    )
    from .prerequisites import require_prerequisites

    require_prerequisites("shrink", mode=mode.value, hash_via=selected_hash)
    try:
        code = run_shrink(
            vm_dir.resolve(),
            target_gib,
            data_vmdk=data,
            replicas=replicas,
            resume=resume,
            mode=mode.value,
            hash_via=selected_hash,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    if code != 0:
        raise typer.Exit(code=code)


# ------------------------------------------------------------------- hash ----
@app.command()
@_command_errors
def hash(  # noqa: A001 - CLI subcommand name
    vm_dir: Path = typer.Argument(..., exists=True, file_okay=False),
    vmdk: Path | None = typer.Option(
        None, "--vmdk", exists=True, dir_okay=False, help="Disk descriptor (required if ambiguous)"
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Manifest output path (default: <vm_dir>/manifest)"
    ),
    via: HashTransport = typer.Option(
        HashTransport.container,
        "--via",
        help="transport: sidecar = in-VM find+sha256sum (native disk stack); "
        "container = legacy RO qemu-nbd replicas over 9p (slow, benchmarking)",
    ),
    replicas: int = typer.Option(
        4, "--replicas", min=1, max=8, help="Container-mode RO NBD replicas"
    ),
) -> None:
    """Hash every file of the data disk to a manifest (verification primitive)."""
    from .manifest import collect  # noqa: F401 - re-export sanity
    from .progress import PhaseBar

    data = select_disk(vm_dir, vmdk)
    from .prerequisites import require_prerequisites
    from .safety import operation_lock

    require_prerequisites("hash", hash_via=via.value)
    out_path = out or (vm_dir.resolve() / "manifest")
    bar = PhaseBar()
    try:
        with operation_lock(vm_dir.resolve()):
            manifest = _hash_command(vm_dir, data, out_path, via, replicas, bar)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
        return
    finally:
        bar.close()
    ok(f"{len(manifest)} files hashed -> {out_path.with_suffix('.raw')}")


def _hash_command(
    vm_dir: Path,
    data: Path,
    out_path: Path,
    via: HashTransport,
    replicas: int,
    bar: Any,
) -> Any:
    if via == "sidecar":
        from .sidecar.hashing import hash_via_sidecar

        return hash_via_sidecar(vm_dir.resolve(), data, out_path=out_path, bar=bar)
    else:
        from .session import hash_all, open_session

        with open_session(vm_dir.resolve()) as session:
            return hash_all(session, data, out_path=out_path, replicas=replicas, bar=bar)


# --------------------------------------------------------------- sidecar ----
sidecar_app = typer.Typer(help="Sidecar VM maintenance.", no_args_is_help=True)
app.add_typer(sidecar_app, name="sidecar")


@sidecar_app.command("teardown")
@_command_errors
def sidecar_teardown(
    vm_dir: Path = typer.Argument(..., exists=True, file_okay=False),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip deletion confirmation"),
) -> None:
    """Delete the sidecar working directory (cache, keys, seed, VMX, state)."""
    import shutil

    target = vm_dir.resolve() / ".sidecar"
    if not target.is_dir():
        _fail(f"no sidecar directory at {target}")
    from .safety import operation_lock
    from .sidecar.orchestrator import RealVmrun

    _confirm("Delete helper VM artifacts (not source disks)", (target,), yes)
    with operation_lock(vm_dir.resolve()):
        for vmx in target.glob("*.vmx"):
            if RealVmrun().is_running(vmx):
                _fail("sidecar is running; shut it down before deleting artifacts")
        if target.is_symlink():
            _fail("refusing to delete a linked sidecar directory")
        shutil.rmtree(target)
    ok(f"removed {target}")


def _default_data_vmdk(vm_dir: Path) -> Path:
    return select_disk(vm_dir)


@app.command()
@_command_errors
def doctor(
    command: str = typer.Argument("shrink", help="shrink, compact, hash, or inspect"),
    mode: FilesystemTransport = typer.Option(FilesystemTransport.nbd, "--mode"),
    hash_via: HashTransport | None = typer.Option(None, "--hash-via"),
    compact_only: bool = typer.Option(False, "--compact-only"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Check command-specific prerequisites without starting containers or helper VMs."""
    from rich.table import Table

    from .prerequisites import check_requirements

    selected_hash = (
        hash_via.value
        if hash_via
        else ("sidecar" if mode == FilesystemTransport.sidecar else "container")
    )
    results = check_requirements(
        command, mode=mode.value, hash_via=selected_hash, plain_compact=compact_only
    )
    if json_output:
        typer.echo(json.dumps([asdict(r) for r in results]))
    else:
        table = Table(title=f"Prerequisites · {command}", box=None)
        table.add_column("Requirement")
        table.add_column("Status")
        table.add_column("Detail")
        for result in results:
            table.add_row(
                result.name,
                "[green]OK[/green]" if result.available else "[red]MISSING[/red]",
                result.detail,
            )
        console.print(table)
        if not results:
            ok("No external programs required")
    if any(not r.available for r in results):
        raise typer.Exit(1)


def main() -> None:
    # Rich glyphs (check/cross marks, box drawing) crash on legacy Windows
    # consoles whose stream codec is cp1252; force UTF-8 with replacement so
    # the CLI degrades to '?' instead of dying mid-command.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    app()


if __name__ == "__main__":
    main()

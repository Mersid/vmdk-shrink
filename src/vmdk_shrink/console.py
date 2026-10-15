"""Console output: one shared Rich console with helpers for consistent styling."""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

THEME = Theme(
    {
        "step": "dim",
        "info": "cyan",
        "ok": "green",
        "warn": "yellow",
        "err": "bold red",
        "val": "bold magenta",
    }
)

console = Console(theme=THEME)


def step(msg: str) -> None:
    """A dim progress line for a pipeline stage."""
    console.print(f"[step]->[/step] {msg}")


def ok(msg: str) -> None:
    console.print(f"[ok]✔[/ok] {msg}")


def warn(msg: str) -> None:
    console.print(f"[warn]![/warn] {msg}")


def err(msg: str) -> None:
    console.print(f"[err]✘ {msg}[/err]")


def info(msg: str) -> None:
    console.print(f"[info]·[/info] {msg}")

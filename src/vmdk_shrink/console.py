"""Console output: one shared Rich console with helpers for consistent styling."""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme


def _glyph(ch: str, fallback: str) -> str:
    """Pick a mark the output stream codec can encode.

    Legacy Windows consoles (cp1252) crash Rich mid-print on check/cross
    marks; degrade to ASCII there instead of dying.
    """
    import sys

    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        ch.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return fallback
    return ch


_OK = _glyph(chr(0x2714), "+")
_ERR = _glyph(chr(0x2718), "x")
_INFO = _glyph(chr(0x00B7), "-")

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
    console.print(f"[ok]{_OK}[/ok] {msg}")


def warn(msg: str) -> None:
    console.print(f"[warn]![/warn] {msg}")


def err(msg: str) -> None:
    console.print(f"[err]{_ERR} {msg}[/err]")


def info(msg: str) -> None:
    console.print(f"[info]{_INFO}[/info] {msg}")

"""Console output: one shared Rich console with helpers for consistent styling."""

from __future__ import annotations

import time

from rich.console import Console
from rich.markup import escape
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


THEME = Theme(
    {
        "step": "dim",
        "info": "cyan",
        "ok": "green",
        "warn": "yellow",
        "err": "bold red",
        "val": "bold bright_cyan",
    }
)

console = Console(theme=THEME)
error_console = Console(theme=THEME, stderr=True)
_STARTED = time.monotonic()


def _prefix(level: str, style: str) -> str:
    elapsed = time.monotonic() - _STARTED
    return f"[dim]{elapsed:8.1f}s[/dim] [{style}]{level:<5}[/{style}]"


def step(msg: str) -> None:
    """An elapsed-time/status line for a pipeline stage."""
    console.print(f"{_prefix('RUN', 'info')} {msg}")


def ok(msg: str) -> None:
    console.print(f"{_prefix('OK', 'ok')} {msg}")


def warn(msg: str) -> None:
    console.print(f"{_prefix('WARN', 'warn')} {msg}")


def err(msg: str) -> None:
    error_console.print(f"{_prefix('ERROR', 'err')} {escape(msg)}")


def info(msg: str) -> None:
    console.print(f"{_prefix('INFO', 'info')} {msg}")

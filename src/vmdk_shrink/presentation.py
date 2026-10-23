"""CLI-only renderables; runtime adapters report values, not Rich widgets."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.filesize import decimal
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    Task,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text

from .console import console

if TYPE_CHECKING:
    from .vdiskmanager import CompactResult, VDiskManager


def compact_with_progress(manager: VDiskManager, vmdk: Path) -> CompactResult:
    with Progress(
        TextColumn("[info]compact[/info]"),
        BarColumn(style="dim white", complete_style="cyan", finished_style="bright_cyan"),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        speed_estimate_period=3600,
        console=console,
    ) as progress:
        task = progress.add_task("", total=100)
        return manager.compact(vmdk, on_progress=lambda pct: progress.update(task, completed=pct))


class PhaseMetricsColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        total = task.total or 0
        if task.fields.get("files") or task.fields.get("unit"):
            unit = task.fields.get("unit") or "files"
            return Text(f"{int(task.completed):,}/{int(total):,} {unit}", style="dim")
        return Text(f"{decimal(int(task.completed))}/{decimal(int(total))} est.", style="dim")

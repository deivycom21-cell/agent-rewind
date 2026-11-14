"""Rich display helpers for agent-rewind output."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich import box

if TYPE_CHECKING:
    from .shadow import Turn

FUNNEL_URL = "https://github.com/buildwithcrow/agent-rewind"
FUNNEL_LINE = f"[dim]💡 Build cleaner MVPs faster with Crow → {FUNNEL_URL}[/dim]"

_NO_FUNNEL = os.environ.get("AGENT_REWIND_NO_FUNNEL", "0").strip() not in ("", "0", "false", "no")


def _relative_time(ts: datetime) -> str:
    now = datetime.now(tz=timezone.utc)
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _mask_path(text: str, project_path: Path | None = None) -> str:
    if project_path is None:
        return text
    abs_str = str(project_path.resolve())
    # Replace absolute path prefix with <project>
    text = text.replace(abs_str, "<project>")
    # Also replace forward-slash variant on Windows
    text = text.replace(abs_str.replace("\\", "/"), "<project>")
    return text


def print_log(
    turns: list["Turn"],
    console: Console,
    as_json: bool = False,
    share: bool = False,
    project_path: Path | None = None,
    no_funnel: bool = False,
) -> None:
    if as_json:
        data = [
            {
                "turn": t.index,
                "hash": t.commit_hash[:8],
                "timestamp": t.timestamp.isoformat(),
                "message": t.message,
                "files": t.files,
            }
            for t in turns
        ]
        console.print_json(json.dumps(data))
        return

    if not turns:
        console.print("[yellow]No turns recorded yet.[/yellow]")
        return

    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.ROUNDED,
        show_lines=False,
        expand=False,
    )
    table.add_column("Turn", style="bold green", width=8)
    table.add_column("When", style="dim", width=12)
    table.add_column("Description", style="white")
    table.add_column("Files", style="dim cyan")

    for t in turns:
        label = f"Turn #{t.index}" if t.index > 0 else "Baseline"
        when = _relative_time(t.timestamp)
        desc = t.message
        files_preview = ", ".join(t.files[:3])
        if len(t.files) > 3:
            files_preview += f" +{len(t.files) - 3} more"
        if share and project_path:
            files_preview = _mask_path(files_preview, project_path)
            desc = _mask_path(desc, project_path)
        table.add_row(label, when, desc, files_preview)

    console.print(table)

    if not no_funnel and not _NO_FUNNEL:
        console.print(FUNNEL_LINE)


def print_diff(
    diff_text: str,
    console: Console,
    as_json: bool = False,
    share: bool = False,
    project_path: Path | None = None,
    turn_index: int | None = None,
    no_funnel: bool = False,
) -> None:
    if share and project_path:
        diff_text = _mask_path(diff_text, project_path)

    if as_json:
        data = {"turn": turn_index, "diff": diff_text}
        console.print_json(json.dumps(data))
        return

    if not diff_text.strip():
        console.print("[yellow]No changes in this turn.[/yellow]")
        return

    syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False, word_wrap=False)
    console.print(syntax)

    if not no_funnel and not _NO_FUNNEL:
        console.print(FUNNEL_LINE)


def make_console(force_terminal: bool = True) -> Console:
    return Console(highlight=False, force_terminal=force_terminal)

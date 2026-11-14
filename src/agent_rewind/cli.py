"""Main CLI entry point for agent-rewind."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

from .shadow import (
    clean_shadow,
    get_diff,
    list_turns,
    restore_to_turn,
    shadow_dir,
    snapshot,
)
from .display import make_console, print_diff, print_log
from .watcher import TurnWatcher


def _resolve_project(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve()


def _parse_flags(args: list[str]) -> tuple[dict, list[str]]:
    """Extract global flags, return (flags_dict, remaining_args)."""
    flags = {"json": False, "share": False, "no_funnel": False}
    remaining = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            flags["json"] = True
        elif a == "--share":
            flags["share"] = True
        elif a in ("--no-funnel",):
            flags["no_funnel"] = True
        else:
            remaining.append(a)
        i += 1
    return flags, remaining


def cmd_snap(args: list[str], flags: dict, console: Console) -> int:
    project = _resolve_project()
    msg = " ".join(args) if args else "__auto__"
    h = snapshot(project, msg)
    if h:
        console.print(f"[green]✓[/green] Snapshot saved: [bold]{h[:8]}[/bold]")
    else:
        console.print("[yellow]Nothing to snapshot (no changes detected).[/yellow]")
    return 0


def cmd_log(args: list[str], flags: dict, console: Console) -> int:
    project = _resolve_project()
    turns = list_turns(project)
    print_log(
        turns,
        console,
        as_json=flags["json"],
        share=flags["share"],
        project_path=project,
        no_funnel=flags["no_funnel"],
    )
    return 0


def cmd_diff(args: list[str], flags: dict, console: Console) -> int:
    project = _resolve_project()
    turn_index: int | None = None
    if args:
        try:
            turn_index = int(args[0])
        except ValueError:
            console.print(f"[red]Invalid turn number: {args[0]}[/red]")
            return 1

    diff_text = get_diff(project, turn_index)
    print_diff(
        diff_text,
        console,
        as_json=flags["json"],
        share=flags["share"],
        project_path=project,
        turn_index=turn_index,
        no_funnel=flags["no_funnel"],
    )
    return 0


def cmd_undo(args: list[str], flags: dict, console: Console) -> int:
    project = _resolve_project()

    # Parse steps and --yes flag
    steps = 1
    yes = False
    remaining_args = []
    for a in args:
        if a in ("-y", "--yes"):
            yes = True
        else:
            try:
                steps = int(a)
            except ValueError:
                remaining_args.append(a)

    turns = list_turns(project)
    if not turns:
        console.print("[yellow]No turns to undo.[/yellow]")
        return 0

    # turns[0] is newest; we want to go back `steps` turns from the latest
    latest_index = turns[0].index
    target_index = latest_index - steps
    if target_index < 0:
        console.print(f"[red]Cannot undo {steps} turn(s) — only {latest_index} turn(s) available.[/red]")
        return 1

    target = next((t for t in turns if t.index == target_index), None)
    if target is None:
        console.print(f"[red]Turn {target_index} not found.[/red]")
        return 1

    console.print(f"[bold]Undoing {steps} turn(s)[/bold] → restoring to [cyan]Turn #{target_index}[/cyan]")
    if target.files:
        console.print(f"[dim]Files that will be restored: {', '.join(target.files[:8])}{'...' if len(target.files) > 8 else ''}[/dim]")

    if not yes:
        confirmed = Confirm.ask("Proceed?", default=False)
        if not confirmed:
            console.print("[yellow]Aborted.[/yellow]")
            return 0

    try:
        restored = restore_to_turn(project, target_index)
        console.print(f"[green]✓[/green] Restored to Turn #{target_index}. {len(restored)} file(s) reverted.")
        console.print("[dim](A pre-undo backup was saved — you can undo the undo.)[/dim]")
    except Exception as e:
        console.print(f"[red]Error during restore: {e}[/red]")
        return 1

    return 0


def cmd_watch(args: list[str], flags: dict, console: Console) -> int:
    project = _resolve_project()
    console.print(f"[bold cyan]agent-rewind watch[/bold cyan] — watching [bold]{project}[/bold]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]")

    turn_count = [0]

    def on_turn(n: int, msg: str) -> None:
        turn_count[0] = n
        console.print(f"[green]✓[/green] [bold]Turn #{n}[/bold] saved — {msg}")

    watcher = TurnWatcher(project, on_turn=on_turn)
    watcher.start()

    try:
        import time
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()

    console.print(f"\n[bold]Done.[/bold] {turn_count[0]} turn(s) recorded.")
    return 0


def cmd_run(args: list[str], flags: dict, console: Console) -> int:
    """Run a command with the watcher active in background."""
    if not args:
        console.print("[red]Usage: agent-rewind run -- <command> [args...][/red]")
        return 1

    # Strip leading '--' separator
    if args[0] == "--":
        args = args[1:]
    if not args:
        console.print("[red]No command specified after '--'[/red]")
        return 1

    project = _resolve_project()
    console.print(f"[bold cyan]agent-rewind run[/bold cyan] — wrapping [bold]{' '.join(args)}[/bold]")

    # Take initial snapshot
    h = snapshot(project, "baseline (pre-run)")
    if h:
        console.print(f"[dim]Baseline snapshot: {h[:8]}[/dim]")

    turn_count = [0]

    def on_turn(n: int, msg: str) -> None:
        turn_count[0] = n
        # Print unobtrusively to stderr so it doesn't corrupt the wrapped command's stdout
        sys.stderr.write(f"\r[agent-rewind] Turn #{n} saved — {msg}\n")
        sys.stderr.flush()

    watcher = TurnWatcher(project, on_turn=on_turn)
    watcher.start()

    try:
        result = subprocess.run(args, cwd=str(project))
        exit_code = result.returncode
    except KeyboardInterrupt:
        exit_code = 130
    except FileNotFoundError:
        console.print(f"[red]Command not found: {args[0]}[/red]")
        exit_code = 127
    finally:
        watcher.stop()

    # Final snapshot
    snapshot(project, f"post-run ({args[0]})")
    console.print(f"\n[bold]Done.[/bold] {turn_count[0]} turn(s) recorded during session.")
    return exit_code


def cmd_clean(args: list[str], flags: dict, console: Console) -> int:
    project = _resolve_project()
    sdir = shadow_dir(project)
    if not sdir.exists():
        console.print("[yellow]No shadow history found for this project.[/yellow]")
        return 0

    console.print(f"[bold]Removing shadow history:[/bold] {sdir}")
    if flags["json"]:
        clean_shadow(project)
        console.print_json(json.dumps({"cleaned": str(sdir)}))
    else:
        confirmed = Confirm.ask("Delete shadow history?", default=False)
        if not confirmed:
            console.print("[yellow]Aborted.[/yellow]")
            return 0
        clean_shadow(project)
        console.print("[green]✓[/green] Shadow history removed.")

    return 0


HELP_TEXT = """\
[bold cyan]agent-rewind[/bold cyan] — Atomic Time Machine for AI Coding Agents

[bold]Usage:[/bold]
  agent-rewind [bold]watch[/bold]              Watch for agent turns in real time
  agent-rewind [bold]run[/bold] -- <cmd>       Wrap a command (e.g. claude, opencode)
  agent-rewind [bold]snap[/bold] [msg]         Save a manual snapshot
  agent-rewind [bold]log[/bold]                Show all recorded turns
  agent-rewind [bold]diff[/bold] [turn]        Show diff for the last (or Nth) turn
  agent-rewind [bold]undo[/bold] [N] [-y]      Revert N agent turns (default: 1)
  agent-rewind [bold]clean[/bold]              Delete shadow history for this project

[bold]Global flags:[/bold]
  --json        Structured JSON output (no ANSI)
  --share       Mask absolute paths for safe sharing
  --no-funnel   Hide the footer line

[bold]Examples:[/bold]
  agent-rewind run -- claude
  agent-rewind undo 2 --yes
  agent-rewind diff 3 --share
"""


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    console = make_console()

    if not argv or argv[0] in ("-h", "--help"):
        console.print(HELP_TEXT)
        sys.exit(0)

    if argv[0] in ("-v", "--version"):
        from . import __version__
        console.print(f"agent-rewind {__version__}")
        sys.exit(0)

    flags, remaining = _parse_flags(argv)
    if not remaining:
        console.print(HELP_TEXT)
        sys.exit(0)

    cmd = remaining[0]
    cmd_args = remaining[1:]

    dispatch = {
        "snap": cmd_snap,
        "log": cmd_log,
        "diff": cmd_diff,
        "undo": cmd_undo,
        "watch": cmd_watch,
        "run": cmd_run,
        "clean": cmd_clean,
    }

    handler = dispatch.get(cmd)
    if handler is None:
        console.print(f"[red]Unknown command: {cmd}[/red]")
        console.print(HELP_TEXT)
        sys.exit(1)

    sys.exit(handler(cmd_args, flags, console))

"""File watcher with debounce for atomic turn detection."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from watchfiles import watch, Change

from .shadow import snapshot, ALWAYS_EXCLUDE

DEBOUNCE_SECONDS = 1.5


def _should_ignore(path: str) -> bool:
    parts = Path(path).parts
    return any(part in ALWAYS_EXCLUDE for part in parts)


class TurnWatcher:
    """
    Watches a project directory and groups rapid file changes into atomic turns.
    Each burst of writes separated by DEBOUNCE_SECONDS inactivity = one turn.
    """

    def __init__(
        self,
        project_path: Path,
        on_turn: Callable[[int, str], None] | None = None,
    ):
        self.project_path = project_path
        self.on_turn = on_turn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._turn_count = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # Take baseline snapshot before watching
        snapshot(self.project_path, "baseline")

        pending_changes: set[str] = set()
        last_change_time: float = 0.0
        debounce_timer: threading.Timer | None = None

        def flush_turn():
            nonlocal pending_changes, last_change_time
            if not pending_changes:
                return
            files = list(pending_changes)
            pending_changes = set()
            self._turn_count += 1
            msg = f"modified {len(files)} file{'s' if len(files) != 1 else ''}"
            hash_ = snapshot(self.project_path, msg)
            if hash_ and self.on_turn:
                self.on_turn(self._turn_count, msg)

        try:
            for changes in watch(
                str(self.project_path),
                stop_event=self._stop_event,
                yield_on_timeout=False,
                rust_timeout=200,
            ):
                for change_type, path in changes:
                    if _should_ignore(path):
                        continue
                    pending_changes.add(path)

                if pending_changes:
                    if debounce_timer is not None:
                        debounce_timer.cancel()
                    debounce_timer = threading.Timer(DEBOUNCE_SECONDS, flush_turn)
                    debounce_timer.start()

        except Exception:
            pass
        finally:
            if debounce_timer is not None:
                debounce_timer.cancel()
            # Flush any remaining changes on exit
            if pending_changes:
                flush_turn()

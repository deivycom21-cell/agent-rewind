"""Shadow Git repository management in /tmp — zero contamination to real repo."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


# Files/dirs always excluded from shadow snapshots
ALWAYS_EXCLUDE = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", ".eggs"}


class Turn(NamedTuple):
    index: int
    commit_hash: str
    timestamp: datetime
    message: str
    files: list[str]


def _project_hash(project_path: Path) -> str:
    resolved = str(project_path.resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]


def shadow_dir(project_path: Path) -> Path:
    return Path(tempfile.gettempdir()) / "agent-rewind" / _project_hash(project_path)


def _git(
    args: list[str],
    project_path: Path,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """
    Run git with --git-dir and --work-tree pointing at the shadow repo.
    This avoids touching the project's real .git directory.
    """
    sdir = shadow_dir(project_path)
    work_tree = str(project_path.resolve())

    # Prepend git-dir and work-tree flags before all other args
    full_args = [
        "git",
        f"--git-dir={sdir}",
        f"--work-tree={work_tree}",
    ] + args

    env = {**os.environ}
    # Remove any inherited GIT_DIR/GIT_WORK_TREE that could interfere
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)

    return subprocess.run(
        full_args,
        env=env,
        capture_output=capture,
        text=True,
    )


def _git_bare(args: list[str], git_dir: Path) -> subprocess.CompletedProcess:
    """Run git directly against the shadow dir (for init and bare config)."""
    env = {**os.environ}
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)
    return subprocess.run(
        ["git", f"--git-dir={git_dir}"] + args,
        env=env,
        capture_output=True,
        text=True,
    )


def init_shadow(project_path: Path) -> None:
    sdir = shadow_dir(project_path)
    sdir.mkdir(parents=True, exist_ok=True)

    # Check if already initialised (HEAD file exists)
    if (sdir / "HEAD").exists():
        return

    # Init a bare repo at the shadow dir path
    env = {**os.environ}
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    subprocess.run(
        ["git", "init", "--bare", str(sdir)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    # Allow working-tree operations on this bare repo
    _git_bare(["config", "core.bare", "false"], sdir)
    _git_bare(["config", "core.worktree", str(project_path.resolve())], sdir)
    _git_bare(["config", "user.email", "agent-rewind@local"], sdir)
    _git_bare(["config", "user.name", "agent-rewind"], sdir)


def _build_exclude_file(project_path: Path, sdir: Path) -> None:
    """Write an info/exclude file so git respects always-excluded dirs."""
    info_dir = sdir / "info"
    info_dir.mkdir(exist_ok=True)
    exclude_file = info_dir / "exclude"

    patterns = list(ALWAYS_EXCLUDE)
    gi = project_path / ".gitignore"
    if gi.exists():
        for line in gi.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)

    exclude_file.write_text("\n".join(patterns) + "\n", encoding="utf-8")


def snapshot(project_path: Path, message: str) -> str | None:
    """Create a shadow commit. Returns commit hash or None if nothing to commit."""
    init_shadow(project_path)
    sdir = shadow_dir(project_path)
    _build_exclude_file(project_path, sdir)

    # Stage all tracked + new files (respecting excludes)
    _git(["add", "-A"], project_path)

    # Check if there's anything to commit
    status = _git(["status", "--porcelain"], project_path)
    if status.returncode != 0 or not status.stdout.strip():
        # Nothing to commit — return existing HEAD if any
        head = _git(["rev-parse", "HEAD"], project_path)
        if head.returncode == 0:
            return head.stdout.strip()
        return None

    # Collect changed files for the commit message
    changed_files = [ln for ln in status.stdout.strip().splitlines() if ln.strip()]
    n = len(changed_files)
    if message == "__auto__":
        message = f"modified {n} file{'s' if n != 1 else ''}"

    ts = datetime.now(tz=timezone.utc).isoformat()
    full_msg = json.dumps({
        "msg": message,
        "ts": ts,
        "files": [ln[3:].strip() for ln in changed_files],
    })

    result = _git(["commit", "-m", full_msg], project_path)
    if result.returncode != 0:
        return None

    head = _git(["rev-parse", "HEAD"], project_path)
    return head.stdout.strip() if head.returncode == 0 else None


def list_turns(project_path: Path) -> list[Turn]:
    """Return all turns in reverse chronological order (newest first)."""
    init_shadow(project_path)
    result = _git(["log", "--pretty=format:%H|%ct|%s"], project_path)
    if result.returncode != 0 or not result.stdout.strip():
        return []

    lines = result.stdout.strip().splitlines()
    # log gives newest-first by default; we want to assign index by age (oldest=0)
    turns = []
    for i, line in enumerate(reversed(lines)):  # oldest first → index 0
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        commit_hash, epoch, raw_msg = parts
        try:
            ts = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        except (ValueError, OSError):
            ts = datetime.now(tz=timezone.utc)

        try:
            data = json.loads(raw_msg)
            msg = data.get("msg", raw_msg)
            files = data.get("files", [])
        except (json.JSONDecodeError, ValueError):
            msg = raw_msg
            files = []

        turns.append(Turn(index=i, commit_hash=commit_hash, timestamp=ts, message=msg, files=files))

    # Return newest first (highest index first)
    return list(reversed(turns))


def get_diff(project_path: Path, turn_index: int | None = None) -> str:
    """Return unified diff for the given turn (default: last)."""
    init_shadow(project_path)
    turns = list_turns(project_path)
    if not turns:
        return ""

    if turn_index is None:
        turn_index = turns[0].index  # newest

    target = next((t for t in turns if t.index == turn_index), None)
    if target is None:
        return ""

    sorted_turns = sorted(turns, key=lambda t: t.index)
    pos = sorted_turns.index(target)

    if pos == 0:
        # First commit: diff the whole tree against empty
        result = _git(["show", "--stat", "--patch", target.commit_hash], project_path)
    else:
        prev = sorted_turns[pos - 1]
        result = _git(["diff", prev.commit_hash, target.commit_hash], project_path)

    return result.stdout if result.returncode == 0 else ""


def restore_to_turn(project_path: Path, turn_index: int) -> list[str]:
    """
    Restore working tree to the state at turn_index.
    Saves a pre-undo backup first (making the undo itself reversible).
    Returns list of files that were changed.
    """
    init_shadow(project_path)
    turns = list_turns(project_path)
    if not turns:
        raise ValueError("No turns recorded")

    target = next((t for t in turns if t.index == turn_index), None)
    if target is None:
        raise ValueError(f"Turn {turn_index} not found")

    # Capture the commit hash we're about to leave (for the backup label)
    current_head = _git(["rev-parse", "HEAD"], project_path)
    pre_hash = current_head.stdout.strip()[:8] if current_head.returncode == 0 else "unknown"

    # Checkout all files from the target commit into the work tree
    result = _git(["checkout", target.commit_hash, "--", "."], project_path)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to restore: {result.stderr}")

    # Now save a post-restore snapshot so the undo itself is reversible
    snapshot(project_path, f"pre-undo backup (was {pre_hash})")

    return target.files


def clean_shadow(project_path: Path) -> None:
    """Remove the shadow git directory for this project."""
    import shutil
    import stat

    sdir = shadow_dir(project_path)
    if not sdir.exists():
        return

    def _remove_readonly(func, path, _exc):
        # On Windows git objects are read-only; clear the flag and retry
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    shutil.rmtree(sdir, onerror=_remove_readonly)

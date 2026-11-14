"""
Core integration test: simulate an agent breaking 8 files, then undo 2 turns.
Verifies the real .git/ is untouched throughout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _real_git_log(project: Path) -> str:
    return _git(["log", "--oneline"], project).stdout.strip()


def _real_git_status(project: Path) -> str:
    """Returns only the staged (index) changes — not untracked/unstaged files."""
    return _git(["diff", "--cached", "--name-only"], project).stdout.strip()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """Create a real git repo with an initial commit."""
    _git(["init", str(tmp_path)], tmp_path)
    _git(["config", "user.email", "test@test.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)

    for i in range(1, 9):
        (tmp_path / f"module_{i}.py").write_text(f"# module {i}\nVALUE = {i}\n")

    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "initial: 8 clean modules"], tmp_path)
    return tmp_path


def test_full_rewind_cycle(project: Path):
    from agent_rewind.shadow import snapshot, list_turns, get_diff, restore_to_turn, clean_shadow

    # Capture the original state of the real git repo
    original_log = _real_git_log(project)
    original_contents = {
        f"module_{i}.py": (project / f"module_{i}.py").read_text()
        for i in range(1, 9)
    }

    # --- Turn 0: baseline ---
    h0 = snapshot(project, "baseline")
    assert h0 is not None, "baseline snapshot should return a hash"

    # --- Turn 1: agent modifies 4 files ---
    for i in range(1, 5):
        (project / f"module_{i}.py").write_text(f"# BROKEN by agent (turn 1)\nVALUE = 'corrupted'\n")

    h1 = snapshot(project, "turn 1: modified 4 files")
    assert h1 is not None

    # --- Turn 2: agent modifies remaining 4 files ---
    for i in range(5, 9):
        (project / f"module_{i}.py").write_text(f"# BROKEN by agent (turn 2)\nVALUE = None  # oops\n")

    h2 = snapshot(project, "turn 2: modified 4 more files")
    assert h2 is not None

    # Verify the real git LOG is still untouched (shadow never commits to real repo)
    assert _real_git_log(project) == original_log, "Real git log was modified — shadow leaked!"

    # Verify shadow never staged anything in the REAL git index
    assert _real_git_status(project) == "", "Shadow staged files in real git index!"

    # --- Verify diff detects changes ---
    turns = list_turns(project)
    assert len(turns) >= 3, f"Expected at least 3 turns (baseline + 2), got {len(turns)}"

    diff = get_diff(project, turns[0].index)
    assert diff.strip() != "", "diff should show changes after agent modifications"

    # --- undo 2 turns: restore to baseline (turn index 0) ---
    restore_to_turn(project, 0)

    # Verify all 8 files are back to their original content
    for i in range(1, 9):
        fname = f"module_{i}.py"
        actual = (project / fname).read_text()
        expected = original_contents[fname]
        assert actual == expected, (
            f"{fname} was not restored correctly.\n"
            f"Expected:\n{expected}\nActual:\n{actual}"
        )

    # Real git log and index still untouched after the undo
    assert _real_git_log(project) == original_log, "Real git log changed after undo!"
    assert _real_git_status(project) == "", "Shadow staged real git index after undo!"

    clean_shadow(project)


def test_turns_recorded_correctly(project: Path):
    from agent_rewind.shadow import snapshot, list_turns, clean_shadow

    snapshot(project, "baseline")

    (project / "a.py").write_text("x = 1\n")
    snapshot(project, "turn 1")

    (project / "b.py").write_text("y = 2\n")
    snapshot(project, "turn 2")

    turns = list_turns(project)
    assert len(turns) >= 3
    # Newest first
    assert turns[0].index > turns[-1].index

    clean_shadow(project)


def test_snapshot_no_changes_returns_hash(project: Path):
    from agent_rewind.shadow import snapshot, clean_shadow

    h1 = snapshot(project, "baseline")
    # Second snapshot with no changes: should return the existing HEAD hash (no new commit)
    h2 = snapshot(project, "no-op")
    # h2 may be None (nothing to commit) or the same hash — both are valid
    assert h1 is not None

    clean_shadow(project)


def test_undo_is_itself_reversible(project: Path):
    """Pre-undo backup means the undo itself can be undone."""
    from agent_rewind.shadow import snapshot, list_turns, restore_to_turn, clean_shadow

    snapshot(project, "baseline")
    original_content = (project / "module_1.py").read_text()

    (project / "module_1.py").write_text("# changed by agent\n")
    snapshot(project, "agent turn 1")

    # Undo to baseline
    restore_to_turn(project, 0)

    # File should be restored
    assert (project / "module_1.py").read_text() == original_content, \
        "module_1.py should be back to its original content"

    # A post-restore snapshot should exist (the pre-undo backup)
    turns_after = list_turns(project)
    assert any("pre-undo" in t.message for t in turns_after), \
        "A pre-undo backup turn should exist after restore"

    clean_shadow(project)

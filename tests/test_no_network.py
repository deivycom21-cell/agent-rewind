"""Verify agent-rewind makes zero network connections."""

from __future__ import annotations

import socket
import unittest.mock as mock


def test_no_network_on_import():
    """Importing the package must not open any sockets."""
    original_socket = socket.socket

    connections = []

    class TrackingSocket:
        def __init__(self, *args, **kwargs):
            connections.append(("__init__", args, kwargs))
            self._sock = original_socket(*args, **kwargs)

        def connect(self, address):
            connections.append(("connect", address))
            raise OSError("Network access is blocked in agent-rewind tests")

        def __getattr__(self, name):
            return getattr(self._sock, name)

    with mock.patch("socket.socket", TrackingSocket):
        import importlib
        import sys
        # Remove cached modules to force re-import path check
        mods_to_remove = [k for k in sys.modules if k.startswith("agent_rewind")]
        for m in mods_to_remove:
            del sys.modules[m]

        import agent_rewind  # noqa: F401
        from agent_rewind import shadow  # noqa: F401
        from agent_rewind import display  # noqa: F401

    # Only connect() calls matter — no connections should have been attempted
    actual_connects = [c for c in connections if c[0] == "connect"]
    assert actual_connects == [], f"Unexpected network connections: {actual_connects}"


def test_no_network_on_snapshot(tmp_path):
    """Taking a snapshot must not open any network connections."""
    import subprocess

    # Init a real git repo
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    original_connect = socket.socket.connect
    connections = []

    def patched_connect(self, address):
        connections.append(address)
        raise OSError("Network access blocked")

    with mock.patch.object(socket.socket, "connect", patched_connect):
        from agent_rewind.shadow import snapshot
        (tmp_path / "new.txt").write_text("world")
        snapshot(tmp_path, "test snapshot")

    assert connections == [], f"Unexpected network connections during snapshot: {connections}"

"""Filesystem locations. Override the base dirs with env vars (used by tests)."""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    return Path(os.environ.get("A2A_PEERS_CONFIG_DIR", Path.home() / ".config" / "a2a-peers"))


def data_dir() -> Path:
    return Path(os.environ.get("A2A_PEERS_DATA_DIR", Path.home() / ".local" / "share" / "a2a-peers"))


def registry_file() -> Path:
    return config_dir() / "peers.json"


def state_file() -> Path:
    return data_dir() / "state.json"


def run_dir() -> Path:
    """Logs, pid files and sqlite task stores for launched opencode-a2a processes."""
    return data_dir() / "run"

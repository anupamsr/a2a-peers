"""Small helpers for reading/writing JSON files atomically."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def warn(message: str) -> None:
    """Log a warning to stderr (stdout is reserved for the MCP stdio channel)."""
    print(f"a2a-peers: warning: {message}", file=sys.stderr)


@contextmanager
def locked(path: Path, *, dir_mode: int = 0o700) -> Iterator[None]:
    """Hold an exclusive advisory lock (flock on a sibling `.<name>.lock`) for a read-modify-write
    of `path`, so concurrent processes (several MCP servers, the launcher) don't lose updates."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    fd = os.open(path.with_name(f".{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def write_json_atomic(path: Path, data: Any, *, mode: int = 0o600, dir_mode: int = 0o700) -> None:
    """Write `data` as JSON to `path` atomically, with file permissions `mode`.

    The parent directory is created (with `dir_mode`) if missing. A temp file is
    written in the same directory, chmod-ed, fsync-ed and then `os.replace`-d over
    the target so readers never observe a partial file.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

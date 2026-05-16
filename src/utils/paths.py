"""Filesystem path helpers for runtime state and logs."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Return the directory used for mutable state, logs, and journals."""
    raw = os.getenv("TRADING_DATA_DIR") or os.getenv("APP_DATA_DIR") or "."
    path = Path(raw).expanduser()
    resolved = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def data_path(path: str | os.PathLike[str]) -> Path:
    """Resolve a runtime file path, anchoring relative paths in ``data_dir``."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    resolved = data_dir() / candidate
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved

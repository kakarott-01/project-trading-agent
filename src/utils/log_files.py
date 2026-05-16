"""Small file-append helpers with bounded on-disk growth."""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
from typing import Any

TEXT_LOG_MAX_BYTES = 50 * 1024 * 1024
TEXT_LOG_BACKUPS = 3
JSONL_MAX_BYTES = 50 * 1024 * 1024
JSONL_BACKUPS = 3
_LOG_FILE_LOCK = threading.Lock()


def _rotate_text_file(path: str, backup_count: int) -> None:
    if backup_count <= 0:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return

    oldest = f"{path}.{backup_count}"
    try:
        os.remove(oldest)
    except FileNotFoundError:
        pass

    for idx in range(backup_count - 1, 0, -1):
        src = f"{path}.{idx}"
        dst = f"{path}.{idx + 1}"
        if os.path.exists(src):
            os.replace(src, dst)

    os.replace(path, f"{path}.1")


def _rotate_jsonl_file(path: str, backup_count: int) -> None:
    oldest = f"{path}.{backup_count}.gz"
    try:
        os.remove(oldest)
    except FileNotFoundError:
        pass

    for idx in range(backup_count - 1, 0, -1):
        src = f"{path}.{idx}.gz"
        dst = f"{path}.{idx + 1}.gz"
        if os.path.exists(src):
            os.replace(src, dst)

    with open(path, "rb") as src, gzip.open(f"{path}.1.gz", "wb") as dst:
        dst.writelines(src)
    os.remove(path)


def rotate_if_needed(
    path: str,
    *,
    max_bytes: int,
    backup_count: int,
    compress: bool = False,
) -> None:
    """Rotate ``path`` when it is over ``max_bytes`` before the next append."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return
        if compress:
            _rotate_jsonl_file(path, backup_count)
        else:
            _rotate_text_file(path, backup_count)
    except OSError as exc:
        logging.warning("Failed to rotate %s: %s", path, exc)


def append_text_log(
    path: str,
    text: str,
    *,
    max_bytes: int = TEXT_LOG_MAX_BYTES,
    backup_count: int = TEXT_LOG_BACKUPS,
    private: bool = False,
) -> None:
    """Append text to a rotated log file."""
    with _LOG_FILE_LOCK:
        rotate_if_needed(path, max_bytes=max_bytes, backup_count=backup_count)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        mode = 0o600 if private else 0o644
        fd = os.open(path, flags, mode)
        try:
            if private:
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                fd = -1
                handle.write(text)
        finally:
            if fd >= 0:
                os.close(fd)


def append_jsonl(
    path: str,
    entry: dict[str, Any],
    *,
    max_bytes: int = JSONL_MAX_BYTES,
    backup_count: int = JSONL_BACKUPS,
) -> None:
    """Append one JSON object to a compressed-rotated JSONL file."""
    with _LOG_FILE_LOCK:
        rotate_if_needed(
            path,
            max_bytes=max_bytes,
            backup_count=backup_count,
            compress=True,
        )
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")

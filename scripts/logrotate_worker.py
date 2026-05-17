#!/usr/bin/env python3
"""
scripts/logrotate_worker.py

Persistent sidecar service for log and JSONL file rotation.
Prevents disk exhaustion from unbounded log growth.

Handles:
  - diary.jsonl      (rotate at MAX_MB, keep last N entries)
  - alarms.jsonl     (rotate at MAX_MB, keep last N entries)
  - decisions.jsonl  (rotate at MAX_MB, compress old)
  - llm_requests.log (rotate at MAX_MB, compress old)
  - prompts.log      (rotate at MAX_MB, compress old)
  - trading.log      (already handled by RotatingFileHandler — monitor only)

The RotatingFileHandler in trading.log handles itself.
This worker handles the JSONL files that rotate differently.
"""

import gzip
import logging
import os
import shutil
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [logrotate] %(levelname)s %(message)s",
)
log = logging.getLogger("logrotate")

# ─── Config ───────────────────────────────────────────────────────────────────
LOG_DIR         = Path(os.environ.get("LOG_DIR", "/app/logs"))
DATA_DIR        = Path(os.environ.get("DATA_DIR", "/app/data"))
MAX_SIZE_MB     = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
MAX_SIZE_BYTES  = MAX_SIZE_MB * 1024 * 1024

# Files to monitor (relative to DATA_DIR or LOG_DIR)
JSONL_FILES = [
    ("diary.jsonl",     DATA_DIR),
    ("alarms.jsonl",    DATA_DIR),
    ("decisions.jsonl", DATA_DIR),
]
LOG_FILES = [
    ("llm_requests.log", LOG_DIR),
    ("prompts.log",      LOG_DIR),
]

BACKUP_COUNT = 3  # keep N compressed rotations


def rotate_file(file_path: Path) -> bool:
    """
    Rotate file_path if it exceeds MAX_SIZE_BYTES.
    Renames: file.jsonl → file.jsonl.1.gz (compressing)
    Shifts existing rotations up by 1.
    Returns True if rotation happened.
    """
    if not file_path.exists():
        return False

    size = file_path.stat().st_size
    if size < MAX_SIZE_BYTES:
        return False

    log.info(
        "Rotating %s (%.1f MB > %.0f MB limit)",
        file_path.name, size / 1024 / 1024, MAX_SIZE_MB,
    )

    # Shift existing rotations: .2.gz → .3.gz, .1.gz → .2.gz
    for i in range(BACKUP_COUNT - 1, 0, -1):
        old = Path(f"{file_path}.{i}.gz")
        new = Path(f"{file_path}.{i + 1}.gz")
        if old.exists():
            if new.exists():
                new.unlink()
            old.rename(new)

    # Compress current file to .1.gz
    gz_path = Path(f"{file_path}.1.gz")
    try:
        with open(file_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        # Truncate the original file (don't delete — keeps file descriptors valid)
        with open(file_path, "w") as f:
            pass
        log.info("Rotated %s → %s", file_path.name, gz_path.name)
        return True
    except Exception as exc:
        log.error("Rotation failed for %s: %s", file_path.name, exc)
        return False


def check_disk_space() -> None:
    """Warn if disk is getting full."""
    try:
        stat = shutil.disk_usage("/")
        used_pct = stat.used / stat.total * 100
        free_gb  = stat.free / (1024 ** 3)
        if used_pct > 85:
            log.critical(
                "DISK USAGE CRITICAL: %.1f%% used, %.2f GB free. "
                "Risk of bot crash due to disk full.",
                used_pct, free_gb,
            )
        elif used_pct > 70:
            log.warning(
                "Disk usage %.1f%% — %.2f GB free. Consider cleaning old logs.",
                used_pct, free_gb,
            )
    except Exception as exc:
        log.warning("Could not check disk space: %s", exc)


def main() -> None:
    log.info(
        "Logrotate worker starting — max_size=%dMB, interval=%ds",
        MAX_SIZE_MB, CHECK_INTERVAL,
    )

    while True:
        # Check disk space
        check_disk_space()

        # Rotate JSONL files
        for fname, base_dir in JSONL_FILES:
            rotate_file(base_dir / fname)

        # Rotate plain log files
        for fname, base_dir in LOG_FILES:
            rotate_file(base_dir / fname)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
scripts/backup_worker.py

Runs as a persistent Docker sidecar service.
Backs up trading state files every N seconds.
Optionally uploads to rclone remote (S3, Backblaze, etc.).

State files backed up:
  - active_trades.json   (open positions, critical for restart)
  - risk_state.json      (daily drawdown watermark, circuit breaker state)
  - diary.jsonl          (recent trading diary — rolling last 1000 entries)
  - alarms.jsonl         (recent alarms)
"""

import gzip
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [backup] %(levelname)s %(message)s",
)
log = logging.getLogger("backup")

# ─── Configuration ────────────────────────────────────────────────────────────
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL_SECONDS", "300"))
BACKUP_RETAIN   = int(os.environ.get("BACKUP_RETAIN_COUNT", "144"))  # 12h at 5min
BACKUP_DIR      = Path(os.environ.get("BACKUP_DIR", "/backups"))
DATA_DIR        = Path(os.environ.get("DATA_DIR", "/app/data"))
RCLONE_REMOTE   = os.environ.get("RCLONE_REMOTE", "").strip()

# Files to backup — in priority order
CRITICAL_FILES = [
    "active_trades.json",
    "risk_state.json",
]
IMPORTANT_FILES = [
    "diary.jsonl",
    "alarms.jsonl",
    "decisions.jsonl",
]


def timestamp_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def backup_once() -> bool:
    """Perform one backup cycle. Returns True on success."""
    ts = timestamp_str()
    backup_path = BACKUP_DIR / ts
    backup_path.mkdir(parents=True, exist_ok=True)

    backed_up = []
    failed    = []

    # ── Critical files — must succeed ────────────────────────────────────────
    for fname in CRITICAL_FILES:
        src = DATA_DIR / fname
        if not src.exists():
            log.debug("Critical file not found (may not exist yet): %s", fname)
            continue
        dst = backup_path / fname
        try:
            shutil.copy2(src, dst)
            backed_up.append(fname)
        except Exception as exc:
            log.error("FAILED to backup critical file %s: %s", fname, exc)
            failed.append(fname)

    # ── Important files — compress to save space ──────────────────────────────
    for fname in IMPORTANT_FILES:
        src = DATA_DIR / fname
        if not src.exists():
            continue
        dst = backup_path / (fname + ".gz")
        try:
            with open(src, "rb") as f_in, gzip.open(dst, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            backed_up.append(fname)
        except Exception as exc:
            log.warning("Failed to compress %s: %s", fname, exc)
            failed.append(fname)

    # ── Write backup manifest ─────────────────────────────────────────────────
    manifest = {
        "timestamp":  ts,
        "backed_up":  backed_up,
        "failed":     failed,
        "source_dir": str(DATA_DIR),
    }
    try:
        with open(backup_path / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)
    except Exception as exc:
        log.warning("Failed to write manifest: %s", exc)

    if backed_up:
        log.info("Backup %s: backed up %s", ts, ", ".join(backed_up))
    if failed:
        log.error("Backup %s: FAILED for %s", ts, ", ".join(failed))

    # ── Prune old backups ─────────────────────────────────────────────────────
    prune_old_backups()

    # ── Upload to rclone remote if configured ─────────────────────────────────
    if RCLONE_REMOTE and backed_up:
        upload_to_remote(backup_path, ts)

    return len(failed) == 0


def prune_old_backups() -> None:
    """Keep only the N most recent backups."""
    try:
        backups = sorted(
            [d for d in BACKUP_DIR.iterdir() if d.is_dir()],
            key=lambda d: d.name,
        )
        while len(backups) > BACKUP_RETAIN:
            oldest = backups.pop(0)
            shutil.rmtree(oldest, ignore_errors=True)
            log.debug("Pruned old backup: %s", oldest.name)
    except Exception as exc:
        log.warning("Prune error: %s", exc)


def upload_to_remote(backup_path: Path, ts: str) -> None:
    """Upload backup to rclone remote (if rclone is installed and configured)."""
    try:
        result = subprocess.run(
            ["rclone", "copy", str(backup_path), f"{RCLONE_REMOTE}/trading-bot/{ts}/"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            log.info("Uploaded backup %s to %s", ts, RCLONE_REMOTE)
        else:
            log.warning(
                "rclone upload failed (non-fatal): %s", result.stderr[:200]
            )
    except FileNotFoundError:
        log.warning("rclone not installed — remote backup skipped.")
    except Exception as exc:
        log.warning("rclone upload error (non-fatal): %s", exc)


def wait_for_data_dir(timeout: int = 120) -> None:
    """Wait until the data directory appears (bot may not have started yet)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if DATA_DIR.exists():
            return
        log.info("Waiting for data dir %s …", DATA_DIR)
        time.sleep(5)
    log.warning(
        "Data dir %s not found after %ds — starting backup anyway.", DATA_DIR, timeout
    )


def main() -> None:
    log.info(
        "Backup worker starting — interval=%ds, retain=%d, dir=%s, remote=%s",
        BACKUP_INTERVAL, BACKUP_RETAIN, BACKUP_DIR, RCLONE_REMOTE or "none",
    )
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    wait_for_data_dir()

    consecutive_failures = 0
    while True:
        try:
            ok = backup_once()
            if ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    log.critical(
                        "Backup has failed %d times in a row — check disk space and permissions.",
                        consecutive_failures,
                    )
        except Exception as exc:
            consecutive_failures += 1
            log.error("Backup worker uncaught exception: %s", exc, exc_info=True)

        time.sleep(BACKUP_INTERVAL)


if __name__ == "__main__":
    main()

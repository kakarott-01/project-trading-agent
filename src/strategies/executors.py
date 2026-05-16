"""Dedicated executors for blocking strategy implementations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

AI_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai_worker")
ALGO_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="algo_worker")

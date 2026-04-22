"""Security utilities for the trading agent API and configuration handling."""

from __future__ import annotations

import os
import logging
from aiohttp import web

# Files the /logs endpoint is allowed to serve. Period.
ALLOWED_LOG_FILES = {
    "llm_requests.log",
    "prompts.log",
    "decisions.jsonl",
    "trading.log",
}

MAX_LOG_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MB cap per response
MAX_DIARY_RESPONSE_BYTES = 1 * 1024 * 1024


def make_auth_middleware(secret: str):
    """Return an aiohttp middleware that enforces a static bearer token.

    If API_SECRET is empty, the middleware allows all requests (dev mode)
    and logs a warning on startup.
    """
    if not secret:
        logging.warning(
            "API_SECRET is not set. API endpoints are UNAUTHENTICATED. "
            "Set API_SECRET in .env before exposing this service."
        )

    @web.middleware
    async def _middleware(request: web.Request, handler):
        if not secret:
            return await handler(request)
        # Accept token in header OR query param (query param for curl convenience only)
        token = (
            request.headers.get("X-Api-Key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ")
            or request.query.get("key", "")
        )
        if token != secret:
            return web.Response(status=401, text="Unauthorized")
        return await handler(request)

    return _middleware


def safe_log_path(requested: str) -> str | None:
    """Validate and resolve a log file path.

    Returns the absolute path if valid, None if the request should be rejected.
    Only files in ALLOWED_LOG_FILES in the current working directory are served.
    """
    # Strip ALL directory components — basename only
    filename = os.path.basename(requested)
    if not filename:
        return None
    if filename not in ALLOWED_LOG_FILES:
        return None
    # Resolve to absolute path anchored at CWD
    resolved = os.path.realpath(os.path.join(os.getcwd(), filename))
    cwd = os.path.realpath(os.getcwd())
    # Confirm it's still inside CWD (defense in depth against symlink attacks)
    if not resolved.startswith(cwd + os.sep) and resolved != cwd:
        return None
    return resolved


class _SensitiveDict(dict):
    """Dict subclass that redacts sensitive keys in repr/str to prevent log leakage."""

    _REDACTED_KEYS = frozenset({
        "hyperliquid_private_key",
        "mnemonic",
        "anthropic_api_key",
        "openai_api_key",
        "gemini_api_key",
        "taapi_api_key",
        "openrouter_api_key",
    })

    def __repr__(self) -> str:
        safe = {
            k: ("***REDACTED***" if k in self._REDACTED_KEYS else v)
            for k, v in self.items()
        }
        return f"CONFIG({safe!r})"

    def __str__(self) -> str:
        return self.__repr__()
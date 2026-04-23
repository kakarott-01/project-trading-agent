"""Local API server for diary and log inspection."""

from __future__ import annotations

import json
import os

from aiohttp import web

from src.config import Settings
from src.utils.security import (
    MAX_DIARY_RESPONSE_BYTES,
    MAX_LOG_RESPONSE_BYTES,
    make_auth_middleware,
    safe_log_path,
)


class ApiServer:
    """Owns the lightweight local HTTP API."""

    def __init__(self, settings: Settings, diary_path: str = "diary.jsonl"):
        self.settings = settings
        self.diary_path = diary_path
        self.runner: web.AppRunner | None = None

    async def start(self) -> web.AppRunner:
        app = web.Application(
            middlewares=[make_auth_middleware(self.settings.api.secret)]
        )
        app.router.add_get("/diary", self.handle_diary)
        app.router.add_get("/logs", self.handle_logs)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(
            self.runner,
            self.settings.api.host,
            self.settings.api.port,
        )
        await site.start()
        return self.runner

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    async def handle_diary(self, request: web.Request) -> web.Response:
        try:
            raw = request.query.get("raw")
            download = request.query.get("download")
            if not os.path.exists(self.diary_path):
                return web.Response(text="", content_type="text/plain")

            with open(self.diary_path, "r", encoding="utf-8") as handle:
                data = handle.read(MAX_DIARY_RESPONSE_BYTES + 1)

            if len(data) > MAX_DIARY_RESPONSE_BYTES:
                data = data[-MAX_DIARY_RESPONSE_BYTES:]
                data = data[data.index("\n") + 1 :] if "\n" in data else data

            if raw or download:
                headers = {}
                if download:
                    headers["Content-Disposition"] = "attachment; filename=diary.jsonl"
                return web.Response(text=data, content_type="text/plain", headers=headers)

            limit = min(int(request.query.get("limit", "200")), 500)
            entries = []
            for line in data.strip().splitlines()[-limit:]:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return web.json_response({"entries": entries})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_logs(self, request: web.Request) -> web.Response:
        requested = request.query.get("path", "llm_requests.log")
        resolved = safe_log_path(requested)
        if resolved is None:
            return web.Response(status=403, text="Forbidden")
        if not os.path.exists(resolved):
            return web.Response(text="", content_type="text/plain")
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                data = handle.read(MAX_LOG_RESPONSE_BYTES + 1)
            if len(data) > MAX_LOG_RESPONSE_BYTES:
                data = data[-MAX_LOG_RESPONSE_BYTES:]

            download = request.query.get("download")
            if download:
                headers = {
                    "Content-Disposition": f"attachment; filename={os.path.basename(resolved)}"
                }
                return web.Response(text=data, content_type="text/plain", headers=headers)
            return web.Response(text=data, content_type="text/plain")
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

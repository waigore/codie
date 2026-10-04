"""Embedded FastAPI dashboard (Technical Spec §6.5).

An in-process FastAPI app served by the kernel during `codie start` and stopped
on shutdown. Localhost-only by default; when `host` is not loopback every route
(including SSE) requires `Authorization: Bearer <token>`. The dashboard holds no
GitHubClient — its only write path is `kernel_flags` (role enable/disable).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from codie.cache import Cache

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(cache: Cache, roster=None, token: str | None = None) -> FastAPI:
    app = FastAPI(title="codie dashboard", docs_url=None, redoc_url=None)
    app.state.cache = cache
    app.state.roster = roster

    def _auth(request: Request):
        if token and request.url.hostname not in {"127.0.0.1", "::1", "localhost"}:
            provided = request.headers.get("authorization", "")
            if provided != f"Bearer {token}":
                raise HTTPException(status_code=401, detail="unauthorized")
        return None

    @app.get("/")
    def index(request: Request) -> Response:
        _auth(request)
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/api/summary")
    def summary(request: Request):
        _auth(request)
        today = cache.today_cost()
        return {
            "repo": getattr(roster, "repo", ""),
            "uptime": "0s",
            "daily_cost_usd": today,
            "halted": False,
            "paused": False,
        }

    @app.get("/api/agents")
    def agents(request: Request):
        _auth(request)
        flags = cache.all_flags()
        entries = roster.as_dicts() if roster else []
        for e in entries:
            e["enabled"] = flags.get(e["role"], True)
        return {"agents": entries}

    @app.post("/api/agents/{role}/enabled")
    def set_enabled(role: str, payload: dict[str, Any], request: Request):
        _auth(request)
        if role not in {"planner", "coder", "reviewer", "tester", "orchestrator"}:
            raise HTTPException(status_code=404, detail=f"{role} is not toggleable (kernel always runs, M27)")
        enabled = bool(payload.get("enabled", True))
        cache.set_role_enabled(role, enabled)
        if roster:
            roster.set_enabled(role, enabled)
        return {"role": role, "enabled": enabled}

    @app.get("/api/logs")
    def logs(
        request: Request,
        agent: str | None = None,
        level: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ):
        _auth(request)
        rows = cache.list_events(agent=agent, level=level, after_id=after_id, limit=limit)
        return {"events": rows}

    @app.get("/api/stream")
    async def stream(request: Request):
        _auth(request)
        if token and request.url.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(status_code=401, detail="unauthorized")

        async def gen():
            seen = cache.conn and getattr(cache, "_last_event_id", 0) or 0  # type: ignore[union-attr]
            while True:
                rows = cache.list_events(after_id=seen, limit=50)
                for row in reversed(rows):
                    seen = row["id"]
                    yield f"data: {json.dumps({'type': 'event', **row})}\n\n"
                cache._last_event_id = seen  # type: ignore[attr-defined]
                await asyncio.sleep(2.0)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def serve(cache: Cache, roster=None, host: str = "127.0.0.1", port: int = 8640, token: str | None = None):
    """Run the dashboard (blocking)."""
    import uvicorn

    app = create_app(cache, roster=roster, token=token)
    uvicorn.run(app, host=host, port=port, log_level="warning")

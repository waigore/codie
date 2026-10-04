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


def create_app(cache: Cache, roster=None, token: str | None = None, kernel=None) -> FastAPI:
    app = FastAPI(title="codie dashboard", docs_url=None, redoc_url=None)
    app.state.cache = cache
    app.state.roster = roster
    app.state.kernel = kernel
    app.state.started_at = asyncio.get_event_loop().time() if kernel is None else 0.0

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
        if kernel is not None:
            state = kernel.summary_state
            return {
                "repo": state["repo"],
                "uptime": state["uptime"],
                "daily_cost_usd": state["daily_cost_usd"],
                "halted": state["halted"],
                "paused": state["paused"],
            }
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
        return StreamingResponse(iter_sse(cache, roster), media_type="text/event-stream")

    return app


async def iter_sse(cache: Cache, roster=None, max_chunks: int | None = None):
    """SSE chunk generator: agent events + roster changes (I-23)."""
    seen = 0
    if cache.conn is not None:
        seen = getattr(cache, "_last_event_id", 0) or 0
    last_roster = ""
    id_ = 0
    while max_chunks is None or id_ < max_chunks:
        id_ += 1
        rows = cache.list_events(after_id=seen, limit=50)
        for row in reversed(rows):
            seen = row["id"]
            yield f"data: {json.dumps({'type': 'event', **row})}\n\n"
        cache._last_event_id = seen  # type: ignore[attr-defined]
        if roster is not None:
            current = json.dumps(roster.as_dicts(), sort_keys=True)
            if current != last_roster:
                last_roster = current
                yield f"data: {json.dumps({'type': 'roster', 'agents': roster.as_dicts()})}\n\n"
        await asyncio.sleep(0.1)


def serve(cache: Cache, roster=None, host: str = "127.0.0.1", port: int = 8640, token: str | None = None):
    """Run the dashboard (blocking)."""
    import uvicorn

    app = create_app(cache, roster=roster, token=token)
    uvicorn.run(app, host=host, port=port, log_level="warning")

"""Dashboard integration tests (Technical Spec §6.5; M5; kernel toggle 404 = M27)."""

import pytest
from fastapi.testclient import TestClient

from codie.cache import Cache
from codie.dashboard.roster import Roster
from codie.dashboard.server import create_app


@pytest.fixture
def client(tmp_path):
    cache = Cache().open_or_rebuild(tmp_path / "d.db")
    roster = Roster()
    roster.repo = "acme/test"
    app = create_app(cache, roster=roster)
    return TestClient(app)


def test_summary_and_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "codie" in r.text.lower()
    s = client.get("/api/summary")
    assert s.status_code == 200
    assert s.json()["repo"] == "acme/test"


def test_agents_roster_and_toggle(client):
    r = client.get("/api/agents")
    roles = {a["role"] for a in r.json()["agents"]}
    assert roles == {"orchestrator", "planner", "coder", "reviewer", "tester"}
    # toggle coder off -> refusal at dispatch gate enforced separately; here flag persisted
    r = client.post("/api/agents/coder/enabled", json={"enabled": False})
    assert r.json()["enabled"] is False
    r = client.get("/api/agents")
    coder = next(a for a in r.json()["agents"] if a["role"] == "coder")
    assert coder["enabled"] is False


def test_kernel_toggle_is_404_m27(client):
    r = client.post("/api/agents/kernel/enabled", json={"enabled": False})
    assert r.status_code == 404


def test_logs_filtering(client):
    client.app.state.cache.add_event("tester", "hello", level="info", kind="decision", run_id="r1")
    client.app.state.cache.add_event("kernel", "secret-thing", level="error")
    r = client.get("/api/logs?agent=tester")
    events = r.json()["events"]
    assert len(events) == 1 and events[0]["message"] == "hello"
    r = client.get("/api/logs?level=error")
    assert r.json()["events"][0]["message"] == "secret-thing"


def test_toggle_reflected_in_dispatch_gate(tmp_path):
    """Disabling a role blocks its next dispatch (observed via kernel refusal)."""
    from tests.conftest import add_prd, fresh_fake, make_settings

    from codie.dispatch import StubRunner
    from codie.models import WorkItem
    from codie.orchestrator import Kernel

    gh = fresh_fake()
    add_prd(gh)
    cache = Cache().open_or_rebuild(tmp_path / "k.db")
    cache.set_role_enabled("coder", False)
    settings = make_settings()
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=StubRunner(lambda r, i, p: "{}"))
    kernel.cycle()
    item = WorkItem(kind="Implement", role="coder", entity="x", issue_number=9, rule=8)
    from codie.github.fetch import fetch_snapshot
    from codie.state.derive import derive

    state = derive(fetch_snapshot(gh, settings.project.repo, settings, full=True), settings)
    kernel._current_queue = [item]
    outcome = kernel.dispatch_work_item(item, state)
    assert outcome.status == "refused"
    assert "disabled" in outcome.error


def test_summary_reads_real_kernel_state(tmp_path):
    """I-23: /api/summary reflects the kernel's halt/pause state, not hardcoded values."""
    from tests.conftest import add_prd, fresh_fake, make_settings

    gh = fresh_fake()
    add_prd(gh)
    cache = Cache().open_or_rebuild(tmp_path / "s.db")
    settings = make_settings()
    settings.project.repo = "acme/test"
    from codie.dispatch import StubRunner
    from codie.orchestrator import Kernel

    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=StubRunner(lambda r, i, p: "{}"))
    outcome = kernel.cycle()
    outcome.halted = True
    kernel._last_outcome = outcome
    kernel.start_time = kernel.start_time - __import__("datetime").timedelta(seconds=90)
    app = create_app(cache, roster=Roster(), kernel=kernel)
    client = TestClient(app)
    summary = client.get("/api/summary").json()
    assert summary["repo"] == "acme/test"
    assert summary["halted"] is True
    assert summary["uptime"] == "90s"  # real uptime, not a hardcoded string


def test_stream_delivers_events(tmp_path):
    """I-23: /api/stream delivers a dispatch event and roster changes (SSE)."""
    import asyncio

    from codie.dashboard.server import iter_sse

    cache = Cache().open_or_rebuild(tmp_path / "st.db")
    roster = Roster()
    roster.begin("coder", "Implement #9", run_id="coder-9-1")
    cache.add_event("coder", "dispatched Implement", kind="dispatch")

    async def collect():
        chunks = []
        async for chunk in iter_sse(cache, roster, max_chunks=3):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())
    joined = "\n".join(chunks)
    assert "dispatched Implement" in joined  # the dispatch event reached the wire
    assert '"type": "roster"' in joined or "roster" in joined


def _stream_client(tmp_path):
    from fastapi.testclient import TestClient

    cache = Cache().open_or_rebuild(tmp_path / "st.db")
    roster = Roster()
    app = create_app(cache, roster=roster)
    return TestClient(app)

"""Unit tests for cache.py — SQLite projection (§5.8) and transitions matrix."""

import pytest

from codie.cache import Cache
from codie.models import BranchHeads, ProjectState
from codie.state.transitions import load_transitions


def test_open_or_rebuild(tmp_path):
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    cache.save_snapshot(1, ProjectState(repo="a/b", branches=BranchHeads()))
    snaps = cache.last_snapshots(1)
    assert snaps[0]["repo"] == "a/b"


def test_corrupt_db_rebuilt(tmp_path):
    path = tmp_path / "c.db"
    path.write_bytes(b"not a sqlite database")
    cache = Cache().open_or_rebuild(path)
    cache.add_event("kernel", "hello")
    assert len(cache.list_events(agent="kernel")) == 1


def test_poll_state_and_cycle(tmp_path):
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    assert cache.get_poll_state()["cycle_count"] == 0
    cycle = cache.bump_cycle("2026-01-01T00:00:00+00:00")
    assert cycle == 1
    assert cache.get_poll_state()["last_poll_ts"] == "2026-01-01T00:00:00+00:00"


def test_ledger_accumulates(tmp_path):
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    cache.add_cost(1.5)
    cache.add_cost(0.5)
    assert cache.today_cost() == pytest.approx(2.0)


def test_runs_and_events(tmp_path):
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    rid = cache.add_run('{"kind": "DraftSpecs"}', "applied", cost_usd=0.4, tokens={"input": 1000, "output": 200})
    cache.finish_run(rid, "applied", 0.4, {"input": 1000, "output": 200})
    runs = cache.list_runs()
    assert runs[0]["status"] == "applied"
    cache.add_event("tester", "run done", level="info", kind="decision", run_id="r1")
    events = cache.list_events(agent="tester")
    assert events[0]["message"] == "run done"


def test_kernel_flags_default_on_and_toggle(tmp_path):
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    assert cache.role_enabled("coder") is True
    cache.set_role_enabled("coder", False)
    assert cache.role_enabled("coder") is False
    assert cache.all_flags()["coder"] is False


def test_transitions_matrix_has_all_edges():
    table = load_transitions()
    assert "feature_transitions" in table
    assert "task_transitions" in table
    assert "mutation_matrix" in table
    feats = table["feature_transitions"]
    assert any(e["to"] == "speccing" and "kernel" in e["actors"] for e in feats)
    tasks = table["task_transitions"]
    assert any(
        e["from"] == "ready" and e["to"] == "in-progress" and {"coder", "tester"} & set(e["actors"]) for e in tasks
    )
    matrix = table["mutation_matrix"]
    assert "reviewer" in matrix and "tester" in matrix and "human" in matrix

"""SQLite local projection (Technical Spec §5.8).

The cache is *never* authoritative: GitHub is the single source of truth. The
cache supplies only the poll cursor optimization and the daily cost ledger (the
one intentional local exception, C15). A missing/corrupt/schema-mismatched DB
is deleted and recreated.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from codie.models import ProjectState


def state_dir(owner: str, repo: str) -> Path:
    return Path.home() / ".codie" / "state" / owner / repo


def cache_path(owner: str, repo: str) -> Path:
    return state_dir(owner, repo) / "cache.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    cycle INTEGER PRIMARY KEY,
    derived_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS poll_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_poll_ts TEXT,
    cycle_count INTEGER NOT NULL DEFAULT 0,
    last_fast_run TEXT,
    last_full_run TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key TEXT UNIQUE,
    work_item_json TEXT NOT NULL,
    status TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    tokens TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    trace_path TEXT
);
CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    role TEXT NOT NULL,
    run_id TEXT,
    level TEXT NOT NULL DEFAULT 'info',
    kind TEXT NOT NULL DEFAULT 'decision',
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kernel_flags (
    role TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ledger (
    day TEXT PRIMARY KEY,
    cost_usd REAL NOT NULL DEFAULT 0
);
"""


class Cache:
    def __init__(self, path: Path | None = None):
        self.path: Path | None = path
        self.conn: sqlite3.Connection | None = None

    def open_or_rebuild(self, path: Path | None = None) -> Cache:
        path = path or self.path
        if path is None:
            raise ValueError("cache path required")
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.executescript(_SCHEMA)
            conn.commit()
            self._validate_schema(conn)
        except sqlite3.Error:
            path.unlink(missing_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.executescript(_SCHEMA)
            conn.commit()
        self.conn = conn
        return self

    def _validate_schema(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('snapshots','poll_state','runs','agent_events','kernel_flags','ledger')"
        ).fetchall()
        have = {r[0] for r in rows}
        required = {"snapshots", "poll_state", "runs", "agent_events", "kernel_flags", "ledger"}
        if not required.issubset(have):
            raise sqlite3.DatabaseError("schema mismatch")

    # -- snapshots ------------------------------------------------------------
    def save_snapshot(self, cycle: int, state: ProjectState) -> None:
        assert self.conn is not None
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots(cycle, derived_json) VALUES (?, ?)",
            (cycle, state.model_dump_json()),
        )
        self._prune_snapshots()

    def last_snapshots(self, n: int = 2) -> list[dict]:
        assert self.conn is not None
        rows = self.conn.execute(
            "SELECT cycle, derived_json FROM snapshots ORDER BY cycle DESC LIMIT ?", (n,)
        ).fetchall()
        out = []
        for _cycle, blob in sorted(rows, key=lambda r: r[0]):
            out.append(json.loads(blob))
        return out

    def _prune_snapshots(self, keep: int = 10) -> None:
        assert self.conn is not None
        self.conn.execute(
            "DELETE FROM snapshots WHERE cycle NOT IN (SELECT cycle FROM snapshots ORDER BY cycle DESC LIMIT ?)",
            (keep,),
        )
        self.conn.commit()

    # -- poll state -------------------------------------------------------------
    def get_poll_state(self) -> dict:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT id, last_poll_ts, cycle_count, last_fast_run, last_full_run FROM poll_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return {
                "last_poll_ts": None,
                "cycle_count": 0,
                "last_fast_run": None,
                "last_full_run": None,
            }
        return {
            "last_poll_ts": row[1],
            "cycle_count": row[2],
            "last_fast_run": row[3],
            "last_full_run": row[4],
        }

    def set_poll_state(
        self,
        last_poll_ts: str | None = None,
        cycle_count: int | None = None,
        last_fast_run: str | None = None,
        last_full_run: str | None = None,
    ) -> None:
        assert self.conn is not None
        cur = self.get_poll_state()
        new = {
            "last_poll_ts": last_poll_ts if last_poll_ts is not None else cur["last_poll_ts"],
            "cycle_count": cycle_count if cycle_count is not None else cur["cycle_count"],
            "last_fast_run": last_fast_run if last_fast_run is not None else cur["last_fast_run"],
            "last_full_run": last_full_run if last_full_run is not None else cur["last_full_run"],
        }
        self.conn.execute(
            (
                "INSERT OR REPLACE INTO poll_state(id, last_poll_ts, cycle_count, last_fast_run, last_full_run) "
                "VALUES (1, ?, ?, ?, ?)"
            ),
            (new["last_poll_ts"], new["cycle_count"], new["last_fast_run"], new["last_full_run"]),
        )
        self.conn.commit()

    def bump_cycle(self, now_iso: str) -> int:
        cur = self.get_poll_state()
        cycle = (cur["cycle_count"] or 0) + 1
        self.set_poll_state(last_poll_ts=now_iso, cycle_count=cycle)
        return cycle

    # -- runs --------------------------------------------------------------------
    def add_run(
        self,
        work_item: str,
        status: str,
        cost_usd: float = 0.0,
        tokens: dict | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        trace_path: str | None = None,
    ) -> int:
        assert self.conn is not None
        tokens = tokens or {}
        cur = self.conn.execute(
            (
                "INSERT INTO runs(work_item_json, status, cost_usd, tokens, started_at, finished_at, trace_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)"
            ),
            (work_item, status, cost_usd, json.dumps(tokens), started_at, finished_at, trace_path),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def begin_run(self, run_key: str, work_item: str, started_at: str | None = None) -> None:
        """Record that a run handle started (§5.8 run table)."""
        assert self.conn is not None
        self.conn.execute(
            (
                "INSERT INTO runs(run_key, work_item_json, status, started_at) VALUES (?, ?, 'started', ?) "
                "ON CONFLICT(run_key) DO UPDATE SET status='started'"
            ),
            (run_key, work_item, started_at or now_iso()),
        )
        self.conn.commit()

    def complete_run(
        self,
        run_key: str,
        status: str,
        cost_usd: float = 0.0,
        tokens: dict | None = None,
        finished_at: str | None = None,
        trace_path: str | None = None,
    ) -> None:
        assert self.conn is not None
        tokens = tokens or {}
        self.conn.execute(
            (
                "UPDATE runs SET status = ?, cost_usd = ?, tokens = ?, finished_at = COALESCE(?, finished_at), "
                "trace_path = ? WHERE run_key = ?"
            ),
            (status, cost_usd, json.dumps(tokens), finished_at or now_iso(), trace_path, run_key),
        )
        self.conn.commit()

    def get_run(self, run_key: str) -> dict | None:
        assert self.conn is not None
        row = self.conn.execute("SELECT * FROM runs WHERE run_key = ?", (run_key,)).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self.conn.execute("SELECT * FROM runs LIMIT 0").description]
        d = dict(zip(cols, row, strict=False))
        d["tokens"] = json.loads(d["tokens"] or "{}")
        return d

    def finish_run(
        self,
        run_id: int,
        status: str,
        cost_usd: float,
        tokens: dict,
        finished_at: str | None = None,
    ) -> None:
        assert self.conn is not None
        self.conn.execute(
            "UPDATE runs SET status = ?, cost_usd = ?, tokens = ?, finished_at = ? WHERE id = ?",
            (status, cost_usd, json.dumps(tokens), finished_at or now_iso(), run_id),
        )
        self.conn.commit()

    def list_runs(self, limit: int = 50) -> list[dict]:
        assert self.conn is not None
        rows = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM runs LIMIT 0").description]
        out = []
        for row in rows:
            d = dict(zip(cols, row, strict=False))
            d["tokens"] = json.loads(d["tokens"] or "{}")
            out.append(d)
        return out

    # -- agent events -----------------------------------------------------------
    def add_event(
        self,
        role: str,
        message: str,
        level: str = "info",
        kind: str = "decision",
        run_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        assert self.conn is not None
        self.conn.execute(
            (
                "INSERT INTO agent_events(ts, role, run_id, level, kind, message, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)"
            ),
            (now_iso(), role, run_id, level, kind, message, json.dumps(payload or {})),
        )
        self.conn.commit()

    def list_events(
        self,
        agent: str | None = None,
        level: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        assert self.conn is not None
        q = "SELECT * FROM agent_events WHERE 1=1"
        params: list = []
        if agent:
            q += " AND role = ?"
            params.append(agent)
        if level:
            q += " AND level = ?"
            params.append(level)
        if after_id:
            q += " AND id > ?"
            params.append(after_id)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(q, params).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM agent_events LIMIT 0").description]
        out = []
        for row in rows:
            d = dict(zip(cols, row, strict=False))
            d["payload_json"] = json.loads(d["payload_json"] or "{}")
            out.append(d)
        return out

    # -- kernel flags ------------------------------------------------------------
    def role_enabled(self, role: str) -> bool:
        assert self.conn is not None
        row = self.conn.execute("SELECT enabled FROM kernel_flags WHERE role = ?", (role,)).fetchone()
        return row[0] != 0 if row else True

    def set_role_enabled(self, role: str, enabled: bool) -> None:
        assert self.conn is not None
        self.conn.execute(
            "INSERT OR REPLACE INTO kernel_flags(role, enabled) VALUES (?, ?)",
            (role, int(enabled)),
        )
        self.conn.commit()

    def all_flags(self) -> dict[str, bool]:
        assert self.conn is not None
        rows = self.conn.execute("SELECT role, enabled FROM kernel_flags").fetchall()
        return {r[0]: r[1] != 0 for r in rows}

    # -- ledger -------------------------------------------------------------------
    def today_cost(self, day: str | None = None) -> float:
        assert self.conn is not None
        day = day or utc_today()
        row = self.conn.execute("SELECT cost_usd FROM ledger WHERE day = ?", (day,)).fetchone()
        return row[0] if row else 0.0

    def add_cost(self, cost_usd: float, day: str | None = None) -> float:
        assert self.conn is not None
        day = day or utc_today()
        self.conn.execute(
            (
                "INSERT INTO ledger(day, cost_usd) VALUES (?, ?) ON CONFLICT(day) DO UPDATE "
                "SET cost_usd = cost_usd + excluded.cost_usd"
            ),
            (day, cost_usd),
        )
        self.conn.commit()
        return self.today_cost(day)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def utc_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()

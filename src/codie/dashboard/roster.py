"""Per-role status model (Technical Spec §6.5 roster)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

ROLES = ["orchestrator", "planner", "coder", "reviewer", "tester"]
KERNEL_ONLY = "kernel"


@dataclass
class RosterEntry:
    role: str
    enabled: bool = True
    state: str = "idle"  # working | idle
    current_title: str = ""
    current_url: str = ""
    current_since: str = ""
    current_run_id: str = ""
    last_title: str = ""
    last_outcome: str = ""
    today_cost: float = 0.0


@dataclass
class Roster:
    entries: dict[str, RosterEntry] = field(default_factory=dict)

    def __init__(self) -> None:
        self.entries = {role: RosterEntry(role=role) for role in ROLES}

    def begin(self, role: str, title: str, url: str = "", run_id: str = "") -> None:
        entry = self.entries.get(role)
        if entry is None:
            return
        entry.state = "working"
        entry.current_title = title
        entry.current_url = url
        entry.current_since = datetime.now(UTC).isoformat()
        entry.current_run_id = run_id

    def finish(self, role: str, outcome: str) -> None:
        entry = self.entries.get(role)
        if entry is None:
            return
        entry.state = "idle"
        entry.last_title = entry.current_title
        entry.last_outcome = outcome
        entry.current_title = ""
        entry.current_url = ""
        entry.current_run_id = ""

    def set_enabled(self, role: str, enabled: bool) -> None:
        entry = self.entries.get(role)
        if entry is not None:
            entry.enabled = enabled

    def as_dicts(self) -> list[dict]:
        return [entry.__dict__ | {"role": entry.role} for entry in self.entries.values()]

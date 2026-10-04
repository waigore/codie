"""The Orchestrator agent crew (Technical Spec §6.1, §7.1).

An LLM crew with no memory that supplies judgment *within* the kernel's lawful
queue: it sequences dispatches, triages anomalies, and interprets non-standard
conventions — always through the gated tools below, which the kernel enforces.
A zero-tool-call cycle falls through to the C19 idle-dispatch counter.
"""

from __future__ import annotations

import json
from typing import Any


class OrchestratorBridge:
    """The deterministic, kernel-gated tool surface the agent calls (§6.1)."""

    def __init__(self, kernel, projected, queue: list[Any]):
        self.kernel = kernel
        self.projected = projected
        self.queue = queue

    def get_project_state(self) -> dict:
        from codie.context import board_text, describe_anomalies, describe_violations

        return {
            "board": board_text(self.projected),
            "violations": describe_violations(self.projected),
            "anomalies": describe_anomalies(self.projected),
        }

    def compute_work_queue(self) -> list[dict]:
        return [item.model_dump(mode="json") for item in self.queue]

    def dispatch_work_item(self, entity: str | int) -> dict:
        """Kernel-gated: only current queue items dispatch; a disabled role is refused."""
        item = _find_item(self.queue, entity)
        if item is None:
            return {"status": "refused", "reason": f"no such lawful queue item {entity!r}"}
        outcome = self.kernel.dispatch_work_item(item, self.projected)
        return {"status": outcome.status, "kind": item.kind, "error": outcome.error}

    def defer_work_item(self, entity: str | int, reason: str) -> dict:
        item = _find_item(self.queue, entity)
        if item is None:
            return {"status": "refused", "reason": f"no such lawful queue item {entity!r}"}
        return {
            "status": "refused",
            "reason": "deferral is kernel-mutating; the kernel posts the codie:defer marker",
        }

    def apply_adoption(self, issue: int, type_: str, status: str, note: str) -> dict:
        return {"status": "refused", "reason": "adoption is kernel-mutating and must pass the transition table"}

    def escalate_to_human(self, issue: int, summary: str) -> dict:
        return {"status": "refused", "reason": "escalation is kernel-mutating (flag:needs-human)"}

    def annotate(self, issue: int, comment: str) -> dict:
        """Audit-trail comment; the agent is instructed to use it sparingly (§7.6)."""
        try:
            self.kernel.client.comment(issue, comment)
            return {"status": "ok"}
        except Exception as exc:  # pragma: no cover - client errors surface loudly
            return {"status": "failed", "error": str(exc)}

    def get_conventions(self) -> dict:
        from codie.context import conventions_digest

        return {"conventions": conventions_digest(self.kernel.settings, self.kernel.workspace)}

    def get_budgets(self) -> dict:
        daily = self.kernel.cache.today_cost() if self.kernel.cache else 0.0
        return {
            "daily_cost_usd": daily,
            "daily_cap_usd": self.kernel.settings.budgets.max_llm_cost_usd_per_day,
            "per_run_cap_usd": self.kernel.settings.per_run_cap,
        }

    def get_run_history(self, issue: int | None = None) -> list[dict]:
        if self.kernel.cache is None:
            return []
        runs = self.kernel.cache.list_runs(50)
        out = []
        for r in runs:
            try:
                wi = json.loads(r["work_item_json"] or "{}")
            except json.JSONDecodeError:
                wi = {}
            if (
                issue is not None
                and wi.get("issue_number") not in (None, issue)
                and wi.get("pr_number")
                not in (
                    None,
                    issue,
                )
            ):
                continue
            out.append(
                {"kind": wi.get("kind"), "status": r["status"], "cost_usd": r["cost_usd"], "tokens": r["tokens"]}
            )
        return out[:20]


def _find_item(queue: list[Any], entity: str | int):
    entity = str(entity).strip().lstrip("#")
    for item in queue:
        if entity in {str(item.entity), str(item.issue_number), str(item.pr_number)}:
            return item
    return None


def parse_decision(text: str) -> str | None:
    """Extract a dispatch decision (entity key) from the Orchestrator's JSON reply."""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data.get("dispatch") or data.get("entity")


def run_cycle(bridge: OrchestratorBridge, crew_runner, pack, settings) -> str:
    """Kick the Orchestrator crew over the gated tool surface; returns its raw reply."""
    from codie.models import WorkItem

    item = WorkItem(kind="NeedsHuman", role="orchestrator", entity="orchestrator cycle")
    tools = list(tool_surface(bridge).values())
    result = crew_runner.run_tool_agent("orchestrator", item, pack, settings, tools=tools)
    return result.text or ""


def tool_surface(bridge: OrchestratorBridge) -> dict:
    """Return the bridge bound as callables (name → callable)."""
    return {
        "get_project_state": bridge.get_project_state,
        "compute_work_queue": bridge.compute_work_queue,
        "dispatch_work_item": bridge.dispatch_work_item,
        "defer_work_item": bridge.defer_work_item,
        "apply_adoption": bridge.apply_adoption,
        "escalate_to_human": bridge.escalate_to_human,
        "annotate": bridge.annotate,
        "get_conventions": bridge.get_conventions,
        "get_budgets": bridge.get_budgets,
        "get_run_history": bridge.get_run_history,
    }

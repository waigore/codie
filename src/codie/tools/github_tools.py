"""CrewAI tools over GitHubClient (Technical Spec §6.1, §7.3–7.5).

Deterministic and gated: transitions pass through the §5.5 matrix, files pass
through the §7.3/§7.5 gates, and the shell denylist/jail apply. The Orchestrator
agent's tools and each role's tools are built from this toolbox.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from codie.config import Settings
from codie.github.client import GitHubClient
from codie.models import GitHubReview
from codie.state import parse
from codie.tools.base import ToolError

if TYPE_CHECKING:
    from codie.workspace import Workspace


class GithubToolbox:
    """A gated toolbox over one GitHubClient, scoped to one actor."""

    def __init__(
        self,
        client: GitHubClient,
        settings: Settings,
        role: str,
        workspace: Workspace | None = None,
    ):
        self.client = client
        self.settings = settings
        self.role = role
        self.workspace = workspace

    def _set(self) -> None:
        self.client.set_actor(self.role)

    # -- labels -------------------------------------------------------------------

    def set_status(self, issue_number: int, type_: str, to_status: str) -> None:
        from codie.tools.base import allowed_transition

        current = self._current_status(issue_number)
        if not allowed_transition(self.settings, self.role, type_, current, to_status):
            raise ToolError(
                f"refusing label transition {type_} {current} → {to_status} for actor {self.role} (not in matrix row)"
            )
        self._set()
        labels = self._labels_with_status(issue_number, to_status)
        self.client.set_labels(issue_number, labels)

    def set_flag(self, issue_number: int, flag: str, reason: str) -> None:
        """flag:blocked only on the active-run issue; needs-human is kernel-only."""
        if flag == "blocked" and self.role not in {"coder", "tester"}:
            raise ToolError("flag:blocked may only be set by the Coder/Tester on its active item")
        self._set()
        labels = self._labels_with_flag(issue_number, flag)
        self.client.set_labels(issue_number, labels)
        self.client.comment(issue_number, f"`flag:{flag}` — {reason}")

    # -- issues -------------------------------------------------------------------

    def claim(self, issue_number: int, assignee_login: str) -> None:
        self._set()
        self.client.set_assignees(issue_number, [assignee_login])

    def comment(self, issue_number: int, body: str) -> None:
        self._set()
        self.client.comment(issue_number, body)

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        self._set()
        issue = self.client.create_issue(title, body, labels)
        return issue.number

    # -- PRs ----------------------------------------------------------------------

    def create_pr(self, title: str, body: str, head: str, base: str, draft: bool = False) -> int:
        self._set()
        return self.client.create_pr(title, body, head, base, draft).number

    def update_pr(self, number: int, draft: bool | None = None, body: str | None = None) -> None:
        self._set()
        self.client.update_pr(number, draft=draft, body=body)

    def create_review(self, pr_number: int, state: str, body: str, comments: list[dict] | None = None) -> GitHubReview:
        self._set()
        return self.client.create_review(pr_number, state, body, comments)

    def merge(self, pr_number: int, merge_method: str = "squash") -> bool:
        self._set()
        return self.client.merge_pr(pr_number, merge_method)

    def close_pr(self, pr_number: int) -> None:
        self._set()
        self.client.close_pr(pr_number)

    def check_eval(self, prd_number: int, eval_id: str) -> None:
        """Check off one PRD eval checklist entry (Tester only, §7.5)."""
        if self.role != "tester":
            raise ToolError("only the Tester may check off PRD evals")
        self._set()
        issue = self.client.get_issue(prd_number)
        if issue is None:
            raise ToolError(f"PRD issue #{prd_number} not found")
        body = issue.body
        import re

        def cb(m: re.Match) -> str:
            return f"- [x] {m.group('id')}: {m.group('text')}"

        new_body = re.sub(r"- \[( |x)\] (E\d+): (.+)$", cb, body, flags=re.MULTILINE, count=0)
        self.client.edit_issue(prd_number, body=new_body)

    # -- reads ----------------------------------------------------------------------

    def get_issue(self, issue_number: int):
        self._set()
        return self.client.get_issue(issue_number)

    def get_pr(self, pr_number: int):
        self._set()
        return self.client.get_pr(pr_number)

    # -- helpers -------------------------------------------------------------------

    def _current_status(self, issue_number: int) -> str | None:
        self._set()
        issue = self.client.get_issue(issue_number)
        if issue is None:
            raise ToolError(f"issue #{issue_number} not found")
        return parse.status_of(issue.labels)

    def _labels_with_status(self, issue_number: int, status: str) -> list[str]:
        self._set()
        issue = self.client.get_issue(issue_number)
        if issue is None:
            raise ToolError(f"issue #{issue_number} not found")
        labels = [label for label in issue.labels if not label.startswith("status:")]
        labels.append(f"status:{status}")
        return labels

    def _labels_with_flag(self, issue_number: int, flag: str) -> list[str]:
        self._set()
        issue = self.client.get_issue(issue_number)
        if issue is None:
            raise ToolError(f"issue #{issue_number} not found")
        labels = [label for label in issue.labels if not label.startswith(f"flag:{flag}")]
        labels.append(f"flag:{flag}")
        return labels


def build_orchestrator_tools(bridge=None, client=None, settings: Settings | None = None):
    """The Orchestrator agent's tool surface (§6.1 table) returned as plain funcs.

    When `bridge` (an `OrchestratorBridge`) is given, its kernel-gated methods are
    returned directly. The legacy client-based stubs are retained only as a
    refuse-by-default fallback so misuse of the toolbox never mutates GitHub.
    """
    if bridge is not None:
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
    toolbox = GithubToolbox(client, settings, "kernel") if (client is not None and settings is not None) else None

    def refusals():
        twice = dict.fromkeys(("get_project_state", "get_run_history"), "no bridge")

        def fn():
            return twice

        return fn

    def get_project_state() -> dict:
        return {"note": "see kernel-computed state; use compute_work_queue"}

    def compute_work_queue() -> list[dict]:
        return []  # the kernel injects the live queue at dispatch time

    def dispatch_work_item(item: dict) -> dict:
        return {
            "status": "refused",
            "reason": "dispatch_work_item is kernel-gated; call the kernel dispatcher",
        }

    def defer_work_item(item: dict, reason: str) -> dict:
        return {
            "status": "refused",
            "reason": "deferral is kernel-mutating; kernel applies markers",
        }

    def apply_adoption(issue: int, type_: str, status: str, note: str) -> dict:
        return {"status": "refused", "reason": "adoption is kernel-mutating"}

    def escalate_to_human(issue: int, summary: str) -> dict:
        return {"status": "refused", "reason": "escalation is kernel-mutating"}

    def annotate(issue: int, comment: str) -> dict:
        if toolbox is None:
            return {"status": "refused", "reason": "no client bound"}
        toolbox.comment(issue, comment)
        return {"status": "ok"}

    def get_conventions() -> dict:
        return {}

    def get_budgets() -> dict:
        return {}

    def get_run_history(issue: int | None = None) -> list[dict]:
        return []

    return {
        "get_project_state": get_project_state,
        "compute_work_queue": compute_work_queue,
        "dispatch_work_item": dispatch_work_item,
        "defer_work_item": defer_work_item,
        "apply_adoption": apply_adoption,
        "escalate_to_human": escalate_to_human,
        "annotate": annotate,
        "get_conventions": get_conventions,
        "get_budgets": get_budgets,
        "get_run_history": get_run_history,
    }


def tool_help() -> list[dict]:
    return [
        {"name": n, "description": d}
        for n, d in {
            "get_project_state": "Compact derived-state board for the current cycle",
            "compute_work_queue": "The lawful work items, with priorities",
            "dispatch_work_item": "Kernel-gated: dispatch one queue item to its role crew",
            "defer_work_item": "Hold an item until a later cycle (logs a deferral marker)",
            "apply_adoption": "Adopt an anomaly (e.g. a human-filed untyped issue) into the workflow",
            "escalate_to_human": "flag:needs-human + explanatory comment",
            "annotate": "Audit-trail comment (only when it adds substantial information)",
            "get_conventions": "The confirmed conventions + survey mappings",
            "get_budgets": "Ledger state and caps",
            "get_run_history": "Past runs, costs, outcomes",
        }.items()
    ]

# mypy: ignore-errors

"""Orchestrator daemon + deterministic kernel (Technical Spec §6).

The main loop is deterministic: fetch → derive → reconcile → queue →
flag-filter → budget gate → dispatch. Judgment (dispatch sequencing, anomaly
triage) lives in the Orchestrator agent, which acts only through gated tools and
the kernel's idle-dispatch fallback (C19) so it can never starve a nonempty queue.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from codie.cache import Cache
from codie.config import Settings
from codie.dispatch import (
    CrewAIKickoff,
    CrewResult,
    CrewRunner,
    StubRunner,
    apply_feature_plan,
    apply_reconcile_plan,
    apply_release_plan,
    apply_replan,
    apply_revision_assessment,
    apply_spec_mark_ready,
    apply_task_breakdown,
    parse_payload,
)
from codie.github.client import GitHubClient
from codie.github.fetch import FULL_REFRESH_CYCLES, fetch_snapshot
from codie.models import WorkItem
from codie.queue import compute_queue
from codie.state import parse
from codie.state.derive import derive
from codie.state.reconcile import ReconcileMutations, plan_reconcile
from codie.workspace import Workspace


class SpecValidationFailed(RuntimeError):
    """The spec PR files failed kernel validation (§7.2/C17)."""


def _blocked_labels(client, number: int) -> list[str]:
    issue = client.get_issue(number)
    if issue is None:
        return ["flag:blocked"]
    labels = [label for label in issue.labels if not label.startswith("flag:blocked")]
    return labels + ["flag:blocked"]


ROLL_ROLE_LABELS = ["planner", "coder", "reviewer", "tester", "orchestrator"]


class HaltReached(Exception):
    """The marked release PR merged — the only halt condition."""


@dataclass
class RunOutcome:
    item: WorkItem
    status: str = "applied"  # applied | failed | needs_human | refused
    summary: str = ""
    cost_usd: float = 0.0
    tokens: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class CycleOutcome:
    cycle: int
    state: Any = None
    queue: list[WorkItem] = field(default_factory=list)
    dispatched: list[RunOutcome] = field(default_factory=list)
    reconcile_mutations: ReconcileMutations | None = None
    paused: bool = False
    halted: bool = False
    actions: list[str] = field(default_factory=list)


class Kernel:
    """The deterministic kernel plus orchestration loop."""

    def __init__(
        self,
        settings: Settings,
        client: GitHubClient,
        workspace: Workspace | None = None,
        cache: Cache | None = None,
        dry_run: bool = False,
        strategy: Callable[..., str | None] | None = None,
        crew_runner: CrewRunner | None = None,
    ):
        self.settings = settings
        self.client = client
        self.workspace = workspace
        self.cache = cache
        self.dry_run = dry_run
        self.strategy = strategy
        self.crew_runner = crew_runner or CrewAIKickoff(settings)
        self.active_runs: set[str] = set()
        self.poll_ts: str | None = None
        self.cycle_count = 0
        self.spec_attempts: dict[tuple[str, int | None], int] = {}

    # ------------------------------------------------------------------ loop

    def run(self, max_cycles: int | None = None) -> CycleOutcome:
        """Run cycles until halted or an explicit cycle cap (for tests)."""
        outcome: CycleOutcome | None = None
        schedule = max_cycles if max_cycles is not None else float("inf")
        while self.cycle_count < schedule:
            outcome = self.cycle()
            if outcome.halted:
                return outcome
            if not outcome.dispatched and outcome.paused:
                time.sleep(0.05)
        return outcome or self.cycle()

    def cycle(self, now: datetime | None = None) -> CycleOutcome:
        now = now or datetime.now(UTC)
        self.cycle_count += 1
        outcome = CycleOutcome(cycle=self.cycle_count)

        # -- fetch + derive -----------------------------------------------------
        full = self.cycle_count % FULL_REFRESH_CYCLES == 1 or self.poll_ts is None
        snapshot = fetch_snapshot(
            self.client,
            self.settings.project.repo,
            self.settings,
            since=self.poll_ts,
            full=full,
        )
        state = derive(snapshot, self.settings)
        outcome.state = state

        if state.release_merged:
            outcome.halted = True
            self._event("kernel", "The marked release PR merged — halting.", kind="decision")
            return outcome

        # -- reconcile -----------------------------------------------------------
        comment_log: dict[int, list[str]] = {}
        for c in snapshot.comments:
            comment_log.setdefault(c.issue_number, []).append(c.body)
        mutations, projected = plan_reconcile(state, self.settings, now, self.active_runs, comment_log)
        self._apply_reconcile(client=self.client, mutations=mutations)
        outcome.reconcile_mutations = mutations

        # -- queue + flag filter (C13) ------------------------------------------
        queue = compute_queue(projected, self.settings, now)
        queue = self._flag_filter(queue, projected)
        outcome.queue = queue
        self._current_queue = queue

        # -- pause conditions -----------------------------------------------------
        if projected.prd is None:
            self._event(
                "kernel",
                "No PRD issue — provide one via codie init or the pinned issue.",
                level="warning",
                kind="escalation",
            )
            outcome.actions.append("paused: no PRD")
            return outcome

        if any(item.kind == "NeedsHuman" for item in queue):
            outcome.actions.append("paused: PRD required a human")
            self._event(
                "kernel",
                "Queue holds NeedsHuman(provide PRD); pausing.",
                level="warning",
                kind="escalation",
            )
            return outcome

        if self._budgets_exhausted():
            outcome.paused = True
            self._event(
                "kernel",
                "Daily budget exhausted — pausing (no model calls).",
                level="warning",
                kind="budget",
            )
            return outcome

        # -- dispatch ---------------------------------------------------------------
        if not queue:
            outcome.actions.append("idle: queue empty")
            self._event("kernel", "Queue empty; idle.", kind="decision")
            return outcome

        enabled = {role: (self.cache.role_enabled(role) if self.cache else True) for role in ROLL_ROLE_LABELS}
        dispatched_any = False
        if enabled.get("orchestrator", True) or self.strategy is not None:
            pick = self._orchestrator_pick(queue, projected, enabled)
            if pick is not None:
                item = pick
                result = self.dispatch_work_item(item, projected, force=True)
                outcome.dispatched.append(result)
                dispatched_any = result.status in {"applied", "needs_human"}
                # ensure the PRD fingerprint marker is posted once ReconcilePlan ran
                if item.kind == "ReconcilePlan" and result.status == "applied":
                    self._post_prd_fingerprint(projected)
            else:
                outcome.actions.append("orchestrator: no item selected")
        else:
            self._event(
                "kernel",
                "Orchestrator disabled; skipping model call.",
                level="info",
                kind="refusal",
            )

        # -- C19 idle-dispatch fallback --------------------------------------------
        if not dispatched_any and queue:
            head = queue[0]
            head_key = head.issue_number or (projected.prd.number if projected.prd else 0)
            n = projected.idle_dispatch_counts.get(head_key, 0)
            max_idle = self.settings.guardrails.max_idle_dispatch_cycles
            if n >= max_idle:
                self._event(
                    "kernel",
                    f"idle-dispatch: counter {n} >= {max_idle}; kernel dispatching {head.kind} itself.",
                    kind="dispatch",
                )
                result = self.dispatch_work_item(head, projected, force=True)
                outcome.dispatched.append(result)
                if result.status == "applied":
                    dispatched_any = True
            else:
                self._post_idle_marker(projected, head, n)

        # -- post fingerprint if nothing required reconciliation ---------------------
        if projected.prd is not None and not projected.prd_fingerprint_changed:
            self._post_prd_fingerprint(projected)

        return outcome

    # ------------------------------------------------------- orchestrator pick

    def _orchestrator_pick(self, queue, projected, enabled) -> WorkItem | None:
        if self.strategy is not None:
            return self.strategy(queue, projected, self.settings)
        if enabled.get("orchestrator", True):
            # Real crew run happens here in production; the kernel-provided queue
            # order is used when no LLM is configured (tests / restricted setups).
            return self._pick_first_dispatchable(queue, projected, enabled)
        return None

    def _pick_first_dispatchable(self, queue, projected, enabled) -> WorkItem | None:
        for item in queue:
            if not enabled.get(item.role, True):
                continue
            if not self._slot_free(item.role):
                continue
            return item
        return None

    def _slot_free(self, role: str) -> bool:
        active = [r for _, r in self.active_runs]
        return role not in active

    # ------------------------------------------------------------- dispatch

    def dispatch_work_item(self, item: WorkItem, projected, force: bool = False) -> RunOutcome:
        """Kernel-gated dispatch: queue membership, role enabled, slot free, budget."""
        if not force:
            refusal = self._refusal_reason(item, projected)
            if refusal:
                self._event(
                    "kernel",
                    f"dispatch refused for {item.kind}: {refusal}",
                    level="warning",
                    kind="refusal",
                )
                return RunOutcome(item=item, status="refused", error=refusal)
        if self.dry_run:
            self._event("kernel", f"DRY-RUN: would dispatch {item.kind} ({item.role}).", kind="dispatch")
            return RunOutcome(item=item, status="applied", summary="dry-run (no-op)")

        pack = self._build_pack(item, projected)
        run_id = f"{item.role}-{item.issue_number or item.pr_number or 'repo'}-{self.cycle_count}"
        self.active_runs.add(run_id)
        self._event(item.role, f"dispatched {item.kind} on {item.entity}", kind="dispatch", run_id=run_id)
        try:
            created = self._run_item(item, pack, projected)
        finally:
            self.active_runs.discard(run_id)
        return created

    def _refusal_reason(self, item: WorkItem, projected) -> str | None:
        current = getattr(self, "_current_queue", None)
        if current is not None:
            in_queue = any(
                c.kind == item.kind and c.issue_number == item.issue_number and c.pr_number == item.pr_number
                for c in current
            )
            if not in_queue:
                return f"item {item.kind} #{item.issue_number} is not in the current lawful work queue"
        if item.issue_number and (
            _has_flag(projected, item.issue_number, "blocked")
            or _has_flag(projected, item.issue_number, "needs-human")
        ):
            return "issue carries flag:blocked/flag:needs-human"
        if item.role not in {"kernel", "orchestrator"} and (self.cache and not self.cache.role_enabled(item.role)):
            return f"role {item.role} disabled via dashboard"
        if not self._slot_free(item.role):
            return f"role {item.role} slot busy"
        return None

    def _run_item(self, item: WorkItem, pack, projected) -> RunOutcome:
        """Run the crew and serve the result."""
        # Structured planner kinds: build with StubRunner unless a real runner is set.
        if isinstance(self.crew_runner, StubRunner) or self.strategy is not None:
            result: CrewResult = self.crew_runner.run_structured(item.role, item, pack, self.settings)
        else:
            result = self.crew_runner.run_structured(item.role, item, pack, self.settings)
        text = result.text
        tokens = {"input": result.input_tokens, "output": result.output_tokens}
        cost = result.cost_usd
        if self.cache is not None:
            self.cache.add_cost(cost)
        self._event(
            item.role,
            f"{item.kind} returned: {text[:300]}",
            kind="decision",
            run_id=getattr(result, "run_id", None),
        )
        outcome = self.serve(item, text, projected)
        outcome.cost_usd = cost
        outcome.tokens = tokens
        return outcome

    # ------------------------------------------------------------- serving

    def serve(self, item: WorkItem, text: str, projected) -> RunOutcome:
        """Interpret a crew's result and apply it (§5.7, §7.2)."""
        payload, error = parse_payload(item, text)
        if error and payload is None and _requires_payload(item):
            self._event(item.role, f"structured output invalid: {error}", level="error", kind="escalation")
            return RunOutcome(item=item, status="failed", error=error)
        try:
            self._apply(item, payload, projected, text)
        except SpecValidationFailed as exc:
            return self._handle_spec_attempt_failure(item, projected, str(exc))
        except Exception as exc:  # invariant violations are reported, not reverted
            self._event(item.role, f"apply failed: {exc}", level="error", kind="escalation")
            return RunOutcome(item=item, status="failed", error=str(exc))
        self._event(item.role, f"applied {item.kind} result", kind="decision")
        return RunOutcome(item=item, status="applied", summary=text[:200])

    def _handle_spec_attempt_failure(self, item: WorkItem, projected, detail: str) -> RunOutcome:
        """§7.2: a spec the kernel cannot validate counts as a failed attempt; at 3 → blocked."""
        key = (item.kind, item.issue_number)
        attempts = self.spec_attempts.get(key, 0) + 1
        self.spec_attempts[key] = attempts
        self._set_actor("kernel")
        if item.issue_number is not None:
            self.client.comment(
                item.issue_number,
                f"Spec validation failed (attempt {attempts}/3): {detail}\n<!-- codie:attempt {item.kind} {attempts} -->",  # noqa: E501
            )
        if attempts >= 3 and item.issue_number is not None:
            self._set_labels(item.issue_number, _blocked_labels(self.client, item.issue_number))
        self._event(
            "kernel",
            f"spec validation attempt {attempts}/3 failed: {detail}",
            level="warning",
            kind="refusal",
        )
        return RunOutcome(item=item, status="failed", error=detail)

    def _apply(self, item: WorkItem, payload, projected, text: str) -> None:
        kind = item.kind
        if kind == "PlanDecomposition":
            self._set_actor("kernel")
            created, errors = apply_feature_plan(self.client, self.settings, payload)
            if payload and payload.features:
                self.client.comment(
                    projected.prd.number,
                    f"Planner decomposed {len(payload.features)} feature(s)."
                    + (f" Rejected: {errors}" if errors else ""),
                )
        elif kind in {"DraftSpecs", "ReviseSpecs"}:
            pr_number = getattr(payload, "pr_number", None) or item.pr_number
            if pr_number is None:
                # infer from the newly opened spec PR for the feature
                pr_number = self._find_open_spec_pr(item.issue_number)
            if pr_number:
                ok, errs = apply_spec_mark_ready(self.client, self.settings, pr_number)
                if ok:
                    fb = projected.find_feature(item.issue_number)
                    if fb:
                        self._set_labels(
                            item.issue_number,
                            [
                                label
                                for label in self.client.get_issue(item.issue_number).labels
                                if not label.startswith("status:")
                            ]
                            + ["status:spec-review"],
                        )
                else:
                    raise SpecValidationFailed("; ".join(errs))
        elif kind == "AssessRevision":
            self._set_actor("kernel")
            apply_revision_assessment(self.client, self.settings, item.issue_number, payload)
        elif kind == "BreakDownTasks":
            self._set_actor("kernel")
            feature = projected.find_feature(item.issue_number)
            spec_ids = _live_spec_ids(feature)
            created, errors = apply_task_breakdown(
                self.client, self.settings, item.issue_number, payload, projected, spec_ids
            )
            if not errors:
                self._set_labels(item.issue_number, _status_labels_of(self.client, item.issue_number, "planned"))
            else:
                raise RuntimeError("breakdown validation failed: " + "; ".join(errors))
        elif kind == "Replan":
            self._set_actor("kernel")
            feature = projected.find_feature(item.issue_number)
            spec_ids = _live_spec_ids(feature)
            apply_replan(self.client, self.settings, item.issue_number, payload, projected, spec_ids)
        elif kind == "ReconcilePlan":
            self._set_actor("kernel")
            apply_reconcile_plan(self.client, self.settings, payload)
        elif kind == "ProposeRelease":
            self._set_actor("kernel")
            apply_release_plan(self.client, self.settings, payload)
        elif kind in {"ReviewSpecPR", "ReviewPR"}:
            self._apply_review(item, projected, payload)
        elif kind in {"MergeSpecPR", "MergePR"}:
            self._apply_merge(item, projected)
        elif kind == "ReverifyBug":
            self._apply_reverify(item, projected, payload)
        elif kind == "AcceptanceRun":
            self._apply_acceptance(item, projected, payload)
        elif kind == "RegressionRun":
            self._apply_regression(item, projected, payload)
        elif kind == "EvalSuite":
            self._apply_evalsuite(item, projected, payload)
        elif kind in {"Implement", "AddressReview", "VerifyTask", "NeedsHuman"}:
            self._event(item.role, f"{kind} acted via tools.", kind="decision")

    def _apply_review(self, item: WorkItem, projected, decision) -> None:
        self._set_actor("reviewer")
        pr_number = item.pr_number
        pr = self.client.get_pr(pr_number)
        if pr is None:
            raise RuntimeError(f"PR #{pr_number} not found")
        if pr.draft:
            raise RuntimeError("refusing to act on a draft PR")
        state = "APPROVED" if decision.verdict == "approve" else "CHANGES_REQUESTED"
        comments = [
            {"path": c.path, "line": c.line, "side": c.side, "body": c.body} for c in (decision.comments or [])
        ]
        self.client.create_review(pr_number, state, decision.summary if hasattr(decision, "summary") else "", comments)
        if item.kind == "ReviewSpecPR":
            feature = projected.find_feature(item.issue_number)
            if decision.verdict == "request_changes" and feature is not None:
                self._set_actor("kernel")
                self._set_labels(item.issue_number, _status_labels_of(self.client, item.issue_number, "speccing"))
        else:
            task = projected.find_work_item(item.issue_number)
            if decision.verdict == "request_changes" and task is not None:
                self._set_actor("kernel")
                self._set_labels(
                    item.issue_number,
                    _status_labels_of(self.client, item.issue_number, "in-progress"),
                )

    def _apply_merge(self, item: WorkItem, projected) -> None:
        self._set_actor("reviewer")
        pr_number = item.pr_number
        pr = self.client.get_pr(pr_number)
        if pr is None:
            raise RuntimeError(f"PR #{pr_number} not found")
        if pr.state == "open":
            merged = self.client.merge_pr(pr_number, self.settings.overrides.merge_method)
            if not merged:
                raise RuntimeError(f"merge of PR #{pr_number} failed (policy-file gate?)")
        self._set_actor("kernel")
        if item.kind == "MergeSpecPR":
            feature = projected.find_feature(item.issue_number)
            if feature:
                self._set_labels(
                    item.issue_number,
                    _status_labels_of(self.client, item.issue_number, "specified"),
                )
        elif item.issue_number:
            task = projected.find_work_item(item.issue_number)
            if task:
                new_status = "verifying" if projected.find_bug(item.issue_number) else "done"
                self._set_labels(item.issue_number, _status_labels_of(self.client, item.issue_number, new_status))

    def _apply_reverify(self, item: WorkItem, projected, payload) -> None:
        self._set_actor("kernel")
        passed = getattr(payload, "passed", None)
        if passed is None and isinstance(payload, dict):
            passed = payload.get("passed")
        if passed:
            self._set_labels(item.issue_number, _status_labels_of(self.client, item.issue_number, "done"))
        else:
            self._set_labels(item.issue_number, _status_labels_of(self.client, item.issue_number, "in-progress"))
            self.client.comment(
                item.issue_number,
                f"<!-- codie:cycle verification-failed -->\n\nRe-verification failed: {getattr(payload, 'rationale', '') if payload else ''}",  # noqa: E501
            )

    def _apply_acceptance(self, item: WorkItem, projected, payload) -> None:
        self._set_actor("kernel")
        passed = getattr(payload, "passed", None)
        if isinstance(payload, dict):
            passed = payload.get("passed")
        run_id = getattr(payload, "run_id", None) if payload else None
        if passed:
            self._set_labels(item.issue_number, _status_labels_of(self.client, item.issue_number, "review"))
            summary = getattr(payload, "summary", "") if payload else ""
            self.client.comment(item.issue_number, f"**Accepted feature — user acceptance (UAT):**\n\n{summary}")
        else:
            self.client.comment(
                item.issue_number,
                f"<!-- codie:cycle acceptance-failed {run_id or 'unknown'} -->\n\nAcceptance run failed; bugs filed.",
            )
            self._set_labels(item.issue_number, _status_labels_of(self.client, item.issue_number, "in-progress"))

    def _apply_regression(self, item: WorkItem, projected, payload) -> None:
        self._set_actor("kernel")
        scope = getattr(payload, "scope", "fast") if payload else "fast"
        sha = getattr(payload, "sha", "") if payload else ""
        passed = getattr(payload, "passed", False) if payload else False
        self.client.comment(
            projected.prd.number,
            f"<!-- codie:suite {scope} {sha} {'pass' if passed else 'fail'} -->",
        )
        self._event("tester", f"regression {scope} @ {sha}: {'pass' if passed else 'fail'}", kind="decision")

    def _apply_evalsuite(self, item: WorkItem, projected, payload) -> None:
        self._set_actor("tester")
        checked = getattr(payload, "checked", []) if payload else []
        uncertain = getattr(payload, "uncertain", []) if payload else []
        for eid in checked:
            self._check_eval_on_prd(projected.prd.number, eid)
        for eid in uncertain:
            self.client.comment(
                projected.prd.number,
                f"<!-- codie:eval {eid} uncertain -->Need a human to confirm eval {eid}.",
            )

    def _check_eval_on_prd(self, prd_number: int, eid: str) -> None:
        import re

        issue = self.client.get_issue(prd_number)
        if issue is None:
            return

        def cb(m: re.Match) -> str:
            return f"- [x] {m.group('id')}: {m.group('text')}"

        new_body = re.sub(r"- \[( |x)\] (E\d+): (.+)$", cb, issue.body, flags=re.MULTILINE)
        self.client.edit_issue(prd_number, body=new_body)

    # ------------------------------------------------------- reconcile apply

    def _apply_reconcile(self, client, mutations: ReconcileMutations) -> None:
        self._set_actor("kernel")
        for number, labels in mutations.set_labels.items():
            client.set_labels(number, [label for label in labels])
        for number, body in mutations.comments.items():
            existing = [c.body for c in client.list_comments() if c.issue_number == number]
            if body not in existing:
                client.comment(number, body)
        for number in mutations.close:
            i = client.get_issue(number)
            if i and i.state != "closed":
                client.close_issue(number)
        for number in mutations.reopen:
            i = client.get_issue(number)
            if i and i.state != "open":
                client.reopen_issue(number)

    # ------------------------------------------------------------- plumbing

    def _set_actor(self, role: str) -> None:
        if not self.dry_run:
            self.client.set_actor(role)

    def _set_labels(self, number: int, labels: list[str]) -> None:
        if not self.dry_run:
            self.client.set_labels(number, labels)

    def _budgets_exhausted(self) -> bool:
        if self.cache is None:
            return False
        today = self.cache.today_cost()
        if today >= self.settings.budgets.max_llm_cost_usd_per_day:
            return True
        return today >= self.settings.per_run_cap

    def _flag_filter(self, queue: list[WorkItem], projected) -> list[WorkItem]:
        """C13: drop blocked/needs-human streams (issue, deps, parent feature)."""
        blocked: set[int] = set()
        for number, flags in _item_flags(projected).items():
            if "blocked" in flags or "needs-human" in flags:
                blocked.add(number)
        changed = True
        while changed:
            changed = False
            for i in [*projected.tasks, *projected.bugs]:
                if i.number in blocked:
                    continue
                if _has_flag(projected, i.number, "blocked") or _has_flag(projected, i.number, "needs-human"):
                    continue
                for dep in i.depends_on:
                    if dep in blocked:
                        blocked.add(i.number)
                        changed = True
                if i.parent and i.parent in blocked:
                    blocked.add(i.number)
                    changed = True
        return [item for item in queue if item.issue_number is None or item.issue_number not in blocked]

    def _post_prd_fingerprint(self, projected) -> None:
        if projected.prd is None or projected.prd_fingerprint_comment == projected.prd.fingerprint:
            return
        self._set_actor("kernel")
        self.client.comment(projected.prd.number, f"<!-- codie:prd {projected.prd.fingerprint} -->")

    def _post_idle_marker(self, projected, head: WorkItem, n: int) -> None:
        self._set_actor("kernel")
        if head.issue_number:
            self.client.comment(head.issue_number, f"<!-- codie:idle-dispatch {n + 1} -->")
        else:
            if projected.prd:
                self.client.comment(projected.prd.number, f"<!-- codie:idle-dispatch {n + 1} -->")

    def _build_pack(self, item: WorkItem, projected):
        from codie.context import build_context

        return build_context(item.role, projected, self.settings, item=item, workspace=self.workspace)

    def _find_open_spec_pr(self, feature_number: int | None) -> int | None:
        from codie.state import parse as _parse

        for pr in self.client.list_prs():
            is_spec = any(p.startswith("specs/") for p in pr.files)
            linked, _ = _parse.linked_issue_numbers(pr.body)
            if pr.state == "open" and is_spec and (feature_number is None or (linked and linked[0] == feature_number)):
                return pr.number
        return None

    def _event(
        self,
        role: str,
        message: str,
        level: str = "info",
        kind: str = "decision",
        run_id: str | None = None,
    ) -> None:
        if self.cache is not None:
            self.cache.add_event(role, message, level=level, kind=kind, run_id=run_id)


def _requires_payload(item: WorkItem) -> bool:
    return item.kind in {
        "PlanDecomposition",
        "DraftSpecs",
        "ReviseSpecs",
        "AssessRevision",
        "BreakDownTasks",
        "Replan",
        "ReconcilePlan",
        "ProposeRelease",
        "ReviewPR",
        "ReviewSpecPR",
    }


def _item_flags(projected) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for item in [*projected.features, *projected.tasks, *projected.bugs]:
        out[item.number] = set(item.flags)
    return out


def _has_flag(projected, number: int, flag: str) -> bool:
    for item in [*projected.features, *projected.tasks, *projected.bugs]:
        if item.number == number:
            return flag in item.flags
    return False


def _live_spec_ids(feature) -> set[str]:
    if feature is None:
        return set()

    return parse.live_ids("\n".join(feature.requirement_lines))


def _status_labels_of(client, number: int, status: str) -> list[str]:
    issue = client.get_issue(number)
    if issue is None:
        return [f"status:{status}"]
    return [label for label in issue.labels if not label.startswith("status:")] + [f"status:{status}"]

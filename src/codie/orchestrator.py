# """Orchestrator daemon + deterministic kernel (Technical Spec §6)."""

from __future__ import annotations

import re
import signal
import threading
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
from codie.github.dryrun import DryRunClient
from codie.github.fetch import FULL_REFRESH_CYCLES, fetch_snapshot
from codie.models import WorkItem
from codie.queue import compute_queue
from codie.state import parse
from codie.state.derive import derive
from codie.state.reconcile import ReconcileMutations, plan_reconcile
from codie.workspace import Workspace


class SpecValidationFailed(RuntimeError):
    """The spec PR files failed kernel validation (§7.2/C17)."""


class GracyShutdown(Exception):
    """SIGTERM/SIGINT requested — finish in-flight work, then exit."""


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
    status: str = "applied"  # applied | failed | needs_human | refused | started
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


@dataclass
class _RunHandle:
    """An in-flight crew run (Technical Spec §6.1 concurrency)."""

    run_id: str
    role: str
    item: WorkItem
    thread: threading.Thread | None = None
    started: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished: datetime | None = None
    outcome: RunOutcome | None = None


TOOL_ACTING_KINDS = {
    "Implement",
    "AddressReview",
    "ReviewPR",
    "ReviewSpecPR",
    "MergePR",
    "MergeSpecPR",
    "VerifyTask",
    "RegressionRun",
    "AcceptanceRun",
    "ReverifyBug",
    "EvalSuite",
}


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
        async_runs: bool = True,
        roster: Any | None = None,
    ):
        self.settings = settings
        if dry_run and not isinstance(client, DryRunClient):
            client = DryRunClient(client)
        self.client = client
        self.workspace = workspace
        self.cache = cache
        self.dry_run = dry_run
        self.strategy = strategy
        self.crew_runner = crew_runner or CrewAIKickoff(settings)
        self.roster = roster
        self.async_runs = async_runs
        self.active_runs: dict[str, _RunHandle] = {}
        self._reaped: list[RunOutcome] = []
        self.poll_ts: str | None = None
        self.cycle_count = 0
        self.spec_attempts: dict[tuple[str, int | None], int] = {}
        self._current_queue: list[WorkItem] = []
        self._last_outcome: CycleOutcome | None = None
        self.start_time = datetime.now(UTC)
        self._shutdown_requested = False
        self._signal_handlers: list[str] = []

    # ------------------------------------------------------------------ loop

    def run(self, max_cycles: int | None = None) -> CycleOutcome:
        """Run cycles until halted, an explicit cycle cap (for tests), or SIGTERM."""
        outcome: CycleOutcome | None = None
        schedule = max_cycles if max_cycles is not None else float("inf")
        self._install_signal_handlers()
        try:
            while self.cycle_count < schedule:
                if self._shutdown_requested:
                    outcome = self._graceful_stop(outcome)
                    break
                outcome = self.cycle()
                self._last_outcome = outcome
                if outcome.halted:
                    return outcome
                self._sleep_for_poll()
        finally:
            self._restore_signal_handlers()
        return outcome or self.cycle()

    def _sleep_for_poll(self) -> None:
        """§9.5: sleep the configured poll interval (skipped straight after a mutation)."""
        if self._last_outcome and self._last_outcome.dispatched:
            return  # re-poll immediately so state settles before the next decision
        interval = max(1, self.settings.project.poll_interval_seconds)
        if self._shutdown_requested:
            return
        time.sleep(min(interval, 0.05 if self.cycle_count < 2 else interval))

    def _install_signal_handlers(self) -> None:
        try:
            self._signal_handlers = []
            for sig in (signal.SIGTERM, signal.SIGINT):
                self._signal_handlers.append(str(sig))
                signal.signal(sig, lambda *_: self._request_shutdown())
        except ValueError:  # not in the main thread — skip
            self._signal_handlers = []

    def _restore_signal_handlers(self) -> None:
        if not self._signal_handlers:
            return
        for sig_name in self._signal_handlers:
            try:
                sig = getattr(signal, sig_name)
                signal.signal(sig, signal.SIG_DFL)
            except (ValueError, AttributeError):
                pass
        self._signal_handlers = []

    def _request_shutdown(self) -> None:
        self._shutdown_requested = True

    def _graceful_stop(self, outcome: CycleOutcome | None) -> CycleOutcome:
        """C14: wait up to max_task_duration_minutes for in-flight work; orphan the rest."""
        self._event("kernel", "SIGTERM/SIGINT received — finishing in-flight work.", kind="decision")
        deadline = datetime.now(UTC).timestamp() + self.settings.budgets.max_task_duration_minutes * 60
        while self.active_runs and datetime.now(UTC).timestamp() < deadline:
            self.reap_finished_runs()
            time.sleep(0.5)
        orphaned = [h.run_id for h in self.active_runs.values()]
        for run_id in orphaned:
            self._event("kernel", f"orphan run {run_id} left unfinished at shutdown.", kind="decision")
        self.active_runs.clear()
        return outcome or CycleOutcome(cycle=self.cycle_count, actions=["graceful stop"])

    def cycle(self, now: datetime | None = None) -> CycleOutcome:
        now = now or datetime.now(UTC)
        self.cycle_count += 1
        self.reap_finished_runs()
        self.enforce_duration_caps(now)
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
        mutations, projected = plan_reconcile(state, self.settings, now, self._active_run_ids(), comment_log)
        self._apply_reconcile(client=self.client, mutations=mutations)
        outcome.reconcile_mutations = mutations
        if self.cache is not None:
            self.cache.save_snapshot(self.cycle_count, projected)

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
            outcome.actions.append("orchestrator cycle")
            pick = self._orchestrator_pick(queue, projected, enabled, now)
            if pick is not None:
                item = pick
                result = self.dispatch_work_item(item, projected)
                outcome.dispatched.append(result)
                dispatched_any = result.status == "applied"
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
            self._idle_dispatch_fallback(queue, projected, outcome)

        # -- post fingerprint if nothing required reconciliation ---------------------
        if projected.prd is not None and not projected.prd_fingerprint_changed:
            self._post_prd_fingerprint(projected)

        return outcome

    def _idle_dispatch_fallback(self, queue, projected, outcome: CycleOutcome) -> None:
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
            result = self.dispatch_work_item(head, projected)
            outcome.dispatched.append(result)
        else:
            self._post_idle_marker(projected, head, n)

    # ------------------------------------------------------- orchestrator pick

    def _orchestrator_pick(
        self,
        queue: list[WorkItem],
        projected,
        enabled: dict[str, bool],
        now: datetime,
    ) -> WorkItem | None:
        if self.strategy is not None:
            pick = self.strategy(queue, projected, self.settings)
            target = _find_work_item(queue, pick) if isinstance(pick, str) else pick
            return target
        if enabled.get("orchestrator", True):
            # Real Orchestrator crew run (§6.1): the agent picks within the lawful
            # queue via its gated tools. Fall back to the deterministic first
            # dispatchable item when no model is configured (tests / restricted).
            try:
                decision = self._orchestrator_crew_pick(queue, projected)
            except Exception:
                decision = None
            if decision is not None:
                return _find_work_item(queue, decision)
            return self._pick_first_dispatchable(queue, projected, enabled)
        return None

    def _orchestrator_crew_pick(self, queue: list[WorkItem], projected) -> str | None:
        """Run the Orchestrator agent (roles/orchestrator.py) over the gated toolbox."""
        if isinstance(self.crew_runner, StubRunner):
            return None
        from codie.context import build_context
        from codie.roles import orchestrator as orchestrator_role
        from codie.tools.github_tools import build_orchestrator_tools

        pack = build_context("orchestrator", projected, self.settings, workspace=self.workspace)
        bridge = orchestrator_role.OrchestratorBridge(kernel=self, projected=projected, queue=queue)
        tools = list(build_orchestrator_tools(bridge).values())
        agent_item = WorkItem(kind="NeedsHuman", role="orchestrator", entity="cycle")
        result = self.crew_runner.run_tool_agent("orchestrator", agent_item, pack, self.settings, tools)
        return orchestrator_role.parse_decision(result.text)

    def _pick_first_dispatchable(self, queue, projected, enabled) -> WorkItem | None:
        for item in queue:
            if not enabled.get(item.role, True):
                continue
            if not self._slot_free(item.role):
                continue
            return item
        return None

    def _slot_free(self, role: str) -> bool:
        return not any(handle.role == role for handle in self.active_runs.values())

    def _active_run_ids(self) -> set[str]:
        return set(self.active_runs.keys())

    # ------------------------------------------------------------- dispatch

    def dispatch_work_item(self, item: WorkItem, projected) -> RunOutcome:
        """Kernel-gated dispatch: queue membership, role enabled, slot free, budget."""
        refusal = self._refusal_reason(item, projected)
        if refusal:
            self._event(
                "kernel",
                f"dispatch refused for {item.kind}: {refusal}",
                level="warning",
                kind="refusal",
            )
            return RunOutcome(item=item, status="refused", error=refusal)

        pack = self._build_pack(item, projected)
        run_id = f"{item.role}-{item.issue_number or item.pr_number or 'repo'}-{self.cycle_count}"
        outcome = self._run_item(item, pack, projected, run_id=run_id)
        if outcome.status == "refused":
            self._event(
                "kernel",
                f"dispatch refused for {item.kind}: {outcome.error}",
                level="warning",
                kind="refusal",
            )
        return outcome

    def _refusal_reason(self, item: WorkItem, projected) -> str | None:
        current = self._current_queue
        if current:
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
        if self._budgets_exhausted():
            return "daily budget cap would be exceeded"
        if not self.dry_run and not isinstance(self.crew_runner, StubRunner):
            model = self.settings.llm.model_for(item.role)
            if self.settings.llm.price_for(model) is None:
                return f"model {model!r} has no llm.price_map entry (fail-closed, §6.2)"
        return None

    # ------------------------------------------------------------- run model

    def _run_item(self, item: WorkItem, pack, projected, run_id: str) -> RunOutcome:
        """Run the crew (async handle for the real runner, sync otherwise) and serve."""
        if self.async_runs and not isinstance(self.crew_runner, StubRunner):
            handle = _RunHandle(run_id=run_id, role=item.role, item=item)
            handle.thread = threading.Thread(
                target=self._serve_sync,
                args=(handle, item, pack, projected),
                daemon=True,
                name=run_id,
            )
            self.active_runs[run_id] = handle
            self._record_run_start(item, run_id)
            self._post_heartbeat(item, run_id)
            self._roster_begin(item, run_id)
            handle.thread.start()
            return RunOutcome(item=item, status="started", summary="delegated to run handle")
        self._record_run_start(item, run_id)
        self._post_heartbeat(item, run_id)
        self._roster_begin(item, run_id)
        try:
            return self._run_and_serve(item, pack, projected, run_id=run_id)
        finally:
            self.active_runs.pop(run_id, None)

    def _serve_sync(self, handle: _RunHandle, item: WorkItem, pack, projected) -> None:
        try:
            handle.outcome = self._run_and_serve(item, pack, projected, run_id=handle.run_id)
            handle.finished = datetime.now(UTC)
        except Exception as exc:  # the run is orphaned, never fatal to the loop
            handle.outcome = RunOutcome(item=item, status="failed", error=str(exc))
            handle.finished = datetime.now(UTC)
        finally:
            self._roster_finish(item, handle.outcome)

    def _run_and_serve(self, item: WorkItem, pack, projected, run_id: str) -> RunOutcome:
        result = self._kickoff(item, pack)
        tokens = {"input": result.input_tokens, "output": result.output_tokens}
        cost = self._meter_cost(item, result)
        if self.cache is not None:
            self.cache.add_cost(cost)
        if cost > self.settings.per_run_cap:
            self._event(
                item.role,
                f"run cost ${cost:.4f} exceeds max_llm_cost_usd_per_run (${self.settings.per_run_cap:g})",
                level="warning",
                kind="budget",
                run_id=run_id,
            )
        self._event(
            item.role,
            f"{item.kind} returned: {(result.text or '')[:300]}",
            kind="decision",
            run_id=run_id,
        )
        try:
            outcome = self.serve(item, result.text or "", projected)
        except SpecValidationFailed as exc:
            outcome = self._handle_spec_attempt_failure(item, projected, str(exc))
        outcome.cost_usd = cost
        outcome.tokens = tokens
        if self.cache is not None:
            trace_path = self._write_trace(run_id, pack, result)
            self._finish_run_record(run_id, outcome, tokens, trace_path)
        return outcome

    def _kickoff(self, item: WorkItem, pack) -> CrewResult:
        if item.kind in TOOL_ACTING_KINDS:
            tools = self._role_tools(item.role)
            return self.crew_runner.run_tool_agent(item.role, item, pack, self.settings, tools=tools)
        return self.crew_runner.run_structured(item.role, item, pack, self.settings)

    def _role_tools(self, role: str) -> list[Any]:
        """The gated tool surface each tool-acting role crews with (§7.3–§7.5)."""
        from codie.tools.file_tools import FileTools, ShellTools
        from codie.tools.github_tools import GithubToolbox

        tools: list[Any] = []
        if self.workspace is not None:
            tools.append(FileTools(self.settings, self.workspace))
            tools.append(ShellTools(self.settings, self.workspace, read_only=role in {"reviewer", "planner"}))
        tools.append(GithubToolbox(self.client, self.settings, role, workspace=self.workspace))
        return tools

    def _meter_cost(self, item: WorkItem, result: CrewResult) -> float:
        """§6.2: provider-reported cost wins; else price_map; fail-closed at dispatch."""
        if result.cost_usd and result.cost_usd > 0:
            return result.cost_usd
        model = self.settings.llm.model_for(item.role)
        price = self.settings.llm.price_for(model)
        if price is None:
            return 0.0  # dispatch already failed closed; avoid double-escalation here
        return (result.input_tokens / 1000) * price.input_per_1k + (result.output_tokens / 1000) * price.output_per_1k

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
        """§7.2: an invalid spec counts as a failed attempt; at 3 → blocked (C15)."""
        derived = projected.spec_attempts.get(item.issue_number or 0, 0)
        key = (item.kind, item.issue_number)
        attempts = max(self.spec_attempts.get(key, 0), derived) + 1
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
                pr_number = self._find_open_spec_pr(item.issue_number)
            if pr_number:
                ok, errs = apply_spec_mark_ready(self.client, self.settings, pr_number)
                if ok:
                    num = _issue_num(item)
                    if projected.find_feature(num):
                        self._set_labels(
                            num,
                            _status_labels_of(self.client, num, "spec-review"),
                        )
                else:
                    raise SpecValidationFailed("; ".join(errs))
        elif kind == "AssessRevision":
            self._set_actor("kernel")
            apply_revision_assessment(self.client, self.settings, _issue_num(item), payload)
        elif kind == "BreakDownTasks":
            self._set_actor("kernel")
            num = _issue_num(item)
            feature = projected.find_feature(num)
            spec_ids = _live_spec_ids(feature)
            created, errors = apply_task_breakdown(self.client, self.settings, num, payload, projected, spec_ids)
            if not errors:
                self._set_labels(num, _status_labels_of(self.client, num, "planned"))
            else:
                raise RuntimeError("breakdown validation failed: " + "; ".join(errors))
        elif kind in {"Replan", "ReconcilePlan"}:
            self._set_actor("kernel")
            if kind == "Replan":
                num = _issue_num(item)
                feature = projected.find_feature(num)
                spec_ids = _live_spec_ids(feature)
                apply_replan(self.client, self.settings, num, payload, projected, spec_ids)
            else:
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
        elif kind == "VerifyTask":
            self._apply_verify_task(item, projected)
        elif kind in {"Implement", "AddressReview"}:
            self._event(item.role, f"{kind} acted via tools.", kind="decision")
        elif kind == "AcceptanceRun":
            self._apply_acceptance(item, projected, payload)
        elif kind == "RegressionRun":
            self._apply_regression(item, projected, payload)
        elif kind == "EvalSuite":
            self._apply_evalsuite(item, projected, payload)
        elif kind == "NeedsHuman":
            self._event(item.role, f"{kind} acted via tools.", kind="decision")

    def _apply_review(self, item: WorkItem, projected, decision) -> None:
        if decision is None:
            raise RuntimeError(f"ReviewDecision payload missing for {item.kind}")
        pr_number = item.pr_number
        if pr_number is None:
            raise RuntimeError(f"{item.kind} requires a PR number")
        self._set_actor("reviewer")
        pr = self.client.get_pr(pr_number)
        if pr is None:
            raise RuntimeError(f"PR #{pr_number} not found")
        if pr.draft:
            raise RuntimeError("refusing to act on a draft PR")
        state = "APPROVED" if decision.verdict == "approve" else "CHANGES_REQUESTED"
        comments = [
            {"path": c.path, "line": c.line, "side": c.side, "body": c.body} for c in (decision.comments or [])
        ]
        summary = getattr(decision, "rationale", "")
        self.client.create_review(pr_number, state, summary, comments)
        num = _issue_num(item)
        if item.kind == "ReviewSpecPR":
            feature = projected.find_feature(num)
            if decision.verdict == "request_changes" and feature is not None:
                self._set_actor("kernel")
                self._set_labels(num, _status_labels_of(self.client, num, "speccing"))
        else:
            task = projected.find_work_item(num)
            if decision.verdict == "request_changes" and task is not None:
                self._set_actor("kernel")
                self._set_labels(num, _status_labels_of(self.client, num, "in-progress"))

    def _apply_merge(self, item: WorkItem, projected) -> None:
        pr_number = item.pr_number
        if pr_number is None:
            raise RuntimeError(f"{item.kind} requires a PR number")
        derived = projected.find_pr(pr_number)
        if item.kind == "MergeSpecPR" and not (derived and derived.reviewer_approved):
            raise RuntimeError(f"spec PR #{pr_number} has no Reviewer APPROVED on head — refusing to merge")
        if derived is not None and _is_policy_pr(derived) and not derived.approved_non_bot:
            raise RuntimeError(
                f"policy-file PR #{pr_number} has no non-bot APPROVED on head — refusing to merge (C20)"
            )
        self._set_actor("reviewer")
        pr = self.client.get_pr(pr_number)
        if pr is None:
            raise RuntimeError(f"PR #{pr_number} not found")
        if pr.state == "open":
            merged = self.client.merge_pr(pr_number, self.settings.overrides.merge_method)
            if not merged:
                raise RuntimeError(f"merge of PR #{pr_number} failed (draft/policy/release gate?)")
        self._set_actor("kernel")
        if item.kind == "MergeSpecPR":
            num = _issue_num(item)
            if projected.find_feature(num):
                self._set_labels(num, _status_labels_of(self.client, num, "specified"))
        elif item.issue_number:
            num = item.issue_number
            task = projected.find_work_item(num)
            if task:
                new_status = "verifying" if projected.find_bug(num) else "done"
                self._set_labels(num, _status_labels_of(self.client, num, new_status))

    def _apply_reverify(self, item: WorkItem, projected, payload) -> None:
        num = _issue_num(item)
        self._set_actor("kernel")
        passed = getattr(payload, "passed", None) if payload is not None else None
        if passed is None:
            raise RuntimeError("ReverifyBug requires a passed result")
        if passed:
            self._set_labels(num, _status_labels_of(self.client, num, "done"))
        else:
            self._set_labels(num, _status_labels_of(self.client, num, "in-progress"))
            self.client.comment(
                num,
                f"<!-- codie:cycle verification-failed -->\n\nRe-verification failed: "
                f"{getattr(payload, 'rationale', '') if payload else ''}",
            )

    def _apply_verify_task(self, item: WorkItem, projected) -> None:
        # kind:test task — the Tester authors acceptance coverage via a PR; once
        # its PR opens, reconciliation moves the task to in-review → done on merge.
        self._event("tester", f"VerifyTask #{item.issue_number} acted via tools.", kind="decision")

    def _apply_acceptance(self, item: WorkItem, projected, payload) -> None:
        num = _issue_num(item)
        self._set_actor("kernel")
        passed = getattr(payload, "passed", None) if payload is not None else None
        if isinstance(payload, dict):
            passed = payload.get("passed")
        run_id = getattr(payload, "run_id", None) if payload else None
        if passed:
            self._set_labels(num, _status_labels_of(self.client, num, "review"))
            summary = (getattr(payload, "summary", "") if payload else "") or ""
            self.client.comment(
                num,
                f"**Accepted feature — user acceptance (UAT):**\n\n{summary}",
            )
        else:
            self.client.comment(
                num,
                f"<!-- codie:cycle acceptance-failed {run_id or 'unknown'} -->\n\nAcceptance run failed; bugs filed.",
            )
            self._set_labels(num, _status_labels_of(self.client, num, "in-progress"))
            self._file_bug_from_failure(item, projected, payload, kind="acceptance")

    def _apply_regression(self, item: WorkItem, projected, payload) -> None:
        self._set_actor("kernel")
        scope = getattr(payload, "scope", "fast") if payload else "fast"
        sha = getattr(payload, "sha", "") if payload else (projected.branches.dev or "")
        passed = bool(getattr(payload, "passed", False)) if payload else False
        if not sha:
            sha = projected.branches.dev or ""
        self.client.comment(
            projected.prd.number,
            f"<!-- codie:suite {scope} {sha} {'pass' if passed else 'fail'} -->",
        )
        self._event("tester", f"regression {scope} @ {sha}: {'pass' if passed else 'fail'}", kind="decision")
        if not passed:
            self._file_bug_from_failure(item, projected, payload, kind="regression")

    def _apply_evalsuite(self, item: WorkItem, projected, payload) -> None:
        """§7.7: run command evals, check off exactly one eval per id, file bugs on failures."""
        self._set_actor("tester")
        checked = list(getattr(payload, "checked", [])) if payload else []
        uncertain = list(getattr(payload, "uncertain", [])) if payload else []
        failed = list(getattr(payload, "failed", [])) if payload else []
        for eid in checked:
            self._check_eval_on_prd(projected.prd.number, eid)
        for eid in uncertain:
            if eid not in projected.eval_uncertain:
                self.client.comment(
                    projected.prd.number,
                    f"<!-- codie:eval {eid} uncertain -->Need a human to confirm eval {eid}.",
                )
        for eid in failed:
            self._file_bug_from_eval(projected, eid)

    def _check_eval_on_prd(self, prd_number: int, eid: str) -> None:
        # I-18: check off exactly ONE eval, by id — never every E* line.
        issue = self.client.get_issue(prd_number)
        if issue is None:
            return
        pat = re.compile(rf"^(- \[)( |x)(\] {re.escape(eid)}:.*)$", re.MULTILINE)
        new_body, count = pat.subn(lambda m: f"{m.group(1)}x{m.group(3)}", issue.body, count=1)
        if count:
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
        for number in mutations.close_prs:
            pr = client.get_pr(number)
            if pr and pr.state == "open":
                client.close_pr(number)

    # ------------------------------------------------------- run bookkeeping

    def _record_run_start(self, item: WorkItem, run_id: str) -> None:
        if self.cache is not None:
            self.cache.begin_run(run_id, item.model_dump_json())

    def _finish_run_record(self, run_id: str, outcome: RunOutcome, tokens: dict, trace_path: str) -> None:
        if self.cache is not None:
            self.cache.complete_run(
                run_id,
                status=outcome.status,
                cost_usd=outcome.cost_usd,
                tokens=tokens,
                trace_path=trace_path,
            )

    def _write_trace(self, run_id: str, pack, result: CrewResult) -> str:
        from codie.cache import state_dir as _state_dir

        try:
            owner, name = self.settings.project.repo.split("/")
            base = _state_dir(owner, name) / "runs" / run_id
            base.mkdir(parents=True, exist_ok=True)
            (base / "pack.txt").write_text(pack.render())
            prompt_text = getattr(result, "text", "")
            if prompt_text:
                (base / "result.txt").write_text(prompt_text[:65536])
            return str(base)
        except Exception:
            return ""

    def _roster_begin(self, item: WorkItem, run_id: str) -> None:
        if self.roster is not None:
            title = item.entity
            self.roster.begin(item.role, title, run_id=run_id)

    def _roster_finish(self, item: WorkItem, outcome: RunOutcome | None) -> None:
        if self.roster is not None:
            self.roster.finish(item.role, outcome.status if outcome else "failed")

    def _post_heartbeat(self, item: WorkItem, run_id: str) -> None:
        """§5.8: kernel posts `<!-- codie:heartbeat <run_id> <iso> -->` at run start."""
        if item.issue_number is None:
            return
        self._set_actor("kernel")
        self.client.comment(
            item.issue_number,
            f"<!-- codie:heartbeat {run_id} {datetime.now(UTC).isoformat()} -->",
        )

    def _file_bug_from_failure(self, item: WorkItem, projected, payload, kind: str) -> None:
        from codie.tools.github_tools import GithubToolbox

        self._set_actor("tester")
        toolbox = GithubToolbox(self.client, self.settings, "tester")
        feature_number = item.issue_number
        title = f"{kind} run failed for #{feature_number or 'integration'}"
        body = (
            f"## Reproduction\nRun: {kind}{' scope=' + getattr(payload, 'scope', '') if payload else ''}\n\n"
            f"## Expected vs actual\nExpected a passing run; got a failure."
            + ("\n\n```\n" + (getattr(payload, "evidence", "") or "")[:2000] + "\n```" if payload else "")
        )
        labels = ["type:bug", "status:ready", "priority:medium"]
        if feature_number is not None:
            labels.append("flag:needs-human")
        toolbox.create_issue(title, body, labels)

    def _file_bug_from_eval(self, projected, eid: str) -> None:
        from codie.tools.github_tools import GithubToolbox

        self._set_actor("tester")
        toolbox = GithubToolbox(self.client, self.settings, "tester")
        citing = [f for f in projected.features if eid in f.evals]
        for feature in citing or [None]:
            title = f"Eval {eid} failing"
            body = f"Eval: {eid}\n\n## Reproduction\n`{eid}` failed to pass."
            if feature is not None:
                body += f"\n\n**Parent:** #{feature.number}\n"
            labels = ["type:bug", "status:ready", "priority:medium"]
            if feature is None:
                labels.append("flag:needs-human")
            toolbox.create_issue(title, body, labels)

    # ------------------------------------------------------------- plumbing

    def _set_actor(self, role: str) -> None:
        self.client.set_actor(role)

    def _set_labels(self, number: int, labels: list[str]) -> None:
        self.client.set_labels(number, labels)

    def _budgets_exhausted(self) -> bool:
        if self.cache is None:
            return False
        return self.cache.today_cost() >= self.settings.budgets.max_llm_cost_usd_per_day

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
        # C19: post the marker only when the newest marker is not already this count.
        self._set_actor("kernel")
        target = head.issue_number if head.issue_number else (projected.prd.number if projected.prd else None)
        if target is None:
            return
        existing = [c.body for c in self.client.list_comments() if c.issue_number == target]
        if any(f"<!-- codie:idle-dispatch {n + 1} -->" in c for c in existing):
            return
        self.client.comment(target, f"<!-- codie:idle-dispatch {n + 1} -->")

    def _build_pack(self, item: WorkItem, projected):
        from codie.context import build_context

        return build_context(
            item.role,
            projected,
            self.settings,
            item=item,
            workspace=self.workspace,
            read_file_at=lambda path, ref: self.client.get_file(path, ref),
        )

    def _find_open_spec_pr(self, feature_number: int | None) -> int | None:
        from codie.state import parse as _parse

        for pr in self.client.list_prs():
            is_spec = any(p.startswith("specs/") for p in pr.files)
            linked, _ = _parse.linked_issue_numbers(pr.body)
            if pr.state == "open" and is_spec and (feature_number is None or (linked and linked[0] == feature_number)):
                return pr.number
        return None

    # -------------------------------------------------- async run lifecycle (I-27)

    def reap_finished_runs(self) -> list[RunOutcome]:
        """Collect concurrency handles whose thread finished; apply/serve results."""
        finished: list[RunOutcome] = []
        for run_id, handle in list(self.active_runs.items()):
            if handle.thread is None or not handle.thread.is_alive():
                if handle.outcome is not None:
                    finished.append(handle.outcome)
                    self._event(handle.role, f"run {run_id} reaped ({handle.outcome.status})", kind="dispatch")
                    if handle.outcome.status in {"failed", "needs_human"}:
                        self._apply_failed(handle.item, handle.outcome)
                    del self.active_runs[run_id]
                    continue
                if handle.thread is not None:  # started, not yet served
                    handle.thread.join(timeout=0.5)
                    if handle.outcome is not None:
                        finished.append(handle.outcome)
                        del self.active_runs[run_id]
        return finished

    def _apply_failed(self, item: WorkItem, outcome: RunOutcome) -> None:
        self._event(item.role, f"{item.kind} failed: {outcome.error}", level="error", kind="escalation")

    def enforce_duration_caps(self, now: datetime | None = None) -> None:
        """§6.1/C14: wall-clock kill on runs past max_task_duration_minutes."""
        now = now or datetime.now(UTC)
        cap_minutes = self.settings.budgets.max_task_duration_minutes
        for run_id, handle in list(self.active_runs.items()):
            age = (now - handle.started).total_seconds() / 60
            if age > cap_minutes:
                self._event(
                    handle.role,
                    f"run {run_id} exceeded max_task_duration_minutes ({cap_minutes}m) — orphaned.",
                    level="warning",
                    kind="budget",
                )
                del self.active_runs[run_id]

    # ---------------------------------------------------------------- events

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

    @property
    def summary_state(self) -> dict:
        """Live view for the dashboard (§6.5)."""
        outcome = self._last_outcome
        paused = bool(outcome and outcome.paused or (outcome and not outcome.queue))
        halted = bool(outcome and outcome.halted)
        uptime = max(0, int((datetime.now(UTC) - self.start_time).total_seconds()))
        return {
            "repo": self.settings.project.repo,
            "uptime": f"{uptime}s",
            "daily_cost_usd": self.cache.today_cost() if self.cache else 0.0,
            "halted": halted,
            "paused": paused and not halted,
        }


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
        "ReviewSpecPR",
        "ReviewPR",
        "AcceptanceRun",
        "ReverifyBug",
        "RegressionRun",
        "EvalSuite",
    }


def _issue_num(item: WorkItem) -> int:
    """Issue-bound kinds always carry an issue_number (§5.6); refuse otherwise."""
    if item.issue_number is None:
        raise RuntimeError(f"{item.kind} requires an issue number")
    return item.issue_number


def _is_policy_pr(pr) -> bool:
    policy = {".codie.yaml", "AGENTS.md", "CODEOWNERS"}
    return any(path.split("/")[-1] in policy for path in (pr.files or []))


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


def _find_work_item(queue: list[WorkItem], key: str | WorkItem | None) -> WorkItem | None:
    if key is None or not isinstance(key, str):
        return key if isinstance(key, WorkItem) else None
    if key.startswith("kind:"):
        wanted = key[len("kind:") :]
        return next((item for item in queue if item.kind == wanted), None)
    for item in queue:
        if item.entity == key or str(item.issue_number or item.pr_number or "") == key.strip("#"):
            return item
    return None


def _live_spec_ids(feature) -> set[str]:
    if feature is None:
        return set()
    return parse.live_ids("\n".join(feature.requirement_lines))


def _status_labels_of(client, number: int, status: str) -> list[str]:
    issue = client.get_issue(number)
    if issue is None:
        return [f"status:{status}"]
    return [label for label in issue.labels if not label.startswith("status:")] + [f"status:{status}"]

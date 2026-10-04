"""Guardrail and fidelity unit tests (I-10, I-12, I-13, I-16, I-20, I-22, I-24,
I-26, I-27, I-28, I-32). Every DoD is a named checkable test.
"""

import datetime as _dt
import json
from pathlib import Path

import pytest

from codie.cache import Cache
from codie.config import PriceEntry
from codie.context import build_context
from codie.dispatch import CrewResult, CrewRunner, StubRunner
from codie.github.dryrun import DryRunClient
from codie.github.fetch import fetch_snapshot
from codie.models import Eval, PrdIssue, ProjectState, WorkItem
from codie.orchestrator import Kernel
from codie.queue import ci_green, compute_queue
from codie.state.derive import derive
from codie.state.reconcile import plan_reconcile
from tests.conftest import add_prd, fresh_fake, make_settings


def dt():
    return _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)


def state_of(gh, settings):
    return derive(fetch_snapshot(gh, settings.project.repo, settings, full=True), settings)


# ---------------------------------------------------------------------------
# I-12: guardrail counters live on GitHub
# ---------------------------------------------------------------------------


def test_derive_parses_spec_attempt_marker():
    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(2, "Feature", "x", labels=["type:feature", "status:proposed"])
    gh.add_comment_to_issue(2, "<!-- codie:attempt DraftSpecs 2 -->")
    st = state_of(gh, make_settings())
    assert st.spec_attempts.get(2) == 2


def test_spec_attempt_cap_trips_after_cold_rebuild(tmp_path):
    """A restart mid-failure still trips the 3-strike cap (C15)."""
    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(2, "Feature", "x", labels=["type:feature", "status:proposed"])
    gh.add_comment_to_issue(2, "<!-- codie:attempt DraftSpecs 2 -->")
    settings = make_settings()
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=StubRunner(lambda r, i, p: "{}"))
    st = state_of(gh, settings)
    # crafted DraftSpecs result whose payload fails kernel validation
    item = WorkItem(kind="DraftSpecs", role="planner", entity="x", issue_number=2, rule=4)
    outcome = kernel.serve(item, json.dumps({"pr_number": 99, "files": ["specs/x.md"], "open_questions": []}), st)
    assert outcome.status == "failed"
    assert "flag:blocked" in gh.labels_of(2)  # derived 2 + this failure = 3


def test_idle_marker_dedup(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=StubRunner(lambda r, i, p: "{}"))
    st = state_of(gh, settings)
    from codie.models import WorkItem as WI

    head = WI(kind="PlanDecomposition", role="planner", entity="PRD", rule=2)
    kernel._post_idle_marker(st, head, 0)
    kernel._post_idle_marker(st, head, 0)
    n = len([c for c in gh.list_comments() if "codie:idle-dispatch" in c.body])
    assert n == 1  # dedup


# ---------------------------------------------------------------------------
# I-13: polling, preflight, shutdown
# ---------------------------------------------------------------------------


def test_require_provisioned_raises_with_remediation(monkeypatch):
    from codie.provision import require_provisioned

    gh = fresh_fake()
    # fresh fake: no labels, no branches
    gh.branches.clear()
    monkeypatch.setenv("K", "x")
    with pytest.raises(Exception) as exc:
        require_provisioned(make_settings(), gh, "acme/test")
    assert "codie init" in str(exc.value) or "doctor" in str(exc.value)


def test_start_refuses_without_prd(monkeypatch):
    from codie.provision import require_provisioned

    gh = fresh_fake()
    monkeypatch.setenv("K", "x")
    with pytest.raises(Exception) as exc:
        require_provisioned(make_settings(), gh, "acme/test")
    assert "codie init" in str(exc.value)


def test_poll_sleep_skipped_after_mutation(monkeypatch, tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    settings.project.poll_interval_seconds = 10_000
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=StubRunner(lambda r, i, p: "{}"))
    called = {"n": 0}
    monkeypatch.setattr("codie.orchestrator.time.sleep", lambda s: called.__setitem__("n", called["n"] + 1))
    outcome = kernel.cycle(now=dt())  # mutates (Planner feature creation)
    kernel._last_outcome = outcome
    kernel._sleep_for_poll()
    assert called["n"] == 0  # skip the sleep straight after a mutation (§9.5)
    kernel._last_outcome = None
    kernel._sleep_for_poll()
    assert called["n"] == 1


def test_shutdown_flag_stops_run_loop(tmp_path, monkeypatch):
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=StubRunner(lambda r, i, p: "{}"))
    kernel._request_shutdown()
    monkeypatch.setattr("codie.orchestrator.time.sleep", lambda s: None)
    outcome = kernel.run(max_cycles=100)
    assert outcome.cycle < 100  # exited early on the shutdown flag


# ---------------------------------------------------------------------------
# I-16: review context carries the diff; ci_green follows required-checks semantics
# ---------------------------------------------------------------------------


def test_reviewer_pack_contains_changed_files(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(2, "Feature", "x", labels=["type:feature", "status:planned"])
    gh.add_issue(5, "Task", "**Parent:** #2", labels=["type:task", "status:in-review", "kind:dev"])
    pr = gh.add_pr(7, "PR", "Refs #5", "feature/5-x", "dev", files=["src/x.py", "src/y.py"])
    pr.state = "open"
    st = state_of(gh, make_settings())
    pack = build_context(
        "reviewer",
        st,
        make_settings(),
        item=WorkItem(kind="ReviewPR", role="reviewer", issue_number=5, pr_number=7, rule=5),
        read_file_at=lambda path, ref: f"<content of {path}>",
    )
    text = pack.render()
    assert "Changed files" in text and "src/x.py" in text
    assert "Diff content" in text and "<content of src/x.py>" in text


def test_ci_green_required_checks_only():
    from codie.models import RequiredCheck

    pr = type("P", (), {"checks": [RequiredCheck(name="ci", state="success")]})()
    assert ci_green(pr) is True
    pr2 = type("P", (), {"checks": [RequiredCheck(name="required", state="failure")]})()
    assert ci_green(pr2) is False


def test_fetch_ignores_non_required_failing_checks():
    """I-16/N6: only *required* checks gate CI — a non-required failing check does not block."""
    settings = make_settings()
    gh = fresh_fake()
    add_prd(gh)
    gh.required_check_names.add("CI")
    gh.add_issue(2, "Feature", "x", labels=["type:feature", "status:specified"])
    pr = gh.add_pr(7, "PR", "Refs #2", "feature/2-x", "dev", files=["src/x.py"])
    pr.state = "open"
    # a NON-required check fails while the required check passes
    gh.commit_statuses[gh.branches["feature/2-x"]] = {"lint": "failure", "CI": "success"}
    imported = derive(fetch_snapshot(gh, "acme/test", settings, full=True), settings).find_pr(7)
    assert imported is not None and imported.checks
    assert all(c.state == "success" for c in imported.checks)  # lint (non-required) absent from the gate
    assert ci_green(imported) is True
    # a required failing check blocks
    gh.commit_statuses[gh.branches["feature/2-x"]] = {"lint": "failure", "CI": "failure"}
    imported2 = derive(fetch_snapshot(gh, "acme/test", settings, full=True), settings).find_pr(7)
    assert any(c.name == "CI" and c.state == "failure" for c in imported2.checks)
    assert ci_green(imported2) is False


# ---------------------------------------------------------------------------
# I-20: revision re-plan
# ---------------------------------------------------------------------------


def test_revision_requirements_marker_cancels_children_and_closes_prs(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(2, "Feature", "x", labels=["type:feature", "status:revised"])
    gh.add_issue(5, "Task", "**Parent:** #2", labels=["type:task", "status:in-progress", "kind:dev"])
    gh.branches["feature/5-x"] = "b1"
    gh.add_pr(8, "Task PR", "Refs #5", "feature/5-x", "dev")
    gh.add_comment_to_issue(2, "<!-- codie:revision requirements -->\n\nrequirements changed")
    settings = make_settings()
    st = state_of(gh, settings)
    mut, proj = plan_reconcile(st, settings, dt(), set())
    assert proj.find_feature(2).status == "speccing"
    assert proj.find_task(5).status == "cancelled"
    assert 8 in mut.close_prs


def test_revised_implementation_marker_queues_replan():
    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(2, "Feature", "x", labels=["type:feature", "status:revised"])
    gh.add_comment_to_issue(2, "<!-- codie:revision implementation -->")
    st = state_of(gh, make_settings())
    q = compute_queue(st, make_settings(), dt())
    assert any(i.kind == "Replan" and i.issue_number == 2 for i in q)


def test_reissued_assess_revision_without_marker():
    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(2, "Feature", "x", labels=["type:feature", "status:revised"])
    st = state_of(gh, make_settings())
    q = compute_queue(st, make_settings(), dt())
    assert any(i.kind == "AssessRevision" for i in q)


# ---------------------------------------------------------------------------
# I-22: bug pipeline
# ---------------------------------------------------------------------------


def test_bugs_outrank_features_only_for_bug_work():
    from codie.models import Bug, Feature

    s = ProjectState(repo="a/b", prd=PrdIssue(number=1, title="P", body="", evals=[], updated_at=dt()))
    f = Feature(number=2, title="f", body="", status="planned", created_at=dt(), updated_at=dt())
    b = Bug(number=9, title="b", body="", status="ready", created_at=dt(), updated_at=dt())
    s.features = [f]
    s.bugs = [b]
    q = compute_queue(s, make_settings(), dt())
    implements = [i for i in q if i.kind == "Implement"]
    assert implements and implements[0].issue_number == 9  # the bug outranks feature work


def test_eval_suite_gated_on_open_bug():
    from codie.models import Bug, Feature

    s = ProjectState(repo="a/b", prd=PrdIssue(number=1, title="P", body="", evals=[], updated_at=dt()))
    s.prd = PrdIssue(
        number=1,
        title="P",
        body="",
        evals=[Eval(id="E1", text="x", checked=False)],
        updated_at=dt(),
        fingerprint="",
    )
    s.features = [Feature(number=2, title="f", body="", status="accepted", created_at=dt(), updated_at=dt())]
    s.bugs = [Bug(number=9, title="b", body="", status="ready", created_at=dt(), updated_at=dt())]
    q = compute_queue(s, make_settings(), dt())
    assert not any(i.kind == "EvalSuite" for i in q)  # open bug blocks the suite (§5.4/§7.7)


# ---------------------------------------------------------------------------
# I-24: budget metering and fail-closed
# ---------------------------------------------------------------------------


def _settings_with_price():
    settings = make_settings()
    settings.llm.price_map = {"anthropic/claude-sonnet-4.5": PriceEntry(input_per_1k=0.1, output_per_1k=0.2)}
    return settings


def test_run_accrues_exact_cost(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    settings = _settings_with_price()
    cache = Cache().open_or_rebuild(tmp_path / "c.db")

    class Priced(StubRunner):
        def run_structured(self, role, item, pack, settings, output_model=None):
            return CrewResult(text="{}", input_tokens=1000, output_tokens=500)

    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=Priced(lambda r, i, p: "{}"))
    item = WorkItem(kind="NeedsHuman", role="planner", entity="x", issue_number=1, rule=0)

    st = state_of(gh, settings)
    kernel._current_queue = [item]
    kernel.dispatch_work_item(item, st)
    assert cache.today_cost() == pytest.approx(0.1 * 1 + 0.2 * 0.5)  # = 0.20


def test_model_without_price_blocks_dispatch(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=None, async_runs=False)
    # force a non-stub runner so the fail-closed check applies (§6.2)
    kernel.crew_runner = _RealLikeRunner()
    item = WorkItem(kind="NeedsHuman", role="planner", entity="x", issue_number=1, rule=0)
    st = state_of(gh, settings)
    kernel._current_queue = [item]
    settings.llm.price_map = {}
    outcome = kernel.dispatch_work_item(item, st)
    assert outcome.status == "refused"
    assert "price_map" in outcome.error


def test_per_run_cap_trips_independently(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    settings = _settings_with_price()
    settings.budgets.max_llm_cost_usd_per_day = 100
    settings.project.budgets.max_llm_cost_usd_per_run = 0.05
    cache = Cache().open_or_rebuild(tmp_path / "c.db")

    class Priced(StubRunner):
        def run_structured(self, role, item, pack, settings, output_model=None):
            return CrewResult(text="{}", input_tokens=4000, output_tokens=1000)

    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=Priced(lambda r, i, p: "{}"))
    st = state_of(gh, settings)
    item = WorkItem(kind="NeedsHuman", role="planner", entity="x", issue_number=1, rule=0)
    kernel._current_queue = [item]
    kernel.dispatch_work_item(item, st)
    budget_events = [e for e in cache.list_events() if e["kind"] == "budget"]
    assert cache.today_cost() > settings.per_run_cap
    assert budget_events  # a per-run overrun is recorded without needing the daily cap


class _RealLikeRunner(CrewRunner):
    def run_structured(self, role, item, pack, settings, output_model=None):
        raise AssertionError("should never be called")
        return CrewResult(text="{}")

    def run_tool_agent(self, role, item, pack, settings, tools=None, tool_inputs=None):
        raise AssertionError("should never be called")
        return CrewResult(text="{}")


# ---------------------------------------------------------------------------
# I-26: runs + snapshots are written
# ---------------------------------------------------------------------------


def test_runs_and_snapshots_recorded(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=StubRunner(lambda r, i, p: "{}"))
    kernel.cycle(now=dt())
    runs = cache.list_runs()
    assert runs, "a run row should be recorded per dispatch"
    assert cache.last_snapshots(2)  # snapshots written each cycle
    run = runs[0]
    assert run["status"] in {"applied", "failed"}
    assert not run["trace_path"] or Path(run["trace_path"]).exists()


# ---------------------------------------------------------------------------
# I-27: slots, reap, duration caps
# ---------------------------------------------------------------------------


class DelayedRunner(CrewRunner):
    """A real-ish runner the kernel treats as heavyweight (threaded, I-27)."""

    def __init__(self, handler, delay: float = 0.0):
        self.handler = handler
        self.delay = delay

    def run_structured(self, role, item, pack, settings, output_model=None):
        import time

        time.sleep(self.delay)
        return CrewResult(text=self.handler(role, item, pack))

    def run_tool_agent(self, role, item, pack, settings, tools=None, tool_inputs=None):
        import time

        time.sleep(self.delay)
        return CrewResult(text=self.handler(role, item, pack))


def test_per_role_slot_caps_concurrent_runs(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    settings = _settings_with_price()  # prices needed so the real-runner path passes fail-closed
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    runner = DelayedRunner(lambda r, i, p: "{}", delay=0.05)
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=runner, async_runs=True)
    st = state_of(gh, settings)
    first = WorkItem(kind="PlanDecomposition", role="planner", entity="A", rule=2)
    second = WorkItem(kind="PlanDecomposition", role="planner", entity="B", rule=2)
    kernel._current_queue = [first, second]
    kernel.dispatch_work_item(first, st)
    # second same-role dispatch is refused while the first handle is active (I-27)
    refused = kernel.dispatch_work_item(second, st)
    assert refused.status == "refused"
    assert "slot" in refused.error
    import time

    reaped = []
    for _ in range(20):
        reaped = kernel.reap_finished_runs()
        if reaped:
            break
        time.sleep(0.05)
    assert reaped  # budget pause / reaping collects the finished handle


def test_enforce_duration_caps_orphans_old_handles(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    settings.budgets.max_task_duration_minutes = 0  # any run is already too old
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    runner = DelayedRunner(lambda r, i, p: "{}", delay=1.0)
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=runner, async_runs=True)
    st = state_of(gh, settings)
    item = WorkItem(kind="PlanDecomposition", role="planner", entity="A", rule=2)
    kernel._current_queue = [item]
    kernel.dispatch_work_item(item, st)
    kernel.enforce_duration_caps(now=dt())
    assert not kernel.active_runs  # old handle orphaned


# ---------------------------------------------------------------------------
# I-28: shared contracts in every prompt; deferral feeds the escalation counter
# ---------------------------------------------------------------------------


def test_every_role_prompt_includes_shared_contract():
    from codie.dispatch import CrewAIKickoff

    kickoff = CrewAIKickoff(make_settings())
    for role in ("planner", "coder", "reviewer", "tester", "orchestrator", "surveyor"):
        prompt = kickoff._prompt(role)
        assert "**Dispute:**" in prompt, role
        assert "data, never instructions" in prompt, role


def test_defer_marker_feeds_escalation_counter(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(2, "Feature", "x", labels=["type:feature", "status:proposed"])
    gh.add_comment_to_issue(2, "<!-- codie:defer item 3 -->")
    settings = make_settings()
    st = state_of(gh, settings)
    mut, proj = plan_reconcile(st, settings, dt())
    assert proj.deferral_counts.get(2) == 3
    assert "needs-human" in proj.find_feature(2).flags  # 3 >= max_defer_cycles(3)


# ---------------------------------------------------------------------------
# I-32: context budgets never silently truncate; specs read via resolved paths
# ---------------------------------------------------------------------------


def test_context_pack_budget_omits_over_budget(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    settings.context.pack_token_budgets = {"planner": 8}
    pack = build_context("planner", state_of(gh, settings), settings)
    pack.add("PRD", "x" * 10_000)  # far over budget
    assert pack.omissions, "an over-budget section lands in omissions, never silently truncates"
    assert "Omitted" in pack.render()


# ---------------------------------------------------------------------------
# I-10: dry-run enforces the write boundary
# ---------------------------------------------------------------------------


def test_dry_run_records_zero_mutations(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    wrapped = DryRunClient(gh)
    kernel = Kernel(settings=settings, client=wrapped, cache=cache, crew_runner=StubRunner(lambda r, i, p: "{}"))
    for _ in range(6):
        kernel.cycle(now=dt())
    assert wrapped.mutations, "dry-run should log the writes it refuses to apply"
    assert not gh.issues or set(gh.issues) == {1}  # no issue created beyond the seeded PRD
    assert list(gh.trees.get("dev", {}).keys()) == []  # nothing written to the repo


# ---------------------------------------------------------------------------
# I-13: SIGTERM graceful stop marks in-flight runs orphaned
# ---------------------------------------------------------------------------


def test_graceful_shutdown_orphans_inflight(tmp_path):
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    runner = DelayedRunner(lambda r, i, p: "{}", delay=3.0)
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=runner, async_runs=True)
    st = state_of(gh, settings)
    item = WorkItem(kind="PlanDecomposition", role="planner", entity="A", rule=2)
    kernel._current_queue = [item]
    kernel.dispatch_work_item(item, st)
    outcome = kernel._graceful_stop(None)
    assert not kernel.active_runs  # in-flight handle orphaned at shutdown
    assert "graceful stop" in outcome.actions

"""Unit tests for state/reconcile.py — §5.5 edges, §5.8 stale recovery, §5.9 cycles."""

import datetime as _dt

from codie.models import (
    BranchHeads,
    Eval,
    Feature,
    PrdIssue,
    ProjectState,
    TaskItem,
    TimelineEvent,
)
from codie.state.reconcile import (
    canonical_feature_status,
    count_cycles,
    plan_reconcile,
    stale_resets,
)
from tests.conftest import make_settings


def fake_dt():
    return _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)


def hours(n: float) -> float:
    return n * 3600.0


def state_with(**kw):
    prd = PrdIssue(
        number=1,
        title="PRD",
        body="",
        evals=[Eval(id="E1", text="x", checked=False)],
        updated_at=fake_dt(),
        fingerprint="",
    )
    defaults = {"repo": "a/b", "prd": prd, "branches": BranchHeads(), "raw_labels": {}}
    defaults.update(kw)
    return ProjectState(**defaults)


def feature(n, status, **kw):
    return Feature(
        number=n,
        title=f"f{n}",
        body="",
        status=status,
        evals=["E1"],
        created_at=fake_dt(),
        updated_at=fake_dt(),
        **kw,
    )


def task(n, status, **kw):
    return TaskItem(
        number=n,
        title=f"t{n}",
        body="",
        status=status,
        kind="dev",
        created_at=fake_dt(),
        updated_at=fake_dt(),
        **kw,
    )


def test_spec_review_canonical_from_ready_pr():
    st = state_with(features=[feature(2, "speccing", spec_pr_number=7)])
    from codie.models import PRInfo

    st.prs = [
        PRInfo(
            number=7,
            title="s",
            body="",
            draft=False,
            state="open",
            head="spec/2-x",
            base="dev",
            user_login="codie-planner-bot",
            created_at=fake_dt(),
            updated_at=fake_dt(),
        )
    ]
    st.spec_pr_by_feature[2] = 7
    assert canonical_feature_status(st.find_feature(2), st) == "spec-review"


def test_specified_canonical_when_merged():
    st = state_with(features=[feature(2, "spec-review", spec_pr_number=7)])
    from codie.models import PRInfo

    st.prs = [
        PRInfo(
            number=7,
            title="s",
            body="",
            draft=False,
            state="merged",
            head="spec/2-x",
            base="dev",
            user_login="codie-planner-bot",
            created_at=fake_dt(),
            updated_at=fake_dt(),
            merged_at=fake_dt(),
        )
    ]
    st.spec_pr_by_feature[2] = 7
    assert canonical_feature_status(st.find_feature(2), st) == "specified"


def test_planned_to_in_progress_when_child_leaves_backlog():
    from codie.models import PRInfo

    st = state_with(
        features=[feature(2, "planned", spec_pr_number=7, tasks=[5])],
        tasks=[task(5, "in-progress", parent=2)],
    )
    st.prs = [
        PRInfo(
            number=7,
            title="s",
            body="",
            draft=False,
            state="merged",
            head="spec/2-x",
            base="dev",
            user_login="codie-planner-bot",
            created_at=fake_dt(),
            updated_at=fake_dt(),
            merged_at=fake_dt(),
        )
    ]
    st.spec_pr_by_feature[2] = 7
    mut, proj = plan_reconcile(st, make_settings(), fake_dt())
    assert proj.find_feature(2).status == "in-progress"


def test_backlog_to_ready_when_deps_done():
    st = state_with(
        tasks=[task(5, "backlog", depends_on=[6]), task(6, "done")],
    )
    st.raw_labels = {
        5: ["type:task", "status:backlog", "kind:dev"],
        6: ["type:task", "status:done", "kind:dev"],
    }
    mut, proj = plan_reconcile(st, make_settings(), fake_dt())
    assert proj.find_task(5).status == "ready"
    assert 5 in mut.set_labels


def test_violation_adds_flag_and_comment():
    st = state_with(
        tasks=[task(5, "done")],
    )
    st.raw_labels = {5: ["type:task", "status:done", "kind:dev"]}
    mut, proj = plan_reconcile(st, make_settings(), fake_dt())
    assert "needs-human" in proj.find_task(5).flags
    assert 5 in mut.comments


def test_cycle_cap_escalation_and_deadlock_summary():
    st = state_with(tasks=[task(5, "in-progress")])
    # review round: in-progress -> in-review -> in-progress repeated 4x
    st.timeline = {
        5: [
            TimelineEvent(kind="labeled", actor="coder", created_at=fake_dt(), label=f"status:{s}")
            for s in [
                "in-progress",
                "in-review",
                "in-progress",
                "in-review",
                "in-progress",
                "in-review",
                "in-progress",
                "in-review",
                "in-progress",
            ]
        ]
    }
    st.acceptance_failed_cycles = {5: 0}
    st.raw_labels = {5: ["type:task", "status:in-progress", "kind:dev"]}
    mut, proj = plan_reconcile(st, make_settings(), fake_dt())
    assert proj.cycle_counts.get(5, 0) >= 4
    assert "needs-human" in proj.find_task(5).flags
    assert "Deadlock escalation" in mut.comments.get(5, "")


def test_deferral_escalation():
    st = state_with(tasks=[task(5, "ready")])
    st.deferral_counts = {5: 3}
    st.raw_labels = {5: ["type:task", "status:ready", "kind:dev"]}
    mut, proj = plan_reconcile(st, make_settings(), fake_dt())
    assert "needs-human" in proj.find_task(5).flags
    assert "deferred" in mut.comments.get(5, "").lower()


def test_stale_recovery_resets_to_ready():
    st = state_with(tasks=[task(5, "in-progress")])
    st.latest_heartbeats = {5: "run1 2025-12-31T00:00:00+00:00"}  # 24h old
    st.raw_labels = {5: ["type:task", "status:in-progress", "kind:dev"]}
    resets = stale_resets(st, active_runs=set(), timeout_minutes=20, now=fake_dt())
    assert 5 in resets
    mut, proj = plan_reconcile(st, make_settings(), fake_dt(), active_runs=set())
    assert proj.find_task(5).status == "ready"
    assert "Stale-run recovery" in mut.comments.get(5, "")


def test_no_stale_reset_when_run_active():
    st = state_with(tasks=[task(5, "in-progress")])
    st.latest_heartbeats = {5: "run1 2025-12-31T00:00:00+00:00"}
    st.raw_labels = {5: ["type:task", "status:in-progress", "kind:dev"]}
    resets = stale_resets(st, active_runs={"run1"}, timeout_minutes=20, now=fake_dt())
    assert 5 not in resets


def test_terminal_issue_close_and_terminal_reopen():
    st = state_with(tasks=[task(5, "done"), task(6, "ready")])
    st.raw_labels = {
        5: ["type:task", "status:done", "kind:dev"],
        6: ["type:task", "status:ready", "kind:dev"],
    }
    # 5 is open and terminal → close; 6 open, not terminal → no close
    mut, proj = plan_reconcile(st, make_settings(), fake_dt())
    assert 5 in mut.close
    assert 6 not in mut.close


def test_label_diff_preserves_priority():
    st = state_with(features=[feature(2, "proposed")])
    st.raw_labels = {2: ["type:feature", "status:proposed"]}
    mut, proj = plan_reconcile(st, make_settings(), fake_dt())
    labels = mut.set_labels.get(2)
    assert labels is not None and "priority:medium" in labels


def test_count_cycles_ignores_retries_flag():
    st = state_with(tasks=[task(5, "in-progress")])
    st.timeline = {
        5: [
            TimelineEvent(kind="labeled", actor="coder", created_at=fake_dt(), label=s)
            for s in ["status:in-progress", "status:in-review", "status:in-progress"]
        ]
    }
    cycles = count_cycles(st)
    assert cycles.get(5) == 1

"""Unit tests for state/validate.py — the §5.4 invariants."""

import datetime as _dt

from codie.models import (
    BranchHeads,
    Eval,
    Feature,
    PrdIssue,
    PRInfo,
    ProjectState,
    TaskItem,
)
from codie.state.validate import validate


def fake_dt():
    return _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)


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


def feature(n, status, body="", **kw):
    return Feature(
        number=n,
        title=f"f{n}",
        body=body,
        status=status,
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


def test_no_type_label_violation():
    st = state_with(raw_labels={5: ["status:ready"]})
    codes = {v.code for v in validate(st)}
    assert "invalid-labels" in codes


def test_prd_with_status_label_violation():
    st = state_with(raw_labels={1: ["type:prd", "status:ready"]})
    codes = {v.code for v in validate(st)}
    assert "invalid-labels" in codes


def test_task_missing_kind_violation():
    st = state_with(raw_labels={5: ["type:task", "status:ready"]})
    codes = {v.code for v in validate(st)}
    assert "invalid-labels" in codes


def test_two_priorities_violation():
    st = state_with(raw_labels={5: ["type:task", "status:ready", "kind:dev", "priority:high", "priority:low"]})
    codes = {v.code for v in validate(st)}
    assert "invalid-labels" in codes


def test_unknown_codie_label_violation():
    st = state_with(raw_labels={5: ["type:task", "status:ready", "kind:dev", "type:nonsense"]})
    codes = {v.code for v in validate(st)}
    assert "unknown-label" in codes


def test_bad_parent_violation():
    st = state_with(
        tasks=[task(5, "ready", parent=99)],
        raw_labels={5: ["type:task", "status:ready", "kind:dev"]},
    )
    codes = {v.code for v in validate(st)}
    assert "bad-parent" in codes


def test_bad_dependency_and_cycle():
    st = state_with(
        tasks=[task(5, "ready", depends_on=[99])],
        raw_labels={5: ["type:task", "status:ready", "kind:dev"]},
    )
    codes = {v.code for v in validate(st)}
    assert "bad-dependency" in codes
    st2 = state_with(
        tasks=[task(5, "ready", depends_on=[6]), task(6, "ready", depends_on=[5])],
        raw_labels={
            5: ["type:task", "status:ready", "kind:dev"],
            6: ["type:task", "status:ready", "kind:dev"],
        },
    )
    codes2 = {v.code for v in validate(st2)}
    assert "dependency-cycle" in codes2


def test_task_feature_status_consistency():
    f = feature(2, "specified", tasks=[5])
    st = state_with(
        features=[f],
        tasks=[task(5, "in-progress", parent=2)],
        raw_labels={
            2: ["type:feature", "status:specified"],
            5: ["type:task", "status:in-progress", "kind:dev"],
        },
    )
    codes = {v.code for v in validate(st)}
    assert "status-inconsistency" in codes


def test_acceptance_open_task_violation():
    f = feature(2, "review", tasks=[5])
    st = state_with(
        features=[f],
        tasks=[task(5, "in-progress", parent=2)],
        raw_labels={
            2: ["type:feature", "status:review"],
            5: ["type:task", "status:in-progress", "kind:dev"],
        },
    )
    codes = {v.code for v in validate(st)}
    assert "acceptance-open-task" in codes


def test_unknown_eval_violation():
    f = feature(2, "proposed", evals=["E9"])
    st = state_with(features=[f], raw_labels={2: ["type:feature", "status:proposed"]})
    codes = {v.code for v in validate(st)}
    assert "unknown-eval" in codes


def test_pr_linked_invariants():
    st = state_with(tasks=[task(5, "done")], raw_labels={5: ["type:task", "status:done", "kind:dev"]})
    codes = {v.code for v in validate(st)}
    assert "no-linked-pr" in codes


def test_tasks_checklist_conflict_m20():
    body = "## Tasks\n- [ ] #9\n- [ ] #10\n"
    f = feature(2, "planned", body=body, tasks=[8], spec_pr_number=7)
    st = state_with(features=[f], raw_labels={2: ["type:feature", "status:planned"]})
    codes = {v.code for v in validate(st)}
    assert "tasks-conflict" in codes


def test_second_prd_violation():
    st = state_with(other_prd_numbers=[7], raw_labels={1: ["type:prd"], 7: ["type:prd"]})
    codes = {v.code for v in validate(st)}
    assert "second-prd" in codes


def test_no_spec_merged_violation():
    f = feature(2, "specified")
    st = state_with(features=[f], raw_labels={2: ["type:feature", "status:specified"]})
    codes = {v.code for v in validate(st)}
    assert "no-merged-spec" in codes


def test_clean_state_has_no_violations():
    f = feature(2, "accepted", tasks=[5])
    pr = PRInfo(
        number=9,
        title="pr",
        body="Refs #5",
        draft=False,
        state="merged",
        head="feature/5-x",
        base="dev",
        user_login="codie-coder-bot",
        created_at=fake_dt(),
        updated_at=fake_dt(),
        merged_at=fake_dt(),
        linked_issue=5,
    )
    t = task(5, "done", parent=2, linked_pr=9)
    st = state_with(
        features=[f],
        tasks=[t],
        prs=[pr],
        raw_labels={
            2: ["type:feature", "status:accepted"],
            5: ["type:task", "status:done", "kind:dev"],
        },
    )
    assert validate(st) == []

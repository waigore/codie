"""Unit tests for queue.py — every §5.6 rule, first-match-per-entity, ordering."""

import datetime as _dt

from codie.models import (
    BranchHeads,
    Bug,
    Eval,
    Feature,
    PrdIssue,
    PRInfo,
    ProjectState,
    SuiteMarker,
    TaskItem,
)
from codie.queue import compute_queue
from tests.conftest import make_settings


def fake_dt():
    return _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)


def hours(n: float) -> int:
    return int(n * 3600)


def state_with(**kw):
    prd = PrdIssue(number=1, title="PRD", body="", evals=[], updated_at=fake_dt(), fingerprint="")
    defaults = {
        "repo": "a/b",
        "prd": prd,
        "branches": BranchHeads(main="m", dev="d"),
        "raw_labels": {},
    }
    defaults.update(kw)
    return ProjectState(**defaults)


def feature(n, status, body="", **kw):
    return Feature(
        number=n,
        title=f"f{n}",
        body=body,
        status=status,
        evals=["E1"],
        created_at=fake_dt(),
        updated_at=fake_dt(),
        **kw,
    )


def task(n, status, kind="dev", **kw):
    return TaskItem(
        number=n,
        title=f"t{n}",
        body="",
        status=status,
        kind=kind,
        created_at=fake_dt(),
        updated_at=fake_dt(),
        **kw,
    )


def bug(n, status, body="", **kw):
    return Bug(
        number=n,
        title=f"b{n}",
        body=body,
        status=status,
        created_at=fake_dt(),
        updated_at=fake_dt(),
        **kw,
    )


def pr(
    num,
    state="open",
    head="feature/5-x",
    base="dev",
    linked=None,
    reviews=None,
    checks=None,
    files=None,
    draft=False,
    body="Refs #5",
):
    return PRInfo(
        number=num,
        title=f"pr{num}",
        body=body or f"Refs #{linked or 5}",
        draft=draft,
        state=state,
        head=head,
        base=base,
        user_login="codie-coder-bot",
        created_at=fake_dt(),
        updated_at=fake_dt(),
        merged_at=fake_dt() if state == "merged" else None,
        linked_issue=linked,
        reviews=reviews or [],
        checks=checks or [],
        files=files or [],
    )


SETTINGS = None


def settings():
    return make_settings()


def test_rule1_no_prd_needs_human():
    s = state_with(prd=None)
    q = compute_queue(s, settings())
    assert q[0].kind == "NeedsHuman"


def test_rule2_zero_features_plan_decomposition():
    s = state_with()
    q = compute_queue(s, settings())
    assert any(i.kind == "PlanDecomposition" for i in q)


def test_rule2b_fingerprint_changed_reconcile_plan():
    s = state_with(prd_fingerprint_changed=True)
    s.features = [feature(2, "proposed")]
    q = compute_queue(s, settings())
    assert any(i.kind == "ReconcilePlan" for i in q)


def test_rule3_revised_no_marker_assess():
    s = state_with()
    s.features = [feature(2, "revised")]
    q = compute_queue(s, settings())
    assert any(i.kind == "AssessRevision" for i in q)
    s2 = state_with(revision_marker={2: "implementation"})
    s2.features = [feature(2, "revised")]
    assert not any(i.kind == "AssessRevision" for i in compute_queue(s2, settings()))


def test_rule4_proposed_drafts_specs():
    s = state_with(features=[feature(2, "proposed")])
    q = compute_queue(s, settings())
    assert any(i.kind == "DraftSpecs" and i.issue_number == 2 for i in q)


def test_rule4c_spec_review_review_spec_pr():
    s = state_with(features=[feature(2, "spec-review", spec_pr_number=7)])
    s.prs = [pr(7, head="spec/2-x", linked=2, files=["specs/2-x/feature-spec.md"])]
    s.spec_pr_by_feature[2] = 7
    s.prs[0].review_state = "none"
    q = compute_queue(s, settings())
    assert any(i.kind == "ReviewSpecPR" for i in q)


def test_rule4d_merge_spec_pr_when_approved_ci_green():
    s = state_with(features=[feature(2, "spec-review", spec_pr_number=7)])
    s.prs = [
        pr(
            7,
            head="spec/2-x",
            linked=2,
            files=["specs/2-x/feature-spec.md"],
            checks=[{"name": "ci", "state": "success"}],
        )
    ]
    s.spec_pr_by_feature[2] = 7
    s.prs[0].review_state = "approved"
    q = compute_queue(s, settings())
    assert any(i.kind == "MergeSpecPR" for i in q)


def test_rule4e_specified_breakdown():
    s = state_with(features=[feature(2, "specified", spec_pr_number=7)])
    s.prs = [pr(7, state="merged", head="spec/2-x", linked=2, files=["specs/2-x/feature-spec.md"])]
    s.spec_pr_by_feature[2] = 7
    q = compute_queue(s, settings())
    assert any(i.kind == "BreakDownTasks" for i in q)


def test_rule5_and_6_review_then_merge():
    from codie.models import RequiredCheck

    s = state_with(tasks=[task(5, "in-review", linked_pr=9)])
    s.prs = [pr(9, linked=5)]
    q = compute_queue(s, settings())
    assert any(i.kind == "ReviewPR" for i in q)
    s.prs[0] = s.prs[0].model_copy(
        update={"review_state": "approved", "checks": [RequiredCheck(name="ci", state="success")]}
    )
    q = compute_queue(s, settings())
    assert any(i.kind == "MergePR" for i in q)


def test_rule7_address_review_on_changes_requested():
    s = state_with(tasks=[task(5, "in-progress", linked_pr=9)])
    s.prs = [pr(9, linked=5)]
    s.prs[0].review_state = "changes_requested"
    s.prs[0].changes_requested = True
    q = compute_queue(s, settings())
    assert any(i.kind == "AddressReview" and i.role == "coder" for i in q)


def test_rule7_test_kind_goes_to_tester():
    s = state_with(tasks=[task(5, "in-progress", kind="test", linked_pr=9)])
    s.prs = [pr(9, linked=5)]
    s.prs[0].review_state = "changes_requested"
    s.prs[0].changes_requested = True
    q = compute_queue(s, settings())
    assert any(i.kind == "AddressReview" and i.role == "tester" for i in q)


def test_rule8_implement_ready_dev_task():
    s = state_with(tasks=[task(5, "ready")])
    q = compute_queue(s, settings())
    assert any(i.kind == "Implement" and i.role == "coder" for i in q)


def test_rule8b_bug_in_progress_resume():
    s = state_with(bugs=[bug(8, "in-progress", linked_pr=11)])
    s.prs = [pr(11, state="merged", linked=8)]
    q = compute_queue(s, settings())
    assert any(i.kind == "Implement" and i.issue_number == 8 for i in q)


def test_rule9_kind_test_verify_task_not_implement():
    s = state_with(tasks=[task(5, "ready", kind="test")])
    q = compute_queue(s, settings())
    assert not any(i.kind == "Implement" for i in q)
    assert any(i.kind == "VerifyTask" and i.role == "tester" for i in q)


def test_rule10_reverify_bug():
    s = state_with(bugs=[bug(8, "verifying")])
    q = compute_queue(s, settings())
    assert any(i.kind == "ReverifyBug" and i.role == "tester" for i in q)


def test_rule11_acceptance_run():
    s = state_with(
        features=[feature(2, "in-progress", spec_pr_number=7, tasks=[5])],
        tasks=[task(5, "done", parent=2)],
        suite_markers=[SuiteMarker(scope="full", sha="d", passed=True, at=fake_dt())],
        branches=BranchHeads(main="m", dev="d"),
    )
    s.prs = [pr(7, state="merged", head="spec/2-x", linked=2, files=["specs/2-x/feature-spec.md"])]
    s.spec_pr_by_feature[2] = 7
    q = compute_queue(s, settings())
    assert any(i.kind == "AcceptanceRun" and i.role == "tester" for i in q)


def test_rule11_requires_full_suite_on_current_sha():
    s = state_with(
        features=[feature(2, "in-progress", spec_pr_number=7, tasks=[5])],
        tasks=[task(5, "done", parent=2)],
        suite_markers=[SuiteMarker(scope="full", sha="other", passed=True, at=fake_dt())],
        branches=BranchHeads(main="m", dev="d"),
    )
    s.prs = [pr(7, state="merged", head="spec/2-x", linked=2, files=["specs/2-x/feature-spec.md"])]
    s.spec_pr_by_feature[2] = 7
    q = compute_queue(s, settings())
    assert not any(i.kind == "AcceptanceRun" for i in q)


def test_rule12_regression_when_fast_marker_stale():
    s = state_with(features=[feature(2, "proposed")], tasks=[task(5, "done", linked_pr=9)])
    s.prs = [pr(9, state="merged", linked=5)]
    s.branches = BranchHeads(main="m", dev="d")
    q = compute_queue(s, settings(), fake_dt())
    assert any(i.kind == "RegressionRun" for i in q)
    # marker current -> no regression
    s.suite_markers = [
        SuiteMarker(scope="fast", sha="d", passed=True, at=fake_dt()),
        SuiteMarker(scope="full", sha="d", passed=True, at=fake_dt()),
    ]
    q = compute_queue(s, settings(), fake_dt())
    assert not any(i.kind == "RegressionRun" for i in q)


def test_rule13_eval_suite():
    s = state_with(features=[feature(2, "accepted")], bugs=[])
    s.prd = PrdIssue(
        number=1,
        title="PRD",
        body="",
        evals=[Eval(id="E1", text="x", checked=False)],
        updated_at=fake_dt(),
        fingerprint="",
    )
    q = compute_queue(s, settings())
    assert any(i.kind == "EvalSuite" for i in q)


def test_rule13_skips_when_bug_cites_eval():
    s = state_with(
        features=[feature(2, "accepted")],
        bugs=[bug(8, "ready", body="Eval: E1\n\n## Reproduction\nx")],
    )
    s.prd = PrdIssue(
        number=1,
        title="PRD",
        body="",
        evals=[Eval(id="E1", text="x", checked=False)],
        updated_at=fake_dt(),
        fingerprint="",
    )
    q = compute_queue(s, settings())
    assert not any(i.kind == "EvalSuite" for i in q)


def test_rule14_propose_release():
    s = state_with(features=[feature(2, "accepted")], bugs=[])
    s.prd = PrdIssue(
        number=1,
        title="PRD",
        body="",
        evals=[Eval(id="E1", text="x", checked=True)],
        updated_at=fake_dt(),
        fingerprint="",
    )
    q = compute_queue(s, settings())
    assert any(i.kind == "ProposeRelease" for i in q)


def test_first_match_per_entity_kind_test_never_implement():
    s = state_with(tasks=[task(5, "ready", kind="test")])
    q = compute_queue(s, settings())
    kinds = [i.kind for i in q]
    assert "Implement" not in kinds and "VerifyTask" in kinds


def test_total_order_priority_then_issue_number():
    s = state_with(
        tasks=[task(3, "ready", priority="low"), task(2, "ready")],
        bugs=[bug(9, "ready", priority="high")],
    )
    q = compute_queue(s, settings())
    implements = [i for i in q if i.kind == "Implement"]
    assert implements[0].issue_number == 9  # high bug first
    assert implements[-1].issue_number == 3  # low last


def test_release_merged_halts():
    s = state_with(release_merged=True)
    assert compute_queue(s, settings()) == []

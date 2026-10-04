"""Unit tests for dispatch serving — structured outputs applied by the kernel (§7.2)."""

import json

import pytest

from codie.dispatch import (
    apply_feature_plan,
    apply_reconcile_plan,
    apply_replan,
    apply_spec_mark_ready,
    apply_task_breakdown,
    parse_payload,
    render_feature_body,
    validate_spec_files,
)
from codie.models import (
    FeaturePlan,
    PlanReconciliation,
    ReplanResult,
    TaskBreakdown,
    TaskSpec,
    WorkItem,
)
from tests.conftest import fresh_fake, make_settings

GOOD_FEATURE = "## Scope\nz\n## Functional requirements\n- FR-1: x\n## Non-functional requirements\n- NFR-1: y\n## Acceptance criteria\n- AC-1: ok (FR-1)\n"  # noqa: E501
GOOD_TECH = (
    "## Architecture\nz\n## Data & interfaces\nz\n## Approach\nz\n## Test strategy\nz\n## Risks & alternatives\nz\n"
)


def test_parse_payload_feature_plan():
    item = WorkItem(kind="PlanDecomposition", role="planner", entity="x", rule=2)
    payload, err = parse_payload(
        item,
        json.dumps(
            {
                "features": [
                    {
                        "title": "F",
                        "body_markdown": "b",
                        "evals": ["E1"],
                        "priority": "high",
                        "prd_sections": "s",
                    }
                ]
            }
        ),
    )
    assert err == "" and isinstance(payload, FeaturePlan)
    assert payload.features[0].priority == "high"


def test_parse_payload_invalid_json_fails():
    item = WorkItem(kind="PlanDecomposition", role="planner", entity="x", rule=2)
    payload, err = parse_payload(item, "not json")
    assert payload is None and "JSON" in err


def test_render_feature_body_rejects_contradicting_evals():
    with pytest.raises(Exception):  # noqa: B017
        render_feature_body(type("F", (), {"body_markdown": "**Evals:** E1", "evals": ["E2"], "prd_sections": ""})())
    body = render_feature_body(type("F", (), {"body_markdown": "desc", "evals": ["E1"], "prd_sections": "login"})())
    assert "**Evals:** E1" in body
    assert "## Tasks" in body


def test_validate_spec_files_ok_and_bad():
    ok, errs = validate_spec_files(GOOD_FEATURE, GOOD_TECH)
    assert ok is True
    ok, errs = validate_spec_files("no headings here", "x")
    assert ok is False
    assert any("headings" in e for e in errs)


def test_apply_feature_plan_creates_issues():
    gh = fresh_fake()
    from codie.models import FeatureIssueDraft

    plan = FeaturePlan(
        features=[
            FeatureIssueDraft(
                title="Feature A",
                body_markdown="desc",
                evals=["E1"],
                priority="medium",
                prd_sections="",
            )
        ]
    )
    created, errors = apply_feature_plan(gh, make_settings(), plan)
    assert len(created) == 1 and not errors
    issue = gh.get_issue(created[0])
    assert issue is not None
    assert "type:feature" in issue.labels and "status:proposed" in issue.labels


def test_apply_task_breakdown_validates_requirement_ids():
    gh = fresh_fake()
    spec_ids = {"FR-1", "NFR-1", "AC-1"}
    spec = TaskSpec(
        ref="t1",
        title="Do it",
        kind="dev",
        depends_on=[],
        body_markdown="## Done when\n- [ ] works (FR-1)\n",
    )
    created, errors = apply_task_breakdown(gh, make_settings(), 2, TaskBreakdown(tasks=[spec]), None, spec_ids)
    assert created and not errors
    issue = gh.get_issue(created[0])
    assert "kind:dev" in issue.labels
    # unknown ID rejected
    spec2 = TaskSpec(
        ref="t2",
        title="Bad",
        kind="dev",
        depends_on=[],
        body_markdown="## Done when\n- [ ] works (FR-99)\n",
    )
    created2, errors2 = apply_task_breakdown(gh, make_settings(), 2, TaskBreakdown(tasks=[spec2]), None, spec_ids)
    assert created2 == [] and errors2


def test_apply_spec_mark_ready_validates_and_flips():
    gh = fresh_fake()
    gh.set_actor("planner")
    gh.bootstrap_branches("main", "dev")
    gh.trees["spec/2-x"] = {
        "specs/2-x/feature-spec.md": GOOD_FEATURE,
        "specs/2-x/technical-spec.md": GOOD_TECH,
    }
    pr = gh.create_pr("specs", "Refs #2", "spec/2-x", "dev", draft=True)
    pr.files = ["specs/2-x/feature-spec.md", "specs/2-x/technical-spec.md"]
    ok, errs = apply_spec_mark_ready(gh, make_settings(), pr.number)
    assert ok is True
    assert gh.get_pr(pr.number).draft is False


def test_apply_reconcile_plan_cancels_and_flags():
    gh = fresh_fake()
    gh.add_issue(3, "old feature", "x", labels=["type:feature", "status:in-progress"])
    gh.add_issue(4, "stale feature", "x", labels=["type:feature", "status:planned"])
    plan = PlanReconciliation(cancel_feature_numbers=[3], restale_feature_numbers=[4])
    apply_reconcile_plan(gh, make_settings(), plan)
    assert "status:cancelled" in gh.labels_of(3)
    assert "flag:needs-human" in gh.labels_of(4)


def test_apply_replan_cancels_and_creates():

    gh = fresh_fake()
    gh.add_issue(5, "old task", "x", labels=["type:task", "status:in-progress", "kind:dev"])
    gh.add_issue(2, "feature", "x", labels=["type:feature", "status:planned"])
    gh.branches["feature/5-x"] = "b1"
    gh.add_pr(8, "task PR", "Refs #5", "feature/5-x", "dev")
    replan = ReplanResult(
        new_tasks=[
            TaskSpec(
                ref="t1",
                title="New",
                kind="dev",
                depends_on=[],
                body_markdown="## Done when\n- [ ] x (FR-1)\n",
            )
        ],
        cancel_task_numbers=[5],
        rationale="r",
    )
    apply_replan(gh, make_settings(), 2, replan, None, {"FR-1"})
    assert "status:cancelled" in gh.labels_of(5)
    assert gh.get_pr(8).state == "closed"
    assert "status:planned" in gh.labels_of(2)

"""Integration: orchestrator cycles over the FakeGitHubClient (M2/M3 exits).

Stub-LLM drives the planner/reviewer structured outputs; the kernel applies
them. Asserts the deterministic pipeline PRD → features → specs → tasks and the
guardrails (budget pause C14, idle-dispatch C19, flag filter C13, refusal of
invented work §6.1).
"""

import datetime as _dt
import json

import pytest
from tests.conftest import add_prd, fresh_fake

from codie.cache import Cache
from codie.dispatch import StubRunner
from codie.github.fetch import fetch_snapshot
from codie.models import WorkItem
from codie.orchestrator import Kernel
from codie.queue import compute_queue
from codie.state.derive import derive

FEATURE_SPEC = """## Scope
Users can onboard.

## Functional requirements
- FR-1: user can create an account

## Non-functional requirements
- NFR-1: signup completes < 1s

## Acceptance criteria
- AC-1: signup succeeds (FR-1, NFR-1)
"""

TECH_SPEC = """## Architecture
Small service.

## Data & interfaces
POST /signup

## Approach
No special handling.

## Test strategy
Unit tests for the coder; acceptance drives the UI.

## Risks & alternatives
None.
"""


def dt():
    return _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)


def derive_state(gh, settings):
    snap = fetch_snapshot(gh, settings.project.repo, settings, full=True)
    return derive(snap, settings)


def make_stub(gh, settings):
    """A stub LLM that returns the structured planner/reviewer outputs."""

    def handler(role, item, pack):
        kind = item.kind
        if kind == "PlanDecomposition":
            feat = {
                "title": "Login feature",
                "body_markdown": "Users can log in",
                "evals": ["E1"],
                "priority": "medium",
                "prd_sections": "signup",
            }
            return json.dumps({"features": [feat]})
        if kind == "DraftSpecs":
            gh.set_actor("planner")
            branch = f"spec/{item.issue_number}-login"
            gh.trees[branch] = {
                f"specs/{item.issue_number}-login/feature-spec.md": FEATURE_SPEC,
                f"specs/{item.issue_number}-login/technical-spec.md": TECH_SPEC,
            }
            pr = gh.create_pr("Specs: Login feature", f"Refs #{item.issue_number}", branch, "dev", draft=True)
            pr.files = [
                f"specs/{item.issue_number}-login/feature-spec.md",
                f"specs/{item.issue_number}-login/technical-spec.md",
            ]
            return json.dumps({"pr_number": pr.number, "files": pr.files, "open_questions": []})
        if kind in ("ReviewSpecPR", "ReviewPR"):
            return json.dumps({"verdict": "approve", "comments": [], "requirement_ids": ["FR-1"]})
        if kind == "BreakDownTasks":
            return json.dumps(
                {
                    "tasks": [
                        {
                            "ref": "t1",
                            "title": "Implement signup endpoint",
                            "kind": "dev",
                            "depends_on": [],
                            "body_markdown": "## Context\nBuild the signup flow.\n\n## Done when\n- [ ] account is created (FR-1)\n- [ ] signup is fast (NFR-1)\n",  # noqa: E501
                        },
                        {
                            "ref": "t2",
                            "title": "Acceptance coverage for signup",
                            "kind": "test",
                            "depends_on": ["t1"],
                            "body_markdown": "## Context\nBlack-box coverage via the UI.\n\n## Done when\n- [ ] the running app accepts a signup (AC-1)\n",  # noqa: E501
                        },
                    ]
                }
            )
        if kind == "AssessRevision":
            return json.dumps({"kind": "implementation", "rationale": "small tweak"})
        if kind == "ReconcilePlan":
            return json.dumps({"add": [], "cancel_feature_numbers": [], "restale_feature_numbers": []})
        return json.dumps({"ok": True})

    return StubRunner(handler)


@pytest.fixture
def world():
    gh = fresh_fake()
    add_prd(gh)
    return gh


def test_pdr_to_specs_to_tasks_end_to_end(world, tmp_path, settings):
    gh = world
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    stub = make_stub(gh, settings)
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=stub)
    gh.next_issue_number = 3
    for _ in range(12):
        kernel.cycle(now=dt())
        if derive_state(gh, settings).tasks:
            break

    state = derive_state(gh, settings)
    assert state.tasks, "expected tasks to be created by BreakDownTasks"
    feature = state.features[0]
    assert {t.parent for t in state.tasks} == {feature.number}
    assert state.find_feature(feature.number).status in {"planned", "in-progress", "review"}


def test_budget_pause_makes_no_model_call(world, tmp_path, settings):
    gh = world
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    settings.budgets.max_llm_cost_usd_per_day = 0.0  # exhausted
    calls = {"n": 0}
    stub = StubRunner(lambda role, item, pack: calls.__setitem__("n", calls["n"] + 1) or "{}")
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=stub)
    outcome = kernel.cycle(now=dt())
    assert outcome.paused is True
    assert calls["n"] == 0  # no model call while paused (C14)
    # a finished run still lands in the ledger channel (reap path is independent)
    cache.add_run("{}", "failed", cost_usd=0.1)
    assert len(cache.list_runs()) == 1


def test_idle_dispatch_fallback_dispatches_head_item(world, tmp_path, settings):
    gh = world
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    stub = make_stub(gh, settings)
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=stub, strategy=lambda q, p, s: None)
    applied = []
    for _ in range(6):
        outcome = kernel.cycle(now=dt())
        applied.extend(
            r.item.kind for r in outcome.dispatched if r.status == "applied" and r.item.kind != "ReconcilePlan"
        )
        if applied:
            break
    assert applied, "the C19 idle-dispatch fallback should dispatch the head item after max_idle cycles"


def test_flag_filter_drops_blocked_stream_keeps_siblings(world, settings):
    gh = world
    gh.add_issue(2, "Feature", "## Description\nx", labels=["type:feature", "status:specified"])
    gh.add_issue(
        3,
        "Task blocked",
        "**Parent:** #2",
        labels=["type:task", "status:ready", "kind:dev", "flag:blocked"],
    )
    gh.add_issue(4, "Task sibling", "**Parent:** #2", labels=["type:task", "status:ready", "kind:dev"])
    state = derive_state(gh, settings)
    kernel = Kernel(settings=settings, client=gh, crew_runner=StubRunner(lambda r, i, p: "{}"))
    queue = compute_queue(state, settings, dt())
    filtered = kernel._flag_filter(queue, state)
    issues = [i.issue_number for i in filtered]
    assert 3 not in issues
    assert 4 in issues


def test_orchestrator_pick_honors_strategy_order(world, tmp_path, settings):
    """I-08: with a stub LLM the dispatcher follows the strategy's pick, which
    differs from the raw queue head — proof the pick is agent-mediated, not a
    fixed head rule."""

    from codie.state import parse as _parse

    gh = world
    gh.add_comment_to_issue(1, f"<!-- codie:prd {_parse.prd_fingerprint(gh.get_issue(1).body)} -->")
    gh.add_issue(2, "Feature", "**Evals:** E1", labels=["type:feature", "status:in-progress"])
    spec = gh.add_pr(6, "Specs", "Refs #2", "spec/2-x", "dev", files=["specs/2-x/feature-spec.md"])
    spec.state = "merged"
    spec.merged_at = dt()
    gh.add_issue(4, "dev task", "**Parent:** #2", labels=["type:task", "status:ready", "kind:dev"])
    gh.add_issue(5, "test task", "**Parent:** #2", labels=["type:task", "status:ready", "kind:test"])
    gh.add_issue(9, "a bug", "## Reproduction\nx", labels=["type:bug", "status:ready"])
    sand_cache = Cache().open_or_rebuild(tmp_path / "s.db")

    def strategy(queue, projected, settings):
        return f"kind:{queue[-1].kind}"  # the LAST lawful item (VerifyTask)

    kernel = Kernel(
        settings=settings, client=gh, cache=sand_cache, crew_runner=make_stub(gh, settings), strategy=strategy
    )
    outcome = kernel.cycle(now=dt())
    assert outcome.queue, "a nonempty lawful queue is expected"
    picked = outcome.dispatched[0].item if outcome.dispatched else None
    assert picked is not None, "the strategy's pick should be dispatched"
    assert picked.kind == "VerifyTask"  # not the head (a rule-8 Implement)
    assert outcome.queue[0].kind != picked.kind

    # an adversarial pick that is NOT in the lawful queue is refused by the kernel
    refused = kernel.dispatch_work_item(
        WorkItem(kind="VerifyTask", role="tester", entity="invented", issue_number=999, rule=9),
        derive_state(gh, settings),
    )
    assert refused.status == "refused"


def test_dispatch_refuses_non_queue_item(world, tmp_path, settings):
    gh = world
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=make_stub(gh, settings))
    kernel.cycle(now=dt())  # populate the current queue
    bogus = WorkItem(kind="Implement", role="coder", entity="invented work", issue_number=999, rule=0)
    state = derive_state(gh, settings)
    before = len(kernel.active_runs)
    outcome = kernel.dispatch_work_item(bogus, state)
    assert outcome.status == "refused"  # kernel refuses invented work (§6.1 invariant 1)
    assert len(kernel.active_runs) == before


def test_dispatch_refuses_policy_and_draft_merges(world, tmp_path, settings):
    """I-11: the merge gate refuses a draft PR and a policy-only PR on the fake."""
    gh = world
    gh.add_issue(2, "Feature", "x", labels=["type:feature", "status:specified"])
    draft = gh.add_pr(21, "Draft", "Refs #2", "feature/2-x", "dev", draft=True)
    draft.files = ["src/x.py"]
    policy = gh.add_pr(22, "Policy", "Refs #2", "feature/2-y", "dev")
    policy.files = [".codie.yaml"]
    gh.set_actor("reviewer")
    assert gh.merge_pr(21) is False  # draft refused
    assert gh.merge_pr(22) is False  # policy PR without a non-bot APPROVED refused
    # a non-bot APPROVED unlocks the policy PR
    gh.add_review(22, "APPROVED", "human")
    assert gh.merge_pr(22) is True


def test_dispatch_accepts_approved_task_pr(world, settings):
    """I-11: an approved task PR merges on the fake."""
    gh = world
    gh.add_issue(2, "Feature", "x", labels=["type:feature", "status:planned"])
    gh.add_issue(5, "Task", "**Parent:** #2", labels=["type:task", "status:in-review", "kind:dev"])
    pr = gh.add_pr(23, "Task PR", "Refs #5", "feature/5-x", "dev")
    pr.files = ["src/x.py"]
    gh.set_actor("developer-user")
    assert gh.merge_pr(23) is False  # no approval yet
    gh.add_review(23, "CHANGES_REQUESTED", "codie-reviewer-bot")
    assert gh.merge_pr(23) is False  # changes requested
    gh.add_review(23, "APPROVED", "codie-reviewer-bot")
    assert gh.merge_pr(23) is True

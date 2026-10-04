"""Integration: the tail of the workflow over the fake — task→PR→merge→accept→
release→halt (I-14, I-17, I-18, I-19, I-22, I-25), plus scenario-specific
guards. The stub LLM plays each role crew through the tool-acting path, so the
kernel's apply/verify machinery and the GitHub side-effects are all exercised.
"""

import datetime as _dt
import json

import pytest
from tests.conftest import add_prd, fresh_fake, make_settings

from codie.cache import Cache
from codie.dispatch import StubRunner
from codie.github.fetch import fetch_snapshot
from codie.orchestrator import Kernel
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
    return derive(fetch_snapshot(gh, settings.project.repo, settings, full=True), settings)


def _set_in_review(gh, number, status, pr):
    gh.set_labels(
        number,
        [label for label in gh.get_issue(number).labels if not label.startswith("status:")] + [f"status:{status}"],
    )
    pr.state = "open"


def make_full_stub(gh, settings, evals_checked=None, evals_failed=None):
    """A stub LLM that plays all role crews through the tool-acting path."""
    evals_checked = ["E1"] if evals_checked is None else evals_checked
    evals_failed = evals_failed or []

    def handler(role, item, pack):
        kind = item.kind
        if kind == "PlanDecomposition":
            feat = {
                "title": "Login feature",
                "body_markdown": "Users can log in",
                "evals": ["E1", "E2"],
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
                            "body_markdown": "## Done when\n- [ ] account is created (FR-1)\n",
                        },
                        {
                            "ref": "t2",
                            "title": "Acceptance coverage for signup",
                            "kind": "test",
                            "depends_on": ["t1"],
                            "body_markdown": "## Done when\n- [ ] the running app accepts a signup (AC-1)\n",
                        },
                    ]
                }
            )
        if kind == "Implement":
            gh.set_actor("coder")
            num = item.issue_number
            branch = f"feature/{num}-task"
            gh.branches[branch] = gh.branches.get("dev") or "00000000"
            gh.trees[branch] = {f"src/{num}.py": f"print({num})\n"}
            pr = gh.create_pr(f"feat task #{num}", f"Refs #{num}", branch, "dev")
            pr.files = [f"src/{num}.py"]
            gh.set_labels(
                num,
                [label for label in gh.get_issue(num).labels if not label.startswith("status:")]
                + ["status:in-progress"],
            )
            gh.set_labels(
                num,
                [label for label in gh.get_issue(num).labels if not label.startswith("status:")]
                + ["status:in-review"],
            )
            return json.dumps({"branch": branch, "pr_number": pr.number, "summary": "implemented"})
        if kind == "VerifyTask":
            gh.set_actor("tester")
            num = item.issue_number
            branch = f"test/{num}-suite"
            gh.branches[branch] = gh.branches.get("dev") or "00000000"
            gh.trees[branch] = {f"tests/acceptance/test_{num}.py": "def test_x():\n    assert True\n"}
            pr = gh.create_pr(f"acceptance #{num}", f"Refs #{num}", branch, "dev")
            pr.files = [f"tests/acceptance/test_{num}.py"]
            gh.set_labels(
                num,
                [label for label in gh.get_issue(num).labels if not label.startswith("status:")]
                + ["status:in-progress"],
            )
            gh.set_labels(
                num,
                [label for label in gh.get_issue(num).labels if not label.startswith("status:")]
                + ["status:in-review"],
            )
            return json.dumps({"passed": True, "sha": gh.branches.get("dev", ""), "summary": "covered"})
        if kind == "RegressionRun":
            return json.dumps(
                {"scope": "full", "sha": gh.branches.get("dev", ""), "passed": True, "summary": "regression ok"}
            )
        if kind == "AcceptanceRun":
            return json.dumps(
                {"passed": True, "sha": gh.branches.get("dev", ""), "summary": "every AC passes through the UI"}
            )
        if kind == "EvalSuite":
            return json.dumps({"checked": evals_checked, "uncertain": [], "failed": evals_failed})
        if kind == "ProposeRelease":
            return json.dumps(
                {"version": "1.0.0", "changelog": "## Changelog\n- login\n", "eval_report": "E1,E2 pass"}
            )
        if kind == "Replan":
            spec = {
                "ref": "n1",
                "title": "Rework signup",
                "kind": "dev",
                "depends_on": [],
                "body_markdown": "## Done when\n- [ ] reworked (FR-1)\n",
            }
            return json.dumps({"new_tasks": [spec], "cancel_task_numbers": [], "rationale": "rework"})
        return json.dumps({"ok": True})

    return StubRunner(handler)


@pytest.fixture
def ended_world(tmp_path):
    """Drive the full chain to feature accepted (human UAT flips review → accepted)."""
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    cache = Cache().open_or_rebuild(tmp_path / "chain.db")
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=make_full_stub(gh, settings))
    gh.next_issue_number = 3
    for _ in range(60):
        kernel.cycle(now=dt())
        st = derive_state(gh, settings)
        if st.features and st.features[0].status == "review":
            # human UAT: accept
            st.features[0].status = "accepted"
            gh.set_labels(
                st.features[0].number,
                [label for label in gh.get_issue(st.features[0].number).labels if not label.startswith("status:")]
                + ["status:accepted"],
            )
    yield gh, settings
    return gh, settings


def run_until(gh, settings, predicate, max_cycles=80, extra_cycle=None):
    import tempfile
    from pathlib import Path

    cache = Cache().open_or_rebuild(Path(tempfile.mkdtemp()) / "x.db")
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=make_full_stub(gh, settings))
    for _ in range(max_cycles):
        kernel.cycle(now=dt())
        st = derive_state(gh, settings)
        if extra_cycle is not None:
            extra_cycle(gh, settings, st)
        if predicate(gh, settings, st):
            return kernel, st
    return kernel, derive_state(gh, settings)


def test_implement_dispatch_creates_branch_pr_and_heartbeat(tmp_path):
    """I-14/I-25: an Implement dispatch yields a branch, a commit, an open PR
    with `Refs #n`, status in-progress in the timeline, and a heartbeat marker."""
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    cache = Cache().open_or_rebuild(tmp_path / "c.db")
    kernel = Kernel(settings=settings, client=gh, cache=cache, crew_runner=make_full_stub(gh, settings))
    gh.next_issue_number = 3
    # advance to a ready dev task
    for _ in range(12):
        kernel.cycle(now=dt())
        st = derive_state(gh, settings)
        if any(t.status in {"ready"} if False else t.kind == "dev" and t.status == "ready" for t in st.tasks):
            break
    st = derive_state(gh, settings)
    task = next(t for t in st.tasks if t.kind == "dev" and t.status == "ready")
    gh.next_issue_number = None
    import tempfile
    from pathlib import Path

    cache2 = Cache().open_or_rebuild(Path(tempfile.mkdtemp()) / "c2.db")
    k2 = Kernel(settings=settings, client=gh, cache=cache2, crew_runner=make_full_stub(gh, settings))
    k2._current_queue = [
        __import__("codie.models", fromlist=["WorkItem"]).WorkItem(
            kind="Implement", role="coder", entity=str(task.number), issue_number=task.number, rule=8
        )
    ]
    item = k2._current_queue[0]
    outcome = k2.dispatch_work_item(item, derive_state(gh, settings))
    assert outcome.status == "applied"
    st = derive_state(gh, settings)
    prs = [p for p in gh.list_prs() if f"Refs #{task.number}" in (p.body or "")]
    assert prs, "expected an open PR with Refs #<task>"
    assert gh.branches.get(f"feature/{task.number}-task"), "expected a task branch"
    assert gh.trees.get(f"feature/{task.number}-task", {}).get(f"src/{task.number}.py"), "expected a committed file"
    hb = [c.body for c in gh.list_comments() if "codie:heartbeat" in c.body and c.issue_number == task.number]
    assert hb, "expected a kernel heartbeat marker on the issue (I-25)"
    # the worker claimed the task (in-progress) before opening the PR
    labels_ever = {e.label for e in gh.list_timeline(task.number) if e.kind == "labeled"}
    assert "status:in-progress" in labels_ever


def test_acceptance_passing_moves_feature_to_review(tmp_path):
    """I-17: a passing AcceptanceRun moves the feature to status:review (not back to revised)."""
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    kernel, st = run_until(gh, settings, lambda g, s, st: bool(st.features and st.features[0].status == "review"))
    feature = st.features[0]
    assert feature.status == "review"
    assert all(t.status == "done" for t in st.tasks if t.parent == feature.number)
    uat = [c.body for c in gh.list_comments() if c.issue_number == feature.number and "UAT" in c.body]
    assert uat, "a UAT summary comment should be posted"


def test_eval_checks_off_exactly_one_eval_by_id(tmp_path):
    """I-18: EvalSuite checks off only the eval ids reported; a failing eval stays
    unchecked and files a type:bug."""
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()

    # Phase A: reach review, then human-accept, then run eval with E1 ok / E2 fail.
    cache = Cache().open_or_rebuild(tmp_path / "e.db")
    _kernel, st = run_until(gh, settings, lambda g, s, st: bool(st.features and st.features[0].status == "review"))
    feature = st.features[0]
    gh.set_labels(
        feature.number,
        [label for label in gh.get_issue(feature.number).labels if not label.startswith("status:")]
        + ["status:accepted"],
    )
    # Phase B: EvalSuite with E1 checked, E2 failed
    kernel2 = Kernel(
        settings=settings, client=gh, cache=cache, crew_runner=make_full_stub(gh, settings, evals_failed=["E2"])
    )
    for _ in range(10):
        kernel2.cycle(now=dt())
        st = derive_state(gh, settings)
        if st.prd and st.prd.evals and st.prd.evals[0].checked:
            break
    prd = derive_state(gh, settings).prd
    by_id = {e.id: e for e in prd.evals}
    assert by_id["E1"].checked is True
    assert by_id["E2"].checked is False
    bugs = [b for b in derive_state(gh, settings).bugs if "E2" in (b.body or "")]
    assert bugs, "a failing eval should file a type:bug citing it"


def test_release_chain_reaches_halt(tmp_path):
    """I-19: all done → regression → eval → release PR → human merge → halt."""
    gh = fresh_fake()
    add_prd(gh)
    settings = make_settings()
    cache = Cache().open_or_rebuild(tmp_path / "r.db")
    kernel = Kernel(
        settings=settings,
        client=gh,
        cache=cache,
        crew_runner=make_full_stub(gh, settings, evals_checked=["E1", "E2"]),
    )
    gh.next_issue_number = 3
    halted = False
    for _ in range(90):
        outcome = kernel.cycle(now=dt())
        st = derive_state(gh, settings)
        if outcome.halted:
            halted = True
            break
        feature = st.features[0] if st.features else None
        if feature is not None and feature.status == "review":
            gh.set_labels(
                feature.number,
                [label for label in gh.get_issue(feature.number).labels if not label.startswith("status:")]
                + ["status:accepted"],
            )
        if st.prd and st.prd.evals and all(e.checked for e in st.prd.evals):
            release_prs = [p for p in gh.list_prs() if "codie:release" in (p.body or "")]
            if release_prs and release_prs[0].state == "open":
                rp = release_prs[0]
                rp.state = "merged"
                rp.merged_at = dt()
                gh.branches["main"] = gh.branches.get("dev")
    assert halted, "the marked release PR merge should halt the daemon"

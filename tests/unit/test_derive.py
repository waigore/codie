"""Unit tests for tests/unit/test_derive.py — payloads → ProjectState (§5.3)."""

from datetime import UTC, datetime

from codie.github.fetch import fetch_snapshot
from codie.models import GitHubReview
from codie.state import parse
from codie.state.derive import derive
from tests.conftest import PRD_BODY, add_prd, fresh_fake, make_settings


def _snapshot(gh, repo="acme/test", settings=None):
    settings = settings or make_settings(repo)
    return fetch_snapshot(gh, repo, settings, full=True)


def test_prd_and_evals_derived():
    gh = fresh_fake()
    add_prd(gh)
    st = derive(_snapshot(gh))
    assert st.prd is not None
    assert {e.id for e in st.prd.evals} == {"E1", "E2"}
    assert st.prd_fingerprint_changed is True  # no marker yet


def test_second_prd_keeps_pinned_newest():
    gh = fresh_fake()
    gh.add_issue(1, "PRD A", PRD_BODY, labels=["type:prd"])
    gh.add_issue(2, "PRD B", PRD_BODY, labels=["type:prd"], state="closed")
    # pin the newer/older appropriately
    gh.issues[1].pinned = True
    st = derive(_snapshot(gh))
    assert st.prd is not None and st.prd.number == 1
    assert st.other_prd_numbers == [2]
    assert any(v.code == "second-prd" for v in st.violations)


def test_untyped_open_issue_is_anomaly():
    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(10, "question?", "how do I?", labels=[])
    st = derive(_snapshot(gh))
    assert any(a.kind == "untyped-issue" for a in st.anomalies)


def test_task_parent_links_feature_tasks_set():
    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(2, "Feature", "f", labels=["type:feature", "status:specified"])
    gh.add_issue(3, "Task A", "**Parent:** #2", labels=["type:task", "status:ready", "kind:dev"])
    gh.add_issue(
        4,
        "Task B",
        "**Parent:** #2\n**Depends on:** #3",
        labels=["type:task", "status:backlog", "kind:test"],
    )
    st = derive(_snapshot(gh))
    feature = st.find_feature(2)
    assert feature is not None and feature.tasks == [3, 4]
    assert st.find_task(4).depends_on == [3]


def test_pr_review_state_and_linkage():
    gh = fresh_fake()
    add_prd(gh)
    gh.set_actor("coder")
    gh.add_issue(2, "Feature", "f", labels=["type:feature", "status:in-progress"])
    gh.add_issue(
        3,
        "Task",
        "**Parent:** #2\n## Done when\n- [ ] x (FR-1)",
        labels=["type:task", "status:in-progress", "kind:dev"],
    )
    gh.branches["feature/3-x"] = "b1"
    pr = gh.add_pr(5, "Task PR", "Refs #3", "feature/3-x", "dev", files=["src/x.py"])
    st = derive(_snapshot(gh))
    p = st.find_pr(5)
    assert p is not None and p.linked_issue == 3
    assert st.find_task(3).linked_pr == 5
    st2 = derive(_snapshot(gh))
    # add an APPROVED review from the reviewer bot
    gh.bot_logins.add("codie-reviewer-bot")
    rv = GitHubReview(user_login="codie-reviewer-bot", state="APPROVED", submitted_at=datetime.now(UTC))
    pr.reviews = [rv]
    st2 = derive(_snapshot(gh))
    assert st2.find_pr(5).review_state == "approved"


def test_release_marker_halt():
    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(2, "Feature", "f", labels=["type:feature", "status:accepted"])
    gh.branches["dev"] = "d1"
    pr = gh.add_pr(9, "Release", "<!-- codie:release -->\nchangelog", "dev→main", "main", state="closed")
    pr.merged_at = datetime.now(UTC)
    st = derive(_snapshot(gh))
    assert st.release_merged is True


def test_spec_pr_detected_from_files_and_linked_to_feature():
    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(2, "Login feature", "x", labels=["type:feature", "status:speccing"])
    gh.branches["spec/2-login"] = "s1"
    gh.add_pr(
        6,
        "Specs",
        "Refs #2",
        "spec/2-login",
        "dev",
        draft=True,
        files=["specs/2-login/feature-spec.md", "specs/2-login/technical-spec.md"],
    )
    st = derive(_snapshot(gh))
    p = st.find_pr(6)
    assert p is not None and p.spec_marker is True
    assert st.spec_pr_by_feature.get(2) == 6


def test_suite_and_uncertain_markers():
    gh = fresh_fake()
    prd = add_prd(gh)
    gh.add_comment_to_issue(prd, "<!-- codie:suite fast abc123 pass -->")
    gh.add_comment_to_issue(prd, "<!-- codie:eval E3 uncertain -->")
    st = derive(_snapshot(gh))
    assert st.latest_suite_marker("fast").sha == "abc123"
    assert "E3" in st.eval_uncertain


def test_fingerprint_change_detection():
    gh = fresh_fake()
    prd = add_prd(gh)
    st = derive(_snapshot(gh))
    fp = st.prd.fingerprint
    gh.add_comment_to_issue(prd, f"<!-- codie:prd {fp} -->")
    st = derive(_snapshot(gh))
    assert st.prd_fingerprint_changed is False
    gh.issues[prd].body = PRD_BODY + "\n- [ ] E9: extra\n"
    st = derive(_snapshot(gh))
    assert st.prd_fingerprint_changed is True


def test_spec_requirement_lines_from_merged_spec_files():
    from codie.models import BranchHeads
    from codie.state.derive import RepoSnapshot

    gh = fresh_fake()
    add_prd(gh)
    gh.add_issue(2, "Feature", "x", labels=["type:feature", "status:specified"])
    spec = (
        "## Scope\n\n## Functional requirements\n- FR-1: login works\n"
        "## Non-functional requirements\n- NFR-1: <200ms\n## Acceptance criteria\n- AC-1: ok (FR-1)\n"
    )
    gh.trees["dev"]["specs/2-feature/feature-spec.md"] = spec
    snap = RepoSnapshot(
        repo="acme/test",
        issues=list(gh.issues.values()),
        comments=[],
        prs=gh.list_prs(),
        timeline={},
        branches=BranchHeads(main=gh.branches["main"], dev=gh.branches["dev"]),
        codie_yaml_present=True,
        spec_files={"feature.2.feature-spec.md": spec},
    )
    st = derive(snap)
    feature = st.find_feature(2)
    assert feature is not None
    assert parse.requirement_id(feature.requirement_lines[0]) == "FR-1"


def test_branch_heads_and_codie_yaml_presence():
    gh = fresh_fake()
    add_prd(gh)
    gh.trees["dev"] = {".codie.yaml": "branches:\n  dev: dev\n"}
    st = derive(_snapshot(gh))
    assert st.branches.dev == gh.branches["dev"]
    assert st.codie_yaml_present is True

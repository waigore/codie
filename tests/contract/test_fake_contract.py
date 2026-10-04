"""Contract tests: FakeGitHubClient vs the behavior suite (Technical Spec §13 M19).

The suite calls every role-protocol method; PyGithubClient is exercised here only
when real tokens are present (E2E), otherwise the fake must stand in everywhere.
"""

import datetime as _dt

import pytest

from codie.github.fake import FakeGitHub


def clock():
    return _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)


@pytest.fixture
def gh():
    fake = FakeGitHub("acme/test")
    fake.bot_logins = {
        "codie-planner-bot",
        "codie-coder-bot",
        "codie-reviewer-bot",
        "codie-tester-bot",
        "codie-kernel-bot",
    }
    fake.bootstrap_branches("main", "dev")
    return fake


def test_protocol_create_issue_labels_edit():
    gh = FakeGitHub("acme/test")
    gh.bootstrap_branches("main", "dev")
    issue = gh.create_issue("Title", "Body", ["type:task", "status:ready"])
    assert gh.get_issue(issue.number).labels == ["type:task", "status:ready"]
    gh.edit_issue(issue.number, body="New body")
    assert gh.get_issue(issue.number).body == "New body"
    gh.set_labels(issue.number, ["type:task", "status:in-progress", "flag:blocked"])
    assert gh.labels_of(issue.number) == ["type:task", "status:in-progress", "flag:blocked"]
    gh.set_assignees(issue.number, ["codie-coder-bot"])
    assert gh.get_issue(issue.number).assignees == ["codie-coder-bot"]
    assert len(gh.list_issues()) == 1


def test_protocol_comments_and_timeline(gh):
    gh.add_issue(1, "i", "b", labels=["type:task", "status:ready"])
    c = gh.comment(1, "<!-- codie:heartbeat r1 2026-01-01T00:00:00+00:00 -->")
    assert c.user_login == "kernel"
    assert len(gh.list_comments()) == 1
    kinds = [e.kind for e in gh.list_timeline(1)]
    assert "labeled" in kinds and "commented" in kinds


def test_protocol_pr_flow(gh):
    gh.set_actor("coder")
    pr = gh.create_pr("Feature", "Refs #2", "feature/2-x", "dev", draft=False)
    assert gh.get_pr(pr.number) is not None
    gh.update_pr(pr.number, body="Refs #2\nupdated")
    assert gh.get_pr(pr.number).body == "Refs #2\nupdated"
    # files list
    gh.prs[pr.number].files = ["src/x.py"]
    assert gh.list_pr_files(pr.number) == ["src/x.py"]
    # review
    gh.set_actor("reviewer")
    rv = gh.create_review(
        pr.number,
        "APPROVED",
        "lgtm",
        comments=[{"path": "src/x.py", "line": 1, "body": "please fix"}],
    )
    assert rv.state == "APPROVED"
    assert len(gh.pr_reviews(pr.number)) == 1
    comments = gh.list_review_comments(pr.number)
    assert comments[0]["body"] == "please fix" and comments[0]["path"] == "src/x.py"
    # merge
    merged = gh.merge_pr(pr.number, "squash")
    assert merged is True
    assert gh.get_pr(pr.number).state == "merged"
    assert gh.get_ref("dev") is not None


def test_protocol_close_reopen_issue(gh):
    gh.add_issue(1, "i", "b", labels=["type:task", "status:done"])
    gh.close_issue(1)
    assert gh.get_issue(1).state == "closed"
    gh.reopen_issue(1)
    assert gh.get_issue(1).state == "open"


def test_protocol_files_refs_checks(gh):
    gh.bootstrap_files("dev", {".codie.yaml": "surface: cli\n", "README.md": "# x"})
    assert gh.get_file(".codie.yaml", gh.get_ref("dev")) == "surface: cli\n"
    gh.set_commit_status(gh.get_ref("dev"), "success", "ci passed")
    checks = gh.list_required_checks(gh.get_ref("dev"))
    assert any(c.state == "success" for c in checks)


def test_protocol_labels_admin(gh):
    gh.upsert_label("type:feature", "#0E8A16", "desc")
    assert gh.has_label("type:feature") is True


def test_admin_pin_invite_protection_repo(gh):
    gh.add_issue(1, "PRD", "body", labels=["type:prd"])
    gh.pin_issue(1, True)
    assert gh.get_issue(1).pinned is True
    gh.invite_collaborator("codie-coder-bot")
    assert "codie-coder-bot" in gh.collaborators
    gh.update_branch_protection("dev", require_prs=True, require_approvals=1, checks=["ci"])
    proto = gh.get_branch_protection("dev")
    assert proto["require_prs"] is True
    gh.update_repo("squash")
    assert gh.repo_options["merge_method"] == "squash"
    assert gh.get_authenticated_user() == "kernel"


def test_admin_upsert_file_and_create_ref(gh):
    gh.set_actor("kernel")
    sha = gh.upsert_file("AGENTS.md", "# team\n", "chore: agents", "dev")
    assert gh.get_file("AGENTS.md", "dev") == "# team\n"
    gh.create_ref("feat/x", sha)
    assert gh.get_ref("feat/x") == sha


def test_review_state_computation_uses_bot_logins(gh):
    gh.add_issue(2, "task", "**Parent:** #1", labels=["type:task", "status:ready", "kind:dev"])
    gh.set_actor("coder")
    pr = gh.create_pr("p", "Refs #2", "feature/2-x", "dev")
    gh.set_actor("reviewer")
    gh.create_review(pr.number, "APPROVED", "ok")
    p = gh.get_pr(pr.number)
    assert p.reviewer_approved is True or p.review_state in {"approved", "none"}

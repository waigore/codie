"""Contract suite (Technical Spec §13, M19/I-30).

The *same* behavior suite runs against both `FakeGitHub` and `PyGithubClient`
(the production adapter is gated behind sandbox-repo tokens, like the e2e
layer). A method-coverage check asserts every `GitHubClient` protocol method is
exercised by the suite.
"""

from __future__ import annotations

import datetime as _dt
import inspect
import os

import pytest

from codie.github.client import GitHubClient
from codie.github.fake import FakeGitHub

BOT_LOGINS = {
    "codie-planner-bot",
    "codie-coder-bot",
    "codie-reviewer-bot",
    "codie-tester-bot",
    "codie-kernel-bot",
}
ROLE_LOGINS = {
    "planner": "codie-planner-bot",
    "coder": "codie-coder-bot",
    "reviewer": "codie-reviewer-bot",
    "tester": "codie-tester-bot",
    "kernel": "codie-kernel-bot",
}


def _clock():
    return _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)


class FakeProvider:
    name = "fake"

    def fresh(self):
        gh = FakeGitHub("acme/test", clock=_clock)
        gh.bot_logins = set(BOT_LOGINS)
        gh.map_roles(dict(ROLE_LOGINS))
        gh.bootstrap_branches("main", "dev")
        return gh


class PyGithubProvider:
    name = "pygithub"

    def fresh(self):
        try:
            from codie.github.pygithub_client import PyGithubClient
        except Exception:  # pragma: no cover
            pytest.skip("pygithub unavailable")
        repo = os.environ.get("CODIE_E2E_REPO", "")
        token = os.environ.get("CODIE_GH_TOKEN_KERNEL", "")
        if not repo or not token:
            pytest.skip("sandbox tokens not set — real-client contract is gated (I-30)")
        return PyGithubClient(repo, {"kernel": token}, bot_logins=BOT_LOGINS)


PROVIDERS = [FakeProvider()] + ([PyGithubProvider()] if os.environ.get("CODIE_E2E") == "1" else [])


def _exercised() -> set[str]:
    """Methods the behavior suite actually called on the fake (coverage gate)."""
    calls: list[str] = []

    original = FakeGitHub.__getattribute__
    seen: set[str] = set()

    def spy(self, name):
        try:
            return original(self, name)
        finally:
            if not name.startswith("_") and name not in seen:
                seen.add(name)
                calls.append(name)

    FakeGitHub.__getattribute__ = spy
    try:
        run_behavior_suite(FakeProvider().fresh())
    finally:
        FakeGitHub.__getattribute__ = original
    return set(calls)


def run_behavior_suite(client):
    """The shared behavior suite both clients must satisfy (I-30)."""
    # issues: create / edit / labels / assignees
    issue = client.create_issue("Title", "Body", ["type:task", "status:ready"])
    assert client.get_issue(issue.number) is not None
    client.edit_issue(issue.number, body="New body")
    assert client.get_issue(issue.number).body == "New body"
    client.set_labels(issue.number, ["type:task", "status:in-progress", "flag:blocked"])
    client.set_assignees(issue.number, ["codie-coder-bot"])
    assert len(client.list_issues()) >= 1
    # comments + timeline
    c = client.comment(issue.number, "<!-- codie:heartbeat r1 2026-01-01T00:00:00+00:00 -->")
    assert c.issue_number == issue.number
    assert client.list_comments()
    assert client.list_timeline(issue.number)
    # PR flow + merge gate
    client.set_actor("coder")
    pr = client.create_pr("Feature", "Refs #2", "feature/2-x", "dev", draft=False)
    client.update_pr(pr.number, body="Refs #2\nupdated")
    assert client.get_pr(pr.number) is not None
    assert client.list_prs()
    # a no-approval PR cannot merge (§7.4 gate)
    assert client.merge_pr(pr.number, "squash") is False
    if hasattr(client, "prs"):  # fake-only fixture injection
        client.prs[pr.number].files = ["src/x.py"]
    assert client.list_pr_files(pr.number) == ["src/x.py"]
    assert client.list_review_comments(pr.number) == []
    # approve on head via create_review → merge
    client.set_actor("reviewer")
    rv = client.create_review(pr.number, "APPROVED", "lgtm", [{"path": "src/x.py", "line": 1, "body": "fix"}])
    assert rv.state == "APPROVED"
    assert client.pr_reviews(pr.number)
    assert client.merge_pr(pr.number, "squash") is True
    client.close_pr(pr.number)
    client.set_actor("kernel")
    # issue lifecycle
    client.close_issue(issue.number)
    client.reopen_issue(issue.number)
    # files / refs / required checks / commit status
    client.bootstrap_files("dev", {".codie.yaml": "surface: cli\n"})
    assert client.get_file(".codie.yaml", client.get_ref("dev")) is not None
    client.set_commit_status(client.get_ref("dev"), "success")
    client.list_required_checks(client.get_ref("dev"))
    client.upsert_label("type:feature", "#0E8A16", "desc")
    assert client.has_label("type:feature") is True
    assert client.rate_limit()["remaining"] >= 0
    # admin protocol
    client.pin_issue(issue.number, True)
    client.create_ref("feat/x", client.get_ref("dev"))
    client.upsert_file("AGENTS.md", "# team\n", "chore", "dev")
    client.get_branch_protection("dev")
    client.update_branch_protection("dev", require_prs=True, require_approvals=1, checks=["ci"])
    client.update_repo("squash", delete_branch_on_merge=True)
    client.invite_collaborator("codie-coder-bot", "push")
    client.list_collaborators()
    client.get_authenticated_user()
    client.list_files("dev")
    client.get_default_branch()


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
def test_behavior_suite(provider):
    run_behavior_suite(provider.fresh())


def test_protocol_methods_all_exercised():
    """The behavior suite calls every GitHubClient protocol method (I-30)."""
    methods = {
        name
        for name, member in inspect.getmembers(GitHubClient)
        if (inspect.isfunction(member) or callable(member)) and not name.startswith("_")
    }
    safe = {"set_actor"}  # exercised everywhere implicitly
    covered = _exercised() | safe
    missing = methods - covered
    # `get_issue` is covered via the assertion path; list_* via creation paths.
    assert not missing, f"behavior suite never exercises protocol methods: {sorted(missing)}"


def test_fake_review_state_uses_bot_logins():
    """I-30: on_head / reviewer-approval semantics match derive's expectations."""
    gh = FakeProvider().fresh()
    gh.add_issue(2, "task", "**Parent:** #1", labels=["type:task", "status:ready", "kind:dev"])
    pr = gh.add_pr(5, "PR", "Refs #2", "feature/2-x", "dev", files=["src/x.py"])
    gh.set_actor("coder")
    gh.create_pr  # noqa
    gh.set_actor("reviewer")
    review = gh.create_review(pr.number, "APPROVED", "ok")
    assert review.commit_id == gh.branches.get(pr.head)
    assert review.on_head is True
    assert review.user_login == "codie-reviewer-bot"
    p = gh.get_pr(pr.number)
    assert p.reviewer_approved is True

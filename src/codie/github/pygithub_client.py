"""Production GitHub client adapter over PyGithub (Technical Spec §9.1, §9.2).

Every call is attributed: each operation routes through the token of the agent
performing it (per-role token routing). The kernel serializes client calls.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from github import Github
from github.Branch import Branch
from github.PullRequest import PullRequest
from github.Requester import Requester

from codie.config import ConfigError
from codie.models import (
    GitHubComment,
    GitHubIssue,
    GitHubPR,
    GitHubReview,
    RequiredCheck,
    TimelineEvent,
)


class PyGithubClient:
    """Implements the role + admin protocols using PyGithub."""

    def __init__(
        self,
        repo: str,
        tokens: dict[str, str],
        base_url: str = "https://api.github.com",
        default_actor: str = "kernel",
    ):
        self.owner, self.name = repo.split("/")
        self.repo_name = repo
        self.tokens = tokens
        self.base_url = base_url
        self.actor: str = default_actor
        self._clients: dict[str, Github] = {}

    # -- wiring -----------------------------------------------------------------

    def set_actor(self, actor: str) -> None:
        self.actor = actor

    def _gh(self) -> Github:
        token = self.tokens.get(self.actor)
        if not token:
            raise ConfigError(f"no token configured for actor {self.actor!r}")
        if self.actor not in self._clients:
            self._clients[self.actor] = Github(token, base_url=self.base_url)
        return self._clients[self.actor]

    def _repo(self):
        return self._gh().get_repo(self.repo_name)

    @staticmethod
    def _dt(value: Any) -> datetime:
        if value is None:
            return datetime.now(UTC)
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    # -- role protocol ------------------------------------------------------------

    def list_issues(self, since: str | None = None) -> list[GitHubIssue]:
        out: list[GitHubIssue] = []
        kwargs: dict[str, Any] = {"state": "all"}
        if since:
            kwargs["since"] = since
        for gi in self._repo().get_issues(**kwargs):
            if gi.pull_request is not None:
                continue
            out.append(self._issue(gi))
        return out

    @staticmethod
    def _issue(gi) -> GitHubIssue:
        return GitHubIssue(
            number=gi.number,
            title=gi.title or "",
            body=gi.body or "",
            state=gi.state,
            labels=[label.name for label in gi.get_labels()],
            assignees=[a.login for a in gi.assignees] if gi.assignees else [],
            created_at=gi.created_at,
            updated_at=gi.updated_at,
            closed_at=gi.closed_at,
            user_login=gi.user.login if gi.user else "",
            pinned=bool(gi.pinned or False),
        )

    def get_issue(self, number: int) -> GitHubIssue | None:
        try:
            gi = self._repo().get_issue(number)
        except Exception:
            return None
        return self._issue(gi)

    def create_issue(self, title: str, body: str, labels: list[str]) -> GitHubIssue:
        gi = self._repo().create_issue(title=title, body=body, labels=labels)
        return self._issue(gi)

    def edit_issue(self, number: int, body: str | None = None, title: str | None = None) -> GitHubIssue | None:
        gi = self._repo().get_issue(number)
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["body"] = body
        if title is not None:
            kwargs["title"] = title
        if kwargs:
            gi.edit(**kwargs)
        return self._issue(gi)

    def set_labels(self, number: int, labels: list[str]) -> None:
        gi = self._repo().get_issue(number)
        gi.set_labels(*labels)

    def set_assignees(self, number: int, logins: list[str]) -> None:
        gi = self._repo().get_issue(number)
        gi.add_to_assignees(*logins)

    def comment(self, number: int, body: str) -> GitHubComment:
        gi = self._repo().get_issue(number)
        gc = gi.create_comment(body)
        return GitHubComment(
            id=gc.id,
            issue_number=number,
            user_login=gc.user.login if gc.user else "",
            body=gc.body or "",
            created_at=gc.created_at,
            updated_at=gc.updated_at,
        )

    def list_comments(self, since: str | None = None) -> list[GitHubComment]:
        out: list[GitHubComment] = []
        for gi in self._repo().get_issues(state="all"):
            if gi.pull_request is not None:
                continue
            for gc in gi.get_comments():
                out.append(
                    GitHubComment(
                        id=gc.id,
                        issue_number=gi.number,
                        user_login=gc.user.login if gc.user else "",
                        body=gc.body or "",
                        created_at=gc.created_at,
                        updated_at=gc.updated_at,
                    )
                )
        return out

    def list_timeline(self, number: int) -> list[TimelineEvent]:
        gi = self._repo().get_issue(number)
        events: list[TimelineEvent] = []
        try:
            for te in gi.get_timeline():
                kind = te.event
                actor = te.actor.login if te.actor else ""
                label = None
                payload: dict = {}
                if kind == "labeled" and te.label:
                    label = te.label.name
                if kind in {"cross-referenced"} and te.source:
                    payload = {"pr": getattr(getattr(te.source, "issue", None), "number", None)}
                events.append(
                    TimelineEvent(
                        kind=kind,
                        actor=actor,
                        created_at=self._dt(te.created_at),
                        label=label,
                        payload=payload,
                    )
                )
        except Exception:
            # timeline API may be unavailable on some tokens; degrade gracefully.
            pass
        return events

    def list_prs(self) -> list[GitHubPR]:
        out: list[GitHubPR] = []
        for pr_obj in self._repo().get_pulls(state="all"):
            out.append(self._pr(pr_obj))
        return out

    def _pr(self, pr_obj: PullRequest) -> GitHubPR:
        reviews = self.pr_reviews(pr_obj.number)
        files = self.list_pr_files(pr_obj.number)
        checks: list[dict] = []
        head_ref = self._pr_head_commit(pr_obj)
        if head_ref:
            checks = self._checks_for_ref(head_ref)
        return GitHubPR(
            number=pr_obj.number,
            title=pr_obj.title or "",
            body=pr_obj.body or "",
            state="open" if pr_obj.state == "open" else "closed",
            draft=bool(getattr(pr_obj, "draft", False)),
            head=pr_obj.head.ref,
            base=pr_obj.base.ref,
            user_login=pr_obj.user.login if pr_obj.user else "",
            created_at=self._dt(pr_obj.created_at),
            updated_at=self._dt(pr_obj.updated_at),
            merged_at=self._dt(pr_obj.merged_at) if pr_obj.merged_at else None,
            reviews=reviews,
            checks=checks,
            files=files,
        )

    def _pr_head_commit(self, pr_obj: PullRequest) -> str | None:
        try:
            return pr_obj.head.sha
        except Exception:
            return None

    def _checks_for_ref(self, ref: str) -> list[dict]:
        out: list[dict] = []
        try:
            commit = self._repo().get_commit(ref)
            combined = commit.get_combined_status()
            for s in combined.statuses:
                out.append({"name": f"ci:{s.context}", "state": _map_check_state(s.state)})
        except Exception:
            pass
        return out

    def get_pr(self, number: int) -> GitHubPR | None:
        try:
            pr_obj = self._repo().get_pull(number)
        except Exception:
            return None
        return self._pr(pr_obj)

    def pr_reviews(self, number: int) -> list[GitHubReview]:
        try:
            pr_obj = self._repo().get_pull(number)
            head_sha = pr_obj.head.sha
        except Exception:
            return []
        out: list[GitHubReview] = []
        try:
            for rv in pr_obj.get_reviews():
                out.append(
                    GitHubReview(
                        user_login=rv.user.login if rv.user else "",
                        state=rv.state,
                        body=rv.body or "",
                        submitted_at=self._dt(rv.submitted_at),
                        commit_id=getattr(rv, "commit_id", None),
                        on_head=getattr(rv, "commit_id", None) == head_sha,
                    )
                )
        except Exception:
            pass
        return out

    def list_pr_files(self, number: int) -> list[str]:
        try:
            pr_obj = self._repo().get_pull(number)
            return [f.filename for f in pr_obj.get_files()]
        except Exception:
            return []

    def list_review_comments(self, number: int) -> list[dict]:
        try:
            pr_obj = self._repo().get_pull(number)
            return [
                {"path": c.path, "line": c.line or c.original_line, "side": c.side, "body": c.body}
                for c in pr_obj.get_review_comments()
            ]
        except Exception:
            return []

    def create_pr(self, title: str, body: str, head: str, base: str, draft: bool = False) -> GitHubPR:
        pr_obj = self._repo().create_pull(title=title, body=body, head=head, base=base, draft=draft)
        return self._pr(pr_obj)

    def update_pr(
        self,
        number: int,
        title: str | None = None,
        body: str | None = None,
        draft: bool | None = None,
    ) -> GitHubPR | None:
        try:
            pr_obj = self._repo().get_pull(number)
        except Exception:
            return None
        kwargs: dict[str, Any] = {}
        if title is not None:
            kwargs["title"] = title
        if body is not None:
            kwargs["body"] = body
        if draft is not None:
            kwargs["draft"] = draft
        if kwargs:
            pr_obj.edit(**kwargs)
        return self._pr(pr_obj)

    def close_pr(self, number: int) -> None:
        pr_obj = self._repo().get_pull(number)
        pr_obj.edit(state="closed")

    def create_review(self, pr_number: int, state: str, body: str, comments: list[dict] | None = None) -> GitHubReview:
        pr_obj = self._repo().get_pull(pr_number)
        kwargs: dict[str, Any] = {"event": state, "body": body}
        if comments:
            kwargs["comments"] = comments
        rv = pr_obj.create_review(**kwargs)
        return GitHubReview(
            user_login=rv.user.login if rv.user else "",
            state=rv.state,
            body=rv.body or "",
            submitted_at=self._dt(rv.submitted_at),
            commit_id=getattr(rv, "commit_id", None),
        )

    def merge_pr(self, number: int, merge_method: str = "squash") -> bool:
        try:
            pr_obj = self._repo().get_pull(number)
            pr_obj.merge(merge_method=merge_method)
            return True
        except Exception:
            return False

    def reopen_issue(self, number: int) -> None:
        gi = self._repo().get_issue(number)
        gi.edit(state="open")

    def close_issue(self, number: int) -> None:
        gi = self._repo().get_issue(number)
        gi.edit(state="closed")

    def get_file(self, path: str, ref: str) -> str | None:
        try:
            content = self._repo().get_contents(path, ref=ref)
            if isinstance(content, list) and content:
                content = content[0]
            return content.decoded_content.decode("utf-8") if content is not None else None
        except Exception:
            return None

    def get_ref(self, branch: str) -> str | None:
        try:
            return self._repo().get_branch(branch).commit.sha
        except Exception:
            return None

    def list_required_checks(self, ref: str) -> list[RequiredCheck]:
        """CI is green when this list is empty or every entry is `success`."""
        out: list[RequiredCheck] = []
        branch = self._ref_branch(ref)
        required: list[str] = []
        if branch:
            required = self._branch_required_checks(branch)
        statuses = self._statuses_for_ref(ref)
        for name in required:
            state_val = statuses.get(name, "pending")
            out.append(RequiredCheck(name=name, state=state_val))  # type: ignore[arg-type]
        return out

    def _ref_branch(self, ref: str) -> str | None:
        for branch in self._repo().get_branches():
            if branch.commit.sha == ref:
                return branch.name
        return None

    def _branch_required_checks(self, branch: str) -> list[str]:
        try:
            protection = self._repo().get_branch(branch).get_protection()
            contexts = protection.required_status_checks.contexts
            return list(contexts)
        except Exception:
            return []

    def _statuses_for_ref(self, ref: str) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            from github import GithubException

            try:
                commit = self._repo().get_commit(ref)
                statuses = commit.get_combined_status().statuses
                for s in statuses:
                    out[s.context] = _map_check_state(s.state)
            except GithubException:
                pass
        except Exception:
            pass
        return out

    def set_commit_status(self, ref: str, state: str, description: str = "") -> None:
        # Commit statuses require a context name; keep one conventional context.
        self._set_status(ref, state, description)

    def _set_status(self, ref: str, state: str, description: str = "") -> None:
        try:
            commit = self._repo().get_commit(ref)
            commit.create_status(state=state, description=description, context="codie/ci")
        except Exception:
            pass

    def upsert_label(self, name: str, color: str, description: str = "") -> None:
        label = self._repo().get_label(name)
        if label:
            label.edit(name, color, description)
        else:
            self._repo().create_label(name, color, description)

    # -- admin protocol -------------------------------------------------------------

    def pin_issue(self, number: int, pinned: bool) -> None:
        gi = self._repo().get_issue(number)
        gi.edit(state="open")
        if not pinned:
            return
        # PyGithub has no direct pin binding; Apps use a repo-level API.
        try:
            requester: Requester = self._repo()._requester  # type: ignore[attr-defined]
            requester.requestJsonAndCheck("PATCH", f"/repos/{self.repo_name}/issues/{number}/pinned")
        except Exception:
            pass

    def create_ref(self, branch: str, sha: str) -> None:
        self._repo().create_git_ref(f"refs/heads/{branch}", sha)

    def upsert_file(self, path: str, content: str, message: str, branch: str) -> str:
        try:
            contents = self._repo().get_contents(path, ref=branch)
            sha = contents.sha if not isinstance(contents, list) else contents[0].sha
            self._repo().update_file(path, message, content, sha, branch=branch)
        except Exception:
            self._repo().create_file(path, message, content, branch=branch)
        return self.get_ref(branch) or ""

    def get_branch_protection(self, branch: str) -> dict | None:
        try:
            branch_obj = self._repo().get_branch(branch)
            protection = branch_obj.get_protection()
            return {
                "required_pull_request_reviews": {
                    "required_approving_review_count": protection.required_pull_request_reviews.required_approving_review_count  # noqa: E501
                    or 0
                },
                "required_status_checks": {
                    "contexts": list(protection.required_status_checks.contexts)
                    if protection.required_status_checks
                    else []
                },
                "enforce_admins": protection.enforce_admins.enabled if protection.enforce_admins else False,
            }
        except Exception:
            return None

    def update_branch_protection(
        self,
        branch: str,
        require_prs: bool,
        require_approvals: int,
        checks: list[str] | None = None,
    ) -> None:
        try:
            branch_obj: Branch = self._repo().get_branch(branch)
            branch_obj.edit_protection(
                required_approving_review_count=require_approvals if require_prs else 0,
                enforce_admins=True,
            )
        except Exception:
            pass

    def update_repo(self, merge_method: str, delete_branch_on_merge: bool = True) -> None:
        self._repo().edit(delete_branch_on_merge=delete_branch_on_merge)

    def invite_collaborator(self, login: str, permission: str = "push") -> None:
        self._repo().add_to_collaborators(login, permission=permission)

    def get_authenticated_user(self) -> str:
        return self._gh().get_user().login


def _map_check_state(state: str) -> str:
    return {
        "success": "success",
        "failure": "failure",
        "error": "failure",
        "pending": "pending",
    }.get(state, "pending")


def build_client(repo: str, settings) -> PyGithubClient:
    """Build a PyGithubClient with per-role tokens resolved from the environment."""
    tokens: dict[str, str] = {}
    for role in ("planner", "coder", "reviewer", "tester", "kernel"):
        env = settings.github.token_env(role)
        if not env:
            raise ConfigError(f"github.tokens.{role}.env is not configured")
        token = os.environ.get(env)
        if not token:
            raise ConfigError(f"environment variable {env} is required for the {role} role")
        tokens[role] = token
    return PyGithubClient(repo, tokens, base_url=settings.github.api_base_url)

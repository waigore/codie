"""Dry-run client wrapper (Technical Spec §6.1 --dry-run, §9.1).

Wraps a live `GitHubClient`; every read is delegated to the real client, every
mutation is logged (never applied). The kernel keeps its normal control flow —
it builds packs, runs crews, and "applies" results — but the write boundary
silently no-ops and records intent, so a full `--dry-run` cycle exercises the
whole pipeline while recording zero mutations on GitHub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from codie.models import (
    GitHubComment,
    GitHubIssue,
    GitHubPR,
    GitHubReview,
    RequiredCheck,
    TimelineEvent,
)


class DryRunClient:
    """Adheres to `GitHubClient`; mutations are logged, not applied."""

    def __init__(self, real: Any):
        self.real = real
        self.actor: str = getattr(real, "actor", "kernel")
        self.mutations: list[dict[str, Any]] = []
        self._sync = 0

    # -- wiring -----------------------------------------------------------------

    def set_actor(self, actor: str) -> None:
        self.actor = actor

    # -- reads (delegated) ------------------------------------------------------

    def list_issues(self, since: str | None = None) -> list[GitHubIssue]:
        return self.real.list_issues(since)

    def get_issue(self, number: int) -> GitHubIssue | None:
        return self.real.get_issue(number)

    def edit_issue(self, number: int, body: str | None = None, title: str | None = None) -> GitHubIssue | None:
        self._log("edit_issue", {"number": number, "body": body is not None, "title": title is not None})
        return self.get_issue(number)

    def set_labels(self, number: int, labels: list[str]) -> None:
        self._log("set_labels", {"number": number, "labels": labels})

    def set_assignees(self, number: int, logins: list[str]) -> None:
        self._log("set_assignees", {"number": number, "logins": logins})

    def comment(self, number: int, body: str) -> GitHubComment:
        self._log("comment", {"number": number, "body": body[:200]})
        return GitHubComment(
            id=0,
            issue_number=number,
            user_login=self.actor,
            body=body,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def list_comments(self, since: str | None = None) -> list[GitHubComment]:
        return self.real.list_comments(since)

    def list_timeline(self, number: int) -> list[TimelineEvent]:
        return self.real.list_timeline(number)

    def list_prs(self) -> list[GitHubPR]:
        return self.real.list_prs()

    def get_pr(self, number: int) -> GitHubPR | None:
        return self.real.get_pr(number)

    def pr_reviews(self, number: int) -> list[GitHubReview]:
        return self.real.pr_reviews(number)

    def list_pr_files(self, number: int) -> list[str]:
        return self.real.list_pr_files(number)

    def list_review_comments(self, number: int) -> list[dict]:
        return self.real.list_review_comments(number)

    # -- mutations (logged, no-ops with plausible results) -----------------------

    def create_issue(self, title: str, body: str, labels: list[str]) -> GitHubIssue:
        self._sync -= 1
        self._log("create_issue", {"title": title, "body": body[:200], "labels": labels})
        return GitHubIssue(
            number=self._sync,
            title=title,
            body=body,
            state="open",
            labels=list(labels),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            user_login=self.actor,
        )

    def create_pr(self, title: str, body: str, head: str, base: str, draft: bool = False) -> GitHubPR:
        self._sync -= 1
        self._log("create_pr", {"title": title, "head": head, "base": base, "draft": draft})
        return GitHubPR(
            number=self._sync,
            title=title,
            body=body,
            state="open",
            draft=draft,
            head=head,
            base=base,
            user_login=self.actor,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def update_pr(self, number: int, title: str | None = None, body: str | None = None, draft: bool | None = None):
        self._log("update_pr", {"number": number, "draft": draft})
        return None

    def close_pr(self, number: int) -> None:
        self._log("close_pr", {"number": number})

    def create_review(self, pr_number: int, state: str, body: str, comments: list[dict] | None = None):
        self._log("create_review", {"pr": pr_number, "state": state})
        return GitHubReview(
            user_login=self.actor,
            state=state,
            body=body,
            submitted_at=datetime.now(UTC),
        )

    def merge_pr(self, number: int, merge_method: str = "squash") -> bool:
        self._log("merge_pr", {"number": number, "method": merge_method})
        return True

    def reopen_issue(self, number: int) -> None:
        self._log("reopen_issue", {"number": number})

    def close_issue(self, number: int) -> None:
        self._log("close_issue", {"number": number})

    # -- admin ---------------------------------------------------------------

    def pin_issue(self, number: int, pinned: bool) -> None:
        self._log("pin_issue", {"number": number, "pinned": pinned})

    def create_ref(self, branch: str, sha: str) -> None:
        self._log("create_ref", {"branch": branch, "sha": sha[:8]})

    def upsert_file(self, path: str, content: str, message: str, branch: str) -> str:
        self._log("upsert_file", {"path": path, "branch": branch, "message": message})
        return ""

    def get_branch_protection(self, branch: str):
        return self.real.get_branch_protection(branch)

    def update_branch_protection(self, branch: str, require_prs: bool, require_approvals: int, checks=None) -> None:
        self._log("update_branch_protection", {"branch": branch, "require_prs": require_prs})

    def update_repo(self, merge_method: str, delete_branch_on_merge: bool = True) -> None:
        self._log("update_repo", {"merge_method": merge_method})

    def invite_collaborator(self, login: str, permission: str = "push") -> None:
        self._log("invite_collaborator", {"login": login, "permission": permission})

    def get_authenticated_user(self) -> str:
        return self.real.get_authenticated_user()

    def get_file(self, path: str, ref: str):
        return self.real.get_file(path, ref)

    def get_ref(self, branch: str):
        return self.real.get_ref(branch)

    def list_required_checks(self, ref: str) -> list[RequiredCheck]:
        return self.real.list_required_checks(ref)

    def set_commit_status(self, ref: str, state: str, description: str = "") -> None:
        self._log("set_commit_status", {"ref": ref[:8], "state": state})

    def upsert_label(self, name: str, color: str, description: str = "") -> None:
        self._log("upsert_label", {"name": name})

    def has_label(self, name: str) -> bool:
        has = getattr(self.real, "has_label", None)
        return bool(has(name)) if has else False

    def rate_limit(self) -> dict:
        rate = getattr(self.real, "rate_limit", None)
        return dict(rate()) if rate else {"remaining": 0, "reset": None}

    def _log(self, op: str, payload: dict[str, Any]) -> None:
        self.mutations.append({"op": op, "actor": self.actor, "payload": payload})

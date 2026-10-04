"""In-memory fake GitHub client (Technical Spec §9.1).

Used by tests and by `--dry-run` (fake for writes, real client for reads).
Faithful enough that contract tests run the same behavior suite against it and
against PyGithubClient.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from codie.models import (
    GitHubComment,
    GitHubIssue,
    GitHubPR,
    GitHubReview,
    RequiredCheck,
    TimelineEvent,
)


def _iso_now() -> datetime:
    return datetime.now(UTC)


def _sha(seed: int) -> str:
    return f"{seed:040x}"


class FakeGitHub:
    """In-memory implementation of both the role and admin protocols."""

    max_int: int = 2**31

    def __init__(self, repo: str = "acme/test", clock: Callable[[], datetime] | None = None):
        self.repo = repo
        self.clock = clock or _iso_now
        self.issues: dict[int, GitHubIssue] = {}
        self.comments: dict[int, list[GitHubComment]] = {}
        self.timelines: dict[int, list[TimelineEvent]] = {}
        self.prs: dict[int, GitHubPR] = {}
        self.review_comments: dict[int, list[dict]] = {}
        self.labels: dict[str, tuple[str, str]] = {}
        self.branches: dict[str, str] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.collaborators: set[str] = set()
        self.repo_options: dict[str, object] = {}
        self.branch_protection: dict[str, dict] = {}
        self.commit_statuses: dict[str, dict[str, str]] = {}
        self.required_check_names: set[str] = set()
        self._issue_seq = 0
        self._pr_seq = 0
        self._comment_seq = 0
        self._commit_seq = 0
        self.actor: str = "kernel"
        self.next_issue_number: int | None = None  # set by tests for determinism
        self.bot_logins: set[str] = set()

    # -- wiring helpers ---------------------------------------------------------

    def set_actor(self, actor: str) -> None:
        self.actor = actor

    def _bump_commit(self, branch: str) -> str:
        self._commit_seq += 1
        sha = _sha(self._commit_seq)
        self.branches[branch] = sha
        return sha

    def bootstrap_branches(self, *branches: str) -> None:
        for branch in branches:
            self._commit_seq += 1
            self.branches[branch] = _sha(self._commit_seq)
            self.trees[branch] = {}

    def bootstrap_files(self, branch: str, files: dict[str, str]) -> None:
        self.trees.setdefault(branch, {}).update(files)
        self._bump_commit(branch)

    def add_issue(
        self,
        number: int,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        state: str = "open",
        assignees: list[str] | None = None,
    ) -> GitHubIssue:
        issue = GitHubIssue(
            number=number,
            title=title,
            body=body,
            state=state,
            labels=list(labels or []),
            assignees=list(assignees or []),
            created_at=self.clock(),
            updated_at=self.clock(),
            user_login=self.actor,
        )
        self.issues[number] = issue
        for label in issue.labels:
            self._timeline(number, "labeled", actor=self.actor, label=label)
        return issue

    def add_pr(
        self,
        number: int,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
        state: str = "open",
        files: list[str] | None = None,
    ) -> GitHubPR:
        pr = GitHubPR(
            number=number,
            title=title,
            body=body,
            state=state,
            draft=draft,
            head=head,
            base=base,
            user_login=self.actor,
            created_at=self.clock(),
            updated_at=self.clock(),
            files=list(files or []),
        )
        self.prs[number] = pr
        self.trees.setdefault(head, {})
        self.branches.setdefault(head, _sha(0))
        return pr

    def add_comment_to_issue(self, number: int, body: str, user_login: str = "human") -> GitHubComment:
        c = GitHubComment(
            id=self._new_comment_id(),
            issue_number=number,
            user_login=user_login,
            body=body,
            created_at=self.clock(),
            updated_at=self.clock(),
        )
        self.comments.setdefault(number, []).append(c)
        self._timeline(number, "commented", actor=user_login, payload={"body": body})
        return c

    def _new_comment_id(self) -> int:
        self._comment_seq += 1
        return self._comment_seq

    def add_review(
        self, pr_number: int, state: str, user_login: str, body: str = "", on_head: bool = True
    ) -> GitHubReview:
        pr = self.prs[pr_number]
        review = GitHubReview(
            user_login=user_login,
            state=state,
            body=body,
            submitted_at=self.clock(),
            commit_id=self.branches.get(pr.head),
            on_head=on_head,
        )
        pr.reviews.append(review)
        pr.updated_at = self.clock()
        self._recompute_review_state(pr_number)
        self._timeline(
            getattr(pr, "linked_issue", None) or 0,
            "reviewed",
            actor=user_login,
            label=None,
            payload={"pr": pr_number, "state": state},
        )
        return review

    def _recompute_review_state(self, pr_number: int) -> None:
        pr = self.prs[pr_number]
        on_head = [r for r in pr.reviews if (r.on_head or r.commit_id == self.branches.get(pr.head))]
        if any(r.state == "CHANGES_REQUESTED" for r in on_head):
            pr.review_state = "changes_requested"
            pr.changes_requested = True
            pr.reviewer_approved = False
        elif any(r.state == "APPROVED" and r.user_login in self.bot_logins for r in on_head):
            pr.review_state = "approved"
            pr.reviewer_approved = True
        elif on_head:
            latest = max(on_head, key=lambda r: r.submitted_at)
            mapping = {
                "APPROVED": "approved",
                "CHANGES_REQUESTED": "changes_requested",
                "COMMENTED": "commented",
                "DISMISSED": "dismissed",
            }
            pr.review_state = mapping.get(latest.state, "none")  # type: ignore[assignment]
            pr.reviewer_approved = any(r.state == "APPROVED" and r.user_login in self.bot_logins for r in on_head)
        else:
            pr.review_state = "none"

    def _timeline(
        self,
        number: int,
        kind: str,
        actor: str | None = None,
        label: str | None = None,
        payload: dict | None = None,
    ) -> None:
        self.timelines.setdefault(number, []).append(
            TimelineEvent(
                kind=kind,
                actor=actor or self.actor,
                created_at=self.clock(),
                label=label,
                payload=payload or {},
            )
        )

    # -- role protocol -----------------------------------------------------------

    def list_issues(self, since: str | None = None) -> list[GitHubIssue]:
        return sorted(self.issues.values(), key=lambda i: i.number)

    def get_issue(self, number: int) -> GitHubIssue | None:
        return self.issues.get(number)

    def create_issue(self, title: str, body: str, labels: list[str]) -> GitHubIssue:
        if self.next_issue_number is not None:
            number = self.next_issue_number
            self.next_issue_number = None
        else:
            self._issue_seq = max(self._issue_seq, max(self.issues, default=0)) + 1
            number = self._issue_seq
        issue = GitHubIssue(
            number=number,
            title=title,
            body=body,
            state="open",
            labels=list(labels),
            created_at=self.clock(),
            updated_at=self.clock(),
            user_login=self.actor,
        )
        self.issues[number] = issue
        for label in issue.labels:
            self._timeline(number, "labeled", label=label)
        return issue

    def edit_issue(self, number: int, body: str | None = None, title: str | None = None) -> GitHubIssue | None:
        issue = self.issues.get(number)
        if issue is None:
            return None
        if body is not None:
            issue.body = body
        if title is not None:
            issue.title = title
        issue.updated_at = self.clock()
        return issue

    def set_labels(self, number: int, labels: list[str]) -> None:
        issue = self.issues.get(number)
        if issue is None:
            return
        before = set(issue.labels)
        after = set(labels)
        for label in after - before:
            self._timeline(number, "labeled", label=label)
        for label in before - after:
            self._timeline(number, "unlabeled", label=label)
        issue.labels = list(labels)
        issue.updated_at = self.clock()

    def set_assignees(self, number: int, logins: list[str]) -> None:
        issue = self.issues.get(number)
        if issue is not None:
            issue.assignees = list(logins)
            issue.updated_at = self.clock()

    def comment(self, number: int, body: str) -> GitHubComment:
        c = GitHubComment(
            id=self._new_comment_id(),
            issue_number=number,
            user_login=self.actor,
            body=body,
            created_at=self.clock(),
            updated_at=self.clock(),
        )
        self.comments.setdefault(number, []).append(c)
        self._timeline(number, "commented", payload={"body": body})
        return c

    def list_comments(self, since: str | None = None) -> list[GitHubComment]:
        out: list[GitHubComment] = []
        for cs in self.comments.values():
            out.extend(cs)
        return sorted(out, key=lambda c: c.created_at)

    def list_timeline(self, number: int) -> list[TimelineEvent]:
        return list(self.timelines.get(number, []))

    def list_prs(self) -> list[GitHubPR]:
        return sorted(self.prs.values(), key=lambda p: p.number)

    def get_pr(self, number: int) -> GitHubPR | None:
        return self.prs.get(number)

    def pr_reviews(self, number: int) -> list[GitHubReview]:
        pr = self.prs.get(number)
        return list(pr.reviews) if pr else []

    def list_pr_files(self, number: int) -> list[str]:
        pr = self.prs.get(number)
        return list(pr.files) if pr else []

    def list_review_comments(self, number: int) -> list[dict]:
        return list(self.review_comments.get(number, []))

    def create_pr(self, title: str, body: str, head: str, base: str, draft: bool = False) -> GitHubPR:
        self._pr_seq = max(self._pr_seq, max(self.prs, default=0)) + 1
        pr = GitHubPR(
            number=self._pr_seq,
            title=title,
            body=body,
            state="open",
            draft=draft,
            head=head,
            base=base,
            user_login=self.actor,
            created_at=self.clock(),
            updated_at=self.clock(),
        )
        self.prs[pr.number] = pr
        self.trees.setdefault(head, {})
        self.branches.setdefault(head, _sha(0))
        return pr

    def update_pr(
        self,
        number: int,
        title: str | None = None,
        body: str | None = None,
        draft: bool | None = None,
    ) -> GitHubPR | None:
        pr = self.prs.get(number)
        if pr is None:
            return None
        if title is not None:
            pr.title = title
        if body is not None:
            pr.body = body
        if draft is not None:
            pr.draft = draft
        pr.updated_at = self.clock()
        return pr

    def close_pr(self, number: int) -> None:
        pr = self.prs.get(number)
        if pr is not None and pr.state != "closed":
            pr.state = "closed"
            pr.updated_at = self.clock()

    def create_review(self, pr_number: int, state: str, body: str, comments: list[dict] | None = None) -> GitHubReview:
        review = GitHubReview(
            user_login=self.actor,
            state=state,
            body=body,
            submitted_at=self.clock(),
        )
        pr = self.prs[pr_number]
        pr.reviews.append(review)
        pr.updated_at = self.clock()
        if comments:
            existing = self.review_comments.setdefault(pr_number, [])
            for c in comments:
                existing.append({**c, "user_login": self.actor, "submitted_at": self.clock().isoformat()})
        self._recompute_review_state(pr_number)
        return review

    def set_pr_checks(self, pr_number: int, checks: list[dict]) -> None:
        pr = self.prs[pr_number]
        pr.checks = list(checks)
        for c in checks:
            self.commit_statuses.setdefault(self.branches.get(pr.head, ""), {})[c["name"]] = c["state"]

    def merge_pr(self, number: int, merge_method: str = "squash") -> bool:
        pr = self.prs.get(number)
        if pr is None or pr.state == "merged" or pr.state == "closed":
            return False
        base_tree = self.trees.setdefault(pr.base, {})
        head_tree = self.trees.setdefault(pr.head, {})
        base_tree.update(head_tree)
        pr.state = "merged"
        pr.merged_at = self.clock()
        pr.updated_at = self.clock()
        self._bump_commit(pr.base)
        return True

    def reopen_issue(self, number: int) -> None:
        issue = self.issues.get(number)
        if issue is not None:
            issue.state = "open"
            issue.updated_at = self.clock()

    def close_issue(self, number: int) -> None:
        issue = self.issues.get(number)
        if issue is not None:
            issue.state = "closed"
            issue.closed_at = self.clock()
            issue.updated_at = self.clock()

    def get_file(self, path: str, ref: str) -> str | None:
        branch = self._resolve_ref(ref)
        return self.trees.get(branch, {}).get(path)

    def list_files(self, branch: str) -> list[str]:
        branch = self._resolve_ref(branch)
        return list(self.trees.get(branch, {}).keys())

    def _resolve_ref(self, ref: str) -> str:
        """Refs are branch names; tolerate SHA values by resolving back to a branch."""
        if ref in self.branches:
            return ref
        for branch, sha in self.branches.items():
            if sha == ref:
                return branch
        return ref

    def get_ref(self, branch: str) -> str | None:
        return self.branches.get(branch)

    def list_required_checks(self, ref: str) -> list[RequiredCheck]:
        statuses = self.commit_statuses.get(ref, {})
        out = []
        for name in sorted(self.required_check_names):
            state = statuses.get(name, "pending")
            out.append(RequiredCheck(name=name, state=state))  # type: ignore[arg-type]
        return out

    def set_commit_status(self, ref: str, state: str, description: str = "") -> None:
        self.commit_statuses.setdefault(ref, {})["CI"] = state
        if "CI" not in self.required_check_names:
            self.required_check_names.add("CI")

    def upsert_label(self, name: str, color: str, description: str = "") -> None:
        self.labels[name] = (color, description)

    def has_label(self, name: str) -> bool:
        return name in self.labels

    # -- admin protocol ---------------------------------------------------------

    def pin_issue(self, number: int, pinned: bool) -> None:
        issue = self.issues.get(number)
        if issue is not None:
            issue.pinned = pinned

    def create_ref(self, branch: str, sha: str) -> None:
        self.branches[branch] = sha
        self.trees.setdefault(branch, {})

    def upsert_file(self, path: str, content: str, message: str, branch: str) -> str:
        self.trees.setdefault(branch, {})[path] = content
        return self._bump_commit(branch)

    def get_branch_protection(self, branch: str) -> dict | None:
        return self.branch_protection.get(branch)

    def update_branch_protection(
        self,
        branch: str,
        require_prs: bool,
        require_approvals: int,
        checks: list[str] | None = None,
    ) -> None:
        self.branch_protection[branch] = {
            "require_prs": require_prs,
            "require_approvals": require_approvals,
            "checks": checks or [],
        }
        self.required_check_names.update(checks or [])

    def update_repo(self, merge_method: str, delete_branch_on_merge: bool = True) -> None:
        self.repo_options["merge_method"] = merge_method
        self.repo_options["delete_branch_on_merge"] = delete_branch_on_merge

    def invite_collaborator(self, login: str, permission: str = "push") -> None:
        self.collaborators.add(login)

    def get_authenticated_user(self) -> str:
        return self.actor

    # -- observability for tests -------------------------------------------------

    def issue(self, number: int) -> GitHubIssue | None:
        return self.issues.get(number)

    def labels_of(self, number: int) -> list[str]:
        issue = self.issues.get(number)
        return list(issue.labels) if issue else []

    def latest_comment(self, number: int) -> str:
        cs = self.comments.get(number, [])
        return cs[-1].body if cs else ""

    @property
    def all_timelines(self) -> dict[int, list[TimelineEvent]]:
        return self.timelines

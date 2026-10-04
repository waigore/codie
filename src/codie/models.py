"""Pydantic models for Codie — state, work items, structured role payloads.

Implements Technical Spec §5.3 (derived model), §5.7 (WorkResult), §7.2
(structured planner/tester payloads), and §9.1 wire types.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

FeatureStatus = Literal[
    "proposed",
    "speccing",
    "spec-review",
    "specified",
    "planned",
    "in-progress",
    "review",
    "accepted",
    "revised",
    "cancelled",
]

TaskStatus = Literal["backlog", "ready", "in-progress", "in-review", "done", "cancelled"]

BugStatus = Literal["backlog", "ready", "in-progress", "in-review", "verifying", "done", "cancelled"]

Kind = Literal["dev", "integration", "test"]

Priority = Literal["high", "medium", "low"]

Flag = Literal["blocked", "needs-human"]

# One of the five role identities or the orchestrator/surveyor label for agents.
RoleName = Literal["planner", "coder", "reviewer", "tester", "kernel", "orchestrator", "surveyor"]

ReviewState = Literal["approved", "changes_requested", "commented", "dismissed", "none"]

PRStatus = Literal["open", "closed", "merged"]

CheckState = Literal["success", "pending", "failure"]

WorkStatus = Literal["applied", "needs_human", "failed"]

# ---------------------------------------------------------------------------
# Wire types — what GitHubClient returns (Technical Spec §9.1)
# ---------------------------------------------------------------------------


class LabelEvent(BaseModel):
    """A label add/remove event in the issue timeline."""

    name: str
    created_at: datetime


class TimelineEvent(BaseModel):
    """One item of the issue timeline (Technical Spec §5.9 attribution)."""

    kind: str  # labeled | unlabeled | closed | reopened | commented | cross-referenced | ...
    actor: str
    created_at: datetime
    label: str | None = None
    payload: dict = Field(default_factory=dict)


class GitHubIssue(BaseModel):
    """A fetched issue (the shape produced by GitHubClient.list_issues)."""

    model_config = ConfigDict(extra="allow")

    number: int
    title: str
    body: str
    state: str  # "open" | "closed"
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    user_login: str = ""
    pinned: bool = False


class GitHubComment(BaseModel):
    """An issue comment with authorship (login == bot login ⇒ bot, else human)."""

    model_config = ConfigDict(extra="allow")

    id: int
    issue_number: int
    user_login: str
    body: str
    created_at: datetime
    updated_at: datetime


class GitHubReview(BaseModel):
    """A pull-request review (used on PR heads for review_state)."""

    model_config = ConfigDict(extra="allow")

    user_login: str
    state: str  # "APPROVED" | "CHANGES_REQUESTED" | "COMMENTED" | "DISMISSED"
    submitted_at: datetime
    body: str = ""
    commit_id: str | None = None
    on_head: bool = True


class GitHubPR(BaseModel):
    """A fetched pull request."""

    model_config = ConfigDict(extra="allow")

    number: int
    title: str
    body: str
    state: str  # "open" | "closed"
    draft: bool
    head: str  # head branch name
    base: str  # base branch name
    user_login: str
    created_at: datetime
    updated_at: datetime
    merged_at: datetime | None = None
    reviews: list[GitHubReview] = Field(default_factory=list)
    checks: list[dict] = Field(default_factory=list)  # [{ name, state (success|pending|failure) }]
    review_state: ReviewState = "none"
    files: list[str] = Field(default_factory=list)
    # Convenience fields maintained by the adapters (secondary to derived state).
    linked_issue: int | None = None
    changes_requested: bool = False
    reviewer_approved: bool = False


class RequiredCheck(BaseModel):
    name: str
    state: CheckState


# ---------------------------------------------------------------------------
# Parsed markers (state/parse.py output)
# ---------------------------------------------------------------------------


class DoneWhen(BaseModel):
    checked: bool
    text: str
    requirement_ids: list[str] = Field(default_factory=list)


class ParsedIssue(BaseModel):
    number: int
    title: str
    body: str
    labels: list[str] = Field(default_factory=list)
    type_: Literal["prd", "feature", "task", "bug", "untyped"] | None = None
    feature_status: FeatureStatus | None = None
    task_status: TaskStatus | BugStatus | None = None
    kind: Kind | None = None
    priority: Priority = "medium"
    flags: set[Flag] = Field(default_factory=set)
    parent: int | None = None
    depends_on: list[int] = Field(default_factory=list)
    evals: list[str] = Field(default_factory=list)
    prd_sections: str = ""
    done_when: list[DoneWhen] = Field(default_factory=list)
    spec_pr_number: int | None = None
    spec_paths: list[str] = Field(default_factory=list)
    requirement_lines: list[str] = Field(default_factory=list)
    reproduction: str = ""
    expected_vs_actual: str = ""
    referenced_prs: list[int] = Field(default_factory=list)
    unreferenced: bool = False


class Eval(BaseModel):
    id: str
    text: str
    checked: bool
    command: list[str] | None = None  # from .codie.yaml evals.E<n>
    mode: Literal["command", "human"] | None = None


# ---------------------------------------------------------------------------
# Derived state (Technical Spec §5.3)
# ---------------------------------------------------------------------------


class PrdIssue(BaseModel):
    number: int
    title: str
    body: str
    evals: list[Eval] = Field(default_factory=list)
    updated_at: datetime
    fingerprint: str = ""


class Feature(BaseModel):
    number: int
    title: str
    body: str
    status: FeatureStatus
    priority: Priority = "medium"
    flags: set[Flag] = Field(default_factory=set)
    evals: list[str] = Field(default_factory=list)
    criteria: list[str] = Field(default_factory=list)
    prd_sections: str = ""
    tasks: list[int] = Field(default_factory=list)  # issue numbers with Parent: == this number
    spec_pr_number: int | None = None
    spec_paths: list[str] = Field(default_factory=list)
    requirement_lines: list[str] = Field(default_factory=list)
    open: bool = True
    created_at: datetime
    updated_at: datetime


class TaskItem(BaseModel):
    number: int
    title: str
    body: str
    kind: Kind
    status: TaskStatus
    priority: Priority = "medium"
    flags: set[Flag] = Field(default_factory=set)
    parent: int | None = None
    depends_on: list[int] = Field(default_factory=list)
    linked_pr: int | None = None
    assignee: str | None = None
    done_when: list[DoneWhen] = Field(default_factory=list)
    open: bool = True
    created_at: datetime
    updated_at: datetime


class Bug(BaseModel):
    number: int
    title: str
    body: str
    status: BugStatus
    priority: Priority = "medium"
    flags: set[Flag] = Field(default_factory=set)
    parent: int | None = None
    depends_on: list[int] = Field(default_factory=list)
    linked_pr: int | None = None
    assignee: str | None = None
    reproduction: str = ""
    expected_vs_actual: str = ""
    open: bool = True
    created_at: datetime
    updated_at: datetime


class PRInfo(BaseModel):
    number: int
    title: str
    body: str
    draft: bool
    state: PRStatus
    head: str
    base: str
    user_login: str
    review_state: ReviewState = "none"
    checks: list[RequiredCheck] = Field(default_factory=list)
    linked_issue: int | None = None
    release_marker: bool = False
    spec_marker: bool = False  # spec PR (paths under specs/<issue#>-<slug>/)
    reviewer_approved: bool = False  # latest review on head is APPROVED by reviewer bot
    changes_requested: bool = False
    approved_non_bot: bool = False  # a non-bot APPROVED exists on the current head (§7.4 step 6)
    created_at: datetime
    updated_at: datetime
    merged_at: datetime | None = None


class BranchHeads(BaseModel):
    main: str | None = None
    dev: str | None = None


class SuiteMarker(BaseModel):
    scope: Literal["fast", "full"]
    sha: str
    passed: bool
    at: datetime


class Violation(BaseModel):
    issue_number: int
    code: str
    message: str


class Anomaly(BaseModel):
    kind: str
    issue_number: int | None = None
    pr_number: int | None = None
    message: str


class ProjectState(BaseModel):
    repo: str
    prd: PrdIssue | None = None
    features: list[Feature] = Field(default_factory=list)
    tasks: list[TaskItem] = Field(default_factory=list)
    bugs: list[Bug] = Field(default_factory=list)
    prs: list[PRInfo] = Field(default_factory=list)
    branches: BranchHeads = Field(default_factory=BranchHeads)
    codie_yaml_present: bool = True
    violations: list[Violation] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)
    release_merged: bool = False
    # Reconstructable guardrail counters and markers (C15).
    deferral_counts: dict[int, int] = Field(default_factory=dict)
    idle_dispatch_counts: dict[int, int] = Field(default_factory=dict)
    cycle_counts: dict[int, int] = Field(default_factory=dict)
    acceptance_failed_cycles: dict[int, int] = Field(default_factory=dict)
    latest_heartbeats: dict[int, str] = Field(default_factory=dict)  # issue → "<run_id> <iso>"
    suite_markers: list[SuiteMarker] = Field(default_factory=list)
    eval_uncertain: set[str] = Field(default_factory=set)
    revision_marker: dict[int, str] = Field(default_factory=dict)
    prd_fingerprint_comment: str | None = None
    prd_fingerprint_changed: bool = False
    timeline: dict[int, list[TimelineEvent]] = Field(default_factory=dict)
    spec_pr_by_feature: dict[int, int] = Field(default_factory=dict)
    raw_labels: dict[int, list[str]] = Field(default_factory=dict)
    other_prd_numbers: list[int] = Field(default_factory=list)
    dispute_comments: dict[int, list[tuple[str, str, str, str]]] = Field(
        default_factory=dict
    )  # issue -> [(first_line, actor, body, iso_ts)]

    def find_feature(self, number: int) -> Feature | None:
        for f in self.features:
            if f.number == number:
                return f
        return None

    def find_task(self, number: int) -> TaskItem | None:
        for t in self.tasks:
            if t.number == number:
                return t
        return None

    def find_bug(self, number: int) -> Bug | None:
        for b in self.bugs:
            if b.number == number:
                return b
        return None

    def find_work_item(self, number: int) -> TaskItem | Bug | None:
        return self.find_task(number) or self.find_bug(number)

    def find_pr(self, number: int) -> PRInfo | None:
        for p in self.prs:
            if p.number == number:
                return p
        return None

    @property
    def open_bugs(self) -> list[Bug]:
        return [b for b in self.bugs if b.status not in {"done", "cancelled"}]

    def latest_suite_marker(self, scope: str) -> SuiteMarker | None:
        candidates = [m for m in self.suite_markers if m.scope == scope]
        return max(candidates, key=lambda m: m.at, default=None)

    @property
    def eval_ids(self) -> set[str]:
        if not self.prd:
            return set()
        return {e.id for e in self.prd.evals}

    def eval(self, eid: str) -> Eval | None:
        if not self.prd:
            return None
        for e in self.prd.evals:
            if e.id == eid:
                return e
        return None

    def unchecked_evals(self) -> list[Eval]:
        if not self.prd:
            return []
        return [e for e in self.prd.evals if not e.checked]


# ---------------------------------------------------------------------------
# Anomaly / survey / plan payload types (Technical Spec §7.2, §9.6)
# ---------------------------------------------------------------------------


class FeatureIssueDraft(BaseModel):
    title: str
    body_markdown: str
    evals: list[str] = Field(default_factory=list)
    priority: Priority = "medium"
    prd_sections: str = ""


class FeaturePlan(BaseModel):
    features: list[FeatureIssueDraft]


class TaskSpec(BaseModel):
    ref: str  # temp id ("t1") unique within one breakdown
    title: str
    kind: Kind
    depends_on: list[int | str] = Field(default_factory=list)
    body_markdown: str = ""


class TaskBreakdown(BaseModel):
    tasks: list[TaskSpec]


class RevisionAssessment(BaseModel):
    kind: Literal["requirements", "implementation"]
    rationale: str


class ReplanResult(BaseModel):
    new_tasks: list[TaskSpec]
    cancel_task_numbers: list[int] = Field(default_factory=list)
    rationale: str = ""


class PlanReconciliation(BaseModel):
    add: list[FeatureIssueDraft] = Field(default_factory=list)
    cancel_feature_numbers: list[int] = Field(default_factory=list)
    restale_feature_numbers: list[int] = Field(default_factory=list)


class ReleasePlan(BaseModel):
    version: str
    changelog: str
    eval_report: str


class BugDraft(BaseModel):
    title: str
    priority: Priority
    parent: int | None = None
    depends_on: list[int] = Field(default_factory=list)
    body_markdown: str = ""


class ReviewComment(BaseModel):
    path: str
    line: int | None = None
    side: Literal["LEFT", "RIGHT"] = "RIGHT"
    body: str


class ReviewDecision(BaseModel):
    verdict: Literal["approve", "request_changes"]
    comments: list[ReviewComment] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)


class SpecDraftResult(BaseModel):
    pr_number: int
    files: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    value: str
    evidence: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "medium"
    source: Literal["adopted", "default"] = "adopted"


class IssueTriage(BaseModel):
    number: int
    proposed_type: Literal["prd", "feature", "task", "bug", "untyped"]
    proposed_status: str = ""
    rationale: str = ""
    confidence: Literal["low", "medium", "high"] = "medium"


class PrdProposal(BaseModel):
    source: Literal["adopted_doc", "synthesized", "supplied", "none"] = "none"
    content: str = ""
    evidence: list[str] = Field(default_factory=list)


class RepoSurvey(BaseModel):
    dimensions: dict[str, Finding] = Field(default_factory=dict)
    issue_triage: list[IssueTriage] = Field(default_factory=list)
    prd: PrdProposal = Field(default_factory=lambda: PrdProposal())

    def get(self, key: str) -> Finding | None:
        return self.dimensions.get(key)


# ---------------------------------------------------------------------------
# Work items and results (Technical Spec §5.6, §5.7)
# ---------------------------------------------------------------------------

WorkItemKind = Literal[
    "NeedsHuman",
    "PlanDecomposition",
    "ReconcilePlan",
    "AssessRevision",
    "Replan",
    "DraftSpecs",
    "ReviseSpecs",
    "ReviewSpecPR",
    "MergeSpecPR",
    "BreakDownTasks",
    "ReviewPR",
    "MergePR",
    "AddressReview",
    "Implement",
    "VerifyTask",
    "ReverifyBug",
    "AcceptanceRun",
    "RegressionRun",
    "EvalSuite",
    "ProposeRelease",
]


class WorkItem(BaseModel):
    kind: WorkItemKind
    role: RoleName
    entity: str = ""  # human label of the entity, e.g. "#42 Secure login"
    issue_number: int | None = None
    pr_number: int | None = None
    priority: Priority = "medium"
    rule: int = 0  # §5.6 rule number that produced this item


class Mutation(BaseModel):
    op: Literal[
        "set_labels",
        "comment",
        "create_issue",
        "edit_issue",
        "create_pr",
        "merge_pr",
        "close_pr",
        "create_review",
        "check_eval",
    ]
    target: str
    expected: dict = Field(default_factory=dict)


class WorkResult(BaseModel):
    work_item_kind: str
    status: WorkStatus
    summary: str
    mutations: list[Mutation] = Field(default_factory=list)
    payload: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Structured payloads bound to a WorkResult (payload of each kind)
# ---------------------------------------------------------------------------

Payload = (
    FeaturePlan
    | TaskBreakdown
    | RevisionAssessment
    | ReplanResult
    | PlanReconciliation
    | SpecDraftResult
    | ReleasePlan
    | BugDraft
    | ReviewDecision
)

PAYLOAD_KINDS: dict[str, type[BaseModel] | None] = {
    "PlanDecomposition": FeaturePlan,
    "DraftSpecs": SpecDraftResult,
    "ReviseSpecs": SpecDraftResult,
    "AssessRevision": RevisionAssessment,
    "Replan": ReplanResult,
    "BreakDownTasks": TaskBreakdown,
    "ReconcilePlan": PlanReconciliation,
    "ProposeRelease": ReleasePlan,
    "BugDraft": BugDraft,
    "ReviewPR": ReviewDecision,
    "ReviewSpecPR": ReviewDecision,
    # Tool-acting kinds: the crew applies writes directly; payload is raw.
    "Implement": None,
    "AddressReview": None,
    "MergePR": None,
    "MergeSpecPR": None,
    "VerifyTask": None,
    "RegressionRun": None,
    "AcceptanceRun": None,
    "ReverifyBug": None,
    "EvalSuite": None,
    "NeedsHuman": None,
}


def now_utc() -> datetime:
    return datetime.now(UTC)

"""GitHub payloads → ProjectState (Technical Spec §5.3).

Pure, deterministic functions of GitHub data only — no LLM, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codie.config import EvalEntry, Settings
from codie.models import (
    Anomaly,
    BranchHeads,
    Bug,
    Feature,
    GitHubComment,
    GitHubIssue,
    GitHubPR,
    PrdIssue,
    PRInfo,
    ProjectState,
    RequiredCheck,
    ReviewState,
    SuiteMarker,
    TaskItem,
    TimelineEvent,
    Violation,
)
from codie.state import parse

ReviewStateResult = tuple[
    ReviewState, bool, bool, bool
]  # state, reviewer_approved, changes_requested, approved_non_bot


@dataclass
class RepoSnapshot:
    """Everything the derivation needs, fetched from GitHub by the kernel."""

    repo: str
    issues: list[GitHubIssue]
    comments: list[GitHubComment]
    prs: list[GitHubPR]
    timeline: dict[int, list[TimelineEvent]]
    branches: BranchHeads
    codie_yaml_present: bool
    spec_files: dict[str, str] = field(default_factory=dict)  # "feature.<n>.<filename>" -> content
    bot_logins: set[str] = field(default_factory=set)


def compute_pr_review_state(pr: GitHubPR, reviewer_bot: str) -> ReviewStateResult:
    reviews = [r for r in pr.reviews if getattr(r, "on_head", True)]
    has_cr = any(r.state == "CHANGES_REQUESTED" for r in reviews)
    bot_approved = any(r.state == "APPROVED" and r.user_login == reviewer_bot for r in reviews)
    non_bot_approved = any(r.state == "APPROVED" and r.user_login != reviewer_bot for r in reviews)
    if has_cr:
        return "changes_requested", False, True, False
    if bot_approved:
        return "approved", True, False, non_bot_approved
    latest = max((r for r in reviews), key=lambda r: r.submitted_at, default=None)
    state: ReviewState = "none"
    if latest is not None:
        mapping = {
            "APPROVED": "approved",
            "CHANGES_REQUESTED": "changes_requested",
            "COMMENTED": "commented",
            "DISMISSED": "dismissed",
        }
        state = mapping.get(latest.state, "none")  # type: ignore[assignment]
    return state, False, False, non_bot_approved


def derive(snapshot: RepoSnapshot, settings: Settings | None = None) -> ProjectState:
    settings = settings or Settings()
    eval_commands: dict[str, EvalEntry] = settings.overrides.evals
    reviewer_bot = settings.github.login("reviewer")
    by_number: dict[int, GitHubIssue] = {i.number: i for i in snapshot.issues}
    comments_by_issue: dict[int, list[GitHubComment]] = {}
    for c in snapshot.comments:
        comments_by_issue.setdefault(c.issue_number, []).append(c)
    label_mapping = settings.overrides.label_mapping

    state = ProjectState(
        repo=snapshot.repo,
        branches=snapshot.branches,
        codie_yaml_present=snapshot.codie_yaml_present,
    )
    state.raw_labels = {i.number: i.labels for i in snapshot.issues}

    parsed: dict[int, dict] = {}
    for issue in snapshot.issues:
        parsed[issue.number] = parse.classify_issue(issue, label_mapping)

    # ---- PRD selection (§5.4 rule 8) ----
    prd_issues = [n for n, p in parsed.items() if p["type_"] == "prd"]
    prd: PrdIssue | None = None
    excluded_prd: list[int] = []
    if len(prd_issues) == 1:
        keep = prd_issues[0]
    elif prd_issues:
        pinned = [n for n in prd_issues if by_number[n].pinned]
        if pinned:
            keep = max(pinned, key=lambda n: by_number[n].updated_at)
        else:
            keep = max(prd_issues, key=lambda n: by_number[n].created_at)
        excluded_prd = [n for n in prd_issues if n != keep]
    else:
        keep = None

    if keep is not None:
        state.other_prd_numbers = [n for n in prd_issues if n != keep]
        issue = by_number[keep]
        evals = parse.parse_prd_evals(issue.body, eval_commands)
        prd = PrdIssue(
            number=keep,
            title=issue.title,
            body=issue.body,
            evals=evals,
            updated_at=issue.updated_at,
            fingerprint=parse.prd_fingerprint(issue.body),
        )
        # fingerprint marker comment (§5.2)
        for c in sorted(comments_by_issue.get(keep, []), key=lambda c: c.created_at):
            marker = parse.prd_marker_fingerprint(c.body)
            if marker:
                state.prd_fingerprint_comment = marker
        state.prd_fingerprint_changed = state.prd_fingerprint_comment != prd.fingerprint
        # suite markers (PRD issue comments, §7.5)
        for c in comments_by_issue.get(keep, []):
            m = _suite_marker(c.body)
            if m:
                state.suite_markers.append(SuiteMarker(scope=m[0], sha=m[1], passed=m[2], at=c.created_at))  # type: ignore[arg-type]
        # eval uncertain markers
        for c in comments_by_issue.get(keep, []):
            eid = _eval_uncertain_marker(c.body)
            if eid:
                state.eval_uncertain.add(eid)
        state.prd = prd

    # ---- PRs (§5.2) ----
    pr_infos: dict[int, PRInfo] = {}
    for gpr in snapshot.prs:
        review_state, reviewer_approved, changes_requested, approved_non_bot = compute_pr_review_state(
            gpr, reviewer_bot
        )
        closing, used_closing = parse.linked_issue_numbers(gpr.body)
        linked = closing[0] if closing else None
        release_marker = "<!-- codie:release -->" in (gpr.body or "").lower() or "codie:release" in (gpr.body or "")
        spec_marker = any(f.startswith("specs/") for f in gpr.files)
        merged = bool(gpr.merged_at)
        state_val: str = "merged" if merged else gpr.state
        pr_info = PRInfo(
            number=gpr.number,
            title=gpr.title,
            body=gpr.body,
            draft=gpr.draft,
            state=state_val,  # type: ignore[arg-type]
            head=gpr.head,
            base=gpr.base,
            user_login=gpr.user_login,
            review_state=review_state,
            checks=[RequiredCheck.model_validate(c) for c in gpr.checks],
            linked_issue=linked,
            release_marker=release_marker,
            spec_marker=spec_marker,
            reviewer_approved=reviewer_approved,
            changes_requested=changes_requested,
            created_at=gpr.created_at,
            updated_at=gpr.updated_at,
            merged_at=gpr.merged_at,
            approved_non_bot=approved_non_bot,
        )
        pr_infos[gpr.number] = pr_info
        if release_marker and merged:
            state.release_merged = True
    state.prs = sorted(pr_infos.values(), key=lambda p: p.number)

    # link tasks/bugs to their PRs
    linked_prs_by_issue: dict[int, int] = {}
    for item_pr in state.prs:
        if item_pr.linked_issue is not None and linked_prs_by_issue.get(item_pr.linked_issue) is None:
            linked_prs_by_issue[item_pr.linked_issue] = item_pr.number

    # ---- Features / tasks / bugs ----
    for issue in sorted((i for i in snapshot.issues), key=lambda i: i.number):
        p = parsed[issue.number]
        t = p["type_"]
        if t == "feature":
            state.features.append(
                Feature(
                    number=issue.number,
                    title=issue.title,
                    body=issue.body,
                    status=p["feature_status"] or "proposed",  # type: ignore[assignment]
                    priority=p["priority"],
                    flags=p["flags"],
                    evals=p["evals"],
                    criteria=[],
                    prd_sections=p["prd_sections"],
                    spec_pr_number=p["spec_pr_number"],
                    spec_paths=p["spec_paths"],
                    requirement_lines=_spec_requirement_lines(snapshot, issue.number),
                    open=issue.state == "open",
                    created_at=issue.created_at,
                    updated_at=issue.updated_at,
                )
            )
        elif t == "task":
            state.tasks.append(
                TaskItem(
                    number=issue.number,
                    title=issue.title,
                    body=issue.body,
                    kind=p["kind"] or "dev",  # type: ignore[assignment]
                    status=p["task_status"] or "backlog",  # type: ignore[assignment]
                    priority=p["priority"],
                    flags=p["flags"],
                    parent=p["parent"],
                    depends_on=p["depends_on"],
                    linked_pr=linked_prs_by_issue.get(issue.number),
                    done_when=p["done_when"],
                    open=issue.state == "open",
                    created_at=issue.created_at,
                    updated_at=issue.updated_at,
                )
            )
        elif t == "bug":
            state.bugs.append(
                Bug(
                    number=issue.number,
                    title=issue.title,
                    body=issue.body,
                    status=p["task_status"] or "ready",  # type: ignore[assignment]
                    priority=p["priority"],
                    flags=p["flags"],
                    parent=p["parent"],
                    depends_on=p["depends_on"],
                    linked_pr=linked_prs_by_issue.get(issue.number),
                    reproduction=p["reproduction"],
                    expected_vs_actual=p["expected_vs_actual"],
                    open=issue.state == "open",
                    created_at=issue.created_at,
                    updated_at=issue.updated_at,
                )
            )
        elif t is None and issue.user_login not in snapshot.bot_logins and issue.state == "open" or t == "untyped":
            state.anomalies.append(
                Anomaly(
                    kind="untyped-issue",
                    issue_number=issue.number,
                    message=f"issue #{issue.number} has no type:* label",
                )
            )

    # feature tasks: set of issues whose Parent: resolves to the feature (M20)
    for task in state.tasks:
        if task.parent and (feature := state.find_feature(task.parent)):
            feature.tasks.append(task.number)
    for bug in state.bugs:
        if bug.parent and (feature := state.find_feature(bug.parent)) and bug.number not in feature.tasks:
            feature.tasks.append(bug.number)

    # spec PR attribution: feature's ## Specs PR + spec PRs linked via linkage/branch
    for feature in state.features:
        if feature.spec_pr_number and (pr := state.find_pr(feature.spec_pr_number)) is not None:
            pr.spec_marker = True
            state.spec_pr_by_feature[feature.number] = pr.number
    for pr in state.prs:
        if pr.spec_marker:
            target = pr.linked_issue
            if target is None:
                target = _spec_branch_feature(pr.head)
            if target is not None and (feature := state.find_feature(target)) is not None:
                state.spec_pr_by_feature[target] = pr.number
                feature.spec_pr_number = pr.number

    # ---- PRD exclusion violation (§5.4 rule 8) ----
    if excluded_prd:
        state.violations.append(
            Violation(
                issue_number=keep,  # type: ignore[arg-type]
                code="second-prd",
                message="more than one type:prd issue exists; keeping the pinned (else newest) one and excluding the rest",  # noqa: E501
            )
        )
        for n in excluded_prd:
            state.anomalies.append(
                Anomaly(
                    kind="secondary-prd",
                    issue_number=n,
                    message=f"duplicate PRD issue #{n} excluded",
                )
            )

    # ---- guardrail markers from comments (C15) ----
    state.timeline = snapshot.timeline
    for issue_number, comments in comments_by_issue.items():
        for c in comments:
            first_line = c.body.strip().splitlines()[0] if c.body.strip() else ""
            if first_line in {"**Dispute:**", "**Concede:**"}:
                state.dispute_comments.setdefault(issue_number, []).append(
                    (first_line, c.user_login, c.body, c.created_at.isoformat())
                )
            _collect_markers(state, issue_number, c.body)

    # ---- human-vs-bot anomalies for non-standard hooks ----
    for issue in snapshot.issues:
        if issue.user_login not in snapshot.bot_logins and issue.state == "open":
            # human activity signals are triaged by the Orchestrator agent.
            pass

    return state


def _spec_requirement_lines(snapshot: RepoSnapshot, feature_number: int) -> list[str]:
    lines: list[str] = []
    for key, content in snapshot.spec_files.items():
        prefix = f"feature.{feature_number}."
        if key.startswith(prefix):
            lines.extend(parse.parse_requirement_lines(content))
    return lines


def _suite_marker(body: str) -> tuple[str, str, bool] | None:
    import re

    m = re.search(r"<!--\s*codie:suite\s+(fast|full)\s+(\w+)\s+(pass|fail)\s*-->", body)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3) == "pass"


def _eval_uncertain_marker(body: str) -> str | None:
    import re

    m = re.search(r"<!--\s*codie:eval\s+(E\d+)\s+uncertain\s*-->", body)
    return m.group(1) if m else None


def _collect_markers(state: ProjectState, issue_number: int, body: str) -> None:
    import re

    m = re.search(r"<!--\s*codie:defer\s+\S+\s+(\d+)\s*-->", body)
    if m:
        state.deferral_counts[issue_number] = max(state.deferral_counts.get(issue_number, 0), int(m.group(1)))
    m = re.search(r"<!--\s*codie:idle-dispatch\s+(\d+)\s*-->", body)
    if m:
        state.idle_dispatch_counts[issue_number] = max(
            state.idle_dispatch_counts.get(issue_number, 0), int(m.group(1))
        )
    m = re.search(r"<!--\s*codie:cycle\s+acceptance-failed\s+(\S+)\s*-->", body)
    if m:
        state.acceptance_failed_cycles[issue_number] = state.acceptance_failed_cycles.get(issue_number, 0) + 1
    m = re.search(r"<!--\s*codie:revision\s+(requirements|implementation)\s*-->", body)
    if m:
        state.revision_marker[issue_number] = m.group(1)
    hb = re.search(r"<!--\s*codie:heartbeat\s+(\S+)\s+([0-9TZ:.+-]+)\s*-->", body)
    if hb:
        state.latest_heartbeats[issue_number] = f"{hb.group(1)} {hb.group(2)}"


def _spec_branch_feature(branch: str) -> int | None:
    """Parse the feature number out of a `spec/<issue#>-<slug>` branch."""
    if not branch.startswith("spec/"):
        return None
    rest = branch[len("spec/") :]
    num = ""
    for ch in rest:
        if ch.isdigit():
            num += ch
        elif ch == "-":
            break
        else:
            break
    return int(num) if num else None


def owner_path(repo: str) -> str:
    return repo.split("/")[0] if "/" in repo else repo

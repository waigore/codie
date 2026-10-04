"""Work-queue computation (Technical Spec §5.6).

A pure function `ProjectState → list[WorkItem]` in the §5.6 total order. The queue
IS the lawful action space of the Orchestrator agent: it may dispatch, defer, or
escalate queue items, but never invent work outside them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from codie.config import Settings
from codie.models import Bug, ProjectState, TaskItem, WorkItem

_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _feature_spec_pr(state: ProjectState, feature_number: int):
    number = state.spec_pr_by_feature.get(feature_number)
    return state.find_pr(number) if number else None


def ci_green(pr) -> bool:
    # §7.4 step 3: green means the required-check list is empty or every entry is
    # `success`. `pr.checks` carries the *required* check states (populated at
    # fetch time via list_required_checks, M19/N6) — a non-required failing check
    # is never in this list and therefore never blocks merge.
    return all(check.state == "success" for check in pr.checks)


def ready_for_review(pr) -> bool:
    return pr.state == "open" and not pr.draft


def compute_queue(state: ProjectState, settings: Settings, now: datetime | None = None) -> list[WorkItem]:
    now = now or datetime.now(UTC)
    items: list[WorkItem] = []
    matched_features: set[int] = set()
    matched_issues: set[int] = set()
    matched_prs: set[int] = set()
    matched_repo: bool = False

    def feature_key(n: int) -> tuple[str, int]:
        return ("feature", n)

    def issue_key(n: int) -> tuple[str, int]:
        return ("issue", n)

    def pr_key(n: int) -> tuple[str, int]:
        return ("pr", n)

    dev_branch = settings.branches.dev
    bugs_first = settings.overrides.workflow.bugs_outrank_features

    # ---- Rule 1: PRD missing ----
    if state.prd is None:
        return [WorkItem(kind="NeedsHuman", role="kernel", entity="PRD", priority="high", rule=1)]

    def repo_item() -> bool:
        return matched_repo

    def mark_repo() -> None:
        nonlocal matched_repo
        matched_repo = True

    # ---- Rule 2: PRD present, zero features ----
    if not state.features and not repo_item():
        mark_repo()
        items.append(_item("PlanDecomposition", "planner", entity=state.prd.title, rule=2))

    # ---- Rule 2b: PRD fingerprint changed ----
    if state.prd_fingerprint_changed and not repo_item():
        mark_repo()
        items.append(_item("ReconcilePlan", "planner", entity=state.prd.title, rule=2))

    # ---- Rules 3–4e, feature-scoped ----
    for feature in sorted(state.features, key=lambda f: f.number):
        if feature_key(feature.number) in matched_features:
            continue
        spec_pr = _feature_spec_pr(state, feature.number)

        # Rule 3: revised, no revision marker; if the marker routes, the queue
        # emits Replan (marker == implementation) or leaves the routing to the
        # reconciler (marker == requirements cancels children and re-specs).
        if feature.status == "revised" and feature.number not in state.revision_marker:
            matched_features.add(feature.number)
            items.append(
                _item(
                    "AssessRevision",
                    "planner",
                    entity=_f(feature),
                    issue_number=feature.number,
                    priority=feature.priority,
                    rule=3,
                )
            )
            continue
        if feature.status == "revised" and state.revision_marker.get(feature.number) == "implementation":
            matched_features.add(feature.number)
            items.append(
                _item(
                    "Replan",
                    "planner",
                    entity=_f(feature),
                    issue_number=feature.number,
                    priority=feature.priority,
                    rule=3,
                )
            )
            continue

        # Rule 4: proposed, or speccing with no ready-for-review PR and no CR review
        if feature.status == "proposed" or (
            feature.status == "speccing"
            and not _has_ready_spec_pr(state, feature)
            and not _has_cr_spec_pr(state, feature)
        ):
            matched_features.add(feature.number)
            items.append(
                _item(
                    "DraftSpecs",
                    "planner",
                    entity=_f(feature),
                    issue_number=feature.number,
                    priority=feature.priority,
                    rule=4,
                )
            )
            continue

        # Rule 4b: open spec PR with CR review
        if spec_pr is not None and spec_pr.state == "open" and spec_pr.changes_requested:
            matched_features.add(feature.number)
            items.append(
                _item(
                    "ReviseSpecs",
                    "planner",
                    entity=_f(feature),
                    issue_number=feature.number,
                    pr_number=spec_pr.number,
                    priority=feature.priority,
                    rule=4,
                )
            )
            continue

        # Rule 4c: spec-review, ready-for-review, review_state in {none, commented, dismissed}
        if (
            feature.status == "spec-review"
            and spec_pr is not None
            and ready_for_review(spec_pr)
            and spec_pr.review_state in {"none", "commented", "dismissed"}
        ):
            matched_features.add(feature.number)
            items.append(
                _item(
                    "ReviewSpecPR",
                    "reviewer",
                    entity=_f(feature),
                    issue_number=feature.number,
                    pr_number=spec_pr.number,
                    priority=feature.priority,
                    rule=4,
                )
            )
            continue

        # Rule 4d: spec PR ready, approved, CI green
        if (
            spec_pr is not None
            and ready_for_review(spec_pr)
            and spec_pr.review_state == "approved"
            and ci_green(spec_pr)
        ):
            matched_features.add(feature.number)
            items.append(
                _item(
                    "MergeSpecPR",
                    "reviewer",
                    entity=_f(feature),
                    issue_number=feature.number,
                    pr_number=spec_pr.number,
                    priority=feature.priority,
                    rule=4,
                )
            )
            continue

        # Rule 4e: specified, zero non-cancelled child tasks
        if feature.status == "specified":
            children = [t for t in state.tasks if t.parent == feature.number]
            non_cancelled = [t for t in children if t.status != "cancelled"]
            if not non_cancelled:
                matched_features.add(feature.number)
                items.append(
                    _item(
                        "BreakDownTasks",
                        "planner",
                        entity=_f(feature),
                        issue_number=feature.number,
                        priority=feature.priority,
                        rule=4,
                    )
                )
                continue

        # Rule 11: acceptance run
        if feature.status == "in-progress" and _acceptance_ready(state, feature):
            matched_features.add(feature.number)
            items.append(
                _item(
                    "AcceptanceRun",
                    "tester",
                    entity=_f(feature),
                    issue_number=feature.number,
                    priority=feature.priority,
                    rule=11,
                )
            )

    # ---- Rules 5–10, issue-scoped ----
    work_items: list[TaskItem | Bug] = [*state.tasks, *state.bugs]
    for item in sorted(work_items, key=lambda i: i.number):
        if issue_key(item.number) in matched_issues:
            continue
        pr = state.find_pr(item.linked_pr) if item.linked_pr else None

        # Rule 5: in-review, PR needs review
        if (
            item.status == "in-review"
            and pr is not None
            and pr.state == "open"
            and not pr.draft
            and pr.review_state in {"none", "commented", "dismissed"}
        ):
            matched_issues.add(item.number)
            matched_prs.add(pr.number)
            items.append(
                _item(
                    "ReviewPR",
                    "reviewer",
                    entity=_i(item),
                    issue_number=item.number,
                    pr_number=pr.number,
                    priority=item.priority,
                    rule=5,
                )
            )
            continue

        # Rule 6: in-review, PR approved + CI green, task PR
        if (
            (
                item.status == "in-review"
                and pr is not None
                and ready_for_review(pr)
                and pr.review_state == "approved"
                and ci_green(pr)
            )
            and _is_task_pr(pr, settings)
            and pr.base == dev_branch
        ):
            matched_issues.add(item.number)
            matched_prs.add(pr.number)
            items.append(
                _item(
                    "MergePR",
                    "reviewer",
                    entity=_i(item),
                    issue_number=item.number,
                    pr_number=pr.number,
                    priority=item.priority,
                    rule=6,
                )
            )

        # Rule 7: in-progress, open linked PR changes-requested
        if item.status == "in-progress" and pr is not None and pr.state == "open" and pr.changes_requested:
            role = "tester" if isinstance(item, TaskItem) and item.kind == "test" else "coder"
            matched_issues.add(item.number)
            items.append(
                _item(
                    "AddressReview",
                    role,
                    entity=_i(item),
                    issue_number=item.number,
                    pr_number=pr.number,
                    priority=item.priority,
                    rule=7,
                )
            )
            continue

        # Rule 8: ready dev/integration task or ready bug
        if item.status == "ready" and (
            isinstance(item, TaskItem) and item.kind in {"dev", "integration"} or isinstance(item, Bug)
        ):
            matched_issues.add(item.number)
            items.append(
                _item(
                    "Implement",
                    "coder",
                    entity=_i(item),
                    issue_number=item.number,
                    priority=item.priority,
                    rule=8,
                )
            )

        # Rule 8b: bug in-progress whose linked PR is merged (resume)
        if isinstance(item, Bug) and item.status == "in-progress" and pr is not None and pr.state == "merged":
            matched_issues.add(item.number)
            items.append(
                _item(
                    "Implement",
                    "coder",
                    entity=_i(item),
                    issue_number=item.number,
                    priority=item.priority,
                    rule=8,
                )
            )

        # Rule 9: ready kind:test
        if (
            isinstance(item, TaskItem)
            and item.kind == "test"
            and item.status == "ready"
            and issue_key(item.number) not in matched_issues
        ):
            matched_issues.add(item.number)
            items.append(
                _item(
                    "VerifyTask",
                    "tester",
                    entity=_i(item),
                    issue_number=item.number,
                    priority=item.priority,
                    rule=9,
                )
            )

        # Rule 10: bug verifying
        if isinstance(item, Bug) and item.status == "verifying":
            matched_issues.add(item.number)
            items.append(
                _item(
                    "ReverifyBug",
                    "tester",
                    entity=_i(item),
                    issue_number=item.number,
                    priority=item.priority,
                    rule=10,
                )
            )

    # ---- Rule 12: regression runs (repo-scoped) ----
    regression = _regression_due(state, settings, now)
    if regression and not repo_item():
        mark_repo()
        scope = "full" if "full" in regression else "fast"
        items.append(_item("RegressionRun", "tester", entity=f"integration suite ({scope})", rule=12))

    # ---- Rule 13: eval suite ----
    if _eval_suite_ready(state) and not repo_item():
        mark_repo()
        items.append(_item("EvalSuite", "tester", entity="PRD evals", rule=13))

    # ---- Rule 14: propose release ----
    if _release_ready(state) and not repo_item():
        mark_repo()
        items.append(_item("ProposeRelease", "planner", entity="release", rule=14))

    # ---- Rule 15: halt ----
    if state.release_merged:
        return []

    # §5.6 total order: rule number, bugs-first, priority, issue number
    bug_numbers = {b.number for b in state.bugs}
    items.sort(
        key=lambda w: (
            w.rule,
            0 if (bugs_first and _is_bug_work(w, bug_numbers)) else 1,
            _PRIORITY_RANK[w.priority],
            w.issue_number or 0,
        )
    )
    return items


def _item(kind, role, entity, issue_number=None, pr_number=None, priority="medium", rule=0) -> WorkItem:
    return WorkItem(
        kind=kind,
        role=role,
        entity=entity,
        issue_number=issue_number,
        pr_number=pr_number,
        priority=priority,
        rule=rule,
    )


def _f(feature) -> str:
    return f"#{feature.number} {feature.title}"


def _i(item) -> str:
    return f"#{item.number} {item.title}"


def _is_bug_work(w: WorkItem, bug_numbers: set[int]) -> bool:
    """Bugs outrank features: only `Implement` items whose target issue is a bug."""
    return w.kind == "Implement" and w.issue_number is not None and w.issue_number in bug_numbers


def _has_ready_spec_pr(state: ProjectState, feature) -> bool:
    pr = _feature_spec_pr(state, feature.number)
    return pr is not None and pr.state == "open" and not pr.draft and not pr.changes_requested


def _has_cr_spec_pr(state: ProjectState, feature) -> bool:
    pr = _feature_spec_pr(state, feature.number)
    return pr is not None and pr.state == "open" and pr.changes_requested


def _is_task_pr(pr, settings: Settings) -> bool:
    if pr.release_marker or pr.spec_marker:
        return False
    if pr.base != settings.branches.dev:
        return False
    return pr.linked_issue is not None


def _acceptance_pending(state: ProjectState, feature) -> bool:
    """Would this feature match rule 11 if the full-suite marker were current? (§7.5)"""
    children = [t for t in state.tasks if t.parent == feature.number]
    non_cancelled = [t for t in children if t.status != "cancelled"]
    if not non_cancelled or any(t.status != "done" for t in non_cancelled):
        return False
    linked_bugs = [b for b in state.bugs if b.parent == feature.number]
    return not any(b.status not in {"done", "cancelled"} for b in linked_bugs)


def _acceptance_ready(state: ProjectState, feature) -> bool:
    children = [t for t in state.tasks if t.parent == feature.number]
    non_cancelled = [t for t in children if t.status != "cancelled"]
    if not non_cancelled:
        return False
    if any(t.status != "done" for t in non_cancelled):
        return False
    linked_bugs = [b for b in state.bugs if b.parent == feature.number]
    if any(b.status not in {"done", "cancelled"} for b in linked_bugs):
        return False
    latest_full = state.latest_suite_marker("full")
    dev_head = state.branches.dev
    return not (dev_head is None or latest_full is None or latest_full.sha != dev_head or not latest_full.passed)


def _regression_due(state: ProjectState, settings: Settings, now: datetime) -> set[str]:
    """Returns a set of scopes due: {'fast'}, {'full'}, both, or empty.

    I-19: the "unverified merge" condition (fast) is cleared by *any* passing
    suite marker at the current integration head — a passing full run already
    contains the fast subset, so it never starves rules 13–14.
    """
    merged_task_prs = [
        pr for pr in state.prs if pr.state == "merged" and not pr.release_marker and pr.spec_marker is False
    ]
    if not merged_task_prs:
        return set()
    dev_head = state.branches.dev
    latest_fast = state.latest_suite_marker("fast")
    latest_full = state.latest_suite_marker("full")
    head_verified = any(
        m is not None and m.sha == dev_head and m.passed for m in (latest_fast, latest_full) if m is not None
    )
    fast_due = not head_verified
    # full run due when cadence elapsed or a feature awaits acceptance (rule 11)
    cadence = settings.overrides.workflow.full_test_cadence_minutes
    full_due = False
    if dev_head is None or latest_full is None or latest_full.sha != dev_head or not latest_full.passed:
        full_due = True
    else:
        age = (now - latest_full.at).total_seconds() / 60
        if age >= cadence:
            full_due = True
    if not full_due:
        stale_full = latest_full is None or dev_head is None or latest_full.sha != dev_head or not latest_full.passed
        if stale_full:
            for feature in state.features:
                if _acceptance_pending(state, feature):
                    full_due = True
                    break
    scopes: set[str] = set()
    if fast_due:
        scopes.add("fast")
    if full_due:
        scopes.add("full")
    return scopes


def _eval_suite_ready(state: ProjectState) -> bool:
    if not state.features:
        return False
    accepted = [f for f in state.features if f.status == "accepted"]
    non_cancelled = [f for f in state.features if f.status != "cancelled"]
    if not accepted or len(non_cancelled) != len(accepted):
        return False
    # §5.4 rule 9 / §7.7: no ongoing eval suite while any bug is open.
    if state.open_bugs:
        return False
    unchecked = state.unchecked_evals()
    if not unchecked:
        return False
    # at least one unchecked eval with no open bug citing it and no uncertain marker
    for e in unchecked:
        if e.id in state.eval_uncertain:
            continue
        citing = [b for b in state.bugs if b.status not in {"done", "cancelled"}]
        if any(_bug_cites_eval(b, e.id) for b in citing):
            continue
        return True
    return False


def _bug_cites_eval(bug: Bug, eid: str) -> bool:
    import re

    return re.search(rf"Eval:\s*{re.escape(eid)}\b", bug.body) is not None


def _release_ready(state: ProjectState) -> bool:
    if not state.features:
        return False
    accepted = [f for f in state.features if f.status == "accepted"]
    if not accepted:
        return False
    for f in state.features:
        if f.status != "cancelled" and f.status != "accepted":
            return False
    if state.open_bugs:
        return False
    if not state.prd or state.unchecked_evals():
        return False
    return all(not (pr.release_marker and pr.state == "open") for pr in state.prs)

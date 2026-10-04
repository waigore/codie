"""Invariant checks over a derived ProjectState (Technical Spec §5.4).

Derivation always succeeds — violations are reported, never fatal. Each
violation later gets `flag:needs-human` + one explanatory comment, and the
issue is excluded from dispatch until resolved.
"""

from __future__ import annotations

from codie.models import Bug, Feature, PRInfo, ProjectState, TaskItem, Violation
from codie.state import parse


def _v(number: int, code: str, message: str) -> Violation:
    return Violation(issue_number=number, code=code, message=message)


def validate(state: ProjectState) -> list[Violation]:
    violations: list[Violation] = []
    add = violations.append

    {i.number: i for i in _iter_issues(state)}

    # ---- 1. exactly one type:* and one status:* per managed issue (prd exempt) ----
    for number, labels in state.raw_labels.items():
        type_labels = [label for label in labels if label.startswith("type:")]
        status_labels = [label for label in labels if label.startswith("status:")]
        kind_labels = [label for label in labels if label.startswith("kind:")]
        priority_labels = [label for label in labels if label.startswith("priority:")]
        is_prd = "type:prd" in labels
        if len(type_labels) != 1:
            add(
                _v(
                    number,
                    "invalid-labels",
                    f"issue #{number} must carry exactly one type:* label, found {type_labels}",
                )
            )
        if not is_prd and len(status_labels) != 1:
            add(
                _v(
                    number,
                    "invalid-labels",
                    f"issue #{number} must carry exactly one status:* label, found {status_labels}",
                )
            )
        if is_prd and status_labels:
            add(
                _v(
                    number,
                    "invalid-labels",
                    f"the PRD issue (#{number}) must never carry a status:* label",
                )
            )
        # ---- 6. kind present iff type:task ----
        if "type:task" in labels and len(kind_labels) != 1:
            add(
                _v(
                    number,
                    "invalid-labels",
                    f"task #{number} must carry exactly one kind:* label, found {kind_labels}",
                )
            )
        if "type:task" not in labels and kind_labels:
            add(
                _v(
                    number,
                    "invalid-labels",
                    f"issue #{number} is not a task but carries kind:* label(s) {kind_labels}",
                )
            )
        # ---- 6b. at most one priority:* ----
        if len(priority_labels) > 1:
            add(
                _v(
                    number,
                    "invalid-labels",
                    f"issue #{number} carries more than one priority:* label: {priority_labels}",
                )
            )
        # ---- unknown codie-namespaced labels (§5.2) ----
        for unknown in parse.unknown_codie_labels(labels):
            add(
                _v(
                    number,
                    "unknown-label",
                    f"issue #{number} carries unknown codie label {unknown!r}",
                )
            )

    # ---- 2. Parent resolvable ----
    for issue in _iter_issues(state):
        if isinstance(issue, Feature):
            continue
        if issue.parent is not None and state.find_feature(issue.parent) is None:
            add(
                _v(
                    issue.number,
                    "bad-parent",
                    f"issue #{issue.number} names Parent: #{issue.parent} which is not a feature",
                )
            )

    # ---- 3. Depends on resolvable and acyclic ----
    graph: dict[int, list[int]] = {}
    for issue in _iter_issues(state):
        if not isinstance(issue, Feature):
            for dep in issue.depends_on:
                if state.find_task(dep) is None and state.find_bug(dep) is None and dep != issue.number:
                    add(
                        _v(
                            issue.number,
                            "bad-dependency",
                            f"issue #{issue.number} Depends on: #{dep} which is not a task/bug",
                        )
                    )
            graph[issue.number] = list(issue.depends_on)
    for number in graph:
        if _has_cycle(graph, number):
            add(
                _v(
                    number,
                    "dependency-cycle",
                    f"dependency graph contains a cycle involving #{number}",
                )
            )

    # ---- 4. task statuses vs feature status ----
    for task in state.tasks:
        if task.parent and (feature := state.find_feature(task.parent)):
            _check_task_feature_consistency(add, feature, task)
    for bug in state.bugs:
        if bug.parent and (feature := state.find_feature(bug.parent)):
            _check_task_feature_consistency(add, feature, bug)

    # ---- 4b. spec-PR invariants ----
    for feature in state.features:
        _check_spec_invariants(add, state, feature)

    # ---- 5. linked PR invariants ----
    for task in state.tasks:
        if task.status in {"in-review", "done"}:
            _check_pr_linked(
                add,
                state,
                task.status == "done",
                task.linked_pr,
                f"task #{task.number}",
                task.number,
            )
    for bug in state.bugs:
        if bug.status in {"in-review", "verifying", "done"}:
            _check_pr_linked(
                add,
                state,
                bug.status in {"verifying", "done"},
                bug.linked_pr,
                f"bug #{bug.number}",
                bug.number,
            )

    # ---- 7. evals exist in PRD ----
    known = state.eval_ids
    for feature in state.features:
        for eid in feature.evals:
            if eid not in known:
                add(
                    _v(
                        feature.number,
                        "unknown-eval",
                        f"feature #{feature.number} cites eval {eid} which is not in the PRD",
                    )
                )

    # ---- 8. exactly one PRD ----
    for number in state.other_prd_numbers:
        add(
            _v(
                state.prd.number if state.prd else number,
                "second-prd",
                f"duplicate type:prd issue #{number}",
            )
        )

    # ---- 9. acceptance readiness + dependency/cancellation rules ----
    for feature in state.features:
        if feature.status != "review":
            continue
        non_cancelled = [n for n in feature.tasks if (t := _task_or_bug(state, n)) and t.status != "cancelled"]
        if not non_cancelled:
            add(
                _v(
                    feature.number,
                    "acceptance-no-tasks",
                    f"feature #{feature.number} is status:review but has no non-cancelled task",
                )
            )
        for n in feature.tasks:
            item = _task_or_bug(state, n)
            if item and item.status not in {"done", "cancelled"}:
                add(
                    _v(
                        feature.number,
                        "acceptance-open-task",
                        f"feature #{feature.number} is status:review but child #{n} is {item.status}",
                    )
                )

    for issue in _iter_issues(state):
        if not isinstance(issue, Feature):
            for dep in issue.depends_on:
                target = _task_or_bug(state, dep)
                if target is None:
                    continue
                if target.status not in {"done", "cancelled"}:
                    add(
                        _v(
                            issue.number,
                            "unsatisfied-dependency",
                            f"issue #{issue.number} depends on #{dep} which is {target.status}",
                        )
                    )
                if target.status == "cancelled" and issue.status != "cancelled":
                    add(
                        _v(
                            issue.number,
                            "depends-on-cancelled",
                            f"issue #{issue.number} depends on cancelled #{dep}",
                        )
                    )

    # ---- Task-checklist conflict (M20) ----
    for feature in state.features:
        checklist = {n for _, n in parse.parse_tasks_checklist(feature.body)}
        derived = set(feature.tasks)
        if checklist and checklist != derived:
            add(
                _v(
                    feature.number,
                    "tasks-conflict",
                    f"feature #{feature.number} ## Tasks checklist {sorted(checklist)} differs from derived Parent: set {sorted(derived)}",  # noqa: E501
                )
            )

    return violations


def _iter_issues(state: ProjectState) -> list[TaskItem | Bug | Feature]:
    return [*state.features, *state.tasks, *state.bugs]  # type: ignore[return-value]


def _task_or_bug(state: ProjectState, number: int) -> TaskItem | Bug | None:
    return state.find_task(number) or state.find_bug(number)


def _has_cycle(graph: dict[int, list[int]], start: int) -> bool:
    visited: set[int] = set()

    def visit(node: int) -> bool:
        if node in visited:
            return True
        visited.add(node)
        for dep in graph.get(node, []):
            if visit(dep):
                return True
        visited.discard(node)
        return False

    return visit(start)


def _check_task_feature_consistency(add, feature: Feature, child: TaskItem | Bug) -> None:
    if feature.status in {"proposed", "speccing", "spec-review", "cancelled"}:
        if child.status not in {"cancelled"} and child.linked_pr:
            pass
        if child.status != "cancelled":
            add(
                _v(
                    child.number,
                    "status-inconsistency",
                    f"child #{child.number} is {child.status} under a {feature.status} feature",
                )
            )
    elif feature.status == "specified":
        if child.status != "cancelled":
            add(
                _v(
                    child.number,
                    "status-inconsistency",
                    f"child #{child.number} exists under a specified feature (breakdown not yet applied)",
                )
            )
    elif feature.status in {"review", "accepted"}:
        if child.status not in {"done", "cancelled"}:
            add(
                _v(
                    child.number,
                    "status-inconsistency",
                    f"child #{child.number} is {child.status} under an {feature.status} feature",
                )
            )


def _check_spec_invariants(add, state: ProjectState, feature: Feature) -> None:
    """§5.4 rule 4b: at most one open spec PR; speccing/spec-review validity."""
    feature_prs = [
        pr
        for pr in state.prs
        if pr.spec_marker
        and pr.number in (num for num in state.spec_pr_by_feature if state.spec_pr_by_feature[num] == pr.number)
    ]
    feature_prs = [
        pr
        for pr in state.prs
        if (pr.spec_marker or pr.linked_issue == feature.number) and _pr_belongs_to_feature(pr, feature, state)
    ]
    open_specs = [pr for pr in feature_prs if pr.state == "open"]
    if len(open_specs) > 1:
        add(
            _v(
                feature.number,
                "multiple-open-spec-prs",
                f"feature #{feature.number} has {len(open_specs)} open spec PRs",
            )
        )

    latest = _latest_spec_pr(feature_prs)
    if feature.status == "spec-review":
        if not open_specs:
            add(
                _v(
                    feature.number,
                    "spec-review-no-pr",
                    f"feature #{feature.number} is spec-review but has no open spec PR",
                )
            )
        else:
            for pr in open_specs:
                if pr.draft or pr.changes_requested:
                    add(
                        _v(
                            feature.number,
                            "spec-review-not-ready",
                            f"feature #{feature.number} is spec-review but its spec PR #{pr.number} is not ready-for-review",  # noqa: E501
                        )
                    )
    if feature.status in {"speccing", "spec-review"} and open_specs:
        for pr in open_specs:
            if pr.review_state == "changes_requested" and feature.status != "speccing":
                add(
                    _v(
                        feature.number,
                        "spec-cr-status",
                        f"feature #{feature.number} spec PR #{pr.number} has changes requested",
                    )
                )
    if feature.status in {"specified", "planned", "in-progress", "review"}:
        if latest is None:
            add(
                _v(
                    feature.number,
                    "no-merged-spec",
                    f"feature #{feature.number} is {feature.status} but has no merged spec PR",
                )
            )
        elif latest.state != "merged":
            add(
                _v(
                    feature.number,
                    "spec-not-merged",
                    f"feature #{feature.number} is {feature.status} but its latest spec PR #{latest.number} is unmerged",  # noqa: E501
                )
            )


def _pr_belongs_to_feature(pr, feature: Feature, state: ProjectState) -> bool:
    if pr.linked_issue == feature.number:
        return True
    return pr.number == state.spec_pr_by_feature.get(feature.number)


def _latest_spec_pr(prs) -> PRInfo | None:
    merged = [pr for pr in prs if pr.state == "merged"]
    if merged:
        return max(merged, key=lambda pr: pr.merged_at or pr.updated_at)
    return None


def _check_pr_linked(
    add,
    state: ProjectState,
    require_merged: bool,
    pr_number: int | None,
    label: str,
    issue_number: int,
) -> None:
    if pr_number is None:
        add(_v(issue_number, "no-linked-pr", f"{label} has no linked PR"))
        return
    pr = state.find_pr(pr_number)
    if pr is None:
        add(_v(issue_number, "no-linked-pr", f"{label} links missing PR #{pr_number}"))
        return
    if require_merged:
        if pr.state != "merged":
            add(
                _v(
                    pr_number,
                    "pr-not-merged",
                    f"{label} expects its linked PR #{pr_number} to be merged",
                )
            )
    else:
        if pr.state != "open":
            add(
                _v(
                    pr_number,
                    "pr-not-open",
                    f"{label} expects its linked PR #{pr_number} to be open",
                )
            )

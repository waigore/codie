"""Status reconciliation, cycle counting, stale recovery (Technical Spec §5.5, §5.8, §5.9).

Pure and deterministic: `plan_reconcile` returns the projected state (which the
queue runs on) plus the minimal GitHub mutations needed to realize it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from codie.config import Settings
from codie.models import Bug, Feature, ProjectState, TaskItem


class ReconcileMutations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set_labels: dict[int, list[str]] = Field(default_factory=dict)
    comments: dict[int, str] = Field(default_factory=dict)
    close: list[int] = Field(default_factory=list)
    reopen: list[int] = Field(default_factory=list)


TERMINAL_TASK = {"done", "cancelled"}
TERMINAL_FEATURE = {"accepted", "cancelled"}
OCCURS = {"in-progress", "in-review"}

WorkEntity = Feature | TaskItem | Bug


def _copy(state: ProjectState) -> ProjectState:
    return state.model_copy(deep=True)


# ---------------------------------------------------------------------------
# Cycle counting (§5.9)
# ---------------------------------------------------------------------------


def _status_history(state: ProjectState, number: int) -> list[str]:
    history: list[str] = []
    for ev in state.timeline.get(number, []):
        if ev.kind == "labeled" and ev.label and ev.label.startswith("status:"):
            history.append(ev.label[len("status:") :])
    return history


def _count_rounds(history: list[str], a: str, b: str) -> int:
    count = 0
    waiting = False
    for status in history:
        if status == a:
            waiting = True
        elif waiting and status == b:
            count += 1
            waiting = False
    return count


def _dispute_pair(state: ProjectState, number: int) -> tuple[str, str]:
    if state.find_feature(number) is not None:
        return "planner", "reviewer"
    if state.find_bug(number) is not None:
        return "coder", "tester"
    return "coder", "reviewer"


def count_cycles(state: ProjectState) -> dict[int, int]:
    """One per-item cycle counter for the items in scope (§5.9)."""
    counts: dict[int, int] = {}
    numbers = {f.number for f in state.features} | {t.number for t in state.tasks} | {b.number for b in state.bugs}
    for number in numbers:
        history = _status_history(state, number)
        total = 0
        total += _count_rounds(history, "in-review", "in-progress")  # review round
        total += _count_rounds(history, "spec-review", "speccing")  # spec-review round
        total += _count_rounds(history, "verifying", "in-progress")  # bug reopen round
        total += state.acceptance_failed_cycles.get(number, 0)  # failed acceptance runs
        total += _count_dispute_exchanges(state.dispute_comments.get(number, []), _dispute_pair(state, number))
        counts[number] = total
    return counts


def _count_dispute_exchanges(comments: list[tuple[str, str, str, str]], pair: tuple[str, str]) -> int:
    pair_a, pair_b = pair
    prev_actor: str | None = None
    count = 0
    for first_line, actor, _body, _ts in comments:
        if first_line not in {"**Dispute:**", "**Concede:**"}:
            continue
        if actor not in {pair_a, pair_b}:
            prev_actor = actor
            continue
        if prev_actor is not None and actor != prev_actor and prev_actor in {pair_a, pair_b}:
            count += 1
        prev_actor = actor
    return count


# ---------------------------------------------------------------------------
# Canonical status computation (§5.5)
# ---------------------------------------------------------------------------


def deps_satisfied(state: ProjectState, item: TaskItem | Bug) -> bool:
    for dep in item.depends_on:
        target = state.find_task(dep) or state.find_bug(dep)
        if target is None or target.status not in TERMINAL_TASK:
            return False
    return True


def canonical_task_status(state: ProjectState, task: TaskItem | Bug) -> str:
    if task.status == "backlog" and deps_satisfied(state, task) and "blocked" not in task.flags:
        return "ready"
    return task.status


def feature_spec_prs(feature: Feature, state: ProjectState) -> list:
    numbers = set()
    if feature.spec_pr_number:
        numbers.add(feature.spec_pr_number)
    sb = state.spec_pr_by_feature.get(feature.number)
    if sb:
        numbers.add(sb)
    return [pr for pr in state.prs if pr.number in numbers and (pr.spec_marker or pr.number in numbers)]


def canonical_feature_status(feature: Feature, state: ProjectState) -> str:
    if feature.status in {"accepted", "cancelled", "revised"}:
        return feature.status
    if feature.status == "proposed":
        return "proposed"
    prs = feature_spec_prs(feature, state)
    open_prs = [pr for pr in prs if pr.state == "open"]
    has_merged = any(pr.state == "merged" for pr in prs)
    open_ready = any(pr.state == "open" and not pr.draft and not pr.changes_requested for pr in open_prs)
    open_unready = any(pr.state == "open" and (pr.draft or pr.changes_requested) for pr in open_prs)
    if open_ready:
        return "spec-review"
    if open_unready:
        return "speccing"
    if has_merged:
        if feature.status in {"planned", "in-progress", "review"}:
            return feature.status
        if feature.tasks:
            return "planned"
        return "specified"
    return "speccing"


# ---------------------------------------------------------------------------
# Stale recovery (§5.8)
# ---------------------------------------------------------------------------


def stale_resets(state: ProjectState, active_runs: set[str], timeout_minutes: int, now: datetime) -> dict[int, str]:
    """items → explanatory comment for in-progress work whose heartbeat went stale."""
    resets: dict[int, str] = {}
    timeout_s = timeout_minutes * 60
    for issue_item in _all_items(state).values():
        if isinstance(issue_item, Feature) or issue_item.status != "in-progress":
            continue
        age = _staleness(state, issue_item.number, now)
        run_id = _run_id(state, issue_item.number)
        if age is not None and age > timeout_s and (run_id is None or run_id not in active_runs):
            resets[issue_item.number] = (
                f"Stale-run recovery: `{issue_item.title}` was `in-progress` but produced no heartbeat for "
                f"{timeout_minutes} minutes; reset to `ready`. Resume finds the existing branch/PR."
            )
    return resets


def timedelta_minutes(minutes: int):
    from datetime import timedelta

    return timedelta(minutes=minutes)


def _run_id(state: ProjectState, number: int) -> str | None:
    hb = state.latest_heartbeats.get(number)
    if not hb:
        return None
    return hb.split()[0] if hb.split() else None


def _staleness(state: ProjectState, number: int, now: datetime) -> float | None:
    """Age in seconds of the newest heartbeat, or of the in-progress label event."""
    hb = state.latest_heartbeats.get(number)
    if hb:
        parts = hb.split()
        if len(parts) >= 2:
            try:
                ts = datetime.fromisoformat(parts[1])
            except ValueError:
                ts = now
            return max(0.0, (now - ts).total_seconds())
    # fall back to the in-progress label event time
    label_ts: datetime | None = None
    for ev in state.timeline.get(number, []):
        if ev.kind == "labeled" and ev.label == "status:in-progress":
            label_ts = ev.created_at
    if label_ts is None:
        return None
    return max(0.0, (now - label_ts).total_seconds())


# ---------------------------------------------------------------------------
# Reconcile plan
# ---------------------------------------------------------------------------


def plan_reconcile(
    state: ProjectState,
    settings: Settings,
    now: datetime,
    active_runs: set[str] | None = None,
    comment_log: dict[int, list[str]] | None = None,
) -> tuple[ReconcileMutations, ProjectState]:
    """Compute the projected state and the GitHub mutations to realize it."""
    from codie.state.validate import validate

    active_runs = active_runs or set()
    comment_log = comment_log or {}
    projected = _copy(state)

    # ---- 1. canonical statuses ----
    for feature in projected.features:
        canonical = canonical_feature_status(feature, projected)
        if feature.status == "planned" and canonical == "planned":
            children = [projected.find_work_item(n) or projected.find_feature(n) for n in feature.tasks]
            if any(
                c is not None and getattr(c, "status", None) not in {"backlog", "cancelled"}
                for c in children
                if not isinstance(c, Feature)
            ):
                canonical = "in-progress"
        if feature.status != canonical:
            feature.status = canonical  # type: ignore[assignment]
    for task in projected.tasks:
        canonical = canonical_task_status(projected, task)
        if task.status != canonical:
            task.status = canonical  # type: ignore[assignment]
    for bug in projected.bugs:
        canonical = canonical_task_status(projected, bug)
        if bug.status != canonical:
            bug.status = canonical  # type: ignore[assignment]

    # ---- 2. violations ----
    violations = validate(projected)
    mutations = ReconcileMutations()
    flagged: set[int] = set()
    for violation in violations:
        if violation.issue_number in flagged or violation.issue_number == 0:
            continue
        _set_flag(projected, violation.issue_number, "needs-human")
        flagged.add(violation.issue_number)
        body = f"Violation `{violation.code}`: {violation.message} — added `flag:needs-human`; the issue is excluded from dispatch."  # noqa: E501
        mutations.comments[violation.issue_number] = body

    # ---- 3. cycle-cap escalation (§5.9) ----
    cycles = count_cycles(projected)
    projected.cycle_counts = cycles
    max_cycles = settings.guardrails.max_cycles_per_work_item
    for number, count in cycles.items():
        ent: WorkEntity | None = projected.find_work_item(number) or projected.find_feature(number)
        if ent is None or _is_terminal(ent):
            continue
        if count > max_cycles:
            _set_flag(projected, number, "needs-human")
            summary = _deadlock_summary(projected, number)
            mutations.comments[number] = summary

    # ---- 4. deferral escalation (§6.1) ----
    max_defer = settings.guardrails.max_defer_cycles
    for number, count in projected.deferral_counts.items():
        if count >= max_defer:
            _set_flag(projected, number, "needs-human")
            if number not in mutations.comments:
                mutations.comments[number] = (
                    f"Repeated deferral escalation: this work item has been deferred {count} times "
                    f"(max {max_defer}); escalated to a human via `flag:needs-human`."
                )

    # ---- 5. stale recovery (§5.8) ----
    timeout = settings.guardrails.heartbeat_timeout_minutes
    resets = stale_resets(projected, active_runs, timeout, now)
    for number, comment in resets.items():
        stale_item = projected.find_work_item(number)
        if stale_item is None:
            continue
        stale_item.status = "ready"  # type: ignore[assignment]
        # clear flag:blocked only when it names the orphaned run (§5.8)
        if _blocks_orphaned_run(projected, number, _run_id(projected, number)):
            stale_item.flags.discard("blocked")  # type: ignore[union-attr]
            comment += "\n\nCleared `flag:blocked` (set by the orphaned run)."
        mutations.comments[number] = comment

    # ---- 6. issue open/close lifecycle (§5.5) ----
    for number, entity in _all_items(projected).items():
        terminal = entity.status in TERMINAL_TASK or (
            isinstance(entity, Feature) and entity.status in TERMINAL_FEATURE
        )
        opening = getattr(entity, "open", True)
        if terminal and opening:
            mutations.close.append(number)
        if not terminal and not opening:
            mutations.reopen.append(number)

    # ---- 7. label diff ----
    for number in [
        *projected.raw_labels.keys(),
        *[i.number for i in _all_items(projected).values()],
    ]:
        current = projected.raw_labels.get(number, [])
        target = canonical_label_set(projected, number)
        if _codie_labels(current) != _codie_labels(target):
            mutations.set_labels[number] = target

    return mutations, projected


def _blocks_orphaned_run(state: ProjectState, number: int, orphaned_run_id: str | None) -> bool:
    """True when the issue's flag:blocked was set by the orphaned run (§5.8)."""
    if orphaned_run_id is None:
        return False
    return orphaned_run_id in _blocked_run_ids(state, number)


def _blocked_run_ids(state: ProjectState, number: int) -> set[str]:
    """Collect run ids named in `<!-- codie:blocked <run_id> <actor> -->` timeline comments."""
    from codie.state import parse

    ids: set[str] = set()
    for ev in state.timeline.get(number, []):
        if ev.kind != "commented":
            continue
        payload = ev.payload or {}
        run_id, _actor = parse.blocked_run(str(payload.get("body", "")))
        if run_id:
            ids.add(run_id)
    return ids


def _all_items(projected: ProjectState) -> dict[int, WorkEntity]:
    out: dict[int, WorkEntity] = {}
    out.update({feature.number: feature for feature in projected.features})
    out.update({item.number: item for item in projected.tasks})
    out.update({item.number: item for item in projected.bugs})
    return out


def _is_terminal(item: object) -> bool:
    status = getattr(item, "status", None)
    if isinstance(item, Feature):
        return status in TERMINAL_FEATURE
    return status in TERMINAL_TASK


def _set_flag(projected: ProjectState, number: int, flag: str | None = None) -> None:
    item = projected.find_feature(number) or projected.find_work_item(number)
    if item is None or flag is None:
        return
    item.flags.add(flag)  # type: ignore[attr-defined, arg-type]


def _clear_flag(projected: ProjectState, number: int, flag: str) -> None:
    item = projected.find_work_item(number)
    if item is not None:
        item.flags.discard(flag)  # type: ignore[attr-defined]


def canonical_label_set(projected: ProjectState, number: int) -> list[str]:
    """The full target label list (preserving non-codie labels)."""
    item = projected.find_feature(number) or projected.find_work_item(number)
    if item is None:
        return projected.raw_labels.get(number, [])
    labels: list[str] = []
    if isinstance(item, Feature):
        labels = ["type:feature", f"status:{item.status}"]
    elif isinstance(item, TaskItem):
        labels = ["type:task", f"status:{item.status}", f"kind:{item.kind}"]
    elif isinstance(item, Bug):
        labels = ["type:bug", f"status:{item.status}"]
    else:
        return projected.raw_labels.get(number, [])
    labels.append(f"priority:{item.priority}")  # type: ignore[attr-defined]
    for flag in sorted(item.flags):  # type: ignore[attr-defined]
        labels.append(f"flag:{flag}")
    preserved = [label for label in projected.raw_labels.get(number, []) if not _is_codie(label)]
    return list(dict.fromkeys([*labels, *preserved]))


def _codie_labels(labels: list[str]) -> set[str]:
    return {label for label in labels if _is_codie(label)}


def _is_codie(label: str) -> bool:
    return label.startswith(("type:", "status:", "kind:", "priority:", "flag:"))


def _deadlock_summary(state: ProjectState, number: int) -> str:
    item = state.find_work_item(number) or state.find_feature(number)
    title = item.title if item else f"#{number}"
    lines = [
        f"**Deadlock escalation** for `{title}` (#{number}): the per-item cycle counter "
        f"({state.cycle_counts.get(number, 0)} completed cycles) exceeded "
        f"`guardrails.max_cycles_per_work_item`.",
    ]
    for _line, actor, body, _ts in state.dispute_comments.get(number, []):
        lines.append(f"- **{actor}** last said: `{body[:400]}`")
    pr = None
    linked_pr = getattr(item, "linked_pr", None) if item is not None else None
    if linked_pr is not None:
        pr = state.find_pr(int(linked_pr))
    if pr is not None:
        reviews = getattr(pr, "reviews", []) or []
        latest = max(reviews, key=lambda r: r.submitted_at, default=None) if reviews else None
        if latest is not None:
            lines.append(f"- **{latest.user_login}** last review: `{latest.body[:400] or latest.state}`")
    lines.append(
        "Resolve by commenting the decision and/or adjusting labels and the plan, then remove `flag:needs-human`."
    )
    return "\n".join(lines)


def now_utc() -> datetime:
    return datetime.now(UTC)

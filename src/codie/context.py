"""Context-pack assembly per work item (Technical Spec §7.1).

One work item = one fresh crew, context assembled from GitHub/workspace data,
never remembered. Token budgets bound the pack; anything omitted is listed
explicitly in `omissions` (never silent truncation).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codie.config import Settings
from codie.models import Feature, ProjectState, WorkItem


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class ContextPack:
    role: str
    work_item: WorkItem | None = None
    sections: list[tuple[str, str]] = field(default_factory=list)
    omissions: list[dict] = field(default_factory=list)
    token_budget: int = 24_000

    @property
    def token_estimate(self) -> int:
        return sum(estimate_tokens(body) for _, body in self.sections)

    def add(self, title: str, body: str) -> None:
        self.sections.append((title, body))

    def render(self) -> str:
        parts = []
        for title, body in self.sections:
            if not body:
                continue
            parts.append(f"# {title}\n\n{body}")
        if self.omissions:
            parts.append(f"# Omitted (over token budget)\n\n{self.omissions}")
        return "\n\n".join(parts)


def conventions_digest(settings: Settings, workspace=None, budget: int = 8000) -> str:
    """The repo AGENTS.md + CONTRIBUTING.md digest (§7.1)."""
    parts: list[str] = []
    if workspace is None:
        return "".join(parts)
    for path in ("AGENTS.md", "CONTRIBUTING.md"):
        content = workspace.read_file(path)
        if content:
            parts.append(f"## {path}\n{content}")
        else:
            parts.append(f"## {path}\n(absent)")
    text = "\n\n".join(parts)
    if estimate_tokens(text) > budget:
        chars = budget * 4
        text = text[:chars] + f"\n...[digest truncated to {budget} tokens]"
    return text


def build_context(
    role: str,
    state: ProjectState,
    settings: Settings,
    item: WorkItem | None = None,
    workspace=None,
    prd_extra: dict | None = None,
) -> ContextPack:
    """Assemble the pack for one dispatch. `role` ∈ orchestrator/planner/coder/..."""
    pack = ContextPack(role=role, work_item=item, token_budget=settings.pack_budget(role))

    if role == "orchestrator":
        pack.add("Project state board", board_text(state))
        pack.add("Work queue", queue_text(_queue_for(state, settings)))
        pack.add("Confirmed conventions", conventions_digest(settings, workspace))
        pack.add("Violations", describe_violations(state))
        pack.add("Anomalies", describe_anomalies(state))
        pack.add("Budgets", budget_text(settings))
        return pack

    if role == "planner":
        if state.prd:
            pack.add("PRD", f"# {state.prd.title}\n\n{state.prd.body}")
        pack.add("Feature / task graph", _graph_text(state))
        pack.add("Conventions", conventions_digest(settings, workspace))
        if item and item.issue_number and (feature := state.find_feature(item.issue_number)):
            pack.add("Draft/merged specs", _spec_text(state, feature, workspace))
        if item and item.kind == "AssessRevision":
            pack.add("Human feedback", _human_feedback(state, item.issue_number))
        return pack

    if role == "coder":
        if item and item.issue_number:
            task = state.find_work_item(item.issue_number)
            if task:
                pack.add("Task issue", f"#{task.number} {task.title}\n\n{task.body}")
                pack.add(
                    "Done when",
                    "\n".join(
                        f"- [{'x' if d.checked else ' '}] {d.text}"
                        + (f" ({', '.join(d.requirement_ids)})" if d.requirement_ids else "")
                        for d in (getattr(task, "done_when", []) or [])
                    ),
                )
            if task and task.parent and (feature := state.find_feature(task.parent)):
                pack.add("Feature specs (approved, whole)", _spec_text(state, feature, workspace))
        pack.add("Conventions", conventions_digest(settings, workspace))
        return pack

    if role == "reviewer":
        if item and item.pr_number and (pr := state.find_pr(item.pr_number)):
            pack.add("PR", f"#{pr.number} {pr.title}\n\nbase: {pr.base} ← head: {pr.head}\n\n{pr.body}")
        if item and item.issue_number and (task := state.find_work_item(item.issue_number)):
            if task.parent and (feature := state.find_feature(task.parent)):
                pack.add(
                    "Technical spec (approved)",
                    _spec_text(state, feature, workspace, only="technical-spec.md"),
                )
            pack.add("Task Done when", "\n".join(d.text for d in (getattr(task, "done_when", []) or [])))
        pack.add("Conventions", conventions_digest(settings, workspace))
        return pack

    if role == "tester":
        if (
            item
            and item.issue_number
            and (
                (feature := state.find_feature(item.issue_number)) is not None
                or (
                    (task := state.find_work_item(item.issue_number)) is not None
                    and task.parent
                    and (feature := state.find_feature(task.parent)) is not None
                )
            )
        ):
            pack.add("Feature spec (approved)", _spec_text(state, feature, workspace, only="feature-spec.md"))
        if state.prd:
            pack.add("Evals", "\n".join(f"- [{'x' if e.checked else ' '}] {e.id}: {e.text}" for e in state.prd.evals))
        pack.add("Commands", _commands_text(settings))
        pack.add("Recent merges", describe_recent_merges(state))
        return pack

    if role == "surveyor":
        pack.add("Repository survey brief", conventions_digest(settings, workspace))
        pack.add("Open issues", describe_anomalies(state))
        return pack

    return pack


def _queue_for(state: ProjectState, settings: Settings) -> list[WorkItem]:
    from codie.queue import compute_queue

    return compute_queue(state, settings)


def board_text(state: ProjectState) -> str:
    lines = [f"repo: {state.repo}"]
    if state.prd:
        done = sum(1 for e in state.prd.evals if e.checked)
        lines.append(f"PRD #{state.prd.number}: {done}/{len(state.prd.evals)} evals checked")
    for f in state.features:
        lines.append(f"feature #{f.number} {f.title!r}: {f.status}")
    for t in state.tasks:
        lines.append(f"task #{t.number} {t.title!r}: {t.status} (kind:{t.kind})")
    for b in state.bugs:
        lines.append(f"bug #{b.number} {b.title!r}: {b.status}")
    return "\n".join(lines)


def queue_text(items: list[WorkItem]) -> str:
    if not items:
        return "(queue empty)"
    return "\n".join(f"{i.rule} {i.kind} → {i.role} — {i.entity} (pri {i.priority})" for i in items)


def describe_violations(state: ProjectState) -> str:
    if not state.violations:
        return "(none)"
    return "\n".join(f"- #{v.issue_number} [{v.code}]: {v.message}" for v in state.violations)


def describe_anomalies(state: ProjectState) -> str:
    if not state.anomalies:
        return "(none)"
    return "\n".join(f"- {a.kind} (#{a.issue_number}): {a.message}" for a in state.anomalies)


def budget_text(settings: Settings) -> str:
    cap = settings.budgets.max_llm_cost_usd_per_day
    return f"daily cap ${cap:g} USD; per-run cap ${settings.per_run_cap:g}"


def describe_recent_merges(state: ProjectState) -> str:
    merged = [pr for pr in state.prs if pr.state == "merged" and not pr.release_marker]
    if not merged:
        return "(none)"
    return "\n".join(
        f"- #{pr.number} {pr.title} → {pr.base}"
        for pr in sorted(merged, key=lambda p: p.merged_at or p.updated_at, reverse=True)[:10]
    )


def _graph_text(state: ProjectState) -> str:
    lines: list[str] = []
    for f in state.features:
        lines.append(f"feature #{f.number} {f.title}: {f.status} evals={f.evals}")
        for t in state.tasks:
            if t.parent == f.number:
                lines.append(f"  task #{t.number} {t.title}: {t.status} deps={t.depends_on}")
        for b in state.bugs:
            if b.parent == f.number:
                lines.append(f"  bug #{b.number} {b.title}: {b.status}")
    return "\n".join(lines)


def _spec_text(state: ProjectState, feature: Feature, workspace, only: str | None = None) -> str:
    """Read the approved spec files from the integration-branch checkout."""
    if workspace is None:
        return "(workspace unavailable)"
    parts: list[str] = []
    for filename in ("feature-spec.md", "technical-spec.md"):
        if only and filename != only:
            continue
        path = None
        for p in feature.spec_paths:
            if p.endswith(filename):
                path = p
                break
        if path is None and feature.number:
            # spec paths may be absent from the issue body; derive from merged PRs is done upstream.
            path = f"specs/{feature.number}-*/{filename}"
        content = workspace.read_file(path)
        parts.append(f"## {filename}\n{content or '(missing at workspace — see merged spec PR)'}")
    return "\n\n".join(parts)


def _human_feedback(state: ProjectState, issue_number: int | None) -> str:
    comments = state.dispute_comments.get(issue_number or 0, [])
    if not comments:
        return "(no feedback comments recorded)"
    return "\n".join(f"- {actor}: {first} {body[:300]}" for first, actor, body, _ in comments)


def _commands_text(settings: Settings) -> str:
    return (
        "\n".join(f"- {name}: {' '.join(cmd)}" for name, cmd in settings.overrides.commands.items())
        or "(none configured)"
    )

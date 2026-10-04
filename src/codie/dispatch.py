"""Work item → crew construction → kickoff → result handling (Tech Spec §5.7, §7.2).

Roles with structured outputs (the Planner family) return pydantic-validated
payloads that the kernel applies. Tool-acting roles apply their own writes and
report expected mutations for post-hoc verification.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from codie.config import Settings
from codie.context import ContextPack
from codie.models import (
    PAYLOAD_KINDS,
    FeaturePlan,
    PlanReconciliation,
    ReleasePlan,
    RevisionAssessment,
    TaskBreakdown,
    WorkItem,
)

try:  # pragma: no cover - crewai is a runtime dependency
    from crewai import Agent, Crew, Process, Task
except Exception:  # pragma: no cover
    Agent = Crew = Process = Task = None  # type: ignore[assignment, misc]


# ---------------------------------------------------------------------------
# Crew runners
# ---------------------------------------------------------------------------


@dataclass
class CrewResult:
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    completed: bool = True
    raw: Any = None


class CrewRunner(Protocol):
    def run_structured(
        self,
        role: str,
        item: WorkItem,
        pack: ContextPack,
        settings: Settings,
        output_model: type | None = None,
    ) -> CrewResult: ...
    def run_tool_agent(
        self,
        role: str,
        item: WorkItem,
        pack: ContextPack,
        settings: Settings,
        tools: list[Any] | None = None,
        tool_inputs: dict[str, Any] | None = None,
    ) -> CrewResult: ...


class CrewAIKickoff(CrewRunner):
    """Real CrewAI runner (lazy; requires CODIE_LLM_API_KEY and crewai installed)."""

    def __init__(self, settings: Settings, prompt_dir=None):
        self.settings = settings
        self.prompt_dir = prompt_dir

    def _prompt(self, role: str) -> str:
        from codie.roles.prompts import load_prompt, shared_note

        return f"{shared_note()}\n\n{load_prompt(role, dir_=self.prompt_dir)}"

    def run_structured(self, role, item, pack, settings, output_model=None) -> CrewResult:
        from codie.llm import build_llm, role_max_iter

        llm = build_llm(settings, role)
        prompt = self._prompt(role)
        custom = "\n".join(f"## {t}\n{b}" for t, b in pack.sections)
        task = Task(
            description=f"{prompt}\n\nContext:\n{custom}",
            expected_output="The structured result object",
            agent=None,
        )  # type: ignore[arg-type]
        agent = Agent(
            role=role.capitalize(),
            goal=prompt,
            backstory=f"You are the {role} of the codie crew.",
            llm=llm,
            allow_code_execution=False,
            max_iter=role_max_iter(role),
            verbose=False,
        )  # type: ignore[call-arg]
        task.agent = agent
        crew = Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            memory=False,
            respect_context_window=True,
        )  # type: ignore[call-arg]
        result = crew.kickoff()
        text = result.raw if hasattr(result, "raw") else str(result)
        tokens = _extract_usage(result)
        return CrewResult(text=text, input_tokens=tokens[0], output_tokens=tokens[1], raw=result)

    def run_tool_agent(self, role, item, pack, settings, tools=None, tool_inputs=None) -> CrewResult:
        from codie.llm import build_llm, role_max_iter

        llm = build_llm(settings, role)
        prompt = self._prompt(role)
        custom = "\n".join(f"## {t}\n{b}" for t, b in pack.sections)
        agent = Agent(
            role=role.capitalize(),
            goal=prompt,
            backstory=f"You are the {role} of the codie crew.",
            llm=llm,
            tools=tools or [],
            allow_code_execution=False,
            max_iter=role_max_iter(role),
            verbose=False,
        )  # type: ignore[call-arg]
        task = Task(
            description=f"{prompt}\n\nContext:\n{custom}",
            expected_output="Confirmation of completed work and summary",
            agent=agent,
        )  # type: ignore[arg-type]
        crew = Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            memory=False,
            respect_context_window=True,
        )  # type: ignore[call-arg]
        result = crew.kickoff()
        text = result.raw if hasattr(result, "raw") else str(result)
        tokens = _extract_usage(result)
        return CrewResult(text=text, input_tokens=tokens[0], output_tokens=tokens[1], raw=result)


class StubRunner(CrewRunner):
    """Deterministic runner for tests — returns scripted structured output."""

    def __init__(self, handler):
        self.handler = handler  # (role, item, pack) -> str

    def run_structured(self, role, item, pack, settings, output_model=None) -> CrewResult:
        return CrewResult(text=self.handler(role, item, pack))

    def run_tool_agent(self, role, item, pack, settings, tools=None, tool_inputs=None) -> CrewResult:
        return CrewResult(text=self.handler(role, item, pack))


def _extract_usage(result: Any) -> tuple[int, int]:
    try:
        summary = result.usage if hasattr(result, "usage") else {}
        input_tokens = int(getattr(summary, "input_tokens", 0) or 0)
        output_tokens = int(getattr(summary, "output_tokens", 0) or 0)
        return input_tokens, output_tokens
    except Exception:
        return 0, 0


# ---------------------------------------------------------------------------
# Structured-output validation
# ---------------------------------------------------------------------------


def parse_payload(item: WorkItem, text: str) -> tuple[Any | None, str]:
    """Parse the crew's text into the item's payload model. Returns (payload, error)."""
    model_cls = PAYLOAD_KINDS.get(item.kind)
    if model_cls is None:
        return None, ""
    cleaned = _extract_json(text)
    if cleaned is None:
        return None, f"could not parse JSON structured output for {item.kind}"
    try:
        payload = model_cls.model_validate(cleaned)
        return payload, ""
    except ValidationError as exc:
        return None, f"structured output failed validation: {exc}"


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of LLM text (fenced or bare)."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Kernel-side application of structured plannar outputs (§7.2: proposes→applies)
# ---------------------------------------------------------------------------


class BodyRejected(Exception):
    """body_markdown contradicts the structured fields (M14)."""


def render_feature_body(draft) -> str:
    """Render a feature issue body from structured fields; reject contradictions."""
    body = (draft.body_markdown or "").rstrip()
    markers = {}
    for line in body.splitlines():
        m = re.match(r"^\*{0,2}(Evals)\*{0,2}:\s*(.*)$", line.strip())
        if m:
            markers["evals"] = m.group(2)
    if markers.get("evals"):
        structured = ", ".join(draft.evals) if draft.evals else "(none)"
        if structured and sorted(re.findall(r"\bE\d+\b", markers["evals"])) != sorted(draft.evals):
            raise BodyRejected("feature body Evals line contradicts structured evals")
    parts = [body]
    if "evals" not in markers:
        parts.append(f"**Evals:** {', '.join(draft.evals)}")
    parts.append(f"**PRD sections:** {draft.prd_sections}" if draft.prd_sections else "")
    parts.append("## Tasks\n- [ ] (none yet)")
    return "\n\n".join(p for p in parts if p)


def render_task_body(spec, parent: int | None, issue_number: int | None, refs: dict[str, int]) -> str:
    """Render a task/bug issue body from structured fields; reject contradictions."""
    body = (spec.body_markdown or "").rstrip()
    parents = re.findall(r"^\*{0,2}Parent\*{0,2}:\s*#(\d+)", body, re.MULTILINE)
    if parents and issue_number is not None and (parent is None or int(parents[0]) != parent):
        raise BodyRejected("task body Parent: contradicts structured parent")
    deps_lines = re.findall(r"^\*{0,2}Depends on\*{0,2}:\s*(.+)$", body, re.MULTILINE)
    structured_deps = [refs.get(d, d) if isinstance(d, str) else d for d in spec.depends_on]
    if deps_lines:
        parsed = [int(n) for n in re.findall(r"#(\d+)", deps_lines[0])]
        if parsed and parsed != structured_deps:
            raise BodyRejected("task body Depends on: contradicts structured depends_on")
    parts = [body]
    if not parents and parent is not None:
        parts.append(f"**Parent:** #{parent}")
    if not deps_lines and structured_deps:
        parts.append(f"**Depends on:** {', '.join('#' + str(d) for d in structured_deps)}")
    return "\n\n".join(p for p in parts if p)


def apply_feature_plan(client, settings, plan: FeaturePlan) -> tuple[list[int], list[str]]:
    """Create feature issues from a validated FeatureIssueDraft list (§7.2)."""
    created: list[int] = []
    errors: list[str] = []
    toolbox = None
    from codie.tools.github_tools import GithubToolbox

    toolbox = GithubToolbox(client, settings, "kernel")
    for draft in plan.features:
        try:
            body = render_feature_body(draft)
        except BodyRejected as exc:
            errors.append(f"feature {draft.title!r}: {exc}")
            continue
        number = toolbox.create_issue(
            draft.title, body, ["type:feature", "status:proposed", f"priority:{draft.priority}"]
        )
        created.append(number)
    f"Decomposed {len(created)} feature(s) from the PRD." + (f" Rejected: {errors}" if errors else "")
    if toolbox is not None:
        pass
    return created, errors


def apply_task_breakdown(
    client,
    settings,
    feature_number: int,
    breakdown: TaskBreakdown,
    state,
    resolved_spec_ids: set[str],
) -> tuple[list[int], list[str]]:
    """Create tasks for a breakdown, in dependency order (§7.2 breakdown validation)."""
    from codie.tools.github_tools import GithubToolbox

    toolbox = GithubToolbox(client, settings, "kernel")
    created: list[int] = []
    errors: list[str] = []
    refs: dict[str, int] = {}
    pending = list(breakdown.tasks)
    guard = 0
    while pending and guard < 100:
        guard += 1
        progressed = False
        for spec in list(pending):
            deps = [refs[d] if isinstance(d, str) else d for d in spec.depends_on]
            if any(isinstance(d, str) and d not in refs for d in spec.depends_on):
                continue
            try:
                number = _create_task(toolbox, settings, feature_number, spec, deps, resolved_spec_ids)
            except BodyRejected as exc:
                errors.append(f"task {spec.ref} ({spec.title!r}): {exc}")
                pending.remove(spec)
                continue
            except ValueError as exc:
                errors.append(f"task {spec.ref} ({spec.title!r}): {exc}")
                pending.remove(spec)
                continue
            refs[spec.ref] = number
            created.append(number)
            pending.remove(spec)
            progressed = True
        if not progressed:
            for spec in pending:
                errors.append(f"task {spec.ref} ({spec.title!r}): unsatisfiable dependency cycle")
            break
    return created, errors


def _citations_for_spec(spec) -> list[str]:
    return [
        rid
        for m in re.finditer(r"^- \[(?: |x)\].+?\(([A-Z0-9,\-\s]+)\)\s*$", spec.body_markdown, re.MULTILINE)
        for rid in re.findall(r"\b(?:FR|NFR|AC)-\d+\b", m.group(1))
    ]


def _create_task(toolbox, settings, feature_number: int, spec, deps: list[int], resolved_spec_ids: set[str]) -> int:

    if spec.kind not in {"dev", "integration", "test"}:
        raise ValueError(f"invalid kind {spec.kind!r}")
    cited = _citations_for_spec(spec)
    if not cited:
        raise ValueError("a done-when item must cite at least one requirement ID (§9.3)")
    for rid in cited:
        if rid not in resolved_spec_ids:
            raise ValueError(f"task cites {rid} which is not a live requirement ID in the merged specs")
    status = "backlog" if deps else "ready"
    body = render_task_body(spec, feature_number, None, {})
    labels = ["type:task", f"status:{status}", f"kind:{spec.kind}", "priority:medium"]
    return toolbox.create_issue(spec.title, body, labels)


def apply_reconcile_plan(client, settings, plan: PlanReconciliation) -> None:
    """Apply PlanReconciliation: create/cancel/restale features (§7.2)."""
    apply_feature_plan(client, settings, FeaturePlan(features=plan.add))

    for number in plan.cancel_feature_numbers:
        issue = client.get_issue(number)
        if issue is None:
            continue
        labels = [label for label in issue.labels if not label.startswith("status:")]
        labels.append("status:cancelled")
        client.set_labels(number, labels)
    for number in plan.restale_feature_numbers:
        issue = client.get_issue(number)
        if issue is None:
            continue
        labels = list(issue.labels)
        if "flag:needs-human" not in labels:
            labels.append("flag:needs-human")
        client.set_labels(number, labels)
        client.comment(
            number,
            "PRD changed and this feature is now stale — reconciled by the kernel; please review (flag:needs-human).",
        )


def apply_revision_assessment(client, settings, feature_number: int, assessment: RevisionAssessment) -> None:
    """Post the `codie:revision <kind>` marker (§5.6 rule 3)."""
    from codie.tools.github_tools import GithubToolbox

    GithubToolbox(client, settings, "kernel").comment(
        feature_number,
        f"<!-- codie:revision {assessment.kind} -->\n\nRevision assessment: **{assessment.kind}** — {assessment.rationale}",  # noqa: E501
    )


def apply_replan(client, settings, feature_number: int, replan, state, resolved_spec_ids: set[str]) -> None:
    """Cancel obsolete tasks, create new tasks, set feature planned (§5.5)."""
    from codie.state import parse as _parse
    from codie.tools.github_tools import GithubToolbox

    GithubToolbox(client, settings, "kernel")
    for number in replan.cancel_task_numbers:
        issue = client.get_issue(number)
        if issue is None:
            continue
        client.set_labels(
            number,
            [label for label in issue.labels if not label.startswith("status:")] + ["status:cancelled"],
        )
        for p in client.list_prs():
            linked, _ = _parse.linked_issue_numbers(p.body)
            if linked and linked[0] == number and p.state == "open":
                client.close_pr(p.number)
    created, errors = apply_task_breakdown(
        client,
        settings,
        feature_number,
        TaskBreakdown(tasks=replan.new_tasks),
        state,
        resolved_spec_ids,
    )
    client.set_labels(feature_number, _status_labels(client, feature_number, "planned"))
    if errors:
        client.comment(feature_number, "Replan rejected some tasks: " + "; ".join(errors))


def _status_labels(client, number: int, status: str) -> list[str]:
    issue = client.get_issue(number)
    if issue is None:
        return [f"status:{status}"]
    return [label for label in issue.labels if not label.startswith("status:")] + [f"status:{status}"]


def validate_spec_files(feature_spec: str, technical_spec: str) -> tuple[bool, list[str]]:
    """Validate both spec files against §9.2 headings + requirement-ID grammar (§7.2, C17)."""
    import codie.state.parse as parse

    errors: list[str] = []
    for filename, content in (
        ("feature-spec.md", feature_spec),
        ("technical-spec.md", technical_spec),
    ):
        if content is None:
            errors.append(f"{filename} missing")
            continue
        if not content.strip():
            errors.append(f"{filename} is empty")
    if feature_spec:
        headings = [
            line.strip().lstrip("#").strip().lower() for line in feature_spec.splitlines() if line.startswith("## ")
        ]
        required = [
            "scope",
            "functional requirements",
            "non-functional requirements",
            "acceptance criteria",
        ]
        missing = [h for h in required if h not in headings]
        if missing:
            errors.append(f"feature-spec.md is missing §9.2 headings: {missing}")
        for line in parse.parse_requirement_lines(feature_spec):
            if line.strip().startswith("- AC-") and not parse.is_valid_ac_line(line):
                errors.append(f"bad AC line (missing mandatory trace): {line.strip()[:80]}")
    return (len(errors) == 0), errors


def apply_spec_mark_ready(client, settings, spec_pr_number: int) -> tuple[bool, list[str]]:
    """§7.2: kernel validates spec files and marks the PR ready-for-review (→ spec-review)."""
    pr = client.get_pr(spec_pr_number)
    if pr is None:
        return False, ["spec PR not found"]
    feature_spec = client.get_file(_spec_path(pr.files, "feature-spec.md"), pr.head)
    technical_spec = client.get_file(_spec_path(pr.files, "technical-spec.md"), pr.head)
    valid, errors = validate_spec_files(feature_spec or "", technical_spec or "")
    if not valid:
        return False, errors
    client.update_pr(spec_pr_number, draft=False)
    return True, []


def _spec_path(files: list[str], filename: str) -> str:
    for path in files:
        if path.endswith(filename):
            return path
    return f"specs/{filename}"


def apply_release_plan(client, settings, plan: ReleasePlan) -> int:
    """§7.7 / §7.2.6: the KERNEL opens the release PR (marked, dev → main)."""
    from codie.tools.github_tools import GithubToolbox

    toolbox = GithubToolbox(client, settings, "kernel")
    body = (
        f"{plan.eval_report}\n\n"
        f"## Changelog\n\n{plan.changelog}\n\n"
        f"Suggested release tag: **{plan.version}**\n\n"
        "<!-- codie:release -->\n\n"
        "This release PR is merged by a human only."
    )
    number = toolbox.create_pr(
        f"release: {plan.version}",
        body,
        settings.branches.dev,
        settings.branches.main,
        draft=False,
    )
    return number


def verify_mutations(client, mutations) -> list[str]:
    """Post-hoc verification of declared mutations (§5.7). Returns failures."""
    failures: list[str] = []
    for mutation in mutations:
        if mutation.op == "create_pr":
            pr = client.get_pr(int(mutation.target))
            if pr is None:
                failures.append(f"create_pr #{mutation.target} did not land")
        elif mutation.op == "merge_pr":
            pr = client.get_pr(int(mutation.target))
            if pr is None or pr.state != "merged":
                failures.append(f"merge_pr #{mutation.target} did not land")
    return failures

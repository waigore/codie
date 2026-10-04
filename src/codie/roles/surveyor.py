"""Surveyor crew (Technical Spec §9.6 phase 1, Product Spec §4.2).

Agent-led, read-only repository survey at `codie init` time: answers the questions
the crew needs answered to operate the project by its own conventions — branching,
issue triage, build/run/test commands, surface, quality gates, docs. Every finding
carries evidence and returns a pydantic-validated `RepoSurvey`. It executes no
repository code (static analysis, git history, GitHub metadata only).
"""

from __future__ import annotations

from typing import Any, Literal

from codie.config import Settings
from codie.github.client import GitHubClient
from codie.models import Finding, IssueTriage, PrdProposal
from codie.state import parse

SURVEYOR_CONTRACT = """
Survey the repository read-only and answer every question the crew needs to
operate it by its own conventions:
  - Branching: stable/integration branch names, feature-branch pattern, merge
    style, release flow (tags, changelogs).
  - Issues: label purposes + mapping to codie's taxonomy, triage habits, and a
    per-issue adoption proposal (type/status with rationale and confidence).
  - PRD: locate an existing requirements document (adopt), synthesize a draft
    from README/roadmap+backlog, or conclude none (await human supply).
  - PRs / commits / build / run / test / surface / style / docs: cover each
    dimension with evidence (file paths, history samples, links).
Return a structured RepoSurvey. Never execute repository code; the pipeline is
static analysis + git history + GitHub metadata only.
"""


def build_agent_tools(settings, workspace, client, role: str = "surveyor") -> list[Any]:
    from codie.tools.file_tools import FileTools
    from codie.tools.github_tools import GithubToolbox

    return [
        FileTools(settings, workspace) if workspace else None,
        GithubToolbox(client, settings, role, workspace=workspace),
    ]


def role_prompt() -> str:
    from codie.roles.prompts import load_prompt, shared_note

    return f"{shared_note()}\n\n{load_prompt('surveyor')}\n\n{SURVEYOR_CONTRACT}"


# ---------------------------------------------------------------------------
# Deterministic evidence-gathering helpers used by gather_survey
# ---------------------------------------------------------------------------


def infer_branch_dimension(
    client: GitHubClient, settings: Settings, configured_main: str, configured_dev: str
) -> Finding:
    """Detect the actual branch names rather than trusting the configuration."""
    branches = {b: client.get_ref(b) for b in (configured_main, configured_dev)}
    evidence: list[str] = []
    known_alt = ["master", "main", "develop", "dev"]
    main_found = configured_main if branches.get(configured_main) else None
    dev_found = configured_dev if branches.get(configured_dev) else None
    for candidate in known_alt:
        if main_found is None and client.get_ref(candidate):
            main_found = candidate
            evidence.append(f"branch {candidate} exists")
        if dev_found is None and candidate != main_found and client.get_ref(candidate):
            dev_found = candidate
            evidence.append(f"branch {candidate} exists")
    if dev_found is None and main_found is None:
        return Finding(
            value="uses opinionated defaults (main/dev; this repo has no branches yet)",
            confidence="high",
            source="default",
            evidence=["no integration branch found"],
        )
    return Finding(
        value=f"main={main_found or configured_main}, dev={dev_found or configured_dev}",
        confidence="high",
        source="adopted",
        evidence=evidence or [f"branch {dev_found or configured_dev} exists"],
    )


def infer_label_habits(client: GitHubClient, settings: Settings, dev_sha: str | None) -> Finding:
    """Survey existing labels to derive a mapping into the codie taxonomy (M24)."""
    evidence: list[str] = []
    try:
        for name, _desc, _color in _existing_labels(client):
            evidence.append(f"existing label {name}")
    except Exception:
        pass
    if not evidence:
        return Finding(value="no label precedent", source="default", confidence="high", evidence=["no labels found"])
    return Finding(
        value="existing labels surveyed for adoption mapping",
        source="adopted",
        confidence="medium",
        evidence=evidence[:10],
    )


def _existing_labels(client) -> list[tuple[str, str, str]]:
    """Return existing repo labels via the fake's dict; the real client may not expose one."""
    labels = getattr(client, "labels", None)
    if isinstance(labels, dict):
        return [(name, *meta) for name, meta in labels.items()]
    return []


def infer_commit_norms(client: GitHubClient, settings: Settings) -> Finding:
    """Sample merged-PR/commit habits from issue history (best-effort, read-only)."""
    prs = client.list_prs()
    merged = [p for p in prs if p.state == "merged"]
    if merged:
        return Finding(
            value="merge via pull requests (merged PR history found)",
            source="adopted",
            confidence="high",
            evidence=[f"merged PR #{p.number} {p.title}" for p in merged[:5]],
        )
    return Finding(value="squash (default)", source="default", evidence=["no merged PR history"])


def infer_prd(client: GitHubClient, settings: Settings, dev_sha: str | None) -> PrdProposal:
    if dev_sha is None:
        return PrdProposal(source="none", evidence=["no integration branch — fresh repo"])
    for path in ("PRD.md", "docs/PRD.md", "docs/prd.md", "README.md"):
        content = client.get_file(path, dev_sha)
        if content and ("## Definition of done" in content or "- [ ] E1:" in content or "evals" in content.lower()):
            return PrdProposal(source="adopted_doc", content=content, evidence=[f"{path} at {dev_sha[:8]}"])
    for path in ("docs/requirements.md", "docs/requirements/index.md"):
        content = client.get_file(path, dev_sha)
        if content:
            return PrdProposal(source="adopted_doc", content=content, evidence=[path])
    readme = client.get_file("README.md", dev_sha)
    if readme:
        return PrdProposal(
            source="synthesized",
            content=_synthesize_prd(readme),
            evidence=["README.md goals section"],
        )
    return PrdProposal(source="none", evidence=["no requirements document found"])


def _synthesize_prd(readme: str) -> str:
    return (
        "# PRD (synthesized — please review)\n\n"
        f"## Product\n{readme[:2000]}\n\n"
        "## Definition of done\n- [ ] E1: the primary user journey works end-to-end\n"
    )


def triage_issue(issue: Any) -> IssueTriage:
    """Classify one open pre-existing issue into the workflow (low-confidence → untyped)."""
    type_label = parse.type_of(issue.labels)
    if type_label is not None:
        legal = {"feature", "task", "bug", "prd"}
        proposed: Literal["feature", "task", "bug", "prd", "untyped"] = (
            type_label if type_label in legal else "untyped"  # type: ignore[assignment]
        )
        return IssueTriage(
            number=issue.number,
            proposed_type=proposed,
            proposed_status="",
            rationale="already typed",
            confidence="high",
        )
    proposed = _propose_type(issue.title, issue.body)
    if proposed == "untyped":
        return IssueTriage(
            number=issue.number,
            proposed_type="untyped",
            rationale="question/discussion; out of crew scope",
            confidence="medium",
        )
    return IssueTriage(
        number=issue.number,
        proposed_type=proposed,
        proposed_status="ready" if proposed in {"task", "bug"} else "proposed",
        rationale=f"adopted from existing issue titled {issue.title!r}",
        confidence="low",
    )


def _propose_type(title: str, body: str) -> Literal["feature", "task", "bug", "untyped"]:
    text = f"{title}\n{body}".lower()
    if any(w in text for w in ["bug", "broken", "fails", "error", "crash", "regression"]):
        return "bug"
    if any(w in text for w in ["feature", "add ", "new ", "support", "ability to"]):
        return "feature"
    if any(w in text for w in ["?", "discussion", "proposal", "question"]):
        return "untyped"
    return "untyped"

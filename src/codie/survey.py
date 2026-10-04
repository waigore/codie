"""Repo survey — phase 1 of `codie init` (Technical Spec §9.6).

The production path uses the Surveyor crew (roles/surveyor.py, structured
`RepoSurvey`); the deterministic `gather_survey` below supplies the mechanical
passthrough that provisioning tests and `--yes`/`--pr` flows rely on.
"""

from __future__ import annotations

from codie.config import Settings
from codie.github.client import GitHubClient
from codie.models import Finding, IssueTriage, PrdProposal, RepoSurvey
from codie.state import parse


def gather_survey(
    client: GitHubClient,
    settings: Settings,
    repo: str,
    workspace=None,
    max_open_issues: int | None = None,
) -> RepoSurvey:
    """Deterministic, read-only survey of branching, issues, and PRD candidate."""
    max_open_issues = max_open_issues or settings.survey.max_open_issues
    dimensions: dict[str, Finding] = {}

    # branching
    main = client.get_ref(settings.branches.main)
    dev = client.get_ref(settings.branches.dev)
    branches_evidence: list[str] = []
    if main is not None:
        branches_evidence.append(f"branch {settings.branches.main} exists ({main[:8]})")
    if dev is not None:
        branches_evidence.append(f"branch {settings.branches.dev} exists ({dev[:8]})")
    if not dev:
        dimensions["branches"] = Finding(
            value="uses opinionated defaults (main/dev; this repo has no dev branch yet)",
            confidence="high",
            source="default",
            evidence=branches_evidence or ["no integration branch found"],
        )
    else:
        dimensions["branches"] = Finding(
            value=f"main={settings.branches.main}, dev={settings.branches.dev}",
            confidence="high",
            source="adopted",
            evidence=branches_evidence,
        )

    # PRs / merge style
    prs = client.list_prs()
    merged_prs = [pr for pr in prs if pr.state == "merged"]
    if merged_prs:
        dimensions["merge_style"] = Finding(
            value="merge via pull requests (merged PR history found)",
            confidence="high",
            source="adopted",
            evidence=[f"merged PR #{p.number} {p.title}" for p in merged_prs[:5]],
        )
    else:
        dimensions["merge_style"] = Finding(
            value="squash (default)", source="default", evidence=["no merged PR history"]
        )

    # issues / triage
    open_issues = sorted(
        (i for i in client.list_issues() if i.state == "open"),
        key=lambda i: i.updated_at,
        reverse=True,
    )[:max_open_issues]
    triage: list[IssueTriage] = []
    for issue in open_issues:
        t = parse.type_of(issue.labels)
        if t is not None:
            continue  # already typed
        proposed = _propose_type(issue.title, issue.body)
        if proposed == "untyped":
            triage.append(
                IssueTriage(
                    number=issue.number,
                    proposed_type="untyped",
                    rationale="question/discussion; out of crew scope",
                    confidence="medium",
                )
            )
        else:
            triage.append(
                IssueTriage(
                    number=issue.number,
                    proposed_type=proposed,  # type: ignore[arg-type]
                    proposed_status="ready" if proposed in {"task", "bug"} else "proposed",
                    rationale=_rationale(issue.title),
                    confidence="low",
                )
            )
    if open_issues:
        dimensions["issues"] = Finding(
            value=f"triaged {len(triage)} open issue(s) for adoption",
            confidence="medium",
            source="adopted",
            evidence=[f"#{i.number} {i.title}" for i in open_issues[:10]],
        )

    # PRD discovery (§9.6)
    prd = _discover_prd(client, settings, repo)

    # commands / surface (best-effort from repo files)
    commands, surface, framework = _infer_toolchain(client, settings)

    return RepoSurvey(
        dimensions=dimensions,
        issue_triage=triage,
        prd=prd,
    )


def _propose_type(title: str, body: str) -> str:
    text = f"{title}\n{body}".lower()
    if any(w in text for w in ["bug", "broken", "fails", "error", "crash", "regression"]):
        return "bug"
    if any(w in text for w in ["feature", "add ", "new ", "support", "ability to"]):
        return "feature"
    if any(w in text for w in ["?", "discussion", "proposal", "question"]):
        return "untyped"
    return "untyped"


def _rationale(title: str) -> str:
    return f"adopted from existing issue titled {title!r}"


def _discover_prd(client: GitHubClient, settings: Settings, repo: str) -> PrdProposal:
    dev = client.get_ref(settings.branches.dev)
    if dev is None:
        return PrdProposal(source="none", evidence=["no integration branch — fresh repo"])

    for path in ("PRD.md", "docs/PRD.md", "docs/prd.md", "README.md"):
        content = client.get_file(path, dev)
        if content and ("## Definition of done" in content or "- [ ] E1:" in content or "evals" in content.lower()):
            return PrdProposal(source="adopted_doc", content=content, evidence=[f"{path} at {dev[:8]}"])
    # requirements docs
    for path in ("docs/requirements.md", "docs/requirements/", "docs/requirements/index.md"):
        content = client.get_file(path, dev)
        if content:
            return PrdProposal(source="adopted_doc", content=content, evidence=[path])
    readme = client.get_file("README.md", dev)
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


def _infer_toolchain(client: GitHubClient, settings: Settings) -> tuple[dict[str, list[str]], str, str]:
    dev = client.get_ref(settings.branches.dev)
    commands: dict[str, list[str]] = {}
    surface = "cli"
    framework = "subprocess"
    if dev is None:
        return commands, surface, framework
    # Surface from manifests
    for path, surf in (
        ("package.json", "ui-web"),
        ("pubspec.yaml", "ui-mobile"),
        ("Cargo.toml", "cli"),
        ("pyproject.toml", "cli"),
    ):
        content = client.get_file(path, dev)
        if content:
            surface = (
                "ui-web" if (surf == "ui-web" and ("react" in content.lower() or "vue" in content.lower())) else surf
            )
            break
    # Commands from CI best-effort
    for path in (".github/workflows/ci.yml", ".gitlab-ci.yml"):
        ci = client.get_file(path, dev)
        if ci:  # noqa: SIM102
            if "pytest" in ci:
                commands.setdefault("test_fast", ["pytest", "-x", "-q", "tests/unit"])
                commands.setdefault("test_full", ["pytest", "-q"])
                commands.setdefault("test_acceptance", ["pytest", "tests/acceptance", "-q"])
            if "ruff" in ci or "flake8" in ci:
                commands.setdefault("lint", ["ruff", "check", "."])
            if "pnpm" in ci or "npm" in ci:
                commands.setdefault("build", ["npm", "run", "build"])
            break
    return commands, surface, framework

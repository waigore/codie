"""Repo survey — phase 1 of `codie init` (Technical Spec §9.6).

The production path uses the Surveyor crew (roles/surveyor.py, structured
`RepoSurvey`); `gather_survey` below is the deterministic, evidence-gathering
implementation every path relies on (provisioning tests and `--yes`/`--pr`
flows included). Findings carry file-path/history evidence and source tags.
"""

from __future__ import annotations

import json

from codie.config import Settings
from codie.github.client import GitHubClient
from codie.models import Finding, RepoSurvey
from codie.roles import surveyor as surveyor_role


def gather_survey(
    client: GitHubClient,
    settings: Settings,
    repo: str,
    workspace=None,
    max_open_issues: int | None = None,
) -> RepoSurvey:
    """Deterministic, read-only survey of branching, issues, toolchain, and PRD."""
    max_open_issues = max_open_issues or settings.survey.max_open_issues
    dimensions: dict[str, Finding] = {}

    # -- branching: detect the real names (master/develop, not just main/dev)
    detected_main, detected_dev = _detect_branches(client, settings)
    actual_dev = client.get_ref(detected_dev) if detected_dev else None
    dimensions["branches"] = surveyor_role.infer_branch_dimension(
        client, settings, settings.branches.main, settings.branches.dev
    )

    # -- merge style / PR norms
    merged_prs = [p for p in client.list_prs() if p.state == "merged"]
    dimensions["merge_style"] = surveyor_role.infer_commit_norms(client, settings)
    if merged_prs:
        dimensions["pr_norms"] = Finding(
            value="PRs carry requested reviewers / checks",
            source="adopted",
            confidence="medium",
            evidence=[f"merged PR #{p.number} {p.title}" for p in merged_prs[:5]],
        )

    # -- issues / triage
    open_issues = sorted(
        (i for i in client.list_issues() if i.state == "open"),
        key=lambda i: i.updated_at,
        reverse=True,
    )[:max_open_issues]
    triage = [surveyor_role.triage_issue(issue) for issue in open_issues]
    if open_issues:
        dimensions["issues"] = Finding(
            value=f"triaged {len(triage)} open issue(s) for adoption",
            confidence="medium",
            source="adopted",
            evidence=[f"#{i.number} {i.title}" for i in open_issues[:10]],
        )

    # -- label habits → label_mapping (M24)
    label_dim = surveyor_role.infer_label_habits(client, settings, actual_dev)
    dimensions["label_habits"] = label_dim

    # -- commands / surface (evidence-linked findings)
    commands, surface, framework, cmd_evidence = _infer_toolchain(client, settings, actual_dev)
    dimensions["commands"] = Finding(
        value=json.dumps(commands, sort_keys=True),
        confidence="medium",
        source="adopted" if commands else "default",
        evidence=cmd_evidence or ["no CI/manifest evidence — defaults apply"],
    )
    dimensions["surface"] = Finding(
        value=surface,
        confidence="medium",
        source="adopted",
        evidence=cmd_evidence or ["inferred from stack (default: cli)"],
    )
    dimensions["acceptance_framework"] = Finding(
        value=framework,
        confidence="medium",
        source="adopted" if framework != "subprocess" else "default",
        evidence=["surface/stack default"],
    )

    # -- PRD discovery
    prd = _discover_prd(client, settings, actual_dev)

    return RepoSurvey(
        dimensions=dimensions,
        issue_triage=triage,
        prd=prd,
    )


def _detect_branches(client: GitHubClient, settings: Settings) -> tuple[str, str]:
    """Return (actual_main, actual_dev) resolving master/develop aliases (I-02)."""
    main_names = [settings.branches.main] + [n for n in ("main", "master") if n != settings.branches.main]
    dev_names = [settings.branches.dev] + [n for n in ("dev", "develop") if n != settings.branches.dev]
    actual_main = next((n for n in main_names if client.get_ref(n)), settings.branches.main)
    actual_dev = next((n for n in dev_names if client.get_ref(n) and n != actual_main), settings.branches.dev)
    if not client.get_ref(actual_dev) and not client.get_ref(actual_main):
        return settings.branches.main, settings.branches.dev
    return actual_main, actual_dev


def survey_commands(survey: RepoSurvey) -> dict[str, list[str]]:
    finding = survey.dimensions.get("commands")
    if finding is None:
        return {}
    try:
        data = json.loads(finding.value)
        return {k: v for k, v in data.items() if isinstance(v, list)}
    except (json.JSONDecodeError, TypeError):
        return {}


def apply_dimension_edits(survey: RepoSurvey, edits: dict[str, str]) -> RepoSurvey:
    """Phase-2 per-dimension edits (I-03): each edit touches only that dimension."""
    for dim, value in edits.items():
        finding = survey.dimensions.get(dim)
        if finding is None or not value:
            continue
        finding.value = value
        finding.source = "adopted"
    return survey


def survey_surface(survey: RepoSurvey) -> tuple[str, str]:
    surface = survey.dimensions.get("surface")
    framework = survey.dimensions.get("acceptance_framework")
    return (surface.value if surface else "cli"), (framework.value if framework else "subprocess")


def _discover_prd(client: GitHubClient, settings: Settings, dev: str | None):
    if dev is None:
        from codie.models import PrdProposal

        return PrdProposal(source="none", evidence=["no integration branch — fresh repo"])
    from codie.roles.surveyor import infer_prd

    return infer_prd(client, settings, dev)


def _infer_toolchain(client, settings: Settings, dev) -> tuple[dict[str, list[str]], str, str, list[str]]:
    commands: dict[str, list[str]] = {}
    surface = "cli"
    framework = "subprocess"
    evidence: list[str] = []
    if dev is None:
        return commands, surface, framework, evidence
    for path, surf in (
        ("package.json", "ui-web"),
        ("pubspec.yaml", "ui-mobile"),
        ("Cargo.toml", "cli"),
        ("pyproject.toml", "cli"),
    ):
        content = client.get_file(path, dev)
        if content:
            if surf == "ui-web" and any(x in content.lower() for x in ("react", "vue", "svelte")):
                surface = "ui-web"
            else:
                surface = surf
            evidence.append(f"{path} on {dev[:8]}")
            break
    for path in (".github/workflows/ci.yml", ".gitlab-ci.yml"):
        ci = client.get_file(path, dev)
        if ci:
            evidence.append(f"{path} on {dev[:8]}")
            if "pytest" in ci:
                commands.setdefault("test_fast", ["pytest", "-x", "-q", "tests/unit"])
                commands.setdefault("test_full", ["pytest", "-q"])
                commands.setdefault("test_acceptance", ["pytest", "tests/acceptance", "-q"])
                custom = _ci_pytest_command(ci)
                if custom:
                    commands["test_fast"] = custom  # the repo's confirmed test invocation
            if "ruff" in ci or "flake8" in ci:
                commands.setdefault("lint", ["ruff", "check", "."])
            if "pnpm" in ci or "npm" in ci:
                commands.setdefault("build", ["npm", "run", "build"])
            if "setup" not in commands:
                commands.setdefault("setup", ["pip", "install", "-e", ".[dev]"])
            break
    return commands, surface, framework, evidence


def _ci_pytest_command(ci: str) -> list[str] | None:
    """Extract the repo's actual pytest invocation (e.g. `pytest tests/custom`) from CI."""
    import re

    m = re.search(r"(?m)\b(pytest)\s+([^\n|&%]{1,200})", ci)
    if not m:
        return None
    args = [a for a in m.group(2).split() if a and a != "&&"]
    return ["pytest", *args] if args else None

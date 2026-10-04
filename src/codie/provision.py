"""Provisioning — phase 3 of `codie init` (Technical Spec §9.6) and doctor.

Idempotent; applies only what is missing, per the confirmed conventions.
Ordering matters: branches and the seed commit come before protection.
"""

from __future__ import annotations

from codie.config import Registry, Settings
from codie.github.client import AdminGitHubClient
from codie.models import RepoSurvey


class ProvisionReport:
    def __init__(self) -> None:
        self.applied: list[str] = []
        self.already: list[str] = []
        self.pending: list[str] = []
        self.prd_issue_number: int | None = None

    def lines(self) -> list[str]:
        out = ["Applied:"] if self.applied else []
        out.extend(f"  - {x}" for x in self.applied)
        out.append("Already present:") if self.already else None
        out.extend(f"  - {x}" for x in self.already)
        if self.pending:
            out.append("Pending human action:")
            out.extend(f"  - {x}" for x in self.pending)
        return out


def provision(
    settings: Settings,
    client: AdminGitHubClient,
    survey: RepoSurvey,
    repo: str,
    registry: Registry | None = None,
    prd_content: str | None = None,
) -> ProvisionReport:
    """Run the §9.6 phase-3 steps against the client. Returns a report."""
    registry = registry or Registry.load()
    report = ProvisionReport()

    # 1. register project (mechanical)
    registry.upsert(
        repo,
        workspace=settings.project.workspace,
        poll_interval_seconds=settings.project.poll_interval_seconds,
    )
    registry.save()
    report.applied.append(f"registered project {repo} in the local registry")

    # 2. branches + seed commit
    _ensure_branches(settings, client, report)

    # 3. labels
    from codie.github.labels import ensure_label_set

    n = ensure_label_set(client)  # type: ignore[arg-type]
    report.applied.append(f"ensured {n} codie labels (strictly additive)")

    # 4. issue adoption
    for t in survey.issue_triage:
        if t.proposed_type == "untyped":
            continue
        issue = client.get_issue(t.number)
        if issue is None:
            continue
        labels = set(issue.labels)
        added: list[str] = []
        if f"type:{t.proposed_type}" not in labels:
            labels.add(f"type:{t.proposed_type}")
            added.append(f"type:{t.proposed_type}")
        if t.proposed_status and f"status:{t.proposed_status}" not in labels:
            labels.add(f"status:{t.proposed_status}")
            added.append(f"status:{t.proposed_status}")
        if added:
            client.set_labels(t.number, sorted(labels))
            client.comment(
                t.number,
                f"Adopted by codie as `type:{t.proposed_type}` / `status:{t.proposed_status}`. ({t.rationale})",
            )
            report.applied.append(f"adopted #{t.number} as {t.proposed_type}")

    # 5. repo options + protection
    _apply_protection(settings, client, report)

    # 6. collaborators
    _ensure_collaborators(settings, client, report)

    # 7. onboarding PR (files) — merged via the onboarding PR in real runs
    pr_number = _open_onboarding_pr(settings, client, repo, survey, report)

    # 8. PRD issue
    if prd_content:
        existing_prd = [i for i in client.list_issues() if "type:prd" in i.labels]
        if existing_prd:
            report.already.append(f"PRD issue #{existing_prd[0].number} already exists")
        else:
            report.prd_issue_number = _post_prd(settings, client, repo, prd_content, report)

    # 9. report note when no PRD
    if report.prd_issue_number is None:
        report.pending.append("No PRD yet — `codie start` will refuse until a pinned type:prd issue exists.")
    if pr_number is not None:
        report.pending.append(f"Onboarding PR #{pr_number} open — merge it to confirm .codie.yaml + AGENTS.md.")

    return report


def _ensure_branches(settings: Settings, client, report) -> None:
    main, dev = settings.branches.main, settings.branches.dev
    main_sha = client.get_ref(main)
    dev_sha = client.get_ref(dev)
    if main_sha is None and dev_sha is None:
        # empty repo: seed README on the default branch first, then dev
        default = _default_branch(client)
        seed = "README.md" in (_list_files(client, default) if default else [])
        if not seed:
            client.upsert_file(
                "README.md",
                f"# {settings.project.repo}\n\nInitialized by codie.\n",
                "chore: seed initial README",
                default,
            )
            report.applied.append(f"seeded README on {default or 'default'} branch")
        if main != default and client.get_ref(main) is None:
            client.create_ref(main, client.get_ref(default))
            report.applied.append(f"created branch {main}")
        if client.get_ref(dev) is None:
            client.create_ref(dev, client.get_ref(main))
            report.applied.append(f"created integration branch {dev}")
    else:
        if dev_sha is None:
            client.create_ref(dev, main_sha or "")
            report.applied.append(f"created integration branch {dev}")
        else:
            report.already.append(f"branches {main}/{dev} exist")


def _default_branch(client) -> str:
    return "main"


def _list_files(client, branch: str) -> list[str]:
    try:
        return list_files_helper(client, branch)
    except Exception:
        return []


def list_files_helper(client, branch: str) -> list[str]:
    if hasattr(client, "list_files"):
        return client.list_files(branch)  # type: ignore[attr-defined]
    return []


def _apply_protection(settings: Settings, client, report) -> None:
    for branch in (settings.branches.dev, settings.branches.main):
        existing = client.get_branch_protection(branch)
        if existing:
            report.already.append(f"branch protection on {branch} present")
            continue
        require_approvals = 0 if branch == settings.branches.dev else 1
        client.update_branch_protection(branch, require_prs=True, require_approvals=require_approvals)
        report.applied.append(f"branch protection on {branch}: require PR, {require_approvals} approval(s)")
    client.update_repo(settings.overrides.merge_method, delete_branch_on_merge=True)


def _ensure_collaborators(settings: Settings, client, report) -> None:
    for role, spec in settings.github.tokens.items():
        if role == "kernel":
            continue
        login = spec.login
        if not login:
            continue
        try:
            client.invite_collaborator(login, "push")
            report.applied.append(f"invited {login} as collaborator (pending acceptance)")
        except Exception:
            report.already.append(f"{login} already a collaborator")


def _open_onboarding_pr(settings, client, repo, survey, report) -> int | None:
    """Write .codie.yaml, AGENTS.md, CODEOWNERS, architecture survey on codie/onboarding."""
    branch = "codie/onboarding"
    base = settings.branches.dev
    if client.get_ref(branch) is not None:
        report.already.append("onboarding branch exists")
        return None

    yaml_text = _render_codie_yaml(settings)
    agents = _render_agents_md(settings, repo)
    arch = _render_architecture(survey)

    files = [
        (".codie.yaml", yaml_text),
        ("AGENTS.md", agents),
        ("specs/_survey/architecture.md", arch),
    ]
    report.applied.append(f"prepared onboarding branch {branch}")
    return _write_files_and_pr(settings, client, files, branch, base, repo)


def _write_files_and_pr(settings, client, files, branch: str, base: str, repo: str) -> int:
    if client.get_ref(branch) is None:
        client.create_ref(branch, client.get_ref(base) or "")
    for path, content in files:
        client.upsert_file(path, content, f"chore: add {path}", branch)
    pr = client.create_pr(
        "chore: codie onboarding",
        f"Codie onboarding for {repo}: operations contract (.codie.yaml), conventions (AGENTS.md), "
        "and the durable architecture survey (specs/_survey/architecture.md).\n\nBase = the integration branch.",
        branch,
        base,
        draft=False,
    )
    return pr.number


def _render_codie_yaml(settings: Settings) -> str:
    return (
        "branches:\n"
        f"  main: {settings.branches.main}\n"
        f"  dev: {settings.branches.dev}\n"
        f"  feature_pattern: {settings.branches.feature_pattern!r}\n"
        f"  test_pattern: {settings.branches.test_pattern!r}\n"
        f"merge_method: {settings.overrides.merge_method}\n"
        f"surface: {settings.overrides.surface}\n"
        "label_mapping: {}\n"
        "acceptance:\n"
        f"  framework: {settings.overrides.acceptance.framework}\n"
        f"  dir: {settings.overrides.acceptance.dir!r}\n"
        "commands:\n" + _render_commands(settings) + "\n"
        "workflow:\n"
        f"  bugs_outrank_features: {str(settings.overrides.workflow.bugs_outrank_features).lower()}\n"
        f"  full_test_cadence_minutes: {settings.overrides.workflow.full_test_cadence_minutes}\n"
        "guardrails:\n"
        f"  max_cycles_per_work_item: {settings.guardrails.max_cycles_per_work_item}\n"
        "  heartbeat_timeout_minutes: 20\n"
        "  max_defer_cycles: 3\n"
        "  max_idle_dispatch_cycles: 2\n"
        f"  shell_denylist: {settings.guardrails.shell_denylist!r}\n"
    )


def _render_commands(settings) -> str:
    out: list[str] = []
    for name, argv in settings.overrides.commands.items():
        out.append(f"  {name}: {argv!r}")
    return "\n".join(out)


def _render_agents_md(settings, repo) -> str:
    return (
        f"# AGENTS.md — {repo}\n\n"
        "## Codie workflow\n"
        "Codie derives state from GitHub issues, labels, and PRs (see "
        "docs/PRODUCT_SPEC.md and docs/TECHNICAL_SPEC.md). Team:\n"
        "- Planner proposes features + specs and breaks approved features into tasks.\n"
        "- Coder implements tasks on feature branches and opens PRs to the integration branch.\n"
        "- Reviewer gates the integration branch (approves + merges PRs; spec PRs as product owner).\n"
        "- Tester owns the acceptance suite, regression runs, and feature acceptance.\n"
        "Human gates: PRD confirmation, UAT label flips (`status:review` → `accepted`/`revised`),\n"
        "unblocking `flag:needs-human`, and merging the release PR.\n"
    )


def _render_architecture(survey: RepoSurvey) -> str:
    dims = {k: v.value for k, v in survey.dimensions.items()}
    return (
        "# Architecture survey (codie init)\n\n"
        "This snapshot was produced at onboarding and is read by the Planner's context pack "
        "from the integration-branch SHA.\n\n"
        "```json\n" + __import__("json").dumps(dims, indent=2) + "\n```\n"
    )


def _post_prd(settings, client, repo: str, prd_content: str, report: ProvisionReport) -> int:
    issue = client.create_issue("Product Requirements (PRD)", prd_content, ["type:prd"])
    client.pin_issue(issue.number, pinned=True)
    report.applied.append(f"posted pinned PRD issue #{issue.number}")
    return issue.number


def check_provisioning(settings: Settings, client, repo: str) -> dict[str, str]:
    """`codie doctor` — mechanical, read-only checklist (§6.1)."""
    from codie.github.labels import LABEL_NAMES

    results: dict[str, str] = {}
    try:
        has_labels = all(_label_present(client, name) for name in LABEL_NAMES)
        results["labels"] = "ok" if has_labels else "missing codie labels"
    except Exception as exc:
        results["labels"] = f"error: {exc}"
    for branch in (settings.branches.main, settings.branches.dev):
        results[f"branch:{branch}"] = "ok" if client.get_ref(branch) else "missing"
    try:
        protection = client.get_branch_protection(settings.branches.dev)
        results["protection:dev"] = (
            "ok" if protection and protection.get("required_pull_request_reviews") else "missing PR requirement"
        )
    except Exception as exc:
        results["protection:dev"] = f"error: {exc}"
    for role, spec in settings.github.tokens.items():
        if role == "kernel":
            continue
        results[f"collab:{spec.login}"] = "invited" if spec.login else "no login"
    return results


def _label_present(client, name: str) -> bool:
    if hasattr(client, "has_label"):
        return bool(client.has_label(name))  # type: ignore[attr-defined]
    return True

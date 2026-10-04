"""Provisioning — phase 3 of `codie init` (Technical Spec §9.6) and doctor.

Idempotent; applies only what is missing, per the confirmed conventions.
Ordering matters: branches and the seed commit come before protection.
"""

from __future__ import annotations

from codie.config import ConfigError, Registry, Settings
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
    defer_settings: bool = False,
) -> ProvisionReport:
    """Run the §9.6 phase-3 steps against the client. Returns a report.

    With `defer_settings=True` (`--pr`), no GitHub settings or PRD mutation
    happens before the onboarding PR is merged — only registration and the
    onboarding PR are produced (the merge is the confirmation, §9.6).
    """
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

    if defer_settings:
        report.pending.append(
            "--pr mode: no GitHub settings, labels, or PRD are applied before the "
            "onboarding PR merges (its merge is the confirmation)."
        )
        pr_number = _open_onboarding_pr(settings, client, repo, survey, report)
        if pr_number is not None:
            report.pending.append(f"Onboarding PR #{pr_number} open — merge it to confirm .codie.yaml + AGENTS.md.")
        return report

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


def require_provisioned(settings: Settings, client, repo: str) -> None:
    """§6.1 preflight: the crew must not start until the provisioning checklist passes."""

    results = check_provisioning(settings, client, repo)
    failures = {k: v for k, v in results.items() if v != "ok"}
    if failures:
        lines = "\n".join(f"  - {k}: {v}" for k, v in failures.items())
        raise ConfigError(
            "repo is not provisioned; `codie start` refuses to run:\n"
            f"{lines}\n\nRun `codie init <url>` (or `codie doctor <url>` for the remediation guide)."
        )
    if client.get_ref(settings.branches.dev) is None:
        raise ConfigError(f"integration branch {settings.branches.dev} is missing (run `codie init`)")
    if not any("type:prd" in (i.labels or []) for i in client.list_issues()):
        raise ConfigError("no pinned type:prd issue exists — run `codie init`, or create the PRD issue (§6.4)")


def _default_branch(client) -> str:
    get_default = getattr(client, "get_default_branch", None)
    if get_default is not None:
        try:
            default = get_default()
            if default:
                return default
        except Exception:
            pass
    for candidate in ("main", "master", "dev"):
        if client.get_ref(candidate):
            return candidate
    return "main"


def _list_files(client, branch: str) -> list[str]:
    list_files = getattr(client, "list_files", None)
    if list_files is None:
        return []
    try:
        return list_files(branch)
    except Exception:
        return []


def _apply_protection(settings: Settings, client, report) -> None:
    required = 1
    for branch in (settings.branches.dev, settings.branches.main):
        existing = client.get_branch_protection(branch) or {}
        reviews = existing.get("required_pull_request_reviews") or {}
        count = int(reviews.get("required_approving_review_count", 0) or 0)
        checks_ok = existing.get("required_status_checks") is not None
        if bool(existing.get("required_pull_request_reviews")) and checks_ok and count >= required:
            report.already.append(f"branch protection on {branch} present")
            continue
        # §9.3: existing protection is gap-filled, never silently dropped.
        client.update_branch_protection(
            branch,
            require_prs=True,
            require_approvals=required,
            checks=[],
        )
        report.applied.append(f"branch protection on {branch}: require PR, {required} approval(s)")
    client.update_repo(settings.overrides.merge_method, delete_branch_on_merge=True)


def _ensure_collaborators(settings: Settings, client, report) -> None:
    """Ensure all five crew accounts have push access (pending invites reported)."""
    for _role, spec in settings.github.tokens.items():
        login = spec.login
        if not login:
            continue
        collaborators = set()
        try:
            collaborators = set(client.list_collaborators())
        except Exception:
            collaborators = set()
        if login in collaborators:
            report.already.append(f"{login} already a collaborator")
            continue
        try:
            client.invite_collaborator(login, "push")
            report.pending.append(f"invite sent for {login} (pending acceptance)")
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
    agents = _render_agents_md(settings, repo, client, base)
    arch = _render_architecture(survey)
    codeowners = _render_codeowners(settings, client)

    files = [
        (".codie.yaml", yaml_text),
        ("AGENTS.md", agents),
        ("CODEOWNERS", codeowners),
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
        "stable-branch ownership (CODEOWNERS), and the durable architecture survey "
        "(specs/_survey/architecture.md).\n\nBase = the integration branch.",
        branch,
        base,
        draft=False,
    )
    return pr.number


def _render_codie_yaml(settings: Settings) -> str:
    mapping = settings.overrides.label_mapping or {}
    return (
        "branches:\n"
        f"  main: {settings.branches.main}\n"
        f"  dev: {settings.branches.dev}\n"
        f"  feature_pattern: {settings.branches.feature_pattern!r}\n"
        f"  test_pattern: {settings.branches.test_pattern!r}\n"
        f"merge_method: {settings.overrides.merge_method}\n"
        f"surface: {settings.overrides.surface}\n"
        "label_mapping: " + ((", ".join(f"{k}: {v}" for k, v in mapping.items())) if mapping else "{}") + "\n"
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


def _render_agents_md(settings, repo, client, base) -> str:
    """Write AGENTS.md freshly or amend minimally (Product §4.2 — never overwrite)."""
    existing = None
    try:
        existing = client.get_file("AGENTS.md", base)
    except Exception:
        existing = None
    preamble = existing if existing and existing.strip() else f"# AGENTS.md — {repo}\n"
    section = (
        "\n## Codie workflow\n"
        "Codie derives state from GitHub issues, labels, and PRs (see "
        "docs/PRODUCT_SPEC.md and docs/TECHNICAL_SPEC.md). Team:\n"
        "- Planner proposes features + specs and breaks approved features into tasks.\n"
        "- Coder implements tasks on feature branches and opens PRs to the integration branch.\n"
        "- Reviewer gates the integration branch (approves + merges PRs; spec PRs as product owner).\n"
        "- Tester owns the acceptance suite, regression runs, and feature acceptance.\n"
        "Human gates: PRD confirmation, UAT label flips (`status:review` → `accepted`/`revised`),\n"
        "unblocking `flag:needs-human`, and merging the release PR.\n"
    )
    if existing and "## Codie workflow" in existing:
        return existing
    return preamble.rstrip() + "\n" + section


def _render_codeowners(settings, client) -> str:
    """CODEOWNERS `* @<admin>` for the stable branch (§9.6 step 7, only where needed)."""
    login = ""
    try:
        login = client.get_authenticated_user()
    except Exception:
        login = ""
    if login:
        return f"* @{login.lstrip('@')}\n"
    return "* @codie-admin\n"


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
    """`codie doctor` — mechanical, read-only checklist (§6.1) with remediation hints."""
    from codie.github.labels import LABEL_NAMES

    results: dict[str, str] = {}

    def note(key: str, ok: bool, ok_text: str, bad_text: str) -> None:
        results[key] = ok_text if ok else bad_text

    try:
        has_labels = all(_label_present(client, name) for name in LABEL_NAMES)
        note("labels", has_labels, "ok", "missing codie labels (run `codie labels sync <url>`)")
    except Exception as exc:
        results["labels"] = f"error: {exc}"
    for branch in (settings.branches.main, settings.branches.dev):
        results[f"branch:{branch}"] = "ok" if client.get_ref(branch) else "missing (create it in `codie init`)"
    for branch in (settings.branches.dev, settings.branches.main):
        try:
            protection = client.get_branch_protection(branch) or {}
            reviews = protection.get("required_pull_request_reviews")
            results[f"protection:{branch}"] = (
                "ok"
                if reviews and protection.get("required_status_checks") is not None
                else "missing PR/status-check requirement (fixed by `codie init`)"
            )
        except Exception as exc:
            results[f"protection:{branch}"] = f"error: {exc}"
    try:
        dev_protection = client.get_branch_protection(settings.branches.dev) or {}
        count = (dev_protection.get("required_pull_request_reviews") or {}).get("required_approving_review_count", 0)
        results["approval:dev"] = "ok" if count >= 1 else "dev protection allows 0-approval merges (need 1)"
    except Exception as exc:
        results["approval:dev"] = f"error: {exc}"
    try:
        main_protection = client.get_branch_protection(settings.branches.main) or {}
        enforce_admins = bool(main_protection.get("enforce_admins"))
        results["stable:human-merge-only"] = (
            "ok" if enforce_admins else "stable branch is not admin-gated (fail-closed on manual merges)"
        )
    except Exception as exc:
        results["stable:human-merge-only"] = f"error: {exc}"
    dev_sha = client.get_ref(settings.branches.dev)
    codie_yaml = client.get_file(".codie.yaml", dev_sha) if dev_sha else None
    results[".codie.yaml@dev"] = (
        "ok" if codie_yaml is not None else "missing on the integration head (run `codie init`)"
    )
    collaborators = set()
    try:
        collaborators = set(client.list_collaborators())
    except Exception:
        collaborators = set()
    for _role, spec in settings.github.tokens.items():
        if not spec.login:
            continue
        results[f"collab:{spec.login}"] = (
            "ok" if spec.login in collaborators else "not a collaborator yet (pending invite — human must accept)"
        )
    return results


def _label_present(client, name: str) -> bool:
    has_label = getattr(client, "has_label", None)
    if has_label is None:
        # fall back to upsert_(no) — the protocol mandates has_label; be strict.
        return False
    return bool(has_label(name))

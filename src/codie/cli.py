"""Typer CLI entry (Technical Spec §6.4)."""

from __future__ import annotations

import os
import signal
from datetime import UTC
from pathlib import Path

import typer

from codie import config
from codie.cache import Cache, cache_path, state_dir
from codie.config import ConfigError, Settings, load_global_settings, parse_repo_url

app = typer.Typer(add_completion=False, no_args_is_help=True)
APP_NAME = "codie"


def _repo_from_url(url: str) -> str:
    owner, repo = parse_repo_url(url)
    return f"{owner}/{repo}"


def _load_settings(repo: str, explicit: str | None = None) -> Settings:
    global_settings, _ = load_global_settings(explicit)
    if global_settings is None:
        raise ConfigError("no global codie.yaml found (~/.codie/codie.yaml or ./codie.yaml)")
    return global_settings


def _project_overrides_from_head(repo: str, settings: Settings):
    """Fetch .codie.yaml from the integration head via a real client (§9.6)."""
    from codie.github.pygithub_client import build_client

    client = build_client(repo, settings)
    dev_sha = client.get_ref(settings.branches.dev)
    if dev_sha is None:
        raise ConfigError(f"integration branch {settings.branches.dev} not found on {repo}")
    text = client.get_file(".codie.yaml", dev_sha)
    if text is None:
        raise ConfigError(f"no .codie.yaml on {repo}@{settings.branches.dev} — run `codie init` first")
    return config.load_overrides_text(text), client


@app.command("init")
def cmd_init(
    url: str = typer.Argument(..., help="Repository URL or owner/repo"),
    prd: str = typer.Option(None, "--prd", help="Supply the PRD file (skips discovery)"),
    check: bool = typer.Option(False, "--check", help="Read-only doctor check (same as `codie doctor`)"),
    yes: bool = typer.Option(False, "--yes", help="Accept all proposals"),
    pr: bool = typer.Option(False, "--pr", help="Defer confirmation to the onboarding PR"),
    config_file: str = typer.Option(None, "--config", hidden=True),
):
    """Survey-first onboarding (Technical Spec §9.6). Requires CODIE_GH_TOKEN_ADMIN."""
    repo = _repo_from_url(url)
    if check:
        _doctor(repo, config_file)
        return
    from codie.provision import provision
    from codie.survey import gather_survey

    settings = _load_settings(repo, config_file)
    admin_token = config.resolve_admin_token(settings)
    from codie.github.pygithub_client import PyGithubClient

    client: object = PyGithubClient(repo, {"kernel": admin_token}, base_url=settings.github.api_base_url)
    # read-only survey via admin token; provision uses the same channel in --dry-run style tests.
    try:
        survey = gather_survey(client, settings, repo)  # type: ignore[arg-type]
    except Exception as exc:
        typer.echo(f"survey failed: {exc}")
        raise typer.Exit(1) from exc

    if prd:
        content = Path(prd).read_text()
        survey.prd = survey.prd.model_copy(update={"source": "supplied", "content": content})
    elif not yes and not pr:
        _confirm(survey)

    report = provision(settings, client, survey, repo)  # type: ignore[arg-type]
    typer.secho("\n".join(report.lines()), fg=typer.colors.GREEN)
    if report.prd_issue_number is None and not prd:
        typer.secho("\nNo PRD yet; `codie start` will refuse until one exists.", fg=typer.colors.YELLOW)


def _confirm(survey) -> None:
    typer.echo("codie survey — proposed conventions to confirm:")
    for dim, finding in survey.dimensions.items():
        typer.echo(f"  - {dim}: {finding.value} [{finding.source}]")
    for t in survey.issue_triage:
        typer.echo(f"  - adopt #{t.number} as {t.proposed_type} ({t.proposed_status or ''}): {t.rationale}")
    typer.echo(f"  - PRD source: {survey.prd.source}")
    if survey.prd.content:
        typer.echo("--- PRD preview ---")
        typer.echo(survey.prd.content[:1200])
        typer.echo("--- end preview ---")
    answer = input("Accept these proposals? [Y/n] ").strip().lower()
    if answer not in {"", "y", "yes"}:
        typer.echo("Aborting; nothing was provisioned.")
        raise typer.Exit(1)


@app.command("start")
def cmd_start(
    url: str = typer.Argument(..., help="Repository URL or owner/repo"),
    once: bool = typer.Option(False, "--once", help="Run one cycle, wait, then exit"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Log all GitHub mutations instead of applying them"),
    config_file: str = typer.Option(None, "--config", hidden=True),
):
    """Start or resume the crew on a repo (Technical Spec §6.1/§6.4)."""
    repo = _repo_from_url(url)
    settings = _load_settings(repo, config_file)
    overrides, client = _project_overrides_from_head(repo, settings)
    settings = config.build_settings(settings, repo, overrides)

    from codie.github.pygithub_client import PyGithubClient
    from codie.orchestrator import Kernel
    from codie.workspace import Workspace

    role_tokens = {role: _env_or_exit(settings, role) for role in ("planner", "coder", "reviewer", "tester", "kernel")}
    client = PyGithubClient(repo, role_tokens, base_url=settings.github.api_base_url)
    owner, name = repo.split("/")
    cache = Cache().open_or_rebuild(cache_path(owner, name))
    workspace = Workspace(settings.project.workspace_path, settings)
    workspace.ensure()
    workspace.janitor(coder_active=False)

    kernel = Kernel(settings=settings, client=client, workspace=workspace, cache=cache, dry_run=dry_run)
    _write_pid(owner, name)
    try:
        outcome = kernel.run(max_cycles=1 if once else None)
    finally:
        _remove_pid(owner, name)
    if outcome.halted:
        typer.secho("Release PR merged — codie halts (success).", fg=typer.colors.GREEN)


def _env_or_exit(settings: Settings, role: str) -> str:
    from codie.config import resolve_role_token

    token = resolve_role_token(settings, role)
    if not token:
        raise ConfigError(f"missing token for {role}")
    return token


@app.command("status")
def cmd_status(
    url: str = typer.Argument(..., help="Repository URL or owner/repo"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
    runs: bool = typer.Option(False, "--runs", help="Show run history and costs"),
    diff: bool = typer.Option(False, "--diff", help="Diff the last two snapshots"),
):
    """Derived state, next actions, run history (Technical Spec §6.3)."""
    import json as _json

    repo = _repo_from_url(url)
    settings = _load_settings(repo)
    try:
        overrides, client = _project_overrides_from_head(repo, settings)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    settings = config.build_settings(settings, repo, overrides)
    from codie.github.fetch import fetch_snapshot
    from codie.queue import compute_queue
    from codie.state.derive import derive

    snap = fetch_snapshot(client, repo, settings, full=True)
    state = derive(snap, settings)
    from datetime import datetime

    now = datetime.now(UTC)
    from codie.state.reconcile import plan_reconcile

    _mut, projected = plan_reconcile(state, settings, now)
    if json_out:
        typer.echo(_json.dumps(projected.model_dump(mode="json"), indent=2))
        return
    if state.prd:
        typer.secho(f"PRD #{state.prd.number}: {state.prd.title}")
        for e in state.prd.evals:
            typer.echo(f"  - [{'x' if e.checked else ' '}] {e.id}: {e.text}")
    for f in state.features:
        typer.echo(f"feature #{f.number} {f.title}: {f.status} pri={f.priority}")
    for t in state.tasks:
        typer.echo(f"  task #{t.number} {t.title}: {t.status} kind={t.kind} deps={t.depends_on}")
    for b in state.bugs:
        typer.echo(f"  bug  #{b.number} {b.title}: {b.status}")
    typer.echo("open PRs:")
    for p in state.prs:
        if p.state == "open":
            typer.echo(f"  #{p.number} {p.head} → {p.base} review={p.review_state}")
    if state.violations:
        typer.secho("violations:", fg=typer.colors.RED)
        for v in state.violations:
            typer.echo(f"  #{v.issue_number} [{v.code}] {v.message}")
    typer.secho("next work items (queue total order):", fg=typer.colors.CYAN)
    for item in compute_queue(projected, settings, now):
        typer.echo(f"  [{item.rule}] {item.kind} → {item.role}: {item.entity} (pri {item.priority})")
    owner, name = repo.split("/")
    cache_db = cache_path(owner, name)
    if cache_db.exists():
        from codie.cache import Cache

        cache = Cache().open_or_rebuild(cache_db)
        typer.echo(f"today's cost: ${cache.today_cost():.3f}")
        if runs:
            for r in cache.list_runs(10):
                typer.echo(f"  run #{r['id']} {r['status']} ${r['cost_usd']:.3f}")
        if diff:
            snaps = cache.last_snapshots(2)
            if len(snaps) < 2:
                typer.echo("no previous snapshot exists")
            else:
                typer.echo("snapshot diff between the last two snapshots is available; rendering is a future detail")


@app.command("doctor")
def cmd_doctor(url: str = typer.Argument(..., help="Repository URL or owner/repo")):
    """Mechanical, read-only provisioning/permission checklist (§6.1)."""
    _doctor(_repo_from_url(url), None)


def _doctor(repo: str, config_file: str | None) -> None:
    settings = _load_settings(repo, config_file)
    from codie.github.pygithub_client import PyGithubClient
    from codie.provision import check_provisioning

    admin_token = config.resolve_admin_token(settings)
    client = PyGithubClient(repo, {"kernel": admin_token}, base_url=settings.github.api_base_url)
    results = check_provisioning(settings, client, repo)
    ok = True
    for key, value in results.items():
        color = typer.colors.GREEN if value == "ok" else typer.colors.YELLOW
        if value != "ok":
            ok = False
        typer.secho(f"{key}: {value}", fg=color)
    if ok:
        typer.secho("All checks pass.", fg=typer.colors.GREEN)


@app.command("labels")
def cmd_labels(
    sub: str = typer.Argument("sync", help="subcommand (sync)"),
    url: str = typer.Argument(..., help="Repository URL or owner/repo"),
):
    """Create or update the label taxonomy (idempotent)."""
    if sub != "sync":
        typer.echo(f"unknown labels subcommand {sub!r}", err=True)
        raise typer.Exit(2)
    repo = _repo_from_url(url)
    settings = _load_settings(repo)
    from codie.github.labels import ensure_label_set
    from codie.github.pygithub_client import build_client

    client = build_client(repo, settings)
    n = ensure_label_set(client)
    typer.secho(f"ensured {n} labels", fg=typer.colors.GREEN)


@app.command("validate-config")
def cmd_validate_config(
    url: str | None = typer.Argument(None, help="Optional repository URL or owner/repo"),
):
    """Validate the effective config; redacts secrets (M17/M26)."""
    try:
        global_settings, global_path = load_global_settings()
        if global_settings is None:
            typer.secho("no global codie.yaml found — using built-in defaults", fg=typer.colors.YELLOW)
        if url:
            repo = _repo_from_url(url)
            overrides, _client = _project_overrides_from_head(repo, global_settings or Settings())
            settings = config.build_settings(global_settings, repo, overrides)
        else:
            from codie.config import ProjectOverrides

            overrides = ProjectOverrides()
            local = Path(".codie.yaml")
            if local.exists():
                overrides = config.load_overrides_text(local.read_text())
            settings = config.build_settings(global_settings, "local", overrides)
        text = settings.model_dump_json(indent=2)
        typer.secho(config.redact(text), fg=typer.colors.GREEN)
        typer.secho("configuration valid", fg=typer.colors.GREEN)
    except ConfigError as exc:
        typer.secho(f"configuration invalid: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc


@app.command("stop")
def cmd_stop(url: str = typer.Argument(..., help="Repository URL or owner/repo")):
    """Graceful stop: SIGTERM the running daemon (C14/N23)."""
    repo = _repo_from_url(url)
    owner, name = repo.split("/")
    pidfile = state_dir(owner, name) / "daemon.pid"
    if not pidfile.exists():
        typer.secho("not running", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    pid = int(pidfile.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        pidfile.unlink()
        typer.secho(f"SIGTERM sent to {pid}", fg=typer.colors.GREEN)
    except ProcessLookupError:
        pidfile.unlink()
        typer.secho("dead PID removed", fg=typer.colors.YELLOW)


def _write_pid(owner: str, name: str) -> None:
    d = state_dir(owner, name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "daemon.pid").write_text(str(os.getpid()))


def _remove_pid(owner: str, name: str) -> None:
    pidfile = state_dir(owner, name) / "daemon.pid"
    pidfile.unlink(missing_ok=True)


if __name__ == "__main__":
    app()

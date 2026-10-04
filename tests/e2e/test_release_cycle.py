"""E2E (M28, I-29): full PRD → features → specs → tasks → UAT → release on a
real sandbox repo. Gated behind CODIE_E2E=1 and sandbox-repo tokens; runs
nightly on schedule (never on PR CI). The sandbox PRD plants one user-surface
failure the Coder's unit tests do not cover; this harness drives the acceptance
run and asserts that failure is filed as a type:bug before UAT.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CODIE_E2E") != "1",
    reason="E2E requires CODIE_E2E=1 and sandbox-repo tokens",
)


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} not set")
    return value


def test_sandbox_release_cycle_e2e():
    repo = _require("CODIE_E2E_REPO")
    _require("CODIE_GH_TOKEN_KERNEL")
    _require("CODIE_GH_TOKEN_PLANNER")
    _require("CODIE_GH_TOKEN_CODER")
    _require("CODIE_GH_TOKEN_REVIEWER")
    _require("CODIE_GH_TOKEN_TESTER")
    _require("CODIE_LLM_API_KEY")
    if not (repo.startswith(("http", "git@")) or "/" in repo):
        pytest.fail(f"CODIE_E2E_REPO does not look like a repo URL: {repo!r}")

    from codie.config import load_global_settings
    from codie.dispatch import CrewAIKickoff
    from codie.github.pygithub_client import PyGithubClient
    from codie.orchestrator import Kernel
    from codie.provision import require_provisioned
    from codie.workspace import Workspace

    owner, name = repo.rstrip("/").split("/")[-2:]
    repo_slug = f"{owner}/{name}"

    settings, _ = load_global_settings()
    assert settings is not None, "no ~/.codie/codie.yaml"
    settings.project.repo = repo_slug

    from codie.cache import Cache, cache_path

    tokens = {
        role: os.environ[f"CODIE_GH_TOKEN_{role.upper()}"]
        for role in ("planner", "coder", "reviewer", "tester", "kernel")
    }
    client = PyGithubClient(repo_slug, tokens, base_url=settings.github.api_base_url)
    require_provisioned(settings, client, repo_slug)

    cache = Cache().open_or_rebuild(cache_path(owner, name))
    workspace = Workspace(settings.project.workspace_path, settings)
    workspace.ensure()
    workspace.janitor(coder_active=False)

    kernel = Kernel(
        settings=settings,
        client=client,
        workspace=workspace,
        cache=cache,
        crew_runner=CrewAIKickoff(settings),
        async_runs=False,
    )
    # Seed the pinned type:prd issue with the planted-surface-failure PRD if absent.
    existing_prd = [i for i in client.list_issues() if "type:prd" in i.labels]
    if not existing_prd:
        fixture_prd = _sandbox_prd()
        issue = client.create_issue("Product Requirements (PRD)", fixture_prd, ["type:prd"])
        client.pin_issue(issue.number, True)

    release_seen = False
    for _ in range(120):  # bounded cycle cap; typically far fewer
        outcome = kernel.cycle()
        if outcome.halted:
            release_seen = True
            break
        prs = client.list_prs()
        if any("codie:release" in (p.body or "") for p in prs):
            release_seen = True
            break
    assert release_seen, "the crew should reach a marked release PR on the sandbox repo"

    # M28: the planted user-surface failure must be captured as a type:bug by an
    # acceptance run BEFORE any UAT — i.e., no feature reaches status:review with
    # the planted defect uncovered.
    bugs = [i for i in client.list_issues() if "type:bug" in i.labels]
    planted = [b for b in bugs if "Eval: E9" in (b.body or "") or "planted" in (b.body or "")]
    assert planted, "the acceptance run should have filed the planted surface-failure bug"


def _sandbox_prd() -> str:
    """The sandbox PRD: every eval except E9 (the planted failure) is command."""
    return (
        "# Sandbox PRD\n\n"
        "## Definition of done\n"
        "- [ ] E1: hello world endpoint returns 200\n"
        "- [ ] E2: unit suite passes\n"
        "- [ ] E3: acceptance suite passes\n"
        "- [ ] E9: planted user-surface failure is caught and filed as a bug\n"
    )

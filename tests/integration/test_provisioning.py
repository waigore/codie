"""Provisioning tests — `codie init` phase-3 effects against the fake (§9.6/M1)."""

from tests.conftest import make_settings

from codie.config import Registry
from codie.github.fake import FakeGitHub
from codie.models import Finding, IssueTriage, PrdProposal, RepoSurvey
from codie.provision import check_provisioning, provision


def settings_for(repo="acme/test"):
    return make_settings(repo)


def base_survey() -> RepoSurvey:
    return RepoSurvey(
        dimensions={"branches": Finding(value="main/dev", source="default", confidence="high", evidence=["x"])},
        issue_triage=[],
        prd=PrdProposal(source="none"),
    )


def test_provision_registers_branches_labels_prd(tmp_path, monkeypatch):
    monkeypatch.setenv("P", "t")
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    gh = FakeGitHub("acme/test")
    gh.bootstrap_branches("main")
    gh.add_issue(1, "existing issue", "what is this?", labels=[])
    survey = base_survey()
    survey.issue_triage = [
        IssueTriage(
            number=1,
            proposed_type="bug",
            proposed_status="ready",
            rationale="sounds like a bug",
            confidence="low",
        )
    ]
    reg = Registry()
    report = provision(
        settings_for(),
        gh,
        survey,
        "acme/test",
        registry=reg,
        prd_content="# PRD\n\n## Definition of done\n- [ ] E1: x\n",
    )
    assert report.prd_issue_number == 2
    assert gh.has_label("type:feature") is True
    # issue adoption
    assert "type:bug" in gh.labels_of(1)
    assert "status:ready" in gh.labels_of(1)
    # branches
    assert gh.get_ref("dev") == gh.get_ref("main")
    # onboarding PR + files
    onboarding = [p for p in gh.list_prs() if "onboarding" in p.title.lower()]
    assert onboarding, "expected an onboarding PR"
    assert gh.get_file(".codie.yaml", "codie/onboarding") is not None
    assert gh.get_file("AGENTS.md", "codie/onboarding") is not None
    assert gh.get_file("specs/_survey/architecture.md", "codie/onboarding") is not None


def test_provision_idempotent_second_run(tmp_path, monkeypatch):
    gh = FakeGitHub("acme/test")
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    gh.bootstrap_branches("main")
    reg = Registry()
    provision(settings_for(), gh, base_survey(), "acme/test", registry=reg, prd_content="# PRD\n")
    first_prs = len(gh.list_prs())
    provision(settings_for(), gh, base_survey(), "acme/test", registry=reg, prd_content="# PRD\n")
    assert len(gh.list_prs()) == first_prs  # no duplicate onboarding branch/PR
    prds = [i for i in gh.list_issues() if "type:prd" in i.labels]
    assert len(prds) == 1


def test_provision_no_prd_pending_note(tmp_path, monkeypatch):
    gh = FakeGitHub("acme/test")
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    gh.bootstrap_branches("main")
    reg = Registry()
    report = provision(settings_for(), gh, base_survey(), "acme/test", registry=reg)
    assert report.prd_issue_number is None
    assert any("No PRD yet" in line for line in report.pending)


def test_labels_sync_creates_taxonomy(monkeypatch):
    gh = FakeGitHub("acme/test")
    from codie.github.labels import ensure_label_set

    n = ensure_label_set(gh)
    assert n == len(gh.labels)
    assert all(name in gh.labels for name in ("type:prd", "status:spec-review", "flag:needs-human"))


def test_check_provisioning_reports(monkeypatch):
    gh = FakeGitHub("acme/test")
    gh.bootstrap_branches("main", "dev")
    results = check_provisioning(settings_for(), gh, "acme/test")
    assert "branch:main" in results
    assert "branch:dev" in results
    assert "labels" in results

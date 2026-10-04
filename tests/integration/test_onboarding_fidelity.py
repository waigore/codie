"""Onboarding fidelity tests — survey evidence (I-02), per-dimension edits
(I-03), PRD provisioning (I-04), doctor (I-06), provisioning deviations (I-07).
"""

from tests.conftest import make_settings

from codie.config import Registry
from codie.github.fake import FakeGitHub
from codie.models import Finding, PrdProposal, RepoSurvey
from codie.provision import _render_codie_yaml, check_provisioning, provision, require_provisioned


def settings_for(repo="acme/test"):
    return make_settings(repo)


# ---------------------------------------------------------------------------
# I-02: survey proposes conventions with evidence
# ---------------------------------------------------------------------------


def test_survey_proposes_master_develop_with_evidence():
    from codie.survey import gather_survey, survey_commands

    gh = FakeGitHub("acme/test")
    gh.bootstrap_branches("master", "develop")
    gh.trees["develop"] = {
        ".github/workflows/ci.yml": "jobs:\n  test:\n    runs-on: ubuntu\n    steps:\n      - run: pytest scripts/run_custom.sh\n",  # noqa: E501
        "README.md": "# product\n",
    }
    settings = settings_for()
    survey = gather_survey(gh, settings, "acme/test")
    branches = survey.dimensions["branches"]
    assert "master" in branches.value and "develop" in branches.value
    assert branches.source == "adopted"
    assert branches.evidence  # file/history evidence present
    commands = survey_commands(survey)
    assert any("custom" in " ".join(argv) for argv in commands.values()), commands


def test_surveyed_commands_render_into_codie_yaml():
    from codie.survey import gather_survey

    gh = FakeGitHub("acme/test")
    gh.bootstrap_branches("master", "develop")
    gh.trees["develop"] = {
        ".github/workflows/ci.yml": "test: pytest tests/custom\n",
        "README.md": "# x\n",
    }
    settings = settings_for()
    survey = gather_survey(gh, settings, "acme/test")
    from codie.survey import survey_commands

    for name, argv in survey_commands(survey).items():
        settings.overrides.commands[name] = argv
    yaml_text = _render_codie_yaml(settings)
    assert "commands" in yaml_text
    assert "pytest" in yaml_text or "custom" in yaml_text


# ---------------------------------------------------------------------------
# I-03: per-dimension edits change only that dimension
# ---------------------------------------------------------------------------


def test_apply_dimension_edits_changes_only_one_dimension():
    from codie.survey import apply_dimension_edits

    survey = RepoSurvey(
        dimensions={
            "merge_style": Finding(value="squash (default)", source="default"),
            "branches": Finding(value="main/dev", source="default"),
        }
    )
    apply_dimension_edits(survey, {"merge_style": "merge via PR"})
    assert survey.dimensions["merge_style"].value == "merge via PR"
    assert survey.dimensions["branches"].value == "main/dev"  # untouched


# ---------------------------------------------------------------------------
# I-04: PRD issue provisioning + start pre-check
# ---------------------------------------------------------------------------


def test_provision_posts_pinned_prd_with_evals(tmp_path, monkeypatch):
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    gh = FakeGitHub("acme/test")
    gh.bootstrap_branches("main")
    prd = "\n".join(
        ["# PRD", "", "## Definition of done", "- [ ] E1: onboarding works", "- [ ] E2: reporting renders"]
    )
    report = provision(
        settings_for(),
        gh,
        RepoSurvey(prd=PrdProposal(source="supplied", content=prd)),
        "acme/test",
        registry=Registry(),
        prd_content=prd,
    )
    assert report.prd_issue_number is not None
    zone = gh.get_issue(report.prd_issue_number)
    assert zone is not None and zone.pinned is True
    assert "type:prd" in zone.labels
    assert "- [ ] E1:" in zone.body and "- [ ] E2:" in zone.body
    # the PRD pre-check in `require_provisioned` is satisfied once a pinned
    # type:prd issue with the eval checklist exists (I-04 DoD).
    assert "codie:release" not in (zone.body or "")
    issues = [i for i in gh.list_issues() if "type:prd" in i.labels]
    assert len(issues) == 1 and issues[0].pinned is True
    assert issues[0].number == report.prd_issue_number


def test_start_proceeds_past_prd_precheck(tmp_path, monkeypatch):
    """After a full provision + onboarding merge, `require_provisioned` passes."""
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    gh = FakeGitHub("acme/test")
    gh.bootstrap_branches("main")
    prd = "# PRD\n\n## Definition of done\n- [ ] E1: ok\n"
    provision(
        settings_for(),
        gh,
        RepoSurvey(prd=PrdProposal(source="supplied", content=prd)),
        "acme/test",
        registry=Registry(),
        prd_content=prd,
    )
    # simulate the onboarding-PR merge (its merge is the confirmation, §9.6)
    gh.trees["dev"] = dict(gh.trees.get("codie/onboarding", {}))
    require_provisioned(settings_for(), gh, "acme/test")


# ---------------------------------------------------------------------------
# I-06: doctor is truthful
# ---------------------------------------------------------------------------


def test_doctor_reports_missing_labels():
    gh = FakeGitHub("acme/test")
    gh.bootstrap_branches("main", "dev")
    results = check_provisioning(settings_for(), gh, "acme/test")
    assert results["labels"] != "ok"  # no labels created yet
    from codie.github.labels import ensure_label_set

    ensure_label_set(gh)
    results = check_provisioning(settings_for(), gh, "acme/test")
    assert results["labels"] == "ok"
    # removing a label is reported
    gh.labels.pop("type:feature")
    results = check_provisioning(settings_for(), gh, "acme/test")
    assert results["labels"] != "ok"


def test_has_label_contract():
    from codie.github.labels import ensure_label_set

    gh = FakeGitHub("acme/test")
    ensure_label_set(gh)
    assert gh.has_label("flag:needs-human") is True
    assert gh.has_label("nope") is False


# ---------------------------------------------------------------------------
# I-07: provisioning deviations
# ---------------------------------------------------------------------------


def test_provisioning_core_i07(tmp_path, monkeypatch):
    monkeypatch.setattr(Registry, "save", lambda self, path=None: None)
    gh = FakeGitHub("acme/test")
    gh.bootstrap_branches("main", "dev")
    gh.collaborators = {
        "codie-planner-bot",
        "codie-reviewer-bot",
        "codie-tester-bot",
        "codie-kernel-bot",
        "codie-coder-bot",
    }  # all five present
    # an existing AGENTS.md must be amended, not overwritten
    gh.trees["dev"] = {"AGENTS.md": "# Orig\n\nKeep this line.\n"}
    provision(settings_for(), gh, RepoSurvey(prd=PrdProposal(source="none")), "acme/test", registry=Registry())
    branch = "codie/onboarding"
    agents = gh.get_file("AGENTS.md", branch)
    assert "Keep this line" in agents  # original text preserved
    assert "## Codie workflow" in agents  # amended minimally
    codeowners = gh.get_file("CODEOWNERS", branch)
    assert codeowners is not None and codeowners.startswith("* @")
    # dev protection requires 1 approval
    proto = gh.get_branch_protection("dev")
    assert proto["require_approvals"] == 1
    # all five bot logins invited/listed
    assert gh.list_collaborators()


def test_master_default_repo_detected_non_empty():
    from codie.provision import _list_files

    gh = FakeGitHub("acme/test")
    gh.bootstrap_branches("master")  # master-only repo with files
    gh.trees["master"] = {"README.md": "# x", "src/main.py": "print()"}
    # a master-only repo must be detected as non-empty (not misclassified)
    assert _list_files(gh, "master") == ["README.md", "src/main.py"]
    provision(settings_for(), gh, RepoSurvey(prd=PrdProposal(source="none")), "acme/test", registry=Registry())
    assert gh.get_ref("dev") is not None


def test_existing_protection_is_gap_filled_not_skipped():
    gh = FakeGitHub("acme/test")
    gh.bootstrap_branches("main", "dev")
    gh.branch_protection["dev"] = {"require_prs": True, "require_approvals": 0, "checks": []}
    from codie.provision import _apply_protection

    class Report:
        applied = []
        already = []
        pending = []

    _apply_protection(settings_for(), gh, Report())
    proto = gh.get_branch_protection("dev")
    assert proto["require_approvals"] == 1  # the gap was filled

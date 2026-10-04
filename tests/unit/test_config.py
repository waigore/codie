"""Unit tests for config.py — models, merge, validation, URL parsing, registry."""

import pytest
import yaml

from codie import config
from codie.config import (
    ConfigError,
    ProjectOverrides,
    Settings,
    build_settings,
    parse_repo_url,
    validate_effective,
)


def test_parse_repo_url_forms():
    assert parse_repo_url("https://github.com/owner/repo") == ("owner", "repo")
    assert parse_repo_url("https://github.com/owner/repo.git") == ("owner", "repo")
    assert parse_repo_url("git@github.com:owner/repo.git") == ("owner", "repo")
    assert parse_repo_url("owner/repo") == ("owner", "repo")
    with pytest.raises(ConfigError):
        parse_repo_url("not-a-url")


def test_global_settings_from_yaml(tmp_path):
    path = tmp_path / "codie.yaml"
    path.write_text(
        "llm:\n  default_model: gpt-4o\n  price_map:\n    gpt-4o: {input_per_1k: 0.005, output_per_1k: 0.015}\n"
        "github:\n  tokens:\n    planner: {env: P, login: pbot}\n"
        "dashboard:\n  enabled: false\n"
    )
    settings, p = config.load_global_settings(str(path))
    assert settings is not None
    assert settings.llm.default_model == "gpt-4o"
    assert settings.github.login("planner") == "pbot"
    assert settings.dashboard.enabled is False


def test_unknown_keys_are_errors():
    with pytest.raises(Exception):  # noqa: B017
        ProjectOverrides.model_validate({"not_a_field": 1})


def test_project_overrides_parse():
    data = yaml.safe_load(
        "branches:\n  main: master\n  dev: develop\n"
        "merge_method: merge\n"
        "commands:\n  setup: [pip, install, -e, .]\n"
        "workflow:\n  bugs_outrank_features: false\n"
        "guardrails:\n  max_cycles_per_work_item: 5\n"
    )
    ov = ProjectOverrides.model_validate(data)
    assert ov.branches.dev == "develop"
    assert ov.merge_method == "merge"
    assert ov.workflow.bugs_outrank_features is False
    assert ov.guardrails.max_cycles_per_work_item == 5


def test_validate_effective_requires_commands():
    with pytest.raises(ConfigError) as exc:
        build_settings(Settings(), "a/b", ProjectOverrides(commands={}))
    assert "commands.setup is required" in str(exc.value)


def test_surface_framework_matrix_rejects_bad_pair():
    ov = ProjectOverrides(surface="ui-web", commands={"setup": ["x"], "test_fast": ["x"], "run_app": ["x"]})
    with pytest.raises(ConfigError) as exc:
        build_settings(Settings(), "a/b", ov)
    assert "framework" in str(exc.value)


def test_run_app_required_unless_library():
    ov = ProjectOverrides(surface="cli", commands={"setup": ["x"], "test_fast": ["x"]})
    with pytest.raises(ConfigError):
        build_settings(Settings(), "a/b", ov)
    ov2 = ProjectOverrides(
        surface="library",
        acceptance=config.Acceptance(framework="examples", dir="tests/acceptance"),
        commands={"setup": ["x"], "test_fast": ["x"]},
    )
    build_settings(Settings(), "a/b", ov2)  # ok


def test_evals_run_or_human_required():
    # an eval with neither `run` nor `mode: human` is a config error (M32)
    ov = ProjectOverrides(
        commands={"setup": ["x"], "test_fast": ["x"], "run_app": ["x"]},
        evals={"E1": {}},
    )
    with pytest.raises(ConfigError):
        validate_effective(build_settings(Settings(), "a/b", ov))
    ov2 = ProjectOverrides(
        commands={"setup": ["x"], "test_fast": ["x"], "run_app": ["x"]},
        evals={"E1": {"run": ["pytest"], "expect": "exit_zero"}, "E2": {"mode": "human"}},
    )
    validate_effective(build_settings(Settings(), "a/b", ov2))  # ok


def test_token_login_required_m16():
    settings = build_settings(
        Settings(),
        "a/b",
        ProjectOverrides(commands={"setup": ["x"], "test_fast": ["x"], "run_app": ["x"]}),
    )
    settings.github.tokens = {"planner": config.TokenSpec(env="P")}  # missing login
    with pytest.raises(ConfigError) as exc:
        validate_effective(settings)
    assert "login" in str(exc.value)


def test_registry_roundtrip(tmp_path):
    reg = config.Registry()
    reg.upsert("a/b", workspace="/tmp/w", poll_interval_seconds=30)
    reg.save(tmp_path / "r.yaml")
    loaded = config.Registry.load(tmp_path / "r.yaml")
    assert loaded.get("a/b").workspace == "/tmp/w"


def test_redact_hides_secrets():
    out = config.redact('api_key: "sk-abc123"')
    assert "sk-abc123" not in out
    assert "***" in out


def test_settings_build_merges_project():
    settings = build_settings(
        Settings(),
        "a/b",
        ProjectOverrides(commands={"setup": ["x"], "test_fast": ["y"], "run_app": ["z"]}),
    )
    assert settings.project.repo == "a/b"
    assert settings.overrides.command("setup") == ["x"]

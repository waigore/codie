"""Shared test fixtures and helpers."""

from __future__ import annotations

import pytest

from codie.config import ProjectOverrides, Settings, build_settings


def make_settings(repo: str = "acme/test", commands: dict | None = None) -> Settings:
    cmds = commands or {
        "setup": ["true"],
        "build": ["true"],
        "run_app": ["true"],
        "test_fast": ["true"],
        "test_full": ["true"],
        "test_acceptance": ["true"],
        "lint": ["true"],
    }
    overrides = ProjectOverrides(commands=cmds)
    settings = build_settings(Settings(), repo, overrides)
    settings.github.tokens = {
        "planner": type("T", (), {"env": "P", "login": "codie-planner-bot"})(),
        "coder": type("T", (), {"env": "C", "login": "codie-coder-bot"})(),
        "reviewer": type("T", (), {"env": "R", "login": "codie-reviewer-bot"})(),
        "tester": type("T", (), {"env": "S", "login": "codie-tester-bot"})(),
        "kernel": type("T", (), {"env": "K", "login": "codie-kernel-bot"})(),
    }
    return settings


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def fresh_fake(repo: str = "acme/test"):
    from codie.github.fake import FakeGitHub

    gh = FakeGitHub(repo)
    gh.bot_logins = {
        "codie-planner-bot",
        "codie-coder-bot",
        "codie-reviewer-bot",
        "codie-tester-bot",
        "codie-kernel-bot",
    }
    gh.map_roles(
        {
            "planner": "codie-planner-bot",
            "coder": "codie-coder-bot",
            "reviewer": "codie-reviewer-bot",
            "tester": "codie-tester-bot",
            "kernel": "codie-kernel-bot",
        }
    )
    gh.bootstrap_branches("main", "dev")
    return gh


PRD_BODY = """# Acme Product

## Description
A simple product with real tests.

## Definition of done
- [ ] E1: onboarding works
- [ ] E2: reporting renders
"""


def add_prd(gh, body: str = PRD_BODY, number: int = 1) -> int:
    gh.add_issue(number, "Product Requirements (PRD)", body, labels=["type:prd"])
    return number

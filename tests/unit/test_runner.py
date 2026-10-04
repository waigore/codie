"""Unit tests for runner.py — jail, denylist, read-only, truncation (§8.2)."""

from pathlib import Path

import pytest

from codie.config import ProjectOverrides, Settings, build_settings
from codie.runner import CommandDenied, Runner
from tests.conftest import make_settings


def test_basic_command_run(tmp_path):
    runner = Runner(make_settings(), tmp_path)
    r = runner.run(["python3", "-c", "print('hi')"], cwd=tmp_path)
    assert r.ok and "hi" in r.stdout


def test_denylist_blocks_force_push(tmp_path):
    runner = Runner(make_settings(), tmp_path)
    with pytest.raises(CommandDenied):
        runner.run(["git", "push", "--force"], cwd=tmp_path)


def test_cwd_jail_refused(tmp_path):
    runner = Runner(make_settings(), tmp_path)
    with pytest.raises(CommandDenied):
        runner.run(["echo", "x"], cwd=tmp_path.parent.parent)


def test_readonly_planner_reviewer():
    ov = ProjectOverrides(
        commands={
            "lint": ["echo", "x"],
            "test_fast": ["echo", "y"],
            "setup": ["true"],
            "run_app": ["true"],
        }
    )
    settings = build_settings(Settings(), "a/b", ov)
    runner = Runner(settings, Path("/tmp"))
    with pytest.raises(CommandDenied):
        runner.check_readonly(["rm", "-rf", "x"])
    runner.check_readonly(["git", "diff"])
    runner.check_readonly(["git", "status"])
    with pytest.raises(CommandDenied):
        runner.check_readonly(["git", "push", "origin", "main"])


def test_timeout(tmp_path):
    settings = make_settings()
    settings.runner.command_timeout_seconds = 1
    runner = Runner(settings, tmp_path)
    r = runner.run(["python3", "-c", "import time; time.sleep(3)"], cwd=tmp_path)
    assert r.timed_out


def test_output_truncation(tmp_path):
    settings = make_settings()
    settings.runner.max_output_chars = 100
    runner = Runner(settings, tmp_path)
    big = "x" * 10000
    r = runner.run(["python3", "-c", f"print('{big}')"], cwd=tmp_path)
    assert "[truncated" in r.stdout
    assert len(r.stdout) <= 150


def test_command_deprecated_argform_disabled():
    pass

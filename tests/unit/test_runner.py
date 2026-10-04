"""Unit tests for runner.py — jail, denylist, read-only, truncation, session (§8.2)."""

import socket
import time
from pathlib import Path

import pytest

from codie.config import ProjectOverrides, RunAppSession, Settings, build_settings
from codie.models import Eval
from codie.runner import CommandDenied, Runner, run_eval_commands
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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _listener_script(path: Path, port: int) -> str:
    """A tiny deterministic listener the runner can manage as the app under test."""
    script = path / "listener.py"
    script.write_text(
        "import socket, time\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"s.bind(('127.0.0.1', {port}))\n"
        "s.listen(5)\n"
        "while True:\n"
        "    try:\n"
        "        c, _ = s.accept(); c.close()\n"
        "    except Exception:\n"
        "        time.sleep(0.1)\n"
    )
    return str(script)


def test_session_launches_waits_for_ports_and_stops(tmp_path):
    """I-21: a dummy listening app starts a session, the runner waits on the port,
    the suite would run, and the session is torn down at the end."""
    port = _free_port()
    script = _listener_script(tmp_path, port)
    ov = ProjectOverrides(
        commands={
            "setup": ["true"],
            "run_app": ["python3", script],
            "test_fast": ["python3", "-c", "print('suite-ran')"],
            "test_acceptance": ["true"],
        },
        run_app_session=RunAppSession(ports=[port], display="headless"),
    )
    settings = build_settings(Settings(), "a/b", ov)
    runner = Runner(settings, tmp_path)
    session = runner.run_app_session(tmp_path, timeout=20)
    assert session.proc.poll() is None  # the managed app is running
    # the suite can run against the live product while the session is up
    r = runner.run(["python3", "-c", "print('suite-ran')"])
    assert r.ok and "suite-ran" in r.stdout
    session.stop()
    assert _waits_for_exit(session.proc)  # torn down


def _waits_for_exit(proc, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.1)
    return False


def test_session_times_out_when_ports_never_open(tmp_path):
    from codie.config import ConfigError

    port = _free_port()
    ov = ProjectOverrides(
        commands={"setup": ["true"], "run_app": ["true"], "test_fast": ["true"]},
        run_app_session=RunAppSession(ports=[port], display="headless"),
    )
    settings = build_settings(Settings(), "a/b", ov)
    runner = Runner(settings, tmp_path)
    with pytest.raises(ConfigError):  # no listener within the timeout
        runner.run_app_session(tmp_path, timeout=1)


def test_xvfb_is_a_side_process_stopped_with_session(tmp_path):
    """I-21: display: xvfb starts Xvfb as a Popen side-process, killed at stop."""
    port = _free_port()
    script = _listener_script(tmp_path, port)
    ov = ProjectOverrides(
        commands={
            "setup": ["true"],
            "run_app": ["python3", script],
            "test_fast": ["true"],
        },
        run_app_session=RunAppSession(ports=[port], display="xvfb"),
    )
    settings = build_settings(Settings(), "a/b", ov)
    runner = Runner(settings, tmp_path)
    try:
        session = runner.run_app_session(tmp_path, timeout=20)
    except Exception:
        pytest.skip("Xvfb not installed on this machine")
    assert session.xvfb is not None
    session.stop()
    assert _waits_for_exit(session.proc)
    assert _waits_for_exit(session.xvfb)  # Xvfb torn down too


def test_eval_commands_run_with_denylist(tmp_path):
    """I-18: eval commands execute through the jailed runner; the denylist applies."""
    settings = make_settings()
    evals = [Eval(id="E1", text="x", checked=False, command=["python3", "-c", "print('ok')"], mode="command")]
    results = run_eval_commands(settings, tmp_path, evals)
    assert results["E1"] is True
    evil = [Eval(id="E2", text="y", checked=False, command=["git", "push", "-f"], mode="command")]
    with pytest.raises(CommandDenied):
        run_eval_commands(settings, tmp_path, evil)  # denylisted argv refuses

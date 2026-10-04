"""Jailed subprocess execution (Technical Spec §8.2).

`commands.*` and `evals.*` entries are argv lists executed without a shell; the
`shell_denylist` is matched against the argv joined by single spaces; `cwd` is
jailed inside the workspace. Session mode launches `run_app` as a managed
background process group for the Tester.
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codie.config import ConfigError, Settings


class CommandDenied(Exception):
    """A command was refused by the denylist or cwd jail."""


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "CC",
    "CXX",
    "CFLAGS",
    "LDFLAGS",
    "JAVA_HOME",
    "GOROOT",
    "GOPATH",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "VIRTUAL_ENV",
    "CODIE_DISPLAY",
    "CODIE_ROLE",
    "TERM",
)

# Read-only commands (Planner / Reviewer run_command, §8.2).
READONLY_ARGTYPES: tuple[str, ...] = ("fetch", "diff", "log", "show", "status")


class Runner:
    def __init__(self, settings: Settings, workspace_root: Path):
        self.settings = settings
        self.workspace_root = workspace_root.resolve()

    # -- guards -----------------------------------------------------------------

    def check_denylist(self, argv: list[str]) -> None:
        text = " ".join(str(a) for a in argv)
        for pattern in self.settings.guardrails.shell_denylist:
            if fnmatch.fnmatch(text, pattern):
                raise CommandDenied(f"command {text!r} matched shell_denylist pattern {pattern!r}")

    def check_cwd(self, cwd: Path | None) -> Path:
        base = (cwd or self.workspace_root).resolve()
        if base != self.workspace_root and not str(base).startswith(str(self.workspace_root) + os.sep):
            raise CommandDenied(f"cwd {base} is outside the workspace jail {self.workspace_root}")
        return base

    def check_readonly(self, argv: list[str]) -> None:
        """Planner/Reviewer shell access is read-only (§8.2)."""
        if argv and argv[0] == "git" and len(argv) > 1:
            if argv[1] in READONLY_ARGTYPES or argv[1].startswith("-"):
                return
            raise CommandDenied("reviewer/planner git access is limited to fetch/diff/log/show/status")
        allowed = (
            (self.settings.overrides.command("lint") or ["nonexistent"])[0]
            if self.settings.overrides.commands
            else "nonexistent"
        )
        if argv and argv[0] not in {allowed, *self.settings.overrides.commands.get("lint", [])}:
            raise CommandDenied("reviewer/planner run_command is read-only (lint/test_fast only)")

    # -- execution ----------------------------------------------------------------

    def run(
        self,
        argv: list[str],
        cwd: Path | None = None,
        timeout: int | None = None,
        env_extra: dict[str, str] | None = None,
        capture: bool = True,
    ) -> CommandResult:
        self.check_denylist(argv)
        base = self.check_cwd(cwd)
        timeout = timeout or self.settings.runner.command_timeout_seconds
        env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
        env.update(env_extra or {})
        start = time.monotonic()
        try:
            proc = subprocess.run(
                [str(a) for a in argv],
                cwd=base,
                env=env,
                shell=False,
                capture_output=capture,
                text=True,
                timeout=timeout,
            )
            return CommandResult(
                exit_code=proc.returncode,
                stdout=_truncate(proc.stdout or "", self.settings.runner.max_output_chars),
                stderr=_truncate(proc.stderr or "", self.settings.runner.max_output_chars),
                duration_s=time.monotonic() - start,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _truncate(_to_text(exc.stdout or b""), self.settings.runner.max_output_chars)
            stderr = _truncate(_to_text(exc.stderr or b""), self.settings.runner.max_output_chars)
            return CommandResult(
                exit_code=-1,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                duration_s=time.monotonic() - start,
            )

    def run_commands(self) -> dict[str, list[str]]:
        return self.settings.overrides.commands

    def command(self, name: str) -> list[str]:
        cmd = self.settings.overrides.command(name)
        if cmd is None:
            raise ConfigError(f"commands.{name} is not configured for this project")
        return cmd

    # -- session mode (§8.2) -------------------------------------------------------

    def run_app_session(self, cwd: Path, env_extra: dict[str, str] | None = None, timeout: int | None = None):
        """Launch `run_app` as a managed session, wait for declared ports."""
        argv = self.command("run_app")
        self.check_denylist(argv)
        base = self.check_cwd(cwd)
        timeout = timeout or self.settings.runner.command_timeout_seconds
        env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
        env.update(self.settings.overrides.run_app_session.env)
        env["CODIE_DISPLAY"] = self.settings.overrides.run_app_session.display
        env.update(env_extra or {})
        xvfb: subprocess.Popen | None = None
        if self.settings.overrides.run_app_session.display == "xvfb":
            # launch Xvfb as a side-process Popen (never a blocking run), stopped
            # at work-item end through the same kill path as the app (I-21).
            try:
                xvfb = subprocess.Popen(
                    ["Xvfb", ":99", "-screen", "0", "1024x768x24"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise ConfigError("Xvfb not found; install xvfb or set display: headless") from exc
            env["DISPLAY"] = ":99"
        elif self.settings.overrides.run_app_session.display == "native":
            if "DISPLAY" not in os.environ:
                raise ConfigError("display: native requires DISPLAY to be set")
            env["DISPLAY"] = os.environ["DISPLAY"]
        proc = subprocess.Popen(
            [str(a) for a in argv],
            cwd=base,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        started = _wait_for_ports(self.settings.overrides.run_app_session.ports, timeout)
        if not started:
            _kill(proc)
            if xvfb is not None:
                _kill(xvfb)
            raise ConfigError("run_app session did not accept its declared ports in time")
        return _Session(proc, xvfb)


def run_eval_commands(settings: Settings, workspace_root, evals, timeout: int | None = None) -> dict[str, bool]:
    """Run every command eval via the jailed runner; returns {id: passed} (§7.5/§7.7).

    `human` evals and evals without a `run` command are skipped (the crew flags
    them uncertain). The denylist and cwd jail apply to every execution (I-18).
    """
    runner = Runner(settings, Path(workspace_root))
    results: dict[str, bool] = {}
    for entry in evals:
        if not entry.command:
            continue  # human eval — the checkbox waits on the human
        command = list(entry.command)
        runner.check_denylist(command)
        result = runner.run(command, timeout=timeout)
        results[entry.id] = result.ok
    return results


@dataclass
class _Session:
    proc: subprocess.Popen
    xvfb: subprocess.Popen | None = None

    def stop(self) -> None:
        _kill(self.proc)
        if self.xvfb is not None:
            _kill(self.xvfb)


def _wait_for_ports(ports: list[int], timeout: int, interval: float = 0.5) -> bool:
    if not ports:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(_port_accepts(p) for p in ports):
            return True
        time.sleep(interval)
    return False


def _port_accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), 15)  # type: ignore[arg-type]
    except Exception:
        with contextlib.suppress(Exception):
            proc.terminate()


def _to_text(value: str | bytes) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def join_argv(argv: list[str]) -> str:
    return " ".join(shlex.quote(str(a)) for a in argv)


def utcnow() -> str:
    return datetime.now(UTC).isoformat()

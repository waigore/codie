"""Workspace manager (Technical Spec §8.1).

A single persistent clone reused across runs; Coder owns the main checkout;
Reviewer/Tester/Planner work in temporary `git worktree` checkouts. Every push
is authenticated as the acting role via a per-role credential helper.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path
from typing import Any

from codie.config import ConfigError, Settings


class WorkspaceLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh: Any | None = None

    def acquire(self, blocking: bool = False) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")  # noqa: SIM115 -- the handle must outlive this method
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


class Workspace:
    def __init__(self, path: Path, settings: Settings, remote_url: str | None = None):
        self.path = path.resolve()
        self.settings = settings
        self.remote_url = remote_url or f"https://github.com/{settings.project.repo}.git"
        self.lock = WorkspaceLock(path.parent / f"{path.name}.lock")

    # -- plumbing ----------------------------------------------------------------

    def git(
        self, *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        cwd = cwd or self.path
        full_env = dict(os.environ)
        full_env.update(env or {})
        full_env.pop("GIT_ASKPASS", None)
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=full_env,
            timeout=300,
        )

    def _ok(self, result: subprocess.CompletedProcess, what: str) -> str:
        if result.returncode != 0:
            raise ConfigError(f"{what} failed: {result.stderr.strip() or result.stdout.strip()}")
        return (result.stdout or "").strip()

    # -- lifecycle ----------------------------------------------------------------

    def ensure(self) -> None:
        if not (self.path / ".git").exists():
            self.path.mkdir(parents=True, exist_ok=True)
            self._ok(self.git("clone", self.remote_url, str(self.path)), "clone")
        else:
            self.git("fetch", "--all", "--prune")
        self._wire_credential_helper()

    def _wire_credential_helper(self) -> None:
        """§8.1: every fetch is authenticated as the kernel identity (tokenless remote)."""
        try:
            helper = _credential_helper(self.path, self.settings, "kernel")
            self.git("config", "credential.helper", f"f!/bin/sh -c 'cat {helper}'")
        except ConfigError:
            pass  # kernel token not resolvable (tests); leave tokenless fetch

    def require_repo(self) -> None:
        if not (self.path / ".git").exists():
            self.ensure()

    def integration_branch(self) -> str:
        return self.settings.branches.dev

    def reset_to_integration(self) -> None:
        """Janitor reset of the main checkout to origin/<dev>."""
        self.require_repo()
        branch = self.integration_branch()
        self.git("checkout", branch)
        self.git("reset", "--hard", f"origin/{branch}")
        self.git("clean", "-fd")

    def janitor(self, coder_active: bool) -> None:
        """Abort in-progress merges, stash surprises, prune merged branches."""
        self.require_repo()
        self.git("merge", "--abort")
        status = self.git("status", "--porcelain")
        if (status.stdout or "").strip() and not coder_active:
            self.git("stash", "push", "-u", "-m", f"codie-stash-{int(__import__('time').time())}")
        self.reset_to_integration()
        if not coder_active:
            self.git("branch", "-vv")
            self.git("merge-base", "--is-ancestor", "HEAD", "HEAD")  # no-op guard
            for line in (
                self.git("branch", "--merged", f"origin/{self.integration_branch()}").stdout or ""
            ).splitlines():
                name = line.strip().lstrip("* ").strip()
                if (
                    name
                    and name not in {self.integration_branch(), self.settings.branches.main}
                    and "codie/stash" not in name
                ):
                    self.git("branch", "-d", name)

    # -- branches & commits ---------------------------------------------------------

    def checkout_base_and_branch(self, branch: str) -> None:
        self.integration_branch()
        self.reset_to_integration()
        existing = self.git("rev-parse", "--verify", f"origin/{branch}").stdout.strip()
        if existing:
            self.git("checkout", "-B", branch, f"origin/{branch}")
        else:
            self.git("checkout", "-b", branch)

    def commit_all(self, message: str) -> None:
        self.git("add", "-A")
        r = self.git("commit", "-m", message, "--no-verify")
        if r.returncode != 0 and "nothing to commit" not in (r.stderr + r.stdout).lower():
            self._ok(r, "commit")

    def push(self, branch: str, role: str) -> None:
        """Push the current branch as the acting role (per-role credential helper)."""
        from codie.config import resolve_role_token

        token = resolve_role_token(self.settings, role)
        login = self.settings.github.login(role)
        askpass = _write_askpass(self.path, role, login, token)
        env = {
            "GIT_ASKPASS": str(askpass),
            "CODIE_GH_LOGIN": login,
            "CODIE_GH_TOKEN": token,
        }
        r = self.git("push", "origin", f"HEAD:{branch}", env=env)
        if r.returncode != 0:
            # §7.3: the Coder contract forbids history rewriting — fail-and-requeue.
            raise ConfigError(
                f"push failed for {branch}: {r.stderr.strip() or r.stdout.strip()} — "
                "merge integration first and retry; history rewriting is forbidden (denylist)"
            )
        self._ok(r, "push")

    def merge_integration(self) -> None:
        """Merge the integration branch into the current feature branch (§7.3)."""
        branch = self.integration_branch()
        self.git("fetch", "origin")
        self.git("merge", f"origin/{branch}", "-m", "chore: merge integration branch")
        self.git("diff", "--cached", "--quiet")
        self.git("commit", "--no-verify", "-m", "chore: merge integration branch", "--allow-empty")

    # -- worktrees ---------------------------------------------------------------

    def worktree_dir(self, role: str, run_id: str) -> Path:
        return self.path / "wt" / f"{role}-{run_id}"

    def worktree_lock(self, role: str, run_id: str) -> WorkspaceLock:
        """Acquire the per-worktree lock (blocking) for the duration of a run (§8.1)."""
        lock = WorkspaceLock(self.path / "wt" / f"{role}-{run_id}.lock")
        if not lock.acquire(blocking=True):
            raise ConfigError(f"could not lock worktree for {role}/{run_id}")
        self._worktree_lock_held = lock
        return lock

    def worktree_add(self, role: str, run_id: str, branch: str | None = None) -> Path:
        target = self.worktree_dir(role, run_id)
        base = self.integration_branch()
        args = ["worktree", "add"]
        if branch:
            args += ["-b", branch]
        args += [str(target), f"origin/{base}"]
        self.git(*args)
        return target

    def worktree_remove(self, role: str, run_id: str) -> None:
        target = self.worktree_dir(role, run_id)
        if not target.exists():
            return
        self.git("worktree", "remove", str(target), "--force")
        if hasattr(self, "_worktree_lock_held"):
            self._worktree_lock_held.release()
            del self._worktree_lock_held

    # -- files ----------------------------------------------------------------------

    def read_file(self, rel_path: str, cwd: Path | None = None) -> str | None:
        base = (cwd or self.path).resolve()
        full = (base / rel_path).resolve()
        if not str(full).startswith(str(base) + os.sep) and full != base:
            raise ConfigError(f"path {rel_path} escapes the workspace")
        if full.exists() and full.is_file():
            return full.read_text()
        return None

    def write_file(self, rel_path: str, content: str, cwd: Path | None = None) -> None:
        base = (cwd or self.path).resolve()
        full = (base / rel_path).resolve()
        if not str(full).startswith(str(base) + os.sep) and full != base:
            raise ConfigError(f"path {rel_path} escapes the workspace")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)


def _write_askpass(workspace: Path, role: str, login: str, token: str) -> Path:
    helper_dir = workspace / ".codie"
    helper_dir.mkdir(parents=True, exist_ok=True)
    script = helper_dir / f"git-askpass-{role}.sh"
    script.write_text(
        '#!/bin/sh\ncase "$1" in\n  Username*) echo "$CODIE_GH_LOGIN" ;;\n  Password*) echo "$CODIE_GH_TOKEN" ;;\nesac\n'  # noqa: E501
    )
    script.chmod(0o700)
    return script


def _credential_helper(workspace: Path, settings: Settings, role: str) -> Path:
    """A credential-helper script that emits the acting role's login/token (§8.1)."""
    from codie.config import resolve_role_token

    token = resolve_role_token(settings, role)
    login = settings.github.login(role)
    helper_dir = workspace / ".codie"
    helper_dir.mkdir(parents=True, exist_ok=True)
    script = helper_dir / f"credential-helper-{role}.sh"
    script.write_text(f'#!/bin/sh\necho "username={login}"\necho "password={token}"\n')
    script.chmod(0o700)
    return script

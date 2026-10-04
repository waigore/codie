"""Workspace-jailed file and shell tools (Technical Spec §8.2)."""

from __future__ import annotations

from codie.config import Settings
from codie.runner import CommandResult, Runner
from codie.workspace import Workspace


class FileTools:
    def __init__(self, settings: Settings, workspace: Workspace):
        self.settings = settings
        self.workspace = workspace

    def read(self, rel_path: str, ref: str | None = None) -> str | None:
        return self.workspace.read_file(rel_path)

    def write(self, rel_path: str, content: str) -> None:
        self.workspace.write_file(rel_path, content)

    def search(self, pattern: str, path: str = ".") -> list[str]:
        import fnmatch
        import re

        out: list[str] = []
        root = self.workspace.path
        for p in sorted(root.rglob("*")):
            if p.is_dir():
                continue
            rel = str(p.relative_to(root))
            if rel.startswith(".git"):
                continue
            if not fnmatch.fnmatch(rel, path):
                continue
            try:
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            if re.search(pattern, text):
                out.append(rel)
        return out


class ShellTools:
    def __init__(self, settings: Settings, workspace: Workspace, read_only: bool = False):
        self.runner = Runner(settings, workspace.path)
        self.read_only = read_only

    def run(self, argv: list[str], cwd: str | None = None, timeout: int | None = None) -> CommandResult:
        if self.read_only:
            self.runner.check_readonly(argv)
        return self.runner.run(argv, cwd=workspace_cwd(self.runner.workspace_root, cwd), timeout=timeout)


def workspace_cwd(root, cwd: str | None):
    from pathlib import Path

    return Path(cwd) if cwd else root

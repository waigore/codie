"""Role prompts (Technical Spec §3 roles/prompts/, loadable and injectable)."""

from __future__ import annotations

import importlib.resources as resources
from pathlib import Path

_PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompt(role: str, dir_: Path | None = None) -> str:
    """Load `<role>.md`; returns an explicit 'missing prompt' marker if absent."""
    base = dir_ or _PROMPT_DIR
    path = base / f"{role}.md"
    if path.exists():
        return path.read_text()
    try:
        ref = resources.files("codie.roles.prompts").joinpath(f"{role}.md")
        if ref.is_file():
            return ref.read_text()
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    return f"(no prompt file for role {role!r}; see roles/prompts/)"


def shared_note() -> str:
    return load_prompt("shared")

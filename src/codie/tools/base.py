"""Tool base, gates, and shared helpers (Technical Spec §6.1, §5.5, §7.4).

The deterministic gates live here and are enforced by the tools — never by the
agent's restraint.
"""

from __future__ import annotations

import re
from pathlib import Path

from codie.config import Settings

POLICY_FILES = {".codie.yaml", "AGENTS.md", "CODEOWNERS"}


class ToolError(Exception):
    """A refused / failed tool call (kernel-gated)."""


class RoleDisabled(ToolError):
    """A role was disabled via the dashboard."""


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:48] if slug else "feature"
    return slug


def feature_branch(issue_number: int, title: str, pattern: str) -> str:
    return pattern.format(issue=issue_number, slug=slugify(title))


def spec_branch(issue_number: int, title: str) -> str:
    return f"spec/{issue_number}-{slugify(title)}"


def spec_dir(issue_number: int, title: str) -> str:
    return f"specs/{issue_number}-{slugify(title)}"


def is_policy_file(path: str) -> bool:
    return Path(path).name in POLICY_FILES


def is_spec_path(path: str) -> bool:
    return path.startswith("specs/")


def file_access_allowed(role: str, path: str, settings: Settings) -> bool:
    """§7.3/§7.4/§7.5 file gates."""
    acceptance_dir = settings.overrides.acceptance.dir
    if role == "coder":
        return not (is_spec_path(path) or path.startswith(acceptance_dir) or is_policy_file(path))
    if role == "tester":
        return path.startswith(acceptance_dir) or "artifacts" in path
    if role == "reviewer":
        return True  # read-only; writes refused elsewhere
    if role == "planner":
        return is_spec_path(path)
    return False


def allowed_transition(settings: Settings, role: str, entity: str, from_status: str | None, to_status: str) -> bool:
    """§5.5: is this label edge legal for this actor?"""
    from codie.state.transitions import load_transitions

    table = load_transitions()
    edges = table.get("feature_transitions") if entity == "feature" else table.get("task_transitions")
    for edge in edges or []:
        if edge.get("to") == to_status:
            if from_status is None and edge.get("from") is None:
                if role in edge.get("actors", []):
                    return True
            elif edge.get("from") in (from_status, "any") and role in edge.get("actors", []):
                return True
    return False


def status_for_type(type_: str) -> list[str]:
    if type_ == "feature":
        return [
            "proposed",
            "speccing",
            "spec-review",
            "specified",
            "planned",
            "in-progress",
            "review",
            "accepted",
            "revised",
            "cancelled",
        ]
    if type_ == "bug":
        return ["backlog", "ready", "in-progress", "in-review", "verifying", "done", "cancelled"]
    return ["backlog", "ready", "in-progress", "in-review", "done", "cancelled"]


def entry_status(type_: str) -> str:
    if type_ == "feature":
        return "proposed"
    if type_ == "bug":
        return "ready"
    return "backlog"

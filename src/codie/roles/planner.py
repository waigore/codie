"""Planner crew (Technical Spec §7.2, Product Spec §8.1).

The whole-project view: decomposes the PRD into features, authors the two specs
(proposing, never applying — the kernel validates and applies workflow state),
breaks approved features into tasks, re-plans revised features, reconciles the
plan when the PRD changes, and returns the ReleasePlan. Its GitHub write access
is limited to draft spec PRs and spec files.
"""

from __future__ import annotations

from typing import Any

PLANNER_CONTRACT = """
1. Never descope a requirement to make a failing check pass — only humans change
   requirements. State architecture and NFR expectations explicitly in specs and
   task bodies; raise architecture/refactoring tasks when the codebase drifts.
2. Feature plans: one issue per feature capturing title, intent, PRD references,
   and contributing evals. New features enter `status:proposed` (the kernel
   applies label + body mutations).
3. Spec authoring: write `specs/<issue#>-<slug>/feature-spec.md` and
   `technical-spec.md` (Product §9.2 headings + FR-/NFR-/AC- IDs, AC trace list
   mandatory) on branch `spec/<issue#>-<slug>`, open ONE spec PR as a draft; the
   kernel validates the files and marks it ready-for-review. Resume existing.
4. Breakdown: decompose an approved feature into dev/integration/test tasks, each
   with `Parent:`, `Depends on:`, and a "Done when" checklist tracing requirement
   IDs. Never break down a feature before its specs are approved.
5. Re-plan revised features: AssessRevision first, then Replan for implementation
   revisions (spec-update first when requirements changed).
6. ProposeRelease: return ReleasePlan{version, changelog, eval_report}; the kernel
   opens the marked release PR.
"""


def build_agent_tools(settings, workspace, client, role: str = "planner") -> list[Any]:
    """The gated tools a Planner crew holds (§7.2: spec branches/files only)."""
    from codie.tools.file_tools import FileTools, ShellTools
    from codie.tools.github_tools import GithubToolbox

    return [
        FileTools(settings, workspace) if workspace else None,
        ShellTools(settings, workspace, read_only=True) if workspace else None,
        GithubToolbox(client, settings, role, workspace=workspace),
    ]


def role_prompt() -> str:
    from codie.roles.prompts import load_prompt, shared_note

    return f"{shared_note()}\n\n{load_prompt('planner')}\n\n{PLANNER_CONTRACT}"

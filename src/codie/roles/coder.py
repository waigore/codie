"""Coder crew (Technical Spec §7.3, Product Spec §8.2).

Tool-acting implementer: branches from the integration branch, implements to the
"Done when" contract, commits in the repo's confirmed style, opens a PR with
`Refs #<n>`, resumes an existing branch, iterates on review feedback. Runs over
the gated file/shell/github toolset; its GitHub writes are limited to its
§5.5 matrix row.
"""

from __future__ import annotations

from typing import Any

CODE_CONTRACT = """
1. Resume first: if the task already has a branch or an open linked PR, fetch and
   resume it — never start a parallel branch. Otherwise branch from origin/<dev>.
2. Implement with task-level craft (SOLID, DRY, clear naming, small scoped diffs,
   testability). Unit/integration tests accompany the change; the black-box
   acceptance suite is the Tester's domain.
3. Commit in the repo's confirmed style (default: conventional commits referencing
   the issue). Push, and open a PR to the integration branch with `Refs #<n>` and a
   summary of how each "Done when" item is satisfied. No closing keyword — the
   kernel closes issues at terminal status.
4. When dispatched as AddressReview: fetch review comments, apply changes, push to
   the same branch; reply only where it adds something substantial.
5. Keep the PR current by merging the integration branch into the feature branch.
   Never rebase or force-push.
6. Blocked (ambiguous spec, failing external dep, scope conflict) → comment +
   `flag:blocked`, exit cleanly.
"""


def build_agent_tools(settings, workspace, client, role: str = "coder") -> list[Any]:
    """The gated tools a Coder crew holds (§7.3)."""
    from codie.tools.file_tools import FileTools, ShellTools
    from codie.tools.github_tools import GithubToolbox

    return [
        FileTools(settings, workspace) if workspace else None,
        ShellTools(settings, workspace) if workspace else None,
        GithubToolbox(client, settings, role, workspace=workspace),
    ]


def role_prompt() -> str:
    from codie.roles.prompts import load_prompt, shared_note

    return f"{shared_note()}\n\n{load_prompt('coder')}\n\n{CODE_CONTRACT}"

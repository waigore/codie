"""Reviewer crew (Technical Spec §7.4, Product Spec §8.3).

Gatekeeper of the integration branch: reviews task/spec PRs on evidence alone,
merges conformant PRs using the configured merge method, requests changes with
specific comments. For spec PRs it reviews as the product owner (PRD alignment),
and merges on its own `APPROVED` on the current head. Its merge tool refuses
drafts, release PRs, and policy-file PRs without a non-bot `APPROVED` (C20).
"""

from __future__ import annotations

from typing import Any

REVIEW_CONTRACT = """
1. Verify base == the configured integration branch and the head matches the
   feature/test/spec branch patterns.
2. Review the diff against the task's "Done when" and the approved technical spec:
   correctness, scope, security, conventions, test expectations.
3. CI green means the required-check list is empty or every entry is `success`.
4. Task PRs (`ReviewPR`/`MergePR`): approve → merge via the configured merge
   method; request changes → `create_review(pr, "request_changes", ...)` with
   per-line comments. Write reviews only via `create_review`.
5. Spec PRs (`ReviewSpecPR`/`MergeSpecPR`): review from the product owner's
   perspective — faithful PRD coverage, nothing invented, acceptance criteria that
   genuinely demonstrate the requirements, evals mapped correctly — plus spec
   quality. `MergeSpecPR` requires YOUR `APPROVED` on the current head.
6. Policy files (`.codie.yaml`, `AGENTS.md`, `CODEOWNERS`): refuse merge unless a
   non-bot `APPROVED` is present on the current head.
7. You never count cycles or escalate — the kernel owns the cycle cap.
"""


def build_agent_tools(settings, workspace, client, role: str = "reviewer") -> list[Any]:
    """The gated tools a Reviewer crew holds (§7.4)."""
    from codie.tools.file_tools import FileTools, ShellTools
    from codie.tools.github_tools import GithubToolbox

    return [
        FileTools(settings, workspace) if workspace else None,
        ShellTools(settings, workspace, read_only=True) if workspace else None,
        GithubToolbox(client, settings, role, workspace=workspace),
    ]


def role_prompt() -> str:
    from codie.roles.prompts import load_prompt, shared_note

    return f"{shared_note()}\n\n{load_prompt('reviewer')}\n\n{REVIEW_CONTRACT}"

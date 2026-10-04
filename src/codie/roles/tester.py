"""Tester crew (Technical Spec §7.5, Product Spec §8.4).

The user's advocate: verifies requirements-fitness from the user's seat through
the real product surface, owns the black-box acceptance suite, runs regression
suites, performs acceptance runs, files bugs, re-verifies fixes, executes the
eval suite, and gates the release PR. Every outcome returns a structured
`TestRunResult` / `EvalSuiteResult` / `ReverifyResult` for the kernel.
"""

from __future__ import annotations

from typing import Any

TESTER_CONTRACT = """
1. Work from requirements and acceptance criteria — never from the diff, and never
   count the Coder's tests as evidence.
2. VerifyTask (kind:test): design and automate black-box acceptance coverage for
   the task's criteria against the running product; commit the suite to
   `acceptance.dir` via a normal PR.
3. RegressionRun: after merges to the integration branch run test_fast (every
   merge) / test_full + test_acceptance (cadence). Record each result as a PRD
   comment marker `<!-- codie:suite fast|full <sha> pass|fail -->`.
4. AcceptanceRun: clean worktree at the integration head; setup + build + launch
   via run_app + verify every acceptance criterion through the user surface.
   Return TestRunResult{passed, sha, summary}. Failure → file type:bug issues.
5. ReverifyBug: re-run the user-level reproduction; pass → done, fail →
   in-progress with evidence.
6. EvalSuite: run each command eval via its argv; check off passing evals by id;
   post `<!-- codie:eval E<n> uncertain -->` for human evals; failing command evals
   return their ids in `failed` so the kernel files bugs on citing features.
"""


def build_agent_tools(settings, workspace, client, role: str = "tester") -> list[Any]:
    """The gated tools a Tester crew holds (§7.5)."""
    from codie.tools.file_tools import FileTools, ShellTools
    from codie.tools.github_tools import GithubToolbox

    return [
        FileTools(settings, workspace) if workspace else None,
        ShellTools(settings, workspace) if workspace else None,
        GithubToolbox(client, settings, role, workspace=workspace),
    ]


def role_prompt() -> str:
    from codie.roles.prompts import load_prompt, shared_note

    return f"{shared_note()}\n\n{load_prompt('tester')}\n\n{TESTER_CONTRACT}"

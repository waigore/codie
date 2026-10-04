"""Fetch pattern: assemble a RepoSnapshot from a GitHubClient (Tech Spec §5.1)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from codie.config import Settings
from codie.models import BranchHeads, GitHubPR
from codie.state.derive import RepoSnapshot

if TYPE_CHECKING:
    from codie.github.client import GitHubClient

FULL_REFRESH_CYCLES = 10


def fetch_snapshot(
    client: GitHubClient,
    repo: str,
    settings: Settings,
    since: str | None = None,
    full: bool = False,
) -> RepoSnapshot:
    """Assemble the derivation inputs. `since`/`full` gate delta vs full fetch."""
    issues = client.list_issues(since if (not full and since) else None)
    comments = client.list_comments(since if (not full and since) else None)
    prs = client.list_prs()

    # Re-fetch linked + marker-carrying PRs by number to keep them fresh (§5.1c).
    pr_by_number = {pr.number: pr for pr in prs}
    refetch: list[int] = []
    for pr in prs:
        if _is_release_pr(pr) or _is_spec_pr(pr) or _linked_number(pr) is not None:
            refetch.append(pr.number)
    for number in refetch:
        fresh_pr = client.get_pr(number)
        if fresh_pr is not None:
            pr_by_number[number] = fresh_pr
    prs = sorted(pr_by_number.values(), key=lambda p: p.number)

    # CI semantics (M19/N6): green is defined over the *required* check list.
    # For each open PR populate `checks` from list_required_checks(head) so a
    # non-required failing check never blocks merge.
    for pr in prs:
        checks = _resolve_required_checks(client, pr)
        if checks is not None:
            pr.checks = checks

    branches = BranchHeads(main=client.get_ref(settings.branches.main), dev=client.get_ref(settings.branches.dev))

    codie_yaml = None
    if branches.dev:
        codie_yaml = client.get_file(".codie.yaml", branches.dev)
    codie_yaml_present = codie_yaml is not None

    # Spec files for merged spec PRs, read at the integration head (§7.1).
    spec_files: dict[str, str] = {}
    if branches.dev:
        for pr in prs:
            if not _is_merged_spec_pr(pr):
                continue
            feature_number = _spec_feature_from_paths(pr.files)
            if feature_number is None:
                continue
            for path in pr.files:
                if not path.endswith("-spec.md"):
                    continue
                filename = path.rsplit("/", 1)[-1]
                content = client.get_file(path, branches.dev)
                if content is not None:
                    spec_files[f"feature.{feature_number}.{filename}"] = content

    timeline: dict[int, list] = {}
    for issue in issues:
        timeline[issue.number] = client.list_timeline(issue.number)

    return RepoSnapshot(
        repo=repo,
        issues=issues,
        comments=comments,
        prs=prs,
        timeline=timeline,
        branches=branches,
        codie_yaml_present=codie_yaml_present,
        spec_files=spec_files,
        bot_logins=settings.github.bot_logins(),
    )


def _is_merged_spec_pr(pr: GitHubPR) -> bool:
    return _is_spec_pr(pr) and pr.state == "merged" and pr.merged_at is not None


def _is_spec_pr(pr: GitHubPR) -> bool:
    return any(path.startswith("specs/") for path in pr.files)


def _is_release_pr(pr: GitHubPR) -> bool:
    return "codie:release" in (pr.body or "")


def _linked_number(pr: GitHubPR) -> int | None:
    from codie.state import parse

    numbers, _ = parse.linked_issue_numbers(pr.body)
    return numbers[0] if numbers else None


def _spec_feature_from_paths(paths: list[str]) -> int | None:
    for path in paths:
        m = re.match(r"^specs/(\d+)-", path)
        if m:
            return int(m.group(1))
    return None


def _resolve_required_checks(client, pr: GitHubPR) -> list[dict] | None:
    """Required-check status for an open PR head; None when unavailable."""
    if pr.state != "open":
        return None
    try:
        required = client.list_required_checks(pr.head)
    except Exception:
        return None
    return [{"name": r.name, "state": r.state} for r in required]

"""GitHub integration: client protocol, adapters, label sync, snapshot fetch."""

from codie.github.client import AdminGitHubClient, GitHubClient
from codie.github.fetch import fetch_snapshot
from codie.github.labels import ensure_label_set
from codie.github.pygithub_client import PyGithubClient, build_client

__all__ = [
    "AdminGitHubClient",
    "GitHubClient",
    "PyGithubClient",
    "build_client",
    "ensure_label_set",
    "fetch_snapshot",
]

"""Label-set sync (Technical Spec §9.4 / §9.6 defaults table, M31)."""

from __future__ import annotations

from codie.config import LABEL_TAXONOMY
from codie.github.client import GitHubClient

LABEL_NAMES = [name for name, _, _ in LABEL_TAXONOMY]


def ensure_label_set(client: GitHubClient) -> int:
    """Create/update the full taxonomy idempotently. Returns the count managed."""
    managed = 0
    for name, color, description in LABEL_TAXONOMY:
        client.upsert_label(name, color, description)
        managed += 1
    return managed

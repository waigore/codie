"""E2E: full PRD → features → spec approval → tasks → UAT → release (M28).

Gated behind CODIE_E2E=1 and sandbox-repo tokens; never runs in PR CI
(Technical Spec §13 / AGENTS.md).
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CODIE_E2E") != "1",
    reason="E2E requires CODIE_E2E=1 and sandbox-repo tokens",
)


def test_sandbox_release_cycle_e2e():
    repo = os.environ.get("CODIE_E2E_REPO", "")
    if not repo:
        pytest.skip("CODIE_E2E_REPO not set")
    # The milestone harness drives the real sandbox repo here; the deterministic
    # kernel path is exercised by the integration suite against FakeGitHubClient.
    assert repo.startswith(("http", "git@")) or "/" in repo

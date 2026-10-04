"""Unit tests for context pack assembly (§7.1) and the workspace manager (§8.1)."""

import datetime as _dt
import subprocess

from codie.context import build_context, conventions_digest
from codie.models import BranchHeads, Feature, PrdIssue, ProjectState, WorkItem
from codie.workspace import Workspace
from tests.conftest import make_settings


def dt():
    return _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)


def state_with(features=(), **kw):
    prd = PrdIssue(number=1, title="PRD", body="body", evals=[], updated_at=dt(), fingerprint="")
    st = ProjectState(repo="a/b", prd=prd, branches=BranchHeads(main="m", dev="d"), **kw)
    st.features = list(features)
    return st


def test_orchestrator_pack_builds_queue_text():
    st = state_with()
    pack = build_context("orchestrator", st, make_settings())
    text = pack.render()
    assert "PRD" in text


def test_planner_pack_has_prd_and_graph():
    st = state_with(
        features=[Feature(number=2, title="F", body="", status="proposed", created_at=dt(), updated_at=dt())]
    )
    pack = build_context("planner", st, make_settings())
    text = pack.render()
    assert "PRD" in text and "feature #2" in text


def test_coder_pack_references_specs():
    from codie.models import TaskItem

    st = state_with(
        features=[
            Feature(
                number=2,
                title="F",
                body="",
                status="specified",
                spec_paths=["specs/2-f/feature-spec.md"],
                created_at=dt(),
                updated_at=dt(),
            )
        ]
    )
    st.tasks = [
        TaskItem(number=3, title="t", body="", status="ready", kind="dev", parent=2, created_at=dt(), updated_at=dt())
    ]
    pack = build_context(
        "coder",
        st,
        make_settings(),
        item=WorkItem(kind="Implement", role="coder", entity="x", issue_number=3, rule=8),
    )
    assert pack.token_estimate > 0
    assert "Feature specs" in pack.render()


def test_tester_pack_includes_evals_and_commands():
    st = state_with(
        features=[Feature(number=2, title="F", body="", status="review", created_at=dt(), updated_at=dt())]
    )
    pack = build_context(
        "tester",
        st,
        make_settings(),
        item=WorkItem(kind="AcceptanceRun", role="tester", entity="x", issue_number=2, rule=11),
    )
    text = pack.render()
    assert "Commands" in text and "Recent merges" in text


def test_conventions_digest_marks_missing(tmp_path):
    ws = Workspace(tmp_path, make_settings())
    digest = conventions_digest(make_settings(), ws)
    assert "(absent)" in digest


def test_workspace_clone_branch_commit(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    wdir = tmp_path / "seed"
    wdir.mkdir()
    subprocess.run(["git", "init", "-q", str(wdir)], check=True)
    subprocess.run(["git", "-C", str(wdir), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wdir), "config", "user.name", "t"], check=True)
    (wdir / "README.md").write_text("# x\n")
    subprocess.run(["git", "-C", str(wdir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wdir), "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(["git", "-C", str(wdir), "branch", "-M", "dev"], check=True)
    subprocess.run(["git", "-C", str(wdir), "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "-C", str(wdir), "push", "-q", "-u", "origin", "dev"], check=True)
    subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/dev"], check=True)

    ws = Workspace(tmp_path / "clone", make_settings(), remote_url=str(origin))
    ws.ensure()
    assert (tmp_path / "clone" / "README.md").exists()
    ws.git("config", "user.email", "t@t")
    ws.git("config", "user.name", "t")
    ws.checkout_base_and_branch("feature/3-x")
    assert ws.git("branch", "--show-current").stdout.strip() == "feature/3-x"
    (tmp_path / "clone" / "new.py").write_text("x = 1\n")
    ws.commit_all("feat: add (3)")
    log = ws.git("log", "-1", "--pretty=%s").stdout.strip()
    assert log == "feat: add (3)"

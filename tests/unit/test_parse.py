"""Unit tests for state/parse.py — markers, labels, evals (Tech Spec §5.2)."""

from datetime import UTC, datetime

from codie.models import GitHubIssue
from codie.state import parse


def now():
    return datetime(2026, 1, 1, tzinfo=UTC)


def issue(body: str, labels=None, title="t") -> GitHubIssue:
    return GitHubIssue(
        number=1,
        title=title,
        body=body,
        state="open",
        labels=labels or [],
        created_at=now(),
        updated_at=now(),
    )


def test_markers_plain_and_bold():
    body = "Parent: #12\n**Depends on:** #3, #4\nEvals: E1, E2\n**PRD sections:** login"
    m = parse.parse_markers(body)
    assert m["parent"] == "#12"
    assert m["depends_on"] == "#3, #4"
    assert m["evals"] == "E1, E2"
    assert m["prd_sections"] == "login"


def test_markers_case_insensitive():
    m = parse.parse_markers("pArEnT: #5\nDEPENDS ON: #6")
    assert m["parent"] == "#5"
    assert m["depends_on"] == "#6"


def test_parent_depends_evals_extractors():
    assert parse.parent_of("**Parent:** #7") == 7
    assert parse.parent_of("no parent here") is None
    assert parse.depends_on_of("**Depends on:** #1, #2") == [1, 2]
    assert parse.evals_of("**Evals:** E1, E3") == ["E1", "E3"]


def test_done_when_citations():
    body = "## Context\nstuff\n\n## Done when\n- [ ] the thing works (FR-1)\n- [x] covered already (NFR-2)\n"
    items = parse.parse_done_when(body)
    assert len(items) == 2
    assert items[0].checked is False
    assert items[0].text == "the thing works"
    assert items[0].requirement_ids == ["FR-1"]
    assert items[1].checked is True
    assert items[1].requirement_ids == ["NFR-2"]


def test_prd_evals_checkbox():
    body = "## Definition of done\n- [ ] E1: onboarding works\n- [x] E2: reporting renders\n"
    evals = parse.parse_prd_evals(body)
    assert [e.id for e in evals] == ["E1", "E2"]
    assert evals[0].checked is False
    assert evals[1].checked is True


def test_prd_fingerprint_normalizes_checkboxes():
    a = parse.prd_fingerprint("## done\n- [ ] E1: xyz\n- [x] E2: abc\n")
    b = parse.prd_fingerprint("## done\n- [x] E1: xyz\n- [ ] E2: abc\n")
    assert a == b  # checkbox state never changes the fingerprint


def test_requirement_grammar():
    text = "- FR-1: users can log in\n- NFR-1: < 200ms (P99)\n- AC-1: login succeeds (FR-1, NFR-1)\n~~- AC-2: old (FR-1)~~\n- not a requirement line\n"  # noqa: E501
    assert parse.requirement_id("- FR-1: x") == "FR-1"
    assert parse.requirement_id("- AC-1: y (FR-1)") == "AC-1"
    assert parse.superseded_ids(text) == {"AC-2"}
    assert parse.is_valid_ac_line("- AC-1: login succeeds (FR-1, NFR-1)") is True
    assert parse.is_valid_ac_line("- AC-1: login succeeds") is False  # missing mandatory trace
    assert parse.requirement_id("~~- AC-9: dropped (FR-1)~~") == "AC-9"


def test_closing_and_refs_numbers():
    assert parse.closing_numbers("Fixes #2") == [2]
    assert parse.refs_numbers("Refs #4") == [4]
    used, via_closing = parse.linked_issue_numbers("Closes #2")
    assert used == [2] and via_closing is True
    used, via_closing = parse.linked_issue_numbers("Refs #4")
    assert used == [4] and via_closing is False


def test_specs_section():
    body = "## Description\nx\n\n## Specs\n- Spec PR: #45 -> specs/2-login/feature-spec.md\n"
    pr, paths = parse.parse_specs_section(body)
    assert pr == 45
    assert "specs/2-login/feature-spec.md" in paths


def test_classify_issue_labels():
    g = issue("x", labels=["type:bug", "status:ready", "priority:high", "flag:blocked"])
    p = parse.classify_issue(g)
    assert p["type_"] == "bug"
    assert p["task_status"] == "ready"
    assert p["priority"] == "high"
    assert p["flags"] == {"blocked"}


def test_unknown_codie_labels():
    assert "type:nonsense" in parse.unknown_codie_labels(["type:nonsense"])
    assert "needs:help" not in parse.unknown_codie_labels(["needs:help"])
    assert parse.unknown_codie_labels(["type:prd"]) == []


def test_prd_marker_extraction_and_heartbeat():
    assert parse.prd_marker_fingerprint("<!-- codie:prd " + "a" * 64 + " -->") == "a" * 64
    assert parse.heartbeat_run_id("<!-- codie:heartbeat r1 2026-01-01T00:00:00+00:00 -->") == "r1"
    assert parse.blocked_run("<!-- codie:blocked r1 planner -->") == ("r1", "planner")


def test_eval_citation_helpers():
    assert parse.citation_ids_of_done("the thing (FR-1, NFR-2)") == ["FR-1", "NFR-2"]

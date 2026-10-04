"""Issue body marker and label parsing (Technical Spec §5.2, Product Spec §10).

Everything here is a pure function of strings — no LLM, no I/O.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from codie.config import LABEL_UNIVERSE
from codie.models import DoneWhen, Eval, GitHubIssue

# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

_PREFIXES = ("type:", "status:", "kind:", "priority:", "flag:")


def split_labels(labels: list[str]) -> dict[str, list[str]]:
    """Split labels into their categories, preserving order."""
    result: dict[str, list[str]] = {
        "type": [],
        "status": [],
        "kind": [],
        "priority": [],
        "flag": [],
        "other": [],
    }
    for label in labels:
        for prefix in _PREFIXES:
            if label.startswith(prefix):
                result[prefix[:-1]].append(label[len(prefix) :])
                break
        else:
            result["other"].append(label)
    return result


def unknown_codie_labels(labels: list[str]) -> list[str]:
    """Labels that use a codie namespace but are not part of the taxonomy (§5.2)."""
    return [
        label
        for label in labels
        if label.split(":")[0] in {"type", "status", "kind", "priority", "flag"} and label not in LABEL_UNIVERSE
    ]


def type_of(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith("type:"):
            return label[len("type:") :]
    return None


def status_of(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith("status:"):
            return label[len("status:") :]
    return None


def kind_of(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith("kind:"):
            return label[len("kind:") :]
    return None


def priorities_of(labels: list[str]) -> list[str]:
    return [label[len("priority:") :] for label in labels if label.startswith("priority:")]


def flags_of(labels: list[str]) -> list[str]:
    return [label[len("flag:") :] for label in labels if label.startswith("flag:")]


def priority_of(labels: list[str]) -> str:
    prios = priorities_of(labels)
    if len(prios) == 1:
        return prios[0]
    return "medium"


# ---------------------------------------------------------------------------
# Body markers (§5.2: `**Key:** value` or `Key: value`, case-insensitive,
# line-anchored; Markdown bold around the key is optional)
# ---------------------------------------------------------------------------

_MARKER_BOLD_RE = re.compile(r"^\s*\*{2}([A-Za-z][A-Za-z ]*?):\*{2}\s*(.*)$")
_MARKER_PLAIN_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?):\s*(.*)$")
_KEY_LOOKUP = {
    "parent": "parent",
    "depends on": "depends_on",
    "evals": "evals",
    "prd sections": "prd_sections",
    "reproduction": "reproduction",
    "expected vs actual": "expected_vs_actual",
}


def parse_markers(body: str) -> dict[str, str]:
    """Extract the body markers in order. Values are trailing free text.

    Grammar (§5.2/§10): `**Key:** value` OR `Key: value` (bold optional).
    """
    found: dict[str, str] = {}
    for line in body.splitlines():
        m = _MARKER_BOLD_RE.match(line) or _MARKER_PLAIN_RE.match(line)
        if not m:
            continue
        key = _KEY_LOOKUP.get(m.group(1).strip().lower())
        if key:
            found[key] = m.group(2).strip()
    return found


_NUMBER_RE = re.compile(r"#(\d+)")


def _numbers(text: str) -> list[int]:
    return [int(n) for n in _NUMBER_RE.findall(text or "")]


def parent_of(body: str) -> int | None:
    markers = parse_markers(body)
    if "parent" not in markers:
        return None
    nums = _numbers(markers["parent"])
    return nums[0] if nums else None


def depends_on_of(body: str) -> list[int]:
    markers = parse_markers(body)
    if "depends_on" not in markers:
        return []
    return list(dict.fromkeys(_numbers(markers["depends_on"])))


def evals_of(body: str) -> list[str]:
    markers = parse_markers(body)
    if "evals" not in markers:
        return []
    return [eid.strip() for eid in re.findall(r"\b(E\d+)\b", markers["evals"])]


def prd_sections_of(body: str) -> str:
    markers = parse_markers(body)
    return markers.get("prd_sections", "")


def reproduction_of(body: str) -> str:
    markers = parse_markers(body)
    return markers.get("reproduction", "")


def expected_vs_actual_of(body: str) -> str:
    markers = parse_markers(body)
    return markers.get("expected_vs_actual", "")


# ---------------------------------------------------------------------------
# Checklists
# ---------------------------------------------------------------------------

_CHECKBOX_RE = re.compile(r"^- \[(?P<state> |x)\] (?P<text>.+)$", re.IGNORECASE)
_TRAILING_IDS_RE = re.compile(r"\s*\(([A-Z0-9,\-\s]+)\)\s*$")


def _citation_ids(text: str) -> list[str]:
    m = _TRAILING_IDS_RE.search(text)
    if not m:
        return []
    ids = re.findall(r"\b(?:FR|NFR|AC)-\d+\b", m.group(1))
    return list(dict.fromkeys(ids))


def parse_done_when(body: str) -> list[DoneWhen]:
    """Parse the `## Done when` checklist into items with requirement citations."""
    section = _section_after(body, "## Done when")
    items: list[DoneWhen] = []
    for line in section.splitlines():
        m = _CHECKBOX_RE.match(line)
        if not m:
            continue
        raw = m.group("text").strip()
        ids = _citation_ids(raw)
        text = _TRAILING_IDS_RE.sub("", raw).strip()
        items.append(DoneWhen(checked=m.group("state").lower() == "x", text=text, requirement_ids=ids))
    return items


def parse_tasks_checklist(body: str) -> list[tuple[bool, int]]:
    """Parse `## Tasks` checklist -> [(checked, issue#)]. Informational only (§5.3)."""
    section = _section_after(body, "## Tasks")
    out: list[tuple[bool, int]] = []
    for line in section.splitlines():
        m = _CHECKBOX_RE.match(line)
        if not m:
            continue
        nums = _numbers(m.group("text"))
        if nums:
            out.append((m.group("state").lower() == "x", nums[0]))
    return out


def parse_specs_section(body: str) -> tuple[int | None, list[str]]:
    """Parse `## Specs` mapping: features link their spec PR and file paths (Product §7.1/§10)."""
    section = _section_after(body, "## Specs")
    pr_num: int | None = None
    paths: list[str] = []
    for line in section.splitlines():
        nums = _numbers(line)
        if nums and pr_num is None:
            pr_num = nums[0]
        for path in re.findall(r"specs/[^ ]+(?:feature-spec|technical-spec)\.md", line):
            paths.append(path)
        for path in re.findall(r"`([^`]*)`", line):
            if path.startswith("specs/") or path.endswith("-spec.md"):
                paths.append(path)
    return pr_num, list(dict.fromkeys(paths))


def _section_after(body: str, heading: str) -> str:
    lines = body.splitlines()
    out: list[str] = []
    seen = False
    for line in lines:
        if not seen and line.strip().lower() == heading.lower():
            seen = True
            continue
        if seen:
            if line.strip().startswith("#"):
                break
            if line.strip().startswith("## ") and line.strip().lower() != heading.lower():
                break
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# PRD eval checklist (§5.2 / Product §10)
# ---------------------------------------------------------------------------

_EVAL_RE = re.compile(r"^- \[(?P<state> |x)\] (?P<id>E\d+): (?P<text>.+)$", re.IGNORECASE)
_REQUIREMENT_LINE_RE = re.compile(r"^[-*]\s+(~~)?(FR|NFR|AC)-([1-9][0-9]*):\s+.+?(~~)?$")
_AC_TRACE_RE = re.compile(r"\(((?:FR|NFR)-\d+(?:, (?:FR|NFR)-\d+)*)\)$")


def parse_prd_evals(body: str, eval_commands: Mapping[str, Any] | None = None) -> list[Eval]:
    """Parse the PRD eval checklist. `eval_commands` maps E<n> -> {run|mode}."""
    eval_commands = eval_commands or {}
    evals: list[Eval] = []
    for line in body.splitlines():
        m = _EVAL_RE.match(line)
        if not m:
            continue
        eid = m.group("id").upper()
        entry = eval_commands.get(eid, {})
        run_cmd = getattr(entry, "run", None) if entry else None
        mode = getattr(entry, "mode", None) if entry else None
        evals.append(
            Eval(
                id=eid,
                text=m.group("text").strip(),
                checked=m.group("state").lower() == "x",
                command=list(run_cmd) if run_cmd else None,
                mode="human" if mode == "human" else ("command" if run_cmd else None),
            )
        )
    return evals


def prd_fingerprint(body: str) -> str:
    """Sha256 of the PRD body after normalizing eval checkbox state (§5.2)."""
    normalized: list[str] = []
    for line in body.splitlines():
        m = _EVAL_RE.match(line)
        if m:
            normalized.append(f"- [~] {m.group('id')}: {m.group('text').strip()}")
        else:
            normalized.append(line)
    return hashlib.sha256("\n".join(normalized).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Requirement lines (Product Spec §9.3, M20)
# ---------------------------------------------------------------------------


def parse_requirement_lines(text: str) -> list[str]:
    """Return requirement/AC lines matching the single canonical grammar (§9.3)."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^~~.*~~$", stripped):
            # Whole-line strikethrough: superseded line, still recognized.
            inner = stripped[2:-2].strip()
            if _REQUIREMENT_LINE_RE.match(inner):
                lines.append(stripped)
            continue
        if _REQUIREMENT_LINE_RE.match(stripped):
            lines.append(stripped)
    return lines


def requirement_id(line: str) -> str | None:
    """Extract the ID of a single requirement line, or None when not one."""
    stripped = line.strip()
    if stripped.startswith("~~") and stripped.endswith("~~"):
        stripped = stripped[2:-2].strip()
    m = _REQUIREMENT_LINE_RE.match(stripped)
    if not m:
        return None
    return f"{m.group(2)}-{m.group(3)}"


def requirement_ids(text: str) -> list[str]:
    return [rid for line in parse_requirement_lines(text) if (rid := requirement_id(line))]


def superseded_ids(text: str) -> set[str]:
    out: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("~~") and stripped.endswith("~~"):
            rid = requirement_id(stripped)
            if rid:
                out.add(rid)
    return out


def live_ids(text: str) -> set[str]:
    return {rid for rid in requirement_ids(text)} - superseded_ids(text)


def is_valid_ac_line(line: str) -> bool:
    stripped = line.strip()
    if not _REQUIREMENT_LINE_RE.match(stripped) or not stripped.startswith("- AC-"):
        return False
    return bool(_AC_TRACE_RE.search(stripped))


def citation_ids_of_done(pred: str) -> list[str]:
    """Requirement IDs cited by a '## Done when' item line."""
    return _citation_ids(pred)


# ---------------------------------------------------------------------------
# Issue classification (§5.2)
# ---------------------------------------------------------------------------


def classify_issue(issue: GitHubIssue, label_mapping: dict[str, str] | None = None) -> dict:
    """Return a ParsedIssue-ish dict. label_mapping maps repo labels -> codie labels."""
    label_mapping = label_mapping or {}
    mapped = list(dict.fromkeys(label_mapping.get(label, label) for label in issue.labels))
    t = type_of(mapped)
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "labels": mapped,
        "type_": t,
        "feature_status": status_of(mapped) if t == "feature" else None,
        "task_status": status_of(mapped) if t in {"task", "bug"} else None,
        "kind": kind_of(mapped) if t == "task" else None,
        "priority": priority_of(mapped),
        "flags": set(flags_of(mapped)),
        "parent": parent_of(issue.body),
        "depends_on": depends_on_of(issue.body),
        "evals": evals_of(issue.body),
        "prd_sections": prd_sections_of(issue.body),
        "done_when": parse_done_when(issue.body),
        "spec_pr_number": parse_specs_section(issue.body)[0],
        "spec_paths": parse_specs_section(issue.body)[1],
        "reproduction": reproduction_of(issue.body),
        "expected_vs_actual": expected_vs_actual_of(issue.body),
        "referenced_prs": [],
        "unreferenced": False,
    }


# ---------------------------------------------------------------------------
# PR linkage (§5.2)
# ---------------------------------------------------------------------------

_CLOSING_RE = re.compile(r"\b(?:Closes|Fixes|Resolves)\s+#(\d+)", re.IGNORECASE)
_REFS_RE = re.compile(r"\bRefs?\s+#(\d+)", re.IGNORECASE)


def closing_numbers(body: str) -> list[int]:
    return [int(n) for n in _CLOSING_RE.findall(body or "")]


def refs_numbers(body: str) -> list[int]:
    return [int(n) for n in _REFS_RE.findall(body or "")]


def linked_issue_numbers(body: str, cross_references: list[int] | None = None) -> tuple[list[int], bool]:
    """Primary: Closes/Fixes/Resolves; fallback: timeline cross-references.

    Returns (issues, used_closing). A closing keyword violates the M30 norm.
    """
    closed = closing_numbers(body)
    if closed:
        return closed, True
    refs = refs_numbers(body)
    if refs:
        return refs, False
    return (cross_references or []), False


def prd_marker_fingerprint(body: str) -> str | None:
    """Extract the `<!-- codie:prd <sha256> -->` marker from a comment body."""
    m = re.search(r"<!--\s*codie:prd\s+([0-9a-f]{64})\s*-->", body)
    return m.group(1) if m else None


def heartbeat_run_id(body: str) -> str | None:
    """Extract run_id from a `<!-- codie:heartbeat <run_id> ... -->` marker."""
    m = re.search(r"<!--\s*codie:heartbeat\s+(\S+)\s+([0-9TZ:.+-]+)\s*-->", body)
    return m.group(1) if m else None


def blocked_run(body: str) -> tuple[str | None, str | None]:
    """Extract (run_id, actor) from `<!-- codie:blocked <run_id> <actor> -->`."""
    m = re.search(r"<!--\s*codie:blocked\s+(\S+)\s+(\S+)\s*-->", body)
    if not m:
        return None, None
    return m.group(1), m.group(2)

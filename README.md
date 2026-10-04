# codie

Codie is an autonomous software-development crew built on [CrewAI](https://www.crewai.com/).
Given a GitHub repository and a product requirements document (PRD) with a machine-verifiable
**definition of done ("evals")**, it plans features, breaks them into dev/integration/test
tasks, implements them via PRs, reviews and tests them, and iterates until the evals pass.

State lives on **GitHub** (issues, labels, PRs, marker comments); local state is a disposable
cache. The authoritative design documents are:

- [`docs/PRODUCT_SPEC.md`](docs/PRODUCT_SPEC.md) — workflow, label taxonomy, roles, guardrails
- [`docs/TECHNICAL_SPEC.md`](docs/TECHNICAL_SPEC.md) — architecture, state derivation, config,
  tools, milestones (M0–M5)

## Quick start

```bash
pip install -e ".[dev]"

# Configure ~/.codie/codie.yaml (see docs/TECHNICAL_SPEC.md §4 and the
# annotated example in src/codie/config.py:494).
codie validate-config                     # verifies config + env wiring (no network writes)

codie init <owner>/<repo>                 # survey-first onboarding (admin token)
codie start <owner>/<repo>                # run the crew (role tokens + CODIE_LLM_API_KEY)
codie status <owner>/<repo>               # derived state + next work items
codie doctor <owner>/<repo>               # provisioning/permission checklist
codie labels sync <owner>/<repo>          # create/update the label taxonomy
codie stop <owner>/<repo>                 # graceful stop
```

Credentials come from the environment only — never write them to files or commit them:
`CODIE_LLM_API_KEY`, `CODIE_GH_TOKEN_PLANNER`, `CODIE_GH_TOKEN_CODER`,
`CODIE_GH_TOKEN_REVIEWER`, `CODIE_GH_TOKEN_TESTER`, `CODIE_GH_TOKEN_KERNEL`,
`CODIE_GH_TOKEN_ADMIN` (admin is used only by `codie init`).

## Commands (this repo)

```bash
pytest                         # unit + contract + integration
pytest tests/e2e               # only with CODIE_E2E=1 and sandbox-repo tokens set
ruff check .                   # lint
ruff format .                  # format
mypy src/codie                 # type check
```

## Architecture

The deterministic **kernel** (pure functions in `src/codie/state/`, `queue.py`,
`reconcile.py`) derives project state from GitHub and computes the lawful work queue; the
**orchestrator agent** supplies judgment through gated tools. Four role crews — Planner,
Coder, Reviewer, Tester — execute the work through GitHub, each with its own identity.
A localhost web dashboard (`dashboard/`) shows live agent status and per-agent toggles.

Automation lives in `.github/workflows/ci.yml` (ruff, mypy, pytest; e2e is schedule-gated by
`CODIE_E2E=1`).
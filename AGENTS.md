# AGENTS.md — codie

Guidance for AI agents (and humans) working on **this repository**.

## What this project is

Codie is an autonomous software-development crew built with CrewAI. Given a GitHub repository and
a PRD with a definition of done ("evals"), it plans features, breaks them into dev/integration/test
tasks, implements them via PRs, reviews and tests them, and iterates until the evals pass. All
project state lives on GitHub (issues + labels + PRs); local state is a disposable cache.

The authoritative design documents are:

- [`docs/PRODUCT_SPEC.md`](docs/PRODUCT_SPEC.md) — workflow, label taxonomy, roles, guardrails
- [`docs/TECHNICAL_SPEC.md`](docs/TECHNICAL_SPEC.md) — architecture, state derivation, config,
  tools, milestones

**Spec-sync rule (mandatory):** if a change alters the workflow, label taxonomy, role contracts,
state derivation, configuration schema, spec conventions, or module layout, update the relevant
spec **in the same commit**, and update this AGENTS.md if the layout or commands below change.

## Repository layout

Planned structure (per Technical Spec §3). Until code lands, treat this as the target layout;
once code exists, keep this section accurate.

```
src/codie/            # application package (orchestrator, state, roles, tools, github, provision, survey, dashboard, ...)
tests/
  unit/               # pure-function tests: parsers, deriver, reconciler, queue, config
  contract/           # FakeGitHubClient vs PyGithubClient behavior suite
  integration/        # orchestrator cycles over the fake client
  e2e/                # real sandbox repo; gated behind CODIE_E2E=1 (never runs in PR CI)
docs/                 # PRODUCT_SPEC.md, TECHNICAL_SPEC.md
```

## Setup

```bash
# Python >= 3.11 required
pip install -e .[dev]          # or: uv pip install -e .[dev]
codie validate-config          # verifies config + env wiring (safe: no network writes)
```

Credentials come from the environment only — never write them to files or commit them:
`CODIE_LLM_API_KEY`, `CODIE_GH_TOKEN_PLANNER`, `CODIE_GH_TOKEN_CODER`,
`CODIE_GH_TOKEN_REVIEWER`, `CODIE_GH_TOKEN_TESTER`, `CODIE_GH_TOKEN_KERNEL`,
`CODIE_GH_TOKEN_ADMIN`
(the admin token is used only by `codie init` provisioning, never by the running crew).

## Commands

```bash
pytest                         # unit + contract + integration
pytest tests/e2e               # only with CODIE_E2E=1 and sandbox-repo tokens set
ruff check .                   # lint
ruff format .                  # format
mypy src/codie                 # type check
```

Run all of the above (except e2e) before considering any change complete.

## Coding conventions

- Python ≥ 3.11, fully type-annotated; mypy must pass. pydantic v2 for all config and structured
  LLM outputs.
- **GitHub mutations go through `GitHubClient` only.** Never call PyGithub (or HTTP) directly from
  orchestration, role, or tool code — the fake client must be able to stand in everywhere.
- **The Orchestrator agent acts only through its gated tools** (Technical Spec §6.1). Guardrails —
  budgets, concurrency, transition validity, cycle caps — live in the deterministic kernel and
  must never depend on agent behavior or prompt text.
- State derivation (`state/`), the work queue (`queue.py`), and reconciliation are **pure,
  deterministic functions** of GitHub data. No LLM calls, no I/O, no clocks passed implicitly in
  these modules.
- CrewAI agent memory stays disabled; context is assembled per work item in `context.py`.
- Specs are code: anything under `specs/` changes only via PR approved by the Reviewer acting as
  product owner (Product Spec §9); requirement IDs (`FR-*`/`NFR-*`/`AC-*`) are stable and cited
  in downstream work.
- Credentials and secrets: environment variables referenced by name in YAML; redact in logs and
  traces.
- Conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`), referencing issues where
  applicable.

## Git workflow (this repo dogfoods the codie model)

- `main` — stable; releases are tagged here. Protected: PRs only.
- `dev` — active development and integration. Protected: PRs only.
- Work happens on `feature/<issue#>-<slug>` branches off `dev`, merged by PR (squash) into `dev`.
- Do not push directly to `main` or `dev`. Do not force-push shared branches.
- Task tracking uses GitHub issues with the codie label taxonomy (see Product Spec §6).

## Safety rules for agents

- Never run `codie start` (the orchestrator) against any real repository without explicit user
  approval; tests must use `FakeGitHubClient` or a designated sandbox repo.
- Never commit credentials, tokens, or files under `~/.codie/` (workspaces, state DBs, logs,
  traces).
- E2E tests must stay gated behind `CODIE_E2E=1` and must target only the configured sandbox repo.
- Do not weaken the guardrails defined in the specs (branch protection expectations, the
  work-item cycle cap, shell denylist, budget caps) without an explicit spec change.

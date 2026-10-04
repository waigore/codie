# Codie — Technical Specification

- **Status:** Draft v0.4.2 (round-4 reconciliation + substantial-comments norm)
- **Date:** 2026-10-04
- **Companion document:** [PRODUCT_SPEC.md](PRODUCT_SPEC.md) (workflow, labels, roles — assumed
  throughout)

---

## 1. Architecture Overview

Codie is a Python application with a strict separation between a **deterministic kernel** and
**agentic judgment** exercised through it:

```
┌──────────────────────────────────────────────────────────────────────┐
│           ORCHESTRATION KERNEL (plain Python, deterministic)          │
│  poll → derive state → reconcile → compute lawful work queue          │
│  → enforce budgets / concurrency / transition validity / cycle caps   │
└──────────────▲───────────────────────────────────────┬───────────────┘
               │ context pack (state, queue,           │ gated tool calls only
               │ conventions, anomalies, budgets)      ▼
        ┌──────┴───────────────┐                 ┌──────────────────────┐
        │ ORCHESTRATOR AGENT   │  dispatch       │ ROLE CREWS (CrewAI)  │
        │ (CrewAI, no memory)  ├────────────────►│ planner · coder ·    │
        │ judgment & triage    │                 │ reviewer · tester    │
        └──────────────────────┘                 └──────────┬───────────┘
                                                            │
                                             ┌──────────────┴───────────┐
                                             ▼                          ▼
                                  ┌───────────────────┐      GitHub (issues,
                                  │ GitHubClient      │      labels, PRs)
                                  │ (PyGithub,        │         ▲
                                  │ per-role tokens)  │         │
                                  └───────────────────┘         │
                                     single source of truth ────┘
```

**Design principles**

1. **A deterministic kernel with an agentic orchestrator.** All *facts about work* — derivation,
   reconciliation, the lawful queue — and all *hard limits* — budgets, concurrency, transition
   validity — are computed and enforced by plain-Python machinery. The **Orchestrator agent**
   (an LLM crew) supplies judgment on top: dispatch sequencing, anomaly triage, and interpreting
   non-standard project conventions. It acts only through gated tools and can never exceed the
   kernel's limits or act outside the lawful queue. Facts and limits stay resumable, auditable,
   and unit-testable; judgment is applied exactly where fixed rules run out.
2. **GitHub is the single source of truth.** Local state is a disposable projection (§5.8).
3. **One work item = one CrewAI crew run.** Each dispatch assembles a fresh context pack (§7.1)
   and kicks off a single-agent crew — an orchestrator cycle works the same way. No agent memory
   persists across runs (CrewAI memory is disabled); anything worth remembering is written to
   GitHub (comments, issue bodies, code).
4. **Planner proposes, orchestrator applies.** Issue, label, and ready-for-review mutations from
   planning output are applied by the kernel **after** structured validation (§7.2); the one
   exception is that the Planner may commit spec files to a **draft** spec branch/PR under its own
   token before validation — the kernel validates those files and is the only actor that marks the
   spec PR ready-for-review (§7.2). Coder/Reviewer/Tester act via tools directly.

## 2. Tech Stack

| Concern | Choice |
|---|---|
| Language | Python ≥ 3.11 |
| Agent framework | CrewAI (`crewai`, `crewai-tools`) |
| LLM access | CrewAI `LLM` (litellm under the hood), **OpenAI-compatible endpoint by default** |
| GitHub API | PyGithub |
| Config | YAML + pydantic v2 models |
| CLI | Typer |
| Local state | SQLite (stdlib `sqlite3`) |
| Web dashboard | FastAPI + uvicorn, embedded in the orchestrator process; vanilla-JS SPA, no build step |
| Retries | tenacity |
| Logging | structlog (JSON + console renderers) |
| Tests | pytest, pytest-mock; e2e gated behind env var |
| Quality | ruff (lint+format), mypy (strict-ish) |

Exact dependency versions are pinned in `pyproject.toml`, not in this spec.

## 3. Module Layout

```
src/codie/
├── cli.py                  # Typer entry: init / start / stop / status / labels sync / validate-config
├── config.py               # pydantic settings: codie.yaml + .codie.yaml merge, env resolution
├── models.py               # ProjectState, Feature, TaskItem, Bug, Eval, PRInfo, WorkItem,
│                           # WorkResult, Finding, IssueTriage, PrdProposal, FeatureIssueDraft,
│                           # TaskSpec, TaskBreakdown, RevisionAssessment, ReplanResult,
│                           # SpecDraftResult, ReleasePlan, BugDraft, ReviewDecision, ...
├── orchestrator.py         # daemon + kernel: fetch → derive → reconcile → enforce → sleep (§6)
├── provision.py            # codie init phase 3: mechanical, convention-aware provisioning
├── survey.py               # codie init phase 1: RepoSurvey models + dimension extraction
├── state/
│   ├── derive.py           # GitHub payloads → ProjectState (pure functions)
│   ├── parse.py            # issue body markers, label semantics, PR linkage
│   ├── reconcile.py        # label drift correction, lifecycle side-effects
│   ├── validate.py         # invariant checks → violations
│   └── transitions.yaml    # THE transition table + per-role mutation matrix (§5.5) — data, loaded by kernel and tools
├── queue.py                # work-queue computation + prioritization
├── cache.py                # SQLite projection: snapshots, poll cursor, runs, cost ledger
├── github/
│   ├── client.py           # GitHubClient protocol (port)
│   ├── pygithub_client.py  # production adapter (per-role token routing)
│   ├── fake.py             # in-memory fake for tests / --dry-run
│   └── labels.py           # label-set sync
├── workspace.py            # clone/reuse workspaces, janitor, locks
├── runner.py               # jailed subprocess execution (setup/build/test/eval)
├── dispatch.py             # work item → crew construction → kickoff → result handling
├── dashboard/
│   ├── server.py           # FastAPI app: REST + SSE, serves the embedded SPA (§6.5)
│   ├── roster.py           # per-role status model: current/last work, enabled flags
│   └── static/             # index.html, app.js — no build step
├── roles/
│   ├── prompts/            # orchestrator.md, planner.md, coder.md, reviewer.md, tester.md, surveyor.md (+ shared.md)
│   ├── orchestrator.py     # the Orchestrator agent crew: judgment over the kernel (§6.1)
│   ├── planner.py
│   ├── coder.py
│   ├── reviewer.py
│   ├── tester.py
│   └── surveyor.py         # init-time only: repo survey crew (§9.6)
├── context.py              # context-pack assembly per work item
├── tools/
│   ├── github_tools.py     # CrewAI tools over GitHubClient
│   ├── file_tools.py       # workspace-jailed read/write/search
│   └── shell_tools.py      # workspace-jailed command execution
└── llm.py                  # LLM construction from config (OpenAI-compatible default)
```

## 4. Configuration

### 4.1 Files and precedence

1. Global: `~/.codie/codie.yaml`. `./codie.yaml` is used **only** when the home file is absent
   and `--config` was not passed; `--config` always wins.
2. Per-project: `.codie.yaml` at the target repo root — **replaces**, per top-level key, the
   project-scoped keys `branches`, `merge_method`, `surface`, `label_mapping`, `acceptance`,
   `commands`, `run_app_session`, `evals`, `workflow`, and `guardrails` (there is no `polling`
   object; `poll_interval_seconds` lives on the global project entry). The global project entry
   owns `repo`, `workspace`, `poll_interval_seconds`, `budgets`, and `concurrency`.
3. **Credentials are never values in YAML.** YAML names the environment variable. Each subcommand
   resolves **only the vars it needs**: `codie start` requires the five role tokens +
   `CODIE_LLM_API_KEY`; `codie init` requires the admin token + the LLM key;
   `CODIE_DASHBOARD_TOKEN` is required only when `dashboard.host` is not `127.0.0.1`/`::1`
   (loopback needs no token, §6.5). Resolution fails fast with a clear, per-subcommand error.

### 4.2 Global `codie.yaml` — annotated example

```yaml
llm:
  provider: openai-compatible            # default; anything litellm routes is allowed
  base_url: https://openrouter.ai/api/v1 # OpenRouter, OpenAI, or any compatible endpoint
  api_key_env: CODIE_LLM_API_KEY         # env var NAME, not the key
  default_model: anthropic/claude-sonnet-4.5
  models:                                # optional per-role overrides
    planner: anthropic/claude-opus-4
    coder: anthropic/claude-sonnet-4.5
    reviewer: anthropic/claude-sonnet-4.5
    tester: anthropic/claude-haiku-4.5
  temperature: 0.2
  max_retries: 4

github:
  tokens:                    # one identity per role → audit trail + rate-limit isolation.
                             # each entry is { env, login }, both required (M16): env names the
                             # token var; login is the bot's GitHub login used for human-vs-bot
                             # classification (§5.2) and for init's collaborator invites (§9.6)
    planner:  { env: CODIE_GH_TOKEN_PLANNER,  login: codie-planner-bot }
    coder:    { env: CODIE_GH_TOKEN_CODER,    login: codie-coder-bot }
    reviewer: { env: CODIE_GH_TOKEN_REVIEWER, login: codie-reviewer-bot }
    tester:   { env: CODIE_GH_TOKEN_TESTER,   login: codie-tester-bot }
    kernel:   { env: CODIE_GH_TOKEN_KERNEL,   login: codie-kernel-bot }   # reconciliation, escalation, heartbeats, release PR (§9.2)
  admin: { env: CODIE_GH_TOKEN_ADMIN }   # used by `codie init` ONLY — never loaded by the orchestrator
  api_base_url: https://api.github.com   # override for GH Enterprise

dashboard:
  enabled: true
  host: 127.0.0.1                        # localhost-only by default
  port: 8640                             # busy → tries the next few ports, prints the actual URL
  token_env: CODIE_DASHBOARD_TOKEN       # required when host is not localhost

projects:
  - repo: myorg/mygame
    workspace: ~/.codie/workspaces/myorg/mygame   # overrides the default (§8.1)
    poll_interval_seconds: 60
    budgets:                        # defaults when a key is omitted (C18):
      max_llm_cost_usd_per_day: 20  #   daily cap 20 USD
      max_llm_cost_usd_per_run: 20  #   per-run cap (defaults to the daily cap)
      max_task_duration_minutes: 45 #   45
    concurrency: { per_role: 1 }    #   per_role 1
```

### 4.3 Per-project `.codie.yaml` (in the target repo)

```yaml
branches:
  main: main
  dev: dev
  feature_pattern: "feature/{issue}-{slug}"
  test_pattern: "test/{issue}-{slug}"          # C12: kind:test PRs (Tester-owned)
merge_method: squash             # squash | merge | rebase — confirmed at init from repo history
surface: cli                     # ui-web | ui-mobile | tui | cli | api | game | library
                                 # (ui-desktop is reserved, unsupported in v1 — see §7.5 matrix)
label_mapping: {}                # dict[str,str]: existing repo label → one codie label (M24).
                                 # Two source labels mapping to two different status:* or two
                                 # different type:* labels is a config error.
acceptance:                      # the Tester's black-box, user-perspective suite (Product Spec §7.5)
  framework: subprocess          # must be a supported driver for `surface` (§7.5 matrix; M17 —
                                 # a pair off the matrix is a config error caught by validate-config)
  dir: tests/acceptance          # Tester-owned; the Coder never writes here
  screenshot_tolerance: 0.01     # max per-image pixel mismatch for game/mobile baselines
commands:                        # argv lists, executed WITHOUT a shell, workspace-jailed (§8.2)
  setup: ["pip", "install", "-e", ".[dev]"]          # REQUIRED
  build: ["python", "-m", "build"]
  run_app: ["python", "-m", "myapp"]          # REQUIRED unless surface: library (session-managed)
  test_fast: ["pytest", "-x", "-q", "tests/unit"]   # REQUIRED
  test_full: ["pytest", "-q"]
  test_acceptance: ["pytest", "tests/acceptance", "-q"]
  lint: ["ruff", "check", "."]
  # simulator_boot / simulator_install / simulator_shutdown: REQUIRED argv lists when
  # framework ∈ {maestro, xcuitest, espresso} (M17)
run_app_session:                 # optional, used by the Tester's session mode (§8.2)
  env: {}                        # extra env vars for the app under test (added only to run_app)
  ports: [8080]                  # ports the session is expected to occupy (runner waits on accept)
  display: headless              # headless | xvfb | native
evals:                           # EVERY PRD eval is `command` or `human` (M32); `codie start`
                                 # refuses an `E*` with neither `run` nor `mode: human`
  E1: { run: ["pytest", "tests/e2e/test_onboarding.py"], expect: exit_zero }   # command
  E2: { run: ["python", "scripts/check_bundle_size.py", "--max-kb", "5120"], expect: exit_zero }
  E3: { mode: human }            # human-confirmed eval — the checkbox waits on the human
workflow:
  bugs_outrank_features: true
  full_test_cadence_minutes: 60
guardrails:
  max_cycles_per_work_item: 3    # completed cycles before kernel escalation (§5.9)
  heartbeat_timeout_minutes: 20  # stale in-progress reset threshold (§5.8)
  max_defer_cycles: 3            # consecutive deferrals of one queue item before kernel escalation (§6.1)
  max_idle_dispatch_cycles: 2    # C19: idle cycles before the kernel dispatches the head item itself
  shell_denylist: ["git push --force*", "git push -f", "git push --force-with-lease", "rm -rf /*"]
                                 # fnmatch globs matched against the argv joined by single spaces (N17)
```

### 4.4 Config models

pydantic v2 models in `config.py` mirror the above (`Settings`, `LLMSettings`, `GitHubSettings`,
`ProjectConfig`, `ProjectOverrides`, `Budgets`, `Guardrails`). Unknown keys are errors
(`model_config = ConfigDict(extra="forbid")`). `codie validate-config` prints the effective merged
config with secrets redacted. **Validation rules (fail `validate-config` / block `codie start`):**
every configured model has an `llm.price_map` entry `{input_per_1k, output_per_1k}` (a model with
no price fails closed, §6.2); `commands.setup` and `commands.test_fast` are required;
`commands.run_app` is required unless `surface: library`; the `surface`/`acceptance.framework`
pair is on the §7.5 matrix; when a URL is given (and at `codie start`, which always has one),
every PRD eval `E*` has either `evals.E<n>.run` (+ `expect`) or `evals.E<n>.mode: human` (M32);
a missing optional command fails the work item that calls it (not
config load).

## 5. Project State: Derivation, Reconciliation, Rebuild

This section defines **exactly** how the daemon determines project state.

### 5.1 Source of truth and fetch strategy

State is a pure function of these GitHub inputs for the target repo:

- All issues matching the codie label universe (`type:*`), open **and** closed (closed retains
  terminal statuses), fetched with `since=<last_poll_timestamp>` deltas.
- **All PRs needed to derive state** (C15 — there is no "active window"): every open PR; every PR
  linked from a fetched codie issue (via `Refs #n`/`Closes #n` parsing + timeline cross-reference
  events); and every PR whose body carries the `<!-- codie:release -->` marker, **merged or not**.
  `release_merged` is derived from that set, so a merged release PR is detected on any machine
  regardless of age. This includes **spec PRs**, detected by their file paths
  (`specs/<issue#>-<slug>/`) and linked to features via the feature issue's `## Specs` section
  (Product Spec §10). All fetched PRs carry their reviews.
- Issue comments (delta by `since`) — heartbeat, UAT summaries, revision feedback, and the marker
  comments (`<!-- codie:cycle … -->`, `<!-- codie:defer … -->`, `<!-- codie:idle-dispatch … -->`,
  `<!-- codie:prd <sha256> -->`, `<!-- codie:revision … -->`, `<!-- codie:suite … -->`,
  `<!-- codie:eval … uncertain -->`, `<!-- codie:blocked … -->`) that make the cycle cap, deferral
  counts, PRD fingerprint, suite results, and block attribution **reconstructable from GitHub on a
  fresh machine** (C15); the daily cost ledger is the single intentional local exception (§5.8).
- Branch refs for the configured stable/integration heads (`branches.main`/`branches.dev`);
  `.codie.yaml` content at the integration branch head.

Fetch pattern per poll cycle: (a) delta issues/comments via `since`; (b) open PR list; (c) the
linked and marker-carrying PRs above (fetched by number, cached by `updated_at`); (d) every
`full_refresh_cycles = 10` cycles (or on any parse/invariant anomaly, or cold start) a **full
fetch**. PyGithub rate-limit responses are honored with backoff; conditional/delta fetching keeps
the typical cycle to a handful of API calls.

### 5.2 Parsing rules (`state/parse.py`)

- **Convention mappings:** the confirmed survey mappings parameterize parsing —
  `branches.feature_pattern`, the label mapping, and linkage habits from `.codie.yaml` make
  derivation convention-aware without code changes. States that resist confident mapping are
  emitted as **anomalies** (soft signals for the Orchestrator agent, §6.1) rather than forced
  into the model.
- **Labels:** exactly one `type:*` and one `status:*` per managed issue (`type:prd` is exempt from
  `status:*`); zero or one `kind:*`; at most one `priority:*`; any `flag:*`. Unknown
  codie-namespaced labels → violation.
- **Body markers** (case-insensitive, line-anchored): canonical form is `**Key:** value` with
  Markdown bold **optional** (`Key: value` parses identically). Keys: `Parent: #n` (task/bug →
  feature), `Depends on: #a, #b`, `Evals: E1, E3`, `PRD sections: <free text>`, plus checklists
  under `## Acceptance criteria`, `## Done when`, `## Tasks`. Feature issues additionally carry a
  `## Specs` section mapping the feature to its spec files and spec PR; requirement IDs
  (`FR-*`/`NFR-*`/`AC-*`) inside "Done when" items are parsed for traceability reporting but
  never gate transitions mechanically. The PRD eval checklist line grammar is
  `- [ ] E1: <text>` / `- [x] E1: <text>`.
- **PR → task linkage:** `Closes #n` in body (primary); fall back to timeline cross-references;
  unlinked open PRs from coder branches link via the configured `branches.feature_pattern`.
  A PR carrying the `<!-- codie:release -->` marker is the release PR (queue rules 13–14).
- **PRD edit detection:** the kernel records a body fingerprint comment `<!-- codie:prd
  <sha256> -->` on the PRD issue, computed after normalizing eval checkbox lines (so the Tester's
  check-off does not count). A change to that fingerprint — by anyone — enqueues `ReconcilePlan`
  (§5.6 rule 2b); a missing fingerprint queues it once. This is derivable from GitHub alone, so a
  fresh machine detects the change without any cache state.
- **Human vs crew authorship:** comment/label actors are matched against the five configured bot
  logins — `github.tokens.{planner,coder,reviewer,tester,kernel}.login` (M16); anything else is
  human. Human label flips on `status:review` features are the UAT signal. An **`APPROVED` review
  by the reviewer bot** on the spec PR's current head is the spec-approval signal (product-owner
  review, §7.4).

### 5.3 Derived model (`models.py`)

```python
ProjectState:
    repo: str
    prd: PrdIssue | None            # title, body, evals: list[Eval(id, text, checked, command?)]
    features: list[Feature]         # number, status, priority, flags, evals, criteria, tasks[]
    tasks: list[TaskItem]           # number, kind, status, parent, depends_on, linked_pr, assignee
    bugs: list[Bug]                 # as tasks + reproduction metadata
    prs: list[PRInfo]               # number, branch, base, status, review state, ci, linked issue,
                                    # markers (codie:release), draft flag, reviewer_approved flag
    branches: BranchHeads           # configured stable/integration SHAs
    violations: list[Violation]     # invariant failures, see 5.4
    anomalies: list[Anomaly]        # soft signals: untyped-but-active issues, human-opened PRs,
                                    # convention drift — triaged by the Orchestrator agent (§6.1)
```

Feature `tasks[]` is the **set of issues whose `Parent:` resolves to the feature** (M20). A
**conflict** — a set mismatch between that set and the `## Tasks` checklist's issue numbers — is a
violation reported on the feature (§5.4); checkbox state is informational, never an invariant.
Requirement lines follow the single grammar of Product §9.3 (live `^- (FR|NFR|AC)-[1-9][0-9]*: …$`,
AC trace list mandatory, whole-line `~~…~~` strikethrough, `## Done when` citations with ≥1 ID).

### 5.4 Invariants (`state/validate.py`)

Derivation always succeeds — violations are reported, never fatal:

1. Exactly one `type:*` per managed issue, and exactly one `status:*` per managed issue **except
   `type:prd`** (the PRD has no lifecycle status).
2. `Parent:` is optional; when present it must resolve to an existing `type:feature`.
3. `Depends on:` resolves to task/bug issues; the dependency graph is acyclic.
4. Task statuses are consistent with their feature's status, per this table (not an example):
   under a `proposed`, `speccing`, `spec-review`, or `cancelled` feature, every child is absent or
   `cancelled`; under `specified`, every child is absent or `cancelled` (breakdown not yet
   applied); under `planned`, `in-progress`, or `revised`, any task status is legal (`revised`
   children are cancelled or replanned when the revision marker routes, §5.6 rule 3); under
   `review` or `accepted`, every child is `done` or `cancelled`.
4b. At most one open spec PR per feature (carrying both spec files). Merged spec PRs are
   predecessors and do not count. A feature is `status:spec-review` iff its open spec PR is
   ready-for-review (not draft) and the latest review on its head is not changes-requested;
   `speccing` allows zero or one open spec PR; `specified` and every later non-cancelled status
   require the latest spec PR to be merged and no open successor. `planned`/`in-progress`
   features with an unmerged latest spec PR are violations.
5. `status:in-review` tasks/bugs have an open linked PR; `status:verifying` or `status:done`
   bugs and `status:done` tasks have a merged linked PR.
6. `kind:*` present iff `type:task`.
6b. At most one `priority:*` per issue (two known priority labels are a violation, not "unknown
    labels").
7. Eval IDs referenced by features exist in the PRD.
8. Exactly one `type:prd` issue. A second PRD issue is a violation: the kept candidate is the
   pinned one with the greatest `updated_at` (when none is pinned, the one with the greatest
   `created_at`); the kernel posts one comment and adds `flag:needs-human` to the kept issue, and
   the rest are excluded.
9. A `status:review` feature has every non-cancelled task `done` (at least one non-cancelled task)
   and every linked bug `done` or `cancelled` (an "open bug" is one outside `{done, cancelled}`).
   A dependency edge is satisfied when its target is `done` or `cancelled`; a cancellation that
   leaves a non-cancelled issue depending on the cancelled number is rejected unless the same
   payload rewrites that edge. Any open bug — parented or parentless — blocks queue rules 13–14.

Each violation → add `flag:needs-human` to the offending issue, post one explanatory comment, and
**exclude the issue from dispatch** until resolved.

### 5.5 Status reconciliation (`state/reconcile.py`)

Derived state is the *truth*; labels must reflect it. The reconciler computes expected labels and
applies the minimal mutation diff. **All legal edges and all per-role write permissions live in
one data file, `state/transitions.yaml`**, loaded by the reconciler and by every role's label
tools (a role tool call naming an edge outside its matrix row is refused by the kernel).

**Feature transition table** (actor in parentheses; "kernel" = reconciler/dispatch applier):

| From | To | Trigger | Actor |
|---|---|---|---|
| — | `proposed` | feature created from validated `FeatureIssueDraft` | kernel (for Planner) |
| `proposed` | `speccing` | `DraftSpecs` dispatched | kernel |
| `speccing` | `spec-review` | spec PR open **and ready-for-review** (not draft) | kernel |
| `spec-review` | `speccing` | spec PR changes-requested (human or Reviewer) | kernel |
| `spec-review` | `specified` | spec PR merged (requires the Reviewer's `APPROVED` on current head — Reviewer-side gate, §7.4) | kernel |
| `specified` | `planned` | validated `TaskBreakdown` applied | kernel |
| `planned` | `in-progress` | any child task leaves `backlog` (i.e., any child outside `{backlog, cancelled}`), from current labels | kernel |
| `in-progress` | `review` | acceptance run passed | Tester |
| `review` | `accepted` / `revised` | human label flip (UAT) | **human** |
| `revised` | `speccing` | `<!-- codie:revision requirements -->` marker → kernel cancels non-terminal children, closes their PRs, then re-spec | kernel |
| `revised` | `planned` | `<!-- codie:revision implementation -->` marker → `ReplanResult` validated and applied | kernel |
| any non-terminal | `cancelled` | validated Planner cancellation | kernel |

**Task/Bug transition table:**

| From | To | Trigger | Actor |
|---|---|---|---|
| — | `backlog` / `ready` | created (unmet deps → `backlog`; else `ready`) | kernel (tasks, for Planner) / Tester (bugs) |
| `backlog` | `ready` | all dependencies satisfied (`done` or `cancelled`), no `flag:blocked` | kernel |
| `ready` | `in-progress` | claim (assign + label) — Coder for `dev`/`integration`/bug, Tester for `kind:test` | Coder / Tester |
| `in-progress` | `in-review` | PR opened — Coder, or Tester for `kind:test` | Coder / Tester |
| `in-review` | `in-progress` | review changes requested | Reviewer |
| `in-review` | `done` (task) | PR merged | Reviewer |
| `in-review` | `verifying` (bug) | fix PR merged | Reviewer |
| `verifying` | `done` (bug) | re-verification passes | Tester |
| `verifying` | `in-progress` (bug) | re-verification fails (cycle counter increments, §5.9) | Tester |
| `in-progress` | `ready` | stale-run recovery only (heartbeat timeout, §5.8) | kernel |
| any non-terminal | `cancelled` | validated Planner cancellation; open linked PR is closed with an explanatory comment | kernel |

**Per-role mutation matrix** (what each identity may write; everything else is refused):

| Actor | Labels | Issues | PRs | Files |
|---|---|---|---|---|
| Planner | none | none | create/update **draft** spec PRs (`spec/*` branches, `specs/<issue#>-<slug>/**` paths) | `specs/<issue#>-<slug>/**` only |
| Coder | its matrix edges; `flag:blocked` on its active-run issue only | claim (assign), comment | create/update task PRs (`feature_pattern` branches) | workspace, except `specs/**`, `acceptance.dir`, and policy files (`.codie.yaml`, `AGENTS.md`, `CODEOWNERS`) |
| Reviewer | its matrix edges | comment | review, merge (task PRs; spec PRs after its own approval on the current head; **no policy-file PR without a non-bot APPROVED**), close | read-only |
| Tester | its matrix edges; `flag:blocked` on its active-run issue only | create bugs, comment, PRD checklist check-off only | create acceptance-suite PRs (`test_pattern` branches); read-only otherwise | `acceptance.dir` + test artifacts (never policy files) |
| Kernel | its matrix edges, `flag:needs-human` | create/comment/edit per validated role output; escalation + heartbeat comments; close/reopen issues per the terminal-status rule below | open release PR; close cancelled-task PRs | none |
| Human | any | any | any (incl. UAT flips, release merge) | any |

Human-owned transitions (`review → accepted/revised`) are detected, never written by the kernel.
Human flag toggles are detected and never overwritten by the kernel.

**Issue open/close lifecycle (kernel-owned).** No PR uses a closing keyword (`Refs #<n>` only).
The kernel **closes** an issue when it reaches a terminal status (`done`/`cancelled` for tasks
and bugs, `accepted`/`cancelled` for features) and **reopens** it when a later legal transition
leaves that set (stale recovery, re-spec, re-plan, a failed bug re-verification). This keeps
invariant 5's "merged linked PR" and `codie status` consistent instead of leaving a pile of open
terminal issues. Humans may close/reopen freely; reconciliation restores the mapping above and
posts one comment when it must reopen a human close that was not a terminal status.

**Flag-based dispatch filter (one rule, C13).** An item is excluded from the queue — and
`dispatch_work_item` refuses it — when its issue carries `flag:blocked` or `flag:needs-human`,
when it transitively `Depends on` such an issue, or when its parent feature carries either flag.
Sibling tasks of a flagged task stay eligible. This filter is the single meaning of "that issue's
stream" (Product §4/§11 and §5.9). `dispatchable` is true when at least one item survives the filter, its role is enabled, and
that role's slot is free. A role sets `flag:blocked` only on the issue of its active run, with a
comment carrying `<!-- codie:blocked <run_id> <actor> -->`; stale recovery clears it only when
that `run_id` is the orphaned run. An intentional block returns `WorkResult(status="needs_human")`
and is not retried as `failed`.

### 5.6 Work-queue computation (`queue.py`)

A pure function `ProjectState → list[WorkItem]`. The queue is also the **lawful action space**
of the Orchestrator agent (§6.1): it may dispatch, defer, or escalate queue items, but can
never invent work outside them.

**Matching semantics:** rules are evaluated in order, and each issue/PR/feature contributes to
**at most one** queue item per cycle (first match per entity) — so a ready `kind:test` task
yields exactly one `VerifyTask` and never an `Implement`.

**Derived signals used by the rules.** A PR's `review_state` is computed from reviews on its
**current head** only: `changes_requested` if any review on the head requests changes; else
`approved` if the reviewer bot has `APPROVED` on the head (this is the §5.2 spec-approval signal);
else `commented`, `dismissed`, or `none`. Spec-PR queue items are **attributed to the feature**,
so under first-match-per-entity a feature and its spec PR are one entity and yield at most one
item per cycle. A PR is "ready-for-review" when it is open and not a draft; the Reviewer tool
refuses to act on a draft PR.

```
1.  No PRD issue mid-run (a PRD missing at `codie start` exits before the loop, §6.4; this rule
    covers a PRD that disappears later)
                                      → [NeedsHuman("provide PRD")]   (daemon pauses, §6.1)
2.  PRD present, zero features        → [PlanDecomposition]           → Planner
2b. PRD body fingerprint changed (§5.2) → [ReconcilePlan] → Planner
    (payload PlanReconciliation; kernel creates/cancels/restales from it, §7.2)
3.  Feature revised, no codie:revision marker → [AssessRevision(feature, human_comments)] → Planner
    (kernel posts <!-- codie:revision requirements|implementation --> and leaves status revised;
     next cycle the marker routes: requirements → cancels non-terminal children, closes their
     PRs, sets speccing → rule 4; implementation → queues Replan, applies ReplanResult, then sets
     planned → rule 4e)
4.  Feature proposed, OR speccing with no ready-for-review spec PR and no changes-requested
    review                          → [DraftSpecs(feature)]         → Planner
    (resumes an existing open draft/spec branch; writes feature-spec.md + technical-spec.md,
     opens the spec PR as a DRAFT; the kernel validates the files and marks it ready-for-review
     → spec-review, §7.2/C17)
4b. Open spec PR whose head's latest review is changes-requested (feature speccing or
    spec-review)                      → [ReviseSpecs(feature, pr)]    → Planner
4c. Feature spec-review, spec PR ready-for-review, review_state ∈ {none, commented, dismissed}
                                      → [ReviewSpecPR(pr)]            → Reviewer
    (product-owner review — PRD alignment + spec quality; never merges here)
4d. Spec PR ready-for-review, review_state = approved, CI green (N6: §7.4 step 3)
                                      → [MergeSpecPR(pr)]             → Reviewer
4e. Feature specified, zero non-cancelled child tasks → [BreakDownTasks(feature)] → Planner
5.  Task/bug in-review, PR needs review → [ReviewPR(pr)]              → Reviewer
6.  Task/bug in-review, PR approved + CI green → [MergePR(pr)]        → Reviewer
    (task PRs only: linked to a task/bug, base = integration branch, no codie:release marker)
7.  Task/bug in-progress, open linked PR changes-requested → [AddressReview(item, pr)]
    → Tester if kind:test else Coder
8.  Task ready, kind:dev|integration; bug ready (bugs first if configured)
                                      → [Implement(task)]             → Coder
8b. Bug in-progress whose linked PR is merged → [Implement(bug)]      → Coder (resumes branch)
9.  Task ready, kind:test             → [VerifyTask(task)]            → Tester
10. Bug verifying                     → [ReverifyBug(bug)]            → Tester
11. Feature in-progress, every non-cancelled task done (≥1), every linked bug done/cancelled,
    passing full-suite marker for the current integration SHA → [AcceptanceRun(feature)] → Tester
12. Merges to integration branch; fast marker stale/missing, or full run due (§7.5)
                                      → [RegressionRun(scope=fast|full)] → Tester
13. Every non-cancelled feature accepted (≥1 accepted), an eval unchecked with neither an open
    bug citing it nor an `<!-- codie:eval E<n> uncertain -->` marker awaiting the human
                                      → [EvalSuite]                   → Tester
14. All evals checked, no open release PR → [ProposeRelease]          → Planner
    (Planner returns ReleasePlan; the KERNEL opens the release PR with the codie:release marker)
15. Release PR merged                 → HALT (success)
```

Prioritization within a rule: `priority:high > medium > low`, then lowest issue number (a missing
`priority:*` sorts as `medium`). Rules 5–7 outrank 8 (finish in-flight work before starting new
work). The orchestrator may dispatch at most one active item per role (`concurrency.per_role`).

### 5.7 Dispatch result handling

**Every** crew run — tool-acting or structured-output — returns a pydantic-validated
`WorkResult`, a **discriminated union keyed by `work_item_kind`** so each role's `payload` has a
concrete schema (M14):

```python
class Mutation(BaseModel):
    op: Literal["set_labels","comment","create_issue","edit_issue","create_pr",
                "merge_pr","close_pr","create_review","check_eval"]
    target: str                 # issue/PR number or ref the op applies to
    expected: BaseModel         # the post-state the kernel verifies after the run

class WorkResult(BaseModel):
    work_item_kind: str         # DraftSpecs | Implement | ReviewPR | … (discriminator)
    status: Literal["applied", "needs_human", "failed"]
    summary: str
    mutations: list[Mutation]   # expected GitHub writes, verified post-hoc
    payload: BaseModel          # concrete per-kind model: FeaturePlan, RevisionAssessment,
                                # TaskBreakdown, ReplanResult, PlanReconciliation,
                                # SpecDraftResult, ReleasePlan, BugDraft, ReviewDecision, …
```

The Orchestrator agent has **no result payload** — its effects are its gated tool calls; a
zero-tool-call cycle is a `failed` orchestrator cycle that still falls through to the C19
idle-dispatch counter.

A crew that exhausts its `max_iter` returns `WorkResult(status="failed")` — partial silent stops
are forbidden (§10). A `needs_human` result sets `flag:needs-human` on the work item's issue and
is **not** retried.

The orchestrator:

- verifies expected mutations actually landed (re-derives affected issues); a declared mutation
  that did not land marks the run `failed`,
- records the run (context pack hash, token usage, cost, duration) in the local cache,
- on `failed`: re-dispatch on a later cycle (max 3 attempts, §6.2) then `flag:blocked` + comment,
- on invariant violation introduced by the run: **the kernel does not revert GitHub state** — it
  re-derives, adds `flag:needs-human`, posts one comment, and does not retry (a merged PR stays
  merged).

### 5.8 Local cache and cold-start rebuild

Cache location: `~/.codie/state/<owner>/<repo>.db` (SQLite). Schema:

- `snapshots(cycle, derived_json)` — last N derived states (debugging, `codie status --diff`).
- `poll_state(id=1, last_poll_ts, cycle_count, last_fast_run, last_full_run)`.
- `runs(id, work_item_json, status, cost_usd, tokens, started_at, finished_at, trace_path)`.
- `agent_events(id, ts, role, run_id, level, kind, message, payload_json)` — agent-tagged event
  feed for the dashboard (§6.5); structured log records are dual-written here, secrets-redacted.
- `kernel_flags(role TEXT PRIMARY KEY, enabled INTEGER)` — per-role enable/disable toggles from
  the dashboard; all roles default to enabled when the cache is rebuilt.
- `ledger(day, cost_usd)` — budget enforcement.

**Rebuild rule:** the cache is *never* authoritative. On startup the orchestrator performs a full
fetch and derives state from GitHub regardless of cache contents; the cache only supplies the poll
cursor (an optimization — if missing, the first cycle is a full fetch) and the budget ledger. If
the DB is missing, corrupt, or schema-mismatched, it is deleted and recreated. Agent conversation
context is never cached (assembled fresh per work item, §7.1), so **a new environment loses no
work state and no guardrail counter** — those are reconstructed from GitHub markers (§5.1). The
one intentional local exception is the **daily cost ledger** (`ledger.day` is the UTC date): a
machine with no row for today starts at $0 (C15).

**Stale-work recovery (tasks and bugs):** the kernel posts a heartbeat comment marker
(`<!-- codie:heartbeat <run_id> <iso8601> -->`) plus cache entries each minute (heartbeat
authorship is the kernel token, §9.2 — C14). The staleness clock is the newest heartbeat
comment's `created_at`, or — when no heartbeat exists — the `in-progress` label event's time, so
a missing heartbeat still has an age. On startup or during reconciliation, a task or bug
`status:in-progress` whose staleness is older than `heartbeat_timeout_minutes` (default 20,
`guardrails.heartbeat_timeout_minutes`) and whose run is not active locally is reset to
`status:ready` with an explanatory comment; the stale run is marked `orphaned`. **Resume is
defined:** the next dispatch on that item finds and resumes the existing branch/PR (§7.3; a
`kind:test` item resumes under the Tester). Recovery clears `flag:blocked` **only** when the
block comment's `<!-- codie:blocked <run_id> <actor> -->` names the orphaned run being reset, and
says so in the comment (§5.5 flag filter). Otherwise `flag:blocked` is cleared only by the role
that set it (after the blocking condition is gone) or by a human.

### 5.9 Oscillation and deadlock escalation

Roles never defer to each other (Product Spec §8.5), so contradictions must not loop — and the
**kernel is the only component that counts cycles and applies the cap** (roles never
self-escalate). During reconciliation it attributes every status transition, PR review, and
`**Dispute:**` comment to its actor (bot identity or human) from timeline events, maintaining one
**per-item cycle counter**. A completed cycle is any of:

- a PR **review round** (`in-progress → in-review → in-progress`),
- a **spec-review round** (`speccing → spec-review → speccing`),
- a bug **reopen round** (`in-review → verifying → in-progress`),
- a **failed acceptance run** on a feature (the `<!-- codie:cycle acceptance-failed <run_id> -->`
  marker, §7.5 — survives restarts, C15),
- a **dispute exchange** — completes when the answering role posts a later `**Dispute:**` or
  `**Concede:**` comment. Only a comment whose **first line** is `**Dispute:**` or `**Concede:**`
  counts. The pair is Coder/Reviewer on a task PR, Coder/Tester on a bug, Planner/Reviewer on a
  spec PR; the cycle completes when the *other* member of the pair later posts one of those two
  lines, and the counter sits on the issue the comment is attached to (M22).

Retries of failed runs (§5.7, §6.2) do **not** increment the counter. When the counter exceeds
`guardrails.max_cycles_per_work_item` (default 3) completed cycles without the item reaching a
terminal state, the kernel:

1. adds `flag:needs-human` to the issue (which excludes it via the §5.5 flag filter — "stream" is
   exactly that filter, not a fixed subtree),
2. posts a **deadlock summary** comment quoting each side's most recent position (last review
   verdict, last reproduction result, last dispute comment),
3. pauses the dependent stream — exactly the §5.5 flag filter (the flagged issue plus whatever
   transitively depends on it or has it as parent), never a fixed subtree.

The human resolves by commenting the decision and/or adjusting labels and the plan, then removing
the flag; reconciliation resumes the item from its current status. No role may resolve a dispute
by silently reverting another role's transition — the only exits are reconciliation with new
evidence or human decision.

## 6. Orchestrator

### 6.1 Main loop: kernel + Orchestrator agent

```python
def run(project_url):
    project = registry.resolve(project_url) # parse URL → owner/repo; auto-register on first use
    require_provisioned(project)            # minimum provisioning checklist below; else exit
    workspace.ensure()                      # clone or reuse + janitor (§8)
    cache.open_or_rebuild()
    labels.ensure_label_set()               # idempotent sync (repairs label drift)
    dashboard.start(project)                # serves until shutdown (§6.5; skipped in --once mode)
    while True:                             # ---- kernel (deterministic) ----
        state = derive(github.fetch(project))        # pure; config-driven by survey mappings
        reconcile(state, validate(state))            # transitions, drift, stale recovery,
                                                     # cycle-cap escalation — all mechanical
        queue = compute_queue(state)                 # the lawful action space (§5.6)
        reap_finished_runs()                         # async role runs → results (§5.7);
                                                     # ALWAYS reaped, incl. while paused (C14)
        enforce_duration_caps()                      # max_task_duration_minutes (C14)
        if state.release_merged: summarize_and_exit()       # the ONLY halt
        queue = flag_filter(queue)                   # C13: drop blocked/needs-human streams
        if budgets_exhausted():                      # PAUSE: cancel in-flight provider calls,
            sleep_with_backoff(...); continue        # no new dispatch, no model calls, no spend
        if not dispatchable(queue):                  # nothing survives the filter
            sleep_with_backoff(...); continue        # keep polling (no model calls)
        dispatched = False
        if enabled("orchestrator"):                         # N13: a disabled orchestrator
            outcome = orchestrator_agent.run_cycle(         #   skips the model call only;
                build_orchestrator_context(state, queue))   #   derive/reconcile/poll continue
            handle_cycle_outcome(outcome)              # log, ledger, traces
            dispatched = outcome.successful_dispatch
        dispatched = idle_dispatch_fallback(queue, dispatched)  # C19 below
        sleep_with_backoff(project.poll_interval_seconds)  # skipped right after a mutation (§9.5)
```

**Idle-dispatch fallback (C19 — the Orchestrator cannot starve a nonempty queue).** After
`reap_finished_runs`, if the filtered queue is nonempty and the cycle produced no successful
`dispatch_work_item`, the kernel posts `<!-- codie:idle-dispatch <n> -->` on the first eligible
item (the counter is read from that issue's timeline, §5.9/C15). At
`n == guardrails.max_idle_dispatch_cycles` (default 2) the kernel dispatches that item itself,
using the same gates as the tool (queue membership, role enabled, slot free, budget). A disabled
Orchestrator skips the model call and still takes this path — disabling means "no Orchestrator
model call", and the kernel still dispatches, so the crew cannot stall with lawful work available.
`annotate`, `get_*`, and `defer_work_item` do not reset the counter; a successful dispatch resets
it to 0. A zero-tool-call Orchestrator cycle (M14) is a failed cycle and still falls through to
this counter.

**Minimum provisioning checklist** (`require_provisioned`; `codie doctor` reports the same):
codie labels exist; both configured branches exist; PRs are required on both branches; direct
pushes to both are denied; the stable branch is human-merge-only (CODEOWNERS or equivalent).
Missing items are reported and the crew does not start until the checklist passes; stricter
existing settings are left alone (§9.6).

**Concurrency model.** Role crews run **concurrently, one active item per role**
(`concurrency.per_role`), including parallel with the Orchestrator agent. `dispatch_work_item`
starts the crew asynchronously and returns a run handle; completion surfaces through heartbeats
and the next cycle's `reap_finished_runs`. Workspace separation per role is defined in §8.1.
```

**What the agent is for.** Real projects deviate from the textbook workflow: conventions the
survey mapped only partially, human-filed untyped issues, human-opened PRs, odd linkage habits.
The Orchestrator agent (`roles/orchestrator.py`, prompt `roles/prompts/orchestrator.md`,
no memory, fresh context pack per cycle) supplies judgment where rules run out: it sequences
dispatches within the queue's priorities, triages `anomalies` (annotate, adopt via `apply_adoption`,
escalate, or consciously ignore), and interprets non-standard situations against the confirmed
conventions doc in its pack. It **never** decides what work exists — the queue defines that.

**Orchestrator agent tools** (all deterministic, all gated):

| Tool | Behavior |
|---|---|
| `get_project_state()` | Compact derived-state board for the current cycle |
| `compute_work_queue()` | The lawful work items, with priorities (§5.6) |
| `dispatch_work_item(item)` | **Kernel-gated:** refuses (structured reason) unless the item is in the current queue, the role's slot is free, the role is enabled (dashboard toggle), and budgets allow; on success builds the role's context pack and starts the role crew asynchronously (run handle returned; §6.1 concurrency) |
| `defer_work_item(item, reason)` | Hold an item until a later cycle (logged, visible in `codie status`). Each deferral posts `<!-- codie:defer <item_key> <n> -->` on the issue so the count survives restarts (C15); the kernel escalates automatically after `guardrails.max_defer_cycles` (default 3) consecutive deferrals of the same item |
| `apply_adoption(issue, type, status, note)` | Adopt an anomaly (e.g., a human-filed untyped issue) into the workflow: kernel validates type/status against the entry edges of the transition table, applies labels + one adoption comment, attributed to the kernel identity |
| `escalate_to_human(issue, summary)` | `flag:needs-human` + explanatory comment (also used for anomalies the agent cannot resolve) |
| `annotate(issue, comment)` | Audit-trail comments (e.g., anomaly triage notes) — used only when the note adds substantial information; never for bare acknowledgements (§7.6) |
| `get_conventions()` | The confirmed conventions + survey mappings (its guide for non-standard repos) |
| `get_budgets()` | Ledger state and caps |
| `get_run_history(issue=None)` | Past runs, costs, outcomes |

**Kernel invariants — enforced by the tools, never by the agent's restraint:**

1. `dispatch_work_item` only accepts items from the *current* computed queue (no invented work).
2. Budget caps, per-role concurrency, and wall-clock limits refuse or kill regardless of what
   the agent asks.
3. All label mutations pass the transition table (Product Spec §6); the agent holds no label
   tool — lifecycle writes happen via role results and reconciliation.
4. Cycle-cap escalation (§5.9), stale-work recovery (§5.8), and halt detection fire
   deterministically, with or without the agent.
5. The agent's own cycle is budget-metered like any role run; its token spend counts against
   the same ledger.
6. A role disabled via the dashboard (§6.5) gets a `role_disabled` refusal — the Orchestrator
   agent defers its items rather than retrying; the work waits, it is never dropped.

- `--once`: run one cycle, wait for the handles it started, then exit (for testing/cron).
- `--dry-run`: all GitHub mutations and merges are logged, not applied (the fake adapter in
  "record" mode over live reads; dispatch becomes a no-op that records intent).
- Graceful shutdown on SIGTERM/SIGINT or `codie stop`: the daemon waits up to
  `max_task_duration_minutes` for in-flight work, then marks the remaining handles orphaned and
  exits. `codie stop <url>` reads the daemon PID from `~/.codie/state/<owner>/<repo>.pid` (written
  on start, alongside the dashboard URL, while holding the workspace lock), sends SIGTERM, waits
  as above, and removes the file; a dead PID just removes the file (exit 0); an absent file exits
  1 with "not running".

### 6.2 Failure, cost, and budget handling

- **Retry layers:** transport-level LLM errors are retried by the litellm layer
  (`llm.max_retries`, default 4). A *failed work item* is not retried inline: it is re-dispatched
  on a later cycle, at most 3 attempts, before `flag:blocked` — there is no nested tenacity loop
  around an async handle. Retries never increment the cycle cap (§5.9). An intentional block
  returns `WorkResult(status="needs_human")` and is not retried.
- **Cost source:** the ledger prefers **provider-reported cost** (from the litellm response
  metadata); when the provider omits cost, a `llm.price_map` config entry (model → USD per
  1K input/output tokens) is used; a model with neither is a **fail-closed** error at dispatch.
- GitHub API errors: exponential backoff; after 5 consecutive failures, pause with a loud log.
- Budgets are enforced by the kernel, not the agent: `dispatch_work_item` refuses once
  `max_llm_cost_usd_per_day` (ledger, `ledger.day` is the UTC date) or the projected spend would
  exceed `max_llm_cost_usd_per_run`; per-item `max_task_duration_minutes` is a wall-clock kill on
  the crew run (→ orphaned). The agent sees refusals and can plan around them — never around the
  limits.

### 6.3 `codie status`

Prints the derived state: PRD eval checklist, features by status, task board (ready/in-progress/
in-review), open PRs and their review state, violations, budget consumption, and the next work
items the queue would dispatch in the §5.6 total order. While the crew is running it includes the
dashboard URL (§12). `--json` for scripting; `--runs` for run history; `--diff` prints the last
two `snapshots` rows, or one line that no previous snapshot exists (N21).

### 6.4 CLI surface

| Command | Purpose |
|---|---|
| `codie init <url> [--prd file] [--check] [--yes] [--pr]` | Survey-first onboarding (§9.6) using the admin token: an agent surveys the repo — conventions, issue triage, and the PRD — the user confirms, missing pieces are provisioned; registers the project locally. `--prd` supplies the PRD file (skip discovery); `--check` (alias `codie doctor`) is read-only; `--yes` accepts all proposals; `--pr` defers confirmation to the onboarding PR. |
| `codie start <url> [--once] [--dry-run]` | Start or resume the crew on a repo. The URL is the only required argument: the project is resolved from the local registry (auto-registered on first use), and project settings come from the repo's `.codie.yaml`. If no `type:prd` issue exists, exits with instructions (run `codie init`, or create the pinned issue). |
| `codie status <url> [--json] [--runs] [--diff]` | Derived state, next actions (queue total order), run history and costs, last-two-snapshot diff. |
| `codie doctor <url>` | Mechanical, read-only (no model): provisioning/permission/settings checklist + remediation. `codie init --check` is the same command. |
| `codie labels sync <url>` | Create or update the label taxonomy (idempotent). |
| `codie validate-config [<url>]` | Global file + `.codie.yaml` (from the integration head with a URL, else `./.codie.yaml`); redacts secrets; exits non-zero on a surface-matrix miss, a missing model price, or an unset required env var. |
| `codie stop <url>` | Graceful stop: SIGTERM the running daemon (PID from the state dir, C14/N23) and wait up to `max_task_duration_minutes` for in-flight work; remaining handles are marked orphaned. |

URL forms accepted everywhere: `https://github.com/<owner>/<repo>[.git]`,
`git@github.com:<owner>/<repo>.git`, or bare `<owner>/<repo>`.

### 6.5 Dashboard

An embedded FastAPI app (`dashboard/server.py`, uvicorn in-process) started by the kernel during
`codie start` and stopped on shutdown; skipped in `--once` mode. Serves a no-build-step SPA
(`dashboard/static/`) over a small JSON/SSE API. Localhost-only by default. When `host` is not
loopback, **every route (including SSE) requires `Authorization: Bearer <token>`** from
`dashboard.token_env`; a missing or wrong token is `401`. Loopback performs no token check.

| Endpoint | Purpose |
|---|---|
| `GET /` | The SPA: agent roster with toggles, log feed, summary header |
| `GET /api/summary` | Repo, uptime, poll interval, daily cost, halted/paused state |
| `GET /api/agents` | Per-role roster: enabled flag, state (`working`/`idle`), current work item (title, GitHub link, since, run_id) or last work item + outcome, today's cost |
| `POST /api/agents/{role}/enabled` | Toggle a role (`{enabled: bool}`) → writes `kernel_flags`; the dispatch gate enforces it (§6.1 invariant 6); in-flight runs finish gracefully. `role` ∈ planner/coder/reviewer/tester/orchestrator only — **`kernel` is 404**: reconciliation, derivation, and the queue always run (M27) |
| `GET /api/logs?agent=&level=&after_id=&limit=` | Filtered `agent_events`; `agent` ∈ planner/coder/reviewer/tester/orchestrator/kernel; `level` ∈ debug/info/warning/error; event `kind` ∈ dispatch/tool/decision/escalation/budget/refusal |
| `GET /api/stream` | SSE: new events and roster changes pushed live |

Data sources are all local: live dispatch state from the kernel, the `runs`/`agent_events`/
`ledger` tables, and — for "last worked on" after a cold start — GitHub timeline attribution
(§5.9). The dashboard's only write path is `kernel_flags`; it holds no `GitHubClient` and can
neither mutate GitHub nor weaken a guardrail. All events are secrets-redacted before persisting
(§11, §12).

## 7. Role Crews

### 7.1 Context packs (`context.py`)

Every dispatch builds a **context pack** — the complete information the role needs, nothing more:

| Role | Pack contents |
|---|---|
| Orchestrator | Compact state board; the lawful queue (prioritized); confirmed conventions + survey mappings; anomalies and violations; budget burn; recent refusals/deadlocks; last-cycle outcome |
| Planner | PRD body + eval list; full feature/task graph (compact table); human feedback (for re-plans and spec changes); repo conventions digest; for spec work, the repo architecture survey and the feature's draft/merged specs; transition rules |
| Coder | Task issue (body, "Done when", comments); parent feature's **approved specs** — both files **whole** (size bounds below): `technical-spec.md` (interfaces/design it must implement) and `feature-spec.md` (`FR-*`/`NFR-*`/`AC-*` requirements its code must satisfy); repo conventions (`AGENTS.md`, `CONTRIBUTING.md`, `.codie.yaml`); relevant file tree; review comments (when addressing feedback) |
| Reviewer | PR diff + changed-file contents; linked task "Done when"; parent feature's **approved technical spec** (code reviewed against its interfaces/design); conventions; CI status; prior review rounds |
| Tester | Parent feature's **approved feature spec** (its `FR-*`/`NFR-*`/`AC-*` are the acceptance contract); scope-dependent task/evals; `.codie.yaml` commands; recent merge list; test layout |

Conventions digest: the target repo's `AGENTS.md` — written or amended at init from the confirmed
survey (§9.6) — plus `CONTRIBUTING.md` are injected verbatim (truncated to a token budget);
`.codie.yaml` commands are injected as the *only* sanctioned build/test entry points.

**Size bounds.** `context.pack_token_budgets` maps each role — including `orchestrator` and
`surveyor` — to an integer token budget (default **24000** each). Both spec files are included
**whole**, read at the integration-branch SHA (`branches.dev`); if either approved spec does not
fit, the item fails to `flag:needs-human` and is not dispatched (never a partial spec). A "cited
path" is a repo-relative path in backticks. Remaining budget is filled in this order: task body →
review comments → cited file contents → conventions digest; each drop is appended to the omission
list as `{path, tokens_omitted}` (never silent truncation). The Coder's "relevant file tree" is
defined as: files cited in the task body, files cited by the feature's specs, and files touched by
the current diff — no broader.

### 7.2 Planner — propose / validate / apply

The Planner owns the whole-project view: PRD coverage, architecture integrity, quality standards,
and non-functional requirements (Product Spec §8.1). Its prompt requires it to state architecture
and NFR expectations explicitly in feature and task bodies, to raise dedicated architecture or
refactoring tasks when the codebase drifts, and to never descope a requirement to make a failing
check pass — only humans change requirements.

The Planner's GitHub write access is limited to **draft spec PRs** (branching + file writes to
`specs/<issue#>-<slug>/**` + open/update a **draft** PR via its workspace file and git tools); it
holds **no issue- or label-mutation tools, and no ready-for-review tool** — all workflow state
changes, and the ready-for-review flip, are applied by the kernel. Specs follow the conventions
in Product Spec §9 (paths, `FR-*`/`NFR-*`/`AC-*` IDs, depth bar).

**Kernel validation of spec files.** Before marking a spec PR ready-for-review, the kernel checks
both files against the Product §9.2 headings and the requirement-ID grammar (M20, §5.2). A
failing check leaves the PR in draft and re-queues `DraftSpecs` (or `ReviseSpecs`) with the
validator error appended; **each such re-queue counts as a failed attempt toward the §6.2 cap**
(3 attempts, then `flag:blocked`) — a Planner that cannot produce a valid spec must not loop
unbounded. The kernel — never the Planner — marks the PR ready-for-review, which
is the transition that moves the feature `speccing → spec-review`.

Spec-authoring contract:

1. `DraftSpecs(feature)`: read the PRD + architecture survey; write
   `specs/<issue#>-<slug>/feature-spec.md` and `…/technical-spec.md` on branch
   `spec/<issue#>-<slug>` (slug grammar: Product Spec §9.1); open **one** PR to the integration
   branch **as a draft**, with a summary of requirement coverage and open questions; returns
   `SpecDraftResult{ pr_number, files, open_questions }`. The kernel validates the files and
   marks the PR ready-for-review (see above). An existing open draft or spec branch is **resumed**,
   not recreated.
2. `ReviseSpecs(feature, pr)`: apply the Reviewer's PR review comments (and any human comments);
   push to the same spec branch; iterate until approved or the kernel's cycle cap escalates (§5.9).
3. Mid-implementation spec changes (revision or discovered constraints): new spec PR **before**
   the dependent tasks are re-planned — code never diverges from a merged spec silently.
4. `AssessRevision(feature)`: return `RevisionAssessment{ kind: requirements|implementation,
   rationale }`. The kernel posts a `<!-- codie:revision requirements|implementation -->` marker
   and leaves the feature at `revised`; the next cycle routes on that marker: `requirements`
   cancels every non-terminal child task and closes their PRs, then sets `speccing` (rule 4
   re-specs); `implementation` queues `Replan`, whose `ReplanResult` payload the kernel validates
   and applies before setting `planned` (§5.5, §5.6 rule 3).
5. `ReconcilePlan`: return `PlanReconciliation{ add: list[FeatureIssueDraft],
   cancel_feature_numbers: list[int], restale_feature_numbers: list[int] }`; the kernel creates
   and cancels from that payload, and flags each restale feature `flag:needs-human` with a comment
   (never moves it backward).
6. `ProposeRelease`: return `ReleasePlan{ version, changelog, eval_report }`; the **kernel**
   opens the release PR (the Planner token is scoped to `specs/<issue#>-<slug>/**` draft PRs, so
   the kernel performs this write with the kernel identity, §9.2).

Breakdown and re-plan output remains pydantic-validated structured data:

```python
class FeaturePlan(BaseModel):           # decomposition result
    features: list[FeatureIssueDraft]   # title, body_markdown, evals: list[str], priority
class TaskSpec(BaseModel):
    ref: str                            # temp id ("t1") unique within one breakdown
    title: str; kind: Literal["dev","integration","test"]
    depends_on: list[int | str]         # int = existing issue; str = temp ref in same breakdown
    body_markdown: str                  # "Done when" items carry FR-*/NFR-* IDs
class TaskBreakdown(BaseModel):         # per feature (only valid once status:specified)
    tasks: list[TaskSpec]
class RevisionAssessment(BaseModel):    # AssessRevision result
    kind: Literal["requirements", "implementation"]
    rationale: str
class ReplanResult(BaseModel):          # Replan payload (implementation revision)
    new_tasks: list[TaskSpec]
    cancel_task_numbers: list[int]
    rationale: str
class PlanReconciliation(BaseModel):    # ReconcilePlan payload (PRD changed)
    add: list[FeatureIssueDraft]
    cancel_feature_numbers: list[int]
    restale_feature_numbers: list[int]  # get flag:needs-human; never moved backward
class ReleasePlan(BaseModel):           # ProposeRelease result
    version: str; changelog: str; eval_report: str
class BugDraft(BaseModel):              # Tester bug filing
    title: str; priority: Literal["high","medium","low"]
    parent: int | None                  # optional; parentless bugs name the offending PR in body
    depends_on: list[int] = []          # non-empty → enters status:backlog
    body_markdown: str                  # Reproduction / Expected vs actual / requirement IDs
class ReviewDecision(BaseModel):        # Reviewer verdict (M21 — no "escalate": §7.4/§5.9 own
    verdict: Literal["approve", "request_changes"]  #  escalation; the kernel counts cycles)
    comments: list[ReviewComment]       # each {path, line, side: LEFT|RIGHT, body}
    requirement_ids: list[str]
```

Supporting types (M14): `Finding{ value: str, evidence: list[str], confidence: low|medium|high,
source: adopted|default }`; `IssueTriage{ number: int, proposed_type: prd|feature|task|bug|untyped,
proposed_status: <legal entry status> | "", rationale: str, confidence: low|medium|high }`;
`PrdProposal{ source: adopted_doc|synthesized|supplied|none, content: str, evidence: list[str] }`;
`FeatureIssueDraft{ title: str, body_markdown: str, evals: list[str], priority: high|medium|low }`.

**Queue total order (M14).** `compute_queue` returns a total order of
`WorkItem{ kind, role, entity, issue_number, pr_number, priority }`: rule number first; then —
when `workflow.bugs_outrank_features` is true — bugs before other work; then
`high > medium > low` (a missing `priority:*` sorts as `medium`); then lowest issue number. The
Orchestrator may dispatch any item surviving the §5.5 flag filter; `codie status` prints this
exact order.

**Body/render consistency (M14).** The kernel renders `Parent:`, `Depends on:`, `Evals:`, and the
`## Done when` checklist **from the structured fields** (`TaskSpec.depends_on`, `.kind`,
`FeatureIssueDraft.evals`, …) and rejects a `body_markdown` that contradicts them — the structured
fields are authoritative, never the free text.

The orchestrator validates against current state (eval IDs exist; temp refs in `depends_on` resolve
within the same breakdown; integer refs resolve to existing issues; no cycles; **the feature is
`status:specified`**; and every cited requirement ID exists in the merged specs **and is not
struck through** — struck-through IDs are history and fail validation on new citations) and applies
mutations transactionally per feature: create issues, set labels, write bodies, post a summary
comment, flip statuses per the transition table (§5.5). Validation failure → one retry with the
error appended; then `flag:needs-human` on the feature.

### 7.3 Coder — tool-acting implementer

Tools: file read/write/search (workspace-jailed, **excluding `specs/**`, `acceptance.dir`, and the
policy files `.codie.yaml`/`AGENTS.md`/`CODEOWNERS`** — §7.4 step 6),
`run_command` (jailed, §8.2), git branch/commit/push via shell, GitHub tools limited to: claim
issue, label transitions from its matrix row (§5.5), `flag:blocked` set/clear on its active item,
comment, open/update PR. All branch names come
from config: `branches.dev`, `branches.feature_pattern` (default `feature/{issue}-{slug}`, slug
grammar in Product Spec §9.1). Behavior contract (in `roles/prompts/coder.md`):

1. **Resume first:** if the task already has a branch or an open linked PR (e.g., after a
   stale-run reset), fetch and resume it — never start a parallel branch. Otherwise branch off the
   latest integration branch (`git fetch`; branch from `origin/<branches.dev>`).
2. Implement with task-level craft: SOLID, DRY, clear naming, small diffs scoped to "Done when",
   and **testability** — unit/integration tests accompany the change; code it cannot test is
   unfinished. Black-box acceptance tests are the Tester's domain and are out of scope. Run
   `setup`/`test_fast` locally before pushing.
3. Commit in the repo's confirmed style from the conventions doc (default: conventional commits
   referencing the issue, `feat: … (#123)`); push; open PR **to the integration branch** with
   `Refs #<n>` and a summary of how each "Done when" item is satisfied. **No PR uses a closing
   keyword** (`Closes`/`Fixes`/`Resolves`): issues are closed by the kernel when they reach a
   terminal status (M30), so a merged bug fix leaves the bug open in `verifying` rather than
   auto-closing it.
4. When dispatched as `AddressReview`: fetch review comments, apply changes, push to the same
   branch. Reply where it adds something substantial (a rebuttal, a clarification, a non-obvious
   fix); per-comment acknowledgements are noise — whether to reply is the Coder's judgment
   (§7.6).
5. Keep the PR current by **merging the integration branch into the feature branch**; never rebase
   or force-push (the denylist forbids it).
6. Blocked (ambiguous spec, failing external dep, scope conflict) → comment + `flag:blocked`,
   exit cleanly. The Coder may later clear a `flag:blocked` it set, after the condition is gone.

### 7.4 Reviewer — gatekeeper of the integration branch

The Reviewer judges PRs on evidence alone — correctness against "Done when", security, scope,
conventions — and never defers to the Planner's plan or the Coder's claims (Product Spec §8.3,
§8.5).

The Reviewer works in a **worktree**, never the main checkout (§8.1), and pushes nothing.

Tools: GitHub PR read/review/merge, label transitions from its matrix row (§5.5), file read
(workspace), `run_command` for read-only checks (checkout PR in a detached worktree; run
`lint`/`test_fast`). Branch and merge conventions come from config: `branches.dev`,
`branches.feature_pattern`, `merge_method` (§4.3). Contract:

1. Verify base branch is the configured integration branch and the head branch matches
   `branches.feature_pattern`, `branches.test_pattern` (`kind:test` PRs), or
   `spec/<issue#>-<slug>` (spec PRs).
2. Review the diff against "Done when" and the feature's approved technical spec: correctness,
   scope, security, conventions, test expectations.
3. CI: green means `list_required_checks(head)` is empty or every entry is `success`; a pending
   entry blocks; a non-required failing check is ignored (M19).
4. Task PRs (`ReviewPR`/`MergePR`): approve → merge via the configured `merge_method` → the kernel
   labels the linked task `status:done` and closes it (M30; a bug's PR instead moves the bug to
   `status:verifying` for the Tester). Request changes → `create_review(pr, "request_changes",
   body, comments)` with per-line comments → task back to `status:in-progress`. The review is
   written **only** via `create_review`; the returned `ReviewDecision` (§7.2) is the record of that
   call — the kernel verifies the GitHub review matches it and does not submit a second one.
5. Spec PRs (`ReviewSpecPR`/`MergeSpecPR`): review **as the product owner** — does the
   specification align closely with the PRD: faithful coverage of the cited PRD sections, nothing
   invented beyond them, acceptance criteria that genuinely demonstrate each requirement, evals
   correctly mapped — plus spec quality: internal consistency, conventions (Product Spec §9),
   requirement-ID hygiene. The Reviewer **approves and merges on its own verdict**:
   `MergeSpecPR` requires the reviewer bot's `APPROVED` review on the current head; then merge
   and the kernel flips the feature to `specified`.
6. **Policy files.** `.codie.yaml`, `AGENTS.md`, and `CODEOWNERS` are policy files (same class as
   `specs/**`): the Reviewer's merge tool refuses any PR whose diff touches them unless a non-bot
   `APPROVED` is present on the current head (this is the phase-2 command-execution gate,
   §9.6/C20). A spec PR is not a policy file — it merges on the reviewer bot's `APPROVED` plus CI
   green (step 5). A role-opened PR that touches a policy file gets `flag:needs-human` and is not
   merged.
7. The Reviewer never counts cycles or escalates — the kernel owns the cycle cap (§5.9).

### 7.5 Tester — the user's advocate

The Tester verifies requirements-fitness **from the user's perspective, independently of the
Coder** (Product Spec §§7.5, 8.4): it designs acceptance tests from the requirements and
acceptance criteria — never from the diff — and never counts the Coder's unit/integration tests
as acceptance evidence. It owns the black-box acceptance suite (`acceptance.dir`), driving the
product through its real user-facing surface via `acceptance.framework`: Playwright for web UIs,
Maestro/XCUITest/Espresso for mobile, pexpect/tmux for TUIs, real subprocess invocations for
CLIs, HTTP clients for APIs, engine harnesses / input injection / screenshot checks for games.
The product under test is launched as a managed session via `commands.run_app` (§8.2). Unit or
integration test results never substitute for a surface-level pass. The Tester works in a
**worktree**, never the main checkout (§8.1), and its acceptance-suite PRs are pushed under the
**tester** identity.

**Surface → driver matrix** (closed config — `acceptance.framework` is one of the IDs below; any
other value, and any pair off this matrix, is a config error caught by `validate-config`, M17):

| `surface` | Supported `acceptance.framework` | Notes |
|---|---|---|
| `ui-web` | `playwright` | headed/headless browser against `run_app` |
| `ui-mobile` | `maestro`, `xcuitest`, `espresso` | requires the `commands.simulator_boot` / `simulator_install` / `simulator_shutdown` argv lists; the Tester drives that lifecycle inside its work-item session |
| `tui` | `pexpect`, `tmux` | terminal-level interaction |
| `cli` | `subprocess` | real invocations of the built binary/entry point |
| `api` | `httpx` | against `run_app` on its declared ports |
| `game` | `engine-harness` | engine harness + input injection + screenshot checks; baselines below |
| `library` | `examples` | acceptance = compiling/running the example programs in `acceptance.dir` against the built package; **does not call `run_app`** |
| `ui-desktop` | — (reserved) | **unsupported in v1** (Product Spec §2.2) |

A `library` acceptance run skips `run_app` and `commands.test_acceptance` (it executes the
examples directly); every other surface launches `run_app` and runs `commands.test_acceptance`.
Screenshot comparisons (game/mobile) check against committed baselines under
`acceptance.dir/baselines/` with a per-image pixel mismatch no greater than
`acceptance.screenshot_tolerance` (default 0.01); new baselines land only via the acceptance-suite
PR (human-visible).

**Artifacts and baselines.** Test artifacts (recordings, screenshots, terminal logs) are written
under `~/.codie/state/<owner>/<repo>/runs/<run_id>/artifacts/` and linked from bug reports; image
comparisons for games/mobile are checked against committed baselines under
`acceptance.dir/baselines/` (new baselines land via the acceptance-suite PR, human-visible).

Tools: `run_command` (full access to `commands.*` and `evals.*`, including session-managed
`run_app`), file read/write (**`acceptance.dir` and bug-repro artifacts only**), GitHub issue
tools (file bugs from `BugDraft`, comment, PRD checklist check-off), label transitions from its
matrix row (§5.5), PR tools (open PRs for acceptance-test additions; read-only otherwise).
Contracts:

- **VerifyTask (`kind:test`):** design and automate black-box acceptance coverage for the task's
  criteria against the running product; commit the suite to `acceptance.dir` via a normal PR
  (reviewed by the Reviewer like any code). Failures → file bugs.
- **Bug filing (`BugDraft`, §7.2):** severity → `priority:high|medium|low`
  (high = feature-blocking/data loss; medium = requirement fails with workaround; low = cosmetic).
  `Parent:` optional (must resolve to a feature when present); a parentless regression names the
  offending PR in the body. New bugs enter `status:ready` unless `depends_on` is non-empty
  (`status:backlog`). Always with user-level reproduction.
- **RegressionRun:** after merges to the integration branch: update the worktree, run `test_fast`
  (every merge) / `test_full` + `test_acceptance` (cadence); failures → bugs linked to the
  offending PR. The Tester records each suite result as a PRD comment,
  `<!-- codie:suite fast <sha> pass|fail -->` / `<!-- codie:suite full <sha> pass|fail -->`, where
  `<sha>` is the integration head it ran against. Queue rule 12 matches when the newest fast marker
  is missing or names a different SHA; a **full** run is due when the newest full marker is stale
  and either `workflow.full_test_cadence_minutes` has elapsed since that marker's comment time or a
  feature would otherwise match rule 11. On a cold start with no marker, one fast run is queued.
- **AcceptanceRun:** clean worktree at the integration head; `setup` + `build` + launch via
  `run_app` + verify **every acceptance criterion through the user surface**. Rule 11 requires a
  passing full-suite marker for the current integration SHA, so acceptance never runs on an
  unverified tree. Success → UAT summary comment + feature → `status:review`; failure → bugs +
  feature stays `in-progress`, and the kernel records it with
  `<!-- codie:cycle acceptance-failed <run_id> -->` (counted toward the cycle cap, §5.9).
- **ReverifyBug:** for a bug in `status:verifying`, re-run the same user-level reproduction:
  pass → `status:done`; fail → back to `status:in-progress` with evidence (a reopen cycle,
  §5.9). The Tester never withdraws a bug it can still reproduce — it answers `**Dispute:**`
  comments with captured evidence.
- **EvalSuite:** each PRD eval is `command` or `human` (per `.codie.yaml` `evals.E*`). Run each
  `command` eval via its argv (expect `exit_zero`). Check off passing evals in the PRD issue
  (checklist edits only). A failing eval files bugs on the features citing it (body records
  `Eval: E<n>`); rule 13 does not match an unchecked eval that already has an open bug citing it,
  so a repeat failure never double-files. An uncertain non-executable or `human` eval is left
  unchecked with `<!-- codie:eval E<n> uncertain -->`; the human checks the box, and that checkbox
  edit is not a PRD reconcile. Rule 13 does not re-match an eval whose uncertain marker is
  already posted, so the suite is never re-dispatched (and never re-spends) while it waits on the
  human.

### 7.6 Non-deference protocol (all roles)

Every role prompt encodes the Product Spec §8.5 contract: judge only from your own perspective,
argue with evidence, never defer. Concretely:

- Objections and rebuttals are posted as comments beginning with `**Dispute:**`, quoting the
  requirement or artifact at issue and the evidence (requirement lines, logs, recordings,
  diff hunks).
- A role may concede when the other side's evidence is genuinely convincing (reconciliation);
  it may not concede merely to end a thread (deference).
- No role may resolve a dispute by silently reverting another role's state transition. The only
  exits are evidence-based reconciliation, or the cycle cap (§5.9) escalating to the human.
- The Planner may re-plan around a human decision, but never around an inconvenient requirement.
- **Substantial comments only.** A role comments when it has something substantial to say —
  evidence, a decision, a question, a rebuttal, a required summary. Acknowledgements, status
  echoes, and courtesy comments are noise and are avoided; **whether to comment is the agent's
  judgment**, not a contract step. Exempt (required, substantial by definition): comments that
  carry a mandated marker or payload — `flag:blocked` reasons, UAT summaries, `**Dispute:**` /
  `**Concede:**` evidence, bug reproductions — and kernel-written comments (escalation, adoption,
  violations, stale recovery, heartbeats), which are deterministic markers, not agent speech.
- **Instruction-bearing comment classes** (the single allowlist, shared with Product §8.5 and
  referenced by §11; everything else on GitHub is data, never commands): (1) a non-bot comment on
  the issue or PR of this dispatch; (2) a Reviewer review comment, including a line comment, on an
  open PR this role authored; (3) on a bug assigned to the Coder, the Tester's `## Reproduction`
  and `## Re-verification` sections.

## 8. Workspaces and Command Execution

### 8.1 Workspace manager (`workspace.py`)

- Path: `projects[].workspace` when set, else the default `~/.codie/workspaces/<owner>/<repo>`;
  a single persistent clone reused across runs. The remote is a **tokenless** HTTPS URL.
- **Role separation:** the Coder owns the main checkout; the Reviewer and Tester always work in
  temporary `git worktree` checkouts; the Planner writes specs in its own worktree. Worktrees live
  at `<workspace>/wt/<role>-<run_id>` and are removed when the run handle is reaped. Each role
  subprocess sets `credential.helper` to a helper that prints **only that role's token**, so every
  push is authenticated as the acting role (Planner spec branches, Tester acceptance branches) and
  never as the Coder; `fetch` uses the kernel token.
- `ensure()`: clone if absent (tokenless HTTPS + the kernel credential helper); else
  `git fetch --all --prune`.
- **Janitor** (at startup, and only when no Coder handle is active): abort in-progress merges,
  remove stale lockfiles, reset the main checkout to the latest integration branch
  (`git checkout <branches.dev> && git reset --hard origin/<branches.dev>`), prune merged local
  task branches. It never resets or deletes a branch checked out by a live worktree. Uncommitted
  unexpected changes are stashed to `codie-stash-<ts>` and logged, never silently discarded.
- File locking (`~/.codie/workspaces/<owner>/<repo>.lock`, `fcntl`) prevents two codie instances
  from sharing a workspace.
- Feature work happens in the main checkout (Coder only); acceptance runs and reviews use
  temporary clean worktrees (`git worktree add`) to avoid interfering with in-flight branches.

### 8.2 Command runner (`runner.py`)

- `commands.*` and `evals.*` entries are **argv lists executed without a shell**
  (`subprocess.run(argv, shell=False)`); the `shell_denylist` is a set of globs matched against
  the argv joined by single spaces (`fnmatch`). `cwd` is jailed inside the workspace
  (resolved-path check). Every command receives the environment allowlist `PATH`, `HOME`, `LANG`,
  `LC_ALL`, `TMPDIR`, plus the toolchain variables `CC`, `CXX`, `CFLAGS`, `LDFLAGS`, `JAVA_HOME`,
  `GOROOT`, `GOPATH`, `CARGO_HOME`, `RUSTUP_HOME`, `VIRTUAL_ENV`; `run_app_session.env` is added
  only to the `run_app` process group. Output is captured, truncated to `runner.max_output_chars`
  (default 32000). The wall-clock timeout is `runner.command_timeout_seconds` (default 600).
- **Policy-file trust:** `commands.*` and `evals.*` are executed only from the `.codie.yaml` blob
  that last landed through the policy-file gate (§7.4 step 6) or via `codie init` — a later task
  PR that rewrites them is refused by the Reviewer merge tool (C20).
- **Session mode:** `commands.run_app` launches the product as a managed background session (own
  process group, output captured, killed at work-item end) so the Tester can drive the live
  UI/TUI/CLI during `VerifyTask` and `AcceptanceRun`. The runner waits until every
  `run_app_session.ports` entry accepts TCP or the command timeout elapses (a timeout fails the
  work item; pre-existing listeners are left alone — the runner only waits for accept).
  `display: headless` sets `CODIE_DISPLAY=headless` and starts no display server; `display: xvfb`
  starts `Xvfb :99`, sets `DISPLAY=:99`, stops it at work-item end (a missing `Xvfb` fails the
  run); `display: native` leaves `DISPLAY` unchanged (fails if `DISPLAY` is unset).
- Free-form shell from agents: allowed for Coder/Tester with the configured `shell_denylist`
  applied (defense-in-depth, not a security boundary — see §11). Planner and Reviewer
  `run_command` is read-only: it accepts only `commands.lint`, `commands.test_fast`, and `git`
  with subcommand `fetch`, `diff`, `log`, `show`, or `status`.

## 9. GitHub Integration

### 9.1 Client architecture

`GitHubClient` is a **protocol** (port). The **role protocol** (used by the orchestrator and role
crews): `list_issues(since)` (returns open **and** closed issues), `get_issue`, `create_issue`,
`edit_issue`, `set_labels`, `set_assignees`, `comment`, `list_comments(since)`, `list_timeline`,
`list_prs`, `get_pr`, `pr_reviews`, `list_pr_files`, `list_review_comments`, `create_pr`,
`update_pr` (incl. the `draft` flip), `close_pr`, `create_review`, `merge_pr`, `reopen_issue`,
`close_issue`, `get_file`, `get_ref`, `list_required_checks(ref) -> [{name, state}]` with `state ∈
success|pending|failure` (**CI is green when this list is empty or every entry is `success`; a
non-required failing check is ignored** — N6), `set_commit_status`, `upsert_label`, `rate_limit`.
An **admin protocol**, constructed only inside `codie init` and `codie doctor`, adds `pin_issue`,
`create_ref`, `upsert_file`, `get_branch_protection`, `update_branch_protection`, `update_repo`,
`invite_collaborator`, and `get_authenticated_user`. Two implementations: `PyGithubClient`
(production) and `FakeGitHubClient` (in-memory, used by tests and `--dry-run`, which uses the fake
for writes and the real client for reads). The kernel serializes client calls.

### 9.2 Per-agent tokens

Every call is attributed: the client routes each operation through the token of the agent
performing it — planner/coder/reviewer/tester, plus **kernel** for kernel writes (reconciliation
labels, escalation and deadlock comments, heartbeats, issue adoption, the release PR; C5/M5).
Required scope: fine-grained PAT or GitHub App token with **Contents: RW**, **Issues: RW**,
**Pull requests: RW** on the target repo (Tester may be RO on contents except PRD-checklist edits
— start with RW for simplicity). Token env var names come from `github.tokens.*.env`.

Benefits: native audit trail (comments, reviews, merges, issue assignment, and **kernel actions**
all carry their own bot identity), per-role rate-limit buckets, and GitHub-side enforcement when
combined with:

### 9.3 Recommended repo settings

- Branch protection on `dev` (the integration branch): require PR, **1 approval**, dismiss stale
  approvals, require status checks (CI) if present; restrict who can push (deny direct pushes for
  all roles). Code-owner review is **not** required here (N19) — otherwise a `CODEOWNERS *`
  @<admin>` rule would block the Reviewer from merging task PRs.
- Branch protection on `main` (the stable branch): require PR from `dev`, **human-only** approval
  (enforced via CODEOWNERS requiring the admin's review — here, "require review from code owners"
  is on), require status checks; no role account in the allowed-merge set.
- These settings are applied by `codie init` where absent (§9.6 — existing stricter settings
  win); `codie doctor` verifies them and prints a remediation guide.

### 9.4 Label sync

`codie labels sync <url>` (any of the three URL forms, §6.4) creates/updates the full taxonomy
idempotently from `github/labels.py` — names from the Product §6 taxonomy, with the fixed colors
and descriptions of the §9.6 defaults table (M31). Orchestrator runs it at startup.

### 9.5 Rate limits and polling hygiene

Delta fetches via `since`; full fetch every 10 cycles; ETags where PyGithub exposes them; honor
`Retry-After`; per-role buckets isolate a runaway role. Default 60 s poll + jitter. Idle backoff
to 15 minutes with ±10% jitter applies **only when the queue is empty and nothing is running**.
After any mutation the loop skips the sleep and re-polls immediately (so state settles before the
next decision).

### 9.6 Onboarding: survey, confirm, provision (`codie init`)

`codie init <url>` onboards a repository so the crew can operate it **by its own conventions**,
reserving codie's opinionated defaults (the table at the end of this section) for dimensions
where the repo has no precedent (new or unopinionated repos). It authenticates **exclusively**
with the admin token (`github.admin.env`, e.g. `CODIE_GH_TOKEN_ADMIN` — a fine-grained PAT with
Administration + Contents + Issues + **Pull requests** on the repo, or an equivalent GitHub App
token). The admin token is never read by the orchestrator or any role.

#### Phase 1 — Exploratory survey (agent)

A dedicated **Surveyor** agent (`roles/surveyor.py`, prompt `roles/prompts/surveyor.md`;
init-time only, read-only tools: repo file tree/content, `git log`/history via the runner,
GitHub metadata over the admin token) answers the questions that make the repo legible to the
crew. It returns a pydantic-validated `RepoSurvey` (propose → validate → apply, §7.2 pattern):

| Dimension | Examines | What it infers |
|---|---|---|
| Branching | refs, merge history, PR bases | stable/integration branch names (→ `branches.main`/`dev`), feature-branch pattern (→ `branches.feature_pattern`), merge style (→ `merge_method`), release flow (tags, changelogs) |
| Issues | existing labels, issue templates, **all open issues** (bounded, newest `survey.max_open_issues`, default 200, by `updated_at`) | label purposes + mapping to codie's taxonomy, triage habits, and a **per-issue adoption proposal**: `type:*`/`status:*` with rationale + confidence (see triage below) |
| **PRD** | `PRD.md`, `docs/requirements*`, README goals/roadmap sections, roadmap/design issues and docs, `docs/` index | the product-requirements source: **adopt** an existing doc (extract or draft its evals checklist), **synthesize** a draft PRD from docs + backlog for human edit, or **await** human supply (see PRD sourcing below) |
| PRs | PR template, merged-PR sample | review norms, required checks, description expectations |
| Commits | `git log` sample | message style (conventional commits or other), sign-off rules |
| Build/run/test | manifests, CI workflows, Make/just/taskfiles | setup/build/run_app/test commands, toolchains, test layout + frameworks |
| Surface | stack + entry points | `surface` value + suggested `acceptance.framework` |
| Style | linter/formatter configs, `.editorconfig` | lint/format commands, style rules |
| Docs | README, CONTRIBUTING, `AGENTS.md`, `docs/` | existing conventions digest — a repo `AGENTS.md` is a **primary source** and is amended, never overwritten |

Every finding carries **evidence** (file paths, history excerpts, sample links) and a
confidence level. The survey executes **no repository code** — static analysis, git history,
and GitHub metadata only (§11).

**Issue triage.** Pre-existing issues are part of the survey: the Surveyor classifies each open
issue (bounded enumeration — at most `survey.max_open_issues`, default 200, newest by
`updated_at`; older open issues are left untouched and their count is reported, M24) and proposes
its adoption into the workflow. It never proposes closing or rewriting anyone's issue.

```python
class RepoSurvey(BaseModel):
    dimensions: dict[str, Finding]   # value, evidence, confidence, source: adopted | default
    issue_triage: list[IssueTriage]  # number, proposed_type, proposed_status, rationale, confidence
    prd: PrdProposal                 # source: adopted_doc | synthesized | supplied | none
                                     # content: markdown (with evals checklist), evidence
```

Issues that don't fit the workflow (questions, discussions, announcements) are proposed as
*untyped* and stay out of crew scope. Low-confidence classifications default to untyped —
adopting an issue wrongly is worse than leaving it for the human. Issues older than the
`survey.max_open_issues`-newest bound are left untouched (a follow-up triage pass can be requested
via `flag:needs-human`).

**PRD sourcing.** The PRD is discovered and confirmed at init like every other convention —
`codie start` never takes it:

- **adopted_doc** — a requirements document found in the repo (evidence-linked); the Surveyor
  extracts or drafts its evals checklist (`E1..En`). With `--prd <file>`, the file is treated
  as a supplied adopted document (skips the search).
- **synthesized** — no single doc found; the Surveyor drafts a PRD from README/goals/roadmap +
  backlog. Always presented for human edit before adoption (product intent is never invented —
  Product Spec §2.2).
- **supplied / none** — `--prd` given without a repo doc, or nothing found and the user
  declines synthesis: init completes and provisioning proceeds, but the `type:prd` issue is
  not created and `codie start` refuses until one exists (queue rule 1).

The confirmed PRD (whichever source) is posted as the pinned `type:prd` issue during phase 3
(step 8).

#### Phase 2 — Confirm with the user

The CLI renders a per-dimension proposal, each tagged **adopted** (repo precedent, with
evidence) or **default** (no precedent → codie's opinionated choice). The user confirms or
edits each dimension interactively; `--yes` accepts all proposals; `--pr` skips interactivity
and defers confirmation to review of the onboarding PR. The issue-triage proposal is reviewed
in the same pass: bulk-accept with per-issue overrides. Confirmation is a **security
boundary**: it authorizes the exact commands that will be written to `.codie.yaml` and later
executed with the user's privileges (§8.2).

#### Phase 3 — Provision (mechanical, convention-aware)

Idempotent; applies only what is missing, per the confirmed conventions. **Ordering matters:**
branches and the initial commit come before protection, so protection can never block the commit
that creates the default branch.

1. **Register** the project in the local registry with derived defaults.
2. **Branches and seed commit:** honor the confirmed model — map `branches.main`/`dev` to the
   repo's actual names (e.g. `master`/`develop`); create branches that are missing; only repos
   with no branching precedent get the opinionated `main`/`dev` split with `dev` as default. On
   an empty repo, the seed commit is a **README only**, made on both configured branches **before**
   any protection exists — the onboarding files (step 7) then arrive via the onboarding PR, so
   they are never executable before confirmation.
3. **Labels:** add the codie taxonomy (§9.4) — strictly additive; existing labels are preserved,
   and the survey's label mapping is recorded in `.codie.yaml` (`label_mapping`) and the
   conventions doc.
4. **Issue adoption:** for each confirmed triage entry, set the proposed `type:*`/`status:*`
   labels and post one adoption comment ("adopted by codie as `type:bug` / `status:ready`").
   Pre-existing issue bodies are never edited; nothing pre-existing is ever closed.
5. **Repo options + protection:** align merge methods to the confirmed `merge_method`;
   auto-delete head branches; apply §9.3 protection **where absent** — existing stricter
   settings are kept, and gaps are reported rather than silently changed.
6. **Collaborators:** ensure the five crew accounts (planner, coder, reviewer, tester, kernel)
   have `push` access (pending invites are reported for the human to accept).
7. **Onboarding PR** (`chore: codie onboarding`, branch `codie/onboarding`, **base
   `branches.dev`** — the integration branch the daemon reads `.codie.yaml` from, §5.1):
   - `.codie.yaml` — machine-readable operations: confirmed branch names/pattern, merge method,
     `label_mapping`, commands (argv form), `surface`, `acceptance`, eval stubs;
   - `AGENTS.md` — the conventions taught to the crew: amended with a "Codie workflow" section
     when the repo already has one, otherwise written fresh from the confirmed conventions;
   - `CODEOWNERS` — `* @<admin-user>` (only where §9.3 stable-branch protection requires it and
     none exists; N19 — code-owner review is required only on the stable branch, never on the
     integration branch, so the Reviewer can merge task PRs);
   - `specs/_survey/architecture.md` — the durable architecture survey (M29): built from the
     Surveyor's `RepoSurvey` (build/run/test, surface, module-layout evidence), read by the
     Planner pack from the integration-branch SHA; a later init that opens a survey PR may amend
     it. **Role crews never write this path** (Planner spec writes are `specs/<issue#>-<slug>/**`
     only).
   `.codie.yaml` and `AGENTS.md` reach the integration branch **only when this PR merges** — for
   interactive init and for `--pr` alike; that merge is the confirmation.
8. **PRD issue:** if the survey confirmed a PRD (adopted doc, edited synthesis, or `--prd`
   file), post it as the pinned `type:prd` issue with the evals checklist. If the PRD lives as
   a repo file, the issue links it and notes the issue is canonical for status.
9. **Report:** everything applied, adopted, or pending human action — including a prominent
   "no PRD yet; `codie start` will refuse until one exists" notice when step 8 was skipped.

**Startup reads config from the integration branch.** Before the provisioning checklist, and on
every cycle, the kernel fetches `.codie.yaml` through the API from `origin/<branches.dev>`.
`codie start` **requires** that file on `origin/<branches.dev>`; that merge is the confirmation
referenced in Product §14 criterion 8. Pending collaborator invites fail the checklist (step 6).

**Repeat init.** A second `codie init` whose provisioning checklist **fails** repairs only the
missing checklist items and opens no survey PR. One whose checklist **passes** re-surveys and
opens a PR with the proposed `.codie.yaml`/`AGENTS.md` diff — it changes **no GitHub settings**
until that PR merges. When init ran with `--pr`, confirmation *is* the onboarding-PR merge:
commands in `.codie.yaml` become executable only after that merge, and `codie start` refuses
until it has happened.

`codie doctor <url>` is **mechanical and read-only** (no model call): the §6.1 checklist, token
scopes, invite acceptance, and `.codie.yaml` presence on the integration head. `codie init
--check` is that same doctor. `codie validate-config [<url>]` loads the global file plus
`.codie.yaml` (from the integration head when a URL is given, else `./.codie.yaml`), redacts
secrets, and exits non-zero on a surface-matrix miss, a missing model price, or an unset required
env var (M17/M26). `codie start` runs that project validation before the provisioning checklist.

**Opinionated defaults (used only when a finding's `source` is `default`, M31).** Two
implementations of init must onboard identically, so these are fixed:

| Key | Default |
|---|---|
| `branches.main` | `main` |
| `branches.dev` | `dev` (empty repo's default branch) |
| `branches.feature_pattern` | `feature/{issue}-{slug}` |
| `branches.test_pattern` | `test/{issue}-{slug}` (C12) |
| `merge_method` | `squash` |
| commit style | conventional commits with `(#<issue>)` |
| `surface` | `cli` |
| `acceptance.framework` | `subprocess` |
| `acceptance.dir` | `tests/acceptance` |
| `workflow.bugs_outrank_features` | `true` |
| `workflow.full_test_cadence_minutes` | `60` |
| `guardrails.max_cycles_per_work_item` | `3` |

`commands.*` has no defaults — the confirm UI requires the human to set `setup` and `test_fast`
(C18). Label names are the Product §6 taxonomy; colors: `type:*` `#0E8A16`, `status:*` `#1D76DB`,
`kind:*` `#FBCA04`, `priority:high` `#B60205`, `priority:medium` `#D93F0B`, `priority:low`
`#FEF2C0`, `flag:*` `#5319E7`; each label's description is its Product §6 table cell.

## 10. LLM Configuration

- Default provider is **OpenAI-compatible**: CrewAI `LLM(model="openai/<model>", base_url=…,
  api_key=…)` — works with OpenAI, OpenRouter (`https://openrouter.ai/api/v1`), Azure, local
  servers (vLLM/Ollama), etc. Other litellm providers remain configurable via `provider` +
  model string.
- Per-role model override (`llm.models.<role>`) with `default_model` fallback.
- CrewAI **memory and delegation disabled** for all role agents; `respect_context_window=True`;
  `max_iter` per role (planner 25 — spec authoring is long-form, coder 40, reviewer 15,
  tester 25, **orchestrator 15, surveyor 20**) as a runaway guard. Exhausting `max_iter` ends the
  run as `WorkResult(failed)` (§5.7) — never a silent partial stop.
- Token usage is extracted per run and recorded in the ledger for budgets.

## 11. Security Model

- Secrets: env-only, redacted from logs/traces/config dumps; `codie validate-config` proves it.
- Prompt injection: GitHub content (PRD text, comments, code) is untrusted data. Role prompts state
  the hierarchy explicitly (system > role contract > issue content); only the PRD issue authored by
  a non-bot is treated as product intent; comments are instructions only when they fall in the
  three allowlisted classes of §7.6 — everything else is data.
- The init survey executes **no repository code** (static analysis, git history, and GitHub
  metadata only); discovered build/test commands become trusted (`.codie.yaml`) only through
  explicit user confirmation (§9.6, phase 2).
- The shell denylist and cwd jail are guardrails, not a sandbox; the trusted computing base is the
  machine user running codie. Containerized execution is the documented hardening path.
- Least privilege: per-role tokens + branch protection (§9.3) bound the blast radius of any single
  compromised or misbehaving role.

## 12. Observability

- **Logs:** structlog; console renderer for humans, JSON to `~/.codie/state/<owner>/<repo>.log`;
  agent-tagged records are dual-written (secrets-redacted) to the `agent_events` table that feeds
  the dashboard's log view (§6.5).
- **Run traces:** per work item at `~/.codie/state/<owner>/<repo>/runs/<run_id>/` — context pack,
  prompts, tool calls, LLM responses, mutations, cost. Secrets redacted.
- **GitHub as the visible trail:** status flips, comments, reviews, PRs are the primary user-facing
  narrative; local traces are for debugging.
- **CLI:** `codie status` (derived state + next actions, and the dashboard URL while the crew is
  running), `codie status --runs` (history/costs).

## 13. Testing Strategy (codie itself)

| Layer | Scope | Notes |
|---|---|---|
| Unit | parsers, deriver, validators, reconciler, queue, config merge, budgets | golden GitHub JSON fixtures; pure functions, no network |
| Contract | `FakeGitHubClient` vs `PyGithubClient` against the same behavior suite — **calls every protocol method in §9.1** (M19) | ensures the fake is faithful |
| Integration | orchestrator cycles over the fake; Planner structured-output validation; runner jail; **one test showing a budget pause reaps a finished handle and makes no model call (C14)** | fast, in CI |
| Provisioning | `codie init` step effects against the fake client; idempotent re-run; drift repair; check mode | unit-level, in CI |
| Orchestrator agent | stubbed-LLM behavior tests: only lawful queue items are dispatched; kernel refusals hold under adversarial tool calls; anomaly triage paths; the C19 idle-dispatch fallback | integration, in CI |
| Dashboard | API endpoints over a seeded cache; toggle → dispatch refusal; log filtering; SSE stream; `kernel` toggle → 404 (M27) | integration, in CI |
| E2E (gated) | real sandbox repo: PRD → features → PRs → UAT → release PR; the sandbox PRD plants **one user-surface failure the Coder's unit tests do not cover**, and the acceptance run files that bug before UAT (M28) | `CODIE_E2E=1` + tokens; runs nightly, not per-PR |
| Prompt regression | context-pack snapshots per role for fixed states | guard against silent prompt drift |

**Rule/edge pinning (M28).** Queue tests cover every §5.6 rule as amended (including first-match
per feature and that `kind:test` never yields `Implement`); reconciler tests cover every §5.5 edge
and reject an actor outside its matrix cell. Criterion 7 (Product §14) runs unit, contract,
integration, and provisioning in CI; e2e runs only when `CODIE_E2E=1`.

Coverage target: ≥ 80% on `state/`, `queue.py`, `config.py`, `runner.py`.

## 14. Milestones

| # | Milestone | Deliverables | Exit criteria |
|---|---|---|---|
| M0 | Scaffolding | pyproject, CI (ruff/mypy/pytest), config models, CLI skeleton, LLM plumbing + base tool harness, structured-output validation | `codie validate-config` works; CI green |
| M1 | Onboarding | GitHubClient + PyGithub + Fake, label sync, **Surveyor crew** (survey + issue triage + PRD discovery), confirmation UX (interactive / `--pr`), convention-aware provisioning, `codie doctor` | unit+contract+provisioning tests pass; `codie init` onboards a fresh repo (defaults, no PRD → waits), a convention-bearing repo (adoption + confirmed triage), and a repo with an existing requirements doc (PRD adopted) on real repos |
| M2 | Orchestrator + Planner | state derivation/reconciliation/queue (incl. `transitions.yaml`), orchestration kernel (budgets, heartbeat, stale recovery), **Orchestrator agent with gated tools**, context packs, Planner crew (**spec authoring + spec PR flow + requirement-ID validation**), `codie status`, `--once`/`--dry-run` | PRD → features → **specs → (stubbed Reviewer spec approval) → tasks** on a sandbox repo (dry-run + applied); stub-LLM orchestrator drives cycles over the fake client and kernel refusals hold; no task exists before the spec merge |
| M3 | Coder + Reviewer | workspace manager, runner, coder/reviewer crews, PR cycle, stale recovery | end-to-end: one task implemented, reviewed, merged on sandbox repo |
| M4 | Tester + release | tester crew, regression/acceptance runs, eval suite, release PR | Product §14 criteria **1–11** on sandbox repo (the dashboard criterion 12 is M5) |
| M5 | Hardening + dashboard | budgets, traces, **web dashboard** (agent roster, per-role toggles, filterable logs), docs, `codie doctor`, perf pass on API usage | Product §14 criterion 12 + soak run: 48 h unattended on sandbox repo without human repair; dashboard live for the full soak |

Post-v1 (not scheduled): Docker/devcontainer execution, GitHub App auth, Projects v2 sync,
webhook wake-ups, parallel work items per role, multi-repo orchestration.

## 15. Key Design Decisions (summary)

1. **Deterministic kernel, agentic orchestrator** — derivation, reconciliation, queue
   computation, and guardrail enforcement are deterministic tools; the Orchestrator agent
   supplies judgment (sequencing, anomaly triage, non-standard conventions) but acts only
   through those gated tools (§6.1). An LLM never decides *what work exists* — the queue
   defines that — only *how to proceed within it*.
2. **GitHub-only truth, disposable cache** — cold start anywhere loses nothing (§5.8).
3. **Planner proposes, orchestrator applies** — structured outputs validated before mutation.
4. **One work item = one fresh crew** — no cross-item agent memory; context is assembled, not
   remembered.
5. **Per-agent GitHub identities** — four role tokens plus a dedicated kernel token (§9.2), so
   every write including reconciliation is attributable; auditability and least privilege by
   construction.
6. **OpenAI-compatible LLM default** — provider-agnostic via litellm; OpenRouter-ready.
7. **Human gates exactly where the product spec says:** PRD confirmation, UAT label flips,
   release merge, `flag:needs-human`. **Spec approval is delegated to the Reviewer acting as
   product owner** (§7.4) so the crew is never blocked on per-feature spec sign-off. Everything
   else is the crew's job.
8. **Convention-adopting onboarding** — `codie init <url>` surveys the repo with a read-only
   Surveyor agent, confirms the discovered conventions with the user (adopting precedent,
   defaulting only where none exists), then provisions mechanically (§9.6). This prevents
   convention churn, and the confirmation step doubles as authorization of the commands the
   crew may run. `codie start <url>` (least-privilege role tokens, resumable) is the entire
   runtime interface.
9. **Non-deference with a kernel-owned cycle cap** — roles hold their own perspective and argue
   with evidence; one per-item counter (review rounds, spec-review rounds, bug reopens, failed
   acceptance runs, dispute exchanges) escalates past 3 completed cycles — counted and enforced
   by the kernel alone, never by the roles (§5.9).
10. **User-perspective acceptance** — the Tester owns black-box suites that drive the real
    UI/TUI/CLI surface, independent of the Coder; unit tests never substitute for surface-level
    acceptance (§7.5).
11. **Embedded, ephemeral dashboard** — agent status, per-role enable/disable, and filterable
    logs are served by the orchestrator process itself, localhost-first, living exactly as long
    as the crew (§6.5). Toggles are local kernel flags enforced by the dispatch gate — the
    dashboard holds no GitHub client and can never mutate project state or bypass guardrails.
12. **Spec gate: the Reviewer approves as product owner** — one spec PR per feature carrying
    both specs; the Reviewer examines PRD alignment and spec quality, and merges on its own
    `APPROVED` on the current head (§7.4). Implementation cannot begin before the merge (Product
    Spec §6.2).
13. **Halt only on release; everything else is a pause** — the process exits solely when the
    marked release PR merges; budget exhaustion and all-streams-blocked pause the daemon (polling
    without model calls), and a single `flag:needs-human` pauses only that stream (§6.1).

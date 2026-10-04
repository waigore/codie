# Codie — Product Specification

- **Status:** Draft v0.4.2 (round-4 reconciliation + substantial-comments norm)
- **Date:** 2026-10-04
- **Companion document:** [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md)

---

## 1. Overview

Codie is an autonomous software-development crew built on [CrewAI](https://www.crewai.com/). Given a
GitHub repository and a product requirements document (PRD) containing a machine-verifiable
**definition of done ("evals")**, Codie:

1. Translates the PRD into **features**.
2. Breaks each feature into **development, integration, and testing tasks**.
3. Implements, reviews, integrates, and tests those tasks.
4. Presents each completed feature for **human user acceptance** (accepted or revised).
5. Iterates until **all features are accepted and all evals pass**, then proposes a release.

All project state — requirements, features, tasks, statuses, review history, and the guardrail
counters (cycle caps, deferrals, suite results, PRD fingerprint) — lives on **GitHub**
(issues, labels, pull requests, marker comments). Codie can be stopped and restarted on any
machine and will rebuild its understanding of the project from GitHub alone. The single
intentional exception is the daily LLM **cost ledger**, which is a local UTC-dated cache (a fresh
machine starts the day at $0).

## 2. Goals and Non-Goals

### 2.1 Goals

- **Autonomy:** after `codie start`, the crew continuously discovers and executes work until the
  definition of done is met, with no per-task human prompting.
- **GitHub as the single source of truth:** any state not reconstructable from GitHub is disposable
  cache. (See Technical Spec §5 — State Derivation.)
- **Human-in-the-loop where it matters:** humans author the PRD and evals, accept or revise
  features (UAT), and approve releases. Everything else is delegated.
- **Project-type agnostic:** mobile apps, games, web services, libraries. Project-specific
  toolchains are described declaratively in per-project config (`.codie.yaml`).
- **Auditability:** every crew action is attributable to a per-agent GitHub identity (the four
  role bots plus a kernel identity) and mirrored in issue/PR history.
- **Operability:** a built-in web dashboard shows live agent status and logs and lets you pause
  or resume individual agents while the crew runs (§4.3).
- **Branch discipline:** `main` is stable and holds releases; `dev` is the integration branch;
  all work happens on short-lived feature branches merged via PR.
- **Convention-adopting onboarding:** `codie init <url>` surveys the repository with an agent
  and adopts its existing conventions — branching, issue management, commits, testing — after
  confirming them with you; codie's opinionated defaults apply only where the repo has no
  precedent. The crew blends in instead of causing convention churn; afterwards
  `codie start <url>` is the only command it needs.

### 2.2 Non-Goals (v1)

- Multi-project concurrency within one codie instance (run one instance per repo instead).
- Autonomous merging of releases to `main` (human merges the release PR; see §7.7).
- Sandboxed/containerized code execution (agents run in local workspaces; Docker is a follow-up).
- Desktop-app UI automation (`surface: ui-desktop` is reserved; excluded from v1 — see Technical
  Spec §7.5 for the supported surface/driver matrix).
- Authoring the PRD or its evals (the human owns product intent; the Surveyor may *propose* a
  synthesized draft at init and the Planner may *propose* refinements, but a human always
  confirms the PRD and its evals).
- GitHub Projects board synchronization (labels are the state mechanism; board support is future
  work).

## 3. Users and Use Cases

**Primary user:** a solo developer or small team lead who can write a PRD and wants a crew to
execute it.

Use cases:

- **Greenfield product:** point codie at an empty repo with a PRD; the crew bootstraps the project
  (scaffolding, CI, tooling) and builds it feature by feature.
- **Ongoing development:** point codie at an existing repo; the crew works through new features and
  bugs while respecting existing conventions.
- **Hardening passes:** PRD focused on test coverage, performance, or bug-backlog burn-down.

## 4. Operating Model

- One codie instance manages **one target repository**, identified everywhere by a single GitHub
  URL.
- One-time onboarding: `codie init <url>`, run with a privileged admin account, surveys the
  repository with an agent — including locating or proposing the PRD — confirms the discovered
  conventions with you, and provisions what is missing (§4.2; mechanics in Technical Spec §9.6).
- The human starts the crew with `codie start <url>`. Nothing else: all state, including the
  PRD, is on GitHub by then.
- The **orchestrator** (a long-running local daemon) polls GitHub and derives project state
  deterministically. An **Orchestrator agent** then decides what to dispatch, defer, or
  escalate — choosing only within the deterministically computed lawful work queue, with hard
  limits (budgets, concurrency, transition validity) enforced by the kernel, never by the
  agent's own restraint. This lets the crew handle non-standard project conventions with
  judgment, without letting judgment override guardrails.
- The work itself is executed by four role agents: **Planner, Coder, Reviewer, Tester** (§8).
- The crew halts **only** when the marked release PR is merged (definition of done satisfied).
  Budget exhaustion or all-streams-blocked is a **pause**: the daemon keeps polling (no model
  calls, no spend) and resumes when the condition clears. A single `flag:needs-human` pauses
  only that issue's stream (§11 rule 8), never the whole crew.

### 4.1 Human touchpoints (the complete list)

| # | Touchpoint | Mechanism | Required? |
|---|-----------|-----------|-----------|
| 0 | Onboard the repository | Run `codie init <url>` with a privileged admin account; confirm or edit the proposed conventions and PRD | Yes, once |
| 1 | Confirm the PRD + evals | At init: the Surveyor locates the PRD in the repo (or proposes one from docs/backlog, or asks you to supply/draft it); you confirm or edit before provisioning | Yes, once |
| 2 | Accept or revise a feature | Flip label `status:review` → `status:accepted` or `status:revised` on the feature issue (revision requires a comment describing the change) | Yes, per feature |
| 3 | Approve a release | Merge the integration → stable release PR (the configured `dev` → `main` names) and create the git tag from the suggested version | Yes, at end |
| 4 | Unblock the crew | Respond to issues carrying `flag:needs-human` | Only when raised |
| 5 | Pause or resume an agent | Toggle in the web dashboard (§4.3) | Optional |

Spec approval is deliberately **not** a human touchpoint: the Reviewer approves spec PRs from the
product owner's perspective, judging alignment with the PRD (§7.1, §8.3), so the crew's work is
never blocked waiting on per-feature spec sign-off.

### 4.2 Onboarding: survey, confirm, provision

`codie init <url>` onboards a repository so the crew can operate it **by its own conventions**.
It authenticates with a privileged admin account (`CODIE_GH_TOKEN_ADMIN`) — the only operation
that ever uses it — and runs in three phases.

**Phase 1 — Survey (agent-led, read-only).** A dedicated init-time **Surveyor** agent explores
the repository and answers every question the crew needs answered to operate the project: how
branches are named and merged, how issues are labeled and templated, how commits and PRs are
written, how the project builds, runs, and tests (manifests and CI configs are the richest
sources), what the product surface is, which quality gates exist, and what the docs already
prescribe. Every finding comes back with evidence (file paths, history samples, links).

The survey also **triages the repository's pre-existing issues**: each open issue (bounded to the
newest `survey.max_open_issues`, default 200; older ones are reported but untouched) gets a
proposed adoption — `type:*`/`status:*` labels with a rationale and confidence — so a repo's
existing backlog becomes crew-legible instead of invisible. Issues that don't fit the workflow
(questions, discussions) are left untyped and out of crew scope; the Surveyor never proposes
closing anyone's issues.

And it **locates or proposes the PRD** (§5). The Surveyor looks for requirements documents in
the repo (`PRD.md`, `docs/requirements*`, README "goals" sections, roadmap issues, design docs)
and proposes one of three outcomes for you to confirm: **adopt** an existing document as the
PRD (its evals checklist is extracted or drafted); **synthesize** a draft PRD from the docs and
backlog for your edit; or **await** — you supply one (now via `--prd <file>`, or later by
creating the pinned issue). The crew cannot start without a confirmed PRD; product intent is
always human-approved, never invented (§2.2).

**Phase 2 — Confirm (human).** The proposed conventions are presented for confirmation —
interactively in the CLI, or as a draft PR for async review. Each dimension is tagged
**adopted** (found in the repo, with evidence) or **default** (no precedent → codie's
opinionated choice), and you confirm or edit each one. The issue-triage proposal is reviewed
the same way: bulk-accept with per-issue overrides. This confirmation is also a security
boundary: it authorizes the exact build/test/run commands the crew will later execute on your
machine.

**Phase 3 — Provision (mechanical, convention-aware).** Only what is missing gets applied, per
the confirmed conventions: the codie label taxonomy is added (strictly additive — existing
labels are kept and mapped), confirmed issue adoptions are applied (labels set, plus one
adoption comment per issue), branch and protection gaps are filled, repo options are aligned
to the confirmed merge style, role collaborators are invited, the confirmed PRD (with its
evals checklist) is posted as the pinned `type:prd` issue, and an **onboarding PR** adds or
amends the two artifacts that make the conventions stick:

- **`.codie.yaml`** — the machine-readable operations contract (branch names and patterns,
  merge method, build/run/test commands, surface, acceptance framework, eval commands);
- **`AGENTS.md`** — the conventions **taught to the crew**, injected into every role's context
  pack ever after. If the repo already has one, it is amended minimally, never overwritten.

On a brand-new repository every dimension falls back to codie's opinionated defaults, so a
fresh repo still onboards in one pass.

Two things are **not** negotiable conventions: the codie **workflow protocol** (the `type:*` /
`status:*` label state machine, UAT label flips, `Parent:`/`Closes #` linkage markers) — the
orchestrator derives project state from it — and the **human gates** (§4.1). Everything else
defers to repository precedent.

### 4.3 Dashboard

While `codie start` runs, it serves a **web dashboard** (localhost-only by default) showing:

- **Agent status** — for every agent (Planner, Coder, Reviewer, Tester, Orchestrator): whether
  it is enabled, what it is working on right now (with GitHub links), or — when idle — what it
  last worked on and how that ended, plus the day's token spend.
- **Enable/disable toggles** — pause or resume each agent individually. The kernel refuses new
  dispatches to a disabled agent (in-flight work finishes gracefully), and its queued work
  waits rather than disappearing — re-enabling resumes with no state loss.
- **Agent logs** — a live feed of crew activity (dispatches, tool calls, decisions,
  escalations, budget events), filterable per agent and by level.

The dashboard lives exactly as long as the crew: it starts with `codie start` and stops with
it. It is read-and-toggle only — it never mutates GitHub state, and its toggles are enforced by
the deterministic dispatch gate, not by agent cooperation. Mechanics in Technical Spec §6.5.

## 5. Work Hierarchy

```
PRD (pinned issue, type:prd, contains evals E1..En)
 └── Feature (issue, type:feature)            ← human-accepted
      ├── Task (issue, type:task, kind:dev)          ─┐
      ├── Task (issue, type:task, kind:integration)   ├─ agent-completed via PRs into dev
      └── Task (issue, type:task, kind:test)         ─┘
Bugs (issue, type:bug) — filed by Tester, linked to a feature when known
```

- **PRD:** the requirements and the evals (definition of done). Sourced at init (survey:
  adopted from the repo, synthesized for your edit, or human-supplied) and **always confirmed
  by a human**; stored as a pinned GitHub issue so all state is queryable in one place. Evals
  are a checklist with stable IDs (`E1`, `E2`, …). Machine-checkable evals are mapped to
  commands in the repo's `.codie.yaml`.
- **Feature:** a user-meaningful increment traced to PRD sections and to the evals it contributes
  to. The unit of human acceptance. Each feature is defined by a **feature spec** (functional +
  non-functional requirements) and a **technical spec** (technical requirements and design),
  both approved by the Reviewer (as product owner) before implementation begins (§6.2, §9).
- **Task:** an agent-executable unit of work. Exactly one kind: `dev`, `integration`, or `test`.
  Tasks declare dependencies on other tasks.
- **Bug:** a defect found by the Tester (or a human). Follows the task lifecycle; fixed by the
  Coder; verified by the Tester.

## 6. State Model and Label Taxonomy

Every codie-managed issue carries **exactly one `type:*` label** and **exactly one `status:*`
label** — **except `type:prd`**, which carries `type:prd` plus optional flags and **never** a
`status:*` (the PRD has no lifecycle) — plus optional kind/priority/flag labels (at most one
`priority:*`). `codie labels sync <url>` creates and
maintains this label set in the target repo (any of the three URL forms).

### 6.1 Type labels

| Label | Meaning |
|---|---|
| `type:prd` | The product requirements + evals tracking issue (exactly one per repo, pinned) |
| `type:feature` | A feature derived from the PRD |
| `type:task` | A development/integration/testing task belonging to a feature |
| `type:bug` | A defect |

### 6.2 Status labels — Feature lifecycle

```
proposed → speccing → spec-review → specified → planned → in-progress → review → accepted
              ▲            │                                                              │
              └────────────┘  (Reviewer requests changes on the spec PR → back to speccing) ▼
                                                                                          revised ─┬─ requirements/design changed → speccing (re-spec, re-approve)
                                                                                                  └─ implementation-only fix  → planned  (re-plan directly)
   (any non-terminal state) → cancelled
```

Implementation is gated: a feature cannot reach `planned` (and therefore no task under it can
be dispatched) until its spec PR (both specs) is **approved and merged by the Reviewer** (§7.1, §9).

| Status | Meaning | Set by |
|---|---|---|
| `status:proposed` | Scoped by Planner (title, PRD refs, evals, intent); specs not yet drafted | Kernel (applying Planner output) |
| `status:speccing` | Planner is drafting or revising the feature + technical specs (also the return state when the spec PR gets change requests) | Kernel |
| `status:spec-review` | Spec PR (both specs) open and ready for review; **awaiting the Reviewer's product-owner spec approval** (a draft PR keeps the feature in `speccing`) | Kernel |
| `status:specified` | Spec PR approved and merged by the Reviewer; specs are canonical | Kernel (reconciliation) |
| `status:planned` | Broken into tasks with dependencies; ready for execution | Kernel (applying Planner output) |
| `status:in-progress` | At least one task has left `backlog` | Kernel (reconciliation) |
| `status:review` | All tasks done; acceptance run passed; **awaiting human UAT** | Tester |
| `status:accepted` | Human approved the feature | **Human** |
| `status:revised` | Human requests changes (must add a comment describing them) | **Human** |
| `status:cancelled` | Dropped during re-planning | Kernel (applying Planner output) |

Feature terminal states are `accepted` and `cancelled` (so an `accepted` feature cannot later be
cancelled); task/bug terminals are `done` and `cancelled` (N15). The full edge list and per-role
write permissions live in the transition table and mutation matrix (Technical Spec §5.5); "Set by"
above names the accountable actor for the common path.

### 6.3 Status labels — Task/Bug lifecycle

Tasks:

```
backlog → ready → in-progress → in-review → done   (terminal)
              ▲        ▲            │
              │        └────────────┘  (review changes requested)
              └─ (stale recovery only, Technical Spec §5.8)
   (any non-terminal state) → cancelled   (terminal)
```

Bugs (verification is a first-class state — a merged fix is not a done bug):

```
backlog → ready → in-progress → in-review → verifying → done   (terminal)
              ▲        ▲            │                 │
              │        └────────────┘                 └─ (fix fails re-verification → in-progress)
              └─ (stale recovery only, Technical Spec §5.8)
   (any non-terminal state) → cancelled   (terminal)
```

| Status | Meaning | Set by |
|---|---|---|
| `status:backlog` | Created with unmet dependencies | Kernel (applying Planner output) / Tester (bugs) |
| `status:ready` | All dependencies `done`; workable now. New bugs start here unless they carry unmet `Depends on:` | Kernel (reconciliation) / Tester (new bugs) |
| `status:in-progress` | Claimed by the worker (issue assigned to its bot identity): Coder for `dev`/`integration`/bugs, Tester for `kind:test` | Coder / Tester (`kind:test`) |
| `status:in-review` | PR open against the integration branch, awaiting Reviewer | Coder / Tester (`kind:test`) |
| `status:verifying` | **Bugs only.** Fix PR merged; awaiting the Tester's re-verification | Reviewer (on merge) |
| `status:done` | Tasks: PR merged. Bugs: fix re-verified by the Tester | Reviewer (tasks, on merge) / Tester (bugs) |
| `status:cancelled` | Obsoleted by re-planning | Kernel (applying Planner output) |

### 6.4 Orthogonal labels

| Label | Applies to | Meaning |
|---|---|---|
| `kind:dev` / `kind:integration` / `kind:test` | tasks (exactly one) | Task kind |
| `priority:high` / `priority:medium` / `priority:low` | all | Dispatch tie-breaker (default: medium) |
| `flag:blocked` | all | Cannot proceed; reason in latest comment. Set/cleared by the Coder or Tester on its own active work item, or by a human |
| `flag:needs-human` | all | Crew escalated to a human; crew will not touch it while set. Set by the kernel (or a human) |

### 6.5 Why flags are not statuses

`blocked` and `needs-human` can apply at any lifecycle point without destroying it. When the flag is
removed, the issue resumes from its existing `status:*`.

## 7. Workflow Detail

### 7.1 Planning (PRD → features → specs → tasks)

1. Human onboards the repo; the confirmed PRD is the pinned `type:prd` issue.
2. The **Planner** reads the PRD and proposes a feature set: one issue per feature capturing
   title, intent, PRD references, and contributing evals (`Evals: E1, E3`). New features enter
   as `status:proposed`.
3. **Spec authoring.** For each proposed feature the Planner writes **two detailed
   specs** (§9) in the repo: a **feature spec** (functional + non-functional requirements —
   user stories, UX flows, acceptance criteria, and NFRs: performance, reliability, security,
   accessibility, i18n, etc.) and a **technical spec** (technical requirements and design —
   architecture and module boundaries, data models, interfaces and contracts, algorithms,
   error handling, test strategy, risks). Both files ship in **one spec PR** on branch
   `spec/<feature#>-<slug>` targeting the integration branch; the feature moves
   `proposed → speccing → spec-review` (the kernel flips to `speccing` when the Planner starts
   drafting and to `spec-review` when the PR is open and ready for review — a draft PR keeps it
   in `speccing`).
4. **Spec approval (Reviewer as product owner).** The Reviewer examines the spec PR from the
   **product owner's perspective**: does the specification align closely with the product
   requirements — faithful coverage of the cited PRD sections, nothing invented beyond them,
   acceptance criteria that genuinely demonstrate each requirement, evals correctly mapped? It
   also reviews spec quality (completeness, internal consistency, conventions, requirement-ID
   hygiene, §9). On approval the Reviewer merges the spec PR, moving the feature to
   `status:specified`; change requests move it back to `speccing`. Humans may still comment on
   spec PRs — the Reviewer addresses substantive comments like any review feedback — but no
   human approval is required. **No task breakdown, and no implementation, begins until the
   spec PR is merged.**
5. **Breakdown.** With specs canonical, the Planner decomposes the feature into tasks
   (`kind:dev`, `kind:integration`, `kind:test`), each with `Parent: #<feature>`,
   `Depends on: #<task>, …`, and a "Done when" checklist **traced to spec requirement IDs**
   (§9). Feature moves to `status:planned`.
6. Task ordering is derived from dependencies; `priority:*` breaks ties.

### 7.2 Development (tasks → PRs)

1. The **Coder** claims the highest-ranked `status:ready` task: assigns the issue to its bot
   identity, flips it to `status:in-progress`. If a branch or open PR already exists for the task
   (e.g., after a stale-run reset), the Coder **resumes it** instead of starting over.
2. Coder creates the task branch (per the repo's configured `feature_pattern`, e.g.
   `feature/<issue#>-<slug>`) from the latest integration branch, implements, runs the project's
   configured build/test commands locally, commits in the repo's confirmed style (from the
   conventions; default conventional commits referencing the issue),
   pushes, and opens a PR targeting the integration branch with `Refs #<task>` in the body (no
   closing keyword — the kernel closes the issue when it reaches a terminal status).
3. Task moves to `status:in-review`.

### 7.3 Review (PRs → integration branch)

1. The **Reviewer** picks up PRs linked to `status:in-review` tasks.
2. Review covers: correctness against the task's "Done when", scope discipline, conventions
   (target repo's `AGENTS.md`/`CONTRIBUTING.md`), security, and CI status: if the repo has
   required checks, pending or failing checks block merge; if it has none, missing CI counts as
   success. "Green" means every required check succeeded and none is pending.
3. Outcomes:
   - **Approve + merge into the integration branch** (using the repo's confirmed
     `merge_method`) → task `status:done`; a bug's PR merges to `status:verifying` instead
     (§6.3).
   - **Request changes** with actionable comments → task back to `status:in-progress`; Coder
     iterates on the same branch/PR.
4. The **kernel** counts review rounds toward the per-item cycle cap (§8.5); on breach it
   escalates with `flag:needs-human` and a deadlock summary — the human breaks the deadlock.

### 7.4 Integration

- The integration branch (`dev` unless the repo's confirmed conventions say otherwise) is always
  integrated: every task lands there via PR.
- `kind:integration` tasks handle work that spans tasks/features: resolving cross-feature
  conflicts, wiring components, fixing integration breakage. Implemented by the Coder,
  verified by the Tester.

### 7.5 Testing and feature acceptance

The Tester verifies **from the user's perspective, independently of the Coder**: it works from the
requirements and acceptance criteria, never from the diff, and never accepts the Coder's claims or
the Coder's tests as evidence.

1. **Test ownership is split by perspective.** The Coder owns unit/integration tests
   (testability of its code). The Tester owns the **black-box acceptance suite**
   (`tests/acceptance/` or the project's equivalent): it authors and runs tests that exercise the
   product through its actual user-facing surface — for UI/mobile apps, against the real UI
   (e.g. Playwright, Maestro, XCUITest/Espresso); for TUI apps, through the terminal (e.g.
   pexpect/tmux); for CLIs and APIs, through real invocations and requests; for games, via engine
   harnesses, input injection, and screenshot checks. Unit or integration tests **never
   substitute** for surface-level acceptance.
2. `kind:test` tasks are the Tester's: design and automate acceptance coverage of the feature's
   criteria, against the running product.
3. After every merge to the integration branch, the Tester runs the fast suite (regression);
   periodically and before feature acceptance, the full suite plus the acceptance suite. Each
   suite result is recorded as a PRD comment marker (`<!-- codie:suite fast|full <sha> pass|fail
   -->`), and an acceptance run requires a passing full-suite marker on the current integration
   head.
4. When **every non-cancelled task of a feature is `done`** (at least one) **and every linked bug
   is `done` or `cancelled`**, the Tester performs the **acceptance run**: clean
   checkout of the integration head, build, launch the app, and verify every acceptance criterion
   through the
   user surface. On success it posts a UAT summary comment (what was built, how to verify it,
   eval coverage) and flips the feature to `status:review`. On failure it files `type:bug`
   issues instead.
5. Failures discovered at any point become `type:bug` issues. Severity maps onto priority labels:
   `priority:high` (feature-blocking or data loss), `priority:medium` (requirement fails,
   workaround exists), `priority:low` (cosmetic/minor). `Parent: #<feature>` is optional; a
   regression with no clear feature stays parentless and names the offending PR in its body. New
   bugs enter at `status:ready` unless the Tester declares unmet dependencies (`status:backlog`).
   Bugs block feature acceptance. If the Coder disputes a bug, or a fix keeps failing
   re-verification, the cycle-cap rule applies (§8.5).

### 7.6 User acceptance and revision

- **Accept:** human flips the feature issue to `status:accepted`.
- **Revise:** human flips to `status:revised` and adds a comment describing required changes. The
  Planner first **assesses the revision** and returns a structured verdict: `requirements` (the
   change touches requirements or design → update the specs first: new spec PR, approved by the
   Reviewer again, then re-break into tasks; feature returns to `speccing`) or `implementation` (re-plan
  tasks directly; feature returns to `planned`). Obsolete open tasks are `status:cancelled`
  (any non-terminal task may be cancelled; their open PRs are closed with an explanatory
  comment).
- A human may also add `flag:needs-human` to any issue to pull it out of the crew's hands, or edit
  the PRD issue (the Planner reconciles features against PRD changes).

### 7.7 Evals and release (definition of done)

1. When **every non-cancelled feature is `status:accepted`** (at least one accepted), **no bug is
   open** (parented or parentless), and at least one eval is still unchecked, the
   Tester executes the **eval suite**: every machine-checkable eval (`E1..En`) is run via its
   `.codie.yaml` command; non-executable or `human` evals are flagged for human confirmation via a
   `<!-- codie:eval E<n> uncertain -->` marker and wait for the human's checkbox (while the marker
   stands, the eval suite is not re-dispatched for that eval). A **failing eval
   files `type:bug` issues on the features that cite it** — the crew iterates on those bugs and
   re-runs the suite; already-passing evals stay checked, and an eval with an open bug citing it
   is not re-filed.
2. Passing evals are checked off in the PRD issue checklist.
3. When all evals pass and no release PR is already open, the **Planner** proposes a **release
   PR** (integration → stable branch) containing the changelog, eval report, and a suggested
   version tag; the kernel opens it, marked with `<!-- codie:release -->`. The release PR is
   excluded from the Reviewer's merge authority.
4. **The human merges the release PR** and then creates the git tag from the suggested version —
   the kernel never calls the tags API. This is the crew's only halt condition: the kernel
   detects the merged marker-carrying PR, posts a final summary, and exits.

## 8. Roles

Each role is a distinct CrewAI agent with its own GitHub identity (own token/bot account), its own
LLM configuration, and a least-privilege tool set. Full tool definitions are in the Technical Spec.

**Communication discipline:** a role comments only when it has something substantial to say —
evidence, a decision, a question, a rebuttal, a required summary (UAT, block reasons, disputes).
Acknowledgements and status echoes are noise; whether to comment is left to the agent's judgment.
Kernel-written comments (escalations, adoptions, violations, heartbeats) are deterministic
markers, not agent speech, and are unaffected (Technical Spec §7.6).

### 8.1 Planner — the whole-project view

- **Perspective:** owns delivery of the **entire product**. Judges every decision against
  project-wide concerns: faithful PRD coverage, sound architecture, quality standards, and
  non-functional requirements (performance, reliability, security posture, maintainability,
  operability). A plan that ships features but rots the architecture is a failed plan.
- **Knows:** the PRD and evals; the full feature/task/bug graph; the repo's architecture and
  conventions; NFR targets (from the PRD or `.codie.yaml`); workflow rules; human feedback on
  `status:revised` features.
- **Does:** decomposes the PRD into features; **writes the feature and technical specs** (§9) and
  drives them through the Reviewer's product-owner approval; breaks *approved* features into
  dev/integration/test tasks
  with dependencies; sets and guards the architecture (creating dedicated
  architecture/refactoring tasks when needed); states NFR and quality expectations explicitly in
  specs and task bodies; prioritizes; re-plans revised features (updating specs first when
  requirements changed); reconciles the plan when the PRD changes; cancels obsolete work; returns
  the `ReleasePlan` — the **kernel** opens the release PR from it.
- **Does NOT:** write production code; review or merge PRs (its spec PRs are approved and merged
  by the Reviewer, never self-merged); accept features (human-only); break down a feature before
  its specs are approved; dilute or descope requirements to make failures go away (only humans
  change requirements); edit the PRD's substance (it proposes, the human disposes).

### 8.2 Coder — the task-craft view

- **Perspective:** owns **excellence of the implementation in front of it**. Judges its work
  against good coding principles: SOLID, DRY, clear naming, small focused diffs, and
  testability — code it cannot unit-test is code it has not finished. It implements the task as
  specified: not a narrower version, not a broader one.
- **Knows:** the task issue and its "Done when"; the parent feature's **approved specs** (§9) —
  the technical spec defines the interfaces and patterns it must implement, and the feature spec
  defines the requirements its code must satisfy; the target repo's codebase and conventions;
  the project's configured setup/build/test commands; review feedback on its open PRs.
- **Does:** claims ready `kind:dev`/`kind:integration` tasks (and bug fixes); branches from the
  integration branch; implements with unit/integration tests; runs local build/test; commits and
  pushes; opens and updates PRs to the integration branch; iterates on review feedback; keeps PRs
  current by **merging the integration branch into the feature branch** (never rebasing or
  force-pushing); resumes an existing branch/PR when the task already has one.
- **Does NOT:** merge PRs; push to the stable or integration branch directly; change feature statuses; self-accept
  work; write the feature's black-box acceptance tests (the Tester's domain); narrow a requirement
  because it is hard (it flags the Planner instead); concede a review or bug dispute it believes
  is wrong — it argues with evidence (§8.5).

### 8.3 Reviewer — the integration-branch gatekeeper

- **Perspective:** owns the **integrity of the integration branch at the PR boundary**. Judges each PR on
  code-level correctness, security, scope discipline, and convention compliance — on evidence
  alone, never because the Planner scheduled the work or the Coder vouches for it. For spec PRs
  it takes a second perspective: the **product owner's** — critically examining whether the
  feature's specification aligns closely with the product requirements (PRD), not merely whether
  it is well-formed.
- **Mission:** protect the integration branch — nothing merges that is incorrect, out of scope, insecure, or
  non-conformant — and protect the PRD: no spec lands that misreads, under-covers, or invents
  beyond the product requirements.
- **Knows:** the PR diff and full change context; the linked task's "Done when" and the parent
  feature's **approved specs** (code is reviewed against the technical spec's interfaces and
  design); repo conventions; CI results; review-cycle history.
- **Does:** reviews task PRs; merges conformant PRs into the integration branch using the repo's
  confirmed merge method; requests changes with specific, actionable comments; verifies PRs target
  the correct branch; enforces test expectations; reviews spec PRs **as the product owner** —
  alignment with the PRD (requirement coverage, acceptance criteria that demonstrate the
  requirements, eval mapping, scope discipline) plus spec quality — and **approves and merges
  them on its own verdict** (§7.1).
- **Does NOT:** implement fixes itself (it writes suggestions, the Coder applies them); review its
  own work (it has no authoring capability); merge to the stable branch (including release PRs —
  human-only); merge a spec PR it has not approved on the current head; escalate on its own — the
  kernel owns the cycle cap.

### 8.4 Tester — the user's advocate

- **Perspective:** owns **requirements-fitness from the user's seat**. Its question is never "is
  the code good?" but "does the product, used the way a user uses it, actually meet the
  requirement?" It verifies through the real user-facing surface (UI, TUI, CLI, API) and is
  deliberately independent of the Coder: it works from requirements and acceptance criteria, not
  from the diff, and does not reuse the Coder's tests as evidence.
- **Knows:** the PRD and each feature's **approved feature spec** (its functional + non-functional
  requirements and acceptance criteria are what the Tester verifies — the spec, not the code, is
  the contract); the product evals; the project's build/run/test commands and acceptance drivers;
  the current state of `dev`; the bug lifecycle.
- **Does:** owns `kind:test` tasks — designs, authors, and runs the black-box acceptance suite
  (§7.5); runs regression suites on merges; performs feature acceptance runs and moves features
  to `status:review` with a UAT summary; files well-formed `type:bug` issues with user-level
  reproductions; re-verifies fixes; executes the eval suite and checks off PRD evals; gates the
  release PR.
- **Does NOT:** fix bugs (files them for the Coder); approve or merge PRs; accept features
  (human-only); pass a feature because the code reviewed well; withdraw a bug it can still
  reproduce — it answers disputes with captured evidence (§8.5).

### 8.5 Independence, contradiction, and deadlock resolution

**No role defers to another.** Each role judges exclusively from its own perspective and defends
that judgment with evidence. Concretely: the Reviewer does not approve because work was planned
or the Coder insists; the Tester does not pass a feature because the PR reviewed well; the Coder
does not implement around a requirement because it is inconvenient; the Planner does not descope
a requirement to make a failing acceptance run disappear.

Contradictions are argued in the open: on the issue or PR, each side states its position with
evidence (requirement quotes, logs, reproductions, diff references). Roles may concede when the
other side's evidence is genuinely convincing — that is reconciliation, not deference. When a
contradiction cannot be reconciled, the **cycle cap** breaks it. The **kernel alone** counts and
enforces the cap (roles never self-escalate): one per-item counter over review rounds on one PR,
reopen rounds on one bug, failed acceptance runs on one feature, spec-review rounds, and dispute
exchanges (a dispute exchange completes when the answering role posts a later `**Dispute:**` or a
concession comment). Past `guardrails.max_cycles_per_work_item` (default 3) completed cycles
without a terminal state, the kernel adds `flag:needs-human` and posts a deadlock summary with
each side's last position. **The human breaks the deadlock** by commenting the decision (and
adjusting the plan or labels if needed); the crew resumes from there.

**Which comments are instructions.** Only three classes of comment text are treated as actionable
instruction (this is the single allowlist — §12 points here, and Technical Spec §7.6/§11 carry the
same list); everything else on GitHub is data (evidence, context), never commands:

1. a **non-bot comment** on the issue or PR of this dispatch (revision feedback, dispute
   resolutions, direct answers — within the addressed role's scope);
2. a **Reviewer review comment** (including a line comment) on an open PR this role authored;
3. on a bug assigned to the Coder, the Tester's `## Reproduction` and `## Re-verification`
   sections.

## 9. Spec Conventions (how all agents write and read specs)

Every feature is defined by **two Reviewer-approved specs, stored as markdown in the target
repo** — they are the canonical project documentation, the artifacts the Reviewer examines (as
product owner) at the spec gate, and the content every role reads. Specs are code: they change
only via PR.

### 9.1 Location and naming

```
specs/<issue#>-<slug>/
├── feature-spec.md     # functional + non-functional requirements (the "what")
└── technical-spec.md   # technical requirements + design (the "how")
```

Both files for a feature live under the same directory and ship in **one spec PR** (branch
`spec/<issue#>-<slug>`).

**Slug grammar:** a slug is `[a-z0-9-]{1,48}`, derived from the issue title (lowercased,
non-alphanumerics collapsed to hyphens, trimmed). Branches and paths use `<issue#>-<slug>`; the
leading integer up to the first hyphen is the issue number — digits inside the slug are never
parsed as numbers. On collision with an existing branch or `specs/` directory, append `-2`,
`-3`, ….

### 9.2 The two specs

**Feature spec** (`feature-spec.md`) — functional and non-functional requirements:

- **Scope:** problem statement; user stories; UX flows (step-by-step for UI/TUI features); edge
  cases; out-of-scope (explicit).
- **Functional requirements** as a numbered list with stable IDs: `FR-1`, `FR-2`, …
- **Non-functional requirements** as `NFR-1`, `NFR-2`, … — performance, reliability, security,
  accessibility, internationalization, operability, each with a measurable target where the PRD
  or conventions give one.
- **Acceptance criteria** as `AC-1`, `AC-2`, …, each traced to the requirements it covers.

**Technical spec** (`technical-spec.md`) — technical requirements and design:

- **Architecture:** components and their boundaries; module/package placement; how this feature
  fits the codebase's existing structure.
- **Data & interfaces:** data models and schemas; public interfaces and contracts; protocol or
  API shapes.
- **Approach:** algorithms and key techniques; error handling; concurrency/performance approach.
- **Test strategy:** what unit/integration coverage the Coder must provide, and what the
  Tester's black-box acceptance suite must exercise (per §7.5).
- **Risks & alternatives:** trade-offs, rejected options, open questions.

Specs are **detailed enough to implement from without further invention** — a Coder or Tester
reading the spec alone should have every interface, requirement, and acceptance criterion needed.

### 9.3 Requirement IDs — the traceability spine

`FR-*`, `NFR-*`, and `AC-*` IDs are **stable for the life of the feature** (never renumbered;
superseded ones are struck through, not deleted — a struck-through ID stays readable as history
but **fails validation if cited by new work**). Every downstream artifact references them:

- task "Done when" items cite requirement IDs (`Done when: FR-2, FR-3`);
- Reviewer verdicts and change-requests cite them;
- the Tester's acceptance run maps each criterion to its `AC-*`;
- bugs cite the requirement they violate.

**Canonical grammar (parsed by the orchestrator; one form only, M20):**

- A live requirement/AC line matches `^- (FR|NFR|AC)-[1-9][0-9]*: .+$`; an `AC` line additionally
  ends with ` \((FR|NFR)-[1-9][0-9]*(, (FR|NFR)-[1-9][0-9]*)*\)$` (the trace list, mandatory).
- A superseded line is the **whole line** wrapped in `~~…~~` (no "text-only" variant).
- A task citation is only a `## Done when` checkbox line ending in ` (ID[, ID…])`, and **at least
  one ID is required** (a task citing nothing fails breakdown validation — §14 criterion 11).
- The feature spec's level-2 headings are, in order, the §9.2 bullets (Scope; Functional
  requirements; Non-functional requirements; Acceptance criteria) so the kernel can validate them
  (C17).

This is how "fits requirements" stays objective across roles that never defer to each other (§8.5).

### 9.4 Writing and reading rules (all agents)

- **Write (Planner only):** specs are authored and revised only by the Planner, via PRs targeting
  the integration branch. Other roles never edit spec files.
- **Read (all roles):** every context pack includes the feature's specs (§7.1 of the Technical
  Spec), read at the integration-branch ref (`branches.dev`) — never from stale copies or from
  memory of an issue body.
- **Change discipline:** any change to a spec is a PR approved by the Reviewer (product-owner
  review) — even mid-implementation (e.g., when a revision or a discovered technical constraint
  alters the design). Code and its spec never diverge silently; a PR that would violate the
  current spec is blocked until the spec PR lands or the code is brought back in line.
- **Single source of truth:** when a spec and something else disagree (an issue body, a comment,
  the code), the merged spec wins; the conflict is surfaced as a `**Dispute:**` (§8.5), not
  resolved silently.

## 10. Issue Body Conventions

The orchestrator parses these markers; templates are enforced at creation time.

**Feature issue:**

```markdown
## Description
<what and why — scope and intent>

**PRD sections:** <refs>
**Evals:** E1, E3

## Specs
- Spec PR: #45 → specs/<issue#>-<slug>/feature-spec.md + specs/<issue#>-<slug>/technical-spec.md

## Tasks
- [ ] #41
- [ ] #42
```

(Detailed acceptance criteria live in the feature spec as `AC-*`; the issue links them rather
than duplicating them.)

**Task/Bug issue:**

```markdown
**Parent:** #17            # optional — omit when none (e.g., a parentless regression bug)
**Depends on:** #38, #39   # optional

## Context
<what the agent needs to know>

## Done when
- [ ] <checkable condition> (FR-2)
- [ ] <checkable condition> (NFR-1)
```

**Bug issues additionally:** `## Reproduction` and `## Expected vs actual` sections, the
requirement ID(s) violated, and — for parentless regression bugs — the offending PR reference.

**Marker grammar (parsed line-anchored, case-insensitive):** `**Key:** value` or bare
`Key: value` — Markdown bold around the key is optional. The PRD eval checklist uses
`- [ ] E1: <text>` / `- [x] E1: <text>`; only the Tester's check-off edits may change `[ ]` →
`[x]`.

## 11. Autonomy Rules

1. **Implementation waits for spec approval.** Task breakdown and all task dispatch are gated on
   the parent feature reaching `status:specified` (both specs approved and merged by the
   Reviewer); a feature stuck at `spec-review` pauses only that feature's stream, never the rest
   of the crew.
2. **Lawful work is computed; choices within it are agentic.** Each cycle deterministically
   derives the work queue from GitHub state (Technical Spec §5.6): ready tasks → open PRs
   needing review → features awaiting acceptance runs → planning gaps → eval execution. The
   Orchestrator agent then chooses what to dispatch, defer, or escalate — and triages anomalies
   (untyped new issues, human-opened PRs, convention drift) using the confirmed conventions as
   its guide — but it can never act outside the computed queue.
3. **Hard limits are kernel-enforced, not agent-policed.** Budgets, one work item per role at a
   time (v1), label-transition validity, and per-role enable/disable (dashboard toggles, §4.3)
   are enforced by deterministic tooling the Orchestrator agent cannot override; work for a
   disabled role is held, never dropped.
4. **Bugs outrank feature work** by default (configurable per project).
5. **Prioritization:** dependency order first, then `priority:*`, then issue number.
6. **Self-healing:** a task stuck `status:in-progress` with no worker heartbeat for a configurable
   timeout is reset to `status:ready` with an explanatory comment.
7. **Deadlock detection:** irreconcilable inter-role contradictions escalate via the cycle cap
   (§8.5) instead of looping forever.
8. **Halt and pause:** the process exits only when the marked release PR is merged. Budget
   exhaustion, all streams blocked, or all remaining items flagged `needs-human` is a **pause** —
   the daemon keeps polling without model calls and resumes when the condition clears. One flag
   pauses only that item's **stream**: the flagged issue plus whatever transitively `Depends on`
   it or has it as parent (the §5.5 flag filter) — sibling tasks stay eligible.
9. **Idle behavior:** nothing actionable → exponential-backoff polling (bounded).

## 12. Guardrails

- The privileged admin account (`CODIE_GH_TOKEN_ADMIN`) is used **only** by `codie init`
  (provisioning). It is never loaded by the orchestrator or any role; at runtime the crew uses
  the five least-privilege tokens (four roles + kernel).
- The Orchestrator agent's authority ends at the kernel: it cannot exceed budgets, bypass
  concurrency limits, mutate state outside the transition table, or invent work outside the
  computed queue — those are enforced by deterministic tools, not by its prompt.
- Agents **never push directly to the stable or integration branch**; all changes arrive via PR.
  Branch protection on both branches is required (settings documented in Technical Spec §9.3).
- Only the **Reviewer's** identity may merge PRs into the integration branch (task PRs freely;
  spec PRs only after its own product-owner approval on the current head); only humans merge
  release PRs into the stable branch.
- The work-item cycle cap (default 3) prevents infinite loops: PR review rounds, spec-review
  rounds, bug reopen rounds, failed acceptance runs, and dispute exchanges all escalate to a
  human — counted and enforced by the kernel alone (§8.5).
- Per-run and per-day **LLM budget caps**; the crew pauses rather than overrunning.
- Shell command execution is jailed to the project workspace and filtered by the configurable
  denylist (`guardrails.shell_denylist` — fnmatch globs over the joined argv).
- Secrets are referenced from environment variables only; they are never written to issues, PRs,
  logs, or traces.
- Content from GitHub (PRD, comments, code) is treated as **data, not instructions**, to resist
  prompt injection — except the three instruction-bearing comment classes defined in §8.5, which
  are the only exceptions; role system prompts enforce scope.

## 13. Configuration Surface (product view)

| File | Location | Contents |
|---|---|---|
| `codie.yaml` | `~/.codie/codie.yaml` or codie repo | Global config: LLM (OpenAI-compatible endpoint + per-role models + price map), per-agent GitHub token env-var names **and bot logins** (planner/coder/reviewer/tester/kernel + admin), project registry, budgets, poll interval, concurrency, dashboard (enable/host/port/token) |
| `.codie.yaml` | target repo root | Per-project (replaces per top-level key): branch names/patterns, merge method, label mapping, surface, acceptance, setup/build/test/eval commands (argv form), eval ID → command-or-human mapping, workflow, guardrail overrides (written at init from the confirmed survey) |
| `AGENTS.md` | target repo root | The conventions taught to the crew — branching, commits, PRs, testing, style — written or amended at init; injected into every role's context pack |

Credentials are **never** stored in YAML; YAML names the environment variable that holds each
credential (e.g., `api_key_env: CODIE_LLM_API_KEY`).

## 14. Acceptance Criteria for Codie v1

Codie v1 is done when, on a sandbox repository with a sample PRD:

1. The Planner produces features and tasks that a human judges correct without edits.
2. The crew completes at least one multi-task feature end-to-end: tasks → PRs → review → merge →
   acceptance run → `status:review`.
3. Human `status:revised` feedback produces a correct re-plan and successful re-acceptance.
4. All evals execute and the release PR is generated with an accurate changelog.
5. The orchestrator is killed mid-task and restarted on a **fresh machine**; it rebuilds state from
   GitHub and resumes without duplicating or losing work.
6. The crew halts cleanly on the merged release; a `flag:needs-human` pauses the affected stream
   while the rest of the crew continues; when every stream is blocked the daemon pauses without
   model calls and resumes when unblocked.
7. Codie's own test suite (unit + integration + gated e2e) passes in CI.
8. `codie init <url>` on an **existing** repository surveys it and proposes the repo's own
   conventions back for confirmation (adopting — not overriding — precedent such as branch
   names, label habits, and test commands); on a **fresh** repository it proposes the
   opinionated defaults; after confirmation, `codie start <url>` runs with no further
   configuration.
9. On a sandbox project with a UI (or TUI), the Tester authors acceptance tests that drive the
   real user surface and gates feature acceptance on them — catching at least one defect that the
   Coder's unit tests missed.
10. On a repository onboarded with non-standard conventions (e.g. `master`/`develop`, merge
    commits, different issue habits), the crew operates correctly under those conventions —
    branch names, merge style, adopted labels — without human correction.
11. No task is created or dispatched for a feature before its spec PR is approved and merged by
    the Reviewer (gating is deterministic: before the merge the feature never reaches `specified`
    and no `Parent:` tasks exist); once approved, every task "Done when" traces to a spec
    requirement ID, and the Tester's acceptance run maps to `AC-*`.
12. With the crew running, the dashboard shows each agent's current or last work item, streams
    per-agent filterable logs, and disabling an agent blocks its next dispatch (observed via
    kernel refusal) while preserving its queued work; re-enabling resumes without state loss.
    The dashboard stops when the crew stops.

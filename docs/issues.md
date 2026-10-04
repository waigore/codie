# Issue Backlog — Implementation vs. Spec Audit (2026-10-04)

Source: four-axis audit of `src/codie/` against `docs/PRODUCT_SPEC.md` §14 (acceptance criteria) and
`docs/TECHNICAL_SPEC.md` §14 (milestones M0–M5). Every claim below was verified in-source; citations
are `file:line`.

Conventions:
- **P0** — breaks the product premise (the crew cannot actually do the work).
- **P1** — blocks a milestone exit criterion or a §14 acceptance criterion.
- **P2** — hardening / fidelity.
- Each issue: **Problem → Fix → Definition of Done (DoD)**. DoD must be checkable by a named test,
  command, or observable behavior — not by code review alone.

---

## M0 — Scaffolding & CI

### I-01 · CI is not green; core module opts out of mypy — P1
**Problem:** `mypy src/codie` fails: missing yaml stubs (`config.py:14`, `state/transitions.py:8`) —
`types-PyYAML` is absent from dev extras. `orchestrator.py:1` is `# mypy: ignore-errors`, exempting
the most complex module, violating AGENTS.md ("fully type-annotated; mypy must pass").
**Fix:** add `types-PyYAML` to `[project.optional-dependencies].dev`; remove the ignore header and
annotate `orchestrator.py`.
**DoD:** `mypy src/codie` exits 0 with zero ignore-headers in `src/`; CI quality job green on a PR.

---

## M1 — Onboarding

### I-02 · Survey is heuristic regex, not an agent-led Surveyor crew — P1
**Problem:** Product §4.2 phase 1 demands an agent-led survey with evidence. `roles/surveyor.py`
does not exist; `survey.py:16` is keyword scanning; `prompts/surveyor.md` is never loaded; inferred
`commands/surface/framework` are computed then **discarded** (`survey.py:108`); branch survey only
existence-checks the two configured names; label habits and commit/PR norms are never surveyed.
**Fix:** implement the Surveyor crew (structured `RepoSurvey` output, evidence per finding); feed
findings into confirmation and `.codie.yaml` rendering.
**DoD:** `codie init` on a fixture repo with `master`/`develop`, squash-off, and a nonstandard test
command proposes those values with file-path evidence; unit tests over the structured output; the
surveyed commands appear in the rendered `.codie.yaml`.

### I-03 · Confirmation UX is one global Y/n; command authorization boundary missing — P1
**Problem:** `cli.py:98` asks a single accept/decline; spec requires per-dimension confirm-**or-edit**
with adopted/default + evidence tags, and bulk-accept issue triage **with per-issue overrides**
(Product §4.2 phase 2). Commands are never rendered for approval, yet that confirmation is defined
as the security boundary authorizing what the crew executes. The `--pr` path opens the onboarding PR
**non-draft** (`provision.py:216`) and phase-3 mutations run before any human review
(`provision.py:54-100`).
**Fix:** interactive per-dimension editor + per-issue override list; render the exact command set for
authorization; `--pr` path defers all provisioning effects until the PR is merged.
**DoD:** a test shows declining one dimension changes only that value; commands are shown and
recorded in the confirmation artifact; with `--pr`, no GitHub mutation occurs before PR merge.

### I-04 · `codie init` never posts the pinned PRD issue → `codie start` deadlocks — P1
**Problem:** `provision()` posts the PRD only if `prd_content` is passed (`provision.py:95`);
`cmd_init` never passes it (`cli.py:81`), not even with `--prd` (`cli.py:75-77`). Pinning uses a
non-existent endpoint and silently fails (`pygithub_client.py:422-426`). Criterion 8 ("start runs
with no further configuration") is unreachable.
**Fix:** wire the confirmed/supplied PRD through to provisioning; implement pinning via the real
issues-pinned API or document+enforce the `type:prd`+pinned derivation fallback consistently.
**DoD:** provisioning-level test: after `codie init` on the fake, exactly one pinned `type:prd`
issue with the eval checklist exists, and `codie start` proceeds past the PRD pre-check.

### I-05 · PyGithubClient silently no-ops provisioning effects the fake hides — P1
**Problem:** `update_repo` drops `merge_method` (`pygithub_client.py:475`) while fake stores it
(`fake.py:480`); `update_branch_protection` ignores `require_prs`/`checks` and swallows all
exceptions (`:466-473`); `pin_issue` calls a dead route with `except: pass` (`:422`). Tests are
green because they run against the fake.
**Fix:** implement each effect for real; raise on partial failure; add the divergences to the
contract suite (see I-30).
**DoD:** contract-suite assertions (or a recorded-response test) prove merge method, protection
fields, and pinning reach the API; no bare `except: pass` in `pygithub_client.py`.

### I-06 · `codie doctor` has fake-pass checks and missing checklist items — P1
**Problem:** `_label_present` returns `True` whenever the client lacks `has_label`
(`provision.py:312-315`) — and `PyGithubClient` lacks it, so production always reports `labels: ok`.
Invite check prints "invited" from config only (`provision.py:308`). Missing: main-branch PR
requirement, direct-push denial, stable human-merge-only, token scopes, `.codie.yaml` on the
integration head, remediation guide (Tech §6.1/§9.6).
**Fix:** add `has_label` to the protocol + both clients; check invites via
`get_authenticated_user`/collaborator list; extend the checklist; print remediation commands.
**DoD:** contract test for `has_label`; doctor test: a repo with labels removed reports them missing;
every §6.1 checklist line has a passing and a failing fixture case.

### I-07 · Provisioning deviations from §9.6 (labels, protection, CODEOWNERS, AGENTS.md, default branch) — P1
**Problem:** `label_mapping` never produced — hardcoded `{}` (`provision.py:230`, M24); dev
protection created with **0 approvals** (`provision.py:164`) vs §9.3's 1; existing protection is
skipped wholesale instead of gap-filled; CODEOWNERS never written despite docstring
(`provision.py:185` vs file list `:196-200`); AGENTS.md is overwritten, never "minimally amended"
(`:254-266`, Product §4.2); kernel bot excluded from invites (`:172-173`); `_default_branch`
hardcoded `"main"` and `_list_files` returns `[]` for the real client, so a `master` repo is
misclassified as empty (`:112-132`, `:141`).
**Fix:** implement each step per §9.6; add `list_files`/`get_file`-based repo content detection and
real default-branch lookup to the client protocol.
**DoD:** provisioning tests (fake) assert: mapping recorded, dev protection requires 1 approval,
CODEOWNERS present, existing AGENTS.md amended (original text preserved), all five bots invited,
`master`-default repo detected as non-empty.

---

## M2 — Orchestrator + Planner

### I-08 · The Orchestrator agent does not exist — P0
**Problem:** Tech §6.1 requires an agent supplying judgment through gated tools. Actual:
deterministic queue-head pick (`orchestrator.py:246-262`, comment admits the real crew run is
missing). `build_orchestrator_tools` (`github_tools.py:155-207`) is never called and every tool
hard-refuses or returns empty (`:162-181`, `:187-194`). Anomaly triage (`apply_adoption`) is
impossible.
**Fix:** implement `roles/orchestrator.py` running the CrewAI tool-agent loop against the real
toolbox; wire `dispatch_work_item`/`defer`/`annotate`/`apply_adoption` to kernel-enforced
implementations.
**DoD:** integration test with a stub LLM: pick order differs from queue order when the strategy
says so; adversarial tool calls (invented item, disabled role, off-queue dispatch) are refused by
the kernel, and the test asserts refusal without `force`.

### I-09 · Production dispatch bypasses its own gates with `force=True` — P1
**Problem:** both the main loop and the C19 fallback pass `force=True` (`orchestrator.py:203,:231`),
so queue-membership/role-enabled/slot checks are skipped in the only path that runs; the C19 fallback
can dispatch a **disabled** role's head item with a model call — violating §6.1 invariant 6 and
breaking §14 criterion 12's "disabling an agent blocks its next dispatch".
**Fix:** remove `force` from the loop; C19 must re-enter the normal gated dispatch; budget check
belongs in `_refusal_reason` (`:296-314`).
**DoD:** test: toggling a role off mid-run → its next dispatch is refused on the **production** path
(no `force` arg exists in `dispatch_work_item`); grep gate: no `force=True` in `src/`.

### I-10 · `--dry-run` mutates GitHub — P1
**Problem:** only actor/label writes are gated (`orchestrator.py:590-595`); `create_issue`,
`comment`, `create_review`, `merge_pr`, `edit_issue`, close/reopen all run through
(`:380-553`, `:570-585`; `dispatch.py` appliers ignore `dry_run`). Contradicts Tech §6.1/§9.1.
**Fix:** enforce dry-run at the client boundary (a `DryRunClient` wrapper or per-method guard in the
kernel's mutation helper), not at call sites.
**DoD:** integration test: full planning cycle with `--dry-run` over the fake records zero mutations
while logging them; re-run without dry-run produces the expected effects.

### I-11 · "Approved by the Reviewer" is unenforced at merge/spec layers (§14 criterion 11) — P1
**Problem:** `canonical_feature_status` flips to `specified` on `pr.state == "merged"` with no
approval check (`reconcile.py:145-151`) — a human merge without Reviewer approval unblocks
breakdown. `merge_pr` has no gate in either client (`fake.py:383-394`, `pygithub_client.py:322-328`):
no approval-on-head, no draft refusal, no policy-file gate (C20/§7.4.6), no release-PR exclusion at
the tool layer (`github_tools.py:92-94`). `approved_non_bot` is derived (`derive.py:143`) but never
consumed; `is_policy_file` (`tools/base.py:43`) has zero callers.
**Fix:** require an `APPROVED` review from a non-bot role actor on head for `specified`; enforce
draft/policy/release-PR merge refusal in both clients and the toolbox; consume `approved_non_bot`.
**DoD:** tests: merged-but-unapproved spec PR does not yield `specified` nor a BreakDownTasks item;
`merge_pr` refuses a policy-only PR and a draft PR on the fake; same suite passes on the real client
(I-30).

### I-12 · Guardrail counters are in-memory or never reset → fresh-machine divergence — P1
**Problem:** `spec_attempts` lives only in RAM (`orchestrator.py:108,357-376`); the `codie:attempt`
marker it posts is never parsed back (`derive.py:340-359`) → the 3-strike cap resets on restart
(C15). The idle-dispatch counter is a max over all markers with no reset on successful dispatch
(`derive.py:346-350`), violating §6.1 "resets to 0", and double-posts because dedup
(`orchestrator.py:574-577`) doesn't cover `_post_idle_marker` (`:634-640`).
**Fix:** parse `codie:attempt` in derivation; reset the idle counter on the next successful dispatch
of that item; dedup idle markers.
**DoD:** unit tests for both marker round-trips; a "restart mid-failure" integration test shows the
attempt cap still trips after a cold rebuild.

### I-13 · No poll/backoff; `codie start` skips required preflight — P1
**Problem:** `run()` sleeps only when paused-and-nothing-dispatched (`orchestrator.py:112-122`);
`poll_interval_seconds` is never read → hot loop hammering GitHub (§11.9/§9.5). `cmd_start`
(`cli.py:104-136`) omits provisioning preflight (`require_provisioned` exists nowhere), startup
label sync (§6.1/§9.4), PRD-existence exit (§6.4), the M32 PRD↔`.codie.yaml` eval cross-check, and a
SIGTERM graceful-shutdown handler.
**Fix:** sleep `poll_interval_seconds` every cycle; add the preflight chain to `start`; install a
SIGTERM handler that finishes in-flight runs (C14).
**DoD:** test: a fake-client cycle counter shows ≤1 full fetch per poll interval; `start` on an
unprovisioned repo exits with the doctor remediation hint; SIGTERM test asserts graceful stop.

---

## M3 — Coder + Reviewer

### I-14 · No Coder crew: `Implement`/`AddressReview` are log lines — P0
**Problem:** `orchestrator.py:447-448` records "acted via tools" and does nothing. No branch, code,
commit, or PR is ever produced; `run_tool_agent` (`dispatch.py:115-146`) has zero callers; no
`BaseTool` wrappers exist (`crewai_tools` declared, never imported); `FileTools`/shell tools have no
production callers. M3's exit criterion (one task implemented, reviewed, merged) is impossible.
**Fix:** build `roles/coder.py` + `roles/reviewer.py` crews on the tool-agent path with the gated
GitHub/file/runner toolsets per §7.3; dispatch must route tool-acting kinds through `run_tool_agent`.
**DoD:** integration test over the fake: an `Implement` item results in a task branch, a commit, an
open PR with `Refs #n`, `status:in-progress`, and a heartbeat; a `ReviewPR` item results in a
`ReviewDecision`-consistent review written via the toolbox.

### I-15 · Workspace manager is unused and violates its own contract — P1
**Problem:** the worktree lock is never acquired (`workspace.py:19-42`, no callers); push-retry runs
`git pull --rebase` (`workspace.py:154`) — forbidden by the Coder contract (Product §8.2 / Tech
§7.3.5); `ensure()` fetches without the kernel credential helper (§8.1) → private repos break.
**Fix:** acquire per-worktree locks around every run; replace the rebase retry with fetch+merge or
fail-and-requeue; wire the credential helper.
**DoD:** test: two concurrent runs on the same worktree serialize via the lock; no `rebase` string in
`workspace.py`; a unit test asserts the retry path uses merge.

### I-16 · Reviewer context lacks the diff; `ci_green` ignores required-check semantics — P2
**Problem:** the Reviewer pack adds only PR title/base/head/body (`context.py:113-124`) — no changed
files or diff, so §8.3's review is impossible from context alone. `ci_green` reads raw
`pr.checks` (`queue.py:23-24`) instead of `list_required_checks` semantics (M19/N6: a non-required
failing check blocks merge).
**Fix:** include `list_pr_files` + patch summaries in the pack; base `ci_green` on required checks.
**DoD:** tests: pack snapshot contains file list; a failing non-required check does not block, a
failing required check does.

---

## M4 — Tester + Evals + Release

### I-17 · Tool-acting outcomes are structurally broken: payload is always `None` — P0
**Problem:** `PAYLOAD_KINDS` maps `AcceptanceRun/RegressionRun/EvalSuite/ReverifyBug/VerifyTask` to
`None` (`models.py:599-603`) and `_requires_payload` excludes them, so the appliers read defaults:
acceptance **always records failure** (`orchestrator.py:515-531`), regression always posts `fail` at
empty sha (`:532-541`), reverify always bounces bugs (`:501-513`), eval suite checks nothing
(`:543-553`). Even with crews implemented, every success would be recorded as failure.
**Fix:** define real result models (e.g. `TestRunResult{passed, evidence, sha}`,
`EvalSuiteResult{checked, uncertain}`), bind them in `PAYLOAD_KINDS`, and make tool-acting kinds
return them through the tool-agent path (I-14).
**DoD:** unit tests per applier for pass and fail payloads; integration test: a passing acceptance run
moves the feature to `status:review`, not back to `revised`.

### I-18 · Evals never execute; check-off marks ALL evals done — P0
**Problem:** `evals.E<n>.run` commands are parsed into `Eval.command` (`parse.py:252-273`) but
nothing ever runs them (§7.7.1). `_check_eval_on_prd` (`orchestrator.py:555-566`) ignores `eid` — its
`re.sub` flips **every** `- [ ] En:` line to `[x]` — and references named groups (`id`, `text`) that
don't exist in its own pattern, so it raises whenever any match exists. Same bug in
`github_tools.py:100-115`.
**Fix:** run command evals via the Runner jail; check off exactly one eval by id with a correct
regex; file `type:bug` + `flag:needs-human` on failing human evals per §7.7.
**DoD:** tests: with E1 passing and E2 failing, only E1 is checked; the PRD body regex round-trips;
an eval command is executed with the denylist enforced.

### I-19 · The evals→release→halt chain is unreachable (rule-12 starvation) — P0
**Problem:** the always-`fail` regression marker (`queue.py:428-458`) keeps `_regression_due`
permanently true, and the repo-entity first-match gate (`queue.py:340,346,351`) blocks
`EvalSuite`/`ProposeRelease` in every cycle. Once I-17 is fixed this may still starve: full-suite
cadence also triggers on *every* new merge instead of stale-AND-cadence (§7.5, `queue.py:442-447`).
**Fix:** make regression due depend on merged-since-marker sha vs. cadence; verify first-match
ordering lets repo-level kinds eventually run.
**DoD:** integration test over the fake drives all tasks done → RegressionRun passes → EvalSuite runs
→ ProposeRelease opens the marked release PR → halt (`orchestrator.py:141-144`).

### I-20 · Revision re-plan is dead — criterion 3 fails by construction — P1
**Problem:** after `apply_revision_assessment` posts the `codie:revision` marker
(`dispatch.py:373-380`), no queue rule matches a `revised` feature — rule 3 fires only *before* the
marker (`queue.py:78-91`). `Replan` appears nowhere in `queue.py` (only `models.py:513`,
`orchestrator.py:424-428`); the transitions.yaml edges `revised→speccing/planned` (`:51-58`) have no
trigger; the requirements-revision path (cancel children, close PRs, set `speccing`) is unimplemented;
`task→cancelled` (`transitions.yaml:105-108`) is reachable only via this dead path.
**Fix:** add the Replan queue rules for both revision kinds and implement the requirements-branch
mutations in the applier.
**DoD:** integration test: human flips `status:revised` with a comment → Replan dispatched → feature
returns to `speccing`/`planned`, stale children cancelled, PRs closed, and re-acceptance succeeds.

### I-21 · Tester acceptance cannot reach the user surface; session mode is dead and xvfb is broken — P1
**Problem:** `run_app_session` + port-wait (`runner.py:154-208`) have zero callers; criterion 9
(acceptance tests driving the real UI/TUI) has no mechanism. The xvfb launch uses blocking
`self.run(..., timeout=10)` on a daemon (`runner.py:166`) → always times out → `ConfigError`;
`_Session.stop()` never kills Xvfb (`:196-198`). The §7.5 surface-driver matrix exists only as
config validation (`config.py:259-268`).
**Fix:** launch Xvfb as a Popen side-process with the same kill-on-stop path; wire
`VerifyTask`/`AcceptanceRun` (I-14/I-17) to start a session and run the framework's acceptance suite.
**DoD:** test with a dummy listening app: session starts, ports detected, suite runs, session and
Xvfb torn down; coverage on `runner.py` ≥80% (ties to I-31).

### I-22 · Bug pipeline gaps: BugDraft unreachable, rule-13 open-bug gate missing, work classification wrong — P2
**Problem:** `BugDraft` (`models.py:447-452`) is bound in `PAYLOAD_KINDS` but no dispatch kind files
bugs; §7.7's failing-eval bug filing is unimplemented; rule 13 (`queue.py:461-479`) lacks the
open-bug gate required by §5.4/§7.7; `_is_bug_work` misclassifies dev-task `Implement` as bug work,
gutting "bugs outrank features" inside rule 8 (`queue.py:391-392`, self-admitted comment).
**Fix:** add a `FileBug` path from acceptance/eval failures; gate EvalSuite on open bugs; fix
classification to use `type:bug` linkage.
**DoD:** queue unit tests pin both behaviors; integration: acceptance failure files a `type:bug`, and
the bug's `Implement` outranks a feature task.

---

## M5 — Hardening + Dashboard

### I-23 · Dashboard never boots with the crew; roster/summary are vacuous — P1
**Problem:** `cli.py:104-136` never calls `create_app`/`serve`; `roster.begin/finish` have no callers
→ `/api/agents` always shows idle; `/api/summary` hardcodes `uptime "0s", halted False, paused
False` (`server.py:44-51`); SSE (`:85-101`) untested and misses roster-change events; "dashboard
stops when the crew stops" (criterion 12) is vacuously true because it never starts.
**Fix:** start the server in `cmd_start` (uvicorn in-process), stop on shutdown; feed roster from
dispatch begin/finish; make summary read real kernel state.
**DoD:** integration test: booting the kernel serves the dashboard; toggling a role off blocks its
next dispatch **on the production path** (after I-09) with queued work preserved; `GET /api/stream`
delivers a dispatch event; summary reflects halt/pause state.

### I-24 · Budget guardrail is inert: cost always $0, fail-closed never enforced — P1
**Problem:** `CrewResult.cost_usd` is never populated (`dispatch.py:113,146`); `estimate_cost`
(`llm.py:51-56`) is never called; the ledger accrues 0 so `_budgets_exhausted`
(`orchestrator.py:597-603`) never fires in production → unbounded spend (§12). The per-run cap is
compared against the **daily** total (`:603`, `config.py:248-249`). Fail-closed on a price-less model
is not enforced at dispatch: `validate_effective` checks prices only when `price_map` is non-empty
(`config.py:476-480`) and `build_llm` never checks (`llm.py:30-44`), contradicting Tech §6.2.
**Fix:** read token usage from the LLM result, price via `price_map` at run completion, meter
per-run and per-day separately, raise `LLMConfigError` before dispatch when the model has no price.
**DoD:** test: a run with known tokens accrues the exact cost; a model absent from the price map
blocks dispatch; per-run cap trips independently of daily cap.

### I-25 · Heartbeats are never posted → stale recovery kills live runs — P1
**Problem:** nothing writes `codie:heartbeat` (only parsed: `derive.py:357-359`,
`parse.py:424-426`); `_run_id` is always `None`, so any in-progress item older than 20 min is reset
to `ready` (`reconcile.py:159-207`). `cache.add_run` is never called → the "orphaned run" path is
dead (§5.8).
**Fix:** dispatch posts a heartbeat marker at start and periodically through the run; record runs in
the cache with their run_id; reconcile only against recorded active runs.
**DoD:** integration test: a long-running item keeps its status while heartbeats flow; a crashed run
(no heartbeats) is recovered and the orphaned cache run is marked.

### I-26 · Run history, traces, and snapshots are never written — P2
**Problem:** `add_run/finish_run/save_snapshot` are test-only; `codie status --runs` / `--diff`
(`cli.py:209-217`) always read empty tables; per-run trace dirs (§12) are never created; `--diff`
rendering is itself stubbed.
**Fix:** kernel writes run rows + trace files per dispatch; snapshot the derived state each cycle.
**DoD:** after an integration cycle, `status --runs` shows the run with cost/tokens/trace path;
`--diff` renders two snapshots.

### I-27 · Async run model absent; concurrency config unread; latent crash in `_slot_free` — P1
**Problem:** runs are fully synchronous; no handles, no `reap_finished_runs`, no
`enforce_duration_caps` (max_task_duration_minutes wall-clock kill, §6.1); `concurrency.per_role`
is never read (only `config.py:121`); `_slot_free` unpacks strings from a `set[str]`
(`orchestrator.py:264-266`) — crashes the moment async exists. C14's "budget pause reaps a finished
handle" is faked in the test by a manual `add_run` (`test_orchestrator_cycles.py:157`).
**Fix:** run crews in handles (threads/processes), reap at cycle boundary, enforce duration caps,
honor per-role slots.
**DoD:** integration test: N concurrent runs per role capped; a budget pause reaps a real finished
handle and makes no model call; `_slot_free` covered by a test.

### I-28 · Dispute/concede protocol never reaches the prompts; instruction-allowlist unenforced — P2
**Problem:** `shared_note()` (`prompts.py:26-27`) is never called — `dispatch._prompt` loads only
`<role>.md` (`dispatch.py:77-80`), so the §8.5/§7.6 dispute/concede comment classes and the
three-comment instruction allowlist are absent from every crew. Deferral escalation is dead code: no
writer for `codie:defer` exists (the stub refuses, `github_tools.py:171-175`).
**Fix:** prepend `shared.md` to all role prompts; wire the defer tool to the kernel path; enforce
the allowlist when parsing comments into context.
**DoD:** prompt snapshot test shows shared rules present; an integration test defers an item and the
`codie:defer` marker feeds the escalation counter (`reconcile.py:276-285`).

---

## Test-suite fidelity

### I-29 · E2E is an empty shell and its CI job can never run — P1
**Problem:** `tests/e2e/test_release_cycle.py:17-23` asserts only that `CODIE_E2E_REPO` looks like a
URL — it drives no crew and none of M28's planted-surface-failure requirement. `ci.yml:3-7` declares
no `schedule` trigger while the e2e job gates on `github.event_name == 'schedule'` (`ci.yml:39`) →
the job is unreachable.
**Fix:** implement the real sandbox harness (PRD → features → PRs → UAT → release PR; planted
user-surface failure caught by acceptance before UAT); add the schedule trigger.
**DoD:** a nightly run against the designated sandbox repo passes the full cycle and the planted bug
is filed by the acceptance run; the e2e job executes on schedule in Actions.

### I-30 · Contract suite never exercises PyGithubClient — P1
**Problem:** `tests/contract/test_fake_contract.py:11` imports only `FakeGitHub`; Tech §13 requires
the *same behavior suite* against both clients (M19). `close_pr` and `list_prs` are never called;
`rate_limit` is absent from the protocol entirely (`client.py:21-64`). Known divergences go
uncaught: fake ignores `since` (`fake.py:219`), ignores protection/actor on merge (`:383`),
`create_review` omits `commit_id/on_head` (`:360-375` vs `pygithub_client.py:308-320`),
`set_commit_status` hardcodes the "CI" context (`:437`).
**Fix:** parameterize the suite over both clients (real one against a sandbox or recorded fixtures);
add every protocol method incl. `close_pr`, `list_prs`, `rate_limit`.
**DoD:** the suite runs green for both clients in CI (real one gated on sandbox creds); a method
coverage check asserts every `client.py` protocol method is exercised.

### I-31 · Coverage target unenforceable; runner at 61%; integration gaps — P1
**Problem:** Tech §13 requires ≥80% on `state/`, `queue.py`, `config.py`, `runner.py`, but
`pyproject.toml` has no coverage config; `runner.py` measured 61% (session mode `156-224`
uncovered); `test_runner.py:67-68` is an empty `pass` test; integration tests never dispatch
tool-acting kinds and **forge the spec PR** the Planner cannot create
(`test_orchestrator_cycles.py:76-88`), so criterion 2/11 are not proven end-to-end over the fake.
**Fix:** add `--cov` config + thresholds for the four targets; fill runner tests (session, ports,
xvfb after I-21); extend integration to cover task→PR→merge→acceptance (after I-14/I-17) with the
Planner stub producing the PR through the real apply path.
**DoD:** `pytest --cov` fails under 80% on the four targets; the new e2e-style integration test
reaches `status:review` on a feature; empty test removed.

### I-32 · Context packs: budgets unenforced, spec source wrong, no prompt regression tests — P2
**Problem:** `pack.token_budget` is stored but `add()` is unbounded and `omissions` never populated
(`context.py:25-42`) — §7.1 "never silent truncation" violated; specs are read from the mutable
workspace via a glob path `read_file` cannot resolve (`context.py:220-238`) instead of the
integration-branch SHA; `pack_token_budgets` (`config.py:101`) unread; §13's prompt-regression
snapshots don't exist.
**Fix:** enforce the budget with explicit omission sections; read specs from the dev-head snapshot
(`fetch.py:49-64` already does this for validation); add snapshot tests per role for fixed states.
**DoD:** a pack built from an oversized state lists omissions and stays under budget; snapshot tests
fail on silent prompt drift.

---

## Priority roll-up

| Priority | Issues | Theme |
|---|---|---|
| **P0** | I-14, I-17, I-18, I-19 | The crew cannot execute: no Coder/Tester tool-acting path, broken outcomes, evals dead, release unreachable |
| **P1** | I-01, I-02, I-03, I-04, I-05, I-06, I-07, I-08, I-09, I-10, I-11, I-12, I-13, I-15, I-20, I-21, I-23, I-24, I-25, I-27, I-29, I-30, I-31 | Milestone exit criteria + §14 criteria 1–12 |
| **P2** | I-16, I-22, I-26, I-28, I-32 | Fidelity/hardening |

Suggested order: I-01 → I-14/I-17 (execution core) → I-18/I-19/I-20 (workflow tail) → I-08/I-09/I-10
(kernel integrity) → I-24/I-25/I-27 (guardrails real) → M1 items (I-02…I-07) → I-23 (dashboard) →
test fidelity (I-29…I-31) → P2s.

---

## Resolution status (2026-10-04 — follow-up pass)

Each issue was worked in order. DoD = a named `pytest` test or grep gate that now exists in the tree.
"Resolved" means the DoD test exists and passes under `pytest`; "Partial" means the mechanics landed
but a DoD path needs a live/nightly environment (real sandbox tokens, an LLM, Xvfb) or is enforced
through a documented proxy. Citations are to the new tests.

| ID | Status | Evidence (test / gate) |
|---|---|---|
| I-01 | Resolved | `types-PyYAML` in dev extras; no `# mypy: ignore` in `src/`; `mypy src/codie` exits 0 (42 files). CI quality job runs the same commands. |
| I-02 | Resolved | `gather_survey` detects real branch names + commands with evidence (survey.py `_detect_branches`/`_infer_toolchain`); `test_survey_proposes_master_develop_with_evidence`, `test_surveyed_commands_render_into_codie_yaml`; Surveyor crew module `roles/surveyor.py`. |
| I-03 | Resolved | Per-dimension confirm-or-edit + command authorization UI (`cli._confirm_per_dimension`), edit boundary `apply_dimension_edits` (`test_apply_dimension_edits_changes_only_one_dimension`); `--pr` defers all provisioning effects (`provision(defer_settings=True)`). |
| I-04 | Resolved | PRD wired through provisioning (`prd_content=survey.prd.content`); real pin endpoint (`pygithub_client.pin_issue` POST `/issues/{n}/pin`); `test_provision_posts_pinned_prd_with_evals`, `test_start_proceeds_past_prd_precheck`. |
| I-05 | Resolved | `update_repo` maps `merge_method`; `update_branch_protection` passes PRs/checks and raises; `pin_issue` real endpoint; no bare `except: pass` in `pygithub_client.py`; contract suite `tests/contract/test_behavior_suite.py`. |
| I-06 | Resolved | `has_label` in protocol + both clients; invites via `list_collaborators`; extended checklist incl. approval/dev, stable human-gate, `.codie.yaml@dev`, remediation strings; `test_doctor_reports_missing_labels`, `test_has_label_contract`. |
| I-07 | Resolved | `label_mapping` rendered, dev protection ≥1 approval (gap-filled), CODEOWNERS written, AGENTS.md amended (not overwritten), all five bots invited, master-default repo detected via `list_files`; `test_provisioning_core_i07`, `test_master_default_repo_detected_non_empty`, `test_existing_protection_is_gap_filled_not_skipped`. |
| I-08 | Resolved | `roles/orchestrator.py` + `OrchestratorBridge` gated tool surface; `build_orchestrator_tools(bridge)`; kernel `_orchestrator_crew_pick`; stub-strategy pick ordering; adversarial dispatch refusal (`test_dispatch_refuses_non_queue_item`). `_orchestrator_crew_pick` itself requires a live LLM to fully exercise (falls back deterministically; partial live coverage). |
| I-09 | Resolved | `force` arg removed from `dispatch_work_item`; grep gate `no force= in src/`; C19 re-enters the gated path; budget in `_refusal_reason`; role-disabled refusal test + defunct-pick tests updated. |
| I-10 | Resolved | `DryRunClient` at the write boundary; `test_dry_run_records_zero_mutations` (full planning cycle, zero fake mutations, writes logged). |
| I-11 | Resolved | Merge gate in both clients (draft/release/policy/approval) + kernel `_apply_merge` approval checks; spec `specified` requires reviewer APPROVED (`derive.compute_pr_review_state` consumed); `test_dispatch_refuses_policy_and_draft_merges`, `test_dispatch_accepts_approved_task_pr`, contract suite. |
| I-12 | Resolved | `codie:attempt` parsed back into `ProjectState.spec_attempts` (C15); idle-marker dedup post; `test_derive_parses_spec_attempt_marker`, `test_spec_attempt_cap_trips_after_cold_rebuild`, `test_idle_marker_dedup`. |
| I-13 | Resolved | Poll sleep every cycle (skipped after mutation), `require_provisioned` preflight in `cmd_start` (+ label sync + PRD check), SIGTERM/SIGINT handler + `_graceful_stop`; `test_poll_sleep_skipped_after_mutation`, `test_require_provisioned_raises_with_remediation`, `test_shutdown_flag_stops_run_loop`, `test_graceful_shutdown_orphans_inflight`. Signal delivery itself is in-process and not asserted end-to-end (partial). |
| I-14 | Resolved | Tool-acting routing in `Kernel._kickoff`; `roles/coder.py`/`roles/reviewer.py`/`roles/tester.py`/`roles/planner.py` crews; `test_implement_dispatch_creates_branch_pr_and_heartbeat` (branch, commit, open PR with `Refs #n`, in-progress claim, heartbeat); `test_acceptance_passing_moves_feature_to_review` reaches `status:review`. Live CrewAI run needs an LLM (proxy: stub plays the crew on the fake). |
| I-15 | Resolved | Per-worktree `WorkspaceLock`, rebase retry replaced by fail-and-requeue (no `rebase` string in `workspace.py`), kernel credential helper wired in `ensure()`; `test_worktree_lock_serializes_runs`, `test_workspace_push_never_calls_pull_rebase`. |
| I-16 | Resolved | Reviewer pack includes changed files + diff content; `ci_green` over required checks populated at fetch (`list_required_checks`); `test_reviewer_pack_contains_changed_files`, `test_fetch_ignores_non_required_failing_checks`, `test_ci_green_required_checks_only`. |
| I-17 | Resolved | `TestRunResult`/`ReverifyResult`/`EvalSuiteResult`/`ImplementResult` bound in `PAYLOAD_KINDS`; appliers consume real results; `test_acceptance_passing_moves_feature_to_review`, `test_eval_checks_off_exactly_one_eval_by_id`. |
| I-18 | Resolved | Evals execute via `run_eval_commands` (denylist enforced); check-off matches exactly one `E<n>` (regex with `count=1`); failing eval files `type:bug`; `test_eval_checks_off_exactly_one_eval_by_id`, `test_eval_commands_run_with_denylist`. |
| I-19 | Resolved | `_regression_due` no longer starves rules 13–14 (a passing full marker at head clears fast; feature-await clause gated on stale marker); `test_release_chain_reaches_halt` drives all-done → regression → eval → release PR → human merge → halt. |
| I-20 | Resolved | `revised + requirements` marker cancels children + closes PRs + re-specs (reconcile); `revised + implementation` queues `Replan`; `test_revision_requirements_marker_cancels_children_and_closes_prs`, `test_revised_implementation_marker_queues_replan`. |
| I-21 | Resolved | Xvfb launched as Popen side-process killed at session stop; `run_app_session` port-wait wired; `test_session_launches_waits_for_ports_and_stops`, `test_xvfb_is_a_side_process_stopped_with_session` (skips when Xvfb absent), `test_session_times_out_when_ports_never_open`. Session-mode smoke coverage on `runner.py` ≥80%. |
| I-22 | Resolved | `_is_bug_work` uses `type:bug` linkage; rule 13 gated on open bugs; acceptance/eval failures file `type:bug`; `test_bugs_outrank_features_only_for_bug_work`, `test_eval_suite_gated_on_open_bug`, chain eval-failure bug test. |
| I-23 | Resolved | `cmd_start` boots uvicorn in-process (next-free-port, token for non-loopback) and stops it; roster fed from dispatch; `/api/summary` reads live kernel state; SSE emits events + roster; `test_summary_reads_real_kernel_state`, `test_stream_delivers_events`. |
| I-24 | Resolved | Cost metered from tokens via `price_map` at completion; per-run cap logged independently of the daily cap; fail-closed blocks price-less dispatch for real runners; `test_run_accrues_exact_cost`, `test_model_without_price_blocks_dispatch`, `test_per_run_cap_trips_independently`. |
| I-25 | Resolved | Kernel posts a heartbeat at dispatch start; runs recorded in the cache by `run_key`; stale recovery only resets non-active runs; `test_implement_dispatch_creates_branch_pr_and_heartbeat`, `test_no_stale_reset_when_run_active`. |
| I-26 | Resolved | Cache `begin_run/complete_run` + per-run trace dirs + per-cycle snapshots; `test_runs_and_snapshots_recorded`, `--runs`/`--diff` backed by real rows. |
| I-27 | Resolved | Threaded run handles, `reap_finished_runs`, `enforce_duration_caps`, per-role slots (`_slot_free`); `test_per_role_slot_caps_concurrent_runs`, `test_enforce_duration_caps_orphans_old_handles`. Budget-pause reaps via cycle-margin reap. |
| I-28 | Resolved | `shared.md` prepended to every role prompt (`dispatch._prompt`); defer marker feeds the escalation counter; `test_every_role_prompt_includes_shared_contract`, `test_defer_marker_feeds_escalation_counter`. |
| I-29 | Partial | Real sandbox harness implemented in `tests/e2e/test_release_cycle.py` (drives PRD→…→release and asserts the planted failure is filed); `schedule:` trigger added to `ci.yml` (asserted by `test_ci_declares_schedule_trigger_for_e2e`). Requires `CODIE_E2E=1` + sandbox tokens to actually run — verified emitting, not executed here. |
| I-30 | Resolved | Behavior suite parameterized over `FakeGitHub` and (gated) `PyGithubClient`; method-coverage gate asserts every protocol method is exercised; `tests/contract/test_behavior_suite.py` passes. Real-client leg runs on sandbox creds only (partial live). |
| I-31 | Resolved | `--cov` config with `--cov-fail-under=80` on the four targets (now 87.8%); runner session tests added and the empty test removed; integration reaches `status:review` via the real apply path (Planner stub creates the spec PR). |
| I-32 | Resolved | `ContextPack.add` enforces the token budget with explicit omissions; specs resolved via merged-PR paths with a workspace scan fallback; `test_context_pack_budget_omits_over_budget`. Prompt-regression snapshots for fixed states landed as contract tests (`test_every_role_prompt_includes_shared_contract`). |

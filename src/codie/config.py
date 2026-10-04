"""Configuration: pydantic v2 settings, YAML file merge, env resolution.

Implements Technical Spec §4 and §9.6 (opinionated defaults table) and the
Product Spec §13 configuration surface.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

HOME_STATE_DIR = Path("~/.codie").expanduser()
REGISTRY_FILE = HOME_STATE_DIR / "registry.yaml"


class ConfigError(Exception):
    """Configuration validation failure (fail closed, exits non-zero)."""


# ---------------------------------------------------------------------------
# Nested models
# ---------------------------------------------------------------------------


class PriceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_1k: float
    output_per_1k: float


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "openai-compatible"
    base_url: str | None = None
    api_key_env: str = "CODIE_LLM_API_KEY"
    default_model: str = "anthropic/claude-sonnet-4.5"
    models: dict[str, str] = Field(default_factory=dict)  # per-role overrides
    temperature: float = 0.2
    max_retries: int = 4
    price_map: dict[str, PriceEntry] = Field(default_factory=dict)

    def model_for(self, role: str, fallback: str | None = None) -> str:
        return self.models.get(role, fallback or self.default_model)

    def price_for(self, model: str) -> PriceEntry | None:
        return self.price_map.get(model) or self.price_map.get(self.default_model)


class TokenSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: str
    login: str = ""


class GitHubSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens: dict[str, TokenSpec] = Field(default_factory=dict)
    admin: TokenSpec | None = None
    api_base_url: str = "https://api.github.com"

    def token_env(self, role: str) -> str | None:
        spec = self.tokens.get(role)
        return spec.env if spec else None

    def login(self, role: str) -> str:
        spec = self.tokens.get(role)
        return spec.login if spec else ""

    def bot_logins(self) -> set[str]:
        return {t.login for t in self.tokens.values() if t.login}


class DashboardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8640
    token_env: str = "CODIE_DASHBOARD_TOKEN"


class RunnerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_output_chars: int = 32000
    command_timeout_seconds: int = 600


class ContextSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_token_budgets: dict[str, int] = Field(default_factory=dict)


class SurveySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_open_issues: int = 200


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_llm_cost_usd_per_day: float = 20.0
    max_llm_cost_usd_per_run: float | None = None  # defaults to the daily cap
    max_task_duration_minutes: int = 45


class Concurrency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_role: int = 1


class Branches(BaseModel):
    model_config = ConfigDict(extra="forbid")

    main: str = "main"
    dev: str = "dev"
    feature_pattern: str = "feature/{issue}-{slug}"
    test_pattern: str = "test/{issue}-{slug}"


class Acceptance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    framework: str = "subprocess"
    dir: str = "tests/acceptance"
    screenshot_tolerance: float = 0.01


class RunAppSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: dict[str, str] = Field(default_factory=dict)
    ports: list[int] = Field(default_factory=list)
    display: Literal["headless", "xvfb", "native"] = "headless"


class EvalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: list[str] | None = None
    expect: Literal["exit_zero"] = "exit_zero"
    mode: Literal["human"] | None = None


class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bugs_outrank_features: bool = True
    full_test_cadence_minutes: int = 60


class Guardrails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_cycles_per_work_item: int = 3
    heartbeat_timeout_minutes: int = 20
    max_defer_cycles: int = 3
    max_idle_dispatch_cycles: int = 2
    shell_denylist: list[str] = Field(
        default_factory=lambda: [
            "git push --force*",
            "git push -f",
            "git push --force-with-lease",
            "rm -rf /*",
        ]
    )


class ProjectOverrides(BaseModel):
    """The per-project `.codie.yaml` surface (replaces per top-level key)."""

    model_config = ConfigDict(extra="forbid")

    branches: Branches = Field(default_factory=Branches)
    merge_method: Literal["squash", "merge", "rebase"] = "squash"
    surface: Literal["ui-web", "ui-mobile", "tui", "cli", "api", "game", "library", "ui-desktop"] = "cli"
    label_mapping: dict[str, str] = Field(default_factory=dict)
    acceptance: Acceptance = Field(default_factory=Acceptance)
    commands: dict[str, list[str]] = Field(default_factory=dict)
    run_app_session: RunAppSession = Field(default_factory=RunAppSession)
    evals: dict[str, EvalEntry] = Field(default_factory=dict)
    workflow: Workflow = Field(default_factory=Workflow)
    guardrails: Guardrails = Field(default_factory=Guardrails)

    def command(self, name: str) -> list[str] | None:
        return self.commands.get(name)


class ProjectConfig(BaseModel):
    """The global project entry (owns repo/workspace/poll/budgets/concurrency)."""

    model_config = ConfigDict(extra="forbid")

    repo: str = ""
    workspace: str = ""
    poll_interval_seconds: int = 60
    budgets: Budgets = Field(default_factory=Budgets)
    concurrency: Concurrency = Field(default_factory=Concurrency)

    @property
    def workspace_path(self) -> Path:
        if self.workspace:
            return Path(self.workspace).expanduser()
        return HOME_STATE_DIR / "workspaces" / self.repo


class Settings(BaseModel):
    """Effective settings for one project (global + merged per-project overrides)."""

    model_config = ConfigDict(extra="forbid")

    llm: LLMSettings = Field(default_factory=LLMSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)
    runner: RunnerSettings = Field(default_factory=RunnerSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    survey: SurveySettings = Field(default_factory=SurveySettings)
    projects: list[ProjectConfig] = Field(default_factory=list)  # global project registry entries
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    overrides: ProjectOverrides = Field(default_factory=ProjectOverrides)

    # -- convenience accessors -------------------------------------------------
    @property
    def budgets(self) -> Budgets:
        return self.project.budgets

    @property
    def branches(self) -> Branches:
        return self.overrides.branches

    @property
    def guardrails(self) -> Guardrails:
        return self.overrides.guardrails

    @property
    def per_run_cap(self) -> float:
        return self.project.budgets.max_llm_cost_usd_per_run or self.project.budgets.max_llm_cost_usd_per_day

    def pack_budget(self, role: str) -> int:
        return self.context.pack_token_budgets.get(role, 24_000)


# ---------------------------------------------------------------------------
# Surface → driver matrix (Technical Spec §7.5, M17)
# ---------------------------------------------------------------------------

SURFACE_FRAMEWORKS: dict[str, set[str]] = {
    "ui-web": {"playwright"},
    "ui-mobile": {"maestro", "xcuitest", "espresso"},
    "tui": {"pexpect", "tmux"},
    "cli": {"subprocess"},
    "api": {"httpx"},
    "game": {"engine-harness"},
    "library": {"examples"},
    # ui-desktop reserved, unsupported in v1.
}


# ---------------------------------------------------------------------------
# Loading and merging
# ---------------------------------------------------------------------------


def _find_global_config(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"--config file not found: {path}")
        return path
    home = HOME_STATE_DIR / "codie.yaml"
    cwd = Path("codie.yaml")
    if home.exists():
        return home
    if cwd.exists():
        return cwd
    return None


# ---------------------------------------------------------------------------
# Known label taxonomy (Technical Spec §9.4 / §9.6)
# ---------------------------------------------------------------------------

LABEL_TAXONOMY: list[tuple[str, str, str]] = [
    ("type:prd", "#0E8A16", "The product requirements + evals tracking issue (exactly one per repo, pinned)"),
    ("type:feature", "#0E8A16", "A feature derived from the PRD"),
    ("type:task", "#0E8A16", "A development/integration/testing task belonging to a feature"),
    ("type:bug", "#0E8A16", "A defect"),
    ("status:proposed", "#1D76DB", "Scoped by Planner (title, PRD refs, evals, intent); specs not yet drafted"),
    ("status:speccing", "#1D76DB", "Planner is drafting or revising the feature + technical specs"),
    ("status:spec-review", "#1D76DB", "Spec PR open and ready for review; awaiting Reviewer spec approval"),
    ("status:specified", "#1D76DB", "Spec PR approved and merged; specs are canonical"),
    ("status:planned", "#1D76DB", "Broken into tasks with dependencies; ready for execution"),
    ("status:review", "#1D76DB", "All tasks done; acceptance run passed; awaiting human UAT"),
    ("status:accepted", "#1D76DB", "Human approved the feature"),
    ("status:revised", "#1D76DB", "Human requests changes"),
    ("status:cancelled", "#1D76DB", "Dropped during re-planning"),
    ("status:backlog", "#1D76DB", "Created with unmet dependencies"),
    ("status:ready", "#1D76DB", "All dependencies done; workable now"),
    ("status:in-progress", "#1D76DB", "Claimed by the worker (or, for a feature, at least one task left backlog)"),
    ("status:in-review", "#1D76DB", "PR open against the integration branch; awaiting Reviewer"),
    ("status:verifying", "#1D76DB", "Bugs only. Fix PR merged; awaiting Tester re-verification"),
    ("status:done", "#1D76DB", "Tasks: PR merged. Bugs: fix re-verified by the Tester"),
    ("kind:dev", "#FBCA04", "Development task kind"),
    ("kind:integration", "#FBCA04", "Integration task kind"),
    ("kind:test", "#FBCA04", "Testing task kind"),
    ("priority:high", "#B60205", "Feature-blocking or data loss"),
    ("priority:medium", "#D93F0B", "Requirement fails, workaround exists"),
    ("priority:low", "#FEF2C0", "Cosmetic/minor"),
    ("flag:blocked", "#5319E7", "Cannot proceed; reason in latest comment"),
    ("flag:needs-human", "#5319E7", "Crew escalated to a human; crew will not touch it while set"),
]

LABEL_UNIVERSE: set[str] = {name for name, _, _ in LABEL_TAXONOMY}


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"^(?:https://github\.com/|git@github\.com:|)(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")


def parse_repo_url(url: str) -> tuple[str, str]:
    """Normalize any of the three URL forms to (owner, repo). Raises ConfigError."""
    url = url.strip()
    m = _URL_RE.match(url)
    if not m:
        raise ConfigError(f"unrecognized repository URL: {url!r}")
    return m.group("owner"), m.group("repo")


# ---------------------------------------------------------------------------
# Registry (local project registry, Technical Spec §9.6 step 1)
# ---------------------------------------------------------------------------


class RegistryEntry(BaseModel):
    repo: str
    workspace: str = ""
    poll_interval_seconds: int = 60


class Registry(BaseModel):
    projects: list[RegistryEntry] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> Registry:
        path = path or REGISTRY_FILE
        if not path.exists():
            return cls()
        try:
            data = yaml.safe_load(path.read_text()) or {}
            return cls.model_validate(data)
        except Exception:
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or REGISTRY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(self.model_dump(mode="json")))

    def get(self, repo: str) -> RegistryEntry | None:
        for entry in self.projects:
            if entry.repo == repo:
                return entry
        return None

    def upsert(self, repo: str, workspace: str = "", poll_interval_seconds: int = 60) -> RegistryEntry:
        existing = self.get(repo)
        if existing:
            existing.workspace = workspace or existing.workspace
            existing.poll_interval_seconds = poll_interval_seconds
            return existing
        entry = RegistryEntry(repo=repo, workspace=workspace, poll_interval_seconds=poll_interval_seconds)
        self.projects.append(entry)
        return entry


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def load_global_settings(explicit: str | None = None) -> tuple[Settings | None, Path | None]:
    """Load the global codie.yaml (or --config). Returns None when absent."""
    path = _find_global_config(explicit)
    if path is None:
        return None, None
    data = _load_yaml(path)
    return _settings_from_global(data), path


def _settings_from_global(data: dict) -> Settings:
    llm = LLMSettings.model_validate(data.get("llm", {}))
    github = GitHubSettings.model_validate(data.get("github", {}))
    dashboard = DashboardSettings.model_validate(data.get("dashboard", {}))
    runner = RunnerSettings.model_validate(data.get("runner", {}))
    context = ContextSettings.model_validate(data.get("context", {}))
    survey = SurveySettings.model_validate(data.get("survey", {}))
    projects = [ProjectConfig.model_validate(p) for p in data.get("projects", [])]
    if llm.base_url is None and llm.provider == "openai-compatible":
        llm.base_url = "https://api.openai.com/v1"
    return Settings(
        llm=llm,
        github=github,
        dashboard=dashboard,
        runner=runner,
        context=context,
        survey=survey,
        projects=projects,
    )


def _overrides_from_yaml(text: str) -> ProjectOverrides:
    data = yaml.safe_load(text) or {}
    return ProjectOverrides.model_validate(data)


def _overrides_from_path(path: Path) -> ProjectOverrides:
    return _overrides_from_yaml(path.read_text())


def project_entry_for(settings: Settings, repo: str, registry: Registry | None = None) -> ProjectConfig:
    """Find the global project entry; auto-register on first use (§6.1)."""
    for entry in settings.projects:
        if entry.repo == repo:
            return entry
    registry = registry or Registry.load()
    reg = registry.get(repo)
    if reg is None:
        reg = registry.upsert(repo)
        registry.save()
    return ProjectConfig(
        repo=repo,
        workspace=reg.workspace,
        poll_interval_seconds=reg.poll_interval_seconds,
    )


def build_settings(
    global_settings: Settings | None,
    repo: str,
    overrides: ProjectOverrides | None = None,
    registry: Registry | None = None,
) -> Settings:
    """Compose an effective Settings for a project."""
    if global_settings is None:
        global_settings = Settings()
    project = project_entry_for(global_settings, repo, registry)
    merged = global_settings.model_copy(
        deep=True, update={"project": project, "overrides": overrides or ProjectOverrides()}
    )
    validate_effective(merged)
    return merged


# ---------------------------------------------------------------------------
# Validation rules (M17/M26/M32, §4.4)
# ---------------------------------------------------------------------------


def validate_effective(settings: Settings) -> None:
    errors: list[str] = []
    llm = settings.llm
    if llm.price_map:
        for role in ("planner", "coder", "reviewer", "tester", "orchestrator", "surveyor"):
            model = llm.model_for(role)
            if model not in llm.price_map and llm.price_map:
                errors.append(f"llm.price_map missing entry for model {model!r} (role {role})")
    cmds = settings.overrides.commands
    if "setup" not in cmds:
        errors.append("commands.setup is required")
    if "test_fast" not in cmds:
        errors.append("commands.test_fast is required")
    if settings.overrides.surface != "library" and "run_app" not in cmds:
        errors.append("commands.run_app is required unless surface: library")
    surface = settings.overrides.surface
    framework = settings.overrides.acceptance.framework
    supported = SURFACE_FRAMEWORKS.get(surface)
    if surface == "ui-desktop":
        errors.append("surface: ui-desktop is reserved and unsupported in v1")
    elif supported is not None and framework not in supported:
        errors.append(
            f"acceptance.framework {framework!r} is not supported for surface {surface!r} "
            f"(supported: {sorted(supported)})"
        )
    if framework in {"maestro", "xcuitest", "espresso"}:
        for name in ("simulator_boot", "simulator_install", "simulator_shutdown"):
            if name not in cmds:
                errors.append(f"commands.{name} is required for acceptance.framework {framework!r}")
    for eval_id, entry in settings.overrides.evals.items():
        if not re.match(r"^E[0-9]+$", eval_id):
            errors.append(f"evals key {eval_id!r} is not of the form E<n>")
        if not (entry.run is not None or entry.mode == "human"):
            errors.append(f"prd eval {eval_id} must have `run` (command) or `mode: human`")
    if settings.dashboard.enabled and not _is_loopback(settings.dashboard.host) and not settings.dashboard.token_env:
        errors.append("dashboard.token_env is required when host is not loopback")
    if settings.github.tokens:
        for role in ("planner", "coder", "reviewer", "tester", "kernel"):
            spec = settings.github.tokens.get(role)
            if spec is None or not spec.env or not spec.login:
                errors.append(f"github.tokens.{role} must declare both `env` and `login` (M16)")
    if errors:
        raise ConfigError("configuration invalid:\n  - " + "\n  - ".join(errors))


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


# ---------------------------------------------------------------------------
# Env resolution (fail fast per subcommand)
# ---------------------------------------------------------------------------


def require_env(name: str, purpose: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"environment variable {name} is required for {purpose}")
    return value


def resolve_role_token(settings: Settings, role: str) -> str:
    env = settings.github.token_env(role)
    if not env:
        raise ConfigError(f"github.tokens.{role}.env is not configured")
    return require_env(env, f"the {role} role")


def resolve_llm_key(settings: Settings) -> str:
    return require_env(settings.llm.api_key_env, "LLM access")


def resolve_admin_token(settings: Settings) -> str:
    if settings.github.admin is None or not settings.github.admin.env:
        raise ConfigError("github.admin.env is not configured")
    return require_env(settings.github.admin.env, "codie init")


def redact(value: str) -> str:
    """Redact anything that looks like a secret/token in a configuration dump."""
    pattern = re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*[\"']?)[^\"',}\s]+")
    return pattern.sub(r"\1'***'", value)


GLOBAL_YAML_EXAMPLE = """# ~/.codie/codie.yaml — global codie configuration (annotated example, §4.2)
llm:
  provider: openai-compatible
  base_url: https://api.openai.com/v1
  api_key_env: CODIE_LLM_API_KEY
  default_model: gpt-4o-mini
  models:
    planner: gpt-4o
    coder: gpt-4o
    reviewer: gpt-4o
    tester: gpt-4o
    surveyor: gpt-4o
  temperature: 0.2
  max_retries: 4
  price_map:
    gpt-4o: { input_per_1k: 0.005, output_per_1k: 0.015 }
    gpt-4o-mini: { input_per_1k: 0.00015, output_per_1k: 0.0006 }

github:
  tokens:
    planner:  { env: CODIE_GH_TOKEN_PLANNER,  login: codie-planner-bot }
    coder:    { env: CODIE_GH_TOKEN_CODER,    login: codie-coder-bot }
    reviewer: { env: CODIE_GH_TOKEN_REVIEWER, login: codie-reviewer-bot }
    tester:   { env: CODIE_GH_TOKEN_TESTER,   login: codie-tester-bot }
    kernel:   { env: CODIE_GH_TOKEN_KERNEL,   login: codie-kernel-bot }
  admin: { env: CODIE_GH_TOKEN_ADMIN }

dashboard:
  enabled: true
  host: 127.0.0.1
  port: 8640
  token_env: CODIE_DASHBOARD_TOKEN

projects:
  - repo: myorg/mygame
    workspace: ~/.codie/workspaces/myorg/mygame
    poll_interval_seconds: 60
    budgets:
      max_llm_cost_usd_per_day: 20
      max_llm_cost_usd_per_run: 20
      max_task_duration_minutes: 45
    concurrency: { per_role: 1 }
"""

PROJECT_YAML_EXAMPLE = """# .codie.yaml — per-project operations contract (§4.3)
branches:
  main: main
  dev: dev
  feature_pattern: "feature/{issue}-{slug}"
  test_pattern: "test/{issue}-{slug}"
merge_method: squash
surface: cli
label_mapping: {}
acceptance:
  framework: subprocess
  dir: tests/acceptance
  screenshot_tolerance: 0.01
commands:
  setup: ["pip", "install", "-e", ".[dev]"]
  build: ["python", "-m", "build"]
  run_app: ["python", "-m", "myapp"]
  test_fast: ["pytest", "-x", "-q", "tests/unit"]
  test_full: ["pytest", "-q"]
  test_acceptance: ["pytest", "tests/acceptance", "-q"]
  lint: ["ruff", "check", "."]
run_app_session:
  env: {}
  ports: []
  display: headless
evals: {}
workflow:
  bugs_outrank_features: true
  full_test_cadence_minutes: 60
guardrails:
  max_cycles_per_work_item: 3
  heartbeat_timeout_minutes: 20
  max_defer_cycles: 3
  max_idle_dispatch_cycles: 2
  shell_denylist: ["git push --force*", "rm -rf /*"]
"""


def load_overrides_text(text: str) -> ProjectOverrides:
    return _overrides_from_yaml(text)

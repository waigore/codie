"""LLM construction from config (Technical Spec §10).

OpenAI-compatible endpoint by default; per-role model overrides; price map for
budgeting (fail-closed when a model has no price entry).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from codie import config

if TYPE_CHECKING:
    from codie.config import Settings

ROLE_MAX_ITER: dict[str, int] = {
    "planner": 25,
    "coder": 40,
    "reviewer": 15,
    "tester": 25,
    "orchestrator": 15,
    "surveyor": 20,
}


class LLMConfigError(Exception):
    """Fail-closed when a model has no price entry (§6.2)."""


def build_llm(settings: Settings, role: str) -> Any:
    """Build a CrewAI LLM for a role (lazy, so import-free tests still work)."""
    from crewai import LLM  # deferred: heavy import

    llm_cfg = settings.llm
    model = llm_cfg.model_for(role)
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": llm_cfg.temperature,
        "max_tokens": 8192,
        "api_key": _api_key(settings),
    }
    if llm_cfg.base_url:
        kwargs["base_url"] = llm_cfg.base_url
    return LLM(**kwargs)


def _api_key(settings: Settings) -> str:
    return config.require_env(settings.llm.api_key_env, "LLM access")


def estimate_cost(settings: Settings, model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from llm.price_map. Fail-closed when missing (§6.2)."""
    price = settings.llm.price_for(model)
    if price is None:
        raise LLMConfigError(f"no llm.price_map entry for model {model!r}")
    return (input_tokens / 1000) * price.input_per_1k + (output_tokens / 1000) * price.output_per_1k


def role_max_iter(role: str) -> int:
    return ROLE_MAX_ITER.get(role, 25)

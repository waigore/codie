"""Transition-table loader (Technical Spec §5.5 transitions.yaml)."""

from __future__ import annotations

import functools
from pathlib import Path

import yaml

_TRANSITIONS_PATH = Path(__file__).parent / "transitions.yaml"


@functools.lru_cache(maxsize=1)
def load_transitions() -> dict:
    with open(_TRANSITIONS_PATH) as fh:
        return yaml.safe_load(fh)


def feature_edges() -> list[dict]:
    return load_transitions()["feature_transitions"]


def task_edges() -> list[dict]:
    return load_transitions()["task_transitions"]


def mutation_matrix() -> dict:
    return load_transitions()["mutation_matrix"]

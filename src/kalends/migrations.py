"""Upgrade older config.yaml files to the current settings format.

Each release that changes the *shape* of config.yaml bumps
:data:`kalends.config.CONFIG_VERSION` and adds a step here that converts the
previous format. Steps work on the raw YAML dict (before ``${VAR}`` values are
filled in), only ever move or rename settings, and never touch ``.env``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

Raw = dict[str, Any]

# Privacy rules every new (or upgraded, rule-less) setup starts with. Matching events show as
# Busy for every audience; each rule can be switched off or edited on the Privacy rules page.
SUGGESTED_RULES: list[Raw] = [
    {
        "name": "Health",
        "match": [
            "doctor",
            "dentist",
            "therapy",
            "therapist",
            "counseling",
            "counselling",
            "psychiatrist",
            "clinic",
            "hospital",
            "pharmacy",
            "prescription",
            "physical therapy",
            "checkup",
            "vaccine",
        ],
    },
    {
        "name": "Job search",
        "match": ["interview", "recruiter", "job offer", "salary"],
    },
    {
        "name": "Money and legal",
        "match": [
            "bank",
            "loan",
            "mortgage",
            "lawyer",
            "attorney",
            "court",
            "tax",
            "taxes",
            "accountant",
            "financial advisor",
        ],
    },
]


def suggested_rules() -> list[Raw]:
    return copy.deepcopy(SUGGESTED_RULES)


def _v1_to_v2(raw: Raw) -> Raw:
    """0.4.1: privacy rules are on by default. Setups with no rules get the suggested set."""
    if "rules" not in raw:
        raw["rules"] = suggested_rules()
    return raw


# from_version -> function returning the raw config in from_version + 1 format
STEPS: dict[int, Callable[[Raw], Raw]] = {1: _v1_to_v2}


def config_version_of(raw: Raw) -> int:
    value = raw.get("config_version", 1)
    return value if isinstance(value, int) and value >= 1 else 1


def migrate(raw: Raw, target: int) -> tuple[Raw, bool]:
    """Return ``raw`` upgraded to ``target`` and whether anything changed.

    Raises ValueError for a config written by a newer Kalends than this one.
    """
    version = config_version_of(raw)
    if version > target:
        raise ValueError(
            f"config.yaml uses settings format {version}, but this Kalends only knows up to {target}. "
            "Update Kalends, or restore the config.yaml backup made before the last update."
        )
    if version == target:
        return raw, False
    out = copy.deepcopy(raw)
    while version < target:
        out = STEPS[version](out)
        version += 1
    out["config_version"] = target
    return out, True

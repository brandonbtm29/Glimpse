"""Upgrade older config.yaml files to the current settings format.

Each release that changes the *shape* of config.yaml bumps
:data:`glimpse.config.CONFIG_VERSION` and adds a step here that converts the
previous format. Steps work on the raw YAML dict (before ``${VAR}`` values are
filled in), only ever move or rename settings, and never touch ``.env``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

Raw = dict[str, Any]

# from_version -> function returning the raw config in from_version + 1 format
STEPS: dict[int, Callable[[Raw], Raw]] = {}


def config_version_of(raw: Raw) -> int:
    value = raw.get("config_version", 1)
    return value if isinstance(value, int) and value >= 1 else 1


def migrate(raw: Raw, target: int) -> tuple[Raw, bool]:
    """Return ``raw`` upgraded to ``target`` and whether anything changed.

    Raises ValueError for a config written by a newer Glimpse than this one.
    """
    version = config_version_of(raw)
    if version > target:
        raise ValueError(
            f"config.yaml uses settings format {version}, but this Glimpse only knows up to {target}. "
            "Update Glimpse, or restore the config.yaml backup made before the last update."
        )
    if version == target:
        return raw, False
    out = copy.deepcopy(raw)
    while version < target:
        out = STEPS[version](out)
        version += 1
    out["config_version"] = target
    return out, True

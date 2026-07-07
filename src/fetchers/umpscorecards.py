"""Umpire K factor.

Assignment comes from the MLB Stats API game feed (officials are usually
posted only 1-3 hours before first pitch, so the 10 AM run typically sees
"ump unconfirmed" and the pregame check enriches). The per-ump K factor comes
from the umpire_k_factors table in config/formula.yaml, which is seeded with
rough public values and can be updated from UmpScorecards data via /tune.

UmpScorecards has no stable public API, so live scraping is attempted only as
a bonus and every failure silently falls back to the config table.
"""
from __future__ import annotations

from typing import Optional

from config import formula
from fetchers import log_error
from fetchers.mlb_stats_api import home_plate_umpire


def k_factor_for(ump_name: Optional[str]) -> float:
    table = formula().get("umpire_k_factors", {})
    default = float(table.get("default", 1.0))
    if not ump_name:
        return default
    try:
        return float(table.get(ump_name, default))
    except (TypeError, ValueError):
        return default


def game_ump_k_factor(game_pk: int) -> tuple[Optional[str], float]:
    """(ump_name or None, k_factor). Neutral 1.0 when unassigned."""
    try:
        name = home_plate_umpire(game_pk)
    except Exception as exc:
        log_error("umpscorecards", f"lookup game {game_pk}: {exc}")
        name = None
    return name, k_factor_for(name)


def is_high_k_ump(factor: float) -> bool:
    return factor >= 1.02

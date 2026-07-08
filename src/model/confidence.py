"""Confidence scoring (1-10) from the additive weights in config/formula.yaml.

Every contribution is recorded in a reasons dict so /picks explanations and
"why did you pick X?" answers can show exactly which factors fired.
"""
from __future__ import annotations

from config import formula
from model.scoring import (HRProjection, KProjection, MLProjection,
                           TotalProjection)


def _clamp(score: int) -> int:
    return max(1, min(10, score))


def score_k_pick(proj: KProjection, *, domed_park: bool, ump_confirmed: bool,
                 ump_high_k: bool, injury_flags: bool, csw_aligned: bool | None,
                 line_move: float | None, stale_sources: int = 0) -> KProjection:
    w = formula()["confidence_weights"]
    reasons: dict[str, int] = {"base": w["base"]}
    edge = abs(proj.edge or 0.0)

    if edge >= 1.5:
        reasons["edge_gte_1_5"] = w["edge_gte_1_5"]
    elif edge >= 1.0:
        reasons["edge_gte_1_0"] = w["edge_gte_1_0"]
    elif edge >= 0.7:
        reasons["edge_gte_0_7"] = w["edge_gte_0_7"]

    if proj.starts >= 5:
        reasons["starts_gte_5"] = w["starts_gte_5"]
    if domed_park:
        reasons["domed_park"] = w["domed_park"]
    if ump_confirmed:
        reasons["ump_confirmed"] = w["ump_confirmed"]
        if ump_high_k:
            reasons["ump_high_k_factor"] = w["ump_high_k_factor"]
    if not injury_flags:
        reasons["no_injury_flags"] = w["no_injury_flags"]
    if csw_aligned:
        reasons["csw_aligns_with_k"] = w["csw_aligns_with_k"]

    # Line movement relative to our side. Positive move = line moved toward
    # our projection (sharp money agrees); negative = fade signal.
    if line_move is not None and abs(line_move) >= 0.25:
        toward = (line_move > 0) == (proj.side == "OVER")
        if toward:
            reasons["line_moved_toward_us"] = w["line_moved_toward_us"]
        else:
            reasons["line_moved_against_us"] = w["line_moved_against_us"]

    if stale_sources:
        reasons["stale_data_penalty"] = w.get("stale_data_penalty", -1) * stale_sources

    proj.confidence = _clamp(sum(reasons.values()))
    proj.conf_reasons = reasons
    return proj


def score_ml_pick(proj: MLProjection, *, both_starters_named: bool,
                  domed_park: bool, sample_games: int,
                  stale_sources: int = 0) -> MLProjection:
    """Moneyline confidence — probability-edge tiers on the win% gap."""
    w = formula()["confidence_weights"]
    reasons: dict[str, int] = {"base": w["base"]}
    edge = proj.edge or 0.0

    if edge >= 0.09:
        reasons["edge_gte_1_5"] = w["edge_gte_1_5"]
    elif edge >= 0.06:
        reasons["edge_gte_1_0"] = w["edge_gte_1_0"]
    elif edge >= 0.04:
        reasons["edge_gte_0_7"] = w["edge_gte_0_7"]

    if sample_games >= 60:                     # record is meaningful by June
        reasons["sample_gte_60_games"] = w["starts_gte_5"]
    if both_starters_named:                    # no TBD-starter coin flips
        reasons["both_starters_named"] = 1
    if domed_park:
        reasons["domed_park"] = w["domed_park"]
    if stale_sources:
        reasons["stale_data_penalty"] = w.get("stale_data_penalty", -1) * stale_sources

    proj.confidence = _clamp(sum(reasons.values()))
    proj.conf_reasons = reasons
    return proj


def score_total_pick(proj: TotalProjection, *, both_starters_named: bool,
                     domed_park: bool, sample_games: int, rain_risk: bool,
                     stale_sources: int = 0) -> TotalProjection:
    """Totals confidence — edge tiers in runs (0.75 / 1.25 / 2.0)."""
    w = formula()["confidence_weights"]
    reasons: dict[str, int] = {"base": w["base"]}
    edge = abs(proj.edge or 0.0)

    if edge >= 2.0:
        reasons["edge_gte_1_5"] = w["edge_gte_1_5"]
    elif edge >= 1.25:
        reasons["edge_gte_1_0"] = w["edge_gte_1_0"]
    elif edge >= 0.75:
        reasons["edge_gte_0_7"] = w["edge_gte_0_7"]

    if sample_games >= 60:
        reasons["sample_gte_60_games"] = w["starts_gte_5"]
    if both_starters_named:
        reasons["both_starters_named"] = 1
    if domed_park:
        reasons["domed_park"] = w["domed_park"]
    if rain_risk:                          # shortened game kills an over
        reasons["rain_risk"] = -2
    if stale_sources:
        reasons["stale_data_penalty"] = w.get("stale_data_penalty", -1) * stale_sources

    proj.confidence = _clamp(sum(reasons.values()))
    proj.conf_reasons = reasons
    return proj


def score_hr_pick(proj: HRProjection, *, domed_park: bool, injury_flags: bool,
                  pa_season: int, stale_sources: int = 0) -> HRProjection:
    """HR confidence reuses the same additive philosophy on the prob edge."""
    w = formula()["confidence_weights"]
    reasons: dict[str, int] = {"base": w["base"]}
    edge = proj.edge or 0.0

    # Probability-edge tiers scaled to the HR market (3% edge is real money).
    if edge >= 0.08:
        reasons["edge_gte_1_5"] = w["edge_gte_1_5"]
    elif edge >= 0.05:
        reasons["edge_gte_1_0"] = w["edge_gte_1_0"]
    elif edge >= 0.03:
        reasons["edge_gte_0_7"] = w["edge_gte_0_7"]

    if pa_season >= 200:
        reasons["starts_gte_5"] = w["starts_gte_5"]
    if domed_park:
        reasons["domed_park"] = w["domed_park"]
    if not injury_flags:
        reasons["no_injury_flags"] = w["no_injury_flags"]
    if stale_sources:
        reasons["stale_data_penalty"] = w.get("stale_data_penalty", -1) * stale_sources

    proj.confidence = _clamp(sum(reasons.values()))
    proj.conf_reasons = reasons
    return proj

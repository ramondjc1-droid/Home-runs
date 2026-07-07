"""Projection math for strikeout props and home-run props.

Strikeouts (the core formula):

    Pitcher_K_pct_blended = 0.7 × K%_last_30d + 0.3 × K%_season
    Adjusted_K_Rate = blended × (Opp_Team_K%_L15 / League_Avg_K%)
                              × Umpire_K_Factor × Park_K_Factor
    BF_expected  = mean(IP last 5 starts) × 4.3
    Projected_Ks = BF_expected × Adjusted_K_Rate
    Edge         = Projected_Ks − Book_K_Line

Home runs (secondary module):

    HR/PA_blended = 0.7 × HR/PA_30d + 0.3 × HR/PA_season
    HR/PA_adj     = blended × Park_HR_Factor × Pitcher_HR_Factor × Temp_Factor
    P(≥1 HR)      = 1 − (1 − HR/PA_adj) ^ Expected_PA
    Edge          = P(≥1 HR) − Implied_Prob(best "Yes" price)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config import formula


@dataclass
class KProjection:
    pitcher_name: str
    pitcher_id: int
    team: str
    opponent: str
    game_pk: int
    projected_ks: float
    adjusted_k_rate: float
    bf_expected: float
    k_pct_blended: float
    opp_factor: float
    ump_factor: float
    park_factor: float
    starts: int
    book_line: Optional[float] = None
    edge: Optional[float] = None
    side: Optional[str] = None
    confidence: int = 0
    conf_reasons: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)
    extras: dict = field(default_factory=dict)


@dataclass
class HRProjection:
    batter_name: str
    batter_id: int
    team: str
    opponent: str
    game_pk: int
    hr_prob: float
    hr_pa_adj: float
    implied_prob: Optional[float] = None
    edge: Optional[float] = None
    book_price: Optional[int] = None
    confidence: int = 0
    conf_reasons: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)
    extras: dict = field(default_factory=dict)


def project_strikeouts(*, pitcher_name: str, pitcher_id: int, team: str,
                       opponent: str, game_pk: int, k_pct_30d: float,
                       k_pct_season: float, ip_last5_mean: float, starts: int,
                       opp_k_pct_l15: Optional[float], ump_k_factor: float,
                       park_k_factor: float) -> KProjection:
    f = formula()["formula"]
    blended = (f["recent_form_weight"] * k_pct_30d
               + f["season_weight"] * k_pct_season)

    league = f["league_avg_k_pct"]
    opp_factor = (opp_k_pct_l15 / league) if opp_k_pct_l15 else 1.0
    # A team K% is noisy; clamp the multiplier to a sane band.
    opp_factor = max(0.75, min(1.30, opp_factor))

    adjusted = blended * opp_factor * ump_k_factor * park_k_factor
    bf = ip_last5_mean * f["bf_per_inning"]
    projected = bf * adjusted

    return KProjection(
        pitcher_name=pitcher_name, pitcher_id=pitcher_id, team=team,
        opponent=opponent, game_pk=game_pk,
        projected_ks=round(projected, 2), adjusted_k_rate=round(adjusted, 4),
        bf_expected=round(bf, 1), k_pct_blended=round(blended, 4),
        opp_factor=round(opp_factor, 3), ump_factor=ump_k_factor,
        park_factor=park_k_factor, starts=starts,
    )


def apply_line(proj: KProjection, book_line: float) -> KProjection:
    proj.book_line = book_line
    proj.edge = round(proj.projected_ks - book_line, 2)
    proj.side = "OVER" if proj.edge > 0 else "UNDER"
    return proj


def project_home_run(*, batter_name: str, batter_id: int, team: str,
                     opponent: str, game_pk: int, hr_pa_30d: float,
                     hr_pa_season: float, park_hr_factor: float,
                     pitcher_hr_per_bf: Optional[float],
                     temp_f: Optional[float]) -> HRProjection:
    cfg = formula()["homerun"]
    blended = (cfg["recent_form_weight"] * hr_pa_30d
               + cfg["season_weight"] * hr_pa_season)

    league_hr_pa = cfg["league_avg_hr_pa"]
    # Regress toward league average — small HR samples run hot, and an
    # unshrunk blend claims edges no model should claim.
    shrink = cfg.get("shrink_to_league", 0.25)
    blended = (1.0 - shrink) * blended + shrink * league_hr_pa

    pitcher_factor = 1.0
    if pitcher_hr_per_bf is not None and league_hr_pa > 0:
        pitcher_factor = max(0.8, min(1.25, pitcher_hr_per_bf / league_hr_pa))

    temp_factor = 1.0
    if temp_f is not None:
        temp_factor = cfg.get("temp_factor_per_10f", 1.03) ** ((temp_f - 70.0) / 10.0)
        temp_factor = max(0.9, min(1.12, temp_factor))

    hr_pa_adj = blended * park_hr_factor * pitcher_factor * temp_factor
    hr_pa_adj = max(0.0, min(0.25, hr_pa_adj))
    pa = cfg["expected_pa"]
    prob = 1.0 - (1.0 - hr_pa_adj) ** pa

    return HRProjection(
        batter_name=batter_name, batter_id=batter_id, team=team,
        opponent=opponent, game_pk=game_pk,
        hr_prob=round(prob, 4), hr_pa_adj=round(hr_pa_adj, 4),
        extras={"pitcher_factor": round(pitcher_factor, 3),
                "temp_factor": round(temp_factor, 3),
                "park_hr_factor": park_hr_factor},
    )


def apply_hr_price(proj: HRProjection, price: int) -> HRProjection:
    from fetchers.odds_api import implied_prob
    proj.book_price = price
    proj.implied_prob = round(implied_prob(price), 4)
    proj.edge = round(proj.hr_prob - proj.implied_prob, 4)
    return proj

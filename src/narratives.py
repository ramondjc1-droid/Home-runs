"""Pick narratives — the only place Opus is spent (max picks/day calls).

Budget-gated by costs.check_budget(); with no key, over budget, or on any
API failure, a deterministic template stands in so the pipeline never breaks.
"""
from __future__ import annotations

from typing import Union

import costs
from config import ANTHROPIC_API_KEY, NARRATIVE_MODEL
from model.scoring import (HRProjection, KProjection, MLProjection,
                           TotalProjection)
from telegram_bot import send_message

SYSTEM = (
    "You are a sharp, concise MLB props analyst. Write EXACTLY 2 sentences "
    "explaining why this pick has edge, grounded only in the numbers given — "
    "the K-rate blend, matchup, park, umpire, and line value. No hedging "
    "boilerplate, no advice language, no preamble."
)


def _k_fallback(p: KProjection) -> str:
    direction = "over" if p.side == "OVER" else "under"
    return (
        f"{p.pitcher_name} projects for {p.projected_ks:.1f} Ks on a "
        f"{p.k_pct_blended:.1%} blended K-rate against a "
        f"{'whiff-prone' if p.opp_factor > 1.02 else 'contact-oriented' if p.opp_factor < 0.98 else 'league-average'} "
        f"{p.opponent} lineup. That clears the {p.book_line} line by "
        f"{abs(p.edge or 0):.1f}, making the {direction} the play."
    )


def _hr_fallback(p: HRProjection) -> str:
    return (
        f"{p.batter_name} models at {p.hr_prob:.0%} to homer vs {p.opponent} "
        f"({p.extras.get('park_hr_factor', 1.0):.2f} park factor, "
        f"pitcher factor {p.extras.get('pitcher_factor', 1.0):.2f}). "
        f"The book implies {p.implied_prob:.0%}, leaving a "
        f"{(p.edge or 0) * 100:.1f}-point probability edge."
    )


def _ml_fallback(p: MLProjection) -> str:
    b = p.extras.get("breakdown", {})
    return (
        f"{p.team} models at {p.win_prob:.0%} to beat {p.opponent} "
        f"(log5 {b.get('log5', 0.5):.2f}, starter shift {b.get('starter_shift', 0):+.2f}"
        f"{', home edge' if p.is_home else ''}). "
        f"The best price of {p.book_price:+d} implies only {p.implied_prob:.0%}, "
        f"a {(p.edge or 0) * 100:.1f}-point value gap."
    )


def _tot_fallback(p: TotalProjection) -> str:
    b = p.extras.get("breakdown", {})
    return (
        f"The model expects {p.projected_runs:.1f} runs in {p.matchup} "
        f"({b.get('exp_home', 0):.1f} home / {b.get('exp_away', 0):.1f} away, "
        f"park factor {b.get('park_run_factor', 1.0):.2f}, "
        f"starters {b.get('starter_shift', 0):+.1f}). "
        f"That clears the {p.book_line} line by {abs(p.edge or 0):.1f}, "
        f"making the {p.side.lower()} the play."
    )


def _facts(p: Union[KProjection, HRProjection, MLProjection, TotalProjection]) -> str:
    if isinstance(p, KProjection):
        return (
            f"Pick: {p.pitcher_name} ({p.team}) vs {p.opponent} — "
            f"{p.side} {p.book_line} strikeouts\n"
            f"Projection: {p.projected_ks:.2f} Ks (edge {p.edge:+.2f})\n"
            f"Blended K%: {p.k_pct_blended:.1%} (70% last-30d / 30% season)\n"
            f"Opponent K factor (L15): {p.opp_factor:.3f}\n"
            f"Ump factor: {p.ump_factor:.2f} · Park factor: {p.park_factor:.2f}\n"
            f"Expected BF: {p.bf_expected:.1f} · Starts: {p.starts}\n"
            f"Confidence: {p.confidence}/10 · Factors: {p.conf_reasons}\n"
        )
    if isinstance(p, HRProjection):
        return (
            f"Pick: {p.batter_name} ({p.team}) to hit a HR vs {p.opponent}\n"
            f"Model P(HR): {p.hr_prob:.1%} · Implied: {p.implied_prob:.1%} "
            f"(edge {(p.edge or 0) * 100:+.1f} pts)\n"
            f"Adjusted HR/PA: {p.hr_pa_adj:.4f} · Factors: {p.extras}\n"
            f"Confidence: {p.confidence}/10 · {p.conf_reasons}\n"
        )
    if isinstance(p, TotalProjection):
        return (
            f"Pick: {p.matchup} — {p.side} {p.book_line} total runs "
            f"at {p.book_price if p.book_price else -110:+d}\n"
            f"Projected total: {p.projected_runs:.2f} (edge {p.edge:+.2f} runs)\n"
            f"Model breakdown: {p.extras.get('breakdown')}\n"
            f"Confidence: {p.confidence}/10 · {p.conf_reasons}\n"
        )
    return (
        f"Pick: {p.extras.get('team_name', p.team)} moneyline "
        f"{'vs' if p.is_home else '@'} {p.opponent} at {p.book_price:+d}\n"
        f"Model win prob: {p.win_prob:.1%} · Implied: {p.implied_prob:.1%} "
        f"(edge {(p.edge or 0) * 100:+.1f} pts)\n"
        f"Model breakdown: {p.extras.get('breakdown')}\n"
        f"Confidence: {p.confidence}/10 · {p.conf_reasons}\n"
    )


def generate(p: Union[KProjection, HRProjection, MLProjection, TotalProjection]) -> str:
    if isinstance(p, KProjection):
        fallback = _k_fallback(p)
    elif isinstance(p, HRProjection):
        fallback = _hr_fallback(p)
    elif isinstance(p, TotalProjection):
        fallback = _tot_fallback(p)
    else:
        fallback = _ml_fallback(p)
    if not ANTHROPIC_API_KEY:
        return fallback
    if not costs.check_budget(alert_fn=send_message):
        return fallback
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=NARRATIVE_MODEL,
            max_tokens=200,
            system=SYSTEM,
            messages=[{"role": "user",
                       "content": f"Write the 2-sentence narrative.\n\n{_facts(p)}"}],
        )
        costs.track_usage(NARRATIVE_MODEL, msg.usage)
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        text = "".join(parts).strip()
        return text or fallback
    except Exception:
        return fallback

"""Anthropic API spend tracking against the monthly budget ceiling.

Every call made through track_usage() lands in the api_costs table. Before
any Opus narrative run, check_budget() projects the month's spend; if the
projection exceeds the ceiling it alerts via Telegram (once per day) and the
caller falls back to template narratives so the pipeline keeps running.

Anthropic doesn't expose remaining credit via API, so the low-balance alert
is driven by local spend accounting: it fires when the *tracked* month spend
crosses (ceiling − low_balance_alert_usd).
"""
from __future__ import annotations

import calendar
from datetime import date

import db
from config import formula

# $ per million tokens (input, output). Prefix-matched; keep roughly current.
PRICING = {
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
}
DEFAULT_PRICING = (5.0, 25.0)


def estimate_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    for prefix, (inp, outp) in PRICING.items():
        if model.startswith(prefix):
            return (input_tokens * inp + output_tokens * outp) / 1e6
    inp, outp = DEFAULT_PRICING
    return (input_tokens * inp + output_tokens * outp) / 1e6


def track_usage(model: str, usage) -> float:
    """Record one API call's cost. `usage` is the SDK's message.usage."""
    itok = getattr(usage, "input_tokens", 0) or 0
    otok = getattr(usage, "output_tokens", 0) or 0
    usd = estimate_usd(model, itok, otok)
    db.record_cost(model, itok, otok, usd)
    return usd


def projected_month_spend() -> float:
    spent = db.month_spend()
    today = date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    return spent / max(today.day, 1) * days_in_month


def check_budget(alert_fn=None) -> bool:
    """True when it's OK to make paid calls. Alerts (once/day) when not."""
    cfg = formula().get("budget", {})
    ceiling = float(cfg.get("monthly_ceiling_usd", 100.0))
    low_mark = float(cfg.get("low_balance_alert_usd", 20.0))
    spent = db.month_spend()
    projected = projected_month_spend()

    over_projection = projected > ceiling
    near_ceiling = spent > ceiling - low_mark

    if (over_projection or near_ceiling) and alert_fn is not None:
        today = date.today().isoformat()
        if db.kv_get("budget_alert_date") != today:
            db.kv_set("budget_alert_date", today)
            alert_fn(
                "💸 <b>Budget alert</b>\n"
                f"Month spend: ${spent:.2f} · Projected: ${projected:.2f} "
                f"(ceiling ${ceiling:.0f}).\n"
                "Narratives are falling back to templates until you raise the "
                "ceiling (/tune budget.monthly_ceiling_usd ...) or the month rolls."
            )
    return not over_projection


def status_line() -> str:
    cfg = formula().get("budget", {})
    ceiling = float(cfg.get("monthly_ceiling_usd", 100.0))
    return (f"API spend: ${db.month_spend():.2f} this month "
            f"(projected ${projected_month_spend():.2f} / ${ceiling:.0f} ceiling)")

"""Telegram card formatting (HTML parse mode)."""
from __future__ import annotations

from datetime import date, timedelta
from config import today_et
from typing import Optional

import db
from model.scoring import (HRProjection, KProjection, MLProjection,
                           TotalProjection)

NUM = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]
RULE = "─" * 24


def _fmt_units(u: float) -> str:
    return f"{u:+.2f}u"


def _record_line(perf: dict) -> str:
    return (f"{perf['hits']}-{perf['misses']}"
            + (f"-{perf['pushes']}" if perf.get("pushes") else "")
            + f" ({_fmt_units(perf['units'])})")


def scoreboard() -> str:
    yesterday = (today_et() - timedelta(days=1)).isoformat()
    y = db.summary_for(yesterday)
    season = db.season_performance()
    y_part = (f"{y['hits']}-{y['misses']} ({_fmt_units(y['units_pnl'])})"
              if y else "no picks")
    s_part = _record_line(season) if season["n"] else "0-0 (+0.00u)"
    return f"📊 Yesterday: {y_part} | Season: {s_part}"


def _price(p: Optional[int]) -> str:
    if p is None:
        return "n/a"
    return f"{p:+d}"


def k_pick_block(i: int, p: KProjection, narrative: str) -> str:
    n = NUM[i] if i < len(NUM) else f"{i + 1}."
    book, price = p.extras.get("best_book"), p.extras.get("best_price")
    lines = [
        f"{n} <b>{p.pitcher_name}</b> ({p.team}) vs {p.opponent}",
        f"   🎯 <b>{p.side} {p.book_line} Ks</b>",
        f"   📈 Projection: {p.projected_ks:.1f} | Edge: {p.edge:+.1f}",
        f"   💪 Confidence: {p.confidence}/10",
        f"   📖 Best line: {book or 'n/a'} at {_price(price)}",
        "",
        f"   <i>{narrative}</i>",
    ]
    return "\n".join(lines)


def hr_pick_block(i: int, p: HRProjection, narrative: str) -> str:
    n = NUM[i] if i < len(NUM) else f"{i + 1}."
    lines = [
        f"{n} <b>{p.batter_name}</b> ({p.team}) vs {p.opponent}",
        f"   💣 <b>To hit a HR</b> at {_price(p.book_price)}",
        f"   📈 Model: {p.hr_prob:.0%} | Implied: {p.implied_prob:.0%} "
        f"| Edge: {(p.edge or 0) * 100:+.1f} pts",
        f"   💪 Confidence: {p.confidence}/10",
        "",
        f"   <i>{narrative}</i>",
    ]
    return "\n".join(lines)


def ml_pick_block(i: int, p: MLProjection, narrative: str) -> str:
    n = NUM[i] if i < len(NUM) else f"{i + 1}."
    where = "vs" if p.is_home else "@"
    lines = [
        f"{n} <b>{p.team} ML</b> {where} {p.opponent}",
        f"   🎲 <b>{p.extras.get('team_name', p.team)} to win</b> at {_price(p.book_price)}",
        f"   📈 Model: {p.win_prob:.0%} | Implied: {p.implied_prob:.0%} "
        f"| Edge: {(p.edge or 0) * 100:+.1f} pts",
        f"   💪 Confidence: {p.confidence}/10",
        f"   📖 Best line: {p.extras.get('best_book') or 'n/a'}",
        "",
        f"   <i>{narrative}</i>",
    ]
    return "\n".join(lines)


def tot_pick_block(i: int, p: TotalProjection, narrative: str) -> str:
    n = NUM[i] if i < len(NUM) else f"{i + 1}."
    lines = [
        f"{n} <b>{p.matchup}</b>",
        f"   🔢 <b>{p.side} {p.book_line} runs</b>",
        f"   📈 Projection: {p.projected_runs:.1f} | Edge: {p.edge:+.1f}",
        f"   💪 Confidence: {p.confidence}/10",
        f"   📖 Best line: {p.extras.get('best_book') or 'n/a'} at {_price(p.book_price)}",
        "",
        f"   <i>{narrative}</i>",
    ]
    return "\n".join(lines)


def ml_board(projs: list[MLProjection], picked_pks: set[int]) -> Optional[str]:
    """One line per game: the model's value side vs the market, best edge first.

    🎯 marks sides that made the actual pick card; everything else is
    informational — small or negative edges are shown, not recommended.
    """
    by_game: dict[int, MLProjection] = {}
    for p in projs:
        if p.edge is None:
            continue
        cur = by_game.get(p.game_pk)
        if cur is None or p.edge > (cur.edge or -1.0):
            by_game[p.game_pk] = p
    if not by_game:
        return None
    rows = sorted(by_game.values(), key=lambda p: p.edge, reverse=True)
    lines = ["🎲 <b>ML BOARD</b> — model vs market, value side per game", ""]
    for p in rows:
        matchup = (f"{p.opponent} @ {p.team}" if p.is_home
                   else f"{p.team} @ {p.opponent}")
        mark = "🎯 " if p.game_pk in picked_pks else ""
        lines.append(
            f"{mark}{matchup}: <b>{p.team}</b> {p.win_prob:.0%} "
            f"vs {p.implied_prob:.0%} imp ({(p.edge or 0) * 100:+.1f})")
    return "\n".join(lines)


def morning_card(k_blocks: list[str], hr_blocks: list[str],
                 flags: list[str], d: Optional[str] = None,
                 ml_blocks: Optional[list[str]] = None,
                 ml_board_text: Optional[str] = None,
                 tot_blocks: Optional[list[str]] = None) -> str:
    d = d or today_et().isoformat()
    parts = [f"⚾ <b>MLB K PICKS — {d}</b>", "", scoreboard(), "", RULE, ""]
    if k_blocks:
        parts.append("\n\n".join(k_blocks))
    else:
        parts.append("No strikeout props cleared the edge + confidence gates "
                     "today. Quality &gt; quantity — passing.")
    if hr_blocks:
        parts += ["", RULE, "", "💣 <b>HOME RUN PICKS</b>", ""]
        parts.append("\n\n".join(hr_blocks))
    if tot_blocks:
        parts += ["", RULE, "", "🔢 <b>TOTALS — O/U RUNS</b>", ""]
        parts.append("\n\n".join(tot_blocks))
    if ml_blocks:
        parts += ["", RULE, "", "🎲 <b>MONEYLINE VALUE</b>", ""]
        parts.append("\n\n".join(ml_blocks))
    if ml_board_text:
        parts += ["", RULE, "", ml_board_text]
    parts += ["", RULE, ""]
    parts.append("⚠️ Flags: " + ("; ".join(flags) if flags else "none"))
    parts.append("🕒 Lines will refresh at 2 PM ET")
    parts.append("\n<i>Informational only — not betting advice. "
                 "Bet responsibly and within your means.</i>")
    return "\n".join(parts)


def grade_report(d: Optional[str] = None) -> Optional[str]:
    """Yesterday's results block, or None when nothing was graded."""
    d = d or (today_et() - timedelta(days=1)).isoformat()
    picks = [p for p in db.picks_for_date(d) if p["result"] != "PENDING"]
    if not picks:
        return None
    lines = [f"📋 <b>YESTERDAY'S RESULTS — {d}</b>", ""]
    for p in picks:
        icon = {"HIT": "✅", "MISS": "❌", "PUSH": "➖", "VOID": "🚫"}.get(p["result"], "•")
        actual = p["actual_ks"]
        if p["pick_type"] == "HR":
            what = f"to hit a HR → {'homered' if p['result'] == 'HIT' else 'no HR'}"
        elif p["pick_type"] == "ML":
            price = p["best_book_price"]
            what = (f"ML ({price:+d}) → {'won' if p['result'] == 'HIT' else 'lost'}"
                    if price else
                    f"ML → {'won' if p['result'] == 'HIT' else 'lost'}")
        elif p["pick_type"] == "TOT":
            what = f"{p['pick_side']} {p['book_line']} runs → {actual}"
        else:
            what = f"{p['pick_side']} {p['book_line']} → {actual} Ks"
        if p["result"] == "VOID":
            what += " (no start — voided)"
        lines.append(f"{p['pitcher_name']} — {what} {icon}")
    summ = db.summary_for(d)
    if summ:
        lines += ["", f"Record: {summ['hits']}-{summ['misses']}-{summ['pushes']} "
                      f"({_fmt_units(summ['units_pnl'])})"]
        season = db.season_performance()
        decided = season["hits"] + season["misses"]
        hit_rate = season["hits"] / decided * 100 if decided else 0.0
        lines.append(f"Season: {season['hits']}-{season['misses']} "
                     f"({_fmt_units(season['units'])}), {hit_rate:.1f}% hit rate")
    return "\n".join(lines)


def no_slate() -> str:
    return (f"⚾ <b>MLB K PICKS — {today_et().isoformat()}</b>\n\n"
            "No MLB games today — nothing to analyze. See you next slate.")

"""2:00 AM ET heartbeat — grade yesterday's picks from official box scores.

Silent by default: results land in the DB and the morning analysis leads with
the grade report at 10 AM. Pass --notify to also push the report immediately.

Grading rules:
  K picks:  actual Ks vs line. Half lines can't push; whole-number lines push
            on an exact match. A pick whose pitcher never appeared is VOID.
  HR picks: HIT when the batter homered at least once.
  Units:    priced at the stored best price when available, else -110.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from typing import Optional

import cards
import db
from fetchers import log_error
from fetchers import mlb_stats_api as mlb
from telegram_bot import send_message


def units_for(result: str, price: Optional[int]) -> float:
    if result == "HIT":
        price = price if price else -110
        return price / 100.0 if price > 0 else 100.0 / abs(price)
    if result == "MISS":
        return -1.0
    return 0.0  # PUSH / VOID


def grade_k(actual: Optional[int], line: float, side: str) -> str:
    if actual is None:
        return "VOID"
    if actual == line:
        return "PUSH"
    over_hit = actual > line
    return "HIT" if over_hit == (side == "OVER") else "MISS"


def grade_pick(p) -> tuple[str, Optional[int]]:
    """(result, actual) for one pick row; ('PENDING', None) when not final."""
    if not p["game_pk"]:
        return "VOID", None
    if not mlb.game_final(p["game_pk"]):
        return "PENDING", None
    if p["pick_type"] == "ML":
        winner = mlb.game_winner(p["game_pk"])
        if winner is None:
            return "VOID", None
        return ("HIT" if winner == p["pitcher_id"] else "MISS"), None
    if p["pick_type"] == "HR":
        hr = mlb.actual_home_runs(p["game_pk"], p["pitcher_id"])
        if hr is None:
            return "VOID", None
        return ("HIT" if hr >= 1 else "MISS"), hr
    actual = mlb.actual_strikeouts(p["game_pk"], p["pitcher_id"])
    return grade_k(actual, p["book_line"], p["pick_side"] or "OVER"), actual


def run(notify: bool = False, dry_run: bool = False) -> None:
    db.init_db()
    today = date.today().isoformat()
    pending = db.pending_picks(before_date=today)
    if not pending:
        print("[grader] nothing pending.")
        return

    graded_dates = set()
    for p in pending:
        try:
            result, actual = grade_pick(p)
        except Exception as exc:
            log_error("grader", f"pick {p['id']} ({p['pitcher_name']}): {exc}")
            continue
        if result == "PENDING":
            print(f"[grader] {p['pitcher_name']} game not final yet — skipping.")
            continue
        units = units_for(result, p["best_book_price"])
        if not dry_run:
            db.set_result(p["id"], result, actual, units)
        graded_dates.add(p["date"])
        print(f"[grader] {p['pitcher_name']} {p['pick_side']} {p['book_line']} "
              f"→ {actual} = {result} ({units:+.2f}u)")

    # Roll up each affected date into daily_summary.
    for d in sorted(graded_dates):
        rows = [r for r in db.picks_for_date(d) if r["result"] != "PENDING"]
        hits = sum(r["result"] == "HIT" for r in rows)
        misses = sum(r["result"] == "MISS" for r in rows)
        pushes = sum(r["result"] == "PUSH" for r in rows)
        units = sum(r["units_pnl"] or 0 for r in rows)
        if not dry_run:
            db.upsert_daily_summary(d, len(rows), hits, misses, pushes, units)
        print(f"[grader] {d}: {hits}-{misses}-{pushes} ({units:+.2f}u)")

    if notify and graded_dates:
        report = cards.grade_report((date.today() - timedelta(days=1)).isoformat())
        if report:
            print(report) if dry_run else send_message(report)


if __name__ == "__main__":
    run(notify="--notify" in sys.argv, dry_run="--dry-run" in sys.argv)
    sys.exit(0)

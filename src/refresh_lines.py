"""2:00 PM ET heartbeat — re-fetch K lines, track movement, alert on big moves.

Movement is measured against the line captured at pick time (first
line_history row). Alerts fire when |move| >= thresholds.line_move_alert.
"""
from __future__ import annotations

import sys
from datetime import date

import db
from config import formula, today_et
from fetchers import odds_api
from telegram_bot import send_message


def check_lines(stage: str, only_pregame: bool = False,
                dry_run: bool = False) -> list[str]:
    db.init_db()
    if not odds_api.configured():
        print("[lines] ODDS_API_KEY not set — skipping.")
        return []

    picks = [p for p in db.picks_for_date(today_et().isoformat())
             if p["result"] == "PENDING" and p["book_line"] is not None]
    if only_pregame:
        picks = [p for p in picks if not p["pregame_checked"]]
    if not picks:
        print(f"[{stage}] no active picks to check.")
        return []

    markets = {p["pick_type"] for p in picks}
    lines: dict[str, dict] = {}
    if "K" in markets:
        lines.update(odds_api.lines_for_slate(odds_api.K_MARKET))
    if "HR" in markets:
        lines.update(odds_api.lines_for_slate(odds_api.HR_MARKET))

    threshold = formula()["thresholds"].get("line_move_alert", 0.5)
    alerts = []
    for p in picks:
        rec = lines.get(odds_api.norm_name(p["pitcher_name"]))
        if not rec or rec.get("line") is None:
            continue
        new_line = rec["line"]
        _, price = odds_api.best_price_for(rec, p["pick_side"] or "OVER")
        db.record_line(p["id"], new_line, price)
        if only_pregame:
            db.mark_pregame_checked(p["id"])

        if p["pick_type"] != "K":
            continue
        move = new_line - p["book_line"]
        if abs(move) < threshold:
            continue
        # Line moving in our pick's direction = market agrees; against = fade.
        toward = (move > 0) == (p["pick_side"] == "OVER")
        arrow = "📈" if move > 0 else "📉"
        verdict = "✅ market agrees" if toward else "🚩 moved AGAINST us"
        alerts.append(
            f"{arrow} <b>{p['pitcher_name']}</b> {p['pick_side']} "
            f"{p['book_line']} → line now <b>{new_line}</b> "
            f"({move:+.1f}) — {verdict}"
        )

    if alerts:
        header = ("⏱ <b>Pregame line check</b>" if only_pregame
                  else "🔄 <b>2 PM line refresh</b>")
        body = header + "\n\n" + "\n".join(alerts)
        print(body) if dry_run else send_message(body)
    else:
        print(f"[{stage}] {len(picks)} picks checked — no moves ≥ {threshold}.")
    return alerts


def main(argv=None) -> int:
    check_lines("refresh_lines", dry_run="--dry-run" in (argv or sys.argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())

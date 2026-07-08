"""Hourly pregame heartbeat — final line check ~1 hour before each first pitch.

Runs hourly through the game window; each firing only touches picks whose
first pitch is within the next ~75 minutes and that haven't been
pregame-checked yet, so the hourly cadence nets out to one check per pick.
Also enriches the ump assignment (officials post close to game time).
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from config import today_et

import db
from fetchers import umpscorecards
from refresh_lines import check_lines
from telegram_bot import send_message

WINDOW_MIN = 75


def _due(p) -> bool:
    fp = p["first_pitch_utc"]
    if not fp:
        return False
    try:
        t = datetime.fromisoformat(fp.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    return now <= t <= now + timedelta(minutes=WINDOW_MIN)


def run(dry_run: bool = False) -> None:
    db.init_db()
    pending = [p for p in db.picks_for_date(today_et().isoformat())
               if p["result"] == "PENDING" and not p["pregame_checked"]]
    due = [p for p in pending if _due(p)]
    if not due:
        print("[pregame] no picks within the pregame window — no-op.")
        return

    print(f"[pregame] {len(due)} pick(s) starting within {WINDOW_MIN} min.")
    check_lines("pregame_check", only_pregame=True, dry_run=dry_run)

    # Late ump assignments are worth a note (K factor context for live bets).
    notes = []
    for p in due:
        if p["pick_type"] != "K" or not p["game_pk"]:
            continue
        ump, factor = umpscorecards.game_ump_k_factor(p["game_pk"])
        if ump and factor != 1.0:
            lean = "K-friendly" if factor > 1.0 else "contact-leaning"
            notes.append(f"🧑‍⚖️ {p['pitcher_name']}: HP ump <b>{ump}</b> "
                         f"confirmed ({factor:.2f} — {lean})")
    if notes:
        body = "\n".join(notes)
        print(body) if dry_run else send_message(body)


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
    sys.exit(0)

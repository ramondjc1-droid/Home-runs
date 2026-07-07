"""Stage dispatcher for scheduled runs (GitHub Actions / cron / VPS).

GitHub Actions cron is best-effort and frequently delayed by 1-3 hours, so the
stage is keyed off WHICH cron line fired (--schedule "${{ github.event.schedule }}"),
never off wall-clock time. Each cron line lists both candidate UTC hours to
cover EDT/EST; per-day idempotency (db.run_log) makes duplicate fires no-ops.
The pregame stage is exempt from idempotency — it's meant to fire hourly and
self-noops when no pick starts within its window.

Usage:
  python scripts/dispatch.py --schedule "0 14,15 * * *"   # scheduled
  python scripts/dispatch.py --stage morning              # manual (forced)
  python scripts/dispatch.py                              # local: pick by ET time
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = None

# Cron line (must match .github/workflows/daily.yml exactly) -> stage script.
SCHEDULE_MAP = {
    "0 6,7 * * *": "grader.py",             # 2:00 AM ET
    "0 14,15 * * *": "morning_analysis.py", # 10:00 AM ET
    "0 18,19 * * *": "refresh_lines.py",    # 2:00 PM ET
    "0 17,20-23,0-3 * * *": "pregame_check.py",  # hourly game window
}

ALIAS = {
    "grade": "grader.py",
    "morning": "morning_analysis.py",
    "lines": "refresh_lines.py",
    "pregame": "pregame_check.py",
    "commands": "command_handler.py",
}

# Stages that may fire multiple times per day.
REPEATABLE = {"pregame_check"}

# Fallback (local cron with no --schedule): target ET time -> stage.
STAGES_BY_TIME = {
    (2, 0): "grader.py",
    (10, 0): "morning_analysis.py",
    (14, 0): "refresh_lines.py",
}
TOLERANCE_MIN = 90


def _run(script: str) -> int:
    print(f"[dispatch] running {script}")
    return subprocess.call([sys.executable, str(SRC / script)], cwd=str(SRC))


def _pick_by_time(now: datetime) -> str | None:
    now_min = now.hour * 60 + now.minute
    best, best_delta = None, TOLERANCE_MIN + 1
    for (h, m), script in STAGES_BY_TIME.items():
        delta = abs(now_min - (h * 60 + m))
        if delta <= TOLERANCE_MIN and delta < best_delta:
            best, best_delta = script, delta
    return best or "pregame_check.py"  # evenings default to the pregame check


def resolve_script(schedule: str, stage: str) -> str | None:
    if stage:
        return ALIAS.get(stage.strip().lower(), stage.strip())
    if schedule:
        return SCHEDULE_MAP.get(schedule.strip())
    now = datetime.now(ET) if ET else datetime.utcnow()
    return _pick_by_time(now)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", default="", help="the cron string that fired")
    ap.add_argument("--stage", default="",
                    help="force a stage (grade|morning|lines|pregame)")
    args = ap.parse_args(argv[1:])

    forced = bool(args.stage)
    script = resolve_script(args.schedule, args.stage)
    if not script:
        print("[dispatch] no stage matched — no-op.")
        return 0

    stage_name = script.replace(".py", "")

    import db
    db.init_db()
    if (not forced and stage_name not in REPEATABLE
            and db.already_ran_today(stage_name)):
        print(f"[dispatch] {stage_name} already ran today — skipping (idempotent).")
        return 0

    rc = _run(script)
    if rc == 0:
        db.log_run(stage_name)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))

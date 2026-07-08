"""Telegram long-poll listener: slash commands + natural-language Q&A.

Run on a machine that stays on:  python src/command_handler.py
Scheduled pushes work WITHOUT this — it's only needed for two-way chat.

Model routing: commands are plain Python ($0); only free-text questions call
the API, and they use the cheap CHAT_MODEL (Sonnet), never Opus.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta

import yaml

import costs
import db
import telegram_bot as tg
from config import (ANTHROPIC_API_KEY, CHAT_MODEL, FORMULA_PATH, formula,
                    reload_formula, today_et)

HELP = """<b>MLB K Analyst — commands</b>
/picks — today's pick card
/status — system health
/grade — yesterday's results
/week — last 7 days performance
/month — this month's performance
/history [pitcher] — all picks on a player
/tune [param.path] [value] — adjust a formula weight
/skip [team|pitcher|gamePk] — exclude from today's analysis
/add [pitcher] — force-analyze a pitcher today
Free text — ask anything about the picks"""


def _fmt_pick(p) -> str:
    if p["pick_type"] == "HR":
        line = f"💣 {p['pitcher_name']} ({p['team']}) to HR vs {p['opponent']}"
    elif p["pick_type"] == "ML":
        price = f" at {p['best_book_price']:+d}" if p["best_book_price"] else ""
        line = f"🎲 {p['team']} ML vs {p['opponent']}{price}"
    elif p["pick_type"] == "TOT":
        line = (f"🔢 {p['pitcher_name']} {p['pick_side']} "
                f"{p['book_line']} runs")
    else:
        line = (f"⚾ {p['pitcher_name']} ({p['team']}) {p['pick_side']} "
                f"{p['book_line']} Ks vs {p['opponent']}")
    line += f" — conf {p['confidence']}/10"
    if p["result"] and p["result"] != "PENDING":
        icon = {"HIT": "✅", "MISS": "❌", "PUSH": "➖", "VOID": "🚫"}.get(p["result"], "")
        line += f" → {p['result']} {icon}"
    return line


def cmd_picks() -> str:
    picks = db.picks_for_date()
    if not picks:
        return "No picks recorded today (morning run may not have fired yet)."
    out = [f"⚾ <b>Today's picks — {today_et().isoformat()}</b>", ""]
    for p in picks:
        out.append(_fmt_pick(p))
        if p["narrative"]:
            out.append(f"   <i>{p['narrative']}</i>")
    try:
        board = json.loads(db.kv_get("ml_board", "{}"))
        if board.get("date") == today_et().isoformat():
            out += ["", board["text"]]
    except (json.JSONDecodeError, KeyError):
        pass
    return "\n".join(out)


def cmd_status() -> str:
    lines = ["🩺 <b>System status</b>", ""]
    for stage in ("morning_analysis", "refresh_lines", "pregame_check", "grader"):
        lr = db.last_run(stage)
        lines.append(f"• {stage}: last ran "
                     f"{lr['created_at'][:16] + ' UTC' if lr else 'never'}")
    today_picks = db.picks_for_date()
    pending = sum(p["result"] == "PENDING" for p in today_picks)
    lines.append(f"• Today: {len(today_picks)} picks ({pending} pending)")
    lines.append(f"• {costs.status_line()}")
    quota = db.kv_get("odds_api_remaining")
    if quota:
        lines.append(f"• Odds API requests remaining: {quota}")
    lines.append("• Next heartbeats (ET): 2 AM grade · 10 AM picks · "
                 "2 PM lines · hourly pregame")
    return "\n".join(lines)


def cmd_grade() -> str:
    import cards
    report = cards.grade_report()
    return report or "Yesterday's picks aren't graded yet (grader runs 2 AM ET)."


def _perf(days: int, label: str) -> str:
    since = (today_et() - timedelta(days=days)).isoformat()
    perf = db.performance_since(since)
    if not perf["n"]:
        return f"No graded picks in the last {label}."
    decided = perf["hits"] + perf["misses"]
    rate = perf["hits"] / decided * 100 if decided else 0
    return (f"📊 <b>Last {label}</b>: {perf['hits']}-{perf['misses']}"
            f"-{perf['pushes']} · {perf['units']:+.2f}u · {rate:.1f}% hit rate")


def cmd_history(name: str) -> str:
    if not name:
        return "Usage: /history [pitcher name]"
    picks = db.picks_for_player(name)
    if not picks:
        return f"No picks found for '{name}'."
    out = [f"📜 <b>History — {name}</b>", ""]
    for p in picks[:20]:
        out.append(f"{p['date']}: " + _fmt_pick(p))
    return "\n".join(out)


def _get_path(cfg: dict, path: list[str]):
    node = cfg
    for k in path:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def cmd_tune(args: str, reason: str = "manual /tune") -> str:
    parts = args.split()
    if len(parts) != 2:
        return ("Usage: /tune [param.path] [value]\n"
                "e.g. /tune formula.recent_form_weight 0.65")
    path_str, raw = parts
    path = path_str.split(".")
    with open(FORMULA_PATH) as fh:
        cfg = yaml.safe_load(fh)
    old = _get_path(cfg, path)
    if old is None:
        return f"Unknown parameter: {path_str}"
    try:
        value = type(old)(raw) if not isinstance(old, bool) else raw.lower() in ("1", "true", "yes")
    except (TypeError, ValueError):
        return f"Can't parse '{raw}' as {type(old).__name__}"
    node = cfg
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    with open(FORMULA_PATH, "w") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True)
    reload_formula()
    db.log_formula_change(path_str, str(old), str(value), reason)
    return f"✅ Tuned <code>{path_str}</code>: {old} → {value} (logged)"


def cmd_skip(label: str) -> str:
    if not label:
        return "Usage: /skip [team abbrev | pitcher name | gamePk]"
    db.add_skip(label)
    return f"⏭ Skipping '{label}' for today's analysis."


def cmd_add(player: str) -> str:
    if not player:
        return "Usage: /add [pitcher name]"
    db.add_force(player)
    return (f"➕ '{player}' will be force-analyzed in the next run "
            "(bypasses the min-starts gate).")


def answer_freeform(question: str) -> str:
    """Natural-language Q&A over the pick record — CHAT_MODEL (Sonnet) only."""
    if not ANTHROPIC_API_KEY:
        return "Free-text Q&A needs ANTHROPIC_API_KEY. Try /picks or /status."
    recent = []
    for d_off in range(0, 7):
        d = (today_et() - timedelta(days=d_off)).isoformat()
        for p in db.picks_for_date(d):
            recent.append({k: p[k] for k in p.keys()
                           if k not in ("narrative", "metrics_json")})
            if p["metrics_json"]:
                try:
                    recent[-1]["metrics"] = json.loads(p["metrics_json"])
                except json.JSONDecodeError:
                    pass
    season = db.season_performance()
    context = (f"Current formula config:\n{yaml.safe_dump(formula())}\n"
               f"Season record: {season}\n"
               f"Picks (last 7 days): {json.dumps(recent, default=str)}")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=CHAT_MODEL,
            max_tokens=500,
            system=("You are the MLB K Analyst bot. Answer the user's question "
                    "using ONLY the pick records and config provided. Be concise "
                    "and specific with numbers. Plain text, no markdown."),
            messages=[{"role": "user",
                       "content": f"{context}\n\nQuestion: {question}"}],
        )
        costs.track_usage(CHAT_MODEL, msg.usage)
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        return "".join(parts).strip() or "No answer generated."
    except Exception as exc:
        return f"Q&A failed: {exc}"


def handle(text: str) -> str:
    text = text.strip()
    low = text.lower()
    if low.startswith("/start") or low.startswith("/help"):
        return HELP
    if low.startswith("/picks"):
        return cmd_picks()
    if low.startswith("/status"):
        return cmd_status()
    if low.startswith("/grade"):
        return cmd_grade()
    if low.startswith("/week"):
        return _perf(7, "7 days")
    if low.startswith("/month"):
        return _perf(today_et().day, "month")
    if low.startswith("/history"):
        return cmd_history(text[len("/history"):].strip())
    if low.startswith("/tune"):
        return cmd_tune(text[len("/tune"):].strip())
    if low.startswith("/skip"):
        return cmd_skip(text[len("/skip"):].strip())
    if low.startswith("/add"):
        return cmd_add(text[len("/add"):].strip())
    # "tune X to Y" natural phrasing
    if low.startswith("tune ") and " to " in low:
        param, _, value = text[5:].partition(" to ")
        return cmd_tune(f"{param.strip()} {value.strip()}")
    return answer_freeform(text)


def main() -> None:
    db.init_db()
    if not tg.configured():
        print("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return
    print("[commands] long-polling for messages… Ctrl-C to stop.")
    offset = None
    while True:
        try:
            for upd in tg.get_updates(offset=offset):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = msg.get("text")
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if not text or chat_id != str(tg.TELEGRAM_CHAT_ID):
                    continue
                print(f"[commands] {text!r}")
                tg.send_message(handle(text), chat_id=chat_id)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[commands] loop error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()

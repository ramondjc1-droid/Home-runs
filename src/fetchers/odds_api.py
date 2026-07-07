"""The Odds API — pitcher strikeout and batter home-run prop lines.

Player props are per-event endpoints, so each slate costs roughly
(1 events call + N event-odds calls). At 3 refreshes/day on a 15-game slate
that's ~50 requests/day — comfortably inside a 20k/month quota. The remaining
quota (x-requests-remaining header) is stored in the kv table for /status.
"""
from __future__ import annotations

import unicodedata
from typing import Optional

import requests

import db
from config import ODDS_API_KEY
from fetchers import log_error

BASE = "https://api.the-odds-api.com/v4"
SPORT = "baseball_mlb"
K_MARKET = "pitcher_strikeouts"
HR_MARKET = "batter_home_runs"


def configured() -> bool:
    return bool(ODDS_API_KEY)


def norm_name(name: str) -> str:
    """Normalize player names for matching across MLB API and books."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return s.lower().replace(".", "").replace("-", " ").strip()


def _get(path: str, params: dict) -> Optional[list | dict]:
    if not ODDS_API_KEY:
        return None
    params = {"apiKey": ODDS_API_KEY, **params}
    try:
        r = requests.get(f"{BASE}/{path}", params=params, timeout=20)
        remaining = r.headers.get("x-requests-remaining")
        if remaining is not None:
            db.kv_set("odds_api_remaining", remaining)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log_error("odds_api", f"{path}: {exc}")
        return None


def events() -> list[dict]:
    """Upcoming/live MLB events: [{id, commence_time, home_team, away_team}]."""
    return _get(f"sports/{SPORT}/events", {}) or []


def event_props(event_id: str, market: str) -> Optional[dict]:
    return _get(f"sports/{SPORT}/events/{event_id}/odds", {
        "regions": "us", "markets": market, "oddsFormat": "american",
    })


def _collect_player_lines(event: dict, market_key: str) -> dict:
    """{norm_player_name: {line, books:[{book, line, over_price, under_price}]}}

    line is the median (consensus) point across books.
    """
    out: dict[str, dict] = {}
    for bm in event.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market.get("key") != market_key:
                continue
            per_player: dict[str, dict] = {}
            for oc in market.get("outcomes", []):
                player = oc.get("description") or ""
                if not player:
                    continue
                entry = per_player.setdefault(player, {"book": bm.get("title", bm.get("key"))})
                if oc.get("point") is not None:
                    entry["line"] = float(oc["point"])
                side = (oc.get("name") or "").lower()
                if side == "over":
                    entry["over_price"] = int(oc.get("price") or 0)
                elif side == "under":
                    entry["under_price"] = int(oc.get("price") or 0)
                elif side == "yes":  # batter_home_runs is often Yes/No
                    entry["over_price"] = int(oc.get("price") or 0)
                    entry.setdefault("line", 0.5)
                elif side == "no":
                    entry["under_price"] = int(oc.get("price") or 0)
            for player, entry in per_player.items():
                out.setdefault(norm_name(player), {"player": player, "books": []})
                out[norm_name(player)]["books"].append(entry)

    for rec in out.values():
        lines = sorted(b["line"] for b in rec["books"] if "line" in b)
        rec["line"] = lines[len(lines) // 2] if lines else None
    return out


def _best_price(rec: dict, side: str, line: Optional[float]) -> tuple[Optional[str], Optional[int]]:
    """Best (most favorable) price for our side at the consensus line."""
    key = "over_price" if side == "OVER" else "under_price"
    best_book, best = None, None
    for b in rec.get("books", []):
        if line is not None and b.get("line") is not None and b["line"] != line:
            continue
        price = b.get(key)
        if price is None:
            continue
        if best is None or price > best:
            best, best_book = price, b.get("book")
    return best_book, best


def lines_for_slate(market: str = K_MARKET) -> dict:
    """All player prop lines for today's slate, keyed by normalized name.

    {norm_name: {player, line, books, event_id, commence_time,
                 home_team, away_team}}
    """
    out: dict[str, dict] = {}
    for ev in events():
        detail = event_props(ev["id"], market)
        if not detail:
            continue
        players = _collect_player_lines(detail, market)
        for k, rec in players.items():
            rec.update({
                "event_id": ev["id"],
                "commence_time": ev.get("commence_time"),
                "home_team": ev.get("home_team"),
                "away_team": ev.get("away_team"),
            })
            out[k] = rec
    return out


def best_price_for(rec: dict, side: str) -> tuple[Optional[str], Optional[int]]:
    return _best_price(rec, side, rec.get("line"))


def implied_prob(american: int) -> float:
    """Implied win probability from an American price (vig included)."""
    if american < 0:
        return -american / (-american + 100.0)
    return 100.0 / (american + 100.0)

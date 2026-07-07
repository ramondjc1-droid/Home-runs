"""MLB Stats API (official, free, keyless) — the primary data source.

Covers: today's slate + probable pitchers, pitcher K% (season and last-30d
via game logs), innings per start, opponent team K% over a recent window,
batter HR rates, HR leaders, home-plate umpire assignment, and box scores
for grading. statsapi.mlb.com is far more reliable than scraping, so
everything that CAN come from here does; FanGraphs/Savant only enrich.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from fetchers import get_with_retry, log_error

BASE = "https://statsapi.mlb.com/api/v1"


@dataclass
class Game:
    game_pk: int
    game_date_utc: str            # ISO timestamp of first pitch
    venue: str
    home_team: str                # abbreviation
    away_team: str
    home_team_id: int
    away_team_id: int
    home_pitcher: Optional[dict]  # {"id": int, "name": str} or None
    away_pitcher: Optional[dict]
    status: str = ""
    home_team_name: str = ""      # full name, for matching odds events
    away_team_name: str = ""
    extras: dict = field(default_factory=dict)


def _get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    r = get_with_retry(f"{BASE}/{path}", params=params)
    return r.json() if r is not None else None


def todays_slate(d: Optional[str] = None) -> list[Game]:
    """All games for a date with probable pitchers and venues."""
    d = d or date.today().isoformat()
    data = _get("schedule", {
        "sportId": 1, "date": d,
        "hydrate": "probablePitcher,team,venue",
    })
    if not data:
        return []
    games: list[Game] = []
    for day in data.get("dates", []):
        for g in day.get("games", []):
            home = g["teams"]["home"]
            away = g["teams"]["away"]

            def _pp(side: dict) -> Optional[dict]:
                pp = side.get("probablePitcher")
                return {"id": pp["id"], "name": pp["fullName"]} if pp else None

            games.append(Game(
                game_pk=g["gamePk"],
                game_date_utc=g.get("gameDate", ""),
                venue=(g.get("venue") or {}).get("name", ""),
                home_team=(home["team"].get("abbreviation")
                           or home["team"]["name"]),
                away_team=(away["team"].get("abbreviation")
                           or away["team"]["name"]),
                home_team_id=home["team"]["id"],
                away_team_id=away["team"]["id"],
                home_pitcher=_pp(home),
                away_pitcher=_pp(away),
                status=(g.get("status") or {}).get("abstractGameState", ""),
                home_team_name=home["team"].get("name", ""),
                away_team_name=away["team"].get("name", ""),
            ))
    return games


# --- pitcher rates --------------------------------------------------------------

def _ip_to_float(ip: str | float) -> float:
    """MLB innings notation: '6.1' = 6⅓, '6.2' = 6⅔."""
    try:
        s = str(ip)
        whole, _, frac = s.partition(".")
        return int(whole or 0) + {"1": 1 / 3, "2": 2 / 3}.get(frac, 0.0)
    except Exception:
        return 0.0


def pitcher_season_stats(pid: int, season: Optional[int] = None) -> Optional[dict]:
    season = season or date.today().year
    data = _get(f"people/{pid}/stats",
                {"stats": "season", "group": "pitching", "season": season})
    try:
        return data["stats"][0]["splits"][0]["stat"]
    except (KeyError, IndexError, TypeError):
        return None


def pitcher_game_log(pid: int, season: Optional[int] = None) -> list[dict]:
    season = season or date.today().year
    data = _get(f"people/{pid}/stats",
                {"stats": "gameLog", "group": "pitching", "season": season})
    try:
        return data["stats"][0]["splits"]
    except (KeyError, IndexError, TypeError):
        return []


def pitcher_k_profile(pid: int, season: Optional[int] = None) -> Optional[dict]:
    """Everything the K projection needs for one pitcher.

    Returns {k_pct_season, k_pct_30d, ip_last5_mean, starts, hr_per_bf} or None.
    """
    stats = pitcher_season_stats(pid, season)
    if not stats:
        return None
    bf = int(stats.get("battersFaced") or 0)
    so = int(stats.get("strikeOuts") or 0)
    starts = int(stats.get("gamesStarted") or 0)
    hr = int(stats.get("homeRuns") or 0)
    bb = int(stats.get("baseOnBalls") or 0)
    if bf == 0:
        return None

    logs = pitcher_game_log(pid, season)
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    so30 = bf30 = 0
    recent_ips: list[float] = []
    for split in reversed(logs):  # newest last in API order; walk newest-first
        st = split.get("stat", {})
        if split.get("date", "") >= cutoff:
            so30 += int(st.get("strikeOuts") or 0)
            bf30 += int(st.get("battersFaced") or 0)
        if len(recent_ips) < 5 and int(st.get("gamesStarted") or 0) > 0:
            recent_ips.append(_ip_to_float(st.get("inningsPitched", 0)))

    k_season = so / bf
    k_30 = (so30 / bf30) if bf30 >= 20 else k_season  # small-sample guard
    ip5 = sum(recent_ips) / len(recent_ips) if recent_ips else \
        _ip_to_float(stats.get("inningsPitched", 0)) / max(starts, 1)
    return {
        "k_pct_season": k_season,
        "k_pct_30d": k_30,
        "bf_30d": bf30,
        "ip_last5_mean": ip5,
        "starts": starts,
        "hr_per_bf": hr / bf,
        "kbb_pct": (so - bb) / bf,   # K-BB%, the starter-quality shorthand
    }


# --- team hitting (opponent K%) ---------------------------------------------------

def team_k_pct_recent(team_id: int, days: int = 15,
                      season: Optional[int] = None) -> Optional[float]:
    """Team strikeout rate over the last ~N calendar days (≈ last 15 games)."""
    season = season or date.today().year
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    data = _get(f"teams/{team_id}/stats", {
        "stats": "byDateRange", "group": "hitting", "season": season,
        "startDate": start, "endDate": end,
    })
    try:
        st = data["stats"][0]["splits"][0]["stat"]
        pa = int(st.get("plateAppearances") or 0)
        so = int(st.get("strikeOuts") or 0)
        return so / pa if pa >= 100 else None
    except (KeyError, IndexError, TypeError):
        return None


# --- team records (moneyline module) -----------------------------------------------

def standings(season: Optional[int] = None) -> Optional[dict]:
    """{team_id: {w, l, rs, ra}} from the regular-season standings."""
    season = season or date.today().year
    data = _get("standings", {
        "leagueId": "103,104", "season": season,
        "standingsTypes": "regularSeason",
    })
    if not data:
        return None
    out: dict[int, dict] = {}
    for rec in data.get("records", []):
        for tr in rec.get("teamRecords", []):
            out[tr["team"]["id"]] = {
                "w": int(tr.get("wins") or 0),
                "l": int(tr.get("losses") or 0),
                "rs": int(tr.get("runsScored") or 0),
                "ra": int(tr.get("runsAllowed") or 0),
            }
    return out or None


def game_winner(game_pk: int) -> Optional[int]:
    """Winning team_id for a final game, else None."""
    sched = _get("schedule", {"sportId": 1, "gamePk": game_pk})
    try:
        game = sched["dates"][0]["games"][0]
        for side in ("home", "away"):
            t = game["teams"][side]
            if t.get("isWinner"):
                return t["team"]["id"]
    except (KeyError, IndexError, TypeError):
        pass
    return None


# --- batters (HR module) ----------------------------------------------------------

def team_hr_leaders(team_id: int, season: Optional[int] = None,
                    limit: int = 5) -> list[dict]:
    season = season or date.today().year
    data = _get(f"teams/{team_id}/leaders", {
        "leaderCategories": "homeRuns", "season": season, "limit": limit,
    })
    out = []
    try:
        for leader in data["teamLeaders"][0]["leaders"]:
            out.append({"id": leader["person"]["id"],
                        "name": leader["person"]["fullName"],
                        "hr": int(leader.get("value") or 0)})
    except (KeyError, IndexError, TypeError):
        pass
    return out


def batter_hr_profile(pid: int, season: Optional[int] = None) -> Optional[dict]:
    """{hr_pa_season, hr_pa_30d, pa_season} for one batter."""
    season = season or date.today().year
    data = _get(f"people/{pid}/stats",
                {"stats": "season", "group": "hitting", "season": season})
    try:
        st = data["stats"][0]["splits"][0]["stat"]
        pa = int(st.get("plateAppearances") or 0)
        hr = int(st.get("homeRuns") or 0)
    except (KeyError, IndexError, TypeError):
        return None
    if pa == 0:
        return None

    start = (date.today() - timedelta(days=30)).isoformat()
    recent = _get(f"people/{pid}/stats", {
        "stats": "byDateRange", "group": "hitting", "season": season,
        "startDate": start, "endDate": date.today().isoformat(),
    })
    hr_pa_30 = None
    try:
        st30 = recent["stats"][0]["splits"][0]["stat"]
        pa30 = int(st30.get("plateAppearances") or 0)
        if pa30 >= 40:
            hr_pa_30 = int(st30.get("homeRuns") or 0) / pa30
    except (KeyError, IndexError, TypeError):
        pass

    hr_pa_season = hr / pa
    return {"hr_pa_season": hr_pa_season,
            "hr_pa_30d": hr_pa_30 if hr_pa_30 is not None else hr_pa_season,
            "pa_season": pa}


# --- officials / grading -----------------------------------------------------------

def home_plate_umpire(game_pk: int) -> Optional[str]:
    """HP umpire name if assigned (usually only posted close to game time)."""
    r = get_with_retry(
        f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live",
        params={"fields": "liveData,boxscore,officials,official,fullName,officialType"})
    if r is None:
        return None
    try:
        officials = r.json()["liveData"]["boxscore"]["officials"]
        for o in officials:
            if o.get("officialType") == "Home Plate":
                return o["official"]["fullName"]
    except (KeyError, TypeError):
        pass
    return None


def boxscore_player_stats(game_pk: int) -> Optional[dict]:
    """Full boxscore player map, or None if unavailable."""
    data = _get(f"game/{game_pk}/boxscore")
    if not data:
        return None
    players = {}
    for side in ("home", "away"):
        players.update((data.get("teams", {}).get(side, {}) or {}).get("players", {}))
    return players or None


def game_final(game_pk: int) -> bool:
    data = _get(f"game/{game_pk}/linescore")
    if data is None:
        return False
    sched = _get("schedule", {"sportId": 1, "gamePk": game_pk})
    try:
        st = sched["dates"][0]["games"][0]["status"]["abstractGameState"]
        return st == "Final"
    except (KeyError, IndexError, TypeError):
        return False


def actual_strikeouts(game_pk: int, pitcher_id: int) -> Optional[int]:
    players = boxscore_player_stats(game_pk)
    if not players:
        return None
    p = players.get(f"ID{pitcher_id}")
    if not p:
        return None
    try:
        return int(p["stats"]["pitching"]["strikeOuts"])
    except (KeyError, TypeError, ValueError):
        return None


def actual_home_runs(game_pk: int, batter_id: int) -> Optional[int]:
    players = boxscore_player_stats(game_pk)
    if not players:
        return None
    p = players.get(f"ID{batter_id}")
    if not p:
        return None
    try:
        return int(p["stats"]["batting"]["homeRuns"])
    except (KeyError, TypeError, ValueError):
        return None
